#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
通用回测工具: scripts/backtest.py
用法: python3 backtest.py --param sl_clip=0.001 --param tp1_floor=0.005 [--output json]

输入: journal.jsonl (filled trades)
处理: 对每笔 trade, 用 pos_state + fill_px + sl_pct/tp1_pct 反推价格路径
      然后用 --param 替换 SL/TP 距离, 模拟"如果参数是这样, 这笔 trade 怎么走"

输出: 模拟 PnL / 胜率 / EV / PF / R:R / Edge Score

约束:
  - 只对 pos_state in (SL_STOPPED, BE_STOPPED, TP2_DONE) 做模拟 (这些有明确成交)
  - MISSED / NOISE_INVALIDATED 视为"没成交" (signal 但没被 fill 或无效)
  - 假设: 价格路径 = fill_px -> 触发位置 -> 离开
"""
import sys
import os
import json
import statistics
import argparse
from pathlib import Path
from collections import Counter
from typing import List, Dict, Optional

def load_trades(journal_path: str) -> List[Dict]:
    """加载 journal 里的 filled trades"""
    trades = []
    p = Path(journal_path)
    if not p.exists():
        return trades
    for line in p.read_text().strip().split('\n'):
        if not line: continue
        try:
            j = json.loads(line)
            if j.get('filled') and j.get('entry') and j.get('sl'):
                trades.append(j)
        except: pass
    return trades

def simulate_trade(trade: Dict, sl_pct: float, tp1_pct: float, tp2_pct: Optional[float] = None,
                    quant_mult: float = 0.0001) -> Dict:
    """
    模拟单笔 trade, 用新 SL/TP 距离
    输入 trade 的 pos_state, 推断价格路径
    然后用新 SL/TP 重算 outcome

    PnL 公式 (跟 position_tracker._pnl_usd 一致):
      pnl = sign × (exit_px - entry) × contracts × QUANT_MULT
      其中 sign=1 (long) or -1 (short)
      不需要 × leverage (这是 USD 净 PnL, notional 已经是 entry × contracts × QUANT_MULT)

    price_path 推断 (基于 pos_state):
      - SL_STOPPED: 价格最大反向距离 = old_sl_usd
      - BE_STOPPED: 价格先到 TP1 (推到保本) 然后回到 entry
      - TP2_DONE: 价格走完 TP1 + TP2
      - TP1_DONE: 价格只到 TP1
    """
    fill_px = trade.get('fill_px') or trade.get('entry')
    side = trade.get('side')
    pos_state = trade.get('pos_state', '')
    contracts = trade.get('contracts', 1)

    # 原始 SL/TP 距离 (USD)
    if side == 'long':
        old_sl_usd = fill_px - trade['sl']
        old_tp1_usd = trade['tp1'] - fill_px
        old_tp2_usd = (trade.get('tp2', trade['tp1']) - fill_px)
    else:
        old_sl_usd = trade['sl'] - fill_px
        old_tp1_usd = fill_px - trade['tp1']
        old_tp2_usd = (fill_px - trade.get('tp2', trade['tp1']))

    # 新 SL/TP 距离 (USD)
    new_sl_usd = fill_px * sl_pct
    new_tp1_usd = fill_px * tp1_pct
    new_tp2_usd = fill_px * tp2_pct if tp2_pct else (new_tp1_usd * 1.5)

    # 推断价格路径
    tp1_hit = False
    tp2_hit = False
    max_reverse_usd = 0
    max_forward_usd = 0

    if pos_state == 'SL_STOPPED':
        # 价格最大反向到 SL
        max_reverse_usd = old_sl_usd
        max_forward_usd = 0
    elif pos_state == 'BE_STOPPED':
        # 价格先到 TP1 (推保本), 然后回到 entry
        tp1_hit = True
        max_reverse_usd = 0  # 没反向到原 SL (因保本后 SL = entry)
        max_forward_usd = old_tp1_usd  # 至少走到 TP1
    elif pos_state == 'TP2_DONE':
        tp1_hit = True
        tp2_hit = True
        max_reverse_usd = 0
        max_forward_usd = old_tp2_usd  # 走到 TP2
    elif pos_state == 'TP1_DONE':
        tp1_hit = True
        max_reverse_usd = 0
        max_forward_usd = old_tp1_usd
    else:
        # MISSED / NOISE_INVALIDATED / DUPLICATE_BLOCKED - 没成交
        return {
            'id': trade.get('id'),
            'old_outcome': pos_state,
            'old_pnl': trade.get('net_usd', 0),
            'new_outcome': pos_state,
            'new_pnl': 0.0,
        }

    # 模拟新参数下成交
    fee_usd = 4.85  # 单笔总成本 (跟 position_tracker.FEE_USD 一致)

    # 判定: 新 SL/TP 距离 vs 实际最大价格行程
    if new_sl_usd <= max_reverse_usd:
        # 新 SL 紧到会被触发
        if side == 'long':
            new_exit_px = fill_px - new_sl_usd
        else:
            new_exit_px = fill_px + new_sl_usd
        sign = 1 if side == 'long' else -1
        new_pnl = sign * (new_exit_px - fill_px) * contracts * quant_mult
        new_pnl -= fee_usd
        new_outcome = 'SL_STOPPED'
    elif tp1_hit and new_tp1_usd <= max_forward_usd:
        # 新 TP1 仍能触发 - 假设走完新 TP1
        if side == 'long':
            new_exit_px = fill_px + new_tp1_usd
        else:
            new_exit_px = fill_px - new_tp1_usd
        sign = 1 if side == 'long' else -1
        new_pnl = sign * (new_exit_px - fill_px) * contracts * quant_mult
        new_pnl -= fee_usd
        new_outcome = 'TP1_DONE'
    elif tp2_hit and new_tp2_usd <= max_forward_usd:
        # 新 TP2 触发
        if side == 'long':
            new_exit_px = fill_px + new_tp2_usd
        else:
            new_exit_px = fill_px - new_tp2_usd
        sign = 1 if side == 'long' else -1
        new_pnl = sign * (new_exit_px - fill_px) * contracts * quant_mult
        new_pnl -= fee_usd
        new_outcome = 'TP2_DONE'
    elif tp1_hit and max_forward_usd > 0:
        # 新 TP1 太远, 但旧 TP1 已经触发 + 推保本 + 回到 entry
        # 实际 BE_STOPPED 净赚 1.46U/笔 (avg)
        # 简化: 估算 PnL = new_tp1_usd 的 1/2 (partial exit) * contracts * quant_mult
        # 因为保本策略在 tp1 推保本后只 1/3 仓位 partial_exit
        partial_exit_amt = new_tp1_usd * 0.33 * contracts * quant_mult
        new_pnl = partial_exit_amt - fee_usd  # 0.33 TP1 利润 - 手续费
        new_outcome = 'BE_STOPPED'
    elif pos_state == 'SL_STOPPED' and max_reverse_usd > 0:
        # 旧 SL 触发了, 但新 SL 没触发 - 走完 24h 平仓
        # 简化: 假设回到 entry
        new_pnl = -fee_usd
        new_outcome = 'TIMEOUT'
    else:
        # 既没到新 SL 也没到新 TP1 - 假设走 entry → 微亏手续费
        new_pnl = -fee_usd
        new_outcome = 'HOLD'

    return {
        'id': trade.get('id'),
        'old_outcome': pos_state,
        'old_pnl': trade.get('net_usd', 0),
        'new_outcome': new_outcome,
        'new_pnl': new_pnl,
        'side': side,
        'fill_px': fill_px,
    }


def run_backtest(journal_path: str, sl_pct: float, tp1_pct: float, tp2_pct: Optional[float] = None) -> Dict:
    """跑回测, 返回汇总统计"""
    trades = load_trades(journal_path)

    results = []
    for t in trades:
        r = simulate_trade(t, sl_pct, tp1_pct, tp2_pct)
        results.append(r)

    # 汇总
    pnls = [r['new_pnl'] for r in results if r['new_outcome'] not in ('MISSED', 'NOISE_INVALIDATED', 'DUPLICATE_BLOCKED')]
    if not pnls:
        return {'error': 'no settled trades'}

    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    total_pnl = sum(pnls)
    n = len(pnls)
    win_rate = len(wins) / n * 100

    # PF (profit factor)
    gross_profit = sum(wins) if wins else 0
    gross_loss = abs(sum(losses)) if losses else 0.001
    pf = gross_profit / gross_loss if gross_loss > 0 else 999

    # EV
    ev = total_pnl / n

    # R:R (avg win / abs avg loss)
    avg_win = statistics.mean(wins) if wins else 0
    avg_loss = abs(statistics.mean(losses)) if losses else 1
    rr = avg_win / avg_loss

    # Outcomes 分布
    outcomes = Counter(r['new_outcome'] for r in results)

    return {
        'params': {'sl_pct': sl_pct, 'tp1_pct': tp1_pct, 'tp2_pct': tp2_pct},
        'n_trades': n,
        'total_pnl': total_pnl,
        'win_rate': win_rate,
        'ev_per_trade': ev,
        'profit_factor': pf,
        'rr': rr,
        'avg_win': avg_win,
        'avg_loss': avg_loss,
        'outcomes': dict(outcomes),
    }


def grid_search(journal_path: str, sl_pcts: List[float], tp1_pcts: List[float]) -> List[Dict]:
    """网格搜索 SL/TP 组合"""
    results = []
    for sl_pct in sl_pcts:
        for tp1_pct in tp1_pcts:
            r = run_backtest(journal_path, sl_pct, tp1_pct)
            if 'error' not in r:
                r['params'] = {'sl_pct': sl_pct, 'tp1_pct': tp1_pct}
                results.append(r)
    return results


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='BTC 策略回测工具')
    parser.add_argument('--journal', default='/root/data/journal.jsonl', help='journal 路径')
    parser.add_argument('--sl', type=float, help='SL 比例 (e.g. 0.001 = 0.1%%)')
    parser.add_argument('--tp1', type=float, help='TP1 比例 (e.g. 0.005 = 0.5%%)')
    parser.add_argument('--tp2', type=float, help='TP2 比例 (e.g. 0.01 = 1%%)')
    parser.add_argument('--grid', action='store_true', help='网格搜索 SL/TP')
    parser.add_argument('--top', type=int, default=10, help='网格搜索显示前 N')
    parser.add_argument('--sort', default='total_pnl', help='网格搜索排序字段')
    parser.add_argument('--json', action='store_true', help='JSON 输出')

    args = parser.parse_args()

    if args.grid:
        sl_pcts = [0.001, 0.002, 0.003, 0.004, 0.005, 0.008, 0.010, 0.015, 0.020]
        tp1_pcts = [0.002, 0.003, 0.004, 0.005, 0.006, 0.008, 0.010, 0.015, 0.020]
        results = grid_search(args.journal, sl_pcts, tp1_pcts)
        results.sort(key=lambda r: r.get(args.sort, 0), reverse=True)
        if args.json:
            print(json.dumps(results[:args.top], indent=2, ensure_ascii=False))
        else:
            print(f"=== 网格搜索 Top-{args.top} (按 {args.sort}) ===")
            print(f"{'sl_pct':<8} {'tp1_pct':<8} {'PnL':<8} {'WR':<6} {'EV':<8} {'PF':<6} {'R:R':<6}")
            for r in results[:args.top]:
                p = r['params']
                print(f"  {p['sl_pct']*100:.3f}% {p['tp1_pct']*100:.3f}% {r['total_pnl']:+7.2f} {r['win_rate']:5.1f}% {r['ev_per_trade']:+7.3f} {r['profit_factor']:5.2f} {r['rr']:5.2f}")
    elif args.sl is not None and args.tp1 is not None:
        r = run_backtest(args.journal, args.sl, args.tp1, args.tp2)
        print(json.dumps(r, indent=2, ensure_ascii=False))
    else:
        parser.print_help()
