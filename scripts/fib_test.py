# -*- coding: utf-8 -*-
"""
斐波那契回调策略 —— 真实数据实证回测（Gate.io BTC_USDT 永续，USDT 本位）
========================================================================
要回答的问题：
  Q1  Fib 回调位（23.6/38.2/50/61.8/78.6）单独用作入场，有没有 alpha？
  Q2  哪一档最好？黄金区（50~61.8）是否真的优于浅回调？
  Q3  加趋势过滤（顺 4h EMA144 方向）后是否改善？
  Q4  Fib 与 TD9 叠加是否优于各自单独？
  Q5  与「随机择时」相比显著吗？（蒙特卡洛置换检验）

无未来函数：
  * pivot 需右侧 k 根走完才确认，确认时间 = i+k，信号只能用 conf <= t 的 pivot
  * 信号在 bar t 收盘确认，bar t+1 开盘成交
  * 同根内先判止损（保守）
  * 计入 Gate 真实 taker 0.075%（并对照 maker -0.01%）
"""
from __future__ import annotations
import sys, os, json
import numpy as np
import pandas as pd

# 路径全部相对推导：SK = 本脚本所在目录（scripts/），跟随 skill 实际安装位置
SK = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SK)

from gate_fetch import DATA_DIR                                          # noqa: E402
from indicators import (ema, atr, rsi, td_signal, swing_pivots,
                        fib_retr, fib_retr_up, last_swing_leg, FIB_RET)  # noqa
from backtest import DEFAULT_COUNTS                                      # noqa: E402

ROOT = os.path.dirname(DATA_DIR)      # 项目根（DATA_DIR = <项目根>/data，已自动探测）

TAKER = 0.00075
MAKER = -0.0001
INTERVAL_SEC = {"15m": 900, "1h": 3600, "4h": 14400, "1d": 86400}
MAIN = sys.argv[1] if len(sys.argv) > 1 else "4h"
REFRESH = os.environ.get("BTC_NO_REFRESH", "") != "1"   # 默认每次都联网刷新


# ---------------- 数据 ----------------
# 每次执行都会先联网增量拉新（每周期 1 次 HTTP），再读缓存，保证回测用最新数据。
# 离线模式：BTC_NO_REFRESH=1 python fib_test.py 4h
def load(tf: str):
    from gate_fetch import fetch_ohlcv
    import datetime as _dt
    rows = fetch_ohlcv("BTC_USDT", tf, DEFAULT_COUNTS.get(tf, 5000),
                       cache=True, refresh=REFRESH, verbose=False)
    if not rows:
        raise FileNotFoundError(f"缺 {tf} 数据，请先运行 update_data.py")
    arr = np.array(rows, dtype=float)
    U = getattr(_dt, "UTC", None) or _dt.timezone.utc
    t0 = _dt.datetime.fromtimestamp(arr[0][0], U).strftime("%Y-%m-%d %H:%M")
    t1 = _dt.datetime.fromtimestamp(arr[-1][0], U).strftime("%Y-%m-%d %H:%M")
    print(f"[{tf}] {len(arr)} 根  {t0} ~ {t1} UTC"
          f"{'' if REFRESH else '  （离线缓存）'}")
    return pd.DataFrame({"ts": arr[:, 0], "open": arr[:, 1], "high": arr[:, 2],
                         "low": arr[:, 3], "close": arr[:, 4], "vol": arr[:, 5]})


d = load(MAIN)
n = len(d)
ts = d.ts.values
o, h, l, c = d.open.values, d.high.values, d.low.values, d.close.values
d["e144"] = ema(c, 144)
d["atr"] = atr(h, l, c, 14)
d["atrp"] = d.atr / c * 100
ATRP = d.atrp.values

