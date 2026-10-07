#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
信号日志 · 自动结算 · 分层绩效 · 保守优化
==========================================
把每一次 scan.py 给出的信号落盘，之后用 15m K 线**自动回放结算**，
再按维度分层统计胜率/EV，最后生成「有样本门槛」的调参建议。

设计原则（防过拟合）：
  1. 挂单成交才算数 —— 价格没回到挂单价记 MISSED，不计入胜率
  2. 同一根 K 线内既到止盈又到止损 → **保守判止损**
  3. 建议默认只写 tuning_suggested.json，需 `python journal.py apply` 才生效
  4. 样本不足（n<15）只允许减仓，不允许加权/放宽门槛
  5. 小样本给出 Wilson 置信区间，不做点估计吹牛

用法:
    python journal.py stats            # 绩效总览 + 分层
    python journal.py stats --md       # markdown 表
    python journal.py list             # 列出最近信号（含结算结果）
    python journal.py settle           # 只结算，不打印
    python journal.py tune             # 生成优化建议（不生效）
    python journal.py apply            # 采纳建议 → tuning.json（scan.py 自动应用）
    python journal.py reset            # 清空日志（需确认）
环境变量:
    BTC_NO_JOURNAL=1                   关闭自动记录
    BTC_JOURNAL_COOLDOWN=3600          同方向信号刷新冷却（秒）
