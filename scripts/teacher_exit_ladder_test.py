#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
teacher_exit_ladder_test.py —— 老师的【出场阶梯】套到我的现有 47 笔上

## 为什么做这个（这是本轮唯一还有落地价值的实验）
前面的实验全部证明：**老师的「选单」无法被机械规则复现**（0/54 组参数、
0 个可区分特征、比随机还差）。

但老师的**出场阶梯是可以复现的**，而且结构上明确不同：

| | 我的策略 | 老师 |
|---|---|---|
| TP1 幅度 | 0.35%（中位） | **0.57%** |
| TP1 减仓 | 33% | **50%** |
| TP1 后 | 保本 | 保本 |
| TP2 | 1.0% 左右 | 1.5% |
| SL 宽度 | 0.237%（固定） | 0.161%（结构位） |

**关键问题**：我的 TP1 腿毛利 +5.8U、手续费 4.85U → 净只剩 +1.0U（几乎白干）。
老师的 TP1 腿毛利 +14.3U、净 +9.4U。
如果只看 TP1 这一腿，**减仓比例 33%→50% + 幅度 0.35%→0.57%** 就能把
「白干」变成「真赚钱」—— 这是**不需要复刻选单**就能拿到的改进。

## 方法
同一批入场（我的 47 笔可信单），只换出场阶梯，A/B 对照：
  A = 我的现状（journal 实际 net）
  B = 老师阶梯（TP1 +0.57% 减 50% → 保本 → TP2 +1.5%）
  C = 老师阶梯但 SL 用我的原 SL（隔离 SL 宽度的贡献）
  D = 只把 TP1 减仓从 33% 改成 50%（幅度不变）
  E = 只把 TP1 幅度从 0.35% 改成 0.57%（减仓不变）

## 口径
- 入场：journal 实际 fill_price + fill_ts
- 出场：1m K 线重放，同根双触判 SL（保守，与 position_tracker 一致）
- 手续费：taker 4.85U/笔
- 幽灵成交：只取可信单（status=settled + net 非空 + fill_ts 存在）

