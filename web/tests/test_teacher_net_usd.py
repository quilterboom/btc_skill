#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
回归测试：老师单 canonical PnL 公式 _compute_trade_net_usd（2026-10-28 修）
====================================================================

**修的三个语义坑（都来自真实老师单数据，不是假想）**：

1. **多腿建仓（add_position）** — trade #13（81572 半仓 + 81100 补仓半仓）
   旧代码用 `fill_price`（首腿价 81572）当入场基准 → 整笔 PnL 算错。
   新代码：entry_basis = 加权均价（result.avg_entry 优先，否则按 qty_pct 加权）。

2. **NOT_TRIGGERED 占位事件** — trade #12/#13 的 tp2_partial（目标价从未触达）
   旧代码只按 type 过滤 → NOT_TRIGGERED 的 tp2_partial 会被当出场腿计 gross
   （凭空多出几十 U），或漏计真实出场。新代码：status=NOT_TRIGGERED → 不计 gross、不占仓位。

3. **breakeven_stop 两种语义** — trade #15（无 status = 把止损移到保本，不吃仓位）
   vs trade #12（status=TRIGGERED = 真的被打保本出场）
   旧代码一律当出场腿 → consumed_pct 吃穿，后续腿的 qty_pct 推断全错。
   新代码：只有 status=TRIGGERED 才是出场腿。

跑法：
    cd /root/skills/btc-gate-intraday-strategy/web && \
    /root/.hermes/hermes-agent/venv/bin/python3 -m unittest tests.test_teacher_net_usd -v
"""
import json
import os
import sys
import unittest
from pathlib import Path

WEB_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(WEB_DIR))

from server import _compute_trade_net_usd  # noqa: E402


def _trade(**kw):
    t = {"side": "long", "notional_usd": 5000, "events": []}
    t.update(kw)
    return t


class TestTeacherNetUsd(unittest.TestCase):
    """单腿普通单（trade #4 口径）：TP1 减半 + 剩余打保本"""

    def test_tp1_plus_breakeven_remaining(self):
        t = _trade(
            fill_price=82393,
            events=[
                {"type": "entry", "px": 82393},
                {"type": "tp1_partial", "px": 82987, "qty_pct": 50},
                {"type": "remaining_close", "px": 82393},
            ],
        )
        # 607 张 × 594 点 × 0.5 × 0.0001 = 18.03 毛 - 4.85 fee
        self.assertAlmostEqual(_compute_trade_net_usd(t), 13.18, delta=0.05)

    def test_short_side(self):
        t = _trade(
            side="short", fill_price=82289,
            events=[
                {"type": "entry", "px": 82289},
                {"type": "tp1_partial", "px": 81728, "qty_pct": 50},
                {"type": "remaining_close", "px": 82289, "qty_pct": 50},
            ],
        )
        self.assertAlmostEqual(_compute_trade_net_usd(t), 12.20, delta=0.10)

    def test_missing_qty_pct_does_not_fallback_to_100(self):
        """★ trade #15 实踩的坑：tp1_partial 缺 qty_pct → fallback 100% → 净利虚高 15U"""
        t = _trade(
            fill_price=79979,
            events=[
                {"type": "entry", "px": 79979},
                {"type": "tp1_partial", "px": 80326},          # 无 qty_pct
                {"type": "remaining_close", "px": 80900, "qty_pct": 70},
            ],
        )
        # 正确口径：30% @80326 + 70% @80900 = 46.80 毛 - 4.85 = 41.95
        self.assertAlmostEqual(_compute_trade_net_usd(t), 41.95, delta=0.05)


class TestMultiLegEntry(unittest.TestCase):
    """★ 坑 1：多腿建仓（trade #13）"""

    def _t13(self):
        return _trade(
            fill_price=81572,
            size_contracts=614,
            result={"avg_entry": 81335.2},
            events=[
                {"type": "entry", "px": 81572, "qty_pct": 50},
                {"type": "add_position", "px": 81100, "qty_pct": 50},
                {"type": "tp1_partial", "px": 81880, "qty_pct": 50},
                {"type": "tp2_partial", "px": 82518, "qty_pct": 50, "status": "NOT_TRIGGERED"},
                {"type": "remaining_close", "px": 81336, "qty_pct": 50},
            ],
        )

    def test_uses_weighted_avg_not_first_leg(self):
        # 均价 81335.2：TP1 段 307 张 × 544.8 点 = 16.72 毛，剩余保本 0 → -4.85 fee
        self.assertAlmostEqual(_compute_trade_net_usd(self._t13()), 11.87, delta=0.05)

    def test_not_triggered_placeholder_not_counted(self):
        """★ 坑 2：82518 从未触达 → 不能按 82518 计 gross（否则凭空 +36U）"""
        t = self._t13()
        # 若误计 tp2_partial@82518：+1182 点 × 307 张 → +36.3U 虚高
        self.assertLess(_compute_trade_net_usd(t), 20)

    def test_weighted_avg_without_explicit_avg_entry(self):
        """result.avg_entry 缺失时，按 qty_pct 加权自算均价"""
        t = self._t13()
        t["result"] = {}
        self.assertAlmostEqual(_compute_trade_net_usd(t), 11.87, delta=0.05)


