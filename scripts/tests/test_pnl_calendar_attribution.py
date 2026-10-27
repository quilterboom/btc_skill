#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
P&L 日历归日口径回归测试
========================

2026-10-27 liusir：「数据分析里面的日历下面的盈利数据是错的」

锁住两个 bug：
  1. 日历按**信号日 ts** 归日，但盈亏是出场时产生的 → 每日金额全错
  2. `exit_ts` 被 risk_guard 反复重写（note 实证「重复触发 26267 次」）
     → exit_ts 变成「重写那一刻」的时间，不是真实出场时间

跑法：python3 scripts/tests/test_pnl_calendar_attribution.py
"""
from __future__ import annotations

import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS))

from pnl_calendar import (  # noqa: E402
    compute_pnl_calendar,
    resolve_exit_ts,
    exit_ts_is_suspect,
    _is_filled_trade,
)

FAILS = []


def check(cond: bool, label: str, extra: str = "") -> None:
    if cond:
        print(f"  ✅ {label}")
    else:
        print(f"  ❌ {label} {extra}")
        FAILS.append(label)


def mk(**kw) -> dict:
    base = {
        "id": "t1", "ts": 1_700_000_000, "side": "long", "status": "settled",
        "filled": True, "net_usd": 10.0, "entry": 100.0, "exit_px": 101.0,
        "fill_ts": 1_700_000_000, "exit_ts": 1_700_003_600,
        "outcome": "BE_STOPPED",
    }
    base.update(kw)
    return base


# ------------------------------------------------------------------
print("\n【1】跨日单必须归到出场日，不是信号日")
# 信号 2026-01-20 23:50，出场 2026-01-21 00:10
import datetime as _dt
_sig = int(_dt.datetime(2026, 1, 20, 23, 50).timestamp())
tr = mk(
    ts=_sig, fill_ts=_sig, exit_ts=_sig + 1200,
    net_usd=25.0, hold_h=0.33,
)
cal = compute_pnl_calendar([tr], days=3650)
days = {d["date"]: d for d in cal["days"]}
sig_day = "2026-01-20"
exit_day = "2026-01-21"
check(len(days) == 1, "跨日单只出现在 1 天", str(list(days)))
check(list(days)[0] == exit_day, "归到出场日", f"实际 {list(days)} 期望 {exit_day}")
check(sig_day not in days, "信号日不再出现", str(list(days)))
check(cal["attribution"] == "exit_day", "attribution 标记为 exit_day")

# ------------------------------------------------------------------
print("\n【2】exit_ts 被 risk_guard 污染时必须用 tp2_hit_ts 还原")
# 真实出场 10-11 23:56，但 exit_ts 被改写成 10-27 14:56（差 15 天）
real_exit = 1_769_000_000 - 3600          # 10-11 23:xx
polluted_exit = 1_769_000_000 + 15 * 86400  # 10-26/27
tr2 = mk(
    ts=1_768_000_000, fill_ts=1_768_000_100, exit_ts=polluted_exit,
    tp2_hit_ts=real_exit, net_usd=89.43, outcome="TP2_DONE",
)
exit_ts, basis = resolve_exit_ts(tr2)
check(basis == "tp2_hit_ts", "优先用 tp2_hit_ts", f"实际 basis={basis}")
check(exit_ts == real_exit, "还原到真实出场时刻", f"实际 {exit_ts} vs {real_exit}")
check(exit_ts_is_suspect(tr2), "识别出 exit_ts 被污染")

cal2 = compute_pnl_calendar([tr2], days=3650)
d2 = {d["date"]: d for d in cal2["days"]}
check(len(d2) == 1, "污染记录只归 1 天", str(list(d2)))
check(cal2["n_exit_ts_repaired"] == 1, "统计到 1 笔被还原", str(cal2.get("n_exit_ts_repaired")))

# ------------------------------------------------------------------
print("\n【3】hold_h 与 exit_ts 一致时必须采信 exit_ts")
tr3 = mk(ts=1_769_000_000, fill_ts=1_769_000_000, exit_ts=1_769_000_000 + 7200,
          hold_h=2.0, net_usd=5.0)
exit_ts3, basis3 = resolve_exit_ts(tr3)
check(basis3 == "exit_ts", "一致时采信 exit_ts", f"实际 {basis3}")
check(not exit_ts_is_suspect(tr3), "一致时不标记为可疑")

# ------------------------------------------------------------------
print("\n【4】未成交的作废单不得进日历")
never = [
    mk(id="a", ts=1_769_000_000, filled=False, net_usd=0.0, exit_px=None, outcome="MISSED"),
    mk(id="b", ts=1_769_000_000, filled=False, net_usd=0.0, exit_px=None, outcome="DUPLICATE_BLOCKED"),
]
cal4 = compute_pnl_calendar(never, days=3650)
check(cal4["days"] == [], "未成交单不进日历", str(cal4["days"]))
check(cal4["total_pnl"] == 0, "总额为 0")

# ------------------------------------------------------------------
print("\n【5】总数守恒：归日口径变化不改变总盈亏")
import json
JOURNAL = Path("/root/data/journal.jsonl")
if JOURNAL.exists():
    recs = [json.loads(l) for l in JOURNAL.read_text(encoding="utf-8").splitlines() if l.strip()]
    filled = [r for r in recs if _is_filled_trade(r) and r.get("net_usd") is not None]
    raw_total = sum(float(r["net_usd"]) for r in filled)
    cal5 = compute_pnl_calendar(recs, days=3650)
    check(abs(cal5["total_pnl"] - raw_total) < 0.01,
          "总盈亏与账本一致", f"日历 {cal5['total_pnl']} vs 账本 {raw_total:.2f}")
    # 每一笔都必须落到某一天，且天数 = 有成交的天数
    per_day = {}
    for r in filled:
        et, _ = resolve_exit_ts(r)
        per_day[et] = per_day.get(et, 0) + float(r["net_usd"])
    n_days_expected = len({__import__("datetime").datetime.fromtimestamp(k).strftime("%Y-%m-%d")
                            for k in per_day})
    check(len(cal5["days"]) == n_days_expected,
          "日历天数 = 有成交的天数", f"{len(cal5['days'])} vs {n_days_expected}")
else:
    print("  ⚠️ 跳过（账本不存在）")

# ------------------------------------------------------------------
print("\n【6】空输入不崩")
for empty in ([], [mk(net_usd=None)], [mk(filled=False)]):
    c = compute_pnl_calendar(empty, days=30)
    check(isinstance(c, dict) and "days" in c, "返回结构完整")

# ------------------------------------------------------------------
print("\n" + "=" * 60)
if FAILS:
    print(f"❌ 失败 {len(FAILS)} 项：")
    for f in FAILS:
        print("   -", f)
    sys.exit(1)
print("✅ 全部通过")
