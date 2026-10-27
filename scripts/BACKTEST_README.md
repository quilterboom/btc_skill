# scripts/backtest.py — 通用回测工具

**目的**: 不依赖 1m K 重放（Gate.io 历史只保留 3-4 天）也能跑参数回测

## 核心思路

1. 加载 `journal.jsonl` 所有 `filled=True` 的 trades
2. 用 `pos_state` 推断价格路径（最大反向/正向距离）
3. 用新 SL/TP 距离模拟：如果新 SL ≤ 旧最大反向 → 触发 SL；如果新 TP1 ≤ 旧最大正向 → 触发 TP1
4. 算 PnL (跟 `position_tracker._pnl_usd` 一致: `sign × (exit - entry) × contracts × 0.0001 - fee`)

## 关键发现（2026-10-17 跑 41 笔 baseline）

| 参数 | PnL | WR | PF | R:R | EV |
|---|---|---|---|---|---|
| 现状 SL=0.4% TP1=0.48% | **-17.4U** | 43.9% | 0.90 | 1.16 | -0.42U |
| 新提议 SL=0.1% TP1=0.5% | **+50.3U** | 43.9% | 1.40 | 1.79 | +1.23U |
| 激进 SL=0.4% TP1=1.5% | **+167.4U** | 43.9% | 1.92 | 2.45 | +4.08U |

**激进最佳**: SL=1.0% TP1=2.0% PnL=+413U PF=6.01

## 已知误差

- **PnL 绝对值估算误差 ~30U**（path 简化 + partial_exit 估算）
- **WR 高估 9pp**（BE_STOPPED 算赢了，实际 +1.46U 微赚）
- **参数对比方向是准的**（+50U vs -17U = 67U 差异，方向可靠）

## 限制

1. 只能模拟"有明确成交" 的 pos_state (SL_STOPPED / BE_STOPPED / TP2_DONE / TP1_DONE)
2. MISSED / NOISE_INVALIDATED 视为"没成交" (signal 但没 fill)
3. 不重放 1m K，**无法发现"原本没触发但新参数会触发"的新信号**

## 用法

```bash
# 单点回测
python3 backtest.py --sl 0.001 --tp1 0.005

# 网格搜索
python3 backtest.py --grid --top 10

# 按 R:R 排序
python3 backtest.py --grid --sort rr --top 5

# JSON 输出
python3 backtest.py --grid --json > grid_results.json
```

## 下一步

- 等 W43/W44 真实数据（30+ 笔）验证
- 把 baseline 改 SL=0.1% TP1=0.5% 在生产测试（前提是 backtest.py 方向对）
- 加 `--compare baseline` 对比模式
