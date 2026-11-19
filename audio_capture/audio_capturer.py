# -*- coding: utf-8 -*-
"""
音频捕获器 —— 按配置选择音频来源（麦克风 / 系统音频 / 指定应用）。

来源实现统一收敛到 audio_capture.wasapi_capture（纯 ctypes WASAPI）：
- microphone   麦克风输入
- system       扬声器回环（捕获系统播放的全部声音）
- application  指定进程树的声音（Windows 10 2004+ Application Loopback）

对下游的输出契约保持不变：audio_queue 中放入
{'data': s16le int16 字节(16kHz 单声道), 'timestamp': float, 'volume': int, ...}
的 0.5s 定长块，并沿用原有的音量过滤 / 队列背压逻辑。
"""

import queue
import threading
import time
from typing import Optional

import numpy as np
from loguru import logger

from audio_capture.wasapi_capture import (
    TARGET_RATE,
    WasapiStream,
    _pid_to_exe,
    list_audio_sessions,
    list_capture_devices,
    list_render_devices,
    resolve_target_pid,
)

# 配置 audio.source 合法值 → 中文说明
SOURCE_LABELS = {
    'microphone': '麦克风',
    'system': '系统音频（扬声器回环）',
    'application': '指定应用',
}


class _ChunkAssembler:
    """把任意长度的 PCM 字节流切成定长块（余数进下一块）"""

    def __init__(self, chunk_bytes: int):
        self.chunk_bytes = chunk_bytes
        self._buf = bytearray()

    def feed(self, data: bytes) -> list:
        """喂入字节，返回切出的完整块列表"""
        self._buf.extend(data)
        chunks = []
        while len(self._buf) >= self.chunk_bytes:
            chunks.append(bytes(self._buf[:self.chunk_bytes]))
            del self._buf[:self.chunk_bytes]
        return chunks


