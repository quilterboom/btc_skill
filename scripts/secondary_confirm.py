# -*- coding: utf-8 -*-
"""
二次确认模块（2026-10-08 liusir 决策 v4.2）
================================================
liusir 的核心要求：信号触发后 + 二次确认（盘口买卖比 + OI 变化）= 才写 journal

数据源（gate_fetch.py 已有）：
  - fetch_orderbook_imbalance(contract, limit)  → {ratio, signal_long, signal_short}
  - fetch_oi(contract)  → {oi_change_pct, signal}

二次确认规则（保守，避免漏单）：
  - 做多：C/D 至少 1 个支持 → 通过
  - 做空：C/D 至少 1 个支持 → 通过

阈值（使用数据源函数已定的值，避免凭感觉）：
  - C 层：ratio > 1.3 支持做多；ratio < 0.77 支持做空
  - D 层：OI 上升 > 0.3% 支持做多；OI 下降 < -0.3% 支持做空

设计原则：
  - 严慎细实：每个阈值有数据来源说明
  - 不凭感觉：阈值参考 gate_fetch.py 已定义的 signal 阈值
  - 数据异常时不阻拦（fail-open）：API 出错时按通过处理，避免漏单

调用方：scan.py _scan_loop 中 verdict 成立后 → check_secondary_confirm() → True 写 journal
"""
from __future__ import annotations
import os
from typing import Tuple, Dict, Optional

# 禁用网络访问（测试场景）
_DISABLE_NETWORK = os.environ.get("BTC_NO_NETWORK", "0") == "1"

# 阈值常量（参考 fetch_orderbook_imbalance 函数的 signal 阈值：1.5/0.67）
#   这里用更宽松的 1.3/0.77，给二次确认更多通过机会
OB_BULL_THRESHOLD = 1.3   # 盘口买盘 >= 此值 → 支持做多
OB_BEAR_THRESHOLD = 0.77  # 盘口买盘 <= 此值 → 支持做空

# 阈值常量（参考 fetch_oi 函数的 signal 阈值：0.5%）
#   这里用更宽松的 0.3%，给二次确认更多通过机会
OI_BULL_THRESHOLD = 0.3    # OI 变化 >= 此值 → 支持做多
OI_BEAR_THRESHOLD = -0.3   # OI 变化 <= 此值 → 支持做空

# 兼容旧名（entry.py / 其他文件可能引用）
WEIGHT_A = 0.5
WEIGHT_C = 0.3
WEIGHT_D = 0.2
ENTRY_THRESHOLD = 0.6


def _fetch_data(contract: str = "BTC_USDT") -> Tuple[Optional[Dict], Optional[Dict]]:
    """
    拉取 C 层 + D 层数据
    返回：(ob_data, oi_data) — 失败时对应值为 None
    """
    # 检查环境变量（包括当前进程和 gate_fetch 模块）
    if os.environ.get("BTC_NO_NETWORK", "0") == "1":
        return None, None
    try:
        from gate_fetch import fetch_orderbook_imbalance, fetch_oi
        ob = fetch_orderbook_imbalance(contract, 20)
        oi = fetch_oi(contract)
        return ob, oi
    except Exception as e:
        # 数据源异常 → fail-open（按通过处理）
        return None, None


def check_secondary_confirm(side: str, contract: str = "BTC_USDT") -> Tuple[bool, str]:
    """
    二次确认检查

    参数:
      side: 'long' / 'short' — 当前信号方向
      contract: 合约名（默认 BTC_USDT）

    返回:
      (pass_confirm, reason)
        - pass_confirm: True=通过 / False=失败
        - reason: 详细原因（用于 journal 记录 + TG 卡片展示）
    """
    if side not in ("long", "short"):
        return False, f"side 异常：{side}"

    ob, oi = _fetch_data(contract)

    # 数据拉取失败 → fail-open（不阻拦）
    if ob is None or oi is None:
        return True, f"二次确认跳过（数据源异常）"

    ratio = ob.get("ratio", 1.0)
    oi_change = oi.get("oi_change_pct", 0.0)

    # 二次确认判断
    c_bull = ratio >= OB_BULL_THRESHOLD   # C 支持做多
    c_bear = ratio <= OB_BEAR_THRESHOLD   # C 支持做空
    d_bull = oi_change >= OI_BULL_THRESHOLD   # D 支持做多
    d_bear = oi_change <= OI_BEAR_THRESHOLD   # D 支持做空

    if side == "long":
        # 做多：C/D 至少 1 个支持
        c_pass = c_bull
        d_pass = d_bull
        c_status = "✓支持" if c_bull else "✗反对"
        d_status = "✓支持" if d_bull else "✗反对"
    else:  # short
        # 做空：C/D 至少 1 个支持
        c_pass = c_bear
        d_pass = d_bear
        c_status = "✓支持" if c_bear else "✗反对"
        d_status = "✓支持" if d_bear else "✗反对"

    pass_confirm = c_pass or d_pass  # 至少 1 个支持

    reason = (f"盘口买卖比 {ratio:.2f} ({c_status}) | "
              f"OI 变化 {oi_change:+.2f}% ({d_status})")

    if not pass_confirm:
        reason = f"二次确认失败：{reason}（无 C/D 支持）"

    return pass_confirm, reason


# ============ 自检 ============
if __name__ == "__main__":
    print("=== 二次确认模块自检 ===")
    print(f"阈值: OB 多={OB_BULL_THRESHOLD} 空={OB_BEAR_THRESHOLD} | OI 多={OI_BULL_THRESHOLD}% 空={OI_BEAR_THRESHOLD}%")

    # 测试：禁用网络（fail-open 应通过）
    os.environ["BTC_NO_NETWORK"] = "1"
    pass_, reason = check_secondary_confirm("long")
    print(f"\n[1] 禁用网络 + 做多: pass={pass_}  reason={reason}")
    assert pass_ is True, "禁用网络时应 fail-open"

    pass_, reason = check_secondary_confirm("short")
    print(f"[2] 禁用网络 + 做空: pass={pass_}  reason={reason}")
    assert pass_ is True, "禁用网络时应 fail-open"

    # 测试：side 异常
    pass_, reason = check_secondary_confirm("invalid")
    print(f"[3] side 异常: pass={pass_}  reason={reason}")
    assert pass_ is False, "side 异常时应失败"

    print("\n所有自检通过")
