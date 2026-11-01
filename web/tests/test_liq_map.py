#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
清算地图取数模块测试（2026-11-01，CoinAnk liq_map.py）

锁住的口径：
  1. 无 COINANK_API_KEY → available=False 如实说明，绝不编造数据
  2. 簇提取方向归一：现价上方/下方分开、±0.2% 近带排除、±10% 范围限制
  3. 强度是相对值（占全图最大桶 %），不是绝对张数
  4. 畸形上游响应（空/缺字段/类型错）→ available=False 不抛错
  5. 文件缓存 TTL 生效（跨 scan 子进程）
"""
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "scripts"))
import liq_map  # noqa: E402

FIXTURE = {
    "success": True, "code": "1",
    "data": {
        "symbol": "BTCUSDT",
        "prices": [79000, 79500, 80000, 80500, 81000, 82000, 83000],
        "lastPrice": 80000,
        "lastIndex": 2,
        # 各杠杆强度（与 prices 对齐）→ 合计：[50, 100, 20, 300, 50, 10, 200]
        "x25":  [10, 20, 5, 60, 10, 2, 40],
        "x50":  [20, 40, 5, 120, 20, 4, 80],
        "x100": [20, 40, 10, 120, 20, 4, 80],
    },
}


class FakeResp:
    def __init__(self, body):
        self._b = json.dumps(body).encode()

    def read(self):
        return self._b

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _with_key():
    return mock.patch.dict(os.environ, {"COINANK_API_KEY": "test-key"})


class TestNoKey(unittest.TestCase):
    def test_no_key_unavailable_and_honest(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("COINANK_API_KEY", None)
            r = liq_map.liq_reference(80000)
        self.assertFalse(r["available"])
        self.assertIn("COINANK_API_KEY", r["reason"])
        self.assertIn("不可用", liq_map.format_reference(r))


class TestParsing(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self._cache = os.path.join(self._tmp, "liq_cache.json")
        self._patchers = [
            _with_key(),
            mock.patch.object(liq_map, "CACHE_PATH", Path(self._cache)),
            mock.patch("urllib.request.urlopen", return_value=FakeResp(FIXTURE)),
        ]
        for p in self._patchers:
            p.start()

    def tearDown(self):
        for p in self._patchers:
            p.stop()

    def test_clusters_split_by_side(self):
        r = liq_map.liq_reference(80000)
        self.assertTrue(r["available"])
        # 上方：80500(300, +0.63%) 82000(?) 83000(200, +3.75%)
        #   合计强度 80500=300、81000=50、82000=10、83000=200 → top3: 80500, 83000, 81000
        ab = {c["price"] for c in r["clusters_above"]}
        self.assertIn(80500, ab)
        self.assertIn(83000, ab)
        # 下方：79000(50,-1.25%) 79500(100,-0.63%)
        be = {c["price"] for c in r["clusters_below"]}
        self.assertEqual(be, {79500, 79000})
        # 现价桶（80000）不进任何一侧（近带排除）
        self.assertNotIn(80000, ab | be)

    def test_strength_relative_to_max(self):
        r = liq_map.liq_reference(80000)
        # 最大桶 = 80500(300) → 100%
        top = r["clusters_above"][0]
        self.assertEqual(top["price"], 80500)
        self.assertAlmostEqual(top["strength_pct"], 100.0)
        # 83000 = 200/300 = 66.7%
        c83 = [c for c in r["clusters_above"] if c["price"] == 83000][0]
        self.assertAlmostEqual(c83["strength_pct"], 66.7, places=1)

    def test_range_limit(self):
        # 83000 距现价 +3.75% 在 ±10% 内 → 在；构造更远的应被滤
        r = liq_map.liq_reference(80000)
        for c in r["clusters_above"] + r["clusters_below"]:
            self.assertLessEqual(abs(c["dist_pct"]), 10.0)

    def test_cache_hit_second_call(self):
        liq_map.fetch_liq_map()
        with mock.patch("urllib.request.urlopen",
                        side_effect=AssertionError("不该再请求")):
            r2 = liq_map.fetch_liq_map()
        self.assertTrue(r2.get("cached"))

    def test_malformed_upstream_no_raise(self):
        for bad in ({}, {"success": False}, {"success": True, "data": {}},
                    {"success": True, "data": {"prices": "oops"}}):
            with mock.patch("urllib.request.urlopen", return_value=FakeResp(bad)):
                r = liq_map.liq_reference(80000)
                self.assertFalse(r["available"])

    def test_format_reference_text(self):
        r = liq_map.liq_reference(80000)
        t = liq_map.format_reference(r)
        self.assertIn("清算地图参考", t)
        self.assertIn("上方", t)
        self.assertIn("下方", t)
        self.assertNotIn("undefined", t)


if __name__ == "__main__":
    unittest.main(verbosity=2)
