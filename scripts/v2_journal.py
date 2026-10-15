"""
v2_journal.py — v2.1 训练版虚拟持仓 journal
==========================================

liusir 2026-10-14 拍板：
  - 训练版 v2.1 触发后要能模拟持仓 + 算 PnL（"训练也有加仓，不开仓怎么知道是赚还是亏"）
  - v2.1 自己探索 SL/TP（"绝大多数不会依照自己的想法来"）
  - 回填昨天的历史数据（30 条触发判定 → 模拟 → 看赚亏）

设计：
  ┌──────────────────────────────────────────┐
  │ v2.1 触发（v2_decisions.jsonl）          │
  │   triggered=True → 调 open_v2_position  │
  └──────────────────────────────────────────┘
            ↓
  ┌──────────────────────────────────────────┐
  │ open_v2_position()                       │
  │   - 用 ATR 自适应算 SL（0.4%-1.5% 区间）│
  │   - TP1/TP2 保守（0.5%/1.0%）           │
  │   - 减仓 50% + 推保本（跟实盘一致）     │
  │   - 仓位 50U × 100x = 603 张             │
  │   - duplicate_blocked（同向持仓）       │
  │   - 写 v2_journal.jsonl                  │
  └──────────────────────────────────────────┘
            ↓
  ┌──────────────────────────────────────────┐
  │ settle_v2_all() （每 15min 跑一次）      │
  │   - 拉 1m K 线回放 pending 单            │
  │   - 判定 TP1 / SL / BE / 持仓中         │
  │   - 算 PnL（含 4.85U taker fee）         │
  └──────────────────────────────────────────┘

字段约定：
  - entry 用 v2.1 触发时的 snapshot.px
  - SL 用 ATR 自适应（atr_period=14）
  - TP1/TP2 固定 0.5%/1.0%（保守，"不会依照自己的想法来"）
  - 减仓段 = TP1 段 + 剩余段；TP1 命中后 active_sl=entry（保本）
  - net_usd = gross_usd - 4.85 (taker fee, 跟实盘 2026-10-08 一致)
"""
from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

# 训练版虚拟持仓落盘位置（跟实盘 journal + v2_decisions 完全独立）
TRAINING_DIR = Path("/root/data/training")
V2_JOURNAL_PATH = TRAINING_DIR / "v2_journal.jsonl"
TRAINING_DIR.mkdir(parents=True, exist_ok=True)

# v2.1 自己探索的 SL/TP 规则（liusir 2026-10-14 拍板）
V2_SLTB_RULES = {
    "atr_period": 14,             # 1m K 线 ATR 周期
    "sl_min_pct": 0.004,          # SL 最小 0.4%
    "sl_max_pct": 0.015,          # SL 最大 1.5%
    "tp1_pct": 0.005,             # TP1 固定 0.5%
    "tp2_pct": 0.010,             # TP2 固定 1.0%
    "partial_pct": 0.50,          # 减仓 50%
    "bal": 50.0,                  # 模拟资金 50U（跟实盘一致）
    "lev": 100,                   # 100x（跟实盘一致）
    "taker_fee_usd": 4.85,        # taker 手续费
    "cooldown_secs": 1800,        # 30min cooldown（同向持仓）
}

QUANT_MULT = 0.0001  # 1 张 = 0.0001 BTC


# ============================================================
# ATR 自适应 SL 计算（liusir 10-14: "v2.1 自己摸索一套 SL"）
# ============================================================
def compute_atr_pct(contract: str = "BTC_USDT", ts: int = None, period: int = 14) -> float:
    """
    用最近 period 根 1m K 线的 ATR（average true range）算 SL 百分比。
    - True Range = max(high-low, |high-prev_close|, |low-prev_close|)
    - ATR = mean(TR, period)
    - atr_pct = ATR / close

    返回的 atr_pct 是绝对值（如 0.0063 = 0.63%）。
    """
    try:
        sys.path.insert(0, "/root/skills/btc-gate-intraday-strategy/scripts")
        from gate_fetch import fetch_ohlcv

        # 取触发时刻前 period+1 根 1m K 线
        end_ts_arg = ts if ts is not None else int(time.time())
        bars = fetch_ohlcv(contract=contract, interval="1m", total=period + 5,
                            end_ts=end_ts_arg + 60, verbose=False)
        if len(bars) < period + 1:
            return V2_SLTB_RULES["sl_min_pct"]  # 数据不够用最小值

        # 截取 [ts-period*60, ts] 的窗口
        if ts:
            window = [b for b in bars if b[0] <= ts][-period - 1:]
        else:
            window = bars[-(period + 1):]

        if len(window) < period + 1:
            return V2_SLTB_RULES["sl_min_pct"]

        # 算 TR
        trs = []
        for i in range(1, len(window)):
            high = window[i][2]
            low = window[i][3]
            prev_close = window[i - 1][4]
            tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
            trs.append(tr)

        if not trs:
            return V2_SLTB_RULES["sl_min_pct"]

        atr = sum(trs) / len(trs)
        last_close = window[-1][4]
        if last_close <= 0:
            return V2_SLTB_RULES["sl_min_pct"]

        atr_pct = atr / last_close
        # clamp 到 [sl_min, sl_max]
        return max(V2_SLTB_RULES["sl_min_pct"],
                   min(V2_SLTB_RULES["sl_max_pct"], atr_pct))
    except Exception as e:
        # fallback 用固定最小值
        return V2_SLTB_RULES["sl_min_pct"]


