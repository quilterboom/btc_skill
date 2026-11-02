#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
清算簇前向验证器测试（2026-11-02，liq_map_forward.py）

锁住的口径：
  1. **样本独立性门槛** —— 数量够但挤在同一段行情里 → 报「独立性不足」，
     不给相关系数（10min 一条快照，90min 内 11 条是同一段行情的重复观测，
     相关系数是伪相关，统计功效≈0）
  2. 样本数不够 → 报「样本不足」，且措辞是「还没数据」不是「无关系」
  3. 前向收益按 ts 就近匹配 K 线，且必须有 N 根未来 K 线才计入
  4. 分组统计用摊平的标量字段（不能拿嵌套 dict 排序）
  5. 相关性计算不崩（常数序列 → None，不抛异常）
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "scripts"))
import liq_map_forward as F  # noqa: E402


def _sample(ts, fwd, score=-0.3, above_str=60.0, below_str=90.0):
    return {
        "ts": ts, "ts_iso": "2026-11-02 12:00:00", "px": 78000.0,
        "fwd_ret": fwd, "bwd_ret": None, "px_fwd": 78000.0,
        "score": score, "label": "偏空",
        "above_top": {"price": 78500.0, "strength_pct": above_str, "dist_pct": 0.6},
        "below_top": {"price": 77500.0, "strength_pct": below_str, "dist_pct": -0.6},
        "above_dist": 0.6, "below_dist": 0.6,
    }


class TestPowerCheck(unittest.TestCase):
    """★ 样本独立性门槛 —— 本测试文件的核心。"""

    def test_too_few_samples(self):
        res = F.analyze([_sample(0, 0.1)] * 3, 6, "15m")
        self.assertEqual(res["status"], "样本不足")
        self.assertIn("还没数据", F.render(res, []))

    def test_clustered_samples_rejected(self):
        """11 条挤在 90 分钟内 → 拒绝给结论。"""
        base = 1793600000
        samples = [_sample(base + i * 600, -0.35 + i * 0.005) for i in range(11)]
        res = F.analyze(samples, 6, "15m")
        self.assertEqual(res["status"], "样本独立性不足")
        self.assertNotIn("corr_above_strength_vs_fwd", res)
        reasons = " ".join(res["power"]["reasons"])
        self.assertIn("覆盖时长", reasons)
        self.assertIn("离散度", reasons)

    def test_render_states_not_no_relation(self):
        base = 1793600000
        samples = [_sample(base + i * 600, -0.35 + i * 0.005) for i in range(11)]
        res = F.analyze(samples, 6, "15m")
        txt = F.render(res, samples)
        self.assertIn("样本独立性不足", txt)
        self.assertIn("伪相关", txt)
        self.assertNotIn("无关系", txt)      # 不能说「无关系」——是样本问题
        self.assertNotIn("+0.2224", txt)     # 不该出现相关系数

    def test_distinct_states_counting(self):
        """future_ret 变化 < tol 的相邻样本视为同一状态。"""
        base = 1793600000
        same = [_sample(base + i * 600, 0.10) for i in range(5)]      # 完全不变
        self.assertEqual(F._distinct_states(same), 1)
        diff = [_sample(base + i * 600, 0.10 + i * 0.5) for i in range(5)]
        self.assertEqual(F._distinct_states(diff), 5)

    def test_sufficient_independence_passes(self):
        """跨时段 + 离散度够 + 状态数够 → 允许给结论。"""
        base = 1793600000
        samples = []
        for i in range(20):
            samples.append(_sample(base + i * 3600, (-1) ** i * (0.2 + i * 0.08)))
        res = F.analyze(samples, 6, "15m")
        self.assertEqual(res["status"], "ok")
        self.assertIn("corr_score_vs_fwd", res)

    def test_power_check_reasons_are_specific(self):
        base = 1793600000
        samples = [_sample(base + i * 600, 0.10) for i in range(12)]
        pw = F._power_check(samples)
        self.assertFalse(pw["ok"])
        self.assertGreaterEqual(len(pw["reasons"]), 2)
        self.assertIn("distinct_states", pw["detail"])


class TestBuildSamples(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self._p = [
            mock.patch.object(F, "STORE", Path(self._tmp) / "s.jsonl"),
            mock.patch.object(F, "KLINES", Path(self._tmp) / "k.json"),
        ]
        for p in self._p:
            p.start()

    def tearDown(self):
        for p in self._p:
            p.stop()

    def test_needs_future_bars(self):
        """K 线不够 N 根未来 → 该样本不算。"""
        snaps = [{"ts": 1000, "px": 100.0, "above": [], "below": [], "suggestion": {}}]
        bars = [[1000, 1, 1, 1, 100.0, 1, 1], [1100, 1, 1, 1, 101.0, 1, 1]]
        out = F.build_samples(snaps, bars, 6, "15m")
        self.assertEqual(len(out), 0)

    def test_forward_return_computed(self):
        snaps = [{"ts": 1000, "px": 100.0, "above": [], "below": [], "suggestion": {}}]
        bars = [[1000 + i * 100, 1, 1, 1, 100.0 + i, 1, 1] for i in range(10)]
        out = F.build_samples(snaps, bars, 6, "15m")
        self.assertEqual(len(out), 1)
        # bars[6].close = 106, px = 100 → +6%
        self.assertAlmostEqual(out[0]["fwd_ret"], 6.0, places=4)

    def test_nearest_cluster_picks_closest(self):
        above = [{"price": 79000.0, "strength_pct": 50, "dist_pct": 1.3},
                 {"price": 78200.0, "strength_pct": 90, "dist_pct": 0.25}]
        c = F._nearest_cluster(above, 78000.0, above=True)
        self.assertEqual(c["price"], 78200.0)

    def test_correlation_constant_series_returns_none(self):
        """常数序列 → None，不抛异常。"""
        self.assertIsNone(F._corr([1.0, 1.0, 1.0], [1.0, 2.0, 3.0]))


class TestGroupReport(unittest.TestCase):
    def test_group_report_on_scalar_field(self):
        samples = [{** _sample(0, 0.1), "above_strength": float(i)} for i in range(9)]
        g = F._group_report(samples, "above_strength")
        self.assertEqual(g["status"], "ok")
        self.assertGreaterEqual(len(g["bins"]), 2)
        for b in g["bins"]:
            self.assertIn("fwd_ret", b)
            self.assertIn("n", b["fwd_ret"])

    def test_group_report_insufficient(self):
        g = F._group_report([{"x": 1.0, "fwd_ret": 0.1}], "x")
        self.assertEqual(g["status"], "样本不足")


if __name__ == "__main__":
    unittest.main(verbosity=2)
