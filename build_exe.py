#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""一键构建 Windows 二进制：pyinstaller + 构建后修复。

用法:  python build_exe.py            # 构建（约 10-20 分钟）
       python build_exe.py --fix-only # 只对已有 dist 应用构建后修复

构建后修复（两项缺一不可，均为实测踩坑）：
1. 删除 _internal/PyQt6/Qt6/bin 下 PyQt6 自带的旧版 VC 运行库
   （MSVCP140.dll / VCRUNTIME140.dll / VCRUNTIME140_1.dll）。
   PyQt6 的 PyInstaller runtime hook 在主程序运行前把 Qt6/bin 加入
   DLL 搜索目录；torch 的 c10.dll 会先解析到 Qt 携带的旧版运行库，
   版本过旧导致 DllMain 初始化失败（WinError 1114）。删除后解析落到
   _internal 根的 PyInstaller 副本/系统 VC redist，Qt 与新版完全兼容。
2. transformers 以 'py' 模式落盘（见 RealtimeSubtitle.spec 的
   module_collection_mode）——懒加载注册表需要磁盘 .py 扫描。

产物: dist/RealtimeSubtitle/RealtimeSubtitle.exe（onedir）
分发: 把 models/ 目录放到 exe 旁（见 PACKAGING.md）
"""

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DIST = ROOT / "dist" / "RealtimeSubtitle"
INTERNAL = DIST / "_internal"

# Qt6/bin 中需删除的旧版 VC 运行库
QT_VC_DLLS = ["MSVCP140.dll", "VCRUNTIME140.dll", "VCRUNTIME140_1.dll"]


def fix_qt_vc_runtime():
    """删除 PyQt6 携带的旧版 VC 运行库（见模块 docstring 第 1 条）"""
    qt_bin = INTERNAL / "PyQt6" / "Qt6" / "bin"
    if not qt_bin.is_dir():
        print("[fix] 未找到 PyQt6/Qt6/bin，跳过")
        return
    removed = []
    for name in QT_VC_DLLS:
        target = qt_bin / name
        if target.exists():
            target.unlink()
            removed.append(name)
    if removed:
        print(f"[fix] 已删除 PyQt6 旧版 VC 运行库: {', '.join(removed)}")
    else:
        print("[fix] PyQt6 无旧版 VC 运行库，无需处理")


def link_models():
    """开发机验证用：exe 旁创建指向仓库 models/ 的目录联接（不复制文件）。

    分发时没有仓库 models/，改为手动把 models/ 目录复制到 exe 旁。
    """
    repo_models = ROOT / "models"
    dist_models = DIST / "models"
    if not repo_models.is_dir():
        print("[link] 仓库无 models/，跳过联接")
        return
    if dist_models.exists() or dist_models.is_symlink():
        print("[link] exe 旁已有 models，跳过")
        return
    r = subprocess.run(["cmd", "/c", "mklink", "/J", str(dist_models),
                        str(repo_models)], capture_output=True, text=True)
    if dist_models.is_dir():
        print(f"[link] 已创建 models 联接: {dist_models} -> {repo_models}")
    else:
        print(f"[link] 联接创建失败: {r.stdout} {r.stderr}")


def run_build():
    print("=== PyInstaller 构建（约 10-20 分钟）===")
    # COLLECT 阶段要删除旧 dist 目录；杀软扫描/资源管理器等瞬态句柄会令
    # 其报 WinError 32/5（"文件被占用"/"拒绝访问"），重试即可通过
    max_attempts = 3
    for attempt in range(1, max_attempts + 1):
        r = subprocess.run(
            [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean",
             str(ROOT / "RealtimeSubtitle.spec")],
            cwd=ROOT,
        )
        if r.returncode == 0:
            break
        print(f"PyInstaller 第 {attempt}/{max_attempts} 次失败（exit={r.returncode}）")
        if attempt == max_attempts:
            sys.exit(1)
        import time
        time.sleep(5)
    fix_qt_vc_runtime()
    link_models()
    exe = DIST / "RealtimeSubtitle.exe"
    if exe.exists():
        size_gb = sum(f.stat().st_size for f in DIST.rglob('*') if f.is_file()) / 1e9
        print(f"=== 构建完成: {exe}（{size_gb:.1f} GB）===")
        print("验证: 在 dist/RealtimeSubtitle 下运行 RealtimeSubtitle.exe --list-sources")
        print("分发: 把 models/ 目录放到 exe 旁（详见 PACKAGING.md）")
    else:
        print("未找到产物 exe，检查 build 日志")
        sys.exit(1)


if __name__ == "__main__":
    if "--fix-only" in sys.argv:
        fix_qt_vc_runtime()
    else:
        run_build()
