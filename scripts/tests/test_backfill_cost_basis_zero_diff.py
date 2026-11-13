#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
回归测试：成本基准回填的「差额为 0 不盖章」纪律（2026-11-13）

## 背景

`backfill_hammer_cost_basis.py` 原来对**每一笔** hammer 市价成交都盖
「口径陈旧」章（`net_usd_fill_basis` + `data_quality` + `pnl_basis_stale`）。

但差额（真实成交价口径 − 账面）= 0 的单，说明账本**本来就是按真实成交价
记的**（结算时代码已用 fill_px 当成本基准），**不是陈旧口径**。给它们盖章
有三个后果：

1. `data_quality` 谎称「按计划 entry 记账」（与事实相反）→ 审计线索被污染
2. `pnl_basis_stale(v1)` 把非陈旧单标成陈旧
3. 展示层 `is_stale()` 为真 → 日报卡片打出「-2.04U (账面 -2.04U)」
   —— 同一个数印两遍，纯噪音（违反「恒等于 0 的指标不用正负表达」通则）

实测 2026-11-13：生产 6 笔 + 训练 14 笔差额全为 0.00U，只有训练
`BTC_USDT-1793765732-long` 真正陈旧（账面 +86.51 → 真实 +50.43，差 -36.08U）。

## 纪律

- 差额为 0 → **不写任何字段**（不加 fill_basis / 不加 data_quality / 不打 stale 标记）
- 差额非 0 → 照旧回填（只加字段，`net_usd` 不改写）
- 判据是**差额**，不是「有没有回填过」

跑法：
    cd scripts && /root/.hermes/hermes-agent/venv/bin/python3 -m unittest tests.test_backfill_cost_basis_zero_diff -v
