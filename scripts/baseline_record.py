#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
baseline_record.py — 记录改造前 baseline（一次性，2026-10-17）

drop_forced + SL clip 改造落地时间：2026-10-17 10:30
改造前 settled 笔数：40 笔（10-02 到 10-15）
改造后 settled 笔数：0 笔（待数据积累）

记录为"pre-drop_forced" baseline，等改造后 30+ 笔再做对比。
"""
import sys
import os
import json
from datetime import datetime, timezone, timedelta
from pathlib import Path

TZ = timezone(timedelta(hours=8))
BASELINE_FILE = Path('/root/data/metrics_baselines.jsonl')

sys.path.insert(0, '/root/skills/btc-gate-intraday-strategy/scripts')
from edge_score import compute_edge_score


def record_baseline(label: str, notes: str = "") -> dict:
    """
    记录一次 baseline（含 5 objective + Edge Score）
    label: 例 "pre-drop_forced" / "pre-tp1_floor"
    """
    journal_file = Path('/root/data/journal.jsonl')
    trades = []
    if journal_file.exists():
        for line in journal_file.read_text().strip().split('\n'):
            if not line.strip(): continue
            try: trades.append(json.loads(line))
            except: pass

    settled = [t for t in trades if t.get('status') == 'settled']
    wins = [t for t in settled if (t.get('net_usd') or 0) > 0]
    losses = [t for t in settled if (t.get('net_usd') or 0) < 0]

    obj_net_profit = sum((t.get('net_usd') or 0) for t in settled)
    gross_profit = sum(t.get('net_usd', 0) for t in wins)
    gross_loss = -sum(t.get('net_usd', 0) for t in losses)
    obj_pf = (gross_profit / gross_loss) if gross_loss > 0 else (999.0 if gross_profit > 0 else 0)
    obj_wr = (len(wins) / len(settled) * 100) if settled else 0

    sorted_settled = sorted(settled, key=lambda x: x.get('ts', 0))
    equity = peak = max_dd = 0.0
    for t in sorted_settled:
        equity += t.get('net_usd', 0)
        if equity > peak: peak = equity
        dd = peak - equity
        if dd > max_dd: max_dd = dd
    obj_dd_pct = (max_dd / peak * 100) if peak > 0 else 0

    es = compute_edge_score(trades)

    rec = {
        'label': label,
        'notes': notes,
        'ts': int(datetime.now(TZ).timestamp()),
        'ts_str': datetime.now(TZ).strftime('%Y-%m-%d %H:%M:%S'),
        'obj_net_profit': round(obj_net_profit, 2),
        'obj_profit_factor': round(obj_pf, 2),
        'obj_win_rate': round(obj_wr, 1),
        'obj_max_drawdown_pct': round(obj_dd_pct, 1),
        'obj_trade_count': len(settled),
        'edge_score_total': round(es.total, 2) if es else None,
        'edge_score_n': es.n_trades if es else 0,
    }

    BASELINE_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(BASELINE_FILE, 'a', encoding='utf-8') as f:
        f.write(json.dumps(rec, ensure_ascii=False) + '\n')

    return rec


def list_baselines():
    if not BASELINE_FILE.exists():
        return []
    out = []
    for line in BASELINE_FILE.read_text().strip().split('\n'):
        if not line.strip(): continue
        try: out.append(json.loads(line))
        except: pass
    return out


if __name__ == '__main__':
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument('cmd', choices=['record', 'list'])
    p.add_argument('--label', default='pre-drop_forced')
    p.add_argument('--notes', default='')
    args = p.parse_args()

    if args.cmd == 'record':
        r = record_baseline(args.label, args.notes)
        print(json.dumps(r, ensure_ascii=False, indent=2))
    else:
        baselines = list_baselines()
        print(f"共 {len(baselines)} 个 baseline:")
        for b in baselines:
            print(f"  [{b['label']}] {b['ts_str']}  ES={b.get('edge_score_total')}  N={b['obj_trade_count']}  PnL={b['obj_net_profit']}U")
            if b.get('notes'):
                print(f"    备注: {b['notes']}")
