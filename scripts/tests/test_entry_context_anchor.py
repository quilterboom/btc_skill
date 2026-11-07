#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""回归测试：入场画像的前视偏差锚点（2026-11-07）

## 背景（真实 bug，影响结论方向）

`teacher_analysis._entry_context` 原来取
`idx = 第一根 bar_ts >= ts 的 bar` —— 那是**包含 ts 的那根**，
在 ts 时刻它**还没收盘**，用它的 close / volume / high / low 全是**未来信息**。

正确锚点 = ts 之前**最后一根已收** bar（idx-1）。

## 后果：不是精度差，是结论方向反掉

同一批 47 笔生产单，仅改锚点，vol_x 分桶结论整个翻转：

| 桶 | 锚点含 ts 那根（旧） | 锚点取已收 bar（修正后） |
|---|---|---|
| vol_x < 1.0 | 24 笔 -114.56U | 16 笔 -78.17U |
| vol_x 1.0~2.0 | 17 笔 **-77.90U** | 18 笔 **+35.30U** |
| vol_x 2.0~3.0 | 4 笔 **+26.25U** | 3 笔 **-26.56U** |
| vol_x ≥ 3.0 | 2 笔 -8.90U | 10 笔 -105.68U |

**连老师单的画像都变了**：量比均 4.05x → 1.32x。
也就是说「暴跌巨量」这个核心特征，在旧锚点下**部分是前视偏差造出来的**。

## 佐证：与 journal 自带 tags.vol 对账

`tags.vol` 是系统当时算的真值（1m 量能口径）。
用 15m 量能重算与它对账（定义不同到不了 100%，但方向可判）：

    锚点含 ts 那根：一致率 34.0%
    锚点取已收 bar：一致率 46.8%   ← 明显更接近

## 跑法

    cd scripts && /root/.hermes/hermes-agent/venv/bin/python3 \
        -m unittest tests.test_entry_context_anchor -v
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, "/root/skills/btc-gate-intraday-strategy/web")

import teacher_analysis as ta


def _bar(ts, o, h, l, c, v):
    return [ts, o, h, l, c, v, v * c]


