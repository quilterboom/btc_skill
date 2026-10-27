#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
回归测试：settler「单触判定必须先判 t_sl」守卫（2026-10-24 严重 bug）
====================================================================

**Bug（2026-10-24 07:04 / 07:20 实测两笔 short）**：
10-23 的 BE/SL 分类改写把 `if t_sl:` 守卫静默删掉了，变成了裸 `return _close(...)`。
后果：只要持仓已成交，回放走到**第一根** ts > fill_ts 的 K 线就无条件平仓，
无论价格有没有摸到 SL / TP。

签名（一眼可辨）：
  · exit_px 恰好 == sl（81205.5 / 81222.9），而同期 1m 最高只有 80984 → **价格从未到过止损位**
  · exit_ts 恰好 == fill_ts + 60（成交后第一根 1m）
  · TP1 / TP2 / PARTIAL_CLOSE 全部变成死代码（在 return 之后）

**修复后应得**（真实 K 线回放）：
  · 1792797127-short（07:20 成交，SL 81222.9 / TP1 80504.4）→ 什么都不该触发，**仍持仓**
  · 1792796209-short（07:04 成交，SL 81205.5 / TP1 80504.4）→ 07:11 触 TP1（1/3 锁利），
    07:20 影线打到保本位 80892.7 → BE_STOPPED，净 ≈ +3.15U

跑法：
    cd scripts && /root/.hermes/hermes-agent/venv/bin/python3 -m unittest tests.test_settle_sl_guard -v
