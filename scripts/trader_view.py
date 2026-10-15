#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
交易员视角的多周期综合分析报告 — v2 (liusir 2026-10-13 重构)
=========================================================

3 个主维度（liusir 拍板）:
  A. 趋势 (Trend):     EMA20/50/200 排列 + MACD 动能 + swing 结构
  B. 动量 (Momentum):  RSI(14) + TD Sequential + Stochastic RSI
  C. 波动率 (Volatility): ATR(14) + Bollinger Bands + Keltner 通道

辅维度（佐证用）:
  - 量能 (vol regime: 放量/温和/缩量)
  - 关键位 (Fibonacci pivot / swing)
  - 老师单偏置 (teacher_bias.py)
  - 时段 (亚/欧/美盘)

跨周期: 4h > 1h > 15m
综合判定: 主维度投票 → long / short / neutral

用法:
  python3 trader_view.py                          # 当前行情，打印
  python3 trader_view.py --telegram               # 推送到 Telegram
  python3 trader_view.py --backtest 30            # 回测 30 天
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from indicators import (
    ema, sma, rsi, atr, td_setup, td_signal,
    swing_pivots, last_swing_leg, fib_retr, fib_retr_up,
)
import elliott_wave as ew


# ============================ v2.1 稳定配置 (liusir 10-13) ============================
# 4 个针对亏钱模式的参数改动 — 三段交叉验证通过 (累计 +0.5%)
# 1) norm 阈值 0.15 → 0.20 (更严)
# 2) consensus 不一致 → norm 上限 0.05 (避免拉锯)
# 3) 4h trend 反向 → norm 上限 0.10 (避免逆大势)
# 4) RSI 25-40 改为中性 (避免超卖假信号)
V2_1_BASELINE = {
    "norm_threshold": 0.20,        # bias 判定门槛
    "consensus_cap": 0.05,         # 不一致时 norm 收紧到这个
    "trend_4h_cap": 0.10,          # 4h 反向时 norm 收紧到这个
    "rsi_neutralize_low": 25,      # RSI < 这个才算超卖抄底
    "rsi_neutralize_high": 40,     # RSI > 这个才算偏空
}

# ============================ 数据获取 ============================
GATE_BASE = "https://api.gateio.ws/api/v4/futures/usdt"


def fetch_candles(contract: str, interval: str, limit: int = 1000) -> List[dict]:
    """实时从 gate.io 拉 K 线"""
    url = f"{GATE_BASE}/candlesticks?contract={contract}&interval={interval}&limit={limit}"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())


def fetch_ticker(contract: str) -> dict:
    url = f"{GATE_BASE}/tickers?contract={contract}"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())[0]


def bars_to_arrays(bars: List[dict]):
    opens = np.array([float(b["o"]) for b in bars])
    highs = np.array([float(b["h"]) for b in bars])
    lows = np.array([float(b["l"]) for b in bars])
    closes = np.array([float(b["c"]) for b in bars])
    vols = np.array([float(b["v"]) for b in bars])
    tss = np.array([int(float(b["t"])) for b in bars])
    return opens, highs, lows, closes, vols, tss


# ============================ 新指标: Bollinger / Keltner / StochRSI ============================
def bollinger(closes: np.ndarray, n: int = 20, k: float = 2.0) -> Dict:
    """布林带 (20, 2σ)"""
    if len(closes) < n:
        return {"upper": None, "mid": None, "lower": None, "bandwidth_pct": None,
                "position": None, "state": "数据不足"}
    mid = sma(closes, n)
    std = np.array([np.std(closes[max(0, i - n + 1):i + 1], ddof=1) for i in range(len(closes))])
    upper = mid + k * std
    lower = mid - k * std
    px = closes[-1]
    band_width = (upper[-1] - lower[-1]) / mid[-1] * 100
    # 位置 0=下轨 0.5=中轨 1=上轨
    pos = (px - lower[-1]) / (upper[-1] - lower[-1]) if upper[-1] > lower[-1] else 0.5
    # 状态
    if px >= upper[-1]:
        state = "突破上轨"
    elif px <= lower[-1]:
        state = "跌破下轨"
    elif pos > 0.8:
        state = "偏上轨"
    elif pos < 0.2:
        state = "偏下轨"
    else:
        state = "中轨附近"
    return {
        "upper": round(float(upper[-1]), 2),
        "mid": round(float(mid[-1]), 2),
        "lower": round(float(lower[-1]), 2),
        "bandwidth_pct": round(float(band_width), 3),
        "position": round(float(pos), 3),
        "state": state,
    }


def keltner(highs: np.ndarray, lows: np.ndarray, closes: np.ndarray,
            n_ema: int = 20, n_atr: int = 14, k: float = 1.5) -> Dict:
    """Keltner 通道 (20 EMA, 1.5 ATR)"""
    if len(closes) < max(n_ema, n_atr):
        return {"upper": None, "mid": None, "lower": None, "squeeze": None,
                "state": "数据不足"}
    mid = ema(closes, n_ema)
    a = atr(highs, lows, closes, n_atr)
    upper = mid + k * a
    lower = mid - k * a
    px = closes[-1]
    # squeeze: Bollinger 在 Keltner 内部 = 波动收敛
    band = bollinger(closes, 20, 2.0)
    if band["upper"] is not None:
        squeeze = "收敛中" if band["upper"] < upper[-1] and band["lower"] > lower[-1] else "未收敛"
    else:
        squeeze = "未知"
    if px >= upper[-1]:
        state = "突破上轨"
    elif px <= lower[-1]:
        state = "跌破下轨"
    else:
        state = "通道内"
    return {
        "upper": round(float(upper[-1]), 2),
        "mid": round(float(mid[-1]), 2),
        "lower": round(float(lower[-1]), 2),
        "squeeze": squeeze,
        "state": state,
    }


