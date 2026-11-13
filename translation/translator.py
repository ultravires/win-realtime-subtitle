import torch
import threading
import queue
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
            
            # 加载模型和分词器
            self.tokenizer = AutoTokenizer.from_pretrained(
                model_name, 
                trust_remote_code=True
            )
            
            # 特殊处理腾讯模型可能需要的额外参数
            if "HY-MT" in model_name:
                model_kwargs.pop('quantization_config', None)  # 移除可能不兼容的参数
                model_kwargs['torch_dtype'] = torch.float16
                logger.info("针对腾讯HY模型使用特殊加载配置")
            
            self.model = AutoModelForCausalLM.from_pretrained(
                model_name,
                **model_kwargs
            )
            
            logger.info("翻译模型加载完成")
            
        except Exception as e:
            logger.error(f"翻译模型加载失败: {e}")
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
        """翻译主循环"""
        while self.running:
            try:
                # 获取待翻译文本
                text = text_queue.get(timeout=1.0)
                if not text:
                    continue
                
                # 执行多语言翻译
                translations = self.translate_text(text, self.target_languages)
                
                # 构造翻译结果
                translation_result = {
                    'original': text,
                    'translations': translations,
                    'timestamp': self._get_current_timestamp()
                }
                
                logger.info(f"翻译完成: {text} -> {list(translations.values())}")
                
                # 放入翻译结果队列
                try:
                    translation_queue.put(translation_result, timeout=0.1)
                except queue.Full:
                    logger.warning("翻译队列已满，丢弃结果")
                
            except queue.Empty:
                continue
            except Exception as e:
                logger.error(f"翻译过程中发生错误: {e}")
                continue
    
    def translate_text(self, text: str, target_languages: List[str]) -> Dict[str, str]:
        """翻译文本到多种语言"""
        translations = {}
        
        for lang in target_languages:
            try:
                translated = self._translate_single(text, lang)
                if translated:
                    translations[lang] = translated
            except Exception as e:
                logger.error(f"{lang}翻译失败: {e}")
                translations[lang] = ""
        
        return translations
    
    def _translate_single(self, text: str, target_lang: str) -> str:
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
                max_new_tokens=self.config['translation']['max_new_tokens'],
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