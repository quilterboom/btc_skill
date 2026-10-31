#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
行情大图 · K 线历史窗口（/api/market_chart）
============================================

2026-10-31 liusir：「价格主图能不能左右滑动？目前的数据最早是 10 月 5 号」
                「能不能更久点」

**问题根因**：主图数据走 `trader_view.fetch_candles`（Gate 实时 API），
单次上限 2000 根，且只取尾部。4h 图 160 根 = 26 天 → 最早只到 10-05。
用户想看更久的历史，但 API 给不了 —— **本地 K 线缓存里其实有**：

    4h   5000 根  2024-07-20 → 今天（833 天）
    1h   9145 根  2025-10-15 → 今天（381 天）
    15m  8000 根  2026-08-09 → 今天（ 83 天）
    （零缺口，已核对）

**修法**：主图改成「深历史 + 分页窗口 + 客户端平移」。

数据来源 = 本地缓存（深）+ Gate 实时 API（新鲜，含未收的那根），按 ts 合并。
实测两者在重叠的 1999 根上 OHLCV **逐字段完全一致**（maxdiff=0），
API 只多出「当前未收的那一根」。

**★ 关键设计：指标在全序列上算，不在窗口上算。**
EMA/RSI 是递归的，值只取决于「该时刻及之前的全部 K 线」。
所以只要永远基于**同一条深序列**计算，任何窗口切法得到的 EMA/RSI 完全一致 →
客户端可以自由拼接窗口（往左拼/往右拼/只刷新尾部），曲线**无缝**。
实测：全序列 EMA20 与「尾部 320 根」EMA20 差 0.0000%。

**★ 口径纪律**：`tfs` 里的分析结论（趋势/动能/波动/跨周期投票）**仍然走
`trader_view.fetch_candles` 实时 API**，一个字节都不改 —— 那些数字必须跟
每 15 分钟推的 Telegram 判读报告同源。本模块只管「画图用哪段历史」。

窗口语义：
    GET /api/market_chart?tf=4h&bars=600            → 最新 600 根
    GET /api/market_chart?tf=4h&bars=600&end=<ts>   → ts 之前的 600 根（往回翻）
返回 `has_more_before` 让前端知道还能不能继续往左。
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

SKILL_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SKILL_ROOT / "scripts"))

# 各周期默认拉多少根（客户端还会按需分页往左/右补）
DEFAULT_BARS = 600
MAX_BARS = 3000
# 允许的最大窗口跨度（防止 ?bars=99999 把整个深序列吐出去）
MAX_WINDOW = 3000

# 缓存：全序列（含指标）按周期缓存，TTL 30s（对齐 board 的刷新节奏）
_CACHE: dict = {"ts": 0.0, "data": {}}
_CACHE_TTL = 30.0

TF_LIST = ("4h", "1h", "15m")


def _round(v, nd=1):
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if f != f:  # NaN
        return None
    return round(f, nd)


def _clean(arr, nd=1):
    """numpy array → JSON list（NaN → None）"""
    return [_round(x, nd) for x in arr]


def _deep_bars(tf: str) -> list:
    """深历史 K 线（升序）= 本地缓存 + Gate 实时 API 合并。

    本地缓存提供深度（4h 5000 根 / 1h 9145 根 / 15m 8000 根），
    实时 API 提供「最新的一根（含未收）」以及缓存可能缺的最近几根。
    同 ts 以 API 为准（更新鲜）。
    """
    merged: dict[int, list] = {}

    # ---- 1) 本地缓存（深）----
    try:
        import gate_fetch
        import kline_store
        rows, _depth = kline_store.load_bars(gate_fetch.DATA_DIR, "BTC_USDT", tf)
        for r in rows:
            try:
                ts = int(r[0])
                merged[ts] = [ts, float(r[1]), float(r[2]), float(r[3]),
                              float(r[4]), float(r[5])]
            except (TypeError, ValueError, IndexError):
                continue
    except Exception:
        pass

    # ---- 2) Gate 实时 API（新鲜，含未收那根）----
    try:
        from trader_view import fetch_candles
        for b in fetch_candles("BTC_USDT", tf, limit=2000):
            try:
                ts = int(float(b["t"]))
                merged[ts] = [ts, float(b["o"]), float(b["h"]), float(b["l"]),
                              float(b["c"]), float(b.get("v") or 0)]
            except (KeyError, TypeError, ValueError):
                continue
    except Exception:
        pass

    return [merged[k] for k in sorted(merged)]


