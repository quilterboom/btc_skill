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


# ============================================================
# 亏损原因归因（2026-11-01 liusir：错误归因卡的数据 = 亏损策略的原因分析，
#               这些原因就是优化策略的输入）
# ============================================================
#
# 与上面的「操作错误」分类不同，这里回答的是「这笔为什么亏」——
# 每笔亏损交易判**一个主因**（互斥，分布之和 = 亏损笔数），判定全部基于事实：
#   - 账本事实：side × tags.trend（逆势）/ outcome × net（打满/回吐）/ sl_pct×tp1_pct
#   - K 线上下文：24h 区间位置（追单，§7.5 方向归一）/ 止损后 12h 走势（假止损，§4.5）
# 每个原因对应一条优化杠杆（入场过滤 / 趋势过滤 / 止损放置 / 出场管理），
# 但杠杆上不上线必须另行回测（没数字不下结论）。
CAUSE_TYPES = {
    "ANTI_STOP": "抗单不止损",
    "CHASE_ENTRY": "追单入场（追高/杀跌）",
    "COUNTER_TREND": "逆势入场（与排列相反）",
    "FALSE_STOP": "止损被噪音扫掉",
    "BE_ROUNDTRIP": "利润回吐（保本打回）",
    "PLAN_DRIFT": "止损比止盈远",
    "FULL_STOP": "方向错（止损打满）",
    "OTHER": "其他",
}

# 判定阈值（改前先看注释里的依据）：
# 保本打回的亏损只该是手续费级别（实测 -0.86~-3U）；亏到 -5U 以下的
# 「BE_STOPPED」实际是止损打满（outcome 不预测 net 符号，见分析 skill §0）
_BE_SMALL_LOSS = -5.0
# 追单：距 24h 同向极值 < 20% 区间（§7.5 方向归一 —— long 贴高=追高、short 贴低=杀跌）
_CHASE_NEAR = 0.2
# 假止损：止损后 12h 内价格回触 TP1（§4.5 判「假止损」用 SL 后回 TP1）
_FALSE_STOP_H = 12
# 24h 窗口 K 线少于 20 根不判（覆盖不足宁可不判，不猜）
_MIN_BARS = 20


def _detect_chase(rec: Dict, bars: Optional[list]) -> bool:
    """追单入场：entry 贴近 24h 的「同向极值」——long 贴高点（追高）、short 贴低点（杀跌）。

    ★ 方向归一（§7.5）：两个方向语义相反，绝不能用统一的 price_pos 阈值。
    bars: [(ts,o,h,l,c,v), ...] 升序；只用 ts 之前已收的 K 线（§1.5 锚点纪律）。
    """
    if not bars:
        return False
    ts = _f(rec.get("fill_ts") or rec.get("ts"))
    entry = _f(rec.get("entry"))
    if ts is None or entry is None:
        return False
    win = []
    for b in bars:
        t, h, l = _f(b[0]), _f(b[2]), _f(b[3])
        if t is None or h is None or l is None:
            continue
        if (ts - 86400) <= t < ts:
            win.append((h, l))
    if len(win) < _MIN_BARS:
        return False
    hi = max(w[0] for w in win)
    lo = min(w[1] for w in win)
    rng = hi - lo
    if rng <= 0:
        return False
    side = rec.get("side")
    if side == "long":
        return (hi - entry) / rng < _CHASE_NEAR      # 追高：贴着 24h 高点进
    if side == "short":
        return (entry - lo) / rng < _CHASE_NEAR      # 杀跌追空：贴着 24h 低点进
    return False


def _detect_false_stop(rec: Dict, bars: Optional[list]) -> bool:
    """假止损：SL 被扫后 12h 内价格回触 TP1（止损偏紧/被噪音扫，不是方向错）。"""
    if rec.get("outcome") != "SL_STOPPED" or not bars:
        return False
    exit_ts = _f(rec.get("exit_ts"))
    tp1 = _f(rec.get("tp1"))
    if exit_ts is None or tp1 is None:
        return False
    end = exit_ts + _FALSE_STOP_H * 3600
    side = rec.get("side")
    for b in bars:
        t = _f(b[0])
        if t is None or t <= exit_ts or t > end:
            continue
        h, l = _f(b[2]), _f(b[3])
        if h is None or l is None:
            continue
        if side == "long" and h >= tp1:               # high 回触 TP1
            return True
        if side == "short" and l <= tp1:              # low 回触 TP1
            return True
    return False


def classify_loss_cause(rec: Dict, bars: Optional[list] = None) -> List[str]:
    """给一笔亏损交易判「亏损主因」（单选，按优先级取第一个命中）。

    盈利交易返回 []（错误归因卡只归因亏损）。
    优先级：执行错误(抗单) > 进场时机(追单) > 进场逻辑(逆势) >
            止损放置(假止损) > 出场管理(回吐) > 计划配置(PLAN_DRIFT) > 方向错(打满)。
    """
    net = _f(rec.get("net_usd"))
    if not rec or net is None or net >= 0:
        return []
    if _detect_anti_stop(rec):
        return ["ANTI_STOP"]
    if _detect_chase(rec, bars):
        return ["CHASE_ENTRY"]
    tg = rec.get("tags") or {}
    side, trend = rec.get("side"), tg.get("trend")
    if (side == "long" and trend == "空头排列") or (side == "short" and trend == "多头排列"):
        return ["COUNTER_TREND"]
    if _detect_false_stop(rec, bars):
        return ["FALSE_STOP"]
    if rec.get("outcome") == "BE_STOPPED" and net >= _BE_SMALL_LOSS:
        return ["BE_ROUNDTRIP"]
    if _detect_plan_drift(rec):
        return ["PLAN_DRIFT"]
    # BE_STOPPED 亏超 -5U 的（上面 BE_ROUNDTRIP 没接住）实际是止损打满，
    # 与 SL_STOPPED 同归「方向错」——outcome 不预测 net 符号（§0）
    if rec.get("outcome") in ("SL_STOPPED", "BE_STOPPED"):
        return ["FULL_STOP"]
    return ["OTHER"]


def summarize_loss_causes(journal: List[Dict], bars: Optional[list] = None) -> Dict:
    """亏损原因分布（训练错误归因卡的数据源）。

    返回: {
      "n_losses": 亏损笔数, "trade_count": 分母（已结算可信）,
      "by_cause": [{"type","label","count","pct","net_usd"}],   # 按 count 降序
      "per_trade": [{"id","cause","label","net_usd"}],          # 逐笔记账（供明细/复核）
    }
    """
    rows = [r for r in (journal or []) if _is_trade(r)]
    losses = [r for r in rows if (_f(r.get("net_usd")) or 0) < 0]
    counter: Counter = Counter()
    nets: Dict[str, float] = defaultdict(float)
    per_trade = []
    for r in losses:
        cause = (classify_loss_cause(r, bars) or ["OTHER"])[0]
        net = _f(r.get("net_usd")) or 0.0
        counter[cause] += 1
        nets[cause] += net
        per_trade.append({
            "id": r.get("id"),
            "cause": cause,
            "label": CAUSE_TYPES.get(cause, cause),
            "net_usd": round(net, 2),
        })
    n = len(losses)
    by_cause = [{
        "type": t,
        "label": CAUSE_TYPES.get(t, t),
        "count": c,
        "pct": round(c / n * 100, 1) if n else 0,
        "net_usd": round(nets[t], 2),
    } for t, c in counter.most_common()]
    return {
        "n_losses": n,
        "trade_count": len(rows),
        "by_cause": by_cause,
        "per_trade": per_trade,
    }
