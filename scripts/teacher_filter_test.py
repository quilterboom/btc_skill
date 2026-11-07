#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
teacher_filter_test.py —— 把老师单的入场画像当成过滤规则，套到我的策略单上（2026-11-07）

## 为什么做这个

liusir 2026-10-19 定的方向：**老师单不是用来抄参数的，是用来反推「为什么他在
这个位置敢进」的**，然后把判读逻辑蒸馏成训练环境的入场过滤规则。

现在老师单 20 笔（19 已结算 + 1 持仓中），已过「≥10 单」门槛，可以正式跑对照。

## 老师单的入场画像（teacher_analysis.py 实测，20 笔）

    dd_from_high_pct  均 1.92    （距入场前 24h 高点的回撤）
    range_pos_pct     均 41.05   （入场价在 24h 区间的位置，0=最低）
    vol_x             均 4.05    （入场那根 15m 量 / 前 20 根均量）★ 巨量确认
    aligned           7 顺势（胜率 100%）/ 10 逆势（胜率 80%）

**最突出的一条是 vol_x ≥ 3（巨量确认）** —— 这正是「暴跌巨量 + 缩量反弹」的核心。

## 本脚本做什么

1. 把**同一套**入场画像算到**我的生产策略单**上（同口径：同一函数、同一 K 线）
2. 用老师画像的候选条件做过滤，跑 §7.6 的 **5 关验证**
   （阈值敏感性 / 时间外推 / 方向分开 / 被过滤组分布 / 集中度）
3. 报告结论：成立 / 不成立 / 样本不足

## 只读，不改任何生产数据
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, "/root/skills/btc-gate-intraday-strategy/web")

import cost_basis_view as cbv
from strategy_truth import strategy_truth
import teacher_analysis as ta

CST = timezone(timedelta(hours=8))
PROD = "/root/data/journal.jsonl"
TRAIN = "/root/data/training/journal.jsonl"


def load_ctx_for_journal(path, label):
    """给 journal 的每笔可信单算入场画像（复用 teacher_analysis 的同一函数）。"""
    bars15 = ta._bars("15m")
    bars1h = ta._bars("1h")
    t = strategy_truth(path)
    out = []
    for r in t["records"]:
        if r.get("status") != "settled" or r.get("net_usd") is None:
            continue
        ts = int(r.get("fill_ts") or r.get("ts") or 0)
        px = cbv.cost_basis_of(r)
        if px is None:
            continue
        ctx = ta._entry_context(ts, float(px), bars15, bars1h)
        out.append({
            "id": r.get("id"), "side": r.get("side"),
            "net": cbv.effective_net(r) or 0.0,
            "ts": ts,
            "day": datetime.fromtimestamp(ts, CST).strftime("%Y-%m-%d"),
            "src": r.get("signal_source") or (r.get("meta") or {}).get("signal_source"),
            **ctx,
        })
    return out


def agg(rows, label):
    if not rows:
        print(f"  {label:<44} n=0")
        return None
    net = sum(r["net"] for r in rows)
    wins = sum(1 for r in rows if r["net"] > 0)
    gp = sum(r["net"] for r in rows if r["net"] > 0)
    gl = sum(r["net"] for r in rows if r["net"] < 0)
    print(f"  {label:<44} n={len(rows):>2} 赢{wins:>2} 净{net:+8.2f}U  均{net/len(rows):+7.2f}U  "
          f"盈亏比{gp/abs(gl) if gl else 0:.2f}")
    return {"n": len(rows), "net": net, "rows": rows}


def bucket(rows, key, edges, label):
    """按特征分桶看净利（老师画像逐维拆）。"""
    print(f"\n  ── 按 {label} 分桶 ──")
    out = {}
    for lo, hi in edges:
        if lo is None:
            sel = [r for r in rows if r.get(key) is not None and r[key] < hi]
        elif hi is None:
            sel = [r for r in rows if r.get(key) is not None and r[key] >= lo]
        else:
            sel = [r for r in rows if r.get(key) is not None and lo <= r[key] < hi]
        a = agg(sel, f"{lo}~{hi}")
        if a:
            out[(lo, hi)] = a
    return out


