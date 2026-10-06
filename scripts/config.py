#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
BTC skill 共享配置 + 准入规则
=============================
集中所有"散落在脚本里、互相引用、却没有任何声明的地方"的常量和守卫函数。
改一处，全局生效。

单一来源：
  - 准入距离（500 点）：scan.py / watch.py / journal.py 各自实现了一份
  - 默认 target（1000 点）：watch.py CLI 和 scan.py CLI 默认值不一致是已修复过的 bug
  - 默认 target（500 点）：watch.py CLI 的 --target 默认

任何脚本 import 此文件即可获得统一值。
"""
from __future__ import annotations
import os
from typing import Tuple, Optional


# ============================ 准入常量 ============================
# 同方向已有 pending/filled 时，新策略 entry 距离必须 ≥ 这个值才允许写 journal
ENTRY_MIN_DIST_PTS = 500


# ============================ 止盈/止损 ============================
# 默认单笔目标点数（TP3 硬顶）。scan.py / watch.py CLI 的默认值
DEFAULT_TARGET_PTS = 1000


# ============================ 跳空监测 ============================
# 量能异动后跳空监测窗口（秒）。注释里写 180，实际只跑 60。
# 改这里时同步改 watch.py:JumpTracker.WINDOW_SEC
JUMP_WINDOW_SEC = 60

# 跳空作废阈值（点）：窗口内任意单点 vs 触发价 |Δ| ≥ 此值 → 策略作废
JUMP_INVALIDATE_PTS = 1000.0


# ============================ 持仓结算 ============================
# Settler 巡检间隔（秒）。改这里时同步改 watch.py:_settler_loop 的 sleep
SETTLER_INTERVAL_SEC = 5

# 持仓最长保留（秒，超时强制平仓）
MAX_HOLD_SEC = 24 * 3600          # 24h
# 挂单最长保留（秒，未挂到则作废）
MAX_PEND_SEC = 48 * 3600          # 48h
# journal 同方向刷新冷却（秒）
JOURNAL_COOLDOWN_SEC = 3600       # 1h
# 2026-10-06 加：同方向 fill 后多少秒内禁止开新单（不论 entry 距离）
# 原因：10-04 凌晨 4 笔同结构被反复开、10-05 22:11+22:33 间隔 22 分钟连开两笔
# 原有 min_dist_pts=500 距离兜底拦不住"同一信号被反复扫到"的情况。
SAME_SIDE_BLOCK_SEC = 1800        # 30 分钟

# 2026-10-06 加：TP1 未触发 + 持仓 > 此时长 + 当前浮亏 → 主动减仓 50%
# 原因（最初提出）：回测里 6 笔"未触发"占 4h 窗口 + 漂亏贡献 -22 U。
# 1h 内若 TP1 仍未摸到 + 当前 close 在 entry 之下 → 减仓 50%，避免继续漂亏。
#
# 2026-10-06 回测后修正：
#   实测在 10-04~10-06 样本里这个规则反而 -10 U——震荡市把未来会触 TP1 的单
#   在浮亏时提前砍掉（笔 #5 / #13 都是 1h 时浮亏但后续涨了）。
#   用户决策：默认关闭（24h 兜底），保留实现供未来手动启用。
PARTIAL_CLOSE_AFTER_SEC = 24 * 3600  # 24h 兜底（默认不触发；改回 3600 启用）
PARTIAL_CLOSE_RATIO = 0.5            # 减仓比例（剩下一半继续观望）


# ============================ 准入守卫 ============================
def should_skip_new_signal(contract: str, side: str, new_entry: float,
                           min_dist_pts: float = ENTRY_MIN_DIST_PTS,
                           journal_path: Optional[str] = None,
                           same_side_block_sec: int = SAME_SIDE_BLOCK_SEC,
                           now: Optional[int] = None) -> Tuple[bool, str]:
    """
    检查新策略是否应该跳过（避免同方向 + entry 距离过近的策略堆叠）。

    活跃判定：仅 filled 算真正活跃；pending 超过 MAX_PEND_SEC 自动作废不计，
    避免老挂单无限挂时、后续信号被距离检查绕过导致多条 pending 并存。

    2026-10-06 加：同方向 fill 后 same_side_block_sec 秒内**必拒**（不论 entry 距离）。
    原来的 min_dist_pts=500 兜底拦不住"同一信号被反复扫到"——结构没变、价格小幅波动，
    scan 会在短时间内连续满足条件，每次都给一个新 ID。30 分钟同方向冷却解决这个问题。

    Args:
        contract: 合约名（如 "BTC_USDT"）
        side: "long" / "short"
        new_entry: 新策略的 entry price
        min_dist_pts: 距离阈值，默认 500 点
        journal_path: journal.jsonl 路径；None 时用默认路径
        same_side_block_sec: 同方向 fill 后多少秒内禁止开新单（默认 1800）
        now: 当前时间戳（测试可注入；默认 time.time()）

    Returns:
        (skip, reason): skip=True 表示应跳过；reason 是给用户的解释字符串
    """
    from pathlib import Path
    import json
    import time

    if journal_path is None:
        # 默认 DATA_DIR 是 gate_fetch.py 暴露的全局
        from gate_fetch import DATA_DIR
        journal_path = os.path.join(DATA_DIR, "journal.jsonl")

    if not os.path.exists(journal_path):
        return (False, "")

    try:
        with open(journal_path, encoding="utf-8") as f:
            recs = [json.loads(ln.strip()) for ln in f if ln.strip()]
    except Exception:
        return (False, "")

    # 活跃判定（2026-10-05 修正语义对齐 position_tracker）：
    #   status="pending" + filled=True → 已成交未平仓 → 算活跃
    #   status="pending" + filled=False → 挂单中 → pending 超 MAX_PEND_SEC 自动作废不计
    #   status="settled"/"invalidated" → 不算活跃
    _now = int(time.time()) if now is None else int(now)
    sim = []
    for r in recs:
        if r.get("contract") != contract or r.get("side") != side:
            continue
        st = r.get("status")
        is_filled = bool(r.get("filled"))
        if st == "pending" and is_filled:
            sim.append(r)
        elif st == "pending":
            ts = int(r.get("ts") or 0)
            if _now - ts <= MAX_PEND_SEC:
                sim.append(r)
            # else: 过期 pending，不计入
    if not sim:
        return (False, "")

    # ★ 2026-10-06 新增：同方向 fill 后 same_side_block_sec 秒内必拒（不论 entry 距离）
    for r in sim:
        ft = r.get("fill_ts")
        if ft is None:
            continue
        age = _now - int(ft)
        if 0 <= age < same_side_block_sec:
            mins = age // 60
            rid_short = (r.get("id") or "?")[:25]
            reason = (f"{mins} 分钟内已有同方向 fill 单（id={rid_short}），"
                      f"冷却 {same_side_block_sec // 60} 分钟内禁止再开同方向 → "
                      f"避免同一信号连续开仓")
            return (True, reason)

    near = min(sim, key=lambda r: abs(float(r.get("entry", 0)) - new_entry))
    dist = abs(new_entry - float(near.get("entry", 0)))
    if dist < min_dist_pts:
        reason = (f"同方向已有 {len(sim)} 条策略活跃，"
                  f"最近 entry={float(near['entry']):.1f} 距离新策略 {dist:.0f} 点 < {min_dist_pts:.0f}")
        return (True, reason)
    return (False, "")


def env_or(name: str, default):
    """从环境变量读配置，未设则用 default（用于 BTC_NO_JOURNAL 这类开关）"""
    return os.environ.get(name, default)