class TestBreakevenStopSemantics(unittest.TestCase):
    """★ 坑 3：breakeven_stop 两种语义（#15 放置 vs #12 被打）"""

    def test_breakeven_stop_without_status_is_not_an_exit_leg(self):
        """#15：无 status 的 breakeven_stop = 把止损移到保本 → 不吃仓位"""
        t = _trade(
            fill_price=79979,
            events=[
                {"type": "entry", "px": 79979},
                {"type": "tp1_partial", "px": 80326, "qty_pct": 30},
                {"type": "breakeven_stop", "px": 79979},        # 无 status = 动作
                {"type": "remaining_close", "px": 80900, "qty_pct": 70},
            ],
        )
        self.assertAlmostEqual(_compute_trade_net_usd(t), 41.95, delta=0.05)

    def test_breakeven_stop_triggered_is_an_exit_leg(self):
        """#12：status=TRIGGERED 的 breakeven_stop = 真的被打保本出场 → 0 gross"""
        t = _trade(
            fill_price=83850,
            size_contracts=596,
            events=[
                {"type": "entry", "px": 83850},
                {"type": "tp1_partial", "px": 84455, "qty_pct": 70},
                {"type": "tp2_partial", "px": 85088, "qty_pct": 30, "status": "NOT_TRIGGERED"},
                {"type": "breakeven_stop", "px": 83850, "qty_pct": 30, "status": "TRIGGERED"},
            ],
        )
        # 596 张 × 605 点 × 0.7 = 25.24 毛 - 4.85 fee = 20.39
        self.assertAlmostEqual(_compute_trade_net_usd(t), 20.39, delta=0.05)


class TestOtherEventTypes(unittest.TestCase):
    def test_remaining_pending_consumes_but_no_gross(self):
        t = _trade(
            fill_price=83240,
            events=[
                {"type": "entry", "px": 83240},
                {"type": "tp1_partial", "px": 83770, "qty_pct": 70},
                {"type": "remaining_pending", "px": 83240, "qty_pct": 30},
                {"type": "remaining_close", "px": 83240, "qty_pct": 30},
            ],
        )
        # 601 张 × 530 点 × 0.7 = 22.30 毛 - 4.85 = 17.45
        self.assertAlmostEqual(_compute_trade_net_usd(t), 17.45, delta=0.05)

    def test_sl_triggered_is_marker_not_exit(self):
        """#14：sl_triggered 只是"止损被触发"的标记，真正的出场腿是 close_sl"""
        t = _trade(
            fill_price=80600,
            events=[
                {"type": "entry", "px": 80600},
                {"type": "sl_triggered", "px": 80300},
                {"type": "close_sl", "px": 80300},
            ],
        )
        # 620 张 × -300 点 = -18.60 毛 - 4.85 = -23.45
        self.assertAlmostEqual(_compute_trade_net_usd(t), -23.45, delta=0.05)

    def test_tp2_full_alias(self):
        t = _trade(
            fill_price=81755,
            events=[
                {"type": "pre", "px": 81800},
                {"type": "entry", "px": 81755},
                {"type": "tp1_partial", "px": 82230, "qty_pct": 50},
                {"type": "tp2_full", "px": 82700, "qty_pct": 50},
            ],
        )
        # 612 张 × 475 点 × 0.5 + 612 张 × 945 点 × 0.5 = 43.46 毛 - 4.85 = 38.61
        self.assertAlmostEqual(_compute_trade_net_usd(t), 38.61, delta=0.10)

    def test_range_entry_returns_none(self):
        """区间成交（'82400-82600'）→ 算不出来 → 返回 None 由用户手填"""
        t = _trade(
            entry_zone="82400-82600", fill_price="82400-82600",
            events=[{"type": "entry", "px": 82400, "qty_pct": 50}],
        )
        self.assertIsNone(_compute_trade_net_usd(t))

    def test_no_close_events_returns_none(self):
        t = _trade(fill_price=81572, events=[{"type": "entry", "px": 81572}])
        self.assertIsNone(_compute_trade_net_usd(t))


class TestAgainstRealLedger(unittest.TestCase):
    """用真实 teacher_trades.jsonl 回归：canonical 与历史手填值对账"""

    LEDGER = Path("/root/data/teacher_trades.jsonl")

    def test_ledger_reconciliation(self):
        if not self.LEDGER.exists():
            self.skipTest("teacher_trades.jsonl 不存在")
        rows = [json.loads(l) for l in self.LEDGER.read_text(encoding="utf-8").splitlines() if l.strip()]
        # 有手填 net_usd 的历史单，canonical 应落在 ±0.6U 内（取整差容忍）
        checked = 0
        for t in rows:
            hand = (t.get("result") or {}).get("net_usd")
            if hand is None:
                continue
            auto = _compute_trade_net_usd(t)
            if auto is None:
                continue
            checked += 1
            self.assertAlmostEqual(
                auto, hand, delta=0.6,
                msg=f"trade #{t.get('trade_id')}: canonical={auto} vs 手填={hand}",
            )
        self.assertGreaterEqual(checked, 8, "对账样本太少")


if __name__ == "__main__":
    unittest.main(verbosity=2)
