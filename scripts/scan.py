#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
实时行情分析 + 双向交易计划生成器
=================================
策略：多周期(15m/1h/4h/1d) TD9 + EMA144(位置) + EMA169(斜率) + RSI 金叉死叉 + 量能确认，多空双向，日内平仓
# 趋势判定：close 在 EMA144 上/下方 → 多/空位置；EMA169 5根斜率 > 0 → 趋势方向

用法:
    python scan.py                                        # 默认 BTC_USDT
    python scan.py BTC_USDT --lev 100 --bal 1000          # 指定杠杆档位/账户余额
    python scan.py BTC_USDT --lev 20 --mode isolated      # 逐仓口径
    python scan.py BTC_USDT --risk 0.02 --json out.json --html report.html

输出：① 多周期结构 ② 双向打分 ③ TD9 倒计时 ④ 支撑阻力
      ⑤ 双向交易计划（挂单入场/止损/T1-T3/强平/建议张数） ⑥ 综合研判 ⑦ 执行纪律
"""
from __future__ import annotations
import sys, os, time, json, argparse
import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from gate_fetch import fetch_ohlcv, fetch_ticker, fetch_contract, assert_usdt_linear
import config
from indicators import (ema, rsi, atr, td_signal, td_setup, sma,
                        swing_pivots, last_swing_leg, fib_retr, fib_retr_up, FIB_RET,
                        trendline_low_low, trendline_high_high)

MAINT_RATE = 0.003      # Gate.io BTC_USDT 维持保证金率 0.3%
TAKER_FEE = 0.00075     # taker 0.075%
MAKER_FEE = -0.0001     # maker -0.01%（返佣）
TF_LIST = [("1d", 300, "战略方向"), ("4h", 400, "战术方向"),
           ("1h", 500, "主信号"), ("15m", 400, "入场执行")]
OK, NO, WARN = "✅", "❌", "⚠️"


# ============================ 数据 ============================
def build(tf, cnt, role, contract):
    raw = fetch_ohlcv(contract, tf, cnt, cache=False)
    d = pd.DataFrame({"ts": [r[0] for r in raw], "open": [r[1] for r in raw],
                      "high": [r[2] for r in raw], "low": [r[3] for r in raw],
                      "close": [r[4] for r in raw], "vol": [r[5] for r in raw]})
    d["e144"] = ema(d.close.values, 144)
    d["e169"] = ema(d.close.values, 169)
    d["e34"] = ema(d.close.values, 34)
    d["e144_s5"] = (d.e144 - d.e144.shift(5)) / d.e144.shift(5) * 100
    d["e169_s5"] = (d.e169 - d.e169.shift(5)) / d.e169.shift(5) * 100
    d["rsi6"] = rsi(d.close.values, 6)
    d["rsi14"] = rsi(d.close.values, 14)
    d["atr"] = atr(d.high.values, d.low.values, d.close.values, 14)
    d["atrp"] = d.atr / d.close.values * 100
    d["vr"] = d.vol / sma(d.vol.values, 20)
    d["td9b_cnt"] = td_setup(d.close.values, "buy")
    d["td9s_cnt"] = td_setup(d.close.values, "sell")
    d["td9b"] = td_signal(d.close.values, "buy")
    d["td9s"] = td_signal(d.close.values, "sell")
    d.attrs["role"], d.attrs["tf"] = role, tf
    return d


# 2026-10-06 加：下跌中段反弹过滤器
# 1h 内跌幅 ≥ 1.5% + 做多打分方向 → 减 1 分
# 原因：10-05 22:11 那笔是 4h 跌 2000 点（≈2.3%）的反弹起涨位，6 个结构分全过被打脸。
# 取最近 2 根 1h K 线 high - low 的最大跌幅，作废多头假信号。
DROP_RECOVERY_THRESHOLD_PCT = 1.5


# 2026-10-06 加：环境过滤（基于 1d EMA144 斜率分位）
# §2.9 回测结论：1d EMA144 斜率 >P80 的"强多头末期"做多胜率 14.3%、EV -1.88%，
# 强空头 <P20 做空 27.3%/-0.78%——"环境越强反向越危险"。
# 当前 10-06 P98 仍在强多头区，正是 17 笔连亏的根因。
ENV_REGIME_LOOKBACK = 60   # 用 60 根 1d 斜率算 P20/P80 阈值
ENV_REGIME_PENALTY   = 2   # 砍 2 分（信号成立门槛 4 → 直接砍到不达标）


def compute_env_regime_penalty(d1_df, side: str,
                                lookback: int = ENV_REGIME_LOOKBACK,
                                penalty: int = ENV_REGIME_PENALTY):
    """
    环境过滤：1d EMA144 斜率分位判定"强多头/强空头末期"，砍 2 分。
    只扣反向（强多头+做多、强空头+做空），顺势不扣。

    Args:
        d1_df: 1d K 线 dataframe（build() 返回值）
        side: "long" / "short"
        lookback: 计算分位的历史斜率根数（默认 60）
        penalty: 砍分（默认 2）

    Returns:
        (slope_pct, penalty_int): 当前斜率%（保留 4 位），要扣的分数（0 或 penalty）
    """
    try:
        if d1_df is None or len(d1_df) < max(150, lookback):
            return 0.0, 0
        closes = d1_df.close.values
        e144 = ema(closes, 144)
        # 取最近 lookback 根的斜率序列
        idxs = list(range(-lookback - 1, -1))
        slopes = []
        for i in idxs:
            prev = e144[i - 1]
            if prev == 0:
                continue
            slopes.append((e144[i] - prev) / prev * 100)
        if not slopes:
            return 0.0, 0
        s_cur = slopes[-1]
        s_sorted = sorted(slopes)
        P20 = s_sorted[int(len(s_sorted) * 0.20)]
        P80 = s_sorted[int(len(s_sorted) * 0.80)]
        if side == "long" and s_cur > P80:
            return s_cur, penalty
        if side == "short" and s_cur < P20:
            return s_cur, penalty
        return s_cur, 0
    except Exception:
        return 0.0, 0


def compute_drop_recovery_penalty(df, side: str, threshold_pct: float = DROP_RECOVERY_THRESHOLD_PCT):
    """
    计算"下跌中段反弹"惩罚：近 1h 内跌幅 ≥ threshold_pct + 做多打分方向 → penalty=1。

    Args:
        df: 1h K 线 dataframe（build() 返回值）
        side: "long" / "short"。long 才扣分；short 反方向顺势，不扣。
        threshold_pct: 跌幅阈值（默认 1.5%）

    Returns:
        (drop_pct, penalty): drop_pct 是实际跌幅%，penalty 是要扣的分数（0 或 1）
    """
    try:
        if df is None or len(df) < 2:
            return 0.0, 0
        if side != "long":
            return 0.0, 0
        # 取最近 2 根 1h K 线的最高 - 最低
        h_max = float(max(df.high.values[-2:]))
        l_min = float(min(df.low.values[-2:]))
        if h_max <= 0:
            return 0.0, 0
        drop_pct = (h_max - l_min) / h_max * 100
        penalty = 1 if drop_pct >= threshold_pct else 0
        return drop_pct, penalty
    except Exception:
        return 0.0, 0


def swing_levels(d, k=3):
    h, l = d.high.values, d.low.values
    hi, lo = [], []
    for i in range(k, len(d) - k):
        if h[i] == max(h[i - k:i + k + 1]): hi.append(h[i])
        if l[i] == min(l[i - k:i + k + 1]): lo.append(l[i])
    return (max(hi[-8:]) if hi else float(d.high.iloc[-1]),
            min(lo[-8:]) if lo else float(d.low.iloc[-1]))


def pivot_points(d):
    r = d.iloc[-2] if len(d) > 1 else d.iloc[-1]
    H, L, C = float(r.high), float(r.low), float(r.close)
    P = (H + L + C) / 3
    return {"P": P, "R1": 2 * P - L, "S1": 2 * P - H, "R2": P + (H - L), "S2": P - (H - L)}


def nearest_levels(px, piv, tf_map, struct, atr=None, n=3):
    """汇总关键位 → 现价下方最近 3 个支撑、上方最近 3 个阻力（不足时用 ATR 倍数补齐）"""
    c = [("日线 R2", piv["R2"]), ("日线 R1", piv["R1"]), ("日线枢轴 P", piv["P"]),
         ("日线 S1", piv["S1"]), ("日线 S2", piv["S2"])]
    for tf, d in tf_map.items():
        if d is None: continue
        r = d.iloc[-1]
        c.append((f"{tf} EMA144", float(r.e144)))
        c.append((f"{tf} EMA169", float(r.e169)))
        c.append((f"{tf} EMA34", float(r.e34)))
    c += list(struct.items())
    sup = sorted([x for x in c if x[1] < px * 0.9995], key=lambda x: -x[1])[:n]
    res = sorted([x for x in c if x[1] > px * 1.0005], key=lambda x: x[1])[:n]
    # 结构位不足时用 ATR 倍数补齐，保证支撑/阻力各有 n 档
    if atr:
        i = 1
        while len(sup) < n and i <= 8:
            sup.append((f"现价 -{i}×ATR", px - i * atr)); i += 1
        sup = sorted(sup, key=lambda x: -x[1])[:n]
        i = 1
        while len(res) < n and i <= 8:
            res.append((f"现价 +{i}×ATR", px + i * atr)); i += 1
        res = sorted(res, key=lambda x: x[1])[:n]
    return sup, res


# ============================ 计划 ============================
def liq_price(entry, lev, side):
    """逐仓强平价（隔离保证金 = 名义/杠杆档位）"""
    if side == "long":
        return entry * (1 - 1.0 / lev) / (1 - MAINT_RATE)
    return entry * (1 + 1.0 / lev) / (1 + MAINT_RATE)


def cross_liq(entry, nx, side):
    """全仓强平价（保证金 = 账户权益，名义 = nx × 账户）"""
    nx = max(nx, 1e-9)
    if side == "long":
        return entry * (1 - 1.0 / nx) / (1 - MAINT_RATE)
    return entry * (1 + 1.0 / nx) / (1 + MAINT_RATE)


def max_safe_lev(sl_pct, cushion=1.5):
    return 1.0 / (cushion * sl_pct * (1 - MAINT_RATE) + MAINT_RATE)


def make_plan(side, px, atrp, sup, res, d_atrp, lev, bal, risk, quanto, mode="cross",
              target_pts: float = 500.0):
    """
    long  → 挂最近支撑；short → 挂最近阻力
    止损 = max/min(1.5×ATR, 次级结构位±0.3ATR)，夹在 0.8%~3%
    止盈 = **目标点数封顶**（默认 500 点）：
        T3 = 入场 ± target 点（硬顶，不贪）
        T1/T2 = 目标以内最近的合格结构位，没有就按 40%/70% 目标分批
    """
    a = max(atrp, 0.15) / 100.0
    near_sup = sup[0][1] if sup else px * (1 - a)
    near_res = res[0][1] if res else px * (1 + a)
    tp_pts = float(target_pts)

    if side == "long":
        entry = near_sup if (px - near_sup) / px < 0.025 else px * (1 - 0.30 * a)
        entry_break = (res[0][1] * (1 + 0.05 * a)) if res else px * (1 + 0.10 * a)
        sec = sup[1][1] if len(sup) > 1 else entry - 2 * a * px
        sl = max(entry - 1.50 * a * px, sec - 0.30 * a * px)
        sl_pct = float(np.clip((entry - sl) / entry, 0.004, 0.030))
        R = entry * sl_pct
        cap = entry + tp_pts                                   # 500 点硬顶
        rr = [x[1] for x in res if entry + 1.2 * R < x[1] < cap]
        t1 = rr[0] if rr else entry + 0.40 * tp_pts    # TP1 = 40% × target (1000 → 400 点)
        t2 = rr[1] if len(rr) > 1 else entry + 0.60 * tp_pts  # TP2 = 60% × target (1000 → 600 点)
        t1 = min(max(t1, entry + 0.30 * R), cap - 0.20 * R)
        t2 = min(max(t2, t1 + 0.25 * R), cap)
        tps = [t1, t2, cap]
    else:
        entry = near_res if (near_res - px) / px < 0.025 else px * (1 + 0.30 * a)
        entry_break = (sup[0][1] * (1 - 0.05 * a)) if sup else px * (1 - 0.10 * a)
        sec = res[1][1] if len(res) > 1 else entry + 2 * a * px
        sl = min(entry + 1.50 * a * px, sec + 0.30 * a * px)
        sl_pct = float(np.clip((sl - entry) / entry, 0.004, 0.030))
        R = entry * sl_pct
        cap = entry - tp_pts
        ss = sorted([x[1] for x in sup if cap < x[1] < entry - 1.2 * R], key=lambda v: -v)
        t1 = ss[0] if ss else entry - 0.40 * tp_pts    # TP1 = 40% × target (1000 → 400 点)
        t2 = ss[1] if len(ss) > 1 else entry - 0.60 * tp_pts  # TP2 = 60% × target (1000 → 600 点)
        t1 = max(min(t1, entry - 0.30 * R), cap + 0.20 * R)
        t2 = max(min(t2, t1 - 0.25 * R), cap)
        tps = [t1, t2, cap]

    sl_points = entry * sl_pct                                  # 止损点数
    be_wr = sl_points / (sl_points + tp_pts)                    # 保本胜率（1:1 盈亏比口径）
    notional = 50.0 * lev                                       # 固定 50U 保证金 × 杠杆 = 名义（50×100 = 5000 U）
    nx = notional / bal
    contracts = notional / (entry * quanto)
    margin = notional / lev
    liq_iso = liq_price(entry, lev, side)
    dist_iso = abs(liq_iso / entry - 1) * 100
    if mode == "cross":
        liq = cross_liq(entry, nx, side)
        dist = abs(liq / entry - 1) * 100
    else:
        liq, dist = liq_iso, dist_iso

    return {
        "side": side,
        "entry_limit": entry, "entry_break": entry_break,
        "sl": entry * (1 - sl_pct) if side == "long" else entry * (1 + sl_pct),
        "sl_pct": sl_pct * 100,
        "tp1": tps[0], "tp2": tps[1], "tp3": tps[2],
        "tp1_pct": (tps[0] / entry - 1) * 100, "tp2_pct": (tps[1] / entry - 1) * 100,
        "tp3_pct": (tps[2] / entry - 1) * 100,
        "rr3": abs(tps[2] / entry - 1) / sl_pct,
        "target_pts": tp_pts,
        "sl_points": sl_points,
        "tp_points": [abs(tps[0] - entry), abs(tps[1] - entry), abs(tps[2] - entry)],
        "be_wr": be_wr,                                  # 保本胜率（目标=target 点、亏损=止损点）
        "pnl_at_target": tp_pts * quanto * (notional / (entry * quanto)),
        "liq": liq, "liq_dist_pct": dist,
        "liq_iso": liq_iso, "liq_iso_dist_pct": dist_iso,
        "safe_notional_x": max_safe_lev(sl_pct, 1.5),
        "contracts": contracts, "notional": notional, "notional_x": nx,
        "margin": margin, "liq_before_sl": bool(sl_pct * 100 >= dist),
        "day_reachable": bool(abs(tps[1] / entry - 1) * 100 <= d_atrp * 0.8),
        "taker_cost": TAKER_FEE * 100 * 2, "maker_cost": abs(MAKER_FEE) * 100 * 2,
    }


# ============================ 主流程 ============================
def main():
    ap = argparse.ArgumentParser(add_help=False)
    ap.add_argument("contract", nargs="?", default="BTC_USDT")
    ap.add_argument("--lev", type=float, default=100.0)
    ap.add_argument("--bal", type=float, default=50.0)
    ap.add_argument("--risk", type=float, default=0.01)
    ap.add_argument("--target", type=float, default=1000.0,
                    help="单笔目标盈利点数（BTC 价格点，默认 1000）→ T3 硬顶，不贪；TP1=40%×target=400 点，TP2=60%×target=600 点")
    ap.add_argument("--mode", default="cross", choices=["cross", "isolated"])
    ap.add_argument("--brief", action="store_true", help="极简输出：只要支撑/阻力/入场/出场")
    ap.add_argument("--no-journal", action="store_true",
                    help="不记录信号日志（默认会记，用于后续胜率复盘与自动优化）")
    ap.add_argument("--json", default=None)
    ap.add_argument("--html", default=None)
    ap.add_argument("--event", default=None,
                    help="市场情绪上下文 JSON，例如 '{\"name\":\"NFP\",\"minutes\":25,\"impact\":\"high\"}'")
    a = ap.parse_args()
    CONTRACT, LEV, BAL, RISK, MODE = a.contract, a.lev, a.bal, a.risk, a.mode
    BRIEF = a.brief
    TARGET = a.target

    # 解析情绪上下文（news_watcher 注入或人工传）
    EVENT = None
    if a.event:
        try:
            EVENT = json.loads(a.event)
        except Exception:
            EVENT = None

    # 已生效的调参结果（journal.py apply 生成）→ 只影响门槛与仓位，不改核心逻辑
    try:
        import journal as _J
        TUNE = {} if a.no_journal else _J.load_tuning()
    except Exception:
        _J, TUNE = None, {}
    THR = int(TUNE.get("score_threshold", 4) or 4)
    RS = float(TUNE.get("risk_scale", 1.0) or 1.0)
    SZ = dict(TUNE.get("side_size", {}) or {})

    lines = []
    state = {"show": not a.brief}      # --brief 时先静默，只输出最后的极简块
    def P(s=""):
        if state["show"]:
            print(s)
        lines.append(s)

    # 合约类型校验：必须能取到 USDT 本位正向合约元信息（type == "direct"）
    try:
        ct = assert_usdt_linear(CONTRACT)          # 拒绝币本位 inverse 合约
        quanto = float(ct.get("quanto_multiplier", 0.0001) or 0.0001)
        globals()["MAINT_RATE"] = float(ct.get("maintenance_rate", MAINT_RATE) or MAINT_RATE)
        SETTLE = "USDT"
    except Exception as e:
        P(f"\n{'='*82}")
        P(f"  ❌ {CONTRACT} 不是可用的 USDT 本位合约：{e}")
        P(f"")
        P(f"  本 skill 仅支持 Gate.io【USDT 本位】永续（正向 type=direct，如 BTC_USDT）。")
        P(f"  币本位 / 反向合约（如 BTC_USD，type=inverse）不支持：")
        P(f"    · 计价与盈亏单位是 BTC 而非 USDT")
        P(f"    · 1 张面值固定 100 USD（USDT 本位是 价格 × 0.0001）")
        P(f"    · 维持保证金率 0.5%（USDT 本位 0.3%）、最高杠杆 100x")
        P(f"  → 仓位换算与强平公式全部不适用，请改用 USDT 本位合约名。")
        P(f"{'='*82}\n")
        return 1
    try:
        tk = fetch_ticker(CONTRACT)[0]
        last = float(tk["last"]); mark = float(tk.get("mark_price", tk["last"]))
        fr = float(tk.get("funding_rate", 0)) * 100
        chg = float(tk.get("change_percentage", 0))
    except Exception as e:
        P(f"ticker 失败: {e}"); return 1

    P(f"\n{'='*82}")
    P(f"  {CONTRACT}  实时行情分析 + 多空双向交易计划    {time.strftime('%Y-%m-%d %H:%M:%S')}")
    P(f"  合约类型 {SETTLE} 本位正向(type=direct) ｜ 1 张 = {quanto:g} BTC ｜ "
      f"名义 = 价格 × {quanto:g} USDT")
    P(f"  杠杆档位 {LEV:g}x ｜ {'全仓 cross' if MODE=='cross' else '逐仓 isolated'} ｜ "
      f"账户 {BAL:,.0f} U ｜ 单笔风险 {RISK*100:g}% ｜ 维持保证金率 {MAINT_RATE*100:.1f}%")
    P(f"{'='*82}")
    P(f"  最新价 {last:,.1f}   标记价 {mark:,.1f}   24h {chg:+.2f}%   资金费率 {fr:+.4f}%/8h")

    data = {}
    for tf, cnt, role in TF_LIST:
        try: data[tf] = build(tf, cnt, role, CONTRACT)
        except Exception as e: P(f"  [{tf}] 失败: {e}")
    if "1h" not in data or "4h" not in data:
        P("核心周期缺失"); return 1

    h, m4, d1, m15 = data["1h"], data["4h"], data.get("1d"), data.get("15m")
    hl, ml, dl = h.iloc[-1], m4.iloc[-1], (d1.iloc[-1] if d1 is not None else None)

    # ---- ① 多周期结构 ----
    P(f"\n{'-'*82}\n  ① 多周期趋势结构\n{'-'*82}")
    P(f"{'周期':<6}{'角色':<10}{'收盘':>11}{'EMA144':>11}{'EMA169':>11}{'E144偏离%':>9}{'E169斜率%':>10}"
      f"{'RSI14':>7}{'RSI6':>7}{'ATR%':>7}{'量比':>7}{'TD9':>12}")
    for tf, _, role in TF_LIST:
        if tf not in data: continue
        d = data[tf]; r = d.iloc[-1]
        dev = (r.close - r.e144) / r.e144 * 100
        td = ("抄底9" + OK) if r.td9b else ("逃顶9" + NO) if r.td9s else \
             (f"多{int(r.td9b_cnt)}/9" if r.td9b_cnt > 0 else f"空{int(r.td9s_cnt)}/9" if r.td9s_cnt > 0 else "—")
        P(f"{tf:<6}{role:<10}{r.close:>11,.1f}{r.e144:>11,.1f}{r.e169:>11,.1f}{dev:>+9.2f}"
          f"{r.e169_s5:>+10.3f}{r.rsi14:>7.1f}{r.rsi6:>7.1f}{r.atrp:>7.2f}{r.vr:>7.2f}{td:>12}")

    # ---- ② 双向打分 ----
    def rsi_cross(d, n=5):
        g = bool(((d.rsi6 > d.rsi14) & (d.rsi6.shift(1) <= d.rsi14.shift(1))).tail(n).any())
        s = bool(((d.rsi6 < d.rsi14) & (d.rsi6.shift(1) >= d.rsi14.shift(1))).tail(n).any())
        return g, s
    gx, dx = rsi_cross(h)
    td9b_recent = bool(h.td9b.tail(5).any()); td9s_recent = bool(h.td9s.tail(5).any())
    bidx = int(np.where(h.td9b.values)[0][-1]) if h.td9b.any() else None
    sidx = int(np.where(h.td9s.values)[0][-1]) if h.td9s.any() else None
    conf_b = bool(bidx is not None and len(h) - bidx >= 3 and
                  all(h.close.values[bidx + t] > h.close.values[bidx + t - 1] for t in (1, 2)))
    conf_s = bool(sidx is not None and len(h) - sidx >= 3 and
                  all(h.close.values[sidx + t] < h.close.values[sidx + t - 1] for t in (1, 2)))
    core_b, core_s = td9b_recent and conf_b, td9s_recent and conf_s

    # === 趋势线（1h 周期，3 个 pivot 拟合；只在非横盘时计入打分）===
    # 1h 用全量 500 根 → swing_pivots → 取最近 3 个 pivot 拟合趋势线
    try:
        is_h_1h, conf_h_1h, is_l_1h, conf_l_1h = swing_pivots(h.high.values, h.low.values, k=5)
        t_1h = len(h) - 1
        tl_low_1h = trendline_low_low(h.low.values, is_l_1h, conf_l_1h, t_1h, min_touches=3)
        tl_high_1h = trendline_high_high(h.high.values, is_h_1h, conf_h_1h, t_1h, min_touches=3)
    except Exception:
        tl_low_1h = tl_high_1h = None

    # 趋势线条件：价格站上 + 斜率同向。横盘时不算分（横盘趋势线无意义）
    tl_long_ok  = bool(tl_low_1h  and tl_low_1h['slope']  > 0 and last > tl_low_1h['value_now'])
    tl_short_ok = bool(tl_high_1h and tl_high_1h['slope'] < 0 and last < tl_high_1h['value_now'])
    # 横盘判定（与 BRIEF 段共用）
    sideways = bool(abs(hl.close / hl.e144 - 1) < 0.005) if not np.isnan(hl.e144) else False
    tl_long_score  = tl_long_ok  and not sideways
    tl_short_score = tl_short_ok and not sideways

    rows_b = [("① TD9 抄底完成（1h，近5根）", td9b_recent),
              ("② 其后连续 2 根收阳确认【核心】", core_b),
              ("③ 4h 收盘价在 4h EMA144 之上", bool(ml.close > ml.e144)),
              ("④ 4h EMA169 斜率 > 0（多头结构）", bool(ml.e169_s5 > 0)),
              ("⑤ 1h RSI 金叉（RSI6 上穿 RSI14）", gx),
              ("⑥ 量能确认（1h 量比 > 1.2）", bool(hl.vr > 1.2)),
              ("⑦ 1h 上升趋势线：价格站上 + 斜率 > 0（横盘不计）", tl_long_score)]
    rows_s = [("① TD9 逃顶完成（1h，近5根）", td9s_recent),
              ("② 其后连续 2 根收阴确认【核心】", core_s),
              ("③ 4h 收盘价在 4h EMA144 之下", bool(ml.close < ml.e144)),
              ("④ 4h EMA169 斜率 < 0（空头结构）", bool(ml.e169_s5 < 0)),
              ("⑤ 1h RSI 死叉（RSI6 下穿 RSI14）", dx),
              ("⑥ 量能确认（1h 量比 > 1.2）", bool(hl.vr > 1.2)),
              ("⑦ 1h 下降趋势线：价格跌穿 + 斜率 < 0（横盘不计）", tl_short_score)]
    sc_b = sum(1 for _, v in rows_b if v); sc_s = sum(1 for _, v in rows_s if v)

    # ========== 情绪/事件上下文：funding + 外部事件 ==========
    # Funding rate 极端：顺势加权重 / 逆势降权（cost is asymmetric）
    FUNDING_HIGH = 0.05  # % （Gate.io 多付空，超过这个意味多军过热）
    FUNDING_LOW  = -0.05
    funding_note = ""
    if fr > FUNDING_HIGH:
        # 多付空 → 做多成本高，做空顺势 → 调整 score
        sc_b = max(0, sc_b - 1)
        sc_s = sc_s  # 做空顺势不加分（保持原值；实操可考虑 +0.5 但保守起见不动）
        funding_note = f"funding +{fr:.3f}%（多付空）→ 做多成本高"
    elif fr < FUNDING_LOW:
        sc_s = max(0, sc_s - 1)
        funding_note = f"funding {fr:.3f}%（空付多）→ 做空成本高"

    # 外部事件（news_watcher 注入 / 人工 --event）
    event_note = ""
    if EVENT:
        minutes = EVENT.get("minutes", 99)
        impact = EVENT.get("impact", "low")
        name = EVENT.get("name", "事件")
        if impact == "high" and minutes < 30:
            # 高重要性事件 30min 内 → 双向降权（避免被跳空扫掉）
            sc_b = max(0, sc_b - 2)
            sc_s = max(0, sc_s - 2)
            event_note = f"⚠️ {name} {minutes}min 内（高重要性）→ 双向观望"
        elif impact == "medium" and minutes < 15:
            sc_b = max(0, sc_b - 1)
            sc_s = max(0, sc_s - 1)
            event_note = f"⚠️ {name} {minutes}min 内（中重要性）→ 减仓"
        else:
            event_note = f"📅 {name} {minutes}min 后（{impact}）→ 注意"

    # ========== 2026-10-06：下跌中段反弹过滤器 ==========
    # 1h 内跌幅 ≥ 1.5% + 做多打分方向 → score_long -= 1
    # 原因：10-05 22:11 那笔是 4h 跌 2000 点的反弹起涨位，6 个结构分全过被打脸。
    # 取最近 2 根 1h K 线的 high-low 跌幅，作废多头假信号。
    _drop_pct, _drop_penalty = compute_drop_recovery_penalty(h, side="long")
    if _drop_penalty:
        sc_b = max(0, sc_b - _drop_penalty)
        drop_note = f"1h 内跌 {_drop_pct:.2f}%（≥{DROP_RECOVERY_THRESHOLD_PCT}%）→ 下跌中段反弹，做多减 1 分"
        # 合并到情绪上下文（funding_note / event_note 之后追加）
        note = (funding_note + " ｜ " if funding_note else "") + drop_note
        funding_note = note

    # ========== 2026-10-06：环境过滤（§2.9 反直觉）==========
    # 1d EMA144 斜率 >P80 强多头做多 / <P20 强空头做空 → 各砍 2 分
    # 当前 10-06 在 P98（强多头末期），10-05~10-06 连亏 17 笔正是这个根因。
    _regime_b, _regime_pen_b = compute_env_regime_penalty(d1, side="long")
    if _regime_pen_b:
        sc_b = max(0, sc_b - _regime_pen_b)
        env_note_b = (f"1d EMA144 斜率 {_regime_b:+.2f}% >P80（强多头末期）"
                       f" → 做多减 {_regime_pen_b} 分")
        funding_note = (funding_note + " ｜ " if funding_note else "") + env_note_b
    _regime_s, _regime_pen_s = compute_env_regime_penalty(d1, side="short")
    if _regime_pen_s:
        sc_s = max(0, sc_s - _regime_pen_s)
        env_note_s = (f"1d EMA144 斜率 {_regime_s:+.2f}% <P20（强空头末期）"
                       f" → 做空减 {_regime_pen_s} 分")
        funding_note = (funding_note + " ｜ " if funding_note else "") + env_note_s

    P(f"\n{'-'*82}\n  ② 双向条件打分（1h 主信号 ＋ 4h 方向过滤）\n{'-'*82}")
    P("  【做多 LONG】")
    for nm, v in rows_b: P(f"    {OK if v else NO} {nm}")
    P(f"    → {sc_b}/7" + ("   ★ 核心①②满足" if core_b else "   （核心①②未满足 → 不达标）"))
    P("  【做空 SHORT】")
    for nm, v in rows_s: P(f"    {OK if v else NO} {nm}")
    P(f"    → {sc_s}/7" + ("   ★ 核心①②满足" if core_s else "   （核心①②未满足 → 不达标）"))

    # ---- ③ TD9 倒计时 ----
    cb, cs_ = int(h.td9b_cnt.iloc[-1]), int(h.td9s_cnt.iloc[-1])
    P(f"\n  ③ TD9 倒计时")
    if cb: P(f"    {WARN} 1h 抄底计数 {cb}/9（还差 {9-cb} 根 ≈ {9-cb}h）→ 完成后必须再等 2 根收阳")
    if cs_: P(f"    {WARN} 1h 逃顶计数 {cs_}/9（还差 {9-cs_} 根 ≈ {9-cs_}h）→ 完成后必须再等 2 根收阴")
    if not cb and not cs_: P("    1h 当前无进行中的 TD9 计数")
    if m15 is not None:
        c15, s15 = int(m15.td9b_cnt.iloc[-1]), int(m15.td9s_cnt.iloc[-1])
        if max(c15, s15) >= 6:
            P(f"    {WARN} 15m {'抄底' if c15 >= 6 else '逃顶'}计数 {max(c15, s15)}/9 临近完成"
              f"（15m 仅用于择时，不作方向依据）")

    # ---- ④ 支撑阻力 ----
    h1s, l1s = swing_levels(h); h4s, l4s = swing_levels(m4)
    piv = pivot_points(d1) if d1 is not None else pivot_points(m4)
    struct = {"1h 结构高": h1s, "1h 结构低": l1s, "4h 结构高": h4s, "4h 结构低": l4s}
    # 1h 趋势线作为动态支撑/阻力加入
    if tl_low_1h:
        struct["1h 上升趋势线 (低-低)"] = tl_low_1h['value_now']
    if tl_high_1h:
        struct["1h 下降趋势线 (高-高)"] = tl_high_1h['value_now']

    # ---- 斐波那契摆动段（无未来函数：pivot 需右侧 5 根走完才确认）----
    fib_info = {}
    for tf in ("4h", "1h"):
        if tf not in data:
            continue
        dd = data[tf]
        try:
            ih_, ch_, il_, cl_ = swing_pivots(dd.high.values, dd.low.values, 5)
            kind, lo_v, hi_v, lo_i, hi_i = last_swing_leg(
                dd.high.values, dd.low.values, ih_, ch_, il_, cl_, len(dd) - 1)
            if kind is None or hi_v <= lo_v:
                continue
            lv = fib_retr(lo_v, hi_v) if kind == "up" else fib_retr_up(lo_v, hi_v)
            fib_info[tf] = (kind, lo_v, hi_v, lv)
            for r_, p_ in lv.items():
                struct[f"{tf} Fib {r_:.3f}"] = p_
        except Exception:
            pass
    if fib_info:
        P(f"\n  ④-0 斐波那契摆动段（pivot k=5，已确认）")
        for tf, (kind, lo_v, hi_v, lv) in fib_info.items():
            seg = "上涨段（先低后高）→ 等回调做多" if kind == "up" else "下跌段（先高后低）→ 等反弹做空"
            P(f"    {tf}：{lo_v:,.1f} → {hi_v:,.1f}  {seg}")
            P("      " + "  ".join(f"{r_:.3f}={p_:,.1f}" for r_, p_ in lv.items()))
    sup, res = nearest_levels(last, piv, {"1h": h, "4h": m4, "1d": d1}, struct,
                              atr=float(hl.atr))
    P(f"\n{'-'*82}\n  ④ 关键价位（现价 {last:,.1f}）\n{'-'*82}")
    for nm, v in res[::-1]: P(f"    阻力 ↑  {nm:<14}{v:>11,.1f}   {(v/last-1)*100:+.2f}%")
    P(f"    ──────── 现价 {last:,.1f} ────────")
    for nm, v in sup: P(f"    支撑 ↓  {nm:<14}{v:>11,.1f}   {(v/last-1)*100:+.2f}%")

    # ---- ⑤ 双向计划 ----
    d_atrp = float(dl.atrp) if dl is not None else float(hl.atrp) * 4
    rb = RISK * RS * float(SZ.get("long", 1.0))    # 调参：风险缩放 × 方向仓位系数
    rs_ = RISK * RS * float(SZ.get("short", 1.0))
    plan_b = make_plan("long", last, float(hl.atrp), sup, res, d_atrp, LEV, BAL, rb, quanto,
                       MODE, TARGET)
    plan_s = make_plan("short", last, float(hl.atrp), sup, res, d_atrp, LEV, BAL, rs_, quanto,
                       MODE, TARGET)
    if core_b and sc_b >= THR and sc_b > sc_s: verdict, main_side = "做多信号成立", "long"
    elif core_s and sc_s >= THR and sc_s > sc_b: verdict, main_side = "做空信号成立", "short"
    elif max(sc_b, sc_s) >= 4: verdict, main_side = "临界（再等 1 根确认）", ("long" if sc_b >= sc_s else "short")
    else: verdict, main_side = "无信号 · 观望", ("long" if sc_b >= sc_s else "short")
    # 得分打平时按趋势结构定方向（1d/4h 是否在 EMA144 上方 + 4h EMA169 斜率）
    if sc_b == sc_s:
        _bull = (int(ml.close > ml.e144) + int(ml.e169_s5 > 0)
                 + (int(dl.close > dl.e144) if dl is not None else 0))
        main_side = "long" if _bull >= 2 else "short"
    # 方向偏向度：用于极简模式只给一侧
    bias = abs(sc_b - sc_s)

    # ---- 标签（趋势/量能/Fib 共振）→ 供绩效分层统计使用 ----
    # 2026-10-05 修正（交易员语义对称）：
    #   做多要严：4h 拐头 + 4h EMA169 斜率向上 + 日线确认（dl.close > dl.e144）
    #   做空要宽：4h 一旦拐头（4h.close < 4h.e144 + 4h EMA169 斜率向下）就够
    #           空头反转往往是 4h 一根吞没 K 砸下来的，等日线确认会晚 2000 点
    #   之前 `(dl is None or dl.close > dl.e144)` 漏写了"无日线也要验证"——
    #   没日线数据时反而走最松的路径，与"做多要严"意图相反
    #   改成 dl is not None AND dl.close > dl.e144 才算多头；否则进"震荡"
    trend_tag = ("多头排列" if (ml.close > ml.e144 and ml.e169_s5 > 0
                                and dl is not None and dl.close > dl.e144) else
                 "空头排列" if (ml.close < ml.e144 and ml.e169_s5 < 0) else "震荡")
    vol_tag = "放量" if hl.vr > 1.2 else ("温和" if hl.vr > 0.9 else "缩量")
    _plm = plan_b if main_side == "long" else plan_s
    fib_tag = "无"
    _ftf = "4h" if "4h" in fib_info else ("1h" if "1h" in fib_info else None)
    if _ftf:
        for _rr in (0.5, 0.618, 0.786):
            if abs(fib_info[_ftf][3][_rr] / _plm["entry_limit"] - 1) < 0.003:
                fib_tag = "共振"
                break

    # ---- 绩效日志：先自动结算旧信号，再记录本次（冷却期内同方向只刷新点位）----
    jstat = None
    _reverse_force_close_info = None   # 给卡片用：标记反方向自动平仓
    if _J is not None and not a.no_journal:
        try:
            _J.settle_all(CONTRACT)
            # ★ 准入守卫：调用 config.should_skip_new_signal 统一规则
            # （防止 Settler 失效期间累积多条同向单；同时和 watch.py JumpTracker 规则对齐）
            _skip, _reason = config.should_skip_new_signal(CONTRACT, main_side, float(_plm["entry_limit"]))
            _skipped_reason = _reason if _skip else None

            # ★ 反方向 force_close（2026-10-04 新增）：如果已有 filled 单 + 新信号反方向
            #     → 自动平掉 + 写反方向 journal（不受 500 点限制）
            if not _skipped_reason:
                # 找最近一条 filled 反向单
                _jrecs = _J._read()
                _filled_opposite = None
                for _jr in reversed(_jrecs):
                    if (_jr.get("contract") == CONTRACT
                            and _jr.get("status") == "filled"
                            and _jr.get("side") != main_side):
                        _filled_opposite = _jr
                        break
                if _filled_opposite is not None:
                    try:
                        from position_tracker import force_close_position
                        _exit_px = float(last)
                        _close_res = force_close_position(
                            exit_px=_exit_px,
                            reason=f"scan 反方向信号触发（{_filled_opposite.get('side')} → {main_side}），自动平仓",
                            signal_id=_filled_opposite.get("id"),
                        )
                        if _close_res is not None:
                            _reverse_force_close_info = {
                                "id": _filled_opposite.get("id"),
                                "exit_px": _exit_px,
                                "net_usd": _close_res.get("net_usd", 0),
                                "old_side": _filled_opposite.get("side"),
                                "new_side": main_side,
                            }
                            P(f"\n  🔄 反方向：自动平仓 id={_filled_opposite.get('id')} "
                              f"net={_close_res.get('net_usd', 0):+.2f}U（已 simulated close）")
                    except Exception as e:
                        P(f"\n  ⚠️ 反方向自动平仓失败: {e}")

            if _skipped_reason:
                jstat = "skipped_duplicate"
                P(f"\n  ⚠️ 准入守卫：{_skipped_reason} → 跳过本次 journal 写入")
            else:
                jstat = _J.log_signal(
                    CONTRACT, last, main_side,
                    sc_b if main_side == "long" else sc_s,
                    core_b if main_side == "long" else core_s,
                    verdict, _plm,
                    {"trend": trend_tag, "vol": vol_tag, "fib": fib_tag},
                    {"lev": LEV, "bal": BAL, "risk": RISK, "mode": MODE})
        except Exception:
            jstat = None

    def _perf():
        if _J is None or jstat in (None, "off"):
            return None
        try:
            return _J.brief_line(CONTRACT)
        except Exception:
            return None

    # === 极简模式：只要 支撑/阻力/入场/出场 ===
    # === 横盘判定：1h close 距 1h EMA144 偏离 < 0.5% 视为横盘（已在打分块算过）===
    if BRIEF:
        lines.clear(); state["show"] = True
        # 情绪上下文：event_note 优先，其次 funding_note
        mood = event_note or funding_note
        mood_tag = " · " + mood if mood else ""
        sideways_tag = " · ⚠️ 横盘（建议减仓或观望）" if sideways else ""
        P(f"\n  {CONTRACT}（USDT 本位永续合约）  现价 {last:,.1f}   "
          f"{time.strftime('%m-%d %H:%M')}   {trend_tag} / {vol_tag} / 判定：{verdict}{mood_tag}{sideways_tag}")
        P(f"  数据源 Gate.io 永续 /futures/usdt/* · type=direct ｜ "
          f"标记价 {mark:,.1f} ｜ 资金费率 {fr:+.4f}% ｜ 24h {chg:+.2f}%")
        if mood:
            P(f"  ⚠️ 情绪上下文：{mood}")
        if fib_info:
            _tf = "4h" if "4h" in fib_info else ("1h" if "1h" in fib_info else None)
            if _tf:
                _k, _lo, _hi, _lv = fib_info[_tf]
                P(f"  Fib({_tf}) {_lo:,.0f} → {_hi:,.0f} "
                  f"{'上涨段·回调做多' if _k == 'up' else '下跌段·反弹做空'}"
                  f" ｜ 0.5={_lv[0.5]:,.1f}  0.618={_lv[0.618]:,.1f}  0.786={_lv[0.786]:,.1f}")
        P(f"  {'─'*72}")
        P(f"  【阻力】")
        for nm, v in res[::-1]:
            P(f"      {v:>10,.1f}   {(v/last-1)*100:+.2f}%    {nm}")
        P(f"  【现价】 {last:,.1f}")
        P(f"  【支撑】")
        for nm, v in sup:
            P(f"      {v:>10,.1f}   {(v/last-1)*100:+.2f}%    {nm}")
        P(f"  {'─'*72}")
        # ★ 只给「当前行情偏向的那一个方向」
        pl = plan_b if main_side == "long" else plan_s
        sc = sc_b if main_side == "long" else sc_s
        is_long = main_side == "long"
        tag = "做多 LONG" if is_long else "做空 SHORT"
        star = "★" if "成立" in verdict else "○"
        P(f"  {star} 方向：{tag}（{sc}/7，{'高于' if bias else '等于'}反向"
          f"{'' if bias else '，按趋势定'}{f' {bias} 分' if bias else ''}）")
        P(f"  {'─'*72}")
        brk = "突破追" if is_long else "跌破追"
        P(f"    入场  {pl['entry_limit']:>10,.1f}  挂单（post-only）")
        P(f"           {pl['entry_break']:>10,.1f}  {brk}")
        P(f"    止损  {pl['sl']:>10,.1f}   (-{pl['sl_points']:,.0f} 点 / {(pl['sl']/pl['entry_limit']-1)*100:+.2f}%)")
        _tp = pl["tp_points"]
        P(f"    出场  T1 +{_tp[0]:,.0f}点 ({pl['tp1']:,.1f}) → "
          f"T2 +{_tp[1]:,.0f}点 ({pl['tp2']:,.1f}) → "
          f"T3 +{_tp[2]:,.0f}点 ({pl['tp3']:,.1f})")
        P(f"    仓位  {pl['contracts']:.0f} 张（名义 {pl['notional']:,.0f} U）"
          f"   止损亏 {BAL*RISK:,.0f} U ｜ 达标赚 {pl['pnl_at_target']:,.1f} U")
        _rr = pl["tp_points"][2] / pl["sl_points"]
        _be = pl["be_wr"] * 100
        _warn = (f"  {WARN} R:R 1:{_rr:.2f}，保本胜率需 {_be:.0f}%"
                 + ("（>50% → 只在核心信号成立时做 + 移动止损 0.5%）" if _be > 50 else ""))
        P(f"    目标  {TARGET:,.0f} 点封顶（不贪）｜止损 {pl['sl_points']:,.0f} 点{_warn}")
        P(f"  {'─'*72}")
        if "成立" in verdict:
            P(f"  结论：按{'做多' if is_long else '做空'}执行，挂单等成交（post-only）。")
        else:
            P(f"  结论：核心信号（TD9 + 2 根确认）未满足 → 轻仓或观望。"
              f"当前偏向{'做多' if is_long else '做空'}，"
              f"{'回踩' if is_long else '反弹'} {pl['entry_limit']:,.0f} 附近再动手。")
        _p = _perf()
        if _p:
            P(_p)
        P("")
        payload = {"contract": CONTRACT, "ts": int(time.time()), "last": last,
                   "verdict": verdict, "side": main_side,
                   "score_long": sc_b, "score_short": sc_s,
                   "funding_rate": round(fr, 4),
                   "funding_note": funding_note,
                   "event_note": event_note,
                   "event": EVENT or {},
                   "resistance": [{"name": n, "price": round(v, 1)} for n, v in res],
                   "support": [{"name": n, "price": round(v, 1)} for n, v in sup],
                   "plan": {k: round(v, 1) if isinstance(v, float) else v
                            for k, v in pl.items()}}
        if a.json:
            json.dump(payload, open(a.json, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
            print(f"[saved] {a.json}")
        return 0

    P(f"\n{'-'*82}\n  ⑤ 多空双向交易计划      判定：【{verdict}】\n{'-'*82}")
    for pl, tag, sc in ((plan_b, "做多 LONG", sc_b), (plan_s, "做空 SHORT", sc_s)):
        hot = (main_side == pl["side"] and "成立" in verdict)
        P(f"  ── {tag}  {'★ 主推' if hot else '   备选'}   得分 {sc}/7 ──")
        P(f"    挂单入场 {pl['entry_limit']:>11,.1f}   ({(pl['entry_limit']/last-1)*100:+.2f}% vs 现价)  post-only")
        P(f"    追价触发 {pl['entry_break']:>11,.1f}   (突破/跌破后 stop-limit 追)")
        P(f"    止损 SL  {pl['sl']:>11,.1f}   ({(pl['sl']/pl['entry_limit']-1)*100:+.2f}% 相对入场)")
        P(f"    止盈 T1  {pl['tp1']:>11,.1f}   (+{pl['tp_points'][0]:,.0f}点 / {pl['tp1_pct']:+.2f}%)  平 50%，剩余移成本价")
        P(f"    止盈 T2  {pl['tp2']:>11,.1f}   (+{pl['tp_points'][1]:,.0f}点 / {pl['tp2_pct']:+.2f}%)  平 30%"
          + ("   ✅ 日内可达" if pl["day_reachable"] else "   ⚠️ 日内偏难，降目标或隔夜"))
        P(f"    止盈 T3  {pl['tp3']:>11,.1f}   (+{pl['tp_points'][2]:,.0f}点 / {pl['tp3_pct']:+.2f}%)  尾仓（{TARGET:,.0f}点封顶）")
        P(f"    目标    {TARGET:,.0f} 点封顶 ｜ 止损 {pl['sl_points']:,.0f} 点 ｜ "
          f"R:R 1:{pl['tp_points'][2]/pl['sl_points']:.2f} ｜ 保本胜率 {pl['be_wr']*100:.0f}%"
          + (f"  {WARN} 偏高" if pl["be_wr"] > 0.5 else "  ✅ 可接受"))
        mm = "全仓" if MODE == "cross" else "逐仓"
        P(f"    强平价({mm}){pl['liq']:>11,.1f}   距入场 {pl['liq_dist_pct']:.2f}%"
          + ("   ❌ 在止损之内" if pl["liq_before_sl"] else "   ✅ 在止损之外"))
        P(f"    逐仓{LEV:g}x 对照{pl['liq_iso']:>10,.1f}   距入场仅 {pl['liq_iso_dist_pct']:.2f}%"
          f"   {'❌ 会被噪音打穿' if pl['sl_pct'] > pl['liq_iso_dist_pct'] else '尚可'}")
        P(f"    建议仓位 {pl['contracts']:.2f} 张 = 名义 {pl['notional']:,.0f} U "
          f"({pl['notional_x']:.2f}x 账户) ｜ 保证金 {pl['margin']:,.1f} U")
        P(f"    盈亏预测 止损 -{BAL*RISK:,.1f} U ｜ T1 +{BAL*RISK*0.5:,.1f} U ｜ "
          f"T2 +{BAL*RISK*1.1:,.1f} U ｜ T3 +{BAL*RISK*(0.5+0.6+0.4*pl['rr3']):,.1f} U")
        P("")

    # ---- ⑥ 综合研判 ----
    P(f"{'-'*82}\n  ⑥ 综合研判\n{'-'*82}")
    bull = int(ml.close > ml.e144) + int(ml.e169_s5 > 0) + (int(dl.close > dl.e144) if dl is not None else 0)
    trend = ("多头排列（1d/4h 均在 EMA144 上方且 4h EMA169 斜率向上）" if bull == 3 else
             "偏多但结构不完整" if bull == 2 else "中性/震荡" if bull == 1 else "空头排列")
    P(f"    · 趋势结构：{trend}")
    if dl is not None:
        P(f"    · 相对位置：距 4h EMA144({ml.e144:,.0f}) {(last/ml.e144-1)*100:+.1f}%，"
          f"距 1d EMA144({dl.e144:,.0f}) {(last/dl.e144-1)*100:+.1f}%")
    zone = "超买区" if hl.rsi14 > 70 else ("中性偏强" if hl.rsi14 > 55 else "偏弱")
    P(f"    · 动能：1h RSI14 {hl.rsi14:.0f} / RSI6 {hl.rsi6:.0f}（{zone}）；"
      f"4h 量比 {ml.vr:.2f}、1h 量比 {hl.vr:.2f}"
      f"（{'放量，信号可信' if hl.vr > 1.2 else '缩量，信号可信度打折'}）")
    P(f"    · 波动：1h ATR {hl.atrp:.2f}%（{hl.atr:,.0f} 点）｜日线 ATR {d_atrp:.2f}% "
      f"→ 日内目标以 ≤{d_atrp*0.8:.1f}% 为宜")
    if sideways:
        P(f"    · 横盘预警：1h close 距 1h EMA144 偏离 {((hl.close/hl.e144-1)*100):+.2f}%（<0.5%）"
          f"→ ⚠️ 假突破风险高，<b>建议减仓 50% 或直接观望</b>")
    if verdict.startswith("无信号"):
        P(f"    · 结论：核心①②未满足 —— 今日不宜按 TD9 信号开仓。")
        if sup and res:
            P(f"      若做区间：{sup[0][1]:,.0f} 附近挂多（止损 {plan_b['sl_pct']:.1f}%）、"
              f"{res[0][1]:,.0f} 附近挂空（止损 {plan_s['sl_pct']:.1f}%），快进快出、严格止损。")
        P(f"      若只做一个方向：顺势优先 —— 只在 {sup[0][1]:,.0f} 挂多，放弃逆势做空。")
        if cs_: P(f"      待触发：1h 逃顶计数 {cs_}/9，完成后若连续 2 根收阴 → 做空达 4 分，届时入场。")
        if cb: P(f"      待触发：1h 抄底计数 {cb}/9，完成后若连续 2 根收阳 → 做多达 4 分，届时入场。")
    else:
        P(f"    · 结论：按{'做多' if main_side=='long' else '做空'}方向执行，点位与仓位见 ⑤。")

    # ---- ⑦ 纪律 ----
    P(f"\n{'-'*82}\n  ⑦ 执行纪律\n{'-'*82}")
    P(f"    1. 入场挂 post-only 限价（maker {MAKER_FEE*100:+.3f}% 返佣）；"
      f"taker {TAKER_FEE*100:.3f}%，往返成本差 {(TAKER_FEE+abs(MAKER_FEE))*200:.3f}%")
    P( "    2. 核心①②未满足不进场 —— 回测「见 TD9 就冲」EV 为负（4h: -0.71%/笔，p=0.876）")
    P(f"    2b. 目标 {TARGET:,.0f} 点 + 止损按 ATR 自适应（1.5×ATR，通常 0.4~0.6%）：")
    P( "        → 只在核心信号成立时做；且必须配移动止损 0.5%")
    P( "        → 回测 4h：不加移动止损 EV -0.05%、PF 0.84；加 0.5% 移动止损 EV +0.05%、PF 1.23")
    P( "    3. 做空为逆势方向（回测 EV -0.28%/笔）→ 做空仓位减半，或得分门槛 +1")
    P( "    4. 日内了结：最迟 UTC 23:50（北京 07:50）平仓；T1 兑现后止损立即移成本价")
    P(f"    5. 单笔风险 {RISK*100:g}% 账户（{BAL*RISK:,.0f} U）；连亏 3 笔当日停手")
    P(f"    6. 若用逐仓模式，杠杆档位须 ≤ "
      f"{max_safe_lev(max(plan_b['sl_pct'], plan_s['sl_pct'])/100, 1.5):.0f}x，否则止损未到先被强平")
    _p = _perf()
    if _p:
        P(f"    7. 本次信号已记入日志（{jstat}）：`journal.py stats` 看分层胜率；"
          f"只有 `tune` → `apply` 才会真的改参数，且小样本只允许减仓")
        P(_p)
    P(f"\n{'='*82}")
    P("  ⚠️ 以上为历史统计与结构化推演，不构成投资建议；高杠杆合约存在爆仓风险，请自行复验。")
    P(f"{'='*82}\n")

    def R(x, n=4): return round(float(x), n)
    payload = {"contract": CONTRACT, "ts": int(time.time()), "last": last, "mark": mark,
               "change_24h_pct": chg, "funding_rate_8h_pct": fr,
               "score_long": sc_b, "score_short": sc_s,
               "core_long_ok": bool(core_b), "core_short_ok": bool(core_s),
               "verdict": verdict, "main_side": main_side,
               "sideways": sideways,
               "funding_note": funding_note, "event_note": event_note, "event": EVENT or {},
               "resistance": [{"name": n, "price": R(v)} for n, v in res],
               "support": [{"name": n, "price": R(v)} for n, v in sup],
               "plan_long": {k: R(v) if isinstance(v, float) else v for k, v in plan_b.items()},
               "plan_short": {k: R(v) if isinstance(v, float) else v for k, v in plan_s.items()},
               "tf": {tf: {"close": R(data[tf].iloc[-1].close, 1), "ema144": R(data[tf].iloc[-1].e144, 1),
                           "ema169": R(data[tf].iloc[-1].e169, 1),
                           "atrp": R(data[tf].iloc[-1].atrp, 3), "rsi14": R(data[tf].iloc[-1].rsi14, 1),
                           "vol_ratio": R(data[tf].iloc[-1].vr, 2)} for tf in data}}
    if a.json:
        json.dump(payload, open(a.json, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
        print(f"[saved] {a.json}")
    if a.html:
        write_html(a.html, payload, "\n".join(lines))
        print(f"[saved] {a.html}")
    return 0


def write_html(path, p, text):
    import html as H
    pb, ps = p["plan_long"], p["plan_short"]
    def lv(lst, cls):
        return "".join(f'<tr><td>{x["name"]}</td><td class="{cls}">{x["price"]:,.1f}</td>'
                       f'<td class="sub">{(x["price"]/p["last"]-1)*100:+.2f}%</td></tr>' for x in lst)
    def card(t, pl, sc, cls):
        sgn = 1 if pl["side"] == "long" else -1
        return f"""<div class="card {cls}">
        <h3>{t}<span class="badge">{sc}/7</span></h3>
