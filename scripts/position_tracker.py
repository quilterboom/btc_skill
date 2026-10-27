#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
持仓跟踪模拟器（不入真单，纯本地回放 + 推送）
==============================================

设计目的：
  journal.py 已有 TP1/SL 二选一的结算逻辑，但不支持
  "TP1 部分止盈 → 移动 BE → BE 被打仍算盈利" 这套真实日内做法。
  本脚本在 journal.py 基础上叠加更精细的状态机。

规则（按用户指定）：
  仓位：50 USDT 名义（固定） → 张数 = round(50 / entry / 0.0001)
  杠杆：100x（仅记录用，不影响算法）
  手续费：4.85 USDT/笔（开+平双边，含滑点+funding）
  保本价：开仓价（账面打平，不扣手续费）
  方向：多 + 空都追踪
  止盈分档：1/3 × TP1 + 1/3 × TP2 + 1/3 × TP3（标准 3 段部分止盈）

状态机：
  pending
   ├ 24h 未挂到 → MISSED（不计入胜率）
   ├ 成交后
   │   ├ 打到 TP1 → TP1_HIT, 1/3 仓位平仓，SL 移到 BE
   │   │   ├ 打到 TP2 → TP2_HIT, 1/3 再平仓，SL 移到 TP1
   │   │   │   ├ 打到 TP3 → TP3_HIT, 全部平仓 ✓ 盈利归类
   │   │   │   ├ 回打到 TP1 → 余 1/3 在 TP1 平（部分盈利）
   │   │   │   ├ 回打到 BE  → 余 1/3 盈亏平衡
   │   │   │   └ TIMEOUT 24h → 收盘平
   │   │   ├ 回打到 BE  → BE_STOPPED, 仍算盈利（TP1 部分锁定）
   │   │   ├ TIMEOUT 24h → 收盘平
   │   ├ 直接打到 SL → SL_STOPPED（亏损归类）
   │   └ TIMEOUT 24h → 收盘平

防自欺（同根双触）：
  - 未触发 TP1 前：SL/TP 同根 → 保守判 SL
  - TP1 已触发：BE/TP2 同根 → TP2 优先（已锁定 TP1）
  - TP2 已触发：BE/TP3 同根 → TP3 优先
