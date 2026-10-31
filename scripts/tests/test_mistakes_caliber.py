#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_mistakes_caliber.py — 错误归因口径回归测试（2026-10-31）

背景：生产的「错误归因」卡把 BE_STOPPED（保本被打回）整体打成
「TP1 后没推保本」，实测 36 笔占 50.0%，是归因第一名 —— 但这 36 笔的
active_sl 全部 == entry（保本确实推了），合计净 +185.82。方向整个反了。
同理 SL_STOPPED（按止损价成交 = 纪律执行）被标成「抗单不止损」。

本测试锁死三条：
  1. BE_STOPPED（active_sl == entry）**不得**被判成 NO_BREAKEVEN
  2. SL_STOPPED（exit_px == sl）**不得**被判成 ANTI_STOP
  3. 分母 = 已结算可信交易数，不含 MISSED / invalidated 行

跑法：python3 scripts/tests/test_mistakes_caliber.py
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "web"))

from mistakes import classify_mistake, summarize_mistakes, _is_trade  # noqa: E402

FAILS = []


def check(cond, msg):
    if cond:
        print(f"  ✓ {msg}")
    else:
        print(f"  ✗ {msg}")
        FAILS.append(msg)


def rec(**kw):
    base = {"id": "TEST-1", "status": "settled", "net_usd": 0.0, "outcome": "BE_STOPPED"}
    base.update(kw)
    return base


print("=== 1. BE_STOPPED（保本已推）不得判成 NO_BREAKEVEN ===")
r = rec(entry=100.0, active_sl=100.0, tp1_hit=True, net_usd=-0.85)
ms = classify_mistake(r)
check("NO_BREAKEVEN" not in ms, f"保本已推(active_sl==entry) 不判 NO_BREAKEVEN（实得 {ms}）")

print("=== 2. 真·没推保本（tp1_hit 但 active_sl != entry）要判出来 ===")
r = rec(entry=100.0, active_sl=97.0, tp1_hit=True, net_usd=-12.0)
ms = classify_mistake(r)
check("NO_BREAKEVEN" in ms, f"tp1_hit 且 active_sl!=entry 判 NO_BREAKEVEN（实得 {ms}）")

print("=== 3. SL_STOPPED（按止损价成交=纪律执行）不得判成 ANTI_STOP ===")
r = rec(outcome="SL_STOPPED", side="long", entry=100.0, sl=98.0, exit_px=98.0,
        active_sl=98.0, net_usd=-20.0)
ms = classify_mistake(r)
check("ANTI_STOP" not in ms, f"exit_px==sl 不判 ANTI_STOP（实得 {ms}）")

print("=== 4. 真·抗单（出场价远离止损价）要判出来 ===")
r = rec(outcome="SL_STOPPED", side="long", entry=100.0, sl=99.0, exit_px=97.0,
        active_sl=99.0, net_usd=-31.7)
ms = classify_mistake(r)
check("ANTI_STOP" in ms, f"出场价远离止损价判 ANTI_STOP（实得 {ms}）")

print("=== 5. 分母只算已结算可信交易，不含 MISSED / invalidated ===")
rows = [
    rec(id="A", net_usd=1.0),
    rec(id="B", net_usd=2.0),
    rec(id="C", outcome="MISSED", status="settled", net_usd=None),
    rec(id="D", status="invalidated", outcome=None, net_usd=3.0),
    rec(id="E", status="settled", net_usd=0.0, outcome="NOISE_INVALIDATED"),
]
s = summarize_mistakes(rows)
check(s["trade_count"] == 2, f"trade_count == 2（实得 {s['trade_count']}）")
check(s["not_filled"] == 1, f"not_filled == 1（实得 {s['not_filled']}）")
check(s["total_net"] == 3.0, f"total_net == 3.0（实得 {s['total_net']}）")

print("=== 6. PLAN_DRIFT 只在 |tp1| < sl 时触发 ===")
check("PLAN_DRIFT" not in classify_mistake(
    rec(sl_pct=0.4, tp1_pct=0.48)), "rr>1 不判 PLAN_DRIFT")
check("PLAN_DRIFT" in classify_mistake(
    rec(sl_pct=0.8, tp1_pct=0.24)), "rr<1 判 PLAN_DRIFT")

print("=== 7. 手动 mistake 字段优先于自动判定 ===")
r = rec(entry=100.0, active_sl=100.0, tp1_hit=True, mistake=["CHASING"])
check(classify_mistake(r) == ["CHASING"], f"手动标记优先（实得 {classify_mistake(r)}）")

print("=== 8. 真实生产账本：不得再出现「保本已推」被判 NO_BREAKEVEN ===")
prod = Path("/root/data/journal.jsonl")
if prod.exists():
    recs = [json.loads(l) for l in prod.read_text(encoding="utf-8").splitlines() if l.strip()]
    s = summarize_mistakes(recs)
    print(f"    分母 {s['trade_count']} 笔 · 有问题 {s['total_with_mistake']} 笔 · "
          f"未成交 {s['not_filled']} 笔")
    for m in s["by_type"]:
        print(f"      {m['label']:16s} {m['count']:3d} 笔 ({m['pct']}%)  净值 {m['net_usd']:+.2f}U")
    check(s["trade_count"] > 0, "生产账本有可统计成交单")
    nb = next((m for m in s["by_type"] if m["type"] == "NO_BREAKEVEN"), None)
    check(nb is None or nb["count"] == 0,
          f"生产真实账本里「TP1后没推保本」应为 0 笔（实得 {nb['count'] if nb else 0}）")
    check(len(s["by_type"]) == 0 or all("net_usd" in m for m in s["by_type"]),
          "每类都带净值字段")
else:
    print("  (跳过：生产账本不存在)")

print()
if FAILS:
    print(f"❌ {len(FAILS)} 项失败")
    for f in FAILS:
        print(f"   - {f}")
    sys.exit(1)
print("✅ 全部通过")
