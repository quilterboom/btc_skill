#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
v2.1 波动率闸门回归测试（2026-10-24 新增）
============================================

锁定三件事：
  1. compute_vol_5d 的口径与回测完全一致（只用已收盘 4h bar，ddof=1，取最后 30 根）
  2. **绝不取未收盘 bar**（前视偏差 —— 这是 2026-10-24 回测踩过的最大坑，
     见 btc-strategy-dev skill references/backtest-lookahead-traps.md）
  3. 闸门行为：低波动挡、高波动放、可关、只挡 long、数据不足放行

跑法：
  cd /root/skills/btc-gate-intraday-strategy/scripts
  /root/.hermes/hermes-agent/venv/bin/python3 -m unittest tests.test_v2_vol_gate -v
"""
import json
import math
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import v2_journal  # noqa: E402


BAR = 4 * 3600
TS0 = 1_760_000_000      # 任意固定起点（整点，避免边界歧义）


def mk_bars(n, base_px=80000.0, vol_pct=1.0, start_ts=TS0):
    """
    造 n 根 4h bar：用固定收益率序列造出接近 vol_pct 的已实现波动率。
    用确定性正负交替，保证结果可复算。
    """
    bars = []
    px = base_px
    for i in range(n):
        # 每根 bar 的 close 相对上一根变动 vol_pct/100（正负交替，幅度恒定）
        step = (vol_pct / 100.0) * (1 if i % 2 == 0 else -1)
        o = px
        px = px * (1 + step)
        bars.append([start_ts + i * BAR, o, max(o, px), min(o, px), px])
    return bars


class TestComputeVol5d(unittest.TestCase):
    """口径正确性"""

    def _patch(self, bars):
        """
        把 fetch_ohlcv 换成假的。
        ★ 注意：v2_journal.compute_vol_5d 里是**函数内** `from gate_fetch import fetch_ohlcv`，
          所以必须 patch `gate_fetch.fetch_ohlcv`（import 时才查模块属性），
          patch `v2_journal.fetch_ohlcv` 无效（那是个不存在的模块级名字）。
        """
        def fake(contract=None, interval=None, total=None, end_ts=None, verbose=False):
            if end_ts is None:
                return bars
            return [b for b in bars if b[0] <= end_ts + BAR]
        return mock.patch("gate_fetch.fetch_ohlcv", fake)

    def test_matches_independent_std(self):
        """与独立实现（ddof=1）结果一致"""
        bars = mk_bars(40, vol_pct=1.0)
        end = bars[-1][0] + BAR      # 最后一根已收盘
        with self._patch(bars):
            got = v2_journal.compute_vol_5d(ts=end)
        # 独立算法：只取已收盘、最后 30 根
        closed = [b for b in bars if b[0] + BAR <= end][-30:]
        cl = [b[4] for b in closed]
        rets = [(cl[i] - cl[i - 1]) / cl[i - 1] for i in range(1, len(cl))]
        mean = sum(rets) / len(rets)
        var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
        self.assertAlmostEqual(got, math.sqrt(var) * 100, places=10)

    def test_uses_exactly_30_bars(self):
        """只用最后 30 根，多给的不影响结果"""
        bars_a = mk_bars(35, vol_pct=1.0)
        bars_b = mk_bars(35, vol_pct=1.0)
        # bars_b 前面再塞 10 根完全不同的 bar，末尾 35 根不变
        extra = mk_bars(10, base_px=50000.0, vol_pct=3.0,
                        start_ts=TS0 - 10 * BAR)
        end = bars_a[-1][0] + BAR
        with self._patch(bars_a):
            a = v2_journal.compute_vol_5d(ts=end)
        with self._patch(extra + bars_b):
            b = v2_journal.compute_vol_5d(ts=end)
        self.assertAlmostEqual(a, b, places=10)

    def test_no_lookahead_unclosed_bar_excluded(self):
        """
        ★ 核心：未收盘 bar 的 close 绝不能进入计算。
        造一根「高得离谱但未收盘」的 bar —— 若被误用，波动率会暴涨。
        """
        bars = mk_bars(30, vol_pct=1.0)
        end = bars[-1][0] + BAR - 1      # 最后一根刚好未收盘（差 1 秒收盘）
        # 追加一根未收盘 bar，close 巨大
        ghost = [bars[-1][0] + BAR, 80000.0, 999999.0, 80000.0, 999999.0]
        with self._patch(bars + [ghost]):
            got = v2_journal.compute_vol_5d(ts=end)
        # 只用 bars 的已收盘部分（bars[:-1] 共 29 根 < 30 → 返回 None）
        self.assertIsNone(got,
                          "未收盘 bar 被计入了 —— 前视偏差！")

    def test_no_lookahead_uses_prior_close_only(self):
        """未收盘 bar 存在时，用「已收盘」的 30 根算，结果与不带未收盘 bar 一致"""
        bars = mk_bars(32, vol_pct=1.0)
        end = bars[-1][0] + BAR          # bars[-1] 已收盘
        ghost = [bars[-1][0] + BAR, 80000.0, 999999.0, 80000.0, 999999.0]
        with self._patch(bars):
            plain = v2_journal.compute_vol_5d(ts=end)
        with self._patch(bars + [ghost]):
            with_ghost = v2_journal.compute_vol_5d(ts=end)
        self.assertAlmostEqual(plain, with_ghost, places=10,
                               msg="未收盘 bar 影响了结果 —— 前视偏差！")

    def test_ddof_is_sample_std(self):
        """ddof=1（样本标准差），不是总体标准差"""
        bars = mk_bars(31, vol_pct=1.0)     # 31 根 → 30 根已收盘、29 个收益
        end = bars[-1][0] + BAR
        with self._patch(bars):
            got = v2_journal.compute_vol_5d(ts=end)
        closed = [b for b in bars if b[0] + BAR <= end][-30:]
        cl = [b[4] for b in closed]
        rets = [(cl[i] - cl[i - 1]) / cl[i - 1] for i in range(1, len(cl))]
        mean = sum(rets) / len(rets)
        pop = math.sqrt(sum((r - mean) ** 2 for r in rets) / len(rets)) * 100
        self.assertNotAlmostEqual(got, pop, places=10,
                                  msg="用的是总体标准差(ddof=0)，与回测口径不符")
        samp = math.sqrt(sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)) * 100
        self.assertAlmostEqual(got, samp, places=10)

    def test_insufficient_bars_returns_none(self):
        """不足 30 根已收盘 → None（调用方放行）"""
        bars = mk_bars(10, vol_pct=1.0)
        end = bars[-1][0] + BAR
        with self._patch(bars):
            self.assertIsNone(v2_journal.compute_vol_5d(ts=end))

    def test_higher_vol_larger_value(self):
        """单调性：波动越大，值越大"""
        with self._patch(mk_bars(35, vol_pct=0.5)):
            lo = v2_journal.compute_vol_5d(ts=TS0 + 35 * BAR)
        with self._patch(mk_bars(35, vol_pct=1.5)):
            hi = v2_journal.compute_vol_5d(ts=TS0 + 35 * BAR)
        self.assertLess(lo, hi)


class TestVolGate(unittest.TestCase):
    """闸门行为"""

    def setUp(self):
        self._orig = v2_journal.TRAINING_DIR
        self._tmp = tempfile.mkdtemp(prefix="v2gate_")
        v2_journal.TRAINING_DIR = type(self._orig)(self._tmp)

    def tearDown(self):
        v2_journal.TRAINING_DIR = self._orig

    def _decision(self, bias="long", ts=TS0 + 30 * BAR, px=80000.0):
        # 字段要覆盖 open_v2_position 会读的：bias / ts_epoch / snapshot.px /
        # decision_id / norm / consensus（缺一个就会 KeyError，测不到闸门本身）
        return {"bias": bias, "ts_epoch": ts,
                "snapshot": {"px": px}, "decision_id": "D-TEST-1",
                "norm": 0.35, "consensus": "强一致"}

    def test_rules_have_vol_gate(self):
        self.assertIn("vol_min_pct", v2_journal.V2_SLTB_RULES)
        self.assertTrue(v2_journal.V2_SLTB_RULES.get("use_vol_gate", True))
        self.assertEqual(v2_journal.V2_SLTB_RULES["vol_min_pct"], 0.65)

    def test_low_vol_blocks_long(self):
        """vol < 阈值 → 挡掉，并落一条 gate log"""
        with mock.patch.object(v2_journal, "compute_vol_5d", return_value=0.30), \
             mock.patch.object(v2_journal, "compute_regime_7d", return_value=-0.01), \
             mock.patch.object(v2_journal, "compute_atr_pct", return_value=0.002):
            self.assertIsNone(v2_journal.open_v2_position(self._decision()))
        log = v2_journal.TRAINING_DIR / "v2_gate_log.jsonl"
        self.assertTrue(log.exists(), "闸门挡掉但没落 log —— '为什么没开仓' 不可查")
        rec = json.loads(log.read_text().strip().splitlines()[-1])
        self.assertEqual(rec["gate"], "vol_min")
        self.assertEqual(rec["threshold"], 0.65)
        self.assertAlmostEqual(rec["value"], 0.30)

    def test_high_vol_passes_gate(self):
        """vol >= 阈值 → 不因波动率挡（可能被后面的 cooldown 等挡，但不是 vol）"""
        with mock.patch.object(v2_journal, "compute_vol_5d", return_value=0.90), \
             mock.patch.object(v2_journal, "compute_regime_7d", return_value=-0.01), \
             mock.patch.object(v2_journal, "compute_atr_pct", return_value=0.002), \
             mock.patch.object(v2_journal, "_read_journal", return_value=[]):
            res = v2_journal.open_v2_position(self._decision())
        self.assertIsNotNone(res, "vol 足够却被挡 —— 闸门误伤")
        log = v2_journal.TRAINING_DIR / "v2_gate_log.jsonl"
        self.assertFalse(log.exists(), "闸门放行却落了 gate log")

    def test_gate_can_be_disabled(self):
        """use_vol_gate=False → 回滚开关有效"""
        with mock.patch.dict(v2_journal.V2_SLTB_RULES, {"use_vol_gate": False}), \
             mock.patch.object(v2_journal, "compute_vol_5d", return_value=0.10), \
             mock.patch.object(v2_journal, "compute_regime_7d", return_value=-0.01), \
             mock.patch.object(v2_journal, "compute_atr_pct", return_value=0.002), \
             mock.patch.object(v2_journal, "_read_journal", return_value=[]):
            res = v2_journal.open_v2_position(self._decision())
        self.assertIsNotNone(res, "关掉闸门后仍被挡 —— 回滚开关无效")

    def test_gate_only_blocks_long(self):
        """
        闸门只挡 long —— 空头下未验证，不套用。
        ★ 必须同时关掉 long_only，否则空头会被 long_only 挡掉，
          测到的不是 vol 闸门的行为（第一次写这个测试就踩了这个坑）。
        """
        with mock.patch.dict(v2_journal.V2_SLTB_RULES, {"long_only": False}), \
             mock.patch.object(v2_journal, "compute_vol_5d", return_value=0.10), \
             mock.patch.object(v2_journal, "compute_regime_7d", return_value=0.05), \
             mock.patch.object(v2_journal, "compute_atr_pct", return_value=0.002), \
             mock.patch.object(v2_journal, "_read_journal", return_value=[]):
            res = v2_journal.open_v2_position(self._decision(bias="short"))
        self.assertIsNotNone(res, "闸门挡了空头 —— 超出验证范围")
        log = v2_journal.TRAINING_DIR / "v2_gate_log.jsonl"
        self.assertFalse(log.exists(), "空头被闸门记账了 —— 闸门不该碰空头")

    def test_insufficient_data_passes(self):
        """vol=None（数据不足）→ 放行，同 compute_regime_7d 口径"""
        with mock.patch.object(v2_journal, "compute_vol_5d", return_value=None), \
             mock.patch.object(v2_journal, "compute_regime_7d", return_value=-0.01), \
             mock.patch.object(v2_journal, "compute_atr_pct", return_value=0.002), \
             mock.patch.object(v2_journal, "_read_journal", return_value=[]):
            res = v2_journal.open_v2_position(self._decision())
        self.assertIsNotNone(res, "数据不足时没放行 —— 与既定口径不符")

    def test_regime_gate_still_works(self):
        """7 日涨幅闸门不受影响（不能因为加 vol 闸门而回归）"""
        with mock.patch.object(v2_journal, "compute_vol_5d", return_value=0.90), \
             mock.patch.object(v2_journal, "compute_regime_7d", return_value=0.05), \
             mock.patch.object(v2_journal, "compute_atr_pct", return_value=0.002):
            self.assertIsNone(v2_journal.open_v2_position(self._decision()))

    def test_gate_status_shape(self):
        with mock.patch.object(v2_journal, "compute_vol_5d", return_value=0.42), \
             mock.patch.object(v2_journal, "compute_regime_7d", return_value=-0.02):
            s = v2_journal.gate_status(ts=TS0)
        for k in ("vol_5d_pct", "vol_min_pct", "vol_pass",
                  "regime_7d_pct", "regime_max_7d", "regime_pass", "blocked_24h"):
            self.assertIn(k, s)
        self.assertEqual(s["vol_5d_pct"], 0.42)
        self.assertFalse(s["vol_pass"])
        self.assertTrue(s["regime_pass"])

    def test_gate_log_counts_within_24h(self):
        v2_journal._log_gate_block(TS0, "vol_min", 0.3, 0.65, "D1")
        v2_journal._log_gate_block(TS0 - 25 * 3600, "vol_min", 0.3, 0.65, "D2")  # 超 24h
        with mock.patch.object(v2_journal, "compute_vol_5d", return_value=0.42), \
             mock.patch.object(v2_journal, "compute_regime_7d", return_value=-0.02):
            s = v2_journal.gate_status(ts=TS0)
        self.assertEqual(s["blocked_24h"], 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