# 4h 趋势过滤（主周期为 1h 时用 4h；主周期 4h 时用 1d）
HTF = "1d" if MAIN == "4h" else "4h"
try:
    hd = load(HTF)
    hd["e144"] = ema(hd.close.values, 144)
    _idx = np.searchsorted(hd.ts.values, ts, side="right") - 1
    _idx = np.clip(_idx, 0, len(hd) - 1)
    HTF_UP = (hd.close.values[_idx] > hd.e144.values[_idx])
except Exception as e:
    print("高周期加载失败:", e)
    HTF_UP = np.ones(n, bool)


# ---------------- Fib 信号 ----------------
def pivot_cache(k=5):
    return swing_pivots(h, l, k)


def _pivot_index(is_h, conf_h, is_l, conf_l):
    """预计算 pivot 索引与确认时间（升序），供 O(log n) 查询最近已确认 pivot"""
    h_idx = np.where(is_h)[0]; h_conf = conf_h[h_idx]
    l_idx = np.where(is_l)[0]; l_conf = conf_l[l_idx]
    return h_idx, h_conf, l_idx, l_conf


def _leg_at(t, h_idx, h_conf, l_idx, l_conf):
    """O(log n) 版 last_swing_leg"""
    mh = np.searchsorted(h_conf, t, side="right") - 1
    ml = np.searchsorted(l_conf, t, side="right") - 1
    if mh < 0 or ml < 0:
        return None, None, None, None, None
    h_i, l_i = int(h_idx[mh]), int(l_idx[ml])
    lo_v, hi_v = float(l[l_i]), float(h[h_i])
    if l_i < h_i:
        return "up", lo_v, hi_v, l_i, h_i
    return "down", lo_v, hi_v, l_i, h_i


def fib_signals(r_lo, r_hi, k=5, require_touch=True, use_trend=None,
                band=None, td_filter=None):
    """
    :param r_lo, r_hi: 回调比例区间，例如 0.5, 0.618 → 触及该区间任一位即信号
    :param use_trend:  None 不过滤 / "up" 只在高周期多头时做多 / "down" 只看空
    :param band:       None 用 r_lo~r_hi 区间；或给具体单个 r
    :return: (sigL, sigS) bool 数组
    """
    is_h, conf_h, is_l, conf_l = pivot_cache(k)
    h_idx, h_conf, l_idx, l_conf = _pivot_index(is_h, conf_h, is_l, conf_l)
    sigL = np.zeros(n, bool); sigS = np.zeros(n, bool)
    for t in range(2 * k + 1, n - 1):
        kind, lo_v, hi_v, lo_i, hi_i = _leg_at(t, h_idx, h_conf, l_idx, l_conf)
        if kind is None or hi_v <= lo_v:
            continue
        if kind == "up":
            lv = fib_retr(lo_v, hi_v)
            lo_b = lv[min(FIB_RET, key=lambda x: abs(x - r_hi))]
            hi_b = lv[min(FIB_RET, key=lambda x: abs(x - r_lo))]
            zone_lo, zone_hi = min(lo_b, hi_b), max(lo_b, hi_b)
            # 价格触及回调区（最低价进入区间）且收盘仍在区间上方（未有效跌破）
            if l[t] <= zone_hi and c[t] >= zone_lo and l[t] >= lo_v * 0.995:
                if td_filter is not None and not td_filter[t]:
                    continue
                if use_trend in (None, "up") or (use_trend == "up" and HTF_UP[t]):
                    sigL[t] = True
        else:
            lv = fib_retr_up(lo_v, hi_v)
            lo_b = lv[min(FIB_RET, key=lambda x: abs(x - r_lo))]
            hi_b = lv[min(FIB_RET, key=lambda x: abs(x - r_hi))]
            zone_lo, zone_hi = min(lo_b, hi_b), max(lo_b, hi_b)
            if h[t] >= zone_lo and c[t] <= zone_hi and h[t] <= hi_v * 1.005:
                if td_filter is not None and not td_filter[t]:
                    continue
                if use_trend in (None, "down") or (use_trend == "down" and not HTF_UP[t]):
                    sigS[t] = True
    return sigL, sigS


