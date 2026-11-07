#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
teacher_regime_test.py —— 行情依赖检验：老师模式只在某段行情有效吗？

## 为什么必须做
btc-strategy-dev 发现②：**一个方向赚钱、另一个方向亏钱时，先别怪信号——先拆行情。**

而且这里有个更基础的事实：
**老师 20 笔全部发生在 2026-10-07 → 2026-11-06 这 30 天里**，
而这 30 天 BTC 是从 ~83000 跌到 ~75500 的**下跌行情**。
他在下跌行情里做【反弹多头】并且 19 笔里赢了 17 笔。

那么规则在这 30 天之外的表现，是判断「模式是否可迁移」的关键。

## 分档维度
1. **BTC 自身趋势**：用 30 日涨幅把长历史分成「上涨档 / 下跌档」
2. **窗口**：老师那 30 天 vs 之前 11 个月
3. **方向**：做多反弹 vs 做空反弹 分开看

## 判据
- 同一规则在不同行情档**方向一致且都为正** → 可迁移的真信号
- 只在某一档为正 → 行情依赖（skill 发现②），不能说「模式有效」

## 只读
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, "/root/skills/btc-gate-intraday-strategy/web")

from teacher_replica import CFG, SigGen, bootstrap_ci, gen_signals, load_bars, run, stats

CST = timezone(timedelta(hours=8))
DAY = 86400


def t30_pct(sg, ts):
    """入场前 30 天涨幅%（1h → 720 根）"""
    look = 30 * 24
    # 找 ts 位置
    import bisect
    i = bisect.bisect_right(sg.tss, ts) - 1
    j = i - look
    if j < 0:
        return None
    return (sg.close[i] - sg.close[j]) / sg.close[j] * 100


def bucket_report(trades, sg, label):
    if not trades:
        print(f"    {label:<26} n=0")
        return
    nets = [t["net"] for t in trades]
    s = stats(trades)
    ci_lo, ci_hi, p_pos = bootstrap_ci(nets)
    print(f"    {label:<26} n={s['n']:>3} 胜率{s['win_rate']:>5.1f}% 净{s['net']:>+9.2f}U "
          f"每笔{s['net']/s['n']:>+6.2f}U 盈亏比{s['payoff']:>4.2f} "
          f"CI[{ci_lo:+.1f},{ci_hi:+.1f}] P(EV>0)={p_pos*100:>4.1f}%")


def main():
    print("=" * 116)
    print("行情依赖检验：老师模式是【可迁移的模式】还是【某段行情的产物】？")
    print("=" * 116)

    for tf, tf_min in (("1h", 60), ("15m", 15)):
        sg = SigGen(load_bars(tf), tf_min)
        span = f"{datetime.fromtimestamp(sg.tss[0],CST):%Y-%m-%d} → {datetime.fromtimestamp(sg.tss[-1],CST):%Y-%m-%d}"
        print(f"\n{'#'*116}\n【{tf}】{span}\n{'#'*116}")

        sigs = gen_signals(sg, CFG)
        trades, _ = run(sg, CFG, sigs=sigs)

        # 老师那 30 天
        t_teacher_lo = int(datetime(2026, 10, 5, tzinfo=CST).timestamp())
        t_teacher_hi = int(datetime(2026, 11, 8, tzinfo=CST).timestamp())

        print("\n  ── 1. 窗口对比：老师那 30 天 vs 其它时间 ──")
        in_t = [t for t in trades if t_teacher_lo <= t["ts"] <= t_teacher_hi]
        out_t = [t for t in trades if not (t_teacher_lo <= t["ts"] <= t_teacher_hi)]
        bucket_report(in_t, sg, "老师窗口内(10/05-11/08)")
        bucket_report(out_t, sg, "老师窗口外")

        print("\n  ── 2. 按 BTC 前 30 日涨幅分档 ──")
        for t in trades:
            t["t30"] = t30_pct(sg, t["ts"])
        buckets = [("深度下跌", None, -10), ("下跌", -10, -3), ("横盘", -3, 3),
                   ("上涨", 3, 10), ("深度上涨", 10, None)]
        for name, lo, hi in buckets:
            sel = [t for t in trades if t["t30"] is not None and
                   ((lo is None and t["t30"] < hi) or
                    (hi is None and t["t30"] >= lo) or
                    (lo is not None and hi is not None and lo <= t["t30"] < hi))]
            bucket_report(sel, sg, name)

        print("\n  ── 3. 方向 × 行情档（拆开看）──")
        for side, sname in (("long", "做多"), ("short", "做空")):
            ss = [t for t in trades if t["side"] == side]
            print(f"    【{sname}】")
            for name, lo, hi in buckets:
                sel = [t for t in ss if t["t30"] is not None and
                       ((lo is None and t["t30"] < hi) or
                        (hi is None and t["t30"] >= lo) or
                        (lo is not None and hi is not None and lo <= t["t30"] < hi))]
                bucket_report(sel, sg, f"  {name}")

        print("\n  ── 4. 入场前 30 日涨幅 vs 该笔净值（连续关系）──")
        pairs = [(t["t30"], t["net"]) for t in trades if t["t30"] is not None]
        if len(pairs) > 20:
            pairs.sort()
            k = max(3, len(pairs) // 5)
            for i in range(0, len(pairs), k):
                chunk = pairs[i:i + k]
                av = sum(c[0] for c in chunk) / len(chunk)
                an = sum(c[1] for c in chunk) / len(chunk)
                print(f"    档{i//k+1}: 前30日均{av:>+7.2f}%  该档每笔均{an:>+7.2f}U  n={len(chunk)}")
            # 相关系数
            xs = [p[0] for p in pairs]
            ys = [p[1] for p in pairs]
            mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
            cov = sum((x - mx) * (y - my) for x, y in pairs)
            sx = sum((x - mx) ** 2 for x in xs) ** 0.5
            sy = sum((y - my) ** 2 for y in ys) ** 0.5
            print(f"    相关系数: {cov/(sx*sy):+.3f}（|r|<0.3 = 无单调关系）")

        print("\n  ── 5. 老师自己那 20 笔所在的行情档 ──")
        rows = [json.loads(l) for l in open("/root/data/teacher_trades.jsonl") if l.strip()]
        import teacher_analysis as ta
        per = {t["trade_id"]: t for t in ta.build_teacher_analysis()["per_trade"]}
        ts_list = [per[r["trade_id"]]["entry_ts"] for r in rows
                   if per.get(r["trade_id"], {}).get("entry_ts")]
        t30s = [t30_pct(sg, ts) for ts in ts_list]
        t30s = [x for x in t30s if x is not None]
        if t30s:
            print(f"    老师 20 笔入场前 30 日涨幅: 均 {sum(t30s)/len(t30s):+.2f}% "
                  f"中位 {sorted(t30s)[len(t30s)//2]:+.2f}% 范围 [{min(t30s):+.2f}, {max(t30s):+.2f}]")
        # 同期 BTC 走势
        if ts_list:
            lo, hi = min(ts_list), max(ts_list)
            import bisect
            i0 = bisect.bisect_right(sg.tss, lo) - 1
            i1 = bisect.bisect_right(sg.tss, hi) - 1
            if i0 >= 0 and i1 < len(sg.close):
                chg = (sg.close[i1] - sg.close[i0]) / sg.close[i0] * 100
                print(f"    老师从第一笔到最后一笔：{sg.close[i0]:.0f} → {sg.close[i1]:.0f} ({chg:+.2f}%)")


if __name__ == "__main__":
    main()
