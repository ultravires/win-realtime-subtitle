# 二进制打包指南（PyInstaller）

## 构建

```bash
pip install pyinstaller          # 仅构建机器需要
python build_exe.py              # pyinstaller + 构建后修复（推荐）
# 等价手动流程:
pyinstaller --noconfirm RealtimeSubtitle.spec
python build_exe.py --fix-only
```

产物为 **onedir 目录形态**：`dist/RealtimeSubtitle/RealtimeSubtitle.exe`（外层 exe
+ `_internal/` 依赖目录）。不用 onefile：torch/transformers 依赖达数 GB，onefile
每次启动要解压到临时目录，首启数分钟且翻倍占磁盘。

## 两个必做的构建后处理（build_exe.py 自动执行）

1. **删除 `_internal/PyQt6/Qt6/bin` 的旧版 VC 运行库**（MSVCP140/VCRUNTIME140/
   VCRUNTIME140_1.dll）。PyQt6 的 runtime hook 会把 Qt6/bin 加入 DLL 搜索目录，
   torch 的 c10.dll 会解析到 Qt 携带的旧版运行库 → DllMain 初始化失败
   （WinError 1114）。删除后落到 _internal 根的 PyInstaller 副本/系统 redist。
2. **transformers 以 'py' 模式落盘**（spec 的 `module_collection_mode`）。
   transformers 懒加载注册表靠 `os.listdir(__path__)` 扫磁盘 .py 构建；进 PYZ
   则注册表崩塌，所有模型类报 "Could not import module ..."。

## 实测踩坑（勿回退）

- `module_collection_mode={'transformers': 'py'}` 不能删：删了翻译模型
  HunYuan 与所有 tokenizer 注册表在冻结环境全部失效
- Qt6/bin 三个 VC 运行库 DLL 不能留：删了之后 torch CUDA 导入成功
- bitsandbytes 的 cuda118 DLL 依赖告警可忽略（量化配置为 none 不加载）

## 分发内容

```
RealtimeSubtitle/
├── RealtimeSubtitle.exe          # 主程序
├── _internal/                    # 打包的依赖（torch/transformers/PyQt6/...）
├── config/config.yaml            # 首次运行自动从包内释放（可手工编辑）
└── models/                       # 手动放置（体积大，不随包分发）
    ├── silero_vad.onnx                                   # VAD（2MB）
    ├── sherpa-onnx-streaming-zipformer-bilingual-.../    # 流式识别（~487MB）
    └── sherpa-onnx-punct-ct-transformer-.../             # 标点恢复（~294MB）
```

- **模型目录**：整个 `models/` 从源码目录复制到 exe 旁（流式识别按 config
  `streaming.model_dir` 等相对路径解析，程序启动时工作目录切到 exe 所在目录）
- **翻译模型**：走 HuggingFace 缓存（`~/.cache/huggingface`，HY-MT-7B fp16 约
  15GB）。目标机器无缓存时首次运行会自动下载，或直接复制源机器的 hub 缓存
- **配置**：`config/config.yaml` 缺失时自动从程序包释放默认配置

## 运行与验证

```bash
RealtimeSubtitle.exe --list-sources          # 不加载模型，列设备/应用会话
RealtimeSubtitle.exe --no-translate          # 只识别不翻译（省显存，启动快）
RealtimeSubtitle.exe --audio-source system   # CLI 覆盖声音来源
RealtimeSubtitle.exe --config other.yaml     # 指定配置文件
```

GUI 与源码运行一致：字幕浮窗右键"设置"改声音来源与翻译语言，回车或 Ctrl+C 退出。

## 构建注意

- spec 里对 `transformers / sherpa_onnx / ctranslate2 / bitsandbytes / accelerate`
  等逐个 `collect_all`：这些包的子模块与原生 DLL 是运行时动态加载的，PyInstaller
  静态分析扫不到，缺一个启动即崩。新增依赖（尤其带原生扩展的）记得加进 spec 的
  `add_package` 列表
- `upx=False`：CUDA/ONNX 大 DLL 压缩收益低且有兼容风险
- 翻译模型 HunYuan 在 transformers 5.x 已原生支持
  （`transformers.models.hunyuan_v1_dense`），无需处理 trust_remote_code 动态代码