# ============================================================
# 写入函数（原子：先写 tmp 再 replace）
# ============================================================
def _read_journal() -> list:
    if not V2_JOURNAL_PATH.exists():
        return []
    out = []
    with open(V2_JOURNAL_PATH) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def _write_journal(jrnl: list) -> None:
    """原子写：tmp + replace（跟实盘 journal 一致）"""
    tmp_path = V2_JOURNAL_PATH.with_suffix(".jsonl.tmp")
    with open(tmp_path, "w") as f:
        for sig in jrnl:
            f.write(json.dumps(sig, ensure_ascii=False) + "\n")
    os.replace(tmp_path, V2_JOURNAL_PATH)


# ============================================================
# 开仓：v2.1 触发 → 模拟开仓
# ============================================================
def open_v2_position(decision: dict) -> dict | None:
    """
    v2.1 触发后调：开一笔虚拟持仓。

    参数：
      decision: v2_decisions.jsonl 里的一条判定（含 ts_epoch, bias, snapshot.px, decision_id）

    返回：
      新开的虚拟单 dict（写入 journal 后返回）；None = 被 duplicate_blocked
    """
    bias = decision["bias"]
    if bias not in ("long", "short"):
        return None  # neutral 不开仓

    ts_epoch = decision["ts_epoch"]
    px = decision["snapshot"]["px"]
    decision_id = decision["decision_id"]

    # cooldown 检查：30min 内同向持仓不重开（只看 pending，invalidated 不影响）
    jrnl = _read_journal()
    for sig in reversed(jrnl):
        if sig.get("status") != "pending":
            continue  # settled/invalidated 都不参与 cooldown 判定
        if sig.get("side") == bias and sig.get("signal_source") == "v2_aggregate":
            # 检查 cooldown（用持仓的开仓 ts，不是决策 ts）
            sig_ts = sig.get("ts") or sig.get("fill_ts") or 0
            if ts_epoch - sig_ts < V2_SLTB_RULES["cooldown_secs"]:
                # 新判定被拦，sig（之前的 pending 单）继续保留
                return None
            # 上一笔已过 cooldown → 自然放行（不做任何标记）

    # 算 ATR 自适应 SL
    atr_pct = compute_atr_pct(ts=ts_epoch)
    sl_pct = atr_pct
    tp1_pct = V2_SLTB_RULES["tp1_pct"]
    tp2_pct = V2_SLTB_RULES["tp2_pct"]

    # 算绝对价位
    if bias == "long":
        sl = px * (1 - sl_pct)
        tp1 = px * (1 + tp1_pct)
        tp2 = px * (1 + tp2_pct)
    else:  # short
        sl = px * (1 + sl_pct)
        tp1 = px * (1 - tp1_pct)
        tp2 = px * (1 - tp2_pct)

    # 仓位：50U × 100x = 5000U 名义
    bal = V2_SLTB_RULES["bal"]
    lev = V2_SLTB_RULES["lev"]
    notional = bal * lev
    contracts = int(notional * QUANT_MULT)  # 1 张 = 0.0001 BTC

    sig_id = f"v2-{decision_id}-{bias}"
    sig = {
        "id": sig_id,
        "contract": decision.get("contract", "BTC_USDT"),
        "ts": ts_epoch,
        "side": bias,
        "verdict": f"v2.1 触发 ({decision.get('consensus', '?')}, norm={decision['norm']:.3f})",
        "signal_source": "v2_aggregate",
        "decision_id": decision_id,
        "score": 0,
        "core_ok": False,
        "entry": px,
        "entry_break": px,
        "sl": sl,
        "tp1": tp1,
        "tp2": tp2,
        "sl_pct": sl_pct,
        "tp1_pct": tp1_pct,
        "tp2_pct": tp2_pct,
        "tp_mode": "v2_explore",
        "status": "pending",
        "filled": False,
        "tags": {
            "consensus": decision.get("consensus"),
            "norm": decision["norm"],
            "composite_score": decision.get("composite_score"),
        },
        "meta": {
            "bal": bal,
            "lev": lev,
            "risk": 0.01,
            "mode": "cross",
            "atr_at_open": atr_pct,
            "atr_period": V2_SLTB_RULES["atr_period"],
        },
    }

    jrnl.append(sig)
    _write_journal(jrnl)
    return sig


