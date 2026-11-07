#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
backtest_hammer_limit_entry.py —— hammer 入场执行方式对照回测（2026-11-07）

## 背景：hammer 是 stop-market，追价

hammer 信号的执行链路（`watch/hammer_watcher.py:_process_one`）：

    触发线 = hammer_L × 1.003（结构位：锤子最低点上方 0.3%）
    watcher 每秒 poll 一次 ticker
    ticker ≥ 触发线 → **市价买进**，fill_px = round(ticker_px, 1)

这是 **stop-market**（触发后市价单）：触发后无条件成交，不限价。
BTC 在 76000 价位 1 秒能跑 100~400 点（0.13%~0.5%），
所以成交价常远高于触发线 —— 实测追价单平均追高 **+0.315%**（最大 +0.508%），
把每笔实际风险从计划的 0.237% 放大到 **0.551%（2.6 倍）**。

老师那边是反过来的：**挂单等成交**（5m 暴跌巨量 → 缩量反弹段挂单 → 等回抽）。

## ★ 关键区分：「要不要成交」和「成交在什么价」是两个独立问题

同一个触发时刻（价格上穿触发线），可以对比不同的成交机制：

| 变体 | 机制 | 成交价 | 成交集合 |
|---|---|---|---|
| **A 现状** | stop-**market** | 实际 ticker（含追价滑点） | 与现实一致 |
| **B 零滑点** | stop-market，假设零滑点 | 恰好 = 触发线 | **同 A**（严格可比） |
| **C stop-limit** | 触发后挂限价单，限价 = 触发线×(1+cap) | min(实际, 上限) | **同 A** |

B/C 不改变「哪些单会成交」，只改成交价 → 与 A 严格同样本，可直接比执行质量。
**B−A 就是「追价滑点」这一项单独的代价。**

## D 变体是**策略变更**，不是执行优化（必须单列）

| 变体 | 机制 | 成交价 | 成交集合 |
|---|---|---|---|
| **D 纯限价 @ 计划价** | 信号发出即挂 BUY LIMIT @ hammer_H | ≤ hammer_H | **不同**（可能早于触发线成交 / 不成交） |

D 的成交时刻**早于**触发线（价格还没确认反弹 0.3% 就进了），
入场价更低、样本也不同。它测的是「要不要用另一种入场逻辑」，不是「追价要不要修」，
所以**不能和 A/B/C 混在一个表里比**，单列。

## 出场阶梯（所有变体统一，从各自成交价起算）

    SL  = hammer_L × 0.999       （结构位，不随入场价漂移）
    TP1 = 成交价 × 1.013          （生产 custom_tp1_pct）→ 平 1/3，SL 推保本
    TP2 = 成交价 × 1.029          （生产 custom_tp2_pct）→ 剩余全平
    fee = 4.85U/笔（生产 FEE_USD）
    张数 = 5000 / (成交价 × 0.0001)（canonical_contracts，不除杠杆）

## 样本分类（重要）

账本里的 hammer 记录混了 4 类，只有第 1 类是「真实发生过的成交」：

| 类别 | 判据 | 处理 |
|---|---|---|
| **REAL** | `_fill_method == market_on_watcher` 且非幽灵成交 | 进 A/B/C 对照 |
| GHOST | 幽灵成交（fill_ts < promoted_ts，从未真正发生） | **排除**（另列） |
| EXPIRED | `HAMMER_EXPIRED`（窗口内没到触发线） | 无成交，不进对照 |
| MISSED | `outcome == MISSED` | 无成交，不进对照 |

## 用法

    cd /root/skills/btc-gate-intraday-strategy/scripts
    /root/.hermes/hermes-agent/venv/bin/python3 backtest_hammer_limit_entry.py
    /root/.hermes/hermes-agent/venv/bin/python3 backtest_hammer_limit_entry.py \
        --journal /root/data/training/journal.jsonl --tf 5m

