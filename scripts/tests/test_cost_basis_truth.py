#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
回归测试：1→2→3 三项真值修复（2026-11-04）
=============================================

## 背景：同一天训练环境「多次进场」排查牵出的账本/卡片系统性错账

1. **卡片真值** — hammer 成交卡三处说谎：
   「做多 0 张」（成交时根本没写 contracts）、只显示计划 TP/SL 不显示实际成交价
   与实际风险、`监测耗时 1.0s` 永远是 1 秒（hammer_ts 存 meta 里，代码读顶层）。

2. **账本真值** — `_close` 拿**计划 entry** 当成本基准算 PnL。hammer 走秒级市价
   追价成交（实测 300~400 点溢价），计划 entry 根本没成交过 → 账面系统性虚高：
   训练 5 笔账面 +26.22U 真实 -46.29U；生产 8 笔账面 +100.87U 真实 -33.47U。
   连带 BE（保本）也推在计划价上 → 名义「保本」实际在亏。

3. **同向冷却口径** — 冷却只统计 status=pending 的活跃单，已平仓单的 fill 不计入
   → 前单平掉后 30 分钟内再开同方向畅通无阻（11:45 成交、11:51 平仓、
   12:15 又进，间隔 29m32s）。修法：按**最近一次同方向 fill**（不论是否已平仓）
   计冷却，且**只在训练环境生效**（观察约 1 周再同步生产）。

跑法：
    cd scripts && /root/.hermes/hermes-agent/venv/bin/python3 -m unittest tests.test_cost_basis_truth -v