def stoch_rsi(closes: np.ndarray, n: int = 14, k: int = 3, d: int = 3) -> Dict:
    """Stochastic RSI (14, 3, 3)"""
    if len(closes) < n + 1:
        return {"k": None, "d": None, "state": "数据不足"}
    r = rsi(closes, n)
    valid = r[~np.isnan(r)]
    if len(valid) < k:
        return {"k": None, "d": None, "state": "数据不足"}
    # StochRSI = (RSI - min(RSI,k)) / (max(RSI,k) - min(RSI,k))
    stoch = np.full_like(r, np.nan)
    for i in range(k - 1, len(valid)):
        window = valid[i - k + 1:i + 1]
        lo, hi = window.min(), window.max()
        if hi == lo:
            stoch[i + (len(r) - len(valid))] = 50.0
        else:
            stoch[i + (len(r) - len(valid))] = (r[i + (len(r) - len(valid))] - lo) / (hi - lo) * 100
    # %K = stoch; %D = SMA(K, 3)
    valid_stoch = stoch[~np.isnan(stoch)]
    if len(valid_stoch) < d:
        return {"k": round(float(stoch[-1]), 2), "d": None, "state": "数据不足"}
    k_val = float(stoch[-1])
    d_val = float(np.mean(valid_stoch[-d:]))
    # 状态
    if k_val >= 80:
        state = "超买"
    elif k_val <= 20:
        state = "超卖"
    elif k_val > d_val:
        state = "K>D (看涨)"
    else:
        state = "K<D (看跌)"
    return {"k": round(k_val, 2), "d": round(d_val, 2), "state": state}


# ============================ 三大主维度函数 ============================
def trend_ema_alignment(closes: np.ndarray) -> Dict:
    """A. 趋势 — EMA20/50/200 排列"""
    if len(closes) < 50:
        return {"trend": "数据不足 (<50)", "ema20": None, "ema50": None, "ema200": None,
                "px_vs_ema20": None, "score": 0}
    e20 = ema(closes, 20)[-1]
    e50 = ema(closes, 50)[-1] if len(closes) >= 50 else None
    e200 = ema(closes, 200)[-1] if len(closes) >= 200 else None
    px = closes[-1]
    score = 0
    if e200 is not None and e20 > e50 > e200 and px > e20:
        trend, score = "多头排列", +2
    elif e200 is not None and e20 < e50 < e200 and px < e20:
        trend, score = "空头排列", -2
    elif e20 > e50 and px < e20:
        trend, score = "回踩 EMA20 (多)", +1
    elif e20 < e50 and px > e20:
        trend, score = "反抽 EMA20 (空)", -1
    elif e200 is None:
        trend = "EMA20>50 多" if e20 > e50 else "EMA20<50 空"
        score = +1 if e20 > e50 else -1
    else:
        trend, score = "震荡/缠绕", 0
    return {
        "trend": trend,
        "ema20": round(float(e20), 2),
        "ema50": round(float(e50), 2) if e50 is not None else None,
        "ema200": round(float(e200), 2) if e200 is not None else None,
        "px_vs_ema20": round((px - e20) / e20 * 100, 3),
        "score": score,
    }


def macd_state(closes: np.ndarray) -> Dict:
    """A. 趋势 — MACD (12/26/9)"""
    if len(closes) < 50:
        return {"state": "数据不足", "score": 0, "hist": None}
    ema12 = ema(closes, 12)
    ema26 = ema(closes, 26)
    macd_line = ema12 - ema26
    valid = macd_line[~np.isnan(macd_line)]
    if len(valid) < 9:
        return {"state": "数据不足", "score": 0, "hist": None}
    signal_line = ema(valid, 9)
    full_signal = np.full_like(macd_line, np.nan)
    valid_start = np.where(~np.isnan(macd_line))[0][0]
    full_signal[valid_start:valid_start + len(signal_line)] = signal_line
    hist = macd_line - full_signal
    cur_hist = hist[-1]
    prev_hist = hist[-2] if len(hist) > 1 and not np.isnan(hist[-2]) else cur_hist
    # score: hist 正负 + 是否金叉/死叉
    score = 0
    if cur_hist > 0:
        score += 1
    else:
        score -= 1
    if cur_hist > 0 and prev_hist <= 0:
        state = "金叉"
        score += 1  # 额外 +1
    elif cur_hist < 0 and prev_hist >= 0:
        state = "死叉"
        score -= 1
    elif cur_hist > 0:
        state = "多头"
    else:
        state = "空头"
    return {
        "state": state,
        "score": score,
        "hist": round(float(cur_hist), 2),
        "macd": round(float(macd_line[-1]), 2),
        "signal": round(float(full_signal[-1]), 2),
    }


def swing_structure(highs: np.ndarray, lows: np.ndarray, tss: np.ndarray,
                    last_px: float, tf_label: str) -> Dict:
    """A. 趋势 — swing pivots + 斐波那契"""
    result = ew.analyze(highs.tolist(), lows.tolist(), tss.tolist(),
                        last_px, tf_label, n_pivots=6)
    score = {"long": +2, "short": -2, "neutral": 0}.get(result.get("bias", "neutral"), 0)
    return {
        "label": result.get("label", "?"),
        "bias": result.get("bias", "neutral"),
        "score": score,
        "pos_in_leg": result.get("pos_in_leg"),
        "current_swing": result.get("current_swing", {}),
        "retracements": result.get("retracements", {}),
        "extensions": result.get("extensions", {}),
        "note": result.get("note", ""),
    }


def momentum_rsi(closes: np.ndarray) -> Dict:
    """B. 动量 — RSI(14)  (liusir 10-13 改: 25-40 中性化，避免超卖抄底假信号)"""
    if len(closes) < 20:
        return {"rsi": None, "state": "数据不足", "score": 0}
    r = rsi(closes, 14)[-1]
    # 改动: 25-40 (偏空区) 改为中性 0，避免在下跌趋势中被误认为"接近超卖"加分
    if r >= 75:
        state, score = "超买", -2
    elif r >= 65:
        state, score = "偏多", +1
    elif r >= 40:
        state, score = "中性", 0
    elif r >= 20:
        # 25-40 → 0 (原 -1); ≤20 仍 +1 (超卖抄底)
        state, score = "中性偏空", 0
    else:
        state, score = "超卖", +1
    return {"rsi": round(float(r), 2), "state": state, "score": score}


