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
  9. PLAN_DRIFT       实际和计划不符
  10. OTHER           其他

放在 journal.mistake 字段（list 类型，可多个）。
feedback_loop.py 按 mistake 归因统计（最常犯的前 3 名）。
"""
from __future__ import annotations
from typing import List, Dict, Optional
from collections import Counter

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
    "PLAN_DRIFT": "实际和计划不符",
    "OTHER": "其他",
}

# outcome → 默认 mistake
OUTCOME_TO_MISTAKE = {
    "MISSED": ["OVERTHINKING"],
    "SL_STOPPED": ["ANTI_STOP"],  # 默认，但可以更细
    "BE_STOPPED": ["NO_BREAKEVEN"],  # 打保本说明 TP1 后没跑
}


def classify_mistake(journal_rec: Dict) -> List[str]:
    """
    根据 outcome / note / 自动判断 mistake 类型
    返回 mistake 列表（可多个）
    """
    outcome = journal_rec.get("outcome", "")
    note = (journal_rec.get("note") or "").lower()
    existing = journal_rec.get("mistake") or []
    if existing:
        return existing  # 已有手动标记

    mistakes = []

    # 按 outcome 默认
    if outcome in OUTCOME_TO_MISTAKE:
        mistakes.extend(OUTCOME_TO_MISTAKE[outcome])

    # 按 note 关键词识别
    if "未进场" in note or "超过" in note and "阈值" in note:
        mistakes = ["OVERTHINKING"]  # MISSED 通常是过度分析
    elif "抗单" in note or "没推保本" in note:
        mistakes.append("ANTI_STOP")
    elif "追" in note and ("高" in note or "空" in note):
        mistakes.append("CHASING")

    # 按 sl_pct / tp1_pct 比例判断
    sl_pct = journal_rec.get("sl_pct", 0)
    tp1_pct = journal_rec.get("tp1_pct", 0)
    if sl_pct and tp1_pct:
        rr = abs(tp1_pct) / sl_pct if sl_pct > 0 else 0
        if rr < 1.0:
            mistakes.append("PLAN_DRIFT")  # TP1 < SL，R:R 不合理

    return mistakes or ["OTHER"]


def summarize_mistakes(journal: List[Dict], top_n: int = 5) -> Dict:
    """
    统计最常犯的 mistake
    返回: {
      "total_with_mistake": int,
      "by_type": [{"type": "ANTI_STOP", "label": "抗单不止损", "count": 5, "pct": 25.0}],
      "top3": [...]
    }
    """
    counter = Counter()
    total = 0
    for r in journal:
        m = classify_mistake(r)
        if m and m != ["OTHER"]:
            total += 1
            for t in m:
                counter[t] += 1

    by_type = []
    for t, n in counter.most_common():
        by_type.append({
            "type": t,
            "label": MISTAKE_TYPES.get(t, t),
            "count": n,
            "pct": round(n / total * 100, 1) if total > 0 else 0,
        })

    return {
        "total_with_mistake": total,
        "by_type": by_type,
        "top3": by_type[:3],
    }


def format_mistake_summary(summary: Dict) -> str:
    if not summary or not summary.get("by_type"):
        return "无错误分类数据"
    lines = [
        f"**错误归因**（共 {summary['total_with_mistake']} 笔有效标记）",
    ]
    for m in summary.get("top3", []):
        lines.append(f"  {m['label']} ({m['type']}): {m['count']} 笔 ({m['pct']}%)")
    return "\n".join(lines)
