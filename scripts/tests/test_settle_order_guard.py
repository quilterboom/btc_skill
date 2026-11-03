#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
回归测试：risk_guard SL 分支的**结算顺序** + position_tracker._close 幂等守卫的收窄
====================================================================================

**线上 bug（2026-11-03 04:58 实测命中 2 笔）**
  生产 BTC_USDT-1793649653-long / 训练 BTC_USDT-1793649652-long 结算后 net_usd=null，
  成了 C 类卡死单（status=settled 但净值缺失）→ 不进任何胜率 / 净利统计。

**根因（调用顺序）**
  risk_guard._apply_sl 原顺序：
      ① rec["status"] = "settled"      ← 先标终态
      ② _canonical_close(rec, ...)     ← 后算净值 → position_tracker._close
  而 2026-11-01 给 _close 加的幂等守卫是 `status == "settled" → raise`：
      首次结算在守卫眼里就是「二次结算」→ 抛错 → net_usd 永远写不进去。
  回填脚本 backfill_risk_guard_settlements.py 对这类记录调 canonical 同样会崩。

**修法（两处，缺一不可）**
  1. risk_guard._apply_sl：canonical **先算**，net 写进去之后才标 status="settled"。
  2. position_tracker._close：守卫收窄成「status=settled **且 net_usd 已写**」才拒。
     → 二次结算改写净值的防线（10-28 事故）保持不变；
     → status=settled 但 net 缺失的卡死单允许重算 → 补得上净值。

**跑法**
    cd scripts && /root/.hermes/hermes-agent/venv/bin/python3 -m unittest tests.test_settle_order_guard -v
