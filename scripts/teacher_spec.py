#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
teacher_spec.py —— 从老师账本【重新生成】模式规格（数据，不是代码）

## 设计原则（2026-11-07 liusir 纠正后）
「把能写成 skill 动态的都不要写死」。

所以这里**不写死任何阈值**。所有数字都是：
    从 /root/data/teacher_trades.jsonl 现场算出来
    → 落成 JSON 规格（可 review、可 diff、可随样本增长重新生成）
    → 通用工具 ref_trader_tools.py 消费这个规格

老师样本增加 → 重跑本脚本 → 规格自动更新 → 复刻结论自动跟着更新。
**不需要改任何代码。**

## 输出
    /root/data/teacher_spec.json
      - mode: 出场阶梯规格（TP1 幅度 / 减仓 / TP2 / SL）→ 分布 + 覆盖率
      - dims: 每个判读维度的样本分布 + 覆盖率分位
      - meta: 样本量、时间跨度、生成时间

## 用法
    python3 teacher_spec.py              # 生成规格
    python3 teacher_spec.py --print      # 生成并打印人类可读版
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, "/root/skills/btc-gate-intraday-strategy/web")

import teacher_analysis as ta
from ref_trader_tools import CST, BarSeries, bar_features, spec_from_samples

TEACHER_LEDGER = "/root/data/teacher_trades.jsonl"
OUT_PATH = "/root/data/teacher_spec.json"
KL = "/root/data/BTC_USDT_klines.json"


def num(v):
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    try:
        return float(str(v).strip().replace(",", "").split("-")[0].split("~")[0])
    except (TypeError, ValueError):
        return None


def load_bars(tf):
    return json.load(open(KL))["BTC_USDT"][tf]["bars"]


def collect_entries():
    """老师的入场样本（用 teacher_analysis 的反查口径，入口唯一）。"""
    rows = [json.loads(l) for l in open(TEACHER_LEDGER) if l.strip()]
    rows.sort(key=lambda r: r["trade_id"])
    per = {t["trade_id"]: t for t in ta.build_teacher_analysis()["per_trade"]}
    out = []
    for r in rows:
        pr = per.get(r["trade_id"], {})
        ts = pr.get("entry_ts")
        ev = next((e for e in (r.get("events") or []) if e.get("type") == "entry"), {})
        px = num(r.get("fill_price")) or num(ev.get("px")) or num(r.get("entry_zone"))
        if not ts or not px:
            continue
        res = r.get("result") or {}
        out.append({"id": r["trade_id"], "ts": int(ts), "px": float(px),
                    "side": r.get("side"), "net": res.get("net_usd"),
                    "entry_ts_source": pr.get("entry_ts_source")})
    return out


