#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
老师单偏置模块（v5-dev 改动 6 — 2026-10-13 liusir 拍板）
======================================================

目的:
  训练版 v5 信号生成时，参考老师同期信号的方向 / 时段 / score 阈值，
  让训练版「学着」贴近老师的开单决策。

数据来源:
  /root/data/teacher_signals/teacher_YYYY-MM-DD.jsonl
  /root/data/teacher_trades.jsonl

实现策略（liusir 2026-10-13 拍板 — 4 档匹配）:
  1. 同期同向（最严）: 当前信号 side == 老师当时钟点同向 long/short → bias = +1.0
  2. 同期反向（最该警惕）: 反向 → bias = -1.5（强行压住信号）
  3. 老师当天无单: 不偏置 → bias = 0
  4. 老师有单但时间窗不对（> 24h）: 弱偏置 → bias = +0.3（保守跟随）

用法:
    from teacher_bias import get_teacher_bias

    bias = get_teacher_bias(side='long', hour=22, date_str='2026-10-12')
    # → 'well' | 'oppose' | 'neutral' | 'weak'
    # 配合 teacher_bias_weight 配置项换算成加分 / 减分

设计取舍:
  - 只在训练版生效（生产版不读 teacher，避免 overfit）
  - 偏置只影响「方向倾向」，不强行反转（教师反对时也不抹掉信号）
  - 时间窗 24h 内才算「同期」（老师的意见有时效性）
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# 老师信号/交易 路径
DATA_DIR = "/root/data"
TEACHER_SIGNALS_DIR = os.path.join(DATA_DIR, "teacher_signals")
TEACHER_TRADES_FILE = os.path.join(DATA_DIR, "teacher_trades.jsonl")

# 时间窗（小时）：超过这个时长视为「老师同期无单」
TEACHER_WINDOW_HOURS = 24

# 偏置常量（直接给分用）
BIAS_WELL = 1.0      # 同期同向 — 大幅加分
BIAS_OPPOSE = -1.5   # 同期反向 — 大幅减分（强于同向，警示大于鼓励）
BIAS_NEUTRAL = 0.0   # 老师无单 — 不偏置
BIAS_WEAK = 0.3      # 时间窗过期 — 弱跟随


def _normalize_side(side: str) -> str:
    """归一化 side: 'long' / 'short' / 'buy' / 'sell' / 'LONG' 都返回 'long' 或 'short'"""
    s = side.lower().strip()
    if s in ("long", "buy", "l", "多", "做多"):
        return "long"
    if s in ("short", "sell", "s", "空", "做空"):
        return "short"
    return ""


def _parse_trade_ts(date_field: str, events: list = None) -> Optional[datetime]:
    """
    解析 teacher_trades.jsonl 的 date 字段.
    支持格式: "2026-10-12 22:00" / "2026-10-09 (持仓中)" / "2026-10-07 21:50"

    fallback: 如果 date 解析失败，从 events[0].ts 抽 "YYYY-MM-DD HH:MM"
    """
    if not date_field:
        if not events:
            return None
        date_field = events[0].get("ts", "")

    # 取前 16 字符 "YYYY-MM-DD HH:MM"
    head = date_field[:16].strip()
    try:
        return datetime.strptime(head, "%Y-%m-%d %H:%M")
    except ValueError:
        # 兜底: 只取日期
        try:
            return datetime.strptime(date_field[:10], "%Y-%m-%d")
        except ValueError:
            # 再兜底: 从 events[0].ts 抽 "YYYY-MM-DD HH:MM"
            if events:
                ev_ts = events[0].get("ts", "")
                import re
                m = re.match(r"(\d{4}-\d{2}-\d{2})\s*(\d{2}:\d{2})", ev_ts)
                if m:
                    return datetime.strptime(f"{m.group(1)} {m.group(2)}", "%Y-%m-%d %H:%M")
                m = re.match(r"(\d{4}-\d{2}-\d{2})", ev_ts)
                if m:
                    return datetime.strptime(m.group(1), "%Y-%m-%d")
            return None


def load_teacher_trades() -> List[Dict]:
    """读 /root/data/teacher_trades.jsonl → List[{date, side, ...}]"""
    p = Path(TEACHER_TRADES_FILE)
    if not p.exists():
        return []
    out = []
    for line in p.read_text().strip().split("\n"):
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        ts = _parse_trade_ts(rec.get("date", ""), rec.get("events"))
        if not ts:
            continue
        side = _normalize_side(rec.get("side", ""))
        if not side:
            continue
        out.append({
            "trade_id": rec.get("trade_id"),
            "ts": ts,
            "side": side,
            "entry_zone": rec.get("entry_zone"),
            "tp1": rec.get("tp1"),
            "tp2": rec.get("tp2"),
            "sl_initial": rec.get("sl_initial"),
            "outcome": rec.get("result", {}).get("outcome"),
            "net_usd": rec.get("result", {}).get("net_usd", 0),
        })
    return out


