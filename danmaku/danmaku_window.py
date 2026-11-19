import sys
import random
import queue
from typing import Optional
from PyQt6.QtWidgets import QApplication, QWidget, QLabel
from PyQt6.QtCore import Qt, QTimer, QPropertyAnimation, QPoint
from loguru import logger

# 同屏活跃弹幕上限：翻译高峰时丢弃最早的弹幕，防止堆积刷屏
MAX_ACTIVE_DANMAKUS = 10


class DanmakuLabel(QLabel):
    """弹幕标签类"""

    def __init__(self, text: str, font_size: int, color: str, parent=None):
        super().__init__(text, parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        # 长句换行（配合外部 setMaximumWidth，超宽字幕在屏幕内折行而不是跑出屏幕）
        self.setWordWrap(True)
        self.setStyleSheet(f"""
            QLabel {{
                color: {color};
                background-color: rgba(0, 0, 0, 128);
                border-radius: 8px;
                padding: 8px 16px;
                font-family: "Microsoft YaHei", Arial, sans-serif;
                font-size: {font_size}px;
                font-weight: bold;
            }}
        """)


class DanmakuWindow(QWidget):
    """弹幕展示窗口"""

    def __init__(self, config: dict):
        super().__init__()
        self.config = config
        self.danmaku_queue = queue.Queue()
        self.active_danmakus = []  # 存储活跃的弹幕动画
        self._lane_index = 0       # 车道轮换游标，避免连续弹幕重叠
        self.init_ui()
        self.setup_timer()

    def init_ui(self):
        """初始化UI"""
        # 设置窗口属性
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint |  # 无边框
            Qt.WindowType.WindowStaysOnTopHint |  # 置顶
            Qt.WindowType.Tool  # 工具窗口（不在任务栏显示）
        )

        # 设置透明背景
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)

        # 设置窗口大小（铺满主屏，本身透明，只显示弹幕文字）
        screen = QApplication.primaryScreen().geometry()
        self.setGeometry(screen)

        # 设置窗口标题
        self.setWindowTitle("实时字幕弹幕")

        logger.info("弹幕窗口初始化完成")

    def setup_timer(self):
        """设置定时器"""
        self.timer = QTimer()
        self.timer.timeout.connect(self.update_danmaku)
        self.timer.start(100)  # 100ms刷新一次

    def add_danmaku(self, danmaku_data: dict):
        """添加新的弹幕（必须在 Qt 线程内调用，经 danmaku_queue 由定时器驱动）。

        支持三种事件：seg_original（原文小句，立即飘出）、seg_translation
        （该小句译文，逐语言一条）、合并格式（原文+译文一条，整句路径）。
        """
        try:
            for text in self.danmaku_texts(danmaku_data):
                self._spawn_bullet(text, danmaku_data)
        except Exception as e:
            logger.error(f"添加弹幕失败: {e}")

    def danmaku_texts(self, danmaku_data: dict) -> list:
        """事件 → 弹幕文本列表（空列表表示无可显示内容）"""
        etype = danmaku_data.get('type')
        if etype == 'seg_original':
            t = str(danmaku_data.get('text') or '').strip()
            return [t] if t else []
        if etype == 'seg_translation':
            # 小句译文：原文已随 seg_original 飘过，这里只展示译文
            original = str(danmaku_data.get('original') or '')
            vals = []
            for lang in self.config['translation'].get('target_languages', []):
                v = (danmaku_data.get('translations') or {}).get(lang)
                if v and str(v) != original and str(v) not in vals:
                    vals.append(str(v))
            for k, v in (danmaku_data.get('translations') or {}).items():
                if v and str(v) != original and str(v) not in vals:
                    vals.append(str(v))
            return vals
        text = self.format_danmaku_text(danmaku_data)
        return [text] if text else []

    def _spawn_bullet(self, text: str, danmaku_data: dict):
        """创建单条弹幕标签并启动飘移动画"""
        # 超过同屏上限时丢弃最早的弹幕
        while len(self.active_danmakus) >= MAX_ACTIVE_DANMAKUS:
            oldest = self.active_danmakus.pop(0)
            try:
                if oldest['animation'] is not None:
                    oldest['animation'].stop()
                oldest['label'].deleteLater()
            except Exception:
                pass

        # 创建弹幕标签
        font_size = int(self.config['danmaku'].get('font_size', 16))
        color = self.config['danmaku'].get('color', 'white')
        label = DanmakuLabel(text, font_size, color, self)
        label.setMaximumWidth(self.width())  # 超宽句子在屏幕内换行
        label.adjustSize()

        # 设置初始位置（右侧边缘外，Y 坐标按车道轮换）
        y = self._pick_y()
        label.move(self.width(), y)
        label.show()

        # 创建移动动画
        animation = self.create_danmaku_animation(label, y)

        # 保存活跃弹幕引用
        self.active_danmakus.append({
            'label': label,
            'animation': animation,
            'data': danmaku_data
        })

        logger.debug(f"添加弹幕: {text[:50]}...")

    def _pick_y(self) -> int:
        """车道轮换选择 Y 坐标：相邻弹幕走不同高度，避免重叠。

        车道从配置的 y_range 均分而来（间距按字号推算），并对超出
        屏幕范围的配置值做截断。车道内再加少量随机偏移，观感更自然。
        """
        y_range = self.config['danmaku'].get('y_range', [0, self.height() - 80])
        y_min = max(0, min(int(y_range[0]), self.height() - 80))
        y_max = max(y_min + 1, min(int(y_range[1]), self.height() - 60))
        step = max(48, int(self.config['danmaku'].get('font_size', 16) * 2.2) + 24)
        lanes = list(range(y_min, y_max + 1, step)) or [y_min]

        y = lanes[self._lane_index % len(lanes)]
        self._lane_index += 1
        return min(y + random.randint(0, 12), y_max)

    def format_danmaku_text(self, danmaku_data: dict) -> str:
        """格式化弹幕显示文本"""
        try:
            parts = []

            # 添加原文
            original = danmaku_data.get('original', '')
            if original:
                parts.append(original)

            # 添加翻译（按配置的目标语言顺序）
            translations = danmaku_data.get('translations', {})
            for lang in self.config['translation'].get('target_languages', []):
                if lang in translations and translations[lang]:
                    parts.append(translations[lang])

            if not parts:
                return ""

            # 去重（直通模式下翻译与原文相同）并拼接
            return " | ".join(dict.fromkeys(parts))

        except Exception as e:
            logger.error(f"弹幕文本格式化失败: {e}")
            return ""

    def create_danmaku_animation(self, label: QLabel, y: int):
        """创建弹幕移动动画（从右往左飘出屏幕）"""
        try:
            animation = QPropertyAnimation(label, b"pos")
            animation.setDuration(int(self.config['danmaku'].get('speed', 8000)))

            # 终点取负的标签宽度（而非屏幕宽度），保证换行加宽后的标签也能完整飘出
            animation.setStartValue(QPoint(self.width(), y))
            animation.setEndValue(QPoint(-(label.width() + 20), y))

            # 动画完成后清理
            animation.finished.connect(lambda: self.cleanup_danmaku(label, animation))

            animation.start()
            return animation

        except Exception as e:
            logger.error(f"创建弹幕动画失败: {e}")
            return None

    def cleanup_danmaku(self, label: QLabel, animation):
        """清理已完成的弹幕"""
        try:
            # 从活跃列表中移除
            self.active_danmakus = [
                d for d in self.active_danmakus
                if d['label'] != label and d['animation'] != animation
            ]

            # 删除控件
            label.deleteLater()

        except Exception as e:
            logger.error(f"清理弹幕失败: {e}")

    def update_danmaku(self):
        """更新弹幕显示"""
        try:
            # 从队列获取新的弹幕数据
            while True:
                try:
                    danmaku_data = self.danmaku_queue.get_nowait()
                    self.add_danmaku(danmaku_data)
                except queue.Empty:
                    break

        except Exception as e:
            logger.error(f"更新弹幕失败: {e}")

    def put_danmaku(self, danmaku_data: dict):
        """向弹幕队列添加数据（线程安全：任意线程可调用，
        实际创建控件由 Qt 线程的定时器回调完成）"""
        try:
            self.danmaku_queue.put(danmaku_data, timeout=0.1)
        except queue.Full:
            logger.warning("弹幕队列已满，丢弃数据")

    def get_active_count(self) -> int:
        """获取当前活跃弹幕数量"""
        return len(self.active_danmakus)

    def clear_all_danmakus(self):
        """清除所有弹幕（必须运行在 Qt 线程内；经 aboutToQuit 信号触发）"""
        for danmaku in self.active_danmakus:
            try:
                # 动画创建失败时为 None，逐条 try 避免单条异常中断整体清理
                if danmaku['animation'] is not None:
                    danmaku['animation'].stop()
                danmaku['label'].deleteLater()
            except Exception as e:
                logger.error(f"清除单条弹幕失败: {e}")
        self.active_danmakus.clear()
        logger.info("已清除所有弹幕")


