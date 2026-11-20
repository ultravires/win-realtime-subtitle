import yaml
import logging
from pathlib import Path
from loguru import logger

def load_config(config_path: str = "config/config.yaml") -> dict:
    """加载YAML配置文件"""
    try:
        with open(config_path, 'r', encoding='utf-8') as f:
            config = yaml.safe_load(f)
        return config
    except Exception as e:
        logger.error(f"配置文件加载失败: {e}")
        raise

def save_config(config: dict, config_path: str = "config/config.yaml"):
    """保存配置到 YAML 文件（界面设置写回用）。

    yaml.safe_dump 会重写整个文件（原手工注释丢失，文件头已注明）。
    """
    header = ("# 实时字幕系统配置（可手工编辑，也可在字幕浮窗右键\"设置\"中修改）\n"
              "# 注意：经界面保存会重新生成本文件，此前的手工注释会被覆盖。\n")
    with open(config_path, 'w', encoding='utf-8') as f:
        f.write(header)
        yaml.safe_dump(config, f, allow_unicode=True, sort_keys=False,
                       default_flow_style=False)

def setup_logging(log_level: str = "INFO"):
    """设置日志配置"""
    logger.remove()  # 移除默认处理器
    logger.add(
        "logs/app_{time}.log",
        rotation="100 MB",
        retention="7 days",
        level=log_level,
        format="{time:YYYY-MM-DD HH:mm:ss} | {level} | {name}:{function}:{line} | {message}",
        encoding="utf-8"
    )
    logger.add(
        lambda msg: print(msg, end=""),
        level=log_level,
        format="<green>{time:HH:mm:ss}</green> | <level>{level: <8}</level> | <cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - <level>{message}</level>"
    )

def get_device():
    """获取可用的计算设备"""
    import torch
    if torch.cuda.is_available():
        return "cuda"
    else:
        return "cpu"

def format_danmaku_text(original_text: str, translations: dict) -> str:
    """格式化弹幕文本"""
    parts = [original_text]
    for lang, trans_text in translations.items():
        if trans_text:
            parts.append(trans_text)
    return " | ".join(parts)

def sanitize_text(text: str) -> str:
    """清理文本，去除多余空白和特殊字符"""
    if not text:
        return ""
    # 去除首尾空白
    text = text.strip()
    # 合并多个空格为一个
    import re
    text = re.sub(r'\s+', ' ', text)
    return text