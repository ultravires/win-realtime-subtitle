#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
系统测试脚本
"""

import time
import threading
from loguru import logger
from utils.helpers import load_config, setup_logging
from audio_capture.audio_capturer import AudioCapturer
from transcription.whisper_transcriber import WhisperTranscriber
from translation.translator import TencentTranslator

def test_audio_capture():
    """测试音频捕获功能"""
    logger.info("开始测试音频捕获...")
    
    config = load_config()
    capturer = AudioCapturer(config)
    
    try:
        capturer.start_capture()
        time.sleep(5)  # 捕获5秒音频
        
        chunks_received = 0
        while capturer.get_queue_size() > 0:
            chunk = capturer.get_audio_chunk(timeout=0.1)
            if chunk:
                chunks_received += 1
                logger.info(f"接收到音频块，大小: {len(chunk)} 字节")
        
        logger.info(f"音频捕获测试完成，共接收 {chunks_received} 个音频块")
        
    except Exception as e:
        logger.error(f"音频捕获测试失败: {e}")
    finally:
        capturer.stop_capture()

def test_whisper_transcription():
    """测试Whisper转写功能"""
    logger.info("开始测试Whisper转写...")
    
    config = load_config()
    transcriber = WhisperTranscriber(config)
    
    try:
        # 加载模型
        transcriber.load_model()
        
        # 创建测试音频数据（模拟1秒静音）
        import numpy as np
        test_audio = np.zeros(16000, dtype=np.int16).tobytes()
        
        # 执行转写
        result = transcriber.transcribe_sync(test_audio)
        logger.info(f"Whisper转写结果: '{result}'")
        
        logger.info("Whisper转写测试完成")
        
    except Exception as e:
        logger.error(f"Whisper转写测试失败: {e}")

def test_translation():
    """测试翻译功能"""
    logger.info("开始测试翻译功能...")
    
    config = load_config()
    translator = TencentTranslator(config)
    
    try:
        # 加载模型
        translator.load_model()
        
        # 测试文本
        test_text = "你好，世界！"
        target_lang = "en"
        
        # 执行翻译
        result = translator.translate_sync(test_text, target_lang)
        logger.info(f"翻译结果: '{test_text}' -> '{result}'")
        
        logger.info("翻译测试完成")
        
    except Exception as e:
        logger.error(f"翻译测试失败: {e}")

def test_integration():
    """测试集成流程"""
    logger.info("开始测试集成流程...")
    
    config = load_config()
    
    # 初始化组件
    audio_capturer = AudioCapturer(config)
    transcriber = WhisperTranscriber(config)
    translator = TencentTranslator(config)
    
    text_queue = queue.Queue()
    translation_queue = queue.Queue()
    
    try:
        # 启动各组件
        audio_capturer.start_capture()
        transcriber.start_transcription(audio_capturer.audio_queue, text_queue)
        translator.start_translation(text_queue, translation_queue)
        
        logger.info("集成测试开始，播放一些音频内容...")
        time.sleep(10)  # 运行10秒
        
        # 检查结果
        results = []
        while not translation_queue.empty():
            result = translation_queue.get()
            results.append(result)
            logger.info(f"翻译结果: {result}")
        
        logger.info(f"集成测试完成，共获得 {len(results)} 条翻译结果")
        
    except Exception as e:
        logger.error(f"集成测试失败: {e}")
    finally:
        # 清理
        audio_capturer.stop_capture()
        transcriber.stop_transcription()
        translator.stop_translation()

if __name__ == "__main__":
    setup_logging("DEBUG")
    
    import argparse
    import queue
    
    parser = argparse.ArgumentParser(description="系统测试脚本")
    parser.add_argument("--test", choices=["audio", "whisper", "translation", "integration"], 
                       default="integration", help="选择要测试的模块")
    
    args = parser.parse_args()
    
    if args.test == "audio":
        test_audio_capture()
    elif args.test == "whisper":
        test_whisper_transcription()
    elif args.test == "translation":
        test_translation()
    elif args.test == "integration":
        test_integration()