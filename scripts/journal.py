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

# 2026-10-11 加：训练环境模式（journal 隔离）
# 训练模式下 JOURNAL 切换到 training_journal.jsonl，跟生产隔离
if os.environ.get("TRAINING_MODE") == "1":
    TRAINING_DATA_DIR = os.environ.get("TRAINING_DATA_DIR", "/root/data/training")
    JOURNAL = os.path.join(TRAINING_DATA_DIR, "journal.jsonl")
else:
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
def _account_safety_check() -> tuple[bool, float, str]:
    """2026-10-11 账户级安全阀（防 -50U 累计极端亏损）
    
    返回: (allowed, cumulative_24h_net, message)
      allowed=True  → 允许开新仓
      allowed=False → 阻止开新仓
    
    触发条件: 近 24 小时累计 net_usd < -50U
    解锁条件: 人工调用 `python journal.py unlock` 或时间窗到期
    
    Unlock 机制: marker 文件 (account_unlock.marker) 24h 内有效
    """
    # 检查 unlock marker
    marker = os.path.join(DATA_DIR, "account_unlock.marker")
    if os.path.exists(marker):
        try:
            with open(marker) as f:
                m_ts = int(f.read().strip() or 0)
            # marker 24h 内有效
            if time.time() - m_ts < 24 * 3600:
                recs = _read()
                now = int(time.time())
                win_start = now - 24 * 3600
                cumulative = 0.0
                for r in recs:
                    if r.get("status") != "settled":
                        continue
                    exit_ts = r.get("exit_ts") or r.get("ts") or 0
                    if exit_ts and exit_ts >= win_start:
                        cumulative += r.get("net_usd", 0.0)
                return (True, cumulative, "账户安全阀已解锁 (人工 unlock)")
        except Exception:
            pass
    
    recs = _read()
    now = int(time.time())
    win_start = now - 24 * 3600
    cumulative = 0.0
    for r in recs:
        if r.get("status") != "settled":
            continue
        exit_ts = r.get("exit_ts") or r.get("ts") or 0
        if exit_ts and exit_ts >= win_start:
            cumulative += r.get("net_usd", 0.0)
    
    if cumulative < -50.0:
        return (False, cumulative, f"账户级安全阀触发：近 24h 累计 {cumulative:+.2f}U < -50U 阈值")
    return (True, cumulative, "账户正常")


# 安全阀开关（默认启用，可用环境变量 BTC_NO_ACCOUNT_LOCK=1 临时禁用）
_ACCOUNT_LOCK_ENABLED = os.environ.get("BTC_NO_ACCOUNT_LOCK") != "1"


