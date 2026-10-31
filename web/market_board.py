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

# 分析用的 K 线根数（2026-10-31 新增，两个 namespace 共用）
# ★ 下限 320：EMA200 需要 200 根热机 —— 原来 main namespace 只用 60 根，
#   算不出 EMA200，趋势判定跟 Telegram 判读对不上（实测 15m flat vs up）。
DEFAULT_TF_LIMITS = {"4h": 320, "1h": 480, "15m": 320}

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
    # ★ 2026-10-31：改走共享层 analyze_market() —— 原来这里自己 fetch + analyze_tf，
    #   而 index.html 那张卡走的是另一套自研实现 → 两张卡方向标签都能不一样。
    #   现在两边共用同一个分析器，数字必然一致（也和 Telegram 判读同源）。
    am = analyze_market(DEFAULT_TF_LIMITS)
    per_tf = am["per_tf"]
    out["tfs"] = am["tfs"]
    out["aggregate"] = am["aggregate"]
    for tf in TF_ORDER:
        n = BARS_PER_TF.get(tf, 160)
        bars = am["bars_by_tf"].get(tf) or []
        if not bars:
            out["chart"][tf] = {"t": [], "o": [], "h": [], "l": [], "c": [],
                                "v": [], "ema20": [], "ema50": [], "rsi": []}
            continue
        out["chart"][tf] = _chart_payload(bars, n)

    # ---------- 3. 跨周期投票（已在 analyze_market 里算好） ----------
    if out["aggregate"].get("error"):
        out["aggregate"] = {"error": out["aggregate"]["error"]}

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
    # ★ 2026-10-31：改用共享构造器 build_ma_payload()（两个 namespace 共用）。
    #   原来这里内联拼 payload，main namespace 那边是另一份手写实现 →
    #   两张卡的方向标签都能不一样。共享之后必然一致。
    out["ma_payload"] = build_ma_payload(
        tfs=out["tfs"], aggregate=out["aggregate"], last_px=last_px,
        ts_str=out["ts_str"], hi24=hi24, lo24=lo24, pos_in_range=pos_in_range,
        atr_pct=atr_pct, regime=regime, funding=out["funding"],
    )

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


# ============================================================
# 共享分析层（2026-10-31 新增）
# ============================================================
# 背景：页面上有**两张**都会出「交易员判读」的卡 ——
#   /market 的「交易员判读」卡（ns="board"）
#   / 主 dashboard 的「行情分析」卡（ns="main"）
# 它们共用同一个 LLM（_mm_advice），但**取数口径本来是分叉的**：
#   board：trader_view.analyze_tf（320/480 根，有 EMA200 + 摆动结构）
#   main ：自研 _trend_from（只有 60 根，无 EMA200、无结构）
# 实测同一时刻：15m 判成 flat vs up、1h 判成 down vs「反抽 EMA20（空）」、
#   4h 判成 down vs「空头排列」—— 两张卡给的方向标签就不一样。
#
# 现在统一到 analyze_tf（= 生产 Telegram 判读用的那套），两边必然一致。
def analyze_market(tf_limits: dict | None = None) -> dict:
    """多周期分析（统一口径）。返回 {per_tf, tfs, aggregate, last_px}。

    tf_limits: {"4h": 320, "1h": 480, "15m": 320}，缺省用 DEFAULT。
    ★ 根数下限 320 —— EMA200 需要 200 根热机，60 根算不出 EMA200。
    """
    import trader_view as tv

    limits = dict(DEFAULT_TF_LIMITS)
    if tf_limits:
        limits.update(tf_limits)

    per_tf = {}
    tfs = {}
    bars_by_tf = {}
    last_px = None
    try:
        tk = tv.fetch_ticker("BTC_USDT")
        if tk:
            last_px = float(tk.get("last") or 0) or None
    except Exception:
        pass

    for tf in TF_ORDER:
        n = limits.get(tf, 320)
        try:
            bars = _fetch_bars(tf, n)
        except Exception:
            bars = []
        bars_by_tf[tf] = bars
        if not bars:
            per_tf[tf] = {"tf": tf, "error": "K 线获取失败"}
            tfs[tf] = {"error": "K 线获取失败"}
            continue
        px = last_px if last_px else float(bars[-1]["c"])
        try:
            an = tv.analyze_tf(tf, bars, px)
        except Exception as e:
            an = {"tf": tf, "error": f"指标计算失败：{e}"}
        per_tf[tf] = an
        tfs[tf] = _tf_summary(an)

    try:
        agg = tv.aggregate_bias(per_tf)
    except Exception:
        agg = {}
    aggregate = {
        "bias": agg.get("bias"),
        "norm": agg.get("norm"),
        "score": agg.get("score"),
        "consensus": agg.get("consensus"),
        "consensus_score": agg.get("consensus_score"),
        "rows": agg.get("rows") or [],
    }

    return {"per_tf": per_tf, "tfs": tfs, "aggregate": aggregate,
            "bars_by_tf": bars_by_tf, "last_px": last_px}


