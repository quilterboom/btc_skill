#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""
liq_map_forward.py — 清算簇「前向验证器」
============================================

**问题**：信号样本太稀缺。liusir 2026-11-02 指出历史数据补不回来，
只有从今天开始攒。但「信号 × 清算簇」这条路要等信号触发（实测 1.29 笔/天），
攒 30 个样本要 23 天 —— 太慢。

**解法**：换一条验证路径 —— **清算簇 × 未来价格**。
不依赖信号触发，每条快照都是样本（实测 156 条/天，10 分钟一条）。
问的问题也不同，但更基础：

    「清算簇说上方有个强空单簇（价格磁吸向上），随后价格真的往那去了吗？」

    - 若上方簇越强 → 随后价格越往上走 → 清算簇有预测力（磁吸效应成立）
    - 若下方簇越强 → 随后价格越往下走 → 同上，方向相反
    - 若两者都无区分度 → 清算簇只是事后描述，没有预测力 → **别拿它做决策门**

**为什么先做这条**：路径 B 的样本密度是路径 A 的 121 倍，且**今天就能跑**
（本地已有 15m/1h K 线）。路径 A 照样要攒，但它验证的是「簇位置对**信号**的
区分度」，样本少是它的固有属性。

**口径**
- 用本地 `/root/data/BTC_USDT_klines.json`（Gate 永续，与策略同一交易所）
- 每条快照 ts → 找最近的 K 线 → 向前看 N 根（默认 6 根 15m = 1.5h）
- 计算：未来收益 fwd_ret%、到上方最近簇的距离、到下方最近簇的距离
- 分组统计：按上方簇强度分位 / 下方簇强度分位 / 倾向分区间 → 各组均值
- **样本门槛**：任一组不足 MIN_SAMPLES 就标「样本不足」，不给结论

**用法**
```bash
python3 liq_map_forward.py                      # 默认 6 根 15m
python3 liq_map_forward.py --bars 12 --tf 15m   # 3 小时窗口
python3 liq_map_forward.py --read-ima           # 用 IMA 归档序列（异地副本）
python3 liq_map_forward.py --json               # 机器可读输出
```

**★ 诚实性约定**
- 样本不足时**输出「样本不足」而不是「无关系」** —— 没数据 ≠ 无效
- 前后各看一次（forward / backward），避免只挑对的方向
- 明确写出「这是相关性，不是因果；清算簇是持仓结构的模型输出，不是真实待清算单」
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

DATA_DIR = Path(os.environ.get("BTC_DATA_DIR") or "/root/data")
STORE = DATA_DIR / "liq_map_snapshots.jsonl"
KLINES = DATA_DIR / "BTC_USDT_klines.json"

MIN_SAMPLES = 8        # 任一组的样本门槛
DEFAULT_TF = "15m"
DEFAULT_BARS = 6

# ★ 样本「独立性」门槛 —— 比数量更重要。
#   实测（2026-11-02 15:50）：11 条样本挤在 90 分钟内，future_ret 区间只有 0.29%
#   且全部同号 → 这些相关系数是**伪相关**（同一段行情的重复观测），统计功效≈0。
#   所以除了样本数，还要看：
#     - 覆盖时长（太短 = 只覆盖一种市场状态）
#     - 未来收益的离散度（太窄 = 没有区分空间）
#     - 独立状态数（近似：把 future_ret 变化幅度小于阈值的相邻样本视为同一状态）
MIN_SPAN_HOURS = 12.0        # 样本至少要跨这么多小时
MIN_FWD_STDEV = 0.15         # future_ret 标准差下限（%）
MIN_DISTINCT_STATES = 8      # 近似独立状态数下限


# ---------------------------------------------------------------- 数据源
def load_snapshots(use_ima: bool = False) -> List[Dict]:
    """读快照序列（本地 jsonl，或从 IMA 归档回读）。"""
    if use_ima:
        try:
            import liq_map_collector as C
            rows = C.read_from_ima()
            # IMA 回读的行没有 raw ts，只有 ts_iso → 转成 epoch
            import datetime as dt
            CST = dt.timezone(dt.timedelta(hours=8))
            for r in rows:
                try:
                    r["ts"] = int(dt.datetime.strptime(r["ts_iso"], "%Y-%m-%d %H:%M:%S")
                                  .replace(tzinfo=CST).timestamp())
                except Exception:
                    r["ts"] = 0
            return sorted([r for r in rows if r.get("ts")], key=lambda r: r["ts"])
        except Exception as e:
            print(f"⚠️ IMA 回读失败（改用本地）：{e}", file=sys.stderr)
    if not STORE.exists():
        return []
    rows = []
    for line in STORE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return sorted(rows, key=lambda r: int(r.get("ts") or 0))