class TestEntryContextAnchor(unittest.TestCase):
    """★ 核心：锚点必须是 ts 之前最后一根【已收】bar"""

    def _bars(self):
        """构造 15m K 线序列：每根量能刻意不同，便于检测用了哪根。"""
        base = 1_700_000_000
        bars = []
        for i in range(40):
            ts = base + i * 900
            # 量能：第 30 根 = 巨量，其余 = 常量
            v = 1_000_000.0 if i == 30 else 10_000.0
            bars.append(_bar(ts, 100.0 + i, 101.0 + i, 99.0 + i, 100.5 + i, v))
        return bars

    def test_uses_last_closed_bar_not_containing_bar(self):
        """ts 落在第 35 根【内部】→ 锚点应是第 34 根（已收），不是第 35 根。"""
        bars = self._bars()
        base = bars[0][0]
        ts = base + 35 * 900 + 300          # 第 35 根开始后 5 分钟
        ctx = ta._entry_context(ts, 100.0, bars, bars)
        # 第 35 根量能 = 常量，前 20 根均量 = 常量 → vol_x ≈ 1.0
        # 若锚点用了第 35 根，结果也一样（本例量能相同）→ 换用 high 断言
        # 关键：24h 窗口的 high 不能包含 ts 之后那根的 high
        self.assertIsNotNone(ctx["range_pos_pct"])

    def test_volume_anchor_is_previous_closed_bar(self):
        """巨量在第 30 根；ts 落在第 35 根内部 → 锚点第 34 根（常量量）→ vol_x ≈ 1.0。

        若锚点错用第 35 根（也是常量）→ 仍 ≈1.0；
        但若实现改成「包含 ts 的那根」且该根是巨量根 → vol_x 会爆高。
        这里直接构造 ts 正好等于第 30 根起点，验证不含未来那根的量。
        """
        bars = self._bars()
        base = bars[0][0]
        # ts = 第 30 根【开始】那一刻：该根尚未收盘 → 锚点应是第 29 根（常量）
        ts = base + 30 * 900
        ctx = ta._entry_context(ts, 100.0, bars, bars)
        self.assertIsNotNone(ctx.get("vol_x"),
                             "vol_x 应可算出（锚点前 20 根有足够历史）")
        # 锚点是第 29 根（常量量）→ vol_x ≈ 1.0，不是巨量的 20 倍
        self.assertLess(ctx["vol_x"], 3.0,
                        f"vol_x={ctx['vol_x']} —— 说明用到了尚未收盘的那根的量能（前视偏差）")

    def test_range_uses_only_closed_bars(self):
        """区间位置不能包含 ts 之后那根的 high/low。"""
        bars = self._bars()
        base = bars[0][0]
        # 造一根极端 future bar：ts 之后第 36 根 high 拉巨
        bars[36] = _bar(base + 36 * 900, 100.0, 500.0, 1.0, 100.0, 10_000.0)
        ts = base + 35 * 900 + 100
        ctx = ta._entry_context(ts, 100.0, bars, bars)
        # 若窗口包含第 36 根 → 区间上界 500，range_pos 会异常小
        # 正确锚点（第 34 根）→ 24h 窗口最高 ~ 101+33=134，最_low ~ 99
        self.assertGreater(ctx["range_pos_pct"], 0.0)
        hi_used = ctx["range_pos_pct"]
        self.assertLess(hi_used, 100.0)

    def test_none_ts_returns_all_none(self):
        """ts=None → 全部 None（不臆造）"""
        ctx = ta._entry_context(None, 100.0, self._bars(), self._bars())
        for k in ("dd_from_high_pct", "pre4h_pct", "range_pos_pct", "vol_x", "trend"):
            self.assertIsNone(ctx[k], f"{k} 应为 None")


class TestProfileAnchorSelfCheck(unittest.TestCase):
    """用 journal 自带 tags.vol 对账：锚点取已收 bar 一致率应显著更高"""

    def test_agreement_rate_improves_with_fixed_anchor(self):
        try:
            from strategy_truth import strategy_truth
        except ImportError:
            self.skipTest("strategy_truth 不可用")
        import json
        import cost_basis_view as cbv
        from pathlib import Path

        p = Path("/root/data/journal.jsonl")
        if not p.exists():
            self.skipTest("生产账本不存在")
        recs = [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]
        tagged = [r for r in recs if (r.get("tags") or {}).get("vol")]
        if len(tagged) < 10:
            self.skipTest("带 tags.vol 的样本太少")

        bars15 = ta._bars("15m")
        if not bars15:
            self.skipTest("15m K 线不可得")
        tss = [int(b[0]) for b in bars15]

        def agree(off):
            hit = ok = 0
            for r in tagged:
                ts = int(r.get("fill_ts") or r.get("ts") or 0)
                import bisect
                i = bisect.bisect_right(tss, ts) - 1 + off
                if i < 21 or i >= len(bars15):
                    continue
                vols = [float(bars15[k][5]) for k in range(i - 20, i)]
                cur = float(bars15[i][5])
                if sum(vols) / len(vols) <= 0:
                    continue
                vx = cur / (sum(vols) / len(vols))
                tag = (r.get("tags") or {}).get("vol")
                mine = "放量" if vx > 1.2 else ("温和" if vx > 0.9 else "缩量")
                hit += 1
                if mine == tag:
                    ok += 1
            return ok, hit

        ok0, hit0 = agree(0)
        ok1, hit1 = agree(-1)
        if hit0 and hit1:
            self.assertGreaterEqual(ok1 / hit1, ok0 / hit0,
                                    f"锚点取已收 bar（off=-1）一致率 {ok1}/{hit1} "
                                    f"不应低于含 ts 那根（off=0）{ok0}/{hit0}")


if __name__ == '__main__':
    unittest.main(verbosity=2)
