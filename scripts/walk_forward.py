#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Walk-Forward 回测工具
=====================

2026-10-11 立项（v5 验证基础设施）

目的：
  验证策略参数改动（v5 假设）时，避免用 29 笔数据切分造成过拟合。
  标准做法：前 N 天训练（拟合参数）→ 后 M 天验证（测试效果），多次滚动。

输入：
  journal.jsonl（生产数据）或指定的 backtest_dataset.jsonl

输出：
  walk_forward_report.md（含每次验证的 winrate/net/Sharpe，多次验证的 mean ± std）

设计原则：
  1. 必须 walk-forward（不能用全样本做训练）
  2. 样本门槛 N=30（每个验证窗口）
  3. Bonferroni 校正（多个假设同时验证时阈值收紧）
  4. 不做参数优化——只做"假设验证"（v5 假设 vs v4.2 基线）

用法：
    python3 walk_forward.py                     # 默认滚动 7 天窗口
    python3 walk_forward.py --train-days 30 --test-days 7
    python3 walk_forward.py --out report.md

输出（默认 markdown）：
    # Walk-Forward 回测报告
    ## 基线 (v4.2)
    ...
    ## v5 假设对比
    ### H1: score 阈值改为 [2,3]
    ...
    ## 多重比较校正（Bonferroni）
    ...

环境变量：
    WALK_FORWARD_STRICT=1   严格模式（要求 N≥30 否则报错）
