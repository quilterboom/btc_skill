#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
teacher_replica_v2.py —— 两阶段入场假设 + 修正后的随机对照

## 为什么改两处

### 1. 随机对照的样本数 bug
上一版 `random_baseline` 用 `min(n_target, 80)` 抽样，但被测规则跑了 154 笔。
**笔数不同 → 总金额不可比**（随机少承担一半风险）。
本版：随机抽**同样笔数**，比较**每笔均值**的分布。

### 2. ★ 两阶段入场假设（更贴近老师）
上一版规则：下跌 → 立刻在结构位挂限价 → 等成交。
这等于「用限价单接飞刀」，**入场顺序是错的**。

老师实际的动作序列（从 20 笔的 events 反推）：
    ① 价格跌到某个位置（结构位）
    ② 他【看着盘面确认】跌势停了（反转形态出现）
    ③ 挂限价在那个位置（或略高于它）等成交
    ④ 成交后 TP1 减半 → 保本 → TP2

上一版缺了 ②③。本版加：
    - **确认阶段**：下跌后必须出现一根【看涨反转型】bar
      （收盘 > 开盘，或收盘 > 前一根收盘，或长下影收复）
    - **挂单阶段**：确认后把限价挂在「确认 bar 的低点附近」或「确认 bar 的收盘」
    - **成交阶段**：价格回踩到限价才成交（不回踩 = 不做，模拟老师不追价）

这个假设如果也不成立，就能比较确定地回答「老师的 edge 不在机械规则里」。

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

from teacher_replica import (CFG, FEE, NOTIONAL, SigGen, bootstrap_ci, ema_series,
                             fill_limit, gen_signals, load_bars, run, settle, show, stats)

CST = timezone(timedelta(hours=8))


# ---------------------------------------------------------------- 两阶段入场
def gen_signals_v2(sg, cfg):
    """信号 + 确认阶段：下跌 → 出现看涨反转 bar → 才允许挂单。"""
    base = gen_signals(sg, cfg)
    out = []
    for s in base:
        i = s["i"]
        # 找信号后第一根【看涨确认】bar（最多等 confirm_max_bars）
        maxw = max(1, int(cfg["confirm_wait_hours"] * 60 / sg.tf))
        conf_i = None
        for j in range(i + 1, min(len(sg.bars), i + 1 + maxw)):
            o, h, l, c = (float(sg.bars[j][1]), sg.high[j], sg.low[j], sg.close[j])
            pc = sg.close[j - 1]
            bullish = (c > o) or (c > pc) or (l < pc and c > pc * 0.999)
            if bullish:
                conf_i = j
                break
        if conf_i is None:
            continue
        # 挂单价：确认 bar 的低点（老师挂在结构位）或收盘，取「更保守的」
        conf_low = sg.low[conf_i]
        conf_close = sg.close[conf_i]
        if sg.close[conf_i] > sg.low[conf_i]:
            level = conf_low                       # 有实体 → 挂在低点（回踩才成交）
        else:
            level = conf_close                     # 十字星 → 挂在收盘
        s2 = dict(s)
        s2["confirm_i"] = conf_i
        s2["confirm_ts"] = int(sg.bars[conf_i][0])
        s2["level"] = level
        s2["raw_level"] = s["level"]
        out.append(s2)
    return out


def fill_limit_v2(sg, sig, cfg):
    """确认之后挂限价，等成交。"""
    i = sig["confirm_i"]
    level = sig["level"]
    buf = cfg["level_buf_pct"] / 100.0
    if sig["side"] == "long":
        limit = level * (1 + buf)
    else:
        limit = level * (1 - buf)
    end = min(len(sg.bars), i + 1 + max(1, int(cfg["max_wait_hours"] * 60 / sg.tf)))
    for j in range(i + 1, end):
        op = float(sg.bars[j][1])
        h, l = sg.high[j], sg.low[j]
        if sig["side"] == "long":
            if l <= limit:
                px = min(op, limit)
                return {"fill_i": j, "fill_ts": int(sg.bars[j][0]), "px": px, "limit": limit}
        else:
            if h >= limit:
                px = max(op, limit)
                return {"fill_i": j, "fill_ts": int(sg.bars[j][0]), "px": px, "limit": limit}
    return None


def run_v2(sg, cfg, dedup_hours=12.0, sigs=None):
    if sigs is None:
        sigs = gen_signals_v2(sg, cfg)
    trades = []
    last_t = {}
    dedup_s = dedup_hours * 3600
    for s in sigs:
        key = s["side"]
        if key in last_t and s["ts"] - last_t[key] < dedup_s:
            continue
        f = fill_limit_v2(sg, s, cfg)
        if not f:
            continue
        last_t[key] = s["ts"]
        r = settle(sg, {**s, **f}, cfg)
        trades.append({**s, **f, **r})
    return trades, sigs


# ---------------------------------------------------------------- 公平随机对照
def random_baseline(sg, cfg, n_trades, n_trials=400, seed=11):
    """随机入场 + 同一套出场阶梯 → 【每笔均值】分布。★ 笔数与被测一致。"""
    random.seed(seed)
    lo, hi = 60, len(sg.bars) - 2
    out = []
    for _ in range(n_trials):
        picks = random.sample(range(lo, hi), min(n_trades, hi - lo))
        nets = []
        for i in picks:
            side = "long" if random.random() < 0.9 else "short"
            sig = {"i": i, "ts": int(sg.bars[i][0]), "side": side,
                   "level": sg.low[i] if side == "long" else sg.high[i]}
            f = fill_limit(sg, sig, cfg)
            if not f:
                continue
            nets.append(settle(sg, {**sig, **f}, cfg)["net"])
        if nets:
            out.append(sum(nets) / len(nets))     # ★ 每笔均值
    if not out:
        return 0.0, 0.0, 0.0, []
    out.sort()
    return out[int(0.05 * len(out))], out[int(0.95 * len(out))], sum(out) / len(out), out


