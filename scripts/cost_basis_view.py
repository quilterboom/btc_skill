#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
cost_basis_view.py —— 净值展示口径公共函数（2026-11-04）

## 为什么需要它
hammer 秒级市价成交会**追价**（实测 +300~400 点），而账本里的 `net_usd`
是按**计划 entry** 记的（口径陈旧）。回填脚本把按真实成交价 canonical 重算
的结果写进 `net_usd_fill_basis`（`net_usd` 不改写，保留审计线索）。

于是同一笔单有两个净值：
    账面 net_usd          = 旧口径（按计划 entry）
    可信 net_usd_fill_basis = 新口径（按真实成交价）

所有**展示/统计**层必须用可信值，否则账面虚高会一路传导到看板、日报、
日历、边缘分。原始 `net_usd` 通过 `net_usd_book` 露出，供三段式对照。

## 零依赖（只用标准库）
`position_tracker` 有 import 副作用，web/ 与 scripts/ 都直接 import 本模块，
避免为了一个纯函数把整个 tracker 拖进来。

用法：
    from cost_basis_view import effective_net, net_of, basis_diff
    v = effective_net(rec)          # fill_basis 优先，账面兜底
"""
from __future__ import annotations
from typing import Any, Dict, Optional


def effective_net(rec: Dict[str, Any]) -> Optional[float]:
    """★ 净值取真实成本基准（fill_basis 优先，账面兜底）。

    - 有 `net_usd_fill_basis` → 用它（回填过，口径可信）
    - 没有 → 用 `net_usd`（正常单，两口径一致）
    - 都没有 → None（未结算，别当 0）
    """
    if not isinstance(rec, dict):
        return None
    v = rec.get("net_usd_fill_basis")
    if v is None:
        v = rec.get("net_usd")
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def net_of(rec: Dict[str, Any], default: float = 0.0) -> float:
    """同 effective_net，但 None → default（用于求和）。"""
    v = effective_net(rec)
    return default if v is None else v


def book_net(rec: Dict[str, Any]) -> float:
    """账面净值（原始 net_usd，None → 0）。"""
    if not isinstance(rec, dict):
        return 0.0
    try:
        return float(rec.get("net_usd") or 0)
    except (TypeError, ValueError):
        return 0.0


def basis_diff(rec: Dict[str, Any]) -> float:
    """可信 − 账面（口径差）。"""
    v = effective_net(rec)
    if v is None:
        return 0.0
    return round(v - book_net(rec), 2)


def is_stale(rec: Dict[str, Any]) -> bool:
    """该记录是否属于「口径陈旧」（回填过 → 账面值不可信）。"""
    return bool(isinstance(rec, dict) and rec.get("net_usd_fill_basis") is not None)


def is_ghost_fill(rec: Dict[str, Any]) -> bool:
    """★ 该记录是不是「幽灵成交」（从未真正发生过的 hammer 交易）。

    **前提（必须先满足）**：这条信号归 **HammerWatcher** 独占生命周期
    （`meta.hammer_managed` 为真，或 `signal_source == left_side_oversold_bounce`）。
    非 hammer 信号**一律不算**——同一个签名在别的信号源上有别的成因。

    ⚠️ **不要把「fill_ts < promoted_ts」单独当铁证**（2026-11-06 实测反例）：
    `BTC_USDT-1791692114-long`（15m_pattern）同样是 fill_ts 早于 promoted_ts，
    但它是**真单**——events 里 outcome 事件记 TP2_DONE +37.16（真值），
    账本记 +89.43 是出场被 risk_guard 重写造成的虚高，属于
    strategy_truth 的 `rewritten_exit` 类别，不是幽灵成交。
    把它误标成幽灵 = 把一笔真交易从统计里剔掉 = 新的假数据。

    **签名（命中前提后，按可靠性排序）**：

    1. `fill_ts < promoted_ts` —— 成交发生在信号被推进之前。对 hammer 信号
       而言物理不可能（成交只可能来自 watcher 在 preview_pending 期间的秒级
       市价，而 promote 发生在 preview 窗口到期之后）。
    2. `meta.hammer_managed` 且 `_fill_method != "market_on_watcher"` 却有
       `fill_px` —— hammer 的成交只能来自 watcher 的秒级市价（触发线
       `hammer_L × 1.003`）；别的路径补出来的 fill_px 是「计划限价」，
       不是真实成交价。**本签名单独即可判定**，且能覆盖 fill_ts 缺失的变体。

    **为什么要有这个函数**：2026-11-06 复盘发现，11-05 22:45 生产唯一一笔
    「已结算亏损 -24.09U」就是幽灵成交（hammer 触发线 79190 从未被 1m K 线
    触及，最高只到 79108；成交价 79094.3 == 计划 entry；fill_ts 早于
    promoted_ts）。它让日报/看板凭空多出一笔亏损。

    **处置纪律**：**不改 `net_usd`**（改了就把 bug 固化进账本），只是
    「从统计里剔除 + 标 data_quality」。真值重建不出来就留空等 liusir 拍板。

    用法：
        if cost_basis_view.is_ghost_fill(rec):
            continue
    """
    if not isinstance(rec, dict):
        return False
    meta = rec.get("meta") or {}
    # ── 前提：必须归 HammerWatcher 独占（排除 15m_pattern 等其它信号源）
    is_hammer = bool(meta.get("hammer_managed")) or \
        rec.get("signal_source") == "left_side_oversold_bounce"
    if not is_hammer:
        return False
    # ── 签名 2：非 watcher 成交却有 fill_px（单独即可判定）
    if rec.get("_fill_method") != "market_on_watcher" and rec.get("fill_px") is not None:
        return True
    # ── 签名 1：成交早于推进（对 hammer 信号是物理不可能）
    ft, pt_ = rec.get("fill_ts"), rec.get("promoted_ts")
    if ft and pt_:
        try:
            return int(ft) < int(pt_)
        except (TypeError, ValueError):
            return False
    return False


NOTIONAL_USD = 5000.0   # 名义 = 保证金 50U × 杠杆 100x（与 position_tracker 同源）
QUANT_MULT = 0.0001    # 1 张 = 0.0001 BTC


def cost_basis_of(rec: Dict[str, Any]) -> Optional[float]:
    """★ 这笔单**真实付出**的成本价（fill_px 优先，entry 兜底）。

    与 `position_tracker._cost_basis` 同一语义，但放在零依赖公共模块里，
    让 daily_report / web 都能用，不用 import position_tracker（有副作用）。

    - 限价成交：fill_px == entry → 两者相同，无差别
    - hammer 秒级市价（_fill_method=market_on_watcher）：fill_px 才是真实成交价，
      entry 只是「打算在哪进」——用它算收益 = 账面虚高
    """
    if not isinstance(rec, dict):
        return None
    fp = rec.get("fill_px")
    if fp is not None:
        try:
            v = float(fp)
        except (TypeError, ValueError):
            v = 0.0
        if v > 0:
            return v
    try:
        e = float(rec.get("entry") or 0)
    except (TypeError, ValueError):
        return None
    return e if e > 0 else None


def canonical_contracts(cost_px: float, notional_usd: float = NOTIONAL_USD) -> float:
    """★ 名义口径的仓位数（**不除杠杆**）。

    口径：`contracts = notional_usd / (price × QUANT_MULT)`。
    杠杆只决定保证金（50U），不决定名义——名义已经是 5000U。

    ⚠️ **别写成 `notional / leverage / (price × QUANT_MULT)`**：
    那是把名义又除了一次杠杆 = 仓位小 100 倍（实测 2026-11-07：
    daily_report 老师 TP 对照用这个写法，75885.7 入场只算出 6.6 张
    而非 659 张 → 老师 TP 盈亏被压缩 100 倍 → 卡片结论方向都反了）。

    交叉验证：`teacher_trades.jsonl` 里老师真实单的 size_contracts
    与本公式一致（#18: 78000 入场 → 641 张，公式算 641）。
    """
    if not cost_px or cost_px <= 0:
        return 0.0
    return notional_usd / (cost_px * QUANT_MULT)


def basis_triplet(records) -> Dict[str, float]:
    """批量三段式：{book, true, diff, stale_n}。

    book = 原始 net_usd 合计；true = fill_basis 优先合计；diff = true − book。
    """
    recs = list(records or [])
    book = round(sum(book_net(r) for r in recs), 2)
    true = round(sum(net_of(r) for r in recs), 2)
    return {
        "book": book,
        "true": true,
        "diff": round(true - book, 2),
        "stale_n": sum(1 for r in recs if is_stale(r)),
    }
