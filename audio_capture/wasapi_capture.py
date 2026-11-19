# -*- coding: utf-8 -*-
"""
WASAPI 音频捕获（纯 ctypes，零新增第三方依赖）

统一实现三种音频来源，全部产出 float32 单声道 16kHz numpy 数组：
- input     麦克风/录音设备（WASAPI 共享模式输入）
- loopback  扬声器回环 —— 捕获系统播放的全部声音，无需立体声混音或 virtual-audio-capturer
- process   指定进程（及其子进程树）的声音 —— Windows 10 2004 (build 19041+) Application Loopback API

辅助设施：
- list_capture_devices / list_render_devices  设备枚举（供配置匹配与 --list-sources）
- list_audio_sessions                         有音频会话的应用（供选择与诊断）
- resolve_target_pid                          "进程名/PID" → 捕获目标 PID

COM 全部通过 vtable 直调，接口下标均有 SDK 头文件依据（改前先核对）：
- IAudioClient        Initialize=3 GetMixFormat=8 Start=10 Stop=11 SetEventHandle=13 GetService=14
- IAudioCaptureClient GetBuffer=3 ReleaseBuffer=4 GetNextPacketSize=5
- IMMDeviceEnumerator EnumAudioEndpoints=3 GetDefaultAudioEndpoint=4
- IMMDeviceCollection GetCount=3 Item=4
- IMMDevice           Activate=3 OpenPropertyStore=4
- IPropertyStore      GetCount=3 GetAt=4 GetValue=5
- IAudioSessionManager2   GetSessionEnumerator=5（audiopolicy.h：基 2 方法 + 3）
- IAudioSessionEnumerator GetCount=3 GetSession=4
- IAudioSessionControl2  GetState=3 GetProcessId=14 IsSystemSoundsSession=15
  （audiopolicy.h 实证：基接口 9 方法占 vtable 3..11，Control2 追加
   GetSessionIdentifier=12 GetSessionInstanceIdentifier=13 GetProcessId=14
   IsSystemSoundsSession=15 SetDuckingPreference=16）
- 激活参数扁平布局 [ActivationType=1][TargetProcessId][ProcessLoopbackMode=0(INCLUDE)]，
  设备路径 "VAD\\Process_Loopback"，INCLUDE_TARGET_PROCESS_TREE=0
  （audioclientactivationparams.h 实证，三个独立实现交叉验证一致）
"""

import ctypes
import os
import sys
import threading
import time
from ctypes import POINTER, byref, c_long, c_longlong, c_uint, c_ulong, c_void_p
from ctypes import wintypes
from math import gcd

import numpy as np
from loguru import logger

try:
    from scipy import signal as _scipy_signal
except ImportError:  # pragma: no cover
    _scipy_signal = None

TARGET_RATE = 16000

# ---- WASAPI 常量 ----
AUDCLNT_SHAREMODE_SHARED = 0
AUDCLNT_STREAMFLAGS_LOOPBACK = 0x00020000
AUDCLNT_STREAMFLAGS_EVENTCALLBACK = 0x00040000
AUDCLNT_STREAMFLAGS_AUTOCONVERTPCM = 0x80000000
AUDCLNT_STREAMFLAGS_SRC_DEFAULT_QUALITY = 0x08000000
AUDCLNT_BUFFERFLAGS_SILENT = 0x2
AUDCLNT_BUFFERFLAGS_DATA_DISCONTINUITY = 0x1

DATA_FLOW_RENDER = 0   # eRender（播放设备）
DATA_FLOW_CAPTURE = 1  # eCapture（录音设备）
DEVICE_STATE_ACTIVE = 1
ROLE_CONSOLE = 0       # eConsole：应用默认使用的设备
CLSCTX_INPROC_SERVER = 1
VT_BLOB = 65
VT_LPWSTR = 31
COINIT_MULTITHREADED = 0
RPC_E_CHANGED_MODE = -2147417850  # 0x80010106
E_NOINTERFACE = -2147467262       # 0x80004002

# ---- GUID / IID ----
IID_IUnknown = "{00000000-0000-0000-C000-000000000046}"
IID_IAgileObject = "{94EA2B94-E9CC-49E0-C0FF-EE64CA8F5B90}"
IID_IAudioClient = "{1CB9AD4C-DBFA-4C32-B178-C2F568A703B2}"
IID_IAudioCaptureClient = "{C8ADBD64-E71E-48A0-A4DE-185C395CD317}"
IID_IActivateAudioInterfaceCompletionHandler = "{41D949AB-9862-444A-80F6-C261334DA5EB}"
CLSID_MMDeviceEnumerator = "{BCDE0395-E52F-467C-8E3D-C4579291692E}"
IID_IMMDeviceEnumerator = "{A95664D2-9614-4F35-A746-DE8DB63617E6}"
IID_IAudioSessionManager2 = "{77AA99A0-1BD6-484F-8BC7-2C654C9A9B6F}"
IID_IAudioSessionControl2 = "{BFB7FF88-7239-4FC9-8FA2-07C950BE9C6D}"
# PKEY_Device_FriendlyName（functiondiscoverykeys_devpkey.h）
PKEY_DEVICE_FRIENDLY_NAME = ("{A45C254E-DF1C-4EFD-8020-67D146A850E0}", 14)

