#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
老师单进场点位画像 → 训练策略优化输入
====================================

2026-11-03 liusir 指令：「老师的单的进场点位的分析，也是可以当做训练环境
策略优化的输入」。

**为什么老师单的进场点位值得当优化输入**：老师 17 笔里 14 胜 1 负
（胜率 87.5%，净 +329U）—— 胜率显著高于训练环境的超跌反弹（4/9 = 44%）。
老师进场有**稳定的几何特征**（区间位置 / 距高点回撤 / 量能 / 顺势性），
这些特征可以直接派生成训练侧的入场过滤规则。

**与生产亏损原因输入的分工**：
  - 生产亏损原因 → 「什么不该做」（止损打满 / 逆势 / 追单）= 负面清单
  - 老师进场画像 → 「什么值得做」（区间下沿 / 回调进场 / 放量）= 正面清单
  - 两边都进 train_env_optimize 的候选池，统一用 5 道关卡验证

⚠ **样本纪律**：老师 17 笔是**同一个交易者**的风格，不是市场普遍规律。
派生出的规则只能当「候选」，必须在训练账本（同源、可回放）上验证，
且跨样本（训练 vs 生产）方向不许相反。**小样本派生的规则不许直接上线。**

用法：
  from teacher_entry_input import teacher_entry_profile, derive_entry_candidates
  prof = teacher_entry_profile()           # 画像分布
  cands = derive_entry_candidates(rows)    # 候选过滤函数
