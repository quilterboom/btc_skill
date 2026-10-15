#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
liusir 方法论 — Step 4：90 天回测 + 三段交叉验证
=================================================
liusir 2026-10-13：亏钱就改参数 + out-of-sample 验证找稳定点

跑 90 天（gate.io 数据可拉到 ~ 100 天）。
拆 3 段不重叠：早 (90-60) / 中 (60-30) / 晚 (30-0)
每段都跑相同 60 组 grid（2 步长 × 3 horizon × 5 阈值 × 2 consensus），
看哪个组合在 3 段都正期望 → 那是"稳定点"。
"""
from __future__ import annotations
import json
import os
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from trader_view_sweep import (
    fetch_batched, precompute_signals, evaluate,
)


def run_window(bars_4h, bars_1h, bars_15m, start_ts, end_ts, horizons, steps_min, thresholds):
    """跑一个时间窗口的 grid search"""
    # 注: 切片时让窗口外延 4h 以保证 horizon 落点有效
    # 同时向前推 30 天 + 4h 让 sub_4h/sub_1h 有足够历史 (>=50 根)
    extend_forward = max(horizons) * 3600 + 100
    extend_backward = 30 * 24 * 3600  # 30 天历史保证 EMA200 等指标热机
    real_start = start_ts - extend_backward
    sub_4h = [b for b in bars_4h if real_start <= int(float(b["t"])) <= end_ts + extend_forward]
    sub_1h = [b for b in bars_1h if real_start <= int(float(b["t"])) <= end_ts + extend_forward]
    sub_15m = [b for b in bars_15m if real_start <= int(float(b["t"])) <= end_ts + extend_forward]
    if len(sub_4h) < 50 or len(sub_1h) < 50 or len(sub_15m) < 50:
        return [], 0

    # Precompute
    precomp = {}
    for step_min in steps_min:
        for h in horizons:
            key = (step_min, h)
            precomp[key] = precompute_signals(
                sub_4h, sub_1h, sub_15m,
                step_sec=step_min * 60, horizon_sec=h * 3600
            )
    # 过滤: ts 必须在窗口内 (end_ts 内)
    for key, rs in precomp.items():
        precomp[key] = [r for r in rs if start_ts <= r["ts"] <= end_ts]

    total_results = sum(len(v) for v in precomp.values())

    # Grid
    rows = []
    for step_min in steps_min:
        for h in horizons:
            results = precomp[(step_min, h)]
            for thr in thresholds:
                for cons in (False, True):
                    ev = evaluate(results, thr, require_consensus=cons)
                    rows.append({
                        "step_min": step_min,
                        "horizon_h": h,
                        "thr": thr,
                        "consensus": "强一致" if cons else "任意",
                        "n": ev["combined"]["n"],
                        "win": ev["combined"]["win"],
                        "loss": ev["combined"]["loss"],
                        "wr": ev["combined"]["wr"],
                        "pnl": ev["combined"]["pnl"],
                    })
    return rows, total_results


def cross_validate(bars_4h, bars_1h, bars_15m, horizons, steps_min, thresholds,
                   n_segments=3):
    """
    3 段交叉验证
    """
    # 取所有 15m ts 的范围
    all_ts = sorted(int(float(b["t"])) for b in bars_15m)
    start_ts = all_ts[0]
    end_ts = all_ts[-1]
    total_secs = end_ts - start_ts
    seg_secs = total_secs // n_segments

    segments = []
    for i in range(n_segments):
        s = start_ts + i * seg_secs
        e = (start_ts + (i + 1) * seg_secs) if i < n_segments - 1 else end_ts
        segments.append((s, e, datetime.fromtimestamp(s).strftime("%Y-%m-%d"),
                         datetime.fromtimestamp(e).strftime("%Y-%m-%d")))

    print(f"\n      数据范围: {datetime.fromtimestamp(start_ts).strftime('%Y-%m-%d')} ~ {datetime.fromtimestamp(end_ts).strftime('%Y-%m-%d')}")
    print(f"      分 {n_segments} 段交叉验证:")
    for s, e, sd, ed in segments:
        print(f"        段 {segments.index((s,e,sd,ed))+1}: {sd} ~ {ed}  ({timedelta(seconds=e-s).days} 天)")

    # 跑每段
    seg_rows = []
    seg_pnl_lists = {}  # key → {seg_name: row}
    for idx, (s, e, sd, ed) in enumerate(segments):
        print(f"\n      === 段 {idx+1} ({sd} ~ {ed}) ===")
        rows, total = run_window(bars_4h, bars_1h, bars_15m, s, e, horizons, steps_min, thresholds)
        print(f"        总判定次数: {total} (跨所有步长×horizon)")
        seg_rows.append((sd + "~" + ed, rows))
        for r in rows:
            key = (r["step_min"], r["horizon_h"], r["thr"], r["consensus"])
            seg_pnl_lists.setdefault(key, {})[sd + "~" + ed] = r

    # 找 3 段都正期望的组合
    stable_points = []
    partial_points = []
    for key, segs_data in seg_pnl_lists.items():
        if len(segs_data) != len(segments):
            continue
        pnls = [d["pnl"] for d in segs_data.values()]
        wrs = [d["wr"] for d in segs_data.values()]
        seg_pairs = list(segs_data.items())
        if all(p > 0 for p in pnls) and all(w >= 50 for w in wrs):
            stable_points.append({
                "config": key,
                "seg_pnls": seg_pairs,
                "total_pnl": sum(pnls),
                "min_pnl": min(pnls),
            })
        pos_count = sum(1 for p in pnls if p > 0)
        if pos_count >= 2:
            partial_points.append({
                "config": key,
                "seg_pnls": seg_pairs,
                "pos_count": pos_count,
                "total_pnl": sum(pnls),
            })
    stable_points.sort(key=lambda x: x["total_pnl"], reverse=True)
    partial_points.sort(key=lambda x: (x["pos_count"], x["total_pnl"]), reverse=True)

    return stable_points, partial_points, segments, seg_pnl_lists


def load_local_cached(interval: str, max_bars: int = 8000) -> List[dict]:
    """
    从本地缓存加载 K 线（按时间排序）。
    文件名: /root/data/BTC_USDT_{interval}_{max_bars}.json
    缓存格式: [[ts, vol, close, high, low, open, sum], ...]（_pack 格式）
    gate 字段: o/v/t/c/l/h/sum
    注: 缓存的 o/v/... 列可能错位，只信 h/l/c（高/低/收是价格核心）。
    """
    p = f"/root/data/BTC_USDT_{interval}_8000.json"
    if not os.path.exists(p):
        p = f"/root/data/BTC_USDT_{interval}_1500.json"
    if not os.path.exists(p):
        return []
    with open(p) as f:
        raw = json.load(f)
    bars = []
    for b in raw:
        if isinstance(b, dict):
            bar = b
            h = float(bar.get("h", 0))
            l = float(bar.get("l", 0))
            c = float(bar.get("c", 0))
            t = bar.get("t", 0)
        else:
            t = b[0]
            h = float(b[3])
            l = float(b[4])
            c = float(b[2])
        # 过滤: h/l/c 必为正且 h >= l
        if h <= 0 or l <= 0 or c <= 0 or h < l:
            continue
        # 过滤: c 必须在 [l, h] 区间（防止污染）
        if not (l <= c <= h):
            continue
        # 价格合理范围: 1000-200000 (BTC 实际)
        if c < 1000 or c > 200000:
            continue
        # 用 c 模拟 o 字段（缓存不可信）
        bars.append({
            "t": t,
            "o": c,  # fallback
            "h": h, "l": l, "c": c, "v": 0,
        })
    bars.sort(key=lambda x: x["t"])
    return bars[-max_bars:]


def main():
    days = 90
    print(f"      加载本地缓存 ...")
    # 用本地 8000 根 15m 缓存 (~ 83 天)
    bars_15m = load_local_cached("15m")
    # gate 单次 1000 上限: 用分批
    bars_4h = fetch_batched("4h", days * 6 + 200)
    bars_1h = fetch_batched("1h", days * 24 + 200)
    print(f"      4h={len(bars_4h)} 1h={len(bars_1h)} 15m={len(bars_15m)}")
    if bars_15m:
        print(f"      15m 时间范围: {datetime.fromtimestamp(bars_15m[0]['t']).strftime('%Y-%m-%d')} ~ {datetime.fromtimestamp(bars_15m[-1]['t']).strftime('%Y-%m-%d')}")
    if not bars_15m or len(bars_15m) < 100:
        # 本地缓存不够 → 降级：用 gate.io 数据但承认窗口短
        print(f"      ⚠️ 本地缓存不足 ({len(bars_15m)} 根)，用 gate.io 数据")
        bars_15m = fetch_batched("15m", 1000)
        # 同步 4h / 1h
        bars_4h = fetch_batched("4h", 200)
        bars_1h = fetch_batched("1h", 400)
        print(f"      降级后: 4h={len(bars_4h)} 1h={len(bars_1h)} 15m={len(bars_15m)}")
        print(f"      15m 时间范围: {datetime.fromtimestamp(bars_15m[0]['t']).strftime('%Y-%m-%d')} ~ {datetime.fromtimestamp(bars_15m[-1]['t']).strftime('%Y-%m-%d')}")

    # 重点关注: 改动后的 sweep 显示有希望的组合
    horizons = [2, 4]  # sweep 找到 2h/4h 表现好
    steps_min = [60, 240]
    thresholds = [0.20, 0.25, 0.30]  # 改动后阈值更严 (0.20+)

    stable, partial, segments, seg_pnl_lists = cross_validate(
        bars_4h, bars_1h, bars_15m,
        horizons, steps_min, thresholds,
        n_segments=3
    )

    print()
    print("=" * 80)
    print("## 🏆 3 段都正期望的稳定点 (liusir 方法论的稳定点候选)")
    print("=" * 80)
    if not stable:
        print("  ⚠️ 没有 3 段都正期望的组合")
    else:
        print()
        print("| 步长 | horizon | 阈值 | 过滤 | 段1 PnL | 段2 PnL | 段3 PnL | 累计 | 最小 |")
        print("|---|---|---|---|---|---|---|---|---|")
        for sp in stable[:10]:
            cfg = sp["config"]
            seg_pnls = sp["seg_pnls"]
            pnls = [d["pnl"] for _, d in seg_pnls]
            seg_names = [n for n, _ in seg_pnls]
            print(f"| {cfg[0]}min | {cfg[1]}h | {cfg[2]:.2f} | {cfg[3]} | "
                  f"{pnls[0]:+.1f}% | {pnls[1]:+.1f}% | {pnls[2]:+.1f}% | "
                  f"{sp['total_pnl']:+.1f}% | {sp['min_pnl']:+.1f}% |")

    print()
    print("=" * 80)
    print("## ⚠️ 至少 2 段正期望 (退而求其次)")
    print("=" * 80)
    print("| 步长 | horizon | 阈值 | 过滤 | 正段 | 段1 PnL | 段2 PnL | 段3 PnL | 累计 |")
    print("|---|---|---|---|---|---|---|---|---|")
    for pp in partial[:10]:
        cfg = pp["config"]
        seg_pnls = pp["seg_pnls"]
        pnls = [d["pnl"] for _, d in seg_pnls]
        print(f"| {cfg[0]}min | {cfg[1]}h | {cfg[2]:.2f} | {cfg[3]} | {pp['pos_count']}/3 | "
              f"{pnls[0]:+.1f}% | {pnls[1]:+.1f}% | {pnls[2]:+.1f}% | {pp['total_pnl']:+.1f}% |")

    # 单独列出每段表现最佳的组合
    print()
    print("=" * 80)
    print("## 各段表现最佳的组合 (作为对比基准)")
    print("=" * 80)
    print("(见各段 PnL 列对比)")

    # 找出每段 PnL 最不亏的组合
    print()
    print("=" * 80)
    print("## 🛡️ 全段最稳: 累计亏得最少 + 至少 2 段不亏")
    print("=" * 80)

    # 重新聚合: 找累计 PnL 最接近 0 (但不为正) 的组合
    by_key = seg_pnl_lists

    summary = []
    for key, segs_data in by_key.items():
        if len(segs_data) != len(segments):
            continue
        pnls = [d["pnl"] for d in segs_data.values()]
        ns = [d["n"] for d in segs_data.values()]
        wrs = [d["wr"] for d in segs_data.values()]
        summary.append({
            "config": key,
            "total_pnl": sum(pnls),
            "min_pnl": min(pnls),
            "max_pnl": max(pnls),
            "seg_pnls": pnls,
            "seg_ns": ns,
            "seg_wrs": wrs,
            "all_positive": all(p >= 0 for p in pnls),
        })

    # 按 total_pnl 降序
    summary.sort(key=lambda x: x["total_pnl"], reverse=True)
    print()
    print("| 步长 | horizon | 阈值 | 过滤 | 段1 PnL | 段2 PnL | 段3 PnL | 累计 | 最小 | 全部≥0 |")
    print("|---|---|---|---|---|---|---|---|---|---|")
    for s in summary[:15]:
        cfg = s["config"]
        p = s["seg_pnls"]
        all_pos = "✅" if s["all_positive"] else "❌"
        print(f"| {cfg[0]}min | {cfg[1]}h | {cfg[2]:.2f} | {cfg[3]:<6} | "
              f"{p[0]:+.1f}% | {p[1]:+.1f}% | {p[2]:+.1f}% | "
              f"{s['total_pnl']:+.1f}% | {s['min_pnl']:+.1f}% | {all_pos} |")

    # 找 3 段都 ≥ 0 的组合
    stable_real = [s for s in summary if s["all_positive"] and s["total_pnl"] > 0]
    print()
    if stable_real:
        print(f"## 🏆 3 段全 ≥0 且 累计为正的稳定点: {len(stable_real)} 个")
    else:
        # 找 3 段都 ≥ 0 的（即使 累计 = 0）
        all_non_neg = [s for s in summary if s["all_positive"]]
        print(f"## ⚠️ 3 段全 ≥0 但累计 = 0 的: {len(all_non_neg)} 个")
        for s in all_non_neg[:5]:
            cfg = s["config"]
            p = s["seg_pnls"]
            print(f"   {cfg[0]}min/{cfg[1]}h/thr={cfg[2]}/{cfg[3]} 段={p} 累计={s['total_pnl']}")


if __name__ == "__main__":
    main()