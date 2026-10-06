#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
telegram.py 单元测试
====================

覆盖 _escape_html 全部边界 + push 失败时不抛 + 自动 token 读取。
"""
import os, sys, json, unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from telegram import _escape_html, push, load_token, load_chat


def E(s):
    """构造 HTML entity 期望值（避免 patch 工具反转 < / > / &）"""
    AMP = chr(38) + "amp;"
    LT  = chr(38) + "lt;"
    GT  = chr(38) + "gt;"
    return s.replace("&", AMP).replace("<", LT).replace(">", GT)


class TestEscapeHtml(unittest.TestCase):
    """HTML 转义：必须把裸 < / > / & 转成实体，但保留合法标签"""

    def test_bare_lt_escaped(self):
        self.assertEqual(_escape_html("100 < 200"), E("100 < 200"))

    def test_bare_gt_escaped(self):
        self.assertEqual(_escape_html("score > 3"), E("score > 3"))

    def test_bare_amp_escaped(self):
        self.assertEqual(_escape_html("A & B"), E("A & B"))

    def test_bold_tag_preserved(self):
        self.assertEqual(_escape_html("<b>BTC</b>"), "<b>BTC</b>")

    def test_close_bold_tag_preserved(self):
        self.assertEqual(_escape_html("</b>"), "</b>")

    def test_code_tag_preserved(self):
        self.assertEqual(_escape_html("<code>106</code>"), "<code>106</code>")

    def test_multiple_tags(self):
        text = "<b>BTC</b> ratio <b>4.2</b> ｜ <code>z=3.5</code>"
        result = _escape_html(text)
        self.assertEqual(result, text)

    def test_mixed_bare_and_tags(self):
        """关键场景：<b> 和裸 < 共存"""
        text = "<b>BTC</b> 距离 106 点 < 500"
        result = _escape_html(text)
        self.assertIn("<b>BTC</b>", result)             # 合法标签保留
        self.assertIn(E("< 500"), result)             # 裸 < 转义成 entity
        self.assertNotIn(" 距离 106 点 <", result)    # 原裸 < 不该出现

    def test_existing_entity_not_double_escaped(self):
        """已有合法实体（< 等）不应被双重转义"""
        self.assertEqual(_escape_html("100 < 200"), E("100 < 200"))
        self.assertEqual(_escape_html("100 & 200"), E("100 & 200"))
        self.assertEqual(_escape_html("100 > 200"), E("100 > 200"))

    def test_unknown_tag_escaped(self):
        """未知标签（如 <unknown>）应被转义——因为 TG HTML 不支持"""
        result = _escape_html("text <unknown> more")
        self.assertNotIn("<unknown>", result)
        self.assertIn(E("<unknown>"), result)

    def test_tag_with_attributes_preserved(self):
        """<a href="..."> 这样的带属性标签也要保留"""
        text = '<a href="https://example.com">link</a>'
        self.assertEqual(_escape_html(text), text)

    def test_real_failing_card(self):
        """重现 2026-10-04 真实失败场景：监测卡片附加准入规则说明"""
        card = (
            "✅ <b>BTC 跳空监测结束</b>  价格未大幅波动\n"
            "触发价 <b>84,802.5</b> ｜ 监测 60s（57 次抓取）\n"
            "📈 期间最高 <b>84,802.5</b>（+0 点）｜ "
            "📉 期间最低 <b>84,786.6</b>（-16 点）\n"
            "\n原策略（做多）：判定 无信号\n"
            "原策略继续有效 → 已写/合并 journal pending"
            "\n\n⏸ 准入规则：同方向已有 1 条策略活跃，最近 entry=84638.2 "
            "距离新策略 106 点 < 500（journal 未写入）"
        )
        result = _escape_html(card)
        # 转义后只剩合法标签/实体里的 <，没有裸的
        import re
        stripped = re.sub(r'</?\s*(?:b|i|u|s|code|pre|a)\b[^>]*>', '', result)
        stripped = re.sub(r'&(?:amp|lt|gt|quot);', '', stripped)
        self.assertNotIn("<", stripped, "不应有裸 < 剩余")
        # <b> 仍存在
        self.assertIn("<b>", result)

    def test_empty(self):
        self.assertEqual(_escape_html(""), "")

    def test_only_text(self):
        self.assertEqual(_escape_html("hello world"), "hello world")


class TestPush(unittest.TestCase):
    """push() 必须永不抛 + 自动 token 读取"""

    @patch("urllib.request.urlopen")
    def test_success_returns_true(self, m_urlopen):
        m_urlopen.return_value.__enter__.return_value.read.return_value = b'{"ok":true}'
        ok = push("hello", token="fake_tok", chat_id="123")
        self.assertTrue(ok)

    @patch("urllib.request.urlopen")
    def test_failure_returns_false_no_raise(self, m_urlopen):
        import urllib.error
        m_urlopen.side_effect = urllib.error.HTTPError(
            "url", 400, "Bad Request", {}, b'{"ok":false,"description":"x"}')
        ok = push("hello", token="fake_tok", chat_id="123")
        self.assertFalse(ok)        # 返回 False 但不抛

    def test_missing_token_returns_false(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("BTC_TG_TOKEN", None)
            with patch("telegram.SECRETS_PATH", new=__import__("pathlib").Path("/nonexistent")):
                ok = push("hello")
                self.assertFalse(ok)

    def test_html_escaping_applied_on_push(self, m_urlopen_patcher=None):
        """push 调用前必须 escape_html"""
        # 抓发出的请求体
        captured = {}
        def fake_urlopen(req, **kw):
            captured["body"] = req.data
            class R:
                def __enter__(self): return self
                def __exit__(self, *a): return False
                def read(self): return b'{"ok":true}'
            return R()
        with patch("urllib.request.urlopen", fake_urlopen):
            push("<b>x</b> & y < 100", token="fake", chat_id="123")
        body = json.loads(captured["body"])
        self.assertIn(E("< 100"), body["text"])
        self.assertIn(E("& y"), body["text"])
        self.assertIn("<b>x</b>", body["text"])


class TestSecretsLoading(unittest.TestCase):
    """凭证读取：环境变量优先级 vs 文件"""

    def test_load_token_from_env(self):
        # secrets 文件不存在时，环境变量才生效
        with patch("telegram.SECRETS_PATH", new=__import__("pathlib").Path("/nonexistent")):
            with patch.dict(os.environ, {"BTC_TG_TOKEN": "env_tok_123"}, clear=False):
                tok = load_token()
                self.assertEqual(tok, "env_tok_123")

    def test_load_chat_default(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("BTC_TG_CHAT", None)
            chat = load_chat()
            self.assertEqual(chat, "7097652385")

    def test_load_chat_from_env(self):
        with patch("telegram.SECRETS_PATH", new=__import__("pathlib").Path("/nonexistent")):
            with patch.dict(os.environ, {"BTC_TG_CHAT": "999"}, clear=False):
                chat = load_chat()
                self.assertEqual(chat, "999")


if __name__ == "__main__":
    unittest.main(verbosity=2)



class TestFormatCardOverride(unittest.TestCase):
    """format_card 的 override_active 参数（2026-10-04 新增）"""

    def setUp(self):
        from telegram import format_card
        self.format_card = format_card

        # scan_payload 模板（可被 override 覆盖）
        self.alert = {
            "alert": {
                "ratio": 5.0, "z": 4.0, "tag": "放量", "close": 85000.0,
                "vol": 200000,
            }
        }
        self.scan_payload = {
            "verdict": "临界（再等 1 根确认）",
            "side": "long",
            "score_long": 4,
            "score_short": 1,
            "plan": {
                "entry_limit": 85179.2,
                "sl": 84497.7,
                "sl_pct": 0.8,
                "tp1": 85579.2, "tp1_pct": 0.5,
                "tp2": 85779.2, "tp2_pct": 0.7,
                "tp3": 86179.2, "tp3_pct": 1.2,
                "contracts": 146,
                "notional": 1250.0,
            }
        }

        # 已进场的 journal 条（entry=84638.2 long）
        self.active_long = {
            "id": "BTC_USDT-1791060667-long",
            "contract": "BTC_USDT",
            "side": "long",
            "entry": 84638.2,
            "tp1": 85038.2, "tp2": 85238.2, "tp3": 85638.2,
            "sl": 83961.0,
            "sl_pct": 0.8,
            "tp1_pct": 0.47, "tp2_pct": 0.7, "tp3_pct": 1.2,
            "contracts": 591,
            "status": "filled",
        }
        # 已进场空单（用于测试反方向）
        self.active_short = {
            "id": "BTC_USDT-1791100000-short",
            "contract": "BTC_USDT",
            "side": "short",
            "entry": 85000.0,
            "tp1": 84000.0, "tp2": 83500.0,
            "sl": 85500.0,
            "contracts": 100,
            "status": "filled",
        }

    def test_no_override_uses_scan_data(self):
        """没 override 时用 scan 数据（85179.2 等）"""
        card = self.format_card("volume", self.alert, self.scan_payload)
        self.assertIn("85,179.2", card)
        self.assertIn("84,497.7", card)
        self.assertIn("146", card)

    def test_same_side_override_replaces_entry(self):
        """同方向 override → 用进场 entry（84,638.2），不用 scan 的 85,179.2"""
        card = self.format_card("volume", self.alert, self.scan_payload,
                                override_active=self.active_long)
        # 进场 entry 必须出现
        self.assertIn("84,638.2", card)
        # scan 新 entry 必须**不**出现
        self.assertNotIn("85,179.2", card)
        # 已进场标记必须出现
        self.assertIn("已进场", card)
        self.assertIn("同方向", card)

    def test_same_side_override_replaces_tp_sl(self):
        """同方向 override → TP1/TP2/SL 全部用进场的（85,038.2 / 85,238.2 / 83,961.0）"""
        card = self.format_card("volume", self.alert, self.scan_payload,
                                override_active=self.active_long)
        # 进场的 TP/SL
        self.assertIn("85,038.2", card)
        self.assertIn("85,238.2", card)
        self.assertIn("83,961.0", card)
        # scan 的 TP 不要出现
        self.assertNotIn("85,579.2", card)
        self.assertNotIn("85,779.2", card)
        self.assertNotIn("84,497.7", card)

    def test_same_side_override_replaces_qty(self):
        """同方向 override → 张数用进场（591），不用 scan 的 146"""
        card = self.format_card("volume", self.alert, self.scan_payload,
                                override_active=self.active_long)
        self.assertIn("591", card)
        # scan 的 146 不应出现
        # 注意 591 可能出现在 id 里也可能在 qty 里——只看文本中是否含 146
        # 这里只断言 591 出现
        self.assertIn("591", card)

    def test_reverse_side_keeps_scan_data(self):
        """反方向 override → 保留 scan 数据（85,179.2 等）+ 标记"""
        card = self.format_card("volume", self.alert, self.scan_payload,
                                override_active=self.active_short)
        # 反方向时 scan 数据保留
        self.assertIn("85,179.2", card)
        # 反方向标记
        self.assertIn("反方向", card)
        self.assertIn("short", card)   # "反方向 short"
        # active_short 的 id 应被显示（在 marker 里）
        self.assertIn("BTC_USDT-1791100000-short", card)
        # 但 active_short 的 entry（85000.0）不应作为入场价出现
        # 注意：alert.close=85000.0 会出现，但不应在"入场"那一行
        self.assertNotIn("入场 <b>85,000.0</b>", card)

    def test_no_active_no_marker(self):
        """无 override → 不显示已进场标记"""
        card = self.format_card("volume", self.alert, self.scan_payload)
        self.assertNotIn("已进场", card)
        self.assertNotIn("同方向", card)
        self.assertNotIn("反方向", card)

    def test_override_for_rsi_kind(self):
        """override 在 RSI 卡片也生效"""
        rsi_alert = {"alert": {"side": "超买", "val": 75, "prev": 60,
                                "period": 14, "tf": "1h", "px": 85000}}
        card = self.format_card("rsi", rsi_alert, self.scan_payload,
                                override_active=self.active_long)
        self.assertIn("已进场", card)
        self.assertIn("84,638.2", card)