# ============================================================
# Settler：每 15min 跑一次，回放 1m K 线更新持仓
# ============================================================
def settle_v2_all(verbose: bool = False) -> dict:
    """
    跟实盘 settle_all 类似，但只处理 v2_journal.jsonl。

    回放逻辑（跟实盘一致）：
      - pending 且 filled=False → 找触发 ts 之后的第一根 K 线 low/high 判定是否成交
      - pending 且 filled=True → 检查 TP1 / SL 是否触发
      - TP1 命中 → 推保本（active_sl=entry）+ partial_exit_count+1
      - SL 命中（active_sl 或 sl）→ 立刻 close
      - BE_STOPPED：SL 在 active_sl=entry 时被触发
      - TP2 命中 → 全部平仓

    返回：{'done': int, 'pending': int, 'net_usd': float}
    """
    import math
    sys.path.insert(0, "/root/skills/btc-gate-intraday-strategy/scripts")
    from gate_fetch import fetch_ohlcv

    jrnl = _read_journal()
    if not jrnl:
        return {"done": 0, "pending": 0, "net_usd": 0.0}

    # 一次性拉最新 1m K 线（满足所有 pending 的回放）
    now_ts = int(time.time())
    bars = fetch_ohlcv(contract="BTC_USDT", interval="1m", total=2000,
                        end_ts=now_ts + 60, verbose=False)

    done = 0
    total_net = 0.0
    pending_count = 0

    for sig in jrnl:
        if sig.get("status") == "settled":
            continue
        if sig.get("outcome") == "DUPLICATE_BLOCKED":
            continue

        ts_open = sig["ts"]
        side = sig["side"]
        entry = sig["entry"]
        sl = sig["sl"]
        tp1 = sig["tp1"]
        tp2 = sig.get("tp2")
        contracts = sig.get("contracts") or int(V2_SLTB_RULES["bal"] *
                                               V2_SLTB_RULES["lev"] *
                                               QUANT_MULT)
        sig["contracts"] = contracts  # 写回

        # 找 ts_open 之后的 K 线
        future = [b for b in bars if b[0] >= ts_open]
        if not future:
            pending_count += 1
            continue

        # 第一阶段：成交
        if not sig.get("filled"):
            for b in future:
                _, o, h, l, c, v, s = b
                if side == "long" and l <= entry:
                    sig["filled"] = True
                    sig["fill_ts"] = b[0]
                    sig["fill_px"] = entry
                    sig["last_check_ts"] = b[0]
                    break
                elif side == "short" and h >= entry:
                    sig["filled"] = True
                    sig["fill_ts"] = b[0]
                    sig["fill_px"] = entry
                    sig["last_check_ts"] = b[0]
                    break
            if not sig.get("filled"):
                pending_count += 1
                continue

        # 第二阶段：TP/SL 回放
        active_sl = sig.get("active_sl") or sl
        tp1_hit = sig.get("tp1_hit", False)
        tp1_hit_px = sig.get("tp1_hit_px")
        tp1_hit_ts = sig.get("tp1_hit_ts")
        last_check = sig.get("last_check_ts", sig["fill_ts"])

        settled = False
        close_px = None
        close_ts = None
        outcome = None

        for b in future:
            if b[0] <= last_check:
                continue
            _, o, h, l, c, v, s = b

            if side == "long":
                # SL 检查（active_sl 优先）
                if l <= active_sl:
                    close_px = active_sl
                    close_ts = b[0]
                    if math.isclose(active_sl, entry, abs_tol=1.0):
                        outcome = "BE_STOPPED"
                    else:
                        outcome = "SL_STOPPED"
                    settled = True
                    break
                # TP1 检查
                if not tp1_hit and h >= tp1:
                    tp1_hit = True
                    tp1_hit_px = tp1
                    tp1_hit_ts = b[0]
                    sig["tp1_hit"] = True
                    sig["tp1_hit_px"] = tp1
                    sig["tp1_hit_ts"] = b[0]
                    sig["active_sl"] = entry  # 推保本
                    sig["partial_exit_count"] = sig.get("partial_exit_count", 0) + 1
                    active_sl = entry
                # TP2 检查
                if tp2 and h >= tp2:
                    close_px = tp2
                    close_ts = b[0]
                    outcome = "TP2_DONE"
                    settled = True
                    break
            else:  # short
                if h >= active_sl:
                    close_px = active_sl
                    close_ts = b[0]
                    if math.isclose(active_sl, entry, abs_tol=1.0):
                        outcome = "BE_STOPPED"
                    else:
                        outcome = "SL_STOPPED"
                    settled = True
                    break
                if not tp1_hit and l <= tp1:
                    tp1_hit = True
                    tp1_hit_px = tp1
                    tp1_hit_ts = b[0]
                    sig["tp1_hit"] = True
                    sig["tp1_hit_px"] = tp1
                    sig["tp1_hit_ts"] = b[0]
                    sig["active_sl"] = entry
                    sig["partial_exit_count"] = sig.get("partial_exit_count", 0) + 1
                    active_sl = entry
                if tp2 and l <= tp2:
                    close_px = tp2
                    close_ts = b[0]
                    outcome = "TP2_DONE"
                    settled = True
                    break

            sig["last_check_ts"] = b[0]

        if settled:
            # 算 PnL
            contracts_orig = contracts
            if tp1_hit and outcome not in ("TP2_DONE",):
                # 部分平仓：TP1 段按 tp1 算，剩余按 close_px
                contracts_part = int(contracts_orig * V2_SLTB_RULES["partial_pct"])
                contracts_remain = contracts_orig - contracts_part
                if side == "long":
                    gross = (tp1_hit_px - entry) * contracts_part * QUANT_MULT + \
                            (close_px - entry) * contracts_remain * QUANT_MULT
                else:
                    gross = (entry - tp1_hit_px) * contracts_part * QUANT_MULT + \
                            (entry - close_px) * contracts_remain * QUANT_MULT
            else:
                # 全部按 close_px
                if side == "long":
                    gross = (close_px - entry) * contracts_orig * QUANT_MULT
                else:
                    gross = (entry - close_px) * contracts_orig * QUANT_MULT

            fee = V2_SLTB_RULES["taker_fee_usd"]
            net = gross - fee

            sig["exit_px"] = close_px
            sig["exit_ts"] = close_ts
            sig["outcome"] = outcome
            sig["status"] = "settled"
            sig["gross_usd"] = round(gross, 2)
            sig["net_usd"] = round(net, 2)
            sig["category"] = "profit" if net > 0 else ("breakeven" if abs(net) < 0.5 else "loss")
            sig["mae"] = 0  # 简化：不跟踪
            sig["mfe"] = 0
            sig["hold_h"] = (close_ts - ts_open) / 3600 if close_ts else 0
            total_net += net
            done += 1
            if verbose:
                print(f"  [v2 settle] {sig['id']}: {outcome} 净 {net:+.2f}U (entry={entry:.1f} exit={close_px:.1f})")
        else:
            pending_count += 1

    _write_journal(jrnl)
    return {"done": done, "pending": pending_count, "net_usd": round(total_net, 2)}


