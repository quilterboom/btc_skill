#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ref_trader_tools.py —— 通用工具箱：把「参考交易者」的可复刻部分工程化

## 这个模块为什么存在
2026-11-07 那一轮（复刻老师的交易模式）暴露了一个教训：
把参考方的判断**写成硬编码阈值**放进脚本 → 样本一变就要重写代码，
而且「代码长得像策略」会让没人再怀疑它是否成立。

正确的分层是：

    ┌─ 通用工具（这个模块，与具体是谁无关）────────────────┐
    │  spec_from_samples  从参考方样本反推规格（不看收益）  │
    │  bar_features       从 K 线算判读维度（可扩展）        │
    │  replicate          按规格在长历史上复刻               │
    │  fidelity_gate      保真度门：引擎能否复现参考方净值   │
    │  discrimination     区分度 + 置换检验 + BH-FDR 校正    │
    │  paired_sign_test   「套到我方现有单」的配对检验        │
    └────────────────────────────────────────────────────┘
    ┌─ 参考方特有 ───────────────────────────────────────┐
    │  teacher_spec.py   调上面的工具，产出老师的规格 JSON   │
    │                   （可重新生成，不是硬编码）           │
    └────────────────────────────────────────────────────┘

**规格是数据，不是代码。** 规格 JSON 由脚本从账本重新生成，
样本增长后重跑即可，不需要改任何逻辑。