def log_signal(contract: str, last: float, side: str, score: int, core_ok: bool,
               verdict: str, plan: Dict, tags: Dict, meta: Dict = None,
               preview_secs: int = 0) -> str:
    """
    记录一条信号。同方向 + 冷却期内 → 刷新上一条（不重复计数）。

    2026-10-11 加 preview_secs 参数（liusir 反饋：信号进 journal 前先给
    用户一张预览卡，避免错过推送就不知道发生了什么）：
      - preview_secs=0 → 立即生效 (status="pending")
      - preview_secs>0 → 写入时用 status="preview_pending"，加 preview_until_ts
        字段；调用方需配合 promote_pending/cancel_preview 在到期前后处理。

    2026-10-11 加账户级安全阀（防 -50U 累计极端亏损）：
      - 近 24h 累计 net_usd < -50U 时，阻止新信号挂单
      - 解锁: `python journal.py unlock` 或 24h 时间窗到期

    返回: "added" / "updated" / "off" / "blocked_account_safety"
    """
    if os.environ.get("BTC_NO_JOURNAL") == "1":
        return "off"
    # 2026-10-11 账户级安全阀（v5 风控机制, 非参数调优）
    if _ACCOUNT_LOCK_ENABLED:
        allowed, cum, msg = _account_safety_check()
        if not allowed:
            # 阻止但不返回"off"——让调用方能区分"journals 关闭" vs "安全阀触发"
            # 同时打日志（不推送，避免刷屏）
            print(f"[journal] 账户安全阀触发: {msg} ｜ 信号被阻止 side={side} entry={plan.get('entry_limit')}")
            return "blocked_account_safety"
    recs = _read()
    now = int(time.time())
    if preview_secs and preview_secs > 0:
        initial_status = "preview_pending"
        preview_until_ts = now + int(preview_secs)
    else:
        initial_status = "pending"
        preview_until_ts = None
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
        "status": initial_status, "tags": dict(tags or {}),
        "meta": dict(meta or {}),
        # 2026-10-09 加: TP 体系识别 (15m 形态信号专用)
        "tp_mode": plan.get("tp_mode", "auto"),
        "signal_source": plan.get("signal_source", "1h_main"),  # "1h_main" / "15m_pattern"
        "custom_tp1_pct": plan.get("custom_tp1_pct"),
        "custom_tp2_pct": plan.get("custom_tp2_pct"),
        # 2026-10-11 加: 预览闸门
        "preview_secs": int(preview_secs) if preview_secs and preview_secs > 0 else 0,
        "preview_until_ts": preview_until_ts,
    }
    if recs:
        # 2026-10-11 修复：优先检查同方向是否有 pending 持仓（filled=true）
        # 问题：cooldown 判定在前，导致有持仓时仍走"刷新"逻辑，不阻止新单
        # 修复：先检查持仓，再检查 cooldown
        for r in recs:
            if (r.get("contract") == contract and r.get("side") == side
                    and r.get("status") == "pending"
                    and r.get("filled", False)):
                # 同方向有 pending 持仓，阻止新单
                print(f"[journal] 同方向有 pending 持仓，阻止新单: "
                      f"id={r.get('id')}  entry={r.get('entry')}  filled={r.get('filled')}")
                return "blocked_same_side_holding"
        
        p = recs[-1]
        # 冷却匹配同样适用于 preview_pending (同一信号前后两次刷新都在冷却内)
        if (p.get("contract") == contract and p.get("side") == side
                and p.get("status") in ("pending", "preview_pending")
                and now - int(p.get("ts", 0)) < COOLDOWN):
            keep_id, keep_ts = p.get("id"), p.get("ts")
            p.update(fresh)
            p["id"], p["ts"] = keep_id, keep_ts
            _write(recs)
            return "updated"
    
    fresh["id"] = f"{contract}-{now}-{side}"
    recs.append(fresh)
    _write(recs)
    return "added"


# ============================ 预览闸门（2026-10-11 新增） ============================
def promote_pending(signal_id: str) -> bool:
    """
    把预览中的信号推进到 pending（允许 PositionTracker 接管挂单 / 盯盘）。
    返回 True 表示找到并推进，False 表示没找到 / 已不是 preview_pending。
    """
    recs = _read()
    hit = False
    for r in recs:
        if r.get("id") == signal_id and r.get("status") == "preview_pending":
            r["status"] = "pending"
            r["promoted_ts"] = int(time.time())
            hit = True
            break
    if hit:
        _write(recs)
    return hit


def cancel_preview(signal_id: str, reason: str = "USER_CANCEL") -> bool:
    """
    取消一条仍在 preview 的信号（不进入 pending 也不挂单）。
    返回 True 表示找到并取消。
    """
    recs = _read()
    hit = False
    for r in recs:
        if r.get("id") == signal_id and r.get("status") == "preview_pending":
            r["status"] = "cancelled"
            r["outcome"] = "CANCELLED"
            r["exit_ts"] = int(time.time())
            r["note"] = reason
            hit = True
            break
    if hit:
        _write(recs)
    return hit


