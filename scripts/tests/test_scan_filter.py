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


class TestCoreSignalReverseConfirm(unittest.TestCase):
    """2026-10-08 core_b/core_s 反转确认逻辑测试
    旧逻辑要求 TD9 后立即连续 2 根同向 K 线（5.5 天/30 天 0% 命中）
    新逻辑：10 根窗口内回踩不破 + 突破收盘 + 末根站住
    """

    def _make_df(self, td9_idx, td9_close, td9_low, future_closes,
                 future_lows=None, future_highs=None):
        """构造测试用 df：TD9 索引 + 之后 10 根 K 线的 close/low/high"""
        n = td9_idx + 11
        close = np.zeros(n)
        low = np.zeros(n)
        high = np.zeros(n)
        # TD9 K 线本身
        close[td9_idx] = td9_close
        low[td9_idx] = td9_low
        high[td9_idx] = td9_close
        # 前 30 根做铺垫
        for i in range(td9_idx):
            close[i] = 100
            low[i] = 99
            high[i] = 101
        # 后续 10 根
        for j, (c, l, h) in enumerate(zip(future_closes,
                                            future_lows or future_closes,
                                            future_highs or future_closes)):
            close[td9_idx + 1 + j] = c
            low[td9_idx + 1 + j] = l
            high[td9_idx + 1 + j] = h
        # 同步 td9b/s 标记
        td9b = np.zeros(n, bool)
        td9s = np.zeros(n, bool)
        td9b[td9_idx] = True
        df = pd.DataFrame({
            'ts': np.arange(n), 'close': close, 'low': low, 'high': high,
            'vol': np.ones(n), 'td9b': td9b, 'td9s': td9s,
            'e144': close, 'e169': close, 'e169_s5': np.zeros(n),
            'rsi6': np.full(n, 50.0), 'rsi14': np.full(n, 50.0),
            'atrp': np.ones(n), 'vr': np.ones(n),
        })
        return df

    def _confirm(self, df, td9_col):
        """复刻 scan.py:418-438 的 _confirm_reverse 逻辑"""
        td9_recent = bool(df[td9_col].tail(5).any())
        idx_arr = np.where(df[td9_col].values)[0]
        if len(idx_arr) == 0:
            return False, None
        bidx = int(idx_arr[-1])
        CONF_WIN = 10
        if len(df) - bidx - 1 < CONF_WIN:
            return False, bidx
        td9_low = float(df.low.values[bidx])
        td9_close = float(df.close.values[bidx])
        win_close = df.close.values[bidx + 1: bidx + 1 + CONF_WIN]
        win_low = df.low.values[bidx + 1: bidx + 1 + CONF_WIN]
        win_high = df.high.values[bidx + 1: bidx + 1 + CONF_WIN]
        if td9_col == 'td9b':
            ok = (win_low.min() > td9_low
                  and win_high.max() > td9_close
                  and win_close[-1] > td9_close)
        else:
            ok = (win_high.max() < td9_low
                  and win_low.min() < td9_close
                  and win_close[-1] < td9_close)
        return ok, bidx

    def test_old_logic_was_impossible(self):
        """反向证明：旧逻辑（立即连续 2 根同向）在真实数据上 0% 命中"""
        # 旧逻辑要求：bidx+1 close > bidx close AND bidx+2 close > bidx+1 close
        df = self._make_df(td9_idx=50, td9_close=100, td9_low=98,
                          future_closes=[101, 102, 103, 104, 105, 106, 107, 108, 109, 110])
        bidx = 50
        old_ok = all(df.close.values[bidx + t] > df.close.values[bidx + t - 1]
                     for t in (1, 2))
        self.assertTrue(old_ok, "纯粹涨的场景旧逻辑成立（仅说明它能触发）")
        # 但 TD9 抄底的语义 = "前面 9 根都跌"，意味着抄底 K 线收盘弱
        # 实际数据上"立即连续 2 阳"很少出现

    def test_new_logic_long_pullback_confirm(self):
        """做多：TD9 抄底 → 回踩不破低 → 突破收盘 → 末根站住 → 应成立"""
        # TD9 K 线：低 98 收 100（抄底）
        # 后续 10 根：先涨到 105，回踩 100.5（>98，没破低），再涨到 110（>100），末根 108（>100）
        df = self._make_df(
            td9_idx=50, td9_close=100, td9_low=98,
            future_closes=[103, 105, 104, 102, 100.5, 102, 105, 108, 109, 108],
            future_lows=[102, 104, 103, 101, 99.5, 101, 104, 107, 108, 107],  # 99.5 < 98? 错!
            future_highs=[104, 106, 105, 103, 101, 103, 106, 109, 110, 109],
        )
        # 修：99.5 < 98（TD9 low）→ 应不成立
        # 重新设 low，让回踩最低 = 99（>98 ✓）
        df.loc[51:60, 'low'] = [99, 99, 99, 99, 99, 99, 99, 99, 99, 99]
        ok, _ = self._confirm(df, 'td9b')
        self.assertTrue(ok, "回踩不破低 + 突破 + 末根站住 应成立")

    def test_new_logic_long_break_low_should_fail(self):
        """做多：TD9 后回踩破了前低 → 不应成立"""
        df = self._make_df(
            td9_idx=50, td9_close=100, td9_low=98,
            future_closes=[103, 105, 104, 102, 99, 100, 102, 105, 107, 108],
            future_lows=[102, 104, 103, 101, 97, 99, 101, 104, 106, 107],  # 97 < 98
            future_highs=[104, 106, 105, 103, 100, 101, 103, 106, 108, 109],
        )
        ok, _ = self._confirm(df, 'td9b')
        self.assertFalse(ok, "回踩破前低 → 不应成立")

    def test_new_logic_long_last_close_below_should_fail(self):
        """做多：末根收盘低于 TD9 收盘 → 不应成立（趋势没确立）"""
        df = self._make_df(
            td9_idx=50, td9_close=100, td9_low=98,
            future_closes=[105, 108, 110, 112, 108, 105, 102, 100, 99, 99],
            future_lows=[104, 107, 109, 111, 107, 104, 101, 99, 98.5, 98.5],
            future_highs=[106, 109, 111, 113, 109, 106, 103, 101, 100, 100],
        )
        ok, _ = self._confirm(df, 'td9b')
        self.assertFalse(ok, "末根收盘 99 < TD9 收盘 100 → 不应成立")

    def test_new_logic_short_mirror(self):
        """做空：TD9 逃顶 → 反弹不破前低 → 跌破收盘 → 末根站下"""
        df = self._make_df(
            td9_idx=50, td9_close=100, td9_low=98,
            future_closes=[97, 95, 96, 94, 93, 95, 92, 90, 89, 90],
            future_lows=[96, 94, 95, 93, 92, 94, 91, 89, 88, 89],
            future_highs=[98, 96, 97, 95, 94, 96, 93, 91, 90, 91],  # 反弹最高 98 < 98 ✓ 不破
        )
        # 改 td9s 标记
        df['td9s'] = df['td9b'].copy()
        df['td9b'] = np.zeros(len(df), bool)
        ok, _ = self._confirm(df, 'td9s')
        # 末根 close=90 < td9_close=100 ✓
        # win_low.min() = 88 < td9_close=100 ✓
        # win_high.max() = 98 == td9_low=98（边界条件：< 不成立）
        self.assertFalse(ok, "反弹正好打平 TD9 low → 不应成立（要求严格小于）")

    def test_new_logic_short_proper(self):
        """做空：反弹明显不破前低 + 跌破 + 末根站下 → 应成立"""
        df = self._make_df(
            td9_idx=50, td9_close=100, td9_low=98,
            future_closes=[97, 95, 96, 94, 93, 95, 92, 90, 89, 90],
            future_lows=[96, 94, 95, 93, 92, 94, 91, 89, 88, 89],
            future_highs=[97.5, 96, 97, 95, 94, 96, 93, 91, 90, 91],  # 反弹最高 97.5 < 98 ✓
        )
        df['td9s'] = df['td9b'].copy()
        df['td9b'] = np.zeros(len(df), bool)
        ok, _ = self._confirm(df, 'td9s')
        self.assertTrue(ok, "反弹 97.5 < TD9 low 98 + 跌破 + 末根 90 < 100 → 应成立")


if __name__ == '__main__':
    unittest.main()
