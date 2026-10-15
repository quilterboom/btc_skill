#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
亏钱模式归类 — Step 2 (liusir 方法论)
=====================================
liusir 2026-10-13 拍板：亏钱了就分析为什么亏，找参数改动避免亏钱

读 postmortem_losses.py 的输出，统计:
  A. 三周期一致性 (consensus) × 亏钱
  B. v2 bias (long/short/neutral) × 亏钱
  C. 全周期 RSI≤40 × 亏钱 (反向信号)
  D. 放量环境 × 亏钱
  E. 4h/1h swing 同向 vs 反向
  F. 找出"反复出现的亏钱模式" → 候选参数改动
"""
import json
import re
import sys
from collections import defaultdict, Counter
from datetime import datetime
from pathlib import Path


JOURNAL = "/root/data/journal.jsonl"
POSTMORTEM_LOG = "/tmp/postmortem_full.log"


def parse_postmortem_log():
    """读 postmortem_losses.py 跑出来的完整日志"""
    p = Path(POSTMORTEM_LOG)
    if not p.exists():
        return []
    text = p.read_text()
    records = []
    # 匹配每个亏钱单块
    pattern = re.compile(
        r"  (?P<ts>\d{2}-\d{2} \d{2}:\d{2}) (?P<side>\w+) +entry= *(?P<entry>[\d.]+) +net= *(?P<net>[-+.\d]+)U.*?"
        r"v2: bias=(?P<bias>\w+) +norm=(?P<norm>[-+.\d]+) consensus=(?P<consensus>[\w+()]+).*?"
        r"周期评分: (?P<parts>.*?)\n"
        r"    swing: (?P<swing>.*?)\n"
        r"    momentum: (?P<mom>.*?)\n"
        r"    trend: (?P<trend>.*?)\n",
        re.DOTALL
    )
    for m in pattern.finditer(text):
        d = m.groupdict()
        d["net"] = float(d["net"])
        d["norm"] = float(d["norm"])
        records.append(d)
    return records


def main():
    records = parse_postmortem_log()
    print(f"解析到 {len(records)} 笔亏钱单的 v2 重判数据\n")
    if not records:
        print("⚠️ 没找到日志，先跑 postmortem_losses.py 把日志存到 /tmp/postmortem_full.log")
        return

    print("=" * 70)
    print("【归类 1】三周期一致性 × 亏钱")
    print("=" * 70)
    by_cons = defaultdict(list)
    for r in records:
        by_cons[r["consensus"]].append(r)
    for c in ("强一致", "弱一致（含中性）", "不一致"):
        rows = by_cons[c]
        n = len(rows)
        net = sum(r["net"] for r in rows)
        print(f"  {c:<25} n={n:>2}  净 {net:+7.2f}U")
    print()

    print("=" * 70)
    print("【归类 2】v2 bias × 亏钱")
    print("=" * 70)
    by_bias = defaultdict(list)
    for r in records:
        by_bias[r["bias"]].append(r)
    for b in ("long", "short", "neutral"):
        rows = by_bias[b]
        n = len(rows)
        net = sum(r["net"] for r in rows)
        print(f"  v2={b:<8} n={n:>2}  净 {net:+7.2f}U")
    print()

    print("=" * 70)
    print("【归类 3】开仓方向 vs v2 判定方向")
    print("=" * 70)
    # 开 long 但 v2 neutral/short → 逆 v2 决策
    # 开 long 且 v2 long → 顺 v2 决策
    counter_v2 = defaultdict(list)
    for r in records:
        if r["side"] == "long":
            if r["bias"] == "long":
                key = "顺v2开long"
            else:
                key = f"逆v2开long(v2={r['bias']})"
        elif r["side"] == "short":
            if r["bias"] == "short":
                key = "顺v2开short"
            else:
                key = f"逆v2开short(v2={r['bias']})"
        counter_v2[key].append(r)
    for k in sorted(counter_v2.keys()):
        rows = counter_v2[k]
        net = sum(r["net"] for r in rows)
        print(f"  {k:<25} n={len(rows):>2}  净 {net:+7.2f}U")
    print()

    print("=" * 70)
    print("【归类 4】全周期 RSI ≤ 40 (反向超卖区域)")
    print("=" * 70)
    rsi_low = []
    for r in records:
        mom = r["mom"]
        # 提取 4h/1h/15m 的 RSI
        rsis = re.findall(r"RSI=(\d+)/(\w+)", mom)
        if len(rsis) == 3:
            vals = [int(v[0]) for v in rsis]
            states = [v[1] for v in rsis]
            if all(v <= 40 for v in vals):
                rsi_low.append(r)
                print(f"  {r['ts']}  RSI={vals}  states={states}  net={r['net']:+.2f}")
    n = len(rsi_low)
    net = sum(r["net"] for r in rsi_low)
    print(f"\n  共 {n} 单 (RSI全部≤40)，净 {net:+.2f}U")
    print()

    print("=" * 70)
    print("【归类 5】放量环境 (波动率维度判 +1 + 其它条件)")
    print("=" * 70)
    # 简化: 找出 trend 里 EMA 有震荡/缠绕 + swing 不一致
    ambiguous = []
    for r in records:
        trend = r["trend"]
        if "震荡" in trend or "缠绕" in trend or "回踩" in trend or "反抽" in trend:
            ambiguous.append(r)
    n = len(ambiguous)
    net = sum(r["net"] for r in ambiguous)
    print(f"  trend=震荡/回踩/反抽 类: {n} 单, 净 {net:+.2f}U")
    print()

    print("=" * 70)
    print("【归类 6】最大亏损 Top 5")
    print("=" * 70)
    by_loss = sorted(records, key=lambda r: r["net"])[:5]
    for r in by_loss:
        print(f"  {r['ts']} 开{r['side']} v2={r['bias']} consensus={r['consensus']}")
        print(f"    swing: {r['swing'][:70]}")
        print(f"    net: {r['net']:+.2f}U")
    print()

    print("=" * 70)
    print("【模式汇总 + 候选参数改动】")
    print("=" * 70)
    print()

    # 自动归类模式
    modes = []

    # 模式 1: v2 neutral 但开仓 → 8 单 (10-08 那批)
    n_neutral_open = len([r for r in records if r["bias"] == "neutral"])
    net_neutral = sum(r["net"] for r in records if r["bias"] == "neutral")
    modes.append({
        "pattern": "v2 判定 neutral 但仍开仓",
        "n": n_neutral_open,
        "net": net_neutral,
        "fix": "v2 必须 bias != neutral 才允许开仓 (norm 阈值至少 0.20)",
    })

    # 模式 2: 全周期 RSI≤40 (反向超卖) 仍开 long
    n_low_long = sum(1 for r in rsi_low if r["side"] == "long")
    net_low_long = sum(r["net"] for r in rsi_low if r["side"] == "long")
    if n_low_long > 0:
        modes.append({
            "pattern": "全周期 RSI≤40 但开 long (追底)",
            "n": n_low_long,
            "net": net_low_long,
            "fix": "RSI≤40 是抄底信号，不该追涨；v2 改: RSI≤25 反而加分，25-40 中性",
        })

    # 模式 3: consensus 不一致
    n_incons = len(by_cons.get("不一致", []))
    net_incons = sum(r["net"] for r in by_cons.get("不一致", []))
    modes.append({
        "pattern": "consensus 不一致 (4h/1h/15m 不同向) 但仍开仓",
        "n": n_incons,
        "net": net_incons,
        "fix": "consensus 不一致 时 v2 应给 neutral，不允许开仓",
    })

    # 模式 4: 4h trend=空头/震荡 但 v2 仍开 long (逆大势)
    n_against_4h = 0
    net_against_4h = 0
    for r in records:
        if r["side"] == "long" and ("空头排列" in r["trend"] or "震荡/缠绕" in r["trend"]):
            n_against_4h += 1
            net_against_4h += r["net"]
    if n_against_4h > 0:
        modes.append({
            "pattern": "4h trend 空/震荡 但开 long",
            "n": n_against_4h,
            "net": net_against_4h,
            "fix": "v2 加: 4h trend=空/震荡时, 即使 15m/1h 看多, 综合 norm 上限 0.10",
        })

    for m in sorted(modes, key=lambda x: x["net"]):
        print(f"  【{m['pattern']}】")
        print(f"     n={m['n']}, 净 {m['net']:+.2f}U")
        print(f"     改: {m['fix']}")
        print()


if __name__ == "__main__":
    main()