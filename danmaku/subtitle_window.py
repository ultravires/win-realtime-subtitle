import queue
import sys
from typing import Optional

from PyQt6.QtWidgets import QApplication, QWidget, QLabel, QVBoxLayout
from PyQt6.QtCore import Qt, QTimer
from loguru import logger

# 单区域最大显示字符数：超长文本保留末尾（最新的内容），避免浮窗无限增高
MAX_TEXT_LEN = 300

# 浮窗布局参数（px）
PAD_X = 24      # 左右内边距
PAD_Y = 14      # 上下内边距
LABEL_GAP = 6   # 原文与译文间距


class SubtitleWindow(QWidget):
    """字幕浮窗：上半部分显示识别原文，下半部分显示翻译文本。

    - 无边框、置顶、半透明圆角背景，默认停靠屏幕底部居中，可按住左键拖动
    - 内容整体替换式（同视频字幕）：新字幕到达后覆盖上一条，不做滚动/飘过动画
    - 跨线程数据经 subtitle_queue 由 Qt 定时器在主线程消费，控件只在 Qt 线程更新
    """

    def __init__(self, config: dict):
        super().__init__()
        self.config = config
        self.subtitle_queue = queue.Queue()
        self._current_text = ""   # 当前展示内容标记（供 get_active_count 使用）
        self._drag_offset = None  # 拖动时的鼠标偏移
        self._user_moved = False  # 用户拖动后不再自动回到底部居中
        self._width = 0
        self._y_offset = 100
        self.init_ui()
        self.setup_timer()

    def init_ui(self):
        """初始化UI"""
        cfg = self.config.get('subtitle', {}) or {}
        screen = QApplication.primaryScreen().availableGeometry()

        # 窗口属性：无边框 + 置顶 + 工具窗口（不进任务栏）+ 不抢键盘焦点
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
            | Qt.WindowType.WindowDoesNotAcceptFocus
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setWindowTitle("实时字幕")

        alpha = max(0, min(int(float(cfg.get('background_alpha', 0.55)) * 255), 255))
        self._width = max(420, min(int(screen.width() * float(cfg.get('width_ratio', 0.6))),
                                   screen.width()))
        self._y_offset = max(0, int(cfg.get('y_offset', 100)))

        # 半透明圆角背景画在窗口自身上（objectName 选择器限定，不影响子控件）
        self.setObjectName("subtitleRoot")
        self.setStyleSheet(f"""
            #subtitleRoot {{
                background-color: rgba(0, 0, 0, {alpha});
                border-radius: 14px;
            }}
            QLabel {{
                background: transparent;
            }}
        """)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(PAD_X, PAD_Y, PAD_X, PAD_Y)
        layout.setSpacing(LABEL_GAP)

        # 上半部分：识别原文
        self.original_label = QLabel("等待语音输入…")
        self.original_label.setWordWrap(True)
        self.original_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.original_label.setStyleSheet(
            f'color: {cfg.get("original_color", "white")};'
            'font-family: "Microsoft YaHei", "微软雅黑", Arial, sans-serif;'
            f'font-size: {int(cfg.get("original_font_size", 22))}px;'
            'font-weight: bold;'
        )

        # 下半部分：翻译文本
        self.translation_label = QLabel("")
        self.translation_label.setWordWrap(True)
        self.translation_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.translation_label.setStyleSheet(
            f'color: {cfg.get("translation_color", "#ffd966")};'
            'font-family: "Microsoft YaHei", "微软雅黑", Arial, sans-serif;'
            f'font-size: {int(cfg.get("translation_font_size", 18))}px;'
        )

        layout.addWidget(self.original_label)
        layout.addWidget(self.translation_label)

        self._update_geometry()
        logger.info("字幕浮窗初始化完成")

    def setup_timer(self):
        """设置定时器：100ms 排空一次字幕队列"""
        self.timer = QTimer()
        self.timer.timeout.connect(self.update_subtitle)
        self.timer.start(100)

    def put_subtitle(self, data: dict):
        """投喂字幕（线程安全：任意线程可调用，控件更新由 Qt 线程定时器完成）"""
        try:
            self.subtitle_queue.put(data, timeout=0.1)
        except queue.Full:
            logger.warning("字幕队列已满，丢弃数据")

    def update_subtitle(self):
        """定时器回调：排空队列并更新浮窗"""
        try:
            while True:
                try:
                    data = self.subtitle_queue.get_nowait()
                except queue.Empty:
                    break
                self.add_subtitle(data)
        except Exception as e:
            logger.error(f"更新字幕失败: {e}")

    def add_subtitle(self, data: dict):
        """更新浮窗内容（必须在 Qt 线程内调用，经定时器驱动）"""
        try:
            original = str(data.get('original', '') or '')
            translations = data.get('translations', {}) or {}

            # 译文按配置的目标语言顺序排列；与原文相同的译文（直通模式）不重复展示
            parts = []
            for lang in self.config['translation'].get('target_languages', []):
                v = translations.get(lang)
                if v and str(v) != original:
                    parts.append(str(v))
            # 兜底：target_languages 之外的译文也展示（去重、保持顺序）
            for k, v in translations.items():
                if v and str(v) != original and str(v) not in parts:
                    parts.append(str(v))

            if not original and not parts:
                return

            original_display = self._clip(original)
            translation_display = "\n".join(parts)

            self.original_label.setText(original_display)
            self.translation_label.setText(translation_display)
            self._current_text = f"{original_display}|{translation_display}"
            self._update_geometry()
            if not self.isVisible():
                self.show()
            logger.debug(f"更新字幕: {original_display[:50]}")
        except Exception as e:
            logger.error(f"添加字幕失败: {e}")

    @staticmethod
    def _clip(text: str) -> str:
        """超长文本保留末尾（最新的内容）"""
        text = text.strip()
        if len(text) > MAX_TEXT_LEN:
            return "…" + text[-MAX_TEXT_LEN:]
        return text

    def _update_geometry(self):
        """内容变化后按固定宽度重算窗口高度；未拖动时保持屏幕底部居中停靠"""
        try:
            screen = QApplication.primaryScreen().availableGeometry()
            inner_w = max(120, self._width - 2 * PAD_X)
            self.original_label.setFixedWidth(inner_w)
            self.translation_label.setFixedWidth(inner_w)

            def label_h(lbl: QLabel) -> int:
                # wordWrap 标签按给定宽度取换行后高度；不可用时退回 sizeHint
                h = lbl.heightForWidth(inner_w)
                if not h or h <= 0:
                    h = max(lbl.sizeHint().height(), lbl.minimumSizeHint().height())
                return max(h, lbl.minimumSizeHint().height())

            total = (2 * PAD_Y + LABEL_GAP
                     + label_h(self.original_label) + label_h(self.translation_label))
            self.setFixedWidth(self._width)
            self.resize(self._width, max(total, 2 * PAD_Y + 40))

            if not self._user_moved:
                x = screen.x() + (screen.width() - self._width) // 2
                y = screen.y() + screen.height() - self.height() - self._y_offset
                self.move(x, y)
            self._clamp_into_screen(screen)
        except Exception as e:
            logger.error(f"字幕浮窗布局失败: {e}")

    def _clamp_into_screen(self, screen):
        """保证浮窗完整落在屏幕内"""
        geo = self.frameGeometry()
        x = min(max(geo.x(), screen.x()), screen.right() - geo.width() + 1)
        y = min(max(geo.y(), screen.y()), screen.bottom() - geo.height() + 1)
        if (x, y) != (geo.x(), geo.y()):
            self.move(x, y)

    # ---------------------------------------------------------------- 拖动支持

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_offset = (event.globalPosition().toPoint()
                                 - self.frameGeometry().topLeft())
            event.accept()

    def mouseMoveEvent(self, event):
        if self._drag_offset is not None and (event.buttons() & Qt.MouseButton.LeftButton):
            self.move(event.globalPosition().toPoint() - self._drag_offset)
            self._user_moved = True
            event.accept()

    def mouseReleaseEvent(self, event):
        self._drag_offset = None
        event.accept()

    # ---------------------------------------------------------------- 状态与清理

    def get_active_count(self) -> int:
        """当前是否有字幕在展示（供监控统计，接口与弹幕窗口对齐）"""
        return 1 if self._current_text else 0

    def clear_all_subtitles(self):
        """清空展示内容（必须运行在 Qt 线程内；经 aboutToQuit 信号触发）"""
        self.original_label.setText("")
        self.translation_label.setText("")
        self._current_text = ""
        logger.info("已清空字幕浮窗内容")


