import whisper
import torch
import numpy as np
import threading
import queue
from typing import Optional
from loguru import logger
from utils.helpers import sanitize_text
import time

class WhisperTranscriber:
    """Whisper语音转写器 - 优化版本"""
    
    def __init__(self, config: dict):
        self.config = config
        self.model = None
        self.device = config['whisper']['device']
        self.running = False
        self.transcribe_thread: Optional[threading.Thread] = None
        
        # 性能优化配置
        self.beam_size = config['whisper'].get('beam_size', 1)
        self.best_of = config['whisper'].get('best_of', 1)
        
        # 如果没有GPU，强制使用CPU
        if self.device == "cuda" and not torch.cuda.is_available():
            logger.warning("CUDA不可用，切换到CPU模式")
            self.device = "cpu"
        
    def load_model(self):
        """加载Whisper模型"""
        try:
            model_size = self.config['whisper']['model_size']
            logger.info(f"正在加载Whisper {model_size} 模型...")
            
            self.model = whisper.load_model(
                model_size, 
                device=self.device
            )
            
            logger.info(f"Whisper模型加载完成，使用设备: {self.device}")
            
        except Exception as e:
            logger.error(f"Whisper模型加载失败: {e}")
            raise
    
    def start_transcription(self, audio_queue: queue.Queue, text_queue: queue.Queue):
        """开始转写服务"""
        if self.running:
            logger.warning("转写服务已在运行")
            return
            
        if self.model is None:
            self.load_model()
            
        self.running = True
        self.transcribe_thread = threading.Thread(
            target=self._transcribe_loop,
            args=(audio_queue, text_queue),
            daemon=True
        )
        self.transcribe_thread.start()
        logger.info("语音转写服务已启动")
    
    def stop_transcription(self):
        """停止转写服务"""
        self.running = False
        if self.transcribe_thread:
            self.transcribe_thread.join(timeout=2)
        logger.info("语音转写服务已停止")
    
    def _transcribe_loop(self, audio_queue: queue.Queue, text_queue: queue.Queue):
        """转写主循环 - 优化版本"""
        last_text = ""  # 用于去重
        batch_buffer = []  # 批量处理缓冲区
        batch_timeout = 0.5  # 批量超时时间（秒）
        last_batch_time = time.time()
        
        while self.running:
            try:
                # 获取音频数据（可能带元数据）
                audio_item = audio_queue.get(timeout=0.1)
                
                # 处理不同格式的音频数据
                if isinstance(audio_item, dict):
                    # 新格式：带元数据的字典
                    audio_data = audio_item['data']
                    timestamp = audio_item.get('timestamp', time.time())
                    is_silent = audio_item.get('silent', False)
                    was_replaced = audio_item.get('replaced', False)
                    
                    if was_replaced:
                        logger.debug("处理被替换的音频数据块")
                else:
                    # 旧格式：纯字节数据
                    audio_data = audio_item
                    timestamp = time.time()
                    is_silent = False
                
                # 转换音频数据格式
                audio_np = self._convert_audio_data(audio_data)
                
                if audio_np is not None:
                    # 如果是静音且不是替换数据，则可以选择跳过
                    if is_silent and not was_replaced:
                        logger.debug("跳过静音数据块")
                        continue
                    
                    # 添加到批量缓冲区
                    batch_buffer.append({
                        'audio': audio_np,
                        'timestamp': timestamp,
                        'original_data': audio_data
                    })
                    
                    # 检查是否应该处理批次
                    current_time = time.time()
                    should_process = (
                        len(batch_buffer) >= 3 or  # 缓冲区满
                        (current_time - last_batch_time) >= batch_timeout or  # 超时
                        audio_queue.qsize() < 2  # 队列空闲
                    )
                    
                    if should_process and batch_buffer:
                        self._process_batch(batch_buffer, text_queue, last_text)
                        batch_buffer.clear()
                        last_batch_time = current_time
                        last_text = ""  # 重置去重
                
            except queue.Empty:
                # 队列为空时处理剩余的批量数据
                if batch_buffer:
                    self._process_batch(batch_buffer, text_queue, last_text)
                    batch_buffer.clear()
                    last_batch_time = time.time()
                continue
            except Exception as e:
                logger.error(f"转写过程中发生错误: {e}")
                batch_buffer.clear()  # 清空缓冲区
                continue
    
    def _process_batch(self, batch_buffer: list, text_queue: queue.Queue, last_text: str):
        """处理音频批处理"""
        if not batch_buffer:
            return
            
        try:
            # 合并音频数据进行批量转写
            combined_audio = np.concatenate([item['audio'] for item in batch_buffer])
            
            # 执行转写
            text = self._transcribe_audio(combined_audio)
            
            if text and text != last_text:
                # 清理文本
                clean_text = sanitize_text(text)
                if len(clean_text) > 2:  # 过滤太短的文本
                    logger.info(f"批量转写结果: {clean_text}")
                    try:
                        # 添加时间戳信息
                        result_with_metadata = {
                            'text': clean_text,
                            'timestamp': time.time(),
                            'batch_size': len(batch_buffer),
                            'first_timestamp': batch_buffer[0]['timestamp']
                        }
                        text_queue.put(result_with_metadata, timeout=0.1)
                    except queue.Full:
                        logger.warning("文本队列已满，丢弃结果")
                        
        except Exception as e:
            logger.error(f"批量处理失败: {e}")
    
    def _convert_audio_data(self, audio_data: bytes) -> Optional[np.ndarray]:
        """转换音频数据格式"""
        try:
            # 将二进制PCM数据转为numpy数组
            audio_np = np.frombuffer(audio_data, dtype=np.int16).astype(np.float32) / 32768.0
            return audio_np
        except Exception as e:
            logger.error(f"音频数据转换失败: {e}")
            return None
    
    def _transcribe_audio(self, audio_np: np.ndarray) -> str:
        """转写单段音频 - 优化版本"""
        try:
            # 准备转写参数
            transcribe_params = {
                'verbose': False,
                'task': 'transcribe',
                'without_timestamps': self.config['whisper']['without_timestamps'],
                'language': self.config['whisper']['language'],
                'beam_size': self.beam_size,
                'best_of': self.best_of
            }
            
            # GPU加速参数
            if self.device == "cuda" and self.config['whisper']['fp16']:
                transcribe_params['fp16'] = True
            
            # 执行转写
            result = self.model.transcribe(audio_np, **transcribe_params)
            return result.get("text", "").strip()
            
        except Exception as e:
            logger.error(f"音频转写失败: {e}")
            return ""
    
    def transcribe_sync(self, audio_data: bytes) -> str:
        """同步转写方法（用于测试）"""
        if self.model is None:
            self.load_model()
            
        audio_np = self._convert_audio_data(audio_data)
        if audio_np is not None:
            return self._transcribe_audio(audio_np)
        return ""