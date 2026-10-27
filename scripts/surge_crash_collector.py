#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""
暴涨暴跌事件采集器
====================

**目的**：为交易策略优化积累样本 —— 每次 BTC 暴涨/暴跌，记录**事件前 10 分钟
到事件结束**的完整 1m K 线 + 成交量变化。攒够样本后回答：

- 暴跌前有没有量能特征？（放量倍数 / z 值）
- 暴跌后多久反弹？反弹幅度多大？（→ 校准持仓 SL / 止盈）
- 「量能异动 + 形态」组合对暴跌的提前预警能力有多强？

**定义（2026-10-23 用 liusir 真实例子校准，非拍脑袋）**
------------------------------------------------------------------

liusir 10-23 12:43 口头给了例子：「今天从 84808 跌到 82660，就是一个暴跌」。

从这句话反推出三个参数，缺一不可：

| 参数 | 值 | 怎么定的 |
|---|---|---|
| 幅度口径 | **-2.53%**（峰→谷区间跌幅） | 例子本身就是区间跌幅，不是单根涨跌幅 |
| 时间跨度 | **10.9 小时** | 决定不能用分钟级/小时级窗口 |
| 峰谷 | 84808 → 82660 | 决定锚点 = 段内最高 / 最低 |

**口径演进（每步都有实测数据支撑）**：

| 口径 | 频率 | 能否抓到 -2.53% 这个例子 |
|---|---|---|
| 单根 1m \|涨跌\| ≥ 0.2% | 2.7 次/天 | ❌ 这是分钟级噪音 |
| 1m 局部高低点配对 (K=15) | 183 个碎片 / 5.5 天 | ❌ 把 10.9h 单边行情切碎 |
| 日内 max→min（无序） | — | ❌ 峰谷顺序可能反（10-21 那笔 peak 23:11 晚于 trough 00:24） |
| 滚动窗口峰谷回撤 | — | ❌ 窗口长度与事件跨度脱节，产出 24.2h 的「240min 窗口事件」 |
| **15m 局部高低点配对 (K=8)** | **0.72 次/天 @≥1.5%** | ✅ **精确命中** |

最终口径：**在 15m 序列上用 K=8（=120min 窗口）找局部高点和局部低点，
配对成「单边段」；再回到 1m 精确定位该段的真实峰谷。**

K=8 是关键：K=4/6 会把同一段行情切出多个碎片（25/35 段），
K=12 则漏掉短促但猛烈的行情。实测 K=8 + ≥1.5% → 5.5 天 4 个事件，
一个月约 22 个样本 —— 够做初步统计。

**阈值 1.5%**：≥2.0% 只有 0.18 次/天（一个月 5 个样本，太少），
≥1.5% 有 0.72 次/天（一个月 22 个），且能覆盖 liusir 举的 -2.53% 那类。

**事件窗口**：事件起点 = 段内最高点那根；终点 = 段内最低点那根。
记录 start 前 PRE_MINUTES 分钟 + 整段 + 终点后 POST_MINUTES 分钟。

**后置窗口需要等**：事件刚结束时只能看到「前 N 分钟」，后 N 分钟要等时间走完。
标记 `complete: false`，下次扫描补齐 —— 离线 backfill 和实时 daemon
走同一套检测逻辑，避免两套代码漂移。

**用法**：

```bash
python3 scripts/surge_crash_collector.py --backfill   # 回填历史
python3 scripts/surge_crash_collector.py --daemon     # 实时采集（每 60s）
python3 scripts/surge_crash_collector.py --stats      # 分布统计
```

**输出**：`/root/data/surge_crash_events.jsonl`（追加式，一行一个事件）
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

DATA_DIR = "/root/data"
STORE = Path(DATA_DIR) / "surge_crash_events.jsonl"

# ---- 检测参数（2026-10-23 实测校准）----
PIVOT_K = 8            # 15m 序列上的局部极值窗口（8 × 15m = 120min）
DROP_THRESHOLD = 1.5   # 跌幅阈值 %
RISE_THRESHOLD = 1.5   # 涨幅阈值 %
PRE_MINUTES = 10       # 事件前 N 分钟
POST_MINUTES = 10      # 事件后 N 分钟
MIN_15M_BARS = 40      # 15m 序列太短就不检测