class SubtitleManager:
    """字幕浮窗管理器：QApplication 与事件循环运行在主线程。

    Qt 要求 GUI 应用在主线程创建 QApplication 并运行其事件循环
    （非主线程创建在 Windows 上实测 access violation 崩溃），因此：
    - start() 在主线程创建窗口并显示，但**不阻塞**
    - run_event_loop() 在主线程启动事件循环并阻塞，直至 quit
    - 跨线程只通过线程安全通道交互：
      · 数据面：subtitle_queue（queue.Queue），控件更新由主线程定时器完成
      · 控制面：request_quit() 只置标志，app.quit() 仅由事件循环内部触发
    生命周期接口与 DanmakuManager 完全一致，main.py 按 display_mode 选择。
    """

    QUIT_CHECK_INTERVAL_MS = 200
    mode_label = "字幕浮窗"

    def __init__(self, config: dict):
        self.config = config
        self.app: Optional[QApplication] = None
        self.window: Optional[SubtitleWindow] = None
        self.running = False
        self._quit_timer: Optional[QTimer] = None
        self._quit_requested = False  # 跨线程退出请求标志（request_quit 置位）

    def start(self):
        """在主线程创建 QApplication 与字幕浮窗并显示（非阻塞，不运行事件循环）。

        失败时抛异常，由调用方决定回退策略（如回退到控制台显示）。
        """
        if self.running:
            logger.warning("字幕浮窗已在运行")
            return
        try:
            # 只传 argv[0]，避免 Qt 解析 main.py 的命令行参数
            self.app = QApplication(sys.argv[:1])
            self.window = SubtitleWindow(self.config)
            # 事件循环退出时清空浮窗内容（aboutToQuit 在主线程内触发）
            self.app.aboutToQuit.connect(self.window.clear_all_subtitles)
            self.window.show()
            self.running = True
            self._quit_requested = False
            logger.info("字幕浮窗已创建（GUI 模式）")
        except Exception as e:
            self.running = False
            logger.error(f"字幕浮窗启动失败: {e}")
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
            raise RuntimeError("字幕浮窗未启动，无法运行事件循环")

        def effective_quit() -> bool:
            if self._quit_requested:
                return True
            return bool(quit_check()) if quit_check is not None else False

        # 退出条件在事件循环启动前已满足：直接返回，不进入 exec()
        # （exec 启动前没有任何机制能退出它，进入即死锁风险）
        if effective_quit():
            logger.info("退出条件在事件循环启动前已满足，跳过事件循环")
            self.running = False
            return 0

        def _check():
            try:
                if effective_quit():
                    logger.info("检测到退出条件，退出字幕事件循环")
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
        logger.info("字幕 Qt 事件循环已退出")
        return rc

    def request_quit(self):
        """请求退出事件循环（线程安全：仅置标志，可从任意线程调用）。

        绝不直接调用 app.quit()——事件循环未启动时调用它会死锁（见
        run_event_loop 文档）；标志由启动前检查或循环内轮询定时器消费。
        """
        self._quit_requested = True

    def stop(self):
        """停止字幕浮窗。须在主线程调用（Qt 控件只能由其所在线程操作）。"""
        if self.app is None:
            self.running = False
            return
        try:
            if self._quit_timer is not None:
                self._quit_timer.stop()
            if self.window is not None:
                self.window.clear_all_subtitles()
                self.window.close()
        except Exception as e:
            logger.error(f"字幕浮窗清理失败: {e}")
        self.running = False
        self.request_quit()  # 兜底：异常路径下事件循环若仍在运行则请求退出
        logger.info("字幕浮窗已停止")

    def is_running(self) -> bool:
        """字幕浮窗是否在运行（供监控线程上报状态）"""
        return self.running

    def add_result(self, data: dict):
        """添加字幕结果（线程安全，与 DanmakuManager.add_result 接口一致）"""
        if self.window and self.running:
            self.window.put_subtitle(data)
