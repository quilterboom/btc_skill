#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
回归测试：测试绝不许真发 Telegram（2026-11-04）
================================================

## 事故
生产群收到「🚀 [生产] 🟡 BTC 趋势反转预警 · 部分止盈 70% / 做多 @ 100.0 /
已平 3500 张，剩余 1500 张」——生产环境当时**根本没有单**。

根因：`tests/test_position_tracker.py` 构造 id=`test-1000` 的假持仓触发
`_check_ema_break_warning` → 内部调 `_notify_ema_warning` → 真实
`telegram.push_once` → **真发到生产 chat 7097652385**。
该测试类只 mock 了 `_notify_fill/_notify_partial/_notify_close`，
**没 mock `_notify_ema_warning`**。

审计日志实测污染 **302 条**（10-07 起持续）。

## 为什么旧隔离不够
skill 里那节只隔离了**审计日志路径**（`BTCTG_AUDIT` → 临时文件），
挡不住**真实发送** —— 审计干净了，群里照样被刷。

## 修法（三道）
1. `telegram.tg_dryrun()`：`BTCTG_DRYRUN=1` → 发送前 return False，不碰网络
2. `scripts/tests/__init__.py`：包导入时设 `BTCTG_DRYRUN=1` → **所有测试默认隔离**
3. 专测发送路径的测试（test_telegram / test_telegram_dedup /
   test_notify_missing_cards）显式 `BTCTG_DRYRUN=0`（它们的 urlopen 全被 mock）

跑法：
    cd scripts && /root/.hermes/hermes-agent/venv/bin/python3 -m unittest tests.test_no_test_push -v
"""
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import telegram as T  # noqa: E402


class TestDryrunGate(unittest.TestCase):
    """dry-run 闸门本身的行为。"""

    def test_tg_dryrun_reads_env_live(self):
        """闸门每次现读 env（不是 import 时快照）→ 测试可精确开关。"""
        with patch.dict(os.environ, {"BTCTG_DRYRUN": "1"}, clear=False):
            self.assertTrue(T.tg_dryrun())
        with patch.dict(os.environ, {"BTCTG_DRYRUN": "0"}, clear=False):
            self.assertFalse(T.tg_dryrun())
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("BTCTG_DRYRUN", None)
            self.assertFalse(T.tg_dryrun())

    @patch("urllib.request.urlopen")
    def test_push_dryrun_never_calls_network(self, m_urlopen):
        """dry-run 下 push() 不碰网络，且返回 False。"""
        with patch.dict(os.environ, {"BTCTG_DRYRUN": "1"}, clear=False):
            ok = T.push("测试卡片", token="tok", chat_id="123")
        self.assertFalse(ok)
        m_urlopen.assert_not_called()

    @patch("urllib.request.urlopen")
    def test_push_once_dryrun_never_calls_network(self, m_urlopen):
        """dry-run 下 push_once() 不碰网络。"""
        with patch.dict(os.environ, {"BTCTG_DRYRUN": "1"}, clear=False):
            with T._DEDUP_LOCK:
                T._IDEMPOTENT_SEEN.clear()
            ok = T.push_once("k:dryrun", "测试卡片", token="tok", chat_id="123")
        self.assertFalse(ok)
        m_urlopen.assert_not_called()

    @patch("urllib.request.urlopen")
    def test_dryrun_still_advances_idempotency(self, m_urlopen):
        """★ dry-run 放在幂等检查**之后** → 幂等状态照常推进。

        这样测试仍能断言去重逻辑（第二次同 key 不放行），
        但全程不碰网络。放在幂等之前会让幂等断言全部失效。
        """
        with patch.dict(os.environ, {"BTCTG_DRYRUN": "1"}, clear=False):
            with T._DEDUP_LOCK:
                T._IDEMPOTENT_SEEN.clear()
            r1 = T.push_once("k:idem", "卡1", token="tok", chat_id="123")
            r2 = T.push_once("k:idem", "卡2", token="tok", chat_id="123")
        self.assertFalse(r1)
        self.assertFalse(r2)
        m_urlopen.assert_not_called()
        with T._DEDUP_LOCK:
            self.assertIn("k:idem", T._IDEMPOTENT_SEEN,
                          "dry-run 下幂等标记仍应写入（否则去重逻辑测不到）")


class TestSuiteIsolation(unittest.TestCase):
    """测试套件级别：所有测试默认 dry-run。"""

    def test_tests_package_enables_dryrun(self):
        """scripts/tests/__init__.py 必须设 BTCTG_DRYRUN=1。

        ★ 这是最关键的一条：只要有**一个**测试文件忘了 mock 推送，
        就会把夹具数据当真持仓推到生产群。包级初始化 = 默认全隔离。

        注意：不能断言**运行时**的 env 值 —— 专测发送路径的测试
        （test_telegram 等）会在模块级把它改成 "0"，全量跑时 env 已被覆盖。
        正确做法 = 用独立子进程验证「只 import 包、不 import 任何测试模块」
        时的 env 值。
        """
        import subprocess
        import textwrap
        code = textwrap.dedent("""
            import os, sys
            sys.path.insert(0, %r)
            import tests
            print(os.environ.get("BTCTG_DRYRUN", ""))
            print(os.environ.get("BTCTG_AUDIT", ""))
        """ % os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        out = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True, text=True, timeout=60,
        )
        lines = (out.stdout or "").strip().splitlines()
        self.assertGreaterEqual(len(lines), 2, f"子进程输出异常: {out.stdout} {out.stderr}")
        self.assertEqual(lines[0], "1",
                         "tests 包必须默认开启 dry-run 闸门")
        self.assertNotEqual(lines[1], "/root/data/tg_audit.log",
                            "审计日志绝不能指向生产文件")

    def test_tests_package_isolates_audit_log(self):
        """审计日志也必须隔离（不写生产 tg_audit.log）。"""
        import tests as _pkg  # noqa: F401
        audit = os.environ.get("BTCTG_AUDIT", "")
        self.assertTrue(audit, "tests 包必须设 BTCTG_AUDIT")
        self.assertNotEqual(audit, "/root/data/tg_audit.log",
                            "审计日志绝不能指向生产文件")

    def test_ema_warning_path_is_blocked_by_default(self):
        """★ 本次事故的精确复现：EMA 预警路径在测试里不得发网络。

        之前 `tests/test_position_tracker.py` 的 EMA 用例没 mock
        `_notify_ema_warning` → 真实推送「做多 @ 100.0 / 已平 3500 张」。
        """
        import position_tracker as pt
        sig = {"id": "test-1000", "side": "long", "entry": 100.0,
               "fill_px": 100.0, "contracts": 5000}
        with patch.dict(os.environ, {"BTCTG_DRYRUN": "1"}, clear=False):
            with patch("urllib.request.urlopen") as m_urlopen:
                pt._notify_ema_warning(
                    sig, 3500, 105.0, "long", 100.0, 1.75,
                    70.2, 84.5, 87.0, 70.2, 84.5, 87.0)
                m_urlopen.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)