<table class="kv">
<tr><td>挂单入场 (post-only)</td><td class="big">{pl['entry_limit']:,.1f}</td><td class="sub">{(pl['entry_limit']/p['last']-1)*100:+.2f}%</td></tr>
<tr><td>追价触发 (stop-limit)</td><td>{pl['entry_break']:,.1f}</td><td class="sub">备选</td></tr>
<tr class="sl"><td>止损 SL</td><td class="big">{pl['sl']:,.1f}</td><td class="sub">{(pl['sl']/pl['entry_limit']-1)*100:+.2f}%</td></tr>
<tr class="tp"><td>止盈 T1（平50%）</td><td>{pl['tp1']:,.1f}</td><td class="sub">{pl['tp1_pct']:+.2f}%</td></tr>
<tr class="tp"><td>止盈 T2（平30%）</td><td>{pl['tp2']:,.1f}</td><td class="sub">{pl['tp2_pct']:+.2f}%</td></tr>
<tr class="tp"><td>止盈 T3（尾仓）</td><td>{pl['tp3']:,.1f}</td><td class="sub">{pl['tp3_pct']:+.2f}% · 1:{pl['rr3']:.1f}</td></tr>
<tr class="liq"><td>强平价</td><td>{pl['liq']:,.1f}</td><td class="sub">距入场 {pl['liq_dist_pct']:.2f}%</td></tr>
<tr><td>建议仓位</td><td>{pl['contracts']:.2f} 张</td><td class="sub">{pl['notional']:,.0f} U ({pl['notional_x']:.2f}x)</td></tr>
<tr><td>保证金占用</td><td>{pl['margin']:,.1f} U</td><td class="sub">安全上限 {pl['safe_notional_x']:.0f}x</td></tr>
</table></div>"""
    rows = "".join(f"<tr><td>{k}</td><td>{v['close']:,.1f}</td><td>{v['ema144']:,.1f}</td>"
                   f"<td>{v['atrp']}</td><td>{v['rsi14']}</td><td>{v['vol_ratio']}</td></tr>"
                   for k, v in p["tf"].items())
    hot = "hot" if p["main_side"] == "long" else ""
    hot2 = "hot" if p["main_side"] == "short" else ""
    doc = f"""<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<title>{p['contract']} 双向交易计划</title><style>
