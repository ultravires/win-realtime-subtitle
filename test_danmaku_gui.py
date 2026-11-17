#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""弹幕 GUI 冒烟测试。

验证完整生命周期：主线程创建窗口 → 事件循环内弹幕投喂与动画 → 退出条件触发
quit → stop 清理。测试期间屏幕上会出现一个透明弹幕窗口约 5 秒，弹幕飘过后消失。
"""

import time

from loguru import logger

from utils.helpers import load_config
from danmaku.danmaku_window import DanmakuManager


def main():
    config = load_config("config/config.yaml")
    manager = DanmakuManager(config)

    t0 = time.time()
    manager.start()  # 主线程创建窗口，非阻塞
    logger.info(f"窗口创建耗时 {time.time() - t0:.2f}s，is_running={manager.is_running()}")
    assert manager.is_running(), "弹幕系统未进入运行状态"
    assert manager.window is not None, "窗口未创建"

    # 投喂测试字幕（含直通模式下的"翻译=原文"去重场景与空内容场景）
    samples = [
        {'original': '你好，世界', 'translations': {'en': 'Hello, world'}},
        {'original': '这是一条比较长的测试弹幕，用来验证超宽字幕能否在屏幕内自动换行而不跑出屏幕边界',
         'translations': {'en': 'A long line to verify word wrap inside the screen'}},
        {'original': '实时字幕系统运行中', 'translations': {'en': '实时字幕系统运行中'}},  # 应去重为单条
        {'original': '', 'translations': {}},  # 空内容应被忽略
    ]
    for s in samples:
        manager.add_danmaku(s)
        time.sleep(0.05)

    # 事件循环跑 5 秒后经 quit_check 自动退出；活跃弹幕数在循环运行期间采样
    # （退出时 aboutToQuit → clear_all_danmakus 会清空，事后读数为 0）
    deadline = time.time() + 5
    active_samples = []

    def quit_check() -> bool:
        active_samples.append(manager.window.get_active_count())
        return time.time() > deadline

    manager.run_event_loop(quit_check=quit_check)
    logger.info("事件循环已退出")

    active = active_samples[-1] if active_samples else 0
    logger.info(f"循环内最后一次活跃弹幕数: {active}（4 条投喂，1 条空内容被忽略，预期 3 条）")
    assert active == 3, f"活跃弹幕数异常: {active}"

    manager.stop()
    assert not manager.is_running(), "stop() 后仍在运行状态"
    assert not manager.window.isVisible(), "stop() 后窗口未关闭"

    logger.info("弹幕 GUI 冒烟测试通过")


if __name__ == "__main__":
    main()
