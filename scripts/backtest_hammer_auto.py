#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
hammer 自动挂单模式回测（2026-10-17 改造后）

目的：在用实盘跑出 5 笔之前，先用历史 1m K 模拟 hammer 自动下单的 PnL/胜率，
      跟 teacher 单历史战绩（7/8 胜 +195U）和主信号近 12 笔（33% 胜率 +4.5R）做对比。

用法:
    cd /root/skills/btc-gate-intraday-strategy/scripts
    python3 backtest_hammer_auto.py [--days 4] [--balance 50] [--risk 0.01]

输入:
    /root/data/cache/BTC_USDT_1m_5000.json (fetch_ohlcv 拉 1m K)

输出:
    - hammer 命中率 (每 24h 几个信号)
    - 模拟成交笔数 / 胜率 / 平均净 U / 累计 PnL / 最大回撤
    - 按 hit_type (TP1/TP2/SL) 分桶
    - 跟 teacher 单历史 + 1h 主信号近 12 笔做对照
"""
from __future__ import annotations
import sys, os, json, argparse, time
from collections import defaultdict, Counter
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import gate_fetch
from gate_fetch import fetch_ohlcv

# ============================ hammer 检测（回测专用 inline 版） ============================
# 不调 scan._detect_left_side_long 是因为它内部 cache=False 走 HTTP，monkey-patch 不生效。
# 回测版完全 inline 用传入的 bars_1m 检测，结果与生产逻辑对齐。
def detect_hammer_in_bars(bars_1m, drop_min_pct=1.5, volx_min=3.0, rebound_min_pct=0.3,
                           min_passed=4, drop_required=False):
    """在 bars_1m 内找 hammer 信号（同 _detect_left_side_long 逻辑）。
    bars_1m 必须是升序 1m K 数组，每根 [ts, o, h, l, c, v]。
    返回 None 或 dict (同 _detect_left_side_long 形态)。

    min_passed: 进程条最少通过数 (默认 4 = 全过)。生产是 3/4 也放行，
                但回测用更严过滤噪音 hammer。
    drop_required: 是否强制 drop_ok=True (drop ≥ drop_min_pct)。
                   True 时即便其他三项满 3/3, drop 不过也不放行。
                   这是激进派配置, 解决 42% 噪音 hammer 的根因。
    """
    if len(bars_1m) < 240:
        return None

    def _is_pin(o, h, l, c):
        rng = h - l
        if rng <= 0: return False
        body = abs(c - o)
        lower = min(o, c) - l
        upper = h - max(o, c)
        bullish = c > o
        if not bullish: return False
        if (lower / rng > 0.4 and upper / rng < 0.3 and body / rng < 0.5): return True
        if (lower / rng > 0.2 and upper / rng < 0.1): return True
        if body > 0 and lower / body > 1.5 and lower / max(upper, 1e-6) > 2: return True
        return False

    pin_candidates = []
    for i in range(len(bars_1m) - 1, max(len(bars_1m) - 240, -1), -1):
        if i < 0: i = 0
        b = bars_1m[i]
        o, h, l, c = float(b[1]), float(b[2]), float(b[3]), float(b[4])
        if _is_pin(o, h, l, c):
            pin_candidates.append((i, b))

    if not pin_candidates:
        return None
    pin_candidates.sort(key=lambda x: x[0], reverse=True)

    for hammer_idx, hammer in pin_candidates:
        pre_4h = bars_1m[max(0, hammer_idx - 240):hammer_idx]
        if not pre_4h: continue
        pre_4h_hi = max(pre_4h, key=lambda b: float(b[2]))
        hi_4h = float(pre_4h_hi[2])
        hammer_ts = int(hammer[0])
        hammer_O, hammer_H, hammer_L, hammer_C = float(hammer[1]), float(hammer[2]), float(hammer[3]), float(hammer[4])
        hammer_v = float(hammer[5])

        drop_pct = (hi_4h - hammer_L) / hi_4h * 100.0
        drop_ok = drop_pct >= drop_min_pct

        pre_1h = bars_1m[max(0, hammer_idx - 60):hammer_idx]
        if not pre_1h: continue
        avg_vol_1h = sum(float(b[5]) for b in pre_1h) / len(pre_1h)
        volx = hammer_v / avg_vol_1h if avg_vol_1h > 0 else 0
        volx_ok = volx >= volx_min

        post_20m = bars_1m[hammer_idx + 1:hammer_idx + 21]
        if not post_20m:
            rebound_pct = 0.0
        else:
            post_hi = max(post_20m, key=lambda b: float(b[2]))
            rebound_pct = (float(post_hi[2]) - hammer_L) / hammer_L * 100.0
        rebound_ok = rebound_pct >= rebound_min_pct

        passed = sum([drop_ok, volx_ok, True, rebound_ok])
        # 激进派：drop_ok 必须过 + 其他三项至少 2/3 (即 passed>=3 但 drop_ok=True)
        # 生产默认：drop_ok 可不过, passed>=3 (噪音 hammer 会被放过)
        if drop_required and not drop_ok:
            continue    # drop 没过直接跳过，不进入下一轮
        if passed >= min_passed:
            entry = round(hammer_H, 1)
            sl = round(hammer_L * 0.999, 1)
            tp1_pct = 0.013
            tp2_pct = 0.029
            tp1 = round(entry * (1 + tp1_pct), 1)
            tp2 = round(entry * (1 + tp2_pct), 1)
            pin_kind = "hammer" if hammer_C > hammer_O and (hammer_C - hammer_O) > 0.3 * (hammer_H - hammer_L) else "pin_bar"
            return {
                "ts": hammer_ts, "hammer_kind": pin_kind,
                "hammer_O": hammer_O, "hammer_H": hammer_H,
                "hammer_L": hammer_L, "hammer_C": hammer_C,
                "drop_pct": round(drop_pct, 2), "volx": round(volx, 2),
                "rebound_pct": round(rebound_pct, 2),
                "entry": entry, "sl": sl, "sl_label": "hammer L-0.1%",
                "tp1": tp1, "tp1_pct": tp1_pct,
                "tp2": tp2, "tp2_pct": tp2_pct,
                "drop_ok": drop_ok, "volx_ok": volx_ok,
                "pin_ok": True, "rebound_ok": rebound_ok,
            }
    return None

# ============================ 配置 ============================
DEFAULT_BARS = 5000   # 4 天 1m K = 5000 根
DEFAULT_BAL = 50.0     # 跟 scan.py 默认一致
DEFAULT_RISK = 0.01    # 单笔风险 1% 账户
DEFAULT_LEV = 100
QUANT_MULT = 0.0001    # Gate.io USDT 本位 BTC 合约 1 张 = 0.0001 BTC
TAKER_FEE = 0.00075    # taker 0.075%
MAKER_FEE = -0.0001    # maker -0.01%（返佣）


def simulate_trade(bars_after_hammer, entry, sl, tp1_pct, tp2_pct, balance=50.0, risk=0.01, lev=100):
    """
    在 hammer 命中之后的 1m K 上模拟成交。

    简化模型（保守）：
      - entry 立即以 market 单成交（实际上 hammer 是 post-only 限价挂单，
        但回测只看结果，不模拟挂单行为）
      - SL 用 maker 触发 (实际: taker 强平 0.075%，但保本 SL 触发距离小，
        误差小到可以忽略)
      - TP1/TP2 用 taker (市价止盈)
      - 仓位变化: TP1 减半推保本 (50% @ TP1, 50% 推 entry → TP2 全走)
      - fee 按名义 × 0.15% (往返 taker 0.075%×2), 不复用 4.85 整笔常量

    Returns: dict with hit_type, exit_px, gross_usd, net_usd, hold_bars
    """
    tp1 = entry * (1 + tp1_pct)
    tp2 = entry * (1 + tp2_pct)
    notional = balance * risk * lev
    contracts = max(1, round(notional / (entry * QUANT_MULT)))
    fee_rate = 0.00075 * 2  # 往返 taker 0.15%
    # fee 按名义算：成交一次收一次 fee，整笔 50% + 100% 名义都收
    fee_round = notional * fee_rate  # ≈ 50 × 0.0015 = 0.075U (单笔平仓总 fee)

    tp1_hit = False
    tp2_hit = False
    sl_hit = False
    exit_px = None
    exit_bar_idx = None

    for i, b in enumerate(bars_after_hammer):
        # b: [ts, o, h, l, c, v]
        ts, o, h, l, c = b[0], float(b[1]), float(b[2]), float(b[3]), float(b[4])

        # SL 触发 (用 low 检查)
        if l <= sl:
            sl_hit = True
            exit_px = sl   # 假设精确触发
            exit_bar_idx = i
            break

        # TP 触发顺序：先看 TP1 减半，再看 SL/TP2
        # TP1 触发 (用 high 检查)
        if not tp1_hit and h >= tp1:
            tp1_hit = True
            # 模拟减半推保本：剩余仓位 SL 移到 entry（保本）
            # 简化：TP1 减半的仓位算 (entry→tp1) 的净，剩下 50% 等 TP2 或新 SL(=entry)
            # 这里先记 TP1 命中，循环继续
            sl = entry   # 推保本
            continue

        # TP2 触发 (TP1 已命中 + 高点 ≥ tp2)
        if tp1_hit and h >= tp2:
            tp2_hit = True
            exit_px = tp2
            exit_bar_idx = i
            break

    if exit_px is None:
        # 60 根 1m = 1 小时都没触发出局，按最差情况按 SL 收尾（保守）
        # 实际应该按 timeout 处理，但回测中没 timeout 概念
        last = float(bars_after_hammer[-1][4]) if bars_after_hammer else entry
        sl_pct = (entry - sl) / entry if entry > 0 else 0.01
        timeout_gross = -sl_pct * notional
        return {
            "hit_type": "TIMEOUT",
            "exit_px": last,
            "tp1_hit": tp1_hit,
            "tp2_hit": False,
            "sl_hit": False,
            "hold_bars": len(bars_after_hammer),
            "gross_usd": timeout_gross,
            "net_usd": timeout_gross - fee_round,
            "note": "60 bars 内未触发",
        }

    # 计算 PnL（fee/contracts 在函数开头已算过）
    # 仓位变化模拟 (TP1 减半 + 推保本 + TP2 全走)
    gross_usd = 0.0
    net_usd = 0.0
    if tp2_hit:
        # 全垒打: 50% @ tp1 + 50% @ tp2
        gross_pct = 0.5 * tp1_pct + 0.5 * tp2_pct
        gross_usd = gross_pct * contracts * entry * QUANT_MULT
        net_usd = gross_usd - fee_round
        hit_type = "TP2_FULL"
    elif tp1_hit and not sl_hit:
        # TP1 命中但未到 TP2，最后 SL=entry 保本出
        # 50% @ tp1 + 50% @ entry (保本)
        gross_pct = 0.5 * tp1_pct
        gross_usd = gross_pct * contracts * entry * QUANT_MULT
        net_usd = gross_usd - fee_round
        hit_type = "TP1_BE"
    elif sl_hit and not tp1_hit:
        # SL 全亏 (risk * balance = 0.5U)
        sl_pct = (entry - sl) / entry
        gross_usd = -sl_pct * contracts * entry * QUANT_MULT
        net_usd = gross_usd - fee_round
        hit_type = "SL"
    else:
        # 兜底 (不该走到这里)
        hit_type = "UNKNOWN"
        gross_usd = 0.0
        net_usd = -fee_round

    return {
        "hit_type": hit_type,
        "exit_px": exit_px,
        "tp1_hit": tp1_hit,
        "tp2_hit": tp2_hit,
        "sl_hit": sl_hit,
        "hold_bars": exit_bar_idx + 1 if exit_bar_idx is not None else len(bars_after_hammer),
        "gross_usd": gross_usd,
        "net_usd": net_usd,
        "note": "",
    }


def run_backtest(bars_1m, balance=50.0, risk=0.01, lev=100, min_passed=3, drop_required=False):
    """
    在 1m K 数组上扫所有时间点 (从 index=240 开始)，看哪些点会触发 hammer 信号，
    然后模拟挂单直到 SL/TP1/TP2。

    min_passed: hammer 进程条最少通过数 (3=生产默认 / 4=全过)
    drop_required: 是否强制 drop_ok=True (过滤 42% 噪音 hammer)

    Returns: list of trade dicts
    """
    trades = []
    seen_hammer_ts = set()    # 去重：同一根 hammer 不重复算

    # 从 240 根开始 (需要 4h 历史)
    for i in range(240, len(bars_1m) - 60):  # 留 60 根 (1h) 给模拟推进
        window = bars_1m[max(0, i - 240):i + 1]
        if len(window) < 240:
            continue

        # inline hammer 检测 (用回测专用函数，不调 scan 那个走 HTTP 的版本)
        sig = detect_hammer_in_bars(window, min_passed=min_passed, drop_required=drop_required)
        if not sig:
            continue

        # 去重：hammer ts 重复就跳过 (同一根 pin 在多个 i 的 window 都会被检测到)
        hammer_ts = sig["ts"]
        if hammer_ts in seen_hammer_ts:
            continue
        seen_hammer_ts.add(hammer_ts)

        # 模拟：bars_after = bars_1m[i+1:]
        bars_after = bars_1m[i + 1:]
        if len(bars_after) < 5:
            continue

        result = simulate_trade(
            bars_after_hammer=bars_after,
            entry=sig["entry"],
            sl=sig["sl"],
            tp1_pct=sig["tp1_pct"],
            tp2_pct=sig["tp2_pct"],
            balance=balance,
            risk=risk,
            lev=lev,
        )

        trades.append({
            "hammer_ts": hammer_ts,
            "hammer_ts_str": datetime.fromtimestamp(hammer_ts, tz=timezone(timedelta(hours=8))).strftime("%m-%d %H:%M"),
            "entry": sig["entry"],
            "sl": sig["sl"],
            "tp1": sig["tp1"],
            "tp2": sig["tp2"],
            "drop_pct": sig["drop_pct"],
            "volx": sig["volx"],
            "rebound_pct": sig["rebound_pct"],
            "hammer_kind": sig["hammer_kind"],
            **result,
        })

    return trades


# ============================ 主回测 ============================
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=4)
    ap.add_argument("--balance", type=float, default=DEFAULT_BAL)
    ap.add_argument("--risk", type=float, default=DEFAULT_RISK)
    ap.add_argument("--lev", type=int, default=DEFAULT_LEV)
    a = ap.parse_args()

    n_bars = a.days * 1440   # 1 day = 1440 mins
    print(f"=== hammer 自动挂单模式回测 ===")
    print(f"区间: {a.days} 天 ({n_bars} 根 1m K)")
    print(f"账户: balance={a.balance} risk={a.risk} lev={a.lev}x")
    print(f"TP1=+1.30% / TP2=+2.90% / SL=hammer_L*0.999 (保本风格)")
    print()

    # 拉数据
    print("拉 1m K 线...")
    t0 = time.time()
    bars = fetch_ohlcv("BTC_USDT", "1m", n_bars, refresh=True, verbose=False)
    print(f"  got {len(bars)} bars ({time.time()-t0:.1f}s)")
    print(f"  区间: {datetime.fromtimestamp(bars[0][0]).strftime('%Y-%m-%d %H:%M')} → "
          f"{datetime.fromtimestamp(bars[-1][0]).strftime('%Y-%m-%d %H:%M')}")
    print()

    # 跑回测 (4 天数据；对照组: loose / strict / drop_forced)
    print("扫 hammer 信号 + 模拟成交...")
    t0 = time.time()
    trades_loose = run_backtest(bars, balance=a.balance, risk=a.risk, lev=a.lev, min_passed=3, drop_required=False)
    trades_strict = run_backtest(bars, balance=a.balance, risk=a.risk, lev=a.lev, min_passed=4, drop_required=False)
    trades_drop_forced = run_backtest(bars, balance=a.balance, risk=a.risk, lev=a.lev, min_passed=3, drop_required=True)
    elapsed = time.time() - t0
    print(f"  完成 ({elapsed:.1f}s)")
    print()

    if not trades_loose and not trades_strict:
        print("⚠️ 没有触发任何 hammer 信号")
        print("   可能原因: 区间内没有足够跌幅 + pin 形态同时满足")
        return

    def _stats(label, trades):
        n = len(trades)
        hit_counter = Counter(t["hit_type"] for t in trades)
        wins = sum(1 for t in trades if t["net_usd"] > 0)
        losses = sum(1 for t in trades if t["net_usd"] <= 0)
        wr = wins / max(wins + losses, 1) * 100
        nets = [t["net_usd"] for t in trades]
        total_net = sum(nets)
        avg_net = total_net / n if n else 0
        max_win = max(nets) if nets else 0
        max_loss = min(nets) if nets else 0
        cum = peak = max_dd = 0.0
        for net in nets:
            cum += net
            peak = max(peak, cum)
            max_dd = max(max_dd, peak - cum)
        span_hours = (bars[-1][0] - bars[0][0]) / 3600
        sig_per_day = n / (span_hours / 24) if span_hours > 0 else 0
        return {
            "label": label, "n": n, "wr": wr, "total_net": total_net,
            "avg_net": avg_net, "max_win": max_win, "max_loss": max_loss,
            "max_dd": max_dd, "hit_counter": hit_counter,
            "sig_per_day": sig_per_day, "trades": trades,
        }

    s_loose = _stats("loose (3/4 = 生产默认, 当前)", trades_loose)
    s_strict = _stats("strict (4/4 = 全过, 仿 liusir #8)", trades_strict)
    s_drop = _stats("drop_forced (3/4 + drop≥1.5% 强制)", trades_drop_forced)

    for s in (s_loose, s_strict, s_drop):
        print(f"=== {s['label']} ===")
        print(f"总信号数: {s['n']}")
        print(f"信号频率: {s['sig_per_day']:.1f} 笔/天 (区间 {(bars[-1][0]-bars[0][0])/3600:.1f}h)")
        print()
        print(f"按 hit_type 分桶:")
        for ht, c in s['hit_counter'].most_common():
            ht_trades = [t for t in s['trades'] if t["hit_type"] == ht]
            ht_net = sum(t["net_usd"] for t in ht_trades)
            print(f"  {ht:<10} {c:>3} 笔   累计 {ht_net:>+7.2f}U   平均 {ht_net/c:>+7.2f}U/笔")
        print()
        wins = sum(1 for t in s['trades'] if t["net_usd"] > 0)
        losses = sum(1 for t in s['trades'] if t["net_usd"] <= 0)
        print(f"胜率: {wins}/{wins+losses} = {s['wr']:.1f}%")
        print(f"累计 PnL: {s['total_net']:+.2f}U (账户 {a.balance:.0f}U)")
        print(f"平均: {s['avg_net']:+.2f}U/笔")
        print(f"最大单笔赢: {s['max_win']:+.2f}U")
        print(f"最大单笔亏: {s['max_loss']:+.2f}U")
        print(f"最大回撤: {s['max_dd']:+.2f}U")
        if s['trades']:
            print(f"持仓平均时长: {sum(t['hold_bars'] for t in s['trades'])/s['n']:.0f} 根 (1m)")
        print()

    # ============================ 对照 ============================
    print(f"=== 跟历史对照 ===")
    print(f"  teacher 单 (liusir #8 套路):  9 笔 8 胜 1 平 +201.05U / +22.34U/笔 (10-16 截止)")
    print(f"  1h 主信号近 12 笔:           33% 胜率 +4.5R EV +0.37R")
    print(f"  hammer 自动 loose:           {s_loose['wr']:.1f}% 胜率 {s_loose['total_net']:+.2f}U / {s_loose['avg_net']:+.2f}U/笔  ({s_loose['n']} 笔)")
    print(f"  hammer 自动 strict:          {s_strict['wr']:.1f}% 胜率 {s_strict['total_net']:+.2f}U / {s_strict['avg_net']:+.2f}U/笔  ({s_strict['n']} 笔)")
    print(f"  hammer 自动 drop_forced:     {s_drop['wr']:.1f}% 胜率 {s_drop['total_net']:+.2f}U / {s_drop['avg_net']:+.2f}U/笔  ({s_drop['n']} 笔)")
    print()

    # ============================ 明细 ============================
    print(f"=== 单笔明细 (strict 全过版本) ===")
    print(f"{'idx':>3} {'date':<11} {'hit':<10} {'entry':>9} {'exit':>9} "
          f"{'drop':>5} {'volx':>4} {'reb':>5} {'net_U':>7}")
    print("-" * 80)
    for i, t in enumerate(s_strict['trades'], 1):
        print(f"{i:>3} {t['hammer_ts_str']:<11} {t['hit_type']:<10} "
              f"{t['entry']:>9.1f} {t['exit_px']:>9.1f} "
              f"{t['drop_pct']:>4.1f}% {t['volx']:>3.1f}x "
              f"{t['rebound_pct']:>+4.1f}% {t['net_usd']:>+6.2f}")


if __name__ == "__main__":
    main()
