#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
teacher_engine_fidelity.py —— 保真度门：我的复刻引擎能不能复现老师自己的 20 笔

## 为什么这是必须先做的
btc-strategy-dev 步骤 3：「任何自己写的回放引擎，先用实际 sl/tp1/tp2 复现已结算单，
看复现率」。复现率 < 90% → 引擎只能做直接 K 线统计，不能用它下参数结论。

**如果我的引擎复现不出老师的净值，那后面所有长历史 PnL 数字都是垃圾。**

## 怎么复现
对每笔已结算的老师单：
  - 入场：用老师自己的成交价（不是我的挂单逻辑）
  - 出场：用老师账本里记的 TP1 / TP2 / SL / 减仓比例 / 保本规则
  - 结算：走我的 settle 引擎（同根双触判 SL 的保守口径）
  - 对比：我的净值 vs 老师账本净值

## 只读
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, "/root/skills/btc-gate-intraday-strategy/web")

import teacher_analysis as ta
from teacher_replica import FEE, NOTIONAL, SigGen, load_bars

CST = timezone(timedelta(hours=8))


def num(v):
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    try:
        return float(str(v).strip().replace(",", "").split("-")[0].split("~")[0])
    except (TypeError, ValueError):
        return None


def replay_teacher(sg, tf_min=60, use_bars15=False):
    """用老师的出场参数，在 K 线上重放老师的每一笔。"""
    rows = [json.loads(l) for l in open("/root/data/teacher_trades.jsonl") if l.strip()]
    rows.sort(key=lambda r: r["trade_id"])
    per = {t["trade_id"]: t for t in ta.build_teacher_analysis()["per_trade"]}

    print(f"\n{'='*110}\n【{'15m' if use_bars15 else '1h'} 周期重放】\n{'='*110}")
    print(f"  {'#':<4}{'老师净':<10}{'我算净':<10}{'差':<9}{'出场':<10}{'老师出场':<30}{'判定'}")
    print("  " + "-" * 104)

    ok = bad = skip = 0
    diffs = []
    for r in rows:
        pr = per.get(r["trade_id"], {})
        ts = pr.get("entry_ts")
        if not ts:
            skip += 1
            continue
        res = r.get("result") or {}
        net_teacher = res.get("net_usd")
        if net_teacher is None:
            skip += 1
            continue
        evs = r.get("events") or []
        entry_ev = next((e for e in evs if e.get("type") == "entry"), {})
        px = num(r.get("fill_price")) or num(entry_ev.get("px")) or num(r.get("entry_zone"))
        if not px:
            skip += 1
            continue
        side = r.get("side")
        # 从老师给的出场参数里还原
        tp1 = num(r.get("tp1"))
        tp2 = num(r.get("tp2"))
        sl = num(r.get("sl_initial"))
        tp1ev = next((e for e in evs if e.get("type") in ("tp1_partial", "tp1_hit", "tp1")), None)
        q1_pct = float(tp1ev.get("qty_pct") or 0) if tp1ev else 0.0
        sl_after = num(r.get("sl_after_tp1"))
        # ★ events 里的实际成交价优先（summary 字段与 events 不一致的情况存在）
        if tp1ev and num(tp1ev.get("px")):
            tp1 = num(tp1ev.get("px"))
        tp2ev = next((e for e in evs if e.get("type") in ("tp2_partial", "tp2_full", "tp2_hit")), None)
        if tp2ev and num(tp2ev.get("px")):
            tp2 = num(tp2ev.get("px"))
        # 加仓（#13 这类）：events 里有 add_position → 老师中途加了仓
        addev = next((e for e in evs if e.get("type") == "add_position"), None)
        add_px = num(addev.get("px")) if addev else None
        add_qty_pct = float(addev.get("qty_pct") or 0) if addev else 0.0

        # 定位入场 bar（老师成交时刻）
        i = None
        for k, b in enumerate(sg.bars):
            if int(b[0]) >= ts:
                i = k
                break
        if i is None:
            i = len(sg.bars) - 1
        # 成交那根的 close 作为「成交后」起点
        j = i
        gross = 0.0
        sgn = 1.0 if side == "long" else -1.0   # ★ short 单所有 PnL 取负
        C = max(1, round(NOTIONAL / (px * 1e-4)))
        q1 = int(round(C * q1_pct / 100.0)) if q1_pct else 0
        # 加仓：老师中途加仓 → 均价变成 (px*C + add_px*C_add)/(C+C_add)
        #   （老师 #13 的 qty_pct=50 是「加的量占最终仓位比例」，
        #     这里按『加仓后总量 = 原 C + 加 C*0.5』处理，标注为近似）
        avg_px = px
        if add_px and add_qty_pct:
            add_C = int(round(C * add_qty_pct / 100.0))
            if add_C > 0:
                avg_px = (px * C + add_px * add_C) / (C + add_C)
                C = C + add_C
                q1 = int(round(C * q1_pct / 100.0)) if q1_pct else 0
                px_for_legs = avg_px
            else:
                px_for_legs = px
        else:
            px_for_legs = px
        legs = []
        cur_sl = sl
        cur_qty = C
        tp1_done = False
        exit_reason = None
        exit_px = None
        exit_ts = None
        maxb = min(len(sg.bars), j + 1 + int(72 * 60 / tf_min))
        for k in range(j + 1, maxb):
            h, l = sg.high[k], sg.low[k]
            if side == "long":
                t_sl = (cur_sl is not None and l <= cur_sl)
                t_tp1 = (tp1 and not tp1_done and h >= tp1)
                t_tp2 = (tp2 and h >= tp2)
            else:
                t_sl = (cur_sl is not None and h >= cur_sl)
                t_tp1 = (tp1 and not tp1_done and l <= tp1)
                t_tp2 = (tp2 and l <= tp2)
            if t_sl:
                exit_reason = "BE" if tp1_done else "SL"
                exit_px = cur_sl
                break
            if t_tp1:
                tp1_done = True
                gross += q1 * (tp1 - px_for_legs) * 1e-4 * sgn
                legs.append(("TP1", q1, tp1))
                cur_qty = C - q1
                if sl_after is not None:
                    cur_sl = sl_after
                elif cur_sl is None:
                    cur_sl = px
            if t_tp2:
                exit_reason = "TP2"
                exit_px = tp2
                break
        # ★ 同根双触标记：该笔的出场判定依赖 bar 内先后顺序 → 15m/1h 分辨率无法定序
        ambig = False
        if exit_reason in ("SL", "BE") and tp1 is not None:
            # 找到最后一根：若该根同时触及 TP/TP2 与 SL，就是同根双触
            kk = None
            for k in range(j + 1, maxb):
                h, l = sg.high[k], sg.low[k]
                hit_tp = (h >= tp1) if side == "long" else (l <= tp1)
                if hit_tp and int(sg.bars[k][0]) == int(sg.bars[maxb-1][0]):
                    pass
            kk = maxb - 1
        if exit_reason is None:
            k = maxb - 1
            exit_reason = "TIME"
            exit_px = sg.close[k]
        if exit_reason == "TP2":
            gross += cur_qty * (exit_px - px_for_legs) * 1e-4 * sgn
            legs.append(("TP2", cur_qty, exit_px))
        elif exit_reason in ("SL", "BE"):
            gross += cur_qty * (exit_px - px_for_legs) * 1e-4 * sgn
            legs.append((exit_reason, cur_qty, exit_px))
        else:
            gross += cur_qty * (exit_px - px_for_legs) * 1e-4 * sgn
            legs.append(("TIME", cur_qty, exit_px))
        mine = gross - FEE

        d = mine - net_teacher
        diffs.append(abs(d))
        good = abs(d) < 1.0
        ok += good
        bad += (not good)
        out = (res.get("outcome") or "")[:28]
        print(f"  {r['trade_id']:<4}{net_teacher:<10.2f}{mine:<10.2f}{d:<+9.2f}"
              f"{exit_reason:<10}{out:<30}{'✓' if good else '✗'}")
    n = ok + bad
    print(f"\n  复现率: {ok}/{n} = {ok/n*100:.1f}%" if n else "  无可比样本")
    if diffs:
        diffs_s = sorted(diffs)
        print(f"  绝对差: 均 {sum(diffs)/len(diffs):.2f}U 中位 {diffs_s[len(diffs_s)//2]:.2f}U "
              f"最大 {diffs_s[-1]:.2f}U")
    return ok / n if n else 0


def main():
    print("=" * 110)
    print("保真度门：复刻引擎能否复现老师自己的净值")
    print("=" * 110)
    r1 = replay_teacher(SigGen(load_bars("1h"), 60), 60)
    r2 = replay_teacher(SigGen(load_bars("15m"), 15), 15, use_bars15=True)
    print("\n  判据（btc-strategy-dev 步骤 3）：复现率 ≥ 90% → 可用于参数扫描")
    print(f"    1h 周期复现率 {r1*100:.1f}%   15m 周期复现率 {r2*100:.1f}%")
    if max(r1, r2) < 0.9:
        print("    → ★ 未过保真度门：引擎只能做直接 K 线统计，不能用它下参数结论")
        print("      需先定位差异来源（出场阶梯细节 / 同根双触顺序 / 减仓比例取整）。")


if __name__ == "__main__":
    main()
