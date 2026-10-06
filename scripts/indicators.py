# -*- coding: utf-8 -*-
"""技术指标：EMA / TD Sequential (TD9) / RSI / ATR / 波动率 / 成交量"""
from __future__ import annotations
import numpy as np


def ema(arr: np.ndarray, n: int) -> np.ndarray:
    """指数移动平均，前置 NaN（不做前向填充，避免污染信号）"""
    a = np.asarray(arr, dtype=float)
    k = 2.0 / (n + 1.0)
    out = np.full_like(a, np.nan)
    if len(a) < n:
        return out
    # 用前 n 根 SMA 作为种子（标准做法）
    seed = np.mean(a[:n])
    out[n - 1] = seed
    for i in range(n, len(a)):
        out[i] = a[i] * k + out[i - 1] * (1 - k)
    return out


def sma(arr: np.ndarray, n: int) -> np.ndarray:
    a = np.asarray(arr, dtype=float)
    out = np.full_like(a, np.nan)
    if len(a) < n:
        return out
    cs = np.cumsum(a)
    out[n - 1:] = (cs[n - 1:] - np.concatenate(([0.0], cs[:-n]))) / n
    return out


def rsi(close: np.ndarray, n: int = 14) -> np.ndarray:
    c = np.asarray(close, dtype=float)
    d = np.diff(c, prepend=np.nan)
    gain = np.where(d > 0, d, 0.0)
    loss = np.where(d < 0, -d, 0.0)
    ag = np.full_like(c, np.nan)
    al = np.full_like(c, np.nan)
    if len(c) <= n:
        return ag
    ag[n] = np.nanmean(gain[1:n + 1])
    al[n] = np.nanmean(loss[1:n + 1])
    for i in range(n + 1, len(c)):
        ag[i] = (ag[i - 1] * (n - 1) + gain[i]) / n
        al[i] = (al[i - 1] * (n - 1) + loss[i]) / n
    rs = ag / np.where(al == 0, np.nan, al)
    return 100 - 100 / (1 + rs)


def atr(h: np.ndarray, l: np.ndarray, c: np.ndarray, n: int = 14) -> np.ndarray:
    h = np.asarray(h, float); l = np.asarray(l, float); c = np.asarray(c, float)
    pc = np.concatenate(([np.nan], c[:-1]))
    tr = np.maximum(h - l, np.maximum(np.abs(h - pc), np.abs(l - pc)))
    out = np.full_like(c, np.nan)
    if len(c) <= n:
        return out
    out[n] = np.nanmean(tr[1:n + 1])
    for i in range(n + 1, len(c)):
        out[i] = (out[i - 1] * (n - 1) + tr[i]) / n
    return out


def td_setup(close: np.ndarray, side: str = "buy") -> np.ndarray:
    """
    TD Sequential Setup（DeMark）
      buy  setup: 收盘价 < 4 根前的收盘价，连续计数 1..9
      sell setup: 收盘价 > 4 根前的收盘价，连续计数 1..9
    返回 count 数组（0 = 未计数，1..9 = 当前连号；中断归 0）
    """
    c = np.asarray(close, dtype=float)
    n = len(c)
    cnt = np.zeros(n, dtype=int)
    cur = 0
    for i in range(4, n):
        ok = (c[i] < c[i - 4]) if side == "buy" else (c[i] > c[i - 4])
        if ok:
            cur += 1
        else:
            cur = 0
        cnt[i] = min(cur, 13)
        if cur >= 9:
            cur = 0        # 完成一轮后重置，下一个 setup 重新计数
    return cnt


def td_signal(close: np.ndarray, side: str = "buy") -> np.ndarray:
    """bool 数组：本根收盘刚好完成第 9 根 setup"""
    return td_setup(close, side) == 9


