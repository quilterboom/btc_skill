#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
cost_basis_view.py —— 净值展示口径公共函数（2026-11-04）

## 为什么需要它
hammer 秒级市价成交会**追价**（实测 +300~400 点），而账本里的 `net_usd`
是按**计划 entry** 记的（口径陈旧）。回填脚本把按真实成交价 canonical 重算
的结果写进 `net_usd_fill_basis`（`net_usd` 不改写，保留审计线索）。

于是同一笔单有两个净值：
    账面 net_usd          = 旧口径（按计划 entry）
    可信 net_usd_fill_basis = 新口径（按真实成交价）

所有**展示/统计**层必须用可信值，否则账面虚高会一路传导到看板、日报、
日历、边缘分。原始 `net_usd` 通过 `net_usd_book` 露出，供三段式对照。

## 零依赖（只用标准库）
`position_tracker` 有 import 副作用，web/ 与 scripts/ 都直接 import 本模块，
避免为了一个纯函数把整个 tracker 拖进来。

用法：
    from cost_basis_view import effective_net, net_of, basis_diff
    v = effective_net(rec)          # fill_basis 优先，账面兜底
"""
from __future__ import annotations
from typing import Any, Dict, Optional


def effective_net(rec: Dict[str, Any]) -> Optional[float]:
    """★ 净值取真实成本基准（fill_basis 优先，账面兜底）。

    - 有 `net_usd_fill_basis` → 用它（回填过，口径可信）
    - 没有 → 用 `net_usd`（正常单，两口径一致）
    - 都没有 → None（未结算，别当 0）
    """
    if not isinstance(rec, dict):
        return None
    v = rec.get("net_usd_fill_basis")
    if v is None:
        v = rec.get("net_usd")
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def net_of(rec: Dict[str, Any], default: float = 0.0) -> float:
    """同 effective_net，但 None → default（用于求和）。"""
    v = effective_net(rec)
    return default if v is None else v


def book_net(rec: Dict[str, Any]) -> float:
    """账面净值（原始 net_usd，None → 0）。"""
    if not isinstance(rec, dict):
        return 0.0
    try:
        return float(rec.get("net_usd") or 0)
    except (TypeError, ValueError):
        return 0.0


def basis_diff(rec: Dict[str, Any]) -> float:
    """可信 − 账面（口径差）。"""
    v = effective_net(rec)
    if v is None:
        return 0.0
    return round(v - book_net(rec), 2)


def is_stale(rec: Dict[str, Any]) -> bool:
    """该记录是否属于「口径陈旧」（回填过 → 账面值不可信）。"""
    return bool(isinstance(rec, dict) and rec.get("net_usd_fill_basis") is not None)


def basis_triplet(records) -> Dict[str, float]:
    """批量三段式：{book, true, diff, stale_n}。

    book = 原始 net_usd 合计；true = fill_basis 优先合计；diff = true − book。
    """
    recs = list(records or [])
    book = round(sum(book_net(r) for r in recs), 2)
    true = round(sum(net_of(r) for r in recs), 2)
    return {
        "book": book,
        "true": true,
        "diff": round(true - book, 2),
        "stale_n": sum(1 for r in recs if is_stale(r)),
    }
