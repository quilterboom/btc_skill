# -*- coding: utf-8 -*-
"""
日内短线双向策略回测：TD9 + EMA144(多周期分层) + RSI金叉死叉 + 量能
关键设计：
  * EMA144 只用于【高周期趋势定调】，不用于本周期位置判定
    （TD9 抄底天然出现在价格跌破 EMA 之后，强制"价在EMA上"会自相矛盾、信号枯竭）
  * 附加条件采用【打分制】而非全票通过，可观察"质量 vs 数量"的权衡
合约参数按 Gate.io BTC_USDT 实测：taker 0.075% / maker -0.01% / 维持保证金率 0.3%
"""
from __future__ import annotations
import sys, json
import numpy as np
import pandas as pd
from indicators import ema, rsi, atr, td_signal, sma
from backtest import load

TAKER = 0.00075
MAKER = -0.0001
MAINT_RATE = 0.003
INTERVAL_SEC = {"15m": 900, "1h": 3600, "4h": 14400, "1d": 86400}

MAIN = sys.argv[1] if len(sys.argv) > 1 else "1h"
LOW = load(MAIN); M4 = load("4h"); D1 = load("1d")


def prep(d):
    d = d.copy()
    d["e144"] = ema(d.close.values, 144)
    d["e144_s5"] = (d.e144 - d.e144.shift(5)) / d.e144.shift(5) * 100
    d["rsi6"] = rsi(d.close.values, 6)
    d["rsi14"] = rsi(d.close.values, 14)
    d["atrp"] = atr(d.high.values, d.low.values, d.close.values, 14) / d.close.values * 100
    d["vol_ma20"] = sma(d.vol.values, 20)
    d["vr"] = d.vol / d.vol_ma20
    d["td9b"] = td_signal(d.close.values, "buy")
    d["td9s"] = td_signal(d.close.values, "sell")
    return d


LOW, M4, D1 = prep(LOW), prep(M4), prep(D1)


def align(src, dst, cols):
    step = src.ts.values[1] - src.ts.values[0]
    cts = src.ts.values + step
    idx = np.clip(np.searchsorted(cts, dst.ts.values, side="right") - 1, 0, len(src) - 1)
    return {c_: src[c_].values[idx] for c_ in cols}, (dst.ts.values >= cts[idx])


m4c, m4v = align(M4, LOW, ["e144_s5", "e144", "close"])
d1c, d1v = align(D1, LOW, ["e144_s5", "e144", "close"])

c = LOW.close.values; o = LOW.open.values; h = LOW.high.values
l = LOW.low.values; ts = LOW.ts.values
n = len(c)
print(f"=== 主周期 {MAIN} | {n} 根 | {pd.to_datetime(ts[0],unit='s').date()} ~ {pd.to_datetime(ts[-1],unit='s').date()} ===")

r6, r14 = LOW.rsi6.values, LOW.rsi14.values
gold = np.zeros(n, bool); dead = np.zeros(n, bool)
gold[1:] = (r6[1:] > r14[1:]) & (r6[:-1] <= r14[:-1])
dead[1:] = (r6[1:] < r14[1:]) & (r6[:-1] >= r14[:-1])
vr = LOW.vr.values
warm = 200

# 四个附加条件（已做时间对齐，无未来函数）
COND = {
    "M4价在EMA144上": (m4c["close"] > m4c["e144"]) & m4v,
    "M4斜率>0":       (m4c["e144_s5"] > 0) & m4v,
    "D1斜率>0":       (d1c["e144_s5"] > 0) & d1v,
}
GOLD, DEAD, VOL_UP = gold, dead, (vr > 1.2)


