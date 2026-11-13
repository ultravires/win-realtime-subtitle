#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
环境检查脚本
"""

import sys
import importlib.util

def check_python_version():
    """检查Python版本"""
    version = sys.version_info
    print(f"Python版本: {version.major}.{version.minor}.{version.micro}")
    
    if version.major < 3 or (version.major == 3 and version.minor < 9):
        print("❌ Python版本过低，需要3.9或更高版本")
        return False
    else:
        print("✅ Python版本符合要求")
        return True

def check_package(package_name, install_name=None):
    """检查包是否已安装"""
    if install_name is None:
        install_name = package_name
    
    try:
        spec = importlib.util.find_spec(package_name)
        if spec is not None:
            print(f"✅ {package_name} 已安装")
            return True
        else:
            print(f"❌ {package_name} 未安装 (pip install {install_name})")
            return False
    except ImportError:
        print(f"❌ {package_name} 导入失败 (pip install {install_name})")
        return False

def main():
    print("=" * 50)
    print("实时字幕系统环境检查")
    print("=" * 50)
    
    # 检查Python版本
    python_ok = check_python_version()
    print()
    
    # 检查必需的包
    required_packages = [
        ("yaml", "pyyaml"),
        ("numpy", "numpy"),
        ("torch", "torch"),
        ("whisper", "openai-whisper"),
        ("transformers", "transformers"),
        ("PyQt6", "PyQt6"),
        ("sounddevice", "sounddevice"),
        ("ffmpeg", "ffmpeg-python"),
        ("loguru", "loguru")
    ]
    
    packages_ok = []
    for package, install_name in required_packages:
        ok = check_package(package, install_name)
        packages_ok.append(ok)
        print()
    
    # 检查FFmpeg
    import subprocess
    try:
        result = subprocess.run(["ffmpeg", "-version"], 
                              capture_output=True, text=True, timeout=5)
        if result.returncode == 0:
            print("✅ FFmpeg 已安装")
            ffmpeg_ok = True
        else:
            print("❌ FFmpeg 未正确安装")
            ffmpeg_ok = False
    except (subprocess.TimeoutExpired, FileNotFoundError):
        print("❌ FFmpeg 未找到 (请安装FFmpeg并添加到PATH)")
        ffmpeg_ok = False
    
    print("\n" + "=" * 50)
    print("检查总结:")
    print("=" * 50)
    
    if python_ok and all(packages_ok) and ffmpeg_ok:
        print("🎉 环境检查通过！可以运行系统")
        return True
    else:
        print("⚠️  环境检查未通过，请安装缺失的组件")
        print("\n安装建议:")
        print("1. 运行: pip install -r requirements.txt")
        print("2. 安装FFmpeg并添加到系统PATH")
        print("3. 如使用GPU，请安装CUDA驱动")
        return False

if __name__ == "__main__":
    success = main()
    sys.exit(0 if success else 1)