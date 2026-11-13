#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""端到端关闭测试：验证系统停止后进程真正退出，不留僵尸进程。

流程：启动 main.py（stdin 用管道保持打开）→ 等日志出现"翻译服务已启动"
→ 关闭 stdin（EOF 触发 _wait_for_shutdown 的停止路径）→ 断言进程退出。
"""

import glob
import os
import subprocess
import sys
import time

PROJECT = os.path.dirname(os.path.abspath(__file__))
READY_MARKER = "翻译服务已启动"
STOP_MARKER = "实时字幕系统已关闭"


def newest_log():
    logs = glob.glob(os.path.join(PROJECT, "logs", "app_*.log"))
    return max(logs, key=os.path.getmtime) if logs else None


def wait_log(marker, timeout, since_size=0, log_path=None):
    """轮询指定日志直到出现 marker，返回 (是否出现, 日志当前大小)"""
    deadline = time.time() + timeout
    log = log_path or newest_log()
    while time.time() < deadline:
        if log and os.path.exists(log):
            try:
                with open(log, "r", encoding="utf-8", errors="replace") as f:
                    f.seek(since_size)
                    if marker in f.read():
                        return True, os.path.getsize(log)
            except OSError:
                pass
        time.sleep(1)
    return False, since_size


def main():
    # 记录启动前已存在的日志，避免误盯旧日志（新进程的日志要等导入完成才创建）
    before = set(glob.glob(os.path.join(PROJECT, "logs", "app_*.log")))

    print("启动 main.py (stdin=PIPE)...")
    proc = subprocess.Popen(
        [sys.executable, "main.py"],
        cwd=PROJECT,
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        # 等待新日志文件出现
        log = None
        deadline = time.time() + 60
        while time.time() < deadline:
            new_logs = set(glob.glob(os.path.join(PROJECT, "logs", "app_*.log"))) - before
            if new_logs:
                log = max(new_logs, key=os.path.getmtime)
                break
            time.sleep(1)
        if not log:
            print("FAIL: 60秒内新日志未创建")
            proc.kill()
            return 1

        print(f"等待服务就绪 (日志: {os.path.basename(log)})...")
        ready, _ = wait_log(READY_MARKER, timeout=180, since_size=0, log_path=log)
        if not ready:
            print("FAIL: 180秒内服务未就绪")
            proc.kill()
            return 1
        print("服务就绪，发送 EOF 关闭 stdin...")

        proc.stdin.close()
        stopped, _ = wait_log(STOP_MARKER, timeout=60)
        if not stopped:
            print("FAIL: 60秒内未见停止完成标记")
            proc.kill()
            return 1

        # 给看门狗留时间，然后断言进程退出
        for _ in range(15):
            if proc.poll() is not None:
                break
            time.sleep(1)
        if proc.poll() is None:
            print("FAIL: stop() 完成后进程仍未退出（僵尸进程）")
            proc.kill()
            return 1

        print(f"PASS: 进程已退出, exit code = {proc.returncode}")
        return 0
    finally:
        if proc.poll() is None:
            proc.kill()


if __name__ == "__main__":
    sys.exit(main())
