"""
v2_decision_journal.py — 训练 vs 实盘 两条独立流水线
====================================================

liusir 2026-10-13 拍板：
"我希望实盘和训练的是两条开仓方法，不要互相干扰"

设计：
  ┌─────────────────────────────────────────────────────────────┐
  │ 实盘路径（liusir 真金）                                       │
  ├─────────────────────────────────────────────────────────────┤
  │ 触发:                                                         │
  │   1) watch.py (os_main / 15m_pattern) ← 真实 gate 信号        │
  │   2) liusir 口头单 ← 老师行情/盘感                            │
  │                                                              │
  │ 落点: /root/data/journal.jsonl                               │
  │ 资金: 50U 本金 × 100x                                        │
  │ 模式: cross, TP1 减半 + 推保本                                │
  └─────────────────────────────────────────────────────────────┘
                            ↓↑ 完全独立
  ┌─────────────────────────────────────────────────────────────┐
  │ 训练路径（v2.1 AI 全权，不下真单）                            │
  ├─────────────────────────────────────────────────────────────┤
  │ 触发:                                                         │
  │   1) 15min 一次 cron 跑 trader_view.py (v2.1 判定)            │
  │   2) 每次判定若 norm ≥ 0.20 → 写入训练 journal                │
  │                                                              │
  │ 落点: /root/data/training/v2_decisions.jsonl                  │
  │ 资金: 0 (纯模拟)                                              │
  │ 模式: AI 自己累积数据，不干扰实盘                             │
  └─────────────────────────────────────────────────────────────┘

写入格式（每条 = 一次 v2.1 判定，无论是否触发开仓）：
{
    "decision_id": "v2_20261013_150000",
    "ts": "2026-10-13 15:00:00",
    "ts_epoch": 1791870000,
    "bias": "long",                 # long/short/neutral
    "norm": 0.285,                  # 综合 norm 分数
    "composite_score": 1.14,        # composite score
    "consensus": "strong_long",     # 三周期一致性
    "main_a_score": 2,              # 趋势
    "main_b_score": 2,              # 动量
    "main_c_score": 0,              # 波动率
    "triggered": true,              # 是否触发（norm ≥ 0.20 且 consensus 不为不一致）
    "trigger_reason": "norm 0.285 ≥ 0.20 / consensus strong_long",
    "snapshot": {
        "px": 83530,
        "4h_trend": "多头排列",
        "1h_macd": "金叉初显",
        "15m_rsi": 55,
        ...
    }
}
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime
from pathlib import Path

# 训练判定落盘位置（跟实盘 journal 完全分开）
TRAINING_DECISIONS_PATH = Path("/root/data/training/v2_decisions.jsonl")
TRAINING_DECISIONS_PATH.parent.mkdir(parents=True, exist_ok=True)

# 阈值（v2.1 稳定点配置）
V2_1_BASELINE = {
    "norm_threshold": 0.20,
    "consensus_cap": 0.05,
    "trend_reversal_cap": 0.10,
    "rsi_neutral_range": (25, 40),
}


def record_v2_decision(
    bias: str,
    norm: float,
    composite_score: float,
    consensus: str,
    main_a_score: float,
    main_b_score: float,
    main_c_score: float,
    snapshot: dict,
    forced_trigger: "bool | None" = None,
) -> dict:
    """
    记录一次 v2.1 判定到训练 journal。

    参数：
        bias: "long" / "short" / "neutral"
        norm: 综合 norm 分数
        composite_score: composite score
        consensus: "strong_long" / "weak_long" / "inconsistent" / "weak_short" / "strong_short"
        main_a_score: 趋势分
        main_b_score: 动量分
        main_c_score: 波动率分
        snapshot: 市场状态快照（可存任意字段）
        forced_trigger: 强制触发（None 时按规则算）

    返回：
        写入的 decision dict
    """
    if forced_trigger is None:
        triggered = (
            norm >= V2_1_BASELINE["norm_threshold"]
            and consensus != "inconsistent"
        )
    else:
        triggered = forced_trigger

    trigger_reason = (
        f"norm {norm:.3f} ≥ {V2_1_BASELINE['norm_threshold']} / consensus {consensus}"
        if triggered
        else f"norm {norm:.3f} < {V2_1_BASELINE['norm_threshold']} 或 consensus 不一致"
    )

    now = datetime.now()
    decision_id = f"v2_{now.strftime('%Y%m%d_%H%M%S')}"

    decision = {
        "decision_id": decision_id,
        "ts": now.strftime("%Y-%m-%d %H:%M:%S"),
        "ts_epoch": int(now.timestamp()),
        "bias": bias,
        "norm": round(norm, 4),
        "composite_score": round(composite_score, 2),
        "consensus": consensus,
        "main_a_score": round(main_a_score, 2),
        "main_b_score": round(main_b_score, 2),
        "main_c_score": round(main_c_score, 2),
        "triggered": triggered,
        "trigger_reason": trigger_reason,
        "snapshot": snapshot,
    }

    with open(TRAINING_DECISIONS_PATH, "a") as f:
        f.write(json.dumps(decision, ensure_ascii=False) + "\n")

    return decision


def load_recent_decisions(n: int = 50) -> list[dict]:
    """读最近 n 条训练判定（最新在前）。"""
    if not TRAINING_DECISIONS_PATH.exists():
        return []
    decisions = []
    with open(TRAINING_DECISIONS_PATH) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                decisions.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    decisions.sort(key=lambda d: d.get("ts_epoch", 0), reverse=True)
    return decisions[:n]


def stats_last_24h() -> dict:
    """最近 24h 训练判定统计。"""
    decisions = load_recent_decisions(n=10000)
    cutoff = time.time() - 24 * 3600
    recent = [d for d in decisions if d.get("ts_epoch", 0) >= cutoff]

    triggered = [d for d in recent if d.get("triggered")]
    longs = [d for d in triggered if d.get("bias") == "long"]
    shorts = [d for d in triggered if d.get("bias") == "short"]

    return {
        "window_hours": 24,
        "total_decisions": len(recent),
        "triggered": len(triggered),
        "triggered_long": len(longs),
        "triggered_short": len(shorts),
        "triggered_rate": f"{100 * len(triggered) / max(1, len(recent)):.1f}%",
    }


if __name__ == "__main__":
    # 测试：写一条假决策 + 读回
    print("写入测试决策...")
    d = record_v2_decision(
        bias="long",
        norm=0.285,
        composite_score=1.14,
        consensus="strong_long",
        main_a_score=2,
        main_b_score=2,
        main_c_score=0,
        snapshot={"px": 83530, "4h_trend": "多头排列", "1h_macd": "金叉初显", "15m_rsi": 55},
    )
    print(f"  decision_id={d['decision_id']}, triggered={d['triggered']}")
    print(f"  trigger_reason={d['trigger_reason']}")

    print("\n读最近 3 条:")
    for dec in load_recent_decisions(n=3):
        print(f"  {dec['ts']} | {dec['bias']:6s} | norm={dec['norm']:.3f} | {dec['consensus']:16s} | triggered={dec['triggered']}")

    print("\n24h 统计:")
    stats = stats_last_24h()
    for k, v in stats.items():
        print(f"  {k}: {v}")

    print(f"\n落盘位置: {TRAINING_DECISIONS_PATH}")
