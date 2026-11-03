#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
判读模型链测试（2026-11-02：主 minimax → 备 mimo）

liusir：「之前不是有一个配置，首选minimax,当 minimax 报错了就用小米 mimo」

锁住的口径：
  1. 模型链顺序：**主 minimax，备 mimo**（跟 Hermes 自己的 fallback_providers 同构）
  2. 主模型报错 → 自动降级到备模型，且**用的是备模型的答案**
  3. 主模型正常 → 用主模型的，**不**多打一次备模型
  4. 主模型返回空正文（推理型模型预算被思考吃光）→ 也降级
  5. 两个 key 都缺 → 返回 None（前端 fallback 到规则判读），不静默用空串
  6. ★ 推理型模型的 max_tokens 要够：MiniMax-M3.1-Flash 实测 1200/2000/4000
     都 finish_reason=length 且正文 0 字，3000 才出完整三段
  7. ★ 凭证走环境变量，明文不进 git
"""
import json
import os
import sys
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("MINIMAX_CN_API_KEY", "test-mm-key")
os.environ.setdefault("MIMO_API_KEY", "test-mimo-key")
import server as server_mod  # noqa: E402


class _FakeResp:
    def __init__(self, payload):
        self._b = json.dumps(payload).encode()

    def read(self):
        return self._b

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _ma_payload():
    return {
        "ts_str": "2026-11-02 22:00:00", "last_px": 78148.0,
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


class TestModelChain(unittest.TestCase):
    def setUp(self):
        server_mod._MM_CACHES.clear()
        server_mod._MM_CACHES.update({
            "main": {"ts": 0, "text": None, "provider": ""},
            "board": {"ts": 0, "text": None, "provider": ""},
        })
        self._mm = os.environ.get("MINIMAX_CN_API_KEY", "")
        self._mimo = os.environ.get("MIMO_API_KEY", "")

    def _run(self, handler, ns="t"):
        """跑一次 _mm_advice，记录每次出网请求。"""
        import urllib.request
        calls = []

        def fake(req, timeout=None):
            calls.append({
                "url": getattr(req, "full_url", ""),
                "body": json.loads(req.data.decode()),
                "auth": req.headers.get("Authorization", ""),
            })
            return handler(calls[-1])

        with mock.patch.object(urllib.request, "urlopen", side_effect=fake):
            txt = server_mod._mm_advice(_ma_payload(), ns=ns)
        return txt, calls

    def test_primary_is_minimax(self):
        """主模型是 minimax。"""
        def h(c):
            return _FakeResp({"choices": [{"message": {"content": "主模型答案"}}]})
        txt, calls = self._run(h)
        self.assertEqual(len(calls), 1)
        self.assertIn("api.minimaxi.com", calls[0]["url"])
        self.assertEqual(calls[0]["body"]["model"], "MiniMax-M3.1-Flash-Preview")
        self.assertEqual(txt, "主模型答案")
        self.assertEqual(server_mod._MM_CACHES["t"]["provider"], "minimax")

    def test_no_second_call_when_primary_ok(self):
        """主模型正常 → 不多打一次备模型（省配额/省时间）。"""
        def h(c):
            return _FakeResp({"choices": [{"message": {"content": "主模型答案"}}]})
        _, calls = self._run(h)
        self.assertEqual(len(calls), 1)

    def test_fallback_to_mimo_on_error(self):
        """主模型报错 → 自动降级到 mimo。"""
        def h(c):
            if "minimaxi" in c["url"]:
                raise urllib.error.URLError("主模型挂了")
            return _FakeResp({"choices": [{"message": {"content": "备模型答案"}}]})
        txt, calls = self._run(h)
        self.assertEqual(len(calls), 2)
        self.assertIn("api.minimaxi.com", calls[0]["url"])
        self.assertIn("xiaomimimo.com", calls[1]["url"])
        self.assertEqual(txt, "备模型答案")
        self.assertEqual(server_mod._MM_CACHES["t"]["provider"], "mimo")

    def test_fallback_on_http_error(self):
        def h(c):
            if "minimaxi" in c["url"]:
                raise urllib.error.HTTPError(c["url"], 500, "boom", {}, None)
            return _FakeResp({"choices": [{"message": {"content": "备模型答案"}}]})
        txt, calls = self._run(h)
        self.assertEqual(len(calls), 2)
        self.assertEqual(txt, "备模型答案")

    def test_fallback_on_empty_content(self):
        """★ 主模型正文空（推理型模型预算被思考吃光）→ 也降级。"""
        def h(c):
            if "minimaxi" in c["url"]:
                return _FakeResp({"choices": [{"message": {"content": "",
                                                           "reasoning_content": "x" * 5000}}]})
            return _FakeResp({"choices": [{"message": {"content": "备模型答案"}}]})
        txt, calls = self._run(h)
        self.assertEqual(len(calls), 2)
        self.assertEqual(txt, "备模型答案")

    def test_all_fail_returns_none(self):
        """两个都挂 → None（前端 fallback 规则判读），不静默用空串。"""
        def h(c):
            raise urllib.error.URLError("全挂")
        txt, calls = self._run(h)
        self.assertIsNone(txt)
        self.assertEqual(len(calls), 2)

    def test_missing_primary_key_goes_to_backup(self):
        """只配了备模型 → 直接用备的，不浪费一次尝试。"""
        def h(c):
            return _FakeResp({"choices": [{"message": {"content": "备模型答案"}}]})
        with mock.patch.dict(os.environ, {"MINIMAX_CN_API_KEY": ""}, clear=False):
            txt, calls = self._run(h)
        self.assertEqual(len(calls), 1)
        self.assertIn("xiaomimimo.com", calls[0]["url"])
        self.assertEqual(txt, "备模型答案")

    def test_missing_both_keys_returns_none(self):
        def h(c):
            return _FakeResp({"choices": [{"message": {"content": "x"}}]})
        with mock.patch.dict(os.environ, {"MINIMAX_CN_API_KEY": "", "MIMO_API_KEY": ""},
                             clear=False):
            txt, calls = self._run(h)
        self.assertIsNone(txt)
        self.assertEqual(len(calls), 0)


class TestMaxTokensBudget(unittest.TestCase):
    """★ 推理型模型的 max_tokens 预算。"""

    def _providers(self):
        """从源码里读出模型链配置（保证测试锁的是真实参数）。"""
        import re
        src = (Path(__file__).resolve().parent.parent / "server.py").read_text(encoding="utf-8")
        block = re.search(r"providers = \[(.*?)\]\n", src, re.S)
        self.assertIsNotNone(block, "server.py 里找不到 providers 列表")
        return block.group(1)

    def test_minimax_budget_is_3000(self):
        """实测 1200/2000/4000 都正文为空 → 3000 才够。"""
        block = self._providers()
        self.assertIn("MiniMax-M3.1-Flash-Preview", block)
        self.assertRegex(block, r"3000")

    def test_mimo_budget_is_1200(self):
        block = self._providers()
        self.assertIn("mimo-v2.6-pro", block)
        self.assertRegex(block, r"1200")

    def test_minimax_uses_bearer_auth(self):
        block = self._providers()
        self.assertIn("MINIMAX_CN_API_KEY", block)
        self.assertIn("MIMO_API_KEY", block)


class TestCredentialsNotInGit(unittest.TestCase):
    def test_no_hardcoded_keys(self):
        src = (Path(__file__).resolve().parent.parent / "server.py").read_text(encoding="utf-8")
        # 真的 key 前缀：sk-cp- (minimax) / tp-ct4 (mimo)
        self.assertNotIn("sk-cp-", src, "server.py 里不该有硬编码的 MiniMax key")
        self.assertNotIn("tp-ct4", src, "server.py 里不该有硬编码的 mimo key")

    def test_keys_come_from_env(self):
        src = (Path(__file__).resolve().parent.parent / "server.py").read_text(encoding="utf-8")
        # 凭证从环境变量取，且**不硬编码**在源码里
        self.assertIn("MINIMAX_CN_API_KEY", src)
        self.assertIn("MIMO_API_KEY", src)
        self.assertIn('os.environ.get(pkey_env', src)
        # 明文 key 前缀不能出现在源码里
        self.assertNotIn("sk-cp-", src)
        self.assertNotIn("tp-ct4", src)


if __name__ == "__main__":
    unittest.main(verbosity=2)
