# -*- coding: utf-8 -*-
"""设置对话框：在字幕浮窗右键"设置"打开，可视化修改声音来源与翻译语言。

- 声音来源：麦克风 / 系统音频 / 指定应用，并可从设备与应用会话列表中选择
  （枚举走 audio_capture.wasapi_capture，纯 ctypes WASAPI，无外部依赖）。
  保存后写入 config.yaml，**重启程序生效**（WASAPI 捕获流运行中不支持热切换）。
- 翻译设置：目标语言多选保存后**立即生效**（热更新 translator.target_languages）；
  启用/禁用翻译需重启。
- 保存 = 更新内存中的 config 字典 + 写回 config.yaml（注释会重新生成）。
"""

from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QFormLayout, QGroupBox, QGridLayout,
    QComboBox, QPushButton, QCheckBox, QLabel, QDialogButtonBox,
)
from PyQt6.QtCore import Qt
from loguru import logger

from utils.helpers import save_config

# 与 translator.language_names 保持一致的目标语言选项
LANGUAGES = [
    ('en', '英语'), ('ja', '日语'), ('ko', '韩语'),
    ('fr', '法语'), ('es', '西班牙语'), ('de', '德语'),
    ('ru', '俄语'), ('it', '意大利语'), ('pt', '葡萄牙语'),
    ('ar', '阿拉伯语'),
]

SOURCE_OPTIONS = [
    ('microphone', '麦克风'),
    ('system', '系统音频（扬声器回环）'),
    ('application', '指定应用'),
]


