#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
strategy_truth.py — 平台策略单的唯一真相口径（2026-10-30 新增）

**为什么单独一个模块**：`web/server.py` 里策略单有两条数据线，之前互不知情：
  ① 列表  → `_all_settled()` 读 position_events.jsonl（events）
  ② 汇总  → `_winrate()` 也读 events，但过滤规则不同
两条线口径不一致 → 同一屏上「列表合计」和「汇总数字」对不上（实测差 271U）。
本模块把口径**收敛成一份**，列表和汇总都从这里出，两者必然自洽。

**真相源 = journal.jsonl**（events 只是通知流，可能过期 —— 见
btc-position-ops skill 的 references/2026-10-25-dashboard-winrate-caliber.md）。

口径（照抄那份 reference 的「正确的统计口径」+ 本次补充的三条）：

  1. 真相源 = journal，不是 events
  2. 只算真实成交且已结算：status=settled 且 net_usd 有值
     且 outcome 不在 (MISSED / NOISE_INVALIDATED / DUPLICATE_BLOCKED)
     且不是测试 id（_is_test_id）
  3. 剔双胞胎：tags.duplicate_of 标签 **或** 同 (fill_ts, fill_px, side) 三元组
     **或** 同 (entry, exit_px, net_usd, contracts) 四元组（后加，覆盖
     fill_ts 不同但其余全同的幽灵单）
  4. ★ 剔「出场数据被重写」的记录（2026-10-30 新增）
     —— 见下方 `_is_rewritten_exit()` 的签名说明
  5. 赢 = net_usd > 0（不看 category —— BE_STOPPED 的净额常为负，
     按 category 判赢会虚高胜率）
  6. 持仓中 = status=pending 且 filled=True 且 net_usd 为空

**第 4 条为什么必须加**（实测 4 笔生产单被污染，合计虚高 +208U）：
2026-10-27 14:56-14:57，risk_guard 在 settler 卡死时反复重写持仓的
exit_px/exit_ts，把它们写成「重写那一刻」的市价。这 4 笔的 exit_px 全部
≈ 79618（当时市价），而真实出场在 10-11 23:56 / 10-22。
连带后果：**净值被重算成虚高值**（如 1791692114 真值 +37.16 → 假值 +89.43），
因为重写后的 exit_px 与真实出场价不同。
识别签名（3 条同时成立才判定污染，避免误伤正常单）：
  a) exit_px 与 entry 方向矛盾（多头出场价 < 入场价 0.5% 以上，却记为盈利）
  b) exit_ts 距最后一次触发（tp1/tp2 hit）> 12h
  c) exit_px ≈ 同一批次重写时刻的市价（exit_ts 落在同一 5 秒窗口内）
被剔除的记录**仍然照常出现在列表里**（用户要看到它们），只是不计入汇总，
并在 `excluded` 里列出 id + 原因，供前端显示「N 笔数据存疑，未计入统计」。

**不改账本**：本模块只读不算写，净值修正属于账本操作，由
btc-position-ops 的流程负责。
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Iterable, Optional

DATA_DIR = Path("/root/data")

# 训练环境账本
TRAIN_JOURNAL = Path("/root/data/training/journal.jsonl")

# 「exit_ts 被重写」的判定阈值（小时）—— 与 scripts/pnl_calendar.py 的
# _EXIT_TS_SUSPECT_HOURS 保持一致，两处口径必须相同
EXIT_TS_SUSPECT_HOURS = 12

# 「出场价偏离入场价超过这个比例」即视为方向矛盾（0.5%）
EXIT_PRICE_CONTRADICT_PCT = 0.005


def _read_jsonl(path: Path) -> list:
    if not path.exists():
        return []
    out = []
    try:
        for ln in path.read_text(encoding="utf-8").splitlines():
            ln = ln.strip()
            if not ln:
                continue
            try:
                out.append(json.loads(ln))
            except json.JSONDecodeError:
                continue
    except OSError:
        return []
    return out


def is_test_id(rid: str) -> bool:
    """测试/合成 id 判定（与 web/server.py::_is_test_id 同口径，单独复制以免循环 import）。

    合成 id 形如 BTC_USDT-1000-short —— 数字段不是 10 位 unix 秒 → 判为测试数据。
    """
    rid = rid or ""
    if rid.startswith("TEST") or rid.startswith("test-"):
        return True
    if "TEST" in rid.upper() and rid.upper().startswith("BTC_USDT-TEST"):
        return True
    m = re.match(r"^[A-Za-z_]+-(\d+)-", rid)
    if m:
        try:
            return int(m.group(1)) < 1_000_000_000
        except ValueError:
            return True
    return False


