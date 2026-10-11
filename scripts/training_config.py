#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
训练环境配置（v5-dev）
=====================

2026-10-11 立项（训练环境架构）

目的：
  训练环境独立运行，模拟开单（不计真实资金），积累数据供月度分析。

核心配置：
  - 独立 journal: /root/data/training/journal.jsonl
  - 独立数据: /root/data/training/
  - 独立策略: v5-dev（多因子加权、ATR SL、Kelly 仓位）
  - 模拟成交: 不计真实资金

跟生产环境隔离：
  - 不影响真实资金
  - 不影响生产 journal
  - 不影响用户体验
  - 数据单独备份到 training_trades/
"""

import os

# 训练环境数据目录
TRAINING_DATA_DIR = '/root/data/training'
TRAINING_JOURNAL = os.path.join(TRAINING_DATA_DIR, 'journal.jsonl')
TRAINING_POSITIONS_DIR = os.path.join(TRAINING_DATA_DIR, 'positions')
TRAINING_ALERTS_DIR = os.path.join(TRAINING_DATA_DIR, 'alerts')
TRAINING_EVENTS_DIR = os.path.join(TRAINING_DATA_DIR, 'events')

# 训练环境模式
TRAINING_MODE = True  # True = 训练环境（模拟开单）, False = 生产环境

# v5-dev 策略配置
V5_DEV_CONFIG = {
    # 多因子加权（改动 1）
    'multi_factor': {
        'w1_1h_score': 1.0,
        'w2_15m_pattern': 0.5,
        'w3_time_of_day': 0.3,
        'w4_vol_regime': 0.2,
        'w5_fib_resonance': 0.3,
        'threshold': 2.0,  # 综合评分阈值
    },
    
    # ATR 自适应 SL（改动 2）
    'atr_sl': {
        'sl_min_pct': 0.004,  # 0.4% 下限
        'sl_max_pct': 0.015,  # 1.5% 上限（从 3.0% 收紧）
        'atr_period': 14,
    },
    
    # Kelly 仓位（改动 3）
    'kelly_position': {
        'kelly_fraction_min': 0.10,
        'kelly_fraction_max': 0.50,
        'confidence_adjustment': 0.5,  # 信心调整系数
    },
    
    # 时段权重自适应（改动 4）
    'time_of_day_weight': {
        'rolling_window_days': 30,
        'weight_min': 0.3,
        'weight_max': 1.0,
    },
    
    # EMA 突破预警全平仓（改动 5）
    'ema_full_close': True,
}

# 模拟成交配置
SIMULATION_CONFIG = {
    'slippage_pct': 0.0005,  # 0.05% 滑点
    'fee_pct': 0.000485,     # 0.0485% 手续费（跟真实一致）
    'initial_balance': 1000.0,  # 初始模拟资金 1000U
}

# 训练环境日志
TRAINING_LOG = os.path.join(TRAINING_DATA_DIR, 'training.log')
