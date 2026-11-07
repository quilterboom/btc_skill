#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
回归测试：老师 TP 对照的**仓位张数口径**（2026-11-07）

## 背景（真实 bug，2026-11-07 复盘当天发现）

`daily_report.simulate_teacher_TP` 算老师 TP 盈亏时这么写仓位：

    half_contracts = (NOTIONAL / LEV) / (entry * 0.0001) / 2

`NOTIONAL = 5000` **已经是名义**（保证金 50U × 杠杆 100x）。
再除一次 `LEV` = 把名义又除了一遍杠杆 → **仓位小 100 倍**。

实测（2026-11-06 那笔生产单 BTC_USDT-1793894417-long）：

    计划 entry = 75885.7 → 真实半仓应为 329.44 张
    老写法算出        3.29 张          ← 小 100 倍

后果不是「数字小一点」，是**结论方向反掉**：

    老写法：老师 TP 重算 -4.48U，差异 ✗ -1.40U（老师比 v4.2 差）
    修  后：老师 TP 重算 +27.26U，差异 ✓ +30.34U（老师明显更好）

同一张卡片上「老师 TP vs v4.2 谁更好」的结论被完全颠倒。

## 交叉验证（不是拍脑袋定的口径）

`teacher_trades.jsonl` 里**老师真实单**记录的 `size_contracts`：

    #18: 入场 78000 → 记 641 张 = 5000 / (78000 × 0.0001) = 641.03  ✓

所以真实口径就是 `notional / (price × QUANT_MULT)`，**不除杠杆**。

## 顺带修的成本基准问题

老师 TP 复算原来挂在**计划 entry** 上，但这笔是 hammer 秒级市价成交
（计划 75885.7 → 实际成交 76023.0，追价 +137.3 点）。
老师 TP 体系是「进场后 +0.6% 减半、剩余推保本」——进场价就是**实际成交价**。
挂错基准 = 整条 TP 路径偏移。v4.2 侧走 `_cbv.net_of(e)` 已是 fill 口径，
老师侧却是 entry 口径 → 同一张卡两套口径。

## 跑法

    cd scripts && /root/.hermes/hermes-agent/venv/bin/python3 \
        -m unittest tests.test_teacher_tp_sizing -v
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import daily_report as dr
import cost_basis_view as cbv

CST_TS = 1793894417

# ── 2026-11-06 生产真实单（hammer 秒级市价成交，追价 +137.3 点） ──────────
# ★ 夹具必须自洽（skill btc-position-ops「★ 测试可能把 bug 固化成期望值」）：
#   `_fill_method` / `meta.hammer_managed` / `fill_ts` / `promoted_ts` 都要按
#   真实记录写全——漏掉 `_fill_method` 会让 cost_basis_view.is_ghost_fill 的
#   签名 2 误判成幽灵成交，测试就变成在测另一个 bug。
REAL_HAMMER = {
    'id': 'BTC_USDT-1793894417-long',
    'ts': CST_TS,
    'side': 'long',
    'status': 'settled',
    'outcome': 'BE_STOPPED',
    'signal_source': 'left_side_oversold_bounce',
    'entry': 75885.7,          # 计划价
    'fill_px': 76023.0,        # 真实成交价
    'fill_ts': CST_TS + 1,
    '_fill_method': 'market_on_watcher',   # hammer watcher 秒级市价成交
    'meta': {'hammer_managed': True},
    'net_usd': -3.08,
}

# 15m 行：[ts, o, h, l, c, v]
#   bar0 00:00  high 76999.5 → 触 TP1（无论挂 entry 还是 fill_px 都会触）
#   bar1 01:15  low  75800   → 回到成本价下方 → 剩余段打保本
ROWS = [
    [CST_TS,       76000.0, 76999.5, 75900.0, 76000.0, 1],
    [CST_TS + 900, 76000.0, 76100.0, 75800.0, 75900.0, 1],
]


class TestCanonicalContracts(unittest.TestCase):
    """★ 核心：仓位张数口径——名义已含杠杆，不许再除一次。"""

    def test_notional_divided_by_leverage_is_the_bug(self):
        """75885.7 入场 → 真实半仓 329.44 张；老写法只有 3.29 张。"""
        px = 75885.7
        correct = cbv.canonical_contracts(px)
        self.assertAlmostEqual(correct, 5000 / (px * 0.0001), places=4)
        self.assertAlmostEqual(correct / 2, 329.44, places=2,
                               msg="真实半仓应约 329 张")
        # 老写法的值——用于证明修复前的偏差正好 100 倍
        buggy = (5000 / 100) / (px * 0.0001)
        self.assertAlmostEqual(correct / buggy, 100.0, places=6,
                               msg="老写法（多除一次杠杆）正好小 100 倍")

    def test_matches_teacher_real_trade_size_contracts(self):
        """★ 交叉验证：口径必须与老师真实单记录的 size_contracts 一致。

        teacher_trades.jsonl #18：入场 78000，size_contracts = 641。
        """
        teacher_real_641 = 641        # 老师真实单记录的 size_contracts
        px = 78000.0
        derived = cbv.canonical_contracts(px)
        self.assertAlmostEqual(derived, teacher_real_641, delta=1.0,
                               msg="推导张数应与老师真实单记录一致（约 641）")

    def test_zero_or_invalid_price_is_zero(self):
        self.assertEqual(cbv.canonical_contracts(0), 0.0)
        self.assertEqual(cbv.canonical_contracts(-5), 0.0)


