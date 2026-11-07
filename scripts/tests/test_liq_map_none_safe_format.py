#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""回归测试：liq_map_collector._format_rows 的 None 安全格式化。

★ 2026-11-08 实测事故：`btc-liq-map-collector` 连挂 3 天（11-05 19:59 起每轮
exit 1）。根因：`suggestion.score` 在「上下两侧均无清算簇」时是 **None**，
而 `dict.get(k, 0)` 只对**缺键**生效、值为 None 时照样返回 None →
`f"{None:+.2f}"` 抛 `TypeError: unsupported format string passed to
NoneType.__format__` → sync_ima 整段崩 → IMA 归档全断（本地数据安全）。

通则：**格式化字段一律 None 安全**，不能指望 `.get(k, default)` 兜底 ——
上游给 None 时 default 根本不生效。

纪律（勿改断言方向）：
- 正确的不变量是「None 值必须被渲染成合法数字，不能抛异常」，
  而**不是**「None 必须被丢弃 / 这一行不该存在」。
- 也不能把 None 渲染成有业务含义的其它值（这里渲染 0.00，
  因为 score=0 在该口径下确实等价于「无倾向」）。

跑法：
    cd scripts && /root/.hermes/hermes-agent/venv/bin/python3 -m unittest \\
        tests.test_liq_map_none_safe_format -v
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import liq_map_collector as m  # noqa: E402


def _row(**over):
    base = {
        "ts": 1794021536,
        "ts_iso": "2026-11-07 11:18:56",
        "px": 75993.5,
        "source": "hyperliquid-real",
        "coverage_pct": [-85.57, 88800.1],
        "above": [{"price": 77050.0, "strength_pct": 0.0, "dist_pct": 1.39}],
        "below": [{"price": 75100.0, "strength_pct": 0.0, "dist_pct": -1.18}],
        "suggestion": {"label": None, "score": None,
                       "reference_value": None, "reference_side": None},
    }
    base.update(over)
    return base


class TestFormatRowsNoneSafe(unittest.TestCase):
    def test_none_score_does_not_crash(self):
        """★ 事故签名行：suggestion.score=None + label=None。"""
        out = m._format_rows([_row()])
        self.assertEqual(len(out.split("\n")), 1)   # 1 行，不是 1 个字符
        self.assertIn("2026-11-07 11:18:56", out)

    def test_none_fields_render_as_numbers(self):
        out = m._format_rows([_row()])
        self.assertIn("+0.00", out)          # score 渲染成合法数字
        self.assertIn("75,993.5", out)       # px 正常
        self.assertIn("—", out)              # label 空 → 占位符，不是 "None"

    def test_none_inside_side_rows(self):
        """above/below 里的 price/strength/dist 也可能是 None。"""
        r = _row(above=[{"price": None, "strength_pct": None, "dist_pct": None}])
        out = m._format_rows([r])
        self.assertIn("0/0/+0.0", out)

    def test_none_px_and_coverage(self):
        r = _row(px=None, coverage_pct=None)
        out = m._format_rows([r])
        self.assertIn("0.0", out)

    def test_normal_row_unchanged(self):
        """正常行必须原样渲染（不能因为加了 None 防护就改格式）。"""
        r = _row(suggestion={"label": "偏多", "score": 0.833,
                             "reference_value": 0.6, "reference_side": "上方"})
        out = m._format_rows([r])
        self.assertIn("+0.83", out)
        self.assertIn("偏多", out)

    def test_empty_side_renders_dash(self):
        r = _row(above=[], below=[])
        out = m._format_rows([r])
        self.assertIn("—", out)

    def test_multiple_rows_all_formatted(self):
        rows = [_row(ts_iso="2026-11-05 15:57:56"),
                _row(ts_iso="2026-11-07 11:18:56")]
        out = m._format_rows(rows)
        self.assertEqual(len(out.split("\n")), 2)

    def test_roundtrip_parse_does_not_produce_none_score(self):
        """格式化 → 解析 回环：score 不能又变回 None（那会再崩一次）。"""
        out = m._format_rows([_row()])
        parsed = m.parse_ima_rows(out)
        self.assertEqual(len(parsed), 1)
        self.assertIsNotNone(parsed[0]["suggestion"]["score"])


if __name__ == "__main__":
    unittest.main()
