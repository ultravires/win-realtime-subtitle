#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
CUDA环境诊断脚本
帮助检测和解决CUDA检测问题
"""

import sys
import os
import subprocess
from pathlib import Path

def check_nvidia_driver():
    """检查NVIDIA驱动"""
    print("🔍 检查NVIDIA驱动...")
    try:
        result = subprocess.run(['nvidia-smi'], capture_output=True, text=True, timeout=10)
        if result.returncode == 0:
            print("✅ NVIDIA驱动正常")
            print(result.stdout.split('\n')[0])  # 显示第一行驱动信息
            return True
        else:
            print("❌ NVIDIA驱动异常")
            return False
    except (subprocess.TimeoutExpired, FileNotFoundError):
        print("❌ 未找到nvidia-smi命令，请检查NVIDIA驱动安装")
        return False

def check_cuda_version():
    """检查CUDA版本"""
    print("\n🔍 检查CUDA版本...")
    try:
        import torch
        if torch.cuda.is_available():
            print(f"✅ PyTorch检测到CUDA: {torch.version.cuda}")
            print(f"✅ CUDA设备数量: {torch.cuda.device_count()}")
            for i in range(torch.cuda.device_count()):
                print(f"  设备{i}: {torch.cuda.get_device_name(i)}")
            return True
        else:
            print("❌ PyTorch无法检测到CUDA")
            print("  可能的原因:")
            print("  1. CUDA驱动版本不匹配")
            print("  2. PyTorch安装的不是CUDA版本")
            print("  3. 环境变量配置问题")
            return False
    except ImportError:
        print("❌ 未安装PyTorch")
        return False

def check_environment_variables():
    """检查环境变量"""
    print("\n🔍 检查环境变量...")
    cuda_path = os.environ.get('CUDA_PATH')
    if cuda_path:
        print(f"✅ CUDA_PATH: {cuda_path}")
    else:
        print("⚠️  未设置CUDA_PATH环境变量")
    
    path_vars = os.environ.get('PATH', '')
    cuda_in_path = any('cuda' in path.lower() for path in path_vars.split(';'))
    if cuda_in_path:
        print("✅ PATH中包含CUDA路径")
    else:
        print("⚠️  PATH中未发现CUDA路径")

def check_torch_installation():
    """检查PyTorch安装详情"""
    print("\n🔍 检查PyTorch安装...")
    try:
        import torch
        print(f"✅ PyTorch版本: {torch.__version__}")
        print(f"✅ PyTorch编译配置: {torch.__config__.show()}")
        
        # 检查是否有CUDA支持
        if hasattr(torch, 'cuda') and torch.cuda.is_available():
            print("✅ PyTorch支持CUDA")
        else:
            print("❌ PyTorch不支持CUDA")
            print("  建议重新安装支持CUDA的PyTorch版本:")
            print("  pip uninstall torch")
            print("  pip install torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cu118")
            
    except ImportError:
        print("❌ 未安装PyTorch")

def check_whisper_cuda():
    """检查Whisper的CUDA支持"""
    print("\n🔍 检查Whisper CUDA支持...")
    try:
        import whisper
        import torch
        
        # 尝试加载小模型测试CUDA
        print("尝试加载Whisper模型测试CUDA...")
        model = whisper.load_model("tiny", device="cuda" if torch.cuda.is_available() else "cpu")
        print(f"✅ Whisper模型加载成功，使用设备: {'CUDA' if torch.cuda.is_available() else 'CPU'}")
        
    except Exception as e:
        print(f"❌ Whisper CUDA测试失败: {e}")

def main():
    print("=" * 60)
    print("🤖 CUDA环境诊断工具")
    print("=" * 60)
    
    # 基本系统信息
    print(f"Python版本: {sys.version}")
    print(f"操作系统: {os.name}")
    print()
    
    # 逐项检查
    driver_ok = check_nvidia_driver()
    cuda_ok = check_cuda_version()
    env_ok = check_environment_variables()
    torch_ok = check_torch_installation()
    whisper_ok = check_whisper_cuda()
    
    print("\n" + "=" * 60)
    print("📋 诊断总结:")
    print("=" * 60)
    
    if driver_ok and cuda_ok and torch_ok:
        print("🎉 CUDA环境配置正常！")
        print("💡 建议检查:")
        print("   1. 配置文件中device设置是否为'cuda'")
        print("   2. 重启Python环境")
        print("   3. 检查是否有其他进程占用GPU")
    else:
        print("⚠️  CUDA环境存在问题")
        print("\n🔧 解决建议:")
        if not driver_ok:
            print("   1. 更新或重新安装NVIDIA显卡驱动")
        if not cuda_ok:
            print("   2. 安装支持CUDA的PyTorch版本")
        if not torch_ok:
            print("   3. 重新安装PyTorch: pip install torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cu118")
        print("   4. 检查环境变量PATH是否包含CUDA路径")
        print("   5. 重启终端和Python环境")

if __name__ == "__main__":
    main()