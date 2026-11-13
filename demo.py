#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
系统演示脚本
展示各个模块的基本功能
"""

import time
from loguru import logger
from utils.helpers import load_config, setup_logging

def demo_audio_capture():
    """演示音频捕获功能"""
    logger.info("🎵 演示音频捕获功能...")
    
    try:
        from audio_capture.audio_capturer import AudioCapturer
        config = load_config()
        capturer = AudioCapturer(config)
        
        logger.info("启动音频捕获...")
        capturer.start_capture()
        
        logger.info("捕获音频5秒...")
        time.sleep(5)
        
        queue_size = capturer.get_queue_size()
        logger.info(f"音频队列大小: {queue_size}")
        
        if queue_size > 0:
            chunk = capturer.get_audio_chunk()
            if chunk:
                logger.info(f"音频块大小: {len(chunk)} 字节")
        
        capturer.stop_capture()
        logger.success("音频捕获演示完成 ✓")
        
    except Exception as e:
        logger.error(f"音频捕获演示失败: {e}")

def demo_whisper_transcription():
    """演示Whisper转写功能"""
    logger.info("🗣️ 演示Whisper转写功能...")
    
    try:
        from transcription.whisper_transcriber import WhisperTranscriber
        config = load_config()
        transcriber = WhisperTranscriber(config)
        
        logger.info("加载Whisper模型...")
        transcriber.load_model()
        
        # 创建测试音频（模拟静音）
        import numpy as np
        test_audio = np.zeros(16000, dtype=np.int16).tobytes()
        
        logger.info("执行转写测试...")
        result = transcriber.transcribe_sync(test_audio)
        
        logger.info(f"转写结果: '{result}'")
        logger.success("Whisper转写演示完成 ✓")
        
    except Exception as e:
        logger.error(f"Whisper转写演示失败: {e}")

def demo_translation():
    """演示翻译功能"""
    logger.info("🌍 演示翻译功能...")
    
    try:
        from translation.translator import TencentTranslator
        config = load_config()
        translator = TencentTranslator(config)
        
        logger.info("加载翻译模型...")
        translator.load_model()
        
        test_text = "你好，这是一个测试！"
        target_lang = "en"
        
        logger.info(f"翻译测试: {test_text}")
        result = translator.translate_sync(test_text, target_lang)
        
        logger.info(f"翻译结果: {result}")
        logger.success("翻译演示完成 ✓")
        
    except Exception as e:
        logger.error(f"翻译演示失败: {e}")

def demo_danmaku():
    """演示弹幕功能"""
    logger.info("💬 演示弹幕功能...")
    
    try:
        from danmaku.danmaku_window import DanmakuManager
        config = load_config()
        
        logger.info("启动弹幕系统...")
        manager = DanmakuManager(config)
        
        # 模拟弹幕数据
        danmaku_data = {
            'original': '欢迎使用实时字幕系统！',
            'translations': {
                'en': 'Welcome to real-time subtitle system!',
                'ja': 'リアルタイム字幕システムへようこそ！',
                'ko': '실시간 자막 시스템에 오신 것을 환영합니다!'
            },
            'timestamp': time.time()
        }
        
        logger.info("添加测试弹幕...")
        manager.add_danmaku(danmaku_data)
        
        logger.info("弹幕系统已启动，3秒后关闭...")
        time.sleep(3)
        
        manager.stop()
        logger.success("弹幕演示完成 ✓")
        
    except Exception as e:
        logger.error(f"弹幕演示失败: {e}")

def main():
    """主演示函数"""
    setup_logging("INFO")
    logger.info("🚀 开始系统功能演示")
    logger.info("=" * 50)
    
    # 按顺序演示各模块
    demos = [
        ("音频捕获", demo_audio_capture),
        ("语音转写", demo_whisper_transcription), 
        ("文本翻译", demo_translation),
        ("弹幕展示", demo_danmaku)
    ]
    
    for name, demo_func in demos:
        try:
            logger.info(f"\n📋 开始演示: {name}")
            logger.info("-" * 30)
            demo_func()
            time.sleep(1)  # 间隔时间
        except KeyboardInterrupt:
            logger.info("用户中断演示")
            break
        except Exception as e:
            logger.error(f"{name}演示异常: {e}")
            continue
    
    logger.info("\n" + "=" * 50)
    logger.success("🎉 所有演示完成！")
    logger.info("现在可以运行完整系统: python main.py")

if __name__ == "__main__":
    main()