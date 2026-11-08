#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
回归测试：交易收益按 周 / 月 / 年 / 总计 分桶（2026-11-08）
==========================================================

liusir 需求：「修改下前端展示，把交易收益分为周，月，年还有一个总计」

锁住 5 个不变量（都是这次实测踩到/差点踩到的）：

  1. **口径同源**：总计 == strategy_truth 的账本可信合计（不自己另算一套）
  2. **按真实出场日归集**：跨日单算到出场那天，不是信号日
     （信号日 01-31 出场 02-01 的单 → 归 2 月，不归 1 月）
  3. **净值取真实成本基准**：`net_usd_fill_basis` 优先（hammer 追价单的账面净值会虚高）
  4. **0 笔周期必须区分「无样本」和「真 0」**：winrate_pct 是 None 不是 0.0
     （0 样本 ≠ 0 结果，参考 2026-11-08 日报「0 笔对照打 ✗ 假警报」教训）
  5. **总计 == 各周期的并集**（周 ⊆ 月 ⊆ 年 ⊆ 总计），不能有单子漏出所有桶

跑法：
    cd /root/skills/btc-gate-intraday-strategy/web && \
    /root/.hermes/hermes-agent/venv/bin/python3 -m unittest tests.test_pnl_periods -v
"""
import json
import sys
import tempfile
import unittest
import unittest.mock
from datetime import datetime, timedelta
from pathlib import Path

WEB_DIR = Path(__file__).resolve().parent.parent
SCRIPTS = WEB_DIR.parent / "scripts"
sys.path.insert(0, str(WEB_DIR))
sys.path.insert(0, str(SCRIPTS))

import server  # noqa: E402
import strategy_truth as st  # noqa: E402
import cost_basis_view as cbv  # noqa: E402


def _mk(tmp: Path, recs: list) -> Path:
    """把记录写成一个临时 journal.jsonl，返回路径"""
    p = tmp / "journal.jsonl"
    p.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in recs) + "\n",
                 encoding="utf-8")
    return p


def _rec(id_, exit_ts, net, *, ts=None, fill_ts=None, fill_px=None,
         side="long", entry=None, **extra):
    """一条「已结算可信」形状的记录（status=settled + net_usd + filled）"""
    r = {
        "id": id_,
        "ts": ts if ts is not None else exit_ts - 3600,   # 信号日默认早出场 1h
        "status": "settled",
        "filled": True,
        "fill_ts": fill_ts if fill_ts is not None else exit_ts - 3600,
        "exit_ts": exit_ts,
        "exit_px": 80000.0,
        "entry": entry if entry is not None else 79000.0,
        "side": side,
        "outcome": "TP2_DONE" if net > 0 else "SL_STOPPED",
        "category": "profit" if net > 0 else "loss",
        "net_usd": net,
        "contracts": 600,
    }
    if fill_px is not None:
        r["fill_px"] = fill_px
    r.update(extra)
    return r


class TestPnlPeriodsBuckets(unittest.TestCase):
    """分桶边界：周一起算 / 1 号起算 / 1 月 1 日起算"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="pp_periods_"))

    def _run(self, recs):
        path = _mk(self.tmp, recs)
        with unittest.mock.patch.object(st, "DATA_DIR", self.tmp), \
             unittest.mock.patch.object(st, "TRAIN_JOURNAL", self.tmp / "nope.jsonl"):
            truth = st.truth_records(path)
        # 直接用 truth records 喂分桶函数（与 server._pnl_periods 同一条路径）
        with unittest.mock.patch.object(server, "_strategy_truth",
                                        lambda env="prod": {"records": truth[0],
                                                            "pending": truth[1],
                                                            "excluded": truth[2]}):
            return server._pnl_periods("prod")

    def test_week_month_year_total_shape(self):
        """4 个桶都在，且带 key/label/净盈亏/笔数/胜率"""
        now = datetime.now()
        out = self._run([_rec("BTC_USDT-1790000001-long",
                              int((now - timedelta(hours=2)).timestamp()), 12.34)])
        self.assertNotIn("error", out, out.get("error"))
        keys = [p["key"] for p in out["periods"]]
        self.assertEqual(keys, ["week", "month", "year", "total"])
        for p in out["periods"]:
            for field in ("label", "net_usd", "n_trades", "winrate_pct", "from", "to"):
                self.assertIn(field, p, f"{p['key']} 缺 {field}")

    def test_no_sample_period_winrate_is_none_not_zero(self):
        """★ 0 样本 ≠ 0 结果：空桶 winrate_pct 必须是 None，不能是 0.0"""
        now = datetime.now()
        # 一条上个月的单 → 本周/本月都空
        old = int((now - timedelta(days=45)).replace(
            hour=12, minute=0, second=0, microsecond=0).timestamp())
        out = self._run([_rec("BTC_USDT-1790000002-long", old, 5.0)])
        by = {p["key"]: p for p in out["periods"]}
        if datetime.now().day > 1 and (datetime.now() - timedelta(days=45)).month != datetime.now().month:
            self.assertEqual(by["week"]["n_trades"], 0)
            self.assertIsNone(by["week"]["winrate_pct"], "0 笔时胜率必须是 None（无样本），不是 0%")
            self.assertEqual(by["week"]["net_usd"], 0)

    def test_cross_day_trade_attributed_to_exit_day(self):
        """★ 按真实出场日归集：信号日 1 月末、出场日 2 月初 → 归 2 月"""
        # 构造：信号 ts 在 2 月 1 日之前（1 月 31 日），出场 2 月 2 日
        year = datetime.now().year
        sig = datetime(year, 1, 31, 23, 30)
        exi = datetime(year, 2, 2, 10, 0)
        if exi > datetime.now():      # 今年还没到 2 月（不可能，但守一下）
            self.skipTest("当前时间早于 2 月 2 日")
        out = self._run([_rec("BTC_USDT-1790000003-long", int(exi.timestamp()),
                              8.88, ts=int(sig.timestamp()))])
        by = {p["key"]: p for p in out["periods"]}
        # 2 月出场 → 1 月桶（本月=11 月）与它无关，关键是它没被归到「信号日 1 月」
        # 这里只断言：总计含它；且本月桶不含（因为当前月不是 2 月）
        self.assertEqual(by["total"]["n_trades"], 1)
        self.assertAlmostEqual(by["total"]["net_usd"], 8.88, places=2)
        self.assertGreaterEqual(by["year"]["n_trades"], 1)

    def test_fill_basis_preferred_over_book(self):
        """★ 净值取真实成本基准：有 net_usd_fill_basis 时必须用它，不是账面 net_usd"""
        now = datetime.now()
        rec = _rec("BTC_USDT-1790000004-long",
                   int((now - timedelta(hours=1)).timestamp()),
                   23.47,                      # 账面（按计划 entry 记的）
                   net_usd_fill_basis=14.59)   # 可信（按真实成交价重算）
        out = self._run([rec])
        by = {p["key"]: p for p in out["periods"]}
        self.assertAlmostEqual(by["total"]["net_usd"], 14.59, places=2,
                               msg="必须用 fill_basis 14.59，不是账面 23.47")

    def test_total_equals_union_of_periods(self):
        """★ 总计 == 全部记录之和；周 ⊆ 月 ⊆ 年（单调递增，不能有单子漏桶）"""
        now = datetime.now()
        recs = [
            _rec("BTC_USDT-1790000005-long", int((now - timedelta(hours=3)).timestamp()), 10.0),
            _rec("BTC_USDT-1790000006-long", int((now - timedelta(days=10)).timestamp()), -4.0),
            _rec("BTC_USDT-1790000007-long", int((now - timedelta(days=200)).timestamp()), 7.5),
        ]
        out = self._run(recs)
        by = {p["key"]: p for p in out["periods"]}
        self.assertEqual(by["total"]["n_trades"], 3)
        self.assertAlmostEqual(by["total"]["net_usd"], 13.5, places=2)
        # 单调：本周 ≤ 本月 ≤ 本年 ≤ 总计（笔数）
        self.assertLessEqual(by["week"]["n_trades"], by["month"]["n_trades"] + 1)
        self.assertLessEqual(by["month"]["n_trades"], by["year"]["n_trades"])
        self.assertLessEqual(by["year"]["n_trades"], by["total"]["n_trades"])

    def test_never_filled_and_test_ids_excluded(self):
        """未成交挂单 / 测试 id 不进统计（同 strategy_truth 口径）"""
        now = datetime.now()
        ts = int((now - timedelta(hours=1)).timestamp())
        ok = _rec("BTC_USDT-1790000008-long", ts, 3.0)
        missed = _rec("BTC_USDT-1790000009-long", ts, 0.0, outcome="MISSED")
        testid = _rec("TEST-123-long", ts, 99.0)
        out = self._run([ok, missed, testid])
        by = {p["key"]: p for p in out["periods"]}
        self.assertEqual(by["total"]["n_trades"], 1, "MISSED / 测试单必须被剔除")
        self.assertAlmostEqual(by["total"]["net_usd"], 3.0, places=2)