class AudioCapturer:
    """音频捕获器 - 按来源配置分发到 WASAPI 捕获流"""

    def __init__(self, config: dict):
        self.config = config
        acfg = config['audio']
        self.audio_queue = queue.Queue(maxsize=acfg['buffer_size'])
        self.running = False
        self.capture_thread: Optional[threading.Thread] = None
        self._stream: Optional[WasapiStream] = None

        # 流量控制相关
        self.consecutive_silence_count = 0
        self.max_silence = acfg.get('max_consecutive_silence', 5)
        self.volume_threshold = acfg.get('volume_threshold', 100)
        self.adaptive_buffer = acfg.get('adaptive_buffer', True)

        # 块参数（下游契约）：16kHz 单声道 s16le，chunk_duration 秒定长块
        self.sample_rate = acfg['sample_rate']
        self.chunk_bytes = int(self.sample_rate * 2 * acfg['chunk_duration'])
        self._assembler = _ChunkAssembler(self.chunk_bytes)

        # 来源配置与解析
        self.source_type = str(acfg.get('source', 'microphone')).strip().lower()
        if self.source_type not in SOURCE_LABELS:
            raise ValueError(
                f"audio.source 配置无效: '{self.source_type}'，"
                f"可选值: {', '.join(SOURCE_LABELS)}")
        self.source_device = str(acfg.get('device', '')).strip()
        self.target_process = str(acfg.get('target_process', '')).strip()
        self.target_pid = None
        if self.source_type == 'application':
            self.target_pid = resolve_target_pid(self.target_process,
                                                 interactive=True)
        self._log_source()

        # 性能统计
        self.stats = {
            'total_chunks': 0,
            'dropped_chunks': 0,
            'silence_chunks': 0,
            'processing_delays': []
        }

    # ---------------- 来源描述 ----------------

    def _describe_source(self) -> str:
        """人类可读的来源描述（用于日志与 --list-sources 复核）"""
        if self.source_type == 'microphone':
            name = self.source_device or '系统默认录音设备'
            return f"麦克风（{name}）"
        if self.source_type == 'system':
            name = self.source_device or '系统默认播放设备'
            return f"系统音频（回环 {name}）"
        pid = self.target_pid
        exe = _pid_to_exe(pid) if pid else f"PID {pid}"
        return f"应用音频（{exe}, PID {pid}）"

    def _log_source(self):
        logger.info(f"音频来源: {self._describe_source()} [{self.source_type}]")

    # ---------------- 捕获流构建 ----------------

    def _create_stream(self) -> WasapiStream:
        if self.source_type == 'microphone':
            return WasapiStream("input", device_name=self.source_device)
        if self.source_type == 'system':
            return WasapiStream("loopback", device_name=self.source_device)
        return WasapiStream("process", pid=self.target_pid)

    # ---------------- 生命周期 ----------------

    def start_capture(self):
        """开始音频捕获"""
        if self.running:
            logger.warning("音频捕获已在运行")
            return

        self.running = True
        self.capture_thread = threading.Thread(target=self._capture_loop,
                                               name="audio-capture", daemon=True)
        self.capture_thread.start()

    def stop_capture(self):
        """停止音频捕获"""
        self.running = False
        if self.capture_thread:
            self.capture_thread.join(timeout=3.0)
            self.capture_thread = None
        logger.info("音频捕获已停止")
        self._log_performance_stats()

    def _capture_loop(self):
        """捕获线程：启动 WASAPI 流，回调里完成过滤与入队；失败时置 running=False"""
        stream = self._create_stream()
        try:
            stream.start(self._on_audio_packet)
        except Exception as e:
            logger.error(f"音频捕获启动失败（来源: {self.source_type}）: {e}")
            logger.info("提示: 可运行 'python main.py --list-sources' 查看可用设备/应用，"
                        "并检查 config.yaml 的 audio.source / audio.device / audio.target_process")
            self.running = False
            return

        logger.info("音频捕获已启动")
        try:
            while self.running and stream.error is None:
                time.sleep(0.2)
        finally:
            stream.stop()
            if stream.error:
                logger.error(stream.error)
            self.running = False

    # ---------------- 数据管线 ----------------

    def _on_audio_packet(self, mono: np.ndarray):
        """捕获线程回调：float32 单声道 16k → s16le 字节 → 定长块 → 过滤入队"""
        pcm = np.clip(mono * 32767.0, -32768, 32767).astype(np.int16).tobytes()
        for chunk in self._assembler.feed(pcm):
            self._process_chunk(chunk)

    def _process_chunk(self, audio_data: bytes):
        """单块处理：音量检测 → 静音过滤 → 队列（沿用原 ffmpeg 路径逻辑）"""
        current_time = time.time()
        try:
            audio_np = np.frombuffer(audio_data, dtype=np.int16)
            max_volume = int(np.max(np.abs(audio_np)))

            if max_volume > self.volume_threshold:
                # 有效音频信号
                self.consecutive_silence_count = 0

                # 自适应缓冲区管理
                if self.adaptive_buffer and \
                        self.audio_queue.qsize() > self.config['audio']['buffer_size'] * 0.8:
                    self._adaptive_drop_old_data()

                self.audio_queue.put({
                    'data': audio_data,
                    'timestamp': current_time,
                    'volume': max_volume
                }, timeout=0.1)
                self.stats['total_chunks'] += 1
            else:
                # 静音处理
                self.consecutive_silence_count += 1
                self.stats['silence_chunks'] += 1

                # 连续静音超过阈值后跳过入队，减轻队列压力
                if self.consecutive_silence_count <= self.max_silence:
                    self.audio_queue.put({
                        'data': audio_data,
                        'timestamp': current_time,
                        'volume': max_volume,
                        'silent': True
                    }, timeout=0.1)
                    self.stats['total_chunks'] += 1
                else:
                    logger.debug(f"连续静音 {self.consecutive_silence_count} 次，跳过数据")
        except queue.Full:
            self._handle_queue_full(audio_data, current_time)

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
        except Exception:
            pass

    def _handle_queue_full(self, audio_data: bytes, timestamp: float):
        """处理队列满的情况：丢弃最旧数据后重入队"""
        logger.warning("音频队列已满，执行紧急清理")
        try:
            old_data = self.audio_queue.get_nowait()
            self.stats['dropped_chunks'] += 1
            if isinstance(old_data, dict) and old_data.get('silent', False):
                logger.debug("丢弃静音数据块")
            else:
                logger.debug("丢弃普通音频数据块")

            self.audio_queue.put({
                'data': audio_data,
                'timestamp': timestamp,
                'replaced': True
            }, timeout=0.1)
            self.stats['total_chunks'] += 1
        except Exception as e:
            logger.error(f"队列满处理失败: {e}")

    # ---------------- 对外接口 ----------------

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
            # 兼容保留：WASAPI 事件驱动路径无"块间处理延迟"指标，恒为 0
            'avg_processing_delay': 0,
        }

    def _log_performance_stats(self):
        """记录性能统计"""
        stats = self.get_performance_stats()
        logger.info(f"音频捕获性能统计: {stats}")