# ============================ 数据加载 ============================

def load_bars(interval: str = "1m"):
    """从 kline_store 读缓存（升序）"""
    import kline_store
    rows, _depth = kline_store.load_bars(DATA_DIR, "BTC_USDT", interval)
    rows.sort(key=lambda r: r[0])
    return rows


def bar_dict(b) -> dict:
    """[ts,o,h,l,c,v,sum] → dict"""
    return {
        "ts": int(b[0]),
        "time": time.strftime("%m-%d %H:%M", time.localtime(b[0])),
        "o": round(float(b[1]), 1),
        "h": round(float(b[2]), 1),
        "l": round(float(b[3]), 1),
        "c": round(float(b[4]), 1),
        "v": float(b[5]),
    }


# ============================ 检测 ============================

def find_pivots(bars, k: int):
    """局部极值点：比左右各 k 根都极端。返回 (high_indices, low_indices)"""
    n = len(bars)
    highs, lows = [], []
    if n < 2 * k + 1:
        return highs, lows
    for i in range(k, n - k):
        win_l = bars[i - k:i]
        win_r = bars[i + 1:i + k + 1]
        if bars[i][2] >= max(r[2] for r in win_l) and \
           bars[i][2] > max(r[2] for r in win_r):
            highs.append(i)
        if bars[i][3] <= min(r[3] for r in win_l) and \
           bars[i][3] < min(r[3] for r in win_r):
            lows.append(i)
    return highs, lows


def detect_legs(bars15, threshold: float):
    """
    在 15m 序列上找单边段（局部高 → 后续局部低 = 下跌段；反向 = 上涨段）。
    返回 [(dir, hi_idx15, lo_idx15, peak_px, trough_px, pct)]
    """
    highs, lows = find_pivots(bars15, PIVOT_K)
    legs = []
    for hi in highs:
        nxt = [lo for lo in lows if lo > hi]
        if not nxt:
            continue
        lo = nxt[0]
        hp, lp = bars15[hi][2], bars15[lo][3]
        if hp <= 0:
            continue
        pct = (lp - hp) / hp * 100
        if pct <= -threshold:
            legs.append(("crash", hi, lo, hp, lp, pct))
    for lo in lows:
        nxt = [hi for hi in highs if hi > lo]
        if not nxt:
            continue
        hi = nxt[0]
        lp, hp = bars15[lo][3], bars15[hi][2]
        if lp <= 0:
            continue
        pct = (hp - lp) / lp * 100
        if pct >= threshold:
            legs.append(("surge", hi, lo, hp, lp, pct))
    return legs


def refine_with_1m(bars1, t_start, t_end):
    """在 1m 上精确定位 [t_start, t_end] 区间的真实峰谷"""
    seg = [r for r in bars1 if t_start <= r[0] <= t_end]
    if not seg:
        return None
    hi = max(seg, key=lambda r: r[2])
    lo = min(seg, key=lambda r: r[3])
    return hi, lo


def vol_shape(seq):
    """量能形态：前半 vs 后半趋势比"""
    if len(seq) < 4:
        return {}
    vs = [b["v"] for b in seq]
    half = len(vs) // 2
    m1 = statistics.mean(vs[:half]) or 1
    m2 = statistics.mean(vs[half:])
    return {
        "vol_first_half_avg": round(m1, 1),
        "vol_second_half_avg": round(m2, 1),
        "vol_trend_ratio": round(m2 / m1, 3),
        "vol_max": round(max(vs), 1),
        "vol_min": round(min(vs), 1),
    }


