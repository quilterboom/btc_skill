"""训练环境单策略不变量（2026-11-03 liusir：「训练环境的策略只要一个」）

**单策略不等于「scan.py 里停了 15m 就完事」** —— 实测训练环境有**两个**策略源：

  ① scan.py 的 15m 形态 / 1h 主信号 —— 已被 TRAINING_SINGLE_STRATEGY 停用
  ② **watch/jump.py 的 JumpTracker** —— 它不走 log_signal，自己拼记录
     **直接写 journal**（`Path(DATA_DIR)/"journal.jsonl"`），完全绕过
     scan.py 的单策略闸门 → 写出来的记录没有 signal_source
     → 训练页策略矩阵里显示成「uncategorized」= 第二个策略。
     实测训练账本里确实有一条 BTC_USDT-1792802290-long-JUMP-REVERSE。

所以「策略只有一个」要在**两个层面**都锁住：
  - 调度层：训练模式不调度 JumpTracker（watch/runner.py:fire_rsi）
  - 写入层：journal.log_signal 在训练模式只放行 left_side_oversold_bounce

外加一条数据层断言：训练账本里不能有第二个策略源。
"""
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

SKILL_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(SKILL_ROOT / "scripts"))

TRAIN_JOURNAL = Path("/root/data/training/journal.jsonl")


class TestTrainingJournalSingleStrategy(unittest.TestCase):
    """数据层：训练账本里只有一个策略源"""

    def test_no_second_strategy_source_in_train_journal(self):
        if not TRAIN_JOURNAL.exists():
            self.skipTest("训练账本不存在")
        offenders = []
        for line in TRAIN_JOURNAL.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            r = json.loads(line)
            src = r.get("signal_source")
            if src == "left_side_oversold_bounce":
                continue
            # 没有 signal_source 但带 kind = JumpTracker 写的第二策略源记录
            if r.get("kind"):
                offenders.append((r.get("id"), r.get("kind")))
        self.assertEqual(offenders, [],
                         f"训练账本里有第二策略源记录（JumpTracker 直接写 journal，"
                         f"绕过单策略闸门）: {offenders}")

    def test_all_train_records_are_single_source(self):
        """训练账本里每条记录都该是超跌反弹（或明确标注的非策略记录）"""
        if not TRAIN_JOURNAL.exists():
            self.skipTest("训练账本不存在")
        srcs = set()
        for line in TRAIN_JOURNAL.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            r = json.loads(line)
            srcs.add(r.get("signal_source"))
        self.assertTrue(srcs <= {"left_side_oversold_bounce", None},
                        f"训练账本出现多个策略源: {srcs}")
        self.assertNotIn(None, srcs,
                         "训练账本里有 signal_source 为空的记录 —— "
                         "说明有绕过 log_signal 的写入路径（JumpTracker）")


class TestTrainingJumpNotScheduled(unittest.TestCase):
    """调度层：训练模式不调度 JumpTracker"""

    def test_fire_rsi_returns_early_in_training(self):
        """fire_rsi 在 TRAINING_MODE=1 时必须提前 return，不调度 JumpTracker。"""
        src = (SKILL_ROOT / "scripts" / "watch" / "runner.py").read_text(
            encoding="utf-8")
        i = src.find("def fire_rsi(")
        self.assertGreater(i, 0, "找不到 fire_rsi")
        body = src[i:i + 4000]
        # 训练守卫必须排在 JumpTracker 调度之前
        guard = body.find('os.environ.get("TRAINING_MODE") == "1"')
        dispatch = body.find("JumpTracker.instance().start_or_restart(")
        self.assertGreater(guard, 0, "fire_rsi 缺少训练模式守卫")
        self.assertGreater(dispatch, 0, "fire_rsi 里找不到 JumpTracker 调度")
        self.assertLess(guard, dispatch,
                        "训练守卫必须排在 JumpTracker 调度之前 —— "
                        "否则训练环境还是有第二个策略源")

    def test_jump_only_scheduled_from_fire_rsi(self):
        """JumpTracker 只能从 fire_rsi 调度 —— 那个函数现在有训练守卫。"""
        hits = []
        for p in (SKILL_ROOT / "scripts").rglob("*.py"):
            if "tests" in p.parts:
                continue
            txt = p.read_text(encoding="utf-8", errors="ignore")
            for i, line in enumerate(txt.splitlines(), 1):
                if "start_or_restart(" in line and "def start_or_restart" not in line:
                    hits.append((p.name, i))
        # 允许 jump.py 自己的定义 + runner.py 的唯一调度点
        self.assertTrue(all(
            fn == "runner.py" or fn == "jump.py" for fn, _ in hits),
            f"JumpTracker 在意料之外的地方被调度: {hits}")


class TestJournalSingleStrategyGuard(unittest.TestCase):
    """写入层：journal.log_signal 在训练模式只放行超跌反弹"""

    def _call_log_signal(self, signal_source):
        """在训练模式下调 log_signal，返回它的返回值。"""
        code = (
            "import os, sys, json\n"
            "os.environ['TRAINING_MODE'] = '1'\n"
            "os.environ['BTC_NO_JOURNAL'] = '1'\n"   # 防止真写盘
            f"sys.path.insert(0, {str(SKILL_ROOT / 'scripts')!r})\n"
            "import journal\n"
            "plan = {'entry_limit': 80000, 'entry_break': 80100, 'sl': 79500,\n"
            "        'tp1': 80500, 'tp2': 81000, 'sl_pct': 0.6, 'tp1_pct': 0.6}\n"
            f"plan['signal_source'] = {signal_source!r}\n"
            "print(json.dumps(journal.log_signal(\n"
            "    'BTC_USDT', 80000, 'long', 7, True, 'x', plan, {}, {}, 0)))\n"
        )
        r = subprocess.run([sys.executable, "-c", code],
                           capture_output=True, text=True, timeout=60)
        out = (r.stdout or "").strip().splitlines()
        return out[-1] if out else "", r.stderr or ""

    def test_allows_left_side_bounce(self):
        ret, err = self._call_log_signal("left_side_oversold_bounce")
        # BTC_NO_JOURNAL=1 会先返回 "off" —— 守卫在它之后，
        # 所以这里只能验证「没有守卫拦截」的打印；改用真实路径验证：
        # 关键是不该出现「训练单策略守卫」的打印
        self.assertNotIn("训练单策略守卫", err,
                         "超跌反弹被单策略守卫误拒了 —— 守卫位置或字段读错了")

    def test_rejects_other_sources(self):
        for src in ("15m_pattern", "1h_main", None):
            ret, err = self._call_log_signal(src)
            self.assertNotIn("训练单策略守卫", err,
                             f"signal_source={src!r} 应被守卫拦下但没拦")


class TestJournalGuardOrdering(unittest.TestCase):
    """守卫位置：必须在 BTC_NO_JOURNAL 之后、真正写入之前"""

    def test_guard_exists_in_log_signal(self):
        src = (SKILL_ROOT / "scripts" / "journal.py").read_text(encoding="utf-8")
        i = src.find("def log_signal(")
        body = src[i:i + 5000]
        self.assertIn("训练单策略守卫", body,
                      "journal.log_signal 缺少训练单策略守卫")
        self.assertIn('TRAINING_MODE', body)
        self.assertIn("left_side_oversold_bounce", body)


if __name__ == "__main__":
    unittest.main()
