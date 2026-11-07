#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
replica_driver.py —— 通用复刻驱动器（消费规格 JSON，不含任何硬编码阈值）

## 为什么是驱动器而不是「策略」
2026-11-07 的教训：把参考方的判断写成代码里的阈值 = 把「一次性读数」焊死成
「看起来像策略的东西」。本驱动器的所有数字都来自规格文件
（`/root/data/teacher_spec.json`，由 `teacher_spec.py` 从账本重新生成）。

**改数字 = 改数据 = 重跑 `teacher_spec.py`，不改代码。**

## 三段分离
    信号（signal_fn） → 入场（entry_fn） → 出场（exit_fn）
刻意分离，因为参考方的**出场可以配任何入场**，反过来不行。
混在一起就分不清收益来自哪一段。

## 用法
    python3 replica_driver.py --spec /root/data/teacher_spec.json --tf 1h
    python3 replica_driver.py --spec ... --tf 1h --coverage 80 --direction le
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from ref_trader_tools import (CST, bar_features, bootstrap_ci, discrimination,
                              paired_sign_test, spec_threshold, summarize)

KL = "/root/data/BTC_USDT_klines.json"
FEE = 4.85
NOTIONAL = 5000.0


def load_bars(tf):
    return json.load(open(KL))["BTC_USDT"][tf]["bars"]


def build_condition(spec: dict, coverage: float, dims: list,
                    directions: dict):
    """从规格生成「维度 → 阈值」的条件表。

    directions: {dim: "le"|"ge"} —— 每个维度取哪一侧分位。
    ★ 方向必须按维度语义指定（见 spec_threshold 的说明）。
    """
    conds = {}
    for d in dims:
        direction = directions.get(d, "le")
        th = spec_threshold(spec, d, coverage=coverage, direction=direction)
        if th is None:
            continue
        conds[d] = {"threshold": th, "direction": direction}
    return conds


def signal_from_conds(conds: dict, side_filter=None):
    """生成信号函数：满足条件的 bar 产生候选。

    side_filter: None | "long" | "short" | "counter"（与结构相反）
    """
    def signal_fn(i, bars=None, ts=None, px=None, side=None, feats=None):
        if feats is None:
            return None
        for d, c in conds.items():
            v = feats.get(d)
            if v is None:
                return None
            if c["direction"] == "le":
                if v > c["threshold"]:
                    return None
            else:
                if v < c["threshold"]:
                    return None
        return {"side": side, "feats": feats}
    return signal_fn


