#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
backfill_hammer_cost_basis.py —— hammer 市价成交的账本真值回填（2026-11-04）

## 问题
hammer 走 `hammer_watcher` 秒级市价成交，成交价 `fill_px` 常高于计划入场价
`entry`（追价，实测 300~400 点）。但 `_close` 一直拿**计划 entry** 当成本基准算
PnL → 用没成交过的价格记账，账面系统性虚高。

实测（同一笔单，两种成本基准）：
    训练 5 笔：账面 +26.22U → 真实 -46.29U   （差 -72.51U）
    生产 8 笔：账面 +100.87U → 真实 -33.47U  （差 -134.34U）
典型 11-03 04:58（生产 BTC_USDT-1793649653-long）：
    计划 entry 76709，真实成交 77083.2（追价 374.2 点）
    账面 +14.00U（profit / BE_STOPPED）→ 真实 -10.39U（loss / SL_STOPPED）

## 处置纪律（沿用 btc-position-ops 既有 exit_ts_stale 先例）
**不改写 net_usd / gross_usd / outcome / category** —— 账面值是当时的事实记录，
直接改写会把 bug 固化进账本、丢掉审计线索。改为：
  1. 补 `net_usd_fill_basis` / `gross_usd_fill_basis`：按**真实成交价**用
     canonical `position_tracker._close` 重算（不手算、不信 sl_pct 字段）
  2. 补 `data_quality` 注记：口径陈旧（旧口径按计划 entry 记账）+ 三段式差额
  3. 补 `pnl_basis_stale(v1)` 标记（同 exit_ts_stale(v1) 命名风格）
统计口径层（web/strategy_truth）在展示时按 fill_basis 优先，并同时露出账面值，
供「账面 / 可信 / 差额」三段式对照。

## 用法
    python3 backfill_hammer_cost_basis.py            # dry-run，只报告不写
    python3 backfill_hammer_cost_basis.py --apply    # 真正回填（先备份）
