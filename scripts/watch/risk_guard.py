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
                or bool(sig.get("partial_close_done"))
                or bool(sig.get("guard_partial_done")))
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


def _notify_close_from_record(rec: Dict) -> None:
    """★ 2026-10-29 新增：risk_guard 实时平仓后必须推平仓卡。

    Bug（实测 10-25 之后 12 笔结算全部无平仓卡，生产 7 + 训练 5）：
      risk_guard 用实时 ticker 抢先结算（settler 用 1m K 回放，最坏延迟 60s），
      结算完把 status 改成 settled → settler 的 settle_all 只处理 status=='pending'
      的记录 → position_tracker._notify_close 永远不会被调用 → **平仓卡静默丢失**。
      用户只在 TG 看到「已成交入场」，之后没有任何出场通知。
      连 10-29 04:10 那笔 -15.41U 的 SL 都没有卡。

    修复：risk_guard 结算成功后自己推卡。幂等 key 复用 {持仓id}:close
      （position_tracker._notify_close 内部就是 _push_once(f"{id}:close")）
      → 与 settler 路径天然互斥，不会双推。

    res 从 rec 现取（rec 里已有 net_usd/gross_usd/category/outcome/exit_px/exit_ts），
    不重算一遍公式——口径唯一在 position_tracker._close。
    """
    try:
        from position_tracker import _notify_close
    except Exception as e:
        log(f"[risk_guard] 无法导入 position_tracker._notify_close: {e}")
        return
    res = {
        "outcome": rec.get("outcome"),
        "category": rec.get("category"),
        "net_usd": rec.get("net_usd"),
        "gross_usd": rec.get("gross_usd"),
        "exit_px": rec.get("exit_px"),
        "exit_ts": rec.get("exit_ts"),
        "contracts": rec.get("contracts"),
        "partial_exits": rec.get("partial_exit_count", 0) or 0,
    }
    try:
        _notify_close(rec, res)
    except Exception as e:
        log(f"[risk_guard] 平仓卡推送失败 {rec.get('id')}: {e}")


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
    # ★ 2026-10-30：outcome 必须采用 canonical 判定，不能用上面那个粗略猜测。
    #   上面只看 tp1_hit，漏了 EMA 预警 / 1h 浮亏减仓 两个保本移动来源，
    #   会把「保本被打」误写成「止损扫掉」（反之亦然）。
    #   以 _canonical_close 的 outcome 为准（它按「保本移动过 + 出场价在 entry
    #   附近」双重确认），跟 category 同源，不会自相矛盾。
    if res.get("outcome"):
        rec["outcome"] = res["outcome"]
        rec["pos_state"] = res.get("pos_state") or rec["outcome"]
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