def make_exit(entry: dict, ladder: dict, bars: list, tf_min: int):
    """按规格里的出场阶梯结算。

    ladder 来自规格 JSON：tp1_pct / tp1_reduce_pct / tp2_pct / sl_pct 的中位数。
    ★ short 单所有 PnL 取负（sign bug 修复，2026-11-07）。
    """
    px = entry["px"]
    side = entry["side"]
    sgn = 1.0 if side == "long" else -1.0
    C = max(1, round(NOTIONAL / (px * 1e-4)))
    q1 = int(round(C * ladder["tp1_reduce_pct"] / 100.0))
    tp1 = px * (1 + sgn * ladder["tp1_pct"] / 100.0)
    tp2 = px * (1 + sgn * ladder["tp2_pct"] / 100.0)
    sl = px * (1 - sgn * ladder["sl_pct"] / 100.0)

    tss = [int(b[0]) for b in bars]
    import bisect
    j = bisect.bisect_right(tss, entry["ts"]) - 1
    gross, tp1_hit = 0.0, False
    max_bars = max(2, int(72 * 60 / tf_min))
    end = min(len(bars), j + 1 + max_bars)
    for k in range(j + 1, end):
        b = bars[k]
        h, l = float(b[2]), float(b[3])
        if side == "long":
            t_sl, t_tp1, t_tp2 = l <= sl, (not tp1_hit and h >= tp1), h >= tp2
        else:
            t_sl, t_tp1, t_tp2 = h >= sl, (not tp1_hit and l <= tp1), l <= tp2
        if t_sl:                       # 同根双触也判 SL（保守）
            gross += (C if not tp1_hit else C - q1) * (sl - px) * 1e-4 * sgn
            return {"net": gross - FEE, "exit": "BE" if tp1_hit else "SL"}
        if t_tp1:
            tp1_hit = True
            gross += q1 * (tp1 - px) * 1e-4 * sgn
            sl = px                       # TP1 后移保本
        if t_tp2:
            gross += (C if not tp1_hit else C - q1) * (tp2 - px) * 1e-4 * sgn
            return {"net": gross - FEE, "exit": "TP2"}
    px_end = float(bars[end - 1][4])
    gross += (C if not tp1_hit else C - q1) * (px_end - px) * 1e-4 * sgn
    return {"net": gross - FEE, "exit": "TIME"}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--spec", default="/root/data/teacher_spec.json")
    ap.add_argument("--tf", default="1h")
    ap.add_argument("--coverage", type=float, default=80)
    ap.add_argument("--dedup-hours", type=float, default=12.0)
    ap.add_argument("--min-pass", type=int, default=0,
                    help="至少满足多少个维度（0 = 全满足）")
    args = ap.parse_args()

    spec = json.load(open(args.spec))
    tf_min = {"1m": 1, "5m": 5, "15m": 15, "1h": 60, "4h": 240}[args.tf]
    bars = load_bars(args.tf)
    tss = [int(b[0]) for b in bars]
    ladder = {
        "tp1_pct": spec["mode"]["tp1_pct"]["median"],
        "tp1_reduce_pct": spec["mode"]["tp1_reduce_pct"]["median"],
        "tp2_pct": spec["mode"]["tp2_pct"]["median"],
        "sl_pct": spec["mode"]["sl_pct"]["median"],
    }

    print("=" * 100)
    print(f"规格驱动复刻 | {args.tf} | 覆盖率 {args.coverage:.0f}% | "
          f"去重 {args.dedup_hours}h")
    print(f"规格来源: {spec['meta']['generated_at']} | 样本 {spec['meta']['n_entries']} 笔 "
          f"| {spec['meta']['span']}")
    print(f"出场阶梯（来自规格）: {ladder}")
    print("=" * 100)

    # 每个维度的方向：按语义指定（这里从规格的 median 符号推断「宽松侧」）
    dims = sorted(spec["dims"]["dims"].keys())
    directions = {}
    for d in dims:
        v = spec["dims"]["dims"][d]
        if v.get("categorical"):
            continue
        # 约定：跌幅类/量能类取 ge（要求「至少这么极端」），位置类取 le
        if d.startswith(("drop_", "rise_", "vol_x")):
            directions[d] = "ge"
        elif d in ("hour", "weekday", "is_night"):
            continue
        else:
            directions[d] = "le"

    conds = build_condition(spec, args.coverage / 100.0, dims, directions)
    print(f"\n生效条件（{len(conds)} 个维度）:")
    for d, c in sorted(conds.items()):
        print(f"  {d:<22} {c['direction']}  {c['threshold']}")

    # 逐根扫描
    trades = []
    last = {}
    dedup_s = args.dedup_hours * 3600
    for i in range(60, len(bars) - 1):
        ts = tss[i]
        feats = bar_features(bars, ts, float(bars[i][4]), "long", tf_min=tf_min)
        if not feats:
            continue
        # ★ 计分制：不是「所有维度同时满足」，而是「满足多少个维度」
        #   （老师的判读是多个弱信号的加权，不是一个硬合取）
        passed = 0
        applicable = 0
        for d, c in conds.items():
            v = feats.get(d)
            if v is None:
                continue
            applicable += 1
            if (c["direction"] == "le" and v <= c["threshold"]) or \
               (c["direction"] == "ge" and v >= c["threshold"]):
                passed += 1
        if applicable == 0 or passed < args.min_pass:
            continue
        side = "short" if feats.get("struct") == "up" else "long"
        key = side
        if key in last and ts - last[key] < dedup_s:
            continue
        last[key] = ts
        px = float(bars[i][4])
        r = make_exit({"side": side, "px": px, "ts": ts}, ladder, bars, tf_min)
        trades.append({"ts": ts, "side": side, "px": px, "feats": feats, **r})

    st = summarize(trades)
    print(f"\n结果: n={st['n']} "
          + (f"胜率{st['win_rate']:.1f}% 净{st['net']:+.2f}U "
             f"每笔{st['avg']:+.2f}U 盈亏比{st['payoff']:.2f}"
             if st["n"] else "（无信号：所有维度同时满足的条件太严，"
                             "说明老师的选择不是「单一条件的合取」）"))
    if trades:
        nets = [t["net"] for t in trades]
        lo, hi, p = bootstrap_ci(nets)
        print(f"Bootstrap 90% CI: [{lo:+.2f}, {hi:+.2f}]  P(EV>0)={p*100:.1f}%")
        cut = tss[0] + int((tss[-1] - tss[0]) * 0.7)
        trn = sum(t["net"] for t in trades if t["ts"] < cut)
        ten = sum(t["net"] for t in trades if t["ts"] >= cut)
        print(f"训练 70%: {trn:+.2f}U | 测试 30%: {ten:+.2f}U")
    print(f"\n★ 提示：这是规格驱动的复刻，结论仍需过 §7.6 五关（见 skill "
          f"btc-strategy-analysis）。规格换新 = 重跑 teacher_spec.py，不需要改代码。")


if __name__ == "__main__":
    main()
