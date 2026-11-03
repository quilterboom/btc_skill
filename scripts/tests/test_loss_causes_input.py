"""训练策略调整输入 = 生产亏损原因（2026-11-03 liusir 指令）

背景：liusir 说「训练环境的策略调整输入还要包括生产环境亏损策略的原因」。
训练环境单策略化后（移除 15m_pattern）训练账本只剩 8 笔可信 / 4 笔亏损 ——
样本量太小，**单靠训练账本推不出可用的优化规则**。生产账本同期有
48 笔可信 / 27 笔亏损，亏损主因分布清晰，必须作为优化输入接进来。

锁四条不变量：
  ① 生产亏损原因能聚出分布（且每笔只归一个主因，count 之和 = 亏损笔数）
  ② 生产侧走 strategy_truth **严格口径**（与 web 胜率卡同源），
     跟宽松口径（status=settled + net 非空 + fill_ts）数字必须一致
     —— 宽松口径把双胞胎当独立样本，PnL 重复计入
  ③ 生产账本**只读** —— 本模块不许写任何生产文件
  ④ 训练优化脚本的候选规则必须包含「从生产亏损原因派生」的那些
     （E/F/G/H），且生产对照必须真的跑出来
"""
import os
import sys
import unittest
from pathlib import Path

SKILL_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(SKILL_ROOT / "scripts"))
sys.path.insert(0, str(SKILL_ROOT / "web"))

import loss_causes_input as lci  # noqa: E402


class TestProdLossCausesInput(unittest.TestCase):

    def test_prod_loss_causes_has_distribution(self):
        data = lci.prod_loss_causes()
        self.assertNotIn("error", data)
        self.assertGreater(data["n_losses"], 0, "生产账本应有亏损记录")
        # 每笔亏损只归一个主因 → by_cause 的 count 之和 = n_losses
        total = sum(r["count"] for r in data["by_cause"])
        self.assertEqual(total, data["n_losses"],
                         "错因分布的 count 之和必须等于亏损笔数（每笔一个主因）")
        # 所有错因都有中文标签 + 优化杠杆
        for r in data["by_cause"]:
            self.assertTrue(r["label"], "错因必须有中文标签（用户可见位置）")
            self.assertNotEqual(r["label"], r["type"], "不能把英文枚举直接暴露给用户")
        for lv in data["levers"]:
            self.assertTrue(lv["lever"], "每个错因必须对应一个优化杠杆")

    def test_prod_uses_strict_strategy_truth_caliber(self):
        """生产口径必须与 web 胜率卡同源（strategy_truth 严格口径）。

        宽松口径实测 59 笔 vs 严格口径 48 笔 —— 差 11 笔是双胞胎/
        出场被重写的记录，宽松口径会把它们当独立样本重复计入 PnL。
        """
        import strategy_truth as st
        strict, _p, _e = st.truth_records(st.DATA_DIR / "journal.jsonl")
        data = lci.prod_loss_causes()
        self.assertEqual(
            data["trade_count"], len(strict),
            f"生产样本数 {data['trade_count']} != strategy_truth {len(strict)}"
            " —— 口径不一致，同屏数字对不上账")

    def test_prod_journal_not_written(self):
        """生产账本只读 —— 模块跑完生产文件不能变。"""
        prod = Path("/root/data/journal.jsonl")
        before_mtime = prod.stat().st_mtime_ns
        before_size = prod.stat().st_size
        before_hash = prod.read_bytes()
        lci.prod_loss_causes()
        lci.combined_input()
        self.assertEqual(prod.stat().st_mtime_ns, before_mtime,
                         "生产 journal 的 mtime 变了 —— 被写了")
        self.assertEqual(prod.stat().st_size, before_size,
                         "生产 journal 的大小变了 —— 被写了")
        self.assertEqual(prod.read_bytes(), before_hash,
                         "生产 journal 内容变了 —— 被写了")

    def test_combined_input_has_both_envs(self):
        data = lci.combined_input()
        self.assertIn("prod", data)
        self.assertIn("train", data)
        self.assertTrue(data["priority_levers"], "应产出优化杠杆优先级")
        # 生产样本量应显著大于训练（这正是要接生产原因的理由）
        self.assertGreater(data["prod"]["n_losses"], data["train"]["n_losses"])
        # 样本不足必须显式警告
        self.assertIn("样本", data["sample_warning"])


class TestCandidatesDerivedFromProdCauses(unittest.TestCase):
    """训练优化脚本的候选规则必须包含生产亏损原因派生的那些"""

    def _prod_rows(self):
        import train_env_optimize as teo
        r7f, v5f = teo.load_gates()
        bars = teo.load_bars_15m()
        rows = teo.build_rows(teo._prod_truth_records(), r7f, v5f, bars)
        teo.attach_prod_causes(rows, bars)
        return rows

    def test_derive_candidates_from_prod_causes(self):
        import train_env_optimize as teo
        rows = self._prod_rows()
        cands = teo.derive_cause_candidates(rows)
        self.assertTrue(cands, "应从生产亏损原因派生出候选规则")
        causes = {c[3] for c in cands}
        self.assertTrue(
            any("COUNTER_TREND" in c for c in causes),
            "生产有逆势入场亏损，应派生出对应的入场过滤候选")
        # 每条候选都要能调用（不抛异常）
        for _nm, _desc, pred, _cause in cands:
            for r in rows[:5]:
                pred(r)   # 不应抛

    def test_keeps_majority_gate_rejects_strategy_wipeout(self):
        """关1：滤掉 ≥90% 样本 = 滤光整个策略 = 不通过，不是「有效」。

        实测「逆势不入场」会把 hammer 策略（本身就是逆势买入设计）
        的 8 笔全滤掉 —— 这类规则必须被关1 拦下。
        """
        import train_env_optimize as teo
        rows = self._prod_rows()
        cands = {c[0]: c for c in teo.derive_cause_candidates(rows)}
        key = [k for k in cands if "逆势不入场（counter_trend" in k]
        self.assertTrue(key, "应存在逆势过滤候选")
        _nm, _desc, pred, _cause = cands[key[0]]

        # 造一个「全是逆势 long」的样本 → 保留 0 笔 → 必须判不通过
        fake = []
        for i in range(10):
            fake.append({"t": {"id": f"x{i}", "signal_source": "left_side_oversold_bounce",
                               "sl_pct": 0.006, "tp1_pct": 0.01},
                         "net": -10.0, "side": "long", "ts": 1000 + i,
                         "r7": 0.0, "v5": 1.0, "trend": "空头排列",
                         "drift": 1.6, "chase": 0.5, "cause": None})
        res = teo.validate(fake, "假样本", pred)
        self.assertFalse(res["passed"], "滤光整个策略的规则必须判不通过")
        self.assertTrue(any("关1" in r for r in res["fail_reasons"]),
                        f"失败原因应包含关1，实际: {res['fail_reasons']}")

    def test_cross_sample_conflict_detected(self):
        """双样本方向相反 → 必须标冲突（不可上线）。"""
        import train_env_optimize as teo
        # 训练：全样本 -20U → 保留 -30U（过滤后更差，delta -10U）
        # 生产：全样本 +100U → 保留 +150U（过滤后更好，delta +50U）
        res = {"full": {"net_usd": -20.0}, "keep": {"net_usd": -30.0},
               "prod": {"full": {"net_usd": 100.0}, "keep": {"net_usd": 150.0}}}
        cc = teo._cross_conflict(res)
        self.assertTrue(cc["conflict"],
                        "训练 Δ-10U / 生产 Δ+50U 方向相反，必须报冲突")
        self.assertEqual(cc["train_delta"], -10.0)
        self.assertEqual(cc["prod_delta"], 50.0)

    def test_cross_sample_no_conflict_when_both_improve(self):
        """两边都改善 → 不算冲突。"""
        import train_env_optimize as teo
        res = {"full": {"net_usd": -20.0}, "keep": {"net_usd": -5.0},
               "prod": {"full": {"net_usd": 100.0}, "keep": {"net_usd": 150.0}}}
        cc = teo._cross_conflict(res)
        self.assertFalse(cc["conflict"], "两边都改善（Δ+15 / Δ+50）不是冲突")
        self.assertEqual(cc["train_delta"], 15.0)
        self.assertEqual(cc["prod_delta"], 50.0)


if __name__ == "__main__":
    unittest.main()
