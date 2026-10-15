#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
回测：teacher_bias 对生产 journal 综合评分的影响
====================================================

liusir 2026-10-13 拍板要做的回测：
  对每一笔已结算的 production journal 信号，
  计算「启用 teacher_bias 前后」的综合评分，统计差异。

输入:
  /root/data/journal.jsonl (生产)
  /root/data/teacher_trades.jsonl (老师)

输出:
  - 启用前后评分对比 (mean / median / 阈值过线数)
  - 按 teacher_bias 标签 (well/oppose/neutral/weak) 分桶的胜率
  - 关键单条明细

用法:
    cd /root/skills/btc-gate-intraday-strategy/scripts
    python3 backtest_teacher_bias.py
"""
from __future__ import annotations

import json
import os
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# 复用已有逻辑
from teacher_bias import (
    BIAS_WELL, BIAS_OPPOSE, BIAS_NEUTRAL, BIAS_WEAK,
    TEACHER_WINDOW_HOURS,
    get_teacher_bias,
)
from training_config import V5_DEV_CONFIG

# ============================ 配置 ============================
THRESHOLD = V5_DEV_CONFIG['multi_factor']['threshold']  # 2.0
DATA_DIR = "/root/data"
PROD_JOURNAL = os.path.join(DATA_DIR, "journal.jsonl")
TEACHER_FILE = os.path.join(DATA_DIR, "teacher_trades.jsonl")

# ============================ 加载数据 ============================
def load_teacher():
    out = []
    for line in Path(TEACHER_FILE).read_text().strip().split("\n"):
        if not line.strip(): continue
        rec = json.loads(line)
        d = rec.get("date", "")
        ts = None
        head = d[:16]
        try: ts = datetime.strptime(head, "%Y-%m-%d %H:%M")
        except ValueError:
            try: ts = datetime.strptime(d[:10], "%Y-%m-%d")
            except ValueError:
                ev = (rec.get("events") or [{}])[0].get("ts", "")
                m = re.match(r"(\d{4}-\d{2}-\d{2})\s*(\d{2}:\d{2})", ev)
                if m: ts = datetime.strptime(f"{m.group(1)} {m.group(2)}", "%Y-%m-%d %H:%M")
                else:
                    m = re.match(r"(\d{4}-\d{2}-\d{2})", ev)
                    if m: ts = datetime.strptime(m.group(1), "%Y-%m-%d")
        if not ts: continue
        out.append({"trade_id": rec["trade_id"], "ts": ts, "side": rec["side"],
                    "entry": rec.get("entry_zone"), "outcome": rec["result"]["outcome"],
                    "net_usd": rec["result"].get("net_usd", 0)})
    return out


def load_prod():
    out = []
    for line in Path(PROD_JOURNAL).read_text().strip().split("\n"):
        if not line.strip(): continue
        rec = json.loads(line)
        if "ts" not in rec: continue
        ts = datetime.fromtimestamp(rec["ts"])
        meta = rec.get("meta", {})
        tags = rec.get("tags", {})
        # pattern_15m 提取: 'pattern' tag 或 verdict 关键词
        pattern = tags.get("pattern", "")
        # 简化: verdict 包含 "锤子"+"阳线" 视为 hammer
        verdict = rec.get("verdict", "")
        if "锤子" in verdict:
            pattern = "hammer"
        elif "流星" in verdict:
            pattern = "shooting_star"
        out.append({
            "id": rec.get("id"),
            "ts": ts,
            "date_str": ts.strftime("%Y-%m-%d"),
            "hour": ts.hour,
            "side": rec.get("side"),
            "score": rec.get("score"),
            "status": rec.get("status"),
            "outcome": rec.get("outcome"),
            "category": rec.get("category"),
            "net_usd": rec.get("net_usd"),
            "tp1_hit": rec.get("tp1_hit"),
            "tp_mode": rec.get("tp_mode"),
            "signal_source": meta.get("signal_source", "default"),
            "vol": tags.get("vol"),
            "fib": tags.get("fib"),
            "pattern": pattern,
            "time_of_day": ts.hour,
        })
    out.sort(key=lambda r: r["ts"])
    return out


def v5_signal_strength(rec) -> tuple[float, dict]:
    """
    复现 v5_dev_strategy.calculate_signal_strength 评分
    返回 (总评分, 分数明细)
    """
    cfg = V5_DEV_CONFIG['multi_factor']

    # 1h score 加权
    s1 = rec.get("score") or 0
    w1 = cfg['w1_1h_score'] * s1

    # 15m pattern
    pat = (rec.get("pattern") or "").lower()
    if "hammer" in pat or "锤子" in pat:
        ps = 3
    elif "shooting_star" in pat or "流星" in pat:
        ps = 3
    elif "doji" in pat:
        ps = 1
    else:
        ps = 0
    w2 = cfg['w2_15m_pattern'] * ps

    # time of day weight
    h = rec.get("hour", 0)
    if 0 <= h <= 4: tw = 0.5
    elif h == 22: tw = -1.0
    elif 5 <= h <= 23: tw = -0.5
    else: tw = 0.0
    w3 = cfg['w3_time_of_day'] * tw

    # vol weight
    vol = (rec.get("vol") or "").lower()
    if "温和" in vol: vw = 0.5
    elif "放量" in vol: vw = -0.3
    elif "缩量" in vol: vw = -0.5
    else: vw = 0.0
    w4 = cfg['w4_vol_regime'] * vw

    # fib
    fib = bool(rec.get("fib") and rec["fib"] != "无")
    w5 = cfg['w5_fib_resonance'] * (1 if fib else 0)

    base = w1 + w2 + w3 + w4 + w5

    # teacher bias
    side = rec.get("side")
    if side in ("long", "short"):
        label, tb = get_teacher_bias(side=side, hour=h, date_str=rec["date_str"])
        weighted = tb * V5_DEV_CONFIG['teacher_bias']['weight']
    else:
        label, tb, weighted = "neutral", 0.0, 0.0

    total = base + weighted
    return round(total, 3), {
        "base": round(base, 2),
        "teacher_label": label,
        "teacher_score": tb,
        "teacher_weighted": round(weighted, 3),
        "total_with_bias": round(total, 3),
        "total_without_bias": round(base, 3),
    }


# ============================ 主回测 ============================
def main():
    teacher = load_teacher()
    prod = load_prod()
    print(f"老师单 {len(teacher)} 单")
    print(f"生产 journal {len(prod)} 条")
    print(f"v5 综合评分阈值: {THRESHOLD}\n")

    # 计算每条 production 信号的 v5 评分 (with/without teacher_bias)
    results = []
    for rec in prod:
        total, breakdown = v5_signal_strength(rec)
        rec2 = {**rec, **breakdown}
        rec2["passes_with_bias"] = total >= THRESHOLD
        rec2["passes_without_bias"] = breakdown["total_without_bias"] >= THRESHOLD
        results.append(rec2)

    # ============================ 整体对比 ============================
    with_teacher_pass = sum(1 for r in results if r["passes_with_bias"])
    without_teacher_pass = sum(1 for r in results if r["passes_without_bias"])
    diff = with_teacher_pass - without_teacher_pass

    print(f"过阈值数 (score>={THRESHOLD}):")
    print(f"  无 teacher_bias: {without_teacher_pass} / {len(results)}")
    print(f"  有 teacher_bias: {with_teacher_pass} / {len(results)}")
    print(f"  净变化: {diff:+d} 条 ({diff/len(results)*100:+.1f}%)\n")

    # ============================ 按 teacher_label 分桶胜率 ============================
    by_label = defaultdict(list)
    for r in results:
        if r["status"] != "settled":
            continue
        by_label[r["teacher_label"]].append(r)

    print("按 teacher_label 分桶的胜率（仅 settled 单）:")
    print(f"{'label':<10} {'n':>4} {'wins':>5} {'loss':>5} {'WR':>7} {'net_usd':>10} {'avg':>8}")
    print("-" * 60)
    for label in ("well", "oppose", "neutral", "weak"):
        rows = by_label.get(label, [])
        wins = sum(1 for r in rows if r["category"] == "profit")
        loss = sum(1 for r in rows if r["category"] == "loss")
        wr = wins / max(wins + loss, 1) * 100
        net = sum(r.get("net_usd", 0) or 0 for r in rows)
        avg = net / max(len(rows), 1)
        print(f"{label:<10} {len(rows):>4} {wins:>5} {loss:>5} {wr:>6.1f}% {net:>+9.2f}U {avg:>+7.2f}U")
    print()

    # ============================ 信号被反转的清单 ============================
    flips = [r for r in results if r["passes_with_bias"] != r["passes_without_bias"]]
    if flips:
        print(f"--- 阈值翻转明细 ({len(flips)} 条) ---")
        print(f"{'id':<40} {'side':<6} {'date':<10} {'label':<8} {'base':>6} {'with':>6} {'flip':<10} {'outcome':<15} {'net':>7}")
        print("-" * 110)
        for r in flips:
            direction = "进入" if r["passes_with_bias"] else "滤掉"
            print(f"{r['id']:<40} {r['side']:<6} {r['date_str']:<10} {r['teacher_label']:<8} "
                  f"{r['total_without_bias']:>6.2f} {r['total_with_bias']:>6.2f} {direction:<8} "
                  f"{str(r.get('outcome','-')):<15} {(r.get('net_usd') or 0):>+7.2f}")
        print("→ 信号被 teacher_bias 翻转后，胜率:")
        # 翻入（base 不过 + bias 过了）的胜率
        flipped_in = [r for r in flips if r["passes_with_bias"]]
        flipped_out = [r for r in flips if not r["passes_with_bias"]]
        if flipped_in:
            w = sum(1 for r in flipped_in if r["category"] == "profit")
            l = sum(1 for r in flipped_in if r["category"] == "loss")
            n = sum(r.get("net_usd", 0) or 0 for r in flipped_in)
            print(f"  翻入 (base 不过 + bias 过了): {len(flipped_in)} 条, 胜率 {w/max(w+l,1)*100:.1f}%, 净 {n:+.2f}U")
        if flipped_out:
            w = sum(1 for r in flipped_out if r["category"] == "profit")
            l = sum(1 for r in flipped_out if r["category"] == "loss")
            n = sum(r.get("net_usd", 0) or 0 for r in flipped_out)
            print(f"  翻出 (base 过了 + bias 没过): {len(flipped_out)} 条, 胜率 {w/max(w+l,1)*100:.1f}%, 净 {n:+.2f}U")
        print()

    # ============================ 假设回测（teacher_bias 过滤后总 PnL） ============================
    print("--- 假设 teacher_bias 过滤后的总 PnL ---")
    pnl_orig = sum((r.get("net_usd") or 0) for r in results if r["status"] == "settled")
    pnl_new = sum(
        (r.get("net_usd") or 0) for r in results
        if r["status"] == "settled" and r["passes_with_bias"]
    )
    pnl_baseline = sum(
        (r.get("net_usd") or 0) for r in results
        if r["status"] == "settled" and r["passes_without_bias"]
    )
    print(f"原始 settled 总 PnL: {pnl_orig:+.2f}U ({sum(1 for r in results if r['status']=='settled')} 单)")
    print(f"按 base 阈值过滤后 PnL: {pnl_baseline:+.2f}U ({without_teacher_pass} 单)")
    print(f"按 base + teacher_bias 阈值过滤后 PnL: {pnl_new:+.2f}U ({with_teacher_pass} 单)")
    print(f"teacher_bias 净贡献: {pnl_new - pnl_baseline:+.2f}U\n")

    # ============================ 反例：生产 vs 老师 同期冲突 ============================
    print("--- 老师 vs 生产同期冲突明细 ---")
    conflict_count = 0
    for t in teacher:
        for r in results:
            if r["status"] != "settled": continue
            if abs((r["ts"] - t["ts"]).total_seconds()) > TEACHER_WINDOW_HOURS * 3600: continue
            if r["side"] == t["side"]: continue  # 同向不算冲突
            conflict_count += 1
            if conflict_count <= 10:
                print(f"  老师 #{t['trade_id']} {t['ts']} {t['side']} ({t['outcome']})  vs  "
                      f"生产 {r['id'][-25:]} {r['ts']} {r['side']} → {r.get('outcome','')} "
                      f"({(r.get('net_usd') or 0):+.2f}U)")
    print(f"  冲突信号总数: {conflict_count}\n")


if __name__ == "__main__":
    main()