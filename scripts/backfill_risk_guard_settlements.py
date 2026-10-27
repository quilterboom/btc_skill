#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
backfill_risk_guard_settlements — 补齐 risk_guard SL 分支漏写的 canonical 结算
=============================================================================

**为什么需要**（2026-10-25 排查）：
risk_guard.py 的 SL 分支历史上只写 exit_ts/exit_px/status/outcome/pos_state/
risk_guard_exit_*，**不写** net_usd/gross_usd/category，也**不 append** outcome event
到 position_events.jsonl —— 即整条 settle_all 链（r.update(res) + _append_event）被绕过。
后果：
  · daily_report.simulate_teacher_TP 用 `e.get('net_usd', 0) or 0` → null net 当 0
    计入 v4.2 净利（实测 10-24 报告把 23:15 那笔 -15.12U 算成 0，v4.2 净虚高到 49.38）
  · web/server.py._winrate 只认 position_events.jsonl 里的 outcome 事件 → 胜率漏统计

**口径**：一律复用 position_tracker._close（FEE_USD=4.85 / QUANT_MULT=0.0001 / PARTS=3），
不在本脚本里抄公式。与 risk_guard._canonical_close 同一条代码路径。

**幂等**：
  · journal 字段：已有 net_usd+gross_usd+category 则跳过改写（只补缺字段，不覆盖）
  · outcome event：position_tracker.has_outcome_event(id) 查重后才 append
  · 跑两遍不会重复记录 / 重复事件（scripts/tests/test_settle_sl_guard.py 有回归）

**数据隔离**：必须显式设 BTC_DATA_DIR（prod=/root/data，训练=/root/data/training），
脚本会 assert 它是已知目录之一，避免把 prod 数字写进训练 journal 或反之。

用法：
    BTC_DATA_DIR=/root/data            python3 backfill_risk_guard_settlements.py --side prod
    BTC_DATA_DIR=/root/data/training   python3 backfill_risk_guard_settlements.py --side train
    ... --dry-run   # 只算不写