def _full_series(tf: str) -> dict:
    """全深序列 + 在全序列上算好的 EMA20/EMA50/RSI（带 30s 缓存）。"""
    now = time.time()
    cached = _CACHE["data"].get(tf)
    if cached and now - _CACHE["ts"] < _CACHE_TTL:
        return cached

    bars = _deep_bars(tf)
    if not bars:
        empty = {"t": [], "o": [], "h": [], "l": [], "c": [], "v": [],
                 "ema20": [], "ema50": [], "rsi": [],
                 "earliest_ts": None, "latest_ts": None}
        _CACHE["data"][tf] = empty
        _CACHE["ts"] = now
        return empty

    import numpy as np
    from indicators import ema, rsi

    closes = np.asarray([b[4] for b in bars], dtype=float)
    e20 = ema(closes, 20)
    e50 = ema(closes, 50)
    rs = rsi(closes, 14)

    series = {
        "t": [b[0] for b in bars],
        "o": [_round(b[1], 2) for b in bars],
        "h": [_round(b[2], 2) for b in bars],
        "l": [_round(b[3], 2) for b in bars],
        "c": [_round(b[4], 2) for b in bars],
        "v": [_round(b[5], 0) for b in bars],
        # ★ 在**全序列**上计算 → 任意窗口切法数值一致，拼接无缝
        "ema20": _clean(e20, 2),
        "ema50": _clean(e50, 2),
        "rsi": _clean(rs, 1),
        "earliest_ts": bars[0][0],
        "latest_ts": bars[-1][0],
    }
    _CACHE["data"][tf] = series
    _CACHE["ts"] = now
    return series


def build_chart_window(tf: str, bars: int = DEFAULT_BARS,
                       end_ts: int | None = None) -> dict:
    """切一个历史窗口出来。

    end_ts=None → 最新 bars 根
    end_ts=ts   → ts（含）之前的 bars 根（往回翻页用）

    返回里带 `has_more_before` / `has_more_after`：
    前端据此判断「还能不能继续往左/往右翻」。
    """
    tf = (tf or "").strip()
    if tf not in TF_LIST:
        return {"error": f"不支持的周期: {tf}", "tf": tf, "count": 0}

    try:
        bars = int(bars)
    except (TypeError, ValueError):
        bars = DEFAULT_BARS
    bars = max(50, min(MAX_BARS, bars))

    series = _full_series(tf)
    n = len(series["t"])
    if n == 0:
        return {"error": "K 线数据不可用", "tf": tf, "count": 0,
                "t": [], "o": [], "h": [], "l": [], "c": [], "v": [],
                "ema20": [], "ema50": [], "rsi": [],
                "earliest_ts": None, "latest_ts": None,
                "has_more_before": False, "has_more_after": False}

    if end_ts is None:
        hi = n
    else:
        try:
            end_ts = int(end_ts)
        except (TypeError, ValueError):
            end_ts = None
        if end_ts is None:
            hi = n
        else:
            # 找到第一个 > end_ts 的下标 → [0, hi) 是 end_ts 及之前
            import bisect
            hi = bisect.bisect_right(series["t"], end_ts)
    lo = max(0, hi - bars)

    out = {k: series[k][lo:hi] for k in
           ("t", "o", "h", "l", "c", "v", "ema20", "ema50", "rsi")}
    out.update({
        "tf": tf,
        "count": hi - lo,
        "first_ts": out["t"][0] if out["t"] else None,
        "last_ts": out["t"][-1] if out["t"] else None,
        "earliest_ts": series["earliest_ts"],
        "latest_ts": series["latest_ts"],
        "total": n,
        "has_more_before": lo > 0,
        "has_more_after": hi < n,
    })
    return out


def chart_meta() -> dict:
    """各周期深序列的覆盖范围（前端用来显示「最早能回溯到哪天」）。"""
    out = {}
    for tf in TF_LIST:
        s = _full_series(tf)
        out[tf] = {
            "total": len(s["t"]),
            "earliest_ts": s["earliest_ts"],
            "latest_ts": s["latest_ts"],
        }
    return out


if __name__ == "__main__":
    import datetime
    for tf in TF_LIST:
        w = build_chart_window(tf, bars=5)
        m = chart_meta()[tf]
        fmt = lambda t: datetime.datetime.fromtimestamp(t).strftime("%Y-%m-%d") if t else "—"
        print(f"{tf}: 总 {m['total']} 根  {fmt(m['earliest_ts'])} → {fmt(m['latest_ts'])}"
              f"  | 窗口 {w.get('count')} 根 {fmt(w.get('first_ts'))} → {fmt(w.get('last_ts'))}"
              f"  more_before={w.get('has_more_before')}")
