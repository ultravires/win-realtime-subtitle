import subprocess
import platform
import threading
import queue
import numpy as np
import time
from typing import Optional
from loguru import logger

class AudioCapturer:
    """系统音频捕获器 - 优化版本"""
    
    def __init__(self, config: dict):
        self.config = config
        self.audio_queue = queue.Queue(maxsize=config['audio']['buffer_size'])
        self.running = False
        self.capture_thread: Optional[threading.Thread] = None
        
        # 流量控制相关
        self.consecutive_silence_count = 0
        self.max_silence = config['audio'].get('max_consecutive_silence', 5)
        self.volume_threshold = config['audio'].get('volume_threshold', 100)
        self.adaptive_buffer = config['audio'].get('adaptive_buffer', True)
        
        # 性能统计
        self.stats = {
            'total_chunks': 0,
            'dropped_chunks': 0,
            'silence_chunks': 0,
            'processing_delays': []
        }
        
    def _get_ffmpeg_command(self) -> list:
        """根据操作系统获取FFmpeg命令"""
        system = platform.system()
        sample_rate = self.config['audio']['sample_rate']
        channels = self.config['audio']['channels']
        
        if system == "Windows":
            # Windows使用DirectShow
            device_name = self._detect_windows_audio_device()
            return [
                "ffmpeg",
                "-f", "dshow",
                "-i", f"audio={device_name}",
                "-ar", str(sample_rate),
                "-ac", str(channels),
                "-f", "s16le",
                "-"
            ]
        elif system == "Linux":
            # Linux使用PulseAudio
            return [
                "ffmpeg",
                "-f", "pulse",
                "-i", "default",
                "-ar", str(sample_rate),
                "-ac", str(channels),
                "-f", "s16le",
                "-"
            ]
        elif system == "Darwin":
            # macOS使用AVFoundation
            return [
                "ffmpeg",
                "-f", "avfoundation",
                "-i", ":0",
                "-ar", str(sample_rate),
                "-ac", str(channels),
                "-f", "s16le",
                "-"
            ]
        else:
            raise OSError(f"不支持的操作系统: {system}")
    
    def _detect_windows_audio_device(self) -> str:
        """检测Windows音频设备名称，返回不带 audio= 前缀的裸设备名"""
        try:
            # 列出所有音频设备
            cmd = ["ffmpeg", "-list_devices", "true", "-f", "dshow", "-i", "dummy"]
            process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding='utf-8',
                errors='replace'
            )
            _, stderr = process.communicate()

            import re
            lines = stderr.split('\n')

            # 优先查找系统音频设备（立体声混音/虚拟声卡，可捕获系统播放的声音）
            for line in lines:
                if ('Stereo Mix' in line or '立体声混音' in line or 'virtual-audio-capturer' in line) and '(audio)' in line:
                    # 提取设备名称
                    match = re.search(r'"([^"]+)"', line)
                    if match:
                        logger.info(f"检测到系统音频设备: {match.group(1)}")
                        return match.group(1)

            # 没有系统音频设备时，动态使用检测到的第一个音频输入设备
            audio_devices = []
            for line in lines:
                match = re.search(r'"([^"]+)"\s*\(audio\)', line)
                if match:
                    audio_devices.append(match.group(1))

            if audio_devices:
                logger.warning(f"未找到立体声混音设备，使用检测到的音频设备: {audio_devices[0]} "
                               f"(共检测到 {len(audio_devices)} 个: {audio_devices})")
                return audio_devices[0]

            raise RuntimeError("未检测到任何可用的音频输入设备，请检查麦克风连接")

        except Exception as e:
            logger.error(f"音频设备检测失败: {e}")
            raise
    
    def start_capture(self):
        """开始音频捕获"""
        if self.running:
            logger.warning("音频捕获已在运行")
            return
            
        self.running = True
        self.capture_thread = threading.Thread(target=self._capture_loop, daemon=True)
        self.capture_thread.start()
        logger.info("音频捕获已启动")
    
    def stop_capture(self):
        """停止音频捕获"""
        self.running = False
        if self.capture_thread:
            self.capture_thread.join(timeout=2)
        logger.info("音频捕获已停止")
        self._log_performance_stats()
    
    def _capture_loop(self):
        """音频捕获主循环 - 优化版本"""
        process = None
        try:
            ffmpeg_cmd = self._get_ffmpeg_command()
            logger.info(f"执行FFmpeg命令: {' '.join(ffmpeg_cmd)}")

            process = subprocess.Popen(
                ffmpeg_cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=1024*16
            )

            # 后台线程持续排空ffmpeg的stderr，防止管道写满导致ffmpeg阻塞；
            # 只保留最后20行，供异常退出时诊断
            stderr_lines = []

            def _drain_stderr():
                try:
                    for raw_line in process.stderr:
                        stderr_lines.append(raw_line.rstrip())
                        if len(stderr_lines) > 20:
                            stderr_lines.pop(0)
                except Exception:
                    pass

            threading.Thread(target=_drain_stderr, daemon=True).start()
            
            chunk_size = self.config['audio']['sample_rate'] * 2 * self.config['audio']['chunk_duration']
            last_process_time = time.time()
            
            while self.running and process.poll() is None:
                audio_data = process.stdout.read(chunk_size)
                if audio_data:
                    current_time = time.time()
                    
                    try:
                        # 将音频数据转换为numpy数组进行预处理
                        audio_np = np.frombuffer(audio_data, dtype=np.int16)
                        
                        # 音量检测和静音过滤
                        max_volume = np.max(np.abs(audio_np))
                        
                        if max_volume > self.volume_threshold:
                            # 有效音频信号
                            self.consecutive_silence_count = 0
                            
                            # 自适应缓冲区管理
                            if self.adaptive_buffer and self.audio_queue.qsize() > self.config['audio']['buffer_size'] * 0.8:
                                # 缓冲区接近满时，适度丢弃旧数据
                                self._adaptive_drop_old_data()
                            
                            # 添加时间戳用于性能监控
                            timestamped_data = {
                                'data': audio_data,
                                'timestamp': current_time,
                                'volume': max_volume
                            }
                            
                            self.audio_queue.put(timestamped_data, timeout=0.1)
                            self.stats['total_chunks'] += 1
                            
                        else:
                            # 静音处理
                            self.consecutive_silence_count += 1
                            self.stats['silence_chunks'] += 1
                            
                            # 如果连续静音超过阈值，则跳过以减少队列压力
                            if self.consecutive_silence_count <= self.max_silence:
                                timestamped_data = {
                                    'data': audio_data,
                                    'timestamp': current_time,
                                    'volume': max_volume,
                                    'silent': True
                                }
                                self.audio_queue.put(timestamped_data, timeout=0.1)
                                self.stats['total_chunks'] += 1
                            else:
                                logger.debug(f"连续静音 {self.consecutive_silence_count} 次，跳过数据")
                                
                        # 记录处理延迟
                        processing_delay = current_time - last_process_time
                        if processing_delay > 0.1:  # 超过100ms认为有延迟
                            self.stats['processing_delays'].append(processing_delay)
                        
                        last_process_time = current_time
                        
                    except queue.Full:
                        self._handle_queue_full(audio_data, current_time)
                            
        except Exception as e:
            logger.error(f"音频捕获过程中发生错误: {e}")
        finally:
            if process is not None:
                # running仍为True说明ffmpeg异常退出（正常停止时running已被提前置为False）
                if self.running:
                    exit_code = process.poll()
                    stderr_tail = " | ".join(stderr_lines[-5:]) if stderr_lines else "无输出"
                    logger.error(f"FFmpeg 异常退出 (exit code={exit_code})，最后输出: {stderr_tail}")
                try:
                    process.terminate()
                except Exception:
                    pass
            self.running = False
            logger.info("音频捕获线程结束")
    
    def _adaptive_drop_old_data(self):
        """自适应丢弃旧数据"""
        try:
            # 丢弃最旧的20%数据
            drop_count = max(1, self.config['audio']['buffer_size'] // 5)
            for _ in range(drop_count):
                if not self.audio_queue.empty():
                    self.audio_queue.get_nowait()
                    self.stats['dropped_chunks'] += 1
            logger.debug(f"自适应丢弃 {drop_count} 个旧音频块")
        except:
            pass
    
    def _handle_queue_full(self, audio_data: bytes, timestamp: float):
        """处理队列满的情况"""
        logger.warning("音频队列已满，执行紧急清理")
        try:
            # 移除最旧的数据
            old_data = self.audio_queue.get_nowait()
            self.stats['dropped_chunks'] += 1
            
            # 如果是静音数据，优先丢弃
            if isinstance(old_data, dict) and old_data.get('silent', False):
                logger.debug("丢弃静音数据块")
            else:
                logger.debug("丢弃普通音频数据块")
            
            # 尝试重新添加新数据
            timestamped_data = {
                'data': audio_data,
                'timestamp': timestamp,
                'replaced': True
            }
            self.audio_queue.put(timestamped_data, timeout=0.1)
            self.stats['total_chunks'] += 1
            
        except Exception as e:
            logger.error(f"队列满处理失败: {e}")
    
    def get_audio_chunk(self, timeout: float = 1.0) -> Optional[dict]:
        """获取音频数据块 - 返回带元数据的字典"""
        try:
            return self.audio_queue.get(timeout=timeout)
        except queue.Empty:
            return None
    
    def get_queue_size(self) -> int:
        """获取队列当前大小"""
        return self.audio_queue.qsize()
    
    def get_performance_stats(self) -> dict:
        """获取性能统计信息"""
        return {
            'queue_size': self.get_queue_size(),
            'total_chunks': self.stats['total_chunks'],
            'dropped_chunks': self.stats['dropped_chunks'],
            'silence_chunks': self.stats['silence_chunks'],
            'drop_rate': self.stats['dropped_chunks'] / max(self.stats['total_chunks'], 1),
            'avg_processing_delay': np.mean(self.stats['processing_delays']) if self.stats['processing_delays'] else 0
        }
    
    def _log_performance_stats(self):
        """记录性能统计"""
        stats = self.get_performance_stats()
        logger.info(f"音频捕获性能统计: {stats}")