def build(side, min_score=2, confirm=2, conds=("M4价在EMA144上", "M4斜率>0"),
          use_rsi=True, use_vol=False):
    base = LOW.td9b.values if side == "long" else LOW.td9s.values
    sig = np.zeros(n, bool)
    for i in np.where(base)[0]:
        if i < warm:
            continue
        j = i + confirm
        if j >= n - 2:
            continue
        # 等确认
        if side == "long":
            if not all(c[i + t] > c[i + t - 1] for t in range(1, confirm + 1)):
                continue
        else:
            if not all(c[i + t] < c[i + t - 1] for t in range(1, confirm + 1)):
                continue
        # 打分
        sc = 0
        for cn in conds:
            if cn == "M4价在EMA144上":
                sc += 1 if ((m4c["close"][i] > m4c["e144"][i]) if side == "long"
                            else (m4c["close"][i] < m4c["e144"][i])) else 0
            elif cn == "M4斜率>0":
                sc += 1 if ((m4c["e144_s5"][i] > 0) if side == "long"
                            else (m4c["e144_s5"][i] < 0)) else 0
            elif cn == "D1斜率>0":
                sc += 1 if ((d1c["e144_s5"][i] > 0) if side == "long"
                            else (d1c["e144_s5"][i] < 0)) else 0
        if use_rsi:
            win = range(max(0, i - 4), j + 1)
            sc += 1 if any((GOLD if side == "long" else DEAD)[k] for k in win) else 0
        if use_vol:
            win = range(max(0, i - 3), j + 1)
            sc += 1 if any(VOL_UP[k] for k in win) else 0
        if sc >= min_score:
            sig[j] = True
    return sig


