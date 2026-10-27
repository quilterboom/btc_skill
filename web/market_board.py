#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
行情大图数据源（/market 页面）
================================

2026-10-27 liusir：「网页版展示的很乱，我需要是那种一眼就能知道当前市场环境的数据大图」。

设计原则（跟 web-dashboard-ui skill 一致）：
- 一页只讲一件事：这个页面只讲「当前市场环境」，不掺策略绩效 / 交易明细。
- 数字必须能溯源：**全部复用 scripts/trader_view.py 的分析引擎**，
  所以图上显示的趋势 / 动能 / 波动 / 结构 / 跨周期投票，
  跟每 15 分钟推的 Telegram 判读报告是同一套算法、同一份数据。
  不另起一套「网页专用」的启发式（那会导致两处数字打架）。
- 拿不到真实数据就不渲染：任一周期数据不足 → 该周期显示「数据不足」，
  不用假数据凑数。

输出结构见 build_market_board() 的 docstring。
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

SKILL_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SKILL_ROOT / "scripts"))

# 图表每个周期画多少根 K 线（够看结构，又不让 payload 膨胀）
BARS_PER_TF = {"4h": 160, "1h": 240, "15m": 160}
TF_ORDER = ["4h", "1h", "15m"]

# 缓存：K 线 + 指标整体缓存 60s（多标签页 / 频繁刷新别把 gate 打爆）
_CACHE: dict = {"ts": 0.0, "data": None}
_CACHE_TTL = 60.0


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


def _clean_series(arr, nd=1):
    """numpy array → JSON list（NaN → None）"""
    return [_round(x, nd) for x in arr]


def _fetch_bars(interval: str, limit: int):
    """拉 K 线 → 升序 list[dict]，含未收的最后一根（实时图要看到当前这根）。"""
    from trader_view import fetch_candles
    raw = fetch_candles("BTC_USDT", interval, limit=limit)
    bars = []
    for b in raw:
        try:
            bars.append({
                "t": int(float(b["t"])),
                "o": float(b["o"]), "h": float(b["h"]),
                "l": float(b["l"]), "c": float(b["c"]),
                "v": float(b.get("v", 0)),
            })
        except (KeyError, TypeError, ValueError):
            continue
    bars.sort(key=lambda x: x["t"])
    return bars


def _ema_series(closes, n: int):
    """EMA 序列（用 indicators.ema，跟 trader_view 内部同一套 → 数字一致）"""
    import numpy as np
    from indicators import ema
    arr = np.asarray(closes, dtype=float)
    if len(arr) < n:
        return [None] * len(arr)
    return _clean_series(ema(arr, n), 2)


def _chart_payload(bars, n_bars: int):
    """给前端画图用的精简序列"""
    import numpy as np
    from indicators import rsi
    tail = bars[-n_bars:] if len(bars) > n_bars else bars
    closes = [b["c"] for b in tail]
    e20 = _ema_series(closes, 20)
    e50 = _ema_series(closes, 50)
    rs = _clean_series(rsi(np.asarray(closes, dtype=float), 14), 1) if len(closes) >= 15 \
        else [None] * len(closes)
    return {
        "t": [b["t"] for b in tail],
        "o": [_round(b["o"], 2) for b in tail],
        "h": [_round(b["h"], 2) for b in tail],
        "l": [_round(b["l"], 2) for b in tail],
        "c": [_round(b["c"], 2) for b in tail],
        "v": [_round(b["v"], 0) for b in tail],
        "ema20": e20,
        "ema50": e50,
        "rsi": rs,
    }