def load_klines(tf: str = DEFAULT_TF) -> List[List]:
    """读 Gate 永续 K 线 → [[ts, o, h, l, c, v, ...], ...]（按时间升序）。"""
    if not KLINES.exists():
        return []
    try:
        d = json.loads(KLINES.read_text(encoding="utf-8"))
    except Exception:
        return []
    node = (d.get("BTC_USDT") or {}).get(tf) or {}
    bars = node.get("bars") or []
    return sorted(bars, key=lambda b: int(b[0]))


# ---------------------------------------------------------------- 匹配与前向收益
def _find_bar(bars: List[List], ts: int) -> Optional[int]:
    """找 ts 所在（或之前最近）的 K 线下标。二分。"""
    lo, hi = 0, len(bars) - 1
    best = None
    while lo <= hi:
        mid = (lo + hi) // 2
        if int(bars[mid][0]) <= ts:
            best = mid
            lo = mid + 1
        else:
            hi = mid - 1
    return best


def _nearest_cluster(clusters: List[Dict], px: float, above: bool) -> Optional[Dict]:
    """离现价最近的簇（上方/下方）。"""
    cands = [c for c in clusters if (c["dist_pct"] > 0) == above]
    if not cands:
        return None
    return min(cands, key=lambda c: abs(c["dist_pct"]))


def build_samples(snaps: List[Dict], bars: List[List], n_bars: int,
                  tf: str = DEFAULT_TF) -> List[Dict]:
    """每条快照 → 一个样本（含未来 N 根的收益与到簇距离）。"""
    if not bars:
        return []
    out = []
    for s in snaps:
        ts = int(s.get("ts") or 0)
        if not ts:
            continue
        i = _find_bar(bars, ts)
        if i is None or i + n_bars >= len(bars):
            continue
        px_now = float(s.get("px") or bars[i][4])
        px_fwd = float(bars[i + n_bars][4])
        px_bwd = float(bars[max(0, i - n_bars)][4]) if i >= n_bars else None
        fwd_ret = (px_fwd / px_now - 1) * 100
        bwd_ret = (px_bwd / px_now - 1) * 100 if px_bwd else None
        above = s.get("above") or []
        below = s.get("below") or []
        sug = s.get("suggestion") or {}
        out.append({
            "ts": ts, "ts_iso": s.get("ts_iso"), "px": px_now,
            "fwd_ret": fwd_ret, "bwd_ret": bwd_ret,
            "px_fwd": px_fwd,
            "score": sug.get("score"), "label": sug.get("label"),
            "above_top": above[0] if above else None,
            "below_top": below[0] if below else None,
            "above_dist": abs(above[0]["dist_pct"]) if above else None,
            "below_dist": abs(below[0]["dist_pct"]) if below else None,
        })
    return out


# ---------------------------------------------------------------- 分组统计
def _stats(vals: List[float]) -> Dict:
    if not vals:
        return {"n": 0}
    return {
        "n": len(vals),
        "mean": round(statistics.fmean(vals), 4),
        "median": round(statistics.median(vals), 4),
        "stdev": round(statistics.pstdev(vals), 4) if len(vals) > 1 else 0.0,
    }