## 只读，不改任何账本
"""
from __future__ import annotations

import argparse
import bisect
import json
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import cost_basis_view as cbv

CST = timezone(timedelta(hours=8))
FEE = 4.85
QUANT = 0.0001
TP1_PCT = 0.013          # 生产 custom_tp1_pct
TP2_PCT = 0.029          # 生产 custom_tp2_pct
SL_OFF_HAMMER_L = 0.001  # 生产：SL = hammer_L × (1 - 0.001)
PARTS = 3                # 生产 position_tracker.PARTS（TP1 平 1/3）
KLINE_CACHE = "/root/data/BTC_USDT_klines.json"
DEFAULT_WAIT_HOURS = 12
DEFAULT_TIMEOUT_H = 48
# ★ 上限百分比用**分数**（0.0005 = 0.05%）。
#   真实追价幅度只有 0.013%~0.414% —— 上限设成 5%/10% 永远不会 bind，
#   C 变体会跟 A 一模一样（实测踩过这个坑：cap=5% 时 Δ 恒为 0）。
DEFAULT_CAP_PCTS = [0.0005, 0.001, 0.002, 0.003, 0.005]


# ---------------------------------------------------------------- 数据装载

def load_bars(tf: str) -> list:
    with open(KLINE_CACHE, "r", encoding="utf-8") as f:
        d = json.load(f)
    return sorted(d["BTC_USDT"][tf]["bars"], key=lambda b: b[0])


def load_hammer_signals(journal_path: str) -> list:
    out = []
    with open(journal_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            r = json.loads(line)
            meta = r.get("meta") or {}
            if not meta.get("hammer_managed"):
                continue
            hl, hh = meta.get("hammer_L"), meta.get("hammer_H")
            if not hl or not hh:
                continue
            ghost = cbv.is_ghost_fill(r)
            filled = r.get("_fill_method") == "market_on_watcher" and not ghost
            if ghost:
                cls = "GHOST"
            elif filled:
                cls = "REAL"
            elif r.get("outcome") == "MISSED":
                cls = "MISSED"
            else:
                cls = "EXPIRED"
            out.append({
                "id": r.get("id"), "ts": int(r.get("ts") or 0),
                "side": r.get("side") or "long",
                "hammer_L": float(hl), "hammer_H": float(hh),
                "plan_entry": float(r.get("entry") or 0) or float(hh),
                "actual_fill": r.get("fill_px"),
                "fill_method": r.get("_fill_method"),
                "journal_net": r.get("net_usd"), "outcome": r.get("outcome"),
                "cls": cls,
            })
    return sorted(out, key=lambda s: s["ts"])


# ---------------------------------------------------------------- 出场模拟

def simulate_exit(bars, idx_from, ts_from, entry_px, sl_px, side, timeout_h):
    """统一出场阶梯：SL / TP1(平1/3 + 推保本) / TP2(全平) / 超时估值。

    ★ 2026-11-07 修 **成交价已在止损价之外**的建模 bug：
      若成交价本身已经穿过 SL（限价单在暴跌里成交到止损位下方），
      旧代码会走「SL 触发 → 按 sl_px 出场」，而 sl_px > entry_px（多头），
      于是 gross = (sl_px - entry_px) × 张数 = **正数** → 凭空造出盈利。
      实测把 D 变体从 -31.65U 抬成 +270.47U（全是假账）。
      正确语义：成交即已越过止损 → 立刻在成交价出场（毛利≈0，只剩手续费）。
    """
    s = 1.0 if side == "long" else -1.0
    # ★ 成交价已越过止损位 → 立即出场（不等到 SL 触发）
    crossed = (entry_px <= sl_px) if s > 0 else (entry_px >= sl_px)
    if crossed:
        return -FEE, "SL_AT_FILL"

    contracts = cbv.canonical_contracts(entry_px)
    part = contracts / PARTS
    tp1 = entry_px * (1 + s * TP1_PCT)
    tp2 = entry_px * (1 + s * TP2_PCT)
    end_ts = ts_from + timeout_h * 3600

    legs, be, last_px = [], False, entry_px
    label = "TIMEOUT"
    i, n = idx_from, len(bars)
    while i < n:
        b = bars[i]
        if b[0] > end_ts:
            break
        _, o, h, l, c = b[0], float(b[1]), float(b[2]), float(b[3]), float(b[4])
        last_px = c
        if s > 0:
            sl_t, tp1_t, tp2_t, be_t = l <= sl_px, h >= tp1, h >= tp2, l <= entry_px
        else:
            sl_t, tp1_t, tp2_t, be_t = h >= sl_px, l <= tp1, l <= tp2, h >= entry_px
        if not be:
            if sl_t:
                legs.append((contracts, sl_px)); label = "SL"; break
            if tp1_t:
                legs.append((part, tp1)); be = True; i += 1; continue
            if tp2_t:
                legs.append((contracts, tp2)); label = "TP2"; break
        else:
            if tp2_t:
                legs.append((contracts - part, tp2)); label = "TP2_AFTER_TP1"; break
            if be_t:
                legs.append((contracts - part, entry_px)); label = "BE"; break
        i += 1
    if not legs:
        legs.append((contracts, last_px))
    gross = sum(q * QUANT * (px - entry_px) * s for q, px in legs)
    return gross - FEE, label


def find_limit_fill(bars, tss, idx_from, limit_px, side, wait_h, market_px):
    """**限价单的正确成交语义**。

    BUY LIMIT @ P：
      · 挂单时市价 ≤ P → 立刻成交（按市价，比限价更优）
      · 挂单时市价 > P → 挂单等待，价格**跌回** P 才成交（成交价 = P 或更优）

    SELL LIMIT @ P：完全对称（挂单时市价 ≥ P 立刻成交，否则涨回 P 才成交）。

    ⚠ 我第一版写成了「价格上穿限价才成交」（`h >= limit_px`）——
      那是 **STOP** 的语义，不是 LIMIT。限价买单是等价格**跌回来**。
      用错方向的实测后果：变体 D 变成「信号一发出就永远立刻成交」，
      17/17 成交率全是假的。

    ⚠ market_px 必须是**挂单那一刻**的市价，不能用信号 bar 的 close
      （close 是 bar 结束时的价格 = 未来信息）。

    返回 (索引, 成交价) 或 None（等待期内没到 → 未成交）。
    """
    s = 1.0 if side == "long" else -1.0
    # 挂单时市价已经优于限价 → 立刻成交
    if market_px is not None:
        if s > 0 and market_px <= limit_px:
            return idx_from, market_px
        if s < 0 and market_px >= limit_px:
            return idx_from, market_px

    end_ts = tss[idx_from] + wait_h * 3600
    for i in range(idx_from, len(bars)):
        b = bars[i]
        if b[0] > end_ts:
            return None
        o, h, l = float(b[1]), float(b[2]), float(b[3])
        if s > 0:
            # 买入限价：价格跌回限价 → 成交
            if l <= limit_px:
                fill = min(limit_px, o) if o <= limit_px else limit_px
                return i, fill
        else:
            # 卖出限价：价格涨回限价 → 成交
            if h >= limit_px:
                fill = max(limit_px, o) if o >= limit_px else limit_px
                return i, fill
    return None


# ---------------------------------------------------------------- 主流程

def run(journal_path, tf, wait_h, timeout_h, cap_pcts):
    bars = load_bars(tf)
    tss = [b[0] for b in bars]
    rows = []
    for s in load_hammer_signals(journal_path):
        idx = max(0, bisect.bisect_right(tss, s["ts"]) - 1)
        sl_px = s["hammer_L"] * (1 - SL_OFF_HAMMER_L)
        trigger = s["hammer_L"] * 1.003
        row = {"id": s["id"], "ts": s["ts"], "side": s["side"], "cls": s["cls"],
               "trigger": round(trigger, 1), "plan": round(s["plan_entry"], 1),
               "actual_fill": s["actual_fill"], "journal_net": s["journal_net"],
               "outcome": s["outcome"]}

        if s["cls"] == "REAL":
            # ── A 现状：stop-market，实际 fill_px（含滑点） ──
            ep = float(s["actual_fill"])
            net, lab = simulate_exit(bars, idx, s["ts"], ep, sl_px, s["side"], timeout_h)
            row["A_net"], row["A_label"], row["A_entry"] = round(net, 2), lab, ep

            # ── B 零滑点：成交价 = 触发线（同样本，严格可比） ──
            net, lab = simulate_exit(bars, idx, s["ts"], trigger, sl_px, s["side"], timeout_h)
            row["B_net"], row["B_label"], row["B_entry"] = round(net, 2), lab, trigger

            # ── C stop-limit：成交价 = min(实际, 触发线×(1+cap)) ──
            #   stop-limit 的真实语义：**市价超过上限 → 挂单挂在上限价等回踩**。
            #   回踩到上限价才成交；等不到（窗口内没回踩）→ 这笔不成交（0U）。
            #   这正是 stop-limit 与 stop-market 的本质区别：
            #   市价追单永远成交（但追高），限价单可能不成交（但不追高）。
            row["C"] = {}
            for cap in cap_pcts:
                cap_px = trigger * (1 + cap)
                ep_ = float(s["actual_fill"])
                if ep_ <= cap_px:
                    # 市价没超上限 → 直接按市价成交（同 stop-market）
                    fill_px = ep_
                    iC = idx
                    tsC = s["ts"]
                else:
                    # 市价超上限 → 挂在 cap_px 等回踩
                    # ★ 从 idx+1 开始扫：信号在 idx 这根 bar 收盘时发出，
                    #   挂单最早只能在**下一根**之后成交。用 idx 会把
                    #   信号 bar 自己的 high/low 当成未来（look-ahead）。
                    got = find_limit_fill(bars, tss, idx + 1, cap_px, s["side"], wait_h, None)
                    if not got:
                        row["C"][cap] = {"net": 0.0, "label": "未成交", "entry": None}
                        continue
                    iC, fill_px = got
                    tsC = tss[iC]
                net, lab = simulate_exit(bars, iC, tsC, fill_px, sl_px, s["side"], timeout_h)
                row["C"][cap] = {"net": round(net, 2), "label": lab, "entry": round(fill_px, 1)}
        else:
            row["A_net"] = row["B_net"] = None
            row["A_label"] = row["B_label"] = s["cls"]
            row["A_entry"] = row["B_entry"] = None
            row["C"] = {}

        # ── D 纯限价 @ 计划价（策略变更，单列） ──
        # ★ 从 idx+1 开始扫：信号在 idx bar 收盘时发出，挂单最早下一根才能成交。
        #   用 idx 会 look-ahead——hammer_H 本身就是信号 bar 的 high，
        #   信号 bar 的 high >= limit_px 恒成立 → 永远「立刻成交」，是假账。
        # ★ market_px 用信号 bar 的 **open**（bar 开始价 ≈ 挂单时刻市价），
        #   不用 close（bar 结束价 = 未来信息）。
        mkt_px = float(bars[idx][1]) if idx < len(bars) else None
        fillD = find_limit_fill(bars, tss, idx + 1, s["plan_entry"], s["side"], wait_h, mkt_px)
        if fillD:
            iD, pxD = fillD
            net, lab = simulate_exit(bars, iD, tss[iD], pxD, sl_px, s["side"], timeout_h)
            row["D_net"], row["D_label"], row["D_entry"] = round(net, 2), lab, round(pxD, 1)
        else:
            row["D_net"], row["D_label"], row["D_entry"] = 0.0, "未成交", None
        rows.append(row)
    return rows


def summarize(nets, label, total_n):
    vals = [n for n in nets if n is not None]
    won = [n for n in vals if n > 0]
    lost = [n for n in vals if n < 0]
    gw, gl = sum(won), sum(lost)
    print(f"  {label:<34} 成交 {len(vals):>2}/{total_n:>2} 胜 {len(won):>2} 负 {len(lost):>2} "
          f"净 {sum(vals):+9.2f}U  均 {sum(vals)/len(vals) if vals else 0:+7.2f}U "
          f"盈亏比 {gw/abs(gl) if gl else 0:.2f}")
    return sum(vals), len(vals)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--journal", default="/root/data/journal.jsonl")
    ap.add_argument("--tf", default="15m", choices=["1m", "5m", "15m", "1h"])
    ap.add_argument("--wait-hours", type=int, default=DEFAULT_WAIT_HOURS)
    ap.add_argument("--timeout-hours", type=int, default=DEFAULT_TIMEOUT_H)
    ap.add_argument("--cap-pcts", default=",".join(str(c) for c in DEFAULT_CAP_PCTS))
    args = ap.parse_args()
    cap_pcts = [float(x) for x in args.cap_pcts.split(",") if x.strip()]

    rows = run(args.journal, args.tf, args.wait_hours, args.timeout_hours, cap_pcts)
    reals = [r for r in rows if r["cls"] == "REAL"]

    print("=" * 122)
    print(f"hammer 入场执行方式对照回测  |  {args.journal}  |  K线 {args.tf}  "
          f"|  限价等待 {args.wait_hours}h  |  持仓超时 {args.timeout_hours}h")
    print(f"hammer 信号 {len(rows)} 个：真实成交 {len(reals)} / "
          f"幽灵 {sum(1 for r in rows if r['cls']=='GHOST')} / "
          f"过期 {sum(1 for r in rows if r['cls']=='EXPIRED')} / "
          f"MISSED {sum(1 for r in rows if r['cls']=='MISSED')}")
    print("=" * 122)

    print(f"\n【真实成交的 {len(reals)} 笔 · 同样本对照】")
    print(f"{'信号时间':<16} {'触发线':>10} {'实际成交':>10} {'追价%':>8} | "
          f"{'A现状':>9} {'B零滑点':>9} | {'账本实际':>9}  结局A/B")
    for r in reals:
        t = datetime.fromtimestamp(r["ts"], CST).strftime("%m-%d %H:%M")
        chase = f"{(float(r['actual_fill'])-r['trigger'])/r['trigger']*100:+.3f}%"
        jn = f"{r['journal_net']:+.2f}" if r["journal_net"] is not None else "  -   "
        print(f"{t:<16} {r['trigger']:>10.1f} {str(r['actual_fill']):>10} {chase:>8} | "
              f"{r['A_net']:>+9.2f} {r['B_net']:>+9.2f} | {jn:>9}  {r['A_label']}/{r['B_label']}")

    print(f"\n{'变体':<36} {'成交':>6} {'胜':>3} {'负':>3} {'净利':>11} {'均笔':>9} {'盈亏比':>7}")
    print("-" * 122)
    tA, nA = summarize([r["A_net"] for r in reals], "A 现状 stop-market（市价追）", len(reals))
    tB, nB = summarize([r["B_net"] for r in reals], "B 零滑点（成交=触发线）", len(reals))
    for cap in cap_pcts:
        tC, nC = summarize([r["C"][cap]["net"] for r in reals],
                            f"C stop-limit 上限 +{cap*100:.2f}%", len(reals))
        nfill = sum(1 for r in reals if r["C"][cap]["entry"] is not None)
        print(f"       → 相对 A: {tC-tA:+.2f}U   (成交 {nfill}/{len(reals)})")
    print("-" * 122)
    print(f"  ★ 追价滑点单独代价 = A − B = {tA-tB:+.2f}U   （同样本 {nA} 笔，只改成交价）")
    print(f"  ★ 理论上限（B）：追价全消除可回收 {tB-tA:+.2f}U")

    # C 变体的机制说明：cap 收紧 → 更多单「市价超上限」→ 挂在上限价等回踩
    # → 成交时刻变晚（不是纯价格改善）→ 数字非单调。必须讲清楚。
    print(f"\n  【C stop-limit 的机制】上限越紧，越多单市价超上限 → 挂在上限价等回踩：")
    for cap in cap_pcts:
        over = sum(1 for r in reals
                   if r["actual_fill"] and float(r["actual_fill"]) > r["trigger"] * (1 + cap))
        print(f"    上限 +{cap*100:.2f}%: 市价超上限 {over}/{len(reals)} 笔 → "
              f"这 {over} 笔的成交时刻变晚（不是纯价格改善）")

    # D 单列
    d_vals = [r["D_net"] for r in rows]
    d_filled = [r for r in rows if r["D_entry"] is not None]
    print(f"\n【D 纯限价 @计划价 —— 策略变更，单列】")
    print(f"  信号 {len(rows)} 个 → 成交 {len(d_filled)} 个（{len(d_filled)/len(rows)*100:.0f}%）"
          f"  未成交 {len(rows)-len(d_filled)} 个")
    tD, nD = summarize(d_vals, "D 纯限价 @ 计划价（hammer_H）", len(rows))
    # 同样本（只算 REAL 那 9 笔）
    d_same = [r["D_net"] for r in reals]
    tD_same = sum(d_same)
    print(f"  同样本（只算 REAL {len(reals)} 笔）: A {tA:+.2f}U  vs  D {tD_same:+.2f}U  "
          f"（Δ {tD_same-tA:+.2f}U）")
    print(f"  ⚠ D 的成交时刻早于触发线（未等反弹确认 0.3%），入场逻辑不同，不能与 A/B/C 混比")
    print()
    return rows


if __name__ == "__main__":
    main()
