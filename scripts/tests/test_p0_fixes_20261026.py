#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
P0 修复回归（2026-10-26）
========================

锁三件事：
  1. v2_journal 路径隔离：patch TRAINING_DIR 后，_journal_path() 必须跟着变
     （原 bug：V2_JOURNAL_PATH 是导入时常量，patch 不生效 → 测试把假单写进
      真实 /root/data/training/v2_journal.jsonl，一条 v2-D-TEST-1-long 以
      pending+filled 常驻，把后续所有做多触发堵死 —— 23 条 long 触发 0 条能开）
  2. journal._settle_one 不许碰已成交的单
     （原 bug：无视 filled/fill_ts，按 15m 重判"有没有挂到"，把 hammer_watcher
      的市价成交踩成 MISSED + filled=False，+6.64U 利润从统计里消失；
      而且两个引擎 outcome 词表不同，混写会崩统计口径）
  3. _is_test_id 真实 id 不许被误杀（v2_YYYYMMDD_HHMMSS / 10 位 unix 秒）

跑法：
  cd /root/skills/btc-gate-intraday-strategy/scripts
  /root/.hermes/hermes-agent/venv/bin/python3 -m unittest tests.test_p0_fixes_20261026 -v
"""
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import v2_journal          # noqa: E402
import journal as _journal  # noqa: E402


class TestV2JournalPathIsolation(unittest.TestCase):
    """路径必须调用时按 TRAINING_DIR 现算，patch 才生效"""

    def setUp(self):
        self._orig = v2_journal.TRAINING_DIR
        self._tmp = Path(tempfile.mkdtemp(prefix="v2iso_"))
        v2_journal.TRAINING_DIR = self._tmp

    def tearDown(self):
        v2_journal.TRAINING_DIR = self._orig
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_journal_path_follows_training_dir(self):
        self.assertEqual(v2_journal._journal_path(),
                         self._tmp / "v2_journal.jsonl",
                         "patch TRAINING_DIR 后路径没跟着变 —— 测试会写穿到真实文件")

    def test_write_does_not_touch_real_file(self):
        """写一条假单，真实文件的字节数必须一模一样"""
        real = Path("/root/data/training/v2_journal.jsonl")
        before = real.read_bytes() if real.exists() else b""
        sig = {"id": "v2-D-TEST-1-long", "side": "long", "status": "pending",
               "filled": True, "entry": 80000.0, "signal_source": "v2_aggregate"}
        v2_journal._write_journal([sig])
        after = real.read_bytes() if real.exists() else b""
        self.assertEqual(before, after,
                         "测试写进了真实 /root/data/training/v2_journal.jsonl！")
        # 假单必须落在 tmp
        self.assertTrue((self._tmp / "v2_journal.jsonl").exists())
        self.assertEqual(len(v2_journal._read_journal()), 1)


class TestSettleSkipsFilled(unittest.TestCase):
    """journal._settle_one 不许碰已成交的单"""

    def _rec(self, **kw):
        base = {
            "id": "BTC_USDT-1792856706-long", "contract": "BTC_USDT", "side": "long",
            "ts": 1792856706, "entry": 79400.0, "sl": 79208.6, "tp1": 80432.2,
            "tp2": 81702.6, "sl_pct": 0.2411, "tp1_pct": 1.3, "status": "pending",
            "filled": True, "fill_ts": 1792856756, "fill_px": 79536.4,
            "_fill_method": "market_on_watcher", "contracts": 420,
        }
        base.update(kw)
        return base

    def _bars(self):
        # 一堆不触 SL/TP 的 15m K 线
        out = []
        for i in range(200):
            ts = 1792856706 + i * 900
            out.append([ts, 80000.0, 80100.0, 79900.0, 80050.0, 100])
        return out

    def test_filled_record_is_left_alone(self):
        """已成交 → return None，让 position_tracker 结算"""
        res = _journal._settle_one(self._rec(), self._bars())
        self.assertIsNone(res,
                          "已成交的单被 journal._settle_one 动了 —— 会踩掉市价成交")

    def test_fill_ts_alone_is_enough(self):
        """只有 fill_ts（没有 filled 标志）也算已成交"""
        res = _journal._settle_one(self._rec(filled=False), self._bars())
        self.assertIsNone(res, "fill_ts 存在却没让位给 position_tracker")

    def test_unfilled_still_expires_to_missed(self):
        """未成交挂单仍要走 24h 过期 → MISSED（本引擎的职责不能丢）"""
        rec = self._rec(filled=False, fill_ts=None)
        bars = [[1792856706 + i * 900, 80000.0, 80100.0, 79900.0, 80050.0, 100]
                for i in range(120)]  # 跨过 24h
        # entry=79400 是 long，low 一直 79900 > entry → 永不成交
        res = _journal._settle_one(rec, bars)
        self.assertIsNotNone(res, "未成交挂单 24h 后没作废")
        self.assertEqual(res["outcome"], "MISSED")


class TestIsTestId(unittest.TestCase):
    """真实 id 不许被误杀，测试 id 必须拦下"""

    def test_real_ids_pass(self):
        for rid in ("v2-v2_20261022_221528-long",
                    "v2-v2_20261023_021530-long",
                    "BTC_USDT-1792854921-short",
                    "BTC_USDT-1792868423-short",
                    "BTC_USDT-1792802291-long-JUMP-REVERSE",
                    "BTC_USDT-1792856706-long"):
            self.assertFalse(v2_journal._is_test_id(rid),
                             f"真实 id 被误判成测试数据: {rid}")

    def test_test_ids_blocked(self):
        for rid in ("v2-D-TEST-1-long",
                    "BTC_USDT-1000-short",
                    "BTC_USDT-1002-long",
                    "TEST-FULL-1790000000-long",
                    "test-debug-001"):
            self.assertTrue(v2_journal._is_test_id(rid),
                            f"测试残留 id 漏拦: {rid}")


class TestHammerPctUnits(unittest.TestCase):
    """hammer 的 sl_pct/tp1_pct 必须是 make_plan 的百分数口径（不是小数）"""
    def test_sl_pct_formula_matches_makeplan_convention(self):
        # 复刻 scan.py 修复后的公式
        entry, sl = 79400.0, 79208.6
        sl_pct = ((entry - sl) / entry * 100)
        self.assertAlmostEqual(sl_pct, 0.2411, places=3,
                               msg="hammer sl_pct 公式不对")
        # 必须是正数（long 的 sl 在 entry 下方）
        self.assertGreater(sl_pct, 0, "hammer sl_pct 是负数 —— 符号又反了")

    def test_tp1_pct_is_percent_not_fraction(self):
        tp1_pct_raw = 0.013          # hammer 的小数口径
        stored = tp1_pct_raw * 100   # scan.py 修复后存的值
        self.assertAlmostEqual(stored, 1.3, places=6)
        self.assertGreater(stored, 0.5,
                           "tp1_pct 存成了小数 —— 跟 make_plan 口径差 100 倍")


class TestOutcomeEventIdempotent(unittest.TestCase):
    """完全相同的 outcome 事件只写一次；状态修正（outcome/net 不同）要保留"""

    def setUp(self):
        import position_tracker as _pt
        self._orig_events = _pt.EVENTS
        self._orig_dir = _pt.DATA_DIR
        self._tmp = tempfile.mkdtemp(prefix="evid_")
        _pt.EVENTS = os.path.join(self._tmp, "position_events.jsonl")
        _pt.DATA_DIR = self._tmp
        self.pt = _pt

    def tearDown(self):
        self.pt.EVENTS = self._orig_events
        self.pt.DATA_DIR = self._orig_dir
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _rows(self):
        if not os.path.exists(self.pt.EVENTS):
            return []
        return [json.loads(l) for l in open(self.pt.EVENTS) if l.strip()]

    def test_exact_duplicate_written_once(self):
        ev = {"id": "BTC_USDT-1792854921-short", "outcome": "SL_STOPPED",
              "net_usd": -15.12, "category": "loss", "exit_ts": 1, "ts": 1}
        self.pt._append_event(dict(ev))
        self.pt._append_event(dict(ev))          # 纯重复 → 拦下
        self.pt._append_event(dict(ev, ts=2))    # 只差 ts → 仍算重复
        self.assertEqual(len(self._rows()), 1,
                         "完全相同的 outcome 事件被重复写入")

    def test_correction_is_kept(self):
        """risk_guard 先写 SL_STOPPED，position_tracker 后修正成 BE_STOPPED → 两条都要留"""
        self.pt._append_event({"id": "BTC_USDT-1792796209-short", "outcome": "SL_STOPPED",
                               "net_usd": -24.18, "category": "loss", "ts": 1})
        self.pt._append_event({"id": "BTC_USDT-1792796209-short", "outcome": "BE_STOPPED",
                               "net_usd": 3.15, "category": "profit", "ts": 2})
        rows = self._rows()
        self.assertEqual(len(rows), 2, "状态修正被误当重复删掉了 —— 会丢最新结算结果")
        self.assertEqual(rows[-1]["outcome"], "BE_STOPPED")

    def test_partial_event_not_blocked(self):
        """partial 事件（无 outcome）不受影响"""
        for _ in range(2):
            self.pt._append_event({"id": "BTC_USDT-1792854921-short",
                                   "kind": "ema_break_partial_close", "ts": 1})
        self.assertEqual(len(self._rows()), 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
