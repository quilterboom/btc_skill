#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
teacher_analysis.py
===================
老师单「盈利记录 + 入场行情」分析引擎（纯计算，无网络、无写副作用）。

数据源（全部本地，只读）：
  - /root/data/teacher_trades.jsonl      老师单台账（15 笔，#2 是区间成交无唯一入场价）
  - /root/data/BTC_USDT_klines.json      单文件 K 线缓存（1m/5m/15m/1h/4h/1d）

为什么要反查入场时刻：
  teacher_trades.jsonl 里的 events[].ts 是 liusir 口头口径（"2026-10-09 (持仓中)"、
  "2026-10-16 ~17:30 左右"），机器不可解析。**绝不改写 ts**（见 btc-position-ops §4.5），
  改用「入场价 + 当日 K 线」反查真实可成交时刻，derived 与 declared 分开存，互不覆盖。

输出：
  build_teacher_analysis() -> dict  （给 web/server.py 直接返回给前端）
  CLI: python3 teacher_analysis.py [--json]
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone, timedelta
from typing import Optional

CST = timezone(timedelta(hours=8))
DATA_DIR = "/root/data"
TRADES_PATH = os.path.join(DATA_DIR, "teacher_trades.jsonl")
KLINES_PATH = os.path.join(DATA_DIR, "BTC_USDT_klines.json")
CONTRACT = "BTC_USDT"

# 反查窗口：入场价在当日 15m K 线里的可成交簇可能跨多根，取最早一根
TF_MAIN = "15m"       # 主口径：覆盖 08-06 至今，老师单全部单子都在区间内
TF_HIST = "1h"        # 辅助：10-15 起才有，早期单子会缺（不硬填）
LOOKBACK_H = 24       # 「入场前 24h」用于算回撤/区间位置
EMA_FAST, EMA_SLOW = 20, 50