def td_countdown(close: np.ndarray, low: np.ndarray, high: np.ndarray,
                 side: str = "buy", target: int = 13) -> np.ndarray:
    """
    TD Countdown：在 buy setup 完成之后开始计数，
    条件为 close[i] <= low[i-2]，累计到 13 触发（更严格、更稀有的抄底信号）。
    """
    c = np.asarray(close, float); lo = np.asarray(low, float); hi = np.asarray(high, float)
    n = len(c)
    out = np.zeros(n, dtype=int)
    setup9 = td_signal(c, "buy" if side == "buy" else "sell")
    cur = 0
    active = False
    for i in range(2, n):
        if setup9[i]:
            active = True
            cur = 0
            continue
        if not active:
            continue
        ok = (c[i] <= lo[i - 2]) if side == "buy" else (c[i] >= hi[i - 2])
        if ok:
            cur += 1
            out[i] = cur
            if cur >= target:
                active = False
                cur = 0
        # 若价格击穿对应 TDST 则 countdown 取消（简化：出现反向 setup9 时取消）
        if side == "buy" and td_signal(c, "sell")[i]:
            active = False
            cur = 0
    return out


def pct_distance(a, b) -> np.ndarray:
    """(a-b)/b * 100"""
    a = np.asarray(a, float); b = np.asarray(b, float)
    return (a - b) / b * 100.0


# ==================== 斐波那契（Fibonacci）====================
FIB_RET  = (0.236, 0.382, 0.500, 0.618, 0.786)      # 回调位
FIB_EXT  = (1.272, 1.618, 2.618)                    # 扩展位（止盈目标）


def swing_pivots(high, low, k: int = 5):
    """
    识别摆动高低点（ZigZag / 分形 pivot），**无未来函数**：
      bar i 被判定为 swing high 当且仅当 high[i] 是 [i-k, i+k] 内唯一最大值；
      但它的「确认时间」是 i+k（右侧 k 根走完才知道），因此实时只能使用
      确认时间 <= 当前 bar 的 pivot。

    :return: (is_h, conf_h, is_l, conf_l)
             is_h[i]   : bool，bar i 是否为 swing high
             conf_h[i] : 该 pivot 可供使用的最早 bar 下标（i+k），未判定为 -1
    """
    h = np.asarray(high, float); l = np.asarray(low, float)
    n = len(h)
    is_h = np.zeros(n, bool); is_l = np.zeros(n, bool)
    conf_h = np.full(n, -1, int); conf_l = np.full(n, -1, int)
    if n < 2 * k + 1:
        return is_h, conf_h, is_l, conf_l
    for i in range(k, n - k):
        w_h = h[i - k:i + k + 1]
        w_l = l[i - k:i + k + 1]
        if h[i] >= w_h.max() and (w_h == w_h.max()).sum() == 1:
            is_h[i] = True; conf_h[i] = i + k
        if l[i] <= w_l.min() and (w_l == w_l.min()).sum() == 1:
            is_l[i] = True; conf_l[i] = i + k
    return is_h, conf_h, is_l, conf_l


def fib_retr(lo: float, hi: float) -> dict:
    """
    回调位（从 hi 往 lo 方向回撤）：
      上涨段 lo→hi 后回调 → 支撑位 = hi - (hi-lo)*r
    """
    rng = hi - lo
    return {r: hi - rng * r for r in FIB_RET}


def fib_retr_up(lo: float, hi: float) -> dict:
    """
    反弹位（下跌段 hi→lo 后反弹，用于做空）：
      阻力位 = lo + (hi-lo)*r
    """
    rng = hi - lo
    return {r: lo + rng * r for r in FIB_RET}


def fib_extend(lo: float, hi: float) -> dict:
    """扩展位：上涨段 lo→hi 突破后的目标位 = lo + (hi-lo)*r"""
    rng = hi - lo
    return {r: lo + rng * r for r in FIB_EXT}


