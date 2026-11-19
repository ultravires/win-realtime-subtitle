#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""断句符号稳定提前出字幕的单元测试（无需加载任何模型）。

用假标点模型（长度 > 6 的文本在第 6 个字符后插入逗号）驱动 _maybe_early_emit
状态机，验证：
1. 候选刚出现、未到稳定时长 → 不输出
2. 候选被修正 → 计时重置，不按旧计时输出
3. 符号前的句子 stable 秒内未被修正 → 提前输出该句（含符号）
4. 已输出前缀被记录（_committed_len），端点触发时只输出剩余部分
5. 复位后新语句从头累计
6. 多逗号场景：候选取第一个未提交符号，后续新逗号不干扰已稳定小句
7. 显示通道拥塞 → 条目带 seg=False，原文改由翻译结果兜底
8. 端点收尾剩余为空 → 补句末标记事件（seg_original, sentence_end=True）
9. 端点收尾剩余非空 → 剩余部分带 sentence_end=True 输出
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


class TwoCommaPunct:
    """假标点模型（多逗号）：第 6 字符后插逗号，总长超过 10 时在第 10 字符后再插一个

    模拟说话持续时标点模型不断补新逗号的场景：用于验证候选取"第一个未提交
    断句符号"，后续新逗号不影响已稳定小句的计时。
    """

    def add_punctuation(self, text: str) -> str:
        out = text
        for pos in (6, 10):
            if len(out) > pos:
                out = out[:pos] + "，" + out[pos:]
        return out


def make_transcriber(stable_sec: float = 1.0, punct=None,
                     display_queue=None) -> StreamingTranscriber:
    config = {
        'whisper': {'language': 'zh'},
        'audio': {'volume_threshold': 30},
        'streaming': {
            'early_emit_punctuation': True,
            'early_emit_stable_sec': stable_sec,
        },
    }
    t = StreamingTranscriber(config)
    t.punct = punct or FakePunct()  # 不加载真实模型
    t._display_queue = display_queue
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

    # 场景6：多逗号下候选取"第一个未提交断句符号"——说话继续、标点模型补新
    # 逗号时，第一个小句的候选不变、计时不受干扰，稳定后只提交第一个小句
    display_q = queue.Queue()
    t2 = make_transcriber(stable_sec=1.0, punct=TwoCommaPunct(), display_queue=display_q)
    t2._maybe_early_emit("今天天气很好我们出去玩啊", ts + 3.0, text_queue)
    assert t2._candidate == "今天天气很好，", f"多逗号候选异常: {t2._candidate!r}"
    time.sleep(1.2)
    t2._maybe_early_emit("今天天气很好我们出去玩啊然后我们就回去了", ts + 4.2, text_queue)
    item6 = text_queue.get_nowait()
    assert item6['text'] == "今天天气很好，", f"多逗号应只提交第一个小句: {item6['text']!r}"
    assert t2._committed_len == 6, f"多逗号提交偏移异常: {t2._committed_len}"
    assert item6.get('seg') is True, "直通显示已接线时条目应带 seg=True"
    assert item6.get('sentence_end') is False, "逗号结尾不是句末"
    ev6 = display_q.get_nowait()
    assert ev6['type'] == 'seg_original' and ev6['text'] == "今天天气很好，", \
        f"原文直通事件异常: {ev6!r}"
    assert ev6['sentence_end'] is False
    logger.info("场景6通过: 多逗号下第一个小句独立稳定并提交（含直通事件）")

    # 场景7：显示通道拥塞（队列满）→ 条目 seg=False，原文改由翻译结果兜底携带
    clogged_q = queue.Queue(maxsize=1)
    clogged_q.put({'type': 'seg_original', 'text': '占位', 'sentence_end': False})
    t3 = make_transcriber(stable_sec=1.0, display_queue=clogged_q)
    t3._emit("测试兜底文本", ts + 5.0, text_queue)
    item7 = text_queue.get_nowait()
    assert item7['text'] == "测试兜底文本", f"兜底输出异常: {item7['text']!r}"
    assert item7.get('seg') is False, "显示通道拥塞时条目应带 seg=False"
    logger.info("场景7通过: 显示通道拥塞时走合并通道兜底")

    # 场景8：整句已被提前输出（逗号收尾）后端点触发，且原文恰好只有已提交的
    # 那一小句 → 剩余为空只补句末标记，不向翻译队列输出，并复位状态
    marker_q = queue.Queue()
    t4 = make_transcriber(stable_sec=1.0, display_queue=marker_q)
    t4._committed_len = 6  # 模拟小句"今天天气很好，"已提前输出（对应 6 字符原文）
    t4._flush_endpoint("今天天气很好", ts + 6.0, text_queue)
    assert text_queue.empty(), "剩余为空时不应向翻译队列输出"
    ev8 = marker_q.get_nowait()
    assert ev8['type'] == 'seg_original' and ev8['text'] == "", \
        f"句末标记事件异常: {ev8!r}"
    assert ev8['sentence_end'] is True, "句末标记应带 sentence_end=True"
    assert t4._committed_len == 0 and t4._candidate == "", "端点收尾后状态未复位"
    logger.info("场景8通过: 剩余为空补句末标记事件并复位")

    # 场景9：端点触发且剩余非空 → 剩余部分正常输出并带 sentence_end=True
    t5 = make_transcriber(stable_sec=1.0, display_queue=queue.Queue())
    t5._committed_len = 6  # 模拟已提前输出 "今天天气很好，" 6 字符前缀
    t5._flush_endpoint("今天天气很好我们出去玩", ts + 9.0, text_queue)
    item9 = text_queue.get_nowait()
    assert item9['text'] == "我们出去玩", f"端点剩余输出异常: {item9['text']!r}"
    assert item9.get('sentence_end') is True, "端点剩余输出应带 sentence_end=True"
    assert t5._committed_len == 0 and t5._candidate == "", "端点收尾后状态未复位"
    logger.info("场景9通过: 端点剩余输出带句末标记")

    logger.info("断句符号稳定提前出字幕测试全部通过")


if __name__ == "__main__":
    main()
