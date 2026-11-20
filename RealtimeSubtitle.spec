# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置 —— onedir 形态（dist/RealtimeSubtitle/）。

构建:  pyinstaller --noconfirm RealtimeSubtitle.spec
产物:  dist/RealtimeSubtitle/RealtimeSubtitle.exe（+ _internal 依赖目录）

约定：
- 采用 onedir 而非 onefile：torch/transformers 依赖体积数 GB，onefile
  每次启动要解压到临时目录，首启会拖到数分钟且翻倍占磁盘
- config/config.yaml 作为默认配置打包；首次运行释放到 exe 旁
  （main._prepare_frozen_environment），便于用户直接改配置
- 模型文件（models/ 约 800MB 流式+标点，翻译模型 15GB 走 HF 缓存）不打包，
  分发时放在 exe 旁的 models/ 目录即可；HF 缓存沿用用户目录
- 大体积依赖逐个 collect_all：transformers 子模块与数据文件、sherpa-onnx
  自带的 onnxruntime、ctranslate2/bitsandbytes 的原生 DLL，PyInstaller
  静态分析扫不到运行时动态加载的它们，缺一个启动即崩
"""

import importlib.util

from PyInstaller.utils.hooks import collect_all, copy_metadata

datas = [('config/config.yaml', 'config')]
binaries = []
hiddenimports = []

# torchcodec 的模块会被 torchaudio/datasets 的导入链拉进 PYZ，transformers 的
# audio_utils 在 import 时直接调 importlib.metadata.version("torchcodec")——
# 模块在而 dist-info 元数据不在，版本查询抛 StopIteration，整条模型类导入链
# 死亡（实测 HunYuanDenseV1ForCausalLM 报 "Could not import module ..."）。
# 收集其 dist-info 元数据，让冻结环境与源码环境行为一致。
if importlib.util.find_spec('torchcodec') is not None:
    datas += copy_metadata('torchcodec')
    print('[spec] 已收集 torchcodec dist-info 元数据')


def add_package(name: str):
    """collect_all 一个已安装的包；未安装则跳过（如可选依赖）"""
    if importlib.util.find_spec(name) is None:
        print(f'[spec] 跳过未安装包: {name}')
        return
    d, b, h = collect_all(name)
    datas.extend(d)
    binaries.extend(b)
    hiddenimports.extend(h)
    print(f'[spec] collect_all {name}: data={len(d)} binary={len(b)} hidden={len(h)}')


# 识别与翻译链路的原生依赖（动态加载，静态分析扫不到）
for _pkg in [
    'sherpa_onnx',        # 流式识别 + VAD（内嵌 onnxruntime）
    'transformers',       # 翻译模型（含 hunyuan_v1_dense 原生实现）
    'accelerate',         # device_map=auto 加载
    'tokenizers',         # 分词器原生库
    'safetensors',        # 权重加载
    'sentencepiece',      # 分词器可选后端
    'faster_whisper',     # 离线窗口式后端
    'ctranslate2',        # faster-whisper 推理引擎（原生 DLL）
    'bitsandbytes',       # 量化加载（config quantization 可选）
    'einops',             # 部分 HF 模型代码用到
    'huggingface_hub',    # 模型缓存解析
]:
    add_package(_pkg)

a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    excludes=[
        'tkinter',       # 未使用（PyQt6 GUI）
        'matplotlib',
        'IPython',
        'pytest',
    ],
    # transformers 懒加载注册表依赖 os.listdir(__path__) 扫描磁盘上的 .py 源文件
    # 构建（transformers/utils/import_utils.py create_import_structure_from_path）。
    # 若模块进 PYZ，__file__ 是 .pyc 路径，listdir 落空导致整个模型类注册表
    # 崩塌（实测 HunYuanDenseV1ForCausalLM / Marian tokenizer 全部报
    # "Could not import module ..."）。'py' 模式让整包以源文件落盘，
    # 运行时从磁盘解析，与源码运行行为完全一致。
    module_collection_mode={'transformers': 'py'},
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='RealtimeSubtitle',
    debug=False,
    strip=False,
    upx=False,      # CUDA/ONNX 大 DLL 压缩收益低且有兼容风险，禁用
    console=True,   # 保留控制台：console 显示模式与日志是主要可观测面
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name='RealtimeSubtitle',
)