def last_swing_leg(high, low, is_h, conf_h, is_l, conf_l, t: int):
    """
    在 bar t（含）之前**已确认**的 pivot 中，取最近一个 high 与最近一个 low 构成「一段」。
    :return: (kind, lo_val, hi_val, lo_idx, hi_idx)
             kind = "up"   → 先 low 后 high（上涨段 → 等回调做多）
             kind = "down" → 先 high 后 low（下跌段 → 等反弹做空）
             kind = None   → 数据不足
    """
    h = np.asarray(high, float); l = np.asarray(low, float)
    hs = [int(i) for i in np.where(is_h)[0] if 0 <= conf_h[i] <= t]
    ls = [int(i) for i in np.where(is_l)[0] if 0 <= conf_l[i] <= t]
    if not hs or not ls:
        return None, None, None, None, None
    h_i, l_i = hs[-1], ls[-1]
    lo_val, hi_val = float(l[l_i]), float(h[h_i])
    if l_i < h_i:
        return "up", lo_val, hi_val, l_i, h_i
    return "down", lo_val, hi_val, l_i, h_i


# ==================== 趋势线（低点连低点 / 高点连高点）====================

def _linfit(xs: np.ndarray, ys: np.ndarray) -> tuple:
    """最小二乘线性拟合 y = a*x + b。返回 (a, b)。"""
    if len(xs) < 2:
        return (0.0, 0.0)
    a, b = np.polyfit(xs, ys, 1)
    return float(a), float(b)


def trendline_low_low(low: np.ndarray, is_l: np.ndarray, conf_l: np.ndarray, t: int,
                      min_touches: int = 3, max_lookback: int = 200):
    """
    上升趋势线：取最近 max_lookback 根 bar 内、已确认的低点 pivot，
    用最近 min_touches 个低点做线性拟合，返回 dict 或 None。
      斜率 a > 0 表示上升趋势线
      后续 bar x 处的趋势线值 = a*x + b

    :param min_touches: 至少需要几个 pivot 才拟合（推荐 3 个，2 个容易被噪音牵着走）
    :param max_lookback: 最多回溯多少根 bar
    :return: dict 或 None（数据不足时）
       {
         'slope': float,           # 每根 bar 上升/下降点数
         'intercept': float,
         'idx_start': int,
         'idx_end': int,
         'value_now': float,       # 趋势线在当前 bar (t) 处的值
         'pivots': [(idx, price), ...]
       }
    """
    l = np.asarray(low, float)
    ls = [int(i) for i in np.where(is_l)[0] if 0 <= conf_l[i] <= t]
    if len(ls) < min_touches:
        return None
    # 取最近的 min_touches 个低点（最多回溯 max_lookback 根）
    ls = [i for i in ls if i >= t - max_lookback]
    if len(ls) < min_touches:
        return None
    pivots = ls[-min_touches:]
    xs = np.array(pivots, dtype=float)
    ys = np.array([float(l[i]) for i in pivots], dtype=float)
    a, b = _linfit(xs, ys)
    return {
        'slope': a,
        'intercept': b,
        'idx_start': int(xs[0]),
        'idx_end': int(xs[-1]),
        'value_now': float(a * t + b),
        'pivots': [(int(i), float(l[i])) for i in pivots],
    }


def trendline_high_high(high: np.ndarray, is_h: np.ndarray, conf_h: np.ndarray, t: int,
                        min_touches: int = 3, max_lookback: int = 200):
    """
    下降趋势线：取最近 max_lookback 根 bar 内、已确认的高点 pivot，
    用最近 min_touches 个高点做线性拟合，返回 dict 或 None。
      斜率 a < 0 表示下降趋势线
    """
    h = np.asarray(high, float)
    hs = [int(i) for i in np.where(is_h)[0] if 0 <= conf_h[i] <= t]
    if len(hs) < min_touches:
        return None
    hs = [i for i in hs if i >= t - max_lookback]
    if len(hs) < min_touches:
        return None
    pivots = hs[-min_touches:]
    xs = np.array(pivots, dtype=float)
    ys = np.array([float(h[i]) for i in pivots], dtype=float)
    a, b = _linfit(xs, ys)
    return {
        'slope': a,
        'intercept': b,
        'idx_start': int(xs[0]),
        'idx_end': int(xs[-1]),
        'value_now': float(a * t + b),
        'pivots': [(int(i), float(h[i])) for i in pivots],
    }
