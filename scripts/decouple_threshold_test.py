#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
decouple_threshold_test.py —— 解耦两条阈值，看能不能拿到追价的 +79.00U

## 问题
`watch/hammer_watcher.py` 里两条阈值是**同一条价格线**：

    rebound_min_pct = 0.3   # 信号确认：价格从 hammer_L 反弹 ≥ 0.3%
    TRIGGER_PCT     = 0.003 # 入场触发：价格 ≥ hammer_L × 1.003

→ 信号被发现时，触发线**已经被穿透** → 市价入场必然追价。
实测追价单独代价：生产 -79.00U / 训练 -44.66U（B−A，同样本只改成交价）。

## 解法：让确认线低于触发线
    确认线（rebound_min_pct）= 0.1%  → 信号更早发出
    触发线（TRIGGER_PCT）    = 0.3%  → watcher 挂 stop-buy 在触发线等
    差值 0.2% 覆盖信号延迟（1s poll + 计算，76000 价位 1 秒可跑 0.13%~0.5%）

这样信号在价格**还没到触发线**时就发出 → watcher 能以触发线精确成交
= **B 变体（零滑点）的效果**。

## 本脚本测什么
扫 rebound_min_pct × TRIGGER_PCT 组合，看：
1. 信号数变化（确认线降低 → 信号变多）
2. 「能成交」的比例（信号发出后价格是否够得到触发线）
3. 成交价质量（vs 现状的追价）
4. 净利（用 backtest_hammer_limit_entry 的出场阶梯）

## 只读
"""
from __future__ import annotations

import bisect
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, "/root/skills/btc-gate-intraday-strategy/web")

import backtest_hammer_auto as bha
import backtest_hammer_limit_entry as bh
from ref_trader_tools import bootstrap_ci, paired_sign_test

KL = "/root/data/BTC_USDT_klines.json"


def load_bars(tf="1m"):
    return json.load(open(KL))["BTC_USDT"][tf]["bars"]


def run_combo(bars1m, confirm_pct, trigger_pct, wait_h=1.0):
    """扫一组 (确认线, 触发线) 组合，返回逐笔结果。

    ★ 顺序：信号在 confirm 时刻发出 → watcher 挂 stop-buy 在 trigger 线
      → 价格够到 trigger 才成交（不够 = 不做，模拟不追价）
    """
    sigs = bha.detect_hammer_in_bars(bars1m, rebound_min_pct=confirm_pct)
    if not sigs:
        return []
    tss = [int(b[0]) for b in bars1m]
    out = []
    for s in sigs:
        L = s.get("hammer_L")
        H = s.get("hammer_H")
        ts = s.get("ts")
        sl_px = s.get("sl")
        if not L or not ts or not sl_px:
            continue
        i = bisect.bisect_right(tss, int(ts)) - 1
        if i < 0:
            continue
        trigger = L * (1 + trigger_pct / 100.0)
        # 挂单等：价格够到 trigger 才成交
        end = min(len(bars1m), i + 1 + max(2, int(wait_h * 60)))
        fill_i, fill_px = None, None
        for k in range(i + 1, end):
            h, l = float(bars1m[k][2]), float(bars1m[k][3])
            if l <= trigger <= h:
                fill_i, fill_px = k, trigger      # ★ 恰好在触发线成交，不追价
                break
            if h < trigger:
                continue
            if l <= trigger:
                fill_i, fill_px = k, trigger
                break
        if fill_i is None:
            out.append({"ts": ts, "trigger": trigger, "filled": False,
                        "A_net": None, "B_net": None})
            continue
        # 出场用 backtest_hammer_limit_entry 的同一套阶梯（从 fill_px 起算）
        res = bh.simulate_exit(bars1m, fill_i, int(bars1m[fill_i][0]),
                               fill_px, sl_px, "long", 48.0)
        out.append({"ts": ts, "trigger": trigger, "filled": True,
                    "fill_px": fill_px, "market_px": float(bars1m[i][4]),
                    "net": res.get("net") if isinstance(res, dict) else res,
                    "label": res.get("label") if isinstance(res, dict) else None})
    return out


def main():
    bars1m = load_bars("1m")
    print("=" * 104)
    print("解耦两条阈值：确认线低于触发线，看能否拿到追价的 +79.00U")
    print("=" * 104)

    # 现状基线
    print(f"\n{'确认线':<10}{'触发线':<10}{'信号':<7}{'成交':<7}{'成交率':<9}{'净利':<11}{'每笔':<10}")
    print("-" * 70)
    for confirm in (0.05, 0.1, 0.15, 0.2, 0.3):
        for trigger in (0.3, 0.5, 0.8):
            r = run_combo(bars1m, confirm, trigger, wait_h=1.0)
            if not r:
                continue
            filled = [x for x in r if x.get("filled")]
            nets = [x["net"] for x in filled if x.get("net") is not None]
            n_sig = len(r)
            n_fill = len(filled)
            net = sum(nets) if nets else 0.0
            avg = net / len(nets) if nets else 0.0
            print(f"  {confirm:<10}{trigger:<10}{n_sig:<7}{n_fill:<7}"
                  f"{(n_fill/n_sig*100 if n_sig else 0):>5.0f}%   {net:>+9.2f}U {avg:>+8.2f}U")

    print("\n★ 对照：现状（确认=触发=0.3%，市价追）= -94.06U（生产 9 笔）")
    print("★ 理论上限（零滑点 = 成交价恰好在触发线）= -15.06U（生产 9 笔）")
    print("★ 差额 +79.00U 就是「解耦阈值」能拿回的部分")


if __name__ == "__main__":
    main()