"""
from __future__ import annotations
import os, sys, json, time, argparse
from pathlib import Path
from typing import List, Dict, Optional, Tuple
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from gate_fetch import DATA_DIR, fetch_ohlcv
from indicators import ema as _ema_func
from telegram import load_token, load_chat, push as push_telegram
try:
    from config import PARTIAL_CLOSE_AFTER_SEC, PARTIAL_CLOSE_RATIO
except ImportError:
    PARTIAL_CLOSE_AFTER_SEC = 3600    # 1h 兜底
    PARTIAL_CLOSE_RATIO = 0.5

JOURNAL = os.path.join(DATA_DIR, "journal.jsonl")
EVENTS = os.path.join(DATA_DIR, "position_events.jsonl")

# 用户指定常量
# 2026-10-08 liusir 决策：风控优先（杠杆 100x + 本金 50U 都不动）
#   liusir 明确：单笔保证金 50U = 100% 本金（满仓）——信任入场信号
#   风控兜底不靠"单笔仓位小"，靠"账户回撤 -30% 硬止损"（剩 35U 全停）
#   名义 5000U → BTC 1% 波动 = ±50U = 整个本金 → 单笔最大亏 -50U 量级
#   关键观察（5.5 天 45 条样本）：当前实证最大亏 -44.85U（1791209345-long）
#   满仓 + 回撤兜底 = 接受单笔大亏、靠信号胜率 + 回撤硬止损兜住系统性风险
MARGIN_USD = 50.0                           # 2026-10-08 liusir: 单笔保证金 50U（满仓）
ACCOUNT_BALANCE_USD = 50.0                  # 2026-10-08 liusir: 本金保持 50U 不变
RISK_PER_TRADE_PCT = 1.0                    # 单笔保证金占本金 100%（满仓）
MAX_DRAWDOWN_PCT = 0.30                     # 账户回撤 30% 全停（liusir 拍板）
LEVERAGE = 100                              # 100x 杠杆（liusir 明确不动）
NOTIONAL_USD = MARGIN_USD * LEVERAGE        # 名义 = 5000U
# 单笔总成本：基于 journal 45 条样本真实结算数据反推（2026-10-08 liusir 取消返佣逻辑）
#   历史 journal net-gross 分布：17 条 diff=-4.85（taker 收费）+ 8 条 diff=+1.0（maker 返佣）
#   即大部分单子走 taker（被扣 4.85U），少数纯 maker 单子能拿到 +1.0 返佣。
#   原代码统一按 -1.0U 返佣计算 = **历史 bug**，让 taker 单子"少扣"了 5.85U = 净利虚高。
#   修正：FEE_USD = +4.85（按实际 taker 费率计费，符合 journal 多数 diff 的真实成本）。
#   名义 5000U × 0.0485% × 2 (entry+exit) = 4.85U（Gate.io BTC 永续 taker 实际费率）
#   注：少数纯 maker 单子的 +1.0 返佣**不计入**——保守按 taker 算，避免"返佣兜底"假盈利。
FEE_USD = 4.85                               # 单笔总成本（taker 往返收费，2026-10-08 改为正向收费）
PARTS = 3                                   # 3 段部分止盈
QUANT_MULT = 0.0001                         # 1 张 = 0.0001 BTC
MAX_HOLD = 24 * 3600                        # 日内 24h 强制平
MAX_PEND = 24 * 3600                        # 2026-10-07 liusir 规则：挂单 24h 没挂到 → MISSED
OK, NO, WARN = "✅", "❌", "⚠️"


# ============================ 读写 ============================
def _read_journal() -> List[Dict]:
    if not os.path.exists(JOURNAL):
        return []
    out = []
    for ln in open(JOURNAL, encoding="utf-8"):
        try:
            d = json.loads(ln.strip())
            out.append(d)
        except Exception:
            pass
    return out


def has_outcome_event(event_id: str) -> bool:
    """该 id 是否已有「结算 outcome event」（幂等去重用）。

    ★ 2026-10-25 加：risk_guard SL 分支绕过 settle_all 直接平仓时，历史上只写了
    journal 字段、从不追加 outcome event → daily report 把 null net 当 0、
    dashboard 胜率漏统计。补齐时必须先查重，否则重复 backfill 会刷出多条同 id 事件。

    与 partial 事件的区分：partial 事件带 "kind"（如 ema_break_partial_close）且无
    "outcome"；结算事件带 "outcome"。这里只认带 outcome 的，避免把 EMA 减仓误判成
    「已结算」从而跳过真正的结算事件。
    """
    if not event_id:
        return False
    if not os.path.exists(EVENTS):
        return False
    try:
        for ln in open(EVENTS, encoding="utf-8"):
            ln = ln.strip()
            if not ln:
                continue
            try:
                d = json.loads(ln)
            except Exception:
                continue
            if d.get("id") == event_id and d.get("outcome") is not None:
                return True
    except Exception:
        return False
    return False


def _append_event(ev: Dict) -> None:
    # 2026-10-05 防污染：测试前缀（TEST- / TEST_*）的事件不写 events，
    # 避免演示性 force_close 或本地测试把数据混进 winrate / latest_5。
    # 2026-10-11 加：白名单收紧 → 只放 BTC_USDT- 前缀。其他前缀一律拦下。
    # 背景：今天 08:09 测试时随手造了 B-200-long 这种 ID，混进前端展示；
    #       防御未来任何"非生产格式"的事件污染实盘数据。
    _rid = ev.get("id") or ""
    if not _rid.startswith("BTC_USDT-"):
        return
    # ★ 2026-10-26 幂等：**完全相同**的 outcome 事件只写一次。
    #   背景：实测 10 个 id 有重复 outcome，其中 6 个是纯重复写（同 outcome + 同 net，
    #   只是 ts 差几秒——两个线程都跑了一遍 settle），按行累加的统计会重复计数。
    #   注意**不能**一刀切按 id 去重：另外 4 个是合法的状态修正
    #   （risk_guard 先写 SL_STOPPED，position_tracker 后结算成 BE_STOPPED / TP2_DONE /
    #    MCLOSE），语义是「最新覆盖」，必须保留。
    #   判据：同 id + 同 outcome + 同 net_usd → 纯重复，跳过；任一不同 → 当修正，写入。
    if ev.get("outcome") is not None:
        try:
            for ln in open(EVENTS, encoding="utf-8"):
                ln = ln.strip()
                if not ln:
                    continue
                try:
                    d = json.loads(ln)
                except Exception:
                    continue
                if (d.get("id") == _rid
                        and d.get("outcome") == ev.get("outcome")
                        and d.get("net_usd") == ev.get("net_usd")):
                    return   # 纯重复，不写
        except Exception:
            pass
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(EVENTS, "a", encoding="utf-8") as f:
        f.write(json.dumps(ev, ensure_ascii=False) + "\n")


def _write_journal(recs: List[Dict]) -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = JOURNAL + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        for r in recs:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    os.replace(tmp, JOURNAL)


# ============================ 核心：状态机回放 ============================
def _contracts(entry_price: float) -> int:
    """50U 名义 → 整数张数"""
    if entry_price <= 0:
        return 0
    return max(1, round(NOTIONAL_USD / (entry_price * QUANT_MULT)))


def _pnl_usd(side: str, entry: float, exit_px: float, contracts: int) -> float:
    """USD 损益（不含手续费）"""
    sign = 1 if side == "long" else -1
    return sign * (exit_px - entry) * contracts * QUANT_MULT


def _settle_one(sig: Dict, bars: List[List]) -> Optional[Dict]:
    """
    回放一根信号。bars 是 entry_ts 之后的 1m K 线。
    返回 None = 仍未结束。
    返回 dict = 已结束，含 outcome / net_usd / 类目。
    """
    side = sig["side"]
    entry = float(sig["entry"])
    sl = float(sig["sl"])
    tp1 = float(sig["tp1"])
    tp2 = float(sig.get("tp2") or 0)
    contracts = _contracts(entry)

    # 检查是否记录了当前状态（如有 TP1 已触发，BE 已移）
    #
    # ★ 2026-10-22 修复：回放必须按 K 线时间顺序判断状态，不能用「当前快照」。
    #   根因（实测 BTC_USDT-1792594815-long）：journal 里已写 tp1_hit=True /
    #   active_sl=84333（保本），而回放起点 ts=10-21 23:00 早于真实 TP1 触发
    #   （10-22 04:42）。老代码在循环外就把 cur_sl=保本 / cur_tp1_hit=True 读出来，
    #   于是 10-22 03:01 那根 low=84215 就被判 BE_STOPPED —— 比真实 TP1 早 101 分钟，
    #   触发 sanity check 抛错 → settler 永久卡死 → risk_guard 狂刷 26267 次。
    #
    #   正确语义：每根 K 线判断时，状态必须是「截至这根为止」的状态。
    #   实现：按 ts 比较 tp1_hit_ts / tp2_hit_ts，只有 ts >= 命中时刻才认为已触发。
    _tp1_hit_ts = int(sig.get("tp1_hit_ts") or 0)
    _tp2_hit_ts = int(sig.get("tp2_hit_ts") or 0)
    _active_sl_now = float(sig.get("active_sl") or sl)
    _sl_base = float(sig.get("sl") or sl)

    # 回放起点之前的「已成交」状态快照（用于填充阶段的判定）
    cur_sl = _sl_base
    cur_tp1_hit = bool(sig.get("tp1_hit")) and (_tp1_hit_ts == 0)
    cur_tp2_hit = bool(sig.get("tp2_hit")) and (_tp2_hit_ts == 0)
    cur_state = sig.get("pos_state", "OPEN")    # OPEN / TP1_PARTIAL

    pending_ts = int(sig["ts"])

    last_check_ts = int(sig.get("last_check_ts") or pending_ts)
    idx_start = 0

    # ★ 2026-10-24 性能修复：EMA 预警判定是**循环不变量**（只看 5m/15m 序列
    #   最后一根 close，与当前回放的 1m 根 ts 无关），所以整轮只算一次。
    #   原实现在循环里逐根调用 _check_ema_break_warning → 每次
    #   fetch_ohlcv(cache=True) 都整库写盘 2.5MB（≈0.87s）
    #   → 82 次写 = 71s，占 settle_all 单轮 60% 的耗时。
    #   语义等价性：原实现每次都取该周期「最新 200 根」，回放期间不会变
    #   （除非跨过了新 K 线收盘那一刻 —— 那种情况下新旧行为会有一根的差异，
    #     但那是「取数时机」差异，不是判定逻辑差异；且新版取数更早、更确定）。
    _ema_state = None
    _ema_state_ready = False

    for i, b in enumerate(bars):
        ts = int(b[0])
        if ts <= last_check_ts:
            continue
        h, l, c = float(b[2]), float(b[3]), float(b[4])

        # ★ 2026-10-22 修复：按「截至这根 K 线」恢复状态。
        #   老代码在循环外读一次 tp1_hit/active_sl 就用到底，等于用**未来状态**
        #   回放**过去**的 K 线。实测 BTC_USDT-1792594815-long：
        #   journal 记 tp1_hit_ts=04:42，但回放起点是 10-21 23:00，
        #   于是 03:01 那根 low=84215 就被判 BE_STOPPED（比真实 TP1 早 101 分钟），
        #   撞 sanity check 抛错 → settler 永久卡死。
        #   正确：只有 ts >= tp1_hit_ts 才认为 TP1 已发生、SL 已推到保本。
        if _tp1_hit_ts and ts >= _tp1_hit_ts and not cur_tp1_hit:
            cur_tp1_hit = True
            cur_sl = _active_sl_now
        if _tp2_hit_ts and ts >= _tp2_hit_ts and not cur_tp2_hit:
            cur_tp2_hit = True

        # 1) 24h 未挂到 → MISSED（仅未进场；已成交的持仓不受时间限制，靠趋势反转强平）
        # 2026-10-07 liusir 规则：未进场的挂单不返佣（标记作废），net_usd = 0
        # 2026-10-07 liusir 规则：TIMEOUT 24h 持仓强平已取消——持仓平仓仅靠趋势反转
        if not sig.get("filled") and ts - pending_ts > MAX_PEND:
            return {
                "pos_state": "MISSED", "outcome": "MISSED",
                "exit_ts": ts, "exit_px": round(c, 1),
                "net_usd": 0.0, "category": "skip",
                "note": f"24h 未成交"
            }

        # 2026-10-06 防御：成交当根不判 SL/TP/BE
        # 原因：成交判定本身用的是 low 触 entry，下一根才能开始判 SL/TP
        #       否则手动改 active_sl=entry 后回放会在成交当根立刻误触 BE_STOPPED
        _fill_ts = int(sig.get("fill_ts") or 0)
        if _fill_ts and ts <= _fill_ts:
            last_check_ts = ts
            continue

        # 2) 是否挂到单
        if not sig.get("filled", False):
            # 2026-10-06 liusir 规则：未进场的挂单，如果当前价已离 entry 太远
            #  → 标 MISSED（避免长期挂死单占 journal 空间）
            # 用 K 线 mid 估算当前价：(h+l)/2
            _cur_mid = (h + l) / 2.0
            _dist_pts = abs(_cur_mid - entry)
            _TOO_FAR_PTS = 800.0   # 同方向点位 > 800 点 → 直接 missed
            if _dist_pts > _TOO_FAR_PTS:
                _sig_meta = dict(sig)
                _sig_meta.update({
                    "status": "settled",
                    "pos_state": "MISSED",
                    "outcome": "MISSED",
                    "exit_ts": ts,
                    "exit_px": round(_cur_mid, 1),
                    "net_usd": 0.0,
                    "category": "skip",
                    "note": f"未进场：当前价距 entry {_dist_pts:.0f} 点 > {_TOO_FAR_PTS:.0f} 阈值",
                })
                last_check_ts = ts
                return _sig_meta
            # 2026-10-11 A2 守卫: 防止同一信号重复触发"假成交"
            #   痛点: 今天 10-11 的 6 条 15m_pattern 信号都是 entry 早就到过的，
            #   每 5s settler 回放一次 → 填一次 fill_ts，反复污染 journal。
            #   修复: entry 触发的假 fill 只允许一次；后续 K 线即使 hit 同一 entry 也跳过。
            #   区分: 已成交的 fill (实际进入 SL/TP 判定) 走原路径,
            #   未成交的 fill 走新分支 — 一旦 fill_ts 已存在, 不要再"假装重新成交"。
            hit = (l <= entry) if side == "long" else (h >= entry)
            if not hit:
                last_check_ts = ts
                continue
            # A2 守卫生效 — A1 已经把今天 6 条填过 fill 的状态改了,
            # 这里加一道确认: 若 sig 已存在 fill_ts (上次回放过), 不要重复触发
            if sig.get("fill_ts"):
                # 不变 last_check_ts, 让下一次 settle 重新判断（一般不会发生，下面防御）
                last_check_ts = ts
                continue
            sig["filled"] = True
            sig["fill_ts"] = ts
            sig["fill_px"] = round(entry, 1)
            sig["contracts"] = contracts
            # ★ 成交入场推送（让用户知道单子真进场了）
            _notify_fill(sig, sig["fill_px"], contracts, ts)
            # ★ 立即把 fill 状态持久化到 journal（避免下次扫描重复触发 fill 检测）
            try:
                _recs = _read_journal()
                _matched = False
                for _r in _recs:
                    if _r.get("id") == sig.get("id"):
                        _r.update({k: v for k, v in sig.items() if v not in (None, "")})
                        _matched = True
                        break
                if _matched:
                    _write_journal(_recs)
            except Exception as _e:
                pass

        # 3) 计算每根触发
        # ★ EMA 突破预警（2026-10-05 liusir 策略）：5m/15m EMA144+EMA169 同向突破
        #   → 已盈利的多/空单 → 70% 部分止盈 + SL 移到 BE
        #
        # ★ 2026-10-22 liusir 决策 B：EMA 预警触发的那根 K 线**不判止损**，
        #   从下一根开始判。理由与已有的 fill_ts 防御一致（line 179-185）——
        #   保本移动本身是靠「本根收盘价 close」判定的，而这根的影线 low 是
        #   更早形成的行情，两者时间顺序矛盾。用影线去判刚移动的保本线属于
        #   「同根双触自欺」：预警说趋势反转了，同一根又说保本已被击穿。
        #   实测代价（BTC_USDT-1792594815-long）：03:15 那根 EMA 预警推保本
        #   84333，同一根 low=84308.7 立刻止损出场 —— 比 TP1 路径的 07:05 早 3h50m，
        #   但这单本来浮盈只有 +29 点（0.07%），被一根影线扫掉不合理。
        _skip_sl_this_bar = False
        if sig.get("filled"):
            try:
                # ★ 2026-10-24：ema_state 懒加载一次，循环内复用
                if not _ema_state_ready:
                    _ema_state = _compute_ema_state(sig, side=side)
                    _ema_state_ready = True
                _ema_warned = _check_ema_break_warning(
                    sig, ts, side, entry, c, float(sig.get("fill_px") or entry),
                    ema_state=_ema_state)
                if _ema_warned:
                    # 本根已部分平仓 + 移 SL 到 BE
                    # 重新读 sig 的最新状态（被改了），继续走原 SL/TP 判定
                    cur_sl = float(sig.get("active_sl") or sl)
                    contracts = int(sig.get("contracts") or contracts)
                    _skip_sl_this_bar = True      # ← 决策 B：本根不判 SL
            except Exception as _e:
                if __debug__:
                    print(f"[ema_warn] 调用异常: {_e}")

        if side == "long":
            t_sl = l <= cur_sl
            t_tp1 = h >= tp1
            t_tp2 = tp2 > 0 and h >= tp2
        else:
            t_sl = h >= cur_sl
            t_tp1 = l <= tp1
            t_tp2 = tp2 > 0 and l <= tp2

        if _skip_sl_this_bar:
            t_sl = False

        # 4) 同根双触判定（防自欺）
        #    优先级：当前阶段最高目标 > 原 SL
        #    OPEN 阶段：未触发 TP1 时，TP/SL 同根 → SL
        #    TP1_PARTIAL 阶段：BE/TP2 同根 → TP2（已锁定 BE 兜底）
        if t_sl and t_tp1 and not cur_tp1_hit:
            return _close(sig, "SL", sl, ts, contracts, side, entry, "loss")
        if cur_tp1_hit and not cur_tp2_hit and t_sl and t_tp2:
            return _close(sig, "TP2", tp2, ts, contracts, side, entry, "profit")
        if cur_tp2_hit and t_sl and tp2 > 0:
            return _close(sig, "TP3", tp2, ts, contracts, side, entry, "profit")
        # 5) 单触判定
        #
        # ★ 2026-10-23 修复：BE vs SL 的判定只看 cur_tp1_hit，漏了保本移动的另外两个来源。
        #   保本移动有 3 个来源（见 _close 的 _be_move_ts 计算）：
        #     ① TP1 触发           → active_sl = entry
        #     ② EMA 突破预警       → active_sl = entry   (ema_warning_done=True)
        #     ③ 1h 浮亏主动减仓   → active_sl = entry   (partial_close_done=True)
        #   老代码 `BE_STOPPED if cur_tp1_hit else SL_STOPPED` 只认 ①，
        #   于是 ② 推的保本被标成 SL_STOPPED + category=loss —— 12 笔（10-05~10-08）
        #   全部误标，胜率被严重低估、形态家族统计失真。
        #
        #   正确口径：「保本移动过」= tp1_hit OR ema_warning_done OR partial_close_done；
        #   再用「出场价在 entry 附近」做二次确认（防 active_sl 被后续逻辑推离 entry）。
        #
        # ★★ 2026-10-24 修复（严重）：**`if t_sl:` 守卫被 10-23 那次改动静默删掉了**。
        #   后果：只要持仓已成交（filled=True），回放走到第一根 ts > fill_ts 的 K 线，
        #   就**无条件**执行 _close() —— 无论价格有没有摸到 SL/TP。
        #   现象（实测 2026-10-24 07:04 / 07:20 两笔 short）：
        #     · exit_px 恰好 == sl（81205.5 / 81222.9），而同期 1m 最高只有 80984
        #       → **价格从未到达止损位**，却被记成"止损扫掉"
        #     · exit_ts 恰好 == fill_ts + 60（成交后第一根 K 线）
        #     · TP1 / TP2 / PARTIAL_CLOSE 分支全部变成**死代码**（在 return 之后）
        #   根因：10-23 的改动把 `if t_sl:` + return 改写成裸 return，漏掉守卫。
        #   修复：把守卫加回来（BE 分类逻辑保留在守卫内部）。
        if t_sl:
            _be_moved = (cur_tp1_hit
                         or bool(sig.get("ema_warning_done"))
                         or bool(sig.get("partial_close_done")))
            _be_at_entry = abs(float(cur_sl) - float(entry)) < 1.0
            _is_be = _be_moved and _be_at_entry
            return _close(sig, "BE_STOPPED" if _is_be else "SL_STOPPED",
                          cur_sl, ts, contracts, side, entry,
                          "profit" if _is_be else "loss")
        # 2026-10-12 防御：重复 TP1 部分止盈去重（防止 cron 重跑时同一根 K 线二次触发）
        # 现象：journal 已写 tp1_hit_ts/active_sl=entry 后，下一次 settler 仍在循环开头
        #       读到 cur_tp1_hit=False（因为函数顶部快照早于本轮落盘），会再次走入 partial 分支
        #       → 推送重复 TP1 卡 + 重复落盘。
        # 关键：放在 t_tp2 检查之后，TP2_DONE 优先（同一根 K 线可能 high 同时 ≥ TP1 和 TP2）
        if t_tp2 and cur_tp1_hit and not cur_tp2_hit:
            sig["tp2_hit"] = True
            sig["tp2_hit_ts"] = ts
            sig["tp2_hit_px"] = tp2
            # TP2 触发后：剩 1/3 立即以 TP2 平仓（这是日内 3 段止盈的最后一段）
            # 已平 1/3 TP1 + 1/3 TP2 + 剩余 1/3 也在 TP2 出 = 全平
            return _close(sig, "TP2_DONE", tp2, ts, contracts, side, entry, "profit")

        # 已在 tp1_hit 且不是 TP2 路径再来的 t_tp1 → 跳过重复 partial 触发
        if t_tp1 and cur_tp1_hit:
            sig["last_check_ts"] = ts
            continue

        if t_tp1 and not cur_tp1_hit:
            # TP1 部分止盈：1/3 仓位锁利，止损移到 BE
            # 2026-10-05 修正：同步减少 sig["contracts"]，让前端和结算都按"已减 1/3"为准
            # 同时记录原始张数 _orig_contracts，让 _close() 能正确算部分止盈 PnL
            if not sig.get("_orig_contracts"):
                sig["_orig_contracts"] = contracts
            _partial_qty = max(1, int(contracts // PARTS))
            sig["contracts"] = int(contracts) - _partial_qty
            contracts = sig["contracts"]
            sig["tp1_hit"] = True
            sig["tp1_hit_ts"] = ts
            sig["tp1_hit_px"] = tp1
            sig["active_sl"] = entry            # 移到 BE
            sig["pos_state"] = "TP1_PARTIAL"
            sig["partial_exit_count"] = sig.get("partial_exit_count", 0) + 1
            sig["last_check_ts"] = ts
            # 推送部分止盈
            _notify_partial(sig, "TP1", tp1, contracts, ts)
            # ★ 立即把 TP1 状态持久化到 journal（避免下次 cron 重复触发）
            try:
                _recs = _read_journal()
                _matched = False
                for _r in _recs:
                    if _r.get("id") == sig.get("id"):
                        _r.update({k: v for k, v in sig.items() if v not in (None, "")})
                        _matched = True
                        break
                if _matched:
                    _write_journal(_recs)
            except Exception as _e:
                pass
            cur_tp1_hit = True
            cur_sl = entry
            continue
        # 2026-10-06 加：TP1 未触发 + 持仓 > 1h + 当前浮亏 → 主动减仓 50%
        # 触发条件（同时满足）：
        #   1) sig 已 fill（fill_ts 存在）
        #   2) 持仓时长 > PARTIAL_CLOSE_AFTER_SEC（默认 1h）
        #   3) TP1 未触发（cur_tp1_hit = False）
        #   4) 当前 close 在 entry 之下（long）/ 之上（short）= 浮亏
        # 动作：减仓 50%（PARTIAL_CLOSE_RATIO），SL 移到 BE，返回 PARTIAL_CLOSE 让结算按浮亏结算
        # ★ 注意：用 sig["contracts"]（实际剩余量）而不是 local contracts（settle_one 顶部
        #   从 entry 算出的理论量 = NOTIONAL/entry；用理论量减 50% 会算出错）
        if (sig.get("filled") and sig.get("fill_ts") and not cur_tp1_hit
                and not sig.get("partial_close_done")
                and (ts - int(sig["fill_ts"])) > PARTIAL_CLOSE_AFTER_SEC):
            _in_loss = ((side == "long" and c < entry)
                        or (side == "short" and c > entry))
            if _in_loss:
                _cur_contracts = int(sig.get("contracts") or 0)
                # 减仓 50%（最少减 1 张，避免 0）
                _close_qty = max(1, int(_cur_contracts * PARTIAL_CLOSE_RATIO))
                _remain_qty = max(1, _cur_contracts - _close_qty)
                # 记录原始量（_close 计算 PnL 时需要）
                if not sig.get("_orig_contracts"):
                    sig["_orig_contracts"] = _cur_contracts
                sig["contracts"] = _remain_qty
                contracts = _remain_qty     # 同步 local，避免后续逻辑误用旧值
                sig["partial_close_done"] = True
                sig["partial_close_ts"] = ts
                sig["partial_close_px"] = c
                # SL 移到 BE（保本退出规则：剩下 50% 不再亏）
                sig["active_sl"] = entry
                sig["pos_state"] = "PARTIAL_CLOSED"
                sig["partial_exit_count"] = sig.get("partial_exit_count", 0) + 1
                # 推送部分止盈（复用 _notify_partial，文案同 TP1 partial）
                _notify_partial(sig, "PARTIAL_CLOSE", c, _remain_qty, ts)
                # 立即持久化到 journal
                try:
                    _recs = _read_journal()
                    for _r in _recs:
                        if _r.get("id") == sig.get("id"):
                            _r.update({k: v for k, v in sig.items() if v not in (None, "")})
                            break
                    _write_journal(_recs)
                except Exception:
                    pass
                # 结算已平仓部分（按当前 close），剩余部分继续挂
                return _close(sig, "PARTIAL_CLOSE", c, ts, _remain_qty, side, entry, "loss")

        # 2026-10-07 liusir 规则：TIMEOUT 24h 强平已取消
        #   持仓平仓仅靠 watch/runner.py:_check_trend_reversal 检测趋势反转
        #   老 MAX_HOLD 常量保留为占位（避免破坏其它模块的 import），不再用于强平判定

        sig["last_check_ts"] = ts

    return None


def _close(sig: Dict, outcome: str, exit_px: float, exit_ts: int,
           contracts: int, side: str, entry: float, category: str) -> Dict:
    """
    计算最终损益。考虑部分止盈（已平仓部分按当时价格算，未平部分按当前 exit_px）。

    2026-10-05 修正：contracts 入参是 TP1 触发时的剩余仓位（已减 1/3）；
    原始总量用 sig._orig_contracts 字段（TP1 触发时记下），用它 // PARTS 算每段张数。

    2026-10-18 防御性 sanity check（liusir 10-18 发现 5/13 训练 journal 笔的
    exit_ts < fill_ts 60s 内，疑似 cache 滚动 + 早期路径污染）：
      - exit_ts 必须 > fill_ts，否则直接 raise 阻断污染 journal
      - TP1 hit 时 exit_ts 必须 > tp1_hit_ts（同根双触会优先 TP2，但 BE 必须晚于 TP1）
    """
    # 2026-10-18 sanity check
    _fill_ts = int(sig.get("fill_ts") or 0)
    if _fill_ts and exit_ts <= _fill_ts:
        raise ValueError(
            f"[position_tracker] _close sanity check FAILED: exit_ts={exit_ts} <= "
            f"fill_ts={_fill_ts} (id={sig.get('id', '?')}, outcome={outcome})。"
            f"这是 cache 滚动或早期路径污染的痕迹，宁可放弃 settle 也不要污染 journal。"
        )
    if sig.get("tp1_hit"):
        _tp1_hit_ts = int(sig.get("tp1_hit_ts") or 0)
        if _tp1_hit_ts and exit_ts < _tp1_hit_ts:
            # ★ 2026-10-22 修复：保本移动有三个来源，不止 TP1。
            #   ① TP1 触发         → tp1_hit_ts
            #   ② EMA 突破预警     → ema_warning_ts（2026-10-22 新增字段）
            #   ③ 1h 浮亏主动减仓   → partial_close_ts
            #   老代码只比 tp1_hit_ts，于是「EMA 预警推的保本」被误判成
            #   「BE 早于 TP1」→ 抛错 → settler 永久卡死
            #   （实测 BTC_USDT-1792594815-long：EMA 03:15 推保本、
            #     同根影线跌破 BE 止损，但 tp1_hit_ts 是 04:42 → 必抛错）。
            #   正确口径：BE 只需晚于「**最近一次保本移动**」。
            _be_move_ts = _tp1_hit_ts
            for _k in ("ema_warning_ts", "partial_close_ts"):
                _v = int(sig.get(_k) or 0)
                if _v > _be_move_ts:
                    _be_move_ts = _v
            if _be_move_ts and exit_ts < _be_move_ts:
                raise ValueError(
                    f"[position_tracker] _close sanity check FAILED: exit_ts={exit_ts} < "
                    f"保本移动时刻={_be_move_ts} (tp1_hit_ts={_tp1_hit_ts}, "
                    f"ema_warning_ts={sig.get('ema_warning_ts')}, "
                    f"id={sig.get('id', '?')}, outcome={outcome})。"
                    f"BE 必须晚于保本移动；这是状态时间戳被污染的痕迹。"
                )

    pnl_parts = []  # [(qty, exit_px)]
    contracts_orig = int(sig.get("_orig_contracts") or contracts)
    part_size = max(1, contracts_orig // PARTS)

    # 部分止盈：TP1 / TP2 已平仓的部分
    if sig.get("tp1_hit"):
        pnl_parts.append((part_size, sig["tp1_hit_px"]))
    if sig.get("tp2_hit"):
        pnl_parts.append((part_size, sig["tp2_hit_px"]))
    # EMA 预警分支（2026-10-07 修复）：70% 部分平仓的张数和价格需要计入 PnL
    # 预警分支不写 tp1_hit/_orig_contracts（已有此修复），所以这里要单独走一遍
    if sig.get("ema_warning_done") and sig.get("ema_partial_qty"):
        pnl_parts.append((int(sig["ema_partial_qty"]), float(sig["ema_partial_px"])))

    # 剩余部分按当前 exit_px 平
    closed_qty = sum(q for q, _ in pnl_parts)
    remain = contracts_orig - closed_qty
    if remain > 0:
        pnl_parts.append((remain, exit_px))

    gross = sum(_pnl_usd(side, entry, px, q) for q, px in pnl_parts)
    net = gross - FEE_USD

    # 2026-10-11 v5 风控（非调参）：单笔亏损深度安全阀
    # 背景: 历史数据最大单笔亏损 -44.85U，接近 50U 本金。无任何风控机制
    # 触发条件: net < -30U（含手续费后）→ 标 safety_break=true + 推送告警
    # 这是「防极端亏损」不是「参数调优」——不改 score/SL/TP 等决策参数
    safety_break = net < -30.0

    return {
        "pos_state": outcome,
        "outcome": outcome,
        "exit_ts": exit_ts,
        "exit_px": round(exit_px, 1),
        "net_usd": round(net, 2),
        "gross_usd": round(gross, 2),
        "category": category,
        "partial_exits": len(pnl_parts),
        "contracts": contracts,
        # 2026-10-11 风控: 标记极端亏损 + 记录深度
        "safety_break": safety_break,
        "loss_depth": abs(min(net, 0.0)) if net < 0 else 0.0,
        "note": ""
    }


# ============================ Telegram 推送 ============================
def _notify_fill(sig: Dict, fill_px: float, contracts: int, ts: int) -> None:
    """成交入场推送（limit 单被填时通知一次）"""
    entry = float(sig['entry'])
    side_cn = '做多' if sig['side'] == 'long' else '做空'
    tp1 = float(sig.get('tp1', entry + 100))
    tp2 = float(sig.get('tp2', entry + 200))
    sl  = float(sig.get('sl', entry * 0.992))
    card = (
        f"🎯 <b>BTC 已成交入场</b>\n"
        f"{side_cn} {contracts} 张 @ <b>{fill_px:,.1f}</b>\n"
        f"TP1 <b>{tp1:,.1f}</b> ｜ TP2 <b>{tp2:,.1f}</b> ｜ SL <b>{sl:,.1f}</b>\n"
        f"等待 TP1 / TP2 触发，或 SL 出局"
    )
    _push(card)


def _notify_partial(sig: Dict, label: str, px: float, contracts: int, ts: int) -> None:
    """TP1/TP2 部分止盈推送"""
    partial_qty = contracts // PARTS
    entry = float(sig["entry"])
    side_cn = "做多" if sig["side"] == "long" else "做空"
    sign = 1 if sig["side"] == "long" else -1
    pnl = sign * (px - entry) * partial_qty * QUANT_MULT
    new_sl = entry if label == "TP1" else px
    sl_cn = "BE (开仓价)" if label == "TP1" else f"TP1 {sig['tp1']:,.0f}"

    card = (
        f"🟢 <b>BTC 部分止盈 {label}</b>\n"
        f"{side_cn} {contracts} 张入场 @ <b>{entry:,.1f}</b>\n"
        f"{label} 触发 @ <b>{px:,.1f}</b> ｜ 本段 1/{PARTS} 仓位（{partial_qty} 张）锁利 ≈ <b>{pnl:+.2f}U</b>\n"
        f"止损上移到 <b>{sl_cn}</b>\n"
        f"持仓继续监控，按 TP2/TP3 推进"
    )
    _push(card)


def _notify_close(sig: Dict, res: Dict) -> None:
    """平仓推送"""
    entry = float(sig["entry"])
    side_cn = "做多" if sig["side"] == "long" else "做空"
    cat = res["category"]
    net = res["net_usd"]

    # 2026-10-12 防御：噪音门——BE 单触结算的"零位移"卡片
    # 现象：今晚 22:55 那次"+3.15U TP1 后保本被打"卡片——exit_px=entry、净利仅 3.15U。
    #       根因：active_sl=entry 时单根 K 线影线下探 → 立刻被判定 BE_STOPPED。
    #       这种"价格根本没动就被平"的卡片挤占 TG 无任何决策价值。
    # 改：outcome=BE_STOPPED 且 exit_px 距 entry < 5 点 → 视为零位移噪音，跳过推送。
    #     仅 stderr 警告 + journal audit（你仍能在 report 里看到这些结算）。
    _is_noise_be = False
    if res.get("outcome") == "BE_STOPPED":
        _exit_px = float(res.get("exit_px", 0))
        if abs(_exit_px - entry) < 5.0:
            _is_noise_be = True
    if _is_noise_be:
        import sys as _sys
        print(
            f"[position_tracker] 跳过噪音 BE 卡片 id={sig.get('id')} "
            f"entry={entry:.1f} exit={res.get('exit_px'):.1f} "
            f"net={net:+.2f}U outcome={res.get('outcome')}",
            file=_sys.stderr,
        )
        return

    icon = {"profit": "🟢", "loss": "🔴", "timeout": "⏰", "skip": "⚪"}.get(cat, "•")
    head = {
        "TP1_HIT": "TP1 触发 + 保本被打",
        "TP2_HIT": "TP2 触发 + 保本被打",
        "TP3_HIT": "全部止盈 ✓",
        "TP_BE":    "TP1 + TP2 后 BE 打回",
        "BE_STOPPED": "TP1 后保本被打",
        "SL_STOPPED": "止损扫掉",
        "TIMEOUT": "超时未触发",
        "MISSED":  "24h 未成交"
    }.get(res["outcome"], res["outcome"])

    card = (
        f"{icon} <b>BTC 平仓 · {head}</b>\n"
        f"{side_cn} {res.get('contracts', '?')} 张 @ {entry:,.1f}\n"
        f"出场 @ <b>{res['exit_px']:,.1f}</b> ｜ 净利 <b>{net:+.2f}U</b>（含 4.85U 手续费）\n"
        f"类目：{cat} ｜ 部分止盈 {res.get('partial_exits', 0)} 次"
    )
    _push(card)

    # 2026-10-11 风控安全阀触发告警（不改参数，只加风控机制）
    if res.get("safety_break"):
        depth = res.get("loss_depth", abs(net))
        alert = (
            f"🚨 <b>单笔极端亏损 · 安全阀触发</b>\n"
            f"亏损深度 <b>{depth:.2f}U</b>（超过 -30U 阈值）\n"
            f"持仓：{side_cn} @ {entry:,.1f} → {res['exit_px']:,.1f}\n"
            f"净利 {net:+.2f}U ｜ 类目 {cat}\n"
            f"\n"
            f"⚠️ <b>建议</b>：\n"
            f"  1. 检查当前策略参数（ATR SL 是否太宽）\n"
            f"  2. 单笔超过 -30U 后暂停开新仓，等 v5 验证后再重启\n"
            f"  3. 账户级风控：累计 -50U 时强制全停"
        )
        _push(alert)


def _push(text: str) -> None:
    """调 telegram.push（独立模块，便于测试时 monkey-patch）"""
    tok = load_token()
    chat = load_chat()
    if tok:
        push_telegram(text, tok, chat)


# ============================ 5m/15m EMA 突破预警 ============================
# 2026-10-05 liusir 策略：
#   上升趋势中（多单盈利），如果 5m close < EMA144 且 < EMA169，
#   并且 15m close 也 < EMA144 + EMA169 → 趋势反转预警
#   → 多单 70% 部分止盈 + SL 移到 BE
# 做空镜像对称。
EMA_WARN_PARTIAL = 0.7     # 部分止盈 70%


def _compute_ema_state(sig: Dict, side: str | None = None) -> Optional[Dict]:
    """
    ★ 2026-10-24 性能修复：一次性取 5m/15m K 线并算出 EMA 判定。
      供 _settle_one 的回放循环复用（循环内不再逐根取数/写盘）。

    返回 dict:
      {"below_5m": bool, "below_15m": bool,     ← 按 side 方向判定
       "close_5m", "e144_5m", "e169_5m",
       "close_15m", "e144_15m", "e169_15m"}
    返回 None = 数据不足（K 线不够 / 取数失败）。

    ⚠ 方向语义（与原实现严格一致）：
       做多：close < EMA144 且 close < EMA169 → 跌破
       做空：close > EMA144 且 close > EMA169 → 升破
    below_* 已按 side 取好方向，调用方直接 AND 即可。
    """
    side = side or sig.get("side") or "long"
    try:
        bars_5m = fetch_ohlcv(sig.get("contract", "BTC_USDT"), "5m",
                                200, cache=True)
        bars_15m = fetch_ohlcv(sig.get("contract", "BTC_USDT"), "15m",
                                200, cache=True)
    except Exception as e:
        if __debug__:
            print(f"[ema_warn] K 线获取失败: {e}")
        return None

    def _below(side, close, e144, e169):
        if side == "long":
            return close < e144 and close < e169
        return close > e144 and close > e169

    def _one(bars):
        if not bars:
            return None
        closes = [float(r[4]) for r in bars]
        if len(closes) < 170:
            return None
        e144 = float(_ema_func(np.array(closes), 144)[-1])
        e169 = float(_ema_func(np.array(closes), 169)[-1])
        close = closes[-1]
        return {"close": close, "e144": e144, "e169": e169}

    m5 = _one(bars_5m)
    if not m5:
        return None
    m15 = _one(bars_15m)
    if not m15:
        return None
    return {
        "below_5m": _below(side, m5["close"], m5["e144"], m5["e169"]),
        "below_15m": _below(side, m15["close"], m15["e144"], m15["e169"]),
        "close_5m": m5["close"], "e144_5m": m5["e144"], "e169_5m": m5["e169"],
        "close_15m": m15["close"], "e144_15m": m15["e144"], "e169_15m": m15["e169"],
    }


def _check_ema_break_warning(sig: Dict, ts: int, side: str, entry: float,
                              cur_px: float, fill_px: float,
                              ema_state: Optional[Dict] = None) -> bool:
    """
    EMA 突破预警检测。
    触发条件（同时满足）：
      ① 当前持仓盈利（cur_px > fill_px 做多；cur_px < fill_px 做空）
      ② 5m close < EMA144 且 < EMA169 (做多) / 反之做空
      ③ 15m close < EMA144 且 < EMA169 (做多) / 反之做空
      ④ 该预警在本笔持仓上**未触发过**（用 ema_warning_done 标记）

    触发后：部分平仓 70%、SL 移到 BE、写 partial_exit_count+1、推送。
    返回 True 表示本根已处理过预警（让调用方跳过 SL/TP 判定避免重复触发）。

    ★ 2026-10-24 性能修复：新增 ema_state 参数（预计算的 5m/15m EMA 判定）。
      根因：原实现在**回放循环里逐根 1m** 调用本函数，每次都
            fetch_ohlcv(cache=True) 拉 5m/15m → 每次整库写盘 2.5MB（≈0.87s）
            → 82 次 = 71s，占 settle_all 单轮耗时的 60%。
      关键洞察：**EMA 判定是循环不变量** —— 它只看 5m/15m 序列的最后一根
            close 和 EMA144/169，与当前回放的 1m 根 ts 无关。
            （fetch_ohlcv 返回的是该周期最新 200 根，不随 ts 变化。）
      所以：整轮回放只需取一次 K 线、算一次 EMA，循环内只做「盈利判断 + 比对」。
      为保守起见，ema_state=None 时保持原行为（自己取数自己算），
      单测（tests/test_position_tracker.py 直接调本函数）不受影响。
    """
    if sig.get("ema_warning_done"):
        return False
    # 必须已成交
    if not sig.get("filled"):
        return False
    # 必须盈利
    if side == "long" and cur_px <= fill_px:
        return False
    if side == "short" and cur_px >= fill_px:
        return False

    if ema_state is None:
        ema_state = _compute_ema_state(sig)
    if not ema_state:
        return False
    # ema_state 里的 below_5m / below_15m 已按 side 算好方向
    if not (ema_state["below_5m"] and ema_state["below_15m"]):
        return False
    close_5m = ema_state["close_5m"]
    e144_5m = ema_state["e144_5m"]
    e169_5m = ema_state["e169_5m"]
    close_15m = ema_state["close_15m"]
    e144_15m = ema_state["e144_15m"]
    e169_15m = ema_state["e169_15m"]

    # ★ 触发！70% 部分平仓 + SL 移到 BE
    sig["ema_warning_done"] = True
    sig["ema_warning_ts"] = ts
    sig["ema_warning_px"] = cur_px
    contracts_total = int(sig.get("contracts") or _contracts(entry))
    partial = int(contracts_total * EMA_WARN_PARTIAL)
    if partial < 1:
        return False

    # 部分平仓的 PnL（同模块函数，直接用）
    partial_pnl_gross = _pnl_usd(side, entry, cur_px, partial)
    # 单笔成本 FEE_USD 全额（与 _close 一致：4.85U 整笔摊到本次部分平仓）
    # 但其实更合理是按比例分摊——这里保守按整笔扣一次（与 _close 一致）
    # 简化：整笔 FEE_USD 直接扣掉——多次部分平仓也只扣一次完整手续费
    # （因为仓位没全平时不算「出场」）
    partial_net = partial_pnl_gross   # 不扣手续费，等全平时一次性扣

    # 更新 sig：减少剩余张数 + 移 SL 到 BE + 部分平仓计数
    sig["contracts"] = contracts_total - partial
    sig["active_sl"] = entry    # BE = 开仓价
    sig["partial_exit_count"] = sig.get("partial_exit_count", 0) + 1
    sig["partial_exits"] = sig.get("partial_exits", 0) + 1
    sig["partial_net_usd"] = round(partial_net, 2)
    # ★ 2026-10-22 新增：记录 EMA 预警的**触发时刻**。
    #   缺这个字段 → 回放时无法知道「保本是什么时候移动的」，
    #   只能用 tp1_hit_ts 代替 → sanity check 误判「BE 早于 TP1」抛错
    #   → settler 永久卡死（实测 BTC_USDT-1792594815-long 卡了 9.6h）。
    sig["ema_warning_ts"] = ts
    sig["ema_warning_px"] = round(float(cur_px), 1)
    # 记录预警已平张数 + 触发价（让 _close 重算 PnL 时能正确把 EMA 部分计入）
    sig["ema_partial_qty"] = partial
    sig["ema_partial_px"] = cur_px
    # ★ 关键：和 TP1 / PARTIAL_CLOSE 分支保持一致——首次部分平仓时记录原始总量
    #   否则 _close() 用 sig["contracts"]（已被减）当 base，后续 PnL 算错
    if not sig.get("_orig_contracts"):
        sig["_orig_contracts"] = contracts_total
    sig["last_check_ts"] = ts

    # 写 events + 推送
    _append_event({
        "id": sig.get("id"),
        "kind": "ema_break_partial_close",
        "partial_contracts": partial,
        "exit_px": cur_px,
        "ts": ts,
        "side": side,
        "reason": "5m/15m EMA144+EMA169 突破预警",
        "ema_close_5m": round(close_5m, 2),
        "ema_e144_5m": round(float(e144_5m), 2),
        "ema_e169_5m": round(float(e169_5m), 2),
        "ema_close_15m": round(close_15m, 2),
        "ema_e144_15m": round(float(e144_15m), 2),
        "ema_e169_15m": round(float(e169_15m), 2),
        "partial_net_usd": round(partial_net, 2),
    })
    _notify_ema_warning(sig, partial, cur_px, side, entry, partial_net,
                        close_5m, e144_5m, e169_5m, close_15m, e144_15m, e169_15m)

    # 持久化
    try:
        _recs = _read_journal()
        for _r in _recs:
            if _r.get("id") == sig.get("id"):
                _r.update({k: v for k, v in sig.items() if v not in (None, "")})
                break
        _write_journal(_recs)
    except Exception:
        pass
    return True


def _notify_ema_warning(sig: Dict, partial: int, px: float, side: str,
                        entry: float, partial_net: float,
                        close_5m: float, e144_5m: float, e169_5m: float,
                        close_15m: float, e144_15m: float, e169_15m: float) -> None:
    """EMA 突破预警推送：带 70% 部分平仓 + SL 移到 BE"""
    side_cn = "做多" if side == "long" else "做空"
    # 趋势方向描述：long 双跌破 = 上升结构变弱；short 双涨破 = 下降结构变弱
    structure_word = "上升结构变弱" if side == "long" else "下降结构变弱"
    break_word = "双跌破" if side == "long" else "双涨破"
    icon = "🟡"
    card = (
        f"{icon} <b>BTC 趋势反转预警 · 部分止盈 70%</b>\n"
        f"{side_cn} @ <b>{entry:,.1f}</b> 当前 <b>{px:,.1f}</b>\n"
        f"5m close <b>{close_5m:,.1f}</b> vs EMA144 <b>{e144_5m:,.1f}</b> / "
        f"EMA169 <b>{e169_5m:,.1f}</b>\n"
        f"15m close <b>{close_15m:,.1f}</b> vs EMA144 <b>{e144_15m:,.1f}</b> / "
        f"EMA169 <b>{e169_15m:,.1f}</b>\n"
        f"\n已平 <b>{partial}</b> 张，剩余 <b>{sig.get('contracts', partial)}</b> 张\n"
        f"本笔盈利（未扣费）<b>{partial_net:+.2f}U</b> ｜ SL 已移到 BE（开仓价）\n"
        f"\n⚠️ 5m + 15m {break_word} EMA144/169 → {structure_word}；保留 30% 跑趋势反转\n"
        f"出场后会按走完整价 + 4.85U 手续费最终结算"
    )
    _push(card)


# ============================ 主流程 ============================
def force_close_position(
    exit_px: float,
    reason: str = "manual",
    signal_id: Optional[str] = None,
    use_latest_filled: bool = True,
    verbose: bool = False,
) -> Optional[Dict]:
    """
    模拟平仓（不入真单）：立即把指定 filled journal 条标记为 settled。

    设计目的：
      JumpTracker 反方向信号触发时，自动调此函数平掉已进场单子（不调交易所 API）。
      适用于"做多时突然出现做空信号"——立即平多，避免反方向风险敞口。

    参数：
      exit_px: 平仓价（用市价/触发价），用于计算 PnL
      reason: 平仓原因（写入 journal note + 推送卡片）
      signal_id: 指定 journal id；为 None 时取 latest filled
      use_latest_filled: 如果 signal_id 没找到，是否回退到 latest filled
      verbose: 是否打印过程日志

    返回：
      平仓结果 dict（含 outcome/net_usd/contracts 等），或 None（未找到 filled 单）
    """
    recs = _read_journal()
    target = None

    # 1. 优先按 signal_id 精确匹配
    if signal_id is not None:
        for r in recs:
            # position_tracker 的语义：status="pending" + filled=True 表示「已成交未平仓」
            # 2026-10-11 加：跳过 preview_pending（预览闸门还没开，不应被强平）
            if r.get("id") == signal_id and r.get("status") == "pending" and r.get("filled") is True:
                target = r
                break

    # 2. 回退：找最近一条已成交（不论方向）
    if target is None and use_latest_filled:
        for r in reversed(recs):
            # 同样跳过 preview_pending
            if r.get("status") == "preview_pending":
                continue
            if r.get("status") == "pending" and r.get("filled") is True:
                target = r
                break

    # 2026-10-11 加：如果调用方明确传了一个 ID，但该 ID 处于 preview_pending
    # （还没 promote，意味着下单指令还没真正发出），不允许回退去平别的单。
    # 这是防御：避免外部脚本误把"想取消的预览"当成"想平的持仓"处理。
    if signal_id is not None and use_latest_filled and target is not None:
        # 检查原 signal_id 是不是 preview_pending / cancelled
        _src = next((r for r in recs if r.get("id") == signal_id), None)
        if _src and _src.get("status") in ("preview_pending", "cancelled"):
            if verbose:
                print(f"[force_close] signal_id={signal_id} 是 {_src['status']}，"
                      f"不允许回退平仓（target={target.get('id')}）")
            target = None

    if target is None:
        if verbose:
            print(f"[force_close] 无可平仓的 filled 单（signal_id={signal_id}）")
        return None

    # 3. 计算 PnL（用现有 _close，outcome=MCLOSE）
    entry = float(target.get("entry") or target.get("fill_px", 0))
    side = target.get("side", "long")
    contracts = int(target.get("contracts") or target.get("_contracts", 1))
    ts = int(time.time())

    # _close 内部根据 net_usd 自动判 category（profit/loss）
    res = _close(
        target, "MCLOSE", exit_px, ts,
        contracts, side, entry, "profit"   # 占位，_close 内部重算
    )

    # 4. 标记 settled + 写 reason
    target.update(res)
    target["status"] = "settled"
    target["note"] = reason

    # 5. 写回 journal + 事件流 + 推送
    _write_journal(recs)
    _append_event({
        "id": target.get("id"),
        "outcome": "MCLOSE",
        "net_usd": res["net_usd"],
        "category": res["category"],
        "exit_ts": ts,
        "exit_px": exit_px,
        "reason": reason,
        "ts": ts,
    })
    _notify_close(target, res)

    if verbose:
        print(f"[force_close] 平仓 id={target.get('id')} side={side} entry={entry:.1f} exit={exit_px:.1f} net={res['net_usd']:+.2f}U reason={reason}")

    return {**target, **res}


def settle_all(verbose: bool = False) -> Tuple[int, int]:
    """
    扫描所有 status='pending' 的信号，逐一回放。
    2026-10-11 注：preview_pending 不在此过滤内（预览期内不让结算回放管）。
    返回 (已结算条数, 待结算条数)
    """
    recs = _read_journal()
    pend = [r for r in recs if r.get("status") == "pending"]
    if not pend:
        return (0, 0)

    try:
        bars = fetch_ohlcv("BTC_USDT", "1m", 8000, cache=True)
        bars = sorted(bars, key=lambda b: b[0])
    except Exception as e:
        if verbose:
            print(f"[position_tracker] K 线获取失败: {e}")
        return (0, len(pend))

    n_done = 0
    n_still = 0
    n_error = 0
    _err_ids: list = []
    # 2026-10-12 修 #3：journal 双胞胎去重（按 entry+exit+outcome+side 聚类）
    #   痛点：23:57:20 + 23:57:32 双胞胎卡片（1791692114 + 1791695718 同 entry/exit/net）
    #   根因：journal 双胞胎两条都 pending → 各自 _settle_one → 各 _notify_close 推卡
    #   修 #1 已堵住新双胞胎产生；此补丁对历史已落盘双胞胎做兜底
    #   规则：相同 entry+exit_px+outcome+side 视为双胞胎 → 保留 id 最长的（带 -JUMP 后缀短）
    from collections import defaultdict
    _dedup_groups: dict = defaultdict(list)
    for _r in recs:
        if _r.get("status") != "pending":
            continue
        _key = (
            _r.get("entry"), _r.get("exit_px"), _r.get("outcome"), _r.get("side"),
            _r.get("fill_px"), _r.get("exit_ts"),
        )
        _dedup_groups[_key].append(_r)
    _skipped_ids: set = set()
    for _key, _group in _dedup_groups.items():
        if len(_group) > 1:
            # 保留 id 最短的（无 -JUMP 后缀为主单）
            _group_sorted = sorted(_group, key=lambda x: (len(x.get("id") or ""), x.get("id") or ""))
            for _dup in _group_sorted[1:]:
                _skipped_ids.add(_dup.get("id"))
    _seen_ids: set = set()
    for r in recs:
        if r.get("status") != "pending":
            continue
        rid = r.get("id")
        if rid in _skipped_ids or rid in _seen_ids:
            n_still += 1
            if rid in _skipped_ids:
                r["status"] = "settled"
                r["note"] = (r.get("note") or "") + " | dedup:skipped (双胞胎已由主单结算)"
            continue
        try:
            res = _settle_one(r, bars)
        except Exception as e:
            # ★ 2026-10-22 修复：原代码 `except: continue` 把异常静默吞掉，
            #   导致这一笔永远 pending —— 实测 BTC_USDT-1792594815-long 因为
            #   sanity check 误判（exit_ts < tp1_hit_ts）每次都抛 ValueError，
            #   settler 每 5s 撞一次、每次都跳过，单笔卡死 9.6 小时、浮亏 -138.91U。
            #   现在：① 记 error 字段留证据 ② 记日志 ③ 计数上报（不再静默）。
            r["settle_error"] = str(e)[:300]
            r["settle_error_ts"] = int(time.time())
            r["settle_error_count"] = int(r.get("settle_error_count") or 0) + 1
            n_error += 1
            if verbose:
                print(f"[position_tracker] 结算 {r.get('id', '?')} 异常: {e}")
            _err_ids.append(r.get("id"))
            continue

        if res:
            r.update(res)
            r["status"] = "settled"
            # 2026-10-18 修复：hold_h 必须由 settle 强制按真实 (exit_ts - fill_ts) 重算，
            # 不能用 journal 里写死的字段——之前 8/13 笔 hold_h 跟实际差几小时
            # （因为 sig.get("hold_h") 是早期某个错误写入的，不是 _close 算出来的）
            _ft = int(r.get("fill_ts") or 0)
            _et = int(r.get("exit_ts") or 0)
            if _ft and _et and _et > _ft:
                r["hold_h"] = round((_et - _ft) / 3600, 1)
            n_done += 1
            _append_event({"id": r.get("id"), "outcome": res["outcome"],
                           "net_usd": res["net_usd"], "category": res["category"],
                           "exit_ts": res["exit_ts"], "ts": int(time.time())})
            _notify_close(r, res)
            _seen_ids.add(rid)
        else:
            n_still += 1

    if n_done or n_error:
        _write_journal(recs)
        if verbose:
            print(f"[position_tracker] 结算 {n_done} 条｜ 仍 pending {n_still} 条")
    if n_error:
        # ★ 2026-10-22：错误必须显式上报，不能只写进 journal 就完事——
        #   实测卡死案例里 journal 写了 error 但没人看，等于还是静默失败。
        #   走 stderr（watch.log 会收）+ 写独立文件，便于事后 grep。
        _err_msg = (f"[position_tracker] ⚠️ {n_error} 笔结算异常（已卡 pending）："
                    f"{_err_ids}")
        try:
            import sys as _sys
            print(_err_msg, file=_sys.stderr)
        except Exception:
            pass
        try:
            with open("/root/data/settle_errors.log", "a") as _ef:
                _ef.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {_err_msg}\n")
        except Exception:
            pass
    return (n_done, n_still)


def report() -> str:
    """绩效报告"""
    recs = _read_journal()
    settled = [r for r in recs if r.get("status") == "settled"]
    if not settled:
        return "尚无已结算持仓。"

    lines = [f"\n{'='*74}", "  持仓模拟绩效（50U 名义 × 100x 杠杆 + 4.85U 手续费）",
             f"{'='*74}"]
    lines.append(f"  结算 {len(settled)} 条")

    for r in settled:
        lines.append(f"\n  [{r.get('id', '?')[:25]}] {r.get('side','?')} @ {r.get('entry',0):,.1f}"
                     f"  →  {r.get('outcome','?')} @ {r.get('exit_px',0):,.1f}")
        lines.append(f"    净利 {r.get('net_usd', 0):+.2f}U ｜ 类目 {r.get('category','?')}"
                     f" ｜ 部分止盈 {r.get('partial_exits', 0)} 次")

    total = sum(r.get("net_usd", 0) for r in settled)
    lines.append(f"\n  累计净利润：<b>{total:+.2f}U</b>")
    lines.append(f"{'='*74}\n")
    return "\n".join(lines)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--settle", action="store_true", help="扫描并结算所有 pending")
    ap.add_argument("--report", action="store_true", help="打印绩效报告")
    args = ap.parse_args()

    if args.report:
        print(report())
    if args.settle:
        n, _ = settle_all(verbose=True)
        print(f"[done] 结算 {n} 条")
    if not (args.report or args.settle):
        ap.print_help()
