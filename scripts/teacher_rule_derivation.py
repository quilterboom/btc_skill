#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
teacher_rule_derivation.py —— 从老师自己的 20 笔，逐条件反推「规则的阈值该定在哪」

## 为什么必须先做这一步
上一版复刻规则（teacher_replica.py）的阈值是**我拍的**（drop_min=1.0 / drop_look=12），
结果只命中老师 30% 的单 —— 说明阈值口径和老师实际画像对不上。

正确顺序：
    ① 先看老师每笔入场时，各条件的实际值分布   ← 本脚本
    ② 用【老师自己的分布】定阈值（不是用 PnL 拟合阈值）
    ③ 再拿这个规格去长历史检验有没有 edge

**★ 关键：阈值必须从老师画像来，不能从回测收益来。**
用 PnL 扫阈值 = 在历史噪声上拟合（btc-strategy-dev 步骤 7）。

## 本脚本产出
每个候选条件在老师 20 笔上的实际取值分布 + 覆盖率（多少比例的老师单满足条件）
→ 阈值取「覆盖 80~90% 老师单」的档位。

## 只读
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, "/root/skills/btc-gate-intraday-strategy/web")

import teacher_analysis as ta
from teacher_replica import SigGen, load_bars

CST = timezone(timedelta(hours=8))


def teacher_entries():
    rows = [json.loads(l) for l in open("/root/data/teacher_trades.jsonl") if l.strip()]
    rows.sort(key=lambda r: r["trade_id"])
    per = {t["trade_id"]: t for t in ta.build_teacher_analysis()["per_trade"]}
    out = []
    for r in rows:
        pr = per.get(r["trade_id"], {})
        ts = pr.get("entry_ts")
        px = r.get("entry_zone") or r.get("fill_price")
        if isinstance(px, str):
            px = float(px.split("-")[0].split("~")[0])
        if not ts or not px:
            continue
        out.append({"id": r["trade_id"], "ts": int(ts), "px": float(px),
                    "side": r.get("side"), "net": (r.get("result") or {}).get("net_usd")})
    return out


def feats_at(sg, ts, px, side):
    """在【入场时刻】算各条件值。★ 只用 ts 之前最后一根已收 bar。"""
    i = None
    for k, b in enumerate(sg.bars):
        if int(b[0]) >= ts:
            i = k
            break
    if i is None:
        i = len(sg.bars) - 1
    i = max(0, i - 1)
    if i < 60:
        return {}
    f = {}
    # 结构
    e20, e50 = sg.e20[i], sg.e50[i]
    if e20 and e50:
        f["struct"] = "up" if e20 > e50 * 1.0005 else ("down" if e20 < e50 * 0.9995 else "flat")
    # 跌幅（多档 lookback，单位小时）
    for hrs in (1, 3, 6, 12, 24):
        look = int(hrs * 60 / sg.tf)
        if i < look:
            continue
        f[f"drop_{hrs}h%"] = round((sg.close[i - look] - sg.close[i]) / sg.close[i - look] * 100, 3)
    # 量能
    vx = sg.vol_x(i)
    if vx is not None:
        f["vol_x"] = vx
    # 距 swing 低/高
    for look_hrs, tag in ((12, "12h"), (24, "24h")):
        look = int(look_hrs * 60 / sg.tf)
        if i < look:
            continue
        lo = min(sg.low[j] for j in range(i - look + 1, i + 1))
        hi = max(sg.high[j] for j in range(i - look + 1, i + 1))
        if side == "long":
            f[f"dist_low_{tag}%"] = round((px - lo) / lo * 100, 3)
            f[f"pos_{tag}%"] = round((px - lo) / (hi - lo) * 100, 1) if hi > lo else None
        else:
            f[f"dist_high_{tag}%"] = round((hi - px) / px * 100, 3)
    # ATR%
    if sg.atr[i]:
        f["atr%"] = round(sg.atr[i] / sg.close[i] * 100, 3)
    # 入场价 vs EMA50
    if e50:
        f["px_vs_ema50%"] = round((px - e50) / e50 * 100, 3)
    return f


def coverage(name, vals, thresholds, higher_is_pass=True):
    """阈值覆盖率：多少比例的老师单满足条件。"""
    out = []
    for th in thresholds:
        if higher_is_pass:
            n = sum(1 for v in vals if v is not None and v >= th)
        else:
            n = sum(1 for v in vals if v is not None and v <= th)
        out.append((th, n, n / len(vals) * 100 if vals else 0))
    return out


