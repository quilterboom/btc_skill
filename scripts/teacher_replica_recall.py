#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
teacher_replica_recall.py —— 召回检验：复刻规则能不能命中老师自己的 20 笔

## 为什么这是第一顺位检验
在谈「复刻规则的收益」之前，必须先答一个问题：

    **老师做的那些单，我的规则会不会做？**

如果召回率接近 0 → 复刻的根本不是老师的模式，后面所有 PnL 数字都无意义
（复刻的是另一个东西）。
如果召回率高 → 说明规则确实在复刻老师的决策，再看收益才有意义。

## 三个口径
- **同日命中**：规则在老师入场当天产生过信号（方向不限）
- **同向命中**：规则在老师入场前 N 根内产生过【同方向】信号
- **精确命中**：规则信号在老师入场前 N 根内同方向，且挂单成交价接近老师成交价

## 只读
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, "/root/skills/btc-gate-intraday-strategy/web")

import teacher_analysis as ta
from teacher_replica import (CFG, SigGen, fill_limit, gen_signals, load_bars,
                             settle, stats, show)

CST = timezone(timedelta(hours=8))


def main():
    print("=" * 108)
    print("召回检验：复刻规则能否命中老师的 20 笔")
    print("=" * 108)

    bars = load_bars("15m")
    sg = SigGen(bars, 15)
    trades_teacher = [json.loads(l) for l in open("/root/data/teacher_trades.jsonl") if l.strip()]
    trades_teacher.sort(key=lambda r: r["trade_id"])
    ta_per = {t["trade_id"]: t for t in ta.build_teacher_analysis()["per_trade"]}

    # 规则信号（不挂单，只看信号本身）
    sigs = gen_signals(sg, CFG)
    print(f"\n规则在 15m 全历史产生 {len(sigs)} 个信号"
          f"（{datetime.fromtimestamp(sg.tss[0],CST):%Y-%m-%d} → {datetime.fromtimestamp(sg.tss[-1],CST):%Y-%m-%d}）")

    sig_by_side = {}
    for s in sigs:
        sig_by_side.setdefault(s["side"], []).append(s)

    print(f"\n{'#':<4}{'老师时间':<12}{'方向':<7}{'老师价':<10}"
          f"{'同日命中':<10}{'同向命中(±12根=3h)':<22}{'最近同向信号':<28}")
    print("  " + "-" * 100)

    n_day = n_dir = n_prec = n_total = 0
    detail = []
    for r in trades_teacher:
        ta_row = ta_per.get(r["trade_id"], {})
        ts = ta_row.get("entry_ts")
        px = r.get("entry_zone") or r.get("fill_price")
        side = r.get("side")
        if not ts or not px:
            continue
        n_total += 1
        day = datetime.fromtimestamp(ts, CST).date()
        # 同日
        day_hit = any(datetime.fromtimestamp(s["ts"], CST).date() == day for s in sigs)
        # 同向（信号在入场前 12 根内）
        cands = [s for s in sig_by_side.get(side, [])
                 if -12 * 900 <= (s["ts"] - ts) <= 12 * 900]
        dir_hit = bool(cands)
        near = None
        if cands:
            near = min(cands, key=lambda s: abs(s["ts"] - ts))
        if day_hit:
            n_day += 1
        if dir_hit:
            n_dir += 1
        ts_txt = datetime.fromtimestamp(ts, CST).strftime("%m-%d %H:%M")
        near_txt = ""
        if near:
            dt_min = (near["ts"] - ts) / 60
            near_txt = f"{dt_min:+.0f}min 量{near['vol_x']}x 跌{near['drop_pct']:.1f}%"
            # 精确：同向 + 挂单成交价接近
            f = fill_limit(sg, near, CFG)
            if f and abs(f["px"] - float(px)) / float(px) < 0.005:
                n_prec += 1
                near_txt += " ✓价近"
        print(f"  {r['trade_id']:<4}{ts_txt:<12}{side:<7}{px:<10}"
              f"{'✓' if day_hit else '✗':<10}"
              f"{'✓' if dir_hit else '✗':<22}{near_txt:<28}")
        detail.append({"trade_id": r["trade_id"], "day_hit": day_hit, "dir_hit": dir_hit,
                       "near": near_txt})

    print(f"\n  同日命中  : {n_day}/{n_total} = {n_day/n_total*100:.0f}%")
    print(f"  同向命中  : {n_dir}/{n_total} = {n_dir/n_total*100:.0f}%  (±3h 内同方向信号)")
    print(f"  价格也近  : {n_prec}/{n_total} = {n_prec/n_total*100:.0f}%  (挂单成交价差 <0.5%)")

    # ── 反向：规则会不会产生一堆老师根本没做的信号？──
    print(f"\n  ── 信号规模 vs 老师实际单量 ──")
    print(f"    规则信号数       : {len(sigs)}")
    print(f"    老师实际单数     : {n_total}")
    print(f"    信号/单 比值      : {len(sigs)/max(n_total,1):.1f}x")
    print("    → 比值远大于 1 = 规则比老师宽松得多（老师会跳过大量信号）")
    print("      这是预期的：老师有人的判断（选单），规则只有形态条件。")
    print("      ★ 所以「规则命中老师」率高 ≠ 规则能替老师做决定。")

    # ── 老师「没做」的信号长什么样：反向对照 ──
    print(f"\n  ── 关键检验：规则信号在老师不做的时候，收益如何？──")
    print("    （若规则所有信号都赚 → 老师不需要判断；")
    print("      若只在老师做的那些信号上赚 → 老师的选单能力是 edge 的一部分）")
    from teacher_replica import run
    tr, _ = run(sg, CFG)
    if not tr:
        print("    无成交信号")
        return
    # 标出哪些成交信号落在老师入场日附近
    t_days = {datetime.fromtimestamp(ta_per[r["trade_id"]]["entry_ts"], CST).date()
              for r in trades_teacher if ta_per.get(r["trade_id"], {}).get("entry_ts")}
    near_t = [t for t in tr if datetime.fromtimestamp(t["ts"], CST).date() in t_days]
    far_t = [t for t in tr if datetime.fromtimestamp(t["ts"], CST).date() not in t_days]
    show(stats(near_t, "老师做过的那几天"), "    ")
    show(stats(far_t, "老师没做的那些天"), "    ")


if __name__ == "__main__":
    main()
