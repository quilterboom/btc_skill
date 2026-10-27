#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
backtest_dynamic_tp1.py — 浮盈 + 反向放量自动减半回测（liusir 10-20 决策）

规则：持仓中 → close 在 float 方向 + 反向放量 ≥阈值 + 持续 ≥阈值根
   → 触发 partial_exit(50%) 推保本(SL=entry)。

回测：所有 41 笔已 settle 的 journal，模拟"如果触发了那一刻"，PnL 变化。
—— 只看数据，不动生产。
"""
import argparse, json, sys
from pathlib import Path

sys.path.insert(0, "/root/skills/btc-gate-intraday-strategy/scripts")
import gate_fetch

JOURNAL_PATH = Path("/root/data/journal.jsonl")
TRAINING_JOURNAL_PATH = Path("/root/data/training/journal.jsonl")

PROFIT_GRID = [0.0, 25.0, 50.0, 100.0, 150.0]


def load_journal(path: Path = JOURNAL_PATH) -> list:
    out = []
    if not path.exists():
        return out
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
                out.append(d)
            except Exception:
                pass
    return out


def load_1m_bars(start: int, end: int, lookback_min: int = 30, _bar_cache: dict | None = None) -> list:
    """拉 [start - lookback, end] 区间内 1m K。

    cache=True 不够:本地 1m cache 滚动窗口只覆盖最近 ~5.5 天,历史 journal
    多在 cache 外。要走 _download_full(end_ts=end) 分页拉历史段。

    _bar_cache:进程内缓存,key=(start,end,lookback_min) → bars,避免重复 HTTP
    """
    if _bar_cache is not None and (start, end, lookback_min) in _bar_cache:
        return _bar_cache[(start, end, lookback_min)]
    try:
        # 估算窗口内根数
        need = (end - start) // 60 + lookback_min * 2 + 10
        # 直接走 _download_full,end_ts=end 能拉到 1+ 天前数据(Gate 5-7 天窗口实测)
        raw = gate_fetch._download_full("BTC_USDT", "1m", need, end_ts=end, verbose=False)
        bars = [raw[k] for k in sorted(raw)]
        out = [r for r in bars if (start - lookback_min * 60) <= r[0] <= end]
        if _bar_cache is not None:
            _bar_cache[(start, end, lookback_min)] = out
        return out
    except Exception as e:
        # 回退:cache 模式
        need = max((end - start) // 60 + lookback_min * 2, 200)
        rows = gate_fetch.fetch_ohlcv("BTC_USDT", "1m", need, cache=True, refresh=False, verbose=False)
        out = [r for r in rows if (start - lookback_min * 60) <= r[0] <= end]
        if _bar_cache is not None:
            _bar_cache[(start, end, lookback_min)] = out
        return out


def find_fill_idx(bars: list, fill_ts: int) -> int | None:
    """找离 fill_ts 最近的那根 1m K,前后 ±5min 容差"""
    best = None
    best_delta = float("inf")
    for i, b in enumerate(bars):
        if abs(b[0] - fill_ts) <= 300:
            d = abs(b[0] - fill_ts)
            if d < best_delta:
                best_delta = d
                best = i
    return best


def detect(bars: list, side: str, entry: float, fill_idx: int,
           profit_thresh: float, z_thresh: float,
           min_rebound_pts: float, min_streak: int,
           lookback_avg: int = 20) -> dict | None:
    """
    在 fill_idx 之后,扫 1m K:
      浮盈:close 在 entry 的有利方向(profit_thresh 以上)
      反向放量:close 反向走 + vol > z_thresh*avg + 持续 min_streak 根
      累计反向 ≥ min_rebound_pts
    返回最早触发 {ts, px, ...} 或 None.
    """
    profit_dir = 1 if side == "short" else -1
    n = len(bars)
    for i in range(fill_idx + 1, n):
        b = bars[i]
        ts, o, hi, lo, c, v = b[0], b[1], b[2], b[3], b[4], b[5]
        # 浮盈门槛(per-bar check)
        floating = profit_dir * (c - entry)
        if floating < profit_thresh:
            continue
        # lookback avg
        if i < lookback_avg:
            continue
        avg = sum(bars[j][5] for j in range(i - lookback_avg, i)) / lookback_avg
        z = v / avg
        if z < z_thresh:
            continue
        # 检查方向
        c_prev = bars[i - 1][4]
        rev_dir = (c < c_prev and side == "long") or (c > c_prev and side == "short")
        if not rev_dir:
            continue
        # 反向 streak 检查(连续 N 根 close 反向)
        if i < min_streak + 1:
            continue
        ok = True
        for j in range(i - min_streak + 1, i + 1):
            cb = bars[j][4]
            cp = bars[j - 1][4]
            if (cb < cp and side == "long") or (cb > cp and side == "short"):
                continue
            if j == i:  # 当前 bar 是反向
                continue
            ok = False
            break
        if not ok:
            continue
        # 累计反向位移
        if side == "short":
            cum = c - bars[i - min_streak + 1][4]
        else:
            cum = bars[i - min_streak + 1][4] - c
        if abs(cum) < min_rebound_pts:
            continue
        return {
            "trigger_idx": i,
            "ts": ts,
            "px": c,
            "z": z,
            "vol_avg": avg,
            "vol_current": v,
            "streak": min_streak,
            "rebound_pts": cum,
            "floating_pts": floating,
        }
    return None


def simulate(r: dict, profit_thresh: float, z_thresh: float,
             min_rebound_pts: float, min_streak: int) -> dict | None:
    fill_ts = int(r.get("fill_ts") or 0)
    exit_ts = int(r.get("exit_ts") or 0)
    fill_px = float(r.get("fill_px") or 0)
    exit_px = float(r.get("exit_px") or 0)
    contracts = int(r.get("contracts") or 0)
    side = r.get("side", "long")
    net_actual = float(r.get("net_usd") or 0)
    if not (fill_ts and exit_ts and fill_px and contracts):
        return None
    bars = load_1m_bars(fill_ts, exit_ts, lookback_min=30)
    if len(bars) < 50:
        return {"id": r["id"], "skip": f"insufficient({len(bars)})"}
    fill_idx = find_fill_idx(bars, fill_ts)
    if fill_idx is None:
        return {"id": r["id"], "skip": "no_fill_idx"}
    trig = detect(bars, side, fill_px, fill_idx,
                  profit_thresh, z_thresh, min_rebound_pts, min_streak)
    if not trig:
        return {"id": r["id"], "side": side, "triggered": False,
                "net_actual": net_actual, "net_dynamic": net_actual,
                "delta": 0.0}
    # 模拟减半
    half = contracts // 2
    remain = contracts - half
    sign = 1 if side == "long" else -1
    fee_per_contract = 4.85 / contracts
    # 触发时平 half,单边 momentum 价
    p_partial = sign * (trig["px"] - fill_px) * half * 0.0001 - half * fee_per_contract
    # 剩 remain 走到 exit(按 _try_partial_close 实际是 SL=entry 推保本,简化起见按 exit_px 算)
    p_remain = sign * (exit_px - fill_px) * remain * 0.0001 - remain * fee_per_contract
    net_dynamic = p_partial + p_remain
    return {
        "id": r["id"], "side": side,
        "fill_px": fill_px, "exit_px": exit_px,
        "trigger_px": trig["px"],
        "trigger_ts": trig["ts"],
        "z": trig["z"], "streak": trig["streak"],
        "rebound_pts": trig["rebound_pts"],
        "floating_pts": trig["floating_pts"],
        "net_actual": net_actual,
        "net_dynamic": round(net_dynamic, 2),
        "delta": round(net_dynamic - net_actual, 2),
        "triggered": True,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--profit", type=float, default=50.0,
                    help="浮盈门槛(点)，默认 50")
    ap.add_argument("--z-thresh", type=float, default=3.0)
    ap.add_argument("--min-rebound", type=float, default=50.0)
    ap.add_argument("--min-streak", type=int, default=3)
    ap.add_argument("--use-training", action="store_true")
    args = ap.parse_args()

    if args.use_training:
        records = load_journal(TRAINING_JOURNAL_PATH)
    else:
        records = load_journal(JOURNAL_PATH)
    print(f"加载 {len(records)} 笔 journal\n")

    triggered = []
    not_triggered = []
    skipped = []
    bar_cache: dict = {}   # 进程内 bars 缓存(同一单 fill→exit 区间不会重复)
    for r in records:
        result = simulate(r, args.profit, args.z_thresh,
                          args.min_rebound, args.min_streak)
        if result is None:
            continue
        if result.get("skip"):
            skipped.append(result)
        elif result.get("triggered"):
            triggered.append(result)
        else:
            not_triggered.append(result)

    # 总览
    print(f"动态 TP1 参数: 浮盈 ≥{args.profit} 点 + z≥{args.z_thresh} + 反向 ≥{args.min_rebound} 点 + 持续 ≥{args.min_streak} 根")
    print(f"总笔数: {len(records)} | 触发: {len(triggered)} | 未触发: {len(not_triggered)} | 跳过: {len(skipped)}\n")

    if triggered:
        # 按 delta 排序 — 改善最大的在前
        triggered.sort(key=lambda r: r.get("delta", 0), reverse=True)
        # 区分改善 vs 恶化
        improved = [r for r in triggered if r["delta"] > 0.5]
        worsened = [r for r in triggered if r["delta"] < -0.5]
        neutral = [r for r in triggered if abs(r["delta"]) <= 0.5]
        print(f"改善 (>0.5U): {len(improved)}")
        print(f"  累计改善 PnL: {sum(r['delta'] for r in improved):+.2f}U")
        print(f"恶化 (<-0.5U): {len(worsened)}")
        print(f"  累计恶化 PnL: {sum(r['delta'] for r in worsened):+.2f}U")
        print(f"中性: {len(neutral)}\n")

        total_actual = sum(r["net_actual"] for r in triggered)
        total_dynamic = sum(r["net_dynamic"] for r in triggered)
        print(f"触发 {len(triggered)} 笔实际 PnL: {total_actual:+.2f}U")
        print(f"          动态 TP1 PnL: {total_dynamic:+.2f}U")
        print(f"          总 Delta: {total_dynamic - total_actual:+.2f}U\n")

        print(f"{'id':<35} {'side':<5} {'z':>5} {'reb':>6} {'fl_p':>6} {'actual':>10} {'dynamic':>10} {'delta':>10}")
        for r in triggered:
            print(f"{r['id']:<35} {r['side']:<5} {r['z']:>5.2f} {r['rebound_pts']:>+6.0f} {r.get('floating_pts',0):>+6.0f} {r['net_actual']:>+10.2f}U {r['net_dynamic']:>+10.2f}U {r['delta']:>+10.2f}U")

    # 无触发且亏损:那才是真损失
    lost_no_trigger = [r for r in not_triggered if r.get("net_actual", 0) < 0]
    if lost_no_trigger:
        print(f"\n亏损但未触发({len(lost_no_trigger)} 笔,showing top 10 by abs net):")
        lost_no_trigger.sort(key=lambda r: r["net_actual"])
        for r in lost_no_trigger[:10]:
            print(f"  {r['id']:<35} {r['side']:<5} actual={r['net_actual']:+.2f}U")


if __name__ == "__main__":
    main()
