#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
回归测试：daily_report 卡片净值口径统一（2026-11-05）

## 背景

11-04 的「净值展示口径收敛到单一公共模块 cost_basis_view」（commit 7b7476e）
把 daily_report 的**老师 TP 对照**路径改成了 fill_basis 口径
（`simulate_teacher_TP` 里 `v42_net = _cbv.net_of(e)`），
但**journal 明细展示**路径漏改——`render_md` 仍直接读账面 `net_usd`。

后果：同一张复盘卡片上，**同一笔单显示两个净值**。实测 2026-11-04：

    · 09:00 long 1793754012-long → SL_STOPPED (-14.80U)   ← 账面（计划 entry）
    · 1793754012-long entry=76927.1 v4.2=-39.04            ← 可信（真实 fill_px）

hammer 秒级市价成交追价 +372.9 点（entry 76927.1 → fill_px 77300.0），
账面 -14.80U vs 可信 -39.04U，差 24.24U。卡片自己跟自己打架。

## 纪律

- 展示层一律走 `cost_basis_view.effective_net`（fill_basis 优先，账面兜底）。
- 口径陈旧的单**必须把账面值单列出来**，不藏差额（三段式：账面/可信/差额）。
- JSON 报告同时落 `net_usd_book` / `net_usd_effective` / `basis_diff` / `basis_stale`。

跑法：
    cd scripts && /root/.hermes/hermes-agent/venv/bin/python3 -m unittest tests.test_daily_report_caliber -v
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import daily_report as dr
import cost_basis_view as cbv


# 实测 2026-11-04 生产账本真实记录（hammer 市价追价成交）
STALE_REC = {
    'id': 'BTC_USDT-1793754012-long',
    'ts': 1793754012,
    'side': 'long',
    'status': 'settled',
    'outcome': 'SL_STOPPED',
    'entry': 76927.1,
    'fill_px': 77300.0,
    'net_usd': -14.8,               # 账面：按计划 entry 记（口径陈旧）
    'net_usd_fill_basis': -39.04,  # 可信：按真实成交价 canonical 重算
}

FRESH_REC = {
    'id': 'BTC_USDT-1793763911-long',
    'ts': 1793763911,
    'side': 'long',
    'status': 'settled',
    'outcome': 'BE_STOPPED',
    'entry': 76323.0,
    'fill_px': 76323.0,
    'net_usd': -1.6,
    'net_usd_fill_basis': None,
}


def _render(records):
    """用真实 journal_entry_view + render_md 渲染卡片。"""
    entries = [dr.journal_entry_view(r) for r in records]
    report = {
        'date': '2026-11-04',
        'generated_at_cst': '2026-11-05 06:00:50',
        'market': {
            'open_c': 77659, 'close_c': 80274, 'change_pct': 3.37,
            'high': 81194, 'high_time': '23:15', 'low': 75700, 'low_time': '11:00',
            'amplitude_pct': 7.07, 'trend': '偏多', 'n_15m_bars': 96,
        },
        'pattern_scan': {
            'raw_pattern_count': 7, 'with_next_root_confirmed': 1,
            'pass_30m_filter': 0, 'pass_list': [],
        },
        'journal_window': {
            'total_entries': len(entries), '15m_pattern_entries': 0,
            'entries': entries,
        },
        'pattern_distribution': {},
    }
    return dr.render_md(report)


class TestDailyReportCaliber(unittest.TestCase):
    """daily_report 卡片净值口径。"""

    def test_stale_trade_shows_effective_net_not_book(self):
        """★ 核心：口径陈旧的单必须显示可信净值（-39.04），不能只显示账面（-14.80）。"""
        out = _render([STALE_REC])
        self.assertIn('-39.04U', out,
                      '卡片必须显示 fill_basis 口径的可信净值 -39.04U')

    def test_stale_trade_also_shows_book_value_inline(self):
        """三段式：账面值单列出来，差额不藏。"""
        out = _render([STALE_REC])
        self.assertIn('账面 -14.80U', out,
                      '口径陈旧的单必须把账面值单列（三段式）')

    def test_fresh_trade_has_no_book_annotation(self):
        """两口径一致的普通单不加账面注记（避免噪音）。"""
        out = _render([FRESH_REC])
        self.assertIn('-1.60U', out)
        self.assertNotIn('账面', out)

    def test_missing_net_still_flagged(self):
        """持仓中 / net 缺失的标识不能被口径改动吃掉。"""
        pending = {'id': 'x-long', 'ts': 1793765000, 'side': 'long',
                   'status': 'pending', 'outcome': None, 'net_usd': None,
                   'net_usd_fill_basis': None}
        out = _render([pending])
        self.assertIn('持仓中', out)

    def test_settled_with_null_net_still_flagged(self):
        """已结算但 net 缺失 → 'net 缺失'（C 类信号不许被静默）。"""
        stuck = {'id': 'y-long', 'ts': 1793765000, 'side': 'long',
                 'status': 'settled', 'outcome': 'TP1_PARTIAL', 'net_usd': None,
                 'net_usd_fill_basis': None}
        out = _render([stuck])
        self.assertIn('net 缺失', out)

    def test_entry_view_carries_three_stage_fields(self):
        """JSON 报告落三段式字段，供下游/人工对账。"""
        v = dr.journal_entry_view(STALE_REC)
        self.assertEqual(v['net_usd'], -14.8)            # 账面原值保留
        self.assertEqual(v['net_usd_book'], -14.8)        # 账面
        self.assertEqual(v['net_usd_effective'], -39.04)  # 可信
        self.assertEqual(v['net_usd_fill_basis'], -39.04)
        self.assertEqual(v['basis_diff'], -24.24)        # 差额
        self.assertTrue(v['basis_stale'])

        f = dr.journal_entry_view(FRESH_REC)
        self.assertEqual(f['net_usd_effective'], -1.6)
        self.assertEqual(f['basis_diff'], 0.0)
        self.assertFalse(f['basis_stale'])

    def test_card_matches_teacher_comparison_caliber(self):
        """★ 同一张卡上，同一笔单在明细段和老师对照段的净值必须一致。

        这是 2026-11-04 实际发生的 bug：明细 -14.80U vs 对照 -39.04U。
        """
        out = _render([STALE_REC])
        teacher_side = cbv.net_of(STALE_REC)          # 老师对照段口径
        card_side = cbv.effective_net(STALE_REC)      # 明细段口径
        self.assertEqual(teacher_side, card_side,
                         '明细段与老师对照段口径必须一致')
        self.assertIn(f'{card_side:+.2f}U', out)


if __name__ == '__main__':
    unittest.main(verbosity=2)
