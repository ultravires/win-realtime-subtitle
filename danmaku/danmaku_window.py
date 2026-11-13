import sys
import random
import queue
from typing import Optional
from PyQt6.QtWidgets import QApplication, QWidget, QLabel, QVBoxLayout
from PyQt6.QtCore import Qt, QTimer, QPropertyAnimation, QPoint, QRect
from PyQt6.QtGui import QFont, QColor, QPalette
from loguru import logger

class DanmakuLabel(QLabel):
    """弹幕标签类"""
    
    def __init__(self, text: str, parent=None):
        super().__init__(text, parent)
        self.setup_style()
        
    def setup_style(self):
        """设置弹幕样式"""
        self.setStyleSheet("""
            QLabel {
                color: white;
                background-color: rgba(0, 0, 0, 128);
                border-radius: 8px;
                padding: 8px 16px;
                font-family: "Microsoft YaHei", Arial, sans-serif;
                font-size: 16px;
                font-weight: bold;
            }
        """)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)

class DanmakuWindow(QWidget):
    """弹幕展示窗口"""
    
    def __init__(self, config: dict):
        super().__init__()
        self.config = config
        self.danmaku_queue = queue.Queue()
        self.active_danmakus = []  # 存储活跃的弹幕动画
        self.init_ui()
        self.setup_timer()
        
    def init_ui(self):
        """初始化UI"""
        # 设置窗口属性
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint |  # 无边框
            Qt.WindowType.WindowStaysOnTopHint |  # 置顶
            Qt.WindowType.Tool  # 工具窗口
        )
        
        # 设置透明背景
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        
        # 设置窗口大小
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
        """添加新的弹幕"""
        try:
            # 格式化弹幕文本
            text = self.format_danmaku_text(danmaku_data)
            if not text:
                return
                
            # 创建弹幕标签
            label = DanmakuLabel(text, self)
            
            # 设置初始位置（右侧边缘，随机Y坐标）
            x = self.width()
            y_range = self.config['danmaku']['y_range']
            y = random.randint(y_range[0], y_range[1])
            
            label.move(x, y)
            label.show()
            
            # 创建移动动画
            animation = self.create_danmaku_animation(label, x, y)
            
            # 保存活跃弹幕引用
            self.active_danmakus.append({
                'label': label,
                'animation': animation,
                'data': danmaku_data
            })
            
            logger.debug(f"添加弹幕: {text[:50]}...")
            
        except Exception as e:
            logger.error(f"添加弹幕失败: {e}")
    
    def format_danmaku_text(self, danmaku_data: dict) -> str:
        """格式化弹幕显示文本"""
        try:
            parts = []
            
            # 添加原文
            original = danmaku_data.get('original', '')
            if original:
                parts.append(original)
            
            # 添加翻译
            translations = danmaku_data.get('translations', {})
            for lang in self.config['translation']['target_languages']:
                if lang in translations and translations[lang]:
                    parts.append(translations[lang])
            
            if not parts:
                return ""
                
            return " | ".join(parts)
            
        except Exception as e:
            logger.error(f"弹幕文本格式化失败: {e}")
            return ""
    
    def create_danmaku_animation(self, label: QLabel, start_x: int, y: int):
        """创建弹幕移动动画"""
        try:
            animation = QPropertyAnimation(label, b"pos")
            animation.setDuration(self.config['danmaku']['speed'])
            
            # 设置起始和结束位置
            start_pos = QPoint(start_x, y)
            end_pos = QPoint(-label.width(), y)
            
            animation.setStartValue(start_pos)
            animation.setEndValue(end_pos)
            
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
        """向弹幕队列添加数据"""
        try:
            self.danmaku_queue.put(danmaku_data, timeout=0.1)
        except queue.Full:
            logger.warning("弹幕队列已满，丢弃数据")
    
    def get_active_count(self) -> int:
        """获取当前活跃弹幕数量"""
        return len(self.active_danmakus)
    
    def clear_all_danmakus(self):
        """清除所有弹幕"""
        try:
            # 停止所有动画
            for danmaku in self.active_danmakus:
                danmaku['animation'].stop()
                danmaku['label'].deleteLater()
            
            self.active_danmakus.clear()
            logger.info("已清除所有弹幕")
            
        except Exception as e:
            logger.error(f"清除弹幕失败: {e}")

class DanmakuManager:
    """弹幕管理器"""
    
    def __init__(self, config: dict):
        self.config = config
        self.app: Optional[QApplication] = None
        self.window: Optional[DanmakuWindow] = None
        self.running = False
        
    def start(self):
        """启动弹幕系统"""
        if self.running:
            logger.warning("弹幕系统已在运行")
            return
            
        try:
            # 创建Qt应用
            self.app = QApplication(sys.argv)
            
            # 创建弹幕窗口
            self.window = DanmakuWindow(self.config)
            self.window.show()
            
            self.running = True
            logger.info("弹幕系统启动成功")
            
            # 启动事件循环
            return self.app.exec()
            
        except Exception as e:
            logger.error(f"弹幕系统启动失败: {e}")
            raise
    
    def stop(self):
        """停止弹幕系统"""
        if self.window:
            self.window.clear_all_danmakus()
        self.running = False
        logger.info("弹幕系统已停止")
    
    def add_danmaku(self, danmaku_data: dict):
        """添加弹幕（线程安全）"""
        if self.window and self.running:
            self.window.put_danmaku(danmaku_data)