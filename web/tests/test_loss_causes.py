#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
亏损原因分类器口径测试（2026-11-01，训练错误归因卡的数据源）

锁住的口径：
  1. 只归因亏损交易（net>=0 返回 []）
  2. 追单判定的方向归一：long 贴 24h 高点=追高、short 贴 24h 低点=杀跌
     —— 反过来不成立（long 贴低点不是追单）
  3. 逆势判定：long×空头排列 / short×多头排列
  4. 假止损：SL 后 12h 内回触 TP1（K 线不足不判，不猜）
  5. BE_STOPPED 但亏超 -5U 不算「利润回吐」（outcome 不预测 net 符号）
  6. 主因单选互斥：count 之和 == 亏损笔数
  7. K 线窗口锚点纪律：只用 ts 之前的 K 线
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "scripts"))
from mistakes import classify_loss_cause, summarize_loss_causes  # noqa: E402

PASS, FAIL = 0, 0


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✓ {name}")
    else:
        FAIL += 1
        print(f"  ✗ {name} {extra}")


def rec(**kw):
    base = {"id": "t1", "status": "settled", "net_usd": -10.0, "side": "long",
            "entry": 100.0, "exit_px": 99.0, "sl": 99.0, "tp1": 100.5,
            "outcome": "SL_STOPPED", "ts": 100000, "exit_ts": 103600}
    base.update(kw)
    return base


def bars_around(ts, hi=110.0, lo=90.0, n=30, after=None):
    """构造 [ts-24h, ts) 的恒定区间 K 线；after = [(ts_off, h, l)] 止损后的 K 线"""
    out = [(ts - 86400 + i * 1800, 100.0, hi, lo, 100.0, 1) for i in range(n)]
    for off, h, l in (after or []):
        out.append((ts + off, 100.0, h, l, 100.0, 1))
    return sorted(out)


print("【1】只归因亏损")
check("盈利交易不归因", classify_loss_cause(rec(net_usd=+5.0), None) == [])
check("net=None 不归因", classify_loss_cause(rec(net_usd=None), None) == [])
check("net=0 不归因", classify_loss_cause(rec(net_usd=0), None) == [])

print("【2】追单方向归一")
# 24h 区间 [90, 110]，区间宽 20
long_near_high = rec(side="long", entry=109.5, net_usd=-10)     # 距高点 0.5/20=2.5% → 追高
long_near_low = rec(side="long", entry=90.5, net_usd=-10)       # 距低点 2.5% → 抄底，不是追单
short_near_low = rec(side="short", entry=90.5, net_usd=-10)     # 杀跌追空
short_near_high = rec(side="short", entry=109.5, net_usd=-10)   # 不是追单
b = bars_around(100000)
check("long 贴 24h 高点 = 追高", classify_loss_cause(long_near_high, b) == ["CHASE_ENTRY"])
check("long 贴 24h 低点 ≠ 追单", "CHASE_ENTRY" not in classify_loss_cause(long_near_low, b))
check("short 贴 24h 低点 = 杀跌追空", classify_loss_cause(short_near_low, b) == ["CHASE_ENTRY"])
check("short 贴 24h 高点 ≠ 追单", "CHASE_ENTRY" not in classify_loss_cause(short_near_high, b))
check("无 K 线不判追单", classify_loss_cause(long_near_high, None) == ["FULL_STOP"])

print("【3】逆势判定")
check("long×空头排列 = 逆势", classify_loss_cause(
    rec(side="long", tags={"trend": "空头排列"}, net_usd=-10), None) == ["COUNTER_TREND"])
check("short×多头排列 = 逆势", classify_loss_cause(
    rec(side="short", tags={"trend": "多头排列"}, net_usd=-10), None) == ["COUNTER_TREND"])
check("long×多头排列 ≠ 逆势", classify_loss_cause(
    rec(side="long", tags={"trend": "多头排列"}, net_usd=-10), None) == ["FULL_STOP"])

print("【4】假止损（SL 后 12h 回触 TP1）")
# exit_ts = ts+3600；回触 K 线必须严格晚于 exit_ts（止损当根不算回触）
sl_rec = rec(side="long", outcome="SL_STOPPED", tp1=100.5, exit_ts=103600, net_usd=-20)
b_false = bars_around(100000, after=[(7200, 101.0, 99.5)])      # 止损后 1h high 101 ≥ TP1
b_true = bars_around(100000, after=[(7200, 100.0, 95.0)])       # 止损后一路跌，没回 TP1
check("SL 后 1h 回触 TP1 = 假止损", classify_loss_cause(sl_rec, b_false) == ["FALSE_STOP"])
check("SL 后没回 TP1 = 方向错", classify_loss_cause(sl_rec, b_true) == ["FULL_STOP"])
b_late = bars_around(100000, after=[(20 * 3600, 101.0, 99.5)])  # 20h 后才回（超 12h 窗）
check("超 12h 才回 ≠ 假止损", classify_loss_cause(sl_rec, b_late) == ["FULL_STOP"])

print("【5】BE_STOPPED 不看 outcome 看 net")
check("BE 小亏（-3U）= 利润回吐", classify_loss_cause(
    rec(outcome="BE_STOPPED", net_usd=-3.0), None) == ["BE_ROUNDTRIP"])
check("BE 大亏（-22U）≠ 利润回吐（实际打满）", classify_loss_cause(
    rec(outcome="BE_STOPPED", net_usd=-22.0), None) == ["FULL_STOP"])

print("【6】分布互斥加总")
recs = [long_near_high, long_near_low, short_near_low,
        rec(side="long", tags={"trend": "空头排列"}, net_usd=-10),
        rec(outcome="BE_STOPPED", net_usd=-3.0),
        rec(outcome="SL_STOPPED", net_usd=-20, side="long", tp1=100.5, exit_ts=103600),
        rec(net_usd=+8.0)]
res = summarize_loss_causes(recs, b_false)
s = sum(x["count"] for x in res["by_cause"])
check("count 之和 == 亏损笔数", s == res["n_losses"] == 6, f"{s} vs {res['n_losses']}")
check("盈利单不进分布", all(p["net_usd"] < 0 for p in res["per_trade"]))

print("【7】K 线锚点纪律：只用 ts 之前")
b_future = [(200000, 100.0, 110.0, 90.0, 100.0, 1)]   # 全在 ts=100000 之后
check("窗口内 K 线不足 20 根不判", "CHASE_ENTRY" not in classify_loss_cause(long_near_high, b_future))

print(f"\n═══ 结果：{PASS} 通过 / {FAIL} 失败 ═══")
sys.exit(1 if FAIL else 0)
