#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
BTC 策略正反馈闭环系统
======================

核心流程：
  1. 生产环境发现问题（journal 分析）
  2. 训练环境优化策略（backtest + 参数调优）
  3. 每周同步训练版本到生产（cron job）

文件结构：
  - production_analysis.py  : 分析生产 journal，发现问题模式
  - training_optimize.py    : 在训练环境调参优化
  - sync_production.py      : 每周同步训练版本到生产
  - weekly_cron.py          : 周一凌晨自动执行完整闭环

使用方法：
  python3 feedback_loop.py analyze        # 分析生产问题
  python3 feedback_loop.py optimize       # 训练环境优化
  python3 feedback_loop.py sync           # 同步到生产
  python3 feedback_loop.py full           # 完整闭环
"""

import sys
import os
import json
from datetime import datetime, timezone, timedelta
from pathlib import Path

# 添加 scripts 路径
sys.path.insert(0, '/root/skills/btc-gate-intraday-strategy/scripts')

# 常量
DATA_DIR = Path('/root/data')
JOURNAL_FILE = DATA_DIR / 'journal.jsonl'
TEACHER_FILE = DATA_DIR / 'teacher_trades.jsonl'
PRODUCTION_SKILL_DIR = Path('/root/skills/btc-gate-intraday-strategy')
TRAINING_CONFIG = PRODUCTION_SKILL_DIR / 'scripts' / 'training_config.py'

# 时区
TZ = timezone(timedelta(hours=8))


def analyze_production():
    """分析生产环境 journal，发现问题模式

    2026-10-17 借鉴 HyperView (TreborNamor/TradingView-Machine-Learning-GUI)
    5 objective 框架：
      1. net_profit (总盈亏)
      2. profit_factor (盈亏比) —— 你 Edge Score 权重 25%
      3. win_rate (胜率) —— Edge Score 权重 15%
      4. max_drawdown (回撤) —— Edge Score 权重 15%
      5. trade_count (样本量) —— 决策可信度前提
    """
    print("=" * 60)
    print("1. 生产环境问题分析（HyperView 5 objective 框架）")
    print("=" * 60)

    if not JOURNAL_FILE.exists():
        print("✗ journal.jsonl 不存在")
        return {}

    # 读取 journal
    trades = []
    with open(JOURNAL_FILE, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    trades.append(json.loads(line))
                except:
                    pass

    print(f"\n总交易数: {len(trades)}")

    # 统计结局分布
    outcomes = {}
    for t in trades:
        o = t.get('outcome', 'unknown')
        outcomes[o] = outcomes.get(o, 0) + 1

    print(f"\n结局分布:")
    for o, count in sorted(outcomes.items(), key=lambda x: -x[1]):
        pct = count / len(trades) * 100
        print(f"  {o:20s} {count:3d} ({pct:.1f}%)")

    # 计算盈亏
    total_net = 0.0
    wins, losses, be = 0, 0, 0
    settled_trades = []
    for t in trades:
        if t.get('status') == 'settled':
            net = t.get('net_usd', 0)
            total_net += net
            settled_trades.append(t)
            if net > 0:
                wins += 1
            elif net < 0:
                losses += 1
            else:
                be += 1

    settled = wins + losses + be
    win_rate = (wins / settled * 100) if settled > 0 else 0

    # === HyperView 5 objective 框架计算 ===
    # Objective #1: net_profit
    obj_net_profit = total_net

    # Objective #2: profit_factor
    gross_profit = sum(t.get('net_usd', 0) for t in settled_trades if t.get('net_usd', 0) > 0)
    gross_loss = -sum(t.get('net_usd', 0) for t in settled_trades if t.get('net_usd', 0) < 0)
    obj_profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else (999.0 if gross_profit > 0 else 0)

    # Objective #3: win_rate (已有)
    obj_win_rate = win_rate

    # Objective #4: max_drawdown
    sorted_trades = sorted(settled_trades, key=lambda x: x.get('ts', 0))
    equity = 0.0
    peak = 0.0
    max_dd = 0.0
    for t in sorted_trades:
        equity += t.get('net_usd', 0)
        if equity > peak:
            peak = equity
        dd = peak - equity
        if dd > max_dd:
            max_dd = dd
    obj_max_drawdown = (max_dd / peak * 100) if peak > 0 else 0.0  # 百分比

    # Objective #5: trade_count
    obj_trade_count = len(settled_trades)

    print(f"\n=== HyperView 5 Objective ===")
    print(f"  #1 net_profit:    {obj_net_profit:+.2f}U")
    print(f"  #2 profit_factor: {obj_profit_factor:.2f} (PF=3.0 满分)")
    print(f"  #3 win_rate:      {obj_win_rate:.1f}% (WR=60% 满分)")
    print(f"  #4 max_drawdown:  {obj_max_drawdown:.1f}% (DD=0% 满分)")
    print(f"  #5 trade_count:   {obj_trade_count} 笔")

    # Edge Score（之前模块算的）
    try:
        from edge_score import compute_edge_score
        es = compute_edge_score(trades)
        if es:
            print(f"\n=== Edge Score v2 ===")
            print(f"  Total: {es.total:.2f} / 100 (样本 {es.n_trades})")
            print(f"  胜率: {es.components['win_rate']:.1f}  PF: {es.components['profit_factor']:.1f}  赢/亏: {es.components['avg_win_loss']:.1f}")
            print(f"  回撤: {es.components['max_drawdown_pct']:.1f}  修复: {es.components['recovery_factor']:.1f}  一致性: {es.components['consistency_pct']:.1f}")
    except Exception as e:
        print(f"⚠ Edge Score 计算失败: {e}")

    # 发现问题模式（基于 5 objective + Edge Score 维度）
    issues = []

    # 问题1: SL_STOPPED 太多（止损频繁）
    sl_count = outcomes.get('SL_STOPPED', 0)
    if obj_trade_count > 0 and sl_count / obj_trade_count > 0.3:
        issues.append({
            'type': '高止损率',
            'detail': f'SL_STOPPED {sl_count} 笔 ({sl_count/obj_trade_count*100:.1f}%)，止损过于频繁',
            'suggestion': '考虑放宽 SL 或调整入场时机',
            'objective_impact': 'PF↓ + DD↓',
        })

    # 问题2: MISSED 太多（信号未成交）
    missed_count = outcomes.get('MISSED', 0)
    if obj_trade_count > 0 and missed_count / obj_trade_count > 0.1:
        issues.append({
            'type': '高MISSED率',
            'detail': f'MISSED {missed_count} 笔 ({missed_count/obj_trade_count*100:.1f}%)，入场触发但未成交',
            'suggestion': '考虑放宽入场条件或优化 entry 计算',
            'objective_impact': 'trade_count↓',
        })

    # 问题3: TP2_DONE 太少（止盈比例低）
    tp2_count = outcomes.get('TP2_DONE', 0)
    if obj_trade_count > 0 and tp2_count / obj_trade_count < 0.1:
        issues.append({
            'type': '低止盈率',
            'detail': f'TP2_DONE {tp2_count} 笔 ({tp2_count/obj_trade_count*100:.1f}%)，达到第二止盈位的比例过低',
            'suggestion': '考虑调整 TP1/TP2 距离或趋势过滤',
            'objective_impact': 'net_profit↓',
        })

    # 问题4: BE_STOPPED 太多（保本止损频繁）
    be_count = outcomes.get('BE_STOPPED', 0)
    if obj_trade_count > 0 and be_count / obj_trade_count > 0.2:
        issues.append({
            'type': '高保本止损',
            'detail': f'BE_STOPPED {be_count} 笔 ({be_count/obj_trade_count*100:.1f}%)，入场后快速回到入场价',
            'suggestion': '入场时机可能过早，考虑等待更明确的信号',
            'objective_impact': 'WR↓ + DD↓',
        })

    # === 新增 HyperView 5 objective 维度问题 ===
    # 问题5: PF 偏低（盈亏比 < 1.5）
    if obj_profit_factor < 1.5 and obj_profit_factor > 0 and obj_trade_count >= 5:
        issues.append({
            'type': '低盈亏比',
            'detail': f'profit_factor={obj_profit_factor:.2f} (< 1.5 警戒线)',
            'suggestion': '改造方向：拉长 TP1 / 缩窄 SL（但需控制 DD）',
            'objective_impact': 'Edge Score 盈亏比维度↓',
        })

    # 问题6: 回撤 > 30%（生产 Edge Score 弱项）
    if obj_max_drawdown > 30 and obj_trade_count >= 5:
        issues.append({
            'type': '高回撤',
            'detail': f'max_drawdown={obj_max_drawdown:.1f}% (> 30% 警戒线)',
            'suggestion': '缩窄 SL + 严控 BE_STOPPED（保住本金）',
            'objective_impact': 'Edge Score 回撤维度↓',
        })

    # 问题7: 样本量 < 5（Edge Score 不出分）
    if obj_trade_count < 5:
        issues.append({
            'type': '样本量不足',
            'detail': f'trade_count={obj_trade_count} (< 5 Edge Score 不出分)',
            'suggestion': '本周先观察不调参，等样本量 > 5 再决策',
            'objective_impact': 'Edge Score 无法计算',
        })

    # 问题8: 总盈亏持续负值
    if obj_net_profit < -10 and obj_trade_count >= 5:
        issues.append({
            'type': '持续亏损',
            'detail': f'累计净亏损 {obj_net_profit:.2f}U（{obj_trade_count} 笔）',
            'suggestion': '回测 + 检查是否有系统性错误（如 SL 错位、R:R<1）',
            'objective_impact': 'net_profit↓ + 全部维度↓',
        })

    print(f"\n发现问题 ({len(issues)} 个):")
    for i, issue in enumerate(issues, 1):
        print(f"\n  {i}. {issue['type']}")
        print(f"     {issue['detail']}")
        print(f"     建议: {issue['suggestion']}")
        if 'objective_impact' in issue:
            print(f"     影响: {issue['objective_impact']}")

    return {
        'total_trades': len(trades),
        'settled_trades': obj_trade_count,
        'outcomes': outcomes,
        'win_rate': win_rate,
        'total_net': total_net,
        # HyperView 5 objective
        'obj_net_profit': obj_net_profit,
        'obj_profit_factor': obj_profit_factor,
        'obj_max_drawdown_pct': obj_max_drawdown,
        'issues': issues
    }


def optimize_training(analysis_result):
    """在训练环境优化策略参数

    2026-10-17 借鉴 HyperView 5 objective 框架：
      每条建议显式标注"目标影响"，让你评估 trade-off
      而不是只列"建议参数"
    """
    print("\n" + "=" * 60)
    print("2. 训练环境优化（HyperView 5 objective 框架）")
    print("=" * 60)

    if not analysis_result:
        print("✗ 无分析结果，跳过优化")
        return

    issues = analysis_result.get('issues', [])
    if not issues:
        print("✓ 未发现问题，无需优化")
        return

    # 加载当前训练配置
    if TRAINING_CONFIG.exists():
        print(f"\n当前训练配置: {TRAINING_CONFIG}")
        with open(TRAINING_CONFIG, 'r', encoding='utf-8') as f:
            print(f.read())
    else:
        print("⚠ 训练配置文件不存在")

    # 根据问题类型推荐参数调整（带 5 objective 影响标注）
    recommendations = []

    for issue in issues:
        if issue['type'] == '高止损率':
            recommendations.append({
                'param': 'sl_pct',
                'current': '0.4%',
                'suggested': '0.5%',
                'reason': '放宽止损，减少被震出概率',
                'objective_impact': 'net_profit↑ + WR↑ (PF ↑如果 SL 放宽幅度 < TP 距离)',
                'risk': 'DD ↑ (放大单笔亏损)',
            })
            recommendations.append({
                'param': 'entry_filter',
                'current': '当前无',
                'suggested': '加入趋势过滤',
                'reason': '避免逆势入场',
                'objective_impact': 'WR↑ (trade_count↓)',
                'risk': '错过区间震荡机会',
            })

        elif issue['type'] == '高MISSED率':
            recommendations.append({
                'param': 'entry',
                'current': '突破入场',
                'suggested': '回踩入场',
                'reason': '突破追高容易 MISSED，改回踩更稳',
                'objective_impact': 'trade_count↑ (WR略↑ if 顺势)',
                'risk': '可能错过强势行情',
            })

        elif issue['type'] == '低止盈率':
            recommendations.append({
                'param': 'tp2_pct',
                'current': '2.9%',
                'suggested': '2.5%',
                'reason': '降低第二止盈距离，提高达成率',
                'objective_impact': 'TP2_DONE%↑ (net_profit↓单笔但 total 可能 ↑)',
                'risk': '单笔盈利变小',
            })

        elif issue['type'] == '高保本止损':
            recommendations.append({
                'param': 'entry_timing',
                'current': '立即入场',
                'suggested': '等待确认',
                'reason': '入场过早导致快速保本止损',
                'objective_impact': 'WR↑ + DD↓',
                'risk': '错过最佳入场点',
            })

        # === 新增 HyperView 5 objective 维度建议 ===
        elif issue['type'] == '低盈亏比':
            # 这是你的"控制回撤 + 盈亏比"哲学核心问题
            recommendations.append({
                'param': 'sl_pct + tp1_pct',
                'current': 'sl_pct 0.4%, tp1 1.3%',
                'suggested': 'sl_pct 0.1%（保本），tp1 0.5%（保底）',
                'reason': 'Edge Score 盈亏比维度 38.10 分（弱）→ 提 R:R',
                'objective_impact': 'PF↑↑ (回撤可控制) + Edge Score 盈亏比维度↑',
                'risk': 'SL 太窄容易被震出（WR↓）',
            })

        elif issue['type'] == '高回撤':
            recommendations.append({
                'param': 'sl_pct',
                'current': '0.4%',
                'suggested': '0.2% (缩窄)',
                'reason': '回撤 312% 是 Edge Score 唯一 0 分项',
                'objective_impact': 'DD↓↓ (PF↓但 R:R 改善)',
                'risk': 'WR↓↓ (被震出更多)',
            })

        elif issue['type'] == '样本量不足':
            recommendations.append({
                'param': 'NO CHANGE',
                'current': '本周策略',
                'suggested': '继续观察',
                'reason': '样本量 < 5 不出 Edge Score，调参风险大',
                'objective_impact': '不调参 = 不退化',
                'risk': 'N/A',
            })

        elif issue['type'] == '持续亏损':
            recommendations.append({
                'param': 'PAPER MODE 1 周',
                'current': '实盘',
                'suggested': '切训练环境观察 1 周',
                'reason': '净亏损超过 10U = 系统性错误',
                'objective_impact': '避免继续亏 → 保留本金',
                'risk': '错过可能的反弹',
            })

    print(f"\n推荐参数调整 ({len(recommendations)} 项):")
    for i, rec in enumerate(recommendations, 1):
        print(f"\n  {i}. {rec['param']}")
        print(f"     当前: {rec['current']}")
        print(f"     建议: {rec['suggested']}")
        print(f"     原因: {rec['reason']}")
        if 'objective_impact' in rec:
            print(f"     Objective 影响: {rec['objective_impact']}")
        if 'risk' in rec:
            print(f"     风险: {rec['risk']}")

    # 5 objective 摘要
    print(f"\n=== 本周 5 Objective 现状 ===")
    print(f"  #1 net_profit:    {analysis_result.get('obj_net_profit', 0):+.2f}U")
    print(f"  #2 profit_factor: {analysis_result.get('obj_profit_factor', 0):.2f}")
    print(f"  #3 win_rate:      {analysis_result.get('win_rate', 0):.1f}%")
    print(f"  #4 max_drawdown:  {analysis_result.get('obj_max_drawdown_pct', 0):.1f}%")
    print(f"  #5 trade_count:   {analysis_result.get('settled_trades', 0)} 笔")

    # 执行回测验证
    print(f"\n执行回测验证...")
    print("(此处应调用 backtest_teacher_bias.py 进行参数验证)")
    print("⚠ 待实现：自动回测对比新旧参数表现")

    return recommendations


def sync_to_production():
    """将训练版本同步到生产环境"""
    print("\n" + "=" * 60)
    print("3. 同步训练版本到生产")
    print("=" * 60)

    # 备份当前生产版本
    backup_dir = Path(f'/root/.hermes/backups/btc-strategy-{datetime.now().strftime("%Y%m%d-%H%M%S")}')
    backup_dir.mkdir(parents=True, exist_ok=True)

    print(f"备份目录: {backup_dir}")

    # 备份关键文件
    files_to_backup = [
        'scripts/scan.py',
        'scripts/watch.py',
        'scripts/watch/jump.py',
        'scripts/watch/volume.py',
        'scripts/watch/rsi.py',
        'scripts/position_tracker.py',
    ]

    for f in files_to_backup:
        src = PRODUCTION_SKILL_DIR / f
        if src.exists():
            dst = backup_dir / f
            dst.parent.mkdir(parents=True, exist_ok=True)
            import shutil
            shutil.copy2(src, dst)
            print(f"  ✓ 备份 {f}")

    # 同步训练版本到生产
    print(f"\n同步训练版本到生产...")
    print("(此处应将训练环境调优后的文件复制到生产)")
    print("⚠ 待实现：自动同步机制")

    # 验证同步结果
    print(f"\n验证同步结果...")
    print("(此处应验证生产环境文件与训练版本一致)")
    print("⚠ 待实现：自动验证")

    return {
        'backup_dir': str(backup_dir),
        'status': 'pending_implementation'
    }


def run_full_loop():
    """执行完整正反馈闭环"""
    print("🚀 BTC 策略正反馈闭环系统")
    print(f"执行时间: {datetime.now(TZ).strftime('%Y-%m-%d %H:%M:%S')}")
    print()

    # 1. 分析生产问题
    analysis = analyze_production()

    # 2. 训练环境优化
    recommendations = optimize_training(analysis)

    # 3. 同步到生产
    sync_result = sync_to_production()

    # 总结
    print("\n" + "=" * 60)
    print("闭环执行总结")
    print("=" * 60)
    print(f"\n生产分析: 发现 {len(analysis.get('issues', []))} 个问题")
    print(f"训练优化: 生成 {len(recommendations or [])} 条建议")
    print(f"生产同步: {sync_result.get('status', 'unknown')}")

    print("\n下一步:")
    print("  1. 审核优化建议，决定是否采纳")
    print("  2. 执行回测验证（待实现自动化）")
    print("  3. 等待周日 cron 自动同步到生产")
    print("  4. 监控生产表现，收集数据")
    print("  5. 下周重复闭环")


if __name__ == '__main__':
    if len(sys.argv) < 2:
        print("用法: python3 feedback_loop.py [analyze|optimize|sync|full]")
        sys.exit(1)

    cmd = sys.argv[1]
    if cmd == 'analyze':
        analyze_production()
    elif cmd == 'optimize':
        result = analyze_production()
        optimize_training(result)
    elif cmd == 'sync':
        sync_to_production()
    elif cmd == 'full':
        run_full_loop()
    else:
        print(f"未知命令: {cmd}")
        sys.exit(1)
