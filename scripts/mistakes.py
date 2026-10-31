#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
mistakes.py — 交易错误分类（参考 LuxAlgo/trade-journal 的 mistakes tag 系统）

错误分类（基于 liusir 风格 + BTC 实战）：
  1. PREMATURE_ENTRY  过早入场（趋势没到位就进）
  2. LATE_ENTRY       过晚入场（追高/追空）
  3. ANTI_STOP        抗单不止损（不推保本）
  4. NO_BREAKEVEN     TP1 后没推保本
  5. OVERTHINKING     过度分析错过（MISSED）
  6. CHASING          追单（追空被轧 / 追多被砸）
  7. FOMO             FOMO 开单（无信号乱进）
  8. SIZE_TOO_BIG     仓位过大
  9. PLAN_DRIFT       止损比止盈远
  10. OTHER           其他

放在 journal.mistake 字段（list 类型，可多个）。
feedback_loop.py 按 mistake 归因统计（最常犯的前 3 名）。

★ 2026-10-31 重写：把「按 outcome 猜错误」改成「按事实判错误」。

  旧实现（错）：OUTCOME_TO_MISTAKE 把 outcome 直接映射成 mistake：
      SL_STOPPED  → ANTI_STOP    （抗单不止损）
      BE_STOPPED  → NO_BREAKEVEN  （TP1 后没推保本）
      MISSED      → OVERTHINKING  （过度分析错过）
  实测生产账本 93 行：
      「TP1 后没推保本」36 笔占 50.0%，是归因第一名。
      但这 36 笔的 active_sl **全部 == entry**（保本确实推了），
      合计净 +185.82（18 笔净>0，合计 +244.08）—— 赚钱的桶被当成错误。
      真正的「TP1 后没推保本」= 0 笔。
      13 笔 SL_STOPPED 也全部是按止损价成交的（纪律执行），
      却被标成「抗单不止损」。
  标签方向整个是反的：**打保本 = 保本推了**，**止损成交 = 止损纪律执行了**。

  新实现：每个 mistake 都要有**账本里的事实证据**才成立，
          检出条件写在 `_detect_*` 里，可被 tests/test_mistakes_caliber.py 验证。
          分母改成「已结算可信交易数」，不再把没成交/被剔除的行算进分母。