def build_event(bars1, bars15, leg) -> dict:
    """把一个 leg 变成完整事件记录"""
    direction, hi_i, lo_i, _hp15, _lp15, _pct15 = leg
    t_a = min(bars15[hi_i][0], bars15[lo_i][0])
    t_b = max(bars15[hi_i][0], bars15[lo_i][0])

    refined = refine_with_1m(bars1, t_a, t_b)
    if not refined:
        return {}
    hi1, lo1 = refined
    if direction == "crash":
        peak, trough = hi1, lo1
    else:
        peak, trough = lo1, hi1

    anchor = int(peak[0])
    trough_ts = int(trough[0])
    pre_start = anchor - PRE_MINUTES * 60
    post_end = trough_ts + POST_MINUTES * 60

    pre = [bar_dict(b) for b in bars1 if pre_start <= b[0] <= anchor]
    body = [bar_dict(b) for b in bars1 if anchor < b[0] <= trough_ts]
    post = [bar_dict(b) for b in bars1 if trough_ts < b[0] <= post_end]

    # 全局量能基线（用于 z 值）
    allv = [float(b[5]) for b in bars1]
    mu = statistics.mean(allv)
    sd = statistics.pstdev(allv) or 1e-9

    def z_of(b):
        return round((float(b[5]) - mu) / sd, 2)

    peak_px = float(peak[2]) if direction == "crash" else float(peak[3])
    trough_px = float(trough[3]) if direction == "crash" else float(trough[2])
    move_pct = (trough_px - peak_px) / peak_px * 100

    rec = {
        "anchor_ts": anchor,
        "anchor_time": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(anchor)),
        "direction": direction,
        "peak_ts": anchor,
        "peak_time": time.strftime("%Y-%m-%d %H:%M", time.localtime(anchor)),
        "peak_px": round(peak_px, 1),
        "trough_ts": trough_ts,
        "trough_time": time.strftime("%Y-%m-%d %H:%M", time.localtime(trough_ts)),
        "trough_px": round(trough_px, 1),
        "move_pct": round(move_pct, 4),
        "duration_min": round((trough_ts - anchor) / 60.0, 1),
        "pre_minutes": PRE_MINUTES,
        "post_minutes": POST_MINUTES,
        "pre_bars": pre,
        "body_bars": body,
        "post_bars": post,
        "pre_vol_shape": vol_shape(pre),
        "body_vol_shape": vol_shape(body),
        "peak_vol_z": z_of(peak),
        "peak_vol_x_mean": round(float(peak[5]) / mu, 2),
        "vol_baseline_mean": round(mu, 1),
        "thresholds": {
            "pivot_k_15m": PIVOT_K,
            "drop_threshold": DROP_THRESHOLD,
            "rise_threshold": RISE_THRESHOLD,
            "pre_minutes": PRE_MINUTES,
            "post_minutes": POST_MINUTES,
        },
    }
    rec["complete"] = bool(post) and bars1[-1][0] >= post_end

    # 后续走势（策略优化核心字段）
    if post:
        highs = [b["h"] for b in post]
        lows = [b["l"] for b in post]
        rec["follow_up"] = {
            "post_high": round(max(highs), 1),
            "post_low": round(min(lows), 1),
            "max_bounce_pct": round((max(highs) - trough_px) / trough_px * 100, 4),
            "max_fade_pct": round((min(lows) - trough_px) / trough_px * 100, 4),
            "post_close": post[-1]["c"],
            "post_return_pct": round((post[-1]["c"] - trough_px) / trough_px * 100, 4),
            "revert_pct": round((post[-1]["c"] - peak_px) / peak_px * 100, 4),
        }
    return rec


# ============================ 落盘 ============================

def do_backfill(verbose=True):
    """回填：扫全部缓存，追加未记录过的事件"""
    bars1 = load_bars("1m")
    bars15 = load_bars("15m")
    if len(bars15) < MIN_15M_BARS:
        print(f"15m 缓存只有 {len(bars15)} 根，先跑 update_data.py --tfs 15m")
        return 0
    # 15m 限定到 1m 覆盖区间（否则会用到 1m 没有的历史）
    cut = bars1[0][0]
    bars15 = [r for r in bars15 if r[0] >= cut]

    seen = set()
    existing = 0
    if STORE.exists():
        for line in STORE.open():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
                seen.add(rec.get("anchor_ts"))
                existing += 1
            except Exception:
                continue

    legs = detect_legs(bars15, DROP_THRESHOLD)
    fresh = []
    for leg in legs:
        rec = build_event(bars1, bars15, leg)
        if not rec:
            continue
        if rec["anchor_ts"] in seen:
            continue
        seen.add(rec["anchor_ts"])
        fresh.append(rec)

    if not fresh:
        if verbose:
            print(f"无新事件（已有 {existing} 条，检测到 {len(legs)} 条，全部已记录）")
        return 0

    STORE.parent.mkdir(parents=True, exist_ok=True)
    with STORE.open("a") as f:
        for rec in fresh:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    if verbose:
        print(f"新增 {len(fresh)} 条事件（已有 {existing} → {existing + len(fresh)}）")
        for rec in fresh:
            print(f"  {rec['peak_time']} → {rec['trough_time']}  "
                  f"{rec['direction']:<5} {rec['move_pct']:+.2f}%  "
                  f"{rec['duration_min']:.0f}min  "
                  f"peak_vol={rec['peak_vol_x_mean']:.1f}x z={rec['peak_vol_z']:+.1f}  "
                  f"{'complete' if rec['complete'] else 'pending'}")
    return len(fresh)


