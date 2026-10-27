#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
周日 22:00 自动执行 BTC 策略正反馈闭环

功能：
  1. 分析本周生产 journal
  2. 生成优化建议
  3. 同步训练版本到生产
  4. 发送 Telegram 通知给 liusir

由 cron job 每周日 22:00 调用
"""

import sys
import os
import json
from datetime import datetime, timezone, timedelta
from pathlib import Path

# 添加路径
sys.path.insert(0, '/root/skills/btc-gate-intraday-strategy/scripts')
sys.path.insert(0, '/root/skills/btc-gate-intraday-strategy')

# 导入主模块
from feedback_loop import (
    analyze_production,
    optimize_training,
    sync_to_production,
    JOURNAL_FILE,
    TZ
)

# 5 objective + Edge Score 趋势追踪（2026-10-17 借鉴 HyperView）
try:
    from weekly_metrics_tracker import record_weekly_metrics, load_history, format_trend_report
    METRICS_TRACKER_AVAILABLE = True
except ImportError:
    METRICS_TRACKER_AVAILABLE = False

# Telegram Bot API（从环境变量或配置读取）
TELEGRAM_BOT_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN', '')
TELEGRAM_CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID', '7097652385')  # liusir 的 chat_id


def send_telegram(message: str):
    """发送 Telegram 消息"""
    if not TELEGRAM_BOT_TOKEN:
        print(f"⚠ TELEGRAM_BOT_TOKEN 未设置，跳过通知")
        print(f"消息内容:\n{message}")
        return

    import urllib.request
    import urllib.parse

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    data = {
        'chat_id': TELEGRAM_CHAT_ID,
        'text': message,
        'parse_mode': 'Markdown'
    }

    try:
        req = urllib.request.Request(
            url,
            data=urllib.parse.urlencode(data).encode(),
            headers={'Content-Type': 'application/x-www-form-urlencoded'}
        )
        resp = urllib.request.urlopen(req, timeout=10)
        result = json.loads(resp.read())
        if result.get('ok'):
            print(f"✓ Telegram 通知已发送")
        else:
            print(f"✗ Telegram 发送失败: {result}")
    except Exception as e:
        print(f"✗ Telegram 发送异常: {e}")


def generate_weekly_report(analysis, recommendations, sync_result):
    """生成周报"""
    now = datetime.now(TZ)

    # 统计本周数据
    total_trades = analysis.get('total_trades', 0)
    win_rate = analysis.get('win_rate', 0)
    total_net = analysis.get('total_net', 0)
    issues = analysis.get('issues', [])

    # 生成 Markdown 格式
    # 2026-10-17 借鉴 HyperView 5 objective + Edge Score
    report = f"""📊 *BTC 策略周报* ({now.strftime('%Y-%m-%d')})

*本周表现:*
• 总交易: {total_trades} 笔（已结算 {analysis.get('settled_trades', 0)}）
• 胜率: {win_rate:.1f}%
• 净盈亏: {total_net:+.2f}U

*HyperView 5 Objective:*
• net_profit:     {analysis.get('obj_net_profit', 0):+.2f}U
• profit_factor:  {analysis.get('obj_profit_factor', 0):.2f}  (PF=3.0 满分)
• win_rate:       {win_rate:.1f}%  (WR=60% 满分)
• max_drawdown:   {analysis.get('obj_max_drawdown_pct', 0):.1f}%  (DD=0% 满分)
• trade_count:    {analysis.get('settled_trades', 0)} 笔
"""

    # Edge Score（如果 analyze 算过）
    try:
        from edge_score import compute_edge_score
        from pathlib import Path
        import json as _json
        _trades = []
        for _line in Path('/root/data/journal.jsonl').read_text().strip().split('\n'):
            if not _line.strip(): continue
            try: _trades.append(_json.loads(_line))
            except: pass
        _es = compute_edge_score(_trades)
        if _es:
            report += f"""
*Edge Score v2:*
• Total: *{_es.total:.2f}* / 100 (样本 {_es.n_trades})
• 胜率: {_es.components['win_rate']:.1f}  PF: {_es.components['profit_factor']:.1f}  赢/亏: {_es.components['avg_win_loss']:.1f}
• 回撤: {_es.components['max_drawdown_pct']:.1f}  修复: {_es.components['recovery_factor']:.1f}  一致性: {_es.components['consistency_pct']:.1f}
"""
    except Exception:
        pass

    report += "\n*发现的问题:*\n"

    if issues:
        for i, issue in enumerate(issues, 1):
            impact = issue.get('objective_impact', '')
            report += f"{i}. *{issue['type']}*\n   {issue['detail']}"
            if impact:
                report += f"\n   影响: {impact}"
            report += "\n"
    else:
        report += "无重大问题 ✓\n"

    report += f"""
*优化建议:*
"""

    if recommendations:
        for i, rec in enumerate(recommendations[:3], 1):  # 只显示前 3 条
            impact = rec.get('objective_impact', '')
            risk = rec.get('risk', '')
            report += f"{i}. `{rec['param']}`: {rec['current']} → {rec['suggested']}\n"
            if impact:
                report += f"   Objective: {impact}\n"
            if risk and risk != 'N/A':
                report += f"   风险: {risk}\n"
    else:
        report += "本周无调整建议\n"

    report += f"""
*同步状态:*
备份位置: `{Path(sync_result.get('backup_dir', '')).name}`
状态: {sync_result.get('status', 'unknown')}

*下一步:*
• 周一开盘后监控新参数表现
• 收集下周数据
"""

    return report


def run_weekly_sync():
    """执行每周同步"""
    print("=" * 60)
    print(f"BTC 策略周度同步 - {datetime.now(TZ).strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 60)

    # 1. 分析生产问题
    print("\n[1/3] 分析生产环境...")
    analysis = analyze_production()

    # 2. 生成优化建议
    print("\n[2/3] 生成优化建议...")
    recommendations = optimize_training(analysis)

    # 3. 同步到生产
    print("\n[3/3] 同步训练版本到生产...")
    sync_result = sync_to_production()

    # 4. 生成周报
    report = generate_weekly_report(analysis, recommendations or [], sync_result)

    # 5. 记录本周 5 objective + Edge Score 到历史文件（2026-10-17 新增）
    if METRICS_TRACKER_AVAILABLE:
        try:
            metrics = record_weekly_metrics()
            history = load_history()
            report += f"\n\n*本周记录:*\n• 5 objective + Edge Score 已存到历史 (week {metrics['week_label']})\n• 当前累计 {len(history)} 周历史"
            if len(history) >= 2:
                report += f"\n\n*趋势 (周环比):*\n```\n{format_trend_report(history)}\n```"
        except Exception as e:
            print(f"⚠ metrics tracker 失败: {e}")

    # 6. 发送通知
    print("\n" + "=" * 60)
    print("执行完成，发送通知...")
    print("=" * 60)
    print(report)

    send_telegram(report)

    print("\n✓ 周度同步完成")


if __name__ == '__main__':
    run_weekly_sync()