## 只读
"""
from __future__ import annotations

import json
import math
import random
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Callable, Iterable, Optional

CST = timezone(timedelta(hours=8))


class BarSeries:
    """K 线特征容器 —— 通用，不含任何交易逻辑。

    ★ 锚点规则写死在构造里：所有特征只用【当前及之前】的已收 bar。
      任何重算特征的工具都该用它，别自己写循环（前视偏差的头号来源）。
    """

    def __init__(self, bars: list, tf_min: int = 60):
        self.bars = bars
        self.tf = tf_min
        self.tss = [int(b[0]) for b in bars]
        self.open = [float(b[1]) for b in bars]
        self.high = [float(b[2]) for b in bars]
        self.low = [float(b[3]) for b in bars]
        self.close = [float(b[4]) for b in bars]
        self.vol = [float(b[5]) for b in bars]
        self.e20 = ema_series(self.close, 20)
        self.e50 = ema_series(self.close, 50)
        self.atr = atr_series(bars, 14)

    def last_closed(self, ts: int) -> int:
        """ts 之前最后一根【已收】bar 的下标。"""
        return _last_closed_index(self.tss, ts)

    def vol_x(self, i: int, look: int = 20) -> Optional[float]:
        if i < look:
            return None
        base = sum(self.vol[i - look:i]) / look
        return self.vol[i] / base if base > 0 else None

    def drop_pct(self, i: int, look: int) -> Optional[float]:
        if i < look:
            return None
        return (self.close[i - look] - self.close[i]) / self.close[i - look] * 100

    def structure(self, i: int, look: int):
        if i < look:
            return None, None
        w = range(i - look + 1, i + 1)
        return min(self.low[j] for j in w), max(self.high[j] for j in w)


# 旧名兼容（历史脚本用 SigGen）
SigGen = BarSeries


# ---------------------------------------------------------------- 规格反推
def spec_from_samples(samples: list[dict],
                      keys: Optional[Iterable[str]] = None,
                      coverages=(0.5, 0.6, 0.7, 0.8, 0.9),
                      lower_is_tighter: Optional[dict] = None) -> dict:
    """从参考方样本反推「规格」：每个维度的阈值按【覆盖率】取分位。

    ★ 关键：阈值来自**样本分布**，不来自收益。
      收益拟合 = 在噪声上选点（过拟合的主要来源）。

    lower_is_tighter: {key: bool} —— True 表示「值越小越符合」（如回撤），
                        缺省按「值越小越符合」处理（保守：宁可多信号少成交）。
    """
    if not samples:
        return {}
    keys = list(keys) if keys else sorted(
        k for k in samples[0] if not k.startswith("_") and k != "side")
    spec = {"_n": len(samples), "_generated_from": "sample_distribution",
            "_coverages": list(coverages), "dims": {}}
    for k in keys:
        raw = [s[k] for s in samples if s.get(k) is not None]
        if not raw:
            continue
        # 分类维度（str）单独存分布，不做分位
        if isinstance(raw[0], str):
            spec["dims"][k] = {"n": len(raw), "categorical": True,
                               "dist": dict(Counter(raw).most_common()),
                               "top_share_pct": round(
                                   Counter(raw).most_common(1)[0][1] / len(raw) * 100, 1)}
            continue
        vals = sorted(float(x) for x in raw)
        if len(vals) < 3:
            continue
        # 覆盖率 c → 取第 (1-c) 分位（从「小」那端），保证 c 比例的样本满足阈值
        entry = {"n": len(vals), "min": vals[0], "max": vals[-1],
                 "median": vals[len(vals) // 2],
                 "mean": sum(vals) / len(vals)}
        for c in coverages:
            # le_pXX: XX% 的样本 <= 该值 → 取【第 XX 分位】（数值较小那侧）
            #   用作「上限型」阈值，如「距低点不超过 X」
            idx_le = min(len(vals) - 1, max(0, int(round(c * (len(vals) - 1)))))
            entry[f"le_p{int(c*100)}"] = vals[idx_le]
            # ge_pXX: XX% 的样本 >= 该值 → 取【第 (1-XX) 分位】（数值较大那侧）
            #   用作「下限型」阈值，如「跌幅至少 X」
            idx_ge = min(len(vals) - 1, max(0, int(round((1.0 - c) * (len(vals) - 1)))))
            entry[f"ge_p{int(c*100)}"] = vals[idx_ge]
        spec["dims"][k] = entry
    return spec


def spec_threshold(spec: dict, key: str, coverage: float = 0.8,
                   direction: str = "le") -> Optional[float]:
    """从规格里取某维度的阈值。

    direction="le": 取 le_pXX —— 语义「XX% 的样本 <= 该值」（上限型条件）
    direction="ge": 取 ge_pXX —— 语义「XX% 的样本 >= 该值」（下限型条件）

    ★ 方向必须显式指定：同一个维度，「距低点近」和「跌幅大」是相反的方向，
      混用会把样本分布读反。
    """
    dims = spec.get("dims") or {}
    # 规格文件里 dims 可能再嵌一层 {"dims": {...}}
    if "dims" in dims and isinstance(dims["dims"], dict):
        dims = dims["dims"]
    d = dims.get(key)
    if not d or d.get("categorical"):
        return None
    # 兼容两种键名（pXX 与 direction_pXX）
    suffix = f"{direction}_p{int(coverage*100)}"
    if suffix in d:
        return d[suffix]
    alt = f"p{int(coverage*100)}"
    return d.get(alt)


# ---------------------------------------------------------------- 判读维度
def ema_series(vals: list, n: int) -> list:
    if len(vals) < n:
        return [None] * len(vals)
    k = 2.0 / (n + 1)
    v = sum(vals[:n]) / n
    out = [None] * (n - 1) + [v]
    for x in vals[n:]:
        v = x * k + v * (1 - k)
        out.append(v)
    return out


def atr_series(bars: list, n: int = 14) -> list:
    trs = [None]
    for i in range(1, len(bars)):
        h, l, pc = float(bars[i][2]), float(bars[i][3]), float(bars[i - 1][4])
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    out = [None] * len(bars)
    if len(trs) < n:
        return out
    v = sum(trs[1:n + 1]) / n
    out[n] = v
    for i in range(n + 1, len(bars)):
        v = (v * (n - 1) + trs[i]) / n
        out[i] = v
    return out


def _last_closed_index(tss: list, ts: int) -> int:
    """★ ts 之前最后一根【已收】bar 的下标（含 ts 那根未收 → 用前一根）。

    前视偏差的头号来源。锚在这里，不要改。
    """
    import bisect
    i = bisect.bisect_right(tss, ts) - 1     # 含 ts 的那根
    return max(0, i - 1)                     # 前一根才是当时可见的


def bar_features(bars: list, ts: int, px: float, side: str,
                 tf_min: int = 60) -> dict:
    """入场时刻的判读维度。

    ★ 所有特征只用 ts 之前最后一根已收 bar。
    ★ 维度是**可扩展的**：加新维度就往这里加，不需要改任何其它脚本。
    """
    tss = [int(b[0]) for b in bars]
    i = _last_closed_index(tss, ts)
    out = {}
    if i < 60 or len(bars) < 61:
        return out
    closes = [float(b[4]) for b in bars]
    highs = [float(b[2]) for b in bars]
    lows = [float(b[3]) for b in bars]
    vols = [float(b[5]) for b in bars]
    c = closes[i]

    e20 = ema_series(closes, 20)
    e50 = ema_series(closes, 50)
    atr = atr_series(bars, 14)
    if e20[i] and e50[i]:
        out["struct"] = "up" if e20[i] > e50[i] * 1.0005 else (
            "down" if e20[i] < e50[i] * 0.9995 else "flat")
        out["px_vs_ema20_pct"] = round((px - e20[i]) / e20[i] * 100, 3)
        out["px_vs_ema50_pct"] = round((px - e50[i]) / e50[i] * 100, 3)

    for hrs in (1, 3, 6, 12, 24, 48):
        look = max(1, int(hrs * 60 / tf_min))
        if i < look:
            continue
        out[f"drop_{hrs}h_pct"] = round((closes[i - look] - c) / closes[i - look] * 100, 3)
        out[f"rise_{hrs}h_pct"] = -out[f"drop_{hrs}h_pct"]

    base = sum(vols[i - 20:i]) / 20
    if base > 0:
        out["vol_x"] = round(vols[i] / base, 3)

    for hrs in (12, 24):
        look = max(2, int(hrs * 60 / tf_min))
        if i < look:
            continue
        lo = min(lows[i - look + 1:i + 1])
        hi = max(highs[i - look + 1:i + 1])
        out[f"dist_low_{hrs}h_pct"] = round((px - lo) / lo * 100, 3)
        out[f"dist_high_{hrs}h_pct"] = round((hi - px) / px * 100, 3)
        out[f"range_pos_{hrs}h_pct"] = round((px - lo) / (hi - lo) * 100, 1) if hi > lo else None

    if atr[i]:
        out["atr_pct"] = round(atr[i] / c * 100, 4)

    dt = datetime.fromtimestamp(int(bars[i][0]), CST)
    out["hour"] = dt.hour
    out["is_night"] = 1 if (dt.hour >= 20 or dt.hour <= 1) else 0
    out["weekday"] = dt.weekday()
    return out


# ---------------------------------------------------------------- 保真度门
def fidelity_gate(reference_trades: list[dict],
                   engine: Callable[[dict], Optional[float]],
                   tol: float = 1.0) -> dict:
    """引擎能否复现参考方的净值？

    ★ 复现率 < 90% → 引擎只能做「直接 K 线统计」，
      不能用它下参数结论（否则在错的引擎上调参）。

    reference_trades: [{"id", "net_usd", "engine_input": {...}}]
    engine: (engine_input) -> 重放出的净值 或 None
    """
    ok = bad = skip = 0
    diffs = []
    detail = []
    for t in reference_trades:
        want = t.get("net_usd")
        if want is None:
            skip += 1
            continue
        got = engine(t.get("engine_input") or {})
        if got is None:
            skip += 1
            continue
        d = got - want
        diffs.append(abs(d))
        good = abs(d) <= tol
        ok += good
        bad += (not good)
        detail.append({"id": t.get("id"), "want": want, "got": got, "d": d, "ok": good})
    n = ok + bad
    rate = ok / n if n else 0.0
    return {"rate": rate, "ok": ok, "bad": bad, "skip": skip,
            "median_abs_diff": (sorted(diffs)[len(diffs) // 2] if diffs else None),
            "detail": detail,
            "passed": rate >= 0.9}


# ---------------------------------------------------------------- 区分度
def _auc(pos: list, neg: list) -> Optional[float]:
    if not pos or not neg:
        return None
    allv = sorted([(v, 1) for v in pos] + [(v, 0) for v in neg])
    n = len(allv)
    ranks = [0.0] * n
    i = 0
    while i < n:
        j = i
        while j + 1 < n and allv[j + 1][0] == allv[i][0]:
            j += 1
        avg = (i + j) / 2.0 + 1
        for k in range(i, j + 1):
            ranks[k] = avg
        i = j + 1
    s = sum(r for r, (_, lab) in zip(ranks, allv) if lab == 1)
    npos, nneg = len(pos), len(neg)
    return (s - npos * (npos + 1) / 2) / (npos * nneg)


def _bh_fdr(pvals: list, alpha: float = 0.05) -> list:
    """Benjamini-Hochberg FDR 校正 → 每个假设的 q 值。

    ★ 为什么必须有：测 28 个特征时，挑出 p=0.047 那个是**必然**的。
      不做多重比较校正，就会把「28 个里最幸运的一个」当成发现。
    """
    m = len(pvals)
    if not m:
        return []
    order = sorted(range(m), key=lambda i: pvals[i])
    q = [1.0] * m
    prev = 1.0
    for rank in range(m - 1, -1, -1):
        i = order[rank]
        val = min(prev, pvals[i] * m / (rank + 1))
        q[i] = val
        prev = val
    return q


def discrimination(pos: list[dict], neg: list[dict],
                   keys: Optional[Iterable[str]] = None,
                   n_perm: int = 1000, alpha: float = 0.05,
                   min_auc_gap: float = 0.15,
                   seed: int = 42) -> dict:
    """哪些维度能把参考方的真实选择从「规则信号」里区分出来？

    正样本 = 参考方实际做的单
    负样本 = 同期规则产生的信号（规则的候选池）

    判定：|AUC-0.5| >= min_auc_gap **且** 置换 p < alpha **且** BH-FDR q < alpha
    **且** 前后半段方向一致。

    ★ 前后半一致性是硬条件：只在某一段成立的区分度 = 过拟合。
    """
    if not pos or not neg:
        return {"error": "样本不足"}
    keys = list(keys) if keys else sorted(
        set(k for p in pos for k in p if not k.startswith("_")) &
        set(k for n in neg for k in n if not k.startswith("_")))
    ts_pos = [p.get("_ts", 0) for p in pos]
    ts_neg = [n.get("_ts", 0) for n in neg]
    mid = (min(ts_pos) + max(ts_pos)) / 2 if ts_pos else 0

    rows = []
    pvals = []
    for k in keys:
        pv = [float(p[k]) for p in pos if p.get(k) is not None]
        nv = [float(n[k]) for n in neg if n.get(k) is not None]
        if len(pv) < 8 or len(nv) < 20:
            continue
        a = _auc(pv, nv)
        if a is None:
            continue
        random.seed(seed)
        pool = pv + nv
        np_, nn_ = len(pv), len(nv)
        cnt = 0
        for _ in range(n_perm):
            random.shuffle(pool)
            aa = _auc(pool[:np_], pool[np_:])
            if aa is not None and abs(aa - 0.5) >= abs(a - 0.5):
                cnt += 1
        pval = (cnt + 1) / (n_perm + 1)
        p1 = [float(p[k]) for p in pos if p.get(k) is not None and p.get("_ts", 0) <= mid]
        n1 = [float(n[k]) for n in neg if n.get(k) is not None and n.get("_ts", 0) <= mid]
        p2 = [float(p[k]) for p in pos if p.get(k) is not None and p.get("_ts", 0) > mid]
        n2 = [float(n[k]) for n in neg if n.get(k) is not None and n.get("_ts", 0) > mid]
        a1, a2 = _auc(p1, n1), _auc(p2, n2)
        stable = (a1 is not None and a2 is not None and (a1 - 0.5) * (a2 - 0.5) > 0)
        rows.append({"key": k, "auc": a, "p": pval, "auc_h1": a1, "auc_h2": a2,
                     "stable": stable})
        pvals.append(pval)
    qs = _bh_fdr(pvals)
    for row, q in zip(rows, qs):
        row["q"] = q
        row["discriminative"] = (abs(row["auc"] - 0.5) >= min_auc_gap
                                 and row["p"] < alpha and q < alpha and row["stable"])
    rows.sort(key=lambda r: -abs(r["auc"] - 0.5))
    return {"rows": rows, "n_tested": len(rows),
            "n_discriminative": sum(1 for r in rows if r["discriminative"]),
            "alpha": alpha, "min_auc_gap": min_auc_gap}


# ---------------------------------------------------------------- 召回检验
def recall_check(reference_entries: list[dict],
                 candidate_entries: list[dict],
                 window_seconds: float = 3 * 3600,
                 price_tol_pct: float = 0.5) -> dict:
    """规则生成的候选里，有多少能命中参考方实际做的单？

    ★ 这是「复刻是否在复刻同一个东西」的第一道闸门。
      命中率太低（比如 < 50%）→ 复刻的根本不是同一类信号，
      后面所有 PnL 数字都不用看了。

    reference_entries: [{"id", "ts", "px", "side"}]
    candidate_entries: [{"ts", "px", "side"}]
    """
    if not reference_entries or not candidate_entries:
        return {"error": "样本不足"}
    by_side = {}
    for c in candidate_entries:
        by_side.setdefault(c.get("side"), []).append(c)
    for v in by_side.values():
        v.sort(key=lambda x: x["ts"])
    hits = []
    for ref in reference_entries:
        cands = [c for c in by_side.get(ref.get("side"), [])
                 if abs(c["ts"] - ref["ts"]) <= window_seconds]
        near = None
        for c in cands:
            if ref.get("px") and c.get("px"):
                if abs(c["px"] - ref["px"]) / ref["px"] * 100 <= price_tol_pct:
                    near = c
                    break
        hits.append({"id": ref.get("id"), "ts": ref["ts"],
                     "same_dir_hit": bool(cands), "price_hit": near is not None,
                     "offset_min": (min(cands, key=lambda c: abs(c["ts"] - ref["ts"]))["ts"]
                                    - ref["ts"]) / 60 if cands else None})
    n = len(hits)
    return {
        "n_reference": n,
        "same_dir_rate_pct": sum(1 for h in hits if h["same_dir_hit"]) / n * 100,
        "price_hit_rate_pct": sum(1 for h in hits if h["price_hit"]) / n * 100,
        "detail": hits,
        "passed": sum(1 for h in hits if h["same_dir_hit"]) / n >= 0.5,
    }


# ---------------------------------------------------------------- 配对检验
def paired_sign_test(deltas: list[float]) -> dict:
    """「套到我方现有单」的配对检验。

    ★ 只看净 Δ 会骗人：+35U 可能全落在 2 笔上。
      符号检验 + 变好/变差笔数是必看的。
    """
    pos = sum(1 for d in deltas if d > 0)
    neg = sum(1 for d in deltas if d < 0)
    same = len(deltas) - pos - neg
    n = pos + neg
    p = 1.0
    if n:
        k = min(pos, neg)
        p = min(1.0, sum(math.comb(n, i) for i in range(k + 1)) / (2 ** n) * 2)
    mean_d = sum(deltas) / len(deltas) if deltas else 0.0
    sd = 0.0
    if len(deltas) > 1:
        var = sum((d - mean_d) ** 2 for d in deltas) / (len(deltas) - 1)
        sd = var ** 0.5
    t = mean_d / (sd / len(deltas) ** 0.5) if sd > 0 and deltas else 0.0
    return {"n": len(deltas), "better": pos, "worse": neg, "same": same,
            "net_delta": sum(deltas), "mean_delta": mean_d, "t": t, "p_sign": p,
            "significant": p < 0.05}


# ---------------------------------------------------------------- 复刻
def replicate(bars: list, tf_min: int,
              signal_fn: Callable[[int], Optional[dict]],
              entry_fn: Callable[[dict], Optional[dict]],
              exit_fn: Callable[[dict], dict],
              dedup_hours: float = 12.0) -> list:
    """在长历史上按「信号 → 入场 → 出场」三段式复刻。

    三段分离是刻意的：信号规则、入场执行方式、出场阶梯是**三件独立的事**，
    混在一起就分不清收益来自哪一段（参考方的出场可以配任何入场）。
    """
    tss = [int(b[0]) for b in bars]
    trades = []
    last = {}
    dedup_s = dedup_hours * 3600
    for i in range(60, len(bars) - 1):
        sig = signal_fn(i)
        if not sig:
            continue
        key = sig.get("side", "long")
        if key in last and tss[i] - last[key] < dedup_s:
            continue
        ent = entry_fn({**sig, "i": i, "ts": tss[i]})
        if not ent:
            continue
        last[key] = tss[i]
        res = exit_fn({**sig, **ent, "i": i, "ts": tss[i]})
        trades.append({**sig, **ent, **res})
    return trades


# ---------------------------------------------------------------- 报告
def summarize(trades: list[dict]) -> dict:
    if not trades:
        return {"n": 0, "net": 0.0}
    nets = [t["net"] for t in trades]
    wins = [n for n in nets if n > 0]
    losses = [n for n in nets if n < 0]
    gp, gl = sum(wins), abs(sum(losses))
    return {"n": len(trades), "net": sum(nets),
            "win_rate": len(wins) / len(trades) * 100,
            "avg": sum(nets) / len(trades),
            "payoff": (gp / len(wins)) / (gl / len(losses)) if wins and losses else 0.0,
            "best": max(nets), "worst": min(nets)}


def bootstrap_ci(nets: list, n: int = 10000, seed: int = 42) -> tuple:
    if not nets:
        return (0.0, 0.0, 0.0)
    random.seed(seed)
    means = []
    for _ in range(n):
        means.append(sum(random.choice(nets) for _ in nets) / len(nets))
    means.sort()
    return (means[int(0.05 * n)], means[int(0.95 * n)],
            sum(1 for m in means if m > 0) / n)
