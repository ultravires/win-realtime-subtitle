@echo off
chcp 65001 > nul
echo ========================================
echo 实时字幕系统安装脚本
echo ========================================

echo 检查Python版本...
python --version > nul 2>&1
if %errorlevel% neq 0 (
    echo 错误: 未找到Python，请先安装Python 3.9或更高版本
    pause
    exit /b 1
)

echo 安装Python依赖包...
pip install -r requirements.txt
if %errorlevel% neq 0 (
    echo 警告: 部分依赖包安装失败，请手动检查
)

echo.
echo ========================================
echo FFmpeg安装说明:
echo ========================================
echo 请手动安装FFmpeg:
echo 1. 访问 https://ffmpeg.org/download.html
echo 2. 下载Windows版本
echo 3. 解压到任意目录
echo 4. 将bin目录添加到系统PATH环境变量
echo.
echo 或者使用包管理器安装:
echo winget install FFmpeg
echo ========================================

echo.
echo 安装完成！
echo 运行前请确保:
echo 1. FFmpeg已正确安装并添加到PATH
echo 2. 如使用GPU加速，请安装CUDA驱动
echo 3. 确保有足够的磁盘空间下载模型文件

pause