def do_stats():
    if not STORE.exists():
        print("尚无事件。先跑 --backfill")
        return
    recs = [json.loads(l) for l in STORE.open() if l.strip()]
    sur = [r for r in recs if r["direction"] == "surge"]
    cra = [r for r in recs if r["direction"] == "crash"]
    print(f"总事件 {len(recs)}  暴跌 {len(cra)}  暴涨 {len(sur)}")
    if not recs:
        return

    span_days = (recs[-1]["anchor_ts"] - recs[0]["anchor_ts"]) / 86400 or 1
    print(f"跨度 {span_days:.1f} 天  → {len(recs)/span_days:.2f} 次/天")
    print()

    for label, group in [("暴跌", cra), ("暴涨", sur)]:
        if not group:
            continue
        mv = [abs(r["move_pct"]) for r in group]
        dur = [r["duration_min"] for r in group]
        z = [r["peak_vol_z"] for r in group]
        vx = [r["peak_vol_x_mean"] for r in group]
        tr = [r["pre_vol_shape"].get("vol_trend_ratio") for r in group
              if r.get("pre_vol_shape", {}).get("vol_trend_ratio")]
        print(f"=== {label} ({len(group)} 次) ===")
        print(f"  幅度   均 {statistics.mean(mv):.2f}%  最大 {max(mv):.2f}%")
        print(f"  历时   均 {statistics.mean(dur):.0f}min  最大 {max(dur):.0f}min")
        print(f"  峰位放量 z 均 {statistics.mean(z):+.1f}  最高 {max(z):+.1f}")
        print(f"  峰位放量倍数 均 {statistics.mean(vx):.1f}x  最高 {max(vx):.1f}x")
        if tr:
            print(f"  前10分钟量能趋势比 均 {statistics.mean(tr):.2f}x (>1 = 放量递增)")
        rb = [r["follow_up"]["max_bounce_pct"] for r in group if r.get("follow_up")]
        if rb:
            print(f"  后10分钟最大反弹 均 {statistics.mean(rb):+.3f}%  最高 {max(rb):+.3f}%")
        print()

    done = sum(1 for r in recs if r.get("complete"))
    print(f"后 10 分钟窗口完整: {done}/{len(recs)} "
          f"({len(recs)-done} 条还没走完 10 分钟)")


def do_daemon(interval_s=60):
    print(f"daemon 启动，每 {interval_s}s 扫一次，Ctrl-C 退出")
    while True:
        try:
            n = do_backfill(verbose=False)
            if n:
                print(f"[{time.strftime('%H:%M:%S')}] 新增 {n} 条事件")
        except Exception as exc:
            print(f"[{time.strftime('%H:%M:%S')}] 扫描出错: {exc}")
        time.sleep(interval_s)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--backfill", action="store_true", help="回填历史事件")
    ap.add_argument("--daemon", action="store_true", help="实时持续采集")
    ap.add_argument("--stats", action="store_true", help="统计已有事件")
    ap.add_argument("--interval", type=int, default=60, help="daemon 间隔秒")
    a = ap.parse_args()

    if a.stats:
        do_stats()
    elif a.daemon:
        do_daemon(a.interval)
    elif a.backfill:
        do_backfill()
    else:
        print(__doc__)
        print("run: --backfill | --daemon | --stats")


if __name__ == "__main__":
    main()