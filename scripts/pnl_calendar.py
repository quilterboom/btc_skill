#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
pnl_calendar.py — 每日盈亏日历（参考 LuxAlgo/trade-journal）

输出：
  - 每日盈亏 (含 0 = 当日没单 / BE_STOPPED)
  - 最大单日盈利 / 最大单日亏损
  - 红/绿日计数
  - 月度汇总

用于 BTC 项目前端渲染红/绿日历格子。
"""
from __future__ import annotations
from typing import List, Dict, Optional
from datetime import datetime, timedelta
from collections import defaultdict
import json


# ★ 2026-10-24：从未成交的 outcome（这些单不是交易，净利必须是 0）
NEVER_FILLED_OUTCOMES = {
    None, "MISSED", "DUPLICATE_BLOCKED", "NOISE_INVALIDATED",
    "INVALIDATED", "JUMP_INVALIDATED", "SKIPPED",
}


def _is_filled_trade(t: Dict) -> bool:
    """
    ★ 2026-10-24：判断这一条是不是"真实成交的交易"。

    背景：10-23 的 backfill_fixes.py 给 19 笔**从未成交**的作废挂单
    按满仓算成 −5000U/笔（exit_px=0 → gross=(0-entry)×595×0.0001），
    虚增亏损 −95084U，把生产 P&L 日历显示成 −95107.5U。
    数据已修正（net_usd 归零），但这些单仍会以 net=0 混进日历，
    撑出假的"平局日"和虚高的 n_trades。

    判定顺序：
      1. 有明确的 filled 字段 → 直接采信（True=成交，False/None=没成交）
      2. 没有 filled 字段的老单 → 用「有 exit_px 且 outcome 不在
         never-filled 集合」兜底
    """
    if "filled" in t:
        return bool(t.get("filled"))
    if t.get("outcome") in NEVER_FILLED_OUTCOMES:
        return False
    return t.get("exit_px") is not None


# ★ 2026-10-27：exit_ts 被 risk_guard 污染的识别阈值（小时）
_EXIT_TS_SUSPECT_HOURS = 12


def resolve_exit_ts(t: Dict) -> tuple:
    """
    ★ 2026-10-27：还原这一笔的**真实出场时间**（用于按出场日归集盈亏）。

    背景（两连坑）：
      坑 1 —— 日历原来按 `ts`（信号发出时刻）归日，但盈亏是**出场时**产生的。
              一笔 10-11 深夜开的单可能 10-12 凌晨平仓，按信号日归日
              会让「当天盈亏」既不是当天亏的也不是当天赚的。
              实测 10 笔跨日单让 10 天的数字全错（10-19 显示 +85.07，
              实际那天 0 笔成交；10-11 显示 +178.86，实际 2 笔但
              exit_ts 被污染到 10-27）。

      坑 2 —— `exit_ts` 本身不可信。risk_guard 在 settler 卡死时会
              反复重写持仓的 exit_px/exit_ts（note 实证「重复触发
              26267 次」），把 exit_ts 写成「重写那一刻」的市价时间。
              受影响的 4 笔 exit_ts 全是 10-27 14:56/14:57，exit_px
              ≈ 79618（当时市价），而真实出场在 10-11 23:56 / 10-22。

    还原优先级（越靠前越可信）：
      1. `tp2_hit_ts`  —— 止盈二档触发时刻，最精确
      2. `exit_ts`     —— 但只在它与 `fill_ts + hold_h` 推算值一致
                          （差 ≤ 2h）时才采信，否则视为被污染
      3. `tp1_hit_ts`  —— 保本/减仓触发时刻（下限）
      4. `fill_ts + hold_h` —— 用持仓时长反推

    返回 (exit_ts, 依据标签)
    """
    ft = t.get("fill_ts")
    et = t.get("exit_ts")
    tp2 = t.get("tp2_hit_ts")
    tp1 = t.get("tp1_hit_ts")
    hh = t.get("hold_h")

    # 1. 止盈二档触发时刻 —— 最精确
    if tp2 and ft and tp2 >= ft:
        return int(tp2), "tp2_hit_ts"

    if hh and ft:
        est = int(ft) + int(round(float(hh) * 3600))
        # 2. exit_ts 与 hold_h 推算一致 → exit_ts 可信
        if et and abs(int(et) - est) <= 2 * 3600:
            return int(et), "exit_ts"
        # 3. 退到 tp1（保本触发，不早于它）
        if tp1 and tp1 >= ft:
            return int(max(tp1, est)), "tp1+hold_h"
        # 4. hold_h 反推
        return est, "hold_h推算"

    if et:
        return int(et), "exit_ts"
    if tp1:
        return int(tp1), "tp1_hit_ts"
    if ft:
        return int(ft), "fill_ts"
    return 0, "无"


def exit_ts_is_suspect(t: Dict) -> bool:
    """exit_ts 与其它时间戳严重矛盾 → 标记为被 risk_guard 污染。"""
    et = t.get("exit_ts")
    if not et:
        return False
    for k in ("tp2_hit_ts", "tp1_hit_ts", "last_check_ts"):
        v = t.get(k)
        if v and int(et) - int(v) > _EXIT_TS_SUSPECT_HOURS * 3600:
            return True
    return False


def compute_pnl_calendar(trades: List[Dict], days: int = 90) -> Dict:
    """
    trades: [{net_usd, ts}, ...]
    days: 看最近 N 天
    返回: {
      "days": [{"date": "2026-10-17", "pnl": 12.5, "n_trades": 3, "wins": 2}, ...],
      "max_win_day": {"date": ..., "pnl": ...},
      "max_loss_day": {"date": ..., "pnl": ...},
      "n_green": int, "n_red": int, "n_flat": int,
      "total_pnl": float,
      "monthly": [{"month": "2026-10", "pnl": ..., "n_trades": ..., "n_wins": ...}]
    }
    """
    if not trades:
        return {"days": [], "monthly": [], "n_green": 0, "n_red": 0, "n_flat": 0,
                "total_pnl": 0, "n_exit_ts_repaired": 0, "attribution": "exit_day"}

    # 按日聚合
    # ★ 2026-10-27：按**真实出场日**归集（原按信号日 ts 归日 → 每日金额全错）
    by_day = defaultdict(lambda: {"pnl": 0.0, "n_trades": 0, "wins": 0, "losses": 0, "breakeven": 0})
    n_suspect = 0
    for t in trades:
        net = t.get("net_usd")
        ts = t.get("ts", 0)
        if net is None or not ts:
            continue
        # ★ 2026-10-24：只统计**真实成交**的单。
        #   从未成交的挂单（status=invalidated / outcome=DUPLICATE_BLOCKED /
        #   NOISE_INVALIDATED 等）虽然 net_usd=0，但它们不是"交易"，
        #   混进来会撑出假的"平局日"和虚高的 n_trades
        #   （实测 10-03 显示 12 笔，实际只有 2 笔真交易）。
        #   判定：filled=True 才算成交；老单没有 filled 字段时用
        #        「有 exit_px + outcome 不在 never-filled 集合」兜底。
        if not _is_filled_trade(t):
            continue
        # ★ 2026-10-27：还原真实出场时刻（exit_ts 可能被 risk_guard 污染）
        exit_ts, _basis = resolve_exit_ts(t)
        if exit_ts_is_suspect(t):
            n_suspect += 1
        if not exit_ts:
            exit_ts = ts
        day = datetime.fromtimestamp(exit_ts).strftime("%Y-%m-%d")
        d = by_day[day]
        d["pnl"] += net
        d["n_trades"] += 1
        if net > 0:
            d["wins"] += 1
        elif net < 0:
            d["losses"] += 1
        else:
            d["breakeven"] += 1

    if not by_day:
        return {"days": [], "monthly": [], "n_green": 0, "n_red": 0, "n_flat": 0, "total_pnl": 0}

    # 日期范围（按最近 N 天）
    today = datetime.now()
    start_date = today - timedelta(days=days)

    # 排序
    sorted_days = sorted(by_day.items())
    sorted_days = [(d, v) for d, v in sorted_days if datetime.strptime(d, "%Y-%m-%d") >= start_date]

    # 月度汇总
    by_month = defaultdict(lambda: {"pnl": 0.0, "n_trades": 0, "wins": 0})
    for day, v in sorted_days:
        m = day[:7]  # YYYY-MM
        by_month[m]["pnl"] += v["pnl"]
        by_month[m]["n_trades"] += v["n_trades"]
        by_month[m]["wins"] += v["wins"]

    # 最大单日
    max_win_day = max(({"date": d, "pnl": v["pnl"]} for d, v in sorted_days if v["pnl"] > 0),
                      key=lambda x: x["pnl"], default=None)
    max_loss_day = min(({"date": d, "pnl": v["pnl"]} for d, v in sorted_days if v["pnl"] < 0),
                       key=lambda x: x["pnl"], default=None)

    # 红/绿/平
    n_green = sum(1 for _, v in sorted_days if v["pnl"] > 0)
    n_red = sum(1 for _, v in sorted_days if v["pnl"] < 0)
    n_flat = sum(1 for _, v in sorted_days if v["pnl"] == 0)

    days_list = [{"date": d, **v} for d, v in sorted_days]
    monthly_list = [{"month": m, **v} for m, v in sorted(by_month.items())]

    return {
        "days": days_list,
        "monthly": monthly_list,
        "max_win_day": max_win_day,
        "max_loss_day": max_loss_day,
        "n_green": n_green,
        "n_red": n_red,
        "n_flat": n_flat,
        "total_pnl": round(sum(v["pnl"] for _, v in sorted_days), 2),
        # ★ 2026-10-27：exit_ts 被 risk_guard 污染、已用其它时间戳还原的笔数
        "n_exit_ts_repaired": n_suspect,
        # 口径说明：按**真实出场日**归集（不是信号日）
        "attribution": "exit_day",
    }


def format_pnl_calendar(cal: Dict, label: str = "") -> str:
    """生成人类可读的 P&L 日历文本"""
    if not cal or not cal.get("days"):
        return f"{label} P&L 日历: 无数据"
    lines = [
        f"**P&L 日历** {label}（最近 {len(cal['days'])} 个交易日）",
        f"  总盈亏: **{cal['total_pnl']:.2f}U**  红 {cal['n_red']} / 绿 {cal['n_green']} / 平 {cal['n_flat']}",
    ]
    if cal.get("max_win_day"):
        lines.append(f"  最大单日盈利: {cal['max_win_day']['pnl']:.2f}U ({cal['max_win_day']['date']})")
    if cal.get("max_loss_day"):
        lines.append(f"  最大单日亏损: {cal['max_loss_day']['pnl']:.2f}U ({cal['max_loss_day']['date']})")
    lines.append(f"  月度:")
    for m in cal.get("monthly", []):
        wins = m.get("wins", 0)
        n = m.get("n_trades", 0)
        wr = (wins / n * 100) if n > 0 else 0
        pnl = m.get("pnl", 0)
        sign = "+" if pnl >= 0 else ""
        lines.append(f"    {m['month']}: {sign}{pnl:.2f}U  ({n} 笔, 胜率 {wr:.1f}%)")
    return "\n".join(lines)