def _tf_summary(an: dict) -> dict:
    """把 analyze_tf 的原始输出压成前端要的字段（中文标签在前端做，这里只给原始值）"""
    if "error" in an:
        return {"error": an["error"]}

    trend = an.get("trend_ema", {})
    macd = an.get("macd", {})
    rsi_d = an.get("mom_rsi", {})
    atr_d = an.get("vol_atr", {})
    bb_d = an.get("vol_bb", {})
    kc_d = an.get("vol_kc", {})
    vol_d = an.get("aux_vol", {})
    swing = an.get("swing", {})
    sess = an.get("aux_sess", {})

    return {
        "px": _round(an.get("px"), 2),
        "n_bars": an.get("n_bars"),
        "first_ts": an.get("first_ts"),
        "last_ts": an.get("last_ts"),
        "high_period": an.get("high_period"),
        "low_period": an.get("low_period"),
        # 趋势
        "trend": trend.get("trend"),
        "trend_score": trend.get("score"),
        "ema20": trend.get("ema20"),
        "ema50": trend.get("ema50"),
        "ema200": trend.get("ema200"),
        "px_vs_ema20": trend.get("px_vs_ema20"),
        # 动能
        "macd_state": macd.get("state"),
        "macd_score": macd.get("score"),
        "macd_hist": macd.get("hist"),
        "rsi": rsi_d.get("rsi"),
        "rsi_state": rsi_d.get("state"),
        "rsi_score": rsi_d.get("score"),
        # 波动
        "atr": atr_d.get("atr"),
        "atr_pct": atr_d.get("pct"),
        "atr_regime": atr_d.get("regime"),
        "atr_ratio": atr_d.get("ratio"),
        "bb_state": bb_d.get("state"),
        "bb_position": bb_d.get("position"),
        "bb_bandwidth": bb_d.get("bandwidth_pct"),
        "kc_state": kc_d.get("state"),
        "kc_squeeze": kc_d.get("squeeze"),
        # 量能
        "vol_regime": vol_d.get("regime"),
        "vol_ratio": vol_d.get("ratio"),
        # 结构
        "structure": swing.get("label"),
        "structure_bias": swing.get("bias"),
        "pos_in_leg": swing.get("pos_in_leg"),
        "swing_note": swing.get("note"),
        "swing_kind": (swing.get("current_swing") or {}).get("kind"),
        "swing_lo": (swing.get("current_swing") or {}).get("lo_px"),
        "swing_hi": (swing.get("current_swing") or {}).get("hi_px"),
        "retracements": swing.get("retracements") or {},
        "session": sess.get("name"),
        # 三个维度总分（画堆叠条）
        "score_trend": an.get("main_a_score"),
        "score_momentum": an.get("main_b_score"),
        "score_volatility": an.get("main_c_score"),
    }