def momentum_td(closes: np.ndarray) -> Dict:
    """B. 动量 — TD Sequential setup count"""
    buy = td_setup(closes, "buy")[-1]
    sell = td_setup(closes, "sell")[-1]
    alerts = []
    score = 0
    if buy >= 9:
        alerts.append("🟢 TD buy 9 (抄底)")
        score = +2
    elif buy >= 7:
        score = +1
    elif sell >= 9:
        alerts.append("🔴 TD sell 9 (做空)")
        score = -2
    elif sell >= 7:
        score = -1
    return {
        "buy_count": int(buy),
        "sell_count": int(sell),
        "score": score,
        "alerts": alerts,
    }


def momentum_stoch_rsi(closes: np.ndarray) -> Dict:
    """B. 动量 — Stochastic RSI"""
    r = stoch_rsi(closes)
    # K>80 超买 -1；K<20 超卖 +1；金叉(K>D) +1；死叉(K<D) -1
    if r["state"] == "数据不足":
        score = 0
    elif r["state"] == "超买":
        score = -2
    elif r["state"] == "超卖":
        score = +2
    elif r["state"].startswith("K>D"):
        score = +1
    else:
        score = -1
    return {**r, "score": score}


def volatility_atr(highs: np.ndarray, lows: np.ndarray, closes: np.ndarray) -> Dict:
    """C. 波动率 — ATR(14)"""
    if len(closes) < 20:
        return {"atr": None, "pct": None, "score": 0, "regime": "数据不足"}
    a = atr(highs, lows, closes, 14)[-1]
    px = closes[-1]
    pct = a / px * 100
    # 长期 ATR (用于比较)
    a_full = atr(highs, lows, closes, 14)
    a_avg = np.nanmean(a_full[-50:]) if len(a_full) >= 50 else a
    ratio = a / a_avg if a_avg > 0 else 1.0
    # score: 当前 ATR 显著高（波动放大）→ 不倾向；显著低（收敛）→ 等待突破
    if ratio > 1.5:
        regime, score = "高波动 (放大)", -1
    elif ratio < 0.7:
        regime, score = "低波动 (收敛)", 0  # 收敛中性，等突破
    else:
        regime, score = "正常波动", 0
    return {
        "atr": round(float(a), 2),
        "pct": round(float(pct), 3),
        "score": score,
        "regime": regime,
        "ratio": round(float(ratio), 2),
    }


def volatility_bollinger(closes: np.ndarray) -> Dict:
    """C. 波动率 — Bollinger Bands"""
    b = bollinger(closes)
    # 突破上轨 -1 (回归)；跌破下轨 +1 (反弹)；上轨之上 K 线连阳 +0
    if b["state"] == "数据不足":
        score = 0
    elif b["state"] == "突破上轨":
        score = -1  # 超买警告
    elif b["state"] == "跌破下轨":
        score = +1  # 超卖反弹
    elif b["state"] == "偏上轨":
        score = -0.5
    elif b["state"] == "偏下轨":
        score = +0.5
    else:
        score = 0
    return {**b, "score": score}


def volatility_keltner(highs: np.ndarray, lows: np.ndarray, closes: np.ndarray) -> Dict:
    """C. 波动率 — Keltner 通道 (含 squeeze 检测)"""
    k = keltner(highs, lows, closes)
    # squeeze 中性（等突破）；突破上轨 -1；跌破下轨 +1
    if k["state"] == "突破上轨":
        score = -1
    elif k["state"] == "跌破下轨":
        score = +1
    else:
        score = 0
    if k["squeeze"] == "收敛中":
        score = 0  # squeeze 中性，等方向
    return {**k, "score": score}


# ============================ 辅维度 ============================
def aux_vol_regime(vols: np.ndarray) -> Dict:
    if len(vols) < 20:
        return {"regime": "数据不足", "ratio": None, "score": 0}
    v5 = float(np.mean(vols[-5:]))
    v20 = float(np.mean(vols[-20:]))
    ratio = v5 / v20 if v20 > 0 else 1.0
    if ratio > 1.5:
        regime, score = "放量", 0  # 放量中性（方向不明确）
    elif ratio > 0.8:
        regime, score = "温和", 0
    else:
        regime, score = "缩量", 0
    return {
        "regime": regime,
        "ratio": round(ratio, 2),
        "v5": round(v5, 0),
        "v20": round(v20, 0),
        "score": score,
    }


def aux_session(hour: int) -> Dict:
    """时段 (亚/欧/美盘)"""
    # CST 时段划分
    if 0 <= hour < 8:
        name = "亚盘早段"
    elif 8 <= hour < 16:
        name = "亚盘主段 / 欧盘衔接"
    elif 16 <= hour < 24:
        name = "欧盘 / 美盘"
    return {"name": name, "hour": hour, "score": 0}


def aux_teacher_bias(side: str, hour: int, date_str: str) -> Dict:
    """老师单偏置"""
    try:
        from teacher_bias import get_teacher_bias
        label, score = get_teacher_bias(side, hour, date_str)
        # label → score: well +2 / weak +1 / neutral 0 / oppose -3
        bias_score = {"well": +2, "weak": +1, "neutral": 0, "oppose": -3}.get(label, 0)
        return {"label": label, "score_raw": score, "score": bias_score}
    except Exception as e:
        return {"label": "error", "score": 0, "error": str(e)}


