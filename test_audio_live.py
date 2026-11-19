# -*- coding: utf-8 -*-
"""
音频来源选择 - 真机验证（需要音频设备）

子命令:
  python test_audio_live.py list       列出设备与应用会话
  python test_audio_live.py mic        麦克风捕获 5 秒电平统计
  python test_audio_live.py system     系统回环：TTS 播放期间应有电平（~10 秒）
  python test_audio_live.py process    指定进程捕获：只抓 TTS 进程、不抓静默进程（~15 秒）
  python test_audio_live.py pipeline   AudioCapturer(system) 全链路入队验证（~10 秒）

TTS 使用 PowerShell System.Speech（默认播放设备），验证期间会出声。
"""

import subprocess
import sys
import time

import numpy as np

from audio_capture.wasapi_capture import (
    TARGET_RATE, WasapiStream,
    list_capture_devices, list_render_devices, list_audio_sessions)

PEAK_THRESHOLD = 300  # int16 峰值，低于此视为静音窗口


def tts_command(text: str) -> list:
    return ['powershell', '-NoProfile', '-Command',
            f'Add-Type -AssemblyName System.Speech; '
            f'$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; '
            f'$s.Speak("{text}")']


def speak_seconds(seconds: int = 6, delay: float = 0.0) -> subprocess.Popen:
    """启动一个持续出声的 TTS 子进程（返回 Popen，其 PID 即音频会话进程）"""
    text = ("系统音频捕获验证测试，正在播放测试语音。" * 8)[:seconds * 8 + 20]
    cmd = tts_command(text)
    if delay > 0:
        cmd = ['powershell', '-NoProfile', '-Command',
               f'Start-Sleep {delay}; ' + cmd[3]]
    return subprocess.Popen(cmd)


def capture_stats(stream: WasapiStream, seconds: float, window=0.5) -> list:
    """收集 per-window 峰值统计（float32 域）"""
    stats = []
    t_end = time.time() + seconds
    buf = []

    def on_packet(mono):
        buf.append(mono)

    stream.start(on_packet)
    try:
        t_win = time.time()
        while time.time() < t_end:
            time.sleep(0.05)
            if time.time() - t_win >= window:
                if buf:
                    data = np.concatenate(buf)
                    stats.append((time.time() - t_end + seconds,
                                  float(np.max(np.abs(data))) * 32767))
                    buf = []
                else:
                    stats.append((time.time() - t_end + seconds, 0.0))
                t_win = time.time()
    finally:
        stream.stop()
    return stats


def show_stats(title: str, stats: list):
    print(f"--- {title} ---")
    for t, peak in stats:
        bar = '#' * int(min(peak / 1000, 40))
        mark = ' (有声)' if peak > PEAK_THRESHOLD else ''
        print(f"  t={t:5.1f}s peak={peak:8.0f} {bar}{mark}")


def cmd_list():
    print("[录音输入设备]")
    for name, d in list_capture_devices():
        print(f"  {'[默认] ' if d else '       '}{name}")
    print("[播放输出设备]")
    for name, d in list_render_devices():
        print(f"  {'[默认] ' if d else '       '}{name}")
    print("[音频会话]")
    for s in list_audio_sessions():
        mark = '◉' if s['active'] else '○'
        print(f"  {mark} {s['name']} (PID {s['pid']})")


def cmd_mic():
    stream = WasapiStream("input")
    stats = capture_stats(stream, 5.0)
    show_stats("麦克风 5s（请在测试期间说话）", stats)


def cmd_system():
    # TTS 先启动（1s 延迟），捕获随后进行 → 1~9s 窗口应有声
    tts = speak_seconds(7, delay=1.0)
    stats = capture_stats(WasapiStream("loopback"), 10.0)
    show_stats("系统回环 10s（1s 处启动 TTS）", stats)
    tts.wait()
    mid = [p for t, p in stats if 2.5 < t < 7.0]
    before = [p for t, p in stats if t < 1.2]
    assert mid and max(mid) > PEAK_THRESHOLD, f"TTS 播放期间无信号: mid={mid}"
    assert max(before or [0]) <= PEAK_THRESHOLD, f"TTS 启动前有异常信号: {before}"
    print("✓ 系统回环捕获正常（TTS 期间有声、之前静音）")


def cmd_process():
    # TTS 持续约 16s，覆盖两段捕获：先抓 TTS 进程，再抓静默进程作隔离对照
    tts = speak_seconds(16)
    time.sleep(0.5)
    tts_pid = tts.pid
    # 静默对照组：一个不出声的 powershell 进程
    idle = subprocess.Popen(['powershell', '-NoProfile', '-Command', 'Start-Sleep 20'])

    stats_target = capture_stats(WasapiStream("process", pid=tts_pid), 7.0)
    show_stats(f"进程捕获 目标=TTS进程(PID {tts_pid})", stats_target)

    stats_idle = capture_stats(WasapiStream("process", pid=idle.pid), 5.0)
    show_stats(f"进程捕获 目标=静默进程(PID {idle.pid})，TTS 仍在播放", stats_idle)
    idle.wait()
    tts.wait()

    assert max(p for _, p in stats_target) > PEAK_THRESHOLD, \
        "TTS 进程未捕获到声音（隔离捕获失效）"
    # 静默进程捕获期间 TTS 仍在播 → 仍应接近零（证明只抓目标树）
    assert max(p for _, p in stats_idle) <= PEAK_THRESHOLD, \
        f"静默进程出现信号，捕获未隔离: {[p for _, p in stats_idle]}"
    print("✓ 进程隔离捕获正常（目标有声、无关进程无串音）")


def cmd_pipeline():
    """AudioCapturer(system) 全链路：float32 → s16le 块 → 队列 → 音量统计"""
    from audio_capture.audio_capturer import AudioCapturer
    cap = AudioCapturer({'audio': {
        'sample_rate': 16000, 'channels': 1, 'chunk_duration': 0.5,
        'buffer_size': 20, 'volume_threshold': 30,
        'max_consecutive_silence': 5, 'adaptive_buffer': True,
        'source': 'system', 'device': '', 'target_process': ''}})
    cap.start_capture()
    assert cap.running, "捕获线程启动失败"
    tts = speak_seconds(5)
    chunks = []
    t_end = time.time() + 8.0
    while time.time() < t_end:
        item = cap.get_audio_chunk(timeout=0.5)
        if item:
            chunks.append(item)
    cap.stop_capture()
    tts.wait()
    loud = [c for c in chunks if not c.get('silent') and c['volume'] > PEAK_THRESHOLD]
    print(f"共收到 {len(chunks)} 块，其中 {len(loud)} 块为有声块 "
          f"(最大 volume={max((c['volume'] for c in chunks), default=0)})")
    assert loud, "AudioCapturer(system) 未收到有声块"
    assert all(len(c['data']) == 16000 for c in chunks), "块长度不是 0.5s/16k/16bit"
    print("✓ AudioCapturer(system) 全链路入队正常")


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else 'list'
    {'list': cmd_list, 'mic': cmd_mic, 'system': cmd_system,
     'process': cmd_process, 'pipeline': cmd_pipeline}[cmd]()
    return 0


if __name__ == "__main__":
    sys.exit(main())
