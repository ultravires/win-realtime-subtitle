#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""回归测试：事件循环启动前后的退出请求时序（此前启动前请求会死锁整个进程）。

背景：实测 PyQt6 在 app.exec() 启动之前从其他线程调用 app.quit() 会死锁整个
进程（持有 GIL 的 C++ 调用永不返回，所有 Python 线程冻结）。修复后
request_quit() 只置标志，app.quit() 仅由 run_event_loop 的启动前检查或循环内
轮询定时器（主线程）触发。

用法：python test_quit_before_exec.py <scenario>
  1 = 启动前 request_quit → 应跳过事件循环直接返回（死锁回归验证）
  2 = 运行中从线程 request_quit → 定时器轮询触发退出
  3 = 运行中 quit_check 返回 True → 定时器轮询触发退出（对照组）
  --all = 依次以子进程运行 1/2/3 并校验退出码（Qt 每进程仅允许一个 QApplication）
"""

import os
import subprocess
import sys
import threading
import time

from loguru import logger

from utils.helpers import load_config
from danmaku.subtitle_window import SubtitleManager


def scenario_pre_exec_quit():
    """时序1：request_quit 发生在 run_event_loop 之前（原实现此处之后死锁）"""
    manager = SubtitleManager(load_config("config/config.yaml"))
    manager.start()
    manager.request_quit()  # 事件循环尚未启动（旧实现：此时置标志后进入 exec 即死锁）
    rc = manager.run_event_loop(quit_check=None)
    assert rc == 0, f"应跳过事件循环返回 0，实际 {rc}"
    assert not manager.is_running(), "跳过事件循环后不应处于运行状态"
    manager.stop()
    logger.info("时序1通过: 启动前的退出请求 → 跳过事件循环直接返回")


def scenario_mid_loop_request_quit():
    """时序2：事件循环运行中从其他线程 request_quit → 定时器轮询退出"""
    manager = SubtitleManager(load_config("config/config.yaml"))
    manager.start()

    def quitter():
        time.sleep(0.5)
        manager.request_quit()  # 循环运行中请求退出（标志位路径）

    threading.Thread(target=quitter, daemon=True).start()
    t0 = time.time()
    rc = manager.run_event_loop(quit_check=None)
    elapsed = time.time() - t0
    assert rc == 0, f"事件循环应正常退出，实际 rc={rc}"
    assert elapsed < 5, f"退出耗时异常: {elapsed:.1f}s"
    manager.stop()
    logger.info(f"时序2通过: 运行中 request_quit → {elapsed:.2f}s 内退出")


def scenario_mid_loop_quit_check():
    """时序3（对照）：quit_check 返回 True → 定时器轮询退出"""
    manager = SubtitleManager(load_config("config/config.yaml"))
    manager.start()
    deadline = time.time() + 0.5
    rc = manager.run_event_loop(quit_check=lambda: time.time() > deadline)
    assert rc == 0, f"事件循环应正常退出，实际 rc={rc}"
    manager.stop()
    logger.info("时序3通过: quit_check 路径退出正常")


SCENARIOS = {
    '1': scenario_pre_exec_quit,
    '2': scenario_mid_loop_request_quit,
    '3': scenario_mid_loop_quit_check,
}


def run_all():
    """依次以子进程运行各时序（Qt 每进程仅允许一个 QApplication）"""
    failed = []
    for name, func in SCENARIOS.items():
        print(f"=== 场景 {name}: {func.__doc__} ===", flush=True)
        proc = subprocess.run(
            [sys.executable, __file__, name],
            timeout=60,
        )
        if proc.returncode != 0:
            failed.append(name)
            print(f"场景 {name} FAIL (exit={proc.returncode})", flush=True)
        else:
            print(f"场景 {name} PASS", flush=True)
    if failed:
        print(f"FAIL: 场景 {failed} 未通过", flush=True)
        return 1
    print("全部时序通过", flush=True)
    return 0


if __name__ == "__main__":
    arg = sys.argv[1] if len(sys.argv) > 1 else "--all"
    if arg == "--all":
        sys.exit(run_all())
    SCENARIOS[arg]()
    os._exit(0)  # QApplication 存活时解释器退出可能被 CUDA/Qt 原生线程卡住，直接硬退出
