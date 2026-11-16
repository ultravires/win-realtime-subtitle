#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""翻译延迟基准：同一句连翻 3 次，区分预热与稳态耗时，并报告显存。"""
import subprocess
import time

from utils.helpers import load_config
from translation.translator import TencentTranslator


def vram_used_mib() -> int:
    try:
        out = subprocess.check_output(
            ["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
            text=True)
        return int(out.strip().splitlines()[0])
    except Exception:
        return -1


def main():
    config = load_config("config/config.yaml")
    tr = TencentTranslator(config)
    t0 = time.time()
    tr.load_model()
    print(f"[bench] 模型加载 {time.time()-t0:.1f}s | 显存 {vram_used_mib()} MiB")

    sentence = "今天天气怎么样？我们来测试一下实时字幕系统的延迟表现。"
    for i in range(1, 4):
        t0 = time.time()
        res = tr.translate_text(sentence, config['translation']['target_languages'])
        dt = time.time() - t0
        print(f"[bench] 第{i}次 {dt:.2f}s | 显存 {vram_used_mib()} MiB | "
              f"en={res.get('en','')[:40]}... | zh={res.get('zh','')[:20]}...")


if __name__ == "__main__":
    main()