def build_market_board(force: bool = False) -> dict:
    """行情大图完整数据。

    返回 {
      "ts", "ts_str",
      "price":   {last, chg_24h_pct, high_24h, low_24h, pos_in_range_pct, volume_24h, open_interest},
      "chart":   {"4h": {...}, "1h": {...}, "15m": {...}},   # 画图用
      "tfs":     {"4h": {...}, ...},                          # 每周期指标摘要
      "aggregate": {bias, norm, consensus, rows},             # 跨周期投票
      "volatility": {atr_pct, regime},                        # 1h ATR 口径
      "funding": {current, previous, sign},
      "gate":    {…v2 开仓闸门…},
      "ma_payload": {...},                                    # 喂给 AI 判读用
      "error":   str | None
    }
    """
    now = time.time()
    if not force and _CACHE["data"] and now - _CACHE["ts"] < _CACHE_TTL:
        return _CACHE["data"]

    out = {
        "ts": int(now),
        "ts_str": time.strftime("%Y-%m-%d %H:%M:%S"),
        "price": {}, "chart": {}, "tfs": {},
        "aggregate": {}, "volatility": {}, "funding": {}, "gate": {},
        "ma_payload": {},
        "error": None,
    }

    try:
        import trader_view as tv
    except Exception as e:
        out["error"] = f"分析引擎不可用：{e}"
        return out

    # ---------- 1. ticker ----------
    last_px = None
    hi24 = lo24 = vol24 = oi = None
    chg24 = None
    try:
        tk = tv.fetch_ticker("BTC_USDT")
        if tk:
            last_px = float(tk.get("last") or 0) or None
            hi24 = _round(tk.get("high_24h"), 1)
            lo24 = _round(tk.get("low_24h"), 1)
            vol24 = _round(tk.get("volume_24h"), 0)
            oi = _round(tk.get("total_size"), 0)
            try:
                chg24 = _round(float(tk.get("change_percentage")), 2)
            except (TypeError, ValueError):
                chg24 = None
    except Exception as e:
        out["error"] = f"行情获取失败：{e}"
        return out

    if not last_px:
        out["error"] = "行情获取失败：价格为空"
        return out

    pos_in_range = None
    if hi24 and lo24 and hi24 > lo24:
        pos_in_range = _round((last_px - lo24) / (hi24 - lo24) * 100, 1)
    out["price"] = {
        "last": _round(last_px, 2),
        "chg_24h_pct": chg24,
        "high_24h": hi24, "low_24h": lo24,
        "pos_in_range_pct": pos_in_range,
        "volume_24h": vol24,
        "open_interest": oi,
    }

    # ---------- 2. 多周期 K 线 + 指标 ----------
    per_tf = {}
    for tf in TF_ORDER:
        n = BARS_PER_TF.get(tf, 160)
        try:
            # 多拉一些给 EMA200 热机，图表只画最后 n 根
            bars = _fetch_bars(tf, max(n * 2, 320))
        except Exception as e:
            out["tfs"][tf] = {"error": f"K 线获取失败：{e}"}
            out["chart"][tf] = {"t": [], "o": [], "h": [], "l": [], "c": [],
                                "v": [], "ema20": [], "ema50": [], "rsi": []}
            continue
        out["chart"][tf] = _chart_payload(bars, n)
        try:
            per_tf[tf] = tv.analyze_tf(tf, bars, last_px)
        except Exception as e:
            per_tf[tf] = {"tf": tf, "error": f"指标计算失败：{e}"}
        out["tfs"][tf] = _tf_summary(per_tf[tf])

    # ---------- 3. 跨周期投票 ----------
    try:
        agg = tv.aggregate_bias(per_tf)
        out["aggregate"] = {
            "bias": agg.get("bias"),
            "norm": agg.get("norm"),
            "score": agg.get("score"),
            "consensus": agg.get("consensus"),
            "consensus_score": agg.get("consensus_score"),
            "rows": agg.get("rows") or [],
        }
    except Exception as e:
        out["aggregate"] = {"error": f"跨周期投票失败：{e}"}

    # ---------- 4. 波动率（1h ATR 口径）----------
    atr_1h = out["tfs"].get("1h", {})
    atr_pct = atr_1h.get("atr_pct")
    regime = "normal"
    if atr_pct is not None:
        if atr_pct < 0.30:
            regime = "low"
        elif atr_pct > 1.00:
            regime = "high"
    out["volatility"] = {"atr_pct": atr_pct, "atr_regime": regime,
                         "atr_ratio": atr_1h.get("atr_ratio")}

    # ---------- 5. 资金费率 ----------
    try:
        from gate_fetch import fetch_funding_rate
        fr = fetch_funding_rate("BTC_USDT", limit=3)
        cur = prev = None
        if len(fr) >= 1:
            cur = float(fr[0].get("r", 0))
        if len(fr) >= 2:
            prev = float(fr[1].get("r", 0))
        sign = "neutral"
        if cur is not None:
            if cur > 0.0001:
                sign = "long_pay"
            elif cur < -0.0001:
                sign = "short_pay"
        out["funding"] = {"current": cur, "previous": prev, "sign": sign}
    except Exception as e:
        out["funding"] = {"error": f"资金费率获取失败：{e}"}

    # ---------- 6. v2 开仓闸门 ----------
    # 直接读 v2_journal，不 import server（server.py 以 __main__ 运行，
    # from server import ... 会把整个模块二次加载一份，缓存和状态都会裂开）。
    try:
        import v2_journal
        st = v2_journal.gate_status()
        st["ok"] = True
        out["gate"] = st
    except Exception as e:
        out["gate"] = {"ok": False, "error": f"闸门状态读取失败：{e}",
                       "vol_5d_pct": None, "vol_min_pct": None,
                       "vol_pass": None, "blocked_24h": None}

    # ---------- 7. AI 判读用的 payload（数字必须跟图上一致） ----------
    t = out["tfs"]
    out["ma_payload"] = {
        "ts_str": out["ts_str"],
        "last_px": last_px,
        "trend": {
            "15m": _bias_word(t.get("15m", {})),
            "1h": _bias_word(t.get("1h", {})),
            "4h": _bias_word(t.get("4h", {})),
            "convergence": _convergence(t),
            "ema20_1h": t.get("1h", {}).get("ema20"),
            "ema50_1h": t.get("1h", {}).get("ema50"),
            "ema20_4h": t.get("4h", {}).get("ema20"),
            "ema50_4h": t.get("4h", {}).get("ema50"),
        },
        "volatility": {"atr_pct": atr_pct, "regime": regime},
        "key_levels": {
            "high_24h": hi24, "low_24h": lo24,
            "dist_to_high_pct": _round((hi24 - last_px) / last_px * 100, 2) if hi24 else None,
            "dist_to_low_pct": _round((last_px - lo24) / last_px * 100, 2) if lo24 else None,
            "near_resistance": bool(hi24 and 0 < (hi24 - last_px) / last_px * 100 < 0.5),
            "near_support": bool(lo24 and 0 < (last_px - lo24) / last_px * 100 < 0.5),
        },
        "funding": out["funding"],
    }

    _CACHE["data"] = out
    _CACHE["ts"] = now
    return out


def _bias_word(tf: dict) -> str:
    """把 trend 判定映射成 up / down / flat（跟 build_market_analysis 同口径）"""
    if not tf or tf.get("error"):
        return "flat"
    score = tf.get("trend_score")
    if score is None:
        return "flat"
    if score >= 2:
        return "up"
    if score <= -2:
        return "down"
    # ±1 → 看 EMA20 与 EMA50 相对位置细分
    e20, e50 = tf.get("ema20"), tf.get("ema50")
    if e20 is not None and e50 is not None:
        if e20 > e50:
            return "up"
        if e20 < e50:
            return "down"
    return "flat"


def _convergence(t: dict) -> str:
    words = [_bias_word(t.get(tf, {})) for tf in TF_ORDER]
    if len(set(words)) == 1:
        return "converging" if words[0] != "flat" else "flat"
    return "diverging"
