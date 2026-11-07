#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
teacher_discrimination.py —— 决定性检验：老师的真实入场，能被任何可计算的区分度找出来吗？

## 问题
liusir 的目标：「以老师交易的单去做量化交易，更加接近老师的交易模式」。

前面的复刻全部失败（54 组参数 0 通过，比随机还差）。
但还有一个没排除的可能：**规则本身没错，错在我用的特征不够。**
如果存在某个可计算特征，能把「老师真做的 20 笔」和「规则在同一时期产生的信号」分开，
那说明老师的模式里还有我没提取出来的判读维度 —— 那才是值得继续挖的东西。

## 方法（分类特征的区分度检验）
把老师的 20 笔（正样本）和规则在同一时期的信号（负样本）放一起，
对每个候选特征算 **AUC**（区分度）：
    AUC = 0.5  → 完全无区分度
    AUC = 1.0  → 完美区分
判据：AUC ≥ 0.7 且样本外（前半/后半）都保持方向 → 存在可提取的判读维度。

同时做**置换检验**：随机打乱标签 1000 次，看真实 AUC 排在随机分布的哪个百分位。
p < 0.05 才是真信号（否则就是过拟合出来的假区分度）。

## 只读
"""
from __future__ import annotations

import json
import os
import random
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, "/root/skills/btc-gate-intraday-strategy/web")

import teacher_analysis as ta
from teacher_replica import CFG, SigGen, gen_signals, load_bars

CST = timezone(timedelta(hours=8))


def num(v):
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    try:
        return float(str(v).strip().replace(",", "").split("-")[0].split("~")[0])
    except (TypeError, ValueError):
        return None


def feats(sg, ts, px, side, bars):
    """入场时刻的全部候选特征（严格只用 ts 前最后一根已收 bar）。"""
    i = None
    for k, b in enumerate(sg.bars):
        if int(b[0]) >= ts:
            i = k
            break
    if i is None:
        i = len(sg.bars) - 1
    i = max(0, i - 1)
    if i < 60:
        return {}
    f = {}
    c = sg.close[i]
    e20, e50 = sg.e20[i], sg.e50[i]
    if e20 and e50:
        f["struct_up"] = 1 if e20 > e50 * 1.0005 else 0
        f["px_vs_ema20%"] = round((px - e20) / e20 * 100, 3)
        f["px_vs_ema50%"] = round((px - e50) / e50 * 100, 3)
    for hrs in (1, 3, 6, 12, 24, 48):
        look = max(1, int(hrs * 60 / sg.tf))
        if i < look:
            continue
        f[f"drop_{hrs}h%"] = round((sg.close[i - look] - c) / sg.close[i - look] * 100, 3)
        f[f"rise_{hrs}h%"] = -f[f"drop_{hrs}h%"]
    vx = sg.vol_x(i)
    if vx is not None:
        f["vol_x"] = vx
    for hrs in (12, 24):
        look = max(2, int(hrs * 60 / sg.tf))
        if i < look:
            continue
        lo = min(sg.low[j] for j in range(i - look + 1, i + 1))
        hi = max(sg.high[j] for j in range(i - look + 1, i + 1))
        f[f"dist_low_{hrs}h%"] = round((px - lo) / lo * 100, 3)
        f[f"pos_{hrs}h%"] = round((px - lo) / (hi - lo) * 100, 1) if hi > lo else None
    if sg.atr[i]:
        f["atr%"] = round(sg.atr[i] / c * 100, 4)
    # 最近 N 根 bar 的形态统计
    for n in (8, 24):
        if i < n:
            continue
        ups = sum(1 for j in range(i - n + 1, i + 1)
                   if len(sg.bars[j]) > 1 and sg.close[j] > float(sg.bars[j][1]))
        f[f"up_ratio_{n}"] = ups / n
        f[f"range_{n}avg%"] = round(sum((sg.high[j] - sg.low[j]) / sg.close[j] * 100
                                       for j in range(i - n + 1, i + 1)) / n, 4)
    dt = datetime.fromtimestamp(int(sg.bars[i][0]), CST)
    f["hour"] = dt.hour
    f["is_night"] = 1 if (dt.hour >= 20 or dt.hour <= 1) else 0
    f["weekday"] = dt.weekday()
    return f


def auc(pos, neg):
    """AUC：正样本随机得分高于负样本的概率。"""
    if not pos or not neg:
        return None
    allv = sorted([(v, 1) for v in pos] + [(v, 0) for v in neg])
    n = len(allv)
    ranks = {}
    i = 0
    r = [0] * n
    while i < n:
        j = i
        while j + 1 < n and allv[j + 1][0] == allv[i][0]:
            j += 1
        avg = (i + j) / 2.0 + 1
        for k in range(i, j + 1):
            r[k] = avg
        i = j + 1
    s = sum(rk for rk, (_, lab) in zip(r, allv) if lab == 1)
    npos, nneg = len(pos), len(neg)
    return (s - npos * (npos + 1) / 2) / (npos * nneg)


def main():
    print("=" * 116)
    print("决定性检验：老师的真实入场 vs 规则信号 —— 有没有可计算的判读维度？")
    print("=" * 116)

    rows = [json.loads(l) for l in open("/root/data/teacher_trades.jsonl") if l.strip()]
    rows.sort(key=lambda r: r["trade_id"])
    per = {t["trade_id"]: t for t in ta.build_teacher_analysis()["per_trade"]}

    sg = SigGen(load_bars("15m"), 15)
    sigs = gen_signals(sg, CFG)

    # 正样本：老师的 20 笔
    pos = []
    for r in rows:
        pr = per.get(r["trade_id"], {})
        ts = pr.get("entry_ts")
        ev = next((e for e in (r.get("events") or []) if e.get("type") == "entry"), {})
        px = num(r.get("fill_price")) or num(ev.get("px")) or num(r.get("entry_zone"))
        if not ts or not px:
            continue
        f = feats(sg, int(ts), float(px), r.get("side"), None)
        f["_ts"] = int(ts)
        f["_id"] = r["trade_id"]
        pos.append(f)
    print(f"\n正样本（老师真实入场）n={len(pos)}")

    # 负样本：同一时期的规则信号（去重到相近密度）
    t_lo = min(p["_ts"] for p in pos) - 30 * 86400
    t_hi = max(p["_ts"] for p in pos) + 30 * 86400
    neg_raw = []
    for s in sigs:
        if not (t_lo <= s["ts"] <= t_hi):
            continue
        f = feats(sg, s["ts"], s["ref_px"], s["side"], None)
        f["_ts"] = s["ts"]
        neg_raw.append(f)
    # 去重（同向 12h）
    neg = []
    last = {}
    for f in sorted(neg_raw, key=lambda x: x["_ts"]):
        k = "long" if f.get("struct_up", 1) == 0 else "short"
        if k in last and f["_ts"] - last[k] < 12 * 3600:
            continue
        last[k] = f["_ts"]
        neg.append(f)
    print(f"负样本（同期规则信号）n={len(neg)}")

    keys = sorted(set(k for p in pos for k in p if not k.startswith("_")) &
                  set(k for n in neg for k in n if not k.startswith("_")))

    print(f"\n{'特征':<20}{'AUC':<9}{'方向':<8}{'置换p':<9}{'前半AUC':<10}{'后半AUC':<10}{'判定'}")
    print("  " + "-" * 96)

    results = []
    for k in keys:
        pv = [p[k] for p in pos if p.get(k) is not None]
        nv = [n[k] for n in neg if n.get(k) is not None]
        if len(pv) < 8 or len(nv) < 20:
            continue
        a = auc(pv, nv)
        if a is None:
            continue
        # 置换检验
        random.seed(42)
        pool = pv + nv
        np_, nn_ = len(pv), len(nv)
        cnt = 0
        N = 1000
        for _ in range(N):
            random.shuffle(pool)
            aa = auc(pool[:np_], pool[np_:])
            if aa is not None and abs(aa - 0.5) >= abs(a - 0.5):
                cnt += 1
        pval = (cnt + 1) / (N + 1)
        # 前后半
        mid = (min(p["_ts"] for p in pos) + max(p["_ts"] for p in pos)) / 2
        p1 = [p[k] for p in pos if p.get(k) is not None and p["_ts"] <= mid]
        n1 = [n[k] for n in neg if n.get(k) is not None and n["_ts"] <= mid]
        p2 = [p[k] for p in pos if p.get(k) is not None and p["_ts"] > mid]
        n2 = [n[k] for n in neg if n.get(k) is not None and n["_ts"] > mid]
        a1 = auc(p1, n1)
        a2 = auc(p2, n2)
        stable = (a1 is not None and a2 is not None and
                  (a1 - 0.5) * (a2 - 0.5) > 0)   # 前后半同向
        strong = abs(a - 0.5) >= 0.2 and pval < 0.05 and stable
        direction = "老师更高" if a > 0.5 else "老师更低"
        verdict = "★ 存在判读维度" if strong else ("接近但不稳" if abs(a - 0.5) >= 0.15 else "无区分度")
        results.append((abs(a - 0.5), k, a, pval, a1, a2, strong))
        print(f"  {k:<20}{a:<9.3f}{direction:<8}{pval:<9.3f}"
              f"{(f'{a1:.3f}' if a1 is not None else 'n/a'):<10}"
              f"{(f'{a2:.3f}' if a2 is not None else 'n/a'):<10}{verdict}")

    results.sort(reverse=True)
    print("\n" + "=" * 116)
    print("结论")
    print("=" * 116)
    strong = [r for r in results if r[6]]
    if strong:
        print(f"  ★ 找到 {len(strong)} 个有区分度的判读维度：")
        for _, k, a, pval, a1, a2, _ in strong:
            print(f"    {k}: AUC={a:.3f} p={pval:.3f}（前半{a1:.3f} / 后半{a2:.3f}）")
        print("\n  → 下一步：把这些维度加进规则重跑长历史，看是否转正。")
    else:
        top = results[:5]
        print("  ✗ 没有任何候选特征能把老师的真实入场从规则信号里区分出来")
        print("     （最高 AUC 的几个特征：）")
        for _, k, a, pval, a1, a2, _ in top:
            print(f"    {k}: AUC={a:.3f} p={pval:.3f}（前半{a1:.3f} / 后半{a2:.3f}）")
        print("\n  → 结论：在 20 笔样本 + 现有 K 线分辨率下，**老师的选单无法被机械规则复现**。")
        print("     他的 edge 在『看盘当下的判断』里，不在可计算的形态条件里。")
        print("     这不是继续调参能解决的 —— 0/54 组参数 + 0 个可区分特征 = 判停。")


if __name__ == "__main__":
    main()
