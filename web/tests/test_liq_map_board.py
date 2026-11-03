#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
清算簇网页 payload + 判读参考测试（2026-11-02，liq_map_board.py + server._mm_advice）

锁住的口径：
  1. **网页绝不向上游发请求** —— 上游 IP 配额限流（2026-11-02 15:20 实测被封），
     网页只读采集器已落盘的本地快照
  2. 无快照时 available=False + 明确 reason，不编造
  3. 过期快照要标 stale（采集器 10min 一次 → >20min 未更新 = 异常）
  4. 交易员判读要把清算簇当**参考输入**（不是方向建议），簇价格要进白名单
  5. prompt 里必须有硬约束：清算簇不是目标位、不是止损依据、不得据此下结论
  6. 坏行跳过，不让一行坏数据毁掉整个序列
"""
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "web"))
import liq_map_board as L  # noqa: E402

os.environ.setdefault("MIMO_API_KEY", "test-key")
import server as server_mod  # noqa: E402  (导入 server 需要 MIMO_API_KEY 先设好)


def _row(ts, px=78000.0, score=-0.3, label="偏空",
         above=None, below=None):
    return {
        "ts": ts, "ts_iso": "2026-11-02 12:00:00", "px": px, "source": "coinank-agg",
        "coverage_pct": [-4.0, 4.0],
        "above": above if above is not None else [
            {"price": 78500.0, "strength_pct": 90.0, "dist_pct": 0.6, "exchanges": 6}],
        "below": below if below is not None else [
            {"price": 77500.0, "strength_pct": 70.0, "dist_pct": -0.6, "exchanges": 6}],
        "suggestion": {"label": label, "score": score, "reference_value": 100.0,
                       "reference_side": "下方"},
    }


class TestLiqMapBoard(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self._p = [
            mock.patch.object(L, "STORE", Path(self._tmp) / "s.jsonl"),
            mock.patch.dict(os.environ, {}, clear=False),
        ]
        for p in self._p:
            p.start()
        L._CACHE["ts"] = 0.0
        L._CACHE["data"] = None

    def tearDown(self):
        for p in self._p:
            p.stop()

    def _write(self, rows):
        with open(L.STORE, "w", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")

    def test_no_snapshot_unavailable_honest(self):
        d = L.build_liq_map_board(force=True)
        self.assertFalse(d["available"])
        self.assertIn("还没有清算簇快照", d["reason"])
        self.assertNotIn("above", d)

    def test_builds_from_local_snapshot(self):
        self._write([_row(int(time.time()))])
        d = L.build_liq_map_board(force=True)
        self.assertTrue(d["available"])
        self.assertEqual(d["source"], "local-snapshot")
        self.assertEqual(len(d["above"]), 1)
        self.assertEqual(len(d["below"]), 1)
        self.assertEqual(d["suggestion"]["label"], "偏空")

    def test_stale_flagged_when_old(self):
        # 过期阈值 2h（采集器每小时一次）→ 3 小时前的行才算过期
        self._write([_row(int(time.time()) - 3 * 3600)])
        d = L.build_liq_map_board(force=True)
        self.assertTrue(d["available"])
        self.assertTrue(d["stale"])
        self.assertGreater(d["age_sec"], L.STALE_AFTER_SEC)

    def test_fresh_within_2h_not_stale(self):
        """每小时一次采集 → 1 小时前的行也算新（不能被误判过期）。"""
        self._write([_row(int(time.time()) - 3600)])
        d = L.build_liq_map_board(force=True)
        self.assertTrue(d["available"])
        self.assertFalse(d["stale"])

    def test_fresh_not_stale(self):
        self._write([_row(int(time.time()) - 60)])
        d = L.build_liq_map_board(force=True)
        self.assertFalse(d["stale"])

    def test_bad_lines_skipped(self):
        self._write([_row(1000), _row(2000)])
        with open(L.STORE, "a", encoding="utf-8") as f:
            f.write("{not json\n")
        d = L.build_liq_map_board(force=True)
        self.assertTrue(d["available"])
        self.assertEqual(d["sample_count"], 2)

    def test_history_capped(self):
        base = int(time.time()) - 5000
        self._write([_row(base + i * 60) for i in range(L.HISTORY_LIMIT + 10)])
        d = L.build_liq_map_board(force=True)
        self.assertLessEqual(len(d["history"]), L.HISTORY_LIMIT)

    def test_cluster_drift_computed(self):
        base = int(time.time()) - 300
        a = _row(base, above=[{"price": 78500, "strength_pct": 90, "dist_pct": 1.0, "exchanges": 6}],
                 below=[{"price": 77500, "strength_pct": 70, "dist_pct": -1.0, "exchanges": 6}])
        b = _row(base + 60, above=[{"price": 78500, "strength_pct": 90, "dist_pct": 0.5, "exchanges": 6}],
                 below=[{"price": 77500, "strength_pct": 70, "dist_pct": -1.5, "exchanges": 6}])
        self._write([a, b])
        d = L.build_liq_map_board(force=True)
        dr = d["cluster_drift"]
        self.assertTrue(dr["above"]["approaching"])   # 距离 1.0 → 0.5，靠近
        self.assertFalse(dr["below"]["approaching"])  # 距离 -1.0 → -1.5，远离

    def test_never_calls_upstream(self):
        """★ 网页端绝不向上游发请求（上游 IP 配额限流）。"""
        self._write([_row(int(time.time()))])
        with mock.patch("urllib.request.urlopen",
                        side_effect=AssertionError("网页端不该发 HTTP 请求")) as m:
            d = L.build_liq_map_board(force=True)
        self.assertTrue(d["available"])
        self.assertEqual(m.call_count, 0)


class TestPromptIntegration(unittest.TestCase):
    """交易员判读把清算簇当参考输入（不是方向建议）。"""

    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self._p = [
            mock.patch.object(L, "STORE", Path(self._tmp) / "s.jsonl"),
        ]
        for p in self._p:
            p.start()
        L._CACHE["ts"] = 0.0
        L._CACHE["data"] = None
        # 强制 MIMO_API_KEY 存在（否则 _mm_advice 直接返回 None）
        os.environ["MIMO_API_KEY"] = "test-key"

    def tearDown(self):
        for p in self._p:
            p.stop()
        os.environ.pop("MIMO_API_KEY", None)

    def _ma_payload(self):
        return {
            "ts_str": "2026-11-02 16:00:00", "last_px": 78148.0,
            "trend": {"15m": "down", "1h": "flat", "4h": "up", "convergence": "分歧",
                      "ema20_1h": 78200.0, "ema50_1h": 78500.0,
                      "ema20_4h": 77800.0, "ema50_4h": 77000.0},
            "volatility": {"atr_pct": 0.42, "regime": "normal"},
            "key_levels": {"high_24h": 79100.0, "low_24h": 77500.0,
                           "dist_to_high_pct": 1.2, "dist_to_low_pct": 0.83,
                           "near_resistance": 78960, "near_support": 77500,
                           "pos_in_range_pct": 40.5},
            "funding": {"current": 0.000078, "sign": "long_pay"},
            "structure": {"1h": {"label": "下跌段·反弹", "bias": "down",
                                 "pos_in_leg": 35, "lo": 77500, "hi": 79100}},
            "momentum": {"1h": {"macd": "dead cross", "rsi": 42, "rsi_state": "neutral"}},
            "volume": {"1h": {"regime": "normal"}},
            "voting": {"bias": "down", "consensus": 0.6, "score": -1},
        }

    def _capture_prompt(self, ns):
        import urllib.request
        captured = {}

        class FakeResp:
            def __init__(self, b):
                self._b = b

            def read(self):
                return self._b

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def fake(req, timeout=15):
            captured["body"] = json.loads(req.data.decode())
            return FakeResp(json.dumps(
                {"choices": [{"message": {"content": "测试判读"}}]}).encode())

        with mock.patch.object(urllib.request, "urlopen", side_effect=fake):
            server_mod._mm_advice(self._ma_payload(), ns=ns)
        return captured["body"]["messages"][1]["content"]

    def test_liq_in_prompt(self):
        import server as server_mod
        with open(L.STORE, "w", encoding="utf-8") as f:
            f.write(json.dumps(_row(int(time.time()))) + "\n")
        prompt = self._capture_prompt("t1")
        self.assertIn("清算簇参考", prompt)
        self.assertIn("上方=空单清算簇", prompt)
        self.assertIn("下方=多单清算簇", prompt)
        self.assertIn("上行磁吸", prompt)
        self.assertIn("下行瀑布", prompt)

    def test_cluster_price_in_whitelist(self):
        import server as server_mod
        with open(L.STORE, "w", encoding="utf-8") as f:
            f.write(json.dumps(_row(int(time.time()))) + "\n")
        prompt = self._capture_prompt("t2")
        # 78500 / 77500 是 fixture 的簇价格 → 必须进 allowed_levels
        self.assertIn("清算簇上方空单 78500", prompt)
        self.assertIn("清算簇下方多单 77500", prompt)

    def test_prompt_says_reference_only(self):
        import server as server_mod
        with open(L.STORE, "w", encoding="utf-8") as f:
            f.write(json.dumps(_row(int(time.time()))) + "\n")
        prompt = self._capture_prompt("t3")
        # 必须有硬约束：不是目标位、不是止损依据、不得据此下结论
        self.assertIn("不是目标位", prompt)
        self.assertIn("不是止损依据", prompt)
        self.assertIn("不得据此下结论", prompt)
        self.assertIn("还没被回测验证过预测力", prompt)

    def test_no_liq_data_still_works(self):
        """无清算簇数据 → prompt 照常生成，不带清算簇段落。"""
        import server as server_mod
        if os.path.exists(L.STORE):
            os.remove(L.STORE)
        prompt = self._capture_prompt("t4")
        self.assertNotIn("清算簇参考", prompt)
        self.assertIn("以下是 BTC 当前行情数据", prompt)

    def test_upstream_never_called_for_prompt(self):
        """★ 判读生成时也不许向上游发请求（读本地快照）。"""
        import urllib.request
        import server as server_mod
        with open(L.STORE, "w", encoding="utf-8") as f:
            f.write(json.dumps(_row(int(time.time()))) + "\n")

        calls = []

        class FakeResp:
            def __init__(self, b):
                self._b = b

            def read(self):
                return self._b

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def fake(req, timeout=15):
            calls.append(getattr(req, "full_url", ""))
            return FakeResp(json.dumps(
                {"choices": [{"message": {"content": "x"}}]}).encode())

        with mock.patch.object(urllib.request, "urlopen", side_effect=fake):
            server_mod._mm_advice(self._ma_payload(), ns="t5")
        self.assertEqual(len(calls), 1)   # 只有 mimo 一个请求
        self.assertIn("xiaomimimo", calls[0])
        self.assertNotIn("coinank", calls[0])


if __name__ == "__main__":
    unittest.main(verbosity=2)
