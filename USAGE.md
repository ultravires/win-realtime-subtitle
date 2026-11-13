# 使用说明

## 快速开始

### 1. 环境准备

#### 系统要求
- Windows 10/11, Linux, 或 macOS
- Python 3.9 或更高版本
- 至少 8GB 内存（推荐 16GB）
- NVIDIA GPU（可选，但强烈推荐）

#### 安装步骤

1. **克隆项目**
```bash
git clone <repository-url>
cd realtime-subtitle
```

2. **运行安装脚本**
```bash
# Windows
install.bat

# Linux/macOS
pip install -r requirements.txt
```

3. **安装FFmpeg**
```bash
# Windows: 下载并添加到PATH
# https://ffmpeg.org/download.html

# Ubuntu/Debian
sudo apt update && sudo apt install ffmpeg

# macOS
brew install ffmpeg
```

### 2. 首次运行

1. **配置音频设备**
   - 确保系统音频设置中有"立体声混音"或类似选项
   - 在Windows中可能需要在录音设备中启用"立体声混音"

2. **运行系统**
```bash
# Windows
run.bat

# 或直接运行
python main.py
```

3. **系统启动后**
   - 会自动打开透明的弹幕窗口
   - 开始播放系统音频即可看到实时字幕

## 配置说明

### config.yaml 主要配置项

```yaml
# 音频设置
audio:
  sample_rate: 16000    # 采样率，Whisper要求16kHz
  chunk_duration: 2     # 音频块时长（秒），影响延迟
  buffer_size: 10       # 音频缓冲队列大小

# Whisper模型设置
whisper:
  model_size: "small"   # 模型大小：tiny/base/small/medium/large
  language: "zh"        # 识别语言
  device: "cuda"        # 计算设备：cuda/cpu

# 翻译设置
translation:
  target_languages:     # 目标语言列表
    - "en"             # 英语
    - "ja"             # 日语
    - "ko"             # 韩语

# 弹幕显示设置
danmaku:
  font_size: 16         # 字体大小
  speed: 8000           # 弹幕移动速度（毫秒）
  y_range: [800, 1000]  # 弹幕出现的垂直范围
```

## 性能优化建议

### 1. 硬件优化
- **GPU加速**：使用NVIDIA GPU可显著提升性能
- **内存**：16GB+内存可同时运行多个大模型
- **CPU**：多核CPU有助于并发处理

### 2. 模型选择
```yaml
# 低配置（CPU-only）
whisper:
  model_size: "tiny"    # 最快但精度较低
  device: "cpu"

# 中等配置（GPU）
whisper:
  model_size: "small"   # 平衡速度和精度
  device: "cuda"

# 高配置（高端GPU）
whisper:
  model_size: "medium"  # 更高精度
  device: "cuda"
```

### 3. 量化设置
```yaml
translation:
  quantization: "4bit"  # 降低显存占用，适合消费级GPU
```

## 故障排除

### 常见问题

1. **音频无法捕获**
   - 检查系统音频设置中的"立体声混音"设备
   - 确认FFmpeg已正确安装
   - 查看日志文件 `logs/app_*.log`

2. **Whisper模型加载失败**
   - 检查网络连接（首次运行需要下载模型）
   - 确认磁盘空间充足（模型文件约1-3GB）
   - 尝试切换到CPU模式

3. **翻译响应慢**
   - 降低模型量化级别
   - 减少目标翻译语言数量
   - 使用更快的模型变体

4. **弹幕显示异常**
   - 检查PyQt6是否正确安装
   - 确认显示器分辨率设置
   - 调整弹幕Y轴范围参数

### 日志查看
```bash
# 查看实时日志
tail -f logs/app_*.log

# Windows PowerShell
Get-Content logs\app_*.log -Wait
```

## 测试功能

### 模块独立测试
```bash
# 测试音频捕获
python test_system.py --test audio

# 测试Whisper转写
python test_system.py --test whisper

# 测试翻译功能
python test_system.py --test translation

# 测试完整流程
python test_system.py --test integration
```

## 高级用法

### 自定义翻译模型
在 `config.yaml` 中修改：
```yaml
translation:
  model_name: "your/custom-model-name"
```

### 多语言支持
添加更多目标语言：
```yaml
translation:
  target_languages:
    - "en"
    - "ja" 
    - "ko"
    - "fr"
    - "es"
    - "de"
```

### API模式（开发中）
未来将支持WebSocket API，可用于Web前端集成。

## 性能基准

| 配置 | 延迟 | 内存占用 | 显存占用 |
|------|------|----------|----------|
| CPU + Tiny | 2-3秒 | 4GB | N/A |
| GPU + Small | 1-2秒 | 6GB | 4GB |
| GPU + Medium | 2-4秒 | 8GB | 8GB |

注：延迟包括音频捕获、转写、翻译全流程时间。