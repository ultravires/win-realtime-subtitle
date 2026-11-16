#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""全链路集成测试（不需要真人对麦克风说话）：

默认模式：TTS 音频按真实节奏(0.5s/块)喂入 → 真实流式转写器 → 真实翻译模型
          测量 语音结束→译文产出 的端到端延迟
--capture 模式：只测音频捕获腿（真实 dshow 设备 + 扬声器播放 TTS），
          检查捕获的块是否有真实音频能量
--e2e    模式：真实 dshow 捕获 + 扬声器播放 → 流式转写 → 翻译
          （要求当前默认输出设备的声音能被捕获输入设备听到；回环电平
           因设备/音量而异，失败不代表系统故障，用 --capture 诊断电平）

测试音频再生成（16kHz 16bit 单声道 TTS）：
  powershell -c "Add-Type -AssemblyName System.Speech; \
    $s = New-Object System.Speech.Synthesis.SpeechSynthesizer; \
    $fmt = New-Object System.Speech.AudioFormat.SpeechAudioFormatInfo(16000,[System.Speech.AudioFormat.AudioBitsPerSample]::Sixteen,[System.Speech.AudioFormat.AudioChannel]::Mono); \
    $s.SetOutputToWaveFile('test_audio/tts_test_loud.wav',$fmt); $s.Volume=100; \
    $s.Speak('今天天气怎么样，我们来测试一下实时字幕系统的延迟表现')"

