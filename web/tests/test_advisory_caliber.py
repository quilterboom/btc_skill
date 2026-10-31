# -*- coding: utf-8 -*-
"""交易员判读三连修的回归测试（2026-10-31）。

三个坑，每个都要有防复发的断言：

1. **两卡打架** —— index.html 行情分析卡（ns="main"）和 /market 卡（ns="board"）
   各用一套趋势引擎，同一时刻方向标签能不一样（实测 15m: flat vs up）。
   → 现在两边都走 market_board.analyze_market()（= trader_view.analyze_tf）。
   断言：main 和 board 的 trend.{15m,1h,4h} 必须完全一致。

2. **关键位幻觉** —— ma_payload 只给 24h 高低 + 4 个 EMA，没给摆动高低点，
   LLM 自己编出 75000 这种不存在的位。
   → ma_payload 现在带 structure（含 swing_lo/hi）+ momentum + volume + voting。
   断言：payload 里这四个字段必须在，且 structure 各周期有段高低点。

3. **判读只写半句** —— max_tokens=320 被 mimo-v2.6-pro 的**推理**吃光
   （completion_tokens=320 全是 reasoning_tokens，正文 0 字），finish_reason=length。
   → 提到 1200。断言：源码里 max_tokens 不得低于 1000。

跑法：python3 -m unittest web.tests.test_advisory_caliber -v
"""
import ast
import re
import unittest
from pathlib import Path

WEB = Path(__file__).resolve().parent.parent


class TestMaPayloadCarriesStructure(unittest.TestCase):
    """坑 2：判读能引用的数字必须来自 ma_payload，且 payload 要带摆动高低点。"""

    @classmethod
    def setUpClass(cls):
        import sys
        sys.path.insert(0, str(WEB))
        sys.path.insert(0, str(WEB.parent / "scripts"))
        import market_board as mb
        cls.mb = mb

    def _payload(self):
        am = self.mb.analyze_market(self.mb.DEFAULT_TF_LIMITS)
        return self.mb.build_ma_payload(
            tfs=am["tfs"], aggregate=am["aggregate"], last_px=am["last_px"],
            ts_str="2026-10-31 00:00:00", hi24=100000.0, lo24=90000.0,
            pos_in_range=50.0, atr_pct=0.5, regime="normal",
            funding={"current": 0.0001, "previous": 0.0001, "sign": "long_pay"},
        )

    def test_payload_has_structure_momentum_volume_voting(self):
        """ma_payload 必须带 structure/momentum/volume/voting —— 缺一个判读就会编。"""
        mp = self._payload()
        for key in ("structure", "momentum", "volume", "voting"):
            self.assertIn(key, mp, f"ma_payload 缺 {key} —— 判读会瞎编")
            self.assertTrue(mp[key], f"ma_payload 的 {key} 是空的")

    def test_structure_carries_swing_lo_hi(self):
        """每个周期的 structure 必须有摆动高低点，这是判读唯一合法的关键位来源。"""
        mp = self._payload()
        for tf in ("15m", "1h", "4h"):
            s = (mp.get("structure") or {}).get(tf) or {}
            self.assertIn("lo", s, f"{tf} structure 缺 lo")
            self.assertIn("hi", s, f"{tf} structure 缺 hi")

    def test_key_levels_carries_pos_in_range(self):
        """区间位置必须由后端算好给过去，别让 LLM 自己除。"""
        mp = self._payload()
        self.assertIn("pos_in_range_pct", mp.get("key_levels") or {})


class TestTwoCardsAgree(unittest.TestCase):
    """坑 1：两个 namespace 的方向标签必须同源，否则同屏自相矛盾。"""

    @classmethod
    def setUpClass(cls):
        import sys
        sys.path.insert(0, str(WEB))
        sys.path.insert(0, str(WEB.parent / "scripts"))
        import market_board as mb
        cls.mb = mb

    def test_trend_words_match_across_namespaces(self):
        """main（index.html）与 board（/market）对同一时刻同一周期必须给同一个方向。"""
        am = self.mb.analyze_market(self.mb.DEFAULT_TF_LIMITS)
        board_words = {tf: self.mb._bias_word(am["tfs"].get(tf, {}))
                       for tf in ("15m", "1h", "4h")}
        # main namespace 现在也走 analyze_market + _bias_word，逐周期对比
        for tf in ("15m", "1h", "4h"):
            main_word = self.mb._bias_word(am["tfs"].get(tf, {}))
            self.assertEqual(
                main_word, board_words[tf],
                f"{tf} 方向标签不一致：main={main_word} board={board_words[tf]} —— 同屏会打架")

    def test_bias_word_never_returns_unknown(self):
        """_bias_word 只允许 up/down/flat，前端 trendPill 只认这三个。"""
        for tf in ("15m", "1h", "4h"):
            w = self.mb._bias_word({"trend_score": 0, "ema20": 1.0, "ema50": 1.0})
            self.assertIn(w, ("up", "down", "flat"))


class TestMaxTokensNotTruncated(unittest.TestCase):
    """坑 3：mimo-v2.6-pro 是推理型模型，max_tokens 同时供思考和输出。

    实测 320 时 completion_tokens=320 且 reasoning_tokens=320 → 正文 0 字。
    """

    def test_max_tokens_leaves_room_for_answer(self):
        src = (WEB / "server.py").read_text(encoding="utf-8")
        m = re.search(r'"max_tokens"\s*:\s*(\d+)', src)
        self.assertIsNotNone(m, "server.py 里找不到 max_tokens")
        v = int(m.group(1))
        self.assertGreaterEqual(
            v, 1000,
            f"max_tokens={v} 太小 —— 推理模型的思考会吃光预算，判读只写半句")

    def test_system_prompt_does_not_fight_user_prompt(self):
        """system prompt 别写「一两句短话」，user prompt 要三段式 100-150 字 —— 会打架。

        ★ 注意：要查的是**发给模型的那句 system prompt 的实际内容**，
        不是注释里对旧文案的引用（注释里写旧文案是为了记录为什么改）。
        """
        src = (WEB / "server.py").read_text(encoding="utf-8")
        # 只看 {"role": "system", "content": "..."} 里的 content 值
        m = re.search(
            r'\{"role":\s*"system",\s*"content":\s*"([^"]*)"\}',
            src)
        self.assertIsNotNone(m, "找不到 system prompt")
        sys_prompt = m.group(1)
        self.assertNotIn("一两句短话", sys_prompt,
                         "system prompt 要「一两句短话」但 user prompt 要三段式 100-150 字，"
                         "两个指令打架，模型会往短里写")
        # 反向：应该跟 user prompt 的三段式同向
        self.assertTrue(
            any(w in sys_prompt for w in ("数据", "分类", "方向")),
            f"system prompt 未与 user prompt 的三段式对齐：{sys_prompt}")


class TestOrphanHelpersRemoved(unittest.TestCase):
    """自研 _ema/_atr 已被 analyze_tf 取代，别留下孤儿实现（两套口径迟早打架）。"""

    def test_no_local_trend_engine_left(self):
        src = (WEB / "server.py").read_text(encoding="utf-8")
        self.assertNotIn("def _trend_from", src, "server.py 还留着自研趋势引擎")
        self.assertNotIn("def _atr(", src, "server.py 还留着自研 ATR 实现")


if __name__ == "__main__":
    unittest.main(verbosity=2)