def _try_partial_close(sig: Dict, last: float, qty_pct: int, reason: str,
                       kind: str = "tp1") -> bool:
    """减仓 + 推保本。返回是否成功执行。

    kind="tp1"   价格**到了** tp1 → 真部分止盈（锁利）
    kind="guard" 价格**反向**走 → 主动防御减仓（锁亏）

    ★ 2026-10-30 修复（严重）：两种触发原来共用同一条 TP1 路径。
      实测训练单 BTC_USDT-1793320220-long（10-30 08:32，做多 @77146.7）：
        价格1.4 分钟内反向 0.3% 到 76,870（TP1=78,149.6 从未触达），
        却写 `tp1_hit=True` + `tp1_hit_px=76870`（亏损价），
        卡片推「🟢 BTC 部分止盈 TP1 ｜ 锁利 ≈ -5.98U」——
        负数写「锁利」、绿色图标、还谎称 TP1。**做多亏钱却说止盈。**
      根因 3 层：
        ① adverse 分支复用 `_try_partial_close` → 无条件 stamp tp1_hit
        ② `_notify_partial` 文案硬编码「部分止盈 / 锁利 / 🟢」，不看盈亏符号
        ③ `_close` 的 category 只看「是否推过保本」，net=-22.87 也写 category=profit
      修：kind 分流字段（guard 走 guard_partial_*，**不碰 tp1_hit**），
          卡片文案按 pnl 符号分流，category 由 net 符号定。
    """
    side = sig.get("side")
    entry = float(sig.get("entry", 0))
    tp1 = float(sig.get("tp1", 0))
    if not side or not entry:
        return False
    if kind == "tp1" and not tp1:
        return False
    now = int(time.time())
    # 本段卖出张数：原始总量 // PARTS —— 与 position_tracker._close 的
    # part_size 同口径（qty_pct 参数当前未生效，见文末 D5，先保持记账不变）
    from position_tracker import PARTS as _PARTS
    base = int(sig.get("_orig_contracts") or sig.get("contracts") or 0)
    if base <= 0:
        return False
    qty = max(1, base // _PARTS)
    if not sig.get("_orig_contracts"):
        sig["_orig_contracts"] = base
    if kind == "tp1":
        # 真 TP1：价格到 tp1，这段是**锁利**
        sig["tp1_hit"] = True
        sig["tp1_hit_ts"] = now
        sig["tp1_hit_px"] = round(last, 1)
        sig["pos_state"] = "TP1_PARTIAL"
    else:
        # 主动防御：价格反向，这段是**锁亏**，不是止盈。
        # 独立字段，绝不写 tp1_hit —— 否则下游把亏损价当止盈价
        # （_close 会拿 tp1_hit_px 算「止盈腿」、category 会被判成 profit）。
        sig["guard_partial_done"] = True
        sig["guard_partial_ts"] = now
        sig["guard_partial_px"] = round(last, 1)
        sig["guard_partial_qty"] = qty
        sig["pos_state"] = "GUARD_PARTIAL"
    sig["active_sl"] = entry    # 推保本
    sig["partial_exits"] = sig.get("partial_exits", 0) + 1
    sig["partial_exit_count"] = sig.get("partial_exit_count", 0) + 1
    sig["note"] = (sig.get("note", "") or "") + f" | [risk_guard] {reason} @ {last:.1f}"
    # ★ 2026-10-29：这里必须自己推卡（settler 不会补推，见上方说明）
    _notify_partial_from_record(sig, last,
                                label=("TP1" if kind == "tp1" else "GUARD"))
    return True


def _notify_partial_from_record(sig: Dict, px: float, label: str = "TP1") -> None:
    """★ 2026-10-29：risk_guard 触发减仓时推卡（settler 不补推）。

    张数口径：本段卖出 = 原始总量 // PARTS（与 position_tracker._close 里
    part_size = contracts_orig // PARTS 一致）。risk_guard 不实际减仓，
    但卡片上的数字必须跟最终结算用同一口径，否则「卡上写的张数」和
    「journal 算的 PnL」对不上（display-integrity 硬规则）。
    """
    try:
        from position_tracker import _notify_partial, PARTS
    except Exception as e:
        log(f"[risk_guard] 无法导入 position_tracker._notify_partial: {e}")
        return
    try:
        contracts = int(sig.get("contracts") or 0)
        base = int(sig.get("_orig_contracts") or contracts)
        qty = int(sig.get("guard_partial_qty") or 0) or max(1, base // PARTS)
        _ts = int(sig.get("guard_partial_ts") or sig.get("tp1_hit_ts") or time.time())
        _notify_partial(sig, label, px, contracts or base, _ts, qty=qty)
    except Exception as e:
        log(f"[risk_guard] 部分减仓卡推送失败 {sig.get('id')}: {e}")


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
            _try_partial_close(sig, last, 50, f"tp1_hit @ {last:.1f}", kind="tp1")
            return "TP1"
        if side == "short" and last <= tp1:
            _try_partial_close(sig, last, 50, f"tp1_hit @ {last:.1f}", kind="tp1")
            return "TP1"

    # 3) 主动防御: 进场 N 分钟内反向 X%
    #    ★ 2026-10-30：这条是**亏损方向**的减仓，必须 kind="guard"（锁亏），
    #      绝不能沿用 TP1 路径 —— 否则亏损单会被写成「部分止盈 TP1」。
    minutes_since_fill = (now_ts - fill_ts) / 60
    if minutes_since_fill <= ACTIVE_GUARD_MINUTES and not cur_tp1_hit \
            and not sig.get("guard_partial_done"):
        if side == "long" and last < entry * (1 - ADVERSE_MOVE_PCT):
            _try_partial_close(sig, last, 50, f"adverse_long {minutes_since_fill:.1f}min {last:.1f}<entry-{ADVERSE_MOVE_PCT*100:.1f}%", kind="guard")
            return "GUARD"
        if side == "short" and last > entry * (1 + ADVERSE_MOVE_PCT):
            _try_partial_close(sig, last, 50, f"adverse_short {minutes_since_fill:.1f}min {last:.1f}>entry+{ADVERSE_MOVE_PCT*100:.1f}%", kind="guard")
            return "GUARD"

    return "hold"


# ============================ 主循环 ============================
def risk_guard_loop():
    log("[risk_guard] 线程启动")
    # ★ 2026-10-25 特征串（journalctl 可见）：证明 canonical 结算新代码已加载。
    #   2026-10-30 修：原来用 print() —— systemd `StandardOutput=append:` 下
    #   print 不落 watch.log（log() 是直接写文件的），导致这个"特征串验证"从没生效过，
    #   连 10-25 那条都从没出现在日志里。改用 log() 才真进日志。
    log("[risk_guard] FEATURE canonical-settle-v2 enabled "
        "(SL→net_usd/gross_usd/category + outcome event)")
    # ★ 2026-10-30 特征串：证明「主动防御≠TP1」修复已加载
    #   （guard 走 guard_partial_*、卡片按盈亏符号分流、category 由 net 符号定）
    log("[risk_guard] FEATURE guard-not-tp1-v1 enabled "
        "(adverse→guard_partial_*, card wording by pnl sign, category by net sign)")
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
            pending_close_cards: List[Dict] = []   # ★ 2026-10-29：risk_guard 结算后要推的平仓卡
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
                        # ★ 2026-10-29 修复：risk_guard 抢先结算后 settler 就不会
                        #   再处理这条（status 已 settled）→ 平仓卡必须在这里推。
                        #   幂等 key 复用 {id}:close，与 settler 路径互斥。
                        pending_close_cards.append(r)
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
                # ★ 2026-10-29：journal 落盘 + event 写入成功后才推平仓卡。
                #   顺序很关键：先落盘再推送 → 推送失败时 journal 仍有结算记录，
                #   反过来（先推卡）会出现「卡说有平仓、journal 没记录」的分裂。
                for rec in pending_close_cards:
                    _notify_close_from_record(rec)
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