:root{{--bg:#f6f7f9;--card:#fff;--bd:#e4e7eb;--tx:#1f2328;--sub:#6b7280;--up:#d92b2b;--dn:#0f9960;--ac:#2563eb}}
*{{box-sizing:border-box}}body{{margin:0;padding:26px;background:var(--bg);color:var(--tx);
font:14px/1.65 -apple-system,"PingFang SC","Microsoft YaHei",sans-serif}}
.wrap{{max-width:1060px;margin:0 auto}}h1{{font-size:21px;margin:0 0 6px}}
.meta{{color:var(--sub);font-size:13px;margin-bottom:20px}}
.verdict{{background:#fff;border:1px solid var(--bd);border-left:4px solid var(--ac);border-radius:10px;
padding:13px 18px;margin-bottom:20px;font-weight:600}}
.grid{{display:grid;grid-template-columns:1fr 1fr;gap:16px;margin-bottom:20px}}
.card{{background:var(--card);border:1px solid var(--bd);border-radius:12px;padding:16px 20px}}
.card.hot{{border-color:var(--ac);box-shadow:0 0 0 3px rgba(37,99,235,.08)}}
h3{{margin:0 0 10px;font-size:15px;display:flex;align-items:center;gap:8px}}
.badge{{background:#eef2ff;color:var(--ac);border-radius:20px;padding:2px 9px;font-size:12px}}
table{{width:100%;border-collapse:collapse}}td{{padding:6px 4px;border-bottom:1px solid #f1f3f5}}
.kv td:nth-child(1){{color:var(--sub);width:44%}}
.kv td:nth-child(2){{text-align:right;font-variant-numeric:tabular-nums;font-weight:600}}
.kv td:nth-child(3){{text-align:right;width:82px;color:var(--sub);font-size:12px}}
.big{{font-size:16px;color:var(--ac)}}.sl td{{color:var(--dn)}}.tp td{{color:var(--up)}}
.liq td{{color:#b45309}}.sub{{font-size:12px}}
h2{{font-size:15px;margin:24px 0 10px}}
pre{{background:#fff;border:1px solid var(--bd);border-radius:12px;padding:16px;overflow:auto;
font:12px/1.5 ui-monospace,Menlo,monospace;white-space:pre-wrap}}
</style></head><body><div class="wrap">
<h1>{p['contract']}　实时行情分析 · 多空双向交易计划</h1>
<div class="meta">最新价 <b>{p['last']:,.1f}</b> ｜ 24h {p['change_24h_pct']:+.2f}% ｜
资金费率 {p['funding_rate_8h_pct']:+.4f}%/8h ｜ 生成 {time.strftime('%Y-%m-%d %H:%M:%S')}</div>
<div class="verdict">判定：{p['verdict']}　（做多 {p['score_long']}/7 ／ 做空 {p['score_short']}/7）</div>
<div class="grid">{card('做多 LONG', pb, p['score_long'], hot)}{card('做空 SHORT', ps, p['score_short'], hot2)}</div>
<h2>关键价位</h2><div class="grid">
<div class="card"><table>{lv(p['resistance'][::-1],'tp')}</table></div>
<div class="card"><table>{lv(p['support'],'sl')}</table></div></div>
<h2>多周期状态</h2><div class="card"><table>
<tr><td>周期</td><td>收盘</td><td>EMA144</td><td>ATR%</td><td>RSI14</td><td>量比</td></tr>{rows}</table></div>
<h2>完整输出</h2><pre>{H.escape(text)}</pre>
<div class="meta" style="margin-top:16px">⚠️ 历史统计结论，不构成投资建议；高杠杆合约存在爆仓风险。</div>
</div></body></html>"""
    open(path, "w", encoding="utf-8").write(doc)


if __name__ == "__main__":
    sys.exit(main())
