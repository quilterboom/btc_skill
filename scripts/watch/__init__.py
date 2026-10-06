#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
watch 包入口
============

之前所有逻辑塞在 watch.py 一个文件里，现在拆成模块：
  - common        共享：log + 路径常量
  - journal_ops   journal 文件操作（被 jump/settler 共用）
  - jump          JumpTracker（量能异动后的 60s 跳空监测）
  - volume        VolumeWatcher（1m 量能突增）
  - rsi           RsiWatcher（RSI 越界）
  - settler       持仓结算线程（每 5s 巡检）
  - runner        emit_scan + fire + run + daemonize + main

对外兼容：
  - `python3 watch.py start/stop/status/once/run` 仍可用（见 scripts/watch.py 薄壳）
  - `from watch import JumpTracker, VolumeWatcher, RsiWatcher, _settler_loop` 也兼容
"""
from .common import (
    log, OK, NO, WARN,
    HERE, SCAN, LOG, PID, ALERT_DIR, STALE_SEC, DATA_DIR,
)
from .volume import VolumeWatcher
from .rsi import RsiWatcher
from .jump import JumpTracker
from .settler import settler_loop
from .journal_ops import invalidate_other_pending, find_active

# 兼容：position_tracker.py 早期 import "from watch import _load_tg_token" 等。
# 新代码应该直接 `from telegram import ...`，这里保留 shim 只是为了不破坏老代码。
try:
    from telegram import (
        load_token as _load_tg_token,
        load_chat as _load_tg_chat,
        push as push_telegram,
        format_card as format_tg_card,
    )
except ImportError:
    _load_tg_token = None
    _load_tg_chat = None
    push_telegram = None
    format_tg_card = None


__all__ = [
    "log", "OK", "NO", "WARN",
    "HERE", "SCAN", "LOG", "PID", "ALERT_DIR", "STALE_SEC", "DATA_DIR",
    "VolumeWatcher", "RsiWatcher", "JumpTracker", "settler_loop",
    "invalidate_other_pending", "find_active",
    "_load_tg_token", "_load_tg_chat", "push_telegram", "format_tg_card",
]