# ============================ 单周期完整分析 ============================
def analyze_tf(interval: str, bars: List[dict], last_px: float) -> Dict:
    if len(bars) < 30:
        return {"tf": interval, "error": f"数据不足 ({len(bars)} bars)"}
    o, hi, lo, c, v, tss = bars_to_arrays(bars)
    # 主维度
    trend = trend_ema_alignment(c)
    macd = macd_state(c)
    swing = swing_structure(hi, lo, tss, float(c[-1]), interval)
    main_a_score = trend["score"] + macd["score"] + swing["score"]  # 趋势维度 = EMA + MACD + 结构

    mom_rsi = momentum_rsi(c)
    mom_td = momentum_td(c)
    mom_stoch = momentum_stoch_rsi(c)
    main_b_score = mom_rsi["score"] + mom_td["score"] + mom_stoch["score"]  # 动量维度

    vol_atr = volatility_atr(hi, lo, c)
    vol_bb = volatility_bollinger(c)
    vol_kc = volatility_keltner(hi, lo, c)
    main_c_score = vol_atr["score"] + vol_bb["score"] + vol_kc["score"]  # 波动率维度

    # 辅维度
    aux_vol = aux_vol_regime(v)
    aux_sess = aux_session(datetime.fromtimestamp(int(tss[-1])).hour)

    return {
        "tf": interval,
        "n_bars": len(bars),
        "first_ts": datetime.fromtimestamp(int(tss[0])).strftime("%m-%d %H:%M"),
        "last_ts": datetime.fromtimestamp(int(tss[-1])).strftime("%m-%d %H:%M"),
        "px": round(float(c[-1]), 2),
        "high_period": round(float(max(hi))),
        "low_period": round(float(min(lo))),
        # 主维度 A 趋势
        "trend_ema": trend,
        "macd": macd,
        "swing": swing,
        "main_a_score": main_a_score,
        # 主维度 B 动量
        "mom_rsi": mom_rsi,
        "mom_td": mom_td,
        "mom_stoch": mom_stoch,
        "main_b_score": main_b_score,
        # 主维度 C 波动率
        "vol_atr": vol_atr,
        "vol_bb": vol_bb,
        "vol_kc": vol_kc,
        "main_c_score": main_c_score,
        # 辅维度
        "aux_vol": aux_vol,
        "aux_sess": aux_sess,
    }


# ============================ 跨周期汇聚 ============================
def aggregate_bias(per_tf: Dict[str, Dict]) -> Dict:
    """3 主维度 × 多周期投票 + 三周期一致性
    liusir 10-13 改:
      - 阈值收紧 (0.15→0.20)
      - consensus 不一致 → norm 上限 0.05
      - 4h trend 反向（空头/震荡）且 1h/15m 都同向多 → norm 上限 0.10（避免逆大势追涨）
    """
    weights = {"4h": 3, "1h": 2, "15m": 1}
    total_score = 0
    total_weight = 0
    rows = []
    biases = []
    trend_4h = None
    swing_bias_4h = None
    for tf, data in per_tf.items():
        if "error" in data:
            continue
        w = weights.get(tf, 1)
        a = data["main_a_score"]
        b = data["main_b_score"]
        c = data["main_c_score"]
        # 综合子分 = a*1 + b*1 + c*0.5 (波动率权重稍低，避免波动误导方向)
        sub = a + b + c * 0.5
        total_score += sub * w
        # max sub = |2+2+2|*1 + |2+2+2|*1 + |2+2+2|*0.5 = 4+4+2 = 10
        total_weight += w * 10
        # 收集 bias 用于一致性
        bias_value = {"long": +1, "short": -1, "neutral": 0}.get(data.get("swing", {}).get("bias", "neutral"), 0)
        biases.append((tf, bias_value))
        if tf == "4h":
            trend_4h = data.get("trend_ema", {}).get("trend", "")
            swing_bias_4h = data.get("swing", {}).get("bias", "neutral")
        rows.append({
            "tf": tf,
            "weight": w,
            "A_trend": a,
            "B_momentum": b,
            "C_volatility": c,
            "sub_score": round(sub, 2),
            "structure": data.get("swing", {}).get("label", ""),
        })
    norm = total_score / total_weight if total_weight > 0 else 0
    # 改动 1: 阈值收紧 0.15 → 0.20
    if norm > 0.20:
        bias = "long"
    elif norm < -0.20:
        bias = "short"
    else:
        bias = "neutral"
    # 三周期一致性: 所有 bias_value 同号 → 强一致；2 同号 → 弱一致；杂 → 不一致
    if biases and not any(b == 0 for _, b in biases):
        same = all(b > 0 for _, b in biases) or all(b < 0 for _, b in biases)
        consensus = "强一致" if same else "不一致"
        consensus_score = 1.0 if same else 0.0
    elif biases:
        # 有中性 → 弱一致
        consensus = "弱一致（含中性）"
        consensus_score = 0.5
    else:
        consensus = "无数据"
        consensus_score = 0.0

    # 改动 2: consensus 不一致 → norm 上限 0.05（强制归为 neutral）
    if consensus == "不一致" and abs(norm) > 0.05:
        norm = max(-0.05, min(0.05, norm))
        bias = "neutral"

    # 改动 3: 4h trend 反向 → norm 上限 0.10
    # 当 4h 是震荡/空头/缠绕，且当前判定是 long → 不让 norm 越过 0.10（防止逆大势追涨）
    if trend_4h and swing_bias_4h:
        if (norm > 0) and (swing_bias_4h in ("short", "neutral") or
                            "震荡" in trend_4h or "空头" in trend_4h or "缠绕" in trend_4h):
            if norm > 0.10:
                norm = 0.10
                if bias == "long":
                    bias = "neutral"
        # 反向同理：4h 多头时不让 short norm 过大
        if (norm < 0) and (swing_bias_4h in ("long", "neutral") or
                            "震荡" in trend_4h or "多头" in trend_4h or "缠绕" in trend_4h):
            if norm < -0.10:
                norm = -0.10
                if bias == "short":
                    bias = "neutral"

    return {
        "bias": bias,
        "score": round(total_score, 2),
        "norm": round(norm, 3),
        "rows": rows,
        "consensus": consensus,
        "consensus_score": consensus_score,
    }


