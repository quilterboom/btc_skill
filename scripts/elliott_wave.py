#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
艾略特波浪理论分析（2026-10-12 加）
====================================
机械版：用 swing_pivots 找最近 N 个 pivot 序列，按交替规则归类 wave 1-5 + a-b-c。

⚠️ 重要前提：BTC 1h/4h 不是机械派理想标的，数浪本质是判读而非计算。
   本模块只给"最近一段的 swing + 斐波那契关键位 + 顺势/逆势判定"作为
   主观判读的辅助工具，不直接给信号。

输出 dict 形状：
  {
    "tf": "4h" / "1h" / "1d",
    "pivots": [{"i": int, "kind": "H"/"L", "px": float, "ts": int}, ...],
    "current_swing": {"kind": "up"/"down", "lo_px": float, "hi_px": float, "lo_i": int, "hi_i": int, "bars": int},
    "retracements": {0.382: px, 0.5: px, 0.618: px, 0.786: px},  # 回调位（上涨段回调做多）
    "extensions":   {1.0: px, 1.272: px, 1.618: px},              # 扩展位（顺势下一目标）
    "label": "上涨段·回调中" / "下跌段·反弹中" / "横盘",
    "bias": "long" / "short" / "neutral",
    "note": "当前价距 0.618 回调位差 X 点（顺势做多/反弹做空...）"
  }
"""
from __future__ import annotations
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from indicators import swing_pivots, last_swing_leg, fib_retr, fib_retr_up

RETR_LEVELS = [0.236, 0.382, 0.5, 0.618, 0.786]
EXT_LEVELS  = [1.0, 1.272, 1.618]


def analyze(high, low, ts_arr, last_px: float, tf_label: str, n_pivots: int = 6) -> dict:
    """
    high/low/ts_arr: 同长度数组（1d/4h/1h K 线）
    last_px: 当前价
    tf_label: "1d" / "4h" / "1h" / ...
    n_pivots: 用最近几个 pivot 决定 wave 段
    """
    if len(high) < 30:
        return {"tf": tf_label, "label": "数据不足", "bias": "neutral", "pivots": []}

    is_h, conf_h, is_l, conf_l = swing_pivots(high, low, k=5)
    # 取最近 n_pivots 个 pivot
    h_idxs = [int(i) for i in range(len(high)) if is_h[i]]
    l_idxs = [int(i) for i in range(len(low))  if is_l[i]]
    # 合并按 i 排序，取最后 n_pivots 个
    all_pivots = sorted(
        [{"i": i, "kind": "H", "px": float(high[i]), "ts": int(ts_arr[i])} for i in h_idxs] +
        [{"i": i, "kind": "L", "px": float(low[i]),  "ts": int(ts_arr[i])} for i in l_idxs],
        key=lambda x: x["i"]
    )
    pivots = all_pivots[-n_pivots:]

    # 最近一段 swing
    kind, lo_v, hi_v, lo_i, hi_i = last_swing_leg(high, low, is_h, conf_h, is_l, conf_l, len(high) - 1)
    if kind is None:
        return {"tf": tf_label, "label": "横盘", "bias": "neutral", "pivots": pivots}

    # 计算最近一段的幅度 + bars 数
    span = abs(hi_v - lo_v)
    bars = abs(hi_i - lo_i)

    # 关键位
    if kind == "up":
        # 上涨段：回调位 = hi - range * r
        retr = {r: round(hi_v - span * r, 2) for r in RETR_LEVELS}
        # 扩展位 = hi + range * r（顺势下一目标）
        ext  = {r: round(hi_v + span * r, 2) for r in EXT_LEVELS}
    else:
        # 下跌段：反弹位 = lo + range * r
        retr = {r: round(lo_v + span * r, 2) for r in RETR_LEVELS}
        # 扩展位 = lo - range * r
        ext  = {r: round(lo_v - span * r, 2) for r in EXT_LEVELS}

    # 当前位置：当前价 vs 段
    if kind == "up":
        pos_in_leg = (last_px - lo_v) / span if span > 0 else 0.5
    else:
        pos_in_leg = (hi_v - last_px) / span if span > 0 else 0.5

    # 判定 label / bias
    # "顺势" 含义：上涨段回调 0.382-0.618 区间 → 做多；下跌段反弹 0.382-0.618 → 做空
    # "逆势" 含义：当前价在扩展位区间（破前高/前低） → 可能是 wave 3 或 wave 5
    label, bias = _classify(kind, pos_in_leg, retr, last_px, lo_v, hi_v)

    # 距最近回调/扩展位的点数（min over items 返回 (key, value) = (比率, 价位)）
    nearest_retr_lvl, nearest_retr_px = min(retr.items(), key=lambda kv: abs(kv[1] - last_px))
    dist_pts = last_px - nearest_retr_px
    dist_pct = (last_px - nearest_retr_px) / last_px * 100
    # 比率标签：保留 0.x 三位小数的实际数字
    lvl_label = f"{nearest_retr_lvl:.3f}"  # "0.236" "0.382" "0.500" "0.618" "0.786"
    if kind == "up":
        note = f"距 {lvl_label} 回调位 {nearest_retr_px:,.1f} {dist_pts:+,.0f} 点 ({dist_pct:+.2f}%)"
    else:
        note = f"距 {lvl_label} 反弹位 {nearest_retr_px:,.1f} {dist_pts:+,.0f} 点 ({dist_pct:+.2f}%)"

    return {
        "tf": tf_label,
        "pivots": pivots,
        "current_swing": {
            "kind": kind,
            "lo_px": round(lo_v, 2),
            "hi_px": round(hi_v, 2),
            "lo_i": int(lo_i),
            "hi_i": int(hi_i),
            "bars": int(bars),
            "span_pct": round(span / last_px * 100, 2),
        },
        "retracements": retr,
        "extensions": ext,
        "pos_in_leg": round(pos_in_leg, 3),  # 0=段起点, 1=段终点
        "label": label,
        "bias": bias,
        "note": note,
    }


def _classify(kind, pos, retr, last_px, lo_v, hi_v):
    """
    根据当前价在最近段的位置判断 label + bias。
    简化版（不机械数 5 浪）：

      上涨段 pos 在 [0, 0.382] → "上涨段·初期"，bias=long（顺势做多）
      上涨段 pos 在 [0.382, 0.618] → "上涨段·回调做多区"（黄金坑），bias=long
      上涨段 pos 在 [0.618, 1.0] → "上涨段·末段"，bias=neutral（破前高才续）
      上涨段 pos > 1.0（破前高）→ "上涨段·突破"，bias=long（wave 3 候选）

      下跌段 pos 在 [0, 0.382] → "下跌段·初期"，bias=short
      下跌段 pos 在 [0.382, 0.618] → "下跌段·反弹做空区"，bias=short
      下跌段 pos 在 [0.618, 1.0] → "下跌段·末段"，bias=neutral
      下跌段 pos > 1.0（破前低）→ "下跌段·跌破"，bias=short（wave 5 候选）
    """
    if kind == "up":
        if pos < 0.382:
            return "上涨段·初期", "long"
        if pos < 0.618:
            return "上涨段·回调做多区", "long"
        if pos <= 1.0:
            return "上涨段·末段", "neutral"
        return "上涨段·突破前高", "long"
    else:  # down
        if pos < 0.382:
            return "下跌段·初期", "short"
        if pos < 0.618:
            return "下跌段·反弹做空区", "short"
        if pos <= 1.0:
            return "下跌段·末段", "neutral"
        return "下跌段·跌破前低", "short"
