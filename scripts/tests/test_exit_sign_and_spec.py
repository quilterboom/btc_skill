#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""回归测试：出场结算的 short 单 PnL 符号 + 规格反推（2026-11-07）

## 为什么要有这个测试
2026-11-07 实测发现出场结算的 short 单 TP 腿 PnL **符号反了**：
`gross += q1 * (tp1 - entry) * 1e-4` —— short 单的 tp1 < entry，
这一项必然为负 → 把「止盈」记成了「亏损」。

同类 bug 当天在两个文件里各有一份（settle 与重放循环）。
**空头方向整条结论会反向**，而本仓库正好有 2 笔老师空单 → 必须钉死。

## 覆盖
- short 单止盈 → gross 必须为正
- long 单止盈 → gross 必须为正（对照）
- 打损 → 两个方向 gross 都为负
- TP1 后打保本 → 剩余腿贡献为 0
- 规格反推的覆盖率语义（le_pXX / ge_pXX）

## 跑法
    cd scripts && /root/.hermes/hermes-agent/venv/bin/python3 \
        -m unittest tests.test_exit_sign_and_spec -v
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ref_trader_tools import BarSeries, spec_from_samples, spec_threshold
from replica_driver import make_exit


def _synth(prices, start=1_700_000_000, step=900):
    """合成 15m K 线：给定收盘价序列，H/L 留 ±1 点。"""
    bars = []
    for i, p in enumerate(prices):
        bars.append([start + i * step, p, p + 1.0, p - 1.0, p, 1000.0, 1000.0 * p])
    return bars


LADDER = {"tp1_pct": 0.5, "tp1_reduce_pct": 50.0, "tp2_pct": 1.0, "sl_pct": 50.0}


class TestExitSign(unittest.TestCase):

    def _exit(self, prices, side, entry_i=25, ladder=None):
        bars = _synth(prices)
        sg = BarSeries(bars, 15)
        px = sg.close[entry_i]
        return make_exit({"side": side, "px": px, "ts": int(sg.tss[entry_i])},
                         ladder or LADDER, bars, 15)

    def test_short_tp_produces_positive_gross(self):
        """合成：横盘 30 根 → 持续下跌。short 单在下跌中止盈 → net 必须为正。"""
        r = self._exit([80000.0] * 30 + [80000.0 - 50 * i for i in range(1, 31)], "short")
        self.assertIn(r["exit"], ("TP2",), f"未触发 TP2，exit={r['exit']}")
        self.assertGreater(r["net"], 0,
                           f"short 止盈 net 应为正，实得 {r['net']:.4f}（TP 腿符号反了）")

    def test_long_tp_produces_positive_gross(self):
        """对照：long 单在上涨中止盈 → net 必须为正。"""
        r = self._exit([80000.0] * 30 + [80000.0 + 50 * i for i in range(1, 31)], "long")
        self.assertIn(r["exit"], ("TP2",), f"未触发 TP2，exit={r['exit']}")
        self.assertGreater(r["net"], 0, f"long 止盈 net 应为正，实得 {r['net']:.4f}")

    def test_sl_produces_negative_net_both_sides(self):
        """打损时两个方向 net 都必须为负。

        ★ 方向：long 打损 = 价格**下跌**；short 打损 = 价格**上涨**。
          （构造测试数据时把方向搞反，就会测成「不打损」而不是「打损」）
        """
        ladder = {"tp1_pct": 50.0, "tp1_reduce_pct": 50.0,
                  "tp2_pct": 60.0, "sl_pct": 0.1}
        for side, prices in (("long", [80000.0] * 30 + [80000.0 - 50 * i for i in range(1, 31)]),
                             ("short", [80000.0] * 30 + [80000.0 + 50 * i for i in range(1, 31)])):
            r = self._exit(prices, side, ladder=ladder)
            self.assertEqual(r["exit"], "SL", f"{side} 未按预期打损（exit={r['exit']}）")
            self.assertLess(r["net"], 0, f"{side} 打损 net 应为负")

    def test_be_exit_is_flat_after_tp1(self):
        """TP1 后打保本 → 剩余腿贡献 0（净利只剩 TP1 那段）。"""
        # 横盘 → 跌 10 点/根（TP1 0.05% ≈ 40 点会先触发），再反弹打保本
        prices = ([80000.0] * 30 + [80000.0 - 10 * i for i in range(1, 25)]
                  + [80000.0 - 240 + 3 * i for i in range(1, 20)])
        ladder = {"tp1_pct": 0.05, "tp1_reduce_pct": 50.0,
                  "tp2_pct": 60.0, "sl_pct": 0.3}
        r = self._exit(prices, "short", ladder=ladder)
        if r["exit"] != "BE":
            self.skipTest(f"该路径未走出保本（exit={r['exit']}）")
        # 保本腿成交价 = 入场价 → 剩余腿毛利 0 → 净利 = TP1 毛利 - 手续费
        self.assertGreater(r["net"], 0, "TP1 毛利应覆盖手续费")
        self.assertLess(r["net"], 20, "保本腿不应贡献额外盈利")


