#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
edge_score.py — Edge Score v2（参考 LuxAlgo/trade-journal 的开源公式）

公式的 6 个维度（每项 0-100，按权重加权）：
  1. Win rate       (15%)  满分 60%     — 胜率高但容易刷单，权重低
  2. Profit factor  (25%)  满分 3.0     — 盈亏比核心
  3. Avg win/loss   (20%)  满分 2.5:1   — 赢的平均比亏的平均大多少
  4. Drawdown       (15%)  满分 0%      — 回撤越小越好
  5. Recovery       (10%)  满分 3×      — 赚回回撤能力
  6. Consistency    (15%)  满分 15%     — 最大单日盈亏占比不能太大

阈值是校准点（让稳定的盈利账户能拿 60-80 分），不是统计真理。
少于 5 笔不出分（样本太小毫无意义）。

用于 BTC 项目：每个 set（teacher / 生产 / 训练）单独算一个 Edge Score。
"""
from __future__ import annotations
from typing import List, Dict, Optional, Tuple
from dataclasses import dataclass, asdict
from datetime import datetime
import json

# 公式版本号（v2 = gross P&L 求和包含 breakeven 容差为 0 时的 BE）
EDGE_SCORE_VERSION = 2
MIN_TRADES = 5  # 少于 5 笔不出分

# 满分阈值（注意是 0-100 归一化）
THRESHOLDS = {
    "win_rate": 0.60,        # 60% 胜率 → 100 分
    "profit_factor": 3.0,    # PF 3.0 → 100 分
    "avg_win_loss": 2.5,     # 2.5:1 → 100 分
    "max_drawdown_pct": 0.0,  # 0% 回撤 → 100 分；25% 回撤 → 0 分
    "recovery_factor": 3.0,   # net_profit = 3 × max_drawdown → 100 分
    "consistency_pct": 0.15,  # 最大单日盈亏占总盈利 15% → 100 分
}

# 权重（和 = 100）
WEIGHTS = {
    "win_rate": 15,
    "profit_factor": 25,
    "avg_win_loss": 20,
    "max_drawdown_pct": 15,
    "recovery_factor": 10,
    "consistency_pct": 15,
}


@dataclass
class EdgeScore:
    total: float  # 0-100
    version: int
    n_trades: int
    components: Dict[str, float]  # 6 个维度（0-100）
    raw: Dict[str, float]  # 原始数据
    computed_at: str

    def to_dict(self):
        return asdict(self)


def _clamp(v: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, v))


def _normalize_win_rate(wr: float) -> float:
    """胜率 60% → 100 分；0% → 0 分"""
    return _clamp(wr / THRESHOLDS["win_rate"] * 100)


def _normalize_pf(pf: float) -> float:
    """PF 3.0 → 100 分；PF ≤ 0 → 0 分"""
    if pf <= 0:
        return 0.0
    return _clamp(pf / THRESHOLDS["profit_factor"] * 100)


def _normalize_avg_wl(ratio: float) -> float:
    """平均盈亏比 2.5:1 → 100 分"""
    if ratio <= 0:
        return 0.0
    return _clamp(ratio / THRESHOLDS["avg_win_loss"] * 100)


def _normalize_dd(dd_pct: float) -> float:
    """回撤 0% → 100 分；25% → 0 分"""
    if dd_pct <= 0:
        return 100.0
    # 线性插值 0-25%
    return _clamp((1 - dd_pct / 0.25) * 100)


def _normalize_recovery(rf: float) -> float:
    """Recovery factor 3.0 → 100 分"""
    if rf <= 0:
        return 0.0
    return _clamp(rf / THRESHOLDS["recovery_factor"] * 100)


def _normalize_consistency(c_pct: float) -> float:
    """最大单日盈亏占总盈利 15% → 100 分；≥ 50% → 0 分"""
    if c_pct <= THRESHOLDS["consistency_pct"]:
        return 100.0
    # 15% → 100；50% → 0
    return _clamp((1 - (c_pct - THRESHOLDS["consistency_pct"]) / (0.50 - THRESHOLDS["consistency_pct"])) * 100)


def _extract_trades(trades: List[Dict]) -> List[Dict]:
    """从不同来源格式提取统一的 {net_usd, ts, outcome}"""
    out = []
    for t in trades:
        # 1. journal 风格（直接有 net_usd）
        net = t.get("net_usd")
        ts = t.get("ts", 0)
        outcome = t.get("outcome", "")

        # 2. teacher 风格（result 嵌套）
        if net is None and "result" in t and isinstance(t.get("result"), dict):
            r = t["result"]
            net = r.get("net_usd")
            # date 字符串 "2026-10-07 21:50" 转 ts
            if not ts:
                date_str = t.get("date", "")
                if date_str:
                    try:
                        from datetime import datetime
                        ts = int(datetime.strptime(date_str, "%Y-%m-%d %H:%M").timestamp())
                    except Exception:
                        ts = 0
            # outcome
            outcome = r.get("outcome", "")
            # win 标记
            if r.get("win") is True:
                if not outcome or outcome == "":
                    outcome = "TP2_DONE"

        if net is None:
            continue
        out.append({"net_usd": net, "ts": ts, "outcome": outcome})
    return out


def compute_edge_score(trades: List[Dict]) -> Optional[EdgeScore]:
    """
    trades: journal 或 teacher 格式，自动识别
    返回 EdgeScore 或 None（不足 MIN_TRADES 笔不出分）

    过滤规则：
      - 只看已结算且有 net_usd 的
      - 排除 MISSED / NOISE_INVALIDATED / DUPLICATE_BLOCKED
      - BE_STOPPED 算 0（计入分母，PF 分子分母各加 0）
    """
    # 统一格式
    valid = _extract_trades(trades)

    # 过滤
    valid = [t for t in valid
             if t.get("outcome") not in ("MISSED", "NOISE_INVALIDATED", "DUPLICATE_BLOCKED")]

    n = len(valid)
    if n < MIN_TRADES:
        return None

    # 1. Win rate
    wins = [t for t in valid if (t.get("net_usd") or 0) > 0]
    losses = [t for t in valid if (t.get("net_usd") or 0) < 0]
    breakeven = [t for t in valid if (t.get("net_usd") or 0) == 0]
    win_rate = len(wins) / n if n > 0 else 0.0

    # 2. Profit factor
    gross_profit = sum(t["net_usd"] for t in wins)
    gross_loss = -sum(t["net_usd"] for t in losses)  # 转正
    if gross_loss > 0:
        profit_factor = gross_profit / gross_loss
    else:
        # 全胜：PF 无穷大（用一个上限 999 表示）
        profit_factor = 999.0 if gross_profit > 0 else 0.0

    # 3. Avg win / Avg loss
    avg_win = gross_profit / len(wins) if wins else 0.0
    avg_loss = gross_loss / len(losses) if losses else 0.0
    if avg_loss > 0:
        avg_win_loss = avg_win / avg_loss
    else:
        avg_win_loss = 999.0 if avg_win > 0 else 0.0

    # 4. Max drawdown（按时间顺序累计，找最大回撤）
    sorted_trades = sorted(valid, key=lambda x: x.get("ts", 0))
    equity = 0.0
    peak = 0.0
    max_dd = 0.0
    for t in sorted_trades:
        equity += (t.get("net_usd") or 0)
        if equity > peak:
            peak = equity
        dd = peak - equity
        if dd > max_dd:
            max_dd = dd
    # drawdown 用百分比（相对 initial + peak）
    # 这里没有 initial_balance，所以用 peak 作为分母（保守）
    if peak > 0:
        max_dd_pct = max_dd / peak
    else:
        max_dd_pct = 0.0

    # 5. Recovery factor（累计 net / max_drawdown）
    net_profit = sum((t.get("net_usd") or 0) for t in valid)
    if max_dd > 0:
        recovery_factor = net_profit / max_dd
    else:
        recovery_factor = 999.0 if net_profit > 0 else 0.0

    # 6. Consistency（最大单日盈亏占总盈利 %）
    # ★ 2026-10-27：按**真实出场日**归集（原按信号日 ts → 单日盈亏算错，
    #   与 pnl_calendar 同款 bug。共用 resolve_exit_ts 保证两个口径一致）
    by_day = {}
    for t in sorted_trades:
        try:
            from pnl_calendar import resolve_exit_ts
            exit_ts, _ = resolve_exit_ts(t)
        except Exception:
            exit_ts = t.get("ts", 0)
        if not exit_ts:
            exit_ts = t.get("ts", 0)
        day = datetime.fromtimestamp(exit_ts).strftime("%Y-%m-%d") if exit_ts else "unknown"
        by_day[day] = by_day.get(day, 0) + (t.get("net_usd") or 0)
    if by_day and sum(v for v in by_day.values() if v > 0) > 0:
        daily_profits = [v for v in by_day.values() if v > 0]
        max_daily_win = max(daily_profits) if daily_profits else 0.0
        total_profit = gross_profit
        consistency_pct = max_daily_win / total_profit if total_profit > 0 else 1.0
    else:
        consistency_pct = 1.0  # 无盈利日，一致性最差

    # 归一化各分量
    components = {
        "win_rate": _normalize_win_rate(win_rate),
        "profit_factor": _normalize_pf(profit_factor),
        "avg_win_loss": _normalize_avg_wl(avg_win_loss),
        "max_drawdown_pct": _normalize_dd(max_dd_pct),
        "recovery_factor": _normalize_recovery(recovery_factor),
        "consistency_pct": _normalize_consistency(consistency_pct),
    }

    # 加权平均
    total = sum(components[k] * WEIGHTS[k] for k in components) / sum(WEIGHTS.values())

    return EdgeScore(
        total=round(total, 2),
        version=EDGE_SCORE_VERSION,
        n_trades=n,
        components={k: round(v, 2) for k, v in components.items()},
        raw={
            "win_rate": round(win_rate, 4),
            "profit_factor": round(profit_factor, 2),
            "avg_win_loss": round(avg_win_loss, 2),
            "max_drawdown_pct": round(max_dd_pct, 4),
            "recovery_factor": round(recovery_factor, 2),
            "consistency_pct": round(consistency_pct, 4),
            "gross_profit": round(gross_profit, 2),
            "gross_loss": round(gross_loss, 2),
            "net_profit": round(net_profit, 2),
            "n_wins": len(wins),
            "n_losses": len(losses),
            "n_breakeven": len(breakeven),
            "n_days": len(by_day),
        },
        computed_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    )


def format_edge_score(es: EdgeScore, label: str = "") -> str:
    """生成人类可读的 Edge Score 文本"""
    if not es:
        return f"{label} Edge Score: 不足 {MIN_TRADES} 笔，不出分"
    lines = [
        f"**Edge Score v{es.version}**: **{es.total:.2f} / 100** {label}",
        f"  样本: {es.n_trades} 笔",
        f"  6 维分解:",
        f"    胜率:    {es.components['win_rate']:6.2f}  (raw: {es.raw['win_rate']*100:.1f}%)",
        f"    盈亏比:  {es.components['profit_factor']:6.2f}  (raw: PF={es.raw['profit_factor']:.2f})",
        f"    赢/亏:   {es.components['avg_win_loss']:6.2f}  (raw: {es.raw['avg_win_loss']:.2f}:1)",
        f"    回撤:    {es.components['max_drawdown_pct']:6.2f}  (raw: {es.raw['max_drawdown_pct']*100:.1f}%)",
        f"    修复:    {es.components['recovery_factor']:6.2f}  (raw: {es.raw['recovery_factor']:.2f})",
        f"    一致性:  {es.components['consistency_pct']:6.2f}  (raw: {es.raw['consistency_pct']*100:.1f}%)",
    ]
    return "\n".join(lines)
