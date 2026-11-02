#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
清算地图取数模块测试（2026-11-02 改版：CoinAnk 网页版数据源）

锁住的口径：
  1. 前端鉴权算法：base64(seed 前8位挪到末尾 + '|' + (now_ms+salt) + suffix)，
     且必须每次请求现算（有效期 ±4min）
  2. web 源请求头齐套：coinank-apikey / client=web / web-version —— 缺一不可
  3. 畸形上游响应（空/缺字段/prices 类型错）→ available=False 不抛错、绝不编造
  4. 文件缓存 TTL 生效（跨 scan 子进程），坏缓存不污染
  5. 簇检测：原始曲线 $10 一档 → 必须做峰值 + 最小间隔，不能返回「同一片的三档」
  6. 上方=空单清算簇 / 下方=多单清算簇（CoinAnk 官方读图口径）
"""
import base64
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

# 单交易所 fixture：形状与官方 OpenAPI 一致（prices + x25..x100）
SINGLE_FIXTURE = {
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

# 聚合 fixture：6 交易所各一条等长数组
AGG_FIXTURE = {
    "success": True, "code": "1",
    "data": {
        "prices": [79000, 79500, 80000, 80500, 81000, 82000, 83000],
        "lastPrice": 80000,
        "lastIndex": 2,
        "Binance":      [10, 20, 5, 60, 10, 2, 40],
        "Bybit":        [10, 20, 5, 60, 10, 2, 40],
        "Okex":         [10, 20, 5, 60, 10, 2, 40],
        "Aster":        [0, 0, 0, 0, 0, 0, 0],
        "Hyperliquid":  [0, 0, 0, 0, 0, 0, 0],
        "Lighter":      [0, 0, 0, 0, 0, 0, 0],
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


class TestAuthDerivation(unittest.TestCase):
    """前端鉴权算法 —— 复刻 JS 的 Kb()/NR()/zR()/qR()/jR()/FR()。"""

    def _material(self):
        return {"seed": liq_map._AUTH_SEED, "salt": liq_map._AUTH_SALT,
                "suffix": liq_map._AUTH_SUFFIX, "version": liq_map._AUTH_VERSION,
                "client": liq_map._AUTH_CLIENT}

    def test_derivation_shape(self):
        mat = self._material()
        with mock.patch.object(liq_map, "_now", return_value=1700000000.0):
            key = liq_map._derive_apikey(mat)
        decoded = base64.b64decode(key).decode()
        seed = mat["seed"]
        head = seed[:8]
        expected_body = seed.replace(head, "") + head
        expected_ts = str(int(1700000000.0 * 1000) + int(mat["salt"]))
        self.assertEqual(decoded, f"{expected_body}|{expected_ts}{mat['suffix']}")

    def test_key_is_time_based(self):
        """有效期约 ±4 分钟 → 必须每次请求现算，不能缓存。"""
        mat = self._material()
        with mock.patch.object(liq_map, "_now", return_value=1700000000.0):
            k1 = liq_map._derive_apikey(mat)
        with mock.patch.object(liq_map, "_now", return_value=1700000060.0):
            k2 = liq_map._derive_apikey(mat)
        self.assertNotEqual(k1, k2)

    def test_web_headers_complete(self):
        h = liq_map._web_headers(self._material())
        for k in ("coinank-apikey", "client", "web-version"):
            self.assertIn(k, h)
        self.assertEqual(h["client"], "web")
        self.assertEqual(h["web-version"], "102")


class TestFetchValidation(unittest.TestCase):
    """上游响应校验 —— 坏数据必须报不可用，绝不编造。"""

    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self._patchers = [
            mock.patch.object(liq_map, "CACHE_PATH", Path(os.path.join(self._tmp, "c.json"))),
            mock.patch.object(liq_map, "AUTH_CACHE_PATH", Path(os.path.join(self._tmp, "a.json"))),
            # ★ 必须隔离配额状态（真实环境的退避状态会让测试假失败）
            mock.patch.object(liq_map, "QUOTA_STATE_PATH", Path(os.path.join(self._tmp, "q.json"))),
        ]
        for p in self._patchers:
            p.start()

    def tearDown(self):
        for p in self._patchers:
            p.stop()

    def test_malformed_responses_rejected(self):
        for bad in ({}, {"success": False}, {"success": True, "data": {}},
                    {"success": True, "data": {"prices": "oops"}},
                    {"success": True, "data": {"prices": []}},
                    {"success": True, "data": {"prices": [1, "x", 3]}},
                    {"success": True, "data": None}):
            with mock.patch.object(liq_map, "_http_json", return_value=(True, bad)):
                r = liq_map.fetch_liq_map(force=True)
            self.assertFalse(r["available"], f"应被拒绝: {bad}")
            self.assertIn("reason", r)

    def test_valid_payload_accepted(self):
        with mock.patch.object(liq_map, "_http_json", return_value=(True, SINGLE_FIXTURE)):
            r = liq_map.fetch_liq_map(force=True)
        self.assertTrue(r["available"])
        self.assertEqual(r["prices"], SINGLE_FIXTURE["data"]["prices"])

    def test_network_error_no_raise(self):
        with mock.patch.object(liq_map, "_http_json", return_value=(False, {})):
            r = liq_map.fetch_liq_map(force=True)
        self.assertFalse(r["available"])

    def test_cache_hit_and_bad_cache_ignored(self):
        with mock.patch.object(liq_map, "_http_json", return_value=(True, SINGLE_FIXTURE)):
            liq_map.fetch_liq_map()
        with mock.patch.object(liq_map, "_http_json",
                               side_effect=AssertionError("不该再请求")):
            r2 = liq_map.fetch_liq_map()
        self.assertTrue(r2.get("cached"))

        # 坏缓存（prices 类型错）→ 视为未命中，重新请求
        liq_map.CACHE_PATH.write_text(json.dumps({
            "ts": time.time(),
            "data": {"single": {"ts": time.time(), "symbol": "BTCUSDT",
                                "exchange": "Binance", "payload": {"prices": "bad"}}},
        }), encoding="utf-8")
        with mock.patch.object(liq_map, "_http_json", return_value=(True, SINGLE_FIXTURE)):
            r3 = liq_map.fetch_liq_map()
        self.assertTrue(r3["available"])
        self.assertFalse(r3.get("cached"))


class TestClusterDetection(unittest.TestCase):
    """簇检测 —— 原始曲线 $10 一档，必须做峰值 + 间隔，不能返回同一片的三档。"""

    def test_no_near_duplicate_clusters(self):
        # 构造一条「一个尖峰 + 两侧递减」的曲线，峰值档相邻档也很高
        prices = list(range(70000, 70000 + 60 * 10, 10))
        totals = []
        for i, p in enumerate(prices):
            d = abs(p - 70500)
            totals.append(max(0.0, 1000.0 - d * 8))
        clusters = liq_map._find_clusters(prices, totals, 70500, min_sep_usd=200)
        # 同一片只能出一个簇
        self.assertEqual(len(clusters), 1)
        self.assertEqual(clusters[0]["price"], 70500.0)
        self.assertAlmostEqual(clusters[0]["strength_pct"], 100.0)

    def test_two_separate_peaks_found(self):
        prices = list(range(70000, 70000 + 120 * 10, 10))
        totals = []
        for p in prices:
            a = max(0.0, 1000.0 - abs(p - 70100) * 20)
            b = max(0.0, 800.0 - abs(p - 71000) * 20)
            totals.append(max(a, b))
        clusters = liq_map._find_clusters(prices, totals, 70500, min_sep_usd=200)
        got = sorted(c["price"] for c in clusters)
        self.assertIn(70100.0, got)
        self.assertIn(71000.0, got)

    def test_strength_relative_to_global_max(self):
        prices = [79000, 79500, 80000, 80500, 81000, 82000, 83000]
        totals = [50, 100, 20, 300, 50, 10, 200]
        clusters = liq_map._find_clusters(prices, totals, 80000, min_sep_usd=200)
        top = clusters[0]
        self.assertEqual(top["price"], 80500.0)
        self.assertAlmostEqual(top["strength_pct"], 100.0)
        c83 = [c for c in clusters if c["price"] == 83000.0][0]
        self.assertAlmostEqual(c83["strength_pct"], 66.7, places=1)


class TestReference(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self._patchers = [
            mock.patch.object(liq_map, "CACHE_PATH", Path(os.path.join(self._tmp, "c.json"))),
            mock.patch.object(liq_map, "AUTH_CACHE_PATH", Path(os.path.join(self._tmp, "a.json"))),
            # ★ 必须隔离配额状态：真实环境的 liq_map_quota_state.json 会让
            #   所有测试走「退避中」分支 → 拿不到数据 → 测试假失败
            mock.patch.object(liq_map, "QUOTA_STATE_PATH", Path(os.path.join(self._tmp, "q.json"))),
        ]
        for p in self._patchers:
            p.start()

    def tearDown(self):
        for p in self._patchers:
            p.stop()

    def test_reference_splits_sides(self):
        def _fake(path, params):
            if "Agg" in path:
                return True, AGG_FIXTURE
            return True, SINGLE_FIXTURE
        with mock.patch.object(liq_map, "_http_json", side_effect=_fake):
            r = liq_map.liq_reference(80000)
        self.assertTrue(r["available"])
        ab = {c["price"] for c in r["clusters_above"]}
        be = {c["price"] for c in r["clusters_below"]}
        # 上方局部极大：80500(300) / 83000(200)；82000 只有 10 不是峰
        self.assertIn(80500.0, ab)
        self.assertIn(83000.0, ab)
        self.assertNotIn(82000.0, ab)
        # 下方局部极大：79500(100)
        self.assertIn(79500.0, be)
        # 现价桶不进任何一侧
        self.assertNotIn(80000.0, ab | be)

    def test_non_peak_bucket_excluded(self):
        """簇 = 局部极大，不是「强度排名前 N 的档」。

        fixture 合计强度：[79000]=50, [79500]=100, [80000]=20, [80500]=300,
        [81000]=50, [82000]=10, [83000]=200
        → 79000 强度 50 但它在数据边界，曲线边缘不是峰，不应被当成「下方第二簇」。
        """
        prices = [79000, 79500, 80000, 80500, 81000, 82000, 83000]
        totals = [50, 100, 20, 300, 50, 10, 200]
        clusters = liq_map._find_clusters(prices, totals, 80000, min_sep_usd=200)
        got = {c["price"] for c in clusters}
        self.assertIn(79500.0, got)
        self.assertNotIn(79000.0, got)

    def test_above_is_short_below_is_long(self):
        """口径：上方=空单清算簇，下方=多单清算簇。"""
        with mock.patch.object(liq_map, "_http_json", side_effect=lambda p, q: (True, AGG_FIXTURE)):
            r = liq_map.liq_reference(80000)
        for c in r["clusters_above"]:
            self.assertGreater(c["dist_pct"], 0)
        for c in r["clusters_below"]:
            self.assertLess(c["dist_pct"], 0)

    def test_range_limit(self):
        with mock.patch.object(liq_map, "_http_json", side_effect=lambda p, q: (True, AGG_FIXTURE)):
            r = liq_map.liq_reference(80000)
        for c in r["clusters_above"] + r["clusters_below"]:
            self.assertLessEqual(abs(c["dist_pct"]), 10.0)

    def test_unavailable_when_both_fail(self):
        with mock.patch.object(liq_map, "_http_json", return_value=(False, {})):
            r = liq_map.liq_reference(80000)
        self.assertFalse(r["available"])
        self.assertIn("不可用", liq_map.format_reference(r))

    def test_format_reference_text(self):
        with mock.patch.object(liq_map, "_http_json", side_effect=lambda p, q: (True, AGG_FIXTURE)):
            r = liq_map.liq_reference(80000)
        t = liq_map.format_reference(r)
        self.assertIn("清算地图参考", t)
        self.assertIn("上方", t)
        self.assertIn("下方", t)
        self.assertIn("空单", t)
        self.assertIn("多单", t)
        self.assertNotIn("undefined", t)
        self.assertNotIn("None", t)

    def test_coverage_reported(self):
        with mock.patch.object(liq_map, "_http_json", side_effect=lambda p, q: (True, AGG_FIXTURE)):
            r = liq_map.liq_reference(80000)
        cov = r["coverage_pct"]
        self.assertEqual(len(cov), 2)
        self.assertLess(cov[0], 0)
        self.assertGreater(cov[1], 0)


class TestSuggestion(unittest.TestCase):
    """多空建议：参考值 = 上下两侧强度中的数值最大值（liusir 2026-11-02 口径）。"""

    def _c(self, price, raw, dist):
        return {"price": price, "raw": raw, "strength_pct": raw / 1000 * 100, "dist_pct": dist}

    def test_reference_is_max_of_two_sides(self):
        a = [self._c(80500, 300, 0.6)]
        b = [self._c(79500, 100, -0.6)]
        s = liq_map.compute_suggestion(a, b)
        self.assertTrue(s["available"])
        # 参考值 = max(300, 100) = 300
        self.assertEqual(s["reference_value"], 300)
        self.assertEqual(s["reference_side"], "上方")
        # 倾向分 = (300 - 100) / 300 = 0.667
        self.assertAlmostEqual(s["score"], 0.667, places=3)
        self.assertEqual(s["suggestion"], "偏多")

    def test_below_dominant_gives_bearish(self):
        a = [self._c(80500, 100, 0.6)]
        b = [self._c(79500, 300, -0.6)]
        s = liq_map.compute_suggestion(a, b)
        self.assertEqual(s["reference_value"], 300)
        self.assertEqual(s["reference_side"], "下方")
        self.assertAlmostEqual(s["score"], -0.667, places=3)
        self.assertEqual(s["suggestion"], "偏空")

    def test_score_bounded_minus_one_to_one(self):
        """分母是数值最大值 → 得分恒在 [-1, 1]，不会溢出。"""
        for a_raw, b_raw in ((1000, 0), (0, 1000), (100, 100), (999, 1)):
            s = liq_map.compute_suggestion(
                [self._c(80500, a_raw, 0.6)], [self._c(79500, b_raw, -0.6)])
            self.assertGreaterEqual(s["score"], -1.0)
            self.assertLessEqual(s["score"], 1.0)

    def test_equal_sides_neutral(self):
        a = [self._c(80500, 300, 0.6)]
        b = [self._c(79500, 300, -0.6)]
        s = liq_map.compute_suggestion(a, b)
        self.assertAlmostEqual(s["score"], 0.0)
        self.assertEqual(s["suggestion"], "中性")

    def test_deadband_suppresses_small_difference(self):
        """差 5% → 中性（不超过 0.15 死区）"""
        a = [self._c(80500, 105, 0.6)]
        b = [self._c(79500, 100, -0.6)]
        s = liq_map.compute_suggestion(a, b)
        self.assertAlmostEqual(s["score"], 0.0476, places=3)
        self.assertEqual(s["suggestion"], "中性")

    def test_one_side_missing(self):
        s = liq_map.compute_suggestion([self._c(80500, 300, 0.6)], [])
        self.assertTrue(s["available"])
        self.assertEqual(s["suggestion"], "偏多")
        self.assertAlmostEqual(s["score"], 1.0)
        self.assertIsNone(s["below"])

    def test_no_clusters_unavailable(self):
        s = liq_map.compute_suggestion([], [])
        self.assertFalse(s["available"])
        self.assertIn("reason", s)

    def test_uses_strongest_cluster_per_side(self):
        """每侧取最强的那一簇，不是最强那一簇的强度和。"""
        a = [self._c(80500, 300, 0.6), self._c(81000, 250, 1.0)]
        b = [self._c(79500, 100, -0.6), self._c(79000, 90, -1.0)]
        s = liq_map.compute_suggestion(a, b)
        self.assertEqual(s["reference_value"], 300)
        self.assertEqual(s["above"]["price"], 80500)
        self.assertEqual(s["below"]["price"], 79500)

    def test_share_of_ref(self):
        a = [self._c(80500, 300, 0.6)]
        b = [self._c(79500, 100, -0.6)]
        s = liq_map.compute_suggestion(a, b)
        self.assertEqual(s["above"]["share_of_ref_pct"], 100.0)
        self.assertEqual(s["below"]["share_of_ref_pct"], 33.3)


class TestSuggestionInReference(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self._patchers = [
            mock.patch.object(liq_map, "CACHE_PATH", Path(os.path.join(self._tmp, "c.json"))),
            mock.patch.object(liq_map, "AUTH_CACHE_PATH", Path(os.path.join(self._tmp, "a.json"))),
            # ★ 必须隔离配额状态（真实环境的退避状态会让测试假失败）
            mock.patch.object(liq_map, "QUOTA_STATE_PATH", Path(os.path.join(self._tmp, "q.json"))),
        ]
        for p in self._patchers:
            p.start()

    def tearDown(self):
        for p in self._patchers:
            p.stop()

    def test_reference_contains_suggestion(self):
        with mock.patch.object(liq_map, "_http_json", side_effect=lambda p, q: (True, AGG_FIXTURE)):
            r = liq_map.liq_reference(80000)
        self.assertIn("suggestion", r)
        self.assertTrue(r["suggestion"]["available"])
        # AGG fixture：Binance/Bybit/Okex 各有值（Aster/Hyperliquid/Lighter 全 0）
        # → 80500 合计 60×3=180（上方最强）、79500 合计 20×3=60（下方最强）
        # 参考值 = max(180, 60) = 180 → 上方 → 偏多
        self.assertEqual(r["suggestion"]["reference_value"], 180)
        self.assertEqual(r["suggestion"]["reference_side"], "上方")
        self.assertEqual(r["suggestion"]["suggestion"], "偏多")
        self.assertAlmostEqual(r["suggestion"]["score"], 0.667, places=3)

    def test_format_includes_suggestion(self):
        with mock.patch.object(liq_map, "_http_json", side_effect=lambda p, q: (True, AGG_FIXTURE)):
            r = liq_map.liq_reference(80000)
        t = liq_map.format_reference(r)
        self.assertIn("多空建议", t)
        self.assertIn("偏多", t)
        self.assertIn("参考值", t)
        self.assertNotIn("above", t)   # 用户可见位置不出现英文键名
        self.assertNotIn("below", t)

    def test_format_suggestion_unavailable(self):
        t = liq_map.format_suggestion({"available": False, "reason": "无簇"})
        self.assertIn("不可用", t)


class TestQuotaHandling(unittest.TestCase):
    """★ 配额封禁（2026-11-02 15:20 实测事故）的处理。

    上游按 IP 配额限流：测试时高频打了几十次 → 整个 /api/* 命名空间被封
    （code=403 "please sub api to get data"），浏览器真实会话也一样挂。
    → 封禁期间必须**一个请求都不发**，且不能当成鉴权失败去重发现常量。
    """

    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self._p = [
            mock.patch.object(liq_map, "QUOTA_STATE_PATH", Path(self._tmp) / "q.json"),
            mock.patch.object(liq_map, "CACHE_PATH", Path(self._tmp) / "c.json"),
            mock.patch.object(liq_map, "AUTH_CACHE_PATH", Path(self._tmp) / "a.json"),
        ]
        for p in self._p:
            p.start()

    def tearDown(self):
        for p in self._p:
            p.stop()

    def test_quota_msg_raises_not_auth_failure(self):
        """配额封禁 ≠ 鉴权失败 → 必须抛 QuotaBlocked，不能触发常量重发现。"""
        body = {"success": False, "code": "403", "msg": "please sub api to get data"}
        with mock.patch.object(liq_map, "_http_json", return_value=(True, body)):
            with self.assertRaises(liq_map.QuotaBlocked):
                liq_map._fetch_web("/api/liqMap/getLiqMap", {}, liq_map._auth_material())

    def test_auth_msg_still_returns_auth_flag(self):
        """鉴权失败（system error）→ 仍走常量重发现路径。"""
        body = {"success": False, "code": "1", "msg": "system error"}
        with mock.patch.object(liq_map, "_http_json", return_value=(True, body)):
            data, auth_fail = liq_map._fetch_web("/api/liqMap/getLiqMap", {}, liq_map._auth_material())
        self.assertEqual(data, {})
        self.assertTrue(auth_fail)

    def test_backoff_blocks_all_requests(self):
        """退避期间 → 零 HTTP 请求。"""
        liq_map._quota_record_block()
        self.assertGreater(liq_map.quota_blocked_until(), 0)
        with mock.patch.object(liq_map, "_http_json",
                               side_effect=AssertionError("退避期不该发请求")) as m:
            r = liq_map.fetch_liq_map(force=True)
        self.assertFalse(r["available"])
        self.assertIn("配额封禁", r["reason"])
        self.assertEqual(m.call_count, 0)

    def test_backoff_grows_exponentially(self):
        """连续封禁 → 退避窗口指数增长（有上限）。"""
        d1 = liq_map._quota_record_block() - liq_map._now()
        d2 = liq_map._quota_record_block() - liq_map._now()
        d3 = liq_map._quota_record_block() - liq_map._now()
        self.assertLess(d1, d2)
        self.assertLess(d2, d3)
        self.assertLessEqual(d3, liq_map.QUOTA_BACKOFF_MAX + 1)

    def test_backoff_cleared_on_success(self):
        liq_map._quota_record_block()
        self.assertGreater(liq_map.quota_blocked_until(), 0)
        liq_map._quota_clear()
        self.assertEqual(liq_map.quota_blocked_until(), 0)

    def test_expired_backoff_allows_request(self):
        """退避过期 → 恢复请求。"""
        liq_map._quota_save({"streak": 1, "until": liq_map._now() - 1})
        self.assertEqual(liq_map.quota_blocked_until(), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