"""
from __future__ import annotations
import argparse, json, os, shutil, sys, time
from pathlib import Path

KNOWN_DATA_DIRS = {"/root/data", "/root/data/training"}

# 目标：7 条（prod 4 + train 3），逐条列出，禁止"批量扫 null net"误伤别的记录。
# 注：handoff 说"6 条 = 原 5 条减 1792594829 加 2 条新"，但 1792594829 从来不在原 5 条里
#     （它是单独的排除项）→ 实际动过的记录是 5 + 2 = 7 条。
TARGETS = {
    "prod": [
        "BTC_USDT-1792854921-short",   # 真实止损：1m high 80257 ≥ SL 80175.7
        "BTC_USDT-1792699207-short",   # TP1 后被保本打掉
        "BTC_USDT-1792594815-long",    # 10-22 手动结算 —— 只补 event，net 不动
        "BTC_USDT-1792868423-short",   # 10-25 07:32 risk_guard 真实止损（handoff 后新增）
    ],
    "train": [
        "BTC_USDT-1792854928-short",   # 真实止损
        "BTC_USDT-1792702834-short",   # TP1 后被保本打掉
        "BTC_USDT-1792863032-short",   # 10-25 10:41 risk_guard 真实止损（handoff 后新增）
    ],
}

# ★ 绝不回填：canonical _close 在这条上会丢最后一段（EMA 415 + TP1 197 = 612 > 原始 593 张），
#   对 exit 81888.1 和 84333.0 都算出同一个 gross 9.2 —— 公式本身在这条上失真；
#   且它 10-22 07:05（low 84275）就该打保本 84333，实际拖到 16:53 才在 81888.1 出场，属异常。
#   → 等真实数据，一条数字都不写。
#   ★ 这条**故意**保持 null_net —— 下次扫 null-net 仍会看到它是**正确**的，不要"顺手修掉"。
NEVER_BACKFILL = {"BTC_USDT-1792594829-long": "awaiting real data"}

# 这条只补 outcome event：liusir 10-22 明文「TP1 段 197 张 +7.97U 扣 fee」→ net 3.12。
# canonical _close 却给 4.35（把 EMA 减仓段 415 张和 TP1 段 197 张重复计，612>593）。
# 差 1.23U 属已知口径差，**不改存储值**。
EVENT_ONLY = {"BTC_USDT-1792594815-long"}


def _load_journal(path: Path):
    recs = []
    for ln in path.read_text(encoding="utf-8").splitlines():
        ln = ln.strip()
        if not ln:
            continue
        try:
            recs.append(json.loads(ln))
        except Exception:
            continue
    return recs


def _write_journal(path: Path, recs):
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        for r in recs:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    os.replace(tmp, path)


def _backup(path: Path, tag: str) -> Path:
    """写盘前的强制备份：<file>.bak-<ts>，并确认落盘。"""
    if not path.exists():
        raise FileNotFoundError(f"备份失败，源文件不存在: {path}")
    dst = path.with_name(f"{path.name}.bak-{tag}")
    shutil.copy2(path, dst)
    if not dst.exists() or dst.stat().st_size != path.stat().st_size:
        raise IOError(f"备份校验失败: {dst}（大小不一致或不存在）")
    return dst


def main() -> int:
    ap = argparse.ArgumentParser(description="补齐 risk_guard 漏写的 canonical 结算")
    ap.add_argument("--side", required=True, choices=["prod", "train"])
    ap.add_argument("--dry-run", action="store_true", help="只算不写")
    args = ap.parse_args()

    # ★ 必须在 import gate_fetch / position_tracker **之前**确定 DATA_DIR，
    #   这两个模块在 import 时就把 DATA_DIR 早绑定（后续 setenv 无效）。
    data_dir = os.environ.get("BTC_DATA_DIR", "")
    if data_dir not in KNOWN_DATA_DIRS:
        print(f"[aborted] BTC_DATA_DIR 未显式设置或非法: {data_dir!r}\n"
              f"          必须是 {sorted(KNOWN_DATA_DIRS)} 之一（prod/train 隔离）",
              file=sys.stderr)
        return 2
    expected = "/root/data" if args.side == "prod" else "/root/data/training"
    if data_dir != expected:
        print(f"[aborted] side={args.side} 与 BTC_DATA_DIR={data_dir} 不匹配"
              f"（应为 {expected}）", file=sys.stderr)
        return 2

    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from position_tracker import has_outcome_event, _append_event
    from watch.risk_guard import _canonical_close

    journal = Path(data_dir) / "journal.jsonl"
    events = Path(data_dir) / "position_events.jsonl"
    if not journal.exists():
        print(f"[aborted] journal 不存在: {journal}", file=sys.stderr)
        return 2

    print(f"[backfill] side={args.side} data_dir={data_dir}")
    tag = time.strftime("%Y%m%d-%H%M%S")

    # 备份（dry-run 不写盘，但也先备份以便对照？—— 不：dry-run 完全不动数据）
    if not args.dry_run:
        jb = _backup(journal, tag)
        print(f"[backup] {jb}  ({jb.stat().st_size} bytes)")
        if events.exists():
            eb = _backup(events, tag)
            print(f"[backup] {eb}  ({eb.stat().st_size} bytes)")

    recs = _load_journal(journal)
    by_id = {}
    for r in recs:
        by_id.setdefault(r.get("id"), r)

    report: dict = {"backfilled": [], "event_only": [], "skipped": [],
                    "awaiting": [], "missing": []}
    changed = False

    for rid in TARGETS[args.side]:
        if rid in NEVER_BACKFILL:
            report["awaiting"].append({"id": rid, "status": NEVER_BACKFILL[rid],
                                       "written": "NOTHING"})
            print(f"[awaiting] {rid} → {NEVER_BACKFILL[rid]}（一条数字都不写）")
            continue

        r = by_id.get(rid)
        if r is None:
            report["missing"].append(rid)
            print(f"[missing] {rid} 不在 journal，跳过")
            continue

        event_only = rid in EVENT_ONLY
        # journal 字段幂等：三项都齐了就不再改写
        fields_ok = (r.get("net_usd") is not None and r.get("gross_usd") is not None
                     and r.get("category"))

        if event_only:
            # net/category 用**已存储**的值（不动数据），只补事件
            net, gross, cat = r.get("net_usd"), r.get("gross_usd"), r.get("category")
            if net is None or cat is None:
                print(f"[aborted] {rid} 标记为 event_only 但缺 net/category，"
                      f"拒绝猜测，跳过", file=sys.stderr)
                return 3
            note = (f"stored net={net} cat={cat}（liusir 10-22 口径）；"
                    f"canonical _close 会给 net=4.35 —— 差 1.23U，不改存储值")
            print(f"[event-only] {rid} {note}")
            report["event_only"].append({"id": rid, "net_usd": net, "category": cat,
                                         "note": note})
        else:
            if fields_ok:
                print(f"[skip-fields] {rid} 已有 net/gross/category，不覆盖")
                net, gross, cat = r["net_usd"], r.get("gross_usd"), r["category"]
            else:
                res = _canonical_close(r, float(r.get("exit_px") or 0),
                                       int(r.get("exit_ts") or 0))
                net, gross, cat = res["net_usd"], res["gross_usd"], res["category"]
                if not args.dry_run:
                    # 只补缺字段；已有的（如 net）保持原值不覆盖
                    if r.get("net_usd") is None:
                        r["net_usd"] = net
                    if r.get("gross_usd") is None:
                        r["gross_usd"] = gross
                    if not r.get("category"):
                        r["category"] = cat
                    changed = True
                print(f"[{'would-write' if args.dry_run else 'write'}] {rid} "
                      f"gate={r.get('risk_guard_exit_px')} gross={gross} net={net} cat={cat}")
                report["backfilled"].append({"id": rid, "gross_usd": gross,
                                             "net_usd": net, "category": cat,
                                             "exit_px": r.get("exit_px"),
                                             "exit_ts": r.get("exit_ts")})

        # outcome event（幂等）
        if has_outcome_event(rid):
            print(f"[skip-event] {rid} 已有 outcome event")
            report["skipped"].append(rid)
            continue
        ev = {"id": rid, "outcome": r.get("outcome"), "net_usd": net,
              "category": cat, "exit_ts": r.get("exit_ts"), "ts": int(time.time())}
        if args.dry_run:
            print(f"[would-append-event] {json.dumps(ev, ensure_ascii=False)}")
        else:
            _append_event(ev)
            print(f"[append-event] {json.dumps(ev, ensure_ascii=False)}")

    if changed and not args.dry_run:
        _write_journal(journal, recs)
        print(f"[saved] {journal}")
    elif not args.dry_run:
        print(f"[journal] 无字段改动（事件按需单独追加）")

    out = Path(data_dir) / f"backfill_risk_guard-{tag}.json"
    # ★ 显式区分「故意留 null」vs「还没回填」——避免下一个人把前者当漏网重新标记。
    null_scan = {"intentionally_left_null": [], "still_unbackfilled": []}
    for r in _load_journal(journal):
        if r.get("status") != "settled" or r.get("net_usd") is not None:
            continue
        rid = r.get("id")
        if rid in NEVER_BACKFILL:
            null_scan["intentionally_left_null"].append(
                {"id": rid, "why": NEVER_BACKFILL[rid]})
        else:
            null_scan["still_unbackfilled"].append(
                {"id": rid, "why": "不在 TARGETS 里，未处理"})
    report["null_net_scan"] = null_scan
    if not args.dry_run:
        out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[saved] {out}")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    print("\n===== null-net 复核（区分「故意」与「未处理」）=====")
    for x in null_scan["intentionally_left_null"]:
        print(f"  [故意留 null]  {x['id']} —— {x['why']}（下次扫到是正常的，别修）")
    for x in null_scan["still_unbackfilled"]:
        print(f"  [未处理!]      {x['id']} —— {x['why']}")
    if not null_scan["still_unbackfilled"]:
        print("  ✅ 没有遗漏的 null-net 结算")
    return 0


if __name__ == "__main__":
    sys.exit(main())
