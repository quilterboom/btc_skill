#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
HammerWatcher — 抄底反弹直接进的秒级监测线程 (2026-10-18 liusir 决策)

设计思路：
  scan.py 检测到 hammer（5 项进程条全过）→ 写 journal status="preview_pending"
  HammerWatcher 每秒 poll ticker：
    - ticker >= hammer_L × (1 + trigger_pct) → 市价进（fill_px = ticker）
    - 120s 过期 → 标 cancelled

为什么独立于 Settler：
  - Settler 每 5s 巡检 + 用 1m K 线 close，最坏延迟 60s
  - hammer "反弹已涨完"，必须**秒级**才能抓到
  - fill_px 必须用真实 ticker（market order 语义），不能用 K 线 close

设计借鉴：RiskGuard（同为 watch/ 下的 daemon 线程 + 1s 间隔 + ticker）
"""
from __future__ import annotations
import os, time, threading, json
from pathlib import Path
from typing import Optional, List, Dict

from gate_fetch import DATA_DIR, fetch_ticker
from .common import log


# ============================ 配置 ============================
INTERVAL_SEC = 1.0                     # 每 1 秒检查一次
WATCH_WINDOW_SEC = 120                 # 2 分钟监测窗口（liusir 10-18: "20分钟都涨完了，最多 2 分钟"）
TRIGGER_PCT = 0.003                    # 触发阈值：ticker >= hammer_L × 1.003 (反弹 0.3% 即进)
HEARTBEAT_EVERY = 60                   # 每 60 轮打一行心跳
HEARTBEAT_PATH = Path(DATA_DIR) / "hammer_watcher_heartbeat.json"


# ============================ 心跳 ============================
def _write_heartbeat(n_iter: int, n_active: int, n_filled: int, n_expired: int):
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        tmp = HEARTBEAT_PATH.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({
                "ts": int(time.time()),
                "n_iter": n_iter,
                "n_active": n_active,
                "n_filled": n_filled,
                "n_expired": n_expired,
                "alive": True,
            }, f)
        tmp.replace(HEARTBEAT_PATH)
    except Exception:
        pass


# ============================ Journal 读写 ============================
def _read_journal() -> List[Dict]:
    p = Path(DATA_DIR) / "journal.jsonl"
    if not p.exists():
        return []
    out = []
    with open(p, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except Exception:
                continue
    return out


def _write_journal(recs: List[Dict]):
    p = Path(DATA_DIR) / "journal.jsonl"
    tmp = p.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        for r in recs:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    tmp.replace(p)


def _append_event(ev: Dict):
    """写一笔事件到 position_events.jsonl（与 settler 同格式）"""
    ev_path = Path(DATA_DIR) / "position_events.jsonl"
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(ev_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(ev, ensure_ascii=False) + "\n")
    except Exception:
        pass


# ============================ TG 推送 ============================
# 注：telegram.push 在函数内动态 import（避免 watch 启动时的硬依赖，
#      跟 risk_guard.py / scan.py 的现有模式一致）

def _push_fill(sig: Dict, fill_px: float, elapsed_sec: float):
    """hammer 直接进成交推送"""
    try:
        # ★ 2026-10-27：push_once（幂等 key = 持仓id:hammer_fill）——
        #   watcher 每秒巡检，同一笔单的成交提醒只能推一次，不能刷屏。
        from telegram import push_once as _tg_push
        side_cn = "做多"
        tp1 = float(sig.get("tp1", 0))
        tp2 = float(sig.get("tp2", 0))
        sl = float(sig.get("sl", 0))
        contracts = int(sig.get("contracts", 0))
        card = (
            f"🟢 <b>BTC hammer 直接进</b> 反弹确认\n"
            f"{side_cn} {contracts} 张 @ <b>{fill_px:,.1f}</b>（秒级市价）\n"
            f"监测耗时 {elapsed_sec:.1f}s ｜ TP1 <b>{tp1:,.1f}</b> ｜ TP2 <b>{tp2:,.1f}</b> ｜ SL <b>{sl:,.1f}</b>\n"
            f"入场理由：hammer 触底后 {TRIGGER_PCT*100:.2f}% 反弹已确认"
        )
        _tg_push(f"{sig.get('id')}:hammer_fill", card)
    except Exception as e:
        log(f"[hammer_watcher] TG 推送失败（非阻塞）: {e}")


def _push_expired(sig: Dict, max_high: float):
    """hammer 监测窗过期取消推送"""
    try:
        from telegram import push as _tg_push
        meta = sig.get("meta", {}) or {}
        hammer_L = float(meta.get("hammer_L", 0))
        target = hammer_L * (1 + TRIGGER_PCT)
        card = (
            f"⏰ <b>BTC hammer 监测过期</b> 反弹未达预期\n"
            f"窗口 {WATCH_WINDOW_SEC}s 内最高 ticker={max_high:,.1f}（目标 {target:,.1f}）\n"
            f"未触发进单 → 取消"
        )
        _tg_push(card)
    except Exception as e:
        log(f"[hammer_watcher] TG 过期推送失败（非阻塞）: {e}")


# ============================ 主循环 ============================
def _process_one(sig: Dict, now: int, ticker_px: float) -> Optional[str]:
    """
    处理一条 preview_pending 的 hammer 信号。
    返回: 'filled' / 'expired' / 'wait' (继续等)
    """
    sig_id = sig.get("id", "")
    meta = sig.get("meta", {}) or {}
    hammer_L = float(meta.get("hammer_L", 0))
    hammer_H = float(meta.get("hammer_H", 0))
    if hammer_L <= 0:
        return "expired"  # meta 损坏，取消

    preview_until_ts = int(sig.get("preview_until_ts", 0) or 0)
    if preview_until_ts <= 0:
        return "expired"  # 没有期限，取消

    # 已过期？
    if now >= preview_until_ts:
        sig["status"] = "cancelled"
        sig["outcome"] = "HAMMER_EXPIRED"
        sig["exit_ts"] = now
        sig["note"] = (sig.get("note") or "") + f" | hammer watcher 2min 监测窗过期（最高 ticker 未达 +{TRIGGER_PCT*100:.2f}%）"
        max_high = float(sig.get("_max_high_during_window", 0) or 0)
        _push_expired(sig, max_high)
        _append_event({
            "id": sig_id,
            "kind": "hammer_watcher_expired",
            "ts": now,
            "max_high": max_high,
            "target": hammer_L * (1 + TRIGGER_PCT),
        })
        return "expired"

    # 跟踪最大 ticker（用于过期推送）
    if ticker_px > float(sig.get("_max_high_during_window", 0) or 0):
        sig["_max_high_during_window"] = ticker_px

    # 触发阈值：ticker >= hammer_L × 1.003
    target = hammer_L * (1 + TRIGGER_PCT)
    if ticker_px >= target:
        # 市价进！
        sig["status"] = "pending"   # 改为 pending 让 settler 接管 TP/SL
        sig["filled"] = True
        sig["fill_px"] = round(ticker_px, 1)
        sig["fill_ts"] = now
        sig["_fill_method"] = "market_on_watcher"  # 标记: 监测窗秒级市价进
        # 改 entry 为真实成交价（TP1/TP2 在 _lplan 时已按原 entry 算过,
        # 用 _orig_contracts 的 1/3 做 TP1, 但这里我们复用现有 entry_limit/tp1/tp2)
        elapsed = now - int(sig.get("hammer_ts") or sig.get("ts", now))
        _push_fill(sig, ticker_px, elapsed)
        _append_event({
            "id": sig_id,
            "kind": "hammer_watcher_filled",
            "ts": now,
            "fill_px": ticker_px,
            "elapsed_sec": elapsed,
            "trigger_target": target,
        })
        return "filled"

    return "wait"


def hammer_watcher_loop():
    """
    HammerWatcher 主循环：阻塞运行，跑在独立守护线程里。
    每秒 poll journal + ticker，找 preview_pending 的 hammer 信号处理。
    """
    log("[hammer_watcher] 线程启动")
    n_iter = 0
    n_filled = 0
    n_expired = 0
    while True:
        n_iter += 1
        try:
            now = int(time.time())
            ticker_resp = fetch_ticker("BTC_USDT")
            # fetch_ticker 返回 list[dict]，取 last 字段
            ticker_px = float(ticker_resp[0].get("last", 0)) if ticker_resp else 0.0

            recs = _read_journal()
            changed = False
            active = 0
            for r in recs:
                if (r.get("signal_source") == "left_side_oversold_bounce"
                        and r.get("status") == "preview_pending"):
                    active += 1
                    result = _process_one(r, now, ticker_px)
                    if result == "filled":
                        n_filled += 1
                        changed = True
                    elif result == "expired":
                        n_expired += 1
                        changed = True

            if changed:
                _write_journal(recs)

            # 心跳
            if n_iter % HEARTBEAT_EVERY == 0:
                log(f"[hammer_watcher] 心跳 #{n_iter}（active={active} filled={n_filled} expired={n_expired}）")
                _write_heartbeat(n_iter, active, n_filled, n_expired)

        except Exception as e:
            log(f"[hammer_watcher] 巡检异常: {e}")
        time.sleep(INTERVAL_SEC)


def start_hammer_watcher():
    """启动 HammerWatcher 守护线程（从 watch/runner.py:start_risk_guard 后调）"""
    threading.Thread(target=hammer_watcher_loop, daemon=True, name="HammerWatcher").start()
    log("[hammer_watcher] 启动完成")


def is_alive(max_age_sec: int = 60) -> Optional[bool]:
    """
    检查 HammerWatcher 是否在跑：通过读心跳文件判断
    - True：心跳新鲜（max_age_sec 内）
    - False：心跳过期（说明线程死了）
    - None：心跳文件不存在（说明从来没跑过）
    """
    if not HEARTBEAT_PATH.exists():
        return None
    try:
        hb = json.load(open(HEARTBEAT_PATH))
        age = time.time() - hb.get("ts", 0)
        return age <= max_age_sec
    except Exception:
        return None