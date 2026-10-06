#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
watch/settler.py 单元测试
==========================

覆盖 settler_loop 心跳逻辑、is_alive() 三种状态（True / False / None）。
"""
import os, sys, json, tempfile, time, unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from watch import settler
from watch.settler import _write_heartbeat, is_alive, HEARTBEAT_EVERY, HEARTBEAT_PATH


class TestHeartbeat(unittest.TestCase):
    """心跳写读基本功能"""

    def setUp(self):
        # 用临时心跳文件
        self.tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False)
        self.tmp.close()
        self._orig = settler.HEARTBEAT_PATH
        settler.HEARTBEAT_PATH = Path(self.tmp.name)

    def tearDown(self):
        try:
            os.unlink(self.tmp.name)
        except FileNotFoundError:
            pass
        settler.HEARTBEAT_PATH = self._orig

    def test_write_heartbeat_creates_file(self):
        _write_heartbeat(n_iter=10, n_done_total=2, n_still=3, last_run_ts=time.time())
        self.assertTrue(os.path.exists(self.tmp.name))

    def test_write_heartbeat_fields(self):
        _write_heartbeat(n_iter=42, n_done_total=5, n_still=7, last_run_ts=1234567890.0)
        with open(self.tmp.name) as f:
            hb = json.load(f)
        self.assertEqual(hb["n_iter"], 42)
        self.assertEqual(hb["n_done_total"], 5)
        self.assertEqual(hb["n_still"], 7)
        self.assertEqual(hb["last_run_ts"], 1234567890.0)
        self.assertTrue(hb["alive"])

    def test_write_heartbeat_atomic(self):
        """写心跳用 .tmp + rename，原子性"""
        _write_heartbeat(1, 0, 0, time.time())
        # 不应该有 .tmp 残留
        self.assertFalse(os.path.exists(self.tmp.name + ".tmp"))


class TestIsAlive(unittest.TestCase):
    """is_alive() 三种状态"""

    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False)
        self.tmp.close()
        self._orig = settler.HEARTBEAT_PATH
        settler.HEARTBEAT_PATH = Path(self.tmp.name)

    def tearDown(self):
        try:
            os.unlink(self.tmp.name)
        except FileNotFoundError:
            pass
        settler.HEARTBEAT_PATH = self._orig

    def test_no_file_returns_none(self):
        os.unlink(self.tmp.name)
        # 重新设为不存在的路径
        settler.HEARTBEAT_PATH = Path(self.tmp.name)
        result = is_alive(max_age_sec=60)
        self.assertIsNone(result)

    def test_fresh_heartbeat_returns_true(self):
        # 当前 ts = alive
        _write_heartbeat(100, 5, 2, time.time())
        self.assertTrue(is_alive(max_age_sec=60))

    def test_stale_heartbeat_returns_false(self):
        # 直接写一个 ts=100 秒前的 JSON 文件
        with open(self.tmp.name, "w") as f:
            json.dump({"ts": int(time.time()) - 100, "n_iter": 1, "n_done_total": 0, "n_still": 0, "alive": True}, f)
        self.assertFalse(is_alive(max_age_sec=60))

    def test_boundary_max_age(self):
        """边界：61 秒前的心跳应该被认为过期"""
        with open(self.tmp.name, "w") as f:
            json.dump({"ts": int(time.time()) - 61, "n_iter": 1, "n_done_total": 0, "n_still": 0, "alive": True}, f)
        self.assertFalse(is_alive(max_age_sec=60))

    def test_corrupted_json_returns_none(self):
        with open(self.tmp.name, "w") as f:
            f.write("not json {{{")
        result = is_alive(max_age_sec=60)
        self.assertIsNone(result)


class TestSettlerLoopHeartbeat(unittest.TestCase):
    """settler_loop 内部：每轮写心跳 + 5 分钟打日志心跳"""

    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False)
        self.tmp.close()
        self._orig = settler.HEARTBEAT_PATH
        settler.HEARTBEAT_PATH = Path(self.tmp.name)

    def tearDown(self):
        try:
            os.unlink(self.tmp.name)
        except FileNotFoundError:
            pass
        settler.HEARTBEAT_PATH = self._orig

    def test_settler_loop_writes_heartbeat_each_iteration(self):
        """settler_loop 每跑一轮应该写心跳"""
        from watch.settler import settler_loop
        from unittest.mock import patch
        import threading

        # 让 settle_all 立刻返回 (0, 0)，sleep 也很短
        with patch("position_tracker.settle_all", return_value=(0, 0)), \
             patch("watch.settler.time.sleep", return_value=None), \
             patch("watch.settler.log"):
            # 跑一轮然后退出
            def fake_sleep(_):
                raise StopIteration("跳出循环")
            with patch("watch.settler.time.sleep", fake_sleep):
                try:
                    settler_loop()
                except StopIteration:
                    pass
        # 心跳文件应该存在
        self.assertTrue(os.path.exists(self.tmp.name))
        with open(self.tmp.name) as f:
            hb = json.load(f)
        self.assertGreaterEqual(hb["n_iter"], 1)

    def test_settler_loop_accumulates_n_done_total(self):
        """多次结算的累计 n_done_total 正确"""
        from watch.settler import settler_loop
        import threading

        call_count = [0]
        def fake_settle_all(verbose=False):
            call_count[0] += 1
            return (2, 1) if call_count[0] == 1 else (0, 1)

        # 7 次 sleep：前 6 次 None 跑 6 轮，第 7 次 StopIteration 退出
        # 第 7 轮仍然跑了（settle_all → n_iter=7），然后 sleep 抛异常
        with patch("position_tracker.settle_all", side_effect=fake_settle_all), \
             patch("watch.settler.time.sleep", side_effect=[None]*6 + [StopIteration()]), \
             patch("watch.settler.log"):
            try:
                settler_loop()
            except StopIteration:
                pass

        with open(self.tmp.name) as f:
            hb = json.load(f)
        # 第 1 轮 n_done=2 → n_done_total=2; 第 2-7 轮 n_done=0 → 还是 2
        self.assertEqual(hb["n_done_total"], 2)
        self.assertEqual(hb["n_iter"], 7)

    def test_settler_loop_survives_exception(self):
        """settle_all 抛异常时，循环不应退出"""
        from watch.settler import settler_loop

        call_count = [0]
        def fake_settle_all(verbose=False):
            call_count[0] += 1
            if call_count[0] == 1:
                raise RuntimeError("模拟异常")
            return (0, 0)

        # 异常被 except 捕获，循环继续；最后 sleep 抛 StopIteration 退出
        # 异常那一轮没写心跳（n_iter 没增）
        with patch("position_tracker.settle_all", side_effect=fake_settle_all), \
             patch("watch.settler.time.sleep", side_effect=[None]*6 + [StopIteration()]), \
             patch("watch.settler.log"):
            try:
                settler_loop()
            except StopIteration:
                pass

        with open(self.tmp.name) as f:
            hb = json.load(f)
        # 异常轮 n_iter 不增（仍 0）；后续 6 轮 n_iter=1..6
        # 实际：异常 → except 不增 n_iter → sleep → 第 2 轮 n_iter=1 → ... → 第 7 轮 StopIteration
        # 所以 n_iter=6（7 次循环，1 次异常不算）
        self.assertEqual(hb["n_iter"], 6)
        # settle_all 调用 7 次（1 异常 + 6 正常）
        self.assertEqual(call_count[0], 7)


if __name__ == "__main__":
    unittest.main(verbosity=2)
