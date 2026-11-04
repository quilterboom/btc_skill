#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
亏损原因聚合 —— 训练策略调整的输入源
=====================================

2026-11-03 liusir：「训练环境的策略调整输入还要包括生产环境亏损策略的原因」

**背景**：训练环境单策略化后（11-03 移除 15m_pattern），训练账本只剩 9 笔
（已结算 9、亏损 4）。样本量太小，**单靠训练账本推导不出可用的优化规则** ——
候选规则连「时间外推」这关都过不了（前半/后半各 2~4 笔）。

而生产账本同期有 **48 笔可信记录 / 27 笔亏损**，亏损主因分布清晰：
  利润回吐（保本打回）  13 笔  48.1%  -44.11U   ← 绝对主力
  方向错（止损打满）     4 笔  14.8%  -63.37U
  逆势入场              3 笔  11.1%  -42.57U
  追单入场              2 笔   7.4%  -40.26U
  止损被噪音扫掉         2 笔   7.4%  -41.07U
  抗单不止损            2 笔   7.4%  -66.29U
  止损比止盈远          1 笔   3.7%  -44.85U

**分工（本模块的定位）**：
  - 生产账本 → 提供**亏损原因的分布**（哪种错因最贵、占多少）
  - 训练账本 → 提供**同源可回测样本**（候选规则在训练环境验证）
  两边不混算：生产数字只作「错因优先级」输入，不进训练胜率/净利统计。

**口径纪律**：
  - 生产侧走 `strategy_truth.truth_records`（剔双胞胎 / 出场被重写 / 未成交），
    与 web 胜率卡同源，避免「训练页和生产页数字对不上」。
  - 亏损主因走 `mistakes.classify_loss_cause`（事实判据，不是按 outcome 猜），
    与 web 错误归因卡同源。
  - 生产账本**只读**，本模块不写任何生产文件。