VIRTUAL_AUDIO_DEVICE_PROCESS_LOOPBACK = "VAD\\Process_Loopback"

WAVE_FORMAT_PCM = 1
WAVE_FORMAT_IEEE_FLOAT = 3
WAVE_FORMAT_EXTENSIBLE = 0xFFFE

_ole32 = ctypes.WinDLL("ole32")
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

HRESULT = c_long


# ---------------- 基础结构 ----------------

class _GUID(ctypes.Structure):
    _fields_ = [("d1", wintypes.DWORD), ("d2", wintypes.WORD),
                ("d3", wintypes.WORD), ("d4", ctypes.c_ubyte * 8)]

    def __init__(self, s=None):
        super().__init__()
        if s:
            _ole32.CLSIDFromString(ctypes.c_wchar_p(s), byref(self))


class WAVEFORMATEX(ctypes.Structure):
    _fields_ = [("wFormatTag", wintypes.WORD), ("nChannels", wintypes.WORD),
                ("nSamplesPerSec", wintypes.DWORD),
                ("nAvgBytesPerSec", wintypes.DWORD),
                ("nBlockAlign", wintypes.WORD),
                ("wBitsPerSample", wintypes.WORD), ("cbSize", wintypes.WORD)]


def _float_format(rate: int, channels: int) -> WAVEFORMATEX:
    """IEEE float32 格式描述"""
    f = WAVEFORMATEX()
    f.wFormatTag = WAVE_FORMAT_IEEE_FLOAT
    f.nChannels = channels
    f.nSamplesPerSec = rate
    f.wBitsPerSample = 32
    f.nBlockAlign = channels * 4
    f.nAvgBytesPerSec = rate * f.nBlockAlign
    return f


class _BLOB(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("pBlobData", c_void_p)]


class _ACTIVATION_PARAMS(ctypes.Structure):
    """AUDIOCLIENT_ACTIVATION_PARAMS（PROCESS_LOOPBACK 用途，扁平展开，
    布局经 audioclientactivationparams.h 实证）"""
    _fields_ = [("ActivationType", c_uint),          # 1 = PROCESS_LOOPBACK
                ("TargetProcessId", wintypes.DWORD),
                ("ProcessLoopbackMode", c_uint)]     # 0 = INCLUDE_TARGET_PROCESS_TREE


class _PROPVARIANT(ctypes.Structure):
    """PROPVARIANT（x64 下 24 字节）：vt + 保留 + 16 字节联合。
    本项目只用到 blob（激活参数）与 LPWSTR（设备名）两种形态。"""

    class _U(ctypes.Union):
        _fields_ = [("blob", _BLOB), ("pwszVal", c_void_p),
                    ("rest", ctypes.c_ubyte * 16)]

    _anonymous_ = ("u",)
    _fields_ = [("vt", ctypes.c_ushort), ("wReserved1", ctypes.c_ushort),
                ("wReserved2", ctypes.c_ushort), ("wReserved3", ctypes.c_ushort),
                ("u", _U)]


class _PROPERTYKEY(ctypes.Structure):
    _fields_ = [("fmtid", _GUID), ("pid", wintypes.DWORD)]


# ---------------- COM vtable 直调辅助 ----------------

def _com(ptr, idx, *argtypes):
    """返回调用 COM 接口指针 vtable[idx] 的函数"""
    vtbl = ctypes.cast(ptr, POINTER(POINTER(c_void_p))).contents
    proto = ctypes.WINFUNCTYPE(HRESULT, c_void_p, *argtypes)
    return lambda *args: proto(vtbl[idx])(ptr, *args)


def _release(ptr):
    """调用 IUnknown::Release（vtable[2]）"""
    if ptr:
        proto = ctypes.WINFUNCTYPE(c_ulong, c_void_p)
        vtbl = ctypes.cast(ptr, POINTER(POINTER(c_void_p))).contents
        proto(vtbl[2])(ptr)


def _check(hr: int, what: str):
    if hr < 0:
        raise OSError(f"{what} 失败 (hr=0x{hr & 0xFFFFFFFF:08X})")


def _coinit() -> bool:
    """当前线程 COM 初始化（MTA）；返回是否需要 CoUninitialize"""
    hr = _ole32.CoInitializeEx(None, COINIT_MULTITHREADED)
    if hr == RPC_E_CHANGED_MODE:
        return False  # 已按其他模式初始化，直接可用
    _check(hr, "CoInitializeEx")
    return True


def is_process_loopback_supported() -> bool:
    """Application Loopback API 需要 Windows 10 2004 / build 19041+"""
    try:
        return sys.getwindowsversion().build >= 19041
    except Exception:
        return False


# ---------------- 设备枚举与名称 ----------------

