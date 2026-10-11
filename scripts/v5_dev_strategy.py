#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
v5-dev 策略（多因子加权 + ATR SL + Kelly 仓位）
==============================================

2026-10-11 立项（训练环境架构）

5 个核心改动：
  1. 多因子加权（替代单一 score 阈值）
  2. ATR 自适应 SL（收紧 clip 3.0% → 1.5%）
  3. Kelly 分数仓位（替代固定 50U 名义）
  4. 时段权重自适应
  5. EMA 突破预警全平仓

用法：
    from v5_dev_strategy import calculate_signal_strength, calculate_position_size

    # 计算综合评分
    score = calculate_signal_strength(
        score_1h=5, pattern_15m='hammer', time_hour=22,
        vol_regime='放量', fib_resonance=True
    )

    # 计算仓位（Kelly）
    position = calculate_position_size(
        win_rate=0.6, avg_win=20, avg_loss=10,
        confidence=score, base_notional=5000
    )
"""
from __future__ import annotations
import os
import sys
from typing import Dict, Tuple

# 训练环境配置
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from training_config import V5_DEV_CONFIG


# ============================ 多因子加权（改动 1）============================
def calculate_signal_strength(
    score_1h: int,
    pattern_15m: str = None,
    time_hour: int = None,
    vol_regime: str = None,
    fib_resonance: bool = False
) -> float:
    """
    计算综合评分（多因子加权）

    输入:
      score_1h: 1h score (0-7)
      pattern_15m: 15m 形态家族（'hammer', 'shooting_star', 'spinning_top', ...）
      time_hour: 当前小时（0-23，用于时段权重）
      vol_regime: 量能状态（'放量', '缩量', '温和'）
      fib_resonance: fib 共振（True/False）

    输出:
      综合评分（float，范围 0-10）
    """
    config = V5_DEV_CONFIG['multi_factor']
    
    # 1h score 加权
    w1_score = config['w1_1h_score'] * score_1h
    
    # 15m 形态加权（形态强度 0-3）
    pattern_strength = get_pattern_strength(pattern_15m)
    w2_pattern = config['w2_15m_pattern'] * pattern_strength
    
    # 时段权重（-1 to +1）
    time_weight = get_time_of_day_weight(time_hour)
    w3_time = config['w3_time_of_day'] * time_weight
    
    # vol regime 加权（-1 to +1）
    vol_weight = get_vol_regime_weight(vol_regime)
    w4_vol = config['w4_vol_regime'] * vol_weight
    
    # fib 共振加权（0/1）
    w5_fib = config['w5_fib_resonance'] * (1 if fib_resonance else 0)
    
    # 综合评分
    total_score = w1_score + w2_pattern + w3_time + w4_vol + w5_fib
    
    return round(total_score, 2)


def get_pattern_strength(pattern: str) -> int:
    """
    形态强度加权（v5 新增）

    形态家族强度：
      锤子 + 次根阳 = 3（强反转）
      流星 + 连续2阴 = 3（强反转）
      蜻蜓十字 = 2（中等反转）
      墓碑十字 = 2（中等反转）
      长腿十字 = 1（弱反转）
      纺锤 = 1（弱反转）
      普通十字 = 0.5（犹豫不决）
    """
    if not pattern:
        return 0
    
    pattern = pattern.lower()
    
    if 'hammer' in pattern and 'dragonfly' not in pattern:
        return 3  # 锤子
    if 'shooting_star' in pattern and 'gravestone' not in pattern:
        return 3  # 流星
    if 'dragonfly' in pattern:
        return 2  # 蜻蜓十字
    if 'gravestone' in pattern:
        return 2  # 墓碑十字
    if 'long_legged' in pattern:
        return 1  # 长腿十字
    if 'spinning_top' in pattern:
        return 1  # 纺锤
    if 'doji' in pattern:
        return 1  # 普通十字
    
    return 0


def get_time_of_day_weight(hour: int) -> float:
    """
    时段权重（-1 to +1）

    基于历史时段表现（rolling 30 天）
    现状（2026-10-11 数据）：
      00-04:00 赚钱时段（累计 +58U）→ +0.5
      05-23:00 亏钱时段（累计 -153U）→ -0.5
      22:00 死亡时段（-51U）→ -1.0
    """
    if hour is None:
        return 0.0
    
    # 简化版（实际应该用 rolling 30 天数据）
    if 0 <= hour <= 4:
        return 0.5  # 赚钱时段
    elif hour == 22:
        return -1.0  # 死亡时段
    elif 5 <= hour <= 23:
        return -0.5  # 亏钱时段
    
    return 0.0


def get_vol_regime_weight(vol: str) -> float:
    """
    vol regime 加权（-1 to +1）

    现状（2026-10-11 数据）：
      温和: 67% 胜率，+23U → +0.5
      放量: 42% 胜率，-48U → -0.3
      缩量: 33% 胜率，-65U → -0.5
    """
    if not vol:
        return 0.0
    
    vol = vol.lower()
    
    if '温和' in vol:
        return 0.5
    elif '放量' in vol:
        return -0.3
    elif '缩量' in vol:
        return -0.5
    
    return 0.0


# ============================ ATR 自适应 SL（改动 2）============================
def calculate_atr_sl(
    entry: float,
    side: str,
    atr: float,
    current_sl_pct: float = 0.008
) -> Tuple[float, float]:
    """
    ATR 自适应 SL（v5 改动 2）

    跟 v4.2 区别：
      - v4.2: sl_pct = clip(..., 0.004, 0.030)  # 0.4%-3.0% 区间太宽
      - v5:   sl_pct = clip(..., 0.004, 0.015)  # 0.4%-1.5% 区间收紧

    输入:
      entry: 入场价
      side: 'long' 或 'short'
      atr: ATR（平均真实波幅，百分比或点数）
      current_sl_pct: 当前 SL 百分比（可选，默认 0.8%）

    输出:
      (sl_price, sl_pct)
    """
    config = V5_DEV_CONFIG['atr_sl']
    
    # ATR 自适应：波动率大时 SL 自动放宽
    # atr 作为百分比（例如 0.008 = 0.8%）
    atr_pct = atr if atr < 1 else atr / entry  # 归一化到百分比
    
    # SL 距离 = max(0.30 * ATR, 最小 SL)
    sl_distance_pct = max(0.30 * atr_pct, config['sl_min_pct'])
    
    # clip 到 [min, max] 区间
    sl_pct = max(config['sl_min_pct'], min(sl_distance_pct, config['sl_max_pct']))
    
    # 计算 SL 价格
    if side == 'long':
        sl_price = entry * (1 - sl_pct)
    else:
        sl_price = entry * (1 + sl_pct)
    
    return sl_price, sl_pct


# ============================ Kelly 分数仓位（改动 3）============================
def calculate_position_size(
    win_rate: float,
    avg_win: float,
    avg_loss: float,
    confidence: float,
    base_notional: float = 5000.0
) -> Dict:
    """
    Kelly 分数仓位（v5 改动 3）

    跟 v4.2 区别：
      - v4.2: notional = 50U * 100x = 5000U（固定）
      - v5:   notional = base_notional * Kelly_fraction（按信心动态调）

    输入:
      win_rate: 胜率（0-1）
      avg_win: 平均盈利（U）
      avg_loss: 平均亏损（U，正数）
      confidence: 综合评分（0-10）
      base_notional: 基础名义（默认 5000U）

    输出:
      Dict: {
        'kelly_fraction': Kelly 分数（0.10-0.50）,
        'confidence_adjustment': 信心调整系数（0.5-1.0）,
        'final_fraction': 最终仓位比例,
        'notional': 最终名义（U）,
      }
    """
    config = V5_DEV_CONFIG['kelly_position']
    
    # Kelly 公式: f* = (p * b - q) / b
    # p = win_rate, q = 1 - win_rate, b = avg_win / avg_loss
    if avg_loss == 0:
        kelly = 0.5  # 默认 50%
    else:
        b = avg_win / avg_loss
        p = win_rate
        q = 1 - win_rate
        kelly = (p * b - q) / b if b > 0 else 0
    
    # clip 到 [min, max] 区间
    kelly = max(config['kelly_fraction_min'], min(kelly, config['kelly_fraction_max']))
    
    # 信心调整系数（0.5-1.0）
    # confidence 范围 0-10，归一化到 0-1，然后映射到 0.5-1.0
    confidence_norm = min(confidence / 10, 1.0)
    confidence_adj = config['confidence_adjustment'] + (1 - config['confidence_adjustment']) * confidence_norm
    
    # 最终仓位 = Kelly * 信心调整
    final_fraction = kelly * confidence_adj
    
    # 最终名义
    notional = base_notional * final_fraction
    
    return {
        'kelly_fraction': round(kelly, 4),
        'confidence_adjustment': round(confidence_adj, 4),
        'final_fraction': round(final_fraction, 4),
        'notional': round(notional, 2),
    }


# ============================ 测试 ============================
if __name__ == '__main__':
    # 测试多因子加权
    score = calculate_signal_strength(
        score_1h=5,
        pattern_15m='hammer',
        time_hour=22,
        vol_regime='放量',
        fib_resonance=True
    )
    print(f'综合评分: {score}')
    
    # 测试 ATR SL
    sl_price, sl_pct = calculate_atr_sl(
        entry=83000,
        side='long',
        atr=0.008,  # 0.8% ATR
    )
    print(f'ATR SL: {sl_price:,.1f} ({sl_pct*100:.2f}%)')
    
    # 测试 Kelly 仓位
    position = calculate_position_size(
        win_rate=0.6,
        avg_win=20,
        avg_loss=10,
        confidence=score,
    )
    print(f'Kelly 仓位: {position}')
