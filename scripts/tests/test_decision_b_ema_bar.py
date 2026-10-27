#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
回归：训练 journal 的 BTC_USDT-1792594829-long
=============================================
这笔在 2026-10-22 16:53 撞了 sanity check 抛错（决策 A 时代的最后一笔）：

    exit_ts=03:15 (EMA 预警那根) < tp1_hit_ts=04:42
    → ValueError → settle_all 静默吞掉 → 卡 pending

**决策 B 修复后**（EMA 触发那根不判 SL，从下一根起判）：
    03:15 EMA 推保本 84333（已平 70% = 415 张 @ 84362.5）
    03:16 low=84358.4 > 84333 → 不止损
    03:17 low=84397.5 > 84333 → 不止损
    03:18 low=84336.1 > 84333 → 不止损
    03:19 low=84319.1 <= 84333 → BE_STOPPED @ 84333
    期望 outcome=BE_STOPPED / BE_STOPPED 类，不再抛错
"""
import sys, os, time, json, inspect
sys.path.insert(0, '/root/skills/btc-gate-intraday-strategy/scripts')

import position_tracker as pt
import gate_fetch

FAILS = []
TRAIN_JOURNAL = '/root/data/training/journal.jsonl'


def check(name, cond, detail=""):
    print(f"  {'✅' if cond else '❌'} {name}" + (f"  — {detail}" if detail else ""))
    if not cond:
        FAILS.append(name)


def main():
    bars = sorted(gate_fetch.fetch_ohlcv("BTC_USDT", "1m", 8000, cache=True,
                                         refresh=False, verbose=False),
                  key=lambda b: b[0])

    recs = [json.loads(l) for l in open(TRAIN_JOURNAL) if l.strip()]
    orig = [r for r in recs if '1792594829' in str(r.get('id', ''))][0]

    # 复现当时的卡死状态
    sim = dict(orig)
    sim['status'] = 'pending'
    sim['outcome'] = 'TP1_PARTIAL'
    sim.pop('net_usd', None)
    # 去掉已结算后落的字段，模拟「当时」
    for k in ('hold_h', 'category', 'R', 'net_R', 'mae', 'mfe', 'note'):
        sim.pop(k, None)

    print("\n=== 决策 B 下重放（当时卡死的那笔）===")
    try:
        res = pt._settle_one(sim, bars)
        check("不再抛 sanity check 异常", True)
    except Exception as e:
        check("不再抛 sanity check 异常", False, f"{type(e).__name__}: {e}")
        res = None

    if res:
        et = res.get('exit_ts')
        et_s = time.strftime('%m-%d %H:%M', time.localtime(et)) if et else '?'
        check("结算成功返回", True, f"outcome={res.get('outcome')}")
        check("出场不在 EMA 触发那根（03:15）", et_s != '10-22 03:15', f"got {et_s}")
        check("出场在 TP1 之后（BE 晚于所有保本移动）", et_s >= '10-22 04:42', f"got {et_s}")
        check("outcome 是保本/止损系",
              res.get('outcome') in ('BE_STOPPED', 'SL_STOPPED'),
              f"got {res.get('outcome')}")
        net = res.get('net_usd')
        check("net_usd 不为负（EMA 已平 70% 在保本上方）",
              net is not None and net > 0, f"got {net}")
    else:
        check("结算成功返回", False, "返回 None")

    # 决策 B 代码层断言
    print("\n=== 决策 B 代码断言 ===")
    src = inspect.getsource(pt._settle_one)
    check("_skip_sl_this_bar 标志存在", '_skip_sl_this_bar' in src)
    check("EMA 触发时置位", '_skip_sl_this_bar = True' in src)
    check("sanity check 认 ema_warning_ts", 'ema_warning_ts' in inspect.getsource(pt._close))

    # 训练 journal 当前状态
    print("\n=== 训练 journal 现状 ===")
    t = [r for r in recs if '1792594829' in str(r.get('id', ''))][0]
    check("已 settled（不再卡 pending）", t.get('status') == 'settled',
          f"status={t.get('status')}")
    check("settle_error_count 有留痕", t.get('settle_error_count') is not None,
          f"count={t.get('settle_error_count')}")
    # 卡死判定：正常活跃持仓（status=pending + filled=True + net_usd=None）
    # 不是卡死 —— 它就是当前还开着的仓位。真正的卡死是「settle 抛异常后
    # status 卡在 pending 或 settled 但 net_usd 永远算不出来」。
    stuck = [r for r in recs
             if r.get('status') == 'settled' and r.get('net_usd') is None]
    stuck2 = [r for r in recs
              if r.get('status') == 'pending' and r.get('settle_error_count') is not None]
    check("训练 journal 无卡死记录", len(stuck) == 0 and len(stuck2) == 0,
          f"settled但无net={len(stuck)}, pending且有错={len(stuck2)}")
    live = [r for r in recs if r.get('status') == 'pending'
            and r.get('settle_error_count') is None]
    if live:
        print(f"  ℹ️ 当前有 {len(live)} 笔正常活跃持仓（非卡死）: "
              + ", ".join(str(r.get('id')) for r in live))

    print("\n" + "=" * 62)
    if FAILS:
        print(f"❌ {len(FAILS)} 项失败：")
        for f in FAILS:
            print(f"   - {f}")
        return 1
    print("✅ 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())