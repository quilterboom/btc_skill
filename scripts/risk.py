# -*- coding: utf-8 -*-
"""
风控模块（2026-10-08 liusir 决策）
=================================
liusir 拍板的风控参数（第二轮纠正后）：
- 本金：50U（不动）
- 杠杆：100x（不动）
- 单笔保证金：50U = 100% 本金（满仓，liusir 明确）
- 同时持仓：1 条
- 账户回撤 -30% 硬止损（全停）

约束逻辑（liusir 决策）：
- 不在仓位上保守——满仓进场（信任入场信号）
- 靠 -30% 回撤兜住极端风险（账户剩 35U 全停）
- 单笔潜在亏损可达 -50U 量级，但回撤硬止损兜底

实测数据（5.5 天 45 条样本，50U 保证金 + 100x）：
- 单笔最大实证亏损 -44.85U（1791209345-long），接近爆仓
- 单笔 ≤ 2U 约束违反率 48%（12 条违反）
- 同时持仓最高 5 条
- 最大回撤 -48.7%（已超 -30% 阈值，意味着回撤约束有效触发）
"""
from __future__ import annotations
import json
import os
from pathlib import Path
from typing import Tuple

# 风控常量（liusir 拍板 2026-10-08）
ACCOUNT_BALANCE_USD = 50.0     # 本金 50U（liusir: 保持不变）
MARGIN_PER_TRADE_USD = 50.0    # 单笔保证金 50U = 100% 本金（liusir 明确）
RISK_PER_TRADE_PCT = 1.0       # 单笔保证金占本金 100%（满仓）
MAX_DRAWDOWN_PCT = 0.30        # 账户回撤 30% 全停（liusir 拍板）
MAX_CONCURRENT_POSITIONS = 1   # 同时持仓上限 1 条
LEVERAGE = 100                 # 杠杆 100x（liusir: 不动）

# 数据路径
DATA_DIR = "/root/data"
JOURNAL = os.path.join(DATA_DIR, "journal.jsonl")
EVENTS = os.path.join(DATA_DIR, "position_events.jsonl")


def compute_account_state() -> Tuple[float, float, float]:
    """
    计算当前账户状态
    :return: (current_balance, peak_balance, current_drawdown_pct)
      - current_balance: 当前余额（本金 + 累计已结算净利）
      - peak_balance: 期间峰值
      - current_drawdown_pct: 当前回撤（0.0 - 1.0）
    """
    initial = ACCOUNT_BALANCE_USD
    if not Path(JOURNAL).exists():
        return initial, initial, 0.0

    settled_net = 0.0
    peak = initial
    current = initial

    with open(JOURNAL) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                s = json.loads(line)
            except json.JSONDecodeError:
                continue
            if s.get("outcome") not in ("SL_STOPPED", "BE_STOPPED",
                                        "TP1_DONE", "TP2_DONE"):
                continue
            net = s.get("net_usd") or 0.0
            settled_net += net
            current = initial + settled_net
            peak = max(peak, current)

    if peak <= 0:
        return current, peak, 1.0
    dd = (peak - current) / peak if peak > 0 else 0.0
    return current, peak, dd


def check_drawdown_halt() -> Tuple[bool, str]:
    """
    检查账户回撤是否触发硬止损
    :return: (should_halt, reason)
    """
    current, peak, dd = compute_account_state()
    if dd >= MAX_DRAWDOWN_PCT:
        return True, (f"账户回撤 {dd*100:.1f}% >= {MAX_DRAWDOWN_PCT*100:.0f}%，"
                      f"触发硬止损（峰值 {peak:.2f}U → 当前 {current:.2f}U）")
    return False, ""


def count_active_positions() -> int:
    """计算当前 pending/filled 未结算的单子数"""
    if not Path(JOURNAL).exists():
        return 0
    n = 0
    with open(JOURNAL) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                s = json.loads(line)
            except json.JSONDecodeError:
                continue
            if s.get("status") == "pending":
                n += 1
    return n


def check_can_open_new() -> Tuple[bool, str]:
    """
    检查是否可以开新仓
    :return: (can_open, reason)
    """
    halt, reason = check_drawdown_halt()
    if halt:
        return False, reason

    n_active = count_active_positions()
    if n_active >= MAX_CONCURRENT_POSITIONS:
        return False, (f"同时持仓已达上限 {MAX_CONCURRENT_POSITIONS} 条"
                      f"（当前 {n_active} 条）")

    return True, ""


def get_position_margin() -> float:
    """单笔保证金（liusir 拍板 50U = 100% 本金，满仓）"""
    return MARGIN_PER_TRADE_USD


if __name__ == "__main__":
    current, peak, dd = compute_account_state()
    print(f"当前账户: {current:.2f}U  峰值: {peak:.2f}U  回撤: {dd*100:.1f}%")
    halt, reason = check_drawdown_halt()
    print(f"回撤硬止损: {'已触发' if halt else '未触发'} {reason}")
    n = count_active_positions()
    print(f"当前 pending 单: {n} 条")
    can_open, reason = check_can_open_new()
    print(f"可以开新仓: {can_open} {reason}")
    print(f"\n单笔保证金: {get_position_margin()}U（{RISK_PER_TRADE_PCT*100:.0f}% 本金）")
    print(f"杠杆: {LEVERAGE}x → 名义 {MARGIN_PER_TRADE_USD * LEVERAGE}U")
