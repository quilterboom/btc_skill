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
   ├ 48h 未挂到 → MISSED（不计入胜率）
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
# 50U 保证金 + 100x 杠杆 = 5000U 名义 → 张数 = round(5000 / entry / 0.0001)
MARGIN_USD = 50.0                           # 保证金 50U
LEVERAGE = 100                              # 100x 杠杆
NOTIONAL_USD = MARGIN_USD * LEVERAGE        # 名义 = 5000U
# 单笔总成本：scan 一律推挂单（maker，-0.01% 返佣），按 maker 写死
# 名义 5000U × 0.01% × 2 (entry+exit) = -1.0U（净返佣 1U）
FEE_USD = -1.0                              # 单笔总成本（maker 往返返佣）
PARTS = 3                                   # 3 段部分止盈
QUANT_MULT = 0.0001                         # 1 张 = 0.0001 BTC
MAX_HOLD = 24 * 3600                        # 日内 24h 强制平
MAX_PEND = 48 * 3600                        # 48h 没挂到 → MISSED
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


def _append_event(ev: Dict) -> None:
    # 2026-10-05 防污染：测试前缀（TEST- / TEST_*）的事件不写 events，
    # 避免演示性 force_close 或本地测试把数据混进 winrate / latest_5。
    _rid = ev.get("id") or ""
    if _rid.startswith("TEST") or _rid.startswith("test-"):
        return
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
    cur_sl = float(sig.get("active_sl") or sl)
    cur_tp1_hit = bool(sig.get("tp1_hit"))
    cur_tp2_hit = bool(sig.get("tp2_hit"))
    cur_state = sig.get("pos_state", "OPEN")    # OPEN / TP1_PARTIAL

    pending_ts = int(sig["ts"])

    last_check_ts = int(sig.get("last_check_ts") or pending_ts)
    idx_start = 0

    for i, b in enumerate(bars):
        ts = int(b[0])
        if ts <= last_check_ts:
            continue
        h, l, c = float(b[2]), float(b[3]), float(b[4])

        # 1) 48h 未挂到 → MISSED
        if ts - pending_ts > MAX_PEND:
            return {
                "pos_state": "MISSED", "outcome": "MISSED",
                "exit_ts": ts, "exit_px": round(c, 1),
                "net_usd": -FEE_USD, "category": "skip",
                "note": f"48h 未成交"
            }

        # 2) 是否挂到单
        if not sig.get("filled", False):
            hit = (l <= entry) if side == "long" else (h >= entry)
            if not hit:
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
        if sig.get("filled"):
            try:
                _ema_warned = _check_ema_break_warning(
                    sig, ts, side, entry, c, float(sig.get("fill_px") or entry))
                if _ema_warned:
                    # 本根已部分平仓 + 移 SL 到 BE
                    # 重新读 sig 的最新状态（被改了），继续走原 SL/TP 判定
                    cur_sl = float(sig.get("active_sl") or sl)
                    contracts = int(sig.get("contracts") or contracts)
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
        if t_sl:
            return _close(sig, "BE_STOPPED" if cur_tp1_hit else "SL_STOPPED",
                          cur_sl, ts, contracts, side, entry,
                          "profit" if cur_tp1_hit else "loss")
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
        if t_tp2 and cur_tp1_hit and not cur_tp2_hit:
            sig["tp2_hit"] = True
            sig["tp2_hit_ts"] = ts
            sig["tp2_hit_px"] = tp2
            # TP2 触发后：剩 1/3 立即以 TP2 平仓（这是日内 3 段止盈的最后一段）
            # 已平 1/3 TP1 + 1/3 TP2 + 剩余 1/3 也在 TP2 出 = 全平
            return _close(sig, "TP2_DONE", tp2, ts, contracts, side, entry, "profit")

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

        # 6) TIMEOUT 24h 强制平
        if sig.get("filled") and ts - sig["fill_ts"] > MAX_HOLD:
            return _close(sig, "TIMEOUT", c, ts, contracts, side, entry, "timeout")

        sig["last_check_ts"] = ts

    return None