def _device_friendly_name(dev) -> str:
    """IMMDevice → 友好名称（PropertyStore: GetValue(PKEY_Device_FriendlyName)）"""
    store = c_void_p()
    pv = _PROPVARIANT()
    try:
        if _com(dev, 4, wintypes.DWORD, POINTER(c_void_p))(  # OpenPropertyStore(READ)
                0, byref(store)) < 0:
            return "<未知设备>"
        key = _PROPERTYKEY(_GUID(PKEY_DEVICE_FRIENDLY_NAME[0]),
                           PKEY_DEVICE_FRIENDLY_NAME[1])
        if _com(store, 5, POINTER(_PROPERTYKEY), POINTER(_PROPVARIANT))(  # GetValue
                byref(key), byref(pv)) >= 0 and pv.vt == VT_LPWSTR and pv.pwszVal:
            name = ctypes.c_wchar_p(pv.pwszVal).value or "<未知设备>"
            _ole32.PropVariantClear(byref(pv))
            pv.vt = 0  # 已清空，避免 finally 重复清理
            return name
        return "<未知设备>"
    except Exception:
        return "<未知设备>"
    finally:
        if pv.vt == VT_LPWSTR and pv.pwszVal:
            _ole32.PropVariantClear(byref(pv))
        _release(store)


def _enum_device_names(flow: int) -> list:
    """枚举指定方向的设备，返回 [(name, is_default), ...]"""
    result = []
    uninit = _coinit()
    enum = c_void_p()
    default_dev = c_void_p()
    try:
        _check(_ole32.CoCreateInstance(
            byref(_GUID(CLSID_MMDeviceEnumerator)), None, CLSCTX_INPROC_SERVER,
            byref(_GUID(IID_IMMDeviceEnumerator)), byref(enum)), "CoCreateInstance")
        default_name = None
        if _com(enum, 4, c_uint, c_uint, POINTER(c_void_p))(  # GetDefaultAudioEndpoint
                flow, ROLE_CONSOLE, byref(default_dev)) >= 0:
            default_name = _device_friendly_name(default_dev)
        coll = c_void_p()
        _check(_com(enum, 3, c_uint, wintypes.DWORD, POINTER(c_void_p))(  # EnumAudioEndpoints
            flow, DEVICE_STATE_ACTIVE, byref(coll)), "EnumAudioEndpoints")
        count = c_uint()
        _com(coll, 3, POINTER(c_uint))(byref(count))
        for i in range(count.value):
            dev = c_void_p()
            if _com(coll, 4, c_uint, POINTER(c_void_p))(i, byref(dev)) < 0:
                continue
            try:
                name = _device_friendly_name(dev)
                result.append((name, name == default_name))
            finally:
                _release(dev)
    except OSError as e:
        raise RuntimeError(f"枚举音频设备失败: {e}")
    finally:
        _release(default_dev)
        _release(coll)
        _release(enum)
        if uninit:
            _ole32.CoUninitialize()
    return result


def list_capture_devices() -> list:
    """录音输入设备（microphone 模式可选目标）"""
    return _enum_device_names(DATA_FLOW_CAPTURE)


def list_render_devices() -> list:
    """播放输出设备（system 模式回环目标）"""
    return _enum_device_names(DATA_FLOW_RENDER)


# ---------------- 进程与音频会话 ----------------

def _snapshot_processes() -> list:
    """[(pid, ppid, exe 名小写), ...]（toolhelp 快照，无 COM）"""
    class _PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                    ("th32ProcessID", wintypes.DWORD),
                    ("th32DefaultHeapID", ctypes.POINTER(c_ulong)),
                    ("th32ModuleID", wintypes.DWORD),
                    ("cntThreads", wintypes.DWORD),
                    ("th32ParentProcessID", wintypes.DWORD),
                    ("pcPriClassBase", c_long), ("dwFlags", wintypes.DWORD),
                    ("szExeFile", ctypes.c_wchar * 260)]

    snap = _kernel32.CreateToolhelp32Snapshot(2, 0)  # TH32CS_SNAPPROCESS
    if not snap or snap == -1:
        return []
    out = []
    try:
        e = _PROCESSENTRY32W()
        e.dwSize = ctypes.sizeof(e)
        ok = _kernel32.Process32FirstW(snap, byref(e))
        while ok:
            out.append((e.th32ProcessID, e.th32ParentProcessID,
                        e.szExeFile.lower()))
            ok = _kernel32.Process32NextW(snap, byref(e))
    finally:
        _kernel32.CloseHandle(snap)
    return out


def _pid_to_exe(pid: int) -> str:
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    h = _kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(260)
        size = wintypes.DWORD(260)
        if _kernel32.QueryFullProcessImageNameW(h, 0, buf, byref(size)):
            return os.path.basename(buf.value)
        return ""
    finally:
        _kernel32.CloseHandle(h)


