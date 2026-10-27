#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
回归测试：settler 回放「按 ts 恢复状态」修复
================================================

**Bug（2026-10-22 实测）**：BTC_USDT-1792594815-long
- journal 已写 tp1_hit_ts=10-22 04:42、active_sl=84333（保本）
- 回放起点 ts=10-21 23:00（早于真实 TP1 触发）
- 老代码循环外读一次状态 → 03:01 那根 low=84215 就被判 BE_STOPPED
- exit_ts(03:01) < tp1_hit_ts(04:42) → sanity check 抛 ValueError
- settle_all 的 `except: continue` 静默吞掉 → 这笔永远 pending
- risk_guard 接管，每秒写 exit_px 但不改 status → 刷 26267 次
- 结果：真实应在 07:05 保本止损（净 +3.12U），实际挂 9.6h 显示 -138.91U

**修复后应得**：BE_STOPPED @ 07:05，exit_px=84333，净 +3.12U
"""
import sys, os, time, json, inspect
sys.path.insert(0, '/root/skills/btc-gate-intraday-strategy/scripts')

import position_tracker as pt
import gate_fetch

FAILS = []


def check(name, cond, detail=""):
    print(f"  {'✅' if cond else '❌'} {name}" + (f"  — {detail}" if detail else ""))
    if not cond:
        FAILS.append(name)


def main():
    bars = sorted(gate_fetch.fetch_ohlcv("BTC_USDT", "1m", 8000, cache=True,
                                         refresh=False, verbose=False),
                  key=lambda b: b[0])

    # ---------- Case 1: 真实卡死单（回放到原状） ----------
    print("\n=== Case 1: BTC_USDT-1792594815-long（真实卡死单）===")
    recs = pt._read_journal()
    target = [r for r in recs if '1792594815' in str(r.get('id', ''))][0]
    # 还原成当时的状态（已被我手动结算了，改回 pending + 脏 exit_px）
    sim = dict(target)
    sim['status'] = 'pending'
    sim['outcome'] = 'TP1_PARTIAL'
    sim['pos_state'] = 'TP1_PARTIAL'
    sim['exit_ts'] = 1792658554
    sim['exit_px'] = 81937.8
    sim.pop('net_usd', None)
    sim.pop('settle_error', None)
    sim.pop('settle_error_count', None)

    try:
        res = pt._settle_one(sim, bars)
    except Exception as e:
        check("不再抛 sanity check 异常", False, f"{type(e).__name__}: {e}")
        res = None

    if res:
        et = res.get('exit_ts')
        et_s = time.strftime('%m-%d %H:%M', time.localtime(et)) if et else '?'
        check("结算成功返回", True, f"outcome={res.get('outcome')}")
        # 真实事件链：03:15 EMA 突破预警推保本（active_sl=84333），
        # 同一根 K 线影线 low=84308.7 ≤ 84333 → BE 止损。
        # （TP1 是 04:42 才触发，但那已经晚了 —— EMA 路径先动。）
        check("outcome 是 BE/SL 系（不是 TP2）",
              res.get('outcome') in ('BE_STOPPED', 'SL_STOPPED'),
              f"got {res.get('outcome')}")
        # 说明：这笔是**历史单**，EMA 预警的触发时刻字段（ema_warning_ts）
        # 是 2026-10-22 才加的，历史 journal 里没有 → 回放只能走 TP1 保本路径
        # → 结算在 07:05（TP1 04:42 推保本后，07:05 low=84275 跌破）。
        # 新单（有 ema_warning_ts）会走 03:15 的 EMA 路径，见 Case 3。
        check("exit_ts == 10-22 07:05（历史单走 TP1 保本路径）",
              et_s == '10-22 07:05', f"got {et_s}")
        check("exit_px <= 84333（保本价，无亏损出场）",
              float(res.get('exit_px')) <= 84333.0 + 0.1,
              f"got {res.get('exit_px')}")
        check("net_usd 不为负（保本/微赚，不是 -138.91U）",
              float(res.get('net_usd')) > -1.0,
              f"got {res.get('net_usd')}")
        check("net_usd 在合理区间（3~6U：TP1 段 +7.97 扣 4.85 fee）",
              2.0 < float(res.get('net_usd')) < 6.0,
              f"got {res.get('net_usd')}")
    else:
        check("结算成功返回", False, "返回 None（仍持仓）")

    # ---------- Case 2: 未来状态不能影响更早的 K 线 ----------
    print("\n=== Case 2: 未来状态不污染更早 K 线 ===")
    sim2 = dict(sim)
    # 假设 journal 说 TP1 在 04:42 触发、保本 84333
    sim2['tp1_hit'] = True
    sim2['tp1_hit_ts'] = 1792614600     # 10-22 04:42
    sim2['active_sl'] = 84333.0
    sim2['sl'] = 83972.6
    sim2['filled'] = True
    sim2['fill_ts'] = 1792609200        # 10-22 03:00
    sim2['ts'] = 1792594815             # 10-21 23:00
    sim2['_orig_contracts'] = 593
    sim2['contracts'] = 396
    sim2['partial_exits'] = 1
    sim2['tp1_hit_px'] = 84737.8

    res2 = pt._settle_one(sim2, bars)
    if res2:
        et2 = time.strftime('%m-%d %H:%M', time.localtime(res2.get('exit_ts', 0)))
        # 关键断言：03:01 那根 low=84215 低于保本 84333，
        # 但 TP1 是 04:42 才触发 → 03:01 时保本还没移动，
        # 那根该用原 SL 83972.6 判定（不触发）→ 不能被判 BE_STOPPED
        check("03:01 未被误判为 BE_STOPPED（exit_ts 不是 03:01）",
              et2 != '10-22 03:01', f"got {et2}")
        check("结算时刻不早于 TP1 触发时刻 04:42",
              et2 >= '10-22 04:42', f"got {et2}")
    else:
        check("结算返回", False, "None")

    # ---------- Case 3: 正常 TP1 路径（无未来状态）不受影响 ----------
    print("\n=== Case 3: 正常路径回归（TP1 在回放中自然触发）===")
    sim3 = {
        'id': 'TEST-normal-tp1', 'ts': 1792594815, 'side': 'long',
        'entry': 84333.0, 'sl': 83972.6, 'tp1': 84737.8, 'tp2': 85193.2,
        'filled': True, 'fill_ts': 1792609200, 'fill_px': 84333.0,
        'contracts': 593, '_orig_contracts': 593,
        'status': 'pending', 'pos_state': 'OPEN',
    }
    res3 = pt._settle_one(sim3, bars)
    if res3:
        et3 = time.strftime('%m-%d %H:%M', time.localtime(res3.get('exit_ts', 0)))
        check("自然回放能结算（TP1 或 EMA 路径任一）",
              res3.get('outcome') in ('BE_STOPPED', 'SL_STOPPED',
                                     'TP1_PARTIAL', 'TP2_DONE'),
              f"outcome={res3.get('outcome')} @ {et3}")
        check("不再抛异常（sanity check 通过）", True)
    else:
        check("自然回放返回", False, "None")

    # ---------- Case 5: 决策 B —— EMA 预警那根不判 SL ----------
    print("\n=== Case 5: 决策 B（EMA 触发那根 K 线不判止损）===")
    src = inspect.getsource(pt._settle_one)
    check("_skip_sl_this_bar 存在", '_skip_sl_this_bar' in src)
    check("EMA 触发时置位", '_skip_sl_this_bar = True' in src)
    check("SL 判定前被清零", 'if _skip_sl_this_bar:\n            t_sl = False' in src)
    # 用 Case 3 的自然回放验证：EMA 在 03:15 触发，那根不该止损
    sim5 = {
        'id': 'TEST-decision-B', 'ts': 1792594815, 'side': 'long',
        'entry': 84333.0, 'sl': 83972.6, 'tp1': 84737.8, 'tp2': 85193.2,
        'filled': True, 'fill_ts': 1792609200, 'fill_px': 84333.0,
        'contracts': 593, '_orig_contracts': 593,
        'status': 'pending', 'pos_state': 'OPEN',
    }
    res5 = pt._settle_one(sim5, bars)
    if res5:
        et5 = time.strftime('%m-%d %H:%M', time.localtime(res5.get('exit_ts', 0)))
        # 决策 A 时是 03:15 同根止损；决策 B 应该往后走到真正的止损点
        check("EMA 触发那根（03:15）不再止损", et5 != '10-22 03:15', f"got {et5}")
        check("出场时刻在 EMA 触发之后", et5 > '10-22 03:15', f"got {et5}")
    else:
        check("决策 B 回放返回", False, "None")

    # ---------- Case 4: settle_all 不再静默吞异常 ----------
    print("\n=== Case 4: settle_all 异常上报 ===")
    src = inspect.getsource(pt.settle_all)
    check("settle_all 记录 settle_error 字段", 'settle_error' in src)
    check("settle_all 写 settle_errors.log", 'settle_errors.log' in src)
    check("settle_all 不再是裸 except: continue",
          'settle_error_count' in src)

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
