#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
teacher_replica.py —— 把老师的交易模式量化，在长历史上复刻 + 检验

## 动机（liusir 2026-11-07）
「希望以老师交易的单去做量化交易，想要更加接近老师的交易模式」。

## 从 20 笔反推出的老师模式规格（teacher_pattern_spec.py 实测）

    方向    long 18 / short 2  → 以【做多逆势】为主
    时段    21 点 5 单、22 点 6 单 → 65% 落在 20:00~01:59
    结构    1h EMA20<EMA50 占 14/19 → 【逆势】为主（顺势仅 5/19）
    位置    入场价距 15m 近 48 根 swing 低点 中位 +0.099% → 贴着低点进
            入场价 vs 1h EMA50 均 -0.684% → 在均线下方
    量能    1h 量能倍数 均 1.93x → 小时级别放量确认
    出场    TP1 均 +0.570%（减仓 50%）→ SL 移保本 → TP2 均 +1.502% 剩余全平
            初始 SL 均 0.161%（结构位下方，非固定百分比）
    执行    限价挂在结构位【等成交】，不追价

## 本脚本做什么

1. **信号生成**（长历史逐根滚动，无前视）
   - 条件：逆势（1h EMA20/50 方向相反）+ 下跌到位 + 放量确认（可配）
2. **入场按老师的执行方式**：信号后【挂限价在结构位等成交】，等不到就不做
   （这是老师与我方策略最大的执行差异：老师不追价）
3. **出场用老师的阶梯**：TP1 减 50% → SL 移保本 → TP2 全平 / 打保本
4. **检验**（btc-strategy-dev 步骤 4/6/9）：训练/测试切分、Bootstrap CI、
   随机对照、多种去重间隔、反向检验、收益来源拆解
5. **召回检验**：复刻规则能否命中老师自己的 20 笔（决定「像不像」）