def main():
    entries = teacher_entries()
    print("=" * 108)
    print(f"从老师 {len(entries)} 笔反推规则阈值（阈值取自老师自己的分布，不从 PnL 拟合）")
    print("=" * 108)

    for tf, tf_min in (("1h", 60), ("15m", 15)):
        sg = SigGen(load_bars(tf), tf_min)
        span = f"{datetime.fromtimestamp(sg.tss[0],CST):%Y-%m-%d} → {datetime.fromtimestamp(sg.tss[-1],CST):%Y-%m-%d}"
        print(f"\n{'#'*108}\n【{tf}】{span} | 老师 {len(entries)} 笔\n{'#'*108}")

        allf = []
        for e in entries:
            f = feats_at(sg, e["ts"], e["px"], e["side"])
            f["_id"] = e["id"]
            f["_net"] = e["net"]
            allf.append(f)

        # ── 结构 ──
        structs = [f.get("struct") for f in allf]
        print("\n  ── 结构（1h EMA20/50）──")
        print(f"    down {structs.count('down')} / up {structs.count('up')} / flat {structs.count('flat')}")
        # 顺势 = long+up 或 short+down
        al = sum(1 for f, e in zip(allf, entries)
                 if (e["side"] == "long" and f.get("struct") == "up")
                 or (e["side"] == "short" and f.get("struct") == "down"))
        print(f"    顺势入场 {al}/{len(entries)} = {al/len(entries)*100:.0f}%  → 逆势为主 ✓")

        # ── 跌幅（多档）──
        print("\n  ── 入场前跌幅%（按 lookback 小时数分档）──")
        for hrs in (1, 3, 6, 12, 24):
            key = f"drop_{hrs}h%"
            vals = [f.get(key) for f in allf if f.get(key) is not None]
            if not vals:
                continue
            sv = sorted(vals)
            print(f"    {key:<12} n={len(vals):>2} 均{sum(vals)/len(vals):>7.3f} 中位{sv[len(sv)//2]:>7.3f} "
                  f"范围[{sv[0]:.3f},{sv[-1]:.3f}]")
            ths = [0.5, 1.0, 1.5, 2.0, 2.5, 3.0]
            cov = coverage(key, vals, ths)
            print("      覆盖率: " + "  ".join(f"≥{t}={p:.0f}%" for t, n, p in cov))

        # ── 量能 ──
        print("\n  ── 量能倍数（入场前一根 vs 前 20 根均量）──")
        vals = [f.get("vol_x") for f in allf if f.get("vol_x") is not None]
        if vals:
            sv = sorted(vals)
            print(f"    n={len(vals)} 均{sum(vals)/len(vals):.3f} 中位{sv[len(sv)//2]:.3f} "
                  f"范围[{sv[0]:.3f},{sv[-1]:.3f}]")
            print("      覆盖率: " + "  ".join(
                f"≥{t}={p:.0f}%" for t, n, p in coverage("vol_x", vals, [0.8, 1.0, 1.2, 1.5, 2.0])))

        # ── 距 swing 低 ──
        print("\n  ── 入场价距近期 swing 低点%（做多单）──")
        for tag in ("12h", "24h"):
            key = f"dist_low_{tag}%"
            vals = [f.get(key) for f in allf if f.get(key) is not None]
            if not vals:
                continue
            sv = sorted(vals)
            print(f"    {key:<14} n={len(vals):>2} 均{sum(vals)/len(vals):>7.3f} 中位{sv[len(sv)//2]:>7.3f} "
                  f"范围[{sv[0]:.3f},{sv[-1]:.3f}]")
            print("      覆盖率(≤): " + "  ".join(
                f"≤{t}={p:.0f}%" for t, n, p in coverage(key, vals, [0.0, 0.3, 0.6, 1.0, 2.0], False)))

        # ── 区间位置 ──
        print("\n  ── 入场价在 24h 区间的位置%（做多单）──")
        vals = [f.get("pos_24h%") for f in allf if f.get("pos_24h%") is not None]
        if vals:
            sv = sorted(vals)
            print(f"    n={len(vals)} 均{sum(vals)/len(vals):.1f} 中位{sv[len(sv)//2]:.1f} "
                  f"范围[{sv[0]:.1f},{sv[-1]:.1f}]")
            print("      覆盖率(≤): " + "  ".join(
                f"≤{t}={p:.0f}%" for t, n, p in coverage("pos_24h%", vals, [20, 40, 60, 80], False)))

        # ── vs EMA50 ──
        print("\n  ── 入场价 vs 1h EMA50 %──")
        vals = [f.get("px_vs_ema50%") for f in allf if f.get("px_vs_ema50%") is not None]
        if vals:
            sv = sorted(vals)
            print(f"    n={len(vals)} 均{sum(vals)/len(vals):.3f} 中位{sv[len(sv)//2]:.3f} "
                  f"范围[{sv[0]:.3f},{sv[-1]:.3f}]")
            print("      覆盖率(≤): " + "  ".join(
                f"≤{t}={p:.0f}%" for t, n, p in coverage("px_vs_ema50%", vals, [-2.0, -1.0, 0.0, 1.0], False)))

        # ── ATR ──
        print("\n  ── 1h ATR%（SL 宽度的天然尺度）──")
        vals = [f.get("atr%") for f in allf if f.get("atr%") is not None]
        if vals:
            sv = sorted(vals)
            print(f"    n={len(vals)} 均{sum(vals)/len(vals):.4f} 中位{sv[len(sv)//2]:.4f} "
                  f"范围[{sv[0]:.4f},{sv[-1]:.4f}]")


if __name__ == "__main__":
    main()
