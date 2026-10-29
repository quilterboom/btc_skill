#!/usr/bin/env python3
"""
回归测试：15m 形态检测的「陈旧形态触发」bug（2026-10-30 修）

Bug
----
`_detect_15m_pattern()` 原本的循环是 `for i in range(last_valid_i, -1, -1)`，
它把 fetch 回来的整个 50 根 15m 窗口（= 12.5 小时）全部扫完。
由于循环是**从新到旧**遍历，只要最近几根里没有"形态 + 确认"齐全的组合，
就会一路回溯到十几小时前的旧形态 bar，并把它当成**当前**的形态信号返回。

后果（实测，生产 journal 数据）：
  - 10-29 04:00 / 05:15 两笔 15m_pattern journal，形态 bar 都是 10-28 17:30
    的锤子（形态年龄 10.5 / 11.8 小时），两笔合计 -37.35U。
  - 同一根旧形态 bar 可被反复触发：10-24 05:00 那根触发 4 次、10-28 17:30 触发 2 次。
  - 29 笔 15m_pattern 中有 11 笔的形态 bar 龄 > 60 分钟。

修法
----
形态 bar 必须落在最近 `lookback` 根内（默认 5 根 = 75 分钟），
与 10-09 回测「逐 bar 向前扫描、每根形态只算一次」的口径一致。

跑法：cd scripts && python3 -m unittest tests.test_detect_15m_pattern_stale -v
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _bar(ts, o, h, l, c):
    """构造一根 15m K 线（Gate _pack 格式 [ts, o, h, l, c, v, sum]）"""
    return [ts, o, h, l, c, 100.0, 0.0]


def _hammer(ts):
    """锤子线：实体 100，下影 300，上影 10（满足 lower>=body*2, upper<body*0.5）"""
    return _bar(ts, 80000.0, 80110.0, 79700.0, 80100.0)


def _bull(ts):
    return _bar(ts, 80100.0, 80200.0, 80050.0, 80180.0)


def _flat(ts):
    return _bar(ts, 80180.0, 80190.0, 80160.0, 80175.0)


class TestDetect15mPatternStaleGuard(unittest.TestCase):
    """陈旧形态不得触发信号"""

    def setUp(self):
        import scan
        self.scan = scan
        self._orig_fetch = scan.fetch_ohlcv
        self._orig_trend = scan._30m_trend_at
        self._patchers = []

    def tearDown(self):
        import scan
        scan.fetch_ohlcv = self._orig_fetch
        scan._30m_trend_at = self._orig_trend

    def _install(self, rows):
        """把 fetch_ohlcv 换成返回固定 rows 的桩"""
        def fake_fetch(contract, interval, total=50, cache=True, end_ts=None, **kw):
            return list(rows)
        self.scan.fetch_ohlcv = fake_fetch
        self.scan._30m_trend_at = lambda ts: 0

    def test_stale_pattern_outside_lookback_not_triggered(self):
        """★ 核心用例：唯一形态在 20 根之前 → 必须返回 None

        这正是 10-29 的现场：最近的 bar 都没有形态，只有 20 根前有个锤子，
        旧代码会把它当新形态返回（形态年龄 5 小时）。
        """
        base = 1_700_000_000
        rows = []
        # 20 根前：一根锤子 + 后一根阳线（确认成立）
        rows.append(_hammer(base))
        rows.append(_bull(base + 900))
        # 之后 18 根全是普通 bar，没有任何形态
        for i in range(2, 20):
            rows.append(_flat(base + i * 900))

        self._install(rows)
        res = self.scan._detect_15m_pattern("BTC_USDT", lookback=5)
        self.assertIsNone(
            res,
            f"20 根前的旧形态不该触发信号（lookback=5 只看最近 5 根），却返回 {res}")

    def test_fresh_pattern_within_lookback_still_triggers(self):
        """对照组：形态 bar 落在最近 lookback 根内 → 必须仍然触发（别把功能改死）"""
        base = 1_700_000_000
        rows = []
        for i in range(3):
            rows.append(_flat(base + i * 900))
        # 倒数第 3 根（index len-3）放锤子，倒数第 2 根阳线确认
        rows.append(_hammer(base + 3 * 900))
        rows.append(_bull(base + 4 * 900))
        rows.append(_flat(base + 5 * 900))

        self._install(rows)
        res = self.scan._detect_15m_pattern("BTC_USDT", lookback=5)
        self.assertIsNotNone(res, "lookback 窗口内的形态应该照常触发")
        self.assertEqual(res["side"], "long")
        self.assertEqual(res["kind"], "锤子+次根阳线")

    def test_lookback_boundary_is_respected(self):
        """形态 bar 正好在 lookback 边界外 1 根 → 不触发；边界内 → 触发"""
        base = 1_700_000_000
        # 6 根：index 0 是锤子+阳线确认，index 1..5 是普通 bar
        # lookback=5 时 index 0 在窗口外 → 不触发
        rows = [_hammer(base), _bull(base + 900)]
        for i in range(2, 6):
            rows.append(_flat(base + i * 900))

        self._install(rows)
        res = self.scan._detect_15m_pattern("BTC_USDT", lookback=5)
        self.assertIsNone(res, "lookback=5 时 index 0 已在窗口外，不该触发")

        # lookback=6 → index 0 进入窗口 → 应该触发
        res6 = self.scan._detect_15m_pattern("BTC_USDT", lookback=6)
        self.assertIsNotNone(res6, "lookback=6 时 index 0 在窗口内，应该触发")
        self.assertEqual(res6["side"], "long")

    def test_pattern_too_recent_has_no_confirm_bar(self):
        """最后一根是锤子但没有后一根确认 → 不触发（既有的"要确认 K 线"语义不能破）"""
        base = 1_700_000_000
        rows = [_flat(base), _flat(base + 900), _hammer(base + 2 * 900)]
        self._install(rows)
        res = self.scan._detect_15m_pattern("BTC_USDT", lookback=5)
        self.assertIsNone(res, "没有确认 K 线的锤子不该触发")


if __name__ == "__main__":
    unittest.main(verbosity=2)
