@echo off
chcp 65001 > nul
echo ========================================
echo 启动实时字幕系统
echo ========================================

echo 检查依赖...
python -c "import whisper, transformers, PyQt6" > nul 2>&1
if %errorlevel% neq 0 (
    echo 错误: 依赖包未完全安装，请运行 install.bat
    pause
    exit /b 1
)

echo 检查FFmpeg...
ffmpeg -version > nul 2>&1
if %errorlevel% neq 0 (
    echo 错误: FFmpeg未找到，请确保已正确安装并添加到PATH
    pause
    exit /b 1
)

echo 启动系统...
python main.py

pause