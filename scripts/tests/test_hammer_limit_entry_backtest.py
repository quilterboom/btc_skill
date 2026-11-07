#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""回归测试：hammer 入场执行方式回测脚本（2026-11-07）

## 这个脚本测什么

`backtest_hammer_limit_entry.py` 对比 hammer 信号的四种入场机制：
A 现状 stop-market（市价追）/ B 零滑点 / C stop-limit / D 纯限价。

## 为什么需要回归测试（脚本本身踩过 4 个坑）

1. **cap 尺度**：上限百分比用分数（0.0005=0.05%）。设成 5%/10% 时
   真实追价（0.013%~0.414%）永远超不过上限 → C 变体恒等于 A，Δ 恒为 0。
2. **成交价越过止损位**：限价单在暴跌里成交到 SL 下方时，旧代码按
   `sl_px` 出场，而 `sl_px > entry_px`（多头）→ gross 为**正数** → 凭空盈利。
   实测把 D 从 -31.65U 抬成 +270.47U（全是假账）。
   正确语义：成交即已越过止损 → 立刻在成交价出场（毛利≈0，只剩手续费）。
3. **限价单方向**：BUY LIMIT 是等价格**跌回**限价，不是上穿。
   写成上穿 → 信号一发出就永远「立刻成交」，成交率 100% 是假的。
4. **look-ahead**：
   - 信号 bar 的 high 就是 hammer_H → 用它当限价 + 从 idx 开始扫 = 永远立刻成交
   - 挂单时刻市价要用 bar 的 **open**，用 close 是未来信息

## 跑法

    cd scripts && /root/.hermes/hermes-agent/venv/bin/python3 \
        -m unittest tests.test_hammer_limit_entry_backtest -v
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import backtest_hammer_limit_entry as bt


def _bar(ts, o, h, l, c):
    return [ts, o, h, l, c, 1.0]


class TestLimitOrderSemantics(unittest.TestCase):
    """★ 坑 3：限价单方向 —— BUY LIMIT 是等价格跌回限价"""

    def test_buy_limit_fills_when_price_falls_back(self):
        # bar0 low=100.5 > 限价 99 → 不成交；bar1 low=98 <= 99 → 成交
        bars = [_bar(0, 101.0, 101.5, 100.5, 101.0),
                _bar(900, 101.0, 101.2, 98.0, 98.5)]
        tss = [b[0] for b in bars]
        got = bt.find_limit_fill(bars, tss, 0, 99.0, "long", 12, market_px=100.0)
        self.assertIsNotNone(got, "价格跌回限价 99 → 买入限价应成交")
        idx, px = got
        self.assertEqual(idx, 1)
        self.assertAlmostEqual(px, 99.0, places=6)

    def test_buy_limit_does_not_fill_when_price_never_returns(self):
        """价格一路上涨，永远不跌回限价 → 不成交。"""
        bars = [_bar(0, 100.0, 101.0, 100.5, 101.0),
                _bar(900, 101.0, 102.0, 100.8, 101.5)]
        tss = [b[0] for b in bars]
        got = bt.find_limit_fill(bars, tss, 0, 99.0, "long", 12, market_px=100.0)
        self.assertIsNone(got, "价格没跌回限价 → 买单不该成交")

    def test_buy_limit_fills_immediately_when_market_already_better(self):
        """挂单时市价已优于限价 → 立刻成交（这是挂单那一刻的市价，不是未来）。"""
        bars = [_bar(0, 98.0, 99.0, 97.5, 98.5)]
        tss = [b[0] for b in bars]
        got = bt.find_limit_fill(bars, tss, 0, 99.0, "long", 12, market_px=98.0)
        self.assertIsNotNone(got)
        idx, px = got
        self.assertEqual(idx, 0)
        self.assertAlmostEqual(px, 98.0, places=6,
                               msg="市价优于限价时按市价成交（更优价）")


class TestStopLossCrossedAtFill(unittest.TestCase):
    """★ 坑 2：成交价已在止损价之外 → 立即出场，不能按 SL 出场造出正毛利"""

    def test_fill_below_stop_for_long_exits_immediately(self):
        bars = [_bar(0, 100.0, 101.0, 99.0, 99.5),
                _bar(900, 99.5, 100.0, 99.0, 99.5)]
        # 成交价 95 已跌破 SL 98 → 应立刻按成交价出场，净 = -手续费
        net, label = bt.simulate_exit(bars, 0, 0, 95.0, 98.0, "long", 48)
        self.assertEqual(label, "SL_AT_FILL")
        self.assertAlmostEqual(net, -4.85, places=2,
                               msg="成交即越过止损 → 毛利 0，只剩手续费")
        self.assertLess(net, 0, "绝不能是正数（旧 bug 会算出 +X U）")

    def test_normal_fill_not_crossed(self):
        bars = [_bar(0, 100.0, 101.0, 99.0, 99.5),
                _bar(900, 99.5, 100.0, 98.5, 99.0)]   # low 98.5 <= SL 98? no, 98.5 > 98
        net, label = bt.simulate_exit(bars, 0, 0, 100.0, 98.0, "long", 48)
        self.assertNotEqual(label, "SL_AT_FILL")


