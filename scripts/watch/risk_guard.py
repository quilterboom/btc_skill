#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
RealtimeRiskGuard — 实时风控线程 (2026-10-17 liusir 决策)

为什么独立于 Settler:
  - Settler 每 5s 巡检一次，用 1m K 线 close，最坏情况延迟 ~60s
  - 但"行情不按预期走"可能在 1 秒内发生（瞬间插针 / 流动性真空）
  - 用 ticker 实时价（毫秒级）+ 持仓状态做主动防御

触发逻辑 (liusir 10-17 决策):
  1. 主动防御（"行情不按预期走"):
     - 进场后 N 分钟内反向 ≥ X% → 自动减半 + 推保本
     - 默认 N=3 分钟, X=0.3% (挂单支持位下方 0.3%)
  2. 强制止损（保底):
     - 实时价 ≤ SL → 立即全平（settler 兜底，guard 加速）
  3. TP1 hit → 立即推保本（settler 兜底，guard 加速）

启动: 在 watch/main 调 start_risk_guard()
频率: 1 秒一次（poll ticker）
"""
from __future__ import annotations
import os, time, threading, json
from pathlib import Path
from typing import Optional, List, Dict

from gate_fetch import DATA_DIR, fetch_ticker, fetch_contract
from .common import log


# ============================ 配置 ============================
INTERVAL_SEC = 1.0                    # 每 1 秒检查一次
ACTIVE_GUARD_MINUTES = 3              # 进场后 3 分钟内启用主动防御
ADVERSE_MOVE_PCT = 0.003              # 反向 0.3% (挂单支持位下方)
TP1_PUSH_BE = True                    # TP1 hit → 推保本
HEARTBEAT_EVERY = 60                  # 每 60 轮打一行心跳
HEARTBEAT_PATH = Path(DATA_DIR) / "risk_guard_heartbeat.json"


# ============================ 心跳 ============================
def _write_heartbeat(n_iter, n_guard_triggers, n_sl_triggers, n_tp1_triggers):
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        tmp = HEARTBEAT_PATH.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({
                "ts": int(time.time()),
                "n_iter": n_iter,
                "guard_triggers": n_guard_triggers,
                "sl_triggers": n_sl_triggers,
                "tp1_triggers": n_tp1_triggers,
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
            if not line: continue
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


# ============================ canonical 结算 ============================
# ★ 2026-10-25 修复：SL 分支原来只写 exit_ts/exit_px/status/outcome/pos_state/
#   risk_guard_exit_*，**不写** net_usd/gross_usd/category，也**不 append** outcome event。
#   后果：settle_all 走 r.update(res)+_append_event 的整条链被绕过 →
#     · daily_report 把 null net 当 0 计（v4.2 净利虚高、23:15 那笔 -15.12U 完全消失）
#     · dashboard 胜率统计漏掉这些单（web/server.py 只认 position_events.jsonl 的 outcome 事件）
#   正确口径唯一在 position_tracker._close（FEE_USD/QUANT_MULT/PARTS + 部分止盈段），
#   必须复用它，不能在 watch 里抄一份公式。position_tracker 不 import watch，反向
#   import 无循环依赖（延迟 import 到函数内，避免 import 顺序问题）。
def _canonical_close(sig: Dict, exit_px: float, exit_ts: int) -> Dict:
    """用 position_tracker._close 算 canonical net_usd/gross_usd/category。

    outcome 判定与 position_tracker._settle_one 的 t_sl 分支同口径：
    「保本移动过」= tp1_hit OR ema_warning_done OR partial_close_done，
    再加「出场价在 entry 附近」二次确认 → BE_STOPPED(profit) / SL_STOPPED(loss)。
    """
    from position_tracker import _close as _pt_close, _contracts as _pt_contracts
    side = sig.get("side")
    entry = float(sig.get("entry") or 0)
    if not side or not entry:
        raise ValueError(f"无 side/entry，无法 canonical 结算 (id={sig.get('id')})")
    contracts = int(sig.get("contracts") or 0) or _pt_contracts(entry)
    cur_sl = float(sig.get("active_sl") or sig.get("sl") or 0)
    be_moved = (bool(sig.get("tp1_hit"))
                or bool(sig.get("ema_warning_done"))
                or bool(sig.get("partial_close_done")))
    is_be = be_moved and abs(cur_sl - entry) < 1.0
    outcome = "BE_STOPPED" if is_be else "SL_STOPPED"
    category = "profit" if is_be else "loss"
    return _pt_close(sig, outcome, exit_px, exit_ts, contracts, side, entry, category)


def _append_outcome_event(ev: Dict) -> bool:
    """幂等地追加 outcome event（已有同 id 的 outcome 事件则跳过）。"""
    from position_tracker import has_outcome_event, _append_event
    if has_outcome_event(ev.get("id") or ""):
        return False
    _append_event(ev)
    return True


def _apply_sl(rec: Dict, last: float, now_ts: int) -> Optional[Dict]:
    """SL 平仓：写 canonical net/gross/category，返回待落盘的 outcome event。

    返回 None = canonical 结算失败 → **不写 net**（宁可留空也不编造数字），
    调用方照样标 settled（避免 risk_guard 重复触发），但不会 append event。
    """
    exit_px = round(last, 1)
    rec["exit_ts"] = now_ts
    rec["exit_px"] = exit_px
    # ★ 2026-10-22 修复：SL 分支必须改 status，否则下一轮
    #   `if r.get("status") != "pending": continue` 守卫永远放行，
    #   每秒重复触发一次（实测单笔刷了 26267 次，journal 被反复写盘）。
    #   同时补 outcome/pos_state，settler 才有据可依。
    rec["status"] = "settled"
    rec["outcome"] = rec.get("outcome") or (
        "BE_STOPPED" if rec.get("tp1_hit") else "SL_STOPPED")
    rec["pos_state"] = rec.get("pos_state") or rec["outcome"]
    rec["risk_guard_exit_ts"] = now_ts
    rec["risk_guard_exit_px"] = exit_px
    try:
        res = _canonical_close(rec, exit_px, now_ts)
    except Exception as e:
        rec["settle_error"] = str(e)[:300]
        rec["settle_error_ts"] = now_ts
        log(f"[risk_guard] {rec.get('id', '?')} canonical 结算失败，net 留空（不编造）: {e}")
        return None
    rec["net_usd"] = res["net_usd"]
    rec["gross_usd"] = res["gross_usd"]
    rec["category"] = res["category"]
    return {"id": rec.get("id"), "outcome": rec["outcome"],
            "net_usd": res["net_usd"], "category": res["category"],
            "exit_ts": now_ts, "ts": int(time.time())}


# ============================ 触发函数 ============================
def _ticker_last(contract="BTC_USDT") -> Optional[float]:
    try:
        ts = fetch_ticker(contract)
        if ts:
            return float(ts[0].get("last") or ts[0].get("last_price") or 0)
    except Exception:
        pass
    return None


def _try_partial_close(sig: Dict, last: float, qty_pct: int, reason: str) -> bool:
    """减仓 qty_pct % 仓位 + 推保本。返回是否成功执行。
    简化: 通过 _settle_one 不行 — 这里只做"标记 tp1_hit + 设 active_sl=entry"让 settler 接手平仓。
    真正的市价减仓由 position_tracker 处理（但要先触发 tp1_hit）。"""
    side = sig.get("side")
    entry = float(sig.get("entry", 0))
    tp1 = float(sig.get("tp1", 0))
    if not side or not entry or not tp1:
        return False
    # 标记: tp1_hit (虽然价格未必真到 TP1, 但手动逻辑是"立即推保本"等价于 TP1 减半)
    sig["tp1_hit"] = True
    sig["tp1_hit_ts"] = int(time.time())
    sig["tp1_hit_px"] = round(last, 1)
    sig["active_sl"] = entry    # 推保本
    sig["pos_state"] = "TP1_PARTIAL"
    sig["partial_exits"] = sig.get("partial_exits", 0) + 1
    sig["note"] = (sig.get("note", "") or "") + f" | [risk_guard] {reason} @ {last:.1f}"
    return True


def _check_one(sig: Dict, last: float, now_ts: int):
    """检查单条 pending 持仓，返回是否动了它。"""
    side = sig.get("side")
    entry = float(sig.get("entry", 0))
    sl = float(sig.get("sl", 0))
    tp1 = float(sig.get("tp1", 0))
    if not side or not entry:
        return "skip"
    fill_ts = int(sig.get("fill_ts") or 0)
    if not fill_ts:
        return "skip"  # 未成交不算"持仓"

    cur_sl = float(sig.get("active_sl") or sl)
    cur_tp1_hit = bool(sig.get("tp1_hit"))

    # 1) 强制止损（保底）
    if side == "long" and last <= cur_sl:
        return "SL"
    if side == "short" and last >= cur_sl:
        return "SL"

    # 2) TP1 hit (实时价检测, 加速 settler)
    if TP1_PUSH_BE and not cur_tp1_hit:
        if side == "long" and last >= tp1:
            _try_partial_close(sig, last, 50, f"tp1_hit @ {last:.1f}")
            return "TP1"
        if side == "short" and last <= tp1:
            _try_partial_close(sig, last, 50, f"tp1_hit @ {last:.1f}")
            return "TP1"

    # 3) 主动防御: 进场 N 分钟内反向 X%
    minutes_since_fill = (now_ts - fill_ts) / 60
    if minutes_since_fill <= ACTIVE_GUARD_MINUTES and not cur_tp1_hit:
        if side == "long" and last < entry * (1 - ADVERSE_MOVE_PCT):
            _try_partial_close(sig, last, 50, f"adverse_long {minutes_since_fill:.1f}min {last:.1f}<entry-{ADVERSE_MOVE_PCT*100:.1f}%")
            return "GUARD"
        if side == "short" and last > entry * (1 + ADVERSE_MOVE_PCT):
            _try_partial_close(sig, last, 50, f"adverse_short {minutes_since_fill:.1f}min {last:.1f}>entry+{ADVERSE_MOVE_PCT*100:.1f}%")
            return "GUARD"

    return "hold"


# ============================ 主循环 ============================
def risk_guard_loop():
    log("[risk_guard] 线程启动")
    # ★ 2026-10-25 特征串（journalctl 可见）：证明 canonical 结算新代码已加载。
    #   注意 log() 只在 isatty 时打 stdout，systemd 下不走 journald；
    #   run 命令不 daemonize，直接 print 才能进 journalctl 供验证。
    print("[risk_guard] FEATURE canonical-settle-v2 enabled "
          "(SL→net_usd/gross_usd/category + outcome event)", flush=True)
    try:
        contract = fetch_contract("BTC_USDT")
    except Exception as e:
        log(f"[risk_guard] 合约获取失败（线程退出）: {e}")
        return
    log(f"[risk_guard] 实时风控线程已就绪（每 {INTERVAL_SEC}s 巡检｜"
        f"主动防御 {ACTIVE_GUARD_MINUTES}min/{ADVERSE_MOVE_PCT*100:.1f}%）")

    n_iter = 0
    n_guard = 0
    n_sl = 0
    n_tp1 = 0

    while True:
        try:
            n_iter += 1
            last = _ticker_last("BTC_USDT")
            now_ts = int(time.time())
            if last is None:
                # ticker 拉不到, 跳过本轮
                _write_heartbeat(n_iter, n_guard, n_sl, n_tp1)
                time.sleep(INTERVAL_SEC)
                continue
            recs = _read_journal()
            changed = False
            action = "hold"   # 默认 hold, 避免 unbound
            rid = "?"
            pending_events: List[Dict] = []   # journal 落盘成功后才 append（避免事件先于状态）
            for r in recs:
                if r.get("status") != "pending":
                    continue
                rid = r.get("id", "?")
                action = _check_one(r, last, now_ts)
                if action == "GUARD":
                    n_guard += 1
                    changed = True
                elif action == "SL":
                    n_sl += 1
                    # ★ 2026-10-25 修复：canonical 结算（net_usd/gross_usd/category）
                    #   + outcome event，走 position_tracker._close，不再只写出场字段。
                    #   事件先算出来、journal 写盘成功后再 append（幂等）。
                    ev = _apply_sl(r, last, now_ts)
                    if ev:
                        pending_events.append(ev)
                    changed = True
                elif action == "TP1":
                    n_tp1 += 1
                    changed = True
            if changed:
                _write_journal(recs)
                log(f"[risk_guard] 触发 {action} on {rid}")
                # ★ 2026-10-25：journal 落盘成功后才 append outcome event（幂等，
                #   已有同 id outcome 事件则跳过）—— 补上 settle_all 那条被绕过的链。
                for ev in pending_events:
                    try:
                        if _append_outcome_event(ev):
                            log(f"[risk_guard] outcome event 已补写 {ev.get('id')} "
                                f"net={ev.get('net_usd')} cat={ev.get('category')}")
                    except Exception as e:
                        log(f"[risk_guard] outcome event 写入失败 {ev.get('id')}: {e}")
            if n_iter % HEARTBEAT_EVERY == 0:
                log(f"[risk_guard] 心跳 #{n_iter}（guard={n_guard} sl={n_sl} tp1={n_tp1}）")
            _write_heartbeat(n_iter, n_guard, n_sl, n_tp1)
        except Exception as e:
            log(f"[risk_guard] 巡检异常: {e}")
        time.sleep(INTERVAL_SEC)


def start_risk_guard():
    threading.Thread(target=risk_guard_loop, daemon=True, name="RiskGuard").start()


def is_alive(max_age_sec: int = 60):
    if not HEARTBEAT_PATH.exists():
        return None
    try:
        hb = json.load(open(HEARTBEAT_PATH))
        return (time.time() - hb.get("ts", 0)) <= max_age_sec
    except Exception:
        return None
