#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""断句符号稳定提前出字幕的单元测试（无需加载任何模型）。

用假标点模型（长度 > 6 的文本在第 6 个字符后插入逗号）驱动 _maybe_early_emit
状态机，验证：
1. 候选刚出现、未到稳定时长 → 不输出
2. 候选被修正 → 计时重置，不按旧计时输出
3. 符号前的句子 stable 秒内未被修正 → 提前输出该句（含符号）
4. 已输出前缀被记录（_committed_len），端点触发时只输出剩余部分
"""

import queue
import time

from loguru import logger

from transcription.streaming_transcriber import StreamingTranscriber


class FakePunct:
    """假标点模型：文本长度 > 6 时在第 6 个字符后插入逗号，否则原样返回"""

    def add_punctuation(self, text: str) -> str:
        if len(text) > 6:
            return text[:6] + "，" + text[6:]
        return text


def make_transcriber(stable_sec: float = 1.0) -> StreamingTranscriber:
    config = {
        'whisper': {'language': 'zh'},
        'audio': {'volume_threshold': 30},
        'streaming': {
            'early_emit_punctuation': True,
            'early_emit_stable_sec': stable_sec,
        },
    }
    t = StreamingTranscriber(config)
    t.punct = FakePunct()  # 不加载真实模型
    return t


def main():
    text_queue = queue.Queue()
    t = make_transcriber(stable_sec=1.0)
    ts = time.time()

    # 场景1：候选刚出现，未到稳定时长 → 不输出
    # "今天天气很好我们"(8字) → 标点恢复 "今天天气很好，我们" → 候选 "今天天气很好，"
    t._maybe_early_emit("今天天气很好我们", ts, text_queue)
    assert t._candidate == "今天天气很好，", f"候选异常: {t._candidate!r}"
    assert text_queue.empty(), "未到稳定时长不应输出"
    logger.info("场景1通过: 候选建立且未提前输出")

    # 场景2：0.3s 后候选被修正（文字变化）→ 计时重置，仍不输出
    # "今天天气非常好我们" → 标点恢复 "今天天气非常，好我们" → 新候选 "今天天气非常，"
    time.sleep(0.3)
    t._maybe_early_emit("今天天气非常好我们", ts + 0.3, text_queue)
    assert text_queue.empty(), "候选被修正后不应按旧计时输出"
    logger.info("场景2通过: 修正后计时重置")

    # 场景3：等够 stable 秒且句子未再被修正 → 提前输出
    time.sleep(1.1)
    t._maybe_early_emit("今天天气非常好我们出去", ts + 1.4, text_queue)
    item = text_queue.get_nowait()
    assert item['text'] == "今天天气非常，", f"提前输出内容异常: {item['text']!r}"
    assert t._committed_len == 6, f"已提交偏移异常: {t._committed_len}"
    assert text_queue.empty(), "不应有多余输出"
    logger.info(f"场景3通过: 提前输出 {item['text']!r}, committed={t._committed_len}")

    # 场景4：端点触发时只输出剩余部分（已提交前缀不再重复），且复位状态
    t._emit("我们出去公园玩", ts + 1.5, text_queue)
    item2 = text_queue.get_nowait()
    # FakePunct 在第 6 字符后插逗号（假模型行为），此处验证的是"只输出剩余部分"
    assert item2['text'] == "我们出去公园，玩", f"端点剩余输出异常: {item2['text']!r}"
    t._reset_stream()
    assert t._committed_len == 0 and t._candidate == "", "复位后状态未清零"
    logger.info(f"场景4通过: 端点输出剩余部分 {item2['text']!r}，状态已复位")

    # 场景5：新语句从头累计（复位后原文从 0 开始）
    t._maybe_early_emit("第二句话测试一下", ts + 2.0, text_queue)
    assert t._candidate == "第二句话测试，", f"复位后候选异常: {t._candidate!r}"
    logger.info("场景5通过: 复位后新语句正常建立候选")

    logger.info("断句符号稳定提前出字幕测试全部通过")


if __name__ == "__main__":
    main()