class TestCostBasisOf(unittest.TestCase):
    """成本基准：fill_px 优先，entry 兜底。"""

    def test_fill_px_wins_for_market_fill(self):
        self.assertEqual(cbv.cost_basis_of(REAL_HAMMER), 76023.0)

    def test_entry_used_when_no_fill(self):
        self.assertEqual(cbv.cost_basis_of({'entry': 100.0}), 100.0)

    def test_limit_fill_is_identical_to_entry(self):
        rec = {'entry': 76323.0, 'fill_px': 76323.0}
        self.assertEqual(cbv.cost_basis_of(rec), 76323.0)


class TestTeacherTPSizing(unittest.TestCase):
    """★ 端到端：老师 TP 对照段的盈亏必须按真实仓位算。"""

    def _run(self, rec, rows=None):
        return dr.simulate_teacher_TP([rec], rows if rows is not None else ROWS,
                                      CST_TS, CST_TS + 86400)

    def test_teacher_gross_not_compressed_100x(self):
        """老师 TP 毛利必须是「真实仓位」级别的数，不是压缩 100 倍的版本。

        老写法：half=3.29 张 → 毛利 ≈ +0.20U → 净额 ≈ -4.65U
        修  后：half=329.44 张 → 毛利 ≈ +32.1U → 净额 ≈ +27.3U
        """
        r = self._run(REAL_HAMMER)
        teacher_net = r['teacher_TP_net_usd']
        self.assertGreater(teacher_net, 20.0,
                           f"老师 TP 净额应约 +27U（真实仓位），实际 {teacher_net}——"
                           f"若接近 -4.65 说明仓位又被压缩了")
        # 老写法的值：净额 = 压缩后的毛利 - 手续费
        self.assertNotAlmostEqual(teacher_net, -4.65, places=1,
                                  msg="不该再出现老写法的压缩值")

    def test_conclusion_direction_not_inverted(self):
        """★ 结论方向：老师 TP 明显优于 v4.2（老代码显示的是「差 1.40U」）。"""
        r = self._run(REAL_HAMMER)
        self.assertGreater(r['diff_usd'], 0.0,
                           f"差异应为正（老师更好），实际 {r['diff_usd']}")
        self.assertAlmostEqual(r['diff_usd'], r['teacher_TP_net_usd'] - r['v4_2_net_usd'],
                               places=2)

    def test_teacher_side_uses_fill_basis_not_plan_entry(self):
        """★ 老师 TP 的成本基准必须是 fill_px，且 breakdown 里能查出来。"""
        r = self._run(REAL_HAMMER)
        bd = r['breakdown'][0]
        self.assertEqual(bd['cost_basis'], 76023.0,
                         "老师 TP 复算应挂在真实成交价 76023.0 上")
        self.assertEqual(bd['entry'], 75885.7,
                         "entry 仍保留计划价作审计线索，不该被改写")

    def test_limit_fill_record_unchanged(self):
        """限价成交（fill_px == entry）行为不变，两口径零差别。"""
        rec = dict(REAL_HAMMER)
        rec['fill_px'] = rec['entry']
        rec['net_usd'] = -1.6
        r = self._run(rec)
        self.assertAlmostEqual(r['breakdown'][0]['cost_basis'], 75885.7, places=1)
        self.assertAlmostEqual(r['v4_2_net_usd'], -1.6, places=2)


class TestTeacherTPUnaffectedPaths(unittest.TestCase):
    """修仓位不该动到别的行为：null-net 暴露、幽灵成交剔除照旧。"""

    def test_null_net_still_surfaced(self):
        entries = [
            {'id': 'BTC_USDT-1-short', 'ts': 1792792818, 'entry': 100.0,
             'status': 'settled', 'outcome': 'TP2_DONE', 'net_usd': 37.13},
            {'id': 'BTC_USDT-2-short', 'ts': 1792796209, 'entry': 100.0,
             'status': 'settled', 'outcome': 'SL_STOPPED', 'net_usd': None},
        ]
        rows = [[1792792818, 100.0, 110.0, 90.0, 105.0, 1],
                [1792792828, 105.0, 110.0, 90.0, 105.0, 1]]
        r = dr.simulate_teacher_TP(entries, rows, 1792792818, 1792792838)
        self.assertEqual(r['null_net_count'], 1)
        self.assertEqual(r['v4_2_net_usd'], 37.13)


if __name__ == '__main__':
    unittest.main(verbosity=2)