def build_ma_payload(tfs: dict, aggregate: dict, last_px, ts_str: str,
                     hi24=None, lo24=None, pos_in_range=None,
                     atr_pct=None, regime="normal", funding=None) -> dict:
    """喂给 AI 判读用的 payload（**两个 namespace 共用这一份构造逻辑**）。

    ★ 2026-10-31 扩充：原来只喂 12 个数，导致两个具体问题：
      ① 判读与同屏「多周期结构」卡打架 —— trend 只给 up/down/flat，
         而这个 down 来自 trend_score（只看 EMA 排列 + 价格位置），
         **摆动结构（上涨段/下跌段）根本没进 prompt** → LLM 顺着 "down"
         编出"下跌中继"，而同屏结构卡写的是"上涨段·末段"。
      ② 关键位幻觉 —— 没给摆动高低点，LLM 自己补了个 75000。
      现在把 analyze_tf 已经算好的结构/动能/量能/投票一起喂进去
      （**零额外请求**，全是现成结果）。
    """
    def _struct(tf):
        s = tfs.get(tf) or {}
        return {
            "label": s.get("structure"),
            "bias": s.get("structure_bias"),
            "pos_in_leg": s.get("pos_in_leg"),
            "lo": s.get("swing_lo"),
            "hi": s.get("swing_hi"),
        }

    def _mom(tf):
        s = tfs.get(tf) or {}
        return {
            "macd": s.get("macd_state"),
            "rsi": s.get("rsi"),
            "rsi_state": s.get("rsi_state"),
        }

    def _vol(tf):
        s = tfs.get(tf) or {}
        return {"regime": s.get("vol_regime"), "ratio": s.get("vol_ratio")}

    return {
        "ts_str": ts_str,
        "last_px": last_px,
        "trend": {
            "15m": _bias_word(tfs.get("15m", {})),
            "1h": _bias_word(tfs.get("1h", {})),
            "4h": _bias_word(tfs.get("4h", {})),
            "convergence": _convergence(tfs),
            "ema20_1h": tfs.get("1h", {}).get("ema20"),
            "ema50_1h": tfs.get("1h", {}).get("ema50"),
            "ema20_4h": tfs.get("4h", {}).get("ema20"),
            "ema50_4h": tfs.get("4h", {}).get("ema50"),
        },
        "structure": {tf: _struct(tf) for tf in ("15m", "1h", "4h")},
        "momentum": {tf: _mom(tf) for tf in ("15m", "1h", "4h")},
        "volume": {tf: _vol(tf) for tf in ("15m", "1h", "4h")},
        "voting": {
            "bias": (aggregate or {}).get("bias"),
            "norm": (aggregate or {}).get("norm"),
            "score": (aggregate or {}).get("score"),
            "consensus": (aggregate or {}).get("consensus"),
        },
        "volatility": {"atr_pct": atr_pct, "regime": regime},
        "key_levels": {
            "high_24h": hi24, "low_24h": lo24,
            "dist_to_high_pct": _round((hi24 - last_px) / last_px * 100, 2) if hi24 and last_px else None,
            "dist_to_low_pct": _round((last_px - lo24) / last_px * 100, 2) if lo24 and last_px else None,
            "near_resistance": bool(hi24 and last_px and 0 < (hi24 - last_px) / last_px * 100 < 0.5),
            "near_support": bool(lo24 and last_px and 0 < (last_px - lo24) / last_px * 100 < 0.5),
            "pos_in_range_pct": pos_in_range,
        },
        "funding": funding or {},
    }
