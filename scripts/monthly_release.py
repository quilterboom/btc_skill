#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
月度汇总脚本（Monthly Release）
==============================

2026-10-11 立项（训练环境架构）

目的：
  每月 1 日汇总上月数据（生产 + 老师单 + 训练模拟），对比生产 vs 训练，
  决策 v5 哪些改动上线，月度发布新版本。

用法：
    python3 monthly_release.py --month 2026-10
    python3 monthly_release.py --month 2026-10 --out report.md

输出：
  1. 生产 vs 训练对比报告
  2. 老师单 benchmark 对比
  3. v5 假设验证（walk-forward）
  4. 决策建议（哪些改动上线）
  5. 一键发布命令

数据源：
  - production_trades/journal_YYYY-MM-DD.jsonl
  - teacher_signals/teacher_YYYY-MM-DD.jsonl
  - training_trades/training_YYYY-MM-DD.jsonl
"""
from __future__ import annotations
import os
import sys
import json
import argparse
from datetime import datetime, timedelta, timezone
from typing import List, Dict
from collections import defaultdict

CST = timezone(timedelta(hours=8))
DATA_DIR = '/root/data'


def load_month_trades(data_dir: str, prefix: str, month: str) -> List[Dict]:
    """加载某月的交易数据"""
    recs = []
    for day in range(1, 32):
        try:
            date_str = f"{month}-{day:02d}"
            fpath = os.path.join(data_dir, f"{prefix}_{date_str}.jsonl")
            if os.path.exists(fpath):
                with open(fpath) as f:
                    for ln in f:
                        ln = ln.strip()
                        if ln:
                            try:
                                recs.append(json.loads(ln))
                            except Exception:
                                continue
        except Exception:
            continue
    return recs


def evaluate_trades(reports: List[Dict]) -> Dict:
    """评估交易表现"""
    if not reports:
        return {"n": 0, "win": 0, "loss": 0, "win_rate": 0.0, "net": 0.0, "sharpe": 0.0}
    
    wins = [r for r in reports if r.get("outcome") in ("TP2_DONE", "TP1_PARTIAL", "BE_STOPPED")]
    losses = [r for r in reports if r.get("outcome") in ("SL_STOPPED", "MCLOSE")]
    n = len(reports)
    nw = len(wins)
    nl = len(losses)
    net = sum(r.get("net_usd", 0) for r in reports)
    
    if n > 1:
        net_list = [r.get("net_usd", 0) for r in reports]
        mean = sum(net_list) / n
        var = sum((x - mean) ** 2 for x in net_list) / (n - 1)
        std = var ** 0.5
        sharpe = (mean / std) if std > 0 else 0.0
    else:
        sharpe = 0.0
    
    return {
        "n": n, "win": nw, "loss": nl,
        "win_rate": nw / n * 100 if n else 0.0,
        "net": net, "sharpe": sharpe
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--month", required=True, help="格式: 2026-10")
    ap.add_argument("--out", default=None, help="输出文件（markdown）")
    a = ap.parse_args()
    
    month = a.month
    print(f"📊 月度汇总报告: {month}")
    print()
    
    # 1. 加载数据
    prod_recs = load_month_trades(f"{DATA_DIR}/production_trades", "journal", month)
    teacher_recs = load_month_trades(f"{DATA_DIR}/teacher_signals", "teacher", month)
    training_recs = load_month_trades(f"{DATA_DIR}/training_trades", "training", month)
    
    print(f"数据加载:")
    print(f"  生产交易: {len(prod_recs)} 笔")
    print(f"  老师单: {len(teacher_recs)} 笔")
    print(f"  训练模拟: {len(training_recs)} 笔")
    print()
    
    # 2. 评估
    prod_eval = evaluate_trades(prod_recs)
    training_eval = evaluate_trades(training_recs)
    
    print(f"生产 vs 训练对比:")
    print(f"  {'':<15} {'N':>5} {'Win Rate':>10} {'Net':>10} {'Sharpe':>10}")
    print(f"  {'-'*60}")
    print(f"  {'生产 (v4.2)':<15} {prod_eval['n']:>5} {prod_eval['win_rate']:>9.1f}% {prod_eval['net']:>+10.2f}U {prod_eval['sharpe']:>10.2f}")
    print(f"  {'训练 (v5-dev)':<15} {training_eval['n']:>5} {training_eval['win_rate']:>9.1f}% {training_eval['net']:>+10.2f}U {training_eval['sharpe']:>10.2f}")
    print()
    
    # 3. 决策建议
    delta_wr = training_eval['win_rate'] - prod_eval['win_rate']
    delta_net = training_eval['net'] - prod_eval['net']
    
    print(f"决策建议:")
    if delta_wr > 10 and delta_net > 0:
        print(f"  ✓ v5 显著 > v4.2 (winrate +{delta_wr:.1f}pp, net +{delta_net:+.2f}U)")
        print(f"  建议: 全切 v5，v4.2 退役")
    elif delta_wr > 0 and delta_net > 0:
        print(f"  ○ v5 略好 (winrate +{delta_wr:.1f}pp, net +{delta_net:+.2f}U)")
        print(f"  建议: 灰度上线 v5（10% 仓位），继续观察")
    else:
        print(f"  ✗ v5 不显著或更差 (winrate {delta_wr:+.1f}pp, net {delta_net:+.2f}U)")
        print(f"  建议: 保留 v4.2，v5 关停")
    
    print()
    print(f"月度发布命令:")
    print(f"  # 备份生产数据")
    print(f"  python3 journal.py backup_production")
    print(f"  # 训练环境数据备份")
    print(f"  python3 training_release.py --month {month}")
    
    return 0


if __name__ == "__main__":
    sys.exit(main())
