#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
每笔亏钱单用 v2 重判当时的判定
liusir 2026-10-13 拍板：亏钱单子逐个拆解找稳定点
"""
import json
import os
import sys
import urllib.request
from datetime import datetime
from pathlib import Path

sys.path.insert(0, '/root/skills/btc-gate-intraday-strategy/scripts')
from trader_view import analyze_tf, aggregate_bias

GATE_BASE = "https://api.gateio.ws/api/v4/futures/usdt"


def fetch_batched(interval, total, contract="BTC_USDT"):
    all_bars = []
    end_ts = int(datetime.now().timestamp())
    while len(all_bars) < total:
        limit = min(1000, total - len(all_bars))
        url = f"{GATE_BASE}/candlesticks?contract={contract}&interval={interval}&limit={limit}&to={end_ts}"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=15) as r:
            batch = json.loads(r.read())
        if not batch:
            break
        batch.reverse()
        all_bars.extend(batch)
        end_ts = int(float(batch[0]["t"])) - 1
        if len(batch) < limit:
            break
    seen = set()
    uniq = []
    for b in all_bars:
        t = int(float(b["t"]))
        if t in seen: continue
        seen.add(t)
        uniq.append(b)
    return sorted(uniq, key=lambda b: float(b["t"]))


def main():
    # 拉数据
    print("      拉数据 ...")
    bars_4h = fetch_batched("4h", 600)
    bars_1h = fetch_batched("1h", 1500)
    bars_15m = fetch_batched("15m", 3000)
    print(f"      4h={len(bars_4h)} 1h={len(bars_1h)} 15m={len(bars_15m)}")
    print()

    # 生产 journal 亏钱单
    prod = []
    with open("/root/data/journal.jsonl") as f:
        for line in f:
            if line.strip():
                r = json.loads(line)
                if "ts" in r and r.get("status") in ("settled", "invalidated"):
                    prod.append(r)
    prod.sort(key=lambda r: r["ts"])

    loss_trades = []
    for r in prod:
        if r.get("status") != "settled":
            continue
        net = r.get("net_usd", 0) or 0
        if net < 0:
            loss_trades.append({
                "id": r["id"],
                "ts": r["ts"],
                "ts_str": datetime.fromtimestamp(r["ts"]).strftime("%m-%d %H:%M"),
                "side": r["side"],
                "entry": r["entry"],
                "exit": r.get("exit_px"),
                "outcome": r.get("outcome"),
                "net_usd": net,
                "verdict": r.get("verdict", "")[:50],
                "score": r.get("score"),
                "signal_source": r.get("meta", {}).get("signal_source", "?"),
            })

    print(f"=== 生产 journal 亏钱单 {len(loss_trades)} 单 ===")
    total_loss = sum(t["net_usd"] for t in loss_trades)
    for t in loss_trades:
        print(f"  {t['ts_str']} {t['side']:<5} entry={t['entry']:>8.1f} exit={t['exit']:>8.1f} "
              f"outcome={t['outcome']:<13} net={t['net_usd']:+6.2f}U  {t['verdict']}")
    print(f"总亏损: {total_loss:+.2f}U")

    # 训练版
    print()
    print("=== 训练版 journal (6 单) ===")
    with open("/root/data/training/journal.jsonl") as f:
        for line in f:
            if line.strip():
                r = json.loads(line)
                print(f"  {datetime.fromtimestamp(r['ts']).strftime('%m-%d %H:%M')} {r['side']:<5} "
                      f"entry={r.get('entry', 0):>8.1f} exit={r.get('exit_px', 0):>8.1f} "
                      f"outcome={r.get('outcome', '?'):<13} net={r.get('net_usd', 0):+6.2f}U")

    # 给每笔亏钱单跑 v2 当时的判定
    print()
    print("=== v2 重判每笔亏钱单当时 ===")
    print()
    log_lines = []
    for t in loss_trades:
        ts = t["ts"]
        sub_4h = [b for b in bars_4h if int(float(b["t"])) <= ts]
        sub_1h = [b for b in bars_1h if int(float(b["t"])) <= ts]
        sub_15m = [b for b in bars_15m if int(float(b["t"])) <= ts]
        if len(sub_4h) < 50 or len(sub_1h) < 50 or len(sub_15m) < 50:
            print(f"  {t['ts_str']}: 数据不足")
            continue
        per_tf = {
            "4h": analyze_tf("4h", sub_4h, float(sub_4h[-1]["c"])),
            "1h": analyze_tf("1h", sub_1h, float(sub_1h[-1]["c"])),
            "15m": analyze_tf("15m", sub_15m, float(sub_15m[-1]["c"])),
        }
        agg = aggregate_bias(per_tf)
        parts = []
        for tf in ["4h", "1h", "15m"]:
            d = per_tf[tf]
            parts.append(f"{tf}:A{d['main_a_score']:+.0f}B{d['main_b_score']:+.0f}C{d['main_c_score']:+.0f}")
        print(f"  {t['ts_str']} {t['side']:<5} entry={t['entry']:>8.0f} net={t['net_usd']:+6.2f}U  src={t['signal_source']}")
        print(f"    v2: bias={agg['bias']:<7} norm={agg['norm']:+.3f} consensus={agg['consensus']}")
        print(f"    周期评分: {' '.join(parts)}")
        print(f"    swing: 4h={per_tf['4h']['swing']['label']} 1h={per_tf['1h']['swing']['label']} 15m={per_tf['15m']['swing']['label']}")
        print(f"    momentum: 4h_RSI={per_tf['4h']['mom_rsi']['rsi']:.0f}/{per_tf['4h']['mom_rsi']['state']} 1h_RSI={per_tf['1h']['mom_rsi']['rsi']:.0f}/{per_tf['1h']['mom_rsi']['state']} 15m_RSI={per_tf['15m']['mom_rsi']['rsi']:.0f}/{per_tf['15m']['mom_rsi']['state']}")
        print(f"    trend: 4h={per_tf['4h']['trend_ema']['trend']} 1h={per_tf['1h']['trend_ema']['trend']} 15m={per_tf['15m']['trend_ema']['trend']}")
        print()

        # 同步存一份给 classify 脚本解析
        log_lines.append(f"  {t['ts_str']} {t['side']:<5} entry= {t['entry']:>8.1f} net= {t['net_usd']:+6.2f}U  src={t['signal_source']}")
        log_lines.append(f"    v2: bias={agg['bias']:<7} norm={agg['norm']:+.3f} consensus={agg['consensus']}")
        log_lines.append(f"    周期评分: {' '.join(parts)}")
        log_lines.append(f"    swing: 4h={per_tf['4h']['swing']['label']} 1h={per_tf['1h']['swing']['label']} 15m={per_tf['15m']['swing']['label']}")
        log_lines.append(f"    momentum: 4h_RSI={per_tf['4h']['mom_rsi']['rsi']:.0f}/{per_tf['4h']['mom_rsi']['state']} 1h_RSI={per_tf['1h']['mom_rsi']['rsi']:.0f}/{per_tf['1h']['mom_rsi']['state']} 15m_RSI={per_tf['15m']['mom_rsi']['rsi']:.0f}/{per_tf['15m']['mom_rsi']['state']}")
        log_lines.append(f"    trend: 4h={per_tf['4h']['trend_ema']['trend']} 1h={per_tf['1h']['trend_ema']['trend']} 15m={per_tf['15m']['trend_ema']['trend']}")
        log_lines.append("")

    # 写日志到 /tmp 给 classify 脚本
    with open("/tmp/postmortem_full.log", "w") as f:
        f.write("\n".join(log_lines))
    print(f"\n[日志已存] /tmp/postmortem_full.log  共 {len(loss_trades)} 笔亏钱单")


if __name__ == "__main__":
    main()