class SettingsDialog(QDialog):
    """设置对话框（模态）。on_apply(langs) 在保存后回调（用于热更新翻译语言）。"""

    def __init__(self, config: dict, config_path: str = "config/config.yaml",
                 on_apply=None, parent=None):
        super().__init__(parent)
        self.config = config
        self.config_path = config_path
        self.on_apply = on_apply

        self.setWindowTitle("字幕设置")
        # 字幕浮窗是置顶工具窗口，对话框同样置顶避免被遮挡
        self.setWindowFlags(self.windowFlags() | Qt.WindowType.WindowStaysOnTopHint)
        self.setMinimumWidth(420)

        acfg = self.config.get('audio', {}) or {}
        tcfg = self.config.get('translation', {}) or {}

        layout = QVBoxLayout(self)

        # ---------------- 声音来源 ----------------
        audio_group = QGroupBox("声音来源（保存后重启程序生效）")
        audio_form = QFormLayout(audio_group)

        self.source_combo = QComboBox()
        for key, label in SOURCE_OPTIONS:
            self.source_combo.addItem(label, key)
        current = str(acfg.get('source', 'microphone'))
        idx = self.source_combo.findData(current)
        self.source_combo.setCurrentIndex(idx if idx >= 0 else 0)
        self.source_combo.currentIndexChanged.connect(self._on_source_changed)
        audio_form.addRow("来源:", self.source_combo)

        device_row = QHBoxLayout()
        self.device_combo = QComboBox()
        self.device_combo.setEditable(True)
        device_row.addWidget(self.device_combo, 1)
        refresh_btn = QPushButton("刷新列表")
        refresh_btn.clicked.connect(self._refresh_devices)
        device_row.addWidget(refresh_btn)
        audio_form.addRow("设备/应用:", device_row)

        self.audio_hint = QLabel("")
        self.audio_hint.setWordWrap(True)
        audio_form.addRow(self.audio_hint)

        layout.addWidget(audio_group)

        # ---------------- 翻译设置 ----------------
        trans_group = QGroupBox("翻译")
        trans_form = QFormLayout(trans_group)

        self.translate_check = QCheckBox("启用翻译（不勾选只显示识别原文；修改需重启生效）")
        self.translate_check.setChecked(bool(tcfg.get('enabled', True)))
        trans_form.addRow(self.translate_check)

        self.lang_checks: list[tuple[str, QCheckBox]] = []
        lang_grid = QGridLayout()
        target = list(tcfg.get('target_languages', []) or [])
        for i, (code, label) in enumerate(LANGUAGES):
            cb = QCheckBox(label)
            cb.setChecked(code in target)
            self.lang_checks.append((code, cb))
            lang_grid.addWidget(cb, i // 5, i % 5)
        trans_form.addRow("目标语言:", lang_grid)
        self.lang_hint = QLabel("翻译语言保存后立即生效，无需重启。")
        trans_form.addRow(self.lang_hint)

        layout.addWidget(trans_group)

        # ---------------- 按钮 ----------------
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok
                                   | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("保存")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        buttons.accepted.connect(self._on_save)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self._refresh_devices()

    # ------------------------------------------------------------------ 设备列表

    def _on_source_changed(self, _idx: int):
        self._refresh_devices()

    def _refresh_devices(self):
        """按当前来源枚举可选设备/应用会话（WASAPI 枚举，毫秒级）"""
        source = self.source_combo.currentData()
        self.device_combo.clear()
        try:
            if source == 'microphone':
                from audio_capture.wasapi_capture import list_capture_devices
                self.device_combo.addItem("（系统默认录音设备）", "")
                for name, is_default in list_capture_devices():
                    self.device_combo.addItem(
                        ("[默认] " if is_default else "") + name, name)
                self.audio_hint.setText("选择麦克风输入设备；也可直接输入设备名的一部分进行匹配。")
            elif source == 'system':
                from audio_capture.wasapi_capture import list_render_devices
                self.device_combo.addItem("（系统默认播放设备）", "")
                for name, is_default in list_render_devices():
                    self.device_combo.addItem(
                        ("[默认] " if is_default else "") + name, name)
                self.audio_hint.setText("捕获系统播放的全部声音（回环指定播放设备）；也可直接输入设备名的一部分。")
            else:
                from audio_capture.wasapi_capture import list_audio_sessions
                self.device_combo.addItem("（启动时从正在发声的应用中选择）", "")
                sessions = list_audio_sessions()
                if not sessions:
                    self.device_combo.addItem("（当前无应用注册音频会话）", "")
                for s in sessions:
                    mark = "◉ 正在发声  " if s.get('active') else "○ 待机      "
                    self.device_combo.addItem(f"{mark}{s['name']}  (PID {s['pid']})",
                                              str(s['pid']))
                self.audio_hint.setText("选择目标应用（捕获范围含其子进程）；也可直接输入进程名或 PID。")
        except Exception as e:
            self.audio_hint.setText(f"枚举设备失败: {e}（可直接手动输入）")
            logger.warning(f"设置对话框枚举音频设备失败: {e}")

        # 预选配置里已存的设备/应用
        saved = ''
        if source == 'application':
            saved = str(self.config.get('audio', {}).get('target_process', '') or '')
        else:
            saved = str(self.config.get('audio', {}).get('device', '') or '')
        if saved:
            i = self.device_combo.findData(saved)
            if i >= 0:
                self.device_combo.setCurrentIndex(i)
            else:
                self.device_combo.setEditText(saved)

    # ------------------------------------------------------------------ 保存

    def _picked_or_typed(self) -> str:
        """取设备/应用选择结果：选中列表项返回其 data；手动输入返回文本。

        可编辑 QComboBox 的坑：用户键入新文本时 currentIndex 仍停在原选中项，
        currentData() 会返回**陈旧项**的 data 而非 None——必须比对当前文本
        是否等于选中项的显示文本，不一致即视为手动输入。
        """
        text = self.device_combo.currentText().strip()
        idx = self.device_combo.currentIndex()
        item_text = self.device_combo.itemText(idx) if idx >= 0 else None
        if text and (item_text is None or text != item_text):
            return text  # 手动输入（设备名子串/进程名/PID）
        data = self.device_combo.currentData()
        return '' if data is None else str(data)

    def _on_save(self):
        source = self.source_combo.currentData()
        picked = self._picked_or_typed()

        self.config['audio']['source'] = source
        if source == 'application':
            # 应用会话项保存 PID；手动输入文本原样保存（进程名/PID 均可）
            self.config['audio']['target_process'] = picked
            logger.info(f"设置声音来源: application (target_process="
                        f"{picked or '启动时选择'})")
        else:
            # 设备名按子串匹配保存；空串=系统默认设备
            self.config['audio']['device'] = picked
            logger.info(f"设置声音来源: {source} (device={picked or '默认设备'})")

        self.config['translation']['enabled'] = self.translate_check.isChecked()
        langs = [code for code, cb in self.lang_checks if cb.isChecked()]
        self.config['translation']['target_languages'] = langs
        logger.info(f"设置翻译语言: {langs or '（无，仅显示原文）'}")

        try:
            save_config(self.config, self.config_path)
            logger.info(f"配置已保存: {self.config_path}")
        except Exception as e:
            logger.error(f"配置保存失败: {e}")
            self.lang_hint.setText(f"配置保存失败: {e}")
            return

        # 翻译语言热更新（回调由 main 注入，同步更新 translator）
        if self.on_apply is not None:
            try:
                self.on_apply(langs)
            except Exception as e:
                logger.warning(f"翻译语言热更新失败（配置已保存，重启后生效）: {e}")

        self.accept()
