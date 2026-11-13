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