"""
from __future__ import annotations

import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Dict, List, Optional

SKILL_ROOT = Path(__file__).resolve().parent.parent
if str(SKILL_ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(SKILL_ROOT / "scripts"))
# strategy_truth.py 在 web/ 目录（跟 web/server.py 同源），scripts 侧要显式加进去
if str(SKILL_ROOT / "web") not in sys.path:
    sys.path.insert(0, str(SKILL_ROOT / "web"))

PROD_JOURNAL = Path("/root/data/journal.jsonl")
TRAIN_JOURNAL = Path("/root/data/training/journal.jsonl")


# ---------------------------------------------------------------- 原因 → 优化杠杆
# 每种亏损主因对应的**入场侧可执行杠杆**（不是因果推断，是「这类错因能被什么
# 事前规则挡住」的映射）。杠杆要上线必须另跑回测（没数字不下结论）。
CAUSE_LEVERS = {
    "BE_ROUNDTRIP": {
        "lever": "出场管理：TP1 后保本 + 不回吐",
        "entry_filter": None,          # 出场侧问题，入场过滤挡不住
        "note": "保本被打回 = 止损位太松；要么入场价更近，要么 TP1 更早",
    },
    "FULL_STOP": {
        "lever": "方向过滤：止损打满 = 方向选错",
        "entry_filter": "counter_trend",   # 逆势单不入场
        "note": "止损打满几乎都是方向错，先看排列过滤能不能挡掉",
    },
    "COUNTER_TREND": {
        "lever": "方向过滤：与排列相反不入场",
        "entry_filter": "counter_trend",
        "note": "账本 tags.trend 与 side 相反 —— 可直接事前判",
    },
    "CHASE_ENTRY": {
        "lever": "入场过滤：追高/杀跌不入场",
        "entry_filter": "chase",
        "note": "entry 贴近 24h 同向极值；需 15m K 算区间位置",
    },
    "FALSE_STOP": {
        "lever": "止损放置：止损别被噪音扫",
        "entry_filter": None,
        "note": "SL 后 12h 回触 TP1 = 止损偏紧，需放宽或改结构位止损",
    },
    "ANTI_STOP": {
        "lever": "出场纪律：出场价必须贴着止损价",
        "entry_filter": None,
        "note": "亏损出场但不在 SL 价上 = 没按计划止损",
    },
    "PLAN_DRIFT": {
        "lever": "计划配置：R:R 必须 > 1",
        "entry_filter": "plan_drift",
        "note": "|TP1%| < SL% = 止盈比止损还近，结构上必亏",
    },
    "OTHER": {
        "lever": "待人工归因",
        "entry_filter": None,
        "note": "",
    },
}


def load_bars_15m() -> Optional[list]:
    """15m K 线（本地缓存，不走网络）。追单位置 / 假止损判定需要。

    缓存不可用时返回 None —— 归因仍可做，只是追单/假止损这两类
    会落到「其它主因」，**不猜**。
    """
    try:
        import kline_store
        import gate_fetch
        rows, _depth = kline_store.load_bars(gate_fetch.DATA_DIR, "BTC_USDT", "15m")
        return [(int(r[0]), float(r[1]), float(r[2]), float(r[3]),
                 float(r[4]), float(r[5])) for r in rows]
    except Exception:
        return None


def _prod_truth_records() -> List[Dict]:
    """生产可信记录（与 web 胜率卡同源）。"""
    try:
        import strategy_truth as st
        recs, _pending, _excl = st.truth_records(PROD_JOURNAL)
        return recs
    except Exception:
        # strategy_truth 不可用时退回宽松口径（至少要有净值和成交时间）
        out = []
        if PROD_JOURNAL.exists():
            for line in PROD_JOURNAL.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    r = __import__("json").loads(line)
                except Exception:
                    continue
                if (r.get("status") == "settled" and r.get("net_usd") is not None
                        and r.get("fill_ts")):
                    out.append(r)
        return out


def _train_truth_records() -> List[Dict]:
    try:
        import strategy_truth as st
        recs, _p, _e = st.truth_records(TRAIN_JOURNAL)
        return recs
    except Exception:
        import json
        out = []
        if TRAIN_JOURNAL.exists():
            for line in TRAIN_JOURNAL.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                except Exception:
                    continue
                if (r.get("status") == "settled" and r.get("net_usd") is not None
                        and r.get("fill_ts")):
                    out.append(r)
        return out


def prod_loss_causes(bars: Optional[list] = None) -> Dict:
    """生产亏损原因分布（训练策略调整的输入）。

    返回:
      {
        "n_losses": int, "trade_count": int,
        "by_cause": [{type,label,count,pct,net_usd}],
        "by_cause_source": [{type,label,source,count,net_usd}],  # 错因 × 信号源
        "levers": [{cause,label,lever,count,pct,net_usd,entry_filter,note}],
        "source_breakdown": {source: {n, losses, net_usd}},
        "note": str,
      }
    """
    from mistakes import summarize_loss_causes, CAUSE_TYPES
    recs = _prod_truth_records()
    if bars is None:
        bars = load_bars_15m()
    res = summarize_loss_causes(recs, bars)

    src_of = {r.get("id"): (r.get("signal_source") or "(无来源)") for r in recs}

    # 错因 × 信号源
    cross: Dict = defaultdict(lambda: {"count": 0, "net_usd": 0.0})
    for t in res.get("per_trade", []):
        k = (t["cause"], src_of.get(t.get("id"), "(无来源)"))
        cross[k]["count"] += 1
        cross[k]["net_usd"] += t.get("net_usd") or 0.0
    by_cause_source = [{
        "type": c, "label": CAUSE_TYPES.get(c, c), "source": s,
        "count": v["count"], "net_usd": round(v["net_usd"], 2),
    } for (c, s), v in sorted(cross.items(), key=lambda kv: -kv[1]["count"])]

    # 错因 → 杠杆（按笔数降序）
    levers = []
    for row in res.get("by_cause", []):
        meta = CAUSE_LEVERS.get(row["type"], CAUSE_LEVERS["OTHER"])
        levers.append({
            "cause": row["type"], "label": row["label"],
            "lever": meta["lever"], "entry_filter": meta["entry_filter"],
            "note": meta["note"],
            "count": row["count"], "pct": row["pct"], "net_usd": row["net_usd"],
        })

    # 按信号源的战绩拆分
    src_stat = defaultdict(lambda: {"n": 0, "losses": 0, "net_usd": 0.0})
    for r in recs:
        s = r.get("signal_source") or "(无来源)"
        n = r.get("net_usd")
        src_stat[s]["n"] += 1
        src_stat[s]["net_usd"] += float(n or 0)
        if n is not None and n < 0:
            src_stat[s]["losses"] += 1
    source_breakdown = {
        s: {"n": v["n"], "losses": v["losses"],
            "net_usd": round(v["net_usd"], 2)}
        for s, v in sorted(src_stat.items(), key=lambda kv: -kv[1]["n"])
    }

    res["by_cause_source"] = by_cause_source
    res["levers"] = levers
    res["source_breakdown"] = source_breakdown
    res["note"] = (
        "生产账本只读；错因分布作为训练环境策略调整的**输入**，"
        "不进训练账本的胜率/净利统计"
    )
    return res


def train_loss_causes(bars: Optional[list] = None) -> Dict:
    """训练账本亏损原因分布（同源可回测样本）。"""
    from mistakes import summarize_loss_causes
    recs = _train_truth_records()
    if bars is None:
        bars = load_bars_15m()
    return summarize_loss_causes(recs, bars)


def combined_input(bars: Optional[list] = None) -> Dict:
    """训练策略调整的**完整输入**：生产错因分布（优先级）+ 训练同源样本。

    返回:
      {
        "prod": {…生产亏损原因…},
        "train": {…训练亏损原因…},
        "priority_levers": [按生产笔数排序的杠杆 + 训练同源样本量],
        "sample_warning": str,
      }
    """
    if bars is None:
        bars = load_bars_15m()
    prod = prod_loss_causes(bars)
    train = train_loss_causes(bars)

    # 训练样本里每种错因各有几笔（用于判断「这条杠杆在训练环境能不能验」）
    train_by_cause = Counter(t["cause"] for t in train.get("per_trade", []))

    priority = []
    for lv in prod.get("levers", []):
        priority.append({
            **lv,
            "train_same_cause": train_by_cause.get(lv["cause"], 0),
            "train_loss_total": train.get("n_losses", 0),
        })

    prod_n = prod.get("n_losses", 0)
    train_n = train.get("n_losses", 0)
    warn = (
        f"生产亏损样本 {prod_n} 笔（归因优先级来源）"
        f"｜训练亏损样本 {train_n} 笔（候选规则验证来源）"
    )
    if train_n < 10:
        warn += " —— ★ 训练亏损样本 < 10 笔，候选规则在训练环境只能做方向性验证，"
        warn += "上线前必须在生产侧做影子回放"
    return {
        "prod": prod,
        "train": train,
        "priority_levers": priority,
        "sample_warning": warn,
    }


def format_combined(data: Dict) -> str:
    """人读摘要（终端输出用）。"""
    prod = data.get("prod", {})
    lines = []
    lines.append("=" * 92)
    lines.append("训练策略调整输入 · 生产亏损原因 + 训练同源样本")
    lines.append("=" * 92)
    lines.append("★ %s" % data.get("sample_warning", ""))
    lines.append("")
    lines.append("── 生产环境 · 亏损主因（策略调整的优先级来源）" + "─" * 30)
    for lv in prod.get("levers", []):
        lines.append("  %-14s %2d 笔 %5.1f%%  %+7.2fU  → %s" % (
            lv["label"], lv["count"], lv["pct"], lv["net_usd"], lv["lever"]))
    lines.append("")
    lines.append("  按信号源拆分：")
    for s, v in prod.get("source_breakdown", {}).items():
        lines.append("    %-26s %2d 笔（亏 %d）净 %+8.2fU" % (
            s, v["n"], v["losses"], v["net_usd"]))
    lines.append("")
    lines.append("── 错因 × 信号源" + "─" * 60)
    for r in prod.get("by_cause_source", []):
        lines.append("  %-14s %-26s %2d 笔 %+7.2fU" % (
            r["label"], r["source"], r["count"], r["net_usd"]))
    lines.append("")
    lines.append("── 训练环境 · 同源可回测样本" + "─" * 46)
    tr = data.get("train", {})
    lines.append("  亏损 %d / 已结算 %d 笔" % (
        tr.get("n_losses", 0), tr.get("trade_count", 0)))
    for r in tr.get("by_cause", []):
        lines.append("    %-14s %2d 笔 %5.1f%%  %+7.2fU" % (
            r["label"], r["count"], r["pct"], r["net_usd"]))
    lines.append("")
    lines.append("── 优化杠杆优先级（生产笔数排序）" + "─" * 44)
    for lv in data.get("priority_levers", []):
        lines.append("  %-14s %2d 笔 → %s" % (lv["label"], lv["count"], lv["lever"]))
        lines.append("      %s" % (lv.get("note") or ""))
        lines.append("      入场侧可执行过滤: %s ｜ 训练同源样本 %d 笔" % (
            lv.get("entry_filter") or "无（出场侧问题）",
            lv.get("train_same_cause", 0)))
    return "\n".join(lines)


if __name__ == "__main__":
    import json
    data = combined_input()
    print(format_combined(data))
    if "--json" in sys.argv:
        print()
        print(json.dumps(data, ensure_ascii=False, indent=1))
