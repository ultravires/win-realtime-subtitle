#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
快速测试脚本 - 验证基础功能和简单性能
"""

import time
import threading
import queue
from loguru import logger

from utils.helpers import load_config, setup_logging
from audio_capture.audio_capturer import AudioCapturer

def quick_test(duration_seconds: int = 30):
    """快速测试函数"""
    print(f"开始进行 {duration_seconds} 秒的快速测试...")
    
    # 加载配置
    config = load_config("config/config.yaml")
    setup_logging("INFO")
    
    # 初始化音频捕获器
    audio_capturer = AudioCapturer(config)
    
    try:
        # 启动音频捕获
        audio_capturer.start_capture()
        logger.info("音频捕获已启动")
        
        # 监控线程
        def monitor_stats():
            start_time = time.time()
            while time.time() - start_time < duration_seconds + 5:
                time.sleep(3)  # 每3秒检查一次
                stats = audio_capturer.get_performance_stats()
                queue_size = audio_capturer.get_queue_size()
                logger.info(f"监控: 队列={queue_size}/{config['audio']['buffer_size']}, "
                          f"总={stats['total_chunks']}, 丢弃={stats['dropped_chunks']}")
        
        monitor_thread = threading.Thread(target=monitor_stats, daemon=True)
        monitor_thread.start()
        
        # 运行指定时间
        logger.info(f"测试运行 {duration_seconds} 秒...")
        time.sleep(duration_seconds)
        
        # 停止服务
        logger.info("测试结束，正在停止服务...")
        audio_capturer.stop_capture()
        
        # 输出最终统计
        final_stats = audio_capturer.get_performance_stats()
        print("\n" + "="*50)
        print("快速测试结果:")
        print(f"运行时间: {duration_seconds} 秒")
        print(f"处理块数: {final_stats['total_chunks']}")
        print(f"丢弃块数: {final_stats['dropped_chunks']}")
        print(f"丢弃率: {final_stats['drop_rate']*100:.2f}%")
        print(f"静音块数: {final_stats['silence_chunks']}")
        print(f"平均延迟: {final_stats['avg_processing_delay']*1000:.1f} ms")
        print("="*50)
        
        # 简单评估
        if final_stats['drop_rate'] < 0.05:
            print("✅ 优化效果良好：丢弃率 < 5%")
        elif final_stats['drop_rate'] < 0.15:
            print("⚠️  优化效果一般：丢弃率 5%-15%")
        else:
            print("❌ 仍需进一步优化：丢弃率 > 15%")
            
    except Exception as e:
        logger.error(f"快速测试失败: {e}")
    finally:
        audio_capturer.stop_capture()

if __name__ == "__main__":
    quick_test(30)  # 30秒快速测试