"""
from __future__ import annotations
from typing import List, Dict, Optional
from collections import Counter, defaultdict

# 错误分类
MISTAKE_TYPES = {
    "PREMATURE_ENTRY": "过早入场",
    "LATE_ENTRY": "过晚入场",
    "ANTI_STOP": "抗单不止损",
    "NO_BREAKEVEN": "TP1 后没推保本",
    "OVERTHINKING": "过度分析错过",
    "CHASING": "追单被套",
    "FOMO": "FOMO 开单",
    "SIZE_TOO_BIG": "仓位过大",
    "PLAN_DRIFT": "止损比止盈远",
    "OTHER": "其他",
}

# 不算「真实成交」的 outcome（不能当交易算进分母）
NON_TRADE_OUTCOMES = ("MISSED", "NOISE_INVALIDATED", "DUPLICATE_BLOCKED", "HAMMER_EXPIRED")

# 「出场价 == 止损价」的容差（点数 + 比例双保险）
_PRICE_EPS_ABS = 1.0
_PRICE_EPS_REL = 1e-4

# 判定「抗单不止损」的最少逆向幅度（0.5%）
_ANTI_STOP_MIN_ADVERSE = 0.005


def _f(v) -> Optional[float]:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _is_trade(rec: Dict) -> bool:
    """这一行算不算「真实成交的交易」。

    口径与 web/strategy_truth.py 对齐（只算已结算、净值有值、不是未成交/被剔除的）。
    """
    if not rec or not rec.get("id"):
        return False
    if rec.get("status") != "settled":
        return False
    if rec.get("net_usd") is None:
        return False
    if rec.get("outcome") in NON_TRADE_OUTCOMES:
        return False
    return True


def _px_eq(a, b) -> bool:
    a, b = _f(a), _f(b)
    if a is None or b is None:
        return False
    return abs(a - b) <= max(_PRICE_EPS_ABS, abs(b) * _PRICE_EPS_REL)


def _detect_no_breakeven(rec: Dict) -> bool:
    """真·「TP1 后没推保本」：TP1 命中了，但保护止损**没有**移到入场价。

    旧实现把 BE_STOPPED（保本被打回）整个当成这一类 —— 方向反了。
    正确判据只看两个字段：tp1_hit 为真 且 active_sl != entry。
    """
    if not rec.get("tp1_hit"):
        return False
    entry, active = _f(rec.get("entry")), _f(rec.get("active_sl"))
    if entry is None or active is None:
        return False
    return not _px_eq(active, entry)


def _detect_anti_stop(rec: Dict) -> bool:
    """真·「抗单不止损」：亏损出场，但出场价**不在**止损价上，且逆向幅度 > 0.5%。

    旧实现把 SL_STOPPED（按止损价成交）整个当成这一类 —— 方向反了。
    按计划止损 = 纪律执行，不该算错误。
    """
    net = _f(rec.get("net_usd"))
    if net is None or net >= 0:
        return False
    entry, exit_px, sl = _f(rec.get("entry")), _f(rec.get("exit_px")), _f(rec.get("sl"))
    if entry is None or exit_px is None:
        return False
    side = rec.get("side")
    if side == "long":
        adverse = (entry - exit_px) / entry
    elif side == "short":
        adverse = (exit_px - entry) / entry
    else:
        return False
    if adverse <= _ANTI_STOP_MIN_ADVERSE:
        return False
    # 出场价就是止损价 → 纪律执行，不算抗单
    if sl is not None and _px_eq(exit_px, sl):
        return False
    return True


def _detect_plan_drift(rec: Dict) -> bool:
    """真·「止损比止盈远」：|TP1%| / SL% < 1，即止盈目标比止损还近。

    这是**计划层面**的配置缺陷（不是逐笔操作失误），净值照算，
    所以归因时必须标清楚，别让人误以为是 34 笔手滑。
    """
    sl_pct, tp1_pct = _f(rec.get("sl_pct")), _f(rec.get("tp1_pct"))
    if not sl_pct or tp1_pct is None or sl_pct <= 0:
        return False
    return abs(tp1_pct) / sl_pct < 1.0


def classify_mistake(journal_rec: Dict) -> List[str]:
    """
    根据账本**事实**判断 mistake 类型。
    返回 mistake 列表（可多个）。手动标记优先。
    """
    if not journal_rec:
        return ["OTHER"]

    existing = journal_rec.get("mistake") or []
    if existing:
        return existing  # 已有手动标记

    mistakes: List[str] = []
    note = (journal_rec.get("note") or "").lower()

    if _detect_no_breakeven(journal_rec):
        mistakes.append("NO_BREAKEVEN")
    if _detect_anti_stop(journal_rec):
        mistakes.append("ANTI_STOP")
    if _detect_plan_drift(journal_rec):
        mistakes.append("PLAN_DRIFT")

    # note 关键词（保留原有行为，但不再覆盖已检出的事实类）
    if "未进场" in note or ("超过" in note and "阈值" in note):
        if "OVERTHINKING" not in mistakes:
            mistakes.append("OVERTHINKING")
    elif "抗单" in note or "没推保本" in note:
        if "ANTI_STOP" not in mistakes:
            mistakes.append("ANTI_STOP")
    elif "追" in note and ("高" in note or "空" in note):
        if "CHASING" not in mistakes:
            mistakes.append("CHASING")

    return mistakes or ["OTHER"]


def summarize_mistakes(journal: List[Dict], top_n: int = 5) -> Dict:
    """
    统计最常犯的 mistake。

    分母口径（★ 2026-10-31 修）：**已结算可信交易数**。
      旧实现把 journal 原始行（含 invalidated / MISSED / 被剔除的重复单）全塞进来，
      再把「标了非 OTHER 错误的行数」当分母 —— 生产实测 36/72 = 50.0%，
      而 36 笔里只有 0 笔真是「TP1 后没推保本」。
      现在分母 = 真实成交已结算的交易数，pct 才有意义。

    返回: {
      "total_with_mistake": int,     # 有真实错误标记的交易笔数
      "trade_count": int,             # 分母：已结算可信交易数
      "not_filled": int,              # 信号出了但没成交（单独列，不进分母）
      "total_net": float,             # 分母内这些交易的合计净值
      "by_type": [{"type","label","count","pct","net_usd"}],
      "top3": [...],
    }
    """
    rows = [r for r in (journal or []) if _is_trade(r)]
    not_filled = sum(1 for r in (journal or []) if r.get("outcome") == "MISSED")

    counter: Counter = Counter()
    nets: Dict[str, float] = defaultdict(float)
    trades_with_mistake = 0
    for r in rows:
        ms = classify_mistake(r)
        real = [m for m in ms if m != "OTHER"]
        if not real:
            continue
        trades_with_mistake += 1
        net = _f(r.get("net_usd")) or 0.0
        for t in real:
            counter[t] += 1
            nets[t] += net

    total = len(rows)
    by_type = []
    for t, n in counter.most_common():
        by_type.append({
            "type": t,
            "label": MISTAKE_TYPES.get(t, t),
            "count": n,
            "pct": round(n / total * 100, 1) if total else 0,
            "net_usd": round(nets[t], 2),
        })

    return {
        "total_with_mistake": trades_with_mistake,
        "trade_count": total,
        "not_filled": not_filled,
        "total_net": round(sum((_f(r.get("net_usd")) or 0.0) for r in rows), 2),
        "by_type": by_type,
        "top3": by_type[:3],
    }


def format_mistake_summary(summary: Dict) -> str:
    if not summary or not summary.get("by_type"):
        return "无错误分类数据"
    lines = [
        f"**错误归因**（{summary.get('total_with_mistake', 0)} 笔有问题 / "
        f"{summary.get('trade_count', 0)} 笔成交）",
    ]
    for m in summary.get("top3", []):
        net = m.get("net_usd", 0)
        lines.append(
            f"  {m['label']} ({m['type']}): {m['count']} 笔 ({m['pct']}%) "
            f"净值 {net:+.2f}U"
        )
    if summary.get("not_filled"):
        lines.append(f"  另有 {summary['not_filled']} 笔信号未成交（不计入分母）")
    return "\n".join(lines)
