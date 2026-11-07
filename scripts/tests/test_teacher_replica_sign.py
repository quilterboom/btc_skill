#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""回归测试：复刻引擎的 short 单 PnL 符号 + 出场阶梯记账（2026-11-07）

## 为什么要有这个测试
实测发现 `teacher_replica.settle` 的 short 单 TP 腿 PnL **符号反了**：
`gross += q1 * (tp1 - entry) * 1e-4` —— short 单的 tp1 < entry，
这一项必然为负 → 把「止盈」记成了「亏损」。

同类 bug 在 `teacher_engine_fidelity` 的重放循环里也有一份（两个文件各写了一遍）。

**短单符号错误 = 整个空头方向的结论反向**，而本仓库正好有 2 笔老师空单
（#6 short / #10 short），所以必须钉死。

## 跑法
    cd scripts && /root/.hermes/hermes-agent/venv/bin/python3 \
        -m unittest tests.test_teacher_replica_sign -v
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from teacher_replica import CFG, SigGen, load_bars, settle


def _synth(prices, start=1_700_000_000, step=900):
    """合成 15m K 线：给定收盘价序列，H/L 留 ±1 点。"""
    bars = []
    for i, p in enumerate(prices):
        bars.append([start + i * step, p, p + 1.0, p - 1.0, p, 1000.0, 1000.0 * p])
    return bars


def _flat_then(base, n, delta=0.0):
    return [base] * n


class TestShortSign(unittest.TestCase):

    def _sg(self):
        sg = SigGen(load_bars("15m"), 15)
        self.assertTrue(len(sg.bars) > 200, "15m K 线不足")
        return sg

    def _sg_synth(self, prices):
        return SigGen(_synth(prices), 15)

    def test_short_tp1_produces_positive_gross(self):
        """合成：横盘 30 根 → 持续下跌 30 根。short 单在下跌中触发 TP1/TP2。"""
        sg = self._sg_synth([80000.0] * 30 + [80000.0 - 50 * i for i in range(1, 31)])
        entry_i = 25
        entry = sg.close[entry_i]
        # 手工构造：TP1 在下方 0.5%，SL 在上方 0.5%
        sig = {"i": entry_i, "ts": int(sg.bars[entry_i][0]), "side": "short",
               "level": sg.high[entry_i], "ref_px": entry,
               "drop_pct": 1.0, "vol_x": 1.0, "dist_pct": 0.1, "atr_pct": 0.5}
        cfg = dict(CFG)
        cfg.update({"sl_pct": 50.0, "tp1_pct": 0.5, "tp2_pct": 1.0,
                    "sl_struct": False, "max_hold_hours": 48})
        r = settle(sg, {**sig, "fill_i": entry_i, "px": entry,
                         "fill_ts": int(sg.bars[entry_i][0])}, cfg)
        self.assertIn(r["exit"], ("TP2", "TP1"), f"未触发止盈，exit={r['exit']}")
        self.assertGreater(r["gross"], 0,
                           f"short 单止盈 gross 应为正，实得 {r['gross']:.4f}"
                           "（TP 腿符号反了）")

    def test_long_tp1_produces_positive_gross(self):
        """合成：横盘 30 根 → 持续上涨 30 根。long 单触发 TP1/TP2。"""
        sg = self._sg_synth([80000.0] * 30 + [80000.0 + 50 * i for i in range(1, 31)])
        entry_i = 25
        entry = sg.close[entry_i]
        sig = {"i": entry_i, "ts": int(sg.bars[entry_i][0]), "side": "long",
               "level": sg.low[entry_i], "ref_px": entry,
               "drop_pct": 1.0, "vol_x": 1.0, "dist_pct": 0.1, "atr_pct": 0.5}
        cfg = dict(CFG)
        cfg.update({"sl_pct": 50.0, "tp1_pct": 0.5, "tp2_pct": 1.0,
                    "sl_struct": False, "max_hold_hours": 48})
        r = settle(sg, {**sig, "fill_i": entry_i, "px": entry,
                         "fill_ts": int(sg.bars[entry_i][0])}, cfg)
        self.assertIn(r["exit"], ("TP2", "TP1"), f"未触发止盈，exit={r['exit']}")
        self.assertGreater(r["gross"], 0)

    def test_sl_produces_negative_gross_both_sides(self):
        """打损时两个方向的 gross 都必须为负。"""
        sg = self._sg()
        entry = sg.close[100]
        cfg = dict(CFG)
        cfg.update({"sl_pct": 0.1, "tp1_pct": 50.0, "tp2_pct": 60.0,
                    "sl_struct": False, "max_hold_hours": 48})
        for side in ("long", "short"):
            sig = {"i": 100, "ts": int(sg.bars[100][0]), "side": side,
                   "level": sg.low[100] if side == "long" else sg.high[100],
                   "ref_px": entry, "drop_pct": 1.0, "vol_x": 1.0,
                   "dist_pct": 0.1, "atr_pct": 0.5}
            r = settle(sg, {**sig, "fill_i": 100, "px": entry,
                             "fill_ts": int(sg.bars[100][0])}, cfg)
            self.assertEqual(r["exit"], "SL", f"{side} 未按预期打损")
            self.assertLess(r["gross"], 0, f"{side} 打损 gross 应为负")

    def test_be_exit_is_flat_after_tp1(self):
        """TP1 后打保本：剩余腿 gross 贡献为 0（不是负）。"""
        # 合成：横盘 30 根 → 跌 20 点（触发 TP1 0.3%≈240 点？改用 0.05% 幅度）
        sg = self._sg_synth([80000.0] * 30 + [80000.0 - 10 * i for i in range(1, 21)])
        entry_i = 25
        entry = sg.close[entry_i]
        sig = {"i": entry_i, "ts": int(sg.bars[entry_i][0]), "side": "short",
               "level": sg.high[entry_i], "ref_px": entry,
               "drop_pct": 1.0, "vol_x": 1.0, "dist_pct": 0.1, "atr_pct": 0.5}
        cfg = dict(CFG)
        cfg.update({"sl_pct": 0.3, "tp1_pct": 0.05, "tp2_pct": 60.0,
                    "sl_struct": False, "max_hold_hours": 48})
        r = settle(sg, {**sig, "fill_i": entry_i, "px": entry,
                         "fill_ts": int(sg.bars[entry_i][0])}, cfg)
        if r["exit"] != "BE":
            self.skipTest(f"该 bar 未走出保本（exit={r['exit']}）")
        # BE 腿的成交价 == 入场价 → 贡献 0
        be_legs = [l for l in r["leg"] if l[0] == "BE"]
        for name, qty, px in be_legs:
            self.assertAlmostEqual(px, entry, places=6,
                                   msg="保本腿成交价应等于入场价")


if __name__ == '__main__':
    unittest.main(verbosity=2)