# ---------------- 双向回测引擎 ----------------
def run2(sigL, sigS, sl=0.02, tp_mode="leg", fee=TAKER, max_bars=48,
         day_close=True, tp_fixed=None, is_h=None, conf_h=None, is_l=None, conf_l=None):
    """
    tp_mode: "leg"  → 目标 = 摆动段起点（做多目标 swing high、做空目标 swing low）
             "fixed"→ tp_fixed 百分比
    """
    recs = []
    h_idx, h_conf, l_idx, l_conf = _pivot_index(is_h, conf_h, is_l, conf_l)
    for side, sig in (("long", sigL), ("short", sigS)):
        for i in np.where(sig)[0]:
            j = i + 1
            if j >= n - 2:
                continue
            entry = o[j]
            sl_p = entry * (1 - sl) if side == "long" else entry * (1 + sl)
            if tp_mode == "leg" and tp_fixed is None:
                kind, lo_v, hi_v, _, _ = _leg_at(i, h_idx, h_conf, l_idx, l_conf)
                if kind is None:
                    continue
                tgt = hi_v if side == "long" else lo_v
                if side == "long" and tgt <= entry * 1.002:
                    continue
                if side == "short" and tgt >= entry * 0.998:
                    continue
                tp_p = tgt
            else:
                tp_p = entry * (1 + tp_fixed) if side == "long" else entry * (1 - tp_fixed)
            mae = 0.0
            exit_p, reason, k = np.nan, "timeout", j
            day_end = j + int((86400 - (ts[j] % 86400)) // INTERVAL_SEC[MAIN])
            limit = min(j + max_bars, n - 1, day_end if day_close else n - 1)
            for b in range(j, limit + 1):
                if side == "long":
                    mae = min(mae, (l[b] - entry) / entry)
                    if l[b] <= sl_p: exit_p, reason, k = sl_p, "stop", b; break
                    if h[b] >= tp_p: exit_p, reason, k = tp_p, "take", b; break
                else:
                    mae = min(mae, (entry - h[b]) / entry)
                    if h[b] >= sl_p: exit_p, reason, k = sl_p, "stop", b; break
                    if l[b] <= tp_p: exit_p, reason, k = tp_p, "take", b; break
                if b == limit:
                    exit_p, reason, k = c[b], "dayclose" if day_close else "timeout", b
            if np.isnan(exit_p):
                continue
            gross = (exit_p - entry) / entry if side == "long" else (entry - exit_p) / entry
            net = gross - fee * 2
            recs.append({"side": side, "i": i, "entry": entry, "exit": exit_p,
                         "net": net * 100, "r": net / sl, "mae": -mae * 100,
                         "held": k - j + 1, "reason": reason, "win": net > 0})
    return pd.DataFrame(recs)


def summ(t, tag=""):
    if t is None or len(t) < 6:
        print(f"{tag:<52} 样本不足 n={0 if t is None else len(t)}")
        return None
    w = t[t.net > 0]; ls = t[t.net <= 0]
    pf = w.net.sum() / abs(ls.net.sum()) if len(ls) and ls.net.sum() != 0 else np.inf
    print(f"{tag:<52} n={len(t):<4} 胜率={len(w)/len(t)*100:5.1f}% EV={t.net.mean():+6.2f}% "
          f"EV(R)={t.r.mean():+5.2f}R PF={pf:5.2f} 总={t.net.sum():+8.1f}% "
          f"MAE中位={np.percentile(t.mae,50):.2f}%")
    return {"n": len(t), "wr": len(w)/len(t)*100, "ev": t.net.mean(),
            "ev_r": t.r.mean(), "pf": pf, "total": t.net.sum()}


def perm_test(t, n_iter=3000, seed=7):
    """置换检验：把信号时间随机平移，看真实 EV 在零分布中的分位"""
    if t is None or len(t) < 6:
        return None
    real = t.net.mean()
    rng = np.random.default_rng(seed)
    idx_all = t.i.values
    wins = t.win.values
    cnt = len(t)
    null = np.empty(n_iter)
    span = n - 60
    for b in range(n_iter):
        # 随机择时：随机挑 cnt 个 bar 入场，用同样的持仓分布近似
        # 采用「随机入场 + 相同持有根数」的净收益分布作为零假设
        starts = rng.integers(60, span, size=cnt)
        holds = rng.choice(t.held.values, size=cnt, replace=True)
        rets = []
        for s0, hd_ in zip(starts, holds):
            e = o[s0]
            ex = c[min(s0 + int(hd_), n - 1)]
            rets.append((ex - e) / e * 100 - TAKER * 200)
        null[b] = np.mean(rets)
    p = float((null >= real).mean())
    return real, float(null.mean()), p


print(f"\n{'='*100}")
print(f"  斐波那契回调策略实证 · BTC_USDT 永续（USDT 本位）· 主周期 {MAIN} · 高周期过滤 {HTF}")
print(f"{'='*100}")

is_h, conf_h, is_l, conf_l = pivot_cache(5)
print(f"pivot(k=5)：swing high {int(is_h.sum())} 个 / swing low {int(is_l.sum())} 个\n")

# ========== 【A】各回调档位单独测试（双向，目标=段起点，SL 2%）==========
print(f"【A】单档位测试（SL 2%，目标 = 摆动段起点，日内平仓，taker 0.075%）")
print(f"{'-'*100}")
resA = {}
for r in FIB_RET:
    sL, sS = fib_signals(r, r, k=5)
    tL = run2(sL, np.zeros(n, bool), sl=0.02, tp_mode="leg",
              is_h=is_h, conf_h=conf_h, is_l=is_l, conf_l=conf_l)
    tS = run2(np.zeros(n, bool), sS, sl=0.02, tp_mode="leg",
              is_h=is_h, conf_h=conf_h, is_l=is_l, conf_l=conf_l)
    summ(tL, f"  Fib {r:.3f}  做多（回调买入）")
    summ(tS, f"  Fib {r:.3f}  做空（反弹卖出）")
    resA[r] = (tL, tS)

# ========== 【B】黄金区 0.5~0.618 ==========
print(f"\n【B】区间合并（0.382~0.618『黄金区』vs 浅回调 0.236~0.382 vs 深回调 0.618~0.786）")
print(f"{'-'*100}")
for tag, (a, b) in [("浅回调 0.236~0.382", (0.236, 0.382)),
                    ("黄金区 0.382~0.618", (0.382, 0.618)),
                    ("深回调 0.618~0.786", (0.618, 0.786)),
                    ("全区间 0.236~0.786", (0.236, 0.786))]:
    sL, sS = fib_signals(a, b, k=5)
    tL = run2(sL, np.zeros(n, bool), sl=0.02, tp_mode="leg",
              is_h=is_h, conf_h=conf_h, is_l=is_l, conf_l=conf_l)
    tS = run2(np.zeros(n, bool), sS, sl=0.02, tp_mode="leg",
              is_h=is_h, conf_h=conf_h, is_l=is_l, conf_l=conf_l)
    summ(pd.concat([tL, tS]) if len(tL) and len(tS) else (tL if len(tL) else tS),
         f"  {tag}（双向合并）")
    summ(tL, f"  {tag} · 仅做多")
    summ(tS, f"  {tag} · 仅做空")

# ========== 【C】趋势过滤 ==========
print(f"\n【C】加高周期趋势过滤（{HTF} 在 EMA144 上方只做多 / 下方只做空）")
print(f"{'-'*100}")
sL, sS = fib_signals(0.382, 0.618, k=5)
tL = run2(sL, np.zeros(n, bool), sl=0.02, tp_mode="leg",
          is_h=is_h, conf_h=conf_h, is_l=is_l, conf_l=conf_l)
tS = run2(np.zeros(n, bool), sS, sl=0.02, tp_mode="leg",
          is_h=is_h, conf_h=conf_h, is_l=is_l, conf_l=conf_l)
summ(tL, f"  黄金区做多 · 无过滤（基准）")
summ(tS, f"  黄金区做空 · 无过滤（基准）")
tL = run2(sL & HTF_UP, np.zeros(n, bool), sl=0.02, tp_mode="leg",
          is_h=is_h, conf_h=conf_h, is_l=is_l, conf_l=conf_l)
tS = run2(np.zeros(n, bool), sS & (~HTF_UP), sl=0.02, tp_mode="leg",
          is_h=is_h, conf_h=conf_h, is_l=is_l, conf_l=conf_l)
summ(tL, f"  黄金区做多 · 仅 {HTF} 多头环境（顺势）")
summ(tS, f"  黄金区做空 · 仅 {HTF} 空头环境（顺势）")
summ(pd.concat([tL, tS]), f"  黄金区双向 · 顺势过滤后合并")
tL2 = run2(sL & (~HTF_UP), np.zeros(n, bool), sl=0.02, tp_mode="leg",
           is_h=is_h, conf_h=conf_h, is_l=is_l, conf_l=conf_l)
tS2 = run2(np.zeros(n, bool), sS & HTF_UP, sl=0.02, tp_mode="leg",
           is_h=is_h, conf_h=conf_h, is_l=is_l, conf_l=conf_l)
summ(pd.concat([tL2, tS2]), f"  黄金区双向 · 逆势（对照组）")

# ========== 【D】Fib × TD9 叠加 ==========
print(f"\n【D】Fib 与 TD9 叠加（TD9 完成后 N 根内触及 Fib 位）")
print(f"{'-'*100}")
td_b = td_signal(c, "buy")
td_s = td_signal(c, "sell")


def td_window(arr, win=12):
    """TD9 完成后的 win 根窗口内为 True"""
    out = np.zeros(n, bool)
    for i in np.where(arr)[0]:
        out[i:min(i + win + 1, n)] = True
    return out


w_b, w_s = td_window(td_b, 12), td_window(td_s, 12)
sL0, sS0 = fib_signals(0.382, 0.618, k=5)
for tag, (fl, fs) in [("纯 Fib 黄金区", (np.ones(n, bool), np.ones(n, bool))),
                      ("Fib + TD9抄底窗口(做多)", (w_b, np.ones(n, bool))),
                      ("Fib + TD9逃顶窗口(做空)", (np.ones(n, bool), w_s))]:
    tL = run2(sL0 & fl, np.zeros(n, bool), sl=0.02, tp_mode="leg",
              is_h=is_h, conf_h=conf_h, is_l=is_l, conf_l=conf_l)
    tS = run2(np.zeros(n, bool), sS0 & fs, sl=0.02, tp_mode="leg",
              is_h=is_h, conf_h=conf_h, is_l=is_l, conf_l=conf_l)
    if "做多" in tag:
        summ(tL, f"  {tag}")
    elif "做空" in tag:
        summ(tS, f"  {tag}")
    else:
        summ(tL, f"  {tag} · 做多"); summ(tS, f"  {tag} · 做空")

# ========== 【E】固定 R:R 出场对比 ==========
print(f"\n【E】出场方式对比（黄金区做多，SL 2%）")
print(f"{'-'*100}")
for tp in (0.02, 0.03, 0.04, 0.06):
    tL = run2(sL0, np.zeros(n, bool), sl=0.02, tp_mode="fixed", tp_fixed=tp,
              is_h=is_h, conf_h=conf_h, is_l=is_l, conf_l=conf_l)
    summ(tL, f"  SL2% / TP{tp:.0%}（1:{tp/0.02:.0f}）")
tL = run2(sL0, np.zeros(n, bool), sl=0.02, tp_mode="leg",
          is_h=is_h, conf_h=conf_h, is_l=is_l, conf_l=conf_l)
summ(tL, "  SL2% / 目标=段起点")

# ========== 【F】手续费敏感性 ==========
print(f"\n【F】手续费敏感性（黄金区做多，SL2%，目标=段起点）")
print(f"{'-'*100}")
for tag, fee in [("taker 0.075%", TAKER), ("maker -0.01%（返佣）", MAKER)]:
    tL = run2(sL0, np.zeros(n, bool), sl=0.02, tp_mode="leg", fee=fee,
              is_h=is_h, conf_h=conf_h, is_l=is_l, conf_l=conf_l)
    summ(tL, f"  {tag}")

# ========== 【G】显著性检验 ==========
print(f"\n【G】蒙特卡洛置换检验（随机择时零分布，2000 次）")
print(f"{'-'*100}")
for tag, tt in [("裸 Fib 黄金区做多", run2(sL0, np.zeros(n, bool), sl=0.02, tp_mode="leg",
                                        is_h=is_h, conf_h=conf_h, is_l=is_l, conf_l=conf_l)),
                ("Fib + TD9 抄底窗口 做多", run2(sL0 & w_b, np.zeros(n, bool), sl=0.02, tp_mode="leg",
                                          is_h=is_h, conf_h=conf_h, is_l=is_l, conf_l=conf_l))]:
    if len(tt) >= 6:
        r = perm_test(tt, n_iter=(800 if n > 6000 else 2000))
        if r:
            real, null_m, p = r
            print(f"  {tag:<26} 真实 EV {real:+.3f}% ｜ 随机基准 {null_m:+.3f}% ｜ p = {p:.3f}"
                  + ("  ✅ 显著" if p < 0.05 else "  ❌ 不显著"))


# ========== 【H】Fib × TD9 全档位扫描（最有希望的组合，重点验证）==========
print(f"\n【H】Fib × TD9 抄底窗口 —— 各档位扫描（做多，SL2%，目标=段起点）")
print(f"{'-'*100}")
best = None
for r in FIB_RET:
    sL_r, _ = fib_signals(r, r, k=5)
    t = run2(sL_r & w_b, np.zeros(n, bool), sl=0.02, tp_mode="leg",
             is_h=is_h, conf_h=conf_h, is_l=is_l, conf_l=conf_l)
    summ(t, f"  Fib {r:.3f} + TD9窗口 · 做多")
    if len(t) >= 20 and (best is None or t.net.mean() > best[1]):
        best = (r, t.net.mean(), t)
for tag, (a, b) in [("0.5~0.618", (0.5, 0.618)), ("0.618~0.786", (0.618, 0.786)),
                    ("0.382~0.786", (0.382, 0.786))]:
    sL_r, _ = fib_signals(a, b, k=5)
    t = run2(sL_r & w_b, np.zeros(n, bool), sl=0.02, tp_mode="leg",
             is_h=is_h, conf_h=conf_h, is_l=is_l, conf_l=conf_l)
    summ(t, f"  区间 {tag} + TD9窗口 · 做多")
    if len(t) >= 20 and (best is None or t.net.mean() > best[1]):
        best = (tag, t.net.mean(), t)

print(f"\n【H2】最优组合的稳健性（邻近参数网格 —— 检查是否过拟合）")
print(f"{'-'*100}")
if best is not None:
    print(f"  最优：{best[0]}  EV {best[1]:+.2f}%")
    print(f"  {'SL':<6}{'参数':<14}{'n':>5}{'胜率':>8}{'EV':>9}{'PF':>7}")
    evs = []
    for sl in (0.015, 0.02, 0.025, 0.03):
        for kk in (3, 5, 7):
            ih, ch, il, cl = swing_pivots(h, l, kk)
            sL_k, _ = fib_signals(0.5, 0.618, k=kk)
            t = run2(sL_k & w_b, np.zeros(n, bool), sl=sl, tp_mode="leg",
                     is_h=ih, conf_h=ch, is_l=il, conf_l=cl)
            if len(t) >= 10:
                w = t[t.net > 0]; ls = t[t.net <= 0]
                pf = w.net.sum()/abs(ls.net.sum()) if len(ls) and ls.net.sum() != 0 else np.inf
                print(f"  {sl:<6.3f}{'k='+str(kk):<14}{len(t):>5}{len(w)/len(t)*100:>7.1f}%"
                      f"{t.net.mean():>+9.2f}%{pf:>7.2f}")
                evs.append(t.net.mean())
    if evs:
        print(f"  → 网格 EV 均值 {np.mean(evs):+.2f}%，为正比例 {sum(1 for x in evs if x>0)/len(evs)*100:.0f}%")


# ========== 【I】共振（Confluence）测试 ==========
print(f"\n【I】共振测试：Fib 位 ±0.4% 内是否另有结构位（EMA144 / EMA34 / 日线枢轴 P·R1·S1）")
print(f"{'-'*100}")
try:
    d["e34"] = ema(c, 34)
    # 日线枢轴（按 UTC 自然日分组），前向对齐，只用已收盘的日线
    hdf = hd.copy()
    hdf["day"] = pd.to_datetime(hdf.ts, unit="s").dt.floor("D")
    gp = hdf.groupby("day").agg(H=("high", "max"), L=("low", "min"), C=("close", "last"))
    gp["P"] = (gp.H + gp.L + gp.C) / 3
    gp["R1"] = 2 * gp.P - gp.L
    gp["S1"] = 2 * gp.P - gp.H
    piv_ts = gp.index.values.astype("datetime64[s]").astype(np.int64)
    piv_P, piv_R1, piv_S1 = gp.P.values, gp.R1.values, gp.S1.values

    def piv_at(t):
        j = np.searchsorted(piv_ts, ts[t], side="left") - 1   # 只用昨天收盘后的枢轴
        if j < 0:
            return np.nan, np.nan, np.nan
        return piv_P[j], piv_R1[j], piv_S1[j]

    def has_conf(px, t, tol=0.004):
        P_, R1_, S1_ = piv_at(t)
        lv = [d.e144.values[t], d.e34.values[t], P_, R1_, S1_]
        return any((not np.isnan(x)) and abs(px - x) / px < tol for x in lv)

    sL_c, _ = fib_signals(0.382, 0.786, k=5)
    sig_idx = np.where(sL_c & w_b)[0]
    conf_flag = np.array([has_conf(o[i + 1], i) for i in sig_idx])
    for tag, mask in [("有共振（confluence）", conf_flag), ("无共振（孤立 Fib 位）", ~conf_flag)]:
        s = np.zeros(n, bool); s[sig_idx[mask]] = True
        t = run2(s, np.zeros(n, bool), sl=0.02, tp_mode="leg",
                 is_h=is_h, conf_h=conf_h, is_l=is_l, conf_l=conf_l)
        summ(t, f"  Fib+TD9 做多 · {tag}（n_sig={int(mask.sum())}）")
    # 对照组：不做任何 Fib 过滤的 TD9 信号
    s = np.zeros(n, bool)
    td_idx = np.where(td_b)[0]
    s[td_idx] = True
    summ(run2(s, np.zeros(n, bool), sl=0.02, tp_mode="leg",
              is_h=is_h, conf_h=conf_h, is_l=is_l, conf_l=conf_l),
         "  裸 TD9 做多（无 Fib 过滤，对照组）")
    for tag, mask in [("有共振", conf_flag), ("无共振", ~conf_flag)]:
        s = np.zeros(n, bool); s[sig_idx[mask]] = True
        t = run2(s, np.zeros(n, bool), sl=0.02, tp_mode="leg", fee=MAKER,
                 is_h=is_h, conf_h=conf_h, is_l=is_l, conf_l=conf_l)
        summ(t, f"  Fib+TD9 做多 · {tag} · maker 挂单")
except Exception as e:
    print("  共振测试失败:", type(e).__name__, e)

print(f"\n{'='*100}\n")