# ---------------------------------------------------------------- 基础工具
def _load_json(path: str):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _load_trades() -> list:
    if not os.path.exists(TRADES_PATH):
        return []
    out = []
    with open(TRADES_PATH, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def _bars(interval: str) -> list:
    """返回升序 [[ts,o,h,l,c,v,...]]，缺文件/缺周期返回 []。"""
    if not os.path.exists(KLINES_PATH):
        return []
    try:
        store = _load_json(KLINES_PATH)
    except Exception:
        return []
    node = (store.get(CONTRACT) or {}).get(interval) or {}
    rows = node.get("bars") or []
    return [r for r in rows if isinstance(r, list) and len(r) >= 5]


def _ema(values: list, n: int) -> Optional[float]:
    """标准 EMA（alpha=2/(n+1)），够用即可，跟 indicators.ema 同口径近似。"""
    if len(values) < n:
        return None
    k = 2.0 / (n + 1)
    e = sum(values[:n]) / n
    for v in values[n:]:
        e = v * k + e * (1 - k)
    return e


def _parse_declared_ts(raw: str) -> Optional[int]:
    """从 liusir 口头 ts 里尽力抽一个 epoch（抽不出就 None，绝不臆造）。"""
    if not raw:
        return None
    s = str(raw)
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(s.strip()[:len(datetime.now().strftime(fmt))], fmt)
            return int(dt.replace(tzinfo=CST).timestamp())
        except ValueError:
            continue
    return None


def _declared_day(raw: str) -> Optional[str]:
    """口头 ts 里的日期（MM-DD），用于限定反查当天。"""
    if not raw:
        return None
    s = str(raw).strip()
    if len(s) >= 10 and s[4] == "-" and s[7] == "-":
        return s[5:10]
    return None


def _fill_price(trade: dict) -> Optional[float]:
    """入场价。区间成交（'82400-82600 (区间成交)'）返回 None —— 无法反查就不猜。"""
    px = trade.get("fill_price")
    if isinstance(px, (int, float)):
        return float(px)
    if isinstance(px, str):
        head = px.split("(")[0].strip()
        try:
            if "-" in head:
                lo, hi = head.split("-", 1)
                return (float(lo) + float(hi)) / 2.0
            return float(head)
        except ValueError:
            return None
    return None


def _entry_event(trade: dict) -> dict:
    for e in trade.get("events") or []:
        if e.get("type") == "entry":
            return e
    return {}


# ---------------------------------------------------------------- 反查入场时刻
def _resolve_entry_ts(trade: dict, bars15: list) -> dict:
    """
    用 entry_px + 当日 K 线反查真实入场时刻。

    返回 {ts, source, matched_bars}
      source = "kline"   价位在当日 15m K 线里可成交（反查成功）
      source = "declared" 价位反查不到，但口头 ts 里有可解析时间
      source = "unknown"  两者都没有
    """
    ev = _entry_event(trade)
    declared_raw = ev.get("ts") or trade.get("date") or ""
    px = _fill_price(trade)

    if px is not None and bars15:
        day = _declared_day(str(declared_raw))
        hits = []
        for b in bars15:
            # 列顺序 [ts,o,h,l,c,v,sum]
            try:
                lo, hi = float(b[3]), float(b[2])
            except (TypeError, ValueError):
                continue
            if lo <= px <= hi:
                if day:
                    ts_str = datetime.fromtimestamp(int(b[0]), CST).strftime("%m-%d")
                    if ts_str != day:
                        continue
                hits.append(b)
        if hits:
            return {"ts": int(hits[0][0]), "source": "kline",
                    "matched_bars": len(hits), "declared_raw": declared_raw}

    dt = _parse_declared_ts(str(declared_raw))
    if dt is not None:
        return {"ts": dt, "source": "declared", "matched_bars": 0, "declared_raw": declared_raw}
    return {"ts": None, "source": "unknown", "matched_bars": 0, "declared_raw": declared_raw}


# ---------------------------------------------------------------- 入场行情上下文
def _entry_context(ts: Optional[int], px: Optional[float], bars15: list, bars1h: list) -> dict:
    """
    入场时刻的市场结构画像。ts 为 None 时全部返回 None（不臆造）。
    """
    ctx = {
        "dd_from_high_pct": None,      # 距入场前 24h 最高点的回撤 %
        "pre4h_pct": None,             # 入场前 4h 涨跌幅 %
        "range_pos_pct": None,         # 入场价在入场前 24h 区间的位置 %（0=最低 100=最高）
        "vol_x": None,                 # 入场前一根 15m 量能 / 前 20 根均量
        "trend": None,                 # 15m EMA20 vs EMA50 结构
        "aligned": None,               # 是否顺势（多单在多头结构 / 空单在空头结构）
    }
    if ts is None or not bars15:
        return ctx

    idx = None
    for i, b in enumerate(bars15):
        if int(b[0]) >= ts:
            idx = i
            break
    if idx is None:
        idx = len(bars15) - 1
    if idx < 5:
        return ctx

    win = [b for b in bars15[max(0, idx - 96):idx + 1]]      # 24h = 96 根 15m
    if len(win) < 8:
        return ctx

    highs = [float(b[2]) for b in win]
    lows = [float(b[3]) for b in win]
    hi, lo = max(highs), min(lows)
    entry = px if px is not None else float(bars15[idx][4])

    if hi > 0:
        ctx["dd_from_high_pct"] = round((hi - entry) / hi * 100, 2)
    if hi > lo:
        ctx["range_pos_pct"] = round((entry - lo) / (hi - lo) * 100, 1)

    if idx >= 4:
        c_now = float(bars15[idx][4])
        c_4h = float(bars15[idx - 4][4])
        if c_4h:
            ctx["pre4h_pct"] = round((c_now - c_4h) / c_4h * 100, 2)

    if idx >= 21:
        vols = [float(b[5]) for b in bars15[idx - 20:idx] if len(b) > 5]
        cur_v = float(bars15[idx][5]) if len(bars15[idx]) > 5 else None
        if vols and cur_v and sum(vols) / len(vols) > 0:
            ctx["vol_x"] = round(cur_v / (sum(vols) / len(vols)), 2)

    closes = [float(b[4]) for b in bars15[:idx + 1]]
    e20, e50 = _ema(closes, EMA_FAST), _ema(closes, EMA_SLOW)
    if e20 is not None and e50 is not None:
        if e20 > e50 * 1.0005:
            ctx["trend"] = "up"
        elif e20 < e50 * 0.9995:
            ctx["trend"] = "down"
        else:
            ctx["trend"] = "flat"
    return ctx


def _classify_exit(trade: dict) -> str:
    """出场模式归类（基于 events 类型，不臆测）。"""
    types = [e.get("type") for e in trade.get("events") or []]
    has_sl = any(t in ("sl_triggered", "close_sl") for t in types)
    has_tp2 = any(t in ("tp2_full", "tp2_partial") for t in types)
    has_tp1 = "tp1_partial" in types
    has_be = any(t in ("sl_breakeven", "breakeven_stop") for t in types)
    if has_sl and not has_tp1:
        return "止损出场"
    if has_tp2:
        return "TP1+TP2 止盈"
    if has_tp1 and has_be:
        return "TP1 减半+保本"
    if has_tp1:
        return "TP1 减仓"
    if has_be:
        return "保本出场"
    return "其他"


# ---------------------------------------------------------------- 主入口
def build_teacher_analysis() -> dict:
    trades = _load_trades()
    bars15 = _bars(TF_MAIN)
    bars1h = _bars(TF_HIST)

    rows = []
    for t in trades:
        tid = t.get("trade_id")
        res = t.get("result") or {}
        net = res.get("net_usd")
        settled = net is not None
        px = _fill_price(t)
        ev = _entry_event(t)
        resolved = _resolve_entry_ts(t, bars15)
        ctx = _entry_context(resolved["ts"], px, bars15, bars1h)

        side = t.get("side")
        trend = ctx.get("trend")
        aligned = None
        if trend in ("up", "down"):
            aligned = (side == "long" and trend == "up") or (side == "short" and trend == "down")

        rows.append({
            "trade_id": tid,
            "date": t.get("date"),
            "side": side,
            "entry_px": px,
            "tp1": t.get("tp1"),
            "tp2": t.get("tp2"),
            "entry_ts": resolved["ts"],
            "entry_ts_source": resolved["source"],
            "declared_ts": resolved["declared_raw"],
            "matched_bars": resolved["matched_bars"],
            "net_usd": net,
            "settled": settled,
            "outcome": res.get("outcome"),
            "win": res.get("win"),
            "exit_mode": _classify_exit(t),
            "ctx": ctx,
            "aligned": aligned,
        })

    settled_rows = [r for r in rows if r["settled"]]
    wins = [r for r in settled_rows if (r.get("net_usd") or 0) > 0]
    losses = [r for r in settled_rows if (r.get("net_usd") or 0) < 0]
    flats = [r for r in settled_rows if (r.get("net_usd") or 0) == 0]
    total_net = round(sum(r["net_usd"] for r in settled_rows), 2)
    n = len(settled_rows)

    gross_win = sum(r["net_usd"] for r in wins)
    gross_loss = abs(sum(r["net_usd"] for r in losses))

    # ── 战绩汇总
    summary = {
        "total": len(rows),
        "settled": n,
        "pending": len(rows) - n,
        "wins": len(wins),
        "losses": len(losses),
        "flats": len(flats),
        "win_rate_pct": round(len(wins) / n * 100, 1) if n else None,
        "net_usd": total_net,
        "avg_usd": round(total_net / n, 2) if n else None,
        "best_usd": max((r["net_usd"] for r in settled_rows), default=None),
        "worst_usd": min((r["net_usd"] for r in settled_rows), default=None),
        "payoff": round(gross_win / gross_loss, 2) if gross_loss > 0 else None,
        "gross_win": round(gross_win, 2),
        "gross_loss": round(gross_loss, 2),
    }

    # ── 出场模式分布
    exit_modes = {}
    for r in settled_rows:
        m = exit_modes.setdefault(r["exit_mode"], {"mode": r["exit_mode"], "n": 0, "net": 0.0})
        m["n"] += 1
        m["net"] = round(m["net"] + r["net_usd"], 2)
    for m in exit_modes.values():
        m["avg"] = round(m["net"] / m["n"], 2) if m["n"] else 0.0
    exit_modes = sorted(exit_modes.values(), key=lambda x: -x["n"])

    # ── 入场画像（只统计反查成功的单，declared-only 单不参与，避免口径污染）
    ctx_rows = [r for r in rows if r["entry_ts_source"] == "kline" and r["ctx"]["range_pos_pct"] is not None]
    profile = {"n": len(ctx_rows), "dd_from_high_pct": None, "range_pos_pct": None,
               "pre4h_pct": None, "vol_x": None, "aligned": None, "aligned_win_rate": None,
               "counter_win_rate": None}
    if ctx_rows:
        def avg(key):
            vals = [r["ctx"][key] for r in ctx_rows if r["ctx"][key] is not None]
            return round(sum(vals) / len(vals), 2) if vals else None
        profile["dd_from_high_pct"] = avg("dd_from_high_pct")
        profile["range_pos_pct"] = avg("range_pos_pct")
        profile["pre4h_pct"] = avg("pre4h_pct")
        profile["vol_x"] = avg("vol_x")
        al = [r for r in ctx_rows if r["aligned"] is True and r["settled"]]
        co = [r for r in ctx_rows if r["aligned"] is False and r["settled"]]
        profile["aligned"] = len(al)
        profile["counter"] = len(co)
        if al:
            profile["aligned_win_rate"] = round(len([r for r in al if (r["net_usd"] or 0) > 0]) / len(al) * 100, 1)
        if co:
            profile["counter_win_rate"] = round(len([r for r in co if (r["net_usd"] or 0) > 0]) / len(co) * 100, 1)

    # ── 每笔净利（按单号升序，给柱状图）
    per_trade = [{
        "trade_id": r["trade_id"], "date": r["date"], "side": r["side"],
        "net_usd": r["net_usd"], "settled": r["settled"], "exit_mode": r["exit_mode"],
        "entry_px": r["entry_px"], "entry_ts": r["entry_ts"], "entry_ts_source": r["entry_ts_source"],
        "range_pos_pct": r["ctx"]["range_pos_pct"], "dd_from_high_pct": r["ctx"]["dd_from_high_pct"],
        "pre4h_pct": r["ctx"]["pre4h_pct"], "vol_x": r["ctx"]["vol_x"], "trend": r["ctx"]["trend"],
        "aligned": r["aligned"], "outcome": r["outcome"],
    } for r in sorted(rows, key=lambda x: x["trade_id"] or 0)]

    return {
        "ts": int(datetime.now().timestamp()),
        "ts_str": datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S"),
        "summary": summary,
        "exit_modes": exit_modes,
        "profile": profile,
        "per_trade": per_trade,
        "source": {
            "trades": TRADES_PATH,
            "klines": KLINES_PATH,
            "tf_main": TF_MAIN,
            "derived_bars": len(bars15),
        },
    }


if __name__ == "__main__":
    data = build_teacher_analysis()
    if "--json" in sys.argv:
        print(json.dumps(data, ensure_ascii=False, indent=2))
    else:
        s = data["summary"]
        print("战绩:", json.dumps(s, ensure_ascii=False))
        print("出场模式:", json.dumps(data["exit_modes"], ensure_ascii=False))
        print("入场画像:", json.dumps(data["profile"], ensure_ascii=False))
        for r in data["per_trade"]:
            c = r
            print("  #%-3s %-5s net=%-8s src=%-8s 位置=%-6s 回撤=%-6s 前4h=%-7s 量比=%-5s 结构=%-5s %s" % (
                r["trade_id"], r["side"], r["net_usd"], r["entry_ts_source"],
                r["range_pos_pct"], r["dd_from_high_pct"], r["pre4h_pct"], r["vol_x"], r["trend"],
                r["exit_mode"]))