def _is_rewritten_exit(rec: dict, rewrite_window: Optional[int] = None,
                       check_batch: bool = True) -> bool:
    """★ 判定「exit_px/exit_ts 被 risk_guard 重写」（2026-10-30）

    3 条签名同时成立才判定污染：

    a) 方向矛盾：多头出场价低于入场价 0.5% 以上，却记为盈利（net_usd > 0）
       —— 真实盈利单不可能以低于入场价 0.5% 的价格出场。
       （空头反向：出场价高于入场价 0.5% 以上却记为盈利）
    b) 时间矛盾：exit_ts 距最后一次触发（tp2_hit_ts / tp1_hit_ts）超过 12h
    c) 批次指纹：exit_ts 与「同一批次其它被重写的记录」落在同一时间窗口内
       —— 单笔看没有参照物，由调用方按批次聚合后传入 rewrite_window。

    参数 rewrite_window: 该笔的 exit_ts 所属批次窗口起点（秒）。
                         为 None 时签名 c 不成立（调用方没做批次聚合时）。
    参数 check_batch:  False = 只验 a/b 两条（批次聚合阶段自用，
                       避免自我循环 —— 聚合需要先知道谁是嫌疑）。
    """
    net = rec.get("net_usd")
    entry = rec.get("entry")
    exit_px = rec.get("exit_px")
    exit_ts = rec.get("exit_ts")
    if net is None or entry is None or exit_px is None or exit_ts is None:
        return False
    try:
        entry = float(entry)
        exit_px = float(exit_px)
        exit_ts = int(exit_ts)
        net = float(net)
    except (TypeError, ValueError):
        return False
    if net <= 0:
        return False  # 亏损单不适用（出场价低于入场价是正常的）

    # a) 方向矛盾
    side = rec.get("side")
    if side == "long":
        if exit_px >= entry * (1 - EXIT_PRICE_CONTRADICT_PCT):
            return False
    elif side == "short":
        if exit_px <= entry * (1 + EXIT_PRICE_CONTRADICT_PCT):
            return False
    else:
        return False

    # b) 时间矛盾
    last_trigger = max(int(rec.get("tp2_hit_ts") or 0), int(rec.get("tp1_hit_ts") or 0))
    if not last_trigger:
        return False
    if exit_ts - last_trigger <= EXIT_TS_SUSPECT_HOURS * 3600:
        return False

    # c) 批次指纹
    if not check_batch:
        return True
    if rewrite_window is None:
        return False
    return abs(exit_ts - rewrite_window) <= 300


def _batch_windows(records: Iterable[dict]) -> dict:
    """找出「exit_ts 集中重写」的批次。

    判据分两步：
      ① 先找出满足「方向矛盾 + 时间矛盾」两条签名的**嫌疑记录**
         （见 `_is_rewritten_exit` 的 a/b 两条）
      ② 把嫌疑记录按 exit_ts 聚集 —— **±300 秒内出现 ≥2 笔嫌疑**即判定为
         同一批重写（它们的 exit_px 是「重写那一刻」的市价，不是各自真实出场价）

    注意：聚集用的是**时间**不是 exit_px —— 实测同一批 4 笔的 exit_px
    分散在 79618.7 ~ 79624.6（差 6 点），按 px 聚类会漏；按时间聚类
    则 4 笔全部落在 23 秒内，一网打尽。

    返回 {id: 批次窗口起点（该组最早嫌疑记录的 exit_ts）}。
    """
    suspects = []
    for r in records:
        if _is_rewritten_exit(r, None, check_batch=False):
            suspects.append(r)
    out: dict = {}
    # 按 exit_ts 排序后做 ±300s 滑动窗口聚类
    suspects.sort(key=lambda z: int(z.get("exit_ts") or 0))
    i = 0
    while i < len(suspects):
        j = i
        base_ts = int(suspects[i].get("exit_ts") or 0)
        while j + 1 < len(suspects) and int(suspects[j + 1].get("exit_ts") or 0) - base_ts <= 300:
            j += 1
        group = suspects[i:j + 1]
        if len(group) >= 2:
            for r in group:
                out[r.get("id")] = base_ts
        i = j + 1
    return out


