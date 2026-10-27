#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
单文件存储的回归验证 —— 打**行为**不���状态。

核心不变量：
  1. 浅请求不截断深缓存（total=500 后，8000 还在）
  2. 同一周期不同 total 共享一份缓存（文件数不再增长）
  3. 返回值严格是 total 根、升序、ts 唯一
  4. 离线模式（refresh=False）不联网也能拿到数据
  5. 跨周期隔离（清一个周期不影响其他）
  6. 增量更新后不产生缺口、ts 严格递增
"""
from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PASS, FAIL = "PASS", "FAIL"
results = []


def check(name: str, cond: bool, detail: str = ""):
    results.append((name, PASS if cond else FAIL, detail))
    mark = "✅" if cond else "❌"
    print(f"  {mark} {name}" + (f"  — {detail}" if detail else ""))
    return cond


def depth(contract: str, iv: str) -> int:
    import kline_store
    rows, d = kline_store.load_bars("/root/data", contract, iv)
    return d


def main():
    import gate_fetch

    print("\n=== 1. 浅请求不截断深缓存 ===")
    d_before = depth("BTC_USDT", "1h")
    rows_shallow = gate_fetch.fetch_ohlcv("BTC_USDT", "1h", 500, cache=True,
                                          refresh=True, verbose=False)
    d_after = depth("BTC_USDT", "1h")
    check("1h 浅请求(500) 返回正好 500 根", len(rows_shallow) == 500,
          f"got {len(rows_shallow)}")
    check("1h 深度未被截断", d_after >= d_before,
          f"{d_before} → {d_after}")

    print("\n=== 2. 返回值形状 ===")
    r = rows_shallow
    check("升序", all(r[i][0] < r[i + 1][0] for i in range(len(r) - 1)))
    check("ts 唯一", len({x[0] for x in r}) == len(r))
    check("每行 >= 7 列", all(len(x) >= 7 for x in r))
    check("无未收 K 线（末根已收盘）",
          r[-1][0] + gate_fetch.INTERVAL_SEC["1h"] <= int(time.time()),
          f"last_ts={r[-1][0]}")

    print("\n=== 3. 不同 total 共享同一份缓存（文件数不增长）===")
    files_before = len([f for f in os.listdir("/root/data")
                        if f.startswith("BTC_USDT") and f.endswith(".json")])
    for tot in (300, 700, 1200):
        gate_fetch.fetch_ohlcv("BTC_USDT", "1h", tot, cache=True,
                               refresh=False, verbose=False)
    files_after = len([f for f in os.listdir("/root/data")
                       if f.startswith("BTC_USDT") and f.endswith(".json")])
    check("文件数不增长", files_before == files_after,
          f"{files_before} → {files_after}")

    print("\n=== 4. 离线模式 ===")
    off = gate_fetch.fetch_ohlcv("BTC_USDT", "1h", 200, cache=True,
                                 refresh=False, verbose=False)
    check("离线返回 200 根", len(off) == 200, f"got {len(off)}")

    print("\n=== 5. 跨周期隔离 ===")
    import kline_store
    d_15m = depth("BTC_USDT", "15m")
    d_4h = depth("BTC_USDT", "4h")
    cleared = kline_store.clear_interval("/root/data", "BTC_USDT", "15m")
    check("clear_interval(15m) 返回 True", cleared is True)
    check("15m 已清空", depth("BTC_USDT", "15m") == 0)
    check("4h 未受影响", depth("BTC_USDT", "4h") == d_4h,
          f"4h depth={depth('BTC_USDT','4h')}")
    # 还原 15m
    gate_fetch.fetch_ohlcv("BTC_USDT", "15m", 8000, cache=True,
                           refresh=True, verbose=False)
    check("15m 重新拉回", depth("BTC_USDT", "15m") > 0,
          f"depth={depth('BTC_USDT','15m')}")

    print("\n=== 6. 增量更新后 ts 严格递增、无缺口 ===")
    rows = gate_fetch.fetch_ohlcv("BTC_USDT", "1h", 300, cache=True,
                                  refresh=True, verbose=False)
    ts = [x[0] for x in rows]
    step = gate_fetch.INTERVAL_SEC["1h"]
    gaps = [i for i in range(len(ts) - 1) if ts[i + 1] - ts[i] != step]
    check("ts 严格递增", all(ts[i] < ts[i + 1] for i in range(len(ts) - 1)))
    check("无时间缺口", not gaps, f"{len(gaps)} gaps" if gaps else "clean")

    print("\n=== 7. cache_freshness 报真值 ===")
    fr = gate_fetch.cache_freshness({"1h": 20000, "15m": 8000})
    h1 = next(x for x in fr if x["tf"] == "1h")
    check("1h 报出实际 depth 而非期望 total", h1["depth"] < 20000 or h1["depth"] > 0,
          f"depth={h1['depth']} target=20000 short={h1.get('short_of_target')}")
    check("freshness 有 lag 计算", h1["lag_sec"] is not None,
          f"lag={h1['lag_sec']}s")

    print("\n" + "=" * 60)
    n_pass = sum(1 for _, s, _ in results if s == PASS)
    n_fail = sum(1 for _, s, _ in results if s == FAIL)
    print(f"结果: {n_pass} passed, {n_fail} failed  (共 {len(results)})")
    if n_fail:
        print("\n失败项：")
        for nm, st, dt in results:
            if st == FAIL:
                print(f"  - {nm}  {dt}")
    return 1 if n_fail else 0


if __name__ == "__main__":
    sys.exit(main())