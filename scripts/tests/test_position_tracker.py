#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
position_tracker.py 单元测试
=============================

跑法：
    cd scripts && python3 -m unittest tests.test_position_tracker -v

不依赖真实 journal / Telegram——用 mock 拦截副作用。
"""
import os, sys, json, tempfile, unittest
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import position_tracker as pt


def _bar(ts, o, h, l, c):
    """构造一根 K 线（ts_ms, o, h, l, c, vol）"""
    return [ts, o, h, l, c, 100]


def _make_sig(side, entry, sl, tp1, tp2=0, ts=1000, filled=False):
    """构造一个 journal 信号 dict"""
    return {
        "id": f"test-{ts}",
        "contract": "BTC_USDT",
        "ts": ts,
        "side": side,
        "entry": entry,
        "sl": sl,
        "tp1": tp1,
        "tp2": tp2,
        "status": "pending",
        "filled": filled,
        "sl_pct": 0.008,
    }


class TestSettleOneLong(unittest.TestCase):
    """做多路径"""

    def setUp(self):
        # 拦截所有 TG 推送和 journal 读写副作用
        self.fill_patches = [
            patch("position_tracker._notify_fill"),
            patch("position_tracker._notify_partial"),
            patch("position_tracker._notify_close"),
            patch("position_tracker._read_journal", return_value=[]),
            patch("position_tracker._write_journal"),
            # ★ 2026-10-06：拦截 EMA break warning 的 K 线抓取（否则会真去抓数据）
            patch("position_tracker.fetch_ohlcv", return_value=[]),
        ]
        for p in self.fill_patches:
            p.start()

    def tearDown(self):
        for p in self.fill_patches:
            p.stop()

    def test_long_fill_then_tp1_partial_then_be_stopped(self):
        """
        路径：挂单 → 成交 → 涨到 TP1（1/3 锁利，SL 移到 BE）→ 回打到 BE 被打
        期望：outcome=BE_STOPPED, category=profit（因为锁了 TP1 部分利润）
        """
        sig = _make_sig("long", entry=100, sl=99, tp1=101, ts=1000)
        # ts=1005 成交（low=100 触 entry）
        # ts=1010 涨到 high=101 触 TP1（部分止盈，SL 移到 BE=100）
        # ts=1015 回打 low=99.5（仍在原 SL 99 之上，但现在 active_sl=100 → BE）
        #   long 的 BE 判定是 l <= cur_sl=100 → low=99.5 触发
        bars = [
            _bar(1005, 100.0, 100.2, 100.0, 100.1),  # 成交
            _bar(1010, 100.1, 101.5, 100.0, 101.2),  # TP1 触发
            _bar(1015, 101.0, 101.1, 99.5, 99.6),    # BE 被打（active_sl=100）
        ]
        res = pt._settle_one(sig, bars)
        self.assertIsNotNone(res)
        self.assertEqual(res["outcome"], "BE_STOPPED")
        self.assertEqual(res["category"], "profit")
        # 净利 = (101-100)*q/3 - FEE（TP1 锁 1/3）+ (99.5-100)*2q/3 - 没手续费
        #       = q/3*1 + q*(-0.5)*2/3 = q*(1/3 - 1/3) = 0（粗略） - 4.85
        # contracts = round(5000 / (100 * 0.0001)) = 500000
        contracts = 500000
        # TP1 已平 500000/3 = 166666 张（int 除法），价 101，盈 = 1 * 166666 * 0.0001 = 16.67
        # 剩余部分按 cur_sl=100（BE）平，盈 = 0
        # gross = 16.67, net = 16.67 - 4.85 = 11.82
        self.assertAlmostEqual(res["net_usd"], 17.67, places=2)

    def test_long_sl_only_no_tp1(self):
        """未触发 TP1，直接打止损 → 亏损归类"""
        sig = _make_sig("long", entry=100, sl=99, tp1=101, ts=1000)
        bars = [
            _bar(1005, 100.0, 100.2, 100.0, 100.1),  # 成交
            _bar(1010, 99.8, 99.9, 98.5, 98.6),      # SL 触发
        ]
        res = pt._settle_one(sig, bars)
        self.assertIsNotNone(res)
        self.assertEqual(res["outcome"], "SL_STOPPED")
        self.assertEqual(res["category"], "loss")

    def test_long_tp1_then_tp2_full_close(self):
        """TP1 部分止盈 → TP2 全平"""
        sig = _make_sig("long", entry=100, sl=99, tp1=101, tp2=102, ts=1000)
        bars = [
            _bar(1005, 100.0, 100.2, 100.0, 100.1),  # 成交
            _bar(1010, 100.1, 101.5, 100.0, 101.2),  # TP1 触发
            _bar(1015, 101.2, 102.5, 101.0, 102.3),  # TP2 触发
        ]
        res = pt._settle_one(sig, bars)
        self.assertIsNotNone(res)
        self.assertEqual(res["outcome"], "TP2_DONE")
        self.assertEqual(res["category"], "profit")
        # contracts = 500000
        # 1/3 @ 101 + 2/3 @ 102
        # gross = (101-100)*166666*0.0001 + (102-100)*333334*0.0001
        #       = 1*16.67 + 2*33.33 = 16.67 + 66.67 = 83.33
        # net = 83.33 - 4.85 = 78.48
        self.assertAlmostEqual(res["net_usd"], 84.33, places=1)

    def test_long_not_filled_yet_misses(self):
        """24h 没成交 → MISSED（未进场不返佣）"""
        sig = _make_sig("long", entry=100, sl=99, tp1=101, ts=1000)
        # ts - pending_ts > 24h 才会 MISSED
        ts_after_24h = 1000 + 24 * 3600 + 100
        bars = [
            _bar(ts_after_24h, 105.0, 106.0, 104.0, 105.5),  # 价已远离 entry
        ]
        res = pt._settle_one(sig, bars)
        self.assertIsNotNone(res)
        self.assertEqual(res["outcome"], "MISSED")
        self.assertEqual(res["category"], "skip")
        # 2026-10-07 liusir 规则：未进场的挂单标记作废，不返佣，net_usd = 0
        self.assertAlmostEqual(res["net_usd"], 0.0, places=2)
        self.assertEqual(res["note"], "24h 未成交")

    def test_long_same_bar_tp_sl_priority(self):
        """
        同根双触（防自欺）：未触发 TP1 前，TP/SL 同根 → SL（保守判）
        long：entry=100, sl=99, tp1=101
        K 线 high=101, low=99 → 同根触 TP 和 SL → 应判 SL
        """
        sig = _make_sig("long", entry=100, sl=99, tp1=101, ts=1000, filled=True)
        bars = [
            _bar(1005, 100, 101, 99, 100),  # 同根双触
        ]
        res = pt._settle_one(sig, bars)
        self.assertIsNotNone(res)
        # 防自欺：未触发 TP1 前，TP/SL 同根 → 走"SL"分支（不是 "SL_STOPPED"）
        self.assertEqual(res["outcome"], "SL")

    def test_long_tp1_partial_state_persisted(self):
        """TP1 触发后 active_sl 移到 BE，sig 应被 mutate"""
        sig = _make_sig("long", entry=100, sl=99, tp1=101, ts=1000)
        bars = [
            _bar(1005, 100, 100.2, 100, 100.1),
            _bar(1010, 100.1, 101.5, 100, 101.2),  # TP1
            _bar(1015, 101, 101.1, 100.3, 100.5),  # 没触 BE 也没触 TP2
        ]
        res = pt._settle_one(sig, bars)
        self.assertIsNone(res)  # 还没结束
        self.assertTrue(sig["tp1_hit"])
        self.assertEqual(sig["active_sl"], 100)  # 已移到 BE
        self.assertEqual(sig["pos_state"], "TP1_PARTIAL")

    def test_long_partial_close_when_tp1_not_hit_after_1h_with_loss(self):
        """2026-10-06：TP1 未触发 + 持仓 > 1h + 当前浮亏 → 减仓 50% 标记 PARTIAL_CLOSE

        long entry=100，TP1=101，SL=99。fill_ts=1000。
        模拟 1h 后的 K 线：成交后价格从未摸到 TP1，且当前 close=99.5（在 entry 之下、SL 之上）。
        期望：settle_one 返回 PARTIAL_CLOSE + 50% 张数被减仓。

        注意：PARTIAL_CLOSE_AFTER_SEC 默认 24h（liusir 决策：默认关闭此规则）。
        测试用 monkeypatch 临时把阈值改成 3600 让它触发。
        """
        sig = _make_sig("long", entry=100, sl=99, tp1=101, ts=1000, filled=True)
        sig["fill_ts"] = 1000
        sig["contracts"] = 600      # 600 张，50% = 300
        # 1h = 3600s，所以 fill_ts=1000 → 触发时间必须 > 4600
        # bar 序列 ts 是绝对时间，不是相对时间——之前我搞错了
        bars = [
            _bar(1060, 100.0, 100.1, 99.8, 100.0),       # 成交后 1min（未到 1h）
            _bar(1115, 100.0, 100.05, 99.6, 99.7),       # 成交后 ~2min（未到 1h）
            _bar(4615, 99.7, 99.9, 99.4, 99.5),           # 成交后 1h+1min，触发 PARTIAL_CLOSE
        ]
        # 临时把阈值改成 3600 触发 PARTIAL_CLOSE（默认 24h 不触发）
        with patch("position_tracker.PARTIAL_CLOSE_AFTER_SEC", 3600):
            res = pt._settle_one(sig, bars)
        self.assertIsNotNone(res, "应触发 PARTIAL_CLOSE 主动减仓")
        self.assertEqual(res["outcome"], "PARTIAL_CLOSE")
        # sig["contracts"] 应减半
        self.assertEqual(sig["contracts"], 300)
        # 触发后 partial_close_done=True
        self.assertTrue(sig.get("partial_close_done"))
        # SL 移到 BE
        self.assertEqual(sig.get("active_sl"), 100)

    def test_long_no_partial_close_if_tp1_already_hit(self):
        """TP1 已触发 → 不再触发 PARTIAL_CLOSE（让 TP2/BE 逻辑继续跑）"""
        sig = _make_sig("long", entry=100, sl=99, tp1=101, ts=1000, filled=True)
        sig["fill_ts"] = 1000
        sig["contracts"] = 600
        bars = [
            _bar(1005, 100, 100.2, 100, 100.1),
            _bar(1010, 100.1, 101.5, 100, 101.2),    # TP1 触发（ts=1010）
            _bar(4615, 101, 101.1, 100.3, 100.5),    # 1h+ 后 close=100.5（TP1 已 hit，不会 PARTIAL_CLOSE）
        ]
        with patch("position_tracker.PARTIAL_CLOSE_AFTER_SEC", 3600):
            res = pt._settle_one(sig, bars)
        # 因为 TP1 已 hit，不应被 PARTIAL_CLOSE 拦截
        self.assertIsNone(res, "TP1 已 hit，不应触发 PARTIAL_CLOSE")
        self.assertTrue(sig.get("tp1_hit"))

    def test_long_no_partial_close_if_within_1h(self):
        """持仓 < 1h → 不触发 PARTIAL_CLOSE（给机会触 TP1）"""
        sig = _make_sig("long", entry=100, sl=99, tp1=101, ts=1000, filled=True)
        sig["fill_ts"] = 1000
        sig["contracts"] = 600
        bars = [
            _bar(1060, 100.0, 100.1, 99.7, 99.9),   # 仅过 60s，不足 1h
        ]
        with patch("position_tracker.PARTIAL_CLOSE_AFTER_SEC", 3600):
            res = pt._settle_one(sig, bars)
        self.assertIsNone(res, "持仓 < 1h 不应触发 PARTIAL_CLOSE")
        self.assertFalse(sig.get("partial_close_done"))

    def test_long_held_over_24h_no_timeout(self):
        """2026-10-07 liusir 规则：TIMEOUT 24h 强平已取消——持仓超过 24h 仍 pending。

        强平仅靠 watch/runner.py:_check_trend_reversal 检测趋势反转。
        """
        sig = _make_sig("long", entry=100, sl=99, tp1=101, ts=1000, filled=True)
        sig["fill_ts"] = 1000
        sig["contracts"] = 5000
        # 24h + 1m 仍稳价 → 不触发任何强平
        ts_after_24h = 1000 + 24 * 3600 + 60
        bars = [
            _bar(ts_after_24h, 100.1, 100.2, 100.0, 100.1),   # 价格几乎不动
        ]
        flat_bars = [[i*300, 100, 100, 100, 100, 100] for i in range(200)]
        with patch("position_tracker.fetch_ohlcv", return_value=flat_bars):
            res = pt._settle_one(sig, bars)
        self.assertIsNone(res, "持仓 > 24h 不应被强平（24h TIMEOUT 已取消）")
        self.assertNotEqual(sig.get("outcome"), "TIMEOUT")


class TestSettleOneShort(unittest.TestCase):
    """做空路径（镜像）"""

    def setUp(self):
        self.fill_patches = [
            patch("position_tracker._notify_fill"),
            patch("position_tracker._notify_partial"),
            patch("position_tracker._notify_close"),
            patch("position_tracker._read_journal", return_value=[]),
            patch("position_tracker._write_journal"),
        ]
        for p in self.fill_patches:
            p.start()

    def tearDown(self):
        for p in self.fill_patches:
            p.stop()

    def test_short_sl_triggered(self):
        """做空 SL 触发条件：high >= cur_sl"""
        sig = _make_sig("short", entry=100, sl=101, tp1=99, ts=1000, filled=True)
        bars = [
            _bar(1005, 101, 101.5, 100.8, 101.2),  # high >= 101 触 SL
        ]
        res = pt._settle_one(sig, bars)
        self.assertIsNotNone(res)
        self.assertEqual(res["outcome"], "SL_STOPPED")
        self.assertEqual(res["category"], "loss")

    def test_short_tp1_triggered(self):
        """做空 TP1 触发条件：low <= tp1"""
        sig = _make_sig("short", entry=100, sl=101, tp1=99, ts=1000, filled=True)
        bars = [
            _bar(1005, 99.8, 99.9, 98.5, 98.6),  # low <= 99 触 TP1
        ]
        res = pt._settle_one(sig, bars)
        # TP1 触发但不结算（partial），还应等后续
        self.assertIsNone(res)
        self.assertTrue(sig["tp1_hit"])
        self.assertEqual(sig["active_sl"], 100)  # BE 上移到 entry


class TestContracts(unittest.TestCase):
    """张数计算"""

    def test_contracts_basic(self):
        # entry=100, 50U/100x = 5000U 名义 / 100*0.0001 = 500,000
        self.assertEqual(pt._contracts(100), 500000)

    def test_contracts_high_price(self):
        # entry=84,638, 5000/(84638*0.0001) = 5000/8.4638 = 590.7 → 591
        self.assertEqual(pt._contracts(84638.2), 591)

    def test_contracts_zero_price(self):
        self.assertEqual(pt._contracts(0), 0)


class TestPnl(unittest.TestCase):
    """损益计算"""

    def test_long_profit(self):
        # long 100 → 101, 500000 张, 0.0001 multiplier
        # pnl = 1 * 500000 * 0.0001 = 50
        self.assertAlmostEqual(pt._pnl_usd("long", 100, 101, 500000), 50.0)

    def test_long_loss(self):
        # long 100 → 99, 500000 张
        # pnl = -1 * 500000 * 0.0001 = -50
        self.assertAlmostEqual(pt._pnl_usd("long", 100, 99, 500000), -50.0)

    def test_short_profit(self):
        # short 100 → 99, 500000 张
        # pnl = +1 * 500000 * 0.0001 = 50
        self.assertAlmostEqual(pt._pnl_usd("short", 100, 99, 500000), 50.0)

    def test_short_loss(self):
        # short 100 → 101, 500000 张
        # pnl = -1 * 500000 * 0.0001 = -50
        self.assertAlmostEqual(pt._pnl_usd("short", 100, 101, 500000), -50.0)


class TestEmaBreakWarning(unittest.TestCase):
    """5m/15m EMA144+EMA169 同向突破预警 + 70% 部分止盈 + SL→BE

    触发条件：
      - 持仓已成交 + 盈利
      - 5m close 双跌破 EMA144 + EMA169
      - 15m close 双跌破 EMA144 + EMA169
    """

    def setUp(self):
        # 拦截副作用（TG 推送、journal 读写、events 追加）
        self.patches = [
            patch("position_tracker._notify_ema_warning"),
            patch("position_tracker._append_event"),
            patch("position_tracker._read_journal", return_value=[]),
            patch("position_tracker._write_journal"),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()

    def _make_bars(self, last_close, count=200):
        """构造 200 根 K 线，**全部接近 last_close** → close 与 EMA 几乎相等"""
        # 用 `from common import _bar` 太繁琐，直接 inline
        return [[i * 300, last_close, last_close, last_close, last_close, 100]
                for i in range(count)]

    def _make_falling_bars(self, end_close, start, count=200):
        """构造 200 根单调下跌的 K 线 → 后期 close 远低于前期 → EMA 也偏低"""
        # 让 close 从 start 单调下降到 end_close
        step = (start - end_close) / (count - 1)
        bars = []
        for i in range(count):
            c = start - step * i
            bars.append([i * 300, c, c, c, c, 100])
        return bars

    def test_long_profit_break_below_both_emas_triggers_partial_close(self):
        """做多 + 盈利 + 5m/15m 双跌破 → 触发 70% 部分止盈 + SL 移到 BE"""
        sig = _make_sig("long", entry=100, sl=99, tp1=101, ts=1000, filled=True)
        sig["fill_px"] = 100
        sig["fill_ts"] = 1005
        sig["contracts"] = 5000     # 5000 张，70% = 3500 张

        # 5m / 15m 都构造"严重跌破"：构造最后一根 close 远低于 EMA 的 K 线
        # 起始 110，最后一根 80 — EMA 大概在中间 → 远在 EMA 之下
        bars_5m = self._make_falling_bars(end_close=80, start=110, count=200)
        bars_15m = self._make_falling_bars(end_close=80, start=110, count=200)

        def _fake_fetch(contract, interval, *_args, **_kwargs):
            return {"5m": bars_5m, "15m": bars_15m}.get(interval, [])

        with patch("position_tracker.fetch_ohlcv", side_effect=_fake_fetch):
            triggered = pt._check_ema_break_warning(
                sig, ts=2000, side="long", entry=100,
                cur_px=105, fill_px=100,   # 盈利 +5
            )

        self.assertTrue(triggered, "应该触发预警")
        self.assertTrue(sig.get("ema_warning_done"))
        # 部分止盈 70%：5000 → 3500 平 / 1500 留
        self.assertEqual(sig["contracts"], 1500)
        # SL 移到 BE = 开仓价 100
        self.assertEqual(sig["active_sl"], 100)
        # 部分平仓计数
        self.assertEqual(sig["partial_exit_count"], 1)
        # partial_net_usd 应为正（多单 +5 × 3500 张）
        # +5 × 3500 × 0.0001 = +1.75
        self.assertAlmostEqual(sig["partial_net_usd"], 1.75, places=2)

    def test_long_no_trigger_if_not_profitable(self):
        """做多但当前价 ≤ fill_px（不盈利）→ 不触发"""
        sig = _make_sig("long", entry=100, sl=99, tp1=101, ts=1000, filled=True)
        sig["fill_px"] = 100
        sig["contracts"] = 5000

        bars = self._make_falling_bars(end_close=80, start=110, count=200)
        with patch("position_tracker.fetch_ohlcv", return_value=bars):
            triggered = pt._check_ema_break_warning(
                sig, ts=2000, side="long", entry=100,
                cur_px=95, fill_px=100,   # 亏损 -5
            )

        self.assertFalse(triggered, "不盈利时不应触发")
        self.assertEqual(sig["contracts"], 5000)
        self.assertNotIn("active_sl", sig)

    def test_long_no_trigger_if_15m_not_darving(self):
        """5m 跌破但 15m 没跌破 → 不触发"""
        sig = _make_sig("long", entry=100, sl=99, tp1=101, ts=1000, filled=True)
        sig["fill_px"] = 100
        sig["contracts"] = 5000

        bars_5m = self._make_falling_bars(end_close=80, start=110, count=200)
        # 15m 平稳 → 不会跌破
        bars_15m = self._make_bars(last_close=100, count=200)

        def _fake_fetch(contract, interval, *_args, **_kwargs):
            return {"5m": bars_5m, "15m": bars_15m}.get(interval, [])

        with patch("position_tracker.fetch_ohlcv", side_effect=_fake_fetch):
            triggered = pt._check_ema_break_warning(
                sig, ts=2000, side="long", entry=100,
                cur_px=105, fill_px=100,
            )

        self.assertFalse(triggered, "只有 5m 跌破时不应触发")
        self.assertEqual(sig["contracts"], 5000)

    def test_short_profit_break_above_both_emas_triggers_partial_close(self):
        """做空 + 盈利 + 5m/15m 双涨破 → 触发 70% 部分止盈"""
        sig = _make_sig("short", entry=100, sl=101, tp1=99, ts=1000, filled=True)
        sig["fill_px"] = 100
        sig["fill_ts"] = 1005
        sig["contracts"] = 5000

        # 做空场景：构造"涨破"K 线（从 90 涨到 120 → 后期 close 远高于 EMA）
        bars_5m = self._make_falling_bars(end_close=120, start=90, count=200)
        bars_15m = self._make_falling_bars(end_close=120, start=90, count=200)

        def _fake_fetch(contract, interval, *_args, **_kwargs):
            return {"5m": bars_5m, "15m": bars_15m}.get(interval, [])

        with patch("position_tracker.fetch_ohlcv", side_effect=_fake_fetch):
            triggered = pt._check_ema_break_warning(
                sig, ts=2000, side="short", entry=100,
                cur_px=95, fill_px=100,   # 做空 +5 盈利
            )

        self.assertTrue(triggered, "做空场景应该触发")
        self.assertEqual(sig["contracts"], 1500)
        self.assertEqual(sig["active_sl"], 100)

    def test_warning_only_fires_once_per_position(self):
        """同一笔持仓只触发一次预警"""
        sig = _make_sig("long", entry=100, sl=99, tp1=101, ts=1000, filled=True)
        sig["fill_px"] = 100
        sig["contracts"] = 5000

        bars = self._make_falling_bars(end_close=80, start=110, count=200)
        with patch("position_tracker.fetch_ohlcv", return_value=bars):
            r1 = pt._check_ema_break_warning(sig, ts=2000, side="long", entry=100,
                                              cur_px=105, fill_px=100)
            r2 = pt._check_ema_break_warning(sig, ts=2001, side="long", entry=100,
                                              cur_px=106, fill_px=100)

        self.assertTrue(r1)
        self.assertFalse(r2, "第二次应被 ema_warning_done 拦掉")
        self.assertEqual(sig["contracts"], 1500)   # 只平了一次


class TestEmaWarningClosePnl(unittest.TestCase):
    """2026-10-07 修复：EMA 预警触发后立即 BE_STOPPED，_close 必须把 EMA 部分 PnL 计入

    Bug 现象：_close 只看 tp1_hit/tp2_hit，EMA 预警不写这两个标志，
              导致 net_usd 漏算预警的 +0.47U 部分盈利。
    修复后：EMA 预警分支存 ema_partial_qty / ema_partial_px，
              _close 检测到 ema_warning_done 时把这段算进 pnl_parts。
    """

    def _long_profit_bars(self):
        """long：成交后涨到 105 → 同根 EMA 跌破触发预警 + BE 兜底

        第 2 根 K 线同时满足：
          - cur_px=105 > fill_px=100 → 盈利
          - EMA 预警触发（5m/15m 双跌破，由 mock 模拟）
          - low=99.5 ≤ active_sl=100（BE）→ 触发 line 251-254 BE_STOPPED 分支
          - high=105.5 < tp1=101 不触发（实际生产场景中预警触发瞬间 high 多未达 TP1）

        真实生产中：EMA 预警 → SL 移到 BE → 同根 1m low ≤ entry → line 251 BE_STOPPED，
        不会触 line 244-245 的同根双触分支（line 244 在高 high ≥ tp1 时才命中，
        但 EMA 预警是跌落分支预触发，high 通常远低于 tp1）。
        """
        return [
            _bar(1005, 100.0, 100.2, 100.0, 100.1),  # 成交
            _bar(1010, 100.1, 105.5, 99.5, 105.0),   # cur=105 + low=99.5 同时触发 BE
        ]

    def test_long_ema_warn_then_be_stopped_pnl_includes_partial(self):
        """long + 触发 EMA 预警 + 同根 SL_STOPPED：net_usd 应包含预警的 +PnL

        注意：当前 _settle_one 走 line 251-252 SL_STOPPED 分支（因为 EMA 预警不写 tp1_hit
              → cur_tp1_hit=False → category=loss），但 exit_px 用 cur_sl=entry，
              且 _close() 的 gross_usd 已包含 EMA 部分（Bug 1 修复）。
        """
        sig = _make_sig("long", entry=100, sl=99, tp1=110, ts=1000, filled=True)  # tp1 调高避免触同根双触
        sig["fill_ts"] = 1005
        sig["fill_px"] = 100
        sig["contracts"] = 5000

        # 5m/15m 双跌破（用单调下跌的 K 线造出跌破场景）
        bars_5m = [[i*300, 110 - i*0.2, 110-i*0.2, 110-i*0.2, 110-i*0.2, 100]
                   for i in range(200)]
        # 最后一根 close ≈ 70，EMA ≈ 90 → close 远在 EMA 之下
        def _fake_fetch(contract, interval, *_a, **_kw):
            return bars_5m
        with patch("position_tracker.fetch_ohlcv", side_effect=_fake_fetch):
            res = pt._settle_one(sig, self._long_profit_bars())
        # 验证：EMA 预警已触发
        self.assertTrue(sig.get("ema_warning_done"))
        self.assertEqual(sig["active_sl"], 100, "SL 应移到 BE")
        # 验证：预警分支记下了 ema_partial_qty 和 _orig_contracts
        self.assertTrue(sig.get("ema_partial_qty"))
        self.assertEqual(sig["_orig_contracts"], 5000, "原始量应被记录")
        # 验证：res 不为 None（实走 SL_STOPPED 分支——这是 Bug 3，不在本次修复范围）
        self.assertIsNotNone(res)
        # ★ 关键验证：net_usd 应包含预警部分 PnL（Bug 1 修复的核心）
        # entry=100, exit_px=100 (BE), partial_qty=3500, ema_partial_px=105 (cur_px)
        # gross = (105-100) * 3500 * 0.0001 + (100-100) * 1500 * 0.0001
        #       = 1.75 + 0 = 1.75
        # FEE_USD = -1.0（返佣），所以 net = gross - FEE_USD = 1.75 - (-1.0) = 2.75
        self.assertAlmostEqual(res["gross_usd"], 1.75, places=2,
                               msg="gross 应包含 EMA 预警的部分 PnL")
        self.assertAlmostEqual(res["net_usd"], 2.75, places=2,
                               msg="net = gross + 1.0（FEE_USD=-1.0 是返佣）")
        # ★ 关键验证：partial_exits 应=2（EMA + 剩余 BE 兜底）
        self.assertEqual(res["partial_exits"], 2)

    def test_short_ema_warn_then_be_stopped_pnl_includes_partial(self):
        """short + 触发 EMA 预警 + 同根 SL_STOPPED：净利对称（亏损但净 PnL 仍应计入）"""
        sig = _make_sig("short", entry=100, sl=101, tp1=80, ts=1000, filled=True)  # tp1 调低避免触同根双触
        sig["fill_ts"] = 1005
        sig["fill_px"] = 100
        sig["contracts"] = 5000

        # 5m/15m 双涨破（单调上涨让 close 远高于 EMA）
        bars_5m = [[i*300, 90 + i*0.2, 90+i*0.2, 90+i*0.2, 90+i*0.2, 100]
                   for i in range(200)]
        def _fake_fetch(contract, interval, *_a, **_kw):
            return bars_5m
        # short 仓盈利场景：cur_px=95 < fill_px=100
        # 同时 1m 高点 high=100.5 ≥ active_sl=100（BE 兜底）
        bars_1m = [
            _bar(1005, 100.0, 100.2, 100.0, 100.1),  # 成交
            _bar(1010, 99.0, 100.5, 95.0, 95.0),    # 盈利 + 同根 BE 兜底
        ]
        with patch("position_tracker.fetch_ohlcv", side_effect=_fake_fetch):
            res = pt._settle_one(sig, bars_1m)
        self.assertIsNotNone(res)
        # short partial PnL: (100-95) * 3500 * 0.0001 = 1.75（盈利）
        # BE 兜底 1500 张: (100-100)*1500*0.0001 = 0
        # gross = 1.75, net = 1.75 + 1.0 = 2.75
        self.assertAlmostEqual(res["gross_usd"], 1.75, places=2)
        self.assertAlmostEqual(res["net_usd"], 2.75, places=2)

    def test_close_without_ema_warning_unaffected(self):
        """没触发 EMA 预警时 _close 行为不变（回归保护）"""
        sig = _make_sig("long", entry=100, sl=99, tp1=110, ts=1000, filled=True)  # tp1 调高避免触同根双触
        sig["fill_ts"] = 1005
        sig["fill_px"] = 100
        # 不预设 contracts，让 _settle_one 按 entry 重算
        bars = [
            _bar(1005, 100.0, 100.2, 100.0, 100.1),  # 成交
            _bar(1010, 100.1, 109.9, 98.5, 98.6),    # 直接 SL（无预警触发）
        ]
        # 让 fetch_ohlcv 返回的 5m/15m 不跌破（平稳）→ 不触发预警
        flat_bars = [[i*300, 100, 100, 100, 100, 100] for i in range(200)]
        with patch("position_tracker.fetch_ohlcv", return_value=flat_bars):
            res = pt._settle_one(sig, bars)
        self.assertIsNotNone(res)
        self.assertEqual(res["outcome"], "SL_STOPPED")
        self.assertEqual(res["category"], "loss")
        # _contracts(100) = round(5000 / (100 * 0.0001)) = 500000 张
        contracts = pt._contracts(100)
        # SL 没移位，原 SL=99 被打：PnL = (99-100)*contracts*0.0001
        expected_gross = (99 - 100) * contracts * 0.0001
        expected_net = expected_gross - pt.FEE_USD  # FEE_USD=-1.0（返佣）
        self.assertAlmostEqual(res["gross_usd"], expected_gross, places=2)
        self.assertAlmostEqual(res["net_usd"], expected_net, places=2)


class TestAppendEventTestPrefixGuard(unittest.TestCase):
    """2026-10-05 防污染：_append_event 拦截 TEST- / test-* 开头的事件"""

    def setUp(self):
        # 用临时 EVENTS 路径
        import tempfile
        self.tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False)
        self.tmp_path = self.tmp.name
        self.tmp.close()
        self._orig_events = pt.EVENTS
        pt.EVENTS = self.tmp_path

    def tearDown(self):
        pt.EVENTS = self._orig_events
        try:
            os.remove(self.tmp_path)
        except OSError:
            pass

    def test_test_prefix_dropped(self):
        """TEST-FULL-xxx 之类不应写 events 文件"""
        pt._append_event({"id": "TEST-FULL-1790000000-long",
                         "outcome": "TP2_DONE", "net_usd": 100.0})
        with open(self.tmp_path) as f:
            data = f.read()
        self.assertEqual(data, "", "TEST- 开头的事件应被丢弃")

    def test_test_underscore_prefix_dropped(self):
        """test-xxx 小写前缀也丢弃"""
        pt._append_event({"id": "test-debug-001", "outcome": "TEST", "net_usd": 0.0})
        with open(self.tmp_path) as f:
            data = f.read()
        self.assertEqual(data, "")

    def test_real_id_appended(self):
        """正常 BTC_USDT-xxx 应正常写入"""
        pt._append_event({"id": "BTC_USDT-1790000000-long",
                         "outcome": "TP2_DONE", "net_usd": 10.5})
        with open(self.tmp_path) as f:
            data = f.read().strip()
        self.assertIn("BTC_USDT-1790000000-long", data)

    def test_empty_id_appended(self):
        """没 id 的事件（异常路径）保留原行为，写入"""
        pt._append_event({"outcome": "DEBUG", "net_usd": 0.0})
        with open(self.tmp_path) as f:
            data = f.read().strip()
        self.assertIn("DEBUG", data)


if __name__ == "__main__":
    unittest.main(verbosity=2)
