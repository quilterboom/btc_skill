#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
config.py 单元测试
==================

跑法：
    cd scripts && python3 -m unittest tests.test_config -v

不需要联网、不依赖 journal 真实数据——用临时 journal 文件测。

变更（2026-10-05）：
  should_skip_new_signal 现在把"无 ts 字段的 pending"视为过期丢弃
  （实际场景里 journal 写入时一定有 ts，测试需要补 ts 字段）
"""
import os, sys, json, tempfile, time, unittest
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config

NOW = int(time.time())


def _ts(recents: list) -> list:
    """给测试数据补 ts 字段（模拟真实 journal 记录）"""
    for r in recents:
        if r.get("status") in ("pending", "filled") and "ts" not in r:
            r["ts"] = NOW
    return recents


class TestShouldSkipNewSignal(unittest.TestCase):
    """测试 should_skip_new_signal 的所有边界"""

    def _write_journal(self, recs):
        """写临时 journal.jsonl"""
        fd, path = tempfile.mkstemp(suffix=".jsonl", text=True)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            for r in _ts(recs):
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        return path

    def test_empty_journal_should_not_skip(self):
        path = self._write_journal([])
        skip, reason = config.should_skip_new_signal("BTC_USDT", "long", 84000, journal_path=path)
        self.assertFalse(skip)
        self.assertEqual(reason, "")

    def test_no_same_contract_side(self):
        path = self._write_journal([
            {"contract": "ETH_USDT", "side": "long", "entry": 84000, "status": "pending"},
        ])
        skip, reason = config.should_skip_new_signal("BTC_USDT", "long", 84000, journal_path=path)
        self.assertFalse(skip)

    def test_no_active_status(self):
        path = self._write_journal([
            {"contract": "BTC_USDT", "side": "long", "entry": 84000, "status": "settled"},
            {"contract": "BTC_USDT", "side": "long", "entry": 84000, "status": "invalidated"},
        ])
        skip, reason = config.should_skip_new_signal("BTC_USDT", "long", 84000, journal_path=path)
        self.assertFalse(skip)

    def test_distance_below_threshold_should_skip(self):
        path = self._write_journal([
            {"contract": "BTC_USDT", "side": "long", "entry": 84000, "status": "pending"},
        ])
        skip, reason = config.should_skip_new_signal("BTC_USDT", "long", 84499, journal_path=path)
        self.assertTrue(skip)
        self.assertIn("499 点", reason)
        self.assertIn("< 500", reason)

    def test_distance_exact_threshold_should_not_skip(self):
        path = self._write_journal([
            {"contract": "BTC_USDT", "side": "long", "entry": 84000, "status": "pending"},
        ])
        # 距离 = 500，< 500 是 False → 不跳过
        skip, reason = config.should_skip_new_signal("BTC_USDT", "long", 84500, journal_path=path)
        self.assertFalse(skip)

    def test_distance_above_threshold_should_not_skip(self):
        path = self._write_journal([
            {"contract": "BTC_USDT", "side": "long", "entry": 84000, "status": "pending"},
        ])
        skip, reason = config.should_skip_new_signal("BTC_USDT", "long", 85000, journal_path=path)
        self.assertFalse(skip)

    def test_reverse_side_should_not_skip(self):
        path = self._write_journal([
            {"contract": "BTC_USDT", "side": "long", "entry": 84000, "status": "pending"},
        ])
        skip, reason = config.should_skip_new_signal("BTC_USDT", "short", 84000, journal_path=path)
        self.assertFalse(skip)

    def test_filled_status_also_triggers_skip(self):
        """已成交的单也要阻止同方向新策略

        2026-10-05 语义修正：position_tracker 用 status="pending" + filled=True
        表示「已成交未平仓」，所以「活跃」 = pending+filled=True（不再看 status=filled）。
        """
        path = self._write_journal([
            {"contract": "BTC_USDT", "side": "long", "entry": 84000,
             "status": "pending", "filled": True},
        ])
        skip, reason = config.should_skip_new_signal("BTC_USDT", "long", 84400, journal_path=path)
        self.assertTrue(skip)

    def test_custom_min_dist(self):
        path = self._write_journal([
            {"contract": "BTC_USDT", "side": "long", "entry": 84000, "status": "pending"},
        ])
        # 自定义 100 点阈值 → 距离 400 应该不跳过
        skip, reason = config.should_skip_new_signal("BTC_USDT", "long", 84400,
                                                      min_dist_pts=100, journal_path=path)
        self.assertFalse(skip)

    def test_picks_nearest_of_multiple(self):
        """多个同方向 active 时应选距离最近的那个"""
        path = self._write_journal([
            {"contract": "BTC_USDT", "side": "long", "entry": 83000, "status": "settled"},  # 不计
            {"contract": "BTC_USDT", "side": "long", "entry": 84000, "status": "pending"},  # 距离 1000
            {"contract": "BTC_USDT", "side": "long", "entry": 85000, "status": "pending"},  # 距离 0（最近）
        ])
        skip, reason = config.should_skip_new_signal("BTC_USDT", "long", 85000, journal_path=path)
        self.assertTrue(skip)
        self.assertIn("0 点", reason)

    def test_corrupt_json_should_not_skip(self):
        """journal 解析失败应安全降级为不跳过（不阻塞新策略）"""
        fd, path = tempfile.mkstemp(suffix=".jsonl", text=True)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write("{ not valid json\n")
        skip, reason = config.should_skip_new_signal("BTC_USDT", "long", 84000, journal_path=path)
        self.assertFalse(skip)

    def test_expired_pending_should_not_count(self):
        """超 MAX_PEND_SEC 的 pending 视为过期，不计入活跃（2026-10-05 新增）"""
        path = self._write_journal([
            {"contract": "BTC_USDT", "side": "long", "entry": 84000,
             "status": "pending", "ts": NOW - 49 * 3600},  # 49 小时前
        ])
        skip, reason = config.should_skip_new_signal("BTC_USDT", "long", 84100, journal_path=path)
        self.assertFalse(skip, f"过期 pending 应该不算活跃，但 skip={skip}, reason={reason!r}")

    def test_fresh_pending_should_count(self):
        """48h 内的 pending 算活跃"""
        path = self._write_journal([
            {"contract": "BTC_USDT", "side": "long", "entry": 84000,
             "status": "pending", "ts": NOW - 10 * 3600},  # 10 小时前
        ])
        skip, reason = config.should_skip_new_signal("BTC_USDT", "long", 84100, journal_path=path)
        self.assertTrue(skip, f"10h 内的 pending 应挡同方向，但 skip={skip}")


class TestShouldSkipTimeWindow(unittest.TestCase):
    """2026-10-06：30 分钟同方向已 fill 必拒（不论 entry 距离）"""

    def _write_journal(self, recs):
        fd, path = tempfile.mkstemp(suffix=".jsonl", text=True)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            for r in _ts(recs):
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        return path

    def test_recent_same_side_fill_under_window_is_blocked(self):
        """1h 内同方向已有 filled 单 → 必拒（不论 entry 距离）"""
        path = self._write_journal([
            {"contract": "BTC_USDT", "side": "long", "status": "pending",
             "filled": True, "entry": 85000.0, "fill_ts": NOW - 600,   # 10 分钟前
             "sl": 84320, "tp1": 85400, "tp2": 85600, "id": "BTC_USDT-TEST-long"},
        ])
        skip, reason = config.should_skip_new_signal(
            "BTC_USDT", "long", 89000.0, min_dist_pts=500, journal_path=path)
        self.assertTrue(skip)
        self.assertIn("10", reason)        # 包含分钟数
        self.assertIn("同方向", reason)

    def test_older_same_side_fill_falls_back_to_distance(self):
        """3h 前的同方向 filled → 不被时间窗口拦截，走原有距离判定（距离 > 500 应通过）"""
        path = self._write_journal([
            {"contract": "BTC_USDT", "side": "long", "status": "pending",
             "filled": True, "entry": 85000.0, "fill_ts": NOW - 10800,   # 3 小时前
             "sl": 84320, "tp1": 85400, "tp2": 85600, "id": "BTC_USDT-OLD-long"},
        ])
        skip, reason = config.should_skip_new_signal(
            "BTC_USDT", "long", 89000.0, min_dist_pts=500, journal_path=path)
        self.assertFalse(skip, f"应通过（3h 前 + 距离 > 500）：实际 reason={reason!r}")

    def test_recent_same_side_fill_close_distance_still_blocked(self):
        """窗口内同方向 + entry 距离近 → 也被时间窗口拦（不依赖距离兜底）"""
        path = self._write_journal([
            {"contract": "BTC_USDT", "side": "long", "status": "pending",
             "filled": True, "entry": 85000.0, "fill_ts": NOW - 300,   # 5 分钟前
             "sl": 84320, "tp1": 85400, "tp2": 85600, "id": "BTC_USDT-X-long"},
        ])
        skip, reason = config.should_skip_new_signal(
            "BTC_USDT", "long", 85050.0, min_dist_pts=500, journal_path=path)
        self.assertTrue(skip)
        self.assertIn("5", reason)


class TestConstants(unittest.TestCase):
    """常量合理性"""

    def test_min_dist_positive(self):
        self.assertGreater(config.ENTRY_MIN_DIST_PTS, 0)

    def test_target_positive(self):
        self.assertGreater(config.DEFAULT_TARGET_PTS, 0)

    def test_settler_interval_positive(self):
        self.assertGreaterEqual(config.SETTLER_INTERVAL_SEC, 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
