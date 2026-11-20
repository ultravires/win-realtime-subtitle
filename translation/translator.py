import torch
import threading
import queue
import time
from typing import Dict, List, Optional
from transformers import AutoTokenizer, AutoModelForCausalLM
from loguru import logger

class TencentTranslator:
    """腾讯HY-MT1.5翻译器"""
    
    def __init__(self, config: dict):
        self.config = config
        self.tokenizer = None
        self.model = None
        self.device = config['translation']['device_map']
        self.target_languages = config['translation']['target_languages']
        self.running = False
        self.translate_thread: Optional[threading.Thread] = None

        # 批量合并翻译：翻译一条小句约 2s（生成耗时），说话节奏快于翻译时
        # 会在 text_queue 积压，译文越落越后。把队列里已积压的小句合并成
        # 一批、一次生成完成整批翻译（合并调用摊薄固定开销，输出吞吐不变）
        tcfg = config['translation']
        self.max_new_tokens = int(tcfg.get('max_new_tokens', 100))
        self.batch_max_items = int(tcfg.get('batch_max_items', 6))
        self.batch_max_new_tokens = int(tcfg.get('batch_max_new_tokens', 512))
        
        # 语言映射表
        self.language_names = {
            'en': '英语',
            'ja': '日语',
            'ko': '韩语',
            'fr': '法语',
            'es': '西班牙语',
            'de': '德语',
            'ru': '俄语',
            'it': '意大利语',
            'pt': '葡萄牙语',
            'ar': '阿拉伯语'
        }
    
    def load_model(self):
        """加载翻译模型"""
        try:
            model_name = self.config['translation']['model_name']
            quantization = self.config['translation']['quantization']
            
            logger.info(f"正在加载翻译模型: {model_name}")
            logger.info(f"量化设置: {quantization}")
            
            # 准备模型加载参数
            model_kwargs = {
                'torch_dtype': torch.float16,
                'device_map': self.device,
                'trust_remote_code': True
            }
            
            # 应用量化设置 - 使用兼容的方法
            if quantization == "4bit":
                try:
                    # 尝试使用bitsandbytes量化
                    from transformers import BitsAndBytesConfig
                    quantization_config = BitsAndBytesConfig(
                        load_in_4bit=True,
                        bnb_4bit_compute_dtype=torch.float16,
                        bnb_4bit_use_double_quant=True,
                        bnb_4bit_quant_type="nf4"
                    )
                    model_kwargs['quantization_config'] = quantization_config
                    logger.info("使用BitsAndBytes 4bit量化")
                except ImportError:
                    logger.warning("bitsandbytes未安装，使用float16精度")
                    model_kwargs['torch_dtype'] = torch.float16
            elif quantization == "8bit":
                try:
                    from transformers import BitsAndBytesConfig
                    quantization_config = BitsAndBytesConfig(
                        load_in_8bit=True,
                        bnb_8bit_compute_dtype=torch.float16
                    )
                    model_kwargs['quantization_config'] = quantization_config
                    logger.info("使用BitsAndBytes 8bit量化")
                except ImportError:
                    logger.warning("bitsandbytes未安装，使用float16精度")
                    model_kwargs['torch_dtype'] = torch.float16
            
            # 加载分词器（本地缓存优先：联网探测 huggingface.co 可能卡住数十秒到数分钟）
            t_tok = time.time()
            logger.info("正在加载分词器（本地缓存优先）...")
            try:
                self.tokenizer = AutoTokenizer.from_pretrained(
                    model_name,
                    trust_remote_code=True,
                    local_files_only=True
                )
            except Exception:
                logger.warning("分词器本地缓存未命中，尝试联网获取...")
                self.tokenizer = AutoTokenizer.from_pretrained(
                    model_name,
                    trust_remote_code=True
                )
            logger.info(f"分词器加载完成，耗时 {time.time() - t_tok:.1f}s")
            
            # 特殊处理腾讯模型可能需要的额外参数
            if "HY-MT" in model_name:
                if quantization in ("4bit", "8bit"):
                    # 显式配置了量化：尊重配置（8bit 约 7.5GB，显存紧张时避免溢出到共享内存变慢 10 倍）
                    logger.info(f"HY-MT 使用 {quantization} 量化加载")
                else:
                    model_kwargs.pop('quantization_config', None)
                    model_kwargs['torch_dtype'] = torch.float16
                    logger.info("针对腾讯HY模型使用特殊加载配置")
            
            t_model = time.time()
            logger.info("正在加载模型权重（fp16 约 15GB，需 30 秒到数分钟，取决于磁盘速度；期间无输出属正常）...")
            try:
                self.model = AutoModelForCausalLM.from_pretrained(
                    model_name,
                    local_files_only=True,
                    **model_kwargs
                )
            except Exception:
                logger.warning("模型权重本地缓存未命中，尝试联网获取...")
                self.model = AutoModelForCausalLM.from_pretrained(
                    model_name,
                    **model_kwargs
                )
            logger.info(f"翻译模型加载完成，耗时 {time.time() - t_model:.1f}s")

            # 预热：首条字幕不应背上生成内核的冷启动开销
            try:
                t0 = time.time()
                self.translate_text("你好。", ['en'])
                logger.info(f"翻译模型预热完成，耗时 {time.time() - t0:.1f}s")
            except Exception as e:
                logger.warning(f"翻译预热失败（不影响运行）: {e}")
            
        except Exception as e:
            # 打印完整 traceback 与 transformers 实际加载位置：
            # PyInstaller 打包后 transformers 懒加载注册表的失败会把底层异常
            # 包装成一条误导性消息（"Could not import module ..."），必须看全栈
            import traceback
            import transformers as _tf
            logger.error(f"翻译模型加载失败: {e}")
            logger.error(f"transformers 加载自: {getattr(_tf, '__file__', '?')}")
            logger.error(f"完整 traceback:\n{traceback.format_exc()}")
            raise
    
    def start_translation(self, text_queue: queue.Queue, translation_queue: queue.Queue):
        """开始翻译服务"""
        if self.running:
            logger.warning("翻译服务已在运行")
            return
            
        if self.model is None:
            self.load_model()
            
        self.running = True
        self.translate_thread = threading.Thread(
            target=self._translate_loop,
            args=(text_queue, translation_queue),
            daemon=True
        )
        self.translate_thread.start()
        logger.info("翻译服务已启动")
    
    def stop_translation(self):
        """停止翻译服务"""
        self.running = False
        if self.translate_thread:
            self.translate_thread.join(timeout=2)
        logger.info("翻译服务已停止")
    
    def _translate_loop(self, text_queue: queue.Queue, translation_queue: queue.Queue):
        """翻译主循环：单条取出 + 积压合并成批翻译"""
        while self.running:
            try:
                # 获取待翻译文本（兼容纯文本与带元数据的 dict 两种格式）
                item = text_queue.get(timeout=1.0)
                if not item:
                    continue
            except queue.Empty:
                continue
            except Exception as e:
                logger.error(f"取待翻译文本失败: {e}")
                continue

            try:
                batch = self._collect_batch(item, text_queue)
                self._translate_batch(batch, translation_queue)
            except queue.Empty:
                continue
            except Exception as e:
                logger.error(f"翻译过程中发生错误: {e}")
                continue

    def _collect_batch(self, first: object, text_queue: queue.Queue) -> List[object]:
        """取出首批 + 队列中已积压的小句，合并为一批（上限 batch_max_items）。

        无积压时立即返回单条（不额外等待、不增加延迟）；有积压时一次
        get_nowait 循环全部取出，整批一次生成完成翻译。超出上限的部分
        留在队列里下一轮继续（配合转写端"保新弃旧"，积压有硬上限）。
        """
        batch = [first]
        while len(batch) < self.batch_max_items:
            try:
                nxt = text_queue.get_nowait()
            except queue.Empty:
                break
            if nxt:
                batch.append(nxt)
        if len(batch) > 1:
            logger.info(f"翻译积压合并: {len(batch)} 条小句一次生成")
        return batch

    def _translate_batch(self, batch: List[object], translation_queue: queue.Queue):
        """翻译一批小句：合并文本单次生成（每个目标语言一次调用），
        构造一条合并结果事件；分段模式下显示端把整批译文一次追加进缓冲区"""
        texts = []
        seg_flags = []
        first_tss = []
        for it in batch:
            if isinstance(it, dict):
                texts.append(str(it.get('text') or ''))
                seg_flags.append(bool(it.get('seg')))
                ts = it.get('first_timestamp')
                if ts is not None:
                    first_tss.append(ts)
            else:
                texts.append(str(it))
                seg_flags.append(False)
        texts = [t for t in texts if t.strip()]
        if not texts:
            return

        merged_text = '\n'.join(texts)
        # 分段模式（seg=True）：原文已由转写端直推显示端上屏，这里只发译文
        # 事件，显示端把整批译文追加进译文缓冲区；整句模式沿用合并格式
        seg_mode = any(seg_flags)
        # 输出上限随批内条数放大（每条约需 max_new_tokens 的生成预算），
        # 封顶 batch_max_new_tokens 防止单批生成过长拖慢整体节奏
        max_new = min(self.max_new_tokens * len(texts), self.batch_max_new_tokens)
        translations = self.translate_text(merged_text, self.target_languages,
                                           max_new_tokens=max_new)

        if seg_mode:
            translation_result = {
                'type': 'seg_translation',
                'translations': translations,
                'original': merged_text,
                'timestamp': self._get_current_timestamp()
            }
        else:
            translation_result = {
                'original': merged_text,
                'translations': translations,
                'timestamp': self._get_current_timestamp()
            }
        if first_tss:
            translation_result['first_timestamp'] = min(first_tss)

        logger.info(f"翻译完成 ({len(texts)} 条合并): "
                    f"{merged_text[:40]}{'…' if len(merged_text) > 40 else ''} "
                    f"-> {list(translations.values())}")

        # 放入翻译结果队列
        try:
            translation_queue.put(translation_result, timeout=0.1)
        except queue.Full:
            logger.warning("翻译队列已满，丢弃结果")

    def translate_text(self, text: str, target_languages: List[str],
                       max_new_tokens: int = None) -> Dict[str, str]:
        """翻译文本到多种语言（max_new_tokens 为空时用配置默认值）"""
        translations = {}

        for lang in target_languages:
            try:
                translated = self._translate_single(text, lang, max_new_tokens)
                if translated:
                    translations[lang] = translated
            except Exception as e:
                logger.error(f"{lang}翻译失败: {e}")
                translations[lang] = ""

        return translations

    def _translate_single(self, text: str, target_lang: str,
                          max_new_tokens: int = None) -> str:
        """翻译到单个语言（使用HY-MT官方提示词格式）"""
        try:
            # HY-MT官方格式：将下面的文本翻译成{目标语言}，不要额外解释。{原文}
            lang_name = self.language_names.get(target_lang, target_lang)
            prompt_text = f"将下面的文本翻译成{lang_name}，不要额外解释。{text}"

            messages = [{"role": "user", "content": prompt_text}]

            # return_dict=True 统一返回 BatchEncoding，跨版本行为一致（已实测验证）
            inputs = self.tokenizer.apply_chat_template(
                messages,
                add_generation_prompt=True,
                tokenize=True,
                return_tensors="pt",
                return_dict=True
            ).to(self.model.device)

            # 生成翻译
            outputs = self.model.generate(
                **inputs,
                max_new_tokens=max_new_tokens or self.max_new_tokens,
                do_sample=False,
                pad_token_id=self.tokenizer.pad_token_id,
                eos_token_id=self.tokenizer.eos_token_id
            )

            # 只解码新生成的部分，避免提示词回显污染结果
            input_len = inputs['input_ids'].shape[-1]
            generated = outputs[0][input_len:]
            translated_text = self.tokenizer.decode(generated, skip_special_tokens=True).strip()

            if not translated_text:
                logger.warning(f"({target_lang}) 生成结果为空，原文: {text[:30]}")
                return ""

            # 简单后处理
            translated_text = self._post_process_translation(translated_text, target_lang)

            return translated_text

        except Exception as e:
            logger.error(f"单语种翻译失败 ({target_lang}): {e}")
            return ""
    
    def _post_process_translation(self, text: str, lang: str) -> str:
        """翻译结果后处理"""
        # 移除可能的重复内容
        if '|' in text:
            text = text.split('|')[0].strip()
        
        # 移除开头的标点符号
        import re
        text = re.sub(r'^[^\w\u4e00-\u9fff]+', '', text)
        
        # 限制长度
        max_length = 100
        if len(text) > max_length:
            text = text[:max_length] + "..."
            
        return text.strip()
    
    def _get_current_timestamp(self) -> float:
        """获取当前时间戳"""
        import time
        return time.time()
    
    def translate_sync(self, text: str, target_lang: str = "en") -> str:
        """同步翻译方法（用于测试）"""
        if self.model is None:
            self.load_model()
        return self._translate_single(text, target_lang)