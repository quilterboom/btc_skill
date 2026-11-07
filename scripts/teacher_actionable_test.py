#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
teacher_actionable_test.py —— 老师模式里【唯一两个可机械复现】的维度，套到我的 47 笔上

## 前面所有实验的结论
- 老师的**选单**不可复刻（0/54 组参数、0 个可区分特征、比随机还差）
- 老师唯一在长历史上还有效的是**执行方式**（限价等成交不追价，+79U 已单独验证）

## 本脚本测剩下两个候选（都来自老师的分布，不是从 PnL 拟合）
1. **时段**：老师 65% 的入场在 20:00~01:59；区分度检验里 is_night 是
   最强信号（AUC 0.621，p=0.066，**前半 0.622 / 后半 0.639 稳定**）
2. **结构 SL**：老师 SL 均 0.161%（结构位下方），我的是固定 0.452%

两者都可机械执行，且不需要复刻选单 → 这是「更接近老师模式」唯一有希望的路径。

## 方法
同一批 47 笔入场（journal 实际），只改过滤/出场条件，A/B 对照 + 配对符号检验。

## 只读
"""
from __future__ import annotations

import json
import math
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, "/root/skills/btc-gate-intraday-strategy/web")

import cost_basis_view as cbv
from strategy_truth import strategy_truth

CST = timezone(timedelta(hours=8))
FEE = 4.85
NOTIONAL = 5000.0
KL = "/root/data/BTC_USDT_klines.json"


def load(tf):
    return json.load(open(KL))["BTC_USDT"][tf]["bars"]


def replay(b1m, t1, entry, tp1_pct, tp2_pct, q1_pct, sl_pct, max_min=60 * 72):
    import bisect
    j = bisect.bisect_right(t1, entry["fill_ts"]) - 1
    if j < 0:
        return None
    C = max(1, round(NOTIONAL / (entry["fill_px"] * 1e-4)))
    q1 = int(round(C * q1_pct / 100.0))
    sgn = 1.0 if entry["side"] == "long" else -1.0
    tp1 = entry["fill_px"] * (1 + sgn * tp1_pct / 100.0)
    tp2 = entry["fill_px"] * (1 + sgn * tp2_pct / 100.0)
    sl = entry["fill_px"] * (1 - sgn * sl_pct / 100.0)
    gross, tp1_hit = 0.0, False
    end = min(len(b1m), j + 1 + max_min)
    for k in range(j + 1, end):
        b = b1m[k]
        h, l = float(b[2]), float(b[3])
        if entry["side"] == "long":
            t_sl, t_tp1, t_tp2 = l <= sl, (not tp1_hit and h >= tp1), h >= tp2
        else:
            t_sl, t_tp1, t_tp2 = h >= sl, (not tp1_hit and l <= tp1), l <= tp2
        if t_sl:
            gross += (C if not tp1_hit else C - q1) * (sl - entry["fill_px"]) * 1e-4 * sgn
            return gross - FEE
        if t_tp1:
            tp1_hit = True
            gross += q1 * (tp1 - entry["fill_px"]) * 1e-4 * sgn
            sl = entry["fill_px"]
        if t_tp2:
            gross += (C if not tp1_hit else C - q1) * (tp2 - entry["fill_px"]) * 1e-4 * sgn
            return gross - FEE
    k = end - 1
    gross += (C if not tp1_hit else C - q1) * (float(b1m[k][4]) - entry["fill_px"]) * 1e-4 * sgn
    return gross - FEE


def sign_test(ds):
    pos = sum(1 for d in ds if d > 0)
    neg = sum(1 for d in ds if d < 0)
    n = pos + neg
    if not n:
        return pos, neg, 1.0
    k = min(pos, neg)
    p = sum(math.comb(n, i) for i in range(k + 1)) / (2 ** n) * 2
    return pos, neg, min(p, 1.0)


def main():
    b1m = load("1m")
    t1 = [b[0] for b in b1m]

    t = strategy_truth("/root/data/journal.jsonl")
    recs = []
    for r in t["records"]:
        if not (r.get("status") == "settled" and r.get("net_usd") is not None and r.get("fill_ts")):
            continue
        px = cbv.cost_basis_of(r)
        if px is None:
            continue
        sgn = 1.0 if r.get("side") == "long" else -1.0
        sl_px = r.get("sl")
        my_sl = abs(sl_px - float(px)) / float(px) * 100 if isinstance(sl_px, (int, float)) and px else None
        recs.append({"id": r.get("id"), "side": r.get("side"),
                     "fill_ts": int(r["fill_ts"]), "fill_px": float(px),
                     "effective": cbv.effective_net(r), "my_sl": my_sl,
                     "hour": datetime.fromtimestamp(int(r["fill_ts"]), CST).hour})

    print(f"可信单 n={len(recs)}")
    # 时段分布（我的单）
    from collections import Counter
    hc = Counter(r["hour"] for r in recs)
    night = sum(v for h, v in hc.items() if h >= 20 or h <= 1)
    print(f"我的单时段：20:00~01:59 占 {night}/{len(recs)} = {night/len(recs)*100:.0f}%  "
          f"（老师 65%）")
    print(f"  按小时：{dict(sorted(hc.items()))}\n")

    MY_TP1, MY_Q1, MY_TP2 = 0.480, 33.3, 1.020

    # ── 1. 时段过滤：只做 20:00~01:59 的单 ──
    print("  ── 1. 时段过滤（老师 65% 在 20:00~01:59）──")
    for label, keep in (("全部时段", lambda r: True),
                        ("只做 20:00~01:59", lambda r: r["hour"] >= 20 or r["hour"] <= 1),
                        ("只做 21:00~23:59", lambda r: 21 <= r["hour"] <= 23)):
        sel = [r for r in recs if keep(r)]
        if not sel:
            continue
        nets = [r["effective"] for r in sel]
        wins = [x for x in nets if x > 0]
        losses = [x for x in nets if x < 0]
        pf = (sum(wins)/len(wins))/(abs(sum(losses))/len(losses)) if wins and losses else 0
        print(f"    {label:<20} n={len(sel):>3} 胜率{len(wins)/len(sel)*100:>5.1f}% "
              f"净{sum(nets):>+9.2f}U 每笔{sum(nets)/len(sel):>+7.2f}U 盈亏比{pf:>4.2f}")

    # ── 2. 时段过滤 + 老师出场阶梯 ──
    print("\n  ── 2. 时段过滤 + 老师出场阶梯（TP1 .57/50%, SL .161, TP2 不动）──")
    all_base = [r["effective"] for r in recs]
    night_r = [r for r in recs if r["hour"] >= 20 or r["hour"] <= 1]
    for label, sel in (("全部", recs), ("夜间", night_r)):
        if not sel:
            continue
        b = [r["effective"] for r in sel]
        m = [replay(b1m, t1, r, 0.57, MY_TP2, 50.0, 0.161) or r["effective"] for r in sel]
        print(f"    {label:<6} n={len(sel):>3} 现状净{sum(b):>+9.2f}U → 老师阶梯{sum(m):>+9.2f}U "
              f"(Δ {sum(m)-sum(b):>+8.2f}U)")
        ds = [x - y for x, y in zip(m, b)]
        pos, neg, p = sign_test(ds)
        print(f"           变好{pos} 变差{neg} 不变{len(ds)-pos-neg}  符号检验 p={p:.3f}"
              f"{'  → 显著' if p < 0.05 else '  → 不显著'}")

    # ── 3. 只改 SL 宽度（老师 0.161% vs 我 0.452%）──
    print("\n  ── 3. 只改 SL 宽度 ──")
    for slp in (0.161, 0.25, 0.30, 0.452):
        m = [replay(b1m, t1, r, MY_TP1, MY_TP2, MY_Q1, slp) or r["effective"] for r in recs]
        ds = [x - y for x, y in zip(m, all_base)]
        pos, neg, p = sign_test(ds)
        print(f"    SL{slp:<6} 净{sum(m):>+9.2f}U (Δ{sum(m)-sum(all_base):>+8.2f}U)  "
              f"变好{pos} 变差{neg} p={p:.3f}")

    # ── 4. 老师阶梯 + 夜间，只看净利为正的单的构成 ──
    print("\n  ── 4. 老师阶梯的收益来源（全部 47 笔）──")
    m = [replay(b1m, t1, r, 0.57, MY_TP2, 50.0, 0.161) or r["effective"] for r in recs]
    ds = [(x - y, r["id"], r["hour"]) for x, y, r in zip(m, all_base, recs)]
    ds.sort()
    print("    变好 3 笔：")
    for d, rid, h in ds[-3:][::-1]:
        print(f"      {rid[:26]:<28} {h:>2}点  Δ{d:>+8.2f}U")
    print("    变差 3 笔：")
    for d, rid, h in ds[:3]:
        print(f"      {rid[:26]:<28} {h:>2}点  Δ{d:>+8.2f}U")
    n_pos = sum(1 for d, _, _ in ds if d > 0)
    print(f"    净 Δ = {sum(d for d,_,_ in ds):+.2f}U，其中变好笔占 {n_pos}/{len(ds)}")

    # ── 5. 老师阶梯在【夜间 vs 白天】分别的效果 ──
    print("\n  ── 5. 老师阶梯分段效果 ──")
    for label, pred in (("夜间(20-01)", lambda r: r["hour"] >= 20 or r["hour"] <= 1),
                         ("白天", lambda r: not (r["hour"] >= 20 or r["hour"] <= 1))):
        sel = [r for r in recs if pred(r)]
        if not sel:
            continue
        b = [r["effective"] for r in sel]
        m2 = [replay(b1m, t1, r, 0.57, MY_TP2, 50.0, 0.161) or r["effective"] for r in sel]
        print(f"    {label:<14} n={len(sel):>3} 现状{sum(b):>+9.2f}U → 老师阶梯{sum(m2):>+9.2f}U "
              f"(Δ{sum(m2)-sum(b):>+8.2f}U)")


if __name__ == "__main__":
    main()
