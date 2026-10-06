#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
watch.py — 薄壳入口（保持向后兼容）
====================================

所有逻辑已拆到 watch/ 子模块：
  - common / journal_ops / jump / volume / rsi / settler / runner

本文件只做一件事：把 CLI 调用转发到 watch.runner.main()。
这样 `python3 watch.py start/stop/status/once/run` 命令保持兼容。

老代码 `from watch import JumpTracker, VolumeWatcher, RsiWatcher` 也兼容
（见 watch/__init__.py 的兼容 shim）。

设计要点：
  - 永远不要把业务逻辑写在这里——所有功能都在 watch/ 子模块
  - CLI 参数和守护进程逻辑全部在 watch/runner.py
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# 转发 CLI 到 watch/runner.py 的 main()
from watch.runner import main

if __name__ == "__main__":
    sys.exit(main())
