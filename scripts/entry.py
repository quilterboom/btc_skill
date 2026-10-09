# -*- coding: utf-8 -*-
"""
综合入场流程（2026-10-08 liusir 决策）
====================================
liusir 拍板的方向：A + C + D 综合
- A：多信号组合（核心：CORE + EMA144）
- C：订单流（盘口买卖比 / 大单检测）
- D：资金费率 + OI

设计原则（基于今晚信号验证结果）：
- 6 个老信号单独全部负期望
- 57 个组合里只有 CORE+EMA144 正期望（+0.022%/笔，5.3 次/月）
- 必须叠加订单流 + 市场结构才有真 alpha

设计状态：骨架完成，未经验证（明天用 333 天真实 K 线跑）

模块结构：
1. signal_layer_a()  - A 层：技术信号组合
2. signal_layer_c()  - C 层：订单流（盘口买卖比）
3. signal_layer_d()  - D 层：资金费率 + OI
4. evaluate_entry()  - 综合评分 → 是否进场
5. score_position()  - 综合打分函数（liusir 拍板的"综合"含义）

注意：本文件骨架未验证，**不能在生产用**。明天验证流程：
1. 拉 333 天 1h K 线（已有）
2. 模拟 CORE+EMA144 完整入场出场
3. 加 C + D 维度，看能否提升胜率
4. 跑样本外（最近 30 天）验证
"""
from __future__ import annotations
import numpy as np
import pandas as pd
from typing import Dict, Optional, Tuple

# ============ 权重（liusir 拍板后填）============
WEIGHT_A = 0.5   # A 层权重（技术信号组合）
WEIGHT_C = 0.3   # C 层权重（订单流）
WEIGHT_D = 0.2   # D 层权重（市场结构）
ENTRY_THRESHOLD = 0.6  # 综合评分 >= 0.6 才进场（liusir 拍板）


def signal_layer_a(df: pd.DataFrame) -> Dict[str, bool]:
    """
    A 层：技术信号组合
    验证结果：CORE + EMA144 是 57 个组合里唯一正期望的（+0.022%/笔）
    其他组合（TD9、RSI、VOL）单独全部负期望，应剔除

    返回：dict of signal_name -> bool
    """
    # TODO: 复用 scan.py 的 CORE 计算（10 根窗口回踩确认）
    # TODO: 复用 4h EMA144 > close 判断
    # 占位实现
    return {
        'CORE_OK': False,        # ① 反转确认（10 根窗口）
        'EMA144_OK': False,      # ② 4h 价 > 4h EMA144
    }


def signal_layer_c(df: pd.DataFrame, ob_data: Optional[dict] = None) -> Dict[str, float]:
    """
    C 层：订单流
    - 盘口买卖比（前 20 档挂单量对比）
    - 大单检测（需要 fetcher，本文件未实现）
    - 主动买卖比（taker 主动买入 vs 主动卖出）

    ob_data: 从 Gate.io /futures/usdt/order_book 拉的原始数据
    返回：dict of signal_name -> 0.0-1.0 score
    """
    if ob_data is None:
        return {'OB_BULL': 0.0, 'OB_BEAR': 0.0}
    # 占位实现
    long_signal = ob_data.get('signal_long', 0)
    short_signal = ob_data.get('signal_short', 0)
    return {
        'OB_BULL': float(long_signal),
        'OB_BEAR': float(short_signal),
    }


def signal_layer_d(df: pd.DataFrame, lsr_data: Optional[dict] = None,
                    oi_data: Optional[dict] = None) -> Dict[str, float]:
    """
    D 层：市场结构
    - 多空比（反指逻辑：极端值反转）
    - OI 持仓量变化
    - 资金费率

    lsr_data: Gate.io /futures/usdt/contract_stats（多空比）
    oi_data: Gate.io /futures/usdt/contract_stats（OI 变化）
    返回：dict of signal_name -> 0.0-1.0 score
    """
    return {
        'LSR_BULL': (lsr_data or {}).get('signal_long', 0.0),
        'LSR_BEAR': (lsr_data or {}).get('signal_short', 0.0),
        'OI_RISING': float((oi_data or {}).get('signal', 0)),
    }


def score_position(layer_a: Dict, layer_c: Dict, layer_d: Dict) -> Tuple[float, str]:
    """
    综合评分（liusir 拍板的"综合分析"）
    返回：(score, side)
      - score: 0.0-1.0 综合分数
      - side: 'long' / 'short' / 'none'
    """
    # A 层：CORE + EMA144 必须都成立（不然直接 0）
    if not (layer_a.get('CORE_OK') and layer_a.get('EMA144_OK')):
        return 0.0, 'none'

    # A 层基础分（必须项）
    score_a = 1.0

    # C 层：盘口买卖比加分（多 = 加分，空 = 减分）
    ob_bull = layer_c.get('OB_BULL', 0.0)
    ob_bear = layer_c.get('OB_BEAR', 0.0)
    score_c = ob_bull - ob_bear  # -1 到 1

    # D 层：多空比 + OI
    lsr_bull = layer_d.get('LSR_BULL', 0.0)
    lsr_bear = layer_d.get('LSR_BEAR', 0.0)
    oi = layer_d.get('OI_RISING', 0.0)
    score_d = lsr_bull - lsr_bear + oi * 0.3

    # 综合
    total = (WEIGHT_A * score_a
             + WEIGHT_C * (score_c + 1) / 2  # 归一化到 0-1
             + WEIGHT_D * max(0, min(1, (score_d + 1) / 2)))

    # 方向判断
    if score_c + score_d > 0:
        side = 'long'
    elif score_c + score_d < 0:
        side = 'short'
    else:
        side = 'none'

    return total, side


def evaluate_entry(layer_a: Dict, layer_c: Dict, layer_d: Dict) -> Tuple[bool, str, float]:
    """
    综合入场判定
    返回：(should_enter, side, score)
      - should_enter: True/False
      - side: 'long'/'short'/'none'
      - score: 综合分数
    """
    score, side = score_position(layer_a, layer_c, layer_d)
    should_enter = score >= ENTRY_THRESHOLD and side != 'none'
    return should_enter, side, score


# ============ 自检 ============
if __name__ == "__main__":
    # 模拟：所有信号都成立
    layer_a = {'CORE_OK': True, 'EMA144_OK': True}
    layer_c = {'OB_BULL': 1.0, 'OB_BEAR': 0.0}
    layer_d = {'LSR_BULL': 1.0, 'LSR_BEAR': 0.0, 'OI_RISING': 1.0}

    should, side, score = evaluate_entry(layer_a, layer_c, layer_d)
    print(f"=== 综合入场自检 ===")
    print(f"A 层: {layer_a}")
    print(f"C 层: {layer_c}")
    print(f"D 层: {layer_d}")
    print(f"综合评分: {score:.3f}（阈值 {ENTRY_THRESHOLD}）")
    print(f"方向: {side}")
    print(f"应进场: {should}")
