#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
position_tracker.force_close_position 单元测试
==============================================

覆盖三种场景：
1. 平掉已成交多单（盈利）
2. 平掉已成交多单（亏损）
3. 反向信号触发 → 平多 + 立即写空头 journal
"""
import os, sys, json, tempfile, unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class TestForceClose(unittest.TestCase):
    """force_close_position 基础场景"""

    def setUp(self):
        # 临时 journal 文件
        self.tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False)
        self.tmp.close()
        self._orig_journal = None
        # patch position_tracker.JOURNAL_PATH 用临时文件
        import position_tracker
        self._pt = position_tracker
        # 找 journal 路径常量
        for attr in dir(position_tracker):
            if attr.upper() == "JOURNAL_PATH":
                self._orig_journal = getattr(position_tracker, attr)
                setattr(position_tracker, attr, Path(self.tmp.name))
                break
        # _read_journal/_write_journal 直接读 Path，需要 patch
        self._orig_read = position_tracker._read_journal
        self._orig_write = position_tracker._write_journal
        position_tracker._read_journal = lambda: [
            json.loads(ln.strip()) for ln in open(self.tmp.name) if ln.strip()
        ] if os.path.exists(self.tmp.name) else []
        position_tracker._write_journal = lambda recs: (
            open(self.tmp.name, "w").write("\n".join(json.dumps(r, ensure_ascii=False) for r in recs) + "\n")
            if recs else open(self.tmp.name, "w").close()
        )

    def tearDown(self):
        try:
            os.unlink(self.tmp.name)
        except FileNotFoundError:
            pass
        # 还原
        import position_tracker
        position_tracker._read_journal = self._orig_read
        position_tracker._write_journal = self._orig_write
        if self._orig_journal is not None:
            for attr in dir(position_tracker):
                if attr.upper() == "JOURNAL_PATH":
                    setattr(position_tracker, attr, self._orig_journal)
                    break

    def _seed_filled_long(self, entry=84638.2, exit_px=85038.2, tp1_hit=True, contracts=591):
        """塞一条「已成交未平仓」多单到临时 journal
        语义（2026-10-05 修正）：position_tracker 用 status="pending" + filled=True
        表示已成交未平仓，不再用 status="filled"。
        """
        rec = {
            "id": "BTC_USDT-1791060667-long",
            "contract": "BTC_USDT",
            "side": "long",
            "entry": entry,
            "tp1": 85038.2,
            "tp2": 85238.2,
            "sl": 83961.0,
            "contracts": contracts,
            "filled": True,
            "fill_ts": 1791061500,
            "fill_px": entry,
            "status": "pending",    # ← 改为 pending + filled=True（真实生产语义）
            "tp1_hit": tp1_hit,
            "tp1_hit_px": exit_px if tp1_hit else None,
            "tp1_hit_ts": 1791094920 if tp1_hit else None,
            "pos_state": "TP1_PARTIAL" if tp1_hit else "OPEN",
            "active_sl": entry if tp1_hit else 83961.0,
        }
        with open(self.tmp.name, "w") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    def test_force_close_profit(self):
        """TP1 已触发后平仓盈利"""
        self._seed_filled_long(entry=84638.2, tp1_hit=True)
        with patch.object(self._pt, "_notify_close", return_value=True), \
             patch.object(self._pt, "_append_event", return_value=None):
            res = self._pt.force_close_position(
                exit_px=84700.0,
                reason="测试盈利平仓",
                verbose=True,
            )
        # 验证
        self.assertIsNotNone(res)
        self.assertEqual(res["outcome"], "MCLOSE")
        self.assertEqual(res["status"], "settled")
        # 1/3 已 TP1 @ 85038.2 + 2/3 @ 84700 = 应该盈利
        self.assertGreater(res["net_usd"], 0)
        # journal 应被更新
        with open(self.tmp.name) as f:
            saved = json.loads(f.readline())
        self.assertEqual(saved["status"], "settled")
        self.assertEqual(saved["note"], "测试盈利平仓")

    def test_force_close_loss(self):
        """未触 TP1 时平仓亏损"""
        self._seed_filled_long(entry=84638.2, tp1_hit=False)
        with patch.object(self._pt, "_notify_close", return_value=True), \
             patch.object(self._pt, "_append_event", return_value=None):
            res = self._pt.force_close_position(
                exit_px=83900.0,   # 比 entry 低 738 点
                reason="测试亏损平仓",
            )
        self.assertIsNotNone(res)
        self.assertEqual(res["outcome"], "MCLOSE")
        # 应该亏损
        self.assertLess(res["net_usd"], 0)

    def test_force_close_no_filled_returns_none(self):
        """没有 filled 单时返回 None"""
        with patch.object(self._pt, "_notify_close", return_value=True), \
             patch.object(self._pt, "_append_event", return_value=None):
            res = self._pt.force_close_position(exit_px=84700.0, reason="测试")
        self.assertIsNone(res)

    def test_force_close_by_signal_id(self):
        """按 signal_id 精确匹配"""
        # 塞两条「已成交未平仓」（同方向不同 id）—— status="pending" + filled=True
        rec1 = {
            "id": "AAA-long", "side": "long", "entry": 84000,
            "tp1_hit": False, "status": "pending", "filled": True,
            "contracts": 100, "fill_ts": 1791000000, "fill_px": 84000,
            "active_sl": 83500,
        }
        rec2 = {
            "id": "BBB-long", "side": "long", "entry": 84500,
            "tp1_hit": False, "status": "pending", "filled": True,
            "contracts": 200, "fill_ts": 1791000100, "fill_px": 84500,
            "active_sl": 84000,
        }
        with open(self.tmp.name, "w") as f:
            f.write(json.dumps(rec1) + "\n")
            f.write(json.dumps(rec2) + "\n")

        with patch.object(self._pt, "_notify_close", return_value=True), \
             patch.object(self._pt, "_append_event", return_value=None):
            res = self._pt.force_close_position(
                exit_px=84600.0, reason="指定 AAA 平仓",
                signal_id="AAA-long",
            )
        self.assertIsNotNone(res)
        self.assertEqual(res["id"], "AAA-long")
        # BBB 不应被影响
        with open(self.tmp.name) as f:
            lines = f.readlines()
        self.assertEqual(len(lines), 2)
        bbb = json.loads(lines[1])
        self.assertEqual(bbb["status"], "pending")    # 没被平（仍是持仓跟踪中）

    def test_force_close_latest_filled(self):
        """use_latest_filled=True 时取最新已成交"""
        rec1 = {
            "id": "OLD-long", "side": "long", "entry": 84000,
            "tp1_hit": False, "status": "pending", "filled": True,
            "contracts": 100, "fill_ts": 1791000000, "fill_px": 84000,
            "active_sl": 83500,
        }
        rec2 = {
            "id": "NEW-long", "side": "long", "entry": 84500,
            "tp1_hit": False, "status": "pending", "filled": True,
            "contracts": 200, "fill_ts": 1791000100, "fill_px": 84500,
            "active_sl": 84000,
        }
        with open(self.tmp.name, "w") as f:
            f.write(json.dumps(rec1) + "\n")
            f.write(json.dumps(rec2) + "\n")

        with patch.object(self._pt, "_notify_close", return_value=True), \
             patch.object(self._pt, "_append_event", return_value=None):
            res = self._pt.force_close_position(exit_px=84600.0, reason="默认最新")
        self.assertEqual(res["id"], "NEW-long")


class TestForceCloseIntegrationWithJump(unittest.TestCase):
    """
    JumpTracker 反方向信号场景测试
    模拟：已有 filled 多单 → jump 触发 → scan 建议做空 → 应自动平多 + 写空头 journal
    """

    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False)
        self.tmp.close()
        import position_tracker
        self._pt = position_tracker
        self._orig_read = position_tracker._read_journal
        self._orig_write = position_tracker._write_journal
        position_tracker._read_journal = lambda: [
            json.loads(ln.strip()) for ln in open(self.tmp.name) if ln.strip()
        ] if os.path.exists(self.tmp.name) else []
        position_tracker._write_journal = lambda recs: (
            open(self.tmp.name, "w").write("\n".join(json.dumps(r, ensure_ascii=False) for r in recs) + "\n")
            if recs else open(self.tmp.name, "w").close()
        )

    def tearDown(self):
        try:
            os.unlink(self.tmp.name)
        except FileNotFoundError:
            pass
        import position_tracker
        position_tracker._read_journal = self._orig_read
        position_tracker._write_journal = self._orig_write

    def test_reverse_signal_workflow(self):
        """反向信号工作流：平多单 + 写空头 journal（不受 500 点限制）"""
        # 1. 已有「已成交未平仓」多单
        existing = {
            "id": "EXISTING-long",
            "side": "long",
            "entry": 84638.2,
            "tp1": 85038.2, "tp2": 85238.2, "sl": 83961.0,
            "contracts": 591,
            "filled": True,
            "fill_ts": 1791061500, "fill_px": 84638.2,
            "status": "pending",    # ← 真实生产语义：pending + filled=True
            "tp1_hit": False,
            "active_sl": 83961.0,
        }
        with open(self.tmp.name, "w") as f:
            f.write(json.dumps(existing) + "\n")

        # 2. 模拟 jump 发现反方向信号 → 调 force_close
        with patch.object(self._pt, "_notify_close", return_value=True), \
             patch.object(self._pt, "_append_event", return_value=None):
            close_res = self._pt.force_close_position(
                exit_px=84697.6,
                reason="反方向信号（短空头）触发，自动平多",
                signal_id="EXISTING-long",
            )

        self.assertIsNotNone(close_res)
        self.assertEqual(close_res["status"], "settled")
        self.assertEqual(close_res["outcome"], "MCLOSE")

        # 3. 写新反方向 journal（这里我们手工模拟 jump 的写入逻辑）
        # 注意：jump.py 写 journal 后会调 config.should_skip_new_signal
        # 但反方向场景应跳过该规则（用户要求）
        import time as _time
        new_signal = {
            "id": "NEW-short",
            "side": "short",
            "entry": 84700.0,    # 反方向入场价
            "tp1": 84500.0, "tp2": 84400.0, "sl": 84800.0,
            "contracts": 591,
            "filled": False, "fill_ts": None,
            "status": "pending",
            "ts": int(_time.time()),
        }
        with open(self.tmp.name, "a") as f:
            f.write(json.dumps(new_signal) + "\n")

        # 4. 验证 journal 状态
        with open(self.tmp.name) as f:
            recs = [json.loads(ln) for ln in f if ln.strip()]
        self.assertEqual(len(recs), 2)
        self.assertEqual(recs[0]["status"], "settled")  # 多单已平
        self.assertEqual(recs[0]["outcome"], "MCLOSE")
        self.assertEqual(recs[1]["status"], "pending")  # 空头挂单
        self.assertEqual(recs[1]["side"], "short")


if __name__ == "__main__":
    unittest.main(verbosity=2)
