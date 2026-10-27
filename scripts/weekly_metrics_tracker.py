#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
weekly_metrics_tracker.py — 每周记录 5 objective + Edge Score

每周日 cron 跑一次（跟 weekly_sync 同步），把数字存到 JSONL 文件。
后续 dashboard 可以画趋势线：Edge Score 是不是从 39.95 升到 50+。

数据流：
  weekly_sync.py → 5 objective + Edge Score → weekly_metrics.jsonl
  daily_report.py / 前端 → 读 trend 数据 → 画图
"""
from __future__ import annotations
import sys
import os
import json
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Dict, List, Optional

# 路径
TZ = timezone(timedelta(hours=8))
TRACKER_FILE = Path('/root/data/weekly_metrics.jsonl')


def record_weekly_metrics(week_label: Optional[str] = None) -> Dict:
    """
    记录本周 5 objective + Edge Score 到 TRACKER_FILE

    Returns: dict of all metrics
    """
    sys.path.insert(0, '/root/skills/btc-gate-intraday-strategy/scripts')
    from edge_score import compute_edge_score

    if week_label is None:
        week_label = datetime.now(TZ).strftime('%Y-W%V')  # 例 2026-W42

    # 读生产 journal
    journal_file = Path('/root/data/journal.jsonl')
    trades = []
    if journal_file.exists():
        for line in journal_file.read_text().strip().split('\n'):
            if not line.strip(): continue
            try: trades.append(json.loads(line))
            except: pass

    # 算 5 objective
    # ★ 2026-10-26 改：原来 settled 是混代全量（旧代 33 笔 -95.59U 混在里面），
    #   净利/盈亏比/胜率/回撤四个指标全被旧代拖歪。改成**只算当前代**。
    from era_split import split_by_era
    _groups = split_by_era(trades)
    settled = _groups.get('current_15m') or []
    wins = [t for t in settled if (t.get('net_usd') or 0) > 0]
    losses = [t for t in settled if (t.get('net_usd') or 0) < 0]

    obj_net_profit = sum((t.get('net_usd') or 0) for t in settled)
    gross_profit = sum(t.get('net_usd', 0) for t in wins)
    gross_loss = -sum(t.get('net_usd', 0) for t in losses)
    obj_pf = (gross_profit / gross_loss) if gross_loss > 0 else (999.0 if gross_profit > 0 else 0)
    obj_wr = (len(wins) / len(settled) * 100) if settled else 0

    # Max drawdown
    sorted_settled = sorted(settled, key=lambda x: x.get('ts', 0))
    equity = peak = max_dd = 0.0
    for t in sorted_settled:
        equity += t.get('net_usd', 0)
        if equity > peak: peak = equity
        dd = peak - equity
        if dd > max_dd: max_dd = dd
    obj_dd_pct = (max_dd / peak * 100) if peak > 0 else 0

    # Edge Score
    # ★ 2026-10-26 改：Edge Score 原来算的是**混代**全量（81 笔里含旧代 33 笔
    #   -95.59U），分数被旧代拖低，看不出当前代真实健康度。
    #   现在**只算当前代**（15m 形态信号）—— 这是唯一在跑的策略参数。
    #   旧代历史单独存在 metrics['legacy'] 里备查，不参与主分数。
    from era_split import split_by_era
    _groups = split_by_era(trades)
    _current = _groups.get('current_15m') or []
    es = compute_edge_score(_current)
    es_total = es.total if es else None
    es_components = es.components if es else {}

    # 分代明细（给 dashboard 展示用）
    from era_split import era_summary
    _era_rows = era_summary(trades)

    metrics = {
        'week_label': week_label,
        'ts': int(datetime.now(TZ).timestamp()),
        'ts_str': datetime.now(TZ).strftime('%Y-%m-%d %H:%M:%S'),

        # 5 objective（当前代口径）
        'obj_net_profit': round(obj_net_profit, 2),
        'obj_profit_factor': round(obj_pf, 2),
        'obj_win_rate': round(obj_wr, 1),
        'obj_max_drawdown_pct': round(obj_dd_pct, 1),
        'obj_trade_count': len(settled),

        # Edge Score（当前代口径）
        'edge_score_total': round(es_total, 2) if es_total is not None else None,
        'edge_score_components': es_components,
        'edge_score_n': es.n_trades if es else 0,
        'edge_score_scope': 'current_15m',

        # ★ 分代明细：旧代不再污染主数字，但保留可见
        'era_breakdown': _era_rows,
        'legacy_net_usd': next((r['net_usd'] for r in _era_rows
                                if r['era'] == 'legacy_1h_main'), 0.0),
        'legacy_n': next((r['n'] for r in _era_rows
                          if r['era'] == 'legacy_1h_main'), 0),
    }

    # 写到文件
    TRACKER_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(TRACKER_FILE, 'a', encoding='utf-8') as f:
        f.write(json.dumps(metrics, ensure_ascii=False) + '\n')

    return metrics


def load_history() -> List[Dict]:
    """读所有历史 metrics"""
    if not TRACKER_FILE.exists():
        return []
    history = []
    for line in TRACKER_FILE.read_text().strip().split('\n'):
        if not line.strip(): continue
        try: history.append(json.loads(line))
        except: pass
    return history


def format_trend_report(history: List[Dict]) -> str:
    """生成趋势报告（看上周 vs 上上周）"""
    if not history:
        return "无历史数据"

    lines = ["📈 *5 Objective + Edge Score 趋势*", ""]

    # 表头
    lines.append(f"{'Week':<12} {'PnL':>8} {'PF':>6} {'WR%':>6} {'DD%':>6} {'N':>4} {'ES':>6}")
    lines.append("─" * 56)

    for m in history[-8:]:  # 最近 8 周
        lines.append(
            f"{m.get('week_label', '?'):<12} "
            f"{m.get('obj_net_profit', 0):>+7.1f}U "
            f"{m.get('obj_profit_factor', 0):>6.2f} "
            f"{m.get('obj_win_rate', 0):>5.1f}% "
            f"{m.get('obj_max_drawdown_pct', 0):>5.1f}% "
            f"{m.get('obj_trade_count', 0):>4d} "
            f"{m.get('edge_score_total', 0) or 0:>5.1f}"
        )

    # 趋势判断
    if len(history) >= 2:
        last = history[-1]
        prev = history[-2]
        es_delta = (last.get('edge_score_total') or 0) - (prev.get('edge_score_total') or 0)
        pnl_delta = last.get('obj_net_profit', 0) - prev.get('obj_net_profit', 0)
        lines.append("")
        lines.append(f"周环比: ES {'+' if es_delta>=0 else ''}{es_delta:.2f}  PnL {'+' if pnl_delta>=0 else ''}{pnl_delta:.2f}U")

        if es_delta > 5:
            lines.append("✅ Edge Score 提升明显（改造有效）")
        elif es_delta > 0:
            lines.append("→ Edge Score 微升（继续观察）")
        elif es_delta == 0:
            lines.append("→ Edge Score 持平")
        else:
            lines.append("⚠️ Edge Score 下降（需要分析原因）")

    return '\n'.join(lines)


if __name__ == '__main__':
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument('cmd', choices=['record', 'history', 'trend'])
    args = p.parse_args()

    if args.cmd == 'record':
        m = record_weekly_metrics()
        print(json.dumps(m, ensure_ascii=False, indent=2))
    elif args.cmd == 'history':
        h = load_history()
        print(json.dumps(h, ensure_ascii=False, indent=2))
    elif args.cmd == 'trend':
        h = load_history()
        print(format_trend_report(h))
