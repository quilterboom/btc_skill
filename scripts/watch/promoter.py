#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Promoter — preview_pending 自动 promote 线程
============================================

独立于 Settler（settler 是结算持仓，promoter 是把到期 preview_pending 推到 pending）。
频率 1s/巡（2026-10-15 liusir 拍板：高灵敏以便 preview_secs=任意小值都立刻生效）。

历史问题（2026-10-15 沉淀）：
  在 promoter 出现之前，promote_due_previews 只在 15m 周期检查 (PERIOD_SEC=900) 顺手扫；
  实测 8 条 preview 信号 100% 延迟 14~15 min；其中 BTC_USDT-1792033207-short
  (preview 11:00:37 到期) 推迟到 11:15:14 才 promote，价格已跌穿 entry 438 点 → MISSED。

心跳文件：promoter_heartbeat.json（跟 settler_heartbeat.json 同位置、同 schema）。
心跳日志错开 (HEARTBEAT_EVERY=600 = 10min) 避免和 settler 心跳交叉刷屏。
"""
from __future__ import annotations
import os, time, threading, json
from pathlib import Path

from gate_fetch import DATA_DIR
from .common import log


INTERVAL_SEC = 1                       # 1s 一巡（liusir 拍板）
HEARTBEAT_EVERY = 600                  # 每 600 轮打一行心跳（10 分钟）
HEARTBEAT_PATH = Path(DATA_DIR) / "promoter_heartbeat.json"


def _write_heartbeat(n_iter: int, n_promo_total: int, last_run_ts: float) -> None:
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        tmp = HEARTBEAT_PATH.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({
                "ts": int(time.time()),
                "n_iter": n_iter,
                "n_promo_total": n_promo_total,
                "last_run_ts": last_run_ts,
                "alive": True,
            }, f, ensure_ascii=False)
        tmp.replace(HEARTBEAT_PATH)
    except Exception:
        pass


def promoter_loop(contract: str = "BTC_USDT") -> None:
    """promoter 主循环：阻塞运行在独立 daemon 线程"""
    log("[promoter] 线程启动")
    # 延迟导入：与 journal.py 隔离 import 顺序
    try:
        from journal import promote_due_previews
    except Exception as e:
        log(f"[promoter] 导入 journal.promote_due_previews 失败（线程退出）: {e}")
        return
    log(f"[promoter] preview_pending 自动 promote 线程已就绪（每 {INTERVAL_SEC}s 巡检）")

    n_iter = 0
    n_promo_total = 0
    while True:
        try:
            promoted = promote_due_previews(contract=contract)
            n_promo_total += len(promoted)
            n_iter += 1
            _write_heartbeat(n_iter, n_promo_total, time.time())
            if promoted:
                log(f"[promoter] promote {len(promoted)} 条 preview → pending: {promoted}")
            elif n_iter % HEARTBEAT_EVERY == 0:
                # 心跳哨兵：10 分钟一行（与 settler 的 5min 心跳错开，不同时打避免日志交叉）
                log(f"[promoter] 心跳 #{n_iter}（累计 promote {n_promo_total} 条）")
        except Exception as e:
            log(f"[promoter] 巡检异常: {e}")
        time.sleep(INTERVAL_SEC)


def start_promoter(contract: str = "BTC_USDT") -> None:
    """启动 Promoter 守护线程（从 main / run 调）"""
    threading.Thread(target=promoter_loop, args=(contract,),
                     daemon=True, name="Promoter").start()