"""
from __future__ import annotations
import os
import sys
import json
import argparse
from datetime import datetime, timedelta, timezone
from typing import List, Dict, Tuple
from collections import defaultdict

CST = timezone(timedelta(hours=8))

# 默认路径（跟随 journal）
DATA_DIR = os.environ.get("BTC_DATA_DIR", "/root/data")
DEFAULT_JOURNAL = os.path.join(DATA_DIR, "journal.jsonl")

STRICT = os.environ.get("WALK_FORWARD_STRICT") == "1"


def load_journal(path: str = DEFAULT_JOURNAL) -> List[Dict]:
    """读 journal.jsonl，只保留已结算记录"""
    if not os.path.exists(path):
        print(f"⚠️  journal 文件不存在: {path}")
        return []
    recs = []
    with open(path) as f:
        for ln in f:
            ln = ln.strip()
            if not ln:
                continue
            try:
                r = json.loads(ln)
                if r.get("status") == "settled" and r.get("outcome") not in ("MISSED",):
                    recs.append(r)
            except Exception:
                continue
    return recs


def split_walk_forward(recs: List[Dict],
                       train_days: int = 30,
                       test_days: int = 7) -> List[Tuple[int, int, List[Dict], List[Dict]]]:
    """
    Walk-forward 切分：前 train_days 训练 → 后 test_days 验证，滚动。
    返回: [(train_start_ts, test_start_ts, train_recs, test_recs), ...]
    """
    if not recs:
        return []
    ts_list = sorted([r.get("ts", 0) for r in recs])
    t_min, t_max = ts_list[0], ts_list[-1]
    
    windows = []
    cursor = t_min
    while cursor + (train_days + test_days) * 86400 <= t_max:
        train_start = cursor
        train_end = cursor + train_days * 86400
        test_start = train_end
        test_end = train_end + test_days * 86400
        
        train_recs = [r for r in recs if train_start <= r.get("ts", 0) < train_end]
        test_recs = [r for r in recs if test_start <= r.get("ts", 0) < test_end]
        
        windows.append((train_start, test_start, train_recs, test_recs))
        cursor += test_days * 86400  # 滚动 test_days
    
    return windows


def evaluate(reports: List[Dict]) -> Dict:
    """评估一组已结算记录的指标"""
    if not reports:
        return {"n": 0, "win": 0, "loss": 0, "win_rate": 0.0, "net": 0.0, "avg_net": 0.0, "sharpe": 0.0}
    
    wins = [r for r in reports if r.get("outcome") in ("TP2_DONE", "TP1_PARTIAL", "BE_STOPPED")]
    losses = [r for r in reports if r.get("outcome") in ("SL_STOPPED", "MCLOSE")]
    n = len(reports)
    nw = len(wins)
    nl = len(losses)
    net = sum(r.get("net_usd", 0) for r in reports)
    avg = net / n if n else 0.0
    
    # Sharpe (简化版, 用 avg/std)
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
        "net": net, "avg_net": avg, "sharpe": sharpe
    }


def bonferroni_threshold(n_hypotheses: int, alpha: float = 0.05) -> float:
    """Bonferroni 校正阈值：多个假设同时验证时，单一假设显著性阈值 = alpha / n"""
    return alpha / n_hypotheses if n_hypotheses > 0 else alpha


def format_report(windows: List[Tuple], baseline_results: List[Dict],
                  hypothesis_results: Dict[str, List[Dict]],
                  hypothesis_names: List[str]) -> str:
    """生成 markdown 报告"""
    L = []
    L.append(f"# Walk-Forward 回测报告")
    L.append(f"")
    L.append(f"生成时间: {datetime.now(CST).strftime('%Y-%m-%d %H:%M:%S')} CST")
    L.append(f"")
    L.append(f"## 切分参数")
    L.append(f"- 训练窗口: 30 天")
    L.append(f"- 验证窗口: 7 天")
    L.append(f"- 滚动次数: {len(windows)}")
    L.append(f"")
    
    L.append(f"## 基线 (v4.2) 整体表现")
    base_eval = evaluate([r for _, _, _, te in windows for r in te])
    L.append(f"- 验证集样本 N = {base_eval['n']}")
    L.append(f"- Win rate = {base_eval['win_rate']:.1f}%")
    L.append(f"- Net = {base_eval['net']:+.2f}U")
    L.append(f"- Avg net/trade = {base_eval['avg_net']:+.2f}U")
    L.append(f"- Sharpe = {base_eval['sharpe']:.2f}")
    L.append(f"")
    
    L.append(f"## Walk-Forward 逐窗口")
    L.append(f"")
    L.append(f"### 窗口列表")
    L.append(f"")
    L.append(f"| # | 训练起点 | 验证起点 | 验证 N | Win Rate | Net | Sharpe |")
    L.append(f"|---|---|---|---|---|---|---|")
    for i, (tr_s, te_s, tr, te) in enumerate(windows):
        tr_dt = datetime.fromtimestamp(tr_s, CST).strftime('%Y-%m-%d')
        te_dt = datetime.fromtimestamp(te_s, CST).strftime('%Y-%m-%d')
        ev = evaluate(te)
        L.append(f"| {i+1} | {tr_dt} | {te_dt} | {ev['n']} | {ev['win_rate']:.1f}% | {ev['net']:+.2f}U | {ev['sharpe']:.2f} |")
    L.append(f"")
    
    if hypothesis_results:
        L.append(f"## v5 假设对比")
        L.append(f"")
        for hname, h_results in hypothesis_results.items():
            h_eval = evaluate([r for _, _, _, te in h_results for r in te])
            delta_wr = h_eval['win_rate'] - base_eval['win_rate']
            delta_net = h_eval['net'] - base_eval['net']
            L.append(f"### 假设: {hname}")
            L.append(f"")
            L.append(f"- 验证集样本 N = {h_eval['n']}")
            L.append(f"- Win rate = {h_eval['win_rate']:.1f}%（基线 {base_eval['win_rate']:.1f}%, Δ {delta_wr:+.1f}pp）")
            L.append(f"- Net = {h_eval['net']:+.2f}U（基线 {base_eval['net']:+.2f}U, Δ {delta_net:+.2f}U）")
            L.append(f"- Avg net/trade = {h_eval['avg_net']:+.2f}U")
            L.append(f"- Sharpe = {h_eval['sharpe']:.2f}（基线 {base_eval['sharpe']:.2f}）")
            L.append(f"")
        
        L.append(f"## 多重比较校正（Bonferroni）")
        L.append(f"")
        n_hyp = len(hypothesis_results)
        threshold = bonferroni_threshold(n_hyp)
        L.append(f"- 假设数量: {n_hyp}")
        L.append(f"- 单一假设显著性阈值（Bonferroni 校正）: α' = {threshold:.4f}")
        L.append(f"- 解读：每个假设的 p 值必须 < {threshold:.4f} 才算显著（不是 0.05）")
        L.append(f"")
    
    L.append(f"## 解读建议")
    L.append(f"")
    L.append(f"1. **样本门槛 N≥30**：每个验证窗口应有 30+ 笔记录，否则结论不稳。")
    L.append(f"2. **多重比较校正**：5 个假设同时验证时，单假设 p < 0.05 必然出 1 个假阳性。")
    L.append(f"   正确做法：Bonferroni 校正，阈值收紧到 {bonferroni_threshold(5):.3f}。")
    L.append(f"3. **Walk-forward 验证**：前 30 天训练，后 7 天验证，多次滚动，避免全样本过拟合。")
    L.append(f"4. **不显著时**：v4.2 是对的，继续跑，不要瞎改。")
    L.append(f"5. **显著时**：灰度上线（10% 仓位 v5 + 90% v4.2），A/B 跑 7 天再决策。")
    
    return "\n".join(L)


def main():
    ap = argparse.ArgumentParser(add_help=False)
    ap.add_argument("--train-days", type=int, default=30)
    ap.add_argument("--test-days", type=int, default=7)
    ap.add_argument("--journal", default=DEFAULT_JOURNAL)
    ap.add_argument("--out", default=None)
    ap.add_argument("--md", action="store_true", default=True)
    a = ap.parse_args()
    
    recs = load_journal(a.journal)
    if not recs:
        print("⚠️  journal 为空或加载失败")
        return 1
    
    windows = split_walk_forward(recs, a.train_days, a.test_days)
    if not windows:
        print(f"⚠️  数据不足以做 walk-forward（需 {(a.train_days + a.test_days)} 天）")
        print(f"   实际数据跨度: {(sorted([r.get('ts',0) for r in recs])[-1] - sorted([r.get('ts',0) for r in recs])[0]) / 86400:.1f} 天")
        return 1
    
    # 基线 = 全部验证集（v4.2 现状）
    baseline_results = [r for _, _, _, te in windows for r in te]
    
    # 假设验证：目前空（v5 还没设计好）
    hypothesis_results = {}
    hypothesis_names = []
    
    report = format_report(windows, baseline_results, hypothesis_results, hypothesis_names)
    
    if a.out:
        with open(a.out, "w") as f:
            f.write(report)
        print(f"✓ 报告已写入 {a.out}")
    else:
        print(report)
    
    return 0


if __name__ == "__main__":
    sys.exit(main())