# ============================ 结论式判读函数 ============================
# 把原始指标数据转换成自然语言结论（不贴数字）
def _verdict_ema(te: Dict) -> str:
    trend = te.get("trend", "?")
    pv = te.get("px_vs_ema20")
    if trend == "数据不足 (<50)" or trend == "震荡/缠绕":
        return "EMA 缠绕，方向不明确，等突破"
    if "回踩 EMA20 (多)" in trend:
        return f"价格回踩 EMA20 一线（{pv:+.2f}%），短线偏弱但未破中期均线"
    if "反抽 EMA20 (空)" in trend:
        return f"价格反抽 EMA20 受阻（{pv:+.2f}%），中期均线压制"
    if "多头排列" in trend:
        return "EMA 多头排列确立，趋势偏多"
    if "空头排列" in trend:
        return "EMA 空头排列确立，趋势偏空"
    return trend


def _verdict_macd(m: Dict) -> str:
    state = m.get("state", "?")
    hist = m.get("hist", 0) or 0
    if "金叉" in state:
        return f"MACD 金叉刚成（hist {hist:+.1f}），动能转多"
    if "死叉" in state:
        return f"MACD 死叉刚成（hist {hist:+.1f}），动能转空"
    if "多头" in state:
        # 看柱状是扩大还是收敛
        if abs(hist) > 100:
            return f"MACD 多头但柱状在扩大（hist {hist:+.1f}）"
        else:
            return f"MACD 多头，柱状收敛中（hist {hist:+.1f}）"
    if "空头" in state:
        if abs(hist) > 100:
            return f"MACD 空头且柱状在扩大（hist {hist:+.1f}）"
        else:
            return f"MACD 空头，柱状收敛中（hist {hist:+.1f}）"
    return state


def _verdict_swing(s: Dict, last_px: float) -> str:
    label = s.get("label", "?")
    cs = s.get("current_swing", {})
    pos = s.get("pos_in_leg")
    note = s.get("note", "")
    if not cs:
        return f"{label}（无 swing 数据）"
    kind = cs.get("kind", "?")
    span = cs.get("span_pct", 0)
    bars = cs.get("bars", 0)
    # 判读句
    span_str = f"幅度 {span}%" if span else ""
    bar_str = f"{bars} 根" if bars else ""
    detail = " / ".join(filter(None, [span_str, bar_str]))
    if "末段" in label:
        return f"{label}（{detail}）— 突破前高/前低才延续，否则警惕反转"
    if "突破前高" in label:
        return f"{label}（{detail}）— wave 3 候选，但远离均线注意回踩"
    if "突破前低" in label:
        return f"{label}（{detail}）— wave 5 候选，但远离均线注意反抽"
    if "回调做多区" in label or "初期" in label:
        return f"{label}（{detail}）— 顺势入场区，注意关键位支撑"
    if "反弹做空区" in label:
        return f"{label}（{detail}）— 顺势入场区，注意关键位阻力"
    return f"{label}（{detail}）"


def _verdict_rsi(r: Dict) -> str:
    state = r.get("state", "?")
    val = r.get("rsi")
    if state == "超买":
        return f"超买区（RSI {val:.0f}），警惕回调"
    if state == "超卖":
        return f"超卖区（RSI {val:.0f}），留意反弹"
    if state == "偏多":
        return f"偏多（RSI {val:.0f}），动能在多侧"
    if state == "偏空":
        return f"偏空（RSI {val:.0f}），动能在空侧"
    return f"中性（RSI {val:.0f}）"


def _verdict_td(td: Dict) -> str:
    alerts = td.get("alerts") or []
    if alerts:
        return " / ".join(alerts) + f"（buy={td['buy_count']}, sell={td['sell_count']}）"
    if td.get("sell_count", 0) >= 7:
        return f"TD sell 已 {td['sell_count']} 连阴，离 9 触发 {9 - td['sell_count']} 根"
    if td.get("buy_count", 0) >= 7:
        return f"TD buy 已 {td['buy_count']} 连阳，离 9 触发 {9 - td['buy_count']} 根"
    return f"无信号（buy={td['buy_count']}, sell={td['sell_count']}）"


def _verdict_stoch(st: Dict) -> str:
    state = st.get("state", "?")
    k = st.get("k")
    if state == "数据不足":
        return "数据不足"
    if state == "超买":
        return f"超买（K={k:.0f}），留意回调"
    if state == "超卖":
        return f"超卖（K={k:.0f}），留意反弹"
    if state.startswith("K>D"):
        return f"K>D 看涨（K={k:.0f}）"
    return f"K<D 看跌（K={k:.0f}）"


def _verdict_atr(va: Dict) -> str:
    pct = va.get("pct", 0)
    regime = va.get("regime", "?")
    ratio = va.get("ratio", 1)
    if regime == "数据不足":
        return "数据不足"
    if "高波动" in regime:
        return f"波动放大（{pct:.2f}%，vs 50 均值 ×{ratio}），止损要放宽"
    if "低波动" in regime:
        return f"波动收敛（{pct:.2f}%，vs 50 均值 ×{ratio}），等突破方向"
    return f"正常波动（{pct:.2f}%，ATR vs 50 均值 ×{ratio}）"


def _verdict_bb(bb: Dict) -> str:
    state = bb.get("state", "?")
    bw = bb.get("bandwidth_pct", 0)
    pos = bb.get("position", 0.5)
    if state == "数据不足":
        return "数据不足"
    if "突破上轨" in state:
        return f"突破上轨（带宽 {bw:.1f}%），超买警告"
    if "跌破下轨" in state:
        return f"跌破下轨（带宽 {bw:.1f}%），超卖反弹机会"
    if "偏上轨" in state:
        return f"偏上轨运行（位置 {pos:.2f}），多头占优"
    if "偏下轨" in state:
        return f"偏下轨运行（位置 {pos:.2f}），空头占优"
    return f"中轨附近（带宽 {bw:.1f}%），无明显方向"


def _verdict_kc(kc: Dict) -> str:
    state = kc.get("state", "?")
    sq = kc.get("squeeze", "?")
    if state == "数据不足":
        return "数据不足"
    if sq == "收敛中":
        return "布林带在 Keltner 内 → 波动收敛，等待突破方向"
    if "突破上轨" in state:
        return "突破 Keltner 上轨 → 短期超买"
    if "跌破下轨" in state:
        return "跌破 Keltner 下轨 → 短期超卖"
    return "通道内运行，无明显突破"


