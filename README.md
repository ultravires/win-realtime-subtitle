# 实时多语言字幕系统 🎙️🌐

## 项目简介

这是一个基于Whisper和大语言模型的实时多语言字幕系统，能够实时捕获系统音频、进行语音识别，并将识别结果翻译成多种语言，最后以弹幕形式展示。

## ⚠️ 重要通知 - CUDA兼容性问题

**注意**: 如果您使用的是RTX 40系列显卡（如RTX 5070 Ti），可能会遇到CUDA兼容性问题。请参考 [CUDA兼容性解决方案](CUDA_COMPATIBILITY_SOLUTION.md) 文档。

### 临时解决方案
系统可以在CPU模式下正常运行：
```bash
# 安装CPU版本PyTorch
pip install torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cpu
```

## 🚀 主要特性

- 🔊 **系统音频实时捕获** - 支持Windows/Linux/macOS
- 🎤 **Whisper语音识别** - 高精度语音转文字
- 🌍 **多语言实时翻译** - 支持中英日韩等多种语言
- 💬 **弹幕式字幕展示** - 可自定义的弹幕窗口
- ⚡ **优化的性能架构** - 自适应缓冲区和流量控制

## 🔧 性能优化特性

### 📈 队列优化
- **增大缓冲区**：音频缓冲区从10提升到30个块
- **自适应管理**：根据系统负载动态调整缓冲策略
- **流量控制**：智能静音检测和数据丢弃策略

### ⚡ 处理优化
- **批量处理**：Whisper转写支持批量音频处理
- **参数调优**：优化beam_size和best_of参数提升速度
- **并发控制**：合理的线程池和队列大小配置

### 📊 监控功能
- **实时统计**：详细的性能指标监控
- **自动报告**：定期输出系统性能报告
- **负载预警**：队列压力过大时自动告警

## 📋 系统要求

### 硬件要求
- CPU: Intel i5或同等性能以上
- 内存: 8GB RAM（推荐16GB）
- 存储: 10GB可用空间
- GPU: CUDA兼容显卡（可选，用于加速）

### 软件要求
- Python 3.8+
- FFmpeg
- Windows 10+/Linux/macOS 10.15+

## 🛠️ 安装指南

### 1. 克隆项目
```bash
git clone <repository-url>
cd realtime-subtitle
```

### 2. 环境检查
```bash
# 运行环境检查脚本
python check_env.py
```

### 3. 安装依赖
```bash
# Windows用户
install.bat

# Linux/macOS用户
chmod +x install.sh
./install.sh
```

### 4. CUDA兼容性处理
如果遇到CUDA兼容性问题，请参考 [CUDA兼容性解决方案](CUDA_COMPATIBILITY_SOLUTION.md)

### 5. 快速测试
```bash
# 运行快速测试验证安装
python quick_test.py
```

### 6. 配置系统
编辑 `config/config.yaml` 文件：
```yaml
audio:
  buffer_size: 30  # 增大的缓冲区
  volume_threshold: 100  # 音量阈值
  adaptive_buffer: true  # 自适应缓冲

whisper:
  model_size: "large"  # 模型大小
  beam_size: 1  # 优化参数
  best_of: 1    # 优化参数
```

## ▶️ 使用方法

### 快速启动
```bash
# Windows
run.bat

# Linux/macOS
python main.py
```

### 性能测试
```bash
python test_performance.py
```

### 系统检查
```bash
python check_env.py
```

## 📊 性能监控

系统会自动输出性能统计信息：

```
=== 系统性能详细报告 ===
运行时间: 300.0秒
音频捕获统计:
  总处理块数: 150
  丢弃块数: 3
  丢弃率: 2.00%
  静音块数: 45
  平均处理延迟: 15.2ms
```

### 关键指标说明
- **丢弃率 < 5%**: 性能优秀
- **丢弃率 5%-15%**: 性能良好  
- **丢弃率 > 15%**: 需要优化

## ⚙️ 配置详解

### 音频配置
```yaml
audio:
  # 音频来源：microphone = 麦克风；system = 系统播放声音（扬声器回环）；
  #           application = 只捕获指定应用的声音（Win10 2004+，无需虚拟声卡）
  source: "microphone"
  device: ""             # 设备名子串（如 "HyperX"），留空 = 系统默认设备
  target_process: ""     # application 模式目标：进程名/PID，留空 = 启动时交互选择
  sample_rate: 16000     # 采样率（固定 16k 下游契约）
  channels: 1           # 声道数（自动混合为单声道）
  chunk_duration: 0.5   # 块时长(秒)
  buffer_size: 20       # 缓冲区大小
  volume_threshold: 30  # 静音阈值
  max_consecutive_silence: 5  # 最大连续静音
  adaptive_buffer: true # 自适应缓冲
```

音频来源可用 `python main.py --list-sources` 查看（列出录音/播放设备与正在
发声的应用）。捕获走 WASAPI（纯 ctypes），无需立体声混音或 virtual-audio-capturer。

### Whisper配置
```yaml
whisper:
  model_size: "large"   # 模型大小
  language: "zh"        # 识别语言
  device: "cuda"        # 设备类型
  beam_size: 1          # 束搜索大小(优化)
  best_of: 1           # 最佳候选数(优化)
```

## 🔧 故障排除

### 常见问题

1. **音频队列溢出**
   - 增加 `buffer_size` 配置值
   - 检查系统资源使用情况
   - 运行性能测试诊断问题

2. **识别准确率低**
   - 调整 `volume_threshold` 参数
   - 确保音频输入质量良好
   - 尝试不同的Whisper模型

3. **系统响应慢**
   - 降低 `model_size` 配置
   - 启用 `fp16` 半精度模式
   - 检查GPU驱动和CUDA版本

### 性能调优建议

1. **硬件优化**
   - 使用GPU加速Whisper推理
   - 增加系统内存
   - 使用SSD存储模型文件

2. **软件优化**
   - 根据实际需求调整模型大小
   - 合理设置缓冲区参数
   - 定期运行性能测试

## 📈 版本历史

### v2.0 (当前版本)
- ✨ 新增自适应缓冲区管理
- ✨ 优化队列处理机制
- ✨ 增强性能监控功能
- ✨ 改进批量处理效率
- ✨ 添加详细的性能测试工具

### v1.0
- 🎉 基础功能实现
- 🎉 支持多语言翻译
- 🎉 弹幕式字幕展示

## 🤝 贡献指南

欢迎提交Issue和Pull Request！

## 📄 许可证

MIT License

## 🙏 致谢

- OpenAI Whisper团队
- FFmpeg开发团队
- 所有开源贡献者