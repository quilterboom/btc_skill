#!/usr/bin/env python3
"""
扫描 /root/data/BTC_USDT_*.json 缓存，产出存活清单。

只读，绝不删除。输出：
  1. 每个文件的 interval / total / 实际根数 / 最新 ts / 覆盖天数 / 文件大小
  2. 按 interval 聚合：谁覆盖最深、谁被别的完全覆盖
  3. 分类：LIVE（当前最新）/ STALE（落后 > 2 根）/ DEAD（被同 interval 更深且更新的文件覆盖）

用法:
  python3 audit_kline_cache.py            # 人读表格
  python3 audit_kline_cache.py --json     # 机读
"""
from __future__ import annotations

import argparse
import json
import os
import time
from collections import defaultdict

INTERVAL_SEC = {
    "10s": 10, "1m": 60, "5m": 300, "15m": 900, "30m": 1800,
    "1h": 3600, "2h": 7200, "4h": 14400, "6h": 21600, "8h": 28800,
    "1d": 86400,
}

DATA_DIR = "/root/data"
NOW = int(time.time())


def parse_name(fname: str):
    """BTC_USDT_15m_500.json -> ('BTC_USDT', '15m', 500)"""
    stem = fname[len("BTC_USDT_"):-len(".json")] if fname.startswith("BTC_USDT_") else None
    if stem is None:
        return None
    parts = stem.rsplit("_", 1)
    if len(parts) != 2:
        return None
    interval, total = parts[0], parts[1]
    if interval not in INTERVAL_SEC or not total.isdigit():
        return None
    return "BTC_USDT", interval, int(total)


def inspect(path: str):
    meta = parse_name(os.path.basename(path))
    if meta is None:
        return None
    _, interval, total = meta
    try:
        with open(path) as fh:
            data = json.load(fh)
    except Exception as exc:  # noqa: BLE001
        return {
            "file": os.path.basename(path), "interval": interval, "total": total,
            "bars": -1, "first_ts": None, "last_ts": None, "lag_bars": None,
            "span_days": None, "size_kb": round(os.path.getsize(path) / 1024, 1),
            "status": "CORRUPT", "error": str(exc)[:80],
        }

    if not isinstance(data, list) or not data:
        return {
            "file": os.path.basename(path), "interval": interval, "total": total,
            "bars": len(data) if isinstance(data, list) else 0,
            "first_ts": None, "last_ts": None, "lag_bars": None, "span_days": None,
            "size_kb": round(os.path.getsize(path) / 1024, 1),
            "status": "EMPTY", "error": "not a non-empty list",
        }

    first_ts = int(data[0][0])
    last_ts = int(data[-1][0])
    step = INTERVAL_SEC[interval]
    # 落后多少根：以"最后一根收盘时间"对齐到已收盘的最后一根
    closed_end = (NOW // step) * step
    lag = max(0, (closed_end - last_ts) // step)
    span = (last_ts - first_ts) / 86400.0

    if lag <= 2:
        status = "LIVE"
    elif lag <= 50:
        status = "STALE"
    else:
        status = "BEHIND"

    return {
        "file": os.path.basename(path), "interval": interval, "total": total,
        "bars": len(data), "first_ts": first_ts, "last_ts": last_ts,
        "lag_bars": lag, "span_days": round(span, 2),
        "size_kb": round(os.path.getsize(path) / 1024, 1),
        "status": status, "error": None,
    }


def covered_by(rec, others):
    """若同 interval 存在另一个文件，其区间完全包含本文件 -> 被覆盖。"""
    for o in others:
        if o is rec or o["interval"] != rec["interval"]:
            continue
        if o["first_ts"] is None or rec["first_ts"] is None:
            continue
        if o["first_ts"] <= rec["first_ts"] and o["last_ts"] >= rec["last_ts"]:
            if o["bars"] >= rec["bars"]:
                return o["file"]
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    paths = sorted(
        os.path.join(DATA_DIR, f) for f in os.listdir(DATA_DIR)
        if f.startswith("BTC_USDT_") and f.endswith(".json")
    )
    recs = [r for r in (inspect(p) for p in paths) if r]
    for r in recs:
        r["covered_by"] = covered_by(r, recs) if r["first_ts"] else None

    by_iv = defaultdict(list)
    for r in recs:
        by_iv[r["interval"]].append(r)

    reclaimable = [
        r for r in recs
        if r["status"] in ("BEHIND", "CORRUPT", "EMPTY") and r["covered_by"]
    ]
    dead_uncovered = [
        r for r in recs
        if r["status"] in ("BEHIND", "CORRUPT", "EMPTY") and not r["covered_by"]
    ]

    out = {
        "now": NOW,
        "now_iso": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(NOW)),
        "files": len(recs),
        "total_kb": round(sum(r["size_kb"] for r in recs), 1),
        "by_interval": {k: len(v) for k, v in sorted(by_iv.items())},
        "live": [r["file"] for r in recs if r["status"] == "LIVE"],
        "stale": [r["file"] for r in recs if r["status"] == "STALE"],
        "behind": [r["file"] for r in recs if r["status"] == "BEHIND"],
        "corrupt": [r["file"] for r in recs if r["status"] in ("CORRUPT", "EMPTY")],
        "reclaimable": reclaimable,
        "dead_uncovered": dead_uncovered,
        "records": recs,
    }

    if args.json:
        print(json.dumps(out, ensure_ascii=False, indent=1))
        return

    print(f"扫描时间 {out['now_iso']}  文件 {out['files']} 个  合计 {out['total_kb']} KB")
    print(f"周期分布: {out['by_interval']}\n")

    for iv in sorted(by_iv, key=lambda x: INTERVAL_SEC[x]):
        rs = sorted(by_iv[iv], key=lambda r: -r["bars"])
        print(f"=== {iv}  ({len(rs)} 个文件) ===")
        for r in rs:
            tag = {"LIVE": "LIVE ", "STALE": "stale", "BEHIND": "BEHIND",
                   "CORRUPT": "CRPT ", "EMPTY": "EMPTY"}[r["status"]]
            cov = f"  ⊂ {r['covered_by']}" if r["covered_by"] else ""
            err = f"  ERR={r['error']}" if r.get("error") else ""
            print(f"  {tag}  {r['file']:<28} bars={r['bars']:<6} "
                  f"lag={str(r['lag_bars']):>5}  span={r['span_days']:>7}d  "
                  f"{r['size_kb']:>6}KB{cov}{err}")
        print()

    print("=" * 70)
    print(f"可安全回收（已落后 + 被更深的文件覆盖）: {len(reclaimable)} 个, "
          f"{round(sum(r['size_kb'] for r in reclaimable), 1)} KB")
    for r in reclaimable:
        print(f"  {r['file']:<28} {r['size_kb']:>6}KB  lag={r['lag_bars']}  ⊂ {r['covered_by']}")
    print(f"\n落后但无覆盖（需人工判断，可能是唯一数据源）: {len(dead_uncovered)} 个")
    for r in dead_uncovered:
        print(f"  {r['file']:<28} {r['size_kb']:>6}KB  lag={r['lag_bars']}  bars={r['bars']}")


if __name__ == "__main__":
    main()