def _verdict_vol(v: Dict) -> str:
    regime = v.get("regime", "?")
    ratio = v.get("ratio")
    if regime == "数据不足":
        return "数据不足"
    if regime == "放量":
        return f"近期放量（5/20 = {ratio}），行情有方向感"
    if regime == "缩量":
        return f"近期缩量（5/20 = {ratio}），观望气氛浓"
    return f"量能温和（5/20 = {ratio}）"


def _verdict_sess(sess: Dict) -> str:
    name = sess.get("name", "?")
    hour = sess.get("hour")
    if "亚盘早段" in name:
        return f"{name}（{hour}:00）— 流动性较低，波动可能放大或反向"
    if "亚盘主段" in name:
        return f"{name}（{hour}:00）— 中国 / 亚洲交易时段"
    if "欧盘" in name or "美盘" in name:
        return f"{name}（{hour}:00）— 主流时段，流动性高"
    return f"{name}（{hour}:00）"


# ============================ 报告渲染 ============================
def render_report(per_tf: Dict[str, Dict], aggregate: Dict, ticker: dict,
                  pending: List[Dict], teacher_bias_state: Dict = None) -> str:
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S CST")
    last = float(ticker["last"])
    chg = float(ticker["change_percentage"])
    funding = float(ticker.get("funding_rate", 0))

    lines = []
    lines.append(f"# 📊 BTC 交易员视角 · 综合判读 (v2)")
    lines.append(f"_{now}_")
    lines.append("")
    lines.append(f"**当前价**: ${last:,.2f}　|　**24h**: {chg:+.2f}%　|　"
                 f"**资金费率**: {funding:+.6f}")
    lines.append("")

    # 各周期
    for tf in ["4h", "1h", "15m"]:
        if tf not in per_tf:
            continue
        d = per_tf[tf]
        if "error" in d:
            lines.append(f"## ⏱ {tf} ⚠️ {d['error']}")
            lines.append("")
            continue
        lines.append(f"## ⏱ {tf} ({d['first_ts']} → {d['last_ts']}, {d['n_bars']} 根)")
        lines.append(f"**价格**: ${d['px']:,.2f}　**区间**: ${d['low_period']:,.0f} ~ ${d['high_period']:,.0f}")
        lines.append("")

        # A 趋势（结论式）
        lines.append(f"### 🅰️ 趋势 (Trend) score={d['main_a_score']:+.0f}")
        te = d["trend_ema"]
        verdict_ema = _verdict_ema(te)
        lines.append(f"- **EMA 排列**: {verdict_ema}")
        m = d["macd"]
        verdict_macd = _verdict_macd(m)
        lines.append(f"- **MACD**: {verdict_macd}")
        s = d["swing"]
        verdict_swing = _verdict_swing(s, last)
        lines.append(f"- **结构**: {verdict_swing}")
        lines.append("")

        # B 动量（结论式）
        lines.append(f"### 🅱️ 动量 (Momentum) score={d['main_b_score']:+.0f}")
        r = d["mom_rsi"]
        verdict_rsi = _verdict_rsi(r)
        lines.append(f"- **RSI(14)**: {verdict_rsi}")
        td = d["mom_td"]
        verdict_td = _verdict_td(td)
        lines.append(f"- **TD Sequential**: {verdict_td}")
        st = d["mom_stoch"]
        verdict_stoch = _verdict_stoch(st)
        lines.append(f"- **StochRSI**: {verdict_stoch}")
        lines.append("")

        # C 波动率（结论式）
        lines.append(f"### 🅲 波动率 (Volatility) score={d['main_c_score']:+.0f}")
        va = d["vol_atr"]
        verdict_atr = _verdict_atr(va)
        lines.append(f"- **ATR(14)**: {verdict_atr}")
        bb = d["vol_bb"]
        verdict_bb = _verdict_bb(bb)
        lines.append(f"- **Bollinger**: {verdict_bb}")
        kc = d["vol_kc"]
        verdict_kc = _verdict_kc(kc)
        lines.append(f"- **Keltner**: {verdict_kc}")
        lines.append("")

        # 辅维度（简洁结论）
        lines.append(f"### 📎 辅维度")
        v = d["aux_vol"]
        verdict_vol = _verdict_vol(v)
        lines.append(f"- **量能**: {verdict_vol}")
        sess = d["aux_sess"]
        verdict_sess = _verdict_sess(sess)
        lines.append(f"- **时段**: {verdict_sess}")
        lines.append("")

    # 综合
    lines.append("---")
    lines.append("## 🎯 跨周期综合判定")
    bias = aggregate["bias"]
    bias_emoji = {"long": "🟢 多", "short": "🔴 空", "neutral": "⚪ 中"}[bias]
    consensus_emoji = {
        "强一致": "✅",
        "弱一致（含中性）": "🟡",
        "不一致": "⚠️",
        "无数据": "❓",
    }.get(aggregate["consensus"], "❓")
    lines.append(f"**综合方向**: {bias_emoji}　|　score={aggregate['score']:+.2f}　|　norm={aggregate['norm']:+.3f}")
    lines.append(f"**三周期一致性**: {consensus_emoji} {aggregate['consensus']}")
    lines.append("")
    lines.append("| 周期 | 权重 | A 趋势 | B 动量 | C 波动 | 子分 |")
    lines.append("|---|---|---|---|---|---|")
    for r in aggregate["rows"]:
        lines.append(f"| {r['tf']} | ×{r['weight']} | {r['A_trend']:+.0f} | "
                     f"{r['B_momentum']:+.0f} | {r['C_volatility']:+.0f} | {r['sub_score']:+.2f} |")
    lines.append("")

    # 持仓
    lines.append("---")
    lines.append("## 💼 当前持仓")
    if pending:
        for r in pending:
            entry = float(r["entry"])
            side = r["side"]
            diff = (last - entry) if side == "long" else (entry - last)
            pct = diff / entry * 100
            lines.append(f"- **{side}** @ ${entry:,.2f}　浮 {diff:+.1f}U ({pct:+.2f}%)　"
                         f"SL={r.get('active_sl','?')}　TP1={r.get('tp1','?')}")
    else:
        lines.append("🟢 空仓")
    lines.append("")

    # 老师偏置
    if teacher_bias_state:
        lines.append("---")
        lines.append("## 🎓 老师单偏置 (辅)")
        tb = teacher_bias_state
        notes = {
            "well": "老师同期同向 → 加分",
            "oppose": "老师同期反向 → 警示",
            "weak": "老师有同向单但已过期 → 弱跟随",
            "neutral": "老师窗口内无单 → 不偏置",
        }
        lines.append(f"标签: **{tb['label']}** (score={tb['score']:+.2f})  {notes.get(tb['label'], '')}")
        lines.append("")

    return "\n".join(lines)


