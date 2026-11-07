#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
teacher_fidelity_cause.py —— 剩下的 7 笔复现不出来，是【引擎 bug】还是【K 线分辨率不够】？

## 为什么必须分清
- 若是我引擎的 bug → 修它，保真度能上去，回测结论有效
- 若���分辨率不够（bar 内先后无法定序）→ 修也没用，**任何用这个口径做的回测
  都有同样的不确定性**，必须降级结论口径

## 判定方法
对每笔复现失败的单，找出老师账本里最后一个已知的**精确时刻事件**（形如
"2026-10-23 10:33"），看：
  A. 那个时刻落在哪根 bar
  B. 那根 bar 是否同时触及 TP 和 SL（→ 同根双触 → 15m/1h 无法定序）
  C. 老师成交价 vs 1m/15m 反查的入场时刻是否一致

## 只读
"""
from __future__ import annotations

import bisect
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, "/root/skills/btc-gate-intraday-strategy/scripts")
sys.path.insert(0, "/root/skills/btc-gate-intraday-strategy/web")

import teacher_analysis as ta
from teacher_replica import SigGen, load_bars

CST = timezone(timedelta(hours=8))
TS_RE = re.compile(r"(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2})(:(\d{2}))?")


def num(v):
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    try:
        return float(str(v).strip().replace(",", "").split("-")[0].split("~")[0])
    except (TypeError, ValueError):
        return None


def precise_events(evs):
    """取出有精确到分钟的 ts 的事件。"""
    out = []
    for e in evs:
        ts = e.get("ts")
        if not isinstance(ts, str):
            continue
        m = TS_RE.search(ts)
        if not m:
            continue
        y, mo, d, hh, mi = (int(m.group(i)) for i in (1, 2, 3, 4, 5))
        sec = int(m.group(7) or 0)
        try:
            dt = datetime(y, mo, d, hh, mi, sec, tzinfo=CST)
        except ValueError:
            continue
        out.append((e, int(dt.timestamp())))
    return out


def main():
    rows = [json.loads(l) for l in open("/root/data/teacher_trades.jsonl") if l.strip()]
    rows.sort(key=lambda r: r["trade_id"])
    per = {t["trade_id"]: t for t in ta.build_teacher_analysis()["per_trade"]}

    b1 = json.load(open("/root/data/BTC_USDT_klines.json"))["BTC_USDT"]["1m"]["bars"]
    t1 = [b[0] for b in b1]
    sg15 = SigGen(load_bars("15m"), 15)
    sg1h = SigGen(load_bars("1h"), 60)

    print("=" * 112)
    print("复现失败的原因定位")
    print("=" * 112)
    print(f"\n1m 覆盖: {datetime.fromtimestamp(t1[0],CST):%Y-%m-%d} → {datetime.fromtimestamp(t1[-1],CST):%Y-%m-%d}")
    print(f"15m 覆盖: {datetime.fromtimestamp(sg15.tss[0],CST):%Y-%m-%d} → {datetime.fromtimestamp(sg15.tss[-1],CST):%Y-%m-%d}")
    print(f"1h  覆盖: {datetime.fromtimestamp(sg1h.tss[0],CST):%Y-%m-%d} → {datetime.fromtimestamp(sg1h.tss[-1],CST):%Y-%m-%d}\n")

    causes = {}
    for r in rows:
        tid = r["trade_id"]
        pr = per.get(tid, {})
        res = r.get("result") or {}
        net_t = res.get("net_usd")
        if net_t is None:
            continue
        evs = r.get("events") or []
        pe = precise_events(evs)
        entry_ev = next((e for e in evs if e.get("type") == "entry"), {})
        px = num(r.get("fill_price")) or num(entry_ev.get("px")) or num(r.get("entry_zone"))
        tp1 = num(r.get("tp1"))
        tp2 = num(r.get("tp2"))
        sl = num(r.get("sl_initial"))
        tp1ev = next((e for e in evs if e.get("type") in ("tp1_partial", "tp1_hit", "tp1")), None)
        if tp1ev and num(tp1ev.get("px")):
            tp1 = num(tp1ev.get("px"))

        # 老师的最后一个精确事件
        last = pe[-1] if pe else None
        notes = []
        if last is None:
            notes.append("无精确事件时刻（老师账本时间戳是「后续/后来」这类模糊描述）")
            causes[tid] = "模糊时刻"
        else:
            ev, ets = last
            etype = ev.get("type")
            # 该时刻落在哪根 15m / 1h bar
            i15 = bisect.bisect_right(sg15.tss, ets) - 1
            i1h = bisect.bisect_right(sg1h.tss, ets) - 1
            # 该 bar 是否同时触及 TP 和 SL
            def dual(sg, i):
                if i < 0 or i >= len(sg.bars):
                    return None
                h, l = sg.high[i], sg.low[i]
                t_tp = False
                if tp1:
                    t_tp = (h >= tp1) if r.get("side") == "long" else (l <= tp1)
                t_sl = False
                if sl:
                    t_sl = (l <= sl) if r.get("side") == "long" else (h >= sl)
                return (t_tp, t_sl, h, l)
            d15 = dual(sg15, i15)
            d1h = dual(sg1h, i1h)
            if d15 and d15[0] and d15[1]:
                notes.append(f"15m 同根双触（TP与SL同一根，无法定序）")
            elif d1h and d1h[0] and d1h[1]:
                notes.append(f"1h 同根双触")
            # 1m 覆盖？
            if t1[0] <= ets <= t1[-1]:
                notes.append("该时刻在 1m 覆盖内（可精确定序）")
            else:
                notes.append(f"该时刻在 1m 覆盖外（最早可用 15m）")
            notes.append(f"最后精确事件 {etype} @ {datetime.fromtimestamp(ets,CST):%m-%d %H:%M}")

        # 入场时刻：反查 vs 口述
        kts = pr.get("entry_ts")
        declared = entry_ev.get("ts") or r.get("date") or ""
        if kts:
            kd = datetime.fromtimestamp(kts, CST)
            # 反查命中根数
            hits = 0
            for b in sg15.bars:
                if float(b[3]) <= px <= float(b[2]):
                    if datetime.fromtimestamp(int(b[0]), CST).strftime("%m-%d") == kd.strftime("%m-%d"):
                        hits += 1
            if hits > 1:
                notes.append(f"入场价在当日 15m 有 {hits} 根可成交 → 反查取第一根可能不是老师真实入场")

        print(f"  #{tid:<3} 老师净{net_t:>8.2f} | " + " | ".join(notes))
        causes.setdefault(tid, notes[-1] if notes else "")

    print("\n" + "=" * 112)
    print("归纳")
    print("=" * 112)
    from collections import Counter
    c = Counter()
    for tid, n in causes.items():
        if "模糊时刻" in n:
            c["老师账本时间戳模糊（无法精确回放）"] += 1
        elif "同根双触" in n:
            c["K线分辨率不够（bar 内先后无法定序）"] += 1
        elif "反查取第一根" in n:
            c["入场时刻反查有歧义"] += 1
        else:
            c["其它"] += 1
    for k, v in c.most_common():
        print(f"  {k:<38} {v} 笔")


if __name__ == "__main__":
    main()