def list_audio_sessions() -> list:
    """枚举所有播放设备上的音频会话。

    返回 [{"name": "chrome.exe", "pid": 1234, "active": True}, ...]
    （active 即正在出声；排序：活跃优先，其次名称，再次 PID）
    取不到 PID 的会话（多为系统声音会话）自动排除。
    """
    uninit = _coinit()
    sessions = []
    enum = c_void_p()
    coll = c_void_p()
    try:
        _check(_ole32.CoCreateInstance(
            byref(_GUID(CLSID_MMDeviceEnumerator)), None, CLSCTX_INPROC_SERVER,
            byref(_GUID(IID_IMMDeviceEnumerator)), byref(enum)), "CoCreateInstance")
        _check(_com(enum, 3, c_uint, wintypes.DWORD, POINTER(c_void_p))(
            DATA_FLOW_RENDER, DEVICE_STATE_ACTIVE, byref(coll)), "EnumAudioEndpoints")
        count = c_uint()
        _com(coll, 3, POINTER(c_uint))(byref(count))
        for i in range(count.value):
            dev = c_void_p()
            if _com(coll, 4, c_uint, POINTER(c_void_p))(i, byref(dev)) < 0:
                continue
            try:
                _collect_device_sessions(dev, sessions)
            finally:
                _release(dev)
    except OSError as e:
        raise RuntimeError(f"枚举音频会话失败: {e}")
    finally:
        _release(coll)
        _release(enum)
        if uninit:
            _ole32.CoUninitialize()

    # 排序 + 去重（同一 PID 可能注册多个会话）
    seen = set()
    unique = []
    for s in sorted(sessions, key=lambda x: (not x[2], x[0].lower(), -x[1])):
        key = (s[0], s[1])
        if key not in seen:
            seen.add(key)
            unique.append({"name": s[0], "pid": s[1], "active": s[2]})
    return unique


def _collect_device_sessions(dev, out: list):
    """收集一个播放设备上的音频会话 → [(exe 名, pid, active), ...]"""
    mgr = c_void_p()
    # IMMDevice::Activate(IID_IAudioSessionManager2)
    if _com(dev, 3, POINTER(_GUID), wintypes.DWORD, c_void_p, POINTER(c_void_p))(
            byref(_GUID(IID_IAudioSessionManager2)), CLSCTX_INPROC_SERVER,
            None, byref(mgr)) < 0:
        return
    sess_enum = c_void_p()
    try:
        if _com(mgr, 5, POINTER(c_void_p))(byref(sess_enum)) < 0:  # GetSessionEnumerator
            return
        count = c_uint()
        _com(sess_enum, 3, POINTER(c_uint))(byref(count))
        for i in range(count.value):
            ctl = c_void_p()
            if _com(sess_enum, 4, c_uint, POINTER(c_void_p))(i, byref(ctl)) < 0:
                continue
            try:
                ctl2 = c_void_p()
                if _com(ctl, 0, POINTER(_GUID), POINTER(c_void_p))(
                        byref(_GUID(IID_IAudioSessionControl2)), byref(ctl2)) < 0:
                    continue
                try:
                    pid = wintypes.DWORD()
                    # IAudioSessionControl2::GetProcessId（vtable 14）
                    if _com(ctl2, 14, POINTER(wintypes.DWORD))(byref(pid)) < 0:
                        continue
                    if not pid.value:
                        continue
                    exe = _pid_to_exe(pid.value)
                    if not exe:
                        continue
                    if pid.value == int(os.getpid()):
                        continue  # 排除自身
                    state = c_uint()
                    _com(ctl2, 3, POINTER(c_uint))(byref(state))  # GetState
                    out.append((exe, pid.value, state.value == 1))  # 1 = Active
                finally:
                    _release(ctl2)
            finally:
                _release(ctl)
    finally:
        _release(sess_enum)
        _release(mgr)


def _match_pid_from_sessions(target: str, sessions: list):
    """按名称匹配音频会话。精确 exe 名 > 子串；各自内部活跃优先。"""
    t = target.lower()
    exact = [s for s in sessions if s["name"].lower() == t]
    sub = [s for s in sessions if t in s["name"].lower()]
    for pool in (exact, sub):
        for s in pool:
            if s["active"]:
                return s["pid"]
        if pool:
            return pool[0]["pid"]
    return None


def _match_pid_from_snapshot(target: str):
    """按名称在进程快照中匹配（目标进程尚未产生音频会话时的兜底）。
    同名进程组取根进程（父进程不同名者）——INCLUDE_TARGET_PROCESS_TREE
    模式下抓根进程即可覆盖全部同名子进程（浏览器等多进程架构）"""
    t = target.lower()
    procs = _snapshot_processes()
    same = [(pid, ppid) for pid, ppid, exe in procs if t == exe or t in exe]
    if not same:
        return None
    same_pids = {pid for pid, _ in same}
    roots = [pid for pid, ppid in same if ppid not in same_pids]
    return roots[0] if roots else same[0][0]