# ============================ 持仓查询 ============================
def load_journal_pending() -> List[Dict]:
    p = Path("/root/data/journal.jsonl")
    if not p.exists():
        return []
    out = []
    for line in p.read_text().strip().split("\n"):
        if not line.strip(): continue
        rec = json.loads(line)
        if rec.get("status") == "pending" and rec.get("filled"):
            out.append(rec)
    return out


# ============================ 回测 ============================
def run_backtest(days: int) -> str:
    """
    回测：过去 N 天，每 15 分钟跑一次判定，对比 4h 后实际涨跌。
    """
    print(f"      回测 {days} 天...")
    contract = "BTC_USDT"
    # 拉 15m 数量 = days * 96 + buffer
    n_15m = days * 96 + 200
    n_1h = days * 24 + 200
    n_4h = days * 6 + 200

    # gate 单次上限 1000，分批拉
    def fetch_batched(interval: str, total: int) -> List[dict]:
        all_bars = []
        end_ts = int(datetime.now().timestamp())
        for _ in range(0, total, 1000):
            limit = min(1000, total - len(all_bars))
            url = (f"{GATE_BASE}/candlesticks?contract={contract}&interval={interval}"
                   f"&limit={limit}&to={end_ts}")
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=15) as r:
                batch = json.loads(r.read())
            if not batch:
                break
            # gate 按时间倒序返回，需要反转
            batch.reverse()
            all_bars.extend(batch)
            end_ts = int(float(batch[0]["t"])) - 1  # 下一批在最早的结束点之前
            if len(batch) < limit:
                break
        # 去重
        seen = set()
        uniq = []
        for b in all_bars:
            t = int(float(b["t"]))
            if t in seen: continue
            seen.add(t)
            uniq.append(b)
        return sorted(uniq, key=lambda b: float(b["t"]))

    print(f"      拉取 {n_4h} 根 4h...")
    bars_4h = fetch_batched("4h", n_4h)
    print(f"      拉取 {n_1h} 根 1h...")
    bars_1h = fetch_batched("1h", n_1h)
    print(f"      拉取 {n_15m} 根 15m...")
    bars_15m = fetch_batched("15m", n_15m)
    print(f"      4h={len(bars_4h)} 1h={len(bars_1h)} 15m={len(bars_15m)}")

    # 滚动模拟：每 15min 取截至 t 的数据 → 判定 → 4h 后价格对比
    bars_4h_ts = {int(float(b["t"])): float(b["c"]) for b in bars_4h}
    bars_1h_ts = {int(float(b["t"])): float(b["c"]) for b in bars_1h}
    bars_15m_ts = {int(float(b["t"])): float(b["c"]) for b in bars_15m}

    # 用 15m 主驱动
    all_ts = sorted(bars_15m_ts.keys())
    # 每个 15m t 都跑一遍：但只看有 4h 后数据的
    horizon_sec = 4 * 3600
    results = []
    step = 4  # 每 4 根 15m 跑一次 (1h 一次)，节省时间
    for i, t in enumerate(all_ts):
        if i % step != 0: continue
        if t + horizon_sec not in bars_15m_ts:
            continue
        # 切片: 截至 t 的 bars
        sub_4h = [b for b in bars_4h if int(float(b["t"])) <= t]
        sub_1h = [b for b in bars_1h if int(float(b["t"])) <= t]
        sub_15m = [b for b in bars_15m if int(float(b["t"])) <= t]
        if len(sub_4h) < 50 or len(sub_1h) < 50 or len(sub_15m) < 50:
            continue
        # 行情快照分析
        per_tf = {
            "4h": analyze_tf("4h", sub_4h, float(sub_4h[-1]["c"])),
            "1h": analyze_tf("1h", sub_1h, float(sub_1h[-1]["c"])),
            "15m": analyze_tf("15m", sub_15m, float(sub_15m[-1]["c"])),
        }
        agg = aggregate_bias(per_tf)
        # 4h 后价格
        px_now = float(sub_15m[-1]["c"])
        px_future = bars_15m_ts[t + horizon_sec]
        ret_pct = (px_future - px_now) / px_now * 100
        results.append({
            "ts": t,
            "ts_str": datetime.fromtimestamp(t).strftime("%Y-%m-%d %H:%M"),
            "px": px_now,
            "px_future": px_future,
            "ret_pct": ret_pct,
            "bias": agg["bias"],
            "norm": agg["norm"],
            "score": agg["score"],
        })

    # 统计
    lines = []
    lines.append(f"# 📊 trader_view v2 回测（{days} 天，4h 落点，1h 步长）")
    print(f"\n      总判定次数: {len(results)}")

    by_bias = {"long": [], "short": [], "neutral": []}
    for r in results:
        by_bias[r["bias"]].append(r)

    lines.append("")
    lines.append("## 综合判定胜率")
    lines.append("| 判定 | n | 4h 涨 n | 4h 跌 n | 胜率 | 平均收益 |")
    lines.append("|---|---|---|---|---|---|")
    for bias in ("long", "short", "neutral"):
        rows = by_bias[bias]
        n = len(rows)
        if n == 0:
            lines.append(f"| {bias} | 0 | - | - | - | - |")
            continue
        # long 的"对"=4h 涨 (>0.05%); short 的"对"=4h 跌; neutral 不评
        if bias == "long":
            win = sum(1 for r in rows if r["ret_pct"] > 0.05)
            loss_n = sum(1 for r in rows if r["ret_pct"] < -0.05)
            neutral_n = n - win - loss_n
        elif bias == "short":
            win = sum(1 for r in rows if r["ret_pct"] < -0.05)
            loss_n = sum(1 for r in rows if r["ret_pct"] > 0.05)
            neutral_n = n - win - loss_n
        else:
            win = loss_n = 0
            neutral_n = n
        wr = win / max(win + loss_n, 1) * 100 if win + loss_n > 0 else 0
        avg_ret = sum(r["ret_pct"] for r in rows) / n
        lines.append(f"| {bias} | {n} | win={win} 失={loss_n} 中={neutral_n} | "
                     f"{wr:.1f}% | {avg_ret:+.3f}% |")

    # 假设盈利：long/short 各自全仓博 1% 收益
    pnl_long = sum(max(0, r["ret_pct"]) for r in by_bias["long"])  # 满仓做多的总收益%
    n_long = len(by_bias["long"])
    pnl_short = sum(max(0, -r["ret_pct"]) for r in by_bias["short"])
    n_short = len(by_bias["short"])
    lines.append("")
    lines.append(f"## 假设策略（每次判定开仓，4h 后平）")
    lines.append(f"- long 判定: {n_long} 次，假设累计收益 {pnl_long:+.1f}%")
    lines.append(f"- short 判定: {n_short} 次，假设累计收益 {pnl_short:+.1f}%")
    lines.append(f"- 合计 (满仓做判定方向): {pnl_long + pnl_short:+.1f}% over {days} 天")

    return "\n".join(lines)


