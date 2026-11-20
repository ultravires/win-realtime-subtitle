# -*- coding: utf-8 -*-
"""设置对话框冒烟测试（offscreen Qt）：语言勾选回显、保存写回配置、热更新回调。

运行: python test_settings_dialog.py
"""
import os
import sys
import tempfile

# 离屏渲染，无需真实显示环境
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


def test_dialog_roundtrip():
    from PyQt6.QtWidgets import QApplication
    from danmaku.settings_dialog import SettingsDialog

    app = QApplication.instance() or QApplication(sys.argv[:1])

    with tempfile.TemporaryDirectory() as tmp:
        cfg_path = os.path.join(tmp, "config.yaml")
        config = {
            'audio': {'source': 'microphone', 'device': '', 'target_process': ''},
            'translation': {
                'enabled': True,
                'target_languages': ['en'],
            },
        }

        applied = []
        dialog = SettingsDialog(config, config_path=cfg_path,
                                on_apply=applied.append)

        # 回显：目标语言 en 勾选，其他未勾选
        checked = {code for code, cb in dialog.lang_checks if cb.isChecked()}
        assert checked == {'en'}, checked
        assert dialog.translate_check.isChecked()
        assert dialog.source_combo.currentData() == 'microphone'

        # 修改：来源→系统音频、语言→ja+ko、关闭翻译
        dialog.source_combo.setCurrentIndex(1)  # system
        for code, cb in dialog.lang_checks:
            cb.setChecked(code in ('ja', 'ko'))
        dialog.translate_check.setChecked(False)
        dialog._on_save()

        # 内存 config 已更新
        assert config['audio']['source'] == 'system'
        assert config['translation']['target_languages'] == ['ja', 'ko']
        assert config['translation']['enabled'] is False
        # 热更新回调已触发
        assert applied == [['ja', 'ko']], applied
        # 配置文件已写回且可再次加载
        assert os.path.isfile(cfg_path)
        import yaml
        with open(cfg_path, 'r', encoding='utf-8') as f:
            saved = yaml.safe_load(f)
        assert saved['audio']['source'] == 'system'
        assert saved['translation']['target_languages'] == ['ja', 'ko']
        print("[通过] 设置保存：config 更新 + 写回文件 + 热更新回调")


def test_dialog_device_preset():
    """配置里已保存的设备名应回显到输入框（枚举结果与设备无关也可编辑）"""
    from PyQt6.QtWidgets import QApplication
    from danmaku.settings_dialog import SettingsDialog

    app = QApplication.instance() or QApplication(sys.argv[:1])
    config = {
        'audio': {'source': 'microphone', 'device': 'HyperX', 'target_process': ''},
        'translation': {'enabled': True, 'target_languages': ['en']},
    }
    dialog = SettingsDialog(config, config_path=os.path.join(tempfile.gettempdir(),
                                                             "_unused.yaml"))
    # 枚举里存在 HyperX 设备则按 data 预选；不存在则回显到编辑框
    text = dialog.device_combo.currentText()
    data = dialog.device_combo.currentData()
    assert ('HyperX' in text) or (data == 'HyperX'), (text, data)
    print(f"[通过] 设备回显: {text!r}")
    assert dialog.source_combo.currentData() == 'microphone'


if __name__ == "__main__":
    test_dialog_roundtrip()
    test_dialog_device_preset()
    print("\n全部设置对话框测试通过 ✓")
    sys.exit(0)