# ============================================================
# 统计：最近 N 单 / 全局 PnL
# ============================================================
def stats_v2_trades() -> dict:
    """训练 v2 虚拟持仓统计"""
    jrnl = _read_journal()
    settled = [j for j in jrnl if j.get("status") == "settled"]
    pending = [j for j in jrnl if j.get("status") == "pending"]
    blocked = [j for j in jrnl if j.get("outcome") == "DUPLICATE_BLOCKED"]

    total_net = sum(j.get("net_usd", 0) for j in settled)
    wins = sum(1 for j in settled if j.get("net_usd", 0) > 0)
    win_rate = f"{wins}/{len(settled)} = {wins/max(1,len(settled))*100:.1f}%"

    from collections import Counter
    outcomes = Counter(j.get("outcome") for j in settled)

    return {
        "total": len(jrnl),
        "settled": len(settled),
        "pending": len(pending),
        "duplicate_blocked": len(blocked),
        "total_net_usd": round(total_net, 2),
        "win_rate": win_rate,
        "outcomes": dict(outcomes),
    }


if __name__ == "__main__":
    print("=== v2_journal.py 测试 ===")
    stats = stats_v2_trades()
    for k, v in stats.items():
        print(f"  {k}: {v}")
    print(f"\n落盘位置: {V2_JOURNAL_PATH}")
