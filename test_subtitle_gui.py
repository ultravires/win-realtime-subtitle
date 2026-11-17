#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""字幕浮窗 GUI 冒烟测试。

验证完整生命周期：主线程创建浮窗 → 事件循环内字幕投喂与展示 → 退出条件触发
quit → stop 清理。测试期间屏幕底部会出现字幕浮窗约 5 秒（上：识别原文，下：译文）。
"""

import time

from loguru import logger

from utils.helpers import load_config
from danmaku.subtitle_window import SubtitleManager


def main():
    config = load_config("config/config.yaml")
    manager = SubtitleManager(config)

    t0 = time.time()
    manager.start()  # 主线程创建窗口，非阻塞
    logger.info(f"浮窗创建耗时 {time.time() - t0:.2f}s，is_running={manager.is_running()}")
    assert manager.is_running(), "字幕系统未进入运行状态"
    assert manager.window is not None, "浮窗未创建"

    # 投喂测试字幕（含长句换行、直通模式"译文=原文"去重、空内容场景）
    samples = [
        {'original': '你好，世界', 'translations': {'en': 'Hello, world'}},
        {'original': '这是一条比较长的测试字幕，用来验证识别文本较长时能否在浮窗内自动换行而不溢出',
         'translations': {'en': 'A long line to verify word wrap inside the subtitle window'}},
        {'original': '实时字幕系统运行中', 'translations': {'en': '实时字幕系统运行中'}},  # 译文=原文应不重复展示
        {'original': '', 'translations': {}},  # 空内容应被忽略
    ]
    for s in samples:
        manager.add_result(s)
        time.sleep(0.05)

    # 事件循环跑 5 秒后经 quit_check 自动退出；期间采样"是否有字幕在展示"
    deadline = time.time() + 5
    active_samples = []

    def quit_check() -> bool:
        active_samples.append(manager.window.get_active_count())
        return time.time() > deadline

    manager.run_event_loop(quit_check=quit_check)
    logger.info("事件循环已退出")

    active = active_samples[-1] if active_samples else 0
    logger.info(f"循环内最后一次字幕展示标记: {active}（预期 1，空内容不更新展示状态）")
    assert active == 1, f"字幕展示标记异常: {active}"

    manager.stop()
    assert not manager.is_running(), "stop() 后仍在运行状态"
    assert not manager.window.isVisible(), "stop() 后浮窗未关闭"

    logger.info("字幕浮窗 GUI 冒烟测试通过")


if __name__ == "__main__":
    main()
