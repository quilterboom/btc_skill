#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_guard_not_tp1.py — 主动防御减仓不得冒充 TP1（2026-10-30）
============================================================

Bug（实测训练单 BTC_USDT-1793320220-long，10-30 08:32，做多 @77146.7）：
  价格 1.4 分钟内反向 0.3% 到 76,870（TP1=78,149.6 从未触达），
  却写 tp1_hit=True + tp1_hit_px=76870（亏损价），
  卡片推「🟢 BTC 部分止盈 TP1 ｜ 锁利 ≈ -5.98U」——
  负数写"锁利"、绿色图标、还谎称 TP1。**做多亏钱却说止盈。**

三层根因，三层修复：
  ① risk_guard._try_partial_close 的 adverse 分支 stamp tp1_hit
     → 修：kind="guard" 走 guard_partial_* 独立字段，不碰 tp1_hit
  ② _notify_partial 文案硬编码「部分止盈 / 锁利 / 🟢」
     → 修：图标/标题/动词由 pnl 符号决定
  ③ _close 的 category 由调用方按 outcome 给，net=-22.87 也写 profit
     → 修：category 由 net 符号定（口径同 web._winrate）

跑法：
    cd scripts && python3 -m unittest tests.test_guard_not_tp1 -v
"""
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import position_tracker as pt  # noqa: E402
from watch import risk_guard as rg  # noqa: E402


def _bar(ts, o, h, l, c):
    return [ts, o, h, l, c, 100]


def _push_capture():
    """捕获 push_once_telegram 的 (key, text) 列表"""
    out = []

    def _cap(key, text, *a, **kw):
        out.append((key, text))
    return out, _cap


class TestGuardDoesNotFakeTp1(unittest.TestCase):
    """① 主动防御减仓绝不能 stamp tp1_hit"""

    def _rec(self):
        # 复刻真实训练单：做多 @77146.7，TP1=78149.6（从未触达）
        return {
            "id": "BTC_USDT-GUARD1-long",
            "side": "long",
            "entry": 77146.7,
            "sl": 76706.6,
            "tp1": 78149.6,
            "tp2": 79384.0,
            "ts": 1793320220,
            "fill_ts": 1793320260,
            "fill_px": 77146.7,
            "contracts": 648,
            "status": "pending",
        }

    def test_guard_branch_does_not_set_tp1_hit(self):
        """adverse 触发后 tp1_hit 必须保持 False / 不存在"""
        rec = self._rec()
        out, cap = _push_capture()
        with patch("position_tracker.push_once_telegram", side_effect=cap), \
             patch("position_tracker.load_token", return_value="fake"), \
             patch("position_tracker.load_chat", return_value="fake"):
            ok = rg._try_partial_close(rec, 76870.0, 50, "adverse_long ...", kind="guard")
        self.assertTrue(ok)
        self.assertFalse(rec.get("tp1_hit"),
                         "主动防御减仓不许 stamp tp1_hit（TP1=78149.6 从未触达）")
        self.assertNotIn("tp1_hit_px", rec,
                         "亏损价不许写进 tp1_hit_px（会被当成止盈腿价）")
        self.assertTrue(rec.get("guard_partial_done"))
        self.assertEqual(rec["guard_partial_px"], 76870.0)
        self.assertEqual(rec["guard_partial_qty"], 648 // 3)

    def test_guard_keeps_active_sl_and_qty_bookkeeping(self):
        """主动防御仍要推保本 + 记 _orig_contracts（记账口径不变）"""
        rec = self._rec()
        out, cap = _push_capture()
        with patch("position_tracker.push_once_telegram", side_effect=cap), \
             patch("position_tracker.load_token", return_value="fake"), \
             patch("position_tracker.load_chat", return_value="fake"):
            rg._try_partial_close(rec, 76870.0, 50, "adverse_long ...", kind="guard")
        self.assertEqual(rec["active_sl"], 77146.7, "必须推保本（active_sl=entry）")
        self.assertEqual(rec["_orig_contracts"], 648)
        self.assertEqual(rec["pos_state"], "GUARD_PARTIAL")

    def test_tp1_branch_still_sets_tp1_hit(self):
        """真 TP1 路径行为不变（回归）"""
        rec = self._rec()
        out, cap = _push_capture()
        with patch("position_tracker.push_once_telegram", side_effect=cap), \
             patch("position_tracker.load_token", return_value="fake"), \
             patch("position_tracker.load_chat", return_value="fake"):
            rg._try_partial_close(rec, 78149.6, 50, "tp1_hit @ 78149.6", kind="tp1")
        self.assertTrue(rec.get("tp1_hit"))
        self.assertEqual(rec["tp1_hit_px"], 78149.6)
        self.assertFalse(rec.get("guard_partial_done"))


class TestPartialCardWording(unittest.TestCase):
    """② 卡片文案必须按盈亏符号分流，不许自相矛盾"""

    def _sig(self):
        return {"id": "BTC_USDT-CARD1-long", "side": "long",
                "entry": 77146.7, "tp1": 78149.6, "tp2": 79384.0,
                "active_sl": 77146.7,   # 三条减仓路径都推保本
                "_orig_contracts": 648}

    def _render(self, label, px):
        out, cap = _push_capture()
        with patch("position_tracker.push_once_telegram", side_effect=cap), \
             patch("position_tracker.load_token", return_value="fake"), \
             patch("position_tracker.load_chat", return_value="fake"):
            pt._notify_partial(self._sig(), label, px, 648, 1793320345, qty=216)
        self.assertEqual(len(out), 1, f"必须推 1 张卡（实际 {len(out)}）")
        return out[0][1]

    def test_loss_leg_says_loss_not_profit(self):
        """亏损腿：必须写"锁亏 / 止损"，不许出现"锁利 / 止盈"，不许 🟢"""
        text = self._render("GUARD", 76870.0)
        self.assertNotIn("锁利", text, f"亏损腿不许写「锁利」（{text}）")
        self.assertNotIn("止盈", text, f"亏损腿不许写「止盈」（{text}）")
        self.assertNotIn("🟢", text, f"亏损腿不许用绿点（{text}）")
        self.assertIn("锁亏", text, f"亏损腿要写「锁亏」（{text}）")
        self.assertIn("🔴", text, f"亏损腿要用红点（{text}）")
        self.assertIn("-5.98U", text, f"亏损金额要带负号（{text}）")

    def test_profit_leg_keeps_profit_wording(self):
        """盈利腿（真 TP1）：文案不变，仍是锁利/止盈/🟢"""
        text = self._render("TP1", 78149.6)
        self.assertIn("锁利", text, f"盈利腿要写「锁利」（{text}）")
        self.assertIn("🟢", text, f"盈利腿要用绿点（{text}）")
        # 216 张 × (78149.6-77146.7)=1002.9 点 × 0.0001 = +21.66U
        self.assertIn("+21.66U", text, f"盈利金额要带正号（{text}）")

    def test_card_label_is_source_not_profit_claim(self):
        """label 是触发来源：GUARD 卡不许自称 TP1，要标真实来源"""
        text = self._render("GUARD", 76870.0)
        self.assertNotIn("部分止盈 TP1", text, f"GUARD 卡不许自称 TP1（{text}）")
        self.assertNotIn("TP1 触发", text, f"GUARD 卡不许写「TP1 触发」（{text}）")
        self.assertIn("主动防御", text, f"GUARD 卡要标真实来源（{text}）")
        self.assertIn("BE (开仓价)", text, f"GUARD 推的是保本，卡上要如实写（{text}）")


class TestCategoryFollowsNetSign(unittest.TestCase):
    """③ category 由 net 符号定，不许 net<0 却写 profit"""

    def _close_guard_case(self):
        """复刻真实训练单的记账：guard 减 216@76870 + 剩余 432@76868"""
        sig = {"id": "BTC_USDT-CAT1-long", "side": "long",
               "entry": 77146.7, "sl": 76706.6,
               "fill_ts": 1793320260, "fill_px": 77146.7,
               "_orig_contracts": 648, "contracts": 648,
               "guard_partial_done": True,
               "guard_partial_ts": 1793320345,
               "guard_partial_px": 76870.0,
               "guard_partial_qty": 216}
        return sig

    def test_loss_is_loss_even_when_be_moved(self):
        """推过保本但净亏 → category 必须是 loss（不是 profit）"""
        sig = self._close_guard_case()
        res = pt._close(sig, "BE_STOPPED", 76868.0, 1793320347,
                        648, "long", 77146.7, "profit")
        self.assertLess(res["net_usd"], 0,
                        f"这笔应是净亏（实际 {res['net_usd']}）")
        self.assertEqual(res["category"], "loss",
                         f"net<0 却写 category={res['category']} —— 亏损被标成盈利")

    def test_pnl_value_matches_old_accounting(self):
        """净值口径不变：guard 腿 + 剩余腿 = -18.02 毛 / -22.87 净"""
        sig = self._close_guard_case()
        res = pt._close(sig, "BE_STOPPED", 76868.0, 1793320347,
                        648, "long", 77146.7, "profit")
        self.assertAlmostEqual(res["gross_usd"], -18.02, places=2,
                               msg=f"毛利口径变了？实际 {res['gross_usd']}")
        self.assertAlmostEqual(res["net_usd"], -22.87, places=2,
                               msg=f"净利口径变了？实际 {res['net_usd']}")

    def test_profit_stays_profit(self):
        """净赚仍是 profit（回归）"""
        # contracts 必须够大才盖得住 4.85U 手续费：
        #   500000 张 → TP1 腿 166666 张 × 1 点 × 0.0001 = 16.67U，净 +11.82U
        sig = {"id": "BTC_USDT-CAT2-long", "side": "long",
               "entry": 100.0, "sl": 99.0,
               "fill_ts": 10000, "fill_px": 100.0,
               "tp1_hit": True, "tp1_hit_ts": 10100, "tp1_hit_px": 101.0,
               "_orig_contracts": 500000, "contracts": 500000}
        res = pt._close(sig, "BE_STOPPED", 100.0, 10200,
                        500000, "long", 100.0, "profit")
        self.assertAlmostEqual(res["net_usd"], 11.82, places=2)
        self.assertEqual(res["category"], "profit")

    def test_skip_category_untouched(self):
        """skip（未进场作废）不参与盈亏分类"""
        sig = {"id": "BTC_USDT-CAT3-long", "side": "long", "entry": 100.0}
        res = pt._close(sig, "MISSED", 105.0, 1000, 0, "long", 100.0, "skip")
        self.assertEqual(res["category"], "skip")


class TestCloseCardHeadNotLying(unittest.TestCase):
    """④ 平仓卡标题：没触发 TP1 时不许写「TP1 后保本被打」"""

    def _head(self, sig):
        out, cap = _push_capture()
        res = {"outcome": "BE_STOPPED", "category": "loss",
               "net_usd": -22.87, "exit_px": 76868.0,
               "exit_ts": 1793320347, "contracts": 648,
               "partial_exits": 1}
        with patch("position_tracker.push_once_telegram", side_effect=cap), \
             patch("position_tracker.load_token", return_value="fake"), \
             patch("position_tracker.load_chat", return_value="fake"):
            pt._notify_close(sig, res)
        self.assertEqual(len(out), 1, f"必须推 1 张平仓卡（实际 {len(out)}）")
        return out[0][1]

    def test_guard_be_head_says_guard_not_tp1(self):
        text = self._head({"id": "BTC_USDT-H1-long", "side": "long",
                           "entry": 77146.7, "tp1": 78149.6, "tp2": 79384.0,
                           "guard_partial_done": True})
        self.assertNotIn("TP1 后保本被打", text,
                         f"没触发 TP1 不许写「TP1 后保本被打」（{text}）")
        self.assertIn("主动防御减仓后保本被打", text, f"标题要标真实来源（{text}）")

    def test_tp1_be_head_still_says_tp1(self):
        text = self._head({"id": "BTC_USDT-H2-long", "side": "long",
                           "entry": 77146.7, "tp1": 78149.6, "tp2": 79384.0,
                           "tp1_hit": True})
        self.assertIn("TP1 后保本被打", text, f"真 TP1 后仍写「TP1 后保本被打」（{text}）")


if __name__ == "__main__":
    unittest.main(verbosity=2)
