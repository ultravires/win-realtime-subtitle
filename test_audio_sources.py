# -*- coding: utf-8 -*-
"""
音频来源选择 - 离线单元测试（不访问音频设备）

覆盖：
1. _ChunkAssembler 定长切块与余数续接
2. float32 → s16le 转换、多声道混合、重采样长度
3. AudioCapturer._process_chunk 音量过滤 / 静音跳过 / 队列满处理
4. resolve_target_pid 的会话/快照匹配、PID 直填、错误信息
5. 来源配置解析（无效值报错、描述文本）

运行: python test_audio_sources.py
"""

import queue
import sys

import numpy as np

from audio_capture.audio_capturer import AudioCapturer, _ChunkAssembler
from audio_capture import wasapi_capture as wc


def make_config(**audio_overrides):
    audio = {
        'sample_rate': 16000,
        'channels': 1,
        'chunk_duration': 0.5,
        'buffer_size': 20,
        'volume_threshold': 30,
        'max_consecutive_silence': 5,
        'adaptive_buffer': True,
        'source': 'microphone',
        'device': '',
        'target_process': '',
    }
    audio.update(audio_overrides)
    return {'audio': audio, 'system': {'log_level': 'ERROR'}}


def test_chunk_assembler():
    a = _ChunkAssembler(16000)
    # 精确整块
    chunks = a.feed(b'\x00' * 16000)
    assert len(chunks) == 1 and len(chunks[0]) == 16000
    # 半块 → 无输出
    assert a.feed(b'\x00' * 8000) == []
    # 再半块 → 拼出一整块，余数清零
    chunks = a.feed(b'\x01' * 8000)
    assert len(chunks) == 1 and len(chunks[0]) == 16000
    assert chunks[0][:8000] == b'\x00' * 8000
    assert chunks[0][8000:] == b'\x01' * 8000
    # 一次喂入 2.5 块 → 出 2 块留半块
    chunks = a.feed(b'\x02' * 40000)
    assert len(chunks) == 2 and all(len(c) == 16000 for c in chunks)
    assert len(a._buf) == 8000
    print("✓ ChunkAssembler 定长切块")


def test_conversion():
    # 立体声 48k → 单声道 16k：长度缩小 6 倍，数值取均值
    n = 48000  # 1 秒
    left = np.full(n, 1.0, dtype=np.float32)
    right = np.full(n, 0.5, dtype=np.float32)
    samples = np.stack([left, right], axis=1).reshape(-1)
    mono = wc._to_mono16k(samples, 48000, 2)
    assert len(mono) == 16000, f"重采样长度错误: {len(mono)}"
    assert abs(float(np.mean(mono)) - 0.75) < 0.01, f"声道均值错误: {np.mean(mono)}"
    # 44.1k → 16k
    n2 = 44100
    mono2 = wc._to_mono16k(np.full(n2, 0.9, dtype=np.float32), 44100, 1)
    assert abs(len(mono2) - 16000) <= 2, f"44.1k 重采样长度: {len(mono2)}"
    # float32 → s16le 往返
    pcm = np.clip(np.array([0.5, -0.5], dtype=np.float32) * 32767.0,
                  -32768, 32767).astype(np.int16).tobytes()
    back = np.frombuffer(pcm, dtype=np.int16)
    assert back[0] in (16383, 16384) and back[1] in (-16383, -16384)
    # 静音路径（声道数 > 1 的零块）不崩溃
    mono3 = wc._to_mono16k(np.zeros(96000, dtype=np.float32), 48000, 2)
    assert len(mono3) == 16000 and np.all(mono3 == 0)
    print("✓ 格式转换（混声道/重采样/int16 往返）")


