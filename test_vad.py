# -*- coding: utf-8 -*-
"""VAD 单元测试：真实 silero 模型分类语音/静音 + 模型缺失时降级回退。

运行: python test_vad.py
"""
import sys
import wave

import numpy as np


def _load_speech_samples() -> np.ndarray:
    with wave.open("test_audio/tts_test.wav", "rb") as w:
        assert w.getframerate() == 16000
        return np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)


def test_real_model_classification():
    """真实语音块判定为 speech，静音块判定为非 speech"""
    from transcription.vad import SpeechVAD

    config = {'audio': {'sample_rate': 16000}, 'vad': {'enabled': True}}
    vad = SpeechVAD(config)
    assert vad.load(), "VAD 应加载成功（models/silero_vad.onnx）"
    assert vad.available

    pcm = _load_speech_samples()
    x = pcm.astype(np.float32) / 32768.0
    silence = np.zeros(8000, dtype=np.float32)

    # 静音块 → False
    assert vad.is_speech(silence) is False
    assert vad.is_speech(silence) is False

    # 真实语音块（0.5s 步进喂入，跳过 wav 尾部自然静音）→ True
    speech_hits = 0
    total = 0
    for off in range(0, len(x) - 8000, 8000):  # 末尾 0.5s 是 wav 自带静音，不计
        total += 1
        if vad.is_speech(x[off:off + 8000]):
            speech_hits += 1
    assert speech_hits >= total * 0.8, \
        f"语音块应绝大多数判定为 speech: {speech_hits}/{total}"

    # 语音结束后静音 → False（经尾滞后恢复）
    assert vad.is_speech(silence) in (True, False)  # 尾滞后期内不确定
    for _ in range(4):
        vad.is_speech(silence)
    assert vad.is_speech(silence) is False
    print(f"[通过] 真实模型分类: 语音块 {speech_hits}/{total} 判为 speech，静音判为非 speech")


def test_disabled_and_missing_model_fallback():
    """enabled=false 或模型缺失时 available=False（回退音量阈值，is_speech 不拦截）"""
    from transcription.vad import SpeechVAD

    vad = SpeechVAD({'vad': {'enabled': False}})
    assert vad.load() is False and not vad.available
    x = np.zeros(8000, dtype=np.float32)
    assert vad.is_speech(x) is True  # 不拦截（调用方已做音量预筛）

    vad2 = SpeechVAD({'vad': {'enabled': True, 'model': 'models/__no_such_vad.onnx'}})
    assert vad2.load() is False and not vad2.available
    assert vad2.is_speech(x) is True
    print("[通过] 禁用/模型缺失均回退为不拦截")


def test_transcriber_wiring():
    """流式转写器持有 VAD 实例且未加载时不改变行为（FakeRecognizer 走完整块路径）"""
    from transcription.streaming_transcriber import StreamingTranscriber

    config = {
        'whisper': {'backend': 'sherpa-stream', 'language': 'zh'},
        'streaming': {'punctuation_model': ''},
        'audio': {'volume_threshold': 30, 'sample_rate': 16000},
        'vad': {'enabled': True},
    }
    tr = StreamingTranscriber(config)
    assert tr.vad is not None
    # 模型路径缺省正确
    assert tr.vad.model_path == 'models/silero_vad.onnx'

    class _Result:
        text = ""
    class FakeStream:
        def accept_waveform(self, *a): pass
    class FakeRecognizer:
        def is_ready(self, s): return False
        def is_endpoint(self, s): return False
        def get_result_all(self, s): return _Result()
        def create_stream(self): return FakeStream()
        def reset(self, s): pass

    tr.recognizer = FakeRecognizer()
    tr._stream = tr.recognizer.create_stream()

    # 未加载（available=False）时喂块：静音仍置零、响声原样通过（旧行为）
    loud = (np.sin(np.linspace(0, 100, 8000)) * 10000).astype(np.int16).tobytes()
    quiet = (np.zeros(8000, dtype=np.int16)).tobytes()
    tq = []
    tr._handle_chunk({'data': quiet, 'timestamp': 0}, tq)
    assert not tr._stream_started, "静音块不应置位 _stream_started"
    tr._handle_chunk({'data': loud, 'timestamp': 0}, tq)
    assert tr._stream_started, "响声块在 VAD 未加载时按语音处理"
    print("[通过] 流式转写器 VAD 接线与回退行为")


if __name__ == "__main__":
    test_real_model_classification()
    test_disabled_and_missing_model_fallback()
    test_transcriber_wiring()
    print("\n全部 VAD 测试通过 ✓")
    sys.exit(0)
