import queue
import threading
import time
from typing import Optional

import numpy as np
from loguru import logger

from utils.helpers import sanitize_text

try:
    from faster_whisper import WhisperModel
except ImportError:
    WhisperModel = None


class WhisperTranscriber:
    """基于 faster-whisper 的语音转写器 - 低延迟窗口版本

    低延迟策略（替代旧的 2s 固定分块 + 3 块批量缓冲）：
    - 音频以小块（默认 0.5s）进入，按音量检测拼成"语音窗口"
    - 触发转写的条件（满足其一即转写并清空窗口）：
      1. 句尾静音达到 silence_flush_sec（说话人停顿 → 尽快出字幕）
      2. 窗口时长达到 max_window_sec（连续说话时限制延迟上限）
      3. 长时间没有新音频到达（捕获端在持续静音时会停止推数据）
    - 达到 max_window_sec 时优先切在窗口内最近的静音点，避免切断词语
    """

    def __init__(self, config: dict):
        self.config = config
        wcfg = config['whisper']
        self.model = None
        self.device = wcfg['device']
        self.compute_type = wcfg.get('compute_type') or (
            'int8_float16' if self.device == 'cuda' else 'int8')
        self.model_size = wcfg['model_size']
        self.language = wcfg['language']
        self.beam_size = wcfg.get('beam_size', 1)

        acfg = config['audio']
        self.chunk_duration = acfg['chunk_duration']
        self.volume_threshold = acfg.get('volume_threshold', 30)
        self.max_window_sec = wcfg.get('max_window_sec', 4.0)
        self.silence_flush_sec = wcfg.get('silence_flush_sec', 1.0)
        self.min_audio_sec = 0.3  # 窗口太短不送模型，避免碎字幕

        self.running = False
        self.transcribe_thread: Optional[threading.Thread] = None

        # 语音窗口状态
        self._window: list[np.ndarray] = []
        self._window_samples = 0
        self._window_start_ts: Optional[float] = None
        self._last_chunk_ts: Optional[float] = None
        self._last_text = ""  # 跨窗口去重

        # 如果没有GPU，强制使用CPU
        if self.device == "cuda" and not self._cuda_available():
            logger.warning("CUDA不可用，切换到CPU模式")
            self.device = "cpu"
            self.compute_type = "int8"

    @staticmethod
    def _cuda_available() -> bool:
        try:
            import torch
            return torch.cuda.is_available()
        except ImportError:
            return False

    def load_model(self):
        """加载 faster-whisper 模型"""
        if WhisperModel is None:
            raise RuntimeError("faster-whisper 未安装，请执行: pip install faster-whisper")
        try:
            logger.info(f"正在加载 faster-whisper {self.model_size} "
                        f"(device={self.device}, compute_type={self.compute_type})...")
            t0 = time.time()
            self.model = WhisperModel(
                self.model_size,
                device=self.device,
                compute_type=self.compute_type,
            )
            logger.info(f"faster-whisper 模型加载完成，耗时 {time.time() - t0:.1f}s")
            self._warmup()
        except Exception as e:
            logger.error(f"Whisper模型加载失败: {e}")
            raise

    def _warmup(self):
        """空音频预热：触发 CUDA 内核/cuDNN 初始化，避免首条字幕背上 10-20s 的冷启动"""
        try:
            t0 = time.time()
            segments, _ = self.model.transcribe(
                np.zeros(16000, dtype=np.float32),
                language=self.language, beam_size=self.beam_size)
            _ = "".join(seg.text for seg in segments)  # 迭代才真正触发推理
            logger.info(f"模型预热完成，耗时 {time.time() - t0:.1f}s")
        except Exception as e:
            logger.warning(f"模型预热失败（不影响运行）: {e}")

    # ------------------------------------------------------------------ 服务生命周期

    def start_transcription(self, audio_queue: queue.Queue, text_queue: queue.Queue):
        """开始转写服务"""
        if self.running:
            logger.warning("转写服务已在运行")
            return

        if self.model is None:
            self.load_model()

        self._reset_window()
        self.running = True
        self.transcribe_thread = threading.Thread(
            target=self._transcribe_loop,
            args=(audio_queue, text_queue),
            daemon=True
        )
        self.transcribe_thread.start()
        logger.info(f"语音转写服务已启动 (max_window={self.max_window_sec}s, "
                    f"silence_flush={self.silence_flush_sec}s)")

    def stop_transcription(self):
        """停止转写服务"""
        self.running = False
        if self.transcribe_thread:
            self.transcribe_thread.join(timeout=3)
        logger.info("语音转写服务已停止")

    # ------------------------------------------------------------------ 主循环

    def _transcribe_loop(self, audio_queue: queue.Queue, text_queue: queue.Queue):
        """转写主循环：小块音频 → 语音窗口 → 条件触发转写"""
        while self.running:
            try:
                audio_item = audio_queue.get(timeout=0.1)
            except queue.Empty:
                # 队列无新数据：如果窗口里有话没转完，按"静音超时"触发转写
                # （捕获端持续静音时不再推送数据，只能靠这里兜底）
                if self._window and self._last_chunk_ts is not None:
                    if time.time() - self._last_chunk_ts >= self.silence_flush_sec:
                        self._flush_window(text_queue)
                continue
            except Exception as e:
                logger.error(f"转写循环异常: {e}")
                continue

            try:
                self._handle_chunk(audio_item, text_queue)
            except Exception as e:
                logger.error(f"处理音频块失败: {e}")
                self._reset_window()

    def _handle_chunk(self, audio_item, text_queue: queue.Queue):
        """处理单个音频块：音量判定 → 拼窗 / 累计静音"""
        if isinstance(audio_item, dict):
            audio_data = audio_item['data']
            timestamp = audio_item.get('timestamp', time.time())
        else:
            audio_data = audio_item
            timestamp = time.time()

        audio_np = self._convert_audio_data(audio_data)
        if audio_np is None or len(audio_np) == 0:
            return

        now = time.time()
        self._last_chunk_ts = now

        # 自行计算音量（不依赖捕获端的 silent 标记，块粒度一致）
        peak = float(np.max(np.abs(audio_np * 32768.0)))
        is_speech = peak > self.volume_threshold

        if self._window_start_ts is None:
            self._window_start_ts = timestamp

        if is_speech:
            self._window.append(audio_np)
            self._window_samples += len(audio_np)
        else:
            if self._window:
                # 句中停顿保留在窗口里（自然语流），并累计静音时长
                self._window.append(audio_np)
                self._window_samples += len(audio_np)
                window_sec = self._window_samples / 16000.0
                if window_sec >= self.max_window_sec:
                    self._flush_window(text_queue)
                    return
                if window_sec - self._speech_samples() / 16000.0 >= self.silence_flush_sec:
                    # 句尾静音够了 → 触发转写
                    self._flush_window(text_queue)
                    return
            # 窗口为空时静音直接丢弃，不占用模型时间
            return

        # 连续说话达到窗口上限 → 转写（优先切在静音点）
        if self._window_samples / 16000.0 >= self.max_window_sec:
            self._flush_window(text_queue, cut_at_silence=True)

    # ------------------------------------------------------------------ 窗口与转写

    def _speech_samples(self) -> int:
        """窗口内语音样本数（非静音块）"""
        return sum(len(a) for a in self._window
                   if float(np.max(np.abs(a * 32768.0))) > self.volume_threshold)

    def _reset_window(self):
        self._window = []
        self._window_samples = 0
        self._window_start_ts = None

    def _flush_window(self, text_queue: queue.Queue, cut_at_silence: bool = False):
        """转写当前窗口并清空；cut_at_silence 时只转写到最近静音点，剩余部分留下轮"""
        window = self._window
        window_start = self._window_start_ts
        if not window:
            self._reset_window()
            return

        if cut_at_silence:
            # 在窗口内找最近的静音块边界，把窗口一分为二
            cut_idx = self._last_silence_boundary(window)
            if cut_idx is not None and cut_idx > 0:
                head = window[:cut_idx]
                tail = window[cut_idx:]
                head_samples = sum(len(a) for a in head)
                if head_samples / 16000.0 >= self.min_audio_sec:
                    self._emit_transcription(head, window_start, text_queue)
                self._window = tail
                self._window_samples = sum(len(a) for a in tail)
                return  # 尾部留给下一轮继续积累
            # 没有可用静音点，整窗转写
            self._emit_transcription(window, window_start, text_queue)
            self._reset_window()
            return

        self._emit_transcription(window, window_start, text_queue)
        self._reset_window()

    def _last_silence_boundary(self, window: list[np.ndarray]) -> Optional[int]:
        """返回最后一个静音块的起始下标（作为切分点）"""
        boundary = None
        for i, a in enumerate(window):
            if float(np.max(np.abs(a * 32768.0))) <= self.volume_threshold:
                boundary = i
        return boundary

    def _emit_transcription(self, window: list[np.ndarray], window_start: Optional[float],
                            text_queue: queue.Queue):
        """转写窗口并投递结果"""
        audio_np = np.concatenate(window)
        duration = len(audio_np) / 16000.0
        if duration < self.min_audio_sec:
            return

        t0 = time.time()
        text = self._transcribe_audio(audio_np)
        infer_ms = (time.time() - t0) * 1000

        if text and text != self._last_text:
            clean_text = sanitize_text(text)
            if len(clean_text) > 1:
                self._last_text = clean_text
                logger.info(f"转写结果 ({duration:.1f}s 音频, {infer_ms:.0f}ms): {clean_text}")
                try:
                    text_queue.put({
                        'text': clean_text,
                        'timestamp': time.time(),
                        'window_sec': duration,
                        'first_timestamp': window_start if window_start is not None else time.time(),
                    }, timeout=0.5)
                except queue.Full:
                    logger.warning("文本队列已满，丢弃结果")
        elif text == self._last_text and text:
            logger.debug(f"跨窗口重复结果已过滤: {text}")

    def _convert_audio_data(self, audio_data: bytes) -> Optional[np.ndarray]:
        """s16le PCM → float32 [-1, 1]"""
        try:
            return np.frombuffer(audio_data, dtype=np.int16).astype(np.float32) / 32768.0
        except Exception as e:
            logger.error(f"音频数据转换失败: {e}")
            return None

    def _transcribe_audio(self, audio_np: np.ndarray) -> str:
        """执行转写"""
        try:
            segments, info = self.model.transcribe(
                audio_np,
                language=self.language,
                task='transcribe',
                beam_size=self.beam_size,
                vad_filter=True,               # 过滤窗口内残留静音
                condition_on_previous_text=False,  # 防止短窗口间的幻觉/复读
            )
            return "".join(seg.text for seg in segments).strip()
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