def _format_sessions(sessions: list, limit: int = 12) -> str:
    if not sessions:
        return "（无）"
    return ", ".join(
        f"{s['name']}(PID {s['pid']}{'，发声中' if s['active'] else ''})"
        for s in sessions[:limit])


def _interactive_pick(sessions: list):
    """控制台交互选择音频会话；EOF 或取消返回 None"""
    if not sessions:
        print("当前没有已注册音频会话的应用（让目标应用播放一段声音后重试）。")
        return None
    print("\n已注册音频会话的应用:")
    for i, s in enumerate(sessions, 1):
        mark = "◉ 正在发声" if s["active"] else "○ 待机"
        print(f"  [{i}] {s['name']}  (PID {s['pid']}, {mark})")
    while True:
        try:
            raw = input("选择要捕获的应用编号（直接回车=1）: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return None
        if raw == "":
            raw = "1"
        if not raw.isdigit() or not (1 <= int(raw) <= len(sessions)):
            print(f"请输入 1-{len(sessions)} 之间的编号")
            continue
        chosen = sessions[int(raw) - 1]
        print(f"[音频来源] 已选择: {chosen['name']} (PID {chosen['pid']})")
        return chosen["pid"]


def resolve_target_pid(target: str, interactive: bool = True) -> int:
    """把捕获目标解析为 PID。

    target 支持：数字 PID / 进程名（或名字子串，如 "chrome"）/ 空串。
    空串时：interactive 且有控制台则列出会话供选择，否则自动取最近活跃会话。
    解析失败抛 RuntimeError（附当前可用会话清单，便于修正配置）。
    """
    sessions = list_audio_sessions()
    target = str(target or "").strip()

    if target.isdigit():
        pid = int(target)
        exe = _pid_to_exe(pid)
        if not exe:
            raise RuntimeError(f"PID {pid} 不存在或无权访问")
        logger.info(f"[音频来源] 按 PID 捕获: {exe} (PID {pid})")
        return pid

    if target:
        pid = _match_pid_from_sessions(target, sessions)
        if pid is not None:
            logger.info(f"[音频来源] '{target}' 匹配到音频会话: "
                        f"{_pid_to_exe(pid)} (PID {pid})")
            return pid
        pid = _match_pid_from_snapshot(target)
        if pid is not None:
            logger.info(f"[音频来源] '{target}' 匹配到进程: "
                        f"{_pid_to_exe(pid)} (PID {pid})（该进程当前无活跃音频会话）")
            return pid
        raise RuntimeError(
            f"找不到与 '{target}' 匹配的进程。当前有音频会话的应用: "
            f"{_format_sessions(sessions)}")

    # 未指定目标
    if interactive and sys.stdin is not None and sys.stdin.isatty():
        chosen = _interactive_pick(sessions)
        if chosen is not None:
            return chosen
        logger.warning("交互选择被取消，尝试自动选取最近活跃会话")

    active = [s for s in sessions if s["active"]]
    if active:
        s = active[0]
        logger.info(f"[音频来源] 自动选择最近活跃会话: {s['name']} (PID {s['pid']})")
        return s["pid"]
    raise RuntimeError(
        "当前没有正在发声的应用可选。请先让目标应用播放声音，"
        f"或在配置 audio.target_process 指定进程名/PID。已知音频会话: "
        f"{_format_sessions(sessions)}")


# ---------------- 完成处理器（IActivateAudioInterfaceCompletionHandler） ----------------

class _CompletionHandler:
    """最小 COM 对象：ActivateAudioInterfaceAsync 完成时置事件。

    vtable 手工实现（QI 只应答 IUnknown/ICompletionHandler/IAgileObject；
    AddRef/Release 为固定引用计数哑实现，生命周期由 Python 侧持有保证）。
    """

    _QI = ctypes.WINFUNCTYPE(HRESULT, c_void_p, POINTER(_GUID), POINTER(c_void_p))
    _REF = ctypes.WINFUNCTYPE(c_ulong, c_void_p)
    _DONE = ctypes.WINFUNCTYPE(HRESULT, c_void_p, c_void_p)
    _ACCEPTED = tuple(bytes(_GUID(s)) for s in (
        IID_IUnknown, IID_IActivateAudioInterfaceCompletionHandler, IID_IAgileObject))

    def __init__(self):
        self.done = threading.Event()

        def qi(this, riid, ppv):
            iid = bytes(riid.contents)
            if iid in self._ACCEPTED:
                ppv[0] = this
                return 0
            ppv[0] = None
            return E_NOINTERFACE

        def completed(this, op):
            self.done.set()
            return 0

        self._funcs = [self._QI(qi), self._REF(lambda t: 2),
                       self._REF(lambda t: 1), self._DONE(completed)]
        self._vtbl = (c_void_p * 4)(*[ctypes.cast(f, c_void_p) for f in self._funcs])
        self._box = (c_void_p * 1)(ctypes.cast(self._vtbl, c_void_p))
        self.ptr = ctypes.cast(self._box, c_void_p)


# ---------------- 格式转换 ----------------

def _resample(mono: np.ndarray, src_rate: int) -> np.ndarray:
    """任意采样率 → 16kHz。scipy polyphase 优先；缺 scipy 时整数比率走块均值兜底"""
    if len(mono) == 0:
        return mono
    src_rate = int(src_rate)
    if _scipy_signal is not None:
        g = gcd(TARGET_RATE, src_rate)
        up, down = TARGET_RATE // g, src_rate // g
        if (up, down) == (1, 1):
            return mono.astype(np.float32)
        return _scipy_signal.resample_poly(mono.astype(np.float64), up, down).astype(np.float32)
    step = src_rate // TARGET_RATE
    if step <= 1:
        return mono.astype(np.float32)
    n = (len(mono) // step) * step
    if n == 0:
        return np.empty(0, dtype=np.float32)
    return mono[:n].reshape(-1, step).mean(axis=1).astype(np.float32)


def _to_mono16k(samples: np.ndarray, rate: int, channels: int) -> np.ndarray:
    """float32（已归一化）多声道采样 → float32 单声道 16k"""
    if channels > 1:
        mono = samples.reshape(-1, channels).mean(axis=1)
    else:
        mono = samples
    if rate != TARGET_RATE:
        mono = _resample(mono, rate)
    return np.ascontiguousarray(mono, dtype=np.float32)


# ---------------- 捕获流 ----------------

class WasapiStream:
    """一个音频捕获源，独立线程持续产出 float32 单声道 16kHz 块。

    构造（三选一）：
      WasapiStream("input", device_name)     # 麦克风，device_name 空=默认录音设备
      WasapiStream("loopback", device_name)  # 扬声器回环（系统音频），空=默认播放设备
      WasapiStream("process", pid=1234)      # 指定进程树（含子进程）
    用法：start(on_packet)（失败抛 RuntimeError）；stop() 停止。
    on_packet(np.ndarray) 在捕获线程回调；运行中致命错误写入 self.error。
    """

    def __init__(self, kind: str, device_name: str = "", pid: int = None):
        assert kind in ("input", "loopback", "process")
        self.kind = kind
        self.device_name = (device_name or "").strip()
        self.pid = pid
        self.error = None
        self._stop_evt = threading.Event()
        self._ready_evt = threading.Event()
        self._init_error = None
        self._thread = None
        self._on_packet = None
        self._conv = None        # (rate, channels, bits) 实际交付格式
        self._last_zero_fill = 0.0

    # ---- 生命周期 ----
    def start(self, on_packet):
        """启动捕获线程并阻塞等待初始化完成（失败抛 RuntimeError）"""
        if self._thread is not None:
            raise RuntimeError("捕获流已启动")
        self._on_packet = on_packet
        if self.kind == "process" and not is_process_loopback_supported():
            raise RuntimeError("应用音频捕获需要 Windows 10 2004 (build 19041) 以上")
        self._thread = threading.Thread(target=self._run, name="wasapi-capture",
                                        daemon=True)
        self._thread.start()
        if not self._ready_evt.wait(timeout=8.0):
            self._stop_evt.set()
            self._thread.join(timeout=3.0)
            raise RuntimeError("音频捕获初始化超时")
        if self._init_error:
            self._thread.join(timeout=3.0)
            raise RuntimeError(self._init_error)

    def stop(self):
        self._stop_evt.set()
        if self._thread is not None:
            self._thread.join(timeout=3.0)
            self._thread = None

    def _emit(self, mono: np.ndarray):
        try:
            self._on_packet(mono)
        except Exception as e:  # 回调异常不拖垮捕获线程
            self.error = f"音频数据回调异常: {e}"

    # ---- 设备解析 ----
    def _resolve_device(self, enum_ptr, flow: int):
        """按名称（子串）匹配设备；空名称用默认设备。返回 IMMDevice*"""
        dev = c_void_p()
        if self.device_name:
            dev = self._find_device_by_name(enum_ptr, flow, self.device_name)
            if dev is None:
                kind_label = "录音" if flow == DATA_FLOW_CAPTURE else "播放"
                try:
                    names = [n for n, _ in _enum_device_names(flow)]
                except Exception:
                    names = []
                raise RuntimeError(
                    f"找不到名称含 '{self.device_name}' 的{kind_label}设备。可用设备: {names}")
            return dev
        _check(_com(enum_ptr, 4, c_uint, c_uint, POINTER(c_void_p))(
            flow, ROLE_CONSOLE, byref(dev)), "GetDefaultAudioEndpoint")
        return dev

    def _find_device_by_name(self, enum_ptr, flow: int, name: str):
        coll = c_void_p()
        try:
            _check(_com(enum_ptr, 3, c_uint, wintypes.DWORD, POINTER(c_void_p))(
                flow, DEVICE_STATE_ACTIVE, byref(coll)), "EnumAudioEndpoints")
            count = c_uint()
            _com(coll, 3, POINTER(c_uint))(byref(count))
            for i in range(count.value):
                dev = c_void_p()
                if _com(coll, 4, c_uint, POINTER(c_void_p))(i, byref(dev)) < 0:
                    continue
                try:
                    if name.lower() in _device_friendly_name(dev).lower():
                        return dev  # 所有权移交调用方
                except Exception:
                    _release(dev)
        finally:
            _release(coll)
        return None

    # ---- 初始化 ----
    def _activate_process_client(self, pid: int):
        """Application Loopback：异步激活指定进程树的虚拟捕获设备"""
        params = _ACTIVATION_PARAMS(1, pid, 0)  # PROCESS_LOOPBACK / INCLUDE_TARGET_TREE
        pv = _PROPVARIANT()
        pv.vt = VT_BLOB
        pv.blob.cbSize = ctypes.sizeof(params)
        pv.blob.pBlobData = ctypes.cast(byref(params), c_void_p)

        handler = _CompletionHandler()
        op = c_void_p()
        mmdev = ctypes.WinDLL("mmdevapi")
        mmdev.ActivateAudioInterfaceAsync.argtypes = [
            wintypes.LPCWSTR, POINTER(_GUID), POINTER(_PROPVARIANT),
            c_void_p, POINTER(c_void_p)]
        mmdev.ActivateAudioInterfaceAsync.restype = HRESULT
        _check(mmdev.ActivateAudioInterfaceAsync(
            VIRTUAL_AUDIO_DEVICE_PROCESS_LOOPBACK,
            byref(_GUID(IID_IAudioClient)), byref(pv), handler.ptr, byref(op)),
            "ActivateAudioInterfaceAsync")
        try:
            if not handler.done.wait(timeout=8.0):
                raise RuntimeError("ActivateAudioInterfaceAsync 完成事件超时")
            hr_act = HRESULT()
            client = c_void_p()
            _check(_com(op, 3, POINTER(HRESULT), POINTER(c_void_p))(
                byref(hr_act), byref(client)), "GetActivateResult")
            _check(hr_act.value, f"激活进程 {pid} 的音频捕获（目标进程可能已退出）")
            return client
        finally:
            _release(op)

    def _init_formats(self, client, flags: int):
        """依次尝试初始化格式，确定实际交付格式。

        AUTOCONVERTPCM 让音频引擎代做格式转换，优先请求 16k 单声道；
        设备模式兜底用引擎原生混合格式（可能是 16bit PCM 或 float32 可扩展格式）。
        返回 (rate, channels, bits)，bits=32 表示 float32、16 表示 int16。
        """
        init = _com(client, 3, c_uint, wintypes.DWORD, c_longlong,
                    c_longlong, POINTER(WAVEFORMATEX), c_void_p)
        last_err = None
        for rate, channels in ((TARGET_RATE, 1), (48000, 2)):
            f = _float_format(rate, channels)
            try:
                _check(init(AUDCLNT_SHAREMODE_SHARED, flags, 2000000, 0,
                            byref(f), None),  # 共享模式，200ms 缓冲
                       f"IAudioClient.Initialize({rate}Hz/{channels}ch)")
                return rate, channels, 32
            except OSError as e:
                last_err = e
        if self.kind == "process":
            raise last_err

        # 兜底：引擎原生混合格式
        fmt_ptr = c_void_p()
        _check(_com(client, 8, POINTER(c_void_p))(byref(fmt_ptr)), "GetMixFormat")
        try:
            f = WAVEFORMATEX.from_address(fmt_ptr.value)
            tag, ch, r, bits = (f.wFormatTag, f.nChannels,
                                f.nSamplesPerSec, f.wBitsPerSample)
            ok_float = bits == 32 and tag in (WAVE_FORMAT_IEEE_FLOAT, WAVE_FORMAT_EXTENSIBLE)
            ok_pcm16 = bits == 16 and tag == WAVE_FORMAT_PCM
            if not (ok_float or ok_pcm16):
                raise RuntimeError(
                    f"不支持的引擎混合格式 (tag={tag}, bits={bits})")
            _check(_com(client, 3, c_uint, wintypes.DWORD, c_longlong,
                        c_longlong, POINTER(WAVEFORMATEX), c_void_p)(
                    AUDCLNT_SHAREMODE_SHARED, flags & ~AUDCLNT_STREAMFLAGS_AUTOCONVERTPCM,
                    2000000, 0, byref(f), None), "IAudioClient.Initialize(mix)")
            return r, ch, bits
        finally:
            _ole32.CoTaskMemFree(fmt_ptr)

    # ---- 捕获线程 ----
    def _run(self):
        uninit = False
        client = c_void_p()
        capture = c_void_p()
        event = None
        started = False
        try:
            uninit = _coinit()

            # 1. 获取 IAudioClient
            if self.kind == "process":
                client = self._activate_process_client(self.pid)
                init_flags = (AUDCLNT_STREAMFLAGS_LOOPBACK |
                              AUDCLNT_STREAMFLAGS_EVENTCALLBACK |
                              AUDCLNT_STREAMFLAGS_AUTOCONVERTPCM |
                              AUDCLNT_STREAMFLAGS_SRC_DEFAULT_QUALITY)
            else:
                enum = c_void_p()
                try:
                    _check(_ole32.CoCreateInstance(
                        byref(_GUID(CLSID_MMDeviceEnumerator)), None,
                        CLSCTX_INPROC_SERVER,
                        byref(_GUID(IID_IMMDeviceEnumerator)), byref(enum)),
                        "CoCreateInstance")
                    flow = DATA_FLOW_CAPTURE if self.kind == "input" else DATA_FLOW_RENDER
                    dev = self._resolve_device(enum, flow)
                    try:
                        _check(_com(dev, 3, POINTER(_GUID), wintypes.DWORD,
                                    c_void_p, POINTER(c_void_p))(
                            byref(_GUID(IID_IAudioClient)), CLSCTX_INPROC_SERVER,
                            None, byref(client)), "IMMDevice.Activate")
                    finally:
                        _release(dev)
                finally:
                    _release(enum)
                init_flags = (AUDCLNT_STREAMFLAGS_EVENTCALLBACK |
                              AUDCLNT_STREAMFLAGS_AUTOCONVERTPCM |
                              AUDCLNT_STREAMFLAGS_SRC_DEFAULT_QUALITY)
                if self.kind == "loopback":
                    init_flags |= AUDCLNT_STREAMFLAGS_LOOPBACK

            # 2. 初始化格式
            try:
                rate, channels, bits = self._init_formats(client, init_flags)
            except OSError as e:
                raise RuntimeError(f"初始化音频流失败: {e}")
            self._conv = (rate, channels, bits)

            # 3. 事件句柄 + 捕获客户端 + Start
            event = _kernel32.CreateEventW(None, False, False, None)
            _check(_com(client, 13, c_void_p)(event), "SetEventHandle")
            _check(_com(client, 14, POINTER(_GUID), POINTER(c_void_p))(
                byref(_GUID(IID_IAudioCaptureClient)), byref(capture)),
                "GetService(IAudioCaptureClient)")
            _check(_com(client, 10)(), "Start")
            started = True
            self._ready_evt.set()

            # 4. 读包循环
            self._read_loop(capture, event, rate, channels, bits)

        except Exception as e:
            msg = str(e)
            if self._ready_evt.is_set():
                self.error = f"音频捕获中断: {msg}"
            else:
                self._init_error = msg
                self._ready_evt.set()
        finally:
            if started:
                try:
                    _com(client, 11)()  # Stop
                except Exception:
                    pass
            _release(capture)
            _release(client)
            if event:
                _kernel32.CloseHandle(event)
            if uninit:
                _ole32.CoUninitialize()

    def _read_loop(self, capture, event, rate, channels, bits):
        """事件驱动读包：WaitForSingleObject → 排空全部待处理包 → 回调"""
        get_next = _com(capture, 5, POINTER(c_uint))                  # GetNextPacketSize
        get_buf = _com(capture, 3, POINTER(c_void_p), POINTER(c_uint),
                       POINTER(wintypes.DWORD), c_void_p, c_void_p)   # GetBuffer
        rel_buf = _com(capture, 4, c_uint)                            # ReleaseBuffer

        def _deliver(samples: np.ndarray):
            mono = _to_mono16k(samples, rate, channels)
            self._emit(mono)

        while not self._stop_evt.is_set() and self.error is None:
            wait_ms = 200 if self.kind == "process" else 500
            if _kernel32.WaitForSingleObject(event, wait_ms) != 0:
                # 超时 = 无新包。目标进程静音（process）或设备空闲（loopback）时
                # WASAPI 会停发包；设备静音期产出零块，保持下游端点检测的静音节奏
                now = time.time()
                if now - self._last_zero_fill >= 0.1:
                    self._last_zero_fill = now
                    self._emit(np.zeros(int(TARGET_RATE * 0.1), dtype=np.float32))
                continue

            while not self._stop_evt.is_set() and self.error is None:
                n = c_uint()
                if get_next(byref(n)) < 0 or n.value == 0:
                    break
                data = c_void_p()
                frames = c_uint()
                flags = wintypes.DWORD()
                if get_buf(byref(data), byref(frames), byref(flags), None, None) < 0:
                    break
                try:
                    nf = frames.value
                    if nf == 0:
                        continue
                    if flags.value & AUDCLNT_BUFFERFLAGS_SILENT:
                        # 静音包：按帧数×声道数给零样本，走同一转换管线（等价零块）
                        _deliver(np.zeros(nf * channels, dtype=np.float32))
                        continue
                    if bits == 16:
                        buf = ctypes.string_at(data.value, nf * channels * 2)
                        samples = np.frombuffer(buf, dtype=np.int16).astype(np.float32) / 32768.0
                    else:
                        buf = ctypes.string_at(data.value, nf * channels * 4)
                        samples = np.frombuffer(buf, dtype=np.float32)
                    _deliver(samples)
                finally:
                    rel_buf(frames.value)