"""
import json, os, sys, tempfile, time, unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import position_tracker as pt
import config


# ══════════════════════════════════════════════════════════════
# 1. 成本基准 _cost_basis
# ══════════════════════════════════════════════════════════════
class TestCostBasis(unittest.TestCase):
    """_cost_basis：市价成交用真实成交价，限价/未成交用 entry。"""

    def test_market_fill_uses_fill_px(self):
        """市价成交（hammer）→ 成本基准 = fill_px，不是计划 entry。"""
        sig = {"entry": 76709.0, "fill_px": 77083.2,
               "_fill_method": "market_on_watcher"}
        self.assertEqual(pt._cost_basis(sig, 76709.0), 77083.2)

    def test_limit_fill_fill_px_equals_entry(self):
        """限价成交 fill_px == entry → 成本基准不变（回归：旧路径不受影响）。"""
        sig = {"entry": 80012.6, "fill_px": 80012.6}
        self.assertEqual(pt._cost_basis(sig, 80012.6), 80012.6)

    def test_unfilled_uses_entry(self):
        """未成交（还没 fill_px）→ 用 entry。"""
        sig = {"entry": 80000.0}
        self.assertEqual(pt._cost_basis(sig, 80000.0), 80000.0)

    def test_fill_px_zero_falls_back_to_entry(self):
        """fill_px=0 是脏数据 → 回退 entry，不除零。"""
        sig = {"entry": 80000.0, "fill_px": 0}
        self.assertEqual(pt._cost_basis(sig, 80000.0), 80000.0)


# ══════════════════════════════════════════════════════════════
# 2. 账本真值：_close 用真实成交价算 PnL
# ══════════════════════════════════════════════════════════════
class TestCloseUsesFillPrice(unittest.TestCase):
    """_close 的毛利必须按真实成交价算（不是计划 entry）。"""

    def _sig(self, **kw):
        base = {
            "id": "BTC_USDT-1793649653-long", "contract": "BTC_USDT",
            "side": "long", "entry": 76709.0, "sl": 76603.3,
            "tp1": 77706.2, "tp2": 78933.6, "status": "pending",
            "filled": True, "fill_px": 77083.2, "fill_ts": 1793649653,
            "contracts": 196, "_orig_contracts": 652,
        }
        base.update(kw)
        return base

    def test_pnl_uses_fill_px_not_plan_entry(self):
        """追价成交：毛利按 fill_px 算（旧口径按计划 entry 算 → 虚高）。

        剩余腿张数 = _orig_contracts（652）- 已平腿（0）= 652 张 @76708.0，
        成本 77083.2 → gross = (76708.0-77083.2) × 652 × 0.0001 = -24.46U，
        扣 fee 4.85 → net -29.31U。
        （对照：按计划 entry 76709 算 gross 只有 -0.07U，账面虚高 24.39U）
        """
        sig = self._sig()
        res = pt._close(sig, "SL_STOPPED", 76708.0, 1793653091,
                        196, "long", 76709.0, "loss")
        self.assertAlmostEqual(res["gross_usd"], -24.46, places=2)
        self.assertAlmostEqual(res["net_usd"], -29.31, places=2)

    def test_plan_entry_pnl_differs_from_fill_px(self):
        """同一笔单两种成本基准 → 净额必须不同（证明成本基准真生效）。"""
        sig = self._sig(fill_px=77083.2)
        res_fill = pt._close(dict(sig), "SL_STOPPED", 76708.0, 1793653091,
                             196, "long", 76709.0, "loss")
        # 拿掉 fill_px → 退回计划 entry 口径
        sig2 = dict(sig); sig2.pop("fill_px")
        res_plan = pt._close(sig2, "SL_STOPPED", 76708.0, 1793653091,
                             196, "long", 76709.0, "loss")
        self.assertLess(res_fill["net_usd"], res_plan["net_usd"],
                        "追价成交时真实净值必须低于计划口径（成本更高）")

    def test_no_fill_px_keeps_old_behavior(self):
        """限价单（fill_px==entry）→ 新旧口径一致（回归）。"""
        sig = self._sig(entry=80012.6, fill_px=80012.6, sl=80175.7,
                        tp1=79628.6, contracts=625, _orig_contracts=625,
                        fill_ts=1792860540)
        res = pt._close(sig, "SL_STOPPED", 80177.0, 1792860634,
                        625, "short", 80012.6, "loss")
        self.assertEqual(res["net_usd"], -15.12)


# ══════════════════════════════════════════════════════════════
# 3. 保本（BE）必须推在真实成本上
# ══════════════════════════════════════════════════════════════
class TestBeAtCostBasis(unittest.TestCase):
    """TP1/EMA 触发后推的保本线 = 真实成本价，不是计划入场价。"""

    def test_cost_basis_for_be(self):
        sig = {"entry": 76709.0, "fill_px": 77083.2,
               "_fill_method": "market_on_watcher"}
        self.assertEqual(pt._cost_basis(sig), 77083.2)
        # risk_guard._canonical_close 的 BE 判据：active_sl 在成本 ±1 点内才成立
        self.assertFalse(abs(76709.0 - 77083.2) < 1.0, "计划价不是保本位")
        self.assertTrue(abs(77083.2 - 77083.2) < 1.0, "成本价才是保本位")


# ══════════════════════════════════════════════════════════════
# 4. 同向冷却：已平仓的同向 fill 也算（训练先行）
# ══════════════════════════════════════════════════════════════
class TestCooldownCountsClosedFills(unittest.TestCase):
    """同向冷却按「最近一次同方向 fill（不论是否已平仓）」计，训练先行。"""

    def _write_journal(self, recs):
        fd, path = tempfile.mkstemp(suffix=".jsonl", text=True)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            for r in recs:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        return path

    def _rec(self, **kw):
        base = {"contract": "BTC_USDT", "side": "long", "entry": 76420.0,
                "ts": int(time.time()) - 3600}
        base.update(kw)
        return base

    def test_closed_fill_blocks_in_training(self):
        """训练：前单已平仓（settled）但 fill 在 30 分钟内 → 必拒。"""
        now = int(time.time())
        path = self._write_journal([
            self._rec(id="BTC_USDT-prev-long", status="settled", filled=True,
                      fill_ts=now - 1772, net_usd=-1.61, outcome="BE_STOPPED"),
        ])
        with patch.dict(os.environ, {"TRAINING_MODE": "1"}, clear=False):
            skip, reason = config.should_skip_new_signal(
                "BTC_USDT", "long", 76420.0, journal_path=path, now=now)
        self.assertTrue(skip, "训练：已平仓的同向 fill 在冷却窗内必须拦下")
        self.assertIn("已平仓的同向单也算", reason)

    def test_closed_fill_ignored_in_production(self):
        """生产（无 TRAINING_MODE）：旧口径不变 —— 已平仓单不参与冷却。"""
        now = int(time.time())
        path = self._write_journal([
            self._rec(id="BTC_USDT-prev-long", status="settled", filled=True,
                      fill_ts=now - 1772, net_usd=-1.61, outcome="BE_STOPPED"),
        ])
        env = {k: v for k, v in os.environ.items() if k != "TRAINING_MODE"}
        with patch.dict(os.environ, env, clear=True):
            skip, _ = config.should_skip_new_signal(
                "BTC_USDT", "long", 76420.0, journal_path=path, now=now)
        self.assertFalse(skip, "生产：旧口径不该被这次改动影响（观察期先只改训练）")

    def test_fill_outside_window_passes(self):
        """fill 超出冷却窗（>30min）→ 放行（训练也放行）。"""
        now = int(time.time())
        path = self._write_journal([
            self._rec(id="BTC_USDT-old-long", status="settled", filled=True,
                      fill_ts=now - 2700, net_usd=3.0),
        ])
        with patch.dict(os.environ, {"TRAINING_MODE": "1"}, clear=False):
            skip, _ = config.should_skip_new_signal(
                "BTC_USDT", "long", 76420.0, journal_path=path, now=now)
        self.assertFalse(skip)

    def test_env_override_forces_on(self):
        """BTC_COOLDOWN_INCLUDE_CLOSED=1 显式开启（供生产观察期后同步用）。"""
        now = int(time.time())
        path = self._write_journal([
            self._rec(id="BTC_USDT-prev-long", status="settled", filled=True,
                      fill_ts=now - 600, net_usd=-2.0),
        ])
        env = {k: v for k, v in os.environ.items() if k != "TRAINING_MODE"}
        env["BTC_COOLDOWN_INCLUDE_CLOSED"] = "1"
        with patch.dict(os.environ, env, clear=True):
            skip, _ = config.should_skip_new_signal(
                "BTC_USDT", "long", 76420.0, journal_path=path, now=now)
        self.assertTrue(skip)

    def test_pending_fill_still_blocks_in_production(self):
        """回归：未平仓的同向 fill 在生产照旧拦（旧规则不许被改坏）。"""
        now = int(time.time())
        path = self._write_journal([
            self._rec(id="BTC_USDT-open-long", status="pending", filled=True,
                      fill_ts=now - 600),
        ])
        env = {k: v for k, v in os.environ.items() if k != "TRAINING_MODE"}
        with patch.dict(os.environ, env, clear=True):
            skip, _ = config.should_skip_new_signal(
                "BTC_USDT", "long", 80000.0, journal_path=path, now=now)
        self.assertTrue(skip)


# ══════════════════════════════════════════════════════════════
# 5. hammer 成交：写全字段 + 卡片真值
# ══════════════════════════════════════════════════════════════
class TestHammerFillTruth(unittest.TestCase):
    """hammer 市价成交必须写张数/计划价留档，TP 按成交价重算。"""

    def _sig(self):
        return {
            "id": "BTC_USDT-1793765732-long", "contract": "BTC_USDT",
            "side": "long", "entry": 76420.0,
            "sl": 76227.2, "tp1": 77413.5, "tp2": 78636.2,
            "tp1_pct": 1.3, "tp2_pct": 2.9, "sl_pct": 0.2523,
            "status": "preview_pending", "filled": False,
            "preview_until_ts": int(time.time()) + 300,
            "meta": {"hammer_ts": int(time.time()) - 1353, "hammer_L": 76303.5,
                     "hammer_H": 76420.0},
        }

    def _run(self, sig, ticker):
        from watch import hammer_watcher as hw
        pushed = []
        tmp = tempfile.mkdtemp()
        with patch.object(hw, "DATA_DIR", tmp), \
             patch.object(hw, "_append_event", lambda ev: None), \
             patch.object(hw, "_push_fill", lambda s, px, el: pushed.append((s, px))):
            r = hw._process_one(sig, int(time.time()), ticker)
        return r, pushed

    def test_fill_writes_contracts_and_plan_entry(self):
        sig = self._sig()
        r, _ = self._run(sig, 76824.0)
        self.assertEqual(r, "filled")
        self.assertTrue(sig.get("filled"))
        self.assertEqual(sig["fill_px"], 76824.0)
        self.assertEqual(sig["_fill_method"], "market_on_watcher")
        # 张数必须写（旧 bug：不写 → 卡片「做多 0 张」）
        self.assertGreater(sig.get("contracts", 0), 0,
                           "成交必须写 contracts，否则卡片显示 0 张")
        self.assertEqual(sig.get("_orig_contracts"), sig["contracts"],
                         "_orig_contracts = 成交张数（部分止盈分段基准）")
        # 计划价留档（卡片对照「计划入场 vs 实际成交」）
        self.assertEqual(sig["plan_entry"], 76420.0)
        self.assertEqual(sig["plan_tp1"], 77413.5)
        self.assertEqual(sig["plan_tp2"], 78636.2)

    def test_tp_recomputed_from_fill_price(self):
        """TP1/TP2 按**成交价**重算（策略规则：止盈=入场×(1+幅度)）；SL 不动。"""
        sig = self._sig()
        self._run(sig, 76824.0)
        self.assertAlmostEqual(sig["tp1"], round(76824.0 * 1.013, 1), places=1)
        self.assertAlmostEqual(sig["tp2"], round(76824.0 * 1.029, 1), places=1)
        self.assertEqual(sig["sl"], 76227.2, "SL 是结构位，不随入场价漂移")

    def test_sl_pct_recomputed_from_fill_price(self):
        """风险占比按真实成交价算（旧口径按计划 entry → 追价时低估 3 倍）。"""
        sig = self._sig()
        self._run(sig, 76824.0)
        expect = round((76824.0 - 76227.2) / 76824.0 * 100, 4)
        self.assertAlmostEqual(sig["sl_pct"], expect, places=3)

    def test_card_shows_real_risk_and_plan_vs_fill(self):
        """卡片必须：有张数、有计划入场 vs 实际成交、有实际风险/盈亏比。"""
        from watch import hammer_watcher as hw
        sig = self._sig()
        cards = []
        tmp = tempfile.mkdtemp()
        with patch.object(hw, "DATA_DIR", tmp), \
             patch.object(hw, "_append_event", lambda ev: None), \
             patch("telegram.push_once", lambda key, text: cards.append(text)):
            hw._process_one(sig, int(time.time()), 76824.0)
        self.assertEqual(len(cards), 1, "成交卡必须推一次")
        card = cards[0]
        self.assertNotIn("做多 0 张", card, "旧 bug：张数显示 0")
        self.assertIn("计划入场 76,420.0", card, "必须显示计划入场价")
        self.assertIn("76,824.0", card, "必须显示实际成交价")
        self.assertIn("实际风险", card, "必须显示实际风险（按成交价）")
        self.assertIn("实际盈亏比", card, "必须显示实际盈亏比")
        self.assertIn("追价", card, "溢价必须点名")


if __name__ == "__main__":
    unittest.main(verbosity=2)