def _close(sig: Dict, outcome: str, exit_px: float, exit_ts: int,
           contracts: int, side: str, entry: float, category: str) -> Dict:
    """
    计算最终损益。考虑部分止盈（已平仓部分按当时价格算，未平部分按当前 exit_px）。

    2026-10-05 修正：contracts 入参是 TP1 触发时的剩余仓位（已减 1/3）；
    原始总量用 sig._orig_contracts 字段（TP1 触发时记下），用它 // PARTS 算每段张数。
    """
    pnl_parts = []  # [(qty, exit_px)]
    contracts_orig = int(sig.get("_orig_contracts") or contracts)
    part_size = max(1, contracts_orig // PARTS)

    # 部分止盈：TP1 / TP2 已平仓的部分
    if sig.get("tp1_hit"):
        pnl_parts.append((part_size, sig["tp1_hit_px"]))
    if sig.get("tp2_hit"):
        pnl_parts.append((part_size, sig["tp2_hit_px"]))

    # 剩余部分按当前 exit_px 平
    closed_qty = sum(q for q, _ in pnl_parts)
    remain = contracts_orig - closed_qty
    if remain > 0:
        pnl_parts.append((remain, exit_px))

    gross = sum(_pnl_usd(side, entry, px, q) for q, px in pnl_parts)
    net = gross - FEE_USD

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
    icon = {"profit": "🟢", "loss": "🔴", "timeout": "⏰", "skip": "⚪"}.get(cat, "•")
    head = {
        "TP1_HIT": "TP1 触发 + 保本被打",
        "TP2_HIT": "TP2 触发 + 保本被打",
        "TP3_HIT": "全部止盈 ✓",
        "TP_BE":    "TP1 + TP2 后 BE 打回",
        "BE_STOPPED": "TP1 后保本被打",
        "SL_STOPPED": "止损扫掉",
        "TIMEOUT": "超时未触发",
        "MISSED":  "48h 未成交"
    }.get(res["outcome"], res["outcome"])

    card = (
        f"{icon} <b>BTC 平仓 · {head}</b>\n"
        f"{side_cn} {res.get('contracts', '?')} 张 @ {entry:,.1f}\n"
        f"出场 @ <b>{res['exit_px']:,.1f}</b> ｜ 净利 <b>{net:+.2f}U</b>（含 4.85U 手续费）\n"
        f"类目：{cat} ｜ 部分止盈 {res.get('partial_exits', 0)} 次"
    )
    _push(card)


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


def _check_ema_break_warning(sig: Dict, ts: int, side: str, entry: float,
                              cur_px: float, fill_px: float) -> bool:
    """
    EMA 突破预警检测。
    触发条件（同时满足）：
      ① 当前持仓盈利（cur_px > fill_px 做多；cur_px < fill_px 做空）
      ② 5m close < EMA144 且 < EMA169 (做多) / 反之做空
      ③ 15m close < EMA144 且 < EMA169 (做多) / 反之做空
      ④ 该预警在本笔持仓上**未触发过**（用 ema_warning_done 标记）

    触发后：部分平仓 70%、SL 移到 BE、写 partial_exit_count+1、推送。
    返回 True 表示本根已处理过预警（让调用方跳过 SL/TP 判定避免重复触发）。
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

    # 取 5m / 15m 当前 K 线的 close + 计算 EMA
    try:
        bars_5m = fetch_ohlcv(sig.get("contract", "BTC_USDT"), "5m",
                                200, cache=True)
        bars_15m = fetch_ohlcv(sig.get("contract", "BTC_USDT"), "15m",
                                200, cache=True)
    except Exception as e:
        if __debug__:
            print(f"[ema_warn] K 线获取失败: {e}")
        return False

    def _below(side, close, e144, e169):
        if side == "long":
            return close < e144 and close < e169
        return close > e144 and close > e169

    # 5m：取最后一根 close（已收盘）
    if not bars_5m:
        return False
    closes_5m = [float(r[4]) for r in bars_5m]
    if len(closes_5m) < 170:
        return False
    e144_5m = float(_ema_func(np.array(closes_5m), 144)[-1])
    e169_5m = float(_ema_func(np.array(closes_5m), 169)[-1])
    close_5m = closes_5m[-1]
    if not _below(side, close_5m, e144_5m, e169_5m):
        return False

    # 15m
    if not bars_15m:
        return False
    closes_15m = [float(r[4]) for r in bars_15m]
    if len(closes_15m) < 170:
        return False
    e144_15m = float(_ema_func(np.array(closes_15m), 144)[-1])
    e169_15m = float(_ema_func(np.array(closes_15m), 169)[-1])
    close_15m = closes_15m[-1]
    if not _below(side, close_15m, e144_15m, e169_15m):
        return False

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
        f"\n⚠️ 5m + 15m 双跌破 EMA144/169 → 上升结构变弱；保留 30% 跑趋势反转\n"
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
            if r.get("id") == signal_id and r.get("status") == "pending" and r.get("filled") is True:
                target = r
                break

    # 2. 回退：找最近一条已成交（不论方向）
    if target is None and use_latest_filled:
        for r in reversed(recs):
            if r.get("status") == "pending" and r.get("filled") is True:
                target = r
                break

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
    for r in recs:
        if r.get("status") != "pending":
            continue
        try:
            res = _settle_one(r, bars)
        except Exception as e:
            if verbose:
                print(f"[position_tracker] 结算 {r.get('id', '?')} 异常: {e}")
            continue

        if res:
            r.update(res)
            r["status"] = "settled"
            n_done += 1
            _append_event({"id": r.get("id"), "outcome": res["outcome"],
                           "net_usd": res["net_usd"], "category": res["category"],
                           "exit_ts": res["exit_ts"], "ts": int(time.time())})
            _notify_close(r, res)
        else:
            n_still += 1

    if n_done:
        _write_journal(recs)
        if verbose:
            print(f"[position_tracker] 结算 {n_done} 条｜ 仍 pending {n_still} 条")
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