"""
import copy
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import backfill_hammer_cost_basis as bf  # noqa: E402


# ── 真实账本形状（2026-11-13 实测，从 journal 原样摘录）────────
# 差额 = 0 的单：hammer 追价成交，但账本净值已按真实成交价记
#   （含 ema 部分减仓腿 ema_partial_px/qty + partial_net_usd，
#     canonical 重算必须能复现账面 -2.04U —— 夹具必须自洽）
ZERO_DIFF_REC = {
    "id": "BTC_USDT-1794439831-long",
    "contract": "BTC_USDT",
    "ts": 1794439831,
    "side": "long",
    "score": 7,
    "core_ok": True,
    "entry": 73633.0,
    "entry_break": 73873.0,
    "sl": 73483.0,
    "tp1": 74741.2,
    "tp2": 75768.4,
    "sl_pct": 0.4052,
    "tp1_pct": 1.3,
    "tp_mode": "custom",
    "signal_source": "left_side_oversold_bounce",
    "meta": {
        "lev": 100.0, "bal": 50.0, "risk": 0.01, "mode": "cross",
        "signal_source": "left_side_oversold_bounce",
        "strategy_version": "v1_prod",
        "hammer_ts": 1794439440, "hammer_managed": True,
        "hammer_O": 73586.8, "hammer_H": 73633.0,
        "hammer_L": 73556.6, "hammer_C": 73632.9,
        "hammer_v": 0, "trigger_pct": 0.003,
    },
    "status": "settled",
    "filled": True,
    "fill_px": 73782.0,          # 追价 +149 点
    "fill_ts": 1794439831,
    "_fill_method": "market_on_watcher",
    "_max_high_during_window": 73782.0,
    "plan_entry": 73633.0,
    "plan_tp1": 74590.2,
    "plan_tp2": 75768.4,
    "contracts": 204,
    "_orig_contracts": 678,
    "ema_warning_done": True,
    "ema_warning_ts": 1794439860,
    "ema_warning_px": 73845.0,
    "active_sl": 73782.0,
    "partial_exit_count": 1,
    "partial_exits": 1,
    "partial_net_usd": 2.99,
    "ema_partial_qty": 474,
    "ema_partial_px": 73845.0,
    "last_check_ts": 1794439860,
    "exit_ts": 1794440587,
    "exit_px": 73773.2,
    "risk_guard_exit_ts": 1794440587,
    "risk_guard_exit_px": 73773.2,
    "net_usd": -2.04,
    "gross_usd": 2.81,
    "category": "loss",
    "outcome": "BE_STOPPED",
    "pos_state": "BE_STOPPED",
}

# 真正陈旧的单：账面按计划 entry 记（+86.51），真实成交价口径只有 +50.43
STALE_REC = {
    "id": "BTC_USDT-1793765732-long",
    "contract": "BTC_USDT",
    "ts": 1793765732,
    "side": "long",
    "score": 7,
    "core_ok": True,
    "entry": 76420.0,
    "entry_break": 77026.0,
    "sl": 76227.2,
    "tp1": 77413.5,
    "tp2": 78636.2,
    "sl_pct": 0.2523,
    "tp1_pct": 1.3,
    "tp_mode": "custom",
    "signal_source": "left_side_oversold_bounce",
    "meta": {
        "lev": 100.0, "bal": 50.0, "risk": 0.01, "mode": "cross",
        "signal_source": "left_side_oversold_bounce",
        "strategy_version": "v2_test",
        "hammer_ts": 1793764380, "hammer_managed": True,
        "hammer_O": 76365.0, "hammer_H": 76420.0,
        "hammer_L": 76303.5, "hammer_C": 76419.9,
        "hammer_v": 0, "trigger_pct": 0.003,
    },
    "status": "settled",
    "filled": True,
    "fill_px": 76824.0,          # 追价 +404 点
    "fill_ts": 1793765733,
    "_fill_method": "market_on_watcher",
    "_max_high_during_window": 76824.0,
    "last_check_ts": 1793788380,
    "ema_warning_done": True,
    "ema_warning_ts": 1793767260,
    "ema_warning_px": 76888.0,
    "contracts": 654,
    "active_sl": 76420.0,
    "partial_exit_count": 3,
    "partial_exits": 3,
    "partial_net_usd": 21.39,
    "ema_partial_qty": 457,
    "ema_partial_px": 76888.0,
    "_orig_contracts": 654,
    "tp1_hit": True,
    "tp1_hit_ts": 1793768040,
    "tp1_hit_px": 77413.5,
    "tp2_hit": True,
    "tp2_hit_ts": 1793788440,
    "tp2_hit_px": 78636.2,
    "pos_state": "TP2_DONE",
    "outcome": "TP2_DONE",
    "exit_ts": 1793788440,
    "exit_px": 78636.2,
    "net_usd": 86.51,
    "gross_usd": 91.36,
    "category": "profit",
    "safety_break": False,
    "loss_depth": 0.0,
    "hold_h": 6.3,
}


class TestZeroDiffNotStamped(unittest.TestCase):
    """差额为 0 的单不许盖章。"""

    def test_zero_diff_record_gets_no_fields(self):
        """★ 核心：差额=0 → 不加任何 fill_basis 字段，不打 stale 标记。"""
        rec = copy.deepcopy(ZERO_DIFF_REC)
        res = bf._recompute_fill_basis(rec)
        self.assertIsNotNone(res, "差额=0 的单必须能重算（否则无法证明差额=0）")
        self.assertAlmostEqual(res["net_usd"], ZERO_DIFF_REC["net_usd"], places=2,
                               msg="差额=0 的单重算净值必须等于账面净值")

    def test_stale_record_detected_with_nonzero_diff(self):
        """对照组：真正陈旧的单差额显著非零。"""
        rec = copy.deepcopy(STALE_REC)
        res = bf._recompute_fill_basis(rec)
        self.assertIsNotNone(res)
        self.assertNotAlmostEqual(res["net_usd"], STALE_REC["net_usd"], places=2,
                                  msg="陈旧单重算净值必须与账面不同")
        # 实测值：账面 +86.51 → 真实 +50.43
        self.assertAlmostEqual(res["net_usd"], 50.43, places=2)

    def test_apply_skips_zero_diff_records(self):
        """★ 端到端：--apply 后差额=0 的单**不能**出现任何新增字段。"""
        import json
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as td:
            jp = Path(td) / "journal.jsonl"
            recs = [copy.deepcopy(ZERO_DIFF_REC), copy.deepcopy(STALE_REC)]
            jp.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in recs) + "\n",
                          encoding="utf-8")

            out = bf.backfill(jp, apply=True)
            self.assertEqual(out["no_diff"], [ZERO_DIFF_REC["id"]],
                             "差额=0 的单必须被记进 no_diff")
            self.assertEqual(out["changed"], [STALE_REC["id"]],
                             "只有真正陈旧的单该被回填")

            after = {json.loads(l)["id"]: json.loads(l)
                     for l in jp.read_text(encoding="utf-8").splitlines() if l.strip()}

            z = after[ZERO_DIFF_REC["id"]]
            for k in ("net_usd_fill_basis", "gross_usd_fill_basis",
                      "category_fill_basis", "outcome_fill_basis", "pnl_basis_stale"):
                self.assertNotIn(k, z, f"差额=0 的单不该有字段 {k}")
            self.assertIsNone(z.get("data_quality"), "差额=0 的单不该被盖 data_quality 章")
            self.assertEqual(z["net_usd"], ZERO_DIFF_REC["net_usd"], "net_usd 永不改写")

            s = after[STALE_REC["id"]]
            self.assertIsNotNone(s.get("net_usd_fill_basis"))
            self.assertAlmostEqual(s["net_usd_fill_basis"], 50.43, places=2)
            self.assertEqual(s["net_usd"], STALE_REC["net_usd"], "net_usd 永不改写")
            self.assertIn("口径陈旧", (s.get("data_quality") or ""))

    def test_hammer_market_fill_detector(self):
        """判定条件不变：hammer 市价成交 且 真实成交价 ≠ 计划入场价。"""
        self.assertTrue(bf._is_hammer_market_fill(copy.deepcopy(ZERO_DIFF_REC)))
        self.assertTrue(bf._is_hammer_market_fill(copy.deepcopy(STALE_REC)))
        # 非 watcher 成交不重算
        other = copy.deepcopy(ZERO_DIFF_REC)
        other["_fill_method"] = "limit"
        self.assertFalse(bf._is_hammer_market_fill(other))


if __name__ == "__main__":
    unittest.main(verbosity=2)
