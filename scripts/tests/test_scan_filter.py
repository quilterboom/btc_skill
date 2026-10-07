#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
scan.py 下跌中段反弹过滤器的单元测试
====================================

跑法：cd scripts && python3 -m unittest tests.test_scan_filter -v

不需要联网——mock 1h K 线 dataframe。
"""
import os, sys, json, unittest
import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import scan


def _make_1h_df(closes: list, highs: list = None, lows: list = None) -> pd.DataFrame:
    """构造一个最小可用的 1h K 线 dataframe（带 e144 / td9b 等字段占位）"""
    n = len(closes)
    if highs is None:
        highs = [c + 10 for c in closes]
    if lows is None:
        lows = [c - 10 for c in closes]
    return pd.DataFrame({
        "ts": list(range(n)),
        "open": closes,
        "high": highs,
        "low": lows,
        "close": closes,
        "vol": [100.0] * n,
    })


class TestDropRecoveryFilter(unittest.TestCase):
    """2026-10-06：1h 内跌幅 ≥ 1.5% + 做多打分 → 减 1 分"""

    def test_no_drop_returns_zero_penalty(self):
        """平稳走势：drop_pct=0, penalty=0"""
        # 100 根 1h K 线全在 86000 ± 50
        closes = [86000 + (i % 5 - 2) * 10 for i in range(100)]
        highs = [c + 30 for c in closes]
        lows = [c - 30 for c in closes]
        df = _make_1h_df(closes, highs, lows)
        drop_pct, penalty = scan.compute_drop_recovery_penalty(df, side="long")
        self.assertLess(drop_pct, 1.5)
        self.assertEqual(penalty, 0)

    def test_significant_drop_returns_penalty(self):
        """2.3% 跌幅（类似 10-05 22:11 那种）→ 减 1 分"""
        # 100 根：前 90 根平稳 87000；最后 10 根：87000 → 85000 急跌 + 反弹到 85500
        closes = [87000] * 90 + [86500, 86000, 85500, 85000, 85200, 85400, 85500, 85550, 85580, 85600]
        highs  = [c + 50 for c in closes]
        lows   = [c - 50 for c in closes]
        # 倒数第 2 根 high = 86550；最后 1 根 low = 85450 → drop = (86550-85450)/86550 = 1.27%
        # 调整：让最后 5 根走出更深的跌幅：86000→84500→85800
        closes = closes[:-5] + [86000, 85500, 85000, 84500, 85800]
        highs  = [c + 80 for c in closes]
        lows   = [c - 80 for c in closes]
        # 倒数第 2 根 high=85880, 最后 1 根 low=85720 → drop=(85880-85720)/85880=0.19% 还是不够
        # 改：倒数第 2 根 high=86080, 最后 1 根 low=84420 → drop=(86080-84420)/86080=1.93% ✅
        df = _make_1h_df(closes, highs, lows)
        drop_pct, penalty = scan.compute_drop_recovery_penalty(df, side="long")
        self.assertGreaterEqual(drop_pct, 1.5, f"期望 ≥1.5%，实际 {drop_pct:.2f}%")
        self.assertEqual(penalty, 1)

    def test_short_side_no_penalty_from_drop(self):
        """做空打分方向不受下跌反弹过滤器影响（下跌反弹反倒是顺势）"""
        closes = [87000] * 90 + [86000, 85500, 85000, 84500, 85800]
        highs  = [c + 80 for c in closes]
        lows   = [c - 80 for c in closes]
        df = _make_1h_df(closes, highs, lows)
        # 函数对 short 方向提前 return (0.0, 0)，但 drop_pct 本身仍可计算
        # 这里直接验证：penalty=0（不扣分），drop_pct 由调用方在 long 方向才用
        drop_pct, penalty = scan.compute_drop_recovery_penalty(df, side="short")
        self.assertEqual(penalty, 0)
        # 顺便手动算一下确认跌幅确实 ≥ 1.5%（验证测试数据构造合理）
        h_max = max(df.high.values[-2:])
        l_min = min(df.low.values[-2:])
        real_drop = (h_max - l_min) / h_max * 100
        self.assertGreaterEqual(real_drop, 1.5, f"测试数据没构造好，实际跌幅 {real_drop:.2f}%")

    def test_short_kline_not_enough_returns_zero(self):
        """少于 2 根 K 线 → 返回 (0, 0)，不报错"""
        df = _make_1h_df([86000], [86050], [85950])
        drop_pct, penalty = scan.compute_drop_recovery_penalty(df, side="long")
        self.assertEqual(drop_pct, 0.0)
        self.assertEqual(penalty, 0)


def _apply_verdict_guard(verdict: str, config_skip: bool, config_reason: str):
    """复刻 scan.py:619 加的 verdict 守门（不调 scan.py 全函数——它依赖实时 K 线 + talib）。"""
    _skip, _reason = config_skip, config_reason
    if not _skip and not ("成立" in verdict or "临界" in verdict):
        _skip = True
        _reason = f"verdict 不成立（{verdict}），按纪律核心①②未满足不进场"
    return _skip, _reason


class TestScanVerdictGuard(unittest.TestCase):
    """2026-10-07 加：scan.py 不该给 verdict="无信号·观望"写 journal pending。

    Bug 实例：1791329412 / 1791337511 两条 long 单都是 score=0 + verdict="无信号·观望"
    → 被 log_signal 写入 → 成交 → 各打 SL 亏 -4.85 / -19.01U。
    守门：仅 "成立" / "临界" verdict 才放行；其它一律 skip。
    """

    def test_passive_verdict_no_signal_blocks(self):
        """verdict="无信号 · 观望" → 拦下，不写 journal"""
        skip, reason = _apply_verdict_guard("无信号 · 观望", False, "")
        self.assertTrue(skip)
        self.assertIn("无信号", reason)

    def test_active_verdict_signal_成立_passes(self):
        """信号成立（无论方向）→ 放行"""
        self.assertFalse(_apply_verdict_guard("做多信号成立", False, "")[0])
        self.assertFalse(_apply_verdict_guard("做空信号成立", False, "")[0])

    def test_boundary_verdict_临界_passes(self):
        """临界（再等 1 根确认）也要刷点位——放行"""
        skip, _ = _apply_verdict_guard("临界（再等 1 根确认）", False, "")
        self.assertFalse(skip)

    def test_empty_verdict_blocks(self):
        """空 verdict 防御性拦下"""
        skip, _ = _apply_verdict_guard("", False, "")
        self.assertTrue(skip)

    def test_existing_skip_preserved(self):
        """已有 config_skip（同方向冷却/距离）不能被 verdict 守门覆盖 reason"""
        skip, reason = _apply_verdict_guard("无信号 · 观望", True, "同方向冷却 30 分钟")
        self.assertTrue(skip)
        self.assertEqual(reason, "同方向冷却 30 分钟",
                         "verdict 守门应只在 config_skip=False 时介入")


if __name__ == "__main__":
    unittest.main(verbosity=2)
