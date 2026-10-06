#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Settler — 持仓结算线程
======================

每 5 秒检查一次 journal pending（替代旧的 6m cron）。
异常时 sleep 5s 重试，避免一个错误把线程搞挂。

启动哨兵 + 心跳哨兵（2026-10-04 二次修复）：
  原版"启动哨兵 + 异常兜底"只能捕获"线程没起来"和"线程抛异常"两种死法。
  第三种死法是"线程 hung up 但没异常"（如 SIGSEGV/OOM/GIL 死锁），日志完全静默。
  修复：每 N 轮打一行心跳，即使 n_done=0；这样能在 watch.log 里看到 Settler 还活着。
"""
from __future__ import annotations
import os, time, threading, json
from pathlib import Path
from typing import Optional

from gate_fetch import DATA_DIR
from .common import log


INTERVAL_SEC = 5                       # 每 5s 巡检一次
HEARTBEAT_EVERY = 60                   # 每 60 轮打一行心跳（5 分钟）
HEARTBEAT_PATH = Path(DATA_DIR) / "settler_heartbeat.json"   # 外部脚本能读


def _write_heartbeat(n_iter: int, n_done_total: int, n_still: int, last_run_ts: float) -> None:
    """写心跳到 JSON 文件，外部脚本可以检查 Settler 是否还活着"""
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        tmp = HEARTBEAT_PATH.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({
                "ts": int(time.time()),
                "n_iter": n_iter,
                "n_done_total": n_done_total,
                "n_still": n_still,
                "last_run_ts": last_run_ts,
                "alive": True,
            }, f, ensure_ascii=False)
        tmp.replace(HEARTBEAT_PATH)
    except Exception:
        pass  # 心跳失败不应该影响 Settler 主循环


def settler_loop() -> None:
    """settler 主循环：阻塞运行，跑在独立守护线程里"""
    # ★ 启动哨兵：万一线程根本没起来或启动瞬间崩了，日志里能看到
    log("[settler] 线程启动")
    # 延迟导入：避免与 position_tracker 互相 import
    try:
        from position_tracker import settle_all
    except Exception as e:
        log(f"[settler] 导入 position_tracker 失败（线程退出）: {e}")
        return
    log(f"[settler] 持仓结算线程已就绪（每 {INTERVAL_SEC}s 巡检）")

    n_iter = 0
    n_done_total = 0
    while True:
        # ★ 先写"开始"心跳——即使 settle_all 卡死，也能从 _write_heartbeat 推出 Settler 至少在循环里
        # (heartbeat 文件保留上一次成功 ts，本轮没及时刷新 = Settler 卡了)
        try:
            n_done, n_still = settle_all(verbose=False)
            n_done_total += n_done
            n_iter += 1
            # 写心跳（外部脚本可读）
            _write_heartbeat(n_iter, n_done_total, n_still, time.time())
            if n_done > 0:
                log(f"[settler] 结算 {n_done} 条｜仍 pending {n_still} 条")
            elif n_iter % HEARTBEAT_EVERY == 0:
                # ★ 心跳哨兵：5 分钟一行，证明 Settler 还活着
                log(f"[settler] 心跳 #{n_iter}（累计结算 {n_done_total} 条｜当前 pending {n_still} 条）")
        except Exception as e:
            log(f"[settler] 巡检异常: {e}")
        time.sleep(INTERVAL_SEC)


def start_settler() -> None:
    """启动 Settler 守护线程（从 main / run 调）"""
    threading.Thread(target=settler_loop, daemon=True, name="Settler").start()


def is_alive(max_age_sec: int = 60) -> Optional[bool]:
    """
    检查 Settler 是否在跑：通过读心跳文件判断
    - True：心跳新鲜（max_age_sec 内）
    - False：心跳过期（说明 Settler 死了）
    - None：心跳文件不存在（说明从来没跑过，或 watcher 没启动）
    """
    if not HEARTBEAT_PATH.exists():
        return None
    try:
        hb = json.load(open(HEARTBEAT_PATH))
        age = time.time() - hb.get("ts", 0)
        return age <= max_age_sec
    except Exception:
        return None