def run2(sigL, sigS, sl=0.015, tp=0.03, day_close=True, fee=TAKER, max_bars=48, mae_mode=False):
    recs = []
    for side, sig in (("long", sigL), ("short", sigS)):
        for i in np.where(sig)[0]:
            j = i + 1
            if j >= n - 2:
                continue
            entry = o[j]
            sl_p = entry * (1 - sl) if side == "long" else entry * (1 + sl)
            tp_p = entry * (1 + tp) if side == "long" else entry * (1 - tp)
            mae = 0.0
            exit_p, reason, k = np.nan, "timeout", j
            day_end = j + int((86400 - (ts[j] % 86400)) // INTERVAL_SEC[MAIN])
            limit = min(j + max_bars, n - 1, day_end if day_close else n - 1)
            for b in range(j, limit + 1):
                if side == "long":
                    mae = min(mae, (l[b] - entry) / entry)
                    if l[b] <= sl_p: exit_p, reason, k = sl_p, "stop", b; break
                    if h[b] >= tp_p: exit_p, reason, k = tp_p, "take", b; break
                else:
                    mae = min(mae, (entry - h[b]) / entry)
                    if h[b] >= sl_p: exit_p, reason, k = sl_p, "stop", b; break
                    if l[b] <= tp_p: exit_p, reason, k = tp_p, "take", b; break
                if b == limit:
                    exit_p, reason, k = c[b], "dayclose" if day_close else "timeout", b
            if np.isnan(exit_p):
                continue
            gross = (exit_p - entry) / entry if side == "long" else (entry - exit_p) / entry
            net = gross - fee * 2
            recs.append({"side": side, "entry": entry, "exit": exit_p, "net": net * 100,
                         "r": net / sl, "mae": mae * 100, "held": k - j + 1,
                         "reason": reason, "win": net > 0})
    return pd.DataFrame(recs)


def summ(t, tag=""):
    if t is None or len(t) < 6:
        print(f"{tag:<44} 样本不足 n={0 if t is None else len(t)}")
        return None
    w = t[t.net > 0]; ls = t[t.net <= 0]
    pf = w.net.sum() / abs(ls.net.sum()) if len(ls) and ls.net.sum() != 0 else np.inf
    mae = -t.mae.values
    s = {"n": len(t), "win_rate": len(w) / len(t) * 100, "ev": t.net.mean(), "ev_r": t.r.mean(),
         "pf": pf, "total": t.net.sum(), "mae_p50": np.percentile(mae, 50),
         "mae_p90": np.percentile(mae, 90), "mae_max": mae.max(), "held": t.held.mean()}
    print(f"{tag:<44} n={s['n']:<4} 胜率={s['win_rate']:5.1f}% EV={s['ev']:+6.2f}% EV(R)={s['ev_r']:+5.2f}R "
          f"PF={s['pf']:5.2f} 总={s['total']:+8.1f}% MAE中位={s['mae_p50']:.2f}% P90={s['mae_p90']:.2f}%")
    return s


print(f"\n【A】条件叠加打分：min_score 权衡（做多，等2根确认，SL1.5%/TP3%，日内平）")
for ms in [0, 1, 2, 3, 4]:
    sL = build("long", min_score=ms)
    summ(run2(sL, np.zeros(n, bool)), f"  min_score={ms} (信号{int(sL.sum())})")

print(f"\n【B】RSI 金叉 / 量能 的增量贡献（min_score 固定为 2，含 M4价+M4斜率）")
for ur, uv, nm in [(False, False, "不含RSI/量能(仅M4双条件)"),
                   (True, False, "+RSI金叉"),
                   (False, True, "+放量"),
                   (True, True, "+RSI金叉+放量")]:
    sL = build("long", min_score=2, use_rsi=ur, use_vol=uv)
    summ(run2(sL, np.zeros(n, bool)), f"  {nm} (信号{int(sL.sum())})")

print(f"\n【C】双向对比（min_score=2, 含RSI金叉）")
sL = build("long", min_score=2, use_rsi=True)
sS = build("short", min_score=2, use_rsi=True)
print(f"  做多信号 {int(sL.sum())} 个 | 做空信号 {int(sS.sum())} 个")
summ(run2(sL, np.zeros(n, bool)), "  单边 做多")
summ(run2(np.zeros(n, bool), sS), "  单边 做空")
t2 = run2(sL, sS)
summ(t2, "  双向合计")

print(f"\n【D】止损止盈扫描（双向合计）")
best = None
for sl, tp in [(0.006, 0.012), (0.008, 0.016), (0.01, 0.02), (0.015, 0.03),
               (0.02, 0.04), (0.025, 0.05), (0.03, 0.06)]:
    s = summ(run2(sL, sS, sl, tp), f"  SL{sl:.1%}/TP{tp:.1%}")
    if s and (best is None or s["ev"] > best[1]["ev"]):
        best = ((sl, tp), s)

print(f"\n【E】手续费敏感性（双向，SL1%/TP2%）")
summ(run2(sL, sS, 0.01, 0.02, fee=TAKER), "  全 taker 0.075%（市价进出）")
summ(run2(sL, sS, 0.01, 0.02, fee=MAKER), "  全 maker -0.01%（挂单，返佣）")
summ(run2(sL, sS, 0.01, 0.02, fee=(TAKER + MAKER) / 2), "  混合（一挂一吃）")

print(f"\n【F】日内平仓 vs 隔夜（双向，SL1%/TP2%）")
summ(run2(sL, sS, 0.01, 0.02, day_close=True), "  当日必平")
summ(run2(sL, sS, 0.01, 0.02, day_close=False, max_bars=24), "  持有24根不限日")
summ(run2(sL, sS, 0.01, 0.02, day_close=False, max_bars=72), "  持有72根不限日")

# ---------- 爆仓建模 ----------
print("\n" + "=" * 80)
print("【100 倍杠杆爆仓建模】基于本策略真实的 MAE（最大不利偏移）分布")
print("=" * 80)
tf = run2(sL, sS, 0.01, 0.02)
if len(tf) >= 6:
    mae = -tf.mae.values
    print(f"样本 {len(mae)} 笔。最大不利偏移分布（%）：")
    for p in [25, 50, 75, 90, 95, 99]:
        print(f"    P{p:<3}: {np.percentile(mae, p):.3f}%      最大: {mae.max():.3f}%")
    print(f"\n{'名义/账户倍数':>14}{'初始保证金率':>13}{'强平阈值':>14}{'本策略被打穿率':>16}")
    rows = []
    for lev in [200, 100, 50, 20, 10, 5, 3, 2, 1]:
        imr = 1.0 / lev
        thr = imr - MAINT_RATE          # 全仓满仓口径：反向波动超过此幅度即触发强平
        if thr <= 0:
            continue
        rate = (mae > thr * 100).mean() * 100
        rows.append((lev, imr * 100, thr * 100, rate))
        print(f"{lev:>13}倍{imr*100:>12.2f}%{thr*100:>13.2f}%{rate:>15.1f}%")
    print("\n口径说明：全仓模式且按该倍数开满。若 100 倍杠杆但名义敞口仅 = 账户 2 倍，")
    print("则生死线等同上表『2倍』那行（容忍反向 49.7%）—— 决定生死的是【名义/账户】，")
    print("不是软件里填的那个杠杆数字。杠杆只是允许你开多大的上限。")
    json.dump({"mae": mae.tolist(), "rows": rows},
              open(f"mae_{MAIN}.json", "w"))
    print(f"\n(MAE 明细已存 mae_{MAIN}.json)")
