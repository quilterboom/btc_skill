# BTC 策略正反馈闭环系统

## 系统概述

建立"生产发现不足 → 训练环境优化验证 → 每周同步生产"的正反馈闭环，持续改进交易策略。

## 核心文件

- `feedback_loop.py` - 主控脚本，执行完整闭环
- `weekly_sync.py` - 周度同步脚本（cron 自动调用）
- `/root/data/journal.jsonl` - 生产环境交易记录
- `/root/data/training/journal.jsonl` - 训练环境交易记录（待实现）

## 使用方法

### 1. 手动执行分析

```bash
# 只分析生产问题
python3 feedback_loop.py analyze

# 分析 + 生成优化建议
python3 feedback_loop.py optimize

# 完整闭环（分析 + 优化 + 同步）
python3 feedback_loop.py full
```

### 2. 自动周度同步

已配置 cron job：每周日 22:00 自动执行完整闭环

```bash
# 查看 cron job
# job_id: 6d6bcf893cc5
# 执行时间: 每周日 22:00
```

### 3. 查看执行结果

每次执行会：
1. 分析生产 journal，发现问题模式
2. 生成参数优化建议
3. 备份当前生产版本
4. 发送周报到 Telegram

## 工作流程

### 第一阶段：生产问题发现

分析 `/root/data/journal.jsonl` 中的交易记录，关注：

- **高止损率** (SL_STOPPED > 30%) → 考虑放宽 SL 或调整入场时机
- **高MISSED率** (MISSED > 10%) → 入场触发但未成交，优化 entry 计算
- **低止盈率** (TP2_DONE < 10%) → 调整 TP1/TP2 距离或趋势过滤
- **高保本止损** (BE_STOPPED > 20%) → 入场时机过早

### 第二阶段：训练环境优化

根据问题类型，在训练环境调整参数：

| 问题类型 | 参数调整 | 预期效果 |
|---------|---------|---------|
| 高止损率 | sl_pct: 0.4% → 0.5% | 减少被震出概率 |
| 高MISSED率 | entry: 突破 → 回踩 | 更稳定的入场 |
| 低止盈率 | tp2_pct: 2.9% → 2.5% | 提高止盈达成率 |
| 高保本止损 | entry_timing: 等待确认 | 避免过早入场 |

**关键规则**：策略改动落地前必须回测验证（2026-10-13 liusir 拍板）

### 第三阶段：同步到生产

每周日 22:00 自动执行：

1. 备份当前生产版本到 `/root/.hermes/backups/btc-strategy-YYYYMMDD-HHMMSS/`
2. 将训练环境调优后的文件复制到生产
3. 验证同步结果
4. 发送周报到 Telegram

## 当前状态

### 生产环境表现（2026-10-15）

- 总交易: 65 笔
- 已结算: 40 笔
- 胜率: 32.5% (13胜 21负 6平)
- 净盈亏: +25.02U
- 平均盈亏: +0.63U/笔

### 发现的问题

1. **低止盈率**: TP2_DONE 仅 9.2%，第二止盈位达成率过低
   - 建议: tp2_pct 从 2.9% 降到 2.5%

### 下一步行动

- [ ] 周一开盘后监控当前参数表现
- [ ] 收集本周数据
- [ ] 周日 22:00 自动执行周度同步
- [ ] 审核优化建议，决定是否采纳
- [ ] 执行回测验证（待实现自动化）

## 高级配置

### 训练环境配置

位置: `/root/skills/btc-gate-intraday-strategy/scripts/training_config.py`

包含 v5-dev 策略的 6 项改动：
1. 多因子加权评分
2. ATR 自适应 SL
3. Kelly 仓位管理
4. 时段权重自适应
5. EMA 突破预警全平仓
6. 老师单偏置（teacher_bias）

### Cron Job 管理

```bash
# 查看所有 cron job
# 每周日 22:00 自动执行

# 手动触发一次
# (由系统自动调用 weekly_sync.py)
```

## 故障排查

### 问题：journal.jsonl 格式错误

```bash
# 检查 JSON 格式
python3 -c "import json; [json.loads(l) for l in open('/root/data/journal.jsonl') if l.strip()]"
```

### 问题：备份失败

```bash
# 检查磁盘空间
df -h /root/.hermes/backups/

# 手动备份
cp -r /root/skills/btc-gate-intraday-strategy/scripts/ \
      /root/.hermes/backups/manual-backup-$(date +%Y%m%d-%H%M%S)/
```

### 问题：Telegram 通知失败

```bash
# 检查环境变量
echo $TELEGRAM_BOT_TOKEN

# 手动测试
python3 weekly_sync.py
```

## 最佳实践

1. **每周复盘**：周日 22:00 自动执行，周一早上看周报
2. **数据驱动**：所有改动必须基于回测数据，不拍脑袋
3. **渐进优化**：每次只改 1-2 个参数，观察一周再继续
4. **备份优先**：同步前必须备份，出问题可以回滚
5. **范围纪律**：只动 filled=True 的单，未成交的 pending 不碰（2026-10-08 liusir 明确要求）

## 更新日志

- **2026-10-15 23:56**: 系统初始化，建立完整闭环
  - 创建 feedback_loop.py 和 weekly_sync.py
  - 配置周日 22:00 cron job
  - 首次分析发现 TP2_DONE 率过低问题

---

**维护者**: Hermes Agent  
**最后更新**: 2026-10-15
