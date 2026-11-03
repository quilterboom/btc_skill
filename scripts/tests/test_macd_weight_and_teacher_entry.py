"""训练环境 MACD 权重 + 老师单进场点位输入（2026-11-03 liusir）

两条指令：
  ① 「在训练环境加上 macd 的权重」→ V5_DEV_CONFIG.multi_factor.w6_macd
     + v5_dev_strategy.get_macd_weight（柱体动能：实心/空心 × 方向）
  ② 「老师的单的进场点位的分析，也是可以当做训练环境策略优化的输入」
     → teacher_entry_input.teacher_entry_profile() + derive_entry_candidates()
     → train_env_optimize 的 T 类候选

锁四条不变量：
  A. MACD 权重口径 = 柱体长度变化（|hist| 放大=实心 / 缩小=空心）× 方向，
     与「涨跌方向」是两个独立维度
  B. 老师进场画像必须算出 aligned（顺势/逆势）—— 覆盖 bug 不许复发
     （**ctx 自带 aligned:None，放后面会把算好的覆盖成 None）
  C. 老师派生的阈值只当候选，不许直接当参数上线（样本 < 20 笔）
  D. 训练行必须挂上 range_pos / dd_from_high / vol_x，T1/T2/T3 才能判
"""
import os
import sys
import unittest
from pathlib import Path

SKILL_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(SKILL_ROOT / "scripts"))
sys.path.insert(0, str(SKILL_ROOT / "web"))


class TestMacdWeight(unittest.TestCase):
    """A. MACD 权重口径"""

    def test_macd_weight_in_config(self):
        from training_config import V5_DEV_CONFIG
        w = V5_DEV_CONFIG["multi_factor"].get("w6_macd")
        self.assertIsNotNone(w, "训练环境必须有 w6_macd 权重")
        self.assertGreater(w, 0)

    def test_solid_hollow_is_magnitude_not_direction(self):
        """实心/空心看的是 |hist| 变化，不是涨跌方向。"""
        from v5_dev_strategy import get_macd_weight as g
        # 红柱放大（实心）= 动能增强；红柱缩小（空心）= 动能衰竭
        self.assertGreater(g(20.0, 10.0, "long"), g(10.0, 20.0, "long"),
                           "实心（放大）分值必须 > 空心（缩小）")
        # 绿柱同理
        self.assertLess(g(-20.0, -10.0, "long"), g(-10.0, -20.0, "long"),
                        "反向实心（对手盘增强）分值必须 < 反向空心")

    def test_aligned_with_side_gets_positive(self):
        from v5_dev_strategy import get_macd_weight as g
        self.assertGreater(g(20.0, 10.0, "long"), 0, "多单遇红柱实心应加分")
        self.assertGreater(g(-20.0, -10.0, "short"), 0, "空单遇绿柱实心应加分")

    def test_counter_expanding_is_worst(self):
        """逆势 + 对手盘动能增强 = 最危险。"""
        from v5_dev_strategy import get_macd_weight as g
        worst = g(-20.0, -10.0, "long")   # 多单遇绿柱放大
        self.assertLess(worst, 0)
        self.assertLessEqual(worst, -1.0, "逆势加仓必须重罚")

    def test_neutral_when_flat_or_missing(self):
        from v5_dev_strategy import get_macd_weight as g
        self.assertEqual(g(10.0, 10.0, "long"), 0.0, "柱体持平无信息")
        self.assertEqual(g(None, 10.0, "long"), 0.0, "hist 缺失不臆造")
        self.assertEqual(g(10.0, None, "long"), 0.0, "prev 缺失不臆造")

    def test_weight_actually_flows_into_score(self):
        """w6_macd 必须真的进总分（不是只写在 config 里）。"""
        from v5_dev_strategy import calculate_signal_strength as c
        base = dict(score_1h=3, pattern_15m="hammer", time_hour=2,
                    vol_regime="放量", fib_resonance=True, side="long")
        with_macd = c(macd_hist=20.0, macd_prev_hist=10.0, **base)
        without = c(macd_hist=20.0, macd_prev_hist=10.0, **{**base, "side": None})
        # 同向实心（long）应让分数高于中性口径
        self.assertGreater(with_macd, without,
                           "MACD 权重没进总分 —— w6_macd 没接上")
        bd = c.__wrapped__ if hasattr(c, "__wrapped__") else c
        self.assertIn("w6_macd", c._last_breakdown,
                      "breakdown 必须能看到 w6_macd（否则无法校验生效）")


class TestTeacherEntryInput(unittest.TestCase):
    """B/C/D. 老师单进场画像作为训练优化输入"""

    def test_profile_computes_alignment(self):
        """B: aligned 必须算出来（不许被 **ctx 的 aligned:None 覆盖）。"""
        from teacher_entry_input import teacher_entry_profile
        prof = teacher_entry_profile()
        self.assertNotIn("error", prof)
        a = prof.get("alignment", {})
        self.assertGreater(a.get("aligned_n", 0), 0,
                           "顺势笔数为 0 —— aligned 被 ctx 覆盖的 bug 复发了")
        self.assertGreater(a.get("counter_n", 0), 0)

    def test_profile_has_entry_geometry(self):
        from teacher_entry_input import teacher_entry_profile
        prof = teacher_entry_profile()
        for key in ("range_pos_pct", "dd_from_high_pct", "vol_x"):
            self.assertIn(key, prof["dist"], f"缺进场几何特征 {key}")

    def test_candidates_carry_sample_warning(self):
        """C: 样本不足必须显式警告（老师 17 笔 < 20）。"""
        from teacher_entry_input import teacher_entry_profile
        prof = teacher_entry_profile()
        self.assertIn("样本", prof["sample_warning"])
        self.assertIn("禁止直接上线", prof["sample_warning"])

    def test_derive_candidates_produces_rules(self):
        from teacher_entry_input import derive_entry_candidates
        cands = derive_entry_candidates([])
        self.assertGreater(len(cands), 0, "应派生出老师进场过滤候选")
        for nm, desc, pred, src in cands:
            self.assertTrue(nm and desc, "候选必须有名称和说明")
            self.assertTrue(src.startswith("teacher_entry:"),
                            f"来源标记不对: {src}")

    def test_train_rows_get_entry_features(self):
        """D: 训练行必须挂 range_pos / dd_from_high / vol_x。"""
        import train_env_optimize as teo
        bars = teo.load_bars_15m()
        self.assertTrue(bars, "15m K 线加载失败")
        recs = [{"id": f"x{i}", "ts": bars[-5][0] - i * 300,
                 "side": "long", "net_usd": -10.0, "status": "settled",
                 "entry": bars[-5][4], "sl_pct": 0.6, "tp1_pct": 1.0}
                for i in range(3)]
        r7f, v5f = teo.load_gates()
        rows = teo.build_rows(recs, r7f, v5f, bars)
        n = teo.attach_teacher_entry_features(rows, bars)
        self.assertEqual(n, 3, "3 行都该挂上特征")
        for r in rows:
            self.assertIn("range_pos", r)
            self.assertIn("dd_from_high", r)
            self.assertIn("vol_x", r)


if __name__ == "__main__":
    unittest.main()
