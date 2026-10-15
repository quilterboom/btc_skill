#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
trader_view v2 阈值 × 步长 grid search
liusir 2026-10-13 拍板

变量:
  - norm 阈值: 判定多严才开仓 (0.10 / 0.15 / 0.20 / 0.25 / 0.30)
  - step_min: 每 N 分钟跑一次 (60 / 120 / 240 / 480)
  - horizon_h: 验证窗口 (固定 4h)

输出:
  Markdown 表，每行 = (阈值, 步长) 一组结果
"""
from __future__ import annotations

import json
import os
import sys
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Dict, List

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
# 复用 trader_view 的分析函数
from trader_view import (
    fetch_candles, bars_to_arrays,
    analyze_tf, aggregate_bias,
)


GATE_BASE = "https://api.gateio.ws/api/v4/futures/usdt"


def fetch_batched(interval: str, total: int, contract: str = "BTC_USDT") -> List[dict]:
    """分批拉取 K 线（gate 单次上限 1000）"""
    all_bars = []
    end_ts = int(datetime.now().timestamp())
    while len(all_bars) < total:
        limit = min(1000, total - len(all_bars))
        url = (f"{GATE_BASE}/candlesticks?contract={contract}&interval={interval}"
               f"&limit={limit}&to={end_ts}")
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=15) as r:
            batch = json.loads(r.read())
        if not batch:
            break
        batch.reverse()  # gate 倒序返回
        all_bars.extend(batch)
        end_ts = int(float(batch[0]["t"])) - 1
        if len(batch) < limit:
            break
    # 去重 + 排序
    seen = set()
    uniq = []
    for b in all_bars:
        t = int(float(b["t"]))
        if t in seen: continue
        seen.add(t)
        uniq.append(b)
    return sorted(uniq, key=lambda b: float(b["t"]))


def precompute_signals(bars_4h, bars_1h, bars_15m, step_sec: int, horizon_sec: int):
    """
    提前计算所有 (timestamp, sub_score, norm, bias, px_now, px_future, ret_pct)
    注意: 这里返回的是 agg.bias + agg.norm，**不在 v2 函数里判门**。
    """
    bars_4h_arr = [(int(float(b["t"])), float(b["c"])) for b in bars_4h]
    bars_1h_arr = [(int(float(b["t"])), float(b["c"])) for b in bars_1h]
    bars_15m_arr = [(int(float(b["t"])), float(b["c"])) for b in bars_15m]

    # 用 15m 主驱动（因为 step 通常是它的倍数）
    all_15m_ts = [t for t, _ in bars_15m_arr]

    results = []
    for i, t in enumerate(all_15m_ts):
        if i % (step_sec // 900) != 0:  # 15m 间隔是 900s
            continue
        if t + horizon_sec not in {x[0] for x in bars_15m_arr}:
            continue
        # 切片
        sub_4h = [b for b in bars_4h if int(float(b["t"])) <= t]
        sub_1h = [b for b in bars_1h if int(float(b["t"])) <= t]
        sub_15m = [b for b in bars_15m if int(float(b["t"])) <= t]
        if len(sub_4h) < 50 or len(sub_1h) < 50 or len(sub_15m) < 50:
            continue

        per_tf = {
            "4h": analyze_tf("4h", sub_4h, float(sub_4h[-1]["c"])),
            "1h": analyze_tf("1h", sub_1h, float(sub_1h[-1]["c"])),
            "15m": analyze_tf("15m", sub_15m, float(sub_15m[-1]["c"])),
        }
        agg = aggregate_bias(per_tf)

        px_now = float(sub_15m[-1]["c"])
        # 找 horizon 后的最近一根 15m
        future_ts = t + horizon_sec
        # 在 15m 数组里二分找
        for tt, cc in bars_15m_arr:
            if tt >= future_ts:
                px_future = cc
                break
        else:
            continue
        ret_pct = (px_future - px_now) / px_now * 100

        results.append({
            "ts": t,
            "ts_str": datetime.fromtimestamp(t).strftime("%Y-%m-%d %H:%M"),
            "px_now": px_now,
            "px_future": px_future,
            "ret_pct": ret_pct,
            "bias": agg["bias"],
            "norm": agg["norm"],
            "score": agg["score"],
            "consensus": agg.get("consensus", "无数据"),
            "consensus_score": agg.get("consensus_score", 0),
        })
    return results


def evaluate(results: List[dict], norm_threshold: float, require_consensus: bool = False):
    """对一组已算好的判定结果按 norm_threshold 筛选 + 统计
    require_consensus=True 时，只保留三周期结构 bias 一致的判定
    """
    rows = {"long": [], "short": []}
    for r in results:
        # 注: results 里有 consensus 字段（v2 aggregate_bias 给出）
        if require_consensus and r.get("consensus") != "强一致":
            continue
        if r["bias"] == "long" and r["norm"] >= norm_threshold:
            rows["long"].append(r)
        elif r["bias"] == "short" and r["norm"] <= -norm_threshold:
            rows["short"].append(r)
    # 胜率：long 4h 涨算赢 (>0.05%)，short 4h 跌算赢 (<-0.05%)
    out = {"long": {}, "short": {}, "combined": {}}
    for bias in ("long", "short"):
        items = rows[bias]
        n = len(items)
        if n == 0:
            out[bias] = {"n": 0, "win": 0, "loss": 0, "wr": 0, "avg_ret": 0,
                         "gross_win": 0, "gross_loss": 0}
            continue
        if bias == "long":
            win = sum(1 for r in items if r["ret_pct"] > 0.05)
            loss = sum(1 for r in items if r["ret_pct"] < -0.05)
        else:
            win = sum(1 for r in items if r["ret_pct"] < -0.05)
            loss = sum(1 for r in items if r["ret_pct"] > 0.05)
        if bias == "long":
            gross_win = sum(max(0, r["ret_pct"]) for r in items)
            gross_loss = sum(min(0, r["ret_pct"]) for r in items)
        else:
            gross_win = sum(max(0, -r["ret_pct"]) for r in items)
            gross_loss = sum(min(0, -r["ret_pct"]) for r in items)
        avg_ret = sum(r["ret_pct"] if bias == "long" else -r["ret_pct"] for r in items) / n
        wr = win / max(win + loss, 1) * 100
        out[bias] = {
            "n": n, "win": win, "loss": loss,
            "wr": wr, "avg_ret": avg_ret,
            "gross_win": gross_win, "gross_loss": gross_loss,
        }
    total_n = out["long"]["n"] + out["short"]["n"]
    total_pnl = (out["long"]["gross_win"] + out["long"]["gross_loss"] +
                 out["short"]["gross_win"] + out["short"]["gross_loss"])
    total_win = out["long"]["win"] + out["short"]["win"]
    total_loss = out["long"]["loss"] + out["short"]["loss"]
    total_wr = total_win / max(total_win + total_loss, 1) * 100
    out["combined"] = {
        "n": total_n, "win": total_win, "loss": total_loss,
        "wr": total_wr, "pnl": total_pnl,
    }
    return out


def main():
    days = 30
    n_4h = days * 6 + 200
    n_1h = days * 24 + 200
    n_15m = days * 96 + 200

    print(f"      拉数据 {days} 天...")
    bars_4h = fetch_batched("4h", n_4h)
    bars_1h = fetch_batched("1h", n_1h)
    bars_15m = fetch_batched("15m", n_15m)
    print(f"      4h={len(bars_4h)} 1h={len(bars_1h)} 15m={len(bars_15m)}")

    # 不同的步长分别 precompute（避免同一组数据被多种 step 重复跑）
    horizons = [1 * 3600, 2 * 3600, 4 * 3600]  # 验证窗口：1h/2h/4h
    steps_min = [60, 240]  # 步长：1h / 4h
    thresholds = [0.10, 0.15, 0.20, 0.25, 0.30]

    # Precompute (step × horizon)
    precomp = {}
    for step_min in steps_min:
        for horizon_h in [h // 3600 for h in horizons]:
            key = (step_min, horizon_h)
            print(f"      precompute step={step_min}min horizon={horizon_h}h ...")
            precomp[key] = precompute_signals(
                bars_4h, bars_1h, bars_15m,
                step_sec=step_min * 60, horizon_sec=horizon_h * 3600
            )
            print(f"        → {len(precomp[key])} 次判定")

    # Grid search（每个 step × horizon × threshold × consensus 三档过滤）
    print(f"\n      开始 grid search (40 组 × 2 consensus 过滤 = 80 格) ...")
    table_rows = []
    for step_min in steps_min:
        for horizon_h in [h // 3600 for h in horizons]:
            results = precomp[(step_min, horizon_h)]
            for thr in thresholds:
                for cons in (False, True):
                    ev = evaluate(results, thr, require_consensus=cons)
                    table_rows.append({
                        "step_min": step_min,
                        "horizon_h": horizon_h,
                        "thr": thr,
                        "consensus": "强一致" if cons else "任意",
                        **ev["combined"],
                        "long_n": ev["long"]["n"],
                        "long_wr": ev["long"]["wr"],
                        "short_n": ev["short"]["n"],
                        "short_wr": ev["short"]["wr"],
                    })

    # 输出报告
    print()
    print("=" * 100)
    print(f"# 📊 trader_view v2 grid search ({days} 天)")
    print("=" * 100)

    print(f"\n## 完整 grid (60 组)")
    print()
    print("| 步长 | horizon | 阈值 | 过滤 | 总判定 | 胜 | 负 | 胜率 | 累计盈亏% |")
    print("|---|---|---|---|---|---|---|---|---|")
    for r in table_rows:
        print(f"| {r['step_min']}min | {r['horizon_h']}h | {r['thr']:.2f} | {r['consensus']:<8} | "
              f"{r['n']:>3} | {r['win']:>3} | {r['loss']:>3} | {r['wr']:>5.1f}% | "
              f"{r['pnl']:+6.1f}% |")

    # 排序找出最赚钱的组合
    by_pnl = sorted(table_rows, key=lambda r: r["pnl"], reverse=True)
    print(f"\n## Top 10 by 累计盈亏")
    print("| 步长 | horizon | 阈值 | 过滤 | 累计盈亏% | 总判定 | 胜率 |")
    print("|---|---|---|---|---|---|---|")
    for r in by_pnl[:10]:
        print(f"| {r['step_min']}min | {r['horizon_h']}h | {r['thr']:.2f} | "
              f"{r['consensus']:<8} | {r['pnl']:+.1f}% | {r['n']} | {r['wr']:.1f}% |")

    # 高胜率筛选
    high_wr = sorted([r for r in table_rows if r["wr"] >= 45], key=lambda r: r["pnl"], reverse=True)
    print(f"\n## 胜率 ≥ 45% 的组合")
    print("| 步长 | horizon | 阈值 | 过滤 | 判定数 | 胜率 | 累计盈亏% |")
    print("|---|---|---|---|---|---|---|")
    if not high_wr:
        print("| (无) | - | - | - | - | - | - |")
    for r in high_wr[:10]:
        print(f"| {r['step_min']}min | {r['horizon_h']}h | {r['thr']:.2f} | "
              f"{r['consensus']:<8} | {r['n']} | {r['wr']:.1f}% | {r['pnl']:+.1f}% |")

    # consensus 过滤 vs 不过滤对比（看有没有改善）
    cons_compare = {}
    for r in table_rows:
        key = (r["step_min"], r["horizon_h"], r["thr"])
        if key not in cons_compare:
            cons_compare[key] = {"任意": r, "强一致": None}
        if r["consensus"] == "强一致":
            cons_compare[key]["强一致"] = r
    # 找出「强一致过滤后 PnL 上升」的组合
    cons_better = []
    for k, v in cons_compare.items():
        if v["强一致"] and v["任意"]["pnl"] < v["强一致"]["pnl"]:
            cons_better.append({
                "key": k,
                "any_pnl": v["任意"]["pnl"],
                "cons_pnl": v["强一致"]["pnl"],
                "delta": v["强一致"]["pnl"] - v["任意"]["pnl"],
            })
    cons_better.sort(key=lambda x: x["delta"], reverse=True)
    print(f"\n## consensus 过滤改进最大的 5 组 (强一致 vs 任意)")
    print("| 步长 | horizon | 阈值 | 任意 PnL | 强一致 PnL | 改进 |")
    print("|---|---|---|---|---|---|")
    if not cons_better:
        print("| (无明显改进) | - | - | - | - | - |")
    for r in cons_better[:5]:
        step, hor, thr = r["key"]
        print(f"| {step}min | {hor}h | {thr:.2f} | {r['any_pnl']:+.1f}% | "
              f"{r['cons_pnl']:+.1f}% | {r['delta']:+.1f}% |")


if __name__ == "__main__":
    main()