def exit_ladder(rows):
    """出场阶梯规格 —— 全部从 events 实测，不预设。"""
    tp1_pct, tp1_qty, tp2_pct, sl_pct = [], [], [], []
    for r in rows:
        entry = num(r.get("fill_price")) or num(r.get("entry_zone"))
        if not entry:
            continue
        evs = r.get("events") or []
        sgn = 1.0 if r.get("side") == "long" else -1.0
        tp1ev = next((e for e in evs if e.get("type") in ("tp1_partial", "tp1_hit", "tp1")), None)
        tp2ev = next((e for e in evs if e.get("type") in ("tp2_partial", "tp2_full", "tp2_hit")), None)
        slev = next((e for e in evs if e.get("type") in ("sl_triggered", "close_sl")), None)
        if tp1ev and num(tp1ev.get("px")):
            tp1_pct.append((num(tp1ev["px"]) - entry) / entry * sgn * 100)
        elif num(r.get("tp1")):
            tp1_pct.append((num(r["tp1"]) - entry) / entry * sgn * 100)
        if tp1ev and tp1ev.get("qty_pct"):
            tp1_qty.append(float(tp1ev["qty_pct"]))
        if tp2ev and num(tp2ev.get("px")):
            tp2_pct.append((num(tp2ev["px"]) - entry) / entry * sgn * 100)
        elif num(r.get("tp2")):
            tp2_pct.append((num(r["tp2"]) - entry) / entry * sgn * 100)
        sl = num(r.get("sl_initial"))
        if sl is None and slev and num(slev.get("px")):
            sl = num(slev["px"])
        if sl:
            sl_pct.append(abs(entry - sl) / entry * 100)

    def dist(v):
        if not v:
            return {"n": 0}
        s = sorted(v)
        return {"n": len(v), "min": round(s[0], 4), "p25": round(s[len(s) // 4], 4),
                "median": round(s[len(s) // 2], 4), "p75": round(s[3 * len(s) // 4], 4),
                "max": round(s[-1], 4), "mean": round(sum(v) / len(v), 4)}

    return {
        "tp1_pct": dist(tp1_pct), "tp1_reduce_pct": dist(tp1_qty),
        "tp2_pct": dist(tp2_pct), "sl_pct": dist(sl_pct),
        "breakeven_after_tp1": True,
        "note": "TP1/SL 幅度用 events 里的实际成交价算（summary 字段 sl_pct/tp1_pct 已知损坏，勿用）",
    }


def main():
    entries = collect_entries()
    if not entries:
        print("老师账本里没有可反查的入场样本")
        return

    bars15 = load_bars("15m")
    samples = []
    for e in entries:
        f = bar_features(bars15, e["ts"], e["px"], e["side"], tf_min=15)
        if not f:
            continue
        samples.append({**f, "side": e["side"], "_ts": e["ts"], "_id": e["id"]})

    spec = {
        "meta": {
            "generated_at": datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S CST"),
            "source_ledger": TEACHER_LEDGER,
            "n_entries": len(samples),
            "span": f"{datetime.fromtimestamp(min(e['ts'] for e in entries), CST):%Y-%m-%d}"
                    f" → {datetime.fromtimestamp(max(e['ts'] for e in entries), CST):%Y-%m-%d}",
            "rebuild_cmd": "python3 scripts/teacher_spec.py",
            "principle": "规格是数据不是代码；样本增长后重跑本脚本即自动更新",
        },
        "mode": exit_ladder([json.loads(l) for l in open(TEACHER_LEDGER) if l.strip()]),
        "dims": spec_from_samples(samples, coverages=(0.5, 0.6, 0.7, 0.8, 0.9)),
        "sides": {},
    }
    from collections import Counter
    spec["sides"] = dict(Counter(e["side"] for e in entries))
    # 时段集中度
    hours = Counter(datetime.fromtimestamp(e["ts"], CST).hour for e in entries)
    night = sum(v for h, v in hours.items() if h >= 20 or h <= 1)
    spec["mode"]["hour_hist"] = dict(sorted(hours.items()))
    spec["mode"]["night_share_pct"] = round(night / len(entries) * 100, 1)

    with open(OUT_PATH, "w", encoding="utf-8") as fh:
        json.dump(spec, fh, ensure_ascii=False, indent=2)
    print(f"规格已生成: {OUT_PATH}")
    print(f"  样本 {spec['meta']['n_entries']} 笔 | {spec['meta']['span']}")

    if "--print" in sys.argv:
        print("\n=== 出场阶梯 ===")
        for k, v in spec["mode"].items():
            if isinstance(v, dict) and v.get("n"):
                print(f"  {k:<18} n={v['n']:>2} 中位{v['median']:>8.4f} "
                      f"p25={v['p25']:>8.4f} p75={v['p75']:>8.4f} 范围[{v['min']},{v['max']}]")
        print(f"  时段集中度        夜间(20-01) {spec['mode']['night_share_pct']}%")
        print(f"  方向              {spec['sides']}")
        print("\n=== 判读维度 ===")
        print("  数值维度（le_pXX = XX% 样本 <= 该值；ge_pXX = XX% 样本 >= 该值）")
        print(f"  {'维度':<22}{'n':<4}{'中位':<12}{'le_p80':<12}{'ge_p80':<12}")
        for k, v in spec["dims"]["dims"].items():
            if v.get("categorical"):
                print(f"  {k:<22}{v['n']:<4}分类 {v['dist']}")
                continue
            print(f"  {k:<22}{v['n']:<4}{str(v['median']):<12}"
                  f"{str(v.get('le_p80')):<12}{str(v.get('ge_p80')):<12}")


if __name__ == "__main__":
    main()
