# -*- coding: utf-8 -*-
"""翻译批量合并逻辑单元测试（不加载模型，stub 掉 translate_text）。

运行: python test_translation_batch.py
"""
import queue
import sys


def _make_translator():
    from translation.translator import TencentTranslator
    config = {
        'translation': {
            'device_map': 'cpu',
            'target_languages': ['en'],
            'max_new_tokens': 100,
            'batch_max_items': 4,
            'batch_max_new_tokens': 512,
        }
    }
    return TencentTranslator(config)


def test_collect_batch_no_backlog():
    """无积压：单条取出即返回，不额外等待"""
    tr = _make_translator()
    q = queue.Queue()
    item = {'text': '你好', 'seg': True, 'first_timestamp': 1.0}
    batch = tr._collect_batch(item, q)
    assert len(batch) == 1 and batch[0] is item
    print("[通过] 无积压时单条即时返回")


def test_collect_batch_drains_backlog():
    """有积压：一次取空队列，顺序保持（首条已由 get 取出，不在队列里）"""
    tr = _make_translator()
    q = queue.Queue()
    first = {'text': '句0', 'seg': True, 'first_timestamp': 0}
    backlog = [{'text': f'句{i}', 'seg': True, 'first_timestamp': i} for i in range(1, 3)]
    for it in backlog:
        q.put(it)
    batch = tr._collect_batch(first, q)
    assert [b['text'] for b in batch] == ['句0', '句1', '句2'], batch
    assert q.empty()
    print("[通过] 积压整批取出且顺序保持")


def test_collect_batch_respects_cap():
    """超过 batch_max_items：只取上限条数，剩余留队列下一轮"""
    tr = _make_translator()
    q = queue.Queue()
    first = {'text': '句0', 'seg': True}
    backlog = [{'text': f'句{i}', 'seg': True} for i in range(1, 10)]
    for it in backlog:
        q.put(it)
    batch = tr._collect_batch(first, q)
    assert len(batch) == 4  # batch_max_items=4（首条 + 3 条积压）
    assert q.qsize() == 6, f"剩余应留在队列: {q.qsize()}"
    print("[通过] 批量上限生效，剩余留队")


def test_translate_batch_seg_mode():
    """分段模式合并：一次调用、合并原文、译文事件带 type=seg_translation"""
    tr = _make_translator()
    calls = []

    def fake_translate(text, langs, max_new_tokens=None):
        calls.append((text, langs, max_new_tokens))
        return {lang: f'T({text[:10]})' for lang in langs}

    tr.translate_text = fake_translate

    out_q = queue.Queue()
    batch = [
        {'text': '第一句，', 'seg': True, 'first_timestamp': 5.0},
        {'text': '第二句。', 'seg': True, 'first_timestamp': 6.0},
    ]
    tr._translate_batch(batch, out_q)

    result = out_q.get_nowait()
    assert result['type'] == 'seg_translation'
    assert result['original'] == '第一句，\n第二句。'
    assert result['translations'] == {'en': "T(第一句，\n第二句。)"} or \
        result['translations']['en'].startswith('T(')
    assert result['first_timestamp'] == 5.0  # 取最早时间戳
    # 单次调用、输出上限放大：100*2=200
    assert len(calls) == 1
    assert calls[0][2] == 200
    print("[通过] 分段模式合并翻译事件结构正确")


def test_translate_batch_merged_mode():
    """整句模式（faster-whisper 路径）：合并原文+译文一次展示"""
    tr = _make_translator()
    tr.translate_text = lambda text, langs, max_new_tokens=None: {
        lang: f'T({text})' for lang in langs}
    out_q = queue.Queue()
    tr._translate_batch(['hello world', 'second line'], out_q)
    result = out_q.get_nowait()
    assert 'type' not in result
    assert result['original'] == 'hello world\nsecond line'
    assert result['translations']['en'] == 'T(hello world\nsecond line)'
    print("[通过] 整句模式合并格式正确")


def test_translate_batch_empty_filtered():
    """空文本条目被过滤，全部为空时不产出事件"""
    tr = _make_translator()
    called = []
    tr.translate_text = lambda *a, **k: called.append(a) or {}
    out_q = queue.Queue()
    tr._translate_batch([{'text': '', 'seg': True}, {'text': '  ', 'seg': True}], out_q)
    assert out_q.empty() and not called
    print("[通过] 空文本批次被跳过")


if __name__ == "__main__":
    test_collect_batch_no_backlog()
    test_collect_batch_drains_backlog()
    test_collect_batch_respects_cap()
    test_translate_batch_seg_mode()
    test_translate_batch_merged_mode()
    test_translate_batch_empty_filtered()
    print("\n全部翻译批量合并测试通过 ✓")
    sys.exit(0)
