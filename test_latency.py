#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""转写延迟测试：用 TTS 生成的 wav 模拟实时音频流，按 chunk_duration 切块喂入转写器，
测量从"语音结束"到"文本产出"的延迟与推理耗时。

用法: python test_latency.py [wav路径]
"""
import io
import struct
import sys
import time
import wave
import queue
import threading

import numpy as np

from utils.helpers import load_config
from transcription.whisper_transcriber import WhisperTranscriber
from transcription.streaming_transcriber import StreamingTranscriber


def load_wav(path: str) -> np.ndarray:
    with wave.open(path, 'rb') as w:
        assert w.getframerate() == 16000, f"采样率需为16000, 实际 {w.getframerate()}"
        n = w.getnframes()
        pcm = w.readframes(n)
    return np.frombuffer(pcm, dtype=np.int16)


def main():
    args = [a for a in sys.argv[1:] if not a.startswith('--')]
    use_stream = '--stream' in sys.argv
    wav_path = args[0] if args else "test_audio/tts_test.wav"
    config = load_config("config/config.yaml")
    if use_stream:
        config['whisper']['backend'] = 'sherpa-stream'
    sr = config['audio']['sample_rate']
    chunk_dur = config['audio']['chunk_duration']
    chunk_samples = int(sr * chunk_dur)
    chunk_bytes = chunk_samples * 2

    samples = load_wav(wav_path)
    speech_sec = len(samples) / sr
    print(f"[test] 音频: {wav_path}  时长 {speech_sec:.1f}s  chunk={chunk_dur}s")

    transcriber = StreamingTranscriber(config) if use_stream else WhisperTranscriber(config)
    transcriber.load_model()

    audio_queue = queue.Queue(maxsize=50)
    text_queue = queue.Queue(maxsize=10)
    transcriber.start_transcription(audio_queue, text_queue)

    # 组装字节流: 语音 + 2s 句尾静音（触发停顿转写）
    silence = np.zeros(chunk_samples, dtype=np.int16).tobytes()
    speech = samples.tobytes()
    t_feed_start = time.time()

    for off in range(0, len(speech), chunk_bytes):
        audio_queue.put({'data': speech[off:off + chunk_bytes],
                         'timestamp': time.time()}, timeout=1)
    t_speech_end = time.time()
    for _ in range(int(2.0 / chunk_dur)):  # 2 秒静音块
        audio_queue.put({'data': silence, 'timestamp': time.time()}, timeout=1)

    # 等待结果：直到 3 秒无新结果或超时
    results = []
    quiet_since = time.time()
    deadline = time.time() + 60
    while time.time() < deadline:
        try:
            r = text_queue.get(timeout=1.0)
            results.append((r, time.time() - t_speech_end))
            quiet_since = time.time()
        except queue.Empty:
            if results and time.time() - quiet_since >= 3.0:
                break

    transcriber.stop_transcription()

    print("=" * 60)
    if results:
        for r, lat in results:
            print(f"[result] {r['text']}")
            print(f"         窗口时长 {r.get('window_sec', 0):.1f}s | "
                  f"语音结束→出文本 {lat*1000:.0f}ms")
    else:
        print("[result] 60 秒内未产出任何文本 —— 转写失败")
    print("=" * 60)


if __name__ == "__main__":
    main()