def test_process_chunk_filters():
    cap = AudioCapturer(make_config(volume_threshold=30, max_consecutive_silence=3,
                                    buffer_size=10))
    # 有声块入队
    loud = (np.sin(np.linspace(0, 100, 8000)) * 5000).astype(np.int16).tobytes()
    cap._process_chunk(loud)
    assert cap.audio_queue.qsize() == 1
    assert cap.consecutive_silence_count == 0
    # 静音块：前 max_silence 个入队并带 silent 标记，之后跳过
    silent = np.zeros(8000, dtype=np.int16).tobytes()
    for i in range(6):
        cap._process_chunk(silent)
        if i < 3:
            assert cap.audio_queue.qsize() == 2 + i
    assert cap.consecutive_silence_count == 6
    assert cap.audio_queue.qsize() == 4  # 1 loud + 3 silent
    # 恢复有声 → 计数清零
    cap._process_chunk(loud)
    assert cap.consecutive_silence_count == 0
    print("✓ 音量过滤/静音跳过")

    # 队列满：塞满后强塞有声块 → 紧急清理不丢新块
    cap2 = AudioCapturer(make_config(volume_threshold=30, buffer_size=3))
    for _ in range(3):
        cap2._process_chunk(loud)
    assert cap2.audio_queue.qsize() == 3
    cap2._process_chunk(loud)  # 触发 queue.Full → _handle_queue_full
    assert cap2.stats['dropped_chunks'] >= 1
    assert cap2.audio_queue.qsize() == 3
    print("✓ 队列满紧急清理")


def test_resolve_target():
    orig_sessions, orig_snap, orig_p2e = (wc.list_audio_sessions,
                                          wc._snapshot_processes, wc._pid_to_exe)
    # 先统一打补丁，再测所有依赖这些函数的路径
    sessions = [
        {'name': 'chrome.exe', 'pid': 100, 'active': False},
        {'name': 'chrome.exe', 'pid': 200, 'active': True},
        {'name': 'cloudmusic.exe', 'pid': 300, 'active': False},
    ]
    procs = [
        (1, 0, 'explorer.exe'),
        (10, 1, 'chrome.exe'),   # root
        (11, 10, 'chrome.exe'),  # child
        (12, 10, 'chrome.exe'),  # child
    ]
    wc._pid_to_exe = lambda pid: f"proc{pid}.exe"
    wc.list_audio_sessions = lambda: sessions
    wc._snapshot_processes = lambda: procs
    try:
        # 数字 PID 直填
        pid = wc.resolve_target_pid("1234")
        assert pid == 1234
        # 名称 → 会话精确匹配（活跃优先）
        assert wc._match_pid_from_sessions('chrome.exe', sessions) == 200
        assert wc._match_pid_from_sessions('CHROME.EXE', sessions) == 200
        assert wc._match_pid_from_sessions('cloud', sessions) == 300  # 子串
        assert wc._match_pid_from_sessions('nomatch', sessions) is None
        # 名称 → 快照 root 匹配（同名进程组取根）
        assert wc._match_pid_from_snapshot('chrome') == 10
        assert wc._match_pid_from_snapshot('explorer') == 1
        assert wc._match_pid_from_snapshot('nothing') is None
        # 完整 resolve（含错误信息列出会话）
        assert wc.resolve_target_pid("chrome") == 200
        try:
            wc.resolve_target_pid("wechat")
            assert False, "应抛出 RuntimeError"
        except RuntimeError as e:
            assert "wechat" in str(e) and "chrome.exe" in str(e)
    finally:
        wc.list_audio_sessions, wc._snapshot_processes, wc._pid_to_exe = \
            orig_sessions, orig_snap, orig_p2e
    print("✓ 目标解析（PID/会话匹配/快照 root/错误信息）")


def test_source_config_validation():
    # 无效来源 → ValueError 列出可选值
    try:
        AudioCapturer(make_config(source='linein'))
        assert False, "应抛出 ValueError"
    except ValueError as e:
        assert 'microphone' in str(e) and 'system' in str(e)
    # 描述文本
    cap = AudioCapturer(make_config(source='microphone'))
    assert '麦克风' in cap._describe_source()
    cap = AudioCapturer(make_config(source='system', device='Ditoo'))
    assert '系统音频' in cap._describe_source() and 'Ditoo' in cap._describe_source()
    print("✓ 来源配置校验与描述")


def main():
    print("运行音频来源单元测试（离线，不访问设备）\n" + "=" * 50)
    test_chunk_assembler()
    test_conversion()
    test_process_chunk_filters()
    test_resolve_target()
    test_source_config_validation()
    print("=" * 50)
    print("全部通过 ✓")


if __name__ == "__main__":
    sys.exit(main())