class DanmakuManager:
    """弹幕管理器：QApplication 与事件循环运行在主线程。

    Qt 要求 GUI 应用在主线程创建 QApplication 并运行其事件循环
    （非主线程创建在 Windows 上实测 access violation 崩溃），因此：
    - start() 在主线程创建窗口并显示，但**不阻塞**
    - run_event_loop() 在主线程启动事件循环并阻塞，直至 quit
    - 跨线程只通过线程安全通道交互：
      · 数据面：danmaku_queue（queue.Queue），控件创建由主线程定时器完成
      · 控制面：request_quit() 只置标志，app.quit() 仅由事件循环内部触发
    主线程被事件循环占用期间，回车退出由 main.py 的 stdin 监听线程经
    request_quit() 触发；Ctrl+C 由事件循环内的轮询定时器检测退出标志触发
    （该定时器同时保证主线程周期性回到 Python 字节码层，使信号可被处理）。
    """

    QUIT_CHECK_INTERVAL_MS = 200
    mode_label = "弹幕窗口"

    def __init__(self, config: dict):
        self.config = config
        self.app: Optional[QApplication] = None
        self.window: Optional[DanmakuWindow] = None
        self.running = False
        self._quit_timer: Optional[QTimer] = None
        self._quit_requested = False  # 跨线程退出请求标志（request_quit 置位）

    def start(self):
        """在主线程创建 QApplication 与弹幕窗口并显示（非阻塞，不运行事件循环）。

        失败时抛异常，由调用方决定回退策略（如回退到控制台显示）。
        """
        if self.running:
            logger.warning("弹幕系统已在运行")
            return
        try:
            # 只传 argv[0]，避免 Qt 解析 main.py 的命令行参数
            self.app = QApplication(sys.argv[:1])
            self.window = DanmakuWindow(self.config)
            # 事件循环退出时清理残留弹幕（aboutToQuit 在主线程内触发）
            self.app.aboutToQuit.connect(self.window.clear_all_danmakus)
            self.window.show()
            self.running = True
            self._quit_requested = False
            logger.info("弹幕窗口已创建（GUI 模式）")
        except Exception as e:
            self.running = False
            logger.error(f"弹幕系统启动失败: {e}")
            raise

    def run_event_loop(self, quit_check=None):
        """运行 Qt 事件循环，阻塞直至退出。必须在主线程调用。

        退出判定（每 200ms 由主线程定时器轮询）：quit_check() 为 True，或
        request_quit() 曾被调用。app.quit() 只能由事件循环内部（本定时器回调）
        触发——实测 PyQt6 在 exec() 启动前从其他线程调用 app.quit() 会死锁
        整个进程（持有 GIL 的 C++ 调用永不返回），跨线程退出必须走标志位。
        返回 app.exec() 的退出码。
        """
        if self.app is None or not self.running:
            raise RuntimeError("弹幕系统未启动，无法运行事件循环")

        def effective_quit() -> bool:
            if self._quit_requested:
                return True
            return bool(quit_check()) if quit_check is not None else False

        # 退出条件在事件循环启动前已满足：直接返回，不进入 exec()
        # （exec 启动前没有任何机制能退出它，进入即死锁风险）
        if effective_quit():
            logger.info("退出条件在事件循环启动前已满足，跳过弹幕事件循环")
            self.running = False
            return 0

        def _check():
            try:
                if effective_quit():
                    logger.info("检测到退出条件，退出弹幕事件循环")
                    self.app.quit()
            except Exception as e:
                logger.error(f"退出检查异常，强制退出事件循环: {e}")
                self.app.quit()

        self._quit_timer = QTimer()
        self._quit_timer.timeout.connect(_check)
        self._quit_timer.start(self.QUIT_CHECK_INTERVAL_MS)

        rc = self.app.exec()

        self._quit_timer.stop()
        self.running = False
        logger.info("弹幕 Qt 事件循环已退出")
        return rc

    def request_quit(self):
        """请求退出事件循环（线程安全：仅置标志，可从任意线程调用）。

        绝不直接调用 app.quit()——事件循环未启动时调用它会死锁（见
        run_event_loop 文档）；标志由启动前检查或循环内轮询定时器消费。
        """
        self._quit_requested = True

    def stop(self):
        """停止弹幕系统。须在主线程调用（Qt 控件只能由其所在线程操作）。"""
        if self.app is None:
            self.running = False
            return
        try:
            if self._quit_timer is not None:
                self._quit_timer.stop()
            if self.window is not None:
                self.window.clear_all_danmakus()
                self.window.close()
        except Exception as e:
            logger.error(f"弹幕窗口清理失败: {e}")
        self.running = False
        self.request_quit()  # 兜底：异常路径下事件循环若仍在运行则请求退出
        logger.info("弹幕系统已停止")

    def is_running(self) -> bool:
        """弹幕 GUI 是否在运行（供监控线程上报状态）"""
        return self.running

    def add_danmaku(self, danmaku_data: dict):
        """添加弹幕（线程安全）"""
        if self.window and self.running:
            self.window.put_danmaku(danmaku_data)

    def add_result(self, data: dict):
        """统一投喂接口（与 SubtitleManager.add_result 对齐，main.py 按 display_mode 调用）"""
        self.add_danmaku(data)
