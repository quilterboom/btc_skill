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
    """hammer 直接进成交推送（★ 2026-11-04：卡片真值）

    修的问题：原来这张卡有三个谎 ——
      ① 「做多 0 张」：成交时根本没写 contracts（watcher 不写 → 卡片读缺省 0）
      ② 只显示 TP1/TP2/SL 三个计划价位，不显示**实际成交价**与**实际风险**
         （追价 404 点时，真实风险从 192.8 点涨到 596.8 点、盈亏比从 5.15 掉到 0.99，
          卡上完全看不出来）
      ③ 「监测耗时 1.0s」永远是 1 秒：hammer_ts 存在 meta 里，代码读的是顶层
         → 拿 ts 减 now。真实情况是 hammer 出现后 22 分钟才成交。
    现在：计划入场价 + 实际成交价 + 追价幅度 + 实际风险/盈亏比 + 真实等待时长，
    全部同屏可见。
    """
    try:
        # ★ 2026-10-27：push_once（幂等 key = 持仓id:hammer_fill）——
        #   watcher 每秒巡检，同一笔单的成交提醒只能推一次，不能刷屏。
        from telegram import push_once as _tg_push
        side_cn = "做多"
        tp1 = float(sig.get("tp1", 0))
        tp2 = float(sig.get("tp2", 0))
        sl = float(sig.get("sl", 0))
        contracts = int(sig.get("contracts", 0))
        plan_entry = float(sig.get("plan_entry") or sig.get("entry") or 0)
        # 真实等待时长：自 hammer 出现（meta.hammer_ts）到成交
        meta = sig.get("meta", {}) or {}
        hammer_ts = int(meta.get("hammer_ts") or sig.get("ts") or 0)
        fill_ts = int(sig.get("fill_ts") or 0)
        age_sec = max(0, fill_ts - hammer_ts) if (fill_ts and hammer_ts) else 0
        age_txt = (f"{age_sec/60:.1f} 分钟" if age_sec >= 60 else f"{age_sec:.0f} 秒")
        # 追价幅度：实际成交价 vs 计划入场价
        chase_pts = fill_px - plan_entry if plan_entry else 0.0
        chase_pct = (chase_pts / plan_entry * 100.0) if plan_entry else 0.0
        chase_txt = ""
        if plan_entry and abs(chase_pts) >= 1:
            chase_txt = (f"｜追价 {chase_pts:+,.1f} 点 / {chase_pct:+.2f}%"
                         if chase_pts > 0 else
                         f"｜低于计划 {abs(chase_pts):,.1f} 点 / {chase_pct:+.2f}%")
        # 实际风险（按成交价算）+ 实际盈亏比
        risk_txt = ""
        rr_txt = ""
        if sl and fill_px:
            risk_pts = abs(fill_px - sl)
            risk_pct = risk_pts / fill_px * 100.0
            risk_txt = f"实际风险 {risk_pts:,.1f} 点（{risk_pct:.2f}%）"
            if tp1 and risk_pts > 0:
                rr = abs(tp1 - fill_px) / risk_pts
                rr_txt = f"实际盈亏比 {rr:.2f} : 1"
        card = (
            f"🟢 <b>BTC hammer 直接进</b> 反弹确认\n"
            f"{side_cn} {contracts} 张 @ <b>{fill_px:,.1f}</b>（秒级市价）"
            f"｜计划入场 {plan_entry:,.1f}{chase_txt}\n"
            f"止盈1 <b>{tp1:,.1f}</b> ｜ 止盈2 <b>{tp2:,.1f}</b> ｜ 止损 <b>{sl:,.1f}</b>\n"
            f"{risk_txt}｜{rr_txt}｜hammer 已出现 {age_txt}\n"
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
        sig["note"] = (sig.get("note") or "") + f" | hammer watcher 监测窗过期（最高 ticker 未达 +{TRIGGER_PCT*100:.2f}%）"
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
        _fill_px = round(ticker_px, 1)
        sig["status"] = "pending"   # 改为 pending 让 settler 接管 TP/SL
        sig["filled"] = True
        sig["fill_px"] = _fill_px
        sig["fill_ts"] = now
        sig["_fill_method"] = "market_on_watcher"  # 标记: 监测窗秒级市价进
        # ★ 2026-11-04 卡片/账本真值（此前这里什么账都没记）：
        #   ① 写实际下单张数 —— 原来没写 → 卡片「做多 0 张」、TP1 分段张数也没底
        #   ② TP1/TP2 按**成交价**重算（策略规则：止盈 = 入场价 ×(1+幅度)）
        #      SL 不动：hammer 的 SL 是结构位（hammer L - 0.1%），不随入场价漂移
        #   ③ 计划价留档（plan_entry/plan_tp1/plan_tp2），供卡片与审计对照
        _plan_entry = float(sig.get("entry") or 0)
        sig["plan_entry"] = _plan_entry
        sig["plan_tp1"] = float(sig.get("tp1") or 0)
        sig["plan_tp2"] = float(sig.get("tp2") or 0)
        try:
            from position_tracker import _contracts as _pt_contracts
            # ★ 张数按**真实成交价**定（5000U 名义 / 成交价）= 真实订单量，
            #   与 _settle_one 的 _contracts(_cost_basis()) 同口径 → 卡片与账本一致。
            _qty = int(_pt_contracts(_fill_px)) if _fill_px > 0 else 0
            sig["contracts"] = _qty
            sig["_orig_contracts"] = _qty   # 部分止盈分段的基准量
        except Exception as e:
            log(f"[hammer_watcher] 取张数口径失败（记 0）: {e}")
            sig["contracts"] = 0
        _tp1_pct = float(sig.get("tp1_pct") or 0) / 100.0
        _tp2_pct = float(sig.get("tp2_pct") or 0) / 100.0
        if _tp1_pct > 0:
            sig["tp1"] = round(_fill_px * (1 + _tp1_pct), 1)
        if _tp2_pct > 0:
            sig["tp2"] = round(_fill_px * (1 + _tp2_pct), 1)
        _sl = float(sig.get("sl") or 0)
        if _sl > 0 and _fill_px > 0:
            # 风险占比按真实成交价算（原来按计划 entry 算，追价时低估 3 倍）
            sig["sl_pct"] = round((_fill_px - _sl) / _fill_px * 100, 4)
        elapsed = now - int(sig.get("hammer_ts") or sig.get("ts", now))
        _push_fill(sig, _fill_px, elapsed)
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