"""
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import position_tracker as pt  # noqa: E402


def _fp(path):
    """真实文件指纹（测试前后必须一致）。"""
    import hashlib
    if not os.path.exists(path):
        return None
    return hashlib.sha256(open(path, "rb").read()).hexdigest()


class TestCloseGuardNarrowed(unittest.TestCase):
    """_close 的幂等守卫：只挡「二次结算改写净值」，不再挡「补算缺失净值」。"""

    def _sig(self, **kw):
        base = {
            "id": "BTC_USDT-guard-order-long", "contract": "BTC_USDT", "side": "long",
            "entry": 76709.0, "sl": 76603.3, "tp1": 77706.2, "tp2": 78933.6,
            "status": "pending", "filled": True,
            "fill_ts": 1793649653, "fill_px": 77083.2, "contracts": 196,
        }
        base.update(kw)
        return base

    def test_pending_record_closes_normally(self):
        res = pt._close(self._sig(), "SL_STOPPED", 76708.0, 1793653091,
                        196, "long", 76709.0, "loss")
        self.assertIsNotNone(res.get("net_usd"))

    def test_settled_with_net_still_blocked(self):
        """★ 10-28 事故防线不能因为收窄守卫而失效：净值已写的二次结算必须抛错。"""
        sig = self._sig(status="settled", net_usd=3.14, outcome="SL_STOPPED")
        with self.assertRaises(ValueError) as cm:
            pt._close(sig, "SL_STOPPED", 76708.0, 1793653091,
                      196, "long", 76709.0, "loss")
        self.assertIn("拒绝二次结算", str(cm.exception))

    def test_settled_without_net_can_be_recomputed(self):
        """C 类卡死单（status=settled 但 net=null）必须允许补算，否则永远补不上。"""
        sig = self._sig(status="settled", outcome="SL_STOPPED")
        res = pt._close(sig, "SL_STOPPED", 76708.0, 1793653091,
                        196, "long", 76709.0, "loss")
        self.assertIsNotNone(res.get("net_usd"),
                             "net 缺失的已结算单必须能补算净值")
        self.assertIsNone(sig.get("net_usd"),
                          "_close 不该就地写 sig.net_usd（由调用方决定）")


class TestApplySlOrder(unittest.TestCase):
    """risk_guard._apply_sl：canonical 先算，status 最后写。"""

    def setUp(self):
        import tempfile
        import shutil as _sh
        self._sh = _sh
        self.tmp = tempfile.mkdtemp(prefix="settle_order_test_")
        self.tmp_events = os.path.join(self.tmp, "position_events.jsonl")
        self._patchers = [
            patch("position_tracker.EVENTS", self.tmp_events),
            patch("position_tracker.DATA_DIR", self.tmp),
            # risk_guard 的 log() 落到 watch.common.LOG —— 那是**生产** /root/data/watch.log，
            # 测试期间必须隔离，否则每次跑都往生产日志写假错误。
            patch("watch.common.LOG", os.path.join(self.tmp, "watch.log")),
        ]
        for p in self._patchers:
            p.start()
        self._real_events = "/root/data/position_events.jsonl"
        self._real_fingerprint = _fp(self._real_events)

    def tearDown(self):
        for p in self._patchers:
            p.stop()
        self._sh.rmtree(self.tmp, ignore_errors=True)
        self.assertEqual(_fp(self._real_events), self._real_fingerprint,
                         "测试污染了生产 position_events.jsonl！")

    @staticmethod
    def _real_04_58_rec():
        """11-03 04:58 那笔的真实形状（EMA 预警减仓 456 张 → 移 SL 到保本 → 76708 出场）。

        字段值取自生产 journal BTC_USDT-1793649653-long（id 换掉，不碰生产数据）。
        """
        return {
            "id": "BTC_USDT-TEST-1793649653-long", "contract": "BTC_USDT",
            "side": "long", "entry": 76709.0, "sl": 76603.3,
            "tp1": 77706.2, "tp2": 78933.6,
            "status": "pending", "filled": True,
            "fill_px": 77083.2, "fill_ts": 1793649653,
            "contracts": 196, "_orig_contracts": 652,
            "ema_warning_done": True, "ema_warning_ts": 1793650740,
            "ema_warning_px": 77122.9,
            "ema_partial_qty": 456, "ema_partial_px": 77122.9,
            "active_sl": 76709.0, "partial_exit_count": 1,
        }

    def test_canonical_runs_while_status_still_pending(self):
        """★ 核心回归：canonical 被调用时记录**不能**已经是 settled。"""
        from watch import risk_guard as rg
        seen = {}

        def _spy(rec, exit_px, exit_ts):
            seen["status_at_call"] = rec.get("status")
            return {"net_usd": 1.0, "gross_usd": 5.85, "category": "profit",
                    "outcome": "BE_STOPPED", "pos_state": "BE_STOPPED"}

        rec = self._real_04_58_rec()
        with patch.object(rg, "_canonical_close", side_effect=_spy):
            rg._apply_sl(rec, last=76708.0, now_ts=1793653091)
        self.assertEqual(seen.get("status_at_call"), "pending",
                         "canonical 必须在 status=settled 之前算完（本次线上 bug 的根因）")

    def test_real_record_shape_gets_net(self):
        """真实 04:58 记录形状 → net=+14.00U / gross=18.85 / category=profit / BE_STOPPED。

        手算复核：EMA 腿 456 张 @77122.9 = +18.87U，剩余 196 张 @76708.0 = -0.02U，
        扣 fee 4.85 → net +14.00U。出场价已用 1m K 验证（04:58 那根 low=76700 触及 76708）。
        """
        from watch import risk_guard as rg
        rec = self._real_04_58_rec()
        ev = rg._apply_sl(rec, last=76708.0, now_ts=1793653091)
        self.assertIsNotNone(ev, "canonical 成功必须返回 outcome event")
        self.assertAlmostEqual(rec["net_usd"], 14.00, places=2)
        self.assertAlmostEqual(rec["gross_usd"], 18.85, places=2)
        self.assertEqual(rec["category"], "profit")
        self.assertEqual(rec["outcome"], "BE_STOPPED")
        self.assertEqual(rec["status"], "settled")
        self.assertNotIn("settle_error", rec, "这次不能留下 settle_error")

    def test_failure_still_marks_settled_without_net(self):
        """canonical 算不出来（无 entry）→ 仍标 settled（防 risk_guard 每秒重复触发），
        但绝不写 net（宁可留空也不编造）。"""
        from watch import risk_guard as rg
        rec = self._real_04_58_rec()
        rec["entry"] = 0
        ev = rg._apply_sl(rec, last=76708.0, now_ts=1793653091)
        self.assertIsNone(ev)
        self.assertEqual(rec["status"], "settled")
        self.assertIsNone(rec.get("net_usd"))
        self.assertIn("settle_error", rec)

    def test_second_settle_does_not_overwrite_net(self):
        """同一笔被再次触发时，已写净值不许被改写（10-28 防线在 risk_guard 侧也成立）。"""
        from watch import risk_guard as rg
        rec = self._real_04_58_rec()
        rg._apply_sl(rec, last=76708.0, now_ts=1793653091)
        net_after_first = rec["net_usd"]
        rg._apply_sl(rec, last=76000.0, now_ts=1793654000)
        self.assertEqual(rec["net_usd"], net_after_first,
                         "已结算单的净值被二次结算改写了")


if __name__ == "__main__":
    unittest.main(verbosity=2)