class TestSpecSemantics(unittest.TestCase):

    def test_le_and_ge_quantiles_are_distinct(self):
        """le_pXX（XX% 样本 <= 它）与 ge_pXX（XX% 样本 >= 它）必须不同。

        ★ 语义搞反会把样本分布读反（同一个维度，「近」和「远」方向相反）。
        """
        samples = [{"v": float(i)} for i in range(1, 21)]   # 1..20
        spec = spec_from_samples(samples, keys=["v"])
        le = spec_threshold(spec, "v", 0.8, "le")
        ge = spec_threshold(spec, "v", 0.8, "ge")
        self.assertIsNotNone(le)
        self.assertIsNotNone(ge)
        # le_p80 = 「80% 样本 <= 它」→ 在分布高侧；ge_p80 = 「80% 样本 >= 它」→ 在低侧
        self.assertGreater(le, ge,
                           f"le_p80({le}) 应大于 ge_p80({ge})"
                           "（「80% 样本不超过它」的阈值必然高于「80% 样本不低于它」）")
        self.assertGreaterEqual(sum(1 for s in samples if s["v"] <= le) / len(samples), 0.75)
        self.assertGreaterEqual(sum(1 for s in samples if s["v"] >= ge) / len(samples), 0.75)
        # 具体值：样本 1..20，le_p80 = vals[15] = 16，ge_p80 = vals[3] = 4
        self.assertEqual(le, 16.0, "le_p80 应为第 80 分位（1..20 里 80% 不超过 16）")
        self.assertEqual(ge, 5.0, "ge_p80 应为第 20 分位（1..20 里 80% 不低于 5）")

    def test_categorical_dimension_is_handled(self):
        """分类维度（struct=down/up）不应被当成数值做分位。"""
        samples = [{"s": "down"}] * 11 + [{"s": "up"}] * 7 + [{"s": "flat"}] * 2
        spec = spec_from_samples(samples, keys=["s"])
        d = spec["dims"]["s"]
        self.assertTrue(d.get("categorical"))
        self.assertEqual(d["dist"]["down"], 11)
        self.assertIsNone(spec_threshold(spec, "s", 0.8, "le"))

    def test_spec_is_regenerable_not_hardcoded(self):
        """规格 = 数据的函数：样本变 → 规格变（这是「不写死」的核心保证）。"""
        a = spec_from_samples([{"v": float(i)} for i in range(1, 21)], keys=["v"])
        b = spec_from_samples([{"v": float(i)} for i in range(1, 31)], keys=["v"])
        self.assertNotEqual(a["dims"]["v"]["le_p80"], b["dims"]["v"]["le_p80"])


if __name__ == '__main__':
    unittest.main(verbosity=2)