用法: python test_pipeline.py [--capture|--e2e]
"""
import sys
import time
import queue
import threading
import wave

import numpy as np

from utils.helpers import load_config

SPEECH_END = {'t': None}  # 语音播完的墙钟时间


def load_wav(path: str) -> np.ndarray:
    with wave.open(path, 'rb') as w:
        assert w.getframerate() == 16000, f"采样率需为16000, 实际 {w.getframerate()}"
        pcm = w.readframes(w.getnframes())
    return np.frombuffer(pcm, dtype=np.int16)


def run_e2e(config: dict):
    """真·端到端：真实 dshow 捕获 + 扬声器播放 TTS → 流式转写 → 翻译（等价 main.py 全路径）"""
    import subprocess
    from audio_capture.audio_capturer import AudioCapturer
    from transcription.streaming_transcriber import StreamingTranscriber
    from translation.translator import TencentTranslator

    # 回环播放电平(~17-40)远低于真人语音(500-3000)，测试时临时降低灵敏度（不改产品配置）
    config['audio']['volume_threshold'] = 10

    audio_queue = queue.Queue(maxsize=50)
    text_queue = queue.Queue(maxsize=10)
    translation_queue = queue.Queue(maxsize=20)

    capturer = AudioCapturer(config)
    capturer.start_capture()

    transcriber = StreamingTranscriber(config)
    transcriber.start_transcription(audio_queue, text_queue)

    translator = TencentTranslator(config)
    translator.load_model()
    translator.start_translation(text_queue, translation_queue)

    results = []

    def collect():
        while collect.running:
            try:
                results.append((translation_queue.get(timeout=0.5), time.time()))
            except queue.Empty:
                pass

    collect.running = True
    threading.Thread(target=collect, daemon=True).start()

    print("[e2e] 扬声器播放 TTS，真实麦克风腿捕获...")
    import os
    wav_abs = os.path.abspath("test_audio/tts_test_loud.wav")
    SPEECH_END['t'] = time.time() + 2  # 2s 后开始播
    play = subprocess.Popen(
        ["powershell", "-NoProfile", "-Command",
         "Start-Sleep -Seconds 2; (New-Object Media.SoundPlayer "
         f"'{wav_abs}').PlaySync()"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    # 播放约 7s + 留 8s 处理缓冲
    play.wait()
    SPEECH_END['t'] = time.time()  # PlaySync 返回即语音结束
    print("[e2e] 播放完成，等待译文...")
    deadline = time.time() + 60
    while time.time() < deadline and len(results) < 1:
        time.sleep(0.5)
    time.sleep(1)

    collect.running = False
    capturer.stop_capture()
    transcriber.stop_transcription()
    translator.stop_translation()

    print("=" * 60)
    if results:
        stats = capturer.get_performance_stats()
        print(f"[e2e] 捕获: total={stats['total_chunks']}, silence={stats['silence_chunks']}")
        for r, arrived in results:
            lat = (arrived - SPEECH_END['t']) * 1000
            print(f"[e2e] 原文: {r['original']}")
            print(f"[e2e] 译作: {r['translations']}")
            print(f"[e2e] 延迟: 语音结束→译文 {lat:.0f}ms")
    else:
        print("[e2e] 60 秒内未产出译文 —— 请查上方日志")
    print("=" * 60)


def run_full_pipeline(config: dict):
    from transcription.streaming_transcriber import StreamingTranscriber
    from translation.translator import TencentTranslator

    sr = config['audio']['sample_rate']
    chunk_dur = config['audio']['chunk_duration']
    chunk_samples = int(sr * chunk_dur)
    chunk_bytes = chunk_samples * 2

    samples = load_wav("test_audio/tts_test.wav")
    speech_sec = len(samples) / sr
    speech = samples.tobytes()
    silence = np.zeros(chunk_samples, dtype=np.int16).tobytes()
    print(f"[pipeline] 音频 {speech_sec:.1f}s，chunk={chunk_dur}s，后端=sherpa-stream，翻译=HY-MT 8bit")

    audio_queue = queue.Queue(maxsize=50)
    text_queue = queue.Queue(maxsize=10)
    translation_queue = queue.Queue(maxsize=20)

    transcriber = StreamingTranscriber(config)
    transcriber.start_transcription(audio_queue, text_queue)

    translator = TencentTranslator(config)
    translator.load_model()
    translator.start_translation(text_queue, translation_queue)

    # 收集结果
    results = []

    def collect():
        while collect.running:
            try:
                r = translation_queue.get(timeout=0.5)
                results.append((r, time.time()))
            except queue.Empty:
                pass
            except Exception:
                pass

    collect.running = True
    t = threading.Thread(target=collect, daemon=True)
    t.start()

    # 按真实节奏喂音频：0.5s 一块，块间隔用墙钟对齐
    n_speech_chunks = (len(speech) + chunk_bytes - 1) // chunk_bytes
    t0 = time.time()
    total_chunks = n_speech_chunks + int(2.0 / chunk_dur)  # 加 2s 静音
    for i in range(total_chunks):
        deadline = t0 + i * chunk_dur
        now = time.time()
        if now < deadline:
            time.sleep(deadline - now)
        data = speech[i * chunk_bytes:(i + 1) * chunk_bytes] if i < n_speech_chunks else silence
        if not data:
            data = silence
        try:
            audio_queue.put({'data': data, 'timestamp': time.time()}, timeout=0.5)
        except queue.Full:
            pass
        if i == n_speech_chunks - 1:
            SPEECH_END['t'] = time.time()

    # 等翻译产出（最长 90s）
    print(f"[pipeline] 语音播完，等待译文...")
    deadline = time.time() + 90
    while time.time() < deadline and len(results) < 2:
        time.sleep(0.5)

    collect.running = False
    transcriber.stop_transcription()
    translator.stop_translation()

    print("=" * 60)
    if results:
        for r, arrived in results:
            lat_end = (arrived - SPEECH_END['t']) * 1000 if SPEECH_END['t'] else 0
            first_ts = r.get('first_timestamp')
            lat_full = (arrived - first_ts) * 1000 if first_ts else 0
            print(f"[译文] 原文: {r['original']}")
            print(f"       译作: {r['translations']}")
            print(f"       延迟: 语音结束→译文 {lat_end:.0f}ms | 句首→译文 {lat_full:.0f}ms")
    else:
        print("[译文] 90 秒内未产出 —— 全链路失败，请查上方日志")
    print("=" * 60)


def run_capture_test(config: dict):
    from audio_capture.audio_capturer import AudioCapturer

    capturer = AudioCapturer(config)
    capturer.start_capture()

    # 后台播 TTS（真实扬声器输出）
    import subprocess
    wav_win = "test_audio/tts_test.wav"
    play = subprocess.Popen(
        ["powershell", "-NoProfile", "-Command",
         "(New-Object Media.SoundPlayer " + f"'{wav_win}'" + ").PlaySync()"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    # 采集 12 秒
    volumes = []
    t0 = time.time()
    while time.time() - t0 < 12:
        try:
            item = capturer.audio_queue.get(timeout=1.0)
            data = item['data'] if isinstance(item, dict) else item
            arr = np.frombuffer(data, dtype=np.int16)
            volumes.append(int(np.max(np.abs(arr))))
        except queue.Empty:
            pass
    play.terminate()
    stats = capturer.get_performance_stats()
    capturer.stop_capture()

    print("=" * 60)
    print(f"[capture] 12 秒收到 {len(volumes)} 个块, "
          f"捕获统计: total={stats['total_chunks']}, silence={stats['silence_chunks']}, "
          f"dropped={stats['dropped_chunks']}")
    if volumes:
        print(f"[capture] 块音量: max={max(volumes)}, avg={int(np.mean(volumes))}, "
              f"阈值={config['audio']['volume_threshold']}")
        loud = sum(1 for v in volumes if v > config['audio']['volume_threshold'])
        print(f"[capture] 超阈值块数: {loud}/{len(volumes)}")
        if loud > 0:
            print("[capture] 结论: 捕获腿工作正常，有真实音频进入管线")
        else:
            print("[capture] 结论: 全部静音——当前输入设备收不到扬声器声音"
                  "（虚拟声卡/麦克风未回环），需要真人说话验证")
    else:
        print("[capture] 结论: 12 秒未收到任何块，捕获异常")
    print("=" * 60)


if __name__ == "__main__":
    cfg = load_config("config/config.yaml")
    if "--capture" in sys.argv:
        run_capture_test(cfg)
    elif "--e2e" in sys.argv:
        run_e2e(cfg)
    else:
        run_full_pipeline(cfg)
