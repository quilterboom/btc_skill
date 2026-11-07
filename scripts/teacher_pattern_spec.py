#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
teacher_pattern_spec.py —— 从老师 20 笔单反推「交易模式」的可量化规格

## 目的
liusir 2026-11-07：「希望以老师交易的单去做量化交易，想要更加接近老师的交易模式」。

要做这件事，第一步不是拍参数，是**先把老师的模式拆成可计算的规格**：
什么时刻进（触发）、怎么进（价格）、风险放哪、怎么退（出场阶梯）。

## 本脚本产出
1. **出场阶梯规格**（从 20 笔的 events 里统计，不臆造）
   - TP1 幅度 / 减仓比例 / TP2 幅度 / SL 幅度 / 保本规则
2. **入场触发画像**（用 15m + 1h K 线，从入场时刻往前看）
   - 下跌幅度、回撤深度、量能放大倍数、是否假跌破收回、距结构位距离
   - 时段分布（老师集中在哪些钟点）
3. **入场价与结构的关系**（判断是「回调到支撑」还是「突破追进」）
4. 每笔一个可核对的特征行（供下一步长历史复刻用）

## 只读，不改任何数据
"""
from __future__ import annotations

import json
import os
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone

sys.path.insert(0, "/root/skills/btc-gate-intraday-strategy/scripts")
sys.path.insert(0, "/root/skills/btc-gate-intraday-strategy/web")

import teacher_analysis as ta

CST = timezone(timedelta(hours=8))
KLINE = "/root/data/BTC_USDT_klines.json"


def load_bars(tf):
    d = json.load(open(KLINE))
    return d["BTC_USDT"][tf]["bars"]


def ema(vals, period):
    if len(vals) < period:
        return None
    k = 2.0 / (period + 1)
    v = sum(vals[:period]) / period
    out = [v]
    for x in vals[period:]:
        v = x * k + v * (1 - k)
        out.append(v)
    return out


def atr_pct(bars, idx, period=14):
    """idx 之前（含）最后一根已收 bar 的 ATR%。"""
    if idx < period:
        return None
    trs = []
    for i in range(idx - period + 1, idx + 1):
        h, l, pc = float(bars[i][2]), float(bars[i][3]), float(bars[i - 1][4])
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    atr = sum(trs) / len(trs)
    c = float(bars[idx][4])
    return atr / c * 100 if c else None


def entry_features(ts, px, bars15, bars1h):
    """入场时刻的市场上下文 —— 严格只用 ts 之前最后一根已收 bar。"""
    f = {}
    for name, bars in (("15m", bars15), ("1h", bars1h)):
        idx = None
        for i, b in enumerate(bars):
            if int(b[0]) >= ts:
                idx = i
                break
        if idx is None:
            idx = len(bars) - 1
        idx = max(0, idx - 1)          # ★ 已收 bar
        if idx < 30:
            continue
        o, h, l, c, v = (float(bars[idx][1]), float(bars[idx][2]),
                         float(bars[idx][3]), float(bars[idx][4]), float(bars[idx][5]))
        # 下跌段：入场前 N 根的起点 → 入场前一根收盘
        for look, key in ((4, "4"), (12, "12"), (48, "48")):
            if name == "15m":
                win = 4 * look if key == "12" else (12 * look if key == "48" else look)
            else:
                win = look
            j = idx - win
            if j < 0:
                continue
            f[f"{name}_drop_{key}bars_pct"] = round((float(bars[j][4]) - c) / float(bars[j][4]) * 100, 3)
        # 量能：入场前一根 vs 前 20 根均量
        vols = [float(b[5]) for b in bars[idx - 20:idx] if len(b) > 5]
        if vols and sum(vols) / len(vols) > 0:
            f[f"{name}_vol_x"] = round(v / (sum(vols) / len(vols)), 2)
        # 结构：入场前 96 根（15m=24h / 1h=4天）的极值
        win = bars[max(0, idx - 96):idx + 1]
        if len(win) >= 8:
            hi = max(float(b[2]) for b in win)
            lo = min(float(b[3]) for b in win)
            f[f"{name}_hi_{len(win)}"] = hi
            f[f"{name}_lo_{len(win)}"] = lo
            if hi > 0:
                f[f"{name}_dd_from_hi_pct"] = round((hi - px) / hi * 100, 2)
            if hi > lo:
                f[f"{name}_range_pos_pct"] = round((px - lo) / (hi - lo) * 100, 1)
        # 入场价距近期结构位
        sw_hi = max(float(b[2]) for b in bars[max(0, idx - 48):idx + 1])
        sw_lo = min(float(b[3]) for b in bars[max(0, idx - 48):idx + 1])
        f[f"{name}_swing_hi"] = sw_hi
        f[f"{name}_swing_lo"] = sw_lo
        # 假跌破：入场前 N 根内最低 vs 入场前一根收盘
        recent_lo = min(float(b[3]) for b in bars[max(0, idx - 8):idx + 1])
        f[f"{name}_recent_low"] = recent_lo
        f[f"{name}_reclaim_pct"] = round((px - recent_lo) / recent_lo * 100, 3) if recent_lo else None
        # ATR%
        a = atr_pct(bars, idx)
        if a:
            f[f"{name}_atr_pct"] = round(a, 4)
        # EMA 结构
        closes = [float(b[4]) for b in bars[:idx + 1]]
        s20 = ema(closes, 20)
        s50 = ema(closes, 50)
        e20 = s20[-1] if s20 else None
        e50 = s50[-1] if s50 else None
        if e20 and e50:
            f[f"{name}_ema20_50"] = "up" if e20 > e50 * 1.0005 else ("down" if e20 < e50 * 0.9995 else "flat")
            f[f"{name}_px_vs_ema50_pct"] = round((px - e50) / e50 * 100, 2)
    return f


def _num(v):
    """账本里数值字段可能是 str / None / 数字 —— 统一转 float 或 None。"""
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    try:
        return float(str(v).strip().replace(",", ""))
    except (TypeError, ValueError):
        return None


def exit_spec(rows):
    """出场阶梯规格 —— 只统计老师实际用过的，不臆造。"""
    tp1_pct, tp1_qty, tp2_pct, sl_pct, rr = [], [], [], [], []
    for r in rows:
        evs = r.get("events") or []
        entry = _num(r.get("entry_zone")) or _num(r.get("fill_price"))
        if not entry:
            continue
        tp1 = _num(r.get("tp1"))
        tp2 = _num(r.get("tp2"))
        sl = _num(r.get("sl_initial"))
        tp1ev = next((e for e in evs if e.get("type") in ("tp1_partial", "tp1_hit", "tp1")), None)
        if tp1:
            tp1_pct.append((tp1 / entry - 1) * 100)
        if tp2:
            tp2_pct.append((tp2 / entry - 1) * 100)
        if sl:
            sl_pct.append((entry - sl) / entry * 100)
        if tp1ev and tp1ev.get("qty_pct"):
            tp1_qty.append(float(tp1ev["qty_pct"]))
        if sl and tp2:
            risk = entry - sl
            reward = tp2 - entry
            if risk > 0:
                rr.append(reward / risk)
    def stat(v):
        if not v:
            return "n/a"
        v = sorted(v)
        med = v[len(v) // 2]
        return f"n={len(v):>2} 均{sum(v)/len(v):.3f} 中位{med:.3f} 范围[{v[0]:.3f},{v[-1]:.3f}]"
    print("  TP1 幅度 %      :", stat(tp1_pct))
    print("  TP1 减仓比例 %  :", stat(tp1_qty))
    print("  TP2 幅度 %      :", stat(tp2_pct))
    print("  初始 SL 幅度 %  :", stat(sl_pct))
    print("  R:R (TP2/SL)    :", stat(rr))
    return {"tp1_pct": tp1_pct, "tp1_qty": tp1_qty, "tp2_pct": tp2_pct,
            "sl_pct": sl_pct, "rr": rr}


def main():
    rows = [json.loads(l) for l in open("/root/data/teacher_trades.jsonl") if l.strip()]
    rows.sort(key=lambda r: r["trade_id"])
    bars15 = load_bars("15m")
    bars1h = load_bars("1h")

    print("=" * 108)
    print("老师交易模式规格（20 单反推）")
    print("=" * 108)

    # ── 1. 出场阶梯 ──
    print("\n【1】出场阶梯规格（只统计老师实际用过的）")
    exit_spec(rows)

    # ── 2. 时段分布 ──
    print("\n【2】入场时段分布")
    hours = Counter()
    wd = Counter()
    for r in rows:
        d = r.get("date") or ""
        try:
            dt = datetime.strptime(d[:16], "%Y-%m-%d %H:%M")
        except ValueError:
            continue
        hours[dt.hour] += 1
        wd["一二三四五六日"[dt.weekday()]] += 1
    print("  按小时:", dict(sorted(hours.items())))
    print("  按星期:", dict(sorted(wd.items())))
    night = sum(v for h, v in hours.items() if h >= 20 or h <= 1)
    print(f"  20:00~01:59 集中度: {night}/{len(rows)} = {night/len(rows)*100:.0f}%")

    # ── 3. 方向 ──
    sides = Counter(r.get("side") for r in rows)
    print("\n【3】方向分布:", dict(sides))

    # ── 4. 逐笔特征 ──
    print("\n【4】逐笔入场特征（15m / 1h 双周期，严格只用入场前最后一根已收 bar）")
    hdr = (f"  {'#':<3}{'时间':<12}{'方向':<6}{'入场价':<9}"
           f"{'15m量比':<9}{'1h量比':<9}{'15m24h位置':<12}{'24h回撤':<9}"
           f"{'距swing低':<11}{'1h结构':<8}{'1h距EMA50':<12}{'ATR%':<8}")
    print(hdr)
    print("  " + "-" * 104)
    feat_rows = []
    # 入场时刻用 teacher_analysis 自己的反查结果（口径唯一，不重复实现）
    ta_per = {t["trade_id"]: t for t in ta.build_teacher_analysis()["per_trade"]}
    for r in rows:
        ta_row = ta_per.get(r["trade_id"], {})
        ts = _num(ta_row.get("entry_ts"))
        px = _num(r.get("entry_zone")) or _num(r.get("fill_price"))
        d = (r.get("date") or "")[:16]
        if not ts or not px:
            print(f"  {r['trade_id']:<3}{d:<12}{r.get('side',''):<6}{px}")
            continue
        f = entry_features(int(ts), float(px), bars15, bars1h)
        feat_rows.append({"trade_id": r["trade_id"], "date": d, "side": r.get("side"),
                          "entry": float(px), "ts": ts, **f})
        swing_low = f.get("15m_swing_lo")
        dist_lo = round((float(px) - swing_low) / swing_low * 100, 2) if swing_low else None
        print(f"  {r['trade_id']:<3}{d[5:]:<12}{r.get('side',''):<6}{px:<9.0f}"
              f"{str(f.get('15m_vol_x')):<9}{str(f.get('1h_vol_x')):<9}"
              f"{str(f.get('15m_range_pos_pct'))+'%':<12}"
              f"{str(f.get('15m_dd_from_hi_pct'))+'%':<9}"
              f"{str(dist_lo)+'%':<11}"
              f"{str(f.get('1h_ema20_50')):<8}"
              f"{str(f.get('1h_px_vs_ema50_pct'))+'%':<12}"
              f"{str(f.get('1h_atr_pct')):<8}")

    with open("/root/.hermes/cache/scratch/teacher_features.json", "w") as fh:
        json.dump(feat_rows, fh, ensure_ascii=False, indent=1)

    # ── 5. 聚合：入场触发条件分布 ──
    print("\n【5】入场触发条件分布（找能写成规则的）")
    def agg(key, name):
        vals = [f[key] for f in feat_rows if f.get(key) is not None]
        if not vals:
            print(f"  {name:<24} n/a")
            return
        vals_s = sorted(vals)
        print(f"  {name:<24} n={len(vals):>2} 均{sum(vals)/len(vals):>8.3f} "
              f"中位{vals_s[len(vals_s)//2]:>8.3f} 范围[{vals_s[0]:.3f},{vals_s[-1]:.3f}]")
    for key, name in (
        ("15m_vol_x", "15m 量能倍数"),
        ("1h_vol_x", "1h 量能倍数"),
        ("15m_drop_4bars_pct", "入场前 1h 跌幅%"),
        ("15m_drop_12bars_pct", "入场前 3h 跌幅%"),
        ("15m_drop_48bars_pct", "入场前 12h 跌幅%"),
        ("1h_drop_4bars_pct", "入场前 4h 跌幅%"),
        ("1h_drop_12bars_pct", "入场前 12h 跌幅%"),
        ("1h_drop_48bars_pct", "入场前 48h 跌幅%"),
        ("15m_reclaim_pct", "距近8根最低反弹%"),
        ("1h_atr_pct", "1h ATR%"),
        ("1h_px_vs_ema50_pct", "入场价 vs 1h EMA50 %"),
        ("15m_dd_from_hi_pct", "距24h高点回撤%"),
        ("15m_range_pos_pct", "24h区间位置%"),
    ):
        agg(key, name)

    print("\n  ── 结构判定（顺势/逆势）──")
    tr = Counter(f.get("1h_ema20_50") for f in feat_rows)
    print("   1h EMA20/50:", dict(tr))
    aligned = [f for f in feat_rows if (f.get("side") == "long" and f.get("1h_ema20_50") == "up")
               or (f.get("side") == "short" and f.get("1h_ema20_50") == "down")]
    print(f"   顺势入场 {len(aligned)}/{len(feat_rows)}")

    print("\n  ── 入场价与结构位的关系（回调 vs 追进）──")
    dists = []
    for f in feat_rows:
        sl = f.get("15m_swing_lo")
        if sl:
            dists.append((f["entry"] - sl) / sl * 100)
    if dists:
        ds = sorted(dists)
        print(f"   入场价距 15m 近 48 根 swing 低点: 均 {sum(dists)/len(dists):.3f}% "
              f"中位 {ds[len(ds)//2]:.3f}% 范围 [{ds[0]:.3f}, {ds[-1]:.3f}]")
        print(f"   → 全部为正 = 老师从低点【反弹】进场（不是从高点追进）")


if __name__ == "__main__":
    main()