def gate5(rows, keep_fn, name, thresholds=None):
    """§7.6 的 5 关验证。"""
    print(f"\n  {'='*100}")
    print(f"  规则 [{name}] 的 5 关验证")
    print("  " + "=" * 100)
    keep = [r for r in rows if keep_fn(r)]
    drop = [r for r in rows if not keep_fn(r)]
    ak = agg(keep, "保留组")
    ad = agg(drop, "被过滤组")
    if not ak or not ad:
        print("  ✗ 空组 → 无法判定")
        return False
    fails = []

    # 关 1：阈值敏感性
    print("\n  关1 阈值敏感性：")
    sens_ok = True
    for variant_name, fn in (thresholds or {}).items():
        k2 = [r for r in rows if fn(r)]
        d2 = [r for r in rows if not fn(r)]
        if not k2 or not d2:
            print(f"    {variant_name:<28} 空组 → 该变体未受检验")
            continue
        n2 = sum(r["net"] for r in k2)
        dnet = sum(r["net"] for r in d2)
        better = n2 > ak["net"]
        print(f"    {variant_name:<28} 保留净{n2:+8.2f}U (n={len(k2)})  被过滤净{dnet:+8.2f}U "
              f"(n={len(d2)})  {'✓改善' if better else '✗变差'}")
        if not better:
            sens_ok = False
    if not sens_ok:
        fails.append("关1 阈值敏感性")

    # 关 2：时间外推（被过滤组每段必须非空）
    print("\n  关2 时间外推（前半/后半，被过滤组必须非空）：")
    days = sorted({r["day"] for r in rows})
    if len(days) < 2:
        fails.append("关2 样本时间跨度不足")
    else:
        mid = days[len(days) // 2]
        h1 = [r for r in rows if r["day"] < mid]
        h2 = [r for r in rows if r["day"] >= mid]
        for tag, seg in (("前半", h1), ("后半", h2)):
            k = [r for r in seg if keep_fn(r)]
            d = [r for r in seg if not keep_fn(r)]
            if not d:
                print(f"    {tag} ({mid} 分界): 被过滤组为空 → 该段未受检验 → 整条规则「无法判定」")
                fails.append(f"关2 {tag}被过滤组为空")
                continue
            kn = sum(r["net"] for r in k) if k else 0.0
            dn = sum(r["net"] for r in d)
            base = sum(r["net"] for r in seg)
            improved = kn > base
            print(f"    {tag} ({mid} 分界): 保留{kn:+7.2f}U(n={len(k)}) 被过滤{dn:+7.2f}U(n={len(d)}) "
                  f"全段{base:+7.2f}U → 过滤后{'改善' if improved else '恶化'}")
            # ★ 打印「恶化」必须同时记 fails —— 只打印不记，结论仍会打「✓ 成立」
            if not improved:
                fails.append(f"关2 {tag}未改善")

    # 关 3：方向分开
    print("\n  关3 方向分开：")
    for side in ("long", "short"):
        seg = [r for r in rows if r["side"] == side]
        k = [r for r in seg if keep_fn(r)]
        d = [r for r in seg if not keep_fn(r)]
        if not seg:
            continue
        if not k or not d:
            print(f"    {side:<6} 空组 → 该方向未受检验")
            continue
        kn = sum(r["net"] for r in k)
        base = sum(r["net"] for r in seg)
        improved = kn > base
        print(f"    {side:<6} 保留{kn:+7.2f}U(n={len(k)}) 被过滤{sum(r['net'] for r in d):+7.2f}U(n={len(d)}) "
              f"全段{base:+7.2f}U → {'改善' if improved else '恶化'}")
        if not improved:
            fails.append(f"关3 {side}未改善")

    # 关 4：被过滤的是什么
    print("\n  关4 被过滤组特征分布：")
    for key, name in (("vol_x", "量比"), ("range_pos_pct", "区间位置"),
                       ("dd_from_high_pct", "距高回撤"), ("pre4h_pct", "前4h涨跌"),
                       ("trend", "结构"), ("src", "信号源")):
        def dist(rs):
            vals = [r.get(key) for r in rs if r.get(key) is not None]
            if not vals:
                return "n/a"
            if isinstance(vals[0], str):
                from collections import Counter
                return str(dict(Counter(vals)))
            return f"均{sum(vals)/len(vals):.2f} 区间{min(vals):.2f}~{max(vals):.2f}"
        print(f"    {name:<10} 保留: {dist(keep):<34} 被过滤: {dist(drop)}")

    # 关 5：集中度
    print("\n  关5 集中度（去掉最差一笔后是否仍成立）：")
    worst = min(drop, key=lambda r: r["net"])
    drop2 = [r for r in drop if r is not worst]
    kn = sum(r["net"] for r in keep)
    dn2 = sum(r["net"] for r in drop2)
    base = sum(r["net"] for r in rows)
    share = abs(worst["net"]) / abs(sum(r["net"] for r in drop)) * 100 if drop else 0
    print(f"    被过滤组最差一笔: {worst['id']} {worst['net']:+.2f}U（占被过滤组亏损 {share:.0f}%）")
    print(f"    去掉它后: 保留{kn:+7.2f}U vs 全段{base:+7.2f}U → "
          f"{'仍改善 ✓' if kn>base else '翻转为恶化 ✗（效果由单笔支撑）'}")
    if kn <= base:
        fails.append("关5 集中度（去掉最差一笔后不成立）")

    print(f"\n  → 结论: {'✓ 成立' if not fails else '✗ 不成立 —— ' + '、'.join(fails)}")
    return not fails


def main():
    print("=" * 108)
    print("老师单入场画像 → 套到我的策略单上做过滤（§7.6 五关验证）")
    print("=" * 108)

    # 老师单画像（已有 teacher_analysis 输出，这里重新取以保证同源）
    tinfo = ta.build_teacher_analysis()
    print(f"\n【老师单】{tinfo['summary']['total']} 单 | 已结算 {tinfo['summary']['settled']} | "
          f"胜率 {tinfo['summary']['win_rate_pct']}% | 净 {tinfo['summary']['net_usd']}U | "
          f"均 {tinfo['summary']['avg_usd']}U | 盈亏比 {tinfo['summary']['payoff']}")
    prof = tinfo["profile"]
    print(f"  入场画像: 回撤均 {prof['dd_from_high_pct']}% | 区间位置均 {prof['range_pos_pct']}% | "
          f"量比均 {prof['vol_x']}x | 顺势 {prof['aligned']}(胜率 {prof['aligned_win_rate']}%) / "
          f"逆势 {prof['counter']}(胜率 {prof['counter_win_rate']}%)")
    TVOL = prof["vol_x"]
    TRANGE = prof["range_pos_pct"]
    TDROP = prof["dd_from_high_pct"]

    prod = load_ctx_for_journal(PROD, "prod")
    train = load_ctx_for_journal(TRAIN, "train")
    print(f"\n【我的策略单】生产 {len(prod)} 笔可信已结算 | 训练 {len(train)} 笔")

    for label, rows in (("生产", prod), ("训练", train)):
        print(f"\n{'='*108}")
        print(f"【{label}】{len(rows)} 笔")
        print("=" * 108)
        agg(rows, "全量")
        bucket(rows, "vol_x", [(None, 1.0), (1.0, 2.0), (2.0, 3.0), (3.0, None)],
               f"入场量比 vol_x（老师均 {TVOL}x）")
        bucket(rows, "range_pos_pct", [(None, 20), (20, 60), (60, None)],
               f"入场价在 24h 区间位置（老师均 {TRANGE}%）")
        bucket(rows, "dd_from_high_pct", [(None, 1.0), (1.0, 2.0), (2.0, None)],
               f"距 24h 高点回撤（老师均 {TDROP}%）")
        bucket(rows, "pre4h_pct", [(None, -0.5), (-0.5, 0), (0, None)],
               "入场前 4h 涨跌（老师均 -0.18%）")

    # ── 候选规则（基于【修正锚点后】的老师画像）──
    # ★ 老师真实量比只有 1.32x（不是旧代码算出来的 4.05x —— 那是前视偏差造出来的）
    print(f"\n{'#'*108}")
    print(f"候选规则 1：vol_x ≥ 3.0（老师旧口径的「巨量」——现已证伪，见 anchor 修正）")
    print("#" * 108)
    for label, rows in (("生产", prod), ("训练", train)):
        if not rows:
            continue
        print(f"\n【{label}】")
        gate5(rows, lambda r: (r.get("vol_x") or 0) >= 3.0, f"{label} vol_x ≥ 3.0",
                thresholds={"vol_x ≥ 2.5": lambda r: (r.get("vol_x") or 0) >= 2.5,
                            "vol_x ≥ 3.5": lambda r: (r.get("vol_x") or 0) >= 3.5})

    print(f"\n{'#'*108}")
    print(f"候选规则 2：vol_x 在 1.0~2.0（老师均 {TVOL}x 附近，生产该桶 +35.30U / 18 笔）")
    print("#" * 108)
    for label, rows in (("生产", prod), ("训练", train)):
        if not rows:
            continue
        print(f"\n【{label}】")
        gate5(rows, lambda r: r.get("vol_x") is not None and 1.0 <= r["vol_x"] < 2.0,
                f"{label} 1.0 ≤ vol_x < 2.0",
                thresholds={"0.8≤vol_x<1.6": lambda r: r.get("vol_x") is not None and 0.8 <= r["vol_x"] < 1.6,
                            "1.2≤vol_x<2.4": lambda r: r.get("vol_x") is not None and 1.2 <= r["vol_x"] < 2.4,
                            "1.0≤vol_x<3.0": lambda r: r.get("vol_x") is not None and 1.0 <= r["vol_x"] < 3.0})

    print(f"\n{'#'*108}")
    print(f"候选规则 3：距 24h 高点回撤 ≥ 2.0%（老师均 {TDROP}%，生产该桶 +16.39U / 17 笔）")
    print("#" * 108)
    for label, rows in (("生产", prod), ("训练", train)):
        if not rows:
            continue
        print(f"\n【{label}】")
        gate5(rows, lambda r: (r.get("dd_from_high_pct") or 0) >= 2.0, f"{label} 回撤 ≥ 2.0%",
                thresholds={"回撤≥1.5%": lambda r: (r.get("dd_from_high_pct") or 0) >= 1.5,
                            "回撤≥2.5%": lambda r: (r.get("dd_from_high_pct") or 0) >= 2.5,
                            "回撤≥3.0%": lambda r: (r.get("dd_from_high_pct") or 0) >= 3.0})

    print(f"\n{'#'*108}")
    print(f"候选规则 4：入场前 4h 跌幅 ≤ -0.5%（老师均 -0.18%）")
    print("#" * 108)
    for label, rows in (("生产", prod), ("训练", train)):
        if not rows:
            continue
        print(f"\n【{label}】")
        gate5(rows, lambda r: r.get("pre4h_pct") is not None and r["pre4h_pct"] <= -0.5,
                f"{label} 前4h ≤ -0.5%",
                thresholds={"前4h ≤ -0.3%": lambda r: (r.get("pre4h_pct") or 0) <= -0.3,
                            "前4h ≤ -0.8%": lambda r: (r.get("pre4h_pct") or 0) <= -0.8,
                            "前4h ≤ -1.2%": lambda r: (r.get("pre4h_pct") or 0) <= -1.2})

    print(f"\n{'#'*108}")
    print(f"候选规则 5：回撤 ≥ 2% 且 前4h ≤ -0.3%（老师画像的复合：跌得够 + 跌得急）")
    print("#" * 108)
    for label, rows in (("生产", prod), ("训练", train)):
        if not rows:
            continue
        print(f"\n【{label}】")
        gate5(rows, lambda r: (r.get("dd_from_high_pct") or 0) >= 2.0 and (r.get("pre4h_pct") or 0) <= -0.3,
                f"{label} 回撤≥2% 且 前4h≤-0.3%",
                thresholds={"回撤≥1.5% & 前4h≤-0.5%": lambda r: (r.get("dd_from_high_pct") or 0) >= 1.5 and (r.get("pre4h_pct") or 0) <= -0.5,
                            "回撤≥2.5% & 前4h≤-0.3%": lambda r: (r.get("dd_from_high_pct") or 0) >= 2.5 and (r.get("pre4h_pct") or 0) <= -0.3,
                            "回撤≥2% & 前4h≤0": lambda r: (r.get("dd_from_high_pct") or 0) >= 2.0 and (r.get("pre4h_pct") or 0) <= 0})


if __name__ == "__main__":
    main()
