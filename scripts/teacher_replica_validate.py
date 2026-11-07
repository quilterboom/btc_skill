#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
teacher_replica_validate.py —— 老师模式量化的最终检验

## 前置（已完成的诊断，别重复）
1. `teacher_pattern_spec.py`  → 从 20 笔反推出模式规格
2. `teacher_rule_derivation.py` → 阈值取自老师自己的分布（不从 PnL 拟合）
3. `teacher_engine_fidelity.py` → 引擎复现率 63.2%（受限于入场时刻歧义/账本模糊时间戳/同根双触）
4. `teacher_replica_recall.py` → 规则只命中老师 30% 的单

## 本脚本回答的问题
把「老师的入场规则 + 老师的出场阶梯」放到长历史上，
**是否存在正期望**？以及：
- 训练/测试是否一致
- 是否显著优于随机入场
- 参数是平原还是孤峰
- 反向检验（把条件反过来）是否显著变差
- 多个去重间隔下是否稳健

## 诚实边界
复现率 63.2% 意味着：**这个回测的绝对数字有不确定性**。
但回测不是复现老师的单，而是【用规则生成新单】，
出场逻辑是自洽的保守规则（同根双触判 SL）。
所以结论按「方向 + 稳健性」读，不按绝对金额读。

## 只读
"""
from __future__ import annotations

import itertools
import json
import os
import random
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, "/root/skills/btc-gate-intraday-strategy/web")

from teacher_replica import (BASE_CFG, CFG, SigGen, bootstrap_ci, fill_limit,
                             gen_signals, load_bars, run, show, stats)

CST = timezone(timedelta(hours=8))


def random_baseline(sg, cfg, n_target, n_trials=400, seed=11):
    """随机时点入场 + 同一套出场阶梯 → 净利分布。"""
    random.seed(seed)
    lo, hi = 60, len(sg.bars) - 2
    out = []
    for _ in range(n_trials):
        picks = random.sample(range(lo, hi), min(n_target, hi - lo))
        nets = []
        for i in picks:
            side = "long" if random.random() < 0.9 else "short"
            sig = {"i": i, "ts": int(sg.bars[i][0]), "side": side,
                   "level": sg.low[i] if side == "long" else sg.high[i]}
            f = fill_limit(sg, sig, cfg)
            if not f:
                continue
            from teacher_replica import settle
            nets.append(settle(sg, {**sig, **f}, cfg)["net"])
        if nets:
            out.append(sum(nets))
    if not out:
        return 0.0, 0.0, []
    out.sort()
    return out[int(0.05 * len(out))], out[int(0.95 * len(out))], out


def full_check(sg, cfg, label, dedup=12.0):
    """一套完整的 5 关检验。返回 (通过?, 明细行)。"""
    lines = []
    sigs = gen_signals(sg, cfg)
    trades, _ = run(sg, cfg, dedup_hours=dedup, sigs=sigs)
    if len(trades) < 10:
        lines.append(f"    {label}: n={len(trades)} 样本不足")
        return False, lines, trades
    st = stats(trades, label)
    nets = [t["net"] for t in trades]
    ci_lo, ci_hi, p_pos = bootstrap_ci(nets)
    cut = sg.tss[0] + int((sg.tss[-1] - sg.tss[0]) * 0.7)
    tr = [t for t in trades if t["ts"] < cut]
    te = [t for t in trades if t["ts"] >= cut]
    tr_net = sum(t["net"] for t in tr)
    te_net = sum(t["net"] for t in te)
    rlo, rhi, _ = random_baseline(sg, cfg, len(trades))
    better_than_random = st["net"] > rhi
    lines.append(f"    {label}: n={st['n']:>3} 胜率{st['win_rate']:>5.1f}% 净{st['net']:>+8.2f}U "
                 f"盈亏比{st['payoff']:>4.2f} | 训练{tr_net:>+8.2f} 测试{te_net:>+8.2f} "
                 f"| CI[{ci_lo:+.1f},{ci_hi:+.1f}] P(EV>0)={p_pos*100:>4.1f}% "
                 f"| 随机上界{rhi:+.1f} {'✓优于随机' if better_than_random else '✗不优于随机'}")
    ok = (st["net"] > 0 and tr_net > 0 and te_net > 0 and ci_lo > 0
          and better_than_random)
    return ok, lines, trades


def main():
    print("=" * 116)
    print("老师模式量化复刻 · 最终检验")
    print("=" * 116)

    results = {}

    for tf, tf_min in (("1h", 60), ("15m", 15)):
        sg = SigGen(load_bars(tf), tf_min)
        span = f"{datetime.fromtimestamp(sg.tss[0],CST):%Y-%m-%d} → {datetime.fromtimestamp(sg.tss[-1],CST):%Y-%m-%d}"
        days = (sg.tss[-1] - sg.tss[0]) / 86400
        print(f"\n{'#'*116}\n【{tf}】{span} | {days:.0f} 天\n{'#'*116}")

        # ── 1. 基准：老师原始规格 ──
        print("\n  ── 1. 基准配置（老师反推规格）──")
        ok, lines, trades = full_check(sg, CFG, "基准")
        for l in lines:
            print(l)
        results[(tf, "基准")] = (ok, stats(trades))

        # ── 2. 出场阶梯扫描（老师 SL/TP 的邻域）──
        print("\n  ── 2. 出场阶梯扫描（SL × TP1 × TP2）──")
        print("      判据：训练/测试都为正 + CI 下界 > 0 + 优于随机 → 才算成立")
        best = []
        for sl in (0.15, 0.25, 0.40):
            for tp1 in (0.40, 0.57, 0.80):
                for tp2 in (1.0, 1.5, 2.2):
                    c = dict(CFG)
                    c.update({"sl_pct": sl, "tp1_pct": tp1, "tp2_pct": tp2,
                              "sl_struct": False})   # ★ 先关掉结构 SL，纯固定 % 扫
                    ok, lines, tr = full_check(sg, c, f"SL{sl} TP1{tp1} TP2{tp2}")
                    if ok:
                        best.append((stats(tr)["net"], sl, tp1, tp2, len(tr)))
                    print(lines[0] if lines else "      (样本不足)")
        if best:
            best.sort(reverse=True)
            print(f"\n      ★ 通过全部 5 关的组合 {len(best)} 组：")
            for net, sl, tp1, tp2, n in best[:8]:
                print(f"        SL{sl} TP1{tp1} TP2{tp2}  n={n} 净{net:+.2f}U")
        else:
            print("\n      ★ 0 组通过 → 这个出场/入场组合不存在（btc-strategy-dev 步骤 7：0/N 判停）")

        # ── 3. 参数平原（找孤峰）──
        print("\n  ── 3. 参数平原：逐个动一个参数 ──")
        for key, vals in (("drop_min", (0.3, 0.5, 0.8, 1.2, 1.6)),
                          ("vol_min", (0.6, 0.8, 1.0, 1.3, 1.6)),
                          ("max_dist_pct", (0.6, 1.2, 2.0, 3.0, 5.0)),
                          ("tp1_pct", (0.35, 0.45, 0.57, 0.7, 0.9)),
                          ("tp2_pct", (0.9, 1.2, 1.5, 1.9, 2.4)),
                          ("sl_pct", (0.15, 0.25, 0.35, 0.5, 0.7))):
            row = []
            for v in vals:
                c = dict(CFG)
                c[key] = v
                if key in ("sl_pct",):
                    c["sl_struct"] = False
                t, _ = run(sg, c)
                row.append(f"{v}→{stats(t)['net']:+.0f}" if t else f"{v}→n/a")
            print(f"    {key:<16} " + "  ".join(row))

        # ── 4. 多种去重间隔 ──
        print("\n  ── 4. 多种去重间隔 ──")
        for dh in (4, 6, 8, 12, 18, 24):
            t, _ = run(sg, CFG, dedup_hours=dh)
            s = stats(t)
            if s["n"]:
                print(f"    去重{dh:>2}h  n={s['n']:>3} 胜率{s['win_rate']:>5.1f}% 净{s['net']:>+8.2f}U")
            else:
                print(f"    去重{dh:>2}h  n=0")

        # ── 5. 反向检验 ──
        print("\n  ── 5. 反向检验 ──")
        for sd, name in (("down", "★ 老师模式(down结构做多)"), ("up", "反向(up结构做多)")):
            c = dict(CFG)
            c["struct_dir"] = sd
            t, _ = run(sg, c)
            s = stats(t)
            print(f"    {name:<28} n={s['n']:>3} 胜率{s['win_rate']:>5.1f}% 净{s['net']:>+8.2f}U")

        # ── 6. 方向 / 收益来源 ──
        print("\n  ── 6. 方向 + 收益来源 ──")
        t, _ = run(sg, CFG)
        for side in ("long", "short"):
            sel = [x for x in t if x["side"] == side]
            show(stats(sel, side), "    ")
        by = {}
        for x in t:
            by.setdefault(x["exit"], []).append(x["net"])
        for k, v in sorted(by.items(), key=lambda kv: -sum(kv[1])):
            print(f"    {k:<6} n={len(v):>3} 净{sum(v):>+9.2f}U 均{sum(v)/len(v):>+7.2f}U")

    print("\n" + "=" * 116)
    print("结论")
    print("=" * 116)
    for (tf, name), (ok, st) in results.items():
        verdict = "✓ 成立" if ok else "✗ 不成立"
        print(f"  {tf} {name}: {verdict}  (n={st['n']} 净{st['net']:+.2f}U)")


if __name__ == "__main__":
    main()