def report(sg, cfg, label, trades, runner="v2", dedup=12.0):
    if len(trades) < 8:
        print(f"    {label:<34} n={len(trades):>3} 样本不足")
        return False
    nets = [t["net"] for t in trades]
    st = stats(trades)
    ci_lo, ci_hi, p_pos = bootstrap_ci(nets)
    cut = sg.tss[0] + int((sg.tss[-1] - sg.tss[0]) * 0.7)
    tr_net = sum(t["net"] for t in trades if t["ts"] < cut)
    te_net = sum(t["net"] for t in trades if t["ts"] >= cut)
    per_trade = st["net"] / st["n"]
    rlo, rhi, rmean, _ = random_baseline(sg, cfg, st["n"])
    # p 值：随机每笔均值 ≥ 实测每笔均值 的比例
    _, _, _, rall = random_baseline(sg, cfg, st["n"])
    pval = sum(1 for x in rall if x >= per_trade) / max(len(rall), 1)
    print(f"    {label:<34} n={st['n']:>3} 胜率{st['win_rate']:>5.1f}% 净{st['net']:>+8.2f}U "
          f"每笔{per_trade:>+6.2f} | 训练{tr_net:>+8.2f} 测试{te_net:>+8.2f} "
          f"| CI[{ci_lo:+.1f},{ci_hi:+.1f}] P(EV>0)={p_pos*100:>4.1f}% "
          f"| 随机每笔均{rmean:>+6.2f} p={pval:.3f}")
    ok = (st["net"] > 0 and tr_net > 0 and te_net > 0 and ci_lo > 0 and pval < 0.05)
    return ok


def main():
    print("=" * 116)
    print("两阶段入场假设 + 公平随机对照（每笔均值口径）")
    print("=" * 116)

    for tf, tf_min in (("1h", 60), ("15m", 15)):
        sg = SigGen(load_bars(tf), tf_min)
        span = f"{datetime.fromtimestamp(sg.tss[0],CST):%Y-%m-%d} → {datetime.fromtimestamp(sg.tss[-1],CST):%Y-%m-%d}"
        print(f"\n{'#'*116}\n【{tf}】{span}\n{'#'*116}")

        print("\n  ── A. 一阶段（挂单接飞刀，上一版）──")
        c = dict(CFG)
        c["confirm_wait_hours"] = 6
        sigs1 = gen_signals(sg, c)
        t1, _ = run(sg, c, sigs=sigs1)
        report(sg, c, "一阶段", t1)

        print("\n  ── B. 两阶段（确认反转 → 挂限价等回踩）──")
        sigs2 = gen_signals_v2(sg, c)
        print(f"    （信号 {len(sigs1)} → 确认后 {len(sigs2)}）")
        t2, _ = run_v2(sg, c, sigs=sigs2)
        report(sg, c, "两阶段", t2)

        print("\n  ── C. 两阶段 + 出场阶梯扫描 ──")
        best = []
        for sl in (0.15, 0.25, 0.40):
            for tp1 in (0.40, 0.57, 0.80):
                for tp2 in (1.0, 1.5, 2.2):
                    cc = dict(c)
                    cc.update({"sl_pct": sl, "tp1_pct": tp1, "tp2_pct": tp2, "sl_struct": False})
                    tt, _ = run_v2(sg, cc, sigs=sigs2)
                    if len(tt) < 8:
                        continue
                    s = stats(tt)
                    if s["net"] > 0:
                        best.append((s["net"], sl, tp1, tp2, s["n"], s["win_rate"]))
        if best:
            best.sort(reverse=True)
            print(f"    净利为正的组合 {len(best)}/27 组，前 5：")
            for net, sl, tp1, tp2, n, wr in best[:5]:
                print(f"      SL{sl} TP1{tp1} TP2{tp2}  n={n} 胜率{wr:.1f}% 净{net:+.2f}U")
        else:
            print("    ★ 0/27 组净利为正")

        print("\n  ── D. 两阶段 + 时段过滤（老师 65% 在 20:00~01:59）──")
        for hours, name in ((list(range(20, 24)) + [0, 1], "20-01点"),
                            (list(range(21, 24)), "21-23点"),
                            (list(range(20, 24)), "20-23点"),
                            (list(range(24)), "不限")):
            cc = dict(c)
            cc["hours"] = hours
            cc["hour_ok"] = name != "不限"
            ss = gen_signals_v2(sg, cc)
            tt, _ = run_v2(sg, cc, sigs=ss)
            if len(tt) >= 8:
                s = stats(tt)
                per = s["net"] / s["n"]
                print(f"    {name:<10} n={s['n']:>3} 胜率{s['win_rate']:>5.1f}% "
                      f"净{s['net']:>+8.2f}U 每笔{per:>+6.2f}U")
            else:
                print(f"    {name:<10} n={len(tt):>3} 样本不足")

        print("\n  ── E. 两阶段 + 只做老师的方向分布（90% 多 / 10% 空）──")
        cc = dict(c)
        cc["short_ratio"] = 0.10
        ss = gen_signals_v2(sg, cc)
        tt, _ = run_v2(sg, cc, sigs=ss)
        report(sg, cc, "两阶段(9:1)", tt)


if __name__ == "__main__":
    main()