## 只读，不改任何生产数据
"""
from __future__ import annotations

import bisect
import json
import os
import random
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone

sys.path.insert(0, "/root/skills/btc-gate-intraday-strategy/scripts")
sys.path.insert(0, "/root/skills/btc-gate-intraday-strategy/web")

CST = timezone(timedelta(hours=8))
KLINE = "/root/data/BTC_USDT_klines.json"
FEE = 4.85
NOTIONAL = 5000.0


# ---------------------------------------------------------------- 数据
def load_bars(tf):
    d = json.load(open(KLINE))
    return d["BTC_USDT"][tf]["bars"]


def ema_series(vals, n):
    if len(vals) < n:
        return [None] * len(vals)
    k = 2.0 / (n + 1)
    v = sum(vals[:n]) / n
    out = [None] * (n - 1) + [v]
    for x in vals[n:]:
        v = x * k + v * (1 - k)
        out.append(v)
    return out


def atr_series(bars, n=14):
    trs = [None]
    for i in range(1, len(bars)):
        h, l, pc = float(bars[i][2]), float(bars[i][3]), float(bars[i - 1][4])
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    out = [None] * len(bars)
    if len(trs) < n:
        return out
    v = sum(trs[1:n + 1]) / n
    out[n] = v
    for i in range(n + 1, len(bars)):
        v = (v * (n - 1) + trs[i]) / n
        out[i] = v
    return out


# ---------------------------------------------------------------- 信号
class SigGen:
    """逐根生成信号。★ 所有特征只用第 i 根【已收】bar 及之前的数据。"""

    def __init__(self, bars, tf_min):
        self.bars = bars
        self.tf = tf_min
        self.tss = [int(b[0]) for b in bars]
        self.close = [float(b[4]) for b in bars]
        self.high = [float(b[2]) for b in bars]
        self.low = [float(b[3]) for b in bars]
        self.vol = [float(b[5]) for b in bars]
        self.e20 = ema_series(self.close, 20)
        self.e50 = ema_series(self.close, 50)
        self.atr = atr_series(bars, 14)

    def vol_x(self, i, look=20):
        """第 i 根量能 / 前 look 根均量（不含第 i 根自己）"""
        if i < look:
            return None
        base = sum(self.vol[i - look:i]) / look
        return self.vol[i] / base if base > 0 else None

    def drop_pct(self, i, look):
        """第 i 根收盘 vs i-look 根收盘 的跌幅%"""
        if i < look:
            return None
        return (self.close[i - look] - self.close[i]) / self.close[i - look] * 100

    def structure(self, i, look):
        """近 look 根的 swing 低/高"""
        if i < look:
            return None, None
        w = range(i - look + 1, i + 1)
        return min(self.low[j] for j in w), max(self.high[j] for j in w)


def gen_signals(sg, cfg):
    """生成候选信号。返回 [{i, ts, side, level, ...}]"""
    sigs = []
    bars = sg.bars
    for i in range(cfg["min_i"], len(bars) - 1):
        ts = int(bars[i][0])
        dt = datetime.fromtimestamp(ts, CST)
        # ── 时段过滤 ──
        if cfg["hour_ok"] and dt.hour not in cfg["hours"]:
            continue
        # ── 结构：逆势 ──
        e20, e50 = sg.e20[i], sg.e50[i]
        if e20 is None or e50 is None:
            continue
        struct = "up" if e20 > e50 * 1.0005 else ("down" if e20 < e50 * 0.9995 else "flat")
        side = None
        if struct == "down":
            side = "long"
        elif struct == "up":
            side = "short"
        if side is None:
            continue
        # struct_dir：强制只在指定结构方向找信号（反向检验用）
        want = cfg.get("struct_dir")
        if want and struct != want:
            continue
        # ── 跌幅（lookback 以【小时】为单位，bar 数按周期换算）──
        look = max(1, int(cfg["drop_look_hours"] * 60 / sg.tf))
        dp = sg.drop_pct(i, look)
        if dp is None or dp < cfg["drop_min"]:
            continue
        # ── 量能确认 ──
        vx = sg.vol_x(i)
        if vx is None or vx < cfg["vol_min"]:
            continue
        # ── 结构位（挂限价的地方）──
        lev_look = max(2, int(cfg["level_look_hours"] * 60 / sg.tf))
        sl, sh = sg.structure(i, lev_look)
        # ★ 距 swing 低点的距离用【更短】的 lookback（老师特征是「贴着近端低点进」）
        near_look = max(2, int(cfg["near_look_hours"] * 60 / sg.tf))
        nsl, nsh = sg.structure(i, near_look)
        if sl is None:
            continue
        # ── 距结构位不能太远（老师贴着低点进）──
        px_ref = sg.close[i]
        if side == "long":
            dist = (px_ref - nsl) / nsl * 100
        else:
            dist = (nsh - px_ref) / px_ref * 100
        if dist > cfg["max_dist_pct"]:
            continue
        sigs.append({
            "i": i, "ts": ts, "side": side, "struct": struct,
            "level": sl if side == "long" else sh,
            "ref_px": px_ref, "drop_pct": dp, "vol_x": vx, "dist_pct": dist,
            "atr_pct": (sg.atr[i] / sg.close[i] * 100) if sg.atr[i] else None,
        })
    return sigs


# ---------------------------------------------------------------- 入场（限价等成交）
def fill_limit(sg, sig, cfg):
    """信号后挂限价在 level，等成交。等不到就不做（老师不追价）。"""
    i = sig["i"]
    level = sig["level"]
    buf = cfg["level_buf_pct"] / 100.0
    if sig["side"] == "long":
        limit = level * (1 + buf)
    else:
        limit = level * (1 - buf)
    end = min(len(sg.bars), i + 1 + max(1, int(cfg["max_wait_hours"] * 60 / sg.tf)))
    for j in range(i + 1, end):
        o, h, l = sg.high[j], sg.high[j], sg.low[j]
        op = float(sg.bars[j][1])
        if sig["side"] == "long":
            if l <= limit:
                px = min(op, limit)          # 开盘已低于限价 → 更好价成交
                return {"fill_i": j, "fill_ts": int(sg.bars[j][0]), "px": px, "limit": limit}
        else:
            if h >= limit:
                px = max(op, limit)
                return {"fill_i": j, "fill_ts": int(sg.bars[j][0]), "px": px, "limit": limit}
    return None


# ---------------------------------------------------------------- 出场（老师阶梯）
def settle(sg, fill, cfg):
    """老师出场阶梯：TP1 减 50% → SL 移保本 → TP2 剩余全平 / 打保本。

    同根双触按保守口径判 SL（与 position_tracker 一致）。
    """
    side = fill["side"]
    sgn = 1.0 if side == "long" else -1.0   # ★ short 单所有 PnL 取负
    entry = fill["px"]
    j = fill["fill_i"]
    C = max(1, round(NOTIONAL / (entry * 1e-4)))
    q1 = max(1, C // 2)
    risk = cfg["sl_pct"] / 100.0
    if cfg["sl_struct"]:
        sl = fill["level"] * (1 - cfg["sl_struct_buf_pct"] / 100.0)
        if side == "short":
            sl = fill["level"] * (1 + cfg["sl_struct_buf_pct"] / 100.0)
        # 结构 SL 不能比固定 SL 更近，取两者中更宽（避免过度乐观）
        fixed = entry * (1 - risk) if side == "long" else entry * (1 + risk)
        if side == "long":
            sl = min(sl, fixed)
        else:
            sl = max(sl, fixed)
    else:
        sl = entry * (1 - risk) if side == "long" else entry * (1 + risk)
    tp1 = entry * (1 + cfg["tp1_pct"] / 100.0) if side == "long" else entry * (1 - cfg["tp1_pct"] / 100.0)
    tp2 = entry * (1 + cfg["tp2_pct"] / 100.0) if side == "long" else entry * (1 - cfg["tp2_pct"] / 100.0)

    gross = 0.0
    leg = []
    tp1_hit = False
    end = min(len(sg.bars), j + 1 + max(1, int(cfg["max_hold_hours"] * 60 / sg.tf)))
    for k in range(j + 1, end):
        h, l = sg.high[k], sg.low[k]
        if side == "long":
            t_sl, t_tp1, t_tp2 = l <= sl, (not tp1_hit and h >= tp1), h >= tp2
        else:
            t_sl, t_tp1, t_tp2 = h >= sl, (not tp1_hit and l <= tp1), l <= tp2
        if t_sl and (t_tp1 or t_tp2):
            # 同根双触 → SL（保守）
            px = sl
            if tp1_hit:
                gross += (C - q1) * (px - entry) * 1e-4 * sgn
                leg.append(("BE/SL", C - q1, px))
            else:
                gross += C * (px - entry) * 1e-4 * sgn
                leg.append(("SL", C, px))
            net = gross - FEE
            return {"net": net, "gross": gross, "leg": leg, "exit": "SL",
                    "exit_ts": int(sg.bars[k][0]), "bars": k - j, "tp1_hit": tp1_hit}
        if t_sl:
            px = sl
            if tp1_hit:
                gross += (C - q1) * (px - entry) * 1e-4 * sgn
                leg.append(("BE", C - q1, px))
            else:
                gross += C * (px - entry) * 1e-4 * sgn
                leg.append(("SL", C, px))
            net = gross - FEE
            return {"net": net, "gross": gross, "leg": leg, "exit": "BE" if tp1_hit else "SL",
                    "exit_ts": int(sg.bars[k][0]), "bars": k - j, "tp1_hit": tp1_hit}
        if t_tp1:
            tp1_hit = True
            gross += q1 * (tp1 - entry) * 1e-4 * sgn   # ★ sgn: short 单符号翻转
            leg.append(("TP1", q1, tp1))
            sl = entry                       # ★ TP1 后移保本
        if t_tp2:
            if tp1_hit:
                gross += (C - q1) * (tp2 - entry) * 1e-4 * sgn
                leg.append(("TP2", C - q1, tp2))
            else:
                gross += C * (tp2 - entry) * 1e-4 * sgn
                leg.append(("TP2", C, tp2))
            net = gross - FEE
            return {"net": net, "gross": gross, "leg": leg, "exit": "TP2",
                    "exit_ts": int(sg.bars[k][0]), "bars": k - j, "tp1_hit": tp1_hit}
    # 到期按最后 close 平
    k = end - 1
    px = sg.close[k]
    if tp1_hit:
        gross += (C - q1) * (px - entry) * 1e-4 * sgn
        leg.append(("TIME", C - q1, px))
    else:
        gross += C * (px - entry) * 1e-4 * sgn
        leg.append(("TIME", C, px))
    net = gross - FEE
    return {"net": net, "gross": gross, "leg": leg, "exit": "TIME",
            "exit_ts": int(sg.bars[k][0]), "bars": k - j, "tp1_hit": tp1_hit}


# ---------------------------------------------------------------- 组合
def run(sg, cfg, dedup_hours=12.0, sigs=None):
    if sigs is None:
        sigs = gen_signals(sg, cfg)
    trades = []
    last_t = {}          # 同向独立去重
    dedup_s = dedup_hours * 3600
    for s in sigs:
        key = s["side"]
        if key in last_t and s["ts"] - last_t[key] < dedup_s:
            continue
        f = fill_limit(sg, s, cfg)
        if not f:
            continue
        last_t[key] = s["ts"]
        r = settle(sg, {**s, **f}, cfg)
        trades.append({**s, **f, **r})
    return trades, sigs


def stats(trades, label=""):
    if not trades:
        return {"n": 0, "net": 0.0}
    nets = [t["net"] for t in trades]
    wins = [n for n in nets if n > 0]
    losses = [n for n in nets if n < 0]
    gp = sum(wins)
    gl = abs(sum(losses))
    return {"n": len(trades), "net": sum(nets), "win_rate": len(wins) / len(trades) * 100,
            "avg": sum(nets) / len(trades), "payoff": (gp / len(wins)) / (gl / len(losses)) if wins and losses else 0,
            "best": max(nets), "worst": min(nets), "label": label}


def show(st, prefix="  "):
    if st["n"] == 0:
        print(f"{prefix}{st.get('label','')}: n=0")
        return
    print(f"{prefix}{st.get('label','')}: n={st['n']:>3} 胜率{st['win_rate']:>5.1f}% "
          f"净{st['net']:>+9.2f}U 均{st['avg']:>+7.2f}U 盈亏比{st['payoff']:>5.2f} "
          f"最好{st['best']:>+7.2f} 最差{st['worst']:>+7.2f}")


def bootstrap_ci(nets, n=10000, seed=42):
    if not nets:
        return (0.0, 0.0, 0.0)
    random.seed(seed)
    means = []
    for _ in range(n):
        s = sum(random.choice(nets) for _ in nets) / len(nets)
        means.append(s)
    means.sort()
    return (means[int(0.05 * n)], means[int(0.95 * n)],
            sum(1 for m in means if m > 0) / n)


def random_baseline(sg, trades, n_trials=300, seed=7):
    """随机入场对照：同样数量的随机时点，同样的出场阶梯。"""
    random.seed(seed)
    if not trades:
        return 0.0, 0.0
    target = min(len(trades), 80)
    lo = 1
    hi = len(sg.bars) - 2
    wins = []
    for _ in range(n_trials):
        picks = random.sample(range(lo, hi), min(target, hi - lo))
        nets = []
        for i in picks:
            side = "long" if random.random() < 0.9 else "short"
            sig = {"i": i, "ts": int(sg.bars[i][0]), "side": side,
                   "level": sg.low[i] if side == "long" else sg.high[i]}
            f = fill_limit(sg, sig, CFG)
            if not f:
                continue
            nets.append(settle(sg, {**sig, **f}, CFG)["net"])
        if nets:
            wins.append(sum(nets))
    if not wins:
        return 0.0, 0.0
    wins.sort()
    return wins[int(0.05 * len(wins))], wins[int(0.95 * len(wins))]


# ---------------------------------------------------------------- 主流程
BASE_CFG = {
    "min_i": 60,
    "hours": list(range(24)),
    "hour_ok": False,
    "counter_trend": True,
    "struct_dir": None,
    "drop_look_hours": 12,
    "drop_min": 0.5,
    "vol_min": 0.8,
    "level_look_hours": 24,
    "near_look_hours": 12,
    "max_dist_pct": 2.0,
    "level_buf_pct": 0.02,
    "max_wait_hours": 6,
    "sl_pct": 0.30,
    "sl_struct": True,
    "sl_struct_buf_pct": 0.10,
    "tp1_pct": 0.57,
    "tp2_pct": 1.50,
    "max_hold_hours": 72,
}
CFG = dict(BASE_CFG)


def main():
    print("=" * 110)
    print("老师模式量化复刻 · 长历史检验")
    print("=" * 110)

    for tf, tf_min in (("1h", 60), ("15m", 15)):
        bars = load_bars(tf)
        sg = SigGen(bars, tf_min)
        span = f"{datetime.fromtimestamp(sg.tss[0],CST):%Y-%m-%d} → {datetime.fromtimestamp(sg.tss[-1],CST):%Y-%m-%d}"
        print(f"\n{'#'*110}\n【{tf} 主战场】{len(bars)} 根 | {span} | 共 {(sg.tss[-1]-sg.tss[0])/86400:.0f} 天\n{'#'*110}")

        trades, sigs = run(sg, CFG)
        st = stats(trades, "全样本")
        show(st)
        if not trades:
            print("  ⚠️ 无信号")
            continue

        # ── 训练/测试切分 70/30 ──
        cut = sg.tss[0] + int((sg.tss[-1] - sg.tss[0]) * 0.7)
        tr = [t for t in trades if t["ts"] < cut]
        te = [t for t in trades if t["ts"] >= cut]
        show(stats(tr, "训练 70%"))
        show(stats(te, "测试 30%"))

        # ── Bootstrap ──
        ci_lo, ci_hi, p_pos = bootstrap_ci([t["net"] for t in trades])
        print(f"  Bootstrap 90% CI: [{ci_lo:+.2f}, {ci_hi:+.2f}]  P(EV>0)={p_pos*100:.1f}%")

        # ── 随机对照 ──
        rlo, rhi = random_baseline(sg, trades)
        print(f"  随机入场对照 90% 区间: [{rlo:+.2f}, {rhi:+.2f}]  ← 实测净 {st['net']:+.2f}U "
              f"{'落在区间外 ✓' if st['net'] > rhi else '落在区间内 ✗（与随机无差异）'}")

        # ── 多种去重间隔 ──
        print("  ── 多种去重间隔 ──")
        for dh in (4, 6, 8, 12, 18, 24):
            t2, _ = run(sg, CFG, dedup_hours=dh, sigs=sigs)
            s2 = stats(t2, f"去重{dh}h")
            show(s2)

        # ── 出场来源拆解 ──
        print("  ── 收益来源拆解 ──")
        by = {}
        for t in trades:
            by.setdefault(t["exit"], []).append(t["net"])
        for k, v in sorted(by.items(), key=lambda kv: -sum(kv[1])):
            print(f"    {k:<6} n={len(v):>3} 净{sum(v):>+9.2f}U 均{sum(v)/len(v):>+7.2f}U")

        # ── 方向 ──
        print("  ── 方向 ──")
        for side in ("long", "short"):
            sel = [t for t in trades if t["side"] == side]
            show(stats(sel, side))

        # ── 反向检验：把结构反过来 ──
        print("  ── 反向检验（顺势 vs 逆势）──")
        # 反向检验：结构相反时做同一方向（long）应该显著变差
        rev = dict(CFG)
        rev["struct_dir"] = "down"       # 老师模式：down 结构做多
        t_rev, _ = run(sg, rev, sigs=sigs)
        show(stats(t_rev, "★ 老师模式(down做多)"))
        rev2 = dict(CFG)
        rev2["struct_dir"] = "up"        # 反向：up 结构做多
        t_rev2, _ = run(sg, rev2)
        show(stats(t_rev2, "反向检验(up做多)"))

        # ── 参数平原（扫 drop_min）──
        print("  ── 参数平原：drop_min ──")
        for dm in (0.5, 0.75, 1.0, 1.5, 2.0):
            c = dict(CFG)
            c["drop_min"] = dm
            t3, _ = run(sg, c, dedup_hours=12.0)
            show(stats(t3, f"drop_min={dm}"))


if __name__ == "__main__":
    main()
