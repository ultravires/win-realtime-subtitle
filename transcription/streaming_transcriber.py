import queue
import threading
import time
from typing import Optional

import numpy as np
from loguru import logger

from utils.helpers import sanitize_text

try:
    import sherpa_onnx
except ImportError:
    sherpa_onnx = None


class StreamingTranscriber:
    """sherpa-onnx 流式语音转写器（真流式，逐块增量解码）

    与 WhisperTranscriber 接口完全兼容（load_model / start_transcription /
    stop_transcription），main.py 按配置选择后端。

    - 每 0.5s 音频块增量送入在线解码器，说话过程中模型持续产出部分结果
    - 内置端点检测：句尾停顿 rule1 秒 / 长静音 rule2 秒 → 产出最终文本并复位流
    - CPU 即可实时（RTF << 1），不占显存；可通过 provider 换 CUDA
    - 可选挂接标点恢复模型，字幕更自然
    """

    def __init__(self, config: dict):
        self.config = config
        wcfg = config['whisper']
        scfg = config.get('streaming', {})

        self.model_dir = scfg.get('model_dir',
                                  'models/sherpa-onnx-streaming-zipformer-bilingual-zh-en-2023-02-20')
        self.decoding_method = scfg.get('decoding_method', 'modified_beam_search')
        self.rule1_min_trailing_silence = scfg.get('rule1_min_trailing_silence', 1.0)
        self.rule2_min_trailing_silence = scfg.get('rule2_min_trailing_silence', 2.5)
        self.provider = scfg.get('provider', 'cpu')  # cpu/cuda
        self.num_threads = scfg.get('num_threads', 2)
        self.punct_model_dir = scfg.get('punctuation_model', '')
        self.punct = None  # 可选标点恢复模型（流式 zipformer 不输出标点）

        acfg = config['audio']
        self.volume_threshold = acfg.get('volume_threshold', 30)

        # 与 whisper 配置对齐的语言设置：bilingual zh-en 模型按输入自动区分
        self.language_hint = wcfg.get('language', 'zh')

        self.recognizer = None
        self.running = False
        self.transcribe_thread: Optional[threading.Thread] = None

        # 流状态
        self._stream = None
        self._last_text = ""   # 跨语句去重
        self._stream_started = False

    def load_model(self):
        """加载 sherpa-onnx 在线识别模型（含可选标点模型）"""
        if sherpa_onnx is None:
            raise RuntimeError("sherpa-onnx 未安装，请执行: pip install sherpa-onnx")

        import os
        full_dir = self.model_dir
        if not os.path.isdir(full_dir):
            raise RuntimeError(f"流式模型目录不存在: {full_dir}")

        def find(prefix: str, prefer_int8: bool) -> str:
            """选模型文件：encoder/joiner 用 int8（更快），decoder 用 fp32（精度）"""
            candidates = sorted(
                f for f in os.listdir(full_dir)
                if f.startswith(prefix) and f.endswith('.onnx'))
            if not candidates:
                raise RuntimeError(f"{full_dir} 中找不到 {prefix}*.onnx")
            if prefer_int8:
                int8 = [f for f in candidates if '.int8.' in f]
                if int8:
                    return os.path.join(full_dir, int8[0])
            fp32 = [f for f in candidates if '.int8.' not in f]
            return os.path.join(full_dir, (fp32 or candidates)[0])

        tokens = os.path.join(full_dir, 'tokens.txt')
        if not os.path.isfile(tokens):
            raise RuntimeError(f"找不到 tokens.txt: {tokens}")

        self.recognizer = sherpa_onnx.OnlineRecognizer.from_transducer(
            tokens=tokens,
            encoder=find('encoder', prefer_int8=True),
            decoder=find('decoder', prefer_int8=False),
            joiner=find('joiner', prefer_int8=True),
            num_threads=self.num_threads,
            decoding_method=self.decoding_method,
            enable_endpoint_detection=True,
            rule1_min_trailing_silence=self.rule1_min_trailing_silence,
            rule2_min_trailing_silence=self.rule2_min_trailing_silence,
            rule3_min_utterance_length=20,
            provider=self.provider,
        )
        logger.info(f"sherpa-onnx 流式模型加载完成: {full_dir} (provider={self.provider})")

        self._load_punctuation()

    def _load_punctuation(self):
        """加载标点恢复模型（可选）：给流式结果补 。，？等标点"""
        import os
        if not self.punct_model_dir or not os.path.isdir(self.punct_model_dir):
            if self.punct_model_dir:
                logger.warning(f"标点模型目录不存在，字幕将无标点: {self.punct_model_dir}")
            return
        model_onnx = os.path.join(self.punct_model_dir, 'model.onnx')
        if not os.path.isfile(model_onnx):
            logger.warning(f"标点模型文件缺失: {model_onnx}")
            return
        pcfg = sherpa_onnx.OfflinePunctuationConfig(
            model=sherpa_onnx.OfflinePunctuationModelConfig(
                ct_transformer=model_onnx,
                num_threads=self.num_threads,
                provider=self.provider,
            ))
        self.punct = sherpa_onnx.OfflinePunctuation(pcfg)
        logger.info(f"标点恢复模型加载完成: {self.punct_model_dir}")

    # ------------------------------------------------------------------ 服务生命周期

    def start_transcription(self, audio_queue: queue.Queue, text_queue: queue.Queue):
        """开始转写服务（接口与 WhisperTranscriber 一致）"""
        if self.running:
            logger.warning("转写服务已在运行")
            return
        if self.recognizer is None:
            self.load_model()

        self._stream = self.recognizer.create_stream()
        self._stream_started = False
        self._last_text = ""
        self.running = True
        self.transcribe_thread = threading.Thread(
            target=self._transcribe_loop,
            args=(audio_queue, text_queue),
            daemon=True
        )
        self.transcribe_thread.start()
        logger.info(f"流式转写服务已启动 (decoding={self.decoding_method}, "
                    f"endpoint rule1={self.rule1_min_trailing_silence}s)")

    def stop_transcription(self):
        """停止转写服务"""
        self.running = False
        if self.transcribe_thread:
            self.transcribe_thread.join(timeout=3)
        logger.info("流式转写服务已停止")

    # ------------------------------------------------------------------ 主循环

    def _transcribe_loop(self, audio_queue: queue.Queue, text_queue: queue.Queue):
        while self.running:
            try:
                audio_item = audio_queue.get(timeout=0.1)
            except queue.Empty:
                continue
            except Exception as e:
                logger.error(f"转写循环异常: {e}")
                continue

            try:
                self._handle_chunk(audio_item, text_queue)
            except Exception as e:
                logger.error(f"处理音频块失败: {e}")
                self._reset_stream()

    def _handle_chunk(self, audio_item, text_queue: queue.Queue):
        if isinstance(audio_item, dict):
            audio_data = audio_item['data']
            timestamp = audio_item.get('timestamp', time.time())
        else:
            audio_data = audio_item
            timestamp = time.time()

        audio_np = self._convert_audio_data(audio_data)
        if audio_np is None or len(audio_np) == 0:
            return

        peak = float(np.max(np.abs(audio_np * 32768.0)))
        if peak <= self.volume_threshold:
            # 静音段全长度送入流——端点检测依赖真实静音时长判定句尾
            audio_np = np.zeros(len(audio_np), dtype=np.float32)
        else:
            self._stream_started = True

        t0 = time.time()
        self._stream.accept_waveform(16000, audio_np)
        while self.recognizer.is_ready(self._stream):
            self.recognizer.decode_stream(self._stream)

        partial = self._current_result()
        decode_ms = (time.time() - t0) * 1000
        if partial and self._stream_started:
            logger.debug(f"流式部分结果 ({decode_ms:.0f}ms): {partial}")

        # 端点触发 → 产出该句并复位流
        if self.recognizer.is_endpoint(self._stream):
            self._emit(partial.strip(), timestamp, text_queue)
            self._reset_stream()

    def _current_result(self) -> str:
        try:
            res = self.recognizer.get_result_all(self._stream)
            return res.text
        except AttributeError:
            return self.recognizer.get_result(self._stream)

    def _reset_stream(self):
        """复位识别流：reset 保留解码器状态清理，等价于开启新语句"""
        try:
            self.recognizer.reset(self._stream)
        except AttributeError:
            self._stream = self.recognizer.create_stream()
        self._stream_started = False

    def _emit(self, text: str, timestamp: float, text_queue: queue.Queue):
        if not text:
            return
        clean = sanitize_text(text)
        if self.punct is not None:
            try:
                clean = self.punct.add_punctuation(clean)
            except Exception as e:
                logger.warning(f"标点恢复失败（用原文）: {e}")
        if len(clean) > 1 and clean != self._last_text:
            self._last_text = clean
            logger.info(f"流式转写结果: {clean}")
            try:
                text_queue.put({
                    'text': clean,
                    'timestamp': time.time(),
                    'first_timestamp': timestamp,
                }, timeout=0.5)
            except queue.Full:
                logger.warning("文本队列已满，丢弃结果")

    def _convert_audio_data(self, audio_data: bytes) -> Optional[np.ndarray]:
        try:
            return np.frombuffer(audio_data, dtype=np.int16).astype(np.float32) / 32768.0
        except Exception as e:
            logger.error(f"音频数据转换失败: {e}")
            return None

    def transcribe_sync(self, audio_data: bytes) -> str:
        """同步转写方法（用于测试）：一次性喂入并取结果"""
        if self.recognizer is None:
            self.load_model()
        audio_np = self._convert_audio_data(audio_data)
        if audio_np is None or len(audio_np) == 0:
            return ""
        stream = self.recognizer.create_stream()
        # 分 0.5s 块喂入，模拟真实流式
        hop = 8000
        for off in range(0, len(audio_np), hop):
            stream.accept_waveform(16000, audio_np[off:off + hop])
            while self.recognizer.is_ready(stream):
                self.recognizer.decode_stream(stream)
        tail = np.zeros(16000, dtype=np.float32)  # 1s 静音触发端点
        stream.accept_waveform(16000, tail)
        while self.recognizer.is_ready(stream):
            self.recognizer.decode_stream(stream)
        if self.recognizer.is_endpoint(stream):
            return self.recognizer.get_result(stream).strip()
        return self.recognizer.get_result(stream).strip()
