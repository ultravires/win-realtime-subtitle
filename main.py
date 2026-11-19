#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
实时多语言字幕系统主程序 - 优化版本
"""

import os
import threading
import queue
import signal
import sys
import time
import argparse
from loguru import logger

from utils.helpers import load_config, setup_logging

# 注意：重型依赖（torch / transformers / faster-whisper / sherpa-onnx / PyQt6）
# 不在模块顶部导入，而是在 RealtimeSubtitleSystem.__init__ 中、日志系统就绪后
# 带计时导入——否则启动最初十几秒到几十秒完全无输出，看起来像卡死。

# 关闭流程兜底时限：任何退出请求（Ctrl+C / 回车 / stdin EOF）置位后，超过此时限
# 仍未完成优雅关闭，看门狗直接强制退出，保证进程绝不永久卡死
SHUTDOWN_FORCE_EXIT_SEC = 20.0


class RealtimeSubtitleSystem:
    """实时字幕系统主类 - 优化版本"""

    def __init__(self, config_path: str = "config/config.yaml", overrides: dict = None):
        # ---- 退出状态标志：必须最先定义（信号处理器/看门狗任何时刻都可能访问）----
        self.running = False
        self._stopped = False
        self._fully_stopped = False
        self._gui_mode = False
        self._signal_stop = False   # Ctrl+C / SIGTERM 置位
        self._stdin_stop = False    # 回车 / stdin EOF 置位
        self.threads = []
        self.display_manager = None
        self.performance_stats = {'start_time': None}

        # 尽早注册信号处理器：启动阶段（模型加载等长耗时操作）按下 Ctrl+C 也能被记录。
        # 信号处理器只置标志，绝不在处理器内执行 stop()/sys.exit()——Windows 控制台实测，
        # 主线程阻塞在 input()/C 扩展里时，处理器内的 SystemExit 会被吞掉或打断半途的清理，
        # 这是此前 Ctrl+C 卡死的根源之一；统一改为"置标志 → 主线程/事件循环感知 → 主线程清理"
        signal.signal(signal.SIGINT, self._signal_handler)
        signal.signal(signal.SIGTERM, self._signal_handler)

        # 退出看门狗：独立线程兜底，保证置位后 SHUTDOWN_FORCE_EXIT_SEC 内进程必退。
        # 在 __init__ 启动（而非 start()），覆盖模型加载等启动阶段的 Ctrl+C
        self._start_exit_watchdog()

        # 加载配置（CLI 覆盖项随后应用，优先级高于配置文件）
        self.config = load_config(config_path)
        for key, value in (overrides or {}).items():
            if value:
                self.config['audio'][key] = value
                logger.info(f"CLI 覆盖音频配置: audio.{key} = {value}")

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
        from danmaku.subtitle_window import SubtitleManager
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

        # 字幕显示管理器：按 display_mode 创建（subtitle 字幕浮窗 / danmaku 弹幕 / console 控制台）
        display_mode = self.config['system'].get('display_mode', 'subtitle')
        if display_mode == 'danmaku':
            self.display_manager = DanmakuManager(self.config)
        elif display_mode == 'subtitle':
            self.display_manager = SubtitleManager(self.config)
        else:
            self.display_manager = None  # console 模式不需要 GUI 管理器

        # 数据队列
        self.text_queue = queue.Queue(maxsize=10)
        self.translation_queue = queue.Queue(maxsize=20)

    def _init_translator(self):
        """初始化翻译器，带错误处理"""
        # 翻译器类在 __init__ 的计时导入块中已随模块加载（sys.modules 已缓存），
        # 但 from import 在函数体内绑定的是局部名，跨方法不可见，
        # 此处需重新绑定到本方法作用域（缓存命中，无额外导入耗时）
        from translation.translator import TencentTranslator
        from translation.simple_translator import SimpleTranslator
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
        """信号处理器：只置退出标志，不在处理器内执行清理或退出。

        - GUI 模式：主线程阻塞在 Qt 事件循环内（异常无法从 C 层传递出来），
          置标志 + 线程安全地请求退出事件循环；事件循环退出后 _wait_for_shutdown
          在主线程完成完整清理
        - 控制台模式：主线程轮询退出标志，200ms 内感知并完成清理
        - 看门狗线程兜底：标志置位后超时未完成清理则强制退出进程
        """
        self._signal_stop = True
        logger.info(f"接收到信号 {signum}，正在关闭系统...")
        if self._gui_mode and self.display_manager is not None:
            self.display_manager.request_quit()

    def start(self):
        """启动系统"""
        try:
            logger.info("正在启动实时字幕系统...")
            self.performance_stats['start_time'] = time.time()

            # 启动音频捕获
            self.audio_capturer.start_capture()

            # 启动语音转写：流式后端额外接 display_queue（translation_queue 兼作
            # 显示通道），分段原文稳定即直推浮窗上屏，不等翻译
            backend = self.config['whisper'].get('backend', 'faster-whisper')
            if backend == 'sherpa-stream':
                self.whisper_transcriber.start_transcription(
                    self.audio_capturer.audio_queue,
                    self.text_queue,
                    display_queue=self.translation_queue
                )
            else:
                self.whisper_transcriber.start_transcription(
                    self.audio_capturer.audio_queue,
                    self.text_queue
                )

            # 运行标志必须在启动任何 worker 线程之前置位：
            # 所有消费线程都是 while self.running 循环，不置位会导致线程
            # 启动即退出、text_queue 无人消费直至打满、转写结果被丢弃
            self.running = True

            # 启动翻译服务（如果有翻译器）
            if self.translator is not None:
                self.translator.start_translation(
                    self.text_queue,
                    self.translation_queue
                )
            else:
                # 无翻译模式：直接将文本转发到翻译队列
                self._start_passthrough_mode()

            # 字幕显示：按配置选择字幕浮窗 / 弹幕 / 控制台日志。
            # 无论哪种模式 translation_queue 都必须有消费者，否则打满后
            # 背压会层层传导回 text_queue，字幕被丢弃。
            display_mode = self.config['system'].get('display_mode', 'subtitle')
            if display_mode == 'console' or self.display_manager is None:
                self._gui_mode = False
                self._start_display_worker()
            else:
                # 必须在启动消费线程前置位：信号处理器按此选择关闭路径
                self._gui_mode = True
                self._start_display_gui()

            # 启动监控线程
            self._start_monitor_thread()

            # 等待系统运行
            self._wait_for_shutdown()

        except Exception as e:
            logger.error(f"系统启动失败: {e}")
            # 置位退出标志让看门狗兜底：启动失败路径上 CUDA 原生线程
            # 可能卡住解释器正常退出，必须保证进程真正结束
            self._signal_stop = True
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
                            seg_mode = bool(text_item.get('seg'))
                        else:
                            text = text_item
                            seg_mode = False

                        if seg_mode:
                            # 分段模式：原文已由转写端直推显示端，这里只补一个
                            # 空译文事件让显示端流程对齐（直通模式无译文可追加）
                            result = {
                                'type': 'seg_translation',
                                'translations': {},
                                'original': text,
                                'timestamp': self._get_current_timestamp()
                            }
                        else:
                            # 构造只有原文的结果
                            result = {
                                'original': text,
                                'translations': {'en': text, 'ja': text, 'ko': text},
                                'timestamp': self._get_current_timestamp()
                            }
                        try:
                            self.translation_queue.put(result, timeout=0.1)
                        except queue.Full:
                            logger.warning("翻译队列已满，丢弃结果")
                except queue.Empty:
                    continue
                except Exception as e:
                    logger.error(f"直通模式错误: {e}")

        thread = threading.Thread(target=passthrough_worker, daemon=True)
        thread.start()
        self.threads.append(thread)
        logger.info("启动直通模式（无翻译）")

    def _start_display_gui(self):
        """启动字幕 GUI 显示（字幕浮窗或弹幕，按 display_manager 实际类型）。

        Manager.start() 在主线程创建 QApplication 与窗口（非阻塞，不运行事件
        循环）；事件循环由 _wait_for_shutdown → run_event_loop() 启动并占用主
        线程，退出条件（Ctrl+C / 回车）由循环内轮询定时器检测。本方法额外启动
        消费线程，把 translation_queue 的结果投喂给窗口。GUI 启动失败（如无
        显示环境）时自动回退到控制台显示模式。
        """
        try:
            self.display_manager.start()
        except Exception as e:
            logger.warning(f"字幕 GUI 启动失败: {e}，回退到控制台显示模式")
            self._gui_mode = False
            self._start_display_worker()
            return
        self._start_display_thread()
        logger.info(f"字幕显示: {self.display_manager.mode_label}（GUI）")

    def _start_display_worker(self):
        """字幕显示线程：消费 translation_queue，在控制台输出最终字幕。

        作为 display_mode: console 的显示方式，也是字幕 GUI 启动失败时的回退。"""
        def display_worker():
            while self.running:
                try:
                    result = self.translation_queue.get(timeout=1.0)
                except queue.Empty:
                    continue
                except Exception as e:
                    logger.error(f"显示线程取数失败: {e}")
                    continue
                try:
                    if isinstance(result, dict):
                        etype = result.get('type')
                        if etype == 'seg_original':
                            # 分段原文直通事件：立即显示（不等翻译）
                            line = str(result.get('text') or '')
                        elif etype == 'seg_translation':
                            # 分段译文事件：追加显示（原文已随 seg_original 显示过）
                            original = result.get('original')
                            vals = [str(v) for v in (result.get('translations') or {}).values()
                                    if v and str(v) != str(original or '')]
                            line = ' | '.join(dict.fromkeys(vals))
                        else:
                            # 合并格式（整句路径）：原文+译文一行
                            original = result.get('original', '')
                            translations = result.get('translations', {})
                            # 原文在前，翻译按序拼接；直通模式下翻译与原文相同，去重后只显示一条
                            parts = [original] + [
                                v for v in translations.values() if v and v != original
                            ]
                            line = ' | '.join(dict.fromkeys(p for p in parts if p))
                    else:
                        line = str(result)
                    if line:
                        logger.info(f"显示字幕: {line}")
                except Exception as e:
                    logger.error(f"显示字幕失败: {e}")

        thread = threading.Thread(target=display_worker, daemon=True)
        thread.start()
        self.threads.append(thread)
        logger.info("字幕显示线程已启动（控制台模式）")

    def _start_display_thread(self):
        """启动显示消费线程：translation_queue → 字幕窗口（GUI 显示）"""
        def display_feeder():
            try:
                while self.running:
                    try:
                        data = self.translation_queue.get(timeout=1.0)
                        # 非 dict 数据（异常路径）包装成统一格式，避免静默丢失
                        if not isinstance(data, dict):
                            data = {'original': str(data), 'translations': {}}
                        # 线程安全投喂，控件更新由 Qt 线程的定时器回调完成
                        self.display_manager.add_result(data)
                    except queue.Empty:
                        continue
                    except Exception as e:
                        logger.error(f"显示处理错误: {e}")
                        continue

            except Exception as e:
                logger.error(f"显示线程异常: {e}")

        thread = threading.Thread(target=display_feeder, daemon=True)
        thread.start()
        self.threads.append(thread)

    def _start_exit_watchdog(self):
        """退出看门狗：保证进程真正退出（__init__ 中启动，覆盖启动阶段）。

        任何退出请求（_signal_stop / _stdin_stop）置位后启动倒计时：
        - 优雅关闭完成（_fully_stopped）→ 立即 os._exit(0)
        - 超过 SHUTDOWN_FORCE_EXIT_SEC 仍未完成 → 强制退出，绝不永久卡死
        本线程不依赖异常传播，也不依赖主线程是否可中断——此前 Ctrl+C 落在
        input() / Qt 事件循环 / C 扩展里时主线程无法自救，由看门狗解决。
        """
        def watchdog():
            deadline = None
            while True:
                if self._signal_stop or self._stdin_stop:
                    if self._fully_stopped:
                        os._exit(0)
                    if deadline is None:
                        deadline = time.time() + SHUTDOWN_FORCE_EXIT_SEC
                    elif time.time() > deadline:
                        logger.error(f"关闭流程超过 {SHUTDOWN_FORCE_EXIT_SEC:.0f}s 未完成，强制退出进程")
                        os._exit(0)
                time.sleep(0.2)

        thread = threading.Thread(target=watchdog, name="exit-watchdog", daemon=True)
        thread.start()

    def _start_monitor_thread(self):
        """启动监控线程：检查模块状态与队列压力。

        性能详情不在运行期间周期打印（用户要求），只在 stop() 结束前汇总一次。"""
        def monitor_worker():
            while self.running:
                try:
                    # 监控各模块状态
                    self._check_module_status()

                    # 队列压力监控：仅在负载较高时告警（debug 级别统计不刷屏）
                    self._log_system_stats()

                    # 睡眠一段时间
                    time.sleep(5)

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
        }
        # GUI 模式下监控字幕窗口存活
        if self._gui_mode and self.display_manager is not None:
            statuses['display_gui'] = self.display_manager.is_running()

        for module, status in statuses.items():
            if not status:
                logger.warning(f"模块 {module} 未正常运行")

    def _log_system_stats(self):
        """记录系统统计信息（只在队列压力较高时告警，避免周期性刷屏）"""
        try:
            audio_stats = self.audio_capturer.get_performance_stats()

            stats = {
                'audio_queue_size': self.audio_capturer.get_queue_size(),
                'text_queue_size': self.text_queue.qsize(),
                'translation_queue_size': self.translation_queue.qsize(),
                'audio_capture_stats': audio_stats
            }

            # 只在队列压力较大时记录警告
            if (stats['audio_queue_size'] > self.config['audio']['buffer_size'] * 0.7 or
                stats['text_queue_size'] > 8 or
                stats['translation_queue_size'] > 15):
                logger.warning(f"系统负载较高: {stats}")

        except Exception as e:
            logger.error(f"统计信息记录失败: {e}")

    def _log_detailed_performance_report(self):
        """输出性能汇总报告（只在系统结束前调用一次，不做周期打印）"""
        try:
            current_time = time.time()
            if self.performance_stats['start_time'] is None:
                return
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
            logger.info("系统性能汇总报告（结束时）:")
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
        """等待系统关闭。GUI 模式下主线程在此进入 Qt 事件循环并阻塞。"""
        if self._gui_mode:
            # 主线程被 Qt 事件循环占用，回车退出改由专用线程监听 stdin；
            # stdin 关闭/EOF（如后台重定向运行）同样触发优雅退出
            print("按回车键或 Ctrl+C 停止系统...")
            sys.stdout.flush()
            self._start_stdin_watcher()
            try:
                self.display_manager.run_event_loop(
                    quit_check=lambda: self._signal_stop or self._stdin_stop
                )
            finally:
                self.stop()
                # CUDA/模型原生线程可能卡住解释器正常退出（实测会留下占内存的僵尸进程），
                # 各服务已全部停止，直接强制退出（看门狗同时兜底）
                os._exit(0)

        # 控制台模式：轮询退出标志（信号处理器只置标志，不在处理器内嵌套清理）。
        # 不再用阻塞的 input()——主线程周期性回到 Python 字节码层，Ctrl+C 处理
        # 器必然可执行，标志 200ms 内被感知，彻底避免"SystemExit 被吞→卡死"
        print("按回车键或 Ctrl+C 停止系统...")
        sys.stdout.flush()
        self._start_stdin_watcher()
        try:
            while not (self._signal_stop or self._stdin_stop):
                time.sleep(0.2)
        finally:
            self.stop()
            os._exit(0)

    def _start_stdin_watcher(self):
        """启动回车退出监听线程（主线程被 Qt 事件循环或轮询占用时由它感知回车/EOF）"""
        threading.Thread(target=self._stdin_quit_watcher,
                         name="stdin-watcher", daemon=True).start()

    def _stdin_quit_watcher(self):
        """回车退出监听：回车或 stdin EOF 均触发优雅退出。"""
        try:
            sys.stdin.readline()  # 阻塞直至回车或 EOF
        except Exception:
            pass
        logger.info("收到回车退出指令（或输入流关闭）")
        self._stdin_stop = True
        if self._gui_mode and self.display_manager is not None:
            self.display_manager.request_quit()

    def stop(self):
        """停止系统（应在主线程调用；信号处理器只置标志，不直接调用本方法）"""
        if self._stopped:
            return
        self._stopped = True
        logger.info("正在关闭实时字幕系统...")

        self.running = False

        try:
            # 停止各模块（各自内部有超时/异常保护）
            self.audio_capturer.stop_capture()
            self.whisper_transcriber.stop_transcription()
            if self.translator:
                self.translator.stop_translation()
            if self.display_manager is not None:
                self.display_manager.stop()

            # 输出最终性能汇总（只在结束时打印一次）
            self._log_detailed_performance_report()
        except Exception as e:
            logger.error(f"停止过程中出现异常: {e}")
        finally:
            # 等待线程结束（各自有超时，不会无限等待）
            for thread in self.threads:
                thread.join(timeout=2)

            logger.info("实时字幕系统已关闭")
            self._fully_stopped = True

    def _get_current_timestamp(self) -> float:
        """获取当前时间戳"""
        return time.time()

def _print_audio_sources():
    """--list-sources：列出可用音频来源（设备 / 应用会话），不加载模型"""
    from audio_capture.wasapi_capture import (
        list_capture_devices, list_render_devices, list_audio_sessions)

    print("=" * 60)
    print("可用音频来源（用于 config.yaml 的 audio 配置节）")
    print("=" * 60)

    try:
        print("\n[microphone 模式] 录音输入设备 (audio.device 名称子串):")
        for name, is_default in list_capture_devices():
            print(f"  {'[默认] ' if is_default else '       '}{name}")
    except Exception as e:
        print(f"  枚举失败: {e}")

    try:
        print("\n[system 模式] 播放输出设备（回环目标，audio.device 名称子串）:")
        for name, is_default in list_render_devices():
            print(f"  {'[默认] ' if is_default else '       '}{name}")
    except Exception as e:
        print(f"  枚举失败: {e}")

    try:
        print("\n[application 模式] 有音频会话的应用 (audio.target_process 进程名/PID):")
        sessions = list_audio_sessions()
        if not sessions:
            print("  （当前无已注册音频会话的应用；让目标应用播放一段声音后重试）")
        for s in sessions:
            mark = "◉ 正在发声" if s["active"] else "○ 待机    "
            print(f"  {mark}  {s['name']}  (PID {s['pid']})")
    except Exception as e:
        print(f"  枚举失败: {e}")

    print("\n提示: audio.source 可选 microphone / system / application；"
          "device 留空=系统默认设备；application 模式下 target_process 留空则启动时交互选择。")


def main():
    """主函数"""
    parser = argparse.ArgumentParser(description="实时多语言字幕系统")
    parser.add_argument("--list-sources", action="store_true",
                        help="列出可用音频来源（设备与应用会话）后退出，不加载模型")
    parser.add_argument("--audio-source", choices=["microphone", "system", "application"],
                        help="覆盖配置文件中的 audio.source")
    parser.add_argument("--audio-device", help="覆盖 audio.device（设备名子串，留空=默认设备）")
    parser.add_argument("--audio-target", help="覆盖 audio.target_process（application 模式的进程名/PID）")
    args = parser.parse_args()

    try:
        if args.list_sources:
            _print_audio_sources()
            return

        overrides = {
            'source': args.audio_source,
            'device': args.audio_device,
            'target_process': args.audio_target,
        }

        # 创建系统实例
        system = RealtimeSubtitleSystem(overrides=overrides)

        # 启动系统
        system.start()

    except KeyboardInterrupt:
        logger.info("用户中断程序")
    except Exception as e:
        logger.error(f"程序异常退出: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()
