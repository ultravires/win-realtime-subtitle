# -*- coding: utf-8 -*-
"""Silero VAD（语音活动检测）封装 —— sherpa-onnx VoiceActivityDetector。

作用：识别端此前只按音量阈值区分语音/静音，超过阈值的噪声、呼吸声、
背景音乐都会被当作语音喂给识别器，是静音/噪声幻觉文本的主要来源。
引入 Silero VAD 后，音量达标但非语音的音频块被置零送入流（端点计时
照常推进），语音块原样通过，显著减少非语音段产出的乱码字幕。

流式分类用法（已在本机实测验证，见 test_vad_probe.py）：
- vad.accept_waveform(samples) 喂入 float32 16kHz 音频
- vad.is_speech_detected() 反映当前是否处于语音段（含 min_silence_duration
  的尾滞后，避免切断词尾）
- 内部会把已完结的语音段排入输出队列，本封装每次喂入后即刻排空丢弃
  （只取分类结论，不保留分段音频，防止长时间运行的内存增长）

零新增依赖：模型为 2MB 的 silero_vad.onnx（models/ 目录，不入 git），
推理走 sherpa-onnx 自带的 onnxruntime（CPU 单线程 ~10ms/0.5s 块）。
"""

import os
import threading
from typing import Optional

import numpy as np
from loguru import logger

try:
    import sherpa_onnx
except ImportError:
    sherpa_onnx = None

# 默认模型路径与配置键（config.yaml 的 vad 节）
DEFAULT_MODEL = "models/silero_vad.onnx"
DEFAULT_THRESHOLD = 0.5
DEFAULT_MIN_SILENCE = 0.5   # 语音结束后保持"语音"状态的尾滞后（秒），保护词尾
DEFAULT_MIN_SPEECH = 0.25   # 短于此的声音（按键/碰撞声）不算语音（秒）
DEFAULT_WINDOW = 512        # silero 模型输入窗长（样本数），固定值


class SpeechVAD:
    """块级语音分类器：is_speech(samples) 判定一个音频块是否含语音。

    - load() 失败（模型缺失/sherpa-onnx 不可用）时 available=False，
      调用方自动回退到原有音量阈值行为，不影响运行
    - 仅可在单一消费线程中使用（转写线程），无需加锁
    """

    def __init__(self, config: dict):
        vcfg = (config.get('vad') or {}) if isinstance(config, dict) else {}
        self.enabled = bool(vcfg.get('enabled', True))
        self.model_path = str(vcfg.get('model', DEFAULT_MODEL))
        self.threshold = float(vcfg.get('threshold', DEFAULT_THRESHOLD))
        self.min_silence_duration = float(vcfg.get('min_silence_duration', DEFAULT_MIN_SILENCE))
        self.min_speech_duration = float(vcfg.get('min_speech_duration', DEFAULT_MIN_SPEECH))
        self.sample_rate = int((config.get('audio') or {}).get('sample_rate', 16000)
                               if isinstance(config, dict) else 16000)

        self.available = False
        self._vad: Optional[object] = None
        self._load_lock = threading.Lock()

    # ------------------------------------------------------------------ 生命周期

    def load(self) -> bool:
        """加载 VAD 模型。失败不抛异常（记日志并降级为不可用）。"""
        if not self.enabled:
            logger.info("VAD 已禁用（vad.enabled: false），沿用音量阈值判定")
            return False
        if sherpa_onnx is None:
            logger.warning("sherpa-onnx 未安装，VAD 不可用，沿用音量阈值判定")
            return False
        if not os.path.isfile(self.model_path):
            logger.warning(
                f"VAD 模型不存在: {self.model_path}（下载 silero_vad.onnx 放入 models/ 后生效），"
                "沿用音量阈值判定")
            return False
        with self._load_lock:
            if self._vad is not None:
                return True
            try:
                cfg = sherpa_onnx.VadModelConfig()
                cfg.sample_rate = self.sample_rate
                cfg.silero_vad.model = self.model_path
                cfg.silero_vad.threshold = self.threshold
                cfg.silero_vad.min_silence_duration = self.min_silence_duration
                cfg.silero_vad.min_speech_duration = self.min_speech_duration
                cfg.silero_vad.window_size = DEFAULT_WINDOW
                if not cfg.validate():
                    raise RuntimeError("VadModelConfig 校验失败")
                self._vad = sherpa_onnx.VoiceActivityDetector(
                    cfg, buffer_size_in_seconds=30)
                self.available = True
                logger.info(
                    f"VAD 加载完成: {self.model_path} "
                    f"(threshold={self.threshold}, min_silence={self.min_silence_duration}s)")
                return True
            except Exception as e:
                logger.warning(f"VAD 加载失败，沿用音量阈值判定: {e}")
                self._vad = None
                self.available = False
                return False

    # ------------------------------------------------------------------ 分类

    def is_speech(self, samples_f32: np.ndarray) -> bool:
        """判定音频块是否含语音（块级粒度）。

        喂入整块后取分类状态：块内只要进入语音段即 True（块头部的少量
        噪声随语音一起通过，对识别影响可忽略）；持续噪声/音乐返回 False。
        """
        if self._vad is None:
            return True  # 未加载：不拦截（调用方此前已做音量预筛）
        self._vad.accept_waveform(samples_f32)
        # 排空已完结的语音段（只取状态不留音频，防内存增长）
        while not self._vad.empty():
            self._vad.pop()
        return bool(self._vad.is_speech_detected())

    def reset(self):
        """复位内部状态（换音频来源/重置识别流时调用）"""
        if self._vad is not None:
            try:
                self._vad.reset()
            except Exception as e:
                logger.debug(f"VAD reset 失败（忽略）: {e}")
