#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
hammer 幽灵成交回归测试（2026-11-05）

**症状**：生产/训练账本各出现一批"凭空来的亏损单"——
hammer 信号（left_side_oversold_bounce）明明没成交，账本里却记了一笔
SL_STOPPED 亏损单，还推了"已成交入场"卡。

**根因**（两条路径打架）：
  ① scan.py 写 hammer 信号时 status="preview_pending"，preview 窗口的语义是
     **"等反弹确认的监测窗"**，生命周期归 HammerWatcher 独占：
       · ticker ≥ hammer_L × 1.003 → 市价成交（_fill_method=market_on_watcher）
       · 窗口到期没到触发线 → 标 cancelled / HAMMER_EXPIRED（**本来就没这单**）
  ② 但 journal.promote_due_previews 是**通用** preview→pending 流程
     （1h 主信号 / 15m 形态用），它在**同一秒**把 hammer 信号也 promote 成
     pending → 抢在 watcher 的过期作废之前。
     ③ watcher 之后只认 status=="preview_pending"，于是永远等不到作废。
     ④ 结果落进 position_tracker 的**限价单**路径：回放 1m K 线看到 low ≤ entry
        就"成交"（成交价 = 计划限价，fill_ts 还用信号之前那根 bar 的时间戳）
        → 凭空多一笔亏损单。

**实测影响**：11 笔（生产 5 + 训练 6），账面 -173.34U；
生产可信口径 51→46 笔 / -250.08→-172.03U；训练 12→6 笔 / -55.07→+40.22U。

**防线**：
  ① journal.promote_due_previews 跳过 meta.hammer_managed 的信号
  ② position_tracker._settle_one 不给未成交的 hammer 信号做限价补成交

跑法：
    cd scripts && python3 -m unittest tests.test_hammer_ghost_fill -v
