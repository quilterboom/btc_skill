#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
position_tracker.py 防御性测试 (2026-10-18)
============================================

跑法：
    cd scripts && python3 -m unittest tests.test_sanity_check -v

覆盖 4 个场景：
  1. _close exit_ts <= fill_ts → raise（防 journal 污染）
  2. _close exit_ts < tp1_hit_ts（TP1 hit 但 BE 早于 TP1）→ raise
  3. 正常 BE_STOPPED exit_ts > tp1_hit_ts → 不 raise
  4. settle_all 写盘后 hold_h 由 (exit_ts - fill_ts)/3600 强制重算
"""
import os, sys, json, tempfile, unittest
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import position_tracker as pt


def _bar(ts, o, h, l, c):
    """构造一根 K 线（ts, o, h, l, c, vol）"""
    return [ts, o, h, l, c, 100]


class TestCloseSanityCheck(unittest.TestCase):
    """_close() sanity check：防止 cache 滚动或早期路径污染 journal"""

    def test_close_rejects_exit_before_fill(self):
        """
        exit_ts 必须 > fill_ts，否则 raise。
        背景：10-18 发现 5/13 笔训练 journal 里 BE_STOPPED 的 exit_ts
        等于 fill_ts+60s（疑似 cache 滚动后被 force_close 的 int(time.time())
        或某种早期路径覆盖）。
        """
        sig = {
            "id": "sanity-1",
            "side": "long",
            "entry": 100.0,
            "fill_ts": 10000,
            "fill_px": 100.0,
            "tp1_hit": True,
            "tp1_hit_ts": 10100,
            "tp1_hit_px": 101.0,
        }
        # exit_ts=10060 (fill_ts+60s) 应该 raise
        with self.assertRaises(ValueError) as ctx:
            pt._close(sig, "BE_STOPPED", 100.0, 10060, 1000, "long", 100.0, "profit")
        self.assertIn("sanity check FAILED", str(ctx.exception))
        self.assertIn("exit_ts", str(ctx.exception))

    def test_close_rejects_exit_before_tp1(self):
        """
        TP1 hit 时 exit_ts 必须 >= tp1_hit_ts，否则 raise。
        背景：10-18 发现 1792199726 这笔 tp1_hit_ts=00:45 但 exit_ts=10:03（早 14h+）
        """
        sig = {
            "id": "sanity-2",
            "side": "short",
            "entry": 100.0,
            "fill_ts": 10000,
            "fill_px": 100.0,
            "tp1_hit": True,
            "tp1_hit_ts": 10100,  # TP1 触发时刻
            "tp1_hit_px": 99.0,
        }
        # exit_ts=10050 < tp1_hit_ts=10100 应该 raise
        with self.assertRaises(ValueError) as ctx:
            pt._close(sig, "BE_STOPPED", 100.0, 10050, 1000, "short", 100.0, "profit")
        # ★ 2026-10-26 修正断言：报错文案从「BE 必须晚于 TP1 触发」改成了
        #   「BE 必须晚于保本移动」（2026-10-22 的修复：BE 的基准取
        #   max(tp1_hit_ts, ema_warning_ts, partial_close_ts)，因为 EMA 预警
        #   推的保本可能早于 TP1，原口径会误杀 EMA 分支）。文案变了，断言没跟上。
        self.assertIn("BE 必须晚于保本移动", str(ctx.exception))
        # 关键语义不变：仍然必须 raise，且提到 tp1_hit_ts
        self.assertIn("tp1_hit_ts=10100", str(ctx.exception))

    def test_close_accepts_normal_be_stopped(self):
        """正常 BE_STOPPED：exit_ts > fill_ts 且 > tp1_hit_ts → 不 raise"""
        sig = {
            "id": "sanity-3",
            "side": "long",
            "entry": 100.0,
            "fill_ts": 10000,
            "fill_px": 100.0,
            "tp1_hit": True,
            "tp1_hit_ts": 10100,
            "tp1_hit_px": 101.0,
        }
        # exit_ts=10200 应该没问题
        res = pt._close(sig, "BE_STOPPED", 100.0, 10200, 1000, "long", 100.0, "profit")
        self.assertEqual(res["outcome"], "BE_STOPPED")
        self.assertEqual(res["exit_ts"], 10200)

    def test_close_accepts_sl_without_tp1(self):
        """未触发 TP1 的 SL_STOPPED：只要 exit_ts > fill_ts 即可"""
        sig = {
            "id": "sanity-4",
            "side": "long",
            "entry": 100.0,
            "fill_ts": 10000,
            "fill_px": 100.0,
        }
        res = pt._close(sig, "SL_STOPPED", 99.0, 10100, 1000, "long", 100.0, "loss")
        self.assertEqual(res["outcome"], "SL_STOPPED")

    def test_close_accepts_no_fill_ts(self):
        """fill_ts 缺失（如未进场的 MISSED）→ 不做 sanity check"""
        sig = {
            "id": "sanity-5",
            "side": "long",
            "entry": 100.0,
        }
        # MISSED 没有 fill_ts, exit_ts=1000 没问题
        res = pt._close(sig, "MISSED", 105.0, 1000, 0, "long", 100.0, "skip")
        self.assertEqual(res["outcome"], "MISSED")


class TestSettleAllHoldHRebuild(unittest.TestCase):
    """settle_all 写盘后必须强制按 (exit_ts - fill_ts) 重算 hold_h"""

    def setUp(self):
        self.fill_patches = [
            patch("position_tracker._notify_fill"),
            patch("position_tracker._notify_partial"),
            patch("position_tracker._notify_close"),
            patch("position_tracker._write_journal"),
            patch("position_tracker.fetch_ohlcv", return_value=[]),
        ]
        for p in self.fill_patches:
            p.start()

    def tearDown(self):
        for p in self.fill_patches:
            p.stop()

    def test_hold_h_rebuilt_from_real_timestamps(self):
        """
        即使 journal 里 hold_h 写错（比如 14.8），settle 后应该按 (exit_ts-fill_ts)/3600 重算。
        背景：10-18 发现 1792199726 hold_h=14.8 但实际 fill_ts=10:02 exit_ts=03:41 应该是 17.6h
        （另一个问题是 exit_ts 异常所以 hold_h 也错，但代码逻辑必须确保 rebuild）
        """
        sig = {
            "id": "hold-h-test",
            "contract": "BTC_USDT",
            "ts": 1000,
            "side": "long",
            "entry": 100.0,
            "sl": 99.0,
            "tp1": 101.0,
            "tp2": 102.0,
            "status": "pending",
            "filled": True,
            "fill_ts": 10000,  # 成交时刻
            "hold_h": 99.9,    # 故意写错
        }
        # 模拟 bars: fill 后立刻 SL 触发
        bars = [
            _bar(10060, 100.0, 100.2, 100.0, 100.1),  # 成交当根 (fill_ts=10000)
            _bar(10100, 99.5, 99.6, 98.5, 98.6),       # SL 触发
        ]
        # _read_journal 模拟返回 sig, fetch_ohlcv 模拟返回 bars
        with patch("position_tracker._read_journal", return_value=[sig]):
            with patch("position_tracker.fetch_ohlcv", return_value=bars):
                pt.settle_all(verbose=False)

        # hold_h 应该是 (10100-10000)/3600 = 0.03
        sig_after = sig
        self.assertEqual(sig_after["status"], "settled")
        self.assertEqual(sig_after["hold_h"], 0.0)  # round(0.0277, 1) = 0.0


if __name__ == "__main__":
    unittest.main(verbosity=2)
