#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
性能测试脚本 - 验证优化效果
"""

import time
import threading
import queue
from loguru import logger

from utils.helpers import load_config, setup_logging
from audio_capture.audio_capturer import AudioCapturer
from transcription.whisper_transcriber import WhisperTranscriber

def performance_test(duration_seconds: int = 60):
    """性能测试函数"""
    print(f"开始进行 {duration_seconds} 秒的性能测试...")
    
    # 加载配置
    config = load_config("config/config.yaml")
    setup_logging("INFO")
    
    # 初始化模块
    audio_capturer = AudioCapturer(config)
    whisper_transcriber = WhisperTranscriber(config)
    
    # 文本队列
    text_queue = queue.Queue(maxsize=10)
    
    try:
        # 启动音频捕获
        audio_capturer.start_capture()
        logger.info("音频捕获已启动")
        
        # 启动转写服务
        whisper_transcriber.start_transcription(
            audio_capturer.audio_queue, 
            text_queue
        )
        logger.info("语音转写已启动")
        
        # 监控线程
        def monitor_stats():
            start_time = time.time()
            while time.time() - start_time < duration_seconds + 5:  # 多监控5秒
                time.sleep(5)
                stats = audio_capturer.get_performance_stats()
                queue_size = audio_capturer.get_queue_size()
                logger.info(f"监控: 队列大小={queue_size}, 总块数={stats['total_chunks']}, "
                          f"丢弃率={stats['drop_rate']*100:.2f}%")
        
        monitor_thread = threading.Thread(target=monitor_stats, daemon=True)
        monitor_thread.start()
        
        # 运行指定时间
        logger.info(f"测试运行 {duration_seconds} 秒...")
        time.sleep(duration_seconds)
        
        # 停止服务
        logger.info("测试结束，正在停止服务...")
        audio_capturer.stop_capture()
        whisper_transcriber.stop_transcription()
        
        # 输出最终统计
        final_stats = audio_capturer.get_performance_stats()
        logger.info("=== 性能测试结果 ===")
        logger.info(f"总运行时间: {duration_seconds} 秒")
        logger.info(f"处理的音频块总数: {final_stats['total_chunks']}")
        logger.info(f"丢弃的音频块数: {final_stats['dropped_chunks']}")
        logger.info(f"丢弃率: {final_stats['drop_rate']*100:.2f}%")
        logger.info(f"静音块数: {final_stats['silence_chunks']}")
        logger.info(f"平均处理延迟: {final_stats['avg_processing_delay']*1000:.2f} ms")
        
        # 评估结果
        if final_stats['drop_rate'] < 0.05:  # 丢弃率小于5%
            logger.info("✅ 性能优秀：丢弃率很低")
        elif final_stats['drop_rate'] < 0.15:  # 丢弃率小于15%
            logger.info("⚠️  性能良好：有一定丢弃但可接受")
        else:
            logger.warning("❌ 性能需要改进：丢弃率过高")
            
    except Exception as e:
        logger.error(f"性能测试失败: {e}")
    finally:
        # 确保清理
        audio_capturer.stop_capture()
        whisper_transcriber.stop_transcription()

if __name__ == "__main__":
    # 运行5分钟测试
    performance_test(300)  # 5分钟