"""
from __future__ import annotations

import json
import os
import sys
from collections import Counter, defaultdict
from typing import Callable, Dict, List, Optional, Tuple

SKILL_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _p in (os.path.join(SKILL_ROOT, "scripts"),
           os.path.join(SKILL_ROOT, "web")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

DATA_DIR = "/root/data"
TRADES_PATH = os.path.join(DATA_DIR, "teacher_trades.jsonl")


# ---------------------------------------------------------------- 取数
def _load_teacher_trades() -> List[dict]:
    if not os.path.exists(TRADES_PATH):
        return []
    out = []
    with open(TRADES_PATH, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def teacher_entry_profile() -> dict:
    """老师单进场点位画像（几何特征分布 + 逐笔明细）。

    特征全部来自 teacher_analysis._entry_context（K 线反查），不臆造。
    """
    import teacher_analysis as ta

    trades = _load_teacher_trades()
    if not trades:
        return {"error": "teacher_trades.jsonl 不存在或为空", "n": 0}

    bars15 = ta._bars(ta.TF_MAIN)
    bars1h = ta._bars(ta.TF_HIST)

    rows = []
    for t in trades:
        res = t.get("result") or {}
        px = ta._fill_price(t)
        resolved = ta._resolve_entry_ts(t, bars15)
        ctx = ta._entry_context(resolved["ts"], px, bars15, bars1h)
        # aligned 只在 teacher_analysis.build_teacher_analysis 里算，
        # _entry_context 不算 → 这里补同口径（long+up / short+down = 顺势）。
        side = t.get("side")
        trend = ctx.get("trend")
        aligned = None
        if trend in ("up", "down"):
            aligned = ((side == "long" and trend == "up")
                       or (side == "short" and trend == "down"))
        rows.append({
            **ctx,
            # ★ 必须放在 **ctx 之后 —— ctx 自带 aligned:None 键，
            #   放前面会被覆盖成 None（实测顺势/逆势双双变 0 笔）。
            "trade_id": t.get("trade_id"),
            "side": side,
            "net_usd": res.get("net_usd"),
            "settled": res.get("net_usd") is not None,
            "win": res.get("win"),
            "aligned": aligned,
        })

    settled = [r for r in rows if r["settled"]]
    wins = [r for r in settled if r.get("win") is True]
    losses = [r for r in settled if r.get("win") is False]

    def _med(vals):
        v = [x for x in vals if x is not None]
        if not v:
            return None
        v.sort()
        n = len(v)
        return round(v[n // 2], 2) if n % 2 else round((v[n // 2 - 1] + v[n // 2]) / 2, 2)

    def _avg(vals):
        v = [x for x in vals if x is not None]
        return round(sum(v) / len(v), 2) if v else None

    def _stats(subset, key):
        vals = [r.get(key) for r in subset]
        return {"median": _med(vals), "mean": _avg(vals),
                "min": round(min(x for x in vals if x is not None), 2)
                if any(x is not None for x in vals) else None,
                "max": round(max(x for x in vals if x is not None), 2)
                if any(x is not None for x in vals) else None}

    feats = ("range_pos_pct", "dd_from_high_pct", "pre4h_pct", "vol_x")
    dist = {f: {"all": _stats(settled, f),
                "win": _stats(wins, f),
                "loss": _stats(losses, f)} for f in feats}

    # 顺势 vs 逆势
    aligned = [r for r in settled if r.get("aligned") is True]
    counter = [r for r in settled if r.get("aligned") is False]

    def _wr(sub):
        if not sub:
            return None
        w = sum(1 for r in sub if r.get("win") is True)
        return round(w / len(sub) * 100, 1)

    return {
        "n": len(trades),
        "settled": len(settled),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate_pct": round(len(wins) / len(settled) * 100, 1) if settled else None,
        "dist": dist,
        "alignment": {
            "aligned_n": len(aligned), "aligned_win_rate": _wr(aligned),
            "counter_n": len(counter), "counter_win_rate": _wr(counter),
        },
        "rows": rows,
        "sample_warning": (
            f"★ 老师单仅 {len(settled)} 笔已结算（{len(wins)} 胜 / {len(losses)} 负），"
            "是**单一交易者风格**不是市场普遍规律。派生规则只能当候选，"
            "必须在训练账本上回放验证；样本 < 20 笔时**禁止直接上线**。"
        ),
    }


# ---------------------------------------------------------------- 候选派生
def derive_entry_candidates(rows: List[dict]) -> List[Tuple[str, str, Callable, str]]:
    """从老师进场画像派生候选过滤规则。

    rows = 训练/生产的 build_rows() 输出（需含 side / r7 / v5 / trend 等字段）。
    返回 [(名称, 说明, 过滤函数, 来源标记)]。

    ★ 重要：这里派生的阈值**只当候选**，不许直接当参数写进生产。
    理由：老师 17 笔是单一交易者风格 + 小样本，阈值会过拟合。
    """
    prof = teacher_entry_profile()
    if prof.get("error") or prof.get("settled", 0) < 5:
        return []

    cands: List[Tuple[str, str, Callable, str]] = []
    dist = prof["dist"]
    align = prof["alignment"]

    # ① 区间位置过滤：老师偏好在 24h 区间偏下进场（median ~37%）
    #   → 「区间位置 > 80%（追高）不进」
    med_pos = (dist.get("range_pos_pct", {}).get("win", {}) or {}).get("median")
    if med_pos is not None:
        thr = max(60.0, round(med_pos * 1.6, 1))  # 老师中位 × 1.6 作为追高线
        cands.append((
            f"T1 不追高（区间位置 > {thr:.0f}% 不进）",
            f"老师偏好区间下沿进场（中位 {med_pos}%），追高段是风格外",
            lambda r, _t=thr: (r.get("range_pos") is not None
                               and r["range_pos"] > _t),
            "teacher_entry:range_pos"))

    # ② 距 24h 高点回撤不足不进（老师 median ~1.8%）
    med_dd = (dist.get("dd_from_high_pct", {}).get("win", {}) or {}).get("median")
    if med_dd is not None:
        thr = round(med_dd * 0.5, 2)  # 老师中位的一半以下 = 回撤不足
        cands.append((
            f"T2 回撤不足不进（距 24h 高点 < {thr}% 不进）",
            f"老师进场前有回撤（中位 {med_dd}%），追涨不买没跌过的",
            lambda r, _t=thr: (r.get("dd_from_high") is not None
                               and r["dd_from_high"] < _t),
            "teacher_entry:dd_from_high"))

    # ③ 量能：老师进场平均 vol_x 3.88（放量），极缩量不进
    med_vx = (dist.get("vol_x", {}).get("win", {}) or {}).get("median")
    if med_vx is not None:
        thr = round(med_vx * 0.4, 2)  # 老师中位 × 0.4 以下 = 量能不足
        cands.append((
            f"T3 量能不足不进（vol_x < {thr}x 不进）",
            f"老师进场量能中位 {med_vx}x（放量），缩量段不进",
            lambda r, _t=thr: (r.get("vol_x") is not None
                               and r["vol_x"] < _t),
            "teacher_entry:vol_x"))

    # ④ 顺势性：老师顺势胜率 vs 逆势胜率
    aw, cw = align.get("aligned_win_rate"), align.get("counter_win_rate")
    if aw is not None and cw is not None and aw > cw:
        cands.append((
            f"T4 只顺势（逆势不进：老师顺势胜率 {aw}% vs 逆势 {cw}%）",
            "老师顺势胜率显著更高 → 逆势段不进",
            lambda r: r.get("trend") is not None and (
                (r.get("side") == "long" and r["trend"] != "多头排列")
                or (r.get("side") == "short" and r["trend"] != "空头排列")),
            "teacher_entry:alignment"))

    # ⑤ 追单（跟生产 G 同口径，但用老师的风格阈值做对照）
    cands.append((
        "T5 追单不入场（chase < 0.2）",
        "老师进场多为回调买入，追高追跌段是风格外（与生产 G 同口径）",
        lambda r: r.get("chase") is not None and r["chase"] < 0.2,
        "teacher_entry:chase"))

    return cands


# ---------------------------------------------------------------- CLI
if __name__ == "__main__":
    prof = teacher_entry_profile()
    print(json.dumps({k: v for k, v in prof.items() if k != "rows"},
                     ensure_ascii=False, indent=1))
    print()
    print("=== 逐笔进场画像 ===")
    for r in prof.get("rows", []):
        print(f"  #{r['trade_id']:>2} {r.get('side','?'):5} "
              f"net={r.get('net_usd') or 0:>7.2f} "
              f"range={r.get('range_pos_pct') or '—':>6} "
              f"dd={r.get('dd_from_high_pct') or '—':>5} "
              f"vol={r.get('vol_x') or '—':>5} "
              f"trend={r.get('trend') or '—':>4} "
              f"aligned={r.get('aligned')}")
    print()
    print("=== 派生候选过滤规则 ===")
    for nm, desc, _pd, src in derive_entry_candidates([]):
        print(f"  {nm}\n    {desc}\n    来源: {src}")