def promote_due_previews(contract: str = "BTC_USDT") -> List[str]:
    """
    把所有 preview_until_ts 已过期的 preview_pending 信号自动推进到 pending。
    返回被推进的 signal_id 列表。
    """
    recs = _read()
    now = int(time.time())
    promoted: List[str] = []
    for r in recs:
        if (r.get("contract") == contract
                and r.get("status") == "preview_pending"
                and r.get("preview_until_ts")
                and now >= int(r["preview_until_ts"])):
            r["status"] = "pending"
            r["promoted_ts"] = now
            r["note"] = (r.get("note") or "") + " | auto-promoted after preview"
            promoted.append(r.get("id"))
    if promoted:
        _write(recs)
    return promoted


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
        # 2026-10-07 liusir 规则：TIMEOUT 24h 强平已取消
        #     强平仅依赖 watch/runner.py:_check_trend_reversal 检测趋势反转
        #     老 MAX_HOLD 常量已废弃（保留以避免破坏其它模块的 import）
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
    # 2026-10-11 v5: add_teacher / backup_production 需要自定义参数
    # 在 argparse 之前拦截，避免 argparse 报错
    if len(sys.argv) >= 2 and sys.argv[1] in ("add_teacher", "backup_production"):
        cmd = sys.argv[1]
        if cmd == "add_teacher":
            # 解析参数（直接用 sys.argv 避免 argparse 限制）
            args_dict = {}
            for i in range(2, len(sys.argv), 2):
                if i < len(sys.argv) and sys.argv[i].startswith('--'):
                    key = sys.argv[i][2:].replace('-', '_')
                    if i + 1 < len(sys.argv):
                        args_dict[key] = sys.argv[i + 1]
            
            if not all(k in args_dict for k in ['side', 'entry', 'sl', 'tp1', 'tp2']):
                print("用法: python3 journal.py add_teacher --side long|short --entry X --sl X --tp1 X --tp2 X [--note '...' ]")
                return 1
            
            side = args_dict['side']
            if side not in ['long', 'short']:
                print(f"✗ side 必须是 long 或 short, 得到: {side}")
                return 1
            
            entry = float(args_dict['entry'])
            sl = float(args_dict['sl'])
            tp1 = float(args_dict['tp1'])
            tp2 = float(args_dict['tp2'])
            note = args_dict.get('note', '')
            source = args_dict.get('source', 'liusir_manual')
            
            from datetime import datetime, timezone, timedelta
            CST = timezone(timedelta(hours=8))
            now = datetime.now(CST)
            today = now.strftime("%Y-%m-%d")
            ts = int(now.timestamp())
            
            teacher_rec = {
                "id": f"TEACHER-{ts}-{side}",
                "ts": ts,
                "datetime": now.strftime("%Y-%m-%d %H:%M:%S"),
                "side": side,
                "entry": entry,
                "sl": sl,
                "tp1": tp1,
                "tp2": tp2,
                "note": note,
                "source": source,
                "type": "teacher_signal",
            }
            
            # 写到 teacher_signals/
            teacher_dir = os.path.join(DATA_DIR, "teacher_signals")
            os.makedirs(teacher_dir, exist_ok=True)
            teacher_file = os.path.join(teacher_dir, f"teacher_{today}.jsonl")
            
            with open(teacher_file, "a") as f:
                f.write(json.dumps(teacher_rec, ensure_ascii=False) + "\n")
            
            print(f"✓ 老师单已录入")
            print(f"  ID: {teacher_rec['id']}")
            print(f"  时间: {teacher_rec['datetime']}")
            print(f"  方向: {side}")
            print(f"  入场: {entry:,.1f}")
            print(f"  SL: {sl:,.1f}  TP1: {tp1:,.1f}  TP2: {tp2:,.1f}")
            print(f"  备注: {note}")
            print(f"  文件: {teacher_file}")
            print()
            print(f"用途: 1) benchmark 对比  2) 训练信号（学习开单逻辑）  3) 反向学习验证")
            return 0
        
        if cmd == "backup_production":
            from datetime import datetime, timezone, timedelta
            CST = timezone(timedelta(hours=8))
            today = datetime.now(CST).strftime("%Y-%m-%d")
            
            prod_dir = os.path.join(DATA_DIR, "production_trades")
            os.makedirs(prod_dir, exist_ok=True)
            backup_path = os.path.join(prod_dir, f"journal_{today}.jsonl")
            
            import shutil
            if os.path.exists(JOURNAL):
                shutil.copy2(JOURNAL, backup_path)
                with open(backup_path) as f:
                    recs = [json.loads(ln) for ln in f if ln.strip()]
                print(f"✓ 备份 {len(recs)} 条到 {backup_path}")
            else:
                print(f"⚠️  journal.jsonl 不存在")
            return 0
    
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
    if cmd == "cancel":
        # 2026-10-11 新增：手动取消预览中的信号
        sig_id = sys.argv[2] if len(sys.argv) > 2 else None
        if not sig_id:
            # 列出所有 preview_pending 供挑
            pend = [r for r in _read() if r.get("status") == "preview_pending"]
            if not pend:
                print("无 preview_pending 信号。")
                return 0
            print(f"待取消的预览信号 ({len(pend)} 条):")
            for r in pend:
                _ts = time.strftime("%H:%M:%S", time.localtime(r["ts"]))
                _until = r.get("preview_until_ts") or 0
                _left = max(0, int(_until - time.time()))
                print(f"  id={r.get('id')}  {_ts}  side={r['side']}  "
                      f"entry={r['entry']:,.1f}  剩 {_left}s 自动 promote")
            print("\n用法: python3 journal.py cancel <signal_id>")
            return 0
        ok = cancel_preview(sig_id)
        if ok:
            print(f"✓ 已取消 {sig_id}")
        else:
            print(f"✗ 取消失败（id={sig_id} 已不是 preview_pending？用 list 看下）")
        return 0
    if cmd == "promote":
        # 2026-10-11 新增：手动 promote preview_pending → pending
        sig_id = sys.argv[2] if len(sys.argv) > 2 else None
        if sig_id:
            ok = promote_pending(sig_id)
            print(f"{'✓' if ok else '✗'} promote {sig_id} → {ok}")
            return 0
        # 不传 id：自动 promote 所有到期记录
        promoted = promote_due_previews(a.contract)
        print(f"自动 promote {len(promoted)} 条：{promoted}")
        return 0
    if cmd == "unlock":
        # 2026-10-11 新增：账户级安全阀手动解锁（针对 -50U 累计极端亏损）
        # 解锁方式：把近 24h 累计重置为 0（即不再阻止新信号）
        # 实现: 写一个 unlock marker，让 _account_safety_check 跳过
        import time as _t
        marker = os.path.join(DATA_DIR, "account_unlock.marker")
        now_ts = int(_t.time())
        with open(marker, "w") as f:
            f.write(f"{now_ts}\n")
        # 同时打印当前累计
        allowed, cum, msg = _account_safety_check()
        print(f"✓ 账户安全阀已解锁 (marker 写入 {marker})")
        print(f"  近 24h 累计: {cum:+.2f}U ｜ 状态: {msg}")
        print(f"  解锁到期: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(now_ts + 24*3600))}")
        return 0
    if cmd == "lock":
        # 2026-10-11 新增：手动锁定账户（用于测试 / 暂停开新仓）
        marker = os.path.join(DATA_DIR, "account_unlock.marker")
        if os.path.exists(marker):
            os.remove(marker)
        print(f"✓ 账户安全阀已强制锁定 (移除 {marker})")
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
    if cmd == "add_teacher":
        # 2026-10-11 v5 训练环境：老师单录入（benchmark + 训练信号 + 反向学习）
        # 用法: python3 journal.py add_teacher --side long --entry 83000 --sl 82500 --tp1 83500 --tp2 84000 --note "..."
        # 录入到: teacher_signals/teacher_YYYY-MM-DD.jsonl
        # 解析参数（直接用 sys.argv 避免 argparse 限制）
        args_dict = {}
        for i in range(2, len(sys.argv), 2):
            if sys.argv[i].startswith('--'):
                key = sys.argv[i][2:].replace('-', '_')
                if i + 1 < len(sys.argv):
                    args_dict[key] = sys.argv[i + 1]
        
        if not all(k in args_dict for k in ['side', 'entry', 'sl', 'tp1', 'tp2']):
            print("用法: python3 journal.py add_teacher --side long|short --entry X --sl X --tp1 X --tp2 X [--note '...' ]")
            return 1
        
        side = args_dict['side']
        if side not in ['long', 'short']:
            print(f"✗ side 必须是 long 或 short, 得到: {side}")
            return 1
        
        entry = float(args_dict['entry'])
        sl = float(args_dict['sl'])
        tp1 = float(args_dict['tp1'])
        tp2 = float(args_dict['tp2'])
        note = args_dict.get('note', '')
        source = args_dict.get('source', 'liusir_manual')
        
        from datetime import datetime, timezone, timedelta
        CST = timezone(timedelta(hours=8))
        now = datetime.now(CST)
        today = now.strftime("%Y-%m-%d")
        ts = int(now.timestamp())
        
        teacher_rec = {
            "id": f"TEACHER-{ts}-{side}",
            "ts": ts,
            "datetime": now.strftime("%Y-%m-%d %H:%M:%S"),
            "side": side,
            "entry": entry,
            "sl": sl,
            "tp1": tp1,
            "tp2": tp2,
            "note": note,
            "source": source,
            "type": "teacher_signal",
        }
        
        # 写到 teacher_signals/
        teacher_dir = os.path.join(DATA_DIR, "teacher_signals")
        os.makedirs(teacher_dir, exist_ok=True)
        teacher_file = os.path.join(teacher_dir, f"teacher_{today}.jsonl")
        
        with open(teacher_file, "a") as f:
            f.write(json.dumps(teacher_rec, ensure_ascii=False) + "\n")
        
        print(f"✓ 老师单已录入")
        print(f"  ID: {teacher_rec['id']}")
        print(f"  时间: {teacher_rec['datetime']}")
        print(f"  方向: {side}")
        print(f"  入场: {entry:,.1f}")
        print(f"  SL: {sl:,.1f}  TP1: {tp1:,.1f}  TP2: {tp2:,.1f}")
        print(f"  备注: {note}")
        print(f"  文件: {teacher_file}")
        print()
        print(f"用途: 1) benchmark 对比  2) 训练信号（学习开单逻辑）  3) 反向学习验证")
        return 0
    if cmd == "backup_production":
        # 2026-10-11 v5：备份生产 journal 到 production_trades/
        # 用法: python3 journal.py backup_production
        from datetime import datetime, timezone, timedelta
        CST = timezone(timedelta(hours=8))
        today = datetime.now(CST).strftime("%Y-%m-%d")
        
        prod_dir = os.path.join(DATA_DIR, "production_trades")
        os.makedirs(prod_dir, exist_ok=True)
        backup_path = os.path.join(prod_dir, f"journal_{today}.jsonl")
        
        # 复制当前 journal
        import shutil
        if os.path.exists(JOURNAL):
            shutil.copy2(JOURNAL, backup_path)
            with open(backup_path) as f:
                recs = [json.loads(ln) for ln in f if ln.strip()]
            print(f"✓ 备份 {len(recs)} 条到 {backup_path}")
        else:
            print(f"⚠️  journal.jsonl 不存在")
        return 0
    print(__doc__)
    return 0


if __name__ == "__main__":
    sys.exit(main())
