# -*- coding: utf-8 -*-
"""
EMA144 + TD9 抄底做多策略 —— 组合回测引擎
==========================================
设计原则（避免未来函数 / 过度乐观）：
  * 信号在 bar i 收盘确认，bar i+1 开盘成交
  * 同一根 K 线内止损与止盈同时被触发 → 记为止损（保守）
  * 计入手续费（Gate.io 永续 taker 0.05% × 2）与资金费率成本
  * 不叠杠杆（收益率按名义本金计；杠杆仅放大结果，不改变胜率与方向）
"""
from __future__ import annotations
import os
import sys
import numpy as np
import pandas as pd

TAKER = 0.0005          # Gate.io 永续吃单费率 0.05%
FUND_PER_8H = 0.0001    # 资金费率假设 0.01%/8h（多头支付），可用真实数据替换

INTERVAL_SEC = {"15m": 900, "1h": 3600, "4h": 14400, "1d": 86400}


# ---------------- 数据加载 ----------------
# 默认【每次都先增量刷新到最新】再读缓存：每周期仅 1 次 HTTP 请求。
# 设置环境变量 BTC_NO_REFRESH=1 可切到离线模式（沿用上次缓存）。
DEFAULT_COUNTS = {"15m": 8000, "1h": 20000, "4h": 5000, "1d": 1500}


def load(tf: str, root: str = None, contract: str = "BTC_USDT",
         refresh: bool = None, verbose: bool = False):
    """
    取某周期 K 线 DataFrame。默认会先联网增量更新缓存，再读取。
    :param refresh: None → 由环境变量 BTC_NO_REFRESH 决定（默认 True=刷新）
    """
    import numpy as np
    import pandas as pd

    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from gate_fetch import fetch_ohlcv

    if refresh is None:
        refresh = os.environ.get("BTC_NO_REFRESH", "") != "1"
    total = DEFAULT_COUNTS.get(tf, 5000)
    rows = fetch_ohlcv(contract, tf, total, cache=True, refresh=refresh, verbose=verbose)
    if not rows:
        raise FileNotFoundError(f"缺少 {tf} 数据，请运行 update_data.py")
    arr = np.array(rows, dtype=float)
    return pd.DataFrame({
        "ts": arr[:, 0], "open": arr[:, 1], "high": arr[:, 2],
        "low": arr[:, 3], "close": arr[:, 4], "vol": arr[:, 5],
    })


# ---------------- 回测核心 ----------------
def run(sig, o, h, l, c, sl_pct, tp_pct, max_bars=200,
        exit_ema=None, exit_on_ema=False, trail=None, funding=True, tf="1h"):
    """
    :param sig:        bool 数组，bar i 收盘触发
    :param sl_pct:     止损百分比（0.03 = 3%）
    :param tp_pct:     止盈百分比
    :param max_bars:   最大持仓根数（超时按收盘价平仓）
    :param exit_ema:   EMA 数组；exit_on_ema=True 时收盘跌破 EMA 即出场
    :param trail:      移动止损回撤百分比（None 关闭）
    :return: (trades DataFrame, stats dict)
    """
    n = len(c)
    recs = []
    i = 0
    while i < n - 1:
        if not sig[i]:
            i += 1
            continue
        j = i + 1                       # 下一根开盘入场
        if j >= n - 1:
            break
        entry = o[j]
        sl = entry * (1 - sl_pct)
        tp = entry * (1 + tp_pct)
        peak = entry
        held = 0
        exit_p, exit_reason, exit_bar = np.nan, "timeout", j
        for k in range(j, min(j + max_bars, n)):
            held += 1
            if h[k] > peak:
                peak = h[k]
            # 保守：同根内先判定止损
            if l[k] <= sl:
                exit_p, exit_reason, exit_bar = sl, "stop", k
                break
            if h[k] >= tp:
                exit_p, exit_reason, exit_bar = tp, "take", k
                break
            # 移动止损
            if trail is not None:
                new_sl = peak * (1 - trail)
                if new_sl > sl:
                    sl = new_sl
                    if l[k] <= sl:
                        exit_p, exit_reason, exit_bar = sl, "trail", k
                        break
            # EMA 出场（收盘确认，下一根开盘平）
            if exit_on_ema and exit_ema is not None and not np.isnan(exit_ema[k]):
                if c[k] < exit_ema[k]:
                    if k + 1 < n:
                        exit_p, exit_reason, exit_bar = o[k + 1], "ema", k + 1
                    else:
                        exit_p, exit_reason, exit_bar = c[k], "ema", k
                    break
            # 超时
            if k == min(j + max_bars, n) - 1:
                exit_p, exit_reason, exit_bar = c[k], "timeout", k
        if np.isnan(exit_p):
            break
        gross = (exit_p - entry) / entry
        cost = TAKER * 2
        if funding:
            hrs = INTERVAL_SEC.get(tf, 3600) / 3600.0
            cost += FUND_PER_8H / 8.0 * held * hrs
        net = gross - cost
        recs.append({
            "i": i, "entry_bar": j, "exit_bar": exit_bar, "entry": entry,
            "exit": exit_p, "gross": gross * 100, "net": net * 100,
            "r": (net / sl_pct), "held": held, "reason": exit_reason,
            "win": net > 0,
        })
        i = exit_bar + 1

    t = pd.DataFrame(recs)
    if t.empty:
        return t, {"n": 0}
    wins = t[t.net > 0]
    losses = t[t.net <= 0]
    eq = t.net.cumsum() / 100.0                      # 名义收益率（单笔满仓）
    curve = (1 + eq).values
    mdd = float((np.maximum.accumulate(curve) - curve).max() / np.maximum.accumulate(curve).max())
    pf = (wins.net.sum() / abs(losses.net.sum())) if len(losses) and losses.net.sum() != 0 else np.inf
    stats = {
        "n": len(t),
        "win_rate": len(wins) / len(t) * 100,
        "avg_win": wins.net.mean() if len(wins) else 0,
        "avg_loss": losses.net.mean() if len(losses) else 0,
        "ev": t.net.mean(),
        "ev_r": t.r.mean(),
        "pf": pf,
        "total": t.net.sum(),
        "mdd": mdd * 100,
        "avg_held": t.held.mean(),
        "best": t.net.max(), "worst": t.net.min(),
    }
    return t, stats


def fmt(name, s):
    if s.get("n", 0) < 5:
        return f"{name:<46} 样本不足 n={s.get('n',0)}"
    return (f"{name:<46} n={s['n']:<4} 胜率={s['win_rate']:5.1f}%  "
            f"EV={s['ev']:+6.2f}%  EV(R)={s['ev_r']:+5.2f}R  PF={s['pf']:5.2f}  "
            f"总收益={s['total']:+8.1f}%  回撤={s['mdd']:5.1f}%  均持={s['avg_held']:.0f}根")