## 只读
"""
from __future__ import annotations

import json
import math
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, "/root/skills/btc-gate-intraday-strategy/scripts")
sys.path.insert(0, "/root/skills/btc-gate-intraday-strategy/web")

import cost_basis_view as cbv
from strategy_truth import strategy_truth

CST = timezone(timedelta(hours=8))
FEE = 4.85
NOTIONAL = 5000.0
KL = "/root/data/BTC_USDT_klines.json"


def bars(tf):
    return json.load(open(KL))["BTC_USDT"][tf]["bars"]


def replay(bars1m, ts_list, entry, side, tp1_pct, tp2_pct, q1_pct, sl_pct,
           max_min=60 * 72):
    """sl_pct 传 None → 用该笔自己的真实 SL 宽度（entry['my_sl']）。"""
    """1m 重放：TP1 减 q1% → SL 移保本 → TP2 全平 / 打保本 / 打损。"""
    import bisect
    i = bisect.bisect_left(ts_list, ts_list[0])
    # 找入场位置
    j = bisect.bisect_right(ts_list, entry["fill_ts"]) - 1
    if j < 0:
        return None
    C = max(1, round(NOTIONAL / (entry["fill_px"] * 1e-4)))
    q1 = int(round(C * q1_pct / 100.0))
    sgn = 1.0 if side == "long" else -1.0
    tp1 = entry["fill_px"] * (1 + sgn * tp1_pct / 100.0)
    tp2 = entry["fill_px"] * (1 + sgn * tp2_pct / 100.0)
    if sl_pct is None:
        sl_pct = entry.get("my_sl") or 0.30
    sl = entry["fill_px"] * (1 - sgn * sl_pct / 100.0)
    gross = 0.0
    tp1_hit = False
    end = min(len(bars1m), j + 1 + max_min)
    for k in range(j + 1, end):
        b = bars1m[k]
        h, l = float(b[2]), float(b[3])
        if side == "long":
            t_sl, t_tp1, t_tp2 = l <= sl, (not tp1_hit and h >= tp1), h >= tp2
        else:
            t_sl, t_tp1, t_tp2 = h >= sl, (not tp1_hit and l <= tp1), l <= tp2
        if t_sl and (t_tp1 or t_tp2):
            gross += (C if not tp1_hit else C - q1) * (sl - entry["fill_px"]) * 1e-4 * sgn
            return gross - FEE
        if t_sl:
            gross += (C if not tp1_hit else C - q1) * (sl - entry["fill_px"]) * 1e-4 * sgn
            return gross - FEE
        if t_tp1:
            tp1_hit = True
            gross += q1 * (tp1 - entry["fill_px"]) * 1e-4 * sgn
            sl = entry["fill_px"]              # ★ 保本
        if t_tp2:
            gross += (C if not tp1_hit else C - q1) * (tp2 - entry["fill_px"]) * 1e-4 * sgn
            return gross - FEE
    k = end - 1
    px = float(bars1m[k][4])
    gross += (C if not tp1_hit else C - q1) * (px - entry["fill_px"]) * 1e-4 * sgn
    return gross - FEE


def main():
    b1m = bars("1m")
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
        my_sl = (abs(sl_px - float(px)) / float(px) * 100
                 if isinstance(sl_px, (int, float)) and px else None)
        recs.append({"id": r.get("id"), "side": r.get("side"),
                     "fill_ts": int(r["fill_ts"]), "fill_px": float(px),
                     "journal_net": float(r["net_usd"]),
                     "effective": cbv.effective_net(r),
                     "my_sl": my_sl})
    print(f"可信单 n={len(recs)}")

    # 我这批单实际的 TP1 幅度 / 减仓比例（从 journal 读）
    tp1_amp, q1_pct, sl_w, tp2_amp = [], [], [], []
    for r in t["records"]:
        if not (r.get("status") == "settled" and r.get("net_usd") is not None and r.get("fill_ts")):
            continue
        px = cbv.cost_basis_of(r)
        if px is None:
            continue
        side = r.get("side")
        sgn = 1.0 if side == "long" else -1.0
        # ★ 用真实价格字段算幅度（sl_pct / tp1_pct 字段是坏的，skill §1 ③）
        sl_px = r.get("sl")
        tp1_px = r.get("tp1")
        tp2_px = r.get("tp2")
        if isinstance(sl_px, (int, float)) and px:
            sl_w.append(abs(sl_px - px) / px * 100)
        if isinstance(tp1_px, (int, float)) and px:
            tp1_amp.append((tp1_px - px) / px * sgn * 100)
        if isinstance(tp2_px, (int, float)) and px:
            tp2_amp.append((tp2_px - px) / px * sgn * 100)
        # 减仓比例：从 contracts 与 partial_exit 推断（tp1 减仓后的剩余量）
        c0 = r.get("_orig_contracts") or r.get("contracts")
        pe = r.get("partial_exits")
        if isinstance(c0, (int, float)) and c0:
            rem = r.get("contracts")
            if isinstance(rem, (int, float)) and 0 < rem < c0:
                q1_pct.append((c0 - rem) / c0 * 100)
    def med(v):
        v = sorted(v)
        return v[len(v)//2] if v else None
    MY_TP1 = med(tp1_amp) or 0.48
    # ★ q1_pct 用 contracts 差值推算会把 EMA 预警的减仓也算进来 → 偏大
    #   实测（_orig_contracts 可比的 5 笔）TP1 减仓恒为 33.3% → 直接用计划值
    MY_Q1 = 33.3
    MY_SL = med(sl_w) or 0.30
    MY_TP2 = 1.0
    print(f"我这批单的实际口径（从 journal 反推中位数）: TP1={MY_TP1:.3f}% 减仓={MY_Q1:.1f}% "
          f"SL={MY_SL:.3f}% TP2={MY_TP2:.2f}%")
    print(f"  （样本: tp1 n={len(tp1_amp)} q1 n={len(q1_pct)} sl n={len(sl_w)}）\n")

    VARIANTS = [
        ("A 我的现状（journal 实际）", None),
        ("B 老师全阶梯 (TP1 .57/50%, TP2 1.5%, SL .161)",
         {"tp1": 0.57, "q1": 50, "tp2": 1.5, "sl": 0.161}),
        ("C 老师 TP 但我的原 SL", {"tp1": 0.57, "q1": 50, "tp2": 1.5, "sl": None}),
        ("D 只把减仓→50%", {"tp1": MY_TP1, "q1": 50, "tp2": MY_TP2, "sl": None}),
        ("E 只把 TP1 幅度→0.57%", {"tp1": 0.57, "q1": MY_Q1, "tp2": MY_TP2, "sl": None}),
        ("F 只把 SL 收窄→0.161%", {"tp1": MY_TP1, "q1": MY_Q1, "tp2": MY_TP2, "sl": 0.161}),
        ("G 老师 TP + 我的 TP2", {"tp1": 0.57, "q1": 50, "tp2": MY_TP2, "sl": None}),
        ("H 只把 TP2→1.5%", {"tp1": MY_TP1, "q1": MY_Q1, "tp2": 1.5, "sl": None}),
    ]

    results = {}
    for name, p in VARIANTS:
        nets = []
        for r in recs:
            if p is None:
                nets.append(r["effective"])
                continue
            v = replay(b1m, t1, r, r["side"], p["tp1"], p["tp2"], p["q1"], p["sl"])
            nets.append(v if v is not None else r["effective"])
        wins = [x for x in nets if x > 0]
        losses = [x for x in nets if x < 0]
        gp = sum(wins)
        gl = abs(sum(losses))
        results[name] = (sum(nets), len(wins) / len(nets) * 100,
                          (gp / len(wins)) / (gl / len(losses)) if wins and losses else 0,
                          min(nets), max(nets), nets)
        print(f"  {name:<44} n={len(nets):>3} 胜率{len(wins)/len(nets)*100:>5.1f}% "
              f"净{sum(nets):>+9.2f}U 盈亏比{(gp/len(wins))/(gl/len(losses)) if wins and losses else 0:>4.2f} "
              f"最差{min(nets):>+7.2f} 最好{max(nets):>+7.2f}")

    # ── TP1 单腿的经济学核算（老师的核心差别）──
    print("\n  ── TP1 单腿经济学（为什么减仓比例重要）──")
    for name, tp1p, q1p in (("我的现状 (TP1 0.48% / 减 33.3%)", 0.480, 33.3),
                            ("老师模式 (TP1 0.57% / 减 50%)", 0.570, 50.0)):
        # 用 5000 名义、650 张左右算
        c = 650
        gross = c * q1p / 100 * (tp1p / 100 * 10000) * 1e-4
        # 简化：毛利 = 张数×q1%×点数×1e-4，点数 = 价格×tp1%
        px = 80000
        pts = px * tp1p / 100
        gross2 = c * q1p / 100 * pts * 1e-4
        print(f"    {name:<34} TP1毛利 {gross2:>6.2f}U  手续费 4.85U  → 净 {gross2-4.85:>+7.2f}U")

    base = results["A 我的现状（journal 实际）"][0]
    print(f"\n  相对现状的 Δ：")
    for name, (net, wr, pf, w, b, _) in results.items():
        if name.startswith("A"):
            continue
        print(f"    {name:<44} {net-base:>+9.2f}U")

    # 逐笔：哪些单因为换阶梯变好/变差
    print("\n  ── 逐笔变化（老师阶梯 B vs 现状 A）──")
    a_nets = results["A 我的现状（journal 实际）"][5]
    b_nets = results["B 老师全阶梯 (TP1 .57/50%, TP2 1.5%, SL .161)"][5]
    diffs = [(b - a, r["id"], a, b) for a, b, r in zip(a_nets, b_nets, recs)]
    diffs.sort()
    print("    变差的 5 笔：")
    for d, rid, a, b in diffs[:5]:
        print(f"      {rid:<24} {a:>+8.2f} → {b:>+8.2f}  ({d:+.2f})")
    print("    变好的 5 笔：")
    for d, rid, a, b in diffs[-5:][::-1]:
        print(f"      {rid:<24} {a:>+8.2f} → {b:>+8.2f}  ({d:+.2f})")
    up = sum(1 for d, _, _, _ in diffs if d > 0)
    down = sum(1 for d, _, _, _ in diffs if d < 0)
    same = len(diffs) - up - down
    print(f"    变好 {up} 笔 / 变差 {down} 笔 / 不变 {same} 笔")
    print(f"    变好笔合计 {sum(d for d,_,_,_ in diffs if d>0):+.2f}U  "
          f"变差笔合计 {sum(d for d,_,_,_ in diffs if d<0):+.2f}U  净 {sum(d for d,_,_,_ in diffs):+.2f}U")

    # 配对检验（同一批单，只换出场阶梯 → 配对 t 检验 + 符号检验）
    import statistics, random
    ds = [d for d, _, _, _ in diffs]
    n = len(ds)
    mean_d = sum(ds) / n
    sd = statistics.pstdev(ds) * (n / (n - 1)) ** 0.5 if n > 1 else 0
    t = mean_d / (sd / n ** 0.5) if sd > 0 else 0
    # 符号检验（对不服从正态的情况更稳）
    pos = sum(1 for d in ds if d > 0)
    neg = sum(1 for d in ds if d < 0)
    nn = pos + neg
    p_sign = 1.0
    if nn > 0:
        k = min(pos, neg)
        # 双尾二项检验
        p_sign = sum(math.comb(nn, i) for i in range(k + 1)) / (2 ** nn) * 2
    print(f"    配对 t 检验: 均值差 {mean_d:+.2f}U  t={t:+.2f}（n={n}）")
    print(f"    符号检验  : 变好{pos} / 变差{neg}  p={min(p_sign,1.0):.3f}"
          f"{'  → 显著' if p_sign < 0.05 else '  → 不显著'}")


if __name__ == "__main__":
    main()
