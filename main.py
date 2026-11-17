#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
实时多语言字幕系统主程序 - 优化版本
"""

import threading
import queue
import signal
import sys
import time
from loguru import logger

from utils.helpers import load_config, setup_logging

# 注意：重型依赖（torch / transformers / faster-whisper / sherpa-onnx / PyQt6）
# 不在模块顶部导入，而是在 RealtimeSubtitleSystem.__init__ 中、日志系统就绪后
# 带计时导入——否则启动最初十几秒到几十秒完全无输出，看起来像卡死。

class RealtimeSubtitleSystem:
    """实时字幕系统主类 - 优化版本"""
    
    def __init__(self, config_path: str = "config/config.yaml"):
        # 加载配置
        self.config = load_config(config_path)

        # 设置日志（尽早，保证后续每个阶段都有输出）
        setup_logging(self.config['system']['log_level'])
        logger.info(f"配置加载完成 ({config_path})")

        # 重型依赖带计时导入
        t0 = time.time()
        logger.info("正在导入深度学习依赖（torch / transformers / sherpa-onnx / PyQt6），约 15-40 秒...")
        from audio_capture.audio_capturer import AudioCapturer
        from transcription.whisper_transcriber import WhisperTranscriber
        from transcription.streaming_transcriber import StreamingTranscriber
        from translation.translator import TencentTranslator
        from translation.simple_translator import SimpleTranslator
        from danmaku.danmaku_window import DanmakuManager
        logger.info(f"依赖导入完成，耗时 {time.time() - t0:.1f}s")

        # 初始化各模块（按配置选择识别后端）
        self.audio_capturer = AudioCapturer(self.config)
        backend = self.config['whisper'].get('backend', 'faster-whisper')
        if backend == 'sherpa-stream':
            self.whisper_transcriber = StreamingTranscriber(self.config)
            logger.info("识别后端: sherpa-onnx 真流式")
        else:
            self.whisper_transcriber = WhisperTranscriber(self.config)
            logger.info("识别后端: faster-whisper 窗口式")
        
        # 尝试初始化腾讯翻译器，失败则使用简化版
        self.translator = self._init_translator()
        
        self.danmaku_manager = DanmakuManager(self.config)
        
        # 数据队列
        self.text_queue = queue.Queue(maxsize=10)
        self.translation_queue = queue.Queue(maxsize=20)
        
        # 系统状态
        self.running = False
        self._stopped = False
        self._fully_stopped = False
        self.threads = []
        
        # 性能监控
        self.performance_stats = {
            'start_time': None,
            'last_stats_time': None
        }
        
        # 注册信号处理器
        signal.signal(signal.SIGINT, self._signal_handler)
        signal.signal(signal.SIGTERM, self._signal_handler)
        
    def _init_translator(self):
        """初始化翻译器，带错误处理"""
        try:
            # 首先尝试腾讯翻译器
            translator = TencentTranslator(self.config)
            translator.load_model()
            logger.info("腾讯翻译器初始化成功")
            return translator
        except Exception as e:
            logger.warning(f"腾讯翻译器初始化失败: {e}")
            logger.info("切换到简化翻译器")
            try:
                # 使用简化翻译器作为备选
                simple_translator = SimpleTranslator(self.config)
                simple_translator.load_model()
                logger.info("简化翻译器初始化成功")
                return simple_translator
            except Exception as e2:
                logger.error(f"简化翻译器也初始化失败: {e2}")
                logger.warning("系统将在无翻译模式下运行（仅显示原文）")
                return None
    
    def _signal_handler(self, signum, frame):
        """信号处理器"""
        logger.info(f"接收到信号 {signum}，正在关闭系统...")
        self.stop()
        sys.exit(0)
    
    def start(self):
        """启动系统"""
        try:
            logger.info("正在启动实时字幕系统...")
            import time
            self.performance_stats['start_time'] = time.time()
            
            # 启动音频捕获
            self.audio_capturer.start_capture()
            
            # 启动语音转写
            self.whisper_transcriber.start_transcription(
                self.audio_capturer.audio_queue, 
                self.text_queue
            )
            
            # 启动翻译服务（如果有翻译器）
            if self.translator is not None:
                self.translator.start_translation(
                    self.text_queue, 
                    self.translation_queue
                )
            else:
                # 无翻译模式：直接将文本转发到翻译队列
                self._start_passthrough_mode()
            
            # 启动弹幕展示（在主线程中）
            # self._start_danmaku_thread()
            
            # self.running = True
            # logger.info("实时字幕系统启动完成")
            
            # 启动监控线程
            self._start_monitor_thread()

            # 启动退出看门狗
            self._start_exit_watchdog()

            # 等待系统运行
            self._wait_for_shutdown()
            
        except Exception as e:
            logger.error(f"系统启动失败: {e}")
            self.stop()
            raise
    
    def _start_passthrough_mode(self):
        """启动直通模式（无翻译）"""
        def passthrough_worker():
            while self.running:
                try:
                    text_item = self.text_queue.get(timeout=1.0)
                    if text_item:
                        # 处理可能的元数据格式
                        if isinstance(text_item, dict):
                            text = text_item['text']
                        else:
                            text = text_item
                            
                        # 构造只有原文的结果
                        result = {
                            'original': text,
                            'translations': {'en': text, 'ja': text, 'ko': text},
                            'timestamp': self._get_current_timestamp()
                        }
                        self.translation_queue.put(result, timeout=0.1)
                except queue.Empty:
                    continue
                except Exception as e:
                    logger.error(f"直通模式错误: {e}")
        
        thread = threading.Thread(target=passthrough_worker, daemon=True)
        thread.start()
        self.threads.append(thread)
        logger.info("启动直通模式（无翻译）")
    
    def _start_danmaku_thread(self):
        """启动弹幕线程"""
        def danmaku_worker():
            try:
                while self.running:
                    try:
                        # 从翻译队列获取数据
                        translation_data = self.translation_queue.get(timeout=1.0)
                        # 添加到弹幕系统
                        self.danmaku_manager.add_danmaku(translation_data)
                    except queue.Empty:
                        continue
                    except Exception as e:
                        logger.error(f"弹幕处理错误: {e}")
                        continue
                        
            except Exception as e:
                logger.error(f"弹幕线程异常: {e}")
        
        thread = threading.Thread(target=danmaku_worker, daemon=True)
        thread.start()
        self.threads.append(thread)
    
    def _start_exit_watchdog(self):
        """退出看门狗：确保进程真正退出。

        Ctrl+C 时信号处理器抛出的 SystemExit 可能被阻塞在 input() 中的主线程吞掉
        （Windows 控制台实测如此），导致 stop() 完成后进程仍然滞留。
        本看门狗运行在独立线程，不依赖异常传播。"""
        def watchdog():
            deadline = None
            import os
            import time
            while True:
                if self._stopped:
                    if self._fully_stopped:
                        os._exit(0)
                    if deadline is None:
                        deadline = time.time() + 15
                    elif time.time() > deadline:
                        # stop() 卡死超过15秒，强制退出
                        os._exit(0)
                time.sleep(0.2)

        thread = threading.Thread(target=watchdog, daemon=True)
        thread.start()
        self.threads.append(thread)
        logger.info("退出看门狗已启动")

    def _start_monitor_thread(self):
        """启动监控线程 - 增强版本"""
        def monitor_worker():
            while self.running:
                try:
                    # 监控各模块状态
                    self._check_module_status()
                    
                    # 记录系统统计信息
                    self._log_system_stats()
                    
                    # 每30秒输出一次详细性能报告
                    import time
                    current_time = time.time()
                    if (self.performance_stats['last_stats_time'] is None or 
                        current_time - self.performance_stats['last_stats_time'] >= 30):
                        self._log_detailed_performance_report()
                        self.performance_stats['last_stats_time'] = current_time
                    
                    # 睡眠一段时间
                    time.sleep(5)  # 从10秒改为5秒，更及时的监控
                    
                except Exception as e:
                    logger.error(f"监控线程错误: {e}")
        
        thread = threading.Thread(target=monitor_worker, daemon=True)
        thread.start()
        self.threads.append(thread)
    
    def _check_module_status(self):
        """检查各模块运行状态"""
        statuses = {
            'audio_capture': self.audio_capturer.running,
            'transcription': self.whisper_transcriber.running,
            'translation': self.translator.running if self.translator else True,
            'danmaku': self.danmaku_manager.running
        }
        
        for module, status in statuses.items():
            if not status:
                logger.warning(f"模块 {module} 未正常运行")
    
    def _log_system_stats(self):
        """记录系统统计信息"""
        try:
            # 获取音频捕获器的详细统计
            audio_stats = self.audio_capturer.get_performance_stats()
            
            stats = {
                'audio_queue_size': self.audio_capturer.get_queue_size(),
                'text_queue_size': self.text_queue.qsize(),
                'translation_queue_size': self.translation_queue.qsize(),
                'active_danmakus': self.danmaku_manager.window.get_active_count() if self.danmaku_manager.window else 0,
                'audio_capture_stats': audio_stats
            }
            
            # 只在队列压力较大时记录警告
            if (stats['audio_queue_size'] > self.config['audio']['buffer_size'] * 0.7 or
                stats['text_queue_size'] > 8 or
                stats['translation_queue_size'] > 15):
                logger.warning(f"系统负载较高: {stats}")
            else:
                logger.debug(f"系统统计: 音频队列{stats['audio_queue_size']}/{self.config['audio']['buffer_size']}, "
                           f"文本队列{stats['text_queue_size']}/10, "
                           f"翻译队列{stats['translation_queue_size']}/20")
            
        except Exception as e:
            logger.error(f"统计信息记录失败: {e}")
    
    def _log_detailed_performance_report(self):
        """记录详细性能报告"""
        try:
            import time
            current_time = time.time()
            runtime = current_time - self.performance_stats['start_time']
            
            audio_stats = self.audio_capturer.get_performance_stats()
            
            report = {
                '运行时间': f"{runtime:.1f}秒",
                '音频捕获统计': {
                    '总处理块数': audio_stats['total_chunks'],
                    '丢弃块数': audio_stats['dropped_chunks'],
                    '丢弃率': f"{audio_stats['drop_rate']*100:.2f}%",
                    '静音块数': audio_stats['silence_chunks'],
                    '平均处理延迟': f"{audio_stats['avg_processing_delay']*1000:.1f}ms"
                },
                '队列状态': {
                    '音频队列': f"{self.audio_capturer.get_queue_size()}/{self.config['audio']['buffer_size']}",
                    '文本队列': f"{self.text_queue.qsize()}/10",
                    '翻译队列': f"{self.translation_queue.qsize()}/20"
                }
            }
            
            logger.info("=" * 50)
            logger.info("系统性能详细报告:")
            for key, value in report.items():
                if isinstance(value, dict):
                    logger.info(f"  {key}:")
                    for sub_key, sub_value in value.items():
                        logger.info(f"    {sub_key}: {sub_value}")
                else:
                    logger.info(f"  {key}: {value}")
            logger.info("=" * 50)
            
        except Exception as e:
            logger.error(f"性能报告生成失败: {e}")
    
    def _wait_for_shutdown(self):
        """等待系统关闭"""
        try:
            # 这里可以添加更多的交互逻辑
            input("按回车键停止系统...\n")
        except KeyboardInterrupt:
            pass
        finally:
            self.stop()
            # CUDA/模型原生线程可能卡住解释器正常退出（实测会留下占内存的僵尸进程），
            # 各服务已全部停止，直接强制退出
            import os
            os._exit(0)
    
    def stop(self):
        """停止系统"""
        if self._stopped:
            return
        self._stopped = True
        logger.info("正在关闭实时字幕系统...")
        
        self.running = False
        
        # 停止各模块
        self.audio_capturer.stop_capture()
        self.whisper_transcriber.stop_transcription()
        if self.translator:
            self.translator.stop_translation()
        self.danmaku_manager.stop()
        
        # 输出最终性能统计
        self._log_detailed_performance_report()
        
        # 等待线程结束
        for thread in self.threads:
            thread.join(timeout=2)

        logger.info("实时字幕系统已关闭")
        self._fully_stopped = True
    
    def _get_current_timestamp(self) -> float:
        """获取当前时间戳"""
        import time
        return time.time()

def main():
    """主函数"""
    try:
        # 创建系统实例
        system = RealtimeSubtitleSystem()
        
        # 启动系统
        system.start()
        
    except KeyboardInterrupt:
        logger.info("用户中断程序")
    except Exception as e:
        logger.error(f"程序异常退出: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()