"""
from __future__ import annotations
import os, sys, json, time, math, argparse
from typing import List, Dict, Any, Optional, Callable

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from gate_fetch import DATA_DIR, fetch_ohlcv

JOURNAL = os.path.join(DATA_DIR, "journal.jsonl")
TUNING = os.path.join(DATA_DIR, "tuning.json")
TUNING_SUG = os.path.join(DATA_DIR, "tuning_suggested.json")
COOLDOWN = int(os.environ.get("BTC_JOURNAL_COOLDOWN", "3600"))
MAX_HOLD = 24 * 3600          # 日内：成交后最多持有 24h 强制平
FEE_R_TAKER = 0.0015          # 往返 0.075%×2
FEE_R_MAKER = -0.0002         # 往返 maker 返佣

# 回测基准（实证结论，用于和实盘/纸面日志对照；来源见 SKILL.md §实证结论）
BASELINE = [
    ("回测·4h 裸 TD9 抄底",        47, "27.7%", "-0.71%/笔", "p=0.876 无效"),
    ("回测·4h TD9+2根确认",        13, "69.2%", "+2.94%/笔", "p=0.012 唯一显著"),
    ("回测·4h Fib+TD9 做多",       68, "55.9%", "+0.25%/笔", "p=0.068 边缘"),
    ("回测·1h 做空（全样本）",       None, "—", "-0.28%/笔", "做空系统性偏弱"),
]

OK, NO, WARN = "✅", "❌", "⚠️"


# ============================ 读写 ============================
def _read() -> List[Dict]:
    if not os.path.exists(JOURNAL):
        return []
    out = []
    for ln in open(JOURNAL, encoding="utf-8"):
        ln = ln.strip()
        if not ln:
            continue
        try:
            out.append(json.loads(ln))
        except Exception:
            pass
    return out


def _write(recs: List[Dict]) -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = JOURNAL + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        for r in recs:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    os.replace(tmp, JOURNAL)


# ============================ 记录 ============================
def log_signal(contract: str, last: float, side: str, score: int, core_ok: bool,
               verdict: str, plan: Dict, tags: Dict, meta: Dict = None) -> str:
    """
    记录一条信号。同方向 + 冷却期内 → 刷新上一条（不重复计数）。
    返回: "added" / "updated" / "off"
    """
    if os.environ.get("BTC_NO_JOURNAL") == "1":
        return "off"
    recs = _read()
    now = int(time.time())
    fresh = {
        "contract": contract, "ts": now, "last": round(float(last), 1),
        "side": side, "score": int(score), "core_ok": bool(core_ok),
        "verdict": verdict,
        "entry": round(float(plan["entry_limit"]), 1),
        "entry_break": round(float(plan.get("entry_break", 0) or 0), 1),
        "sl": round(float(plan["sl"]), 1),
        "tp1": round(float(plan["tp1"]), 1),
        "tp2": round(float(plan["tp2"]), 1),
        "sl_pct": round(float(plan["sl_pct"]), 4),
        "tp1_pct": round(float(plan["tp1_pct"]), 4),
        "status": "pending", "tags": dict(tags or {}),
        "meta": dict(meta or {}),
    }
    if recs:
        p = recs[-1]
        if (p.get("contract") == contract and p.get("side") == side
                and p.get("status") == "pending" and now - int(p.get("ts", 0)) < COOLDOWN):
            keep_id, keep_ts = p.get("id"), p.get("ts")
            p.update(fresh)
            p["id"], p["ts"] = keep_id, keep_ts
            _write(recs)
            return "updated"
    fresh["id"] = f"{contract}-{now}-{side}"
    recs.append(fresh)
    _write(recs)
    return "added"


# ============================ 结算 ============================
def _settle_one(r: Dict, bars: List[List]) -> Optional[Dict]:
    """用 15m K 线回放：挂单成交 → TP/SL 谁先到 → 超时按收盘平"""
    side = r["side"]
    entry, sl, tp1 = float(r["entry"]), float(r["sl"]), float(r["tp1"])
    sl_pct = float(r.get("sl_pct") or 0.01) / 100.0
    R = abs(entry - sl)
    if R <= 0:
        return None
    fee_r = FEE_R_MAKER / max(sl_pct, 1e-6)      # post-only 挂单往返
    filled = False
    ft = None
    mae = mfe = 0.0
    for b in bars:
        ts = int(b[0])
        h, l, c = float(b[2]), float(b[3]), float(b[4])
        if ts <= int(r["ts"]):
            continue
        if not filled:
            hit = (l <= entry) if side == "long" else (h >= entry)
            if not hit:
                if ts - int(r["ts"]) > MAX_HOLD:            # 2026-10-07 liusir规则：挂单 24h 都没挂到 → 作废
                    return {"status": "settled", "filled": False, "outcome": "MISSED",
                            "R": 0.0, "net_R": 0.0, "exit_px": round(c, 1), "exit_ts": ts,
                            "mae": 0.0, "mfe": 0.0, "hold_h": round((ts - r["ts"]) / 3600, 1)}
                continue
            filled = True
            ft = ts
        if side == "long":
            mae = max(mae, (entry - l) / R)
            mfe = max(mfe, (h - entry) / R)
            t_sl, t_tp = l <= sl, h >= tp1
            pl = (c - entry) / R
        else:
            mae = max(mae, (h - entry) / R)
            mfe = max(mfe, (entry - l) / R)
            t_sl, t_tp = h >= sl, l <= tp1
            pl = (entry - c) / R
        if t_sl and t_tp:                # 同根双触 → 保守判止损
            return {"status": "settled", "filled": True, "outcome": "SL*", "R": -1.0,
                    "net_R": round(-1.0 - fee_r, 3), "exit_px": round(sl, 1), "exit_ts": ts,
                    "mae": round(mae, 2), "mfe": round(mfe, 2),
                    "hold_h": round((ts - ft) / 3600, 1)}
        if t_sl:
            return {"status": "settled", "filled": True, "outcome": "SL", "R": -1.0,
                    "net_R": round(-1.0 - fee_r, 3), "exit_px": round(sl, 1), "exit_ts": ts,
                    "mae": round(mae, 2), "mfe": round(mfe, 2),
                    "hold_h": round((ts - ft) / 3600, 1)}
        if t_tp:
            # 2026-10-05 修正：与 position_tracker 语义对齐
            #   TP1 = 部分止盈（1/3 仓位锁利，剩 2/3 在场上），
            #   不能把整笔标 settled。要继续持仓跟踪到 BE / TP2 / SL。
            #   这里只记一笔"已触发 TP1 部分止盈"的事件，让 settler 把它落库。
            #   实际 PnL 累计等最终 close 触发再算。
            rr = float(r.get("tp1_pct") or 0) / max(float(r.get("sl_pct") or 1) or 1, 1e-6)
            return {"status": "pending", "filled": True,
                    "outcome": "TP1_PARTIAL", "R": round(rr, 3),
                    "net_R": round(rr - fee_r, 3), "exit_px": round(tp1, 1), "exit_ts": ts,
                    "mae": round(mae, 2), "mfe": round(mfe, 2),
                    "hold_h": round((ts - ft) / 3600, 1),
                    "tp1_hit": True, "tp1_hit_px": round(tp1, 1), "tp1_hit_ts": ts,
                    "active_sl": float(r.get("entry") or 0),    # 移到 BE
                    "pos_state": "TP1_PARTIAL"}
        if ft and ts - ft > MAX_HOLD:    # 日内强平（24h 未触发）
            return {"status": "settled", "filled": True, "outcome": "TIMEOUT", "R": round(pl, 3),
                    "net_R": round(pl - fee_r, 3), "exit_px": round(c, 1), "exit_ts": ts,
                    "mae": round(mae, 2), "mfe": round(mfe, 2),
                    "hold_h": round((ts - ft) / 3600, 1)}
    return None                          # 数据不足，继续 pending


def settle_all(contract: str = "BTC_USDT", verbose: bool = False) -> int:
    recs = _read()
    pend = [r for r in recs if r.get("status") == "pending" and r.get("contract") == contract]
    if not pend:
        return 0
    try:
        bars = fetch_ohlcv(contract, "15m", 8000, cache=True)
    except Exception as e:
        if verbose:
            print(f"  [journal] K 线获取失败，跳过结算: {e}")
        return 0
    bars = sorted(bars, key=lambda b: b[0])
    n = 0
    for r in recs:
        if r.get("status") != "pending":
            continue
        try:
            res = _settle_one(r, bars)
        except Exception:
            res = None
        if res:
            r.update(res)
            n += 1
    if n:
        _write(recs)
    return n


# ============================ 统计 ============================
def wilson(k: int, n: int, z: float = 1.96):
    if n == 0:
        return (0.0, 1.0)
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (max(0.0, c - h), min(1.0, c + h))


def _norm_cdf(x: float) -> float:
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def stat_block(rs: List[Dict]) -> Dict:
    """输入已成交并结算的记录 → 绩效指标"""
    n = len(rs)
    if n == 0:
        return {"n": 0}
    Rs = [float(r.get("net_R", r.get("R", 0))) for r in rs]
    wins = [x for x in Rs if x > 0]
    losses = [x for x in Rs if x <= 0]
    k = len(wins)
    ev = sum(Rs) / n
    sd = (sum((x - ev) ** 2 for x in Rs) / (n - 1)) ** 0.5 if n > 1 else 0.0
    se = sd / math.sqrt(n) if n > 1 else 0.0
    t = ev / se if se > 0 else 0.0
    p = 2 * (1 - _norm_cdf(abs(t))) if se > 0 else 1.0
    gp = sum(wins) or 0.0
    gl = -sum(losses) or 0.0
    lo, hi = wilson(k, n)
    mae = sum(float(r.get("mae", 0)) for r in rs) / n
    mfe = sum(float(r.get("mfe", 0)) for r in rs) / n
    return {"n": n, "win": k, "wr": k / n, "wr_lo": lo, "wr_hi": hi,
            "ev": ev, "sd": sd, "t": t, "p": p,
            "pf": (gp / gl if gl > 0 else float("inf")),
            "avg_win": (sum(wins) / k if k else 0.0),
            "avg_loss": (sum(losses) / len(losses) if losses else 0.0),
            "mae": mae, "mfe": mfe,
            "equity": sum(Rs),
            "max_consec_loss": _max_consec_loss(Rs)}


def _max_consec_loss(Rs: List[float]) -> int:
    cur = mx = 0
    for x in Rs:
        cur = cur + 1 if x <= 0 else 0
        mx = max(mx, cur)
    return mx


def _aligned(r: Dict) -> Optional[bool]:
    """是否顺势：做多 & 多头排列 / 做空 & 空头排列"""
    tr = (r.get("tags") or {}).get("trend", "")
    if tr.startswith("多头"):
        return r.get("side") == "long"
    if tr.startswith("空头"):
        return r.get("side") == "short"
    return None


def all_buckets() -> List:
    f: List = [("全部信号", lambda r: True),
               ("做多", lambda r: r.get("side") == "long"),
               ("做空", lambda r: r.get("side") == "short")]
    f += [("核心①②满足", lambda r: bool(r.get("core_ok"))),
          ("核心①②未满足", lambda r: not bool(r.get("core_ok")))]
    f += [("得分≥4", lambda r: int(r.get("score", 0)) >= 4),
          ("得分=3", lambda r: int(r.get("score", 0)) == 3),
          ("得分≤2", lambda r: int(r.get("score", 0)) <= 2)]
    f += [("顺势", lambda r: _aligned(r) is True),
          ("逆势/震荡", lambda r: _aligned(r) is not True)]
    f += [("放量", lambda r: (r.get("tags") or {}).get("vol") == "放量"),
          ("缩量", lambda r: (r.get("tags") or {}).get("vol") == "缩量")]
    f += [("Fib 共振", lambda r: (r.get("tags") or {}).get("fib") == "共振"),
          ("Fib 无共振", lambda r: (r.get("tags") or {}).get("fib") != "共振")]
    return f


# ============================ 报告 ============================
def fmt_row(name: str, s: Dict, min_n: int = 1) -> Optional[str]:
    if s.get("n", 0) < min_n:
        return None
    n, wr, ev = s["n"], s["wr"] * 100, s["ev"]
    pf = "∞" if s["pf"] == float("inf") else f"{s['pf']:.2f}"
    sig = "★" if (s["p"] < 0.05 and ev > 0) else ("·" if ev > 0 else "")
    return (f"{name:<14}{n:>4}{wr:>7.1f}%{s['wr_lo']*100:>7.1f}~{s['wr_hi']*100:<5.1f}"
            f"{ev:>+8.2f}{pf:>7}{s['mae']:>7.2f}{s['mfe']:>7.2f}"
            f"{s['equity']:>+8.1f}{s['max_consec_loss']:>5}{s['p']:>7.3f}  {sig}")


HEAD = (f"{'分层':<14}{'n':>4}{'胜率':>7}{'95%CI':>13}{'EV(R)':>8}{'PF':>7}"
        f"{'MAE':>7}{'MFE':>7}{'累计R':>8}{'连亏':>5}{'p值':>7}")


def report(min_n: int = 1, md: bool = False) -> str:
    recs = _read()
    settled = [r for r in recs if r.get("status") == "settled"]
    filled = [r for r in settled if r.get("filled")]
    missed = [r for r in settled if not r.get("filled")]
    pend = [r for r in recs if r.get("status") == "pending"]
    L = []
    L.append(f"\n{'='*104}")
    L.append(f"  信号绩效复盘    {time.strftime('%Y-%m-%d %H:%M:%S')}")
    L.append(f"{'='*104}")
    L.append(f"  总记录 {len(recs)} ｜ 已结算 {len(settled)}（成交 {len(filled)} / 未挂到 {len(missed)}）"
             f"｜ 待结算 {len(pend)}")
    if not filled:
        L.append(f"\n  {WARN} 尚无已成交样本。每跑一次 scan.py 会记一条，价格回到挂单价后自动结算。")
        L.append(f"     提示：核心①②未满足时的记录也会留存，用于验证「不达标的信号是不是真的差」。")
        L.append(_baseline_text())
        L.append(f"{'='*104}\n")
        return "\n".join(L)
    L.append(f"\n{'-'*104}\n  【总体】\n{'-'*104}")
    L.append(HEAD)
    L.append(fmt_row("全部（已成交）", stat_block(filled)))
    L.append(f"\n{'-'*104}\n  【分层】只列 n ≥ {min_n} 的分组（小样本看 95%CI，别看点估计）\n{'-'*104}")
    L.append(HEAD)
    for name, fn in all_buckets():
        sub = [r for r in filled if fn(r)]
        s = stat_block(sub)
        row = fmt_row(name, s, min_n)
        if row:
            L.append(row)
    L.append("")
    L.append("  说明：EV(R)=平均每笔净收益（1R=止损距离）；PF=总盈/总亏；MAE/MFE=最大不利/有利偏移（R）；")
    L.append("        p 值为 EV≠0 的正态近似，n<20 基本不可信；★ = p<0.05 且 EV>0。")
    L.append(f"  {WARN} 判据排序：EV(R) > 胜率。胜率低但 EV 正是正常的高赔率形态（T1 常 >1R），")
    L.append("     不要因为胜率掉到 40% 就去改策略；只有 EV<0、连亏≥5 或 PF<1 才动参数。")
    L.append(_baseline_text())
    L.append(f"{'='*104}\n")
    return "\n".join(L)


def _baseline_text() -> str:
    L = [f"\n{'-'*104}\n  【回测基准 · 对照用】（样本来自历史回测，不是本日志）\n{'-'*104}"]
    for nm, n, wr, ev, note in BASELINE:
        L.append(f"    {nm:<22}{('n='+str(n)) if n else '':>7}  胜率 {wr:>7}  EV {ev:>10}   {note}")
    return "\n".join(L)


def brief_line(contract: str = "BTC_USDT", maxn: int = 12) -> Optional[str]:
    """给 scan.py --brief 用的一行绩效摘要（样本为 0 时返回 None）"""
    rs = [r for r in _read() if r.get("status") == "settled" and r.get("filled")
          and r.get("contract") == contract][-maxn:]
    if not rs:
        return None
    s = stat_block(rs)
    tag = OK if s["ev"] > 0.2 else (WARN if s["ev"] > 0 else NO)
    return (f"  📊 近 {s['n']} 笔：胜率 {s['wr']*100:.0f}% ｜ EV {s['ev']:+.2f}R ｜ "
            f"累计 {s['equity']:+.1f}R {tag}")


# ============================ 回测复检（样本外监控） ============================
def recheck(contract: str = "BTC_USDT", timeout: int = 900) -> str:
    """
    用最新数据重跑日内双向回测（daytrade.py），看策略 alpha 是否还在。
    实盘日志样本增长太慢（一天最多 1-2 笔），真正能判断"策略有没有失效"的是这个。
    """
    import subprocess, re
    script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "daytrade.py")
    try:
        r = subprocess.run([sys.executable, script], capture_output=True,
                           text=True, timeout=timeout)
        out = r.stdout or ""
    except Exception as e:
        return f"\n  {WARN} 回测复检失败：{e}\n"

    def grab(tag):
        for ln in out.splitlines():
            if tag in ln:
                m = re.search(r"n=(\d+)\s+胜率=\s*([\d.]+)%\s+EV=\s*([+-][\d.]+)%", ln)
                if m:
                    return int(m.group(1)), float(m.group(2)), float(m.group(3))
        return None

    L = [f"\n{'='*104}",
         "  回测复检 · 用最新数据重跑（判断 alpha 是否衰减，比实盘日志快 100 倍）",
         f"{'='*104}"]
    lo, so = grab("单边 做多"), grab("单边 做空")
    if lo:
        L.append(f"    做多  n={lo[0]:<4} 胜率 {lo[1]:>5.1f}%  EV {lo[2]:+.2f}%/笔   "
                 + (OK + " 仍为正" if lo[2] > 0 else WARN + " ⚠️ 转负 → alpha 衰减，建议 risk_scale 减半"))
    if so:
        L.append(f"    做空  n={so[0]:<4} 胜率 {so[1]:>5.1f}%  EV {so[2]:+.2f}%/笔   "
                 + (OK + " 符合历史（做空系统性偏弱 → 仓位减半）" if so[2] < (lo[2] if lo else 0)
                    else WARN + " ⚠️ 做空反超，历史结论可能已失效"))
    for tag in ("全 maker", "全 taker"):
        g = grab(tag)
        if g:
            L.append(f"    {tag:<12} EV {g[2]:+.2f}%/笔（挂单 vs 吃单的成本差，坚持 post-only）")
    m = re.search(r"100倍\s+1\.00%\s+0\.70%\s+([\d.]+)%", out)
    if m:
        L.append(f"    100x 满仓被打穿率 {m.group(1)}% → 决定生死的是【名义/账户倍数】，不是杠杆数字")
    if lo and so and lo[2] > 0 and so[2] < 0:
        L.append("    → 结论：与历史实证一致（多强空弱），维持现参数，不动。")
    L.append(f"{'='*104}")
    return "\n".join(L)


# ============================ 优化建议 ============================
def load_tuning() -> Dict:
    if os.path.exists(TUNING):
        try:
            return json.load(open(TUNING, encoding="utf-8"))
        except Exception:
            pass
    return {}


def tune(min_n: int = 8) -> Dict:
    """
    生成保守调参建议：
      n<15  → 只允许减仓（风险侧）
      n≥15  → 允许加/减仓位、提门槛
      n≥30  → 允许放宽门槛、放大仓位
    """
    recs = _read()
    filled = [r for r in recs if r.get("status") == "settled" and r.get("filled")]
    cur = load_tuning()
    sug = {
        "version": 2, "generated": int(time.time()),
        "score_threshold": int(cur.get("score_threshold", 4)),
        "core_required": bool(cur.get("core_required", True)),
        "side_size": dict(cur.get("side_size", {"long": 1.0, "short": 0.5})),
        "risk_scale": float(cur.get("risk_scale", 1.0)),
        "notes": [],
    }
    if not filled:
        sug["notes"].append("无已成交样本 → 维持回测默认参数，不做任何调整")
        return sug

    def blk(fn):
        return stat_block([r for r in filled if fn(r)])

    # 0) 生存约束：连亏（不看 EV，属于爆仓/心态风险）
    g = stat_block(filled)
    if g["n"] >= 10 and g["max_consec_loss"] >= 5:
        sug["risk_scale"] = round(min(sug["risk_scale"], 0.5), 2)
        sug["notes"].append(
            f"最大连亏 {g['max_consec_loss']} 笔（n={g['n']}）→ 无论 EV 正负，单笔风险先减半（{sug['risk_scale']}×）")

    # 0.5) 挂单成交率（信号可用性，不是胜率）
    settled = [r for r in recs if r.get("status") == "settled"]
    missed = [r for r in settled if not r.get("filled")]
    if len(settled) >= 10 and len(missed) / len(settled) > 0.4:
        sug["notes"].append(
            f"挂单未成交率 {len(missed)/len(settled)*100:.0f}%（{len(missed)}/{len(settled)}）"
            f" → 挂单太靠内，考虑改追价或用 T1 更近的挂单位（本工具不自动改）")

    # 1) 全局
    g = stat_block(filled)
    if g["n"] >= 20 and g["ev"] < 0:
        sug["risk_scale"] = round(min(sug["risk_scale"], 0.5), 2)
        sug["notes"].append(f"全局 n={g['n']} EV={g['ev']:+.2f}R<0 → 单笔风险减半（{sug['risk_scale']}×）")
    elif g["n"] >= 30 and g["ev"] > 0.4 and g["wr"] > 0.55:
        sug["risk_scale"] = round(min(sug["risk_scale"] * 1.25, 1.5), 2)
        sug["notes"].append(f"全局 n={g['n']} EV={g['ev']:+.2f}R 且胜率{g['wr']*100:.0f}% → 风险上调至 {sug['risk_scale']}×")

    # 2) 分方向仓位
    for side, key in (("long", "long"), ("short", "short")):
        s = blk(lambda r, sd=side: r.get("side") == sd)
        if s["n"] < 15:
            if s["n"] >= min_n and s["ev"] < -0.1:
                sug["side_size"][key] = round(sug["side_size"][key] * 0.5, 2)
                sug["notes"].append(f"{side} n={s['n']}（<15，只允许减仓）EV={s['ev']:+.2f}R → 仓位 ×{sug['side_size'][key]}")
            continue
        if s["ev"] < 0:
            sug["side_size"][key] = round(max(sug["side_size"][key] * 0.5, 0.25), 2)
            sug["notes"].append(f"{side} n={s['n']} EV={s['ev']:+.2f}R<0 → 仓位 ×{sug['side_size'][key]}")
        elif s["ev"] > 0.4 and s["wr"] > 0.55 and s["n"] >= 30:
            sug["side_size"][key] = round(min(sug["side_size"][key] * 1.25, 1.5), 2)
            sug["notes"].append(f"{side} n={s['n']} EV={s['ev']:+.2f}R 胜率{s['wr']*100:.0f}% → 仓位 ×{sug['side_size'][key]}")

    # 3) 得分门槛
    for thr, lab in ((4, "≥4"), (3, "=3"), (2, "≤2")):
        s = blk(lambda r, t=thr: (int(r.get("score", 0)) >= t) if t == 4 else
                ((int(r.get("score", 0)) == 3) if t == 3 else (int(r.get("score", 0)) <= 2)))
        if s["n"] >= 15 and s["ev"] < 0 and thr <= sug["score_threshold"]:
            sug["score_threshold"] = min(6, thr + 1)
            sug["notes"].append(f"得分{lab} n={s['n']} EV={s['ev']:+.2f}R<0 → 门槛提到 {sug['score_threshold']}/6")
            break
        if s["n"] >= 30 and s["ev"] > 0.5 and thr == 3 and sug["score_threshold"] > 3:
            sug["notes"].append(f"得分{lab} n={s['n']} EV={s['ev']:+.2f}R 表现好 → 可考虑门槛降到 3（需人工确认）")

    # 4) 核心条件校验（只做监控，不自动关）
    sc = blk(lambda r: bool(r.get("core_ok")))
    sn = blk(lambda r: not bool(r.get("core_ok")))
    if sc["n"] >= 10 and sn["n"] >= 10:
        sug["notes"].append(
            f"核心①②：满足 n={sc['n']} EV={sc['ev']:+.2f}R vs 未满足 n={sn['n']} EV={sn['ev']:+.2f}R"
            + ("  → 核心条件有效，保持 core_required=True"
               if sc["ev"] > sn["ev"] + 0.2 else "  → ⚠️ 核心条件未见优势，请人工复核（本工具不会自动关闭它）"))

    # 5) 顺势/逆势
    sa = blk(lambda r: _aligned(r) is True)
    sr = blk(lambda r: _aligned(r) is not True)
    if sa["n"] >= 15 and sr["n"] >= 15:
        sug["notes"].append(f"顺势 n={sa['n']} EV={sa['ev']:+.2f}R vs 逆势/震荡 n={sr['n']} EV={sr['ev']:+.2f}R"
                            + ("  → 顺势明显更好，逆势建议放弃或减半"
                               if sa["ev"] > sr["ev"] + 0.3 else ""))
    if not sug["notes"]:
        sug["notes"].append(f"样本 n={len(filled)}，各分层均未达调整门槛 → 维持现状（不动就是最好的优化）")
    return sug


def fmt_tune(sug: Dict) -> str:
    L = [f"\n{'='*104}", "  优化建议（默认不生效，需 python journal.py apply）", f"{'='*104}"]
    L.append(f"    score_threshold : {sug['score_threshold']}/6")
    L.append(f"    core_required   : {sug['core_required']}")
    L.append(f"    side_size       : long ×{sug['side_size'].get('long',1)}  short ×{sug['side_size'].get('short',1)}")
    L.append(f"    risk_scale      : ×{sug['risk_scale']}")
    L.append(f"  {'-'*100}")
    for n in sug["notes"]:
        L.append(f"    · {n}")
    L.append(f"{'='*104}\n")
    return "\n".join(L)


# ============================ CLI ============================
def main():
    ap = argparse.ArgumentParser(add_help=False)
    ap.add_argument("cmd", nargs="?", default="stats")
    ap.add_argument("--min-n", type=int, default=1)
    ap.add_argument("--contract", default="BTC_USDT")
    ap.add_argument("--md", action="store_true")
    ap.add_argument("--limit", type=int, default=20)
    a = ap.parse_args()
    cmd = a.cmd

    if cmd == "settle":
        n = settle_all(a.contract, verbose=True)
        print(f"[journal] 结算 {n} 条 → {JOURNAL}")
        return 0
    if cmd == "stats":
        settle_all(a.contract)
        print(report(min_n=a.min_n, md=a.md))
        return 0
    if cmd == "list":
        recs = _read()[-a.limit:]
        print(f"\n{'时间':<13}{'方向':<7}{'分数':<6}{'核心':<6}{'现价':>10}{'挂单':>10}"
              f"{'止损':>10}{'T1':>10}{'结果':<9}{'R':>7}{'MAE':>6}{'MFE':>6}{'持仓h':>7}")
        for r in recs:
            res = r.get("outcome", "pending")
            print(f"{time.strftime('%m-%d %H:%M', time.localtime(r['ts'])):<13}"
                  f"{r['side']:<7}{r.get('score','-'):<6}{'Y' if r.get('core_ok') else 'N':<6}"
                  f"{r['last']:>10,.1f}{r['entry']:>10,.1f}{r['sl']:>10,.1f}{r['tp1']:>10,.1f}"
                  f"{res:<9}{r.get('net_R','-'):>7}{r.get('mae','-'):>6}{r.get('mfe','-'):>6}"
                  f"{r.get('hold_h','-'):>7}")
        print(f"\n共 {len(_read())} 条 · 文件 {JOURNAL}\n")
        return 0
    if cmd == "tune":
        settle_all(a.contract)
        sug = tune()
        json.dump(sug, open(TUNING_SUG, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
        print(report(min_n=a.min_n))
        print(recheck(a.contract))
        print(fmt_tune(sug))
        print(f"[saved] 建议已写入 {TUNING_SUG}（执行 `python journal.py apply` 生效）")
        return 0
    if cmd == "recheck":
        print(recheck(a.contract))
        return 0
    if cmd == "apply":
        if not os.path.exists(TUNING_SUG):
            print("没有待采纳的建议，先跑 `python journal.py tune`")
            return 1
        sug = json.load(open(TUNING_SUG, encoding="utf-8"))
        sug["applied"] = int(time.time())
        json.dump(sug, open(TUNING, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
        print(f"[saved] {TUNING}\n{fmt_tune(sug)}")
        return 0
    if cmd == "reset":
        c = input(f"确认清空 {JOURNAL}？(yes/no) ")
        if c.strip().lower() == "yes":
            _write([])
            print("已清空")
        return 0
    print(__doc__)
    return 0


if __name__ == "__main__":
    sys.exit(main())