def _group_report(samples: List[Dict], key: str, n_bins: int = 3) -> Dict:
    """按某强度分位分组，看各组未来收益均值。"""
    have = [s for s in samples if s.get(key) is not None]
    if len(have) < MIN_SAMPLES:
        return {"status": "样本不足", "n": len(have), "need": MIN_SAMPLES}
    vals = sorted(s["key"] if False else s[key] for s in have)
    bins = []
    size = max(1, len(vals) // n_bins)
    for b in range(n_bins):
        chunk = vals[b * size:(b + 1) * size] if b < n_bins - 1 else vals[b * size:]
        if not chunk:
            continue
        lo, hi = chunk[0], chunk[-1]
        grp = [s for s in have if lo <= s[key] <= hi]
        bins.append({
            "range": [round(lo, 3), round(hi, 3)],
            "fwd_ret": _stats([s["fwd_ret"] for s in grp]),
            "bwd_ret": _stats([s["fwd_ret"] for s in grp if s.get("bwd_ret") is not None]),
        })
    return {"status": "ok", "n": len(have), "bins": bins}


def _corr(xs: List[float], ys: List[float]) -> Optional[float]:
    if len(xs) < 3 or len(xs) != len(ys):
        return None
    mx, my = statistics.fmean(xs), statistics.fmean(ys)
    num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    dx = sum((x - mx) ** 2 for x in xs) ** 0.5
    dy = sum((y - my) ** 2 for y in ys) ** 0.5
    if dx == 0 or dy == 0:
        return None
    return round(num / (dx * dy), 4)


def _distinct_states(samples: List[Dict], tol: float = 0.05) -> int:
    """近似独立状态数：相邻样本的 future_ret 变化 < tol（%）视为同一状态。

    动机：10 分钟一条快照，90 分钟内 11 条其实是同一段行情的重复观测。
    相关系数在这种数据上是伪相关（样本量虚高、有效样本量≈1）。
    """
    if not samples:
        return 0
    states = 1
    for i in range(1, len(samples)):
        if abs(samples[i]["fwd_ret"] - samples[i - 1]["fwd_ret"]) >= tol:
            states += 1
    return states


def _power_check(samples: List[Dict]) -> Dict:
    """样本「够不够独立」的体检。返回 {ok, reasons[], detail{}}。"""
    reasons = []
    detail = {}
    if len(samples) < 2:
        return {"ok": False, "reasons": ["样本少于 2 条"], "detail": {}}
    span_h = (samples[-1]["ts"] - samples[0]["ts"]) / 3600.0
    detail["span_hours"] = round(span_h, 2)
    if span_h < MIN_SPAN_HOURS:
        reasons.append(f"覆盖时长仅 {span_h:.1f}h（需 ≥{MIN_SPAN_HOURS:.0f}h）")
    fwd = [s["fwd_ret"] for s in samples]
    sd = statistics.pstdev(fwd) if len(fwd) > 1 else 0.0
    detail["fwd_ret_stdev"] = round(sd, 4)
    if sd < MIN_FWD_STDEV:
        reasons.append(f"future_ret 离散度仅 {sd:.3f}%（需 ≥{MIN_FWD_STDEV}%）")
    states = _distinct_states(samples)
    detail["distinct_states"] = states
    if states < MIN_DISTINCT_STATES:
        reasons.append(f"近似独立状态仅 {states} 个（需 ≥{MIN_DISTINCT_STATES}）")
    return {"ok": not reasons, "reasons": reasons, "detail": detail}


def analyze(samples: List[Dict], n_bars: int, tf: str) -> Dict:
    if len(samples) < MIN_SAMPLES:
        return {"status": "样本不足", "n": len(samples), "need": MIN_SAMPLES}
    power = _power_check(samples)
    if not power["ok"]:
        # ★ 样本数量够了但**不够独立** → 报「样本独立性不足」，不给相关系数
        return {"status": "样本独立性不足", "n": len(samples), "need": MIN_SAMPLES,
                "power": power}
    fwd = [s["fwd_ret"] for s in samples]
    res = {
        "status": "ok",
        "n": len(samples),
        "power": power,
        "window": {"tf": tf, "bars": n_bars},
        "fwd_ret_all": _stats(fwd),
        "baseline_mean_fwd_ret": round(statistics.fmean(fwd), 4),
    }
    # 与上方簇强度的关系（上方簇越强 → 预期越往上）
    have_a = [s for s in samples if s.get("above_top")]
    if len(have_a) >= MIN_SAMPLES:
        res["corr_above_strength_vs_fwd"] = _corr(
            [s["above_top"]["strength_pct"] for s in have_a],
            [s["fwd_ret"] for s in have_a])
        res["corr_above_dist_vs_fwd"] = _corr(
            [s["above_dist"] for s in have_a], [s["fwd_ret"] for s in have_a])
    have_b = [s for s in samples if s.get("below_top")]
    if len(have_b) >= MIN_SAMPLES:
        res["corr_below_strength_vs_fwd"] = _corr(
            [s["below_top"]["strength_pct"] for s in have_b],
            [s["fwd_ret"] for s in have_b])
        res["corr_below_dist_vs_fwd"] = _corr(
            [s["below_dist"] for s in have_b], [s["fwd_ret"] for s in have_b])
    # 倾向分 vs 未来收益（分越高越偏多 → 未来收益应越高）
    have_s = [s for s in samples if s.get("score") is not None]
    if len(have_s) >= MIN_SAMPLES:
        res["corr_score_vs_fwd"] = _corr([s["score"] for s in have_s],
                                         [s["fwd_ret"] for s in have_s])
        res["by_score"] = _group_report(have_s, "score")
    # 按上方簇强度分组（strength 摊平成标量字段再分箱）
    flat = [{**s, "above_strength": s["above_top"]["strength_pct"]}
            for s in have_a if s.get("above_top")]
    res["by_above_strength"] = _group_report(flat, "above_strength")
    flat_b = [{**s, "below_strength": s["below_top"]["strength_pct"]}
              for s in have_b if s.get("below_top")]
    res["by_below_strength"] = _group_report(flat_b, "below_strength")
    return res


# ---------------------------------------------------------------- 渲染
def render(res: Dict, samples: List[Dict]) -> str:
    L = []
    if res.get("status") == "样本不足":
        return (f"清算簇前向验证：**样本不足**（{res.get('n', 0)} / 需要 {res.get('need', MIN_SAMPLES)}）\n"
                f"  → 不是「无关系」，是「还没数据」。等样本攒够再看。")
    if res.get("status") == "样本独立性不足":
        pw = res.get("power") or {}
        d = pw.get("detail") or {}
        L.append(f"清算簇前向验证：**样本独立性不足**（{res.get('n', 0)} 条）")
        L.append("  样本数量够，但它们不是独立观测 —— 相关系数会是伪相关，不给结论：")
        for r in pw.get("reasons") or []:
            L.append(f"    ✗ {r}")
        L.append("")
        L.append(f"  体检：覆盖 {d.get('span_hours', 0)}h ｜ future_ret 标准差 "
                 f"{d.get('fwd_ret_stdev', 0)}% ｜ 近似独立状态 {d.get('distinct_states', 0)} 个")
        L.append("")
        L.append("  → 攒够跨时段、跨行情状态的样本后再看。")
        L.append("  → 注意：10 分钟一条快照，短时间内的多条是同一段行情的重复观测。")
        return "\n".join(L)
    if res.get("status") != "ok":
        return f"清算簇前向验证：无法分析（{res.get('status')}）"
    w = res["window"]
    pw = (res.get("power") or {}).get("detail") or {}
    L.append(f"清算簇前向验证（{w['tf']} × {w['bars']} 根）")
    L.append(f"  样本 {res['n']} 条 ｜ 跨 {pw.get('span_hours', 0)}h ｜ "
             f"独立状态 {pw.get('distinct_states', 0)} 个")
    L.append(f"  全样本未来收益均值 {res['baseline_mean_fwd_ret']:+.4f}%")
    L.append("")
    L.append("  【相关性】强度/距离 vs 未来收益")
    for key, label in (("corr_above_strength_vs_fwd", "上方簇强度"),
                       ("corr_above_dist_vs_fwd", "上方簇距离"),
                       ("corr_below_strength_vs_fwd", "下方簇强度"),
                       ("corr_below_dist_vs_fwd", "下方簇距离"),
                       ("corr_score_vs_fwd", "倾向分")):
        v = res.get(key)
        L.append(f"    {label:12s} {('n/a' if v is None else f'{v:+.4f}')}")
    L.append("")
    for gkey, label in (("by_score", "按倾向分分组"), ("by_above_strength", "按上方簇强度分组")):
        g = res.get(gkey) or {}
        if g.get("status") != "ok":
            L.append(f"  【{label}】样本不足（{g.get('n', 0)}）")
            continue
        L.append(f"  【{label}】")
        for b in g["bins"]:
            fr = b["fwd_ret"]
            L.append(f"    {b['range'][0]:>7.2f} ~ {b['range'][1]:<7.2f} n={fr['n']:<3d} "
                     f"未来收益均值 {fr['mean']:+.4f}%  中位数 {fr['median']:+.4f}%")
    L.append("")
    L.append("  ⚠️ 相关性 ≠ 因果。清算簇是持仓结构的模型输出，不是真实待清算单。")
    L.append("  ⚠️ 样本量小，结论不稳；且这几天的样本来自单一市场状态。")
    return "\n".join(L)


def main() -> int:
    ap = argparse.ArgumentParser(description="清算簇前向验证器")
    ap.add_argument("--bars", type=int, default=DEFAULT_BARS, help="向前看几根 K 线")
    ap.add_argument("--tf", default=DEFAULT_TF, help="K 线周期（15m/1h/5m...）")
    ap.add_argument("--read-ima", action="store_true", help="用 IMA 归档序列")
    ap.add_argument("--json", action="store_true", help="JSON 输出")
    a = ap.parse_args()

    snaps = load_snapshots(use_ima=a.read_ima)
    bars = load_klines(a.tf)
    if not snaps:
        print("没有快照序列（先跑 liq_map_collector.py --collect）")
        return 1
    if not bars:
        print(f"没有 {a.tf} K 线（{KLINES}）")
        return 1
    samples = build_samples(snaps, bars, a.bars, a.tf)
    res = analyze(samples, a.bars, a.tf)
    if a.json:
        print(json.dumps({"analysis": res, "samples": samples}, ensure_ascii=False, indent=2))
    else:
        print(render(res, samples))
        print(f"\n（快照 {len(snaps)} 条 → 可用样本 {len(samples)} 条）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