"""
from __future__ import annotations
import argparse
import copy
import json
import os
import sys
import time
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

from position_tracker import _close as _pt_close, _contracts as _pt_contracts  # noqa: E402

JOURNALS = [
    Path("/root/data/journal.jsonl"),
    Path("/root/data/training/journal.jsonl"),
]

FORCE = False   # --force 时强制重算已回填记录（修口径 bug 后重跑用）


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


def _is_hammer_market_fill(rec: dict) -> bool:
    """判定需要重算的记录：hammer 市价成交 且 真实成交价 ≠ 计划入场价。"""
    if rec.get("_fill_method") != "market_on_watcher":
        return False
    fill_px = rec.get("fill_px")
    entry = rec.get("entry")
    if not fill_px or not entry:
        return False
    try:
        # 价差 < 1 点视为同一价（不需要重算）
        return abs(float(fill_px) - float(entry)) >= 1.0
    except (TypeError, ValueError):
        return False


def _recompute_fill_basis(rec: dict):
    """按真实成交价用 canonical _close 重算，返回 (net, gross, category, outcome)。

    _close 有幂等守卫（status=settled 且 net_usd 非空 → raise 拒绝二次结算），
    所以在**副本**上清掉 net_usd 再算，绝不碰原记录。
    """
    tmp = copy.deepcopy(rec)
    tmp["net_usd"] = None       # 绕过幂等守卫（我们就是要重算）
    tmp["status"] = "pending"
    entry = float(rec.get("entry") or 0)
    side = rec.get("side") or "long"
    contracts = int(rec.get("_orig_contracts") or rec.get("contracts") or 0)
    if contracts <= 0:
        # ★ 同 _settle_one 口径：没记张数就按 entry 推（50U 名义 → 整数张数），
        #   不再「无法重算」跳过。实测 11-04 09:00 两笔就是这种（contracts=null）。
        contracts = int(_pt_contracts(entry)) if entry > 0 else 0
    if contracts <= 0:
        return None
    res = _pt_close(
        tmp,
        rec.get("outcome") or "SL_STOPPED",
        float(rec.get("exit_px") or 0),
        int(rec.get("exit_ts") or 0),
        contracts,
        side,
        entry,          # entry 参数；_close 内部会用 _cost_basis() 取真实成交价
        rec.get("category") or "loss",
    )
    # ★ outcome 也按成本基准重判（_close 只回显传入的 outcome，不重判）。
    #   判据同 risk_guard._canonical_close：BE 成立 = 保本移动过 且 止损线在真实
    #   成本价 ±1 点内。旧记录 active_sl 推在**计划 entry**（低于真实成本），
    #   所以按成本基准它们不是「保本被打」而是实打实的亏损止损。
    cost = float(rec.get("fill_px") or entry)
    cur_sl = float(rec.get("active_sl") or rec.get("sl") or 0)
    be_moved = bool(rec.get("tp1_hit") or rec.get("ema_warning_done")
                    or rec.get("partial_close_done") or rec.get("guard_partial_done"))
    is_be = be_moved and abs(cur_sl - cost) < 1.0
    res["outcome_fill_basis"] = "BE_STOPPED" if is_be else (
        "SL_STOPPED" if (rec.get("outcome") or "").endswith("STOPPED")
        else (rec.get("outcome") or ""))
    return res


def backfill(path: Path, apply: bool) -> dict:
    recs = _read_jsonl(path)
    if not recs:
        return {"path": str(path), "n": 0}

    changed = []
    rows = []
    no_diff = []
    for rec in recs:
        if not _is_hammer_market_fill(rec):
            continue
        if rec.get("status") != "settled":
            # 未结算的（如训练 12:15 那笔 pending）不标记 —— 新代码结算时自动用真实成本
            continue
        if rec.get("net_usd_fill_basis") is not None and not FORCE:
            continue  # 已回填过，幂等（--force 时强制重算，修口径 bug 后回填用）

        res = _recompute_fill_basis(rec)
        if res is None:
            rows.append({"id": rec.get("id"), "err": "无法重算（缺 contracts）"})
            continue

        book_net = rec.get("net_usd")
        true_net = res["net_usd"]
        diff = None
        try:
            if book_net is not None:
                diff = round(float(true_net) - float(book_net), 2)
        except (TypeError, ValueError):
            diff = None

        rows.append({
            "id": rec.get("id"),
            "entry": rec.get("entry"),
            "fill_px": rec.get("fill_px"),
            "chase_pts": round(float(rec.get("fill_px")) - float(rec.get("entry")), 1),
            "book_net": book_net,
            "true_net": true_net,
            "diff": diff,
            "book_cat": rec.get("category"),
            "true_cat": res["category"],
            "book_outcome": rec.get("outcome"),
            "true_outcome": res["outcome_fill_basis"],
        })

        if not apply:
            continue

        # ★ 2026-11-13 修：**差额为 0 的单不写任何字段**。
        #   差额 = 0 说明「按真实成交价」和「按计划 entry」算出来的净值**完全相同**
        #   → 账本本来就是按真实成本记的（结算时代码已用 fill_px），**不是陈旧口径**。
        #   老写法给它们也盖「口径陈旧」章的三个后果：
        #     ① data_quality 谎称「按计划 entry 记账」（与事实相反）
        #     ② pnl_basis_stale(v1) 把 20 笔非陈旧单标成陈旧（审计线索被污染）
        #     ③ 展示层 is_stale() 为真 → 日报卡片打出「-2.04U (账面 -2.04U)」
        #        —— 同一个数印两遍，纯噪音（违反「恒等于 0 的指标不用正负表达」通则）
        #   实测 2026-11-13：生产 6 笔 + 训练 14 笔差额全为 0.00U，只有训练
        #   BTC_USDT-1793765732-long 真正陈旧（账面 +86.51 → 真实 +50.43，差 -36.08U）。
        if diff is None or abs(float(diff)) < 0.005:
            no_diff.append(rec.get("id"))
            continue

        # ── 回填（只加字段，不改写既有值）──
        rec["net_usd_fill_basis"] = res["net_usd"]
        rec["gross_usd_fill_basis"] = res["gross_usd"]
        rec["category_fill_basis"] = res["category"]
        rec["outcome_fill_basis"] = res["outcome_fill_basis"]
        rec["pnl_basis_stale"] = "v1"
        _dq = (
            f"【2026-11-04 成本基准陈旧 · 不改净值】本单为 hammer 秒级市价成交，"
            f"计划 entry={rec.get('entry')}、真实成交价 fill_px={rec.get('fill_px')}"
            f"（追价 {float(rec.get('fill_px')) - float(rec.get('entry')):+.1f} 点）。"
            f"账面 net_usd={book_net}（{rec.get('category')}/{rec.get('outcome')}，"
            f"按计划 entry 记账，口径陈旧）；按真实成交价 canonical 重算 "
            f"net_usd_fill_basis={true_net}"
            f"（{res['category']} / {res['outcome_fill_basis']}）。"
            f"差额 {diff:+.2f}U。净值不改写，展示口径按 fill_basis 优先。"
        )
        # 幂等：--force 重跑时替换旧的「成本基准陈旧」注记，不追加重复
        old_dq = rec.get("data_quality") or ""
        if "【2026-11-04 成本基准陈旧" in old_dq:
            parts = [p for p in old_dq.split("\n")
                     if "【2026-11-04 成本基准陈旧" not in p]
            old_dq = "\n".join(parts)
        rec["data_quality"] = (old_dq + "\n" + _dq) if old_dq else _dq
        changed.append(rec.get("id"))

    if apply and changed:
        tmp = path.with_suffix(".jsonl.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            for r in recs:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        tmp.replace(path)

    return {"path": str(path), "n": len(rows), "rows": rows,
            "changed": changed, "applied": apply,
            "no_diff": no_diff}


def main():
    global FORCE
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="真正回填（默认 dry-run）")
    ap.add_argument("--force", action="store_true",
                    help="强制重算已回填记录（修口径 bug 后重跑用）")
    args = ap.parse_args()
    FORCE = args.force

    if args.apply:
        ts = int(time.time())
        for p in JOURNALS:
            if p.exists():
                bak = p.with_suffix(f".jsonl.bak-costbasis-{ts}")
                bak.write_text(p.read_text(encoding="utf-8"), encoding="utf-8")
                print(f"备份 {p} → {bak}")

    total_book = total_true = 0.0
    for p in JOURNALS:
        r = backfill(p, args.apply)
        print(f"\n{'='*70}\n{r['path']}  ｜ 需重算 {r['n']} 笔")
        for row in r.get("rows", []):
            if "err" in row:
                print(f"  ⚠ {row['id']}: {row['err']}")
                continue
            print(f"  {row['id']}")
            print(f"    计划 entry {row['entry']:,.1f} → 真实成交 {row['fill_px']:,.1f}"
                  f"（追价 {row['chase_pts']:+,.1f} 点）")
            print(f"    账面 net {row['book_net']:+.2f}U ({row['book_cat']}/{row['book_outcome']})")
            print(f"    真实 net {row['true_net']:+.2f}U ({row['true_cat']}/{row['true_outcome']})")
            print(f"    差额 {row['diff']:+.2f}U")
            if row.get("book_net") is not None:
                total_book += float(row["book_net"])
                total_true += float(row["true_net"])
        if r.get("applied"):
            print(f"  ✅ 已回填 {len(r['changed'])} 笔（net_usd 未改写，只加 fill_basis 字段 + data_quality 注记）")

    print(f"\n{'='*70}")
    print(f"三段式合计（hammer 市价成交历史笔）:")
    print(f"  账面合计   {total_book:+.2f}U")
    print(f"  可信合计   {total_true:+.2f}U")
    print(f"  差额       {total_true - total_book:+.2f}U")
    print(f"\n{'⚠ dry-run（未写盘）' if not args.apply else '✅ 已写盘'}"
          f" —— 加 --apply 真正回填")


if __name__ == "__main__":
    main()