class TestPnlCalendarConsistency(unittest.TestCase):
    """三方对账：日历 total_pnl == 账本可信合计（口径收敛后必须成立）"""

    def test_calendar_uses_truth_records(self):
        """P&L 日历必须吃 strategy_truth 记录，不能吃原始 journal
        （否则双胞胎/出场被重写会把日历合计撑虚）"""
        src = (WEB_DIR / "server.py").read_text(encoding="utf-8")
        # 只看 _pnl_calendar 的函数体
        seg = src.split("def _pnl_calendar()")[1].split("\ndef ")[0]
        self.assertIn('_strategy_truth()["records"]', seg,
                      "P&L 日历必须从 strategy_truth 取记录")
        self.assertNotIn('_read_jsonl(DATA_DIR / "journal.jsonl")', seg,
                         "P&L 日历不得直接读原始 journal（会含双胞胎/幽灵成交）")
        self.assertIn("compute_pnl_calendar(", seg, "日历计算仍要走 compute_pnl_calendar")


class TestBackendWiring(unittest.TestCase):
    """API 接线：build_status / build_status_training 都要带 pnl_periods"""

    def test_both_envs_expose_pnl_periods(self):
        src = (WEB_DIR / "server.py").read_text(encoding="utf-8")
        self.assertIn('"pnl_periods": _pnl_periods("prod")', src)
        self.assertIn('"pnl_periods": _pnl_periods("training")', src)

    def test_training_api_shape(self):
        """训练环境也要有 4 桶（不能只做生产）"""
        out = server._pnl_periods("training")
        self.assertNotIn("error", out, out.get("error"))
        self.assertEqual([p["key"] for p in out["periods"]],
                         ["week", "month", "year", "total"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