def load_teacher_signals_for_date(date_str: str) -> List[Dict]:
    """
    读 /root/data/teacher_signals/teacher_YYYY-MM-DD.jsonl
    date_str 格式: 'YYYY-MM-DD'
    """
    p = Path(TEACHER_SIGNALS_DIR) / f"teacher_{date_str}.jsonl"
    if not p.exists():
        return []
    out = []
    for line in p.read_text().strip().split("\n"):
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        side = _normalize_side(rec.get("side", ""))
        if not side:
            continue
        out.append({
            "ts": rec.get("ts"),
            "side": side,
            "entry": rec.get("entry"),
            "sl": rec.get("sl"),
            "tp1": rec.get("tp1"),
            "tp2": rec.get("tp2"),
            "note": rec.get("note", ""),
        })
    return out


def get_teacher_bias(side: str, hour: int, date_str: str) -> Tuple[str, float]:
    """
    计算老师偏置（liusir 4 档匹配规则 — 2026-10-13 拍板）

    输入:
      side: 'long' 或 'short'（当前训练版信号方向）
      hour: 0-23（当前小时）
      date_str: 'YYYY-MM-DD'（信号生成日期）

    输出:
      (label, score)
      label ∈ {'well', 'oppose', 'neutral', 'weak'}
      score  : 加到综合评分上的分（well=+1.0, oppose=-1.5, neutral=0, weak=+0.3）

    匹配规则:
      1. 老师当前 hour ± TEACHER_WINDOW_HOURS 小时内有同向单 → well
      2. 老师当前 hour ± TEACHER_WINDOW_HOURS 小时内有反向单 → oppose
      3. 老师当日 ± TEACHER_WINDOW_HOURS 内无单 → neutral
      4. 老师有同向单但超出 TEACHER_WINDOW_HOURS → weak
    """
    side = _normalize_side(side)
    if side not in ("long", "short"):
        return ("neutral", BIAS_NEUTRAL)

    # 当前信号时间
    try:
        cur_ts = datetime.strptime(f"{date_str} {hour:02d}:00", "%Y-%m-%d %H:%M")
    except ValueError:
        return ("neutral", BIAS_NEUTRAL)

    # 优先读 teacher_signals（更细粒度），回退到 teacher_trades
    signals = load_teacher_signals_for_date(date_str)
    trades = load_teacher_trades() if not signals else []

    candidates = signals if signals else trades
    if not candidates:
        return ("neutral", BIAS_NEUTRAL)

    # 把 candidates 转成 (datetime, side) 列表
    items: List[Tuple[datetime, str]] = []
    for c in candidates:
        if isinstance(c.get("ts"), (int, float)):
            # teacher_signals 格式 ts 是 unix 秒
            ts = datetime.fromtimestamp(c["ts"])
        elif c.get("ts"):
            ts = c["ts"]
        else:
            continue
        items.append((ts, c["side"]))

    # 找 TEACHER_WINDOW_HOURS 小时内最近的同向 / 反向单
    cutoff_lo = cur_ts - timedelta(hours=TEACHER_WINDOW_HOURS)
    cutoff_hi = cur_ts + timedelta(hours=TEACHER_WINDOW_HOURS)

    well_within = None
    oppose_within = None
    well_latest = None

    for ts, t_side in items:
        same_side = (t_side == side)
        if cutoff_lo <= ts <= cutoff_hi:
            if same_side and (well_within is None or ts > well_within):
                well_within = ts
            elif not same_side and (oppose_within is None or ts > oppose_within):
                oppose_within = ts
        else:
            # 时间窗外: 记最近一次同向单（弱跟随用）
            if same_side and (well_latest is None or ts > well_latest):
                well_latest = ts

    # 优先级: 同向 (in window) > 反向 (in window) > 时间窗外同向 > 无
    if well_within:
        return ("well", BIAS_WELL)
    if oppose_within:
        return ("oppose", BIAS_OPPOSE)
    if well_latest:
        return ("weak", BIAS_WEAK)
    return ("neutral", BIAS_NEUTRAL)


# ============================ 测试 ============================
if __name__ == "__main__":
    # 1. 加载老师单看看
    print("=== teacher_trades.jsonl ===")
    trades = load_teacher_trades()
    for t in trades:
        print(f"  #{t['trade_id']} {t['ts']} {t['side']} "
              f"entry={t['entry_zone']} outcome={t['outcome']} net={t.get('net_usd', 0):+.2f}U")

    # 2. 测几个时间点
    print("\n=== 偏置查询测试 ===")
    test_cases = [
        # (side, hour, date_str, 期望)
        ("long", 22, "2026-10-12", "trade_id=6 short 82920 — 反向"),
        ("long", 21, "2026-10-08", "trade_id=3 long 81755 — 同向"),
        ("short", 21, "2026-10-08", "trade_id=3 long — 反向"),
        ("long", 12, "2026-10-13", "近期无单 — neutral"),
    ]
    for side, hour, date, comment in test_cases:
        label, score = get_teacher_bias(side, hour, date)
        print(f"  {side}@{date} {hour:02d}:00 → {label:<8} ({score:+.2f})  [{comment}]")