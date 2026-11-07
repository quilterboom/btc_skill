#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
backfill_ghost_fills.py —— 幽灵成交（从未发生的交易）账本标记（2026-11-06）

## 问题
hammer 信号（signal_source=left_side_oversold_bounce）的 preview 窗口语义是
「等反弹确认的监测窗」，生命周期归 **HammerWatcher** 独占：
  · ticker ≥ hammer_L × 1.003 → 秒级市价成交（_fill_method=market_on_watcher）
  · 窗口到期没到触发线 → cancelled / HAMMER_EXPIRED（**本来就没这单**）

但通用 promoter（journal.promote_due_previews）会把 hammer 信号也 promote
成 pending，抢在 watcher 的过期作废之前 → watcher 之后只认 preview_pending，
再也等不到作废 → 信号掉进 position_tracker 的**限价单**路径：回放 1m K 线
看到 low ≤ entry 就「成交」（成交价 = 计划限价）→ **凭空多一笔从未发生的交易**。

## 识别签名（`cost_basis_view.is_ghost_fill`，按可靠性排序）
1. `fill_ts < promoted_ts` —— 成交发生在被推进之前，**物理不可能**，铁证
2. `meta.hammer_managed` 且 `_fill_method != market_on_watcher` 却有 fill_px
3. note 含 `auto-promoted after preview` + signal_source=left_side_oversold_bounce

## 实测影响（本次修复前的账本）
- 生产 6 笔 / 训练 7 笔 = 13 笔幽灵成交
- 生产已被 strategy_truth 计入 5 笔（-78.05U）→ 剔除后
- 训练 7 笔全部计入（-121.66U）→ 剔除后训练线从亏损翻正

**11-05 22:45 生产那笔尤其关键**：它是 11-05 唯一一笔已结算交易，
日报「实盘 journal」把它报成 -24.09U 亏损——**实际这单从未发生过**
（Gate 官方 1m K 线核对：hammer 触发线 79190.0 从未被触及，窗口内最高
只到 79108.0；成交价 79094.3 == 计划 entry；fill_ts 早于 promoted_ts）。

## 处置纪律（沿用 btc-position-ops 既有先例）
**不改写 net_usd / gross_usd / outcome / category** —— 账面值是当时的记录，
改写会把 bug 固化进账本。改为：
  1. 补 `data_quality` 注记：说明成因 + 证据（触发线 vs 窗口内最高价）
  2. 补 `ghost_fill(v1)` 标记（同 exit_ts_stale(v1) / pnl_basis_stale(v1) 命名风格）
  3. 统计口径层（web/strategy_truth 等）**从统计里剔除**，但记录仍照常出现在
     列表里（用户要看到它们），标「数据存疑」角标

## 用法
    python3 backfill_ghost_fills.py            # dry-run，只报告不写
    python3 backfill_ghost_fills.py --apply    # 真正标记（先自动备份）
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

from cost_basis_view import is_ghost_fill  # noqa: E402

JOURNALS = [
    Path("/root/data/journal.jsonl"),
    Path("/root/data/training/journal.jsonl"),
]

MARK = "ghost_fill(v1)"

# 注记幂等：重跑替换同名注记，不追加重复
_NOTE_HEAD = "[2026-11-06 幽灵成交标记 · 不改净值]"


def _read_jsonl(path: Path):
    if not path.exists():
        return []
    out = []
    for ln in path.read_text(encoding="utf-8").splitlines():
        ln = ln.strip()
        if not ln:
            continue
        try:
            out.append(json.loads(ln))
        except json.JSONDecodeError:
            continue
    return out


def _write_jsonl(path: Path, recs: list) -> None:
    """原子写（tmp + replace）。"""
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        for r in recs:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    tmp.replace(path)


def _build_note(rec: dict) -> str:
    """构造注记（幂等：同名注记整段替换）。"""
    meta = rec.get("meta") or {}
    ft, pt_ = rec.get("fill_ts"), rec.get("promoted_ts")
    bits = [
        f"{_NOTE_HEAD} hammer 信号被通用 promoter 抢跑 promote（note 含 "
        f"'auto-promoted after preview'），watcher 的过期作废被跳过，"
        f"信号掉进限价单路径被「补」出一笔从未发生的成交。"
    ]
    if ft and pt_:
        bits.append(
            f"铁证：fill_ts({int(ft)}) 早于 promoted_ts({int(pt_)})——"
            f"成交发生在信号被推进之前，物理不可能。"
        )
    if meta.get("hammer_managed") and rec.get("_fill_method") != "market_on_watcher":
        bits.append(
            f"hammer 信号的成交只能来自 watcher 的秒级市价"
            f"（_fill_method=market_on_watcher），本条 _fill_method="
            f"{rec.get('_fill_method')!r}、fill_px={rec.get('fill_px')}"
            f"（== 计划 entry {rec.get('entry')}）→ 成交价是「计划限价」不是真实成交价。"
        )
    hL = meta.get("hammer_L")
    if hL:
        bits.append(
            f"hammer 触发线 = hammer_L × 1.003 = {round(float(hL) * 1.003, 1)}，"
            f"需 1m K 线最高价触及才算成交；本条从未经 watcher 触发。"
        )
    bits.append(
        f"账面 net_usd={rec.get('net_usd')} 保留不改（审计线索）；"
        f"该笔已从所有统计口径剔除，等 liusir 拍板是否重建真值。"
    )
    return " ".join(bits)


def _apply_note(rec: dict, note: str) -> bool:
    """写注记 + 标记。返回 True 表示有变化。"""
    changed = False
    dq = rec.get("data_quality") or ""
    # 幂等：先剥掉旧注记，再拼新的
    lines = [ln for ln in dq.split("\n") if not ln.startswith(_NOTE_HEAD)]
    lines.append(note)
    new_dq = "\n".join(lines).strip()
    if new_dq != dq:
        rec["data_quality"] = new_dq
        changed = True
    if rec.get("_dq_marks") != MARK:
        rec["_dq_marks"] = MARK
        changed = True
    return changed


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="真正写盘（默认 dry-run）")
    args = ap.parse_args()

    total = 0
    for path in JOURNALS:
        if not path.exists():
            print(f"[skip] {path} 不存在")
            continue
        recs = _read_jsonl(path)
        hits = [r for r in recs if is_ghost_fill(r)]
        print()
        print(f"=== {path}  共 {len(recs)} 条，幽灵成交 {len(hits)} 条 ===")
        changed_recs = []
        for r in hits:
            note = _build_note(r)
            changed = _apply_note(r, note)
            if changed:
                changed_recs.append(r)
            print(f"  {r.get('id'):32} net={r.get('net_usd')} "
                  f"outcome={r.get('outcome')}  "
                  f"{'[改]' if changed else '[已标记]'}")
        gnet = sum(float(r.get('net_usd') or 0) for r in hits)
        print(f"  幽灵成交账面合计: {gnet:+.2f}U（这部分将从统计中剔除）")
        total += len(hits)

        if args.apply and changed_recs:
            ts = int(time.time())
            bak = path.with_suffix(path.suffix + f".bak-{ts}")
            shutil.copy2(path, bak)
            _write_jsonl(path, recs)
            print(f"  ✅ 已写入 {len(changed_recs)} 条；备份 {bak}")

    print()
    print(f"合计幽灵成交: {total} 笔")
    if not args.apply:
        print("（dry-run，未写盘；加 --apply 生效）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