def truth_records(journal_path: Path = DATA_DIR / "journal.jsonl") -> tuple:
    """读账本 → 返回 (可信记录列表, 持仓中数量, 剔除明细)

    可信记录列表每项是**原始 journal 记录**（不加工），调用方自己取字段。
    剔除明细 = [{id, reason}]，reason ∈ {"twin", "rewritten_exit", "invalid"}
    """
    recs = _read_jsonl(Path(journal_path))
    if not recs:
        return [], 0, []

    # ── 第 1 层：过滤出「真实成交且已结算」 ──
    pool = []
    pending = 0
    excluded: list = []
    for r in recs:
        rid = r.get("id") or ""
        if not rid:
            continue
        if r.get("status") == "pending":
            if r.get("filled") and r.get("net_usd") is None:
                pending += 1
            continue
        if r.get("status") != "settled":
            continue
        if r.get("net_usd") is None:
            continue
        if r.get("outcome") in ("MISSED", "NOISE_INVALIDATED", "DUPLICATE_BLOCKED"):
            continue
        if is_test_id(rid):
            continue
        pool.append(r)

    # ── 第 2 层：剔「出场数据被重写」的记录 ──
    windows = _batch_windows(pool)
    keep: list = []
    for r in pool:
        win = windows.get(r.get("id"))
        if _is_rewritten_exit(r, win):
            excluded.append({"id": r.get("id"), "reason": "rewritten_exit"})
            continue
        keep.append(r)

    # ── 第 3 层：剔双胞胎 ──
    seen3: set = set()
    seen4: set = set()
    final: list = []
    for r in sorted(keep, key=lambda z: int(z.get("ts") or 0)):
        tg = r.get("tags") or {}
        k3 = (r.get("fill_ts"), r.get("fill_px"), r.get("side"))
        k4 = (r.get("entry"), r.get("exit_px"), r.get("net_usd"), r.get("contracts"))
        is_twin = bool(tg.get("duplicate_of"))
        if not is_twin and k3[0] is not None and k3 in seen3:
            is_twin = True
        if not is_twin and k4[0] is not None and k4 in seen4:
            is_twin = True
        if is_twin:
            excluded.append({"id": r.get("id"), "reason": "twin"})
            continue
        seen3.add(k3)
        seen4.add(k4)
        final.append(r)

    return final, pending, excluded


def summary(records: list) -> dict:
    """汇总口径：赢 = net_usd > 0；分母 = 已结算可信笔数。

    返回 {total, settled, pending, net_usd, wins, losses, win_rate}
    win_rate 用 "wins/settled" 分数形式（跟老师单面板一致，不是百分比）。
    """
    net = round(sum(float(r.get("net_usd") or 0) for r in records), 2)
    wins = sum(1 for r in records if float(r.get("net_usd") or 0) > 0)
    losses = sum(1 for r in records if float(r.get("net_usd") or 0) < 0)
    return {
        "total": len(records),
        "settled": len(records),
        "wins": wins,
        "losses": losses,
        "net_usd": net,
        "win_rate": f"{wins}/{len(records)}" if records else "0/0",
        "winrate_pct": round(wins / len(records) * 100, 1) if records else None,
        "basis": "net_usd>0",
    }


def strategy_truth(journal_path: Optional[Path] = None) -> dict:
    """平台策略单一次性取全部真相（列表 + 汇总同源）。

    返回 {
      records: [原始 journal 记录],   # 已结算可信单
      pending: int,                    # 持仓中
      excluded: [{id, reason}],       # 被剔除的（双胞胎 / 数据存疑）
      summary: {...},
    }
    """
    path = Path(journal_path) if journal_path else (DATA_DIR / "journal.jsonl")
    records, pending, excluded = truth_records(path)
    s = summary(records)
    s["pending"] = pending
    s["total"] = len(records) + pending
    return {
        "records": records,
        "pending": pending,
        "excluded": excluded,
        "summary": s,
    }


if __name__ == "__main__":
    import sys
    for label, p in (("prod", DATA_DIR / "journal.jsonl"), ("training", TRAIN_JOURNAL)):
        t = strategy_truth(p)
        s = t["summary"]
        print(f"=== {label} ===")
        print(f"  已结算可信 {s['settled']} 笔 ｜ 持仓中 {s['pending']} ｜ 总单数 {s['total']}")
        print(f"  累计净利 {s['net_usd']}U ｜ 胜率 {s['win_rate']} ({s['winrate_pct']}%)")
        print(f"  剔除 {len(t['excluded'])} 笔: "
              + ", ".join(f"{e['id']}({e['reason']})" for e in t["excluded"]))
