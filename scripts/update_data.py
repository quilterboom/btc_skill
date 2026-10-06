#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
K 线缓存批量更新器（每次回测 / 每次看盘前建议先跑一次）
========================================================
默认用【增量更新】：每个周期只发 1 次 HTTP 请求拉最新 1000 根，
与本地缓存按时间戳合并去重 —— 所以"每次执行都更新"成本极低。

用法:
    python update_data.py                      # 增量更新全部默认周期
    python update_data.py --tfs 1h,4h          # 只更新指定周期
    python update_data.py --force              # 忽略缓存，全量重拉（缓存落后太多时用）
    python update_data.py --status             # 只看新鲜度，不联网
    python update_data.py --contract ETH_USDT  # 换合约

默认周期与根数： 15m=8000 / 1h=20000 / 4h=5000 / 1d=1500
"""
from __future__ import annotations
import sys, os, time, datetime, argparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from gate_fetch import (update_all, cache_freshness, INTERVAL_SEC,
                        DATA_DIR, REQUEST_LOG, _cache_path)  # noqa: E402

U = getattr(datetime, "UTC", None) or datetime.timezone.utc
DEFAULT_TF = {"15m": 8000, "1h": 20000, "4h": 5000, "1d": 1500}

OK, NO, WARN = "✅", "❌", "⚠️"


def fmt_ts(t):
    return datetime.datetime.fromtimestamp(t, U).strftime("%Y-%m-%d %H:%M")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--contract", default="BTC_USDT")
    ap.add_argument("--tfs", default=",".join(DEFAULT_TF),
                    help="逗号分隔，如 15m,1h,4h,1d")
    ap.add_argument("--counts", default=None,
                    help="与 --tfs 对应的根数，如 8000,20000,5000,1500")
    ap.add_argument("--force", action="store_true", help="删掉缓存全量重拉")
    ap.add_argument("--status", action="store_true", help="只看新鲜度不联网")
    a = ap.parse_args()

    tfs = [x.strip() for x in a.tfs.split(",") if x.strip()]
    for t in tfs:
        if t not in INTERVAL_SEC:
            print(f"{NO} 不支持的周期: {t}")
            return 1
    if a.counts:
        cs = [int(x) for x in a.counts.split(",")]
        if len(cs) != len(tfs):
            print(f"{NO} --counts 数量必须与 --tfs 一致")
            return 1
    else:
        cs = [DEFAULT_TF.get(t, 5000) for t in tfs]
    tf_counts = dict(zip(tfs, cs))
    tf_counts = dict(sorted(tf_counts.items(), key=lambda kv: INTERVAL_SEC[kv[0]]))

    print(f"合约 {a.contract}（USDT 本位永续）")
    print(f"数据源 /futures/usdt/candlesticks ｜ 缓存目录 {DATA_DIR}")

    # ---------- 只看状态 ----------
    if a.status:
        print(f"\n{'周期':<6}{'根数':>9}{'上限':>8}{'末根(UTC)':>19}{'落后':>11}{'缺口':>7}")
        print("-" * 62)
        for r in cache_freshness(tf_counts, a.contract):
            if not r["exists"]:
                print(f"{r['tf']:<6}{'—':>9}{r['total']:>8}{'（无缓存）':>19}")
                continue
            lag = r.get("lag_sec") or 0
            lag_s = f"{lag/3600:.1f}h" if lag >= 3600 else f"{lag/60:.0f}min"
            miss = r["missing_bars"]
            flag = f"{OK}最新" if miss == 0 else f"{WARN}缺{miss}根"
            print(f"{r['tf']:<6}{r['bars']:>9}{r['total']:>8}"
                  f"{fmt_ts(r['last_ts']):>19}{lag_s:>11}{flag:>9}")
        return 0

    # ---------- 执行更新 ----------
    print(f"模式 {'全量重拉' if a.force else '增量更新'}\n")
    t0 = time.time()
    data = update_all(tf_counts, contract=a.contract, force=a.force, verbose=True)
    el = time.time() - t0

    print(f"\n{'周期':<6}{'根数':>9}{'新增':>7}  起点(UTC)          终点(UTC)")
    print("-" * 62)
    for tf in data:
        rows = data[tf]
        if not rows:
            print(f"{tf:<6}{'—':>9}  {NO} 无数据")
            continue
        print(f"{tf:<6}{len(rows):>9}{'':>7}  {fmt_ts(rows[0][0])}  ~  {fmt_ts(rows[-1][0])}")

    print(f"\n{OK} 更新完成 ｜ HTTP 请求 {len(REQUEST_LOG)} 次 ｜ 耗时 {el:.1f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
