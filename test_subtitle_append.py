#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""字幕浮窗分段追加显示的单元测试（离屏 Qt，无需加载任何模型）。

验证：
1. seg_original 小句事件 → 原文立即追加显示（不等翻译）
2. seg_translation 译文事件 → 追加进译文缓冲区，先到的译文不被覆盖
3. 句末标记后，下一小句到来时清屏换句
4. 合并格式（整句路径，如 faster-whisper 后端）→ 整句替换展示
5. 缓冲区超长 → 丢弃最旧小句，保住最新内容
"""

import os
import queue

# 离屏渲染：无显示环境/不想弹窗也能跑（本机有桌面时会先尝试离屏，失败再可见运行）
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from loguru import logger

from danmaku.subtitle_window import SubtitleWindow


def make_config() -> dict:
    return {
        'translation': {'target_languages': ['en']},
        'subtitle': {
            'width_ratio': 0.5,
            'original_font_size': 22,
            'translation_font_size': 18,
        },
    }


def main():
    from PyQt6.QtWidgets import QApplication
    app = QApplication([])

    w = SubtitleWindow(make_config())

    # ---- 场景1：第一个小句稳定 → 原文立即上屏（此时无任何译文）----
    w.add_subtitle({'type': 'seg_original', 'text': '今天天气真不错，', 'sentence_end': False})
    assert w.original_label.text() == '今天天气真不错，', \
        f"原文未立即显示: {w.original_label.text()!r}"
    assert w.translation_label.text() == '', "尚无译文时译文区应为空"
    logger.info("场景1通过: 小句原文稳定即上屏（不等翻译）")

    # ---- 场景2：该小句译文就绪 → 追加，不影响原文行 ----
    w.add_subtitle({'type': 'seg_translation', 'translations': {'en': 'The weather is really nice,'}})
    assert w.original_label.text() == '今天天气真不错，'
    assert w.translation_label.text() == 'The weather is really nice,', \
        f"译文未追加: {w.translation_label.text()!r}"
    logger.info("场景2通过: 译文就绪后追加显示")

    # ---- 场景3：句内第二个小句 → 原文继续追加（不清屏），译文继续追加 ----
    w.add_subtitle({'type': 'seg_original', 'text': '很适合约上朋友一起出去踢球。', 'sentence_end': True})
    assert w.original_label.text() == '今天天气真不错，很适合约上朋友一起出去踢球。', \
        f"句内小句被清屏: {w.original_label.text()!r}"
    w.add_subtitle({'type': 'seg_translation',
                    'translations': {'en': "It's great to play football with friends."}})
    trans = w.translation_label.text()
    assert 'The weather is really nice,' in trans and "It's great to play football" in trans, \
        f"先到的译文被覆盖: {trans!r}"
    logger.info("场景3通过: 句内追加且译文缓冲区保留全部小句译文")

    # ---- 场景4：句末标记后新小句 → 清屏换句 ----
    w.add_subtitle({'type': 'seg_original', 'text': '第二句开始了', 'sentence_end': False})
    assert w.original_label.text() == '第二句开始了', \
        f"句末后未清屏: {w.original_label.text()!r}"
    assert w.translation_label.text() == '', "换句后旧译文应清空"
    logger.info("场景4通过: 句末标记后换句清屏")

    # ---- 场景5：合并格式（整句路径）→ 整句替换展示并视为句末 ----
    # （模拟 whisper 后端独占流程：窗口从句末状态开始，合并事件逐句替换）
    w._sentence_ended = True
    w.add_subtitle({'original': '这是整句路径的字幕', 'translations': {'en': 'Whole sentence path'}})
    assert w.original_label.text() == '这是整句路径的字幕'
    assert w.translation_label.text() == 'Whole sentence path'
    w.add_subtitle({'original': '下一句整句', 'translations': {'en': 'Next whole sentence'}})
    assert w.original_label.text() == '下一句整句', \
        f"整句路径未替换旧句: {w.original_label.text()!r}"
    logger.info("场景5通过: 合并格式维持整句替换语义")

    # ---- 场景6：缓冲区超长 → 丢弃最旧小句保住最新内容 ----
    w._sentence_ended = True
    for i in range(60):
        w.add_subtitle({'type': 'seg_original', 'text': f'小句{i}，', 'sentence_end': False})
    assert len(w.original_label.text()) <= 300 + 1, \
        f"原文缓冲区未按上限裁剪: {len(w.original_label.text())}"
    assert w.original_label.text().endswith('小句59，'), "裁剪后应保留最新小句"
    logger.info("场景6通过: 超长缓冲丢弃最旧小句")

    # ---- 场景7：空文本句末标记 → 不追加内容、仅置句末状态 ----
    w._sentence_ended = False
    before = w.original_label.text()
    w.add_subtitle({'type': 'seg_original', 'text': '', 'sentence_end': True})
    assert w.original_label.text() == before, "空标记不应改变显示内容"
    assert w._sentence_ended is True, "空标记应置句末状态"
    logger.info("场景7通过: 空句末标记只置状态不改内容")

    logger.info("字幕浮窗分段追加显示测试全部通过")


if __name__ == "__main__":
    main()
