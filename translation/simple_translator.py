import torch
import threading
import queue
from typing import Dict, List, Optional
from transformers import AutoTokenizer, AutoModelForSeq2SeqLM
from loguru import logger

class SimpleTranslator:
    """简化版翻译器 - 使用更稳定的模型"""
    
    def __init__(self, config: dict):
        self.config = config
        self.tokenizer = None
        self.model = None
        self.device = config['translation']['device_map']
        self.target_languages = config['translation']['target_languages']
        self.running = False
        self.translate_thread: Optional[threading.Thread] = None
        
        # 语言代码映射
        self.lang_codes = {
            'en': 'en',
            'ja': 'ja', 
            'ko': 'ko',
            'fr': 'fr',
            'es': 'es',
            'de': 'de'
        }
    
    def load_model(self):
        """加载翻译模型"""
        try:
            # 使用更稳定的翻译模型
            model_name = "Helsinki-NLP/opus-mt-zh-en"  # 中英翻译作为基础
            logger.info(f"正在加载翻译模型: {model_name}")
            
            # 准备模型加载参数
            model_kwargs = {
                'torch_dtype': torch.float16 if torch.cuda.is_available() else torch.float32,
                'device_map': self.device if torch.cuda.is_available() else "cpu",
                'trust_remote_code': True
            }
            
            # 加载模型和分词器
            self.tokenizer = AutoTokenizer.from_pretrained(model_name)
            self.model = AutoModelForSeq2SeqLM.from_pretrained(model_name, **model_kwargs)
            
            logger.info("翻译模型加载完成")
            
        except Exception as e:
            logger.error(f"翻译模型加载失败: {e}")
            # 降级到简单文本复制模式
            logger.warning("使用基础模式 - 仅显示原文")
            self.model = None
            self.tokenizer = None
    
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
                
                logger.info(f"翻译完成: {text}")
                
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
        
        # 如果模型不可用，返回原文
        if self.model is None or self.tokenizer is None:
            for lang in target_languages:
                translations[lang] = text  # 返回原文
            return translations
        
        try:
            # 对于中英翻译模型，只处理英文翻译
            if 'en' in target_languages:
                translated = self._translate_to_english(text)
                translations['en'] = translated
            
            # 其他语言暂时返回原文
            for lang in target_languages:
                if lang != 'en':
                    translations[lang] = text
                    
        except Exception as e:
            logger.error(f"翻译失败: {e}")
            # 出错时返回原文
            for lang in target_languages:
                translations[lang] = text
        
        return translations
    
    def _translate_to_english(self, text: str) -> str:
        """翻译到英文"""
        try:
            # 编码输入
            inputs = self.tokenizer(
                text, 
                return_tensors="pt",
                truncation=True,
                max_length=512
            ).to(self.model.device)
            
            # 生成翻译
            outputs = self.model.generate(
                **inputs,
                max_new_tokens=self.config['translation']['max_new_tokens'],
                temperature=self.config['translation']['temperature'],
                do_sample=False
            )
            
            # 解码结果
            translated_text = self.tokenizer.decode(outputs[0], skip_special_tokens=True)
            return translated_text.strip()
            
        except Exception as e:
            logger.error(f"英文翻译失败: {e}")
            return text  # 返回原文
    
    def _get_current_timestamp(self) -> float:
        """获取当前时间戳"""
        import time
        return time.time()
    
    def translate_sync(self, text: str) -> str:
        """同步翻译方法（用于测试）"""
        if self.model is None:
            self.load_model()
        return self._translate_to_english(text)