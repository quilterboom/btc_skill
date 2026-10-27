#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
K 线单文件存储层
================

**为什么存在**：原来一个文件 = 一个「周期 × 根数」，文件名二维编码
（`BTC_USDT_1m_500.json`），导致 79 个文件、1m 一个周期独占 40 个
（60/61/64/65/67/... 全是反复跑不同 total 参数留下的调试残留）。

**改成**：一个合约一个文件，周期做一级 key。

```json
{
  "_meta": {"version": 2, "updated_at": 1763700000, "contract": "BTC_USDT"},
  "BTC_USDT": {
    "1m": {"bars": [[ts,o,h,l,c,v,sum], ...], "depth": 8000},
    "15m": {"bars": [...], "depth": 8000}
  }
}
```

**兼容策略**：`gate_fetch._cache_path(contract, interval, total)` 签名**不变**，
只是它背后的读写改走本模块。所有现有调用点无需改动即可工作。

**深度语义**：每个周期只存**最深**的一份（`depth`）。请求 `total` 更浅时，
从最深的缓存里切最后 N 根返回——所以 `fetch_ohlcv('1m', 500)` 在只存了
8000 根 1m 的情况下也能正常工作，不再需要单独的文件。

**写入用"就深不就浅"**：请求 total=500 而缓存已深 8000 → 只读不写，
避免把深缓存截断成浅的。反之（请求 20000 而缓存只有 5000）→ 正常增量补齐。
"""
from __future__ import annotations

import json
import os
import threading
import time
from typing import Dict, List, Optional, Tuple

STORE_VERSION = 2

INTERVAL_SEC = {
    "10s": 10, "1m": 60, "5m": 300, "15m": 900, "30m": 1800,
    "1h": 3600, "2h": 7200, "4h": 14400, "6h": 21600, "8h": 28800,
    "1d": 86400,
}


def store_path(data_dir: str, contract: str) -> str:
    """单文件存储路径"""
    return os.path.join(data_dir, f"{contract}_klines.json")


def _read_store(path: str) -> dict:
    """读整个 store；损坏/不存在返回空骨架"""
    if not os.path.exists(path):
        return {"_meta": {"version": STORE_VERSION, "contract": os.path.basename(
            path).split("_")[0], "updated_at": 0}, os.path.basename(
            path).split("_")[0]: {}}
    try:
        with open(path, "r") as f:
            data = json.load(f)
    except Exception:
        # 损坏：备份后当空处理，不让整个数据管道挂掉
        bak = f"{path}.corrupt-{int(time.time())}"
        try:
            os.replace(path, bak)
        except OSError:
            pass
        return {"_meta": {"version": STORE_VERSION, "contract": os.path.basename(
            path).split("_")[0], "updated_at": 0}, os.path.basename(
            path).split("_")[0]: {}}
    if not isinstance(data, dict) or "_meta" not in data:
        # v1（裸 list）或未知格式：当作空，迁移由 migrate_legacy 处理
        return {"_meta": {"version": STORE_VERSION, "updated_at": 0}, }
    return data


def _write_store(path: str, store: dict) -> None:
    """
    原子写：唯一 .tmp + os.replace

    ★ 2026-10-24 修复：原来所有写入进程共用固定名 `path + ".tmp"`，
      并发写时互相 unlink 对方的 tmp → os.replace 报
      "[Errno 2] No such file or directory: ...json.tmp -> ...json"，
      写失败后缓存只剩部分周期（实测 BTC_USDT_klines.json 被截到只剩 5m/15m，
      4h/1h/1d 全丢），回测和实盘取价都受影响。
      修法：tmp 名带 pid + 线程/随机后缀 → 互不干扰；replace 失败不影响原文件。
    """
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.tmp.{os.getpid()}.{threading.get_ident()}"
    try:
        store.setdefault("_meta", {})["version"] = STORE_VERSION
        store["_meta"]["updated_at"] = int(time.time())
        with open(tmp, "w") as f:
            json.dump(store, f)
        os.replace(tmp, path)
    except OSError as e:
        try:
            if os.path.exists(tmp):
                os.unlink(tmp)
        except OSError:
            pass
        import sys as _sys
        print(f"[kline_store] write failed {os.path.basename(path)}: {e}",
              file=_sys.stderr)


def load_bars(data_dir: str, contract: str, interval: str) -> Tuple[List[List], int]:
    """
    读某周期的全部已存 K 线（升序）+ 当前深度。

    返回 (rows, depth)。depth = 已存根数（不是请求的 total）。
    """
    store = _read_store(store_path(data_dir, contract))
    node = store.get(contract)
    if not isinstance(node, dict):
        return [], 0
    cell = node.get(interval)
    if not isinstance(cell, dict):
        return [], 0
    bars = cell.get("bars")
    if not isinstance(bars, list):
        return [], 0
    rows = [r for r in bars if isinstance(r, list) and len(r) >= 6]
    rows.sort(key=lambda r: r[0])
    depth = cell.get("depth")
    if not isinstance(depth, int) or depth != len(rows):
        depth = len(rows)
    return rows, depth


def save_bars(data_dir: str, contract: str, interval: str,
              rows: List[List]) -> None:
    """
    写某周期。**只允许把缓存写得更深**，浅写入会被忽略。

    这样保证 `fetch_ohlcv('1m', 500)` 这种浅请求不会把已存的 8000 根截断。

    ★ 2026-10-24 性能修复：内容**未变**时直接跳过写盘。
      根因：position_tracker._settle_one 的回放循环里逐根 1m 调用
            _check_ema_break_warning → 每次 fetch_ohlcv(cache=True) 都整库写盘
            （2.5MB json.dump ≈ 0.87s）→ 82 次写 = 71s，占 settle_all 的 60%。
      修法：比对新旧 bars，完全一致就不写。
            写盘本身是原子的（唯一 tmp + os.replace），跳过写不影响正确性。
    """
    rows = [r for r in rows if isinstance(r, list) and len(r) >= 6]
    if not rows:
        return
    path = store_path(data_dir, contract)
    store = _read_store(path)
    node = store.setdefault(contract, {})
    if not isinstance(node, dict):
        node = store[contract] = {}
    existing = node.get(interval)
    if isinstance(existing, dict):
        old_bars = existing.get("bars")
        if isinstance(old_bars, list) and len(old_bars) > len(rows):
            # 已存更深 → 拒绝这次浅写，保护深度
            return
    rows = sorted(rows, key=lambda r: r[0])
    # ★ 内容未变 → 跳过写盘（见 docstring 的性能修复说明）
    if isinstance(existing, dict) and existing.get("bars") == rows:
        return
    node[interval] = {"bars": rows, "depth": len(rows)}
    _write_store(path, store)


def clear_interval(data_dir: str, contract: str, interval: str) -> bool:
    """
    只清掉某一周期的桶（保留同文件里其他周期）。

    `update_all(force=True)` 依赖这个——原来删的是单周期文件，
    现在删文件会连带干掉其他周期，所以必须精确到桶。

    返回是否真的清掉了。
    """
    path = store_path(data_dir, contract)
    if not os.path.exists(path):
        return False
    store = _read_store(path)
    node = store.get(contract)
    if not isinstance(node, dict) or interval not in node:
        return False
    node.pop(interval, None)
    _write_store(path, store)
    return True


def store_stats(data_dir: str, contract: str) -> Dict[str, dict]:
    """每个周期的深度 / 最新 ts / 落后根数"""
    rows_by_iv = {}
    store = _read_store(store_path(data_dir, contract))
    node = store.get(contract) or {}
    now = int(time.time())
    out = {}
    for iv, cell in node.items():
        if not isinstance(cell, dict):
            continue
        bars = cell.get("bars") or []
        if not isinstance(bars, list) or not bars:
            continue
        last = int(bars[-1][0])
        step = INTERVAL_SEC.get(iv, 60)
        closed_end = (now // step) * step
        out[iv] = {
            "depth": cell.get("depth", len(bars)),
            "bars": len(bars),
            "last_ts": last,
            "lag_bars": max(0, (closed_end - last) // step),
        }
        rows_by_iv[iv] = bars
    return out


def migrate_legacy(data_dir: str, contract: str = "BTC_USDT",
                   dry_run: bool = True) -> dict:
    """
    把旧的 `{contract}_{interval}_{total}.json` 全部并入单文件 store。

    合并规则（同 interval 多个文件）：按**区间包含**判定保留最深的，
    完全被覆盖的直接丢弃；部分重叠的按 ts 合并去重，取更深的总深度。

    dry_run=True（默认）只报告不写盘。
    """
    legacy = []
    for f in sorted(os.listdir(data_dir)):
        if not (f.startswith(contract + "_") and f.endswith(".json")):
            continue
        if f == f"{contract}_klines.json":
            continue
        stem = f[len(contract) + 1:-len(".json")]
        parts = stem.rsplit("_", 1)
        if len(parts) != 2 or parts[0] not in INTERVAL_SEC or not parts[1].isdigit():
            continue
        p = os.path.join(data_dir, f)
        try:
            with open(p) as fh:
                rows = json.load(fh)
        except Exception as exc:
            legacy.append({"file": f, "interval": parts[0], "total": int(parts[1]),
                           "bars": -1, "error": str(exc)[:60]})
            continue
        rows = [r for r in rows if isinstance(r, list) and len(r) >= 6]
        rows.sort(key=lambda r: r[0])
        legacy.append({"file": f, "interval": parts[0], "total": int(parts[1]),
                       "bars": len(rows), "error": None,
                       "first_ts": int(rows[0][0]) if rows else None,
                       "last_ts": int(rows[-1][0]) if rows else None,
                       "size_kb": round(os.path.getsize(p) / 1024, 1)})

    by_iv: Dict[str, list] = {}
    for r in legacy:
        if r.get("error") is None and r["bars"] > 0:
            by_iv.setdefault(r["interval"], []).append(r)

    merged: Dict[str, dict] = {}
    dropped: List[str] = []
    for iv, items in by_iv.items():
        items.sort(key=lambda x: (-x["bars"]))
        best = items[0]
        keep_rows: Dict[int, List] = {}
        depth = best["bars"]
        for it in items:
            p = os.path.join(data_dir, it["file"])
            with open(p) as fh:
                rows = [r for r in json.load(fh) if isinstance(r, list) and len(r) >= 6]
            for r in rows:
                keep_rows[int(r[0])] = r
            if it is not best:
                dropped.append(it["file"])
        merged[iv] = {
            "bars": [keep_rows[t] for t in sorted(keep_rows)],
            "depth": depth,
            "sources": [i["file"] for i in items],
        }

    report = {
        "dry_run": dry_run,
        "contract": contract,
        "legacy_files": len(legacy),
        "unreadable": [r["file"] for r in legacy if r.get("error")],
        "intervals_merged": sorted(merged),
        "would_drop": sorted(dropped),
        "per_interval": {
            iv: {"depth": m["depth"], "unique_bars": len(m["bars"]),
                 "from": len(m["sources"]), "sources": m["sources"]}
            for iv, m in sorted(merged.items())
        },
    }

    if not dry_run:
        store = _read_store(store_path(data_dir, contract))
        node = store.setdefault(contract, {})
        for iv, m in merged.items():
            cur = node.get(iv)
            if isinstance(cur, dict) and isinstance(cur.get("bars"), list) \
                    and len(cur["bars"]) > len(m["bars"]):
                continue  # store 里已更深，不覆盖
            node[iv] = {"bars": m["bars"], "depth": m["depth"]}
        _write_store(store_path(data_dir, contract), store)
        report["wrote"] = store_path(data_dir, contract)
    return report


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("--migrate", action="store_true", help="迁移旧文件到单文件")
    ap.add_argument("--apply", action="store_true", help="真的写盘（默认只 dry-run）")
    ap.add_argument("--stats", action="store_true", help="打印单文件 store 状态")
    ap.add_argument("--data-dir", default="/root/data")
    ap.add_argument("--contract", default="BTC_USDT")
    a = ap.parse_args()

    if a.stats:
        s = store_stats(a.data_dir, a.contract)
        print(f"store: {store_path(a.data_dir, a.contract)}")
        if not s:
            print("  (empty)")
        for iv, v in sorted(s.items(), key=lambda kv: INTERVAL_SEC[kv[0]]):
            print(f"  {iv:<5} depth={v['depth']:<6} last_ts={v['last_ts']} "
                  f"lag={v['lag_bars']}")
    elif a.migrate:
        rep = migrate_legacy(a.data_dir, a.contract, dry_run=not a.apply)
        print(json.dumps(rep, ensure_ascii=False, indent=1))
    else:
        print(f"store path: {store_path(a.data_dir, a.contract)}")
        s = store_stats(a.data_dir, a.contract)
        print(f"intervals in store: {sorted(s) or '(none yet)'}")
        print("\nrun: --migrate (dry) | --migrate --apply | --stats")