"""
import os, sys, unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import position_tracker as pt
import gate_fetch


def _bar(ts, o, h, l, c):
    return [ts, o, h, l, c, 100]


def _sig(side, entry, sl, tp1, tp2=0, ts=1000, fill_ts=1005, fill_px=None,
         contracts=5000, sid="BTC_USDT-guard"):
    return {
        "id": sid, "contract": "BTC_USDT", "ts": ts, "side": side,
        "entry": entry, "sl": sl, "tp1": tp1, "tp2": tp2,
        "status": "pending", "filled": True,
        "fill_ts": fill_ts, "fill_px": fill_px if fill_px is not None else entry,
        "contracts": contracts, "sl_pct": 0.008,
    }


class TestSlGuardNotCloseWhenUntouched(unittest.TestCase):
    """核心回归：价格没到 SL/TP 就**不许**平仓"""

    def setUp(self):
        self.patchers = [
            patch("position_tracker._notify_fill"),
            patch("position_tracker._notify_partial"),
            patch("position_tracker._notify_close"),
            patch("position_tracker._read_journal", return_value=[]),
            patch("position_tracker._write_journal"),
            # 拦掉 EMA 预警的 K 线抓取（本测试只测 SL/TP 守卫，不测预警）
            patch("position_tracker.fetch_ohlcv", return_value=[]),
        ]
        for p in self.patchers:
            p.start()

    def tearDown(self):
        for p in self.patchers:
            p.stop()

    def test_long_not_close_when_sl_tp_untouched(self):
        """多单：后续 K 线既没跌破 SL 也没到 TP1 → 必须返回 None（继续持仓）"""
        sig = _sig("long", entry=100, sl=99, tp1=110, tp2=120)
        bars = [
            _bar(1005, 100.0, 100.2, 100.0, 100.1),   # 成交当根（按 fill_ts 跳过）
            _bar(1010, 100.1, 100.4, 100.05, 100.3),  # 没触 SL(99) / TP1(110)
            _bar(1020, 100.3, 100.5, 100.1, 100.2),   # 同上
        ]
        res = pt._settle_one(sig, bars)
        self.assertIsNone(
            res,
            f"价格没到 SL/TP 却被平仓了（outcome={res.get('outcome') if res else None}，"
            f"exit_px={res.get('exit_px') if res else None}）—— `if t_sl:` 守卫丢了？")

    def test_short_not_close_when_sl_tp_untouched(self):
        """空单镜像：同样不该平仓"""
        sig = _sig("short", entry=100, sl=101, tp1=80, tp2=70)
        bars = [
            _bar(1005, 100.0, 100.2, 100.0, 100.1),
            _bar(1010, 100.1, 100.35, 100.05, 100.2),
            _bar(1020, 100.2, 100.4, 100.1, 100.3),
        ]
        res = pt._settle_one(sig, bars)
        self.assertIsNone(res, "空单价格没到 SL/TP 却被平仓了")

    def test_long_sl_still_triggers(self):
        """反向回归：真打到 SL 时必须平（防止守卫加过头把 SL 也关掉）"""
        sig = _sig("long", entry=100, sl=99, tp1=110, tp2=120)
        bars = [
            _bar(1005, 100.0, 100.2, 100.0, 100.1),
            _bar(1010, 100.1, 100.4, 100.05, 100.3),
            _bar(1020, 100.3, 100.5, 98.5, 98.6),     # low 98.5 ≤ sl 99
        ]
        res = pt._settle_one(sig, bars)
        self.assertIsNotNone(res, "真打到 SL 却没平仓")
        self.assertEqual(res["outcome"], "SL_STOPPED")
        self.assertEqual(res["exit_px"], 99)
        self.assertEqual(res["category"], "loss")

    def test_short_sl_still_triggers(self):
        """空单反向回归：真打到 SL 时必须平"""
        sig = _sig("short", entry=100, sl=101, tp1=80, tp2=70)
        bars = [
            _bar(1005, 100.0, 100.2, 100.0, 100.1),
            _bar(1010, 100.1, 100.35, 100.05, 100.2),
            _bar(1020, 100.2, 101.5, 100.1, 101.4),   # high 101.5 ≥ sl 101
        ]
        res = pt._settle_one(sig, bars)
        self.assertIsNotNone(res)
        self.assertEqual(res["outcome"], "SL_STOPPED")
        self.assertEqual(res["exit_px"], 101)

    def test_exit_ts_is_not_fill_ts_plus_first_bar(self):
        """签名回归：不能出现「成交后第一根 K 线就以 SL 价平仓」的形态"""
        sig = _sig("long", entry=100, sl=99, tp1=110, tp2=120)
        bars = [
            _bar(1005, 100.0, 100.2, 100.0, 100.1),
            _bar(1010, 100.1, 100.4, 100.05, 100.3),  # 第一根，什么都没触
        ]
        res = pt._settle_one(sig, bars)
        if res is not None:
            self.assertNotEqual(
                res.get("exit_ts"), 1010,
                "成交后第一根 K 线就被平仓 = `if t_sl:` 守卫丢失的签名")


class TestRealTrades20261024(unittest.TestCase):
    """用 2026-10-24 真实两笔 short 逐字复现（真实 1m K 线）"""

    def setUp(self):
        self.patchers = [
            patch("position_tracker._notify_fill"),
            patch("position_tracker._notify_partial"),
            patch("position_tracker._notify_close"),
            patch("position_tracker._read_journal", return_value=[]),
            patch("position_tracker._write_journal"),
            patch("position_tracker.fetch_ohlcv", return_value=[]),
        ]
        for p in self.patchers:
            p.start()
        # ★ 2026-10-25 修：本类是「时点快照」回归 —— 断言的是**当时**（10-24 07:30）
        #   持仓状态。原来直接取最新 8000 根 1m，K 线缓存随时间增长后窗口会一路延伸：
        #   1792797127-short 后来在 10-24 真的打到 TP2(80067.6) 结算了，于是
        #   「应该仍持仓」这条断言必然 FAIL —— 是测试数据失效，不是守卫回退。
        #   截到测试写时的时点，让断言与 docstring 描述的窗口一致、且可重复。
        _cutoff = 1792798200   # 2026-10-24 07:30 CST
        self.bars = sorted(
            [b for b in gate_fetch.fetch_ohlcv("BTC_USDT", "1m", 8000, cache=True,
                                               refresh=False, verbose=False)
             if b[0] <= _cutoff],
            key=lambda b: b[0])

    def tearDown(self):
        for p in self.patchers:
            p.stop()

    def test_1792797127_still_open(self):
        """BTC_USDT-1792797127-short：07:20 成交 @ 80892.7，SL 81222.9 / TP1 80504.4
        07:21 之后最高 80984、最低 80470 都没到 SL/TP → **应该仍持仓**（返回 None）"""
        sig = _sig("short", entry=80892.7, sl=81222.9, tp1=80504.4, tp2=80067.6,
                   ts=1792797127, fill_ts=1792797600, fill_px=80892.7,
                   contracts=618, sid="BTC_USDT-1792797127-short")
        res = pt._settle_one(sig, self.bars)
        self.assertIsNone(
            res,
            f"价格从没到过 SL 81222.9 / TP1 80504.4，却被平仓了：{res}")

    def test_1792796209_tp1_then_be(self):
        """BTC_USDT-1792796209-short：07:04 成交 @ 80892.7，SL 81205.5 / TP1 80504.4
        07:11 low=80470 ≤ TP1 80504.4 → TP1 触发（1/3 锁利），SL 移到 BE 80892.7
        07:20 high=80905.8 ≥ BE 80892.7 → BE_STOPPED
        净 = 206 张 × 388.3 点 × 0.0001 − 4.85U = +3.15U"""
        sig = _sig("short", entry=80892.7, sl=81205.5, tp1=80504.4, tp2=80067.6,
                   ts=1792796209, fill_ts=1792796640, fill_px=80892.7,
                   contracts=618, sid="BTC_USDT-1792796209-short")
        res = pt._settle_one(sig, self.bars)
        self.assertIsNotNone(res, "07:11 该触 TP1，却什么都没结算")
        self.assertEqual(res["outcome"], "BE_STOPPED")
        self.assertEqual(res["exit_px"], 80892.7, "BE 出场价应等于入场价")
        self.assertEqual(res["exit_ts"], 1792797600, "BE 应在 07:20 那根影线触发")
        self.assertEqual(res["category"], "profit", "TP1 已锁利，BE 出场应算盈利")
        self.assertAlmostEqual(res["net_usd"], 3.15, places=1,
                               msg="净利 ≈ +3.15U（TP1 段 +8.00 − 手续费 4.85）")


class TestRiskGuardSlWritesCanonicalNet(unittest.TestCase):
    """★ 2026-10-25 回归：risk_guard SL 分支必须写 canonical net/gross/category + outcome event

    Bug：SL 分支只写 exit_ts/exit_px/status/outcome/pos_state/risk_guard_exit_*，
    不写 net_usd/gross_usd/category，也不 append outcome event →
      · daily_report 把 null net 当 0（v4.2 净利虚高，23:15 那笔 -15.12U 消失）
      · dashboard 胜率漏统计这些单（web/server.py 只认 position_events.jsonl）
    """

    def setUp(self):
        import tempfile, shutil as _sh, importlib
        self.tmp = tempfile.mkdtemp(prefix="rg_sl_test_")
        self._sh = _sh
        self.tmp_events = os.path.join(self.tmp, "position_events.jsonl")
        # ★ 2026-10-25 修（严重）：原来先 patch 再 importlib.reload(_pt)，
        #   reload 会**重新执行模块体** → EVENTS = os.path.join(DATA_DIR, ...)
        #   把我的 patch 覆盖掉 → 测试把假数据写进**真实** /root/data/position_events.jsonl
        #   （实测污染 7 行 BTC_USDT-1000/1002/1003，且让同 id 的后续断言互相干扰）。
        #   正确顺序：先 reload 让模块干净，再 patch 模块属性。
        import position_tracker as _pt
        importlib.reload(_pt)
        self.pt = _pt
        self.patchers = [
            patch("position_tracker.EVENTS", self.tmp_events),
            patch("position_tracker.DATA_DIR", self.tmp),
            # ★ 2026-10-26 修：risk_guard._apply_sl 的 log() 落到 watch.common.LOG，
            #   那是**生产** /root/data/watch.log。test_canonical_close_failure_leaves_net_empty
            #   故意造 entry=0 触发结算失败 → 每跑一次测试就往生产日志写一行假错误
            #   「BTC_USDT-1000-short canonical 结算失败」（实测累积 17 行），
            #   把真错误淹掉、误导排错。测试期间日志必须也隔离到 tmp。
            patch("watch.common.LOG", os.path.join(self.tmp, "watch.log")),
        ]
        for p in self.patchers:
            p.start()
        # 真实文件指纹，tearDown 校验：测试绝不许碰生产数据
        self._real_events = "/root/data/position_events.jsonl"
        self._real_fingerprint = self._fp(self._real_events)

    @staticmethod
    def _fp(path):
        if not os.path.exists(path):
            return None
        import hashlib
        return hashlib.sha256(open(path, "rb").read()).hexdigest()

    def tearDown(self):
        import importlib, position_tracker as _pt
        for p in self.patchers:
            p.stop()
        importlib.reload(_pt)
        # ★ 强制断言：测试期间真实 position_events.jsonl 一个字节都不许变
        assert self._fp(self._real_events) == self._real_fingerprint, (
            "测试污染了生产 position_events.jsonl！")
        self._sh.rmtree(self.tmp, ignore_errors=True)

    def assert_wrote_to_tmp_only(self):
        """显式证明写的是 tmp 文件，不是真实文件。"""
        self.assertEqual(self.pt.EVENTS, self.tmp_events,
                         "EVENTS 没指向 tmp —— patch 被 reload 覆盖了？")
        self.assertTrue(os.path.exists(self.tmp_events),
                        "预期事件应写进 tmp 文件")

    def _rec(self, **kw):
        base = {
            "id": "BTC_USDT-1000-short", "contract": "BTC_USDT", "side": "short",
            "entry": 80012.6, "sl": 80175.7, "tp1": 79628.6, "tp2": 79196.5,
            "status": "pending", "filled": True,
            "fill_ts": 1792860540, "fill_px": 80012.6, "contracts": 625,
        }
        base.update(kw)
        return base

    def test_sl_writes_net_gross_category(self):
        """SL 平仓后 journal 记录必须带 net_usd/gross_usd/category"""
        from watch import risk_guard as rg
        rec = self._rec()
        ev = rg._apply_sl(rec, last=80177.0, now_ts=1792860634)
        self.assertEqual(rec["status"], "settled")
        for f in ("net_usd", "gross_usd", "category"):
            self.assertIn(f, rec, f"SL 平仓没写 {f} —— risk_guard SL 分支回退了？")
        self.assertEqual(rec["net_usd"], -15.12)
        self.assertEqual(rec["gross_usd"], -10.27)
        self.assertEqual(rec["category"], "loss")
        self.assertIsNotNone(ev, "_apply_sl 必须返回 outcome event")

    def test_sl_event_matches_canonical_close(self):
        """risk_guard 算出的 net 必须 == position_tracker._close（同一公式）"""
        from watch import risk_guard as rg
        rec = self._rec()
        ev = rg._apply_sl(rec, last=80177.0, now_ts=1792860634)
        res = self.pt._close(self._rec(), "SL_STOPPED", 80177.0, 1792860634,
                             625, "short", 80012.6, "loss")
        self.assertEqual(ev["net_usd"], res["net_usd"],
                         "risk_guard 与 position_tracker._close 口径不一致")
        self.assertEqual(ev["category"], res["category"])

    def test_tp1_then_be_is_profit(self):
        """TP1 锁利后被保本打掉 → category 必须是 profit（不是 loss）"""
        from watch import risk_guard as rg
        rec = self._rec(id="BTC_USDT-1001-short", contracts=409,
                        tp1_hit=True, tp1_hit_ts=1792720800, tp1_hit_px=81239.2,
                        entry=81631.1, active_sl=81631.1, _orig_contracts=613,
                        fill_ts=1792720020)
        ev = rg._apply_sl(rec, last=81643.4, now_ts=1792722618)
        self.assertEqual(rec["category"], "profit")
        self.assertEqual(rec["net_usd"], 2.64)
        self.assertEqual(rec["gross_usd"], 7.49)

    def test_outcome_event_idempotent(self):
        """同一 id 的 outcome event 只能有一条（重复 backfill 不许刷事件）"""
        from watch import risk_guard as rg
        rec = self._rec()
        ev = rg._apply_sl(rec, last=80177.0, now_ts=1792860634)
        self.assertTrue(rg._append_outcome_event(ev), "首次 append 必须成功")
        self.assertFalse(rg._append_outcome_event(ev), "第二次 append 必须被幂等跳过")
        self.assert_wrote_to_tmp_only()
        n = sum(1 for ln in open(self.tmp_events, encoding="utf-8")
                if ln.strip() and "BTC_USDT-1000-short" in ln)
        self.assertEqual(n, 1, f"outcome event 被写了 {n} 次")

    def test_partial_event_is_not_an_outcome_event(self):
        """EMA 减仓事件（带 kind 无 outcome）不能算「已结算」，否则真正的结算事件会被跳过"""
        self.pt._append_event({"id": "BTC_USDT-1002-long", "kind": "ema_break_partial_close",
                               "partial_contracts": 415, "ts": 1})
        self.assert_wrote_to_tmp_only()
        self.assertFalse(self.pt.has_outcome_event("BTC_USDT-1002-long"),
                         "partial 事件被误判成 outcome 事件")

    def test_has_outcome_event_detects_settlement(self):
        self.assertFalse(self.pt.has_outcome_event("BTC_USDT-1003-long"),
                         "测试污染残留：这条 id 已在真实 events 里，说明隔离失效")
        self.pt._append_event({"id": "BTC_USDT-1003-long", "outcome": "SL_STOPPED",
                               "net_usd": -1.0, "category": "loss",
                               "exit_ts": 2, "ts": 3})
        self.assert_wrote_to_tmp_only()
        self.assertTrue(self.pt.has_outcome_event("BTC_USDT-1003-long"))

    def test_events_path_is_isolated(self):
        """★ 2026-10-25 回归：patch 必须真的生效（否则污染生产数据）"""
        self.assertEqual(self.pt.EVENTS, self.tmp_events)
        self.assertNotEqual(self.pt.EVENTS, "/root/data/position_events.jsonl")
        self.pt._append_event({"id": "BTC_USDT-1004-long", "outcome": "SL_STOPPED",
                               "net_usd": -1.0, "category": "loss",
                               "exit_ts": 2, "ts": 3})
        self.assertTrue(os.path.exists(self.tmp_events), "写错地方了：tmp 没有文件")
        self.assertFalse(
            any("BTC_USDT-1004-long" in ln
                for ln in open("/root/data/position_events.jsonl", encoding="utf-8")),
            "测试数据泄漏进了生产 position_events.jsonl！")

    def test_canonical_close_failure_leaves_net_empty(self):
        """canonical 算不出来时必须留空 net，不许编造数字"""
        from watch import risk_guard as rg
        rec = self._rec(entry=0)          # 无 entry → _canonical_close 抛错
        ev = rg._apply_sl(rec, last=80177.0, now_ts=1792860634)
        self.assertIsNone(ev, "canonical 失败时不该返回 event")
        self.assertIsNone(rec.get("net_usd"), "canonical 失败时不该写 net")
        self.assertEqual(rec["status"], "settled", "仍要标 settled 防重复触发")


class TestDailyReportNullNet(unittest.TestCase):
    """★ 2026-10-25 回归：daily_report 不许把 null net 静默当 0"""

    def test_null_net_surfaced_not_zeroed(self):
        import daily_report as dr
        entries = [
            {"id": "BTC_USDT-1-short", "ts": 1792792818, "entry": 100.0,
             "status": "settled", "outcome": "TP2_DONE", "net_usd": 37.13},
            # 已结算但 net 缺失 —— 老代码会当 0 计入
            {"id": "BTC_USDT-2-short", "ts": 1792796209, "entry": 100.0,
             "status": "settled", "outcome": "SL_STOPPED", "net_usd": None},
        ]
        rows = [[1792792818, 100.0, 110.0, 90.0, 105.0, 1],
                [1792792828, 105.0, 110.0, 90.0, 105.0, 1]]
        r = dr.simulate_teacher_TP(entries, rows, 1792792818, 1792792838)
        self.assertEqual(r["null_net_count"], 1,
                         "null-net 结算没被暴露出来")
        self.assertEqual(r["null_net_settlements"][0]["journal_id"],
                         "BTC_USDT-2-short")
        self.assertEqual(r["v4_2_net_usd"], 37.13,
                         "null net 被静默当 0 计入了合计")

    def test_no_null_net_reports_clean(self):
        import daily_report as dr
        entries = [{"id": "BTC_USDT-3-short", "ts": 1792792818, "entry": 100.0,
                    "status": "settled", "outcome": "TP2_DONE", "net_usd": 1.5}]
        rows = [[1792792818, 100.0, 110.0, 90.0, 105.0, 1]]
        r = dr.simulate_teacher_TP(entries, rows, 1792792818, 1792792838)
        self.assertEqual(r["null_net_count"], 0)

    def test_open_price_uses_bar_open_not_close(self):
        """开盘价必须取 win_15m[0][1]（open），不是 win_15m[0][4]（close）"""
        import inspect
        import daily_report as dr
        src = inspect.getsource(dr.build_report)
        self.assertIn("first_c = win_15m[0][1]", src,
                      "daily_report 又退回用 close 当开盘价了（应为 win_15m[0][1]）")


class TestDashboardRejectsSyntheticIds(unittest.TestCase):
    """★ 2026-10-25 回归：合成 id 不许进胜率统计

    背景：单测曾把假事件写进真实 position_events.jsonl（patch 被 importlib.reload
    覆盖），而 BTC_USDT-1000-short 这类 id 既不带 TEST- 前缀、又以 BTC_USDT- 开头，
    同时穿过 _winrate 的前缀过滤和 _all_settled 的白名单 →
    实测把胜率从 38.2%(n=68) 拉成 37.1%(n=70)、合计净利 +56.78U 虚成 +40.66U。
    """

    @classmethod
    def setUpClass(cls):
        import importlib.util, tempfile
        root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        spec = importlib.util.spec_from_file_location(
            "btc_status_server", os.path.join(root, "web", "server.py"))
        cls.server = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.server)
        cls.tmp = tempfile.mkdtemp(prefix="winrate_test_")

    @classmethod
    def tearDownClass(cls):
        import shutil
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def _write_events(self, events):
        import json as _json
        p = os.path.join(self.tmp, "position_events.jsonl")
        with open(p, "w", encoding="utf-8") as f:
            for e in events:
                f.write(_json.dumps(e, ensure_ascii=False) + "\n")
        return p

    def test_synthetic_id_is_flagged(self):
        s = self.server
        self.assertTrue(s._is_test_id("BTC_USDT-1000-short"),
                        "合成短数字段 id 必须被判定为测试数据")
        self.assertTrue(s._is_test_id("BTC_USDT-1002-long"))
        self.assertTrue(s._is_test_id("TEST-FULL-1790000000-long"))
        self.assertTrue(s._is_test_id("test-debug-001"))
        # 真实生产 id（10 位 unix 秒）绝不能被误杀
        self.assertFalse(s._is_test_id("BTC_USDT-1792854921-short"))
        self.assertFalse(s._is_test_id("BTC_USDT-1792868423-short"))
        self.assertFalse(s._is_test_id("BTC_USDT-1792594815-long"))

    def test_synthetic_id_cannot_enter_winrate(self):
        """合成 id 混进 events 也不能污染胜率/合计"""
        s = self.server
        real = [
            {"id": "BTC_USDT-1792854921-short", "outcome": "SL_STOPPED",
             "net_usd": -15.12, "category": "loss", "exit_ts": 1, "ts": 1},
            {"id": "BTC_USDT-1792699207-short", "outcome": "TP1_PARTIAL",
             "net_usd": 2.64, "category": "profit", "exit_ts": 2, "ts": 2},
        ]
        fake = [
            {"id": "BTC_USDT-1000-short", "outcome": "SL_STOPPED",
             "net_usd": -15.12, "category": "loss", "exit_ts": 3, "ts": 3},
            {"id": "BTC_USDT-1002-long", "outcome": "BE_STOPPED",
             "net_usd": 99.0, "category": "profit", "exit_ts": 4, "ts": 4},
        ]
        orig = s.DATA_DIR
        try:
            from pathlib import Path as _P
            s.DATA_DIR = _P(self.tmp)
            self._write_events(real + fake)
            wr = s._winrate()
            self.assertEqual(wr["total"], 2,
                             f"合成 id 进了胜率统计 (total={wr['total']})")
            self.assertAlmostEqual(wr["net_usd_total"], -12.48, places=2,
                                   msg="合计净利被合成 id 污染")
            ids = [x["id"] for x in wr["latest_5"]]
            self.assertFalse(any(i.startswith("BTC_USDT-100") for i in ids),
                             f"latest_5 里出现了合成 id: {ids}")
            # _all_settled 同样要过滤
            got = {r["id"] for r in s._all_settled()}
            self.assertEqual(got, {"BTC_USDT-1792854921-short",
                                   "BTC_USDT-1792699207-short"})
        finally:
            s.DATA_DIR = orig

    def test_real_ids_still_count(self):
        """反向回归：过滤别加过头，真实生产 id 必须照常统计"""
        s = self.server
        orig = s.DATA_DIR
        try:
            from pathlib import Path as _P
            s.DATA_DIR = _P(self.tmp)
            self._write_events([
                {"id": "BTC_USDT-1792854921-short", "outcome": "SL_STOPPED",
                 "net_usd": -15.12, "category": "loss", "exit_ts": 1, "ts": 1},
            ])
            wr = s._winrate()
            self.assertEqual(wr["total"], 1)
            self.assertAlmostEqual(wr["net_usd_total"], -15.12, places=2)
        finally:
            s.DATA_DIR = orig


if __name__ == "__main__":
    unittest.main(verbosity=2)
