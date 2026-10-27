#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
era_split.py — 策略「代」口径统一模块（2026-10-26 新增）

为什么需要
----------
生产 journal 里混着**两代参数完全不同的策略**：

  · 旧代 = 1h 主信号（10-02 ~ 10-10）   35 笔已结算  胜率 25.7%  净 -92.48U  盈亏比 0.47
  · 新代 = 15m 形态信号（10-11 起）      21 笔已结算  胜率 52.4%  净 +81.48U  盈亏比 1.73

把两代混算，全局数字永远是「当前代赚钱 / 被旧代拖成负数」的混合体：
  你看到的 -14.75U / 胜率 34.5% / 边缘分 38，全是混合口径。
  **当前代本身是赚钱的**，只是被旧代拖累。

划分依据（不硬编码日期）
------------------------
1. `signal_source` 字段：新代有值（15m_pattern / left_side_oversold_bounce），
   旧代是 None（10-17 之前没这个字段）。
2. `strategy_version` 字段：10-19 起新增，标 v1_prod / v2_test。
3. 兜底：都没有 → 按 `ts` 落到 fallback 分界（可配置）。

用法
----
    from era_split import split_by_era, ERA_LABELS
    groups = split_by_era(trades)        # {'legacy_1h_main': [...], 'current_15m': [...]}
    cur = groups.get('current_15m', [])
"""
from __future__ import annotations
from typing import Dict, List, Optional

# 新代（15m 形态 + hammer）的 signal_source 取值
CURRENT_SIGNAL_SOURCES = ("15m_pattern", "left_side_oversold_bounce")

# 兜底分界：2026-10-11 00:00 CST（15m 形态信号首次落 journal 的日期）
FALLBACK_SPLIT_TS = 1791600000

ERA_LABELS = {
    "legacy_1h_main": "旧代 1h 主信号",
    "current_15m": "当前代 15m 形态",
}


def _is_settled(t: Dict) -> bool:
    """已结算且有净值 —— 统计口径的唯一准入条件。

    注意：outcome 也要有。journal.py 的旧引擎写的是 SL / SL*（异类词表），
    只认 position_tracker 的词表（BE_STOPPED / SL_STOPPED / TP2_DONE / ...）。"""
    return (t.get("status") == "settled"
            and t.get("net_usd") is not None
            and t.get("outcome") is not None)


def era_of(t: Dict, fallback_ts: int = FALLBACK_SPLIT_TS) -> str:
    """判定单笔属于哪一代。"""
    src = t.get("signal_source")
    if src in CURRENT_SIGNAL_SOURCES:
        return "current_15m"
    # strategy_version 有值 → 一定是新代码路径（10-19 之后）
    if (t.get("meta") or {}).get("strategy_version"):
        return "current_15m"
    if src is None and int(t.get("ts") or 0) < fallback_ts:
        return "legacy_1h_main"
    # 有 signal_source 但不是新代取值（如 1h_main）→ 按 ts 兜底
    return "legacy_1h_main" if int(t.get("ts") or 0) < fallback_ts else "current_15m"


def split_by_era(trades: List[Dict], fallback_ts: int = FALLBACK_SPLIT_TS) -> Dict[str, List[Dict]]:
    """按代切分**已结算**的交易。

    返回: {'legacy_1h_main': [...], 'current_15m': [...]}
    """
    out: Dict[str, List[Dict]] = {}
    for t in trades or []:
        if not _is_settled(t):
            continue
        out.setdefault(era_of(t, fallback_ts), []).append(t)
    return out


def era_summary(trades: List[Dict], fallback_ts: int = FALLBACK_SPLIT_TS) -> List[Dict]:
    """分代汇总（给 dashboard / 报表用）。

    返回: [{'era':..., 'label':..., 'n':..., 'win_rate':..., 'net_usd':...,
            'profit_factor':..., 'avg_win_loss':...}, ...]
    """
    groups = split_by_era(trades, fallback_ts)
    rows = []
    for era in ("current_15m", "legacy_1h_main"):
        g = groups.get(era) or []
        if not g:
            continue
        nets = [float(t["net_usd"]) for t in g]
        wins = [n for n in nets if n > 0]
        losses = [n for n in nets if n < 0]
        gp = sum(wins)
        gl = -sum(losses)
        n_win, n_loss = len(wins), len(losses)
        rows.append({
            "era": era,
            "label": ERA_LABELS.get(era, era),
            "n": len(g),
            "win_rate": round(n_win / len(g) * 100, 1) if g else 0.0,
            "net_usd": round(sum(nets), 2),
            "profit_factor": round(gp / gl, 2) if gl > 0 else (round(gp, 2) if gp > 0 else 0.0),
            "avg_win_loss": round((gp / n_win) / (gl / n_loss), 2) if n_win and n_loss else 0.0,
            "max_win": round(max(nets), 2) if nets else 0.0,
            "max_loss": round(min(nets), 2) if nets else 0.0,
        })
    return rows


def split_by_version(trades: List[Dict]) -> Dict[str, List[Dict]]:
    """按 strategy_version 切分已结算交易（v1_prod vs v2_test A/B 对照用）。

    没写 strategy_version 的归到 'unlabeled' —— **不要**把它们算进任何一组，
    否则 A/B 对照会被旧数据污染。
    """
    out: Dict[str, List[Dict]] = {}
    for t in trades or []:
        if not _is_settled(t):
            continue
        ver = (t.get("meta") or {}).get("strategy_version") or "unlabeled"
        out.setdefault(ver, []).append(t)
    return out
