#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
sweep_hammer_entry_exec.py —— hammer 入场执行方式参数扫描（2026-11-07）

## 回答的问题

「stop-limit 上限设多少、等多久」—— 参数不是拍脑袋定的，要扫出来。
同时必须回答一个**生产约束问题**：stop-limit 挂上去等回踩，
需要挂单窗口足够长；而生产 hammer watcher 的 preview 窗口只有 **120 秒**。

## 扫描维度

| 维度 | 取值 | 含义 |
|---|---|---|
| cap | 0.05% ~ 0.50% | stop-limit 的限价上限 = 触发线 × (1+cap) |
| wait_h | 0.033h(2min) ~ 12h | 挂单等回踩的窗口 |
| 环境 | 生产 / 训练 | 生产是样本内，训练是样本外 |

## 关键设计（别搞错）

- **只用 REAL 样本**（真实发生过的 market_on_watcher 成交），幽灵/过期/MISSED 不进对照。
- **A（现状）永远用真实 fill_px**，B/C/D 各自算自己的成交价。
- **C 的数字非单调**：cap 收紧 → 更多单「市价超上限」→ 挂在上限价等回踩 →
  成交时刻变晚（不是纯价格改善）。所以每个 cap 都要同时报
  「市价超上限笔数」和「最终成交率」。
- **等回踩窗口的时长直接决定 C 有没有意义**：窗口太短 → 挂单等不到 → 不成交。

## 用法

    cd /root/skills/btc-gate-intraday-strategy/scripts
    /root/.hermes/hermes-agent/venv/bin/python3 sweep_hammer_entry_exec.py
    /root/.hermes/hermes-agent/venv/bin/python3 sweep_hammer_entry_exec.py --tf 5m

## 只读
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import backtest_hammer_limit_entry as bt

PROD = "/root/data/journal.jsonl"
TRAIN = "/root/data/training/journal.jsonl"
CAPS = [0.0005, 0.001, 0.002, 0.003, 0.005]
WAITS = [0.033, 0.083, 0.25, 1.0, 4.0, 12.0]   # 2min / 5min / 15min / 1h / 4h / 12h


def _agg(rows, key):
    vals = []
    filled = 0
    for r in rows:
        if key == "A":
            v, e = r["A_net"], r["A_entry"]
        elif key == "B":
            v, e = r["B_net"], r["B_entry"]
        else:
            cap = float(key)
            cell = r["C"].get(cap) or {}
            v, e = cell.get("net"), cell.get("entry")
        if v is None:
            continue
        vals.append(v)
        if e is not None:
            filled += 1
    return len(vals), filled, sum(vals)


def run_grid(journal, tf, label):
    print(f"\n{'='*118}")
    print(f"【{label}】{journal}  |  K线 {tf}")
    print("="*118)
    out = {}
    for wait_h in WAITS:
        rows = bt.run(journal, tf, wait_h, bt.DEFAULT_TIMEOUT_H, CAPS)
        reals = [r for r in rows if r["cls"] == "REAL"]
        nA, fA, tA = _agg(reals, "A")
        nB, fB, tB = _agg(reals, "B")
        print(f"\n  ── 挂单等待窗口 {wait_h}h "
              f"({'2分钟' if wait_h<=0.04 else f'{wait_h*60:.0f}分钟' if wait_h<1 else f'{wait_h}小时'}) ──")
        print(f"    A 现状 stop-market      成交 {fA}/{nA}  净 {tA:+9.2f}U")
        print(f"    B 零滑点（理论上限）      成交 {fB}/{nB}  净 {tB:+9.2f}U   "
              f"（追价可回收 {tB-tA:+.2f}U）")
        for cap in CAPS:
            nC, fC, tC = _agg(reals, cap)
            over = sum(1 for r in reals
                       if r["actual_fill"] and float(r["actual_fill"]) > r["trigger"] * (1 + cap))
            print(f"    C stop-limit +{cap*100:.2f}%      成交 {fC}/{nC}  净 {tC:+9.2f}U   "
                  f"ΔvsA {tC-tA:+8.2f}U   (市价超上限 {over}/{nA})")
        out[wait_h] = {"A": tA, "B": tB,
                       "C": {cap: _agg(reals, cap)[2] for cap in CAPS}}
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tf", default="15m", choices=["1m", "5m", "15m", "1h"])
    args = ap.parse_args()

    print("★ hammer 入场执行方式参数扫描")
    print(f"  生产 preview 窗口只有 120 秒 → wait_h=0.033 那行最接近现状可用范围")
    print(f"  样本量极小（生产 REAL 9 笔 / 训练 REAL 8 笔）→ 只能当方向参考，不是参数结论")

    p = run_grid(PROD, args.tf, "生产（样本内）")
    t = run_grid(TRAIN, args.tf, "训练（样本外）")

    print(f"\n{'='*118}")
    print("【汇总】生产 vs 训练 的一致性检查（方向一致才算数）")
    print("="*118)
    print(f"{'wait_h':>8} {'cap':>8} {'生产 ΔvsA':>12} {'训练 ΔvsA':>12} {'方向一致':>10}")
    for wait_h in WAITS:
        for cap in CAPS:
            dp = p[wait_h]["C"][cap] - p[wait_h]["A"]
            dt = t[wait_h]["C"][cap] - t[wait_h]["A"]
            same = "✓" if (dp > 0) == (dt > 0) else "✗"
            print(f"{wait_h:>8} {cap*100:>7.2f}% {dp:>+12.2f} {dt:>+12.2f} {same:>10}")
    print()
    return p, t


if __name__ == "__main__":
    main()
