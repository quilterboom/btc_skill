"""训练环境单策略模式不变量（2026-11-03 落地）

背景：liusir 决定训练环境只留 left_side_oversold_bounce（超跌反弹），
15m 形态 / 1h 主信号在训练侧全部停用。历史上出过一次事故：

  11-01 23:15 训练 scan 抛 NameError: TRAINING_SINGLE_STRATEGY is not defined
  → 当时常量定义在 main() 里，而 _15m_signal 的判断（line 1211）在
    main() 内更靠前的位置引用它 → 整个训练 scan 崩了一轮。

所以这里锁三条不变量：
  ① 常量在模块级 —— import 时就能读到，不依赖 main() 跑过
  ② 直接 `--training` 调用 scan.py 时（环境变量在 main() 里才设），
     main() 必须重新求值，否则训练模式静默失效（15m 形态会复活）
  ③ 训练模式下 15m 形态检测器根本不被调用（不只是过滤结果）

生产侧（未设 TRAINING_MODE）必须保持 False —— 15m 形态是生产主信号。
"""
import ast
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

SCAN_PY = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scan.py")


class TestTrainingSingleStrategyConstant(unittest.TestCase):
    """① 常量必须在模块级，且定义行早于所有使用行"""

    @classmethod
    def setUpClass(cls):
        with open(SCAN_PY, encoding="utf-8") as f:
            cls.src = f.read()
        cls.tree = ast.parse(cls.src)

    def test_constant_defined_at_module_level(self):
        found = []
        for node in self.tree.body:               # 只看模块顶层
            if isinstance(node, ast.Assign):
                for t in node.targets:
                    if getattr(t, "id", None) == "TRAINING_SINGLE_STRATEGY":
                        found.append(node.lineno)
            elif isinstance(node, ast.AnnAssign):
                if getattr(node.target, "id", None) == "TRAINING_SINGLE_STRATEGY":
                    found.append(node.lineno)
        self.assertTrue(found,
                        "TRAINING_SINGLE_STRATEGY 必须定义在模块级 —— "
                        "定义在 main() 里会被更靠前的引用踩成 NameError "
                        "(11-01 真实事故)")
        self.assertEqual(len(found), 1, "常量只能定义一次")

    def test_defined_before_all_uses(self):
        """模块级定义行必须早于每一次 Load 引用行。"""
        define_line = None
        for node in self.tree.body:
            if isinstance(node, ast.Assign):
                for t in node.targets:
                    if getattr(t, "id", None) == "TRAINING_SINGLE_STRATEGY":
                        define_line = node.lineno
        self.assertIsNotNone(define_line)

        uses = []
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Name) and node.id == "TRAINING_SINGLE_STRATEGY":
                if isinstance(node.ctx, ast.Load):
                    uses.append(node.lineno)
        self.assertTrue(uses)
        for ln in uses:
            self.assertGreater(
                ln, define_line,
                f"line {ln} 引用了 TRAINING_SINGLE_STRATEGY，"
                f"但定义在 line {define_line} —— 又是 11-01 那个 NameError")

    def test_main_recomputes_after_setting_env(self):
        """② main() 里设了 TRAINING_MODE 后必须重新求值常量（global 语句）"""
        fn = None
        for node in ast.walk(self.tree):
            if isinstance(node, ast.FunctionDef) and node.name == "main":
                fn = node
        self.assertIsNotNone(fn)
        assert fn is not None          # 供类型检查器收窄
        has_global = any(
            isinstance(n, ast.Global) and "TRAINING_SINGLE_STRATEGY" in n.names
            for n in ast.walk(fn))
        self.assertTrue(
            has_global,
            "main() 必须用 `global TRAINING_SINGLE_STRATEGY` 重新求值 —— "
            "scan.py 被直接 `--training` 调用时环境变量在 main() 里才设，"
            "不重算会让训练模式静默失效（15m 形态复活）")


class TestTrainingModeRuntime(unittest.TestCase):
    """③ 运行期：训练模式真的不调用 15m 形态检测器；生产模式照常调用"""

    def _run_scan_with_env(self, training: bool):
        """跑 scan.py（--no-journal 不写盘），返回 (rc, stdout+stderr)"""
        import subprocess
        env = dict(os.environ)
        env.pop("TRAINING_MODE", None)
        if training:
            env["TRAINING_MODE"] = "1"
        cmd = [sys.executable, SCAN_PY, "BTC_USDT", "--brief",
               "--target", "1000", "--no-journal"]
        if training:
            cmd.append("--training")
        r = subprocess.run(cmd, capture_output=True, text=True,
                           timeout=180, env=env, cwd=os.path.dirname(SCAN_PY))
        return r.returncode, (r.stdout or "") + (r.stderr or "")

    def test_training_mode_declares_single_strategy(self):
        rc, out = self._run_scan_with_env(True)
        self.assertEqual(rc, 0, f"训练 scan 退出码 {rc}:\n{out[-2000:]}")
        self.assertIn("单策略模式", out,
                      "训练模式必须打印单策略声明 —— 没打印说明 "
                      "TRAINING_SINGLE_STRATEGY 是 False，15m 形态会复活")

    def test_training_mode_does_not_call_15m_detector(self):
        """训练模式下 _detect_15m_pattern 一次都不能被调用"""
        rc, out = self._run_scan_with_env(True)
        self.assertEqual(rc, 0, out[-2000:])
        self.assertNotIn("15m 形态信号命中", out,
                         "训练模式产出了 15m 形态信号 —— 单策略模式失效")

    def test_prod_mode_does_not_declare_single_strategy(self):
        """生产模式不能进单策略（15m 形态是生产主信号）"""
        rc, out = self._run_scan_with_env(False)
        self.assertEqual(rc, 0, out[-2000:])
        self.assertNotIn("单策略模式", out,
                         "生产模式误进了训练单策略 —— 生产 15m 形态会被停掉")


if __name__ == "__main__":
    unittest.main()