# ============================ 主流程 ============================
def main():
    p = argparse.ArgumentParser()
    p.add_argument("--telegram", action="store_true", help="推送到 Telegram")
    p.add_argument("--limit", type=int, default=500)
    p.add_argument("--backtest", type=int, default=0, metavar="DAYS",
                   help="回测 N 天（如 --backtest 30）")
    args = p.parse_args()

    if args.backtest > 0:
        report = run_backtest(args.backtest)
        print(report)
        return

    contract = "BTC_USDT"
    ticker = fetch_ticker(contract)
    last_px = float(ticker["last"])

    per_tf = {}
    for interval in ["4h", "1h", "15m"]:
        try:
            bars = fetch_candles(contract, interval, limit=args.limit)
            per_tf[interval] = analyze_tf(interval, bars, last_px)
        except Exception as e:
            per_tf[interval] = {"tf": interval, "error": str(e)}

    aggregate = aggregate_bias(per_tf)
    pending = load_journal_pending()

    teacher_bias_state = None
    try:
        from teacher_bias import get_teacher_bias
        now = datetime.now()
        lbl_long, s_long = get_teacher_bias("long", now.hour, now.strftime("%Y-%m-%d"))
        lbl_short, s_short = get_teacher_bias("short", now.hour, now.strftime("%Y-%m-%d"))
        if aggregate["bias"] == "long":
            chosen = (lbl_long, s_long)
        elif aggregate["bias"] == "short":
            chosen = (lbl_short, s_short)
        else:
            chosen = ("neutral", 0.0)
        teacher_bias_state = {"label": chosen[0], "score": chosen[1]}
    except Exception:
        pass

    report = render_report(per_tf, aggregate, ticker, pending, teacher_bias_state)
    print(report)

    # ===== 训练路径：v2.1 判定落盘到 v2_decisions.jsonl（独立于实盘 journal）=====
    try:
        from v2_decision_journal import record_v2_decision
        snapshot = {
            "px": last_px,
            "4h_label": per_tf.get("4h", {}).get("trend", {}).get("label", ""),
            "1h_label": per_tf.get("1h", {}).get("trend", {}).get("label", ""),
            "15m_label": per_tf.get("15m", {}).get("trend", {}).get("label", ""),
            "4h_swing": per_tf.get("4h", {}).get("swing", {}).get("bias", ""),
            "1h_swing": per_tf.get("1h", {}).get("swing", {}).get("bias", ""),
            "15m_swing": per_tf.get("15m", {}).get("swing", {}).get("bias", ""),
            "consensus_score": aggregate.get("consensus_score"),
            "teacher_bias": teacher_bias_state.get("label") if teacher_bias_state else "none",
        }
        # 实时 aggregate 结构（不是 backtest），字段名不同
        # score = 主分（A 趋势 + B 动量 + C 波动）；norm = 归一化；consensus = 三周期一致性
        # 主维度分数 aggregate 没有（在 per_tf 里），从 per_tf 取或跳过
        main_a_score = sum(
            per_tf[tf]["trend"].get("score", 0) for tf in ["4h", "1h", "15m"]
        ) if "trend" in per_tf.get("4h", {}) else 0
        main_b_score = sum(
            per_tf[tf].get("momentum", {}).get("score", 0) for tf in ["4h", "1h", "15m"]
        )
        main_c_score = sum(
            per_tf[tf].get("volatility", {}).get("score", 0) for tf in ["4h", "1h", "15m"]
        )

        decision = record_v2_decision(
            bias=aggregate["bias"],
            norm=abs(aggregate["norm"]),
            composite_score=aggregate["score"],
            consensus=aggregate.get("consensus", "unknown"),
            main_a_score=main_a_score,
            main_b_score=main_b_score,
            main_c_score=main_c_score,
            snapshot=snapshot,
        )
        print(f"\n[训练落盘] decision_id={decision['decision_id']} triggered={decision['triggered']}")
    except Exception as e:
        print(f"\n[训练落盘失败] {e}")

    if args.telegram:
        out = Path("/root/.hermes/cron/output/trader_view.md")
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(report)
        print(f"\n[已写] {out}")


if __name__ == "__main__":
    main()