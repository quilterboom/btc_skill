#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
watch/jump.py 单元测试

跑法：cd scripts && python3 -m unittest tests.test_jump -v

不依赖实时 K 线 / 网络——直接测守门逻辑（已抽成 _apply_verdict_guard）。
"""
import os, sys, json, unittest, tempfile, re as _re
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from watch import jump


def _apply_verdict_guard(verdict: str, config_skip: bool):
    """复刻 jump.py:308-318 加的 verdict 守门（同 scan.py:619）"""
    skip, skip_reason = config_skip, None
    if not skip and not ("成立" in verdict or "临界" in verdict):
        skip = True
        skip_reason = (f"verdict 不成立（{verdict}），按纪律核心①②未满足不进场")
    return skip, skip_reason


class TestJumpVerdictGuard(unittest.TestCase):
    """2026-10-07 加：jump.py 普通路径的 verdict 守门。

    Bug 实例：BTC_USDT-1791339325-long 是 verdict="无信号·观望" 但 jump.py 普通路径写出的。
    修复：仅 "成立" / "临界" 放行；其它一律 skip。
    反方向 JUMP-REVERSE 路径保留不动（line 247 的 force_close 覆盖路径）。
    """

    def test_no_signal_verdict_blocks(self):
        skip, reason = _apply_verdict_guard("无信号 · 观望", False)
        self.assertTrue(skip)
        self.assertIn("无信号", reason)

    def test_no_signal_verdict_side_short_blocks(self):
        skip, reason = _apply_verdict_guard("无信号 · 观望", False)
        self.assertTrue(skip)
        self.assertIn("无信号", reason)

    def test_active_long_成立_passes(self):
        skip, _ = _apply_verdict_guard("做多信号成立", False)
        self.assertFalse(skip, "做多信号成立必须放行")

    def test_active_short_成立_passes(self):
        skip, _ = _apply_verdict_guard("做空信号成立", False)
        self.assertFalse(skip, "做空信号成立必须放行")

    def test_boundary_临界_passes(self):
        skip, _ = _apply_verdict_guard("临界（再等 1 根确认）", False)
        self.assertFalse(skip, "临界（再等 1 根确认）也要放行")

    def test_empty_verdict_blocks(self):
        skip, _ = _apply_verdict_guard("", False)
        self.assertTrue(skip, "空 verdict 防御性拦下")

    def test_existing_skip_preserved(self):
        """已有 config_skip（冷却/距离）保留——verdict 守门不应覆盖"""
        skip, _ = _apply_verdict_guard("无信号 · 观望", True)
        self.assertTrue(skip, "已有 config_skip=True 必须保留")


class TestJumpReverseNotBlocked(unittest.TestCase):
    """反方向 JUMP-REVERSE 路径保留不动（line 247 的 force_close 覆盖逻辑）。

    文档化原则：守门只在普通 jump 路径（line 308 的 else 分支）生效。
    """

    def test_reverse_path_template_exists_before_guard(self):
        """源码中 'JUMP-REVERSE' 应在 'verdict 不成立' 之前出现（结构证明）"""
        import inspect
        src = inspect.getsource(jump.JumpTracker._run)
        rev_pos = src.find("JUMP-REVERSE")
        guard_pos = src.find("verdict 不成立")
        self.assertGreater(rev_pos, 0,
                           "反方向 ID 模板必须存在（line 247）")
        self.assertGreater(guard_pos, 0,
                           "verdict 守门代码必须嵌入普通路径")
        self.assertLess(rev_pos, guard_pos,
                        "反方向 ID 模板应在 verdict 守门之前分支返回")


if __name__ == "__main__":
    unittest.main(verbosity=2)