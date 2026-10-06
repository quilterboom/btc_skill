#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
watch 子模块共享：log 函数、路径常量、UI 符号
"""
from __future__ import annotations
import os, sys, time
from typing import Optional
from pathlib import Path
from gate_fetch import DATA_DIR

OK, NO, WARN = "✅", "❌", "⚠️"

# ============================ 路径 ============================
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # scripts/
SCAN = os.path.join(HERE, "scan.py")
LOG = os.path.join(DATA_DIR, "watch.log")
PID = os.path.join(DATA_DIR, "watch.pid")
ALERT_DIR = os.path.join(DATA_DIR, "alerts")
STALE_SEC = 300            # 超过 5 分钟没收到任何推送 → 判定连接假死，重连

# 兼容旧 import: watch.py / position_tracker.py 可能还会用到 DATA_DIR + STALE_SEC
__all__ = ["log", "OK", "NO", "WARN", "HERE", "SCAN", "LOG", "PID", "ALERT_DIR",
           "STALE_SEC", "DATA_DIR"]


# ============================ 日志 ============================
def log(msg: str) -> None:
    """统一日志：时间戳 + 消息，写 watch.log + 守护进程时 stdout"""
    line = f"[{time.strftime('%m-%d %H:%M:%S')}] {msg}"
    try:
        if sys.stdout.isatty():      # 守护进程只写文件，避免双写
            print(line, flush=True)
    except Exception:
        pass
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass
