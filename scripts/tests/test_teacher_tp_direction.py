#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""回归测试：老师 TP 对照的**空单方向**（2026-11-07）

## 背景（真实 bug）

`daily_report.simulate_teacher_TP` 此前**只按多头写**，对 short 完全算反：

| 判定项 | 老代码（多空通用） | 空单应该 |
|---|---|---|
| TP1 命中 | `r[2] >= tp1_target`（high 触达） | `r[3] <= tp1_target`（low 触达） |
| TP1 目标价 | `cost_px * 1.006`（成本价**上方**） | `cost_px * 0.994`（成本价**下方**） |
| 保本止损 | `r[3] <= cost_px`（low 触达） | `r[2] >= cost_px`（high 触达） |
| 盈亏符号 | 恒正 | 空单要取负 |

## 后果不是数字偏一点，是结论方向反掉

实测生产单 `BTC_USDT-1792798212-short`（v4.2 净值 **+37.13U** 的赢家）：
- 持仓期 **0** 根 bar 出现 `high >= cost*1.006` → 老代码**永远判它 TP1 未中**
- 持仓期 **20** 根 bar 出现 `low <= cost*0.994` → 这才是真正的 TP1，老代码**漏判**

于是日报「老师 TP 对照」把这类单显示成「老师 TP 没优势」——是方向 bug 制造的假象。

影响面：可信单里 **32% 是空单**（实测 15/47），这部分对照结论全是错的。

## 跑法

    cd scripts && /root/.hermes/hermes-agent/venv/bin/python3 \
        -m unittest tests.test_teacher_tp_direction -v
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import daily_report as dr
import cost_basis_view as cbv

BASE_TS = 1792792818
PX = 81105.2          # 空单成本价（取自真实单 BTC_USDT-1792798212-short）
HALF = cbv.canonical_contracts(PX) / 2


def _rows_for_short_tp1():
    """空单 TP1 场景：成本 81105.2，空单 TP1 = 81105.2 × 0.994 = 80618.6

    bar0: low 80500（跌破 80618.6 → **空单 TP1 命中**）
           high 81200（没有涨到 81105.2×1.006=81591.8，老代码永远判不中）
    bar1: high 81150（涨回成本价上方 → 剩余腿打保本）
    """
    return [
        [BASE_TS,       81000.0, 81200.0, 80500.0, 80600.0, 1],
        [BASE_TS + 900, 80600.0, 81150.0, 80580.0, 81000.0, 1],
    ]


def _rows_for_short_no_tp1():
    """空单 TP1 未触场景：价格只小跌，没到 80618.6 → 走 v4.2 相同结果"""
    return [
        [BASE_TS,       81000.0, 81100.0, 80800.0, 80900.0, 1],
        [BASE_TS + 900, 80900.0, 81050.0, 80700.0, 80800.0, 1],
    ]


SHORT_TP1 = {
    'id': 'BTC_USDT-1792798212-short',
    'ts': BASE_TS,
    'side': 'short',
    'status': 'settled',
    'outcome': 'TP2_DONE',
    'entry': PX,
    'fill_px': PX,          # 限价成交：fill == entry
    'fill_ts': BASE_TS,
    'net_usd': 37.13,
}


class TestShortDirection(unittest.TestCase):
    """★ 核心：空单的 TP1 / 保本止损方向必须与多头相反"""

    def _run(self, rec, rows):
        return dr.simulate_teacher_TP([rec], rows, BASE_TS, BASE_TS + 86400)

    def test_short_tp1_is_detected(self):
        """空单跌到 TP1 → 必须判命中（老代码判不中）。"""
        r = self._run(SHORT_TP1, _rows_for_short_tp1())
        bd = r['breakdown'][0]
        self.assertTrue(bd['tp1_hit'],
                        "空单 TP1 必须命中——价格跌到 80500 < TP1 80618.6")
        self.assertAlmostEqual(bd['tp1_px'], 80500.0, places=1,
                               msg="空单 TP1 成交价应取该 bar 的 low")

    def test_short_pnl_sign_is_negative_multiplied(self):
        """空单 TP1 毛利必须为正（跌了才赚），不能是负数。"""
        r = self._run(SHORT_TP1, _rows_for_short_tp1())
        tp1_gross = HALF * 0.0001 * (80500.0 - PX) * -1.0
        self.assertGreater(tp1_gross, 0.0,
                           "空单 TP1 腿毛利应为正（跌 605 点 × 空单符号）")
        # 老师 TP 净利 = TP1腿毛利 + 剩余打保本(0) - 手续费
        self.assertAlmostEqual(r['teacher_TP_net_usd'], round(tp1_gross - 4.85, 2),
                               places=1)

    def test_short_tp1_target_below_cost(self):
        """★ 空单 TP1 目标价必须在成本价**下方**（0.994），不是上方（1.006）。"""
        r = self._run(SHORT_TP1, _rows_for_short_tp1())
        # 若目标价写成 1.006，bar0 的 high=81200 < 81591.8 → 永远判不中
        self.assertTrue(r['breakdown'][0]['tp1_hit'],
                        "TP1 目标价若写成 cost*1.006，空单永远不可能命中")

    def test_short_no_tp1_falls_back_to_v42(self):
        """空单 TP1 未触 → 与 v4.2 相同（不虚增差异）。"""
        r = self._run(SHORT_TP1, _rows_for_short_no_tp1())
        self.assertFalse(r['breakdown'][0]['tp1_hit'])
        self.assertAlmostEqual(r['teacher_TP_net_usd'], r['v4_2_net_usd'], places=2)
        self.assertAlmostEqual(r['diff_usd'], 0.0, places=2)


class TestLongDirectionUnchanged(unittest.TestCase):
    """修空单不许动到多头行为（回归防线）。"""

    LONG_TP1 = {
        'id': 'BTC_USDT-long-tp1',
        'ts': BASE_TS,
        'side': 'long',
        'status': 'settled',
        'outcome': 'BE_STOPPED',
        'entry': 76000.0,
        'fill_px': 76000.0,
        'fill_ts': BASE_TS,
        'net_usd': -3.08,
    }
    ROWS = [
        [BASE_TS,       76000.0, 76999.5, 75900.0, 76000.0, 1],
        [BASE_TS + 900, 76000.0, 76100.0, 75800.0, 75900.0, 1],
    ]

    def test_long_tp1_still_detected(self):
        r = dr.simulate_teacher_TP([self.LONG_TP1], self.ROWS,
                                   BASE_TS, BASE_TS + 86400)
        bd = r['breakdown'][0]
        self.assertTrue(bd['tp1_hit'])
        self.assertAlmostEqual(bd['tp1_px'], 76999.5, places=1)

    def test_long_net_positive(self):
        r = dr.simulate_teacher_TP([self.LONG_TP1], self.ROWS,
                                   BASE_TS, BASE_TS + 86400)
        self.assertGreater(r['teacher_TP_net_usd'], 0.0,
                           "多头 TP1 腿毛利 + 剩余打保本 → 净利为正")


class TestRealShortRecord(unittest.TestCase):
    """用真实生产单做端到端校验（不是拍脑袋的夹具）"""

    def test_real_short_record_direction(self):
        """真实单 BTC_USDT-1792798212-short：v4.2 +37.13U

        修好后它必须被判为 TP1 命中（价格跌到 TP2 80277.9，必然先过 TP1 80618.6）。
        """
        import json
        from pathlib import Path
        p = Path("/root/data/journal.jsonl")
        if not p.exists():
            self.skipTest("生产账本不存在")
        rec = None
        for line in p.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            r = json.loads(line)
            if r.get("id") == "BTC_USDT-1792798212-short":
                rec = r
                break
        if rec is None:
            self.skipTest("真实单不在账本里")
        self.assertEqual(rec.get("side"), "short")
        self.assertGreater(rec.get("net_usd") or 0, 0,
                           "这单在 v4.2 里是赢家")

        # 用真实单的成本/时间 + 合成「跌下去」的 K 线
        cost = cbv.cost_basis_of(rec)
        if cost is None:
            self.skipTest("该单没有成本基准")
        cost = float(cost)
        rec_ts = int(rec["ts"])
        tp1_target = cost * 0.994
        rows = [
            [rec_ts,         cost + 50, cost + 100, tp1_target - 10, tp1_target - 20, 1],
            [rec_ts + 900,   tp1_target - 20, cost + 60, tp1_target - 30, cost + 10, 1],
        ]
        out = dr.simulate_teacher_TP([rec], rows, rec_ts, rec_ts + 86400)
        self.assertTrue(out['breakdown'][0]['tp1_hit'],
                        "空单跌穿 TP1 必须命中（老代码判不中）")
        self.assertGreater(out['teacher_TP_net_usd'], 0.0,
                           "空单 TP1 命中后老师 TP 净利必须为正")


if __name__ == '__main__':
    unittest.main(verbosity=2)