class TestExitLadder(unittest.TestCase):
    """出场阶梯：SL / TP1(平1/3 推保本) / TP2(全平)"""

    def test_sl_hit(self):
        bars = [_bar(0, 100.0, 100.5, 97.0, 97.5)]
        net, label = bt.simulate_exit(bars, 0, 0, 100.0, 98.0, "long", 48)
        self.assertEqual(label, "SL")
        self.assertLess(net, 0)

    def test_tp1_then_breakeven(self):
        # bar0 触 TP1 (100*1.013=101.3)，bar1 跌回成本价 → BE
        bars = [_bar(0, 100.0, 101.5, 99.9, 101.4),
                _bar(900, 101.4, 101.5, 99.5, 99.8)]
        net, label = bt.simulate_exit(bars, 0, 0, 100.0, 98.0, "long", 48)
        self.assertEqual(label, "BE")
        self.assertGreater(net, 0, "TP1 腿赚的钱要超过手续费")

    def test_tp2_full(self):
        # 阶梯顺序：TP1 先于 TP2 —— bar0 触 TP1(101.3)，bar1 触 TP2(102.9)
        bars = [_bar(0, 100.0, 101.5, 99.9, 101.4),
                _bar(900, 101.4, 103.5, 101.2, 103.0)]
        net, label = bt.simulate_exit(bars, 0, 0, 100.0, 98.0, "long", 48)
        self.assertEqual(label, "TP2_AFTER_TP1")
        self.assertGreater(net, 0)


class TestCapScale(unittest.TestCase):
    """★ 坑 1：cap 用分数（0.0005 = 0.05%）"""

    def test_default_caps_are_fractions_not_percentages(self):
        """真实追价只有 0.013%~0.414% → cap 必须在这个量级才有意义。"""
        for c in bt.DEFAULT_CAP_PCTS:
            self.assertLess(c, 0.01,
                            f"cap={c} 看起来是百分比数值（如 5%）而非分数 —— "
                            f"真实追价只有 0.013%~0.414%，永远 bind 不到")


class TestSignalClassification(unittest.TestCase):
    """样本分类：REAL / GHOST / EXPIRED / MISSED"""

    def test_ghost_not_counted_as_real(self):
        import json
        import tempfile
        rec = {
            "id": "BTC_USDT-1-long", "ts": 1000, "side": "long", "status": "settled",
            "outcome": "SL_STOPPED", "signal_source": "left_side_oversold_bounce",
            "entry": 100.0, "fill_px": 99.0, "fill_ts": 900, "promoted_ts": 1000,
            "net_usd": -1.0, "_fill_method": "market_on_watcher",
            "meta": {"hammer_managed": True, "hammer_L": 98.0, "hammer_H": 100.0},
        }
        with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False) as f:
            f.write(json.dumps(rec) + "\n")
            path = f.name
        try:
            sigs = bt.load_hammer_signals(path)
            self.assertEqual(len(sigs), 1)
            self.assertEqual(sigs[0]["cls"], "GHOST",
                             "fill_ts < promoted_ts 是幽灵成交，不能算 REAL")
        finally:
            os.unlink(path)

    def test_real_fill_classified_real(self):
        import json
        import tempfile
        rec = {
            "id": "BTC_USDT-2-long", "ts": 1000, "side": "long", "status": "settled",
            "outcome": "BE_STOPPED", "signal_source": "left_side_oversold_bounce",
            "entry": 100.0, "fill_px": 100.5, "fill_ts": 1001, "promoted_ts": 1000,
            "net_usd": 1.0, "_fill_method": "market_on_watcher",
            "meta": {"hammer_managed": True, "hammer_L": 98.0, "hammer_H": 100.0},
        }
        with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False) as f:
            f.write(json.dumps(rec) + "\n")
            path = f.name
        try:
            sigs = bt.load_hammer_signals(path)
            self.assertEqual(sigs[0]["cls"], "REAL")
        finally:
            os.unlink(path)


if __name__ == '__main__':
    unittest.main(verbosity=2)