"""
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# ★ 测试隔离：审计日志写到临时文件 + 干跑闸门（防真发 Telegram）
os.environ.setdefault("BTCTG_DRYRUN", "1")
os.environ.setdefault("BTCTG_AUDIT", os.path.join(
    os.environ.get("TMPDIR") or "/tmp", "btc_test_tg_audit.log"))

import journal as J
import position_tracker as pt
from cost_basis_view import is_ghost_fill


def _bar(ts, o, h, l, c):
    """构造一根 K 线（ts, o, h, l, c, vol）"""
    return [ts, o, h, l, c, 100]


def _hammer_rec(**over):
    """一条 hammer 信号（preview_pending，未成交）"""
    now = int(time.time())
    rec = {
        "contract": "BTC_USDT",
        "ts": now - 200,
        "side": "long",
        "entry": 79094.3,
        "sl": 78874.1,
        "tp1": 80122.5,
        "tp2": 81388.0,
        "status": "preview_pending",
        "preview_secs": 120,
        "preview_until_ts": now - 60,     # 窗口已过
        "id": f"BTC_USDT-{now}-long",
        "signal_source": "left_side_oversold_bounce",
        "meta": {
            "signal_source": "left_side_oversold_bounce",
            "hammer_managed": True,
            "hammer_L": 78953.1,
            "hammer_H": 79094.3,
            "trigger_pct": 0.003,
        },
    }
    rec.update(over)
    return rec


class TestPromoterSkipsHammer(unittest.TestCase):
    """防线 ①：promoter 不得 promote hammer 信号（它归 watcher 独占）"""

    def _run_promote(self, recs, watcher_alive=True):
        tmp = Path(tempfile.mkdtemp()) / "journal.jsonl"
        tmp.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in recs),
                       encoding="utf-8")
        with patch.object(J, "JOURNAL", str(tmp)), \
             patch.object(J, "_hammer_watcher_alive", return_value=watcher_alive):
            promoted = J.promote_due_previews("BTC_USDT")
        out = [json.loads(l) for l in tmp.read_text(encoding="utf-8").splitlines() if l.strip()]
        return promoted, out

    def test_hammer_not_promoted_when_watcher_alive(self):
        """watcher 活着 → hammer 保持 preview_pending（等 watcher 自己成交或作废）"""
        rec = _hammer_rec()
        promoted, out = self._run_promote([rec], watcher_alive=True)
        self.assertEqual(promoted, [], "watcher 活着时 promoter 不该 promote hammer 信号")
        self.assertEqual(out[0]["status"], "preview_pending")
        self.assertNotIn("promoted_ts", out[0])

    def test_non_hammer_still_promoted(self):
        """普通信号（1h 主信号 / 15m 形态）照常 promote —— 不能被误伤"""
        rec = _hammer_rec()
        rec["meta"] = {"signal_source": "1h_main"}
        rec["signal_source"] = "1h_main"
        promoted, out = self._run_promote([rec], watcher_alive=True)
        self.assertEqual(len(promoted), 1, "非 hammer 信号必须照常 promote")
        self.assertEqual(out[0]["status"], "pending")
        self.assertIn("promoted_ts", out[0])

    def test_hammer_cancelled_when_watcher_dead(self):
        """watcher 死了 → 兜底作废（cancelled/HAMMER_EXPIRED），不能卡在 preview_pending"""
        rec = _hammer_rec()
        promoted, out = self._run_promote([rec], watcher_alive=False)
        self.assertEqual(promoted, [], "兜底作废不算 promote")
        self.assertEqual(out[0]["status"], "cancelled")
        self.assertEqual(out[0]["outcome"], "HAMMER_EXPIRED")
        self.assertIsNone(out[0].get("fill_ts"), "作废的单不能有成交")


class TestSettleNoGhostLimitFill(unittest.TestCase):
    """防线 ②：settler 不给未成交的 hammer 信号做限价补成交"""

    def _settle(self, rec, bars):
        with patch.object(pt, "_notify_fill", lambda *a, **k: None):
            return pt._settle_one(rec, bars)

    def test_hammer_never_limit_filled(self):
        """hammer 未成交 → MISSED（净利 0），不是一笔限价成交"""
        rec = _hammer_rec()
        rec["status"] = "pending"
        rec["promoted_ts"] = int(rec["ts"]) + 60
        base = int(rec["ts"]) + 120
        # 整根 K 线都在 entry 下方 → 限价单路径必然判定"成交"
        bars = [_bar(base, 78860.1, 78975.9, 78803.0, 78900.9)]
        res = self._settle(rec, bars)
        self.assertIsNotNone(res, "hammer 未成交必须给出结论（不能 return None 悬着）")
        self.assertEqual(res["outcome"], "MISSED")
        self.assertEqual(res["net_usd"], 0.0)
        self.assertEqual(res["category"], "skip")

    def test_watcher_filled_hammer_still_settles_normally(self):
        """watcher 已市价成交的 hammer → 正常按 SL/TP 结算（防线不能误伤真单）"""
        rec = _hammer_rec()
        rec["status"] = "pending"
        rec["filled"] = True
        rec["fill_ts"] = int(rec["ts"]) + 60
        rec["fill_px"] = 79094.3
        rec["contracts"] = 632
        rec["_fill_method"] = "market_on_watcher"
        base = int(rec["ts"]) + 120
        # 成交后一根 K 线跌破 SL → SL_STOPPED
        bars = [_bar(base, 79000.0, 79010.0, 78800.0, 78810.0)]
        res = self._settle(rec, bars)
        self.assertIsNotNone(res)
        self.assertEqual(res["outcome"], "SL_STOPPED")
        self.assertLess(res["net_usd"], 0)


class TestGhostFillSignature(unittest.TestCase):
    """账本层不变量：hammer 信号出现「成交早于 promote」时，必须被标记为幽灵成交。

    ★ 2026-11-07 修正断言方向：
      原断言是「账本里不该有任何 fill_ts < promoted_ts 的记录」——
      这与既定处置纪律**直接矛盾**：`backfill_ghost_fills.py` 的纪律是
      **标记不删**（不改 net_usd / 不删记录，只补 data_quality + ghost_fill(v1)，
      统计层剔除，记录照常出现在列表里供审计）。
      按原断言，5 笔已被正确标记的幽灵单会让测试红 —— 测试在要求「删记录」。

      正确的���变量是：
        对 **hammer 信号**（生命周期归 HammerWatcher 独占）：
          fill_ts < promoted_ts = 物理不可能 → 必须被 is_ghost_fill 命中，
          即从统计里剔除（否则会凭空多出一笔亏损）。
        对**非 hammer 信号**：同样签名不代表幽灵
        （实测 BTC_USDT-1791692114-long 是 15m_pattern 真单，events 记 TP2_DONE +37.16），
          不许被误标成幽灵 —— 误标 = 把真交易剔出统计 = 新的假数据。
    """

    def _retro_fills(self, path):
        """返回 [(id, is_ghost_fill)] —— 所有 fill_ts < promoted_ts 的记录。"""
        out = []
        p = Path(path)
        if not p.exists():
            return out
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            r = json.loads(line)
            ft, pt_ = r.get("fill_ts"), r.get("promoted_ts")
            if ft and pt_ and int(ft) < int(pt_):
                out.append((r.get("id"), is_ghost_fill(r),
                            bool((r.get("meta") or {}).get("hammer_managed"))))
        return out

    def test_prod_retro_fills_all_classified(self):
        """生产账本：每笔 retro-fill 要么是 hammer 幽灵（已标记），要么是非 hammer 真单。"""
        bad = []
        for rid, ghost, is_hammer in self._retro_fills("/root/data/journal.jsonl"):
            if is_hammer and not ghost:
                bad.append(f"{rid}(hammer 幽灵但未被标记)")
        self.assertEqual(bad, [],
                         f"生产账本存在未标记的 hammer 幽灵单: {bad}")

    def test_training_retro_fills_all_classified(self):
        """训练账本：同上。"""
        bad = []
        for rid, ghost, is_hammer in self._retro_fills("/root/data/training/journal.jsonl"):
            if is_hammer and not ghost:
                bad.append(f"{rid}(hammer 幽灵但未被标记)")
        self.assertEqual(bad, [],
                         f"训练账本存在未标记的 hammer 幽灵单: {bad}")

    def test_non_hammer_retro_fill_not_marked_as_ghost(self):
        """★ 反向防线：非 hammer 信号的 retro-fill **不许**被标成幽灵。

        实测反例 BTC_USDT-1791692114-long（15m_pattern）：events 记 TP2_DONE +37.16
        是真值，账本 +89.43 是出场被 risk_guard 重写造成的虚高（rewritten_exit 类别）。
        把它误标成幽灵 = 把一笔真交易从统计里剔掉 = 新的假数据。
        """
        import cost_basis_view as cbv
        rec = {
            "id": "BTC_USDT-fake-long",
            "fill_ts": 1000, "promoted_ts": 2000,
            "signal_source": "15m_pattern",
            "meta": {"hammer_managed": False},
        }
        self.assertFalse(cbv.is_ghost_fill(rec),
                         "非 hammer 信号不该被判成幽灵成交")


if __name__ == '__main__':
    unittest.main(verbosity=2)
