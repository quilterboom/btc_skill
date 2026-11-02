#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
清算簇采集器测试（2026-11-02，liq_map_collector.py）

锁住的口径：
  1. 本地派生特征按 ts 去重 —— 定时器与手动触发撞同一秒不能写两行
     （重复行对回测是硬伤：那一秒的快照会被算两遍）
  2. 坏行跳过，不让一行坏数据毁掉整个序列
  3. 待同步游标：只有 ts > synced_ts 的行才算待同步，且按 ts 升序
  4. IMA 追加内容格式：markdown 表行，字段齐全（时间/现价/上下簇/倾向分/倾向/覆盖/来源）
  5. list_notebook 的 limit 必须是 (0,20] —— 传 50 会回 code=51（实战踩到）
  6. 不可逆防护：IMA 业务错误必须抛出，不能静默吞掉（否则以为同步成功了）
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "scripts"))
import liq_map_collector as C  # noqa: E402


def _row(ts, px=78000.0, score=0.25, label="偏多"):
    return {
        "ts": ts, "ts_iso": "2026-11-02 12:00:00", "px": px, "source": "coinank-agg",
        "coverage_pct": [-4.0, 4.0],
        "above": [{"price": 78500.0, "strength_pct": 90.0, "dist_pct": 0.6, "exchanges": 6}],
        "below": [{"price": 77500.0, "strength_pct": 70.0, "dist_pct": -0.6, "exchanges": 6}],
        "suggestion": {"label": label, "score": score, "reference_value": 100.0,
                       "reference_side": "上方"},
    }


class TestStoreDedup(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self._p = [
            mock.patch.object(C, "STORE", Path(self._tmp) / "s.jsonl"),
            mock.patch.object(C, "RAW_DIR", Path(self._tmp) / "raw"),
            mock.patch.object(C, "SYNC_STATE", Path(self._tmp) / "state.json"),
        ]
        for p in self._p:
            p.start()

    def tearDown(self):
        for p in self._p:
            p.stop()

    def test_same_ts_not_written_twice(self):
        self.assertTrue(C._append_store(_row(1000)))
        self.assertFalse(C._append_store(_row(1000)))
        rows = C._read_store()
        self.assertEqual(len(rows), 1)

    def test_different_ts_both_written(self):
        self.assertTrue(C._append_store(_row(1000)))
        self.assertTrue(C._append_store(_row(2000)))
        self.assertEqual(len(C._read_store()), 2)

    def test_bad_line_skipped(self):
        C._append_store(_row(1000))
        with open(C.STORE, "a", encoding="utf-8") as f:
            f.write("{not json\n")
        C._append_store(_row(2000))
        rows = C._read_store()
        self.assertEqual(len(rows), 2)          # 坏行被跳过
        self.assertEqual(rows[0]["ts"], 1000)

    def test_pending_rows_respects_cursor(self):
        for ts in (1000, 2000, 3000):
            C._append_store(_row(ts))
        C._write_state({"synced_ts": 2000})
        pend = C._pending_rows()
        self.assertEqual([r["ts"] for r in pend], [3000])

    def test_pending_rows_sorted(self):
        for ts in (3000, 1000, 2000):
            C._append_store(_row(ts))
        pend = C._pending_rows()
        self.assertEqual([r["ts"] for r in pend], [1000, 2000, 3000])


class TestFormat(unittest.TestCase):
    def test_row_format_has_all_fields(self):
        txt = C._format_rows([_row(1000)])
        self.assertIn("2026-11-02", txt)
        self.assertIn("78,000", txt)
        self.assertIn("78500/90/+0.6", txt)
        self.assertIn("77500/70/-0.6", txt)
        self.assertIn("+0.25", txt)
        self.assertIn("偏多", txt)
        self.assertIn("-4.0%~+4.0%", txt)
        self.assertIn("coinank-agg", txt)

    def test_empty_side_renders_dash(self):
        r = _row(1000)
        r["below"] = []
        txt = C._format_rows([r])
        self.assertIn("—", txt)

    def test_note_header_explains_calibers(self):
        h = C._note_header("T")
        self.assertIn("上方", h)
        self.assertIn("下方", h)
        self.assertIn("参考值", h)
        self.assertIn("相对值", h)


class TestImaLimits(unittest.TestCase):
    """IMA 接口参数边界 —— 实战踩过的坑。"""

    def test_list_notebook_limit_is_20(self):
        """list_notebook 的 limit 范围是 (0,20]，传 50 回 code=51。"""
        calls = {}

        def _fake(api_path, body):
            calls[api_path] = body
            return {"note_folder_infos": [{"folder_id": "f1", "name": "AI"}], "is_end": True}

        with mock.patch.object(C, "_api", side_effect=_fake):
            fid = C._find_notebook("AI")
        self.assertEqual(fid, "f1")
        self.assertEqual(calls["openapi/note/v1/list_notebook"]["limit"], 20)
        self.assertLessEqual(calls["openapi/note/v1/list_notebook"]["limit"], 20)

    def test_api_raises_on_business_error(self):
        """业务错误必须抛 —— 静默吞掉会让人以为同步成功了。"""
        with mock.patch.object(C.subprocess, "run") as m:
            m.return_value = mock.Mock(returncode=0,
                                       stdout=json.dumps({"code": 51, "msg": "invalid limit"}),
                                       stderr="")
            with self.assertRaises(RuntimeError):
                C._api("openapi/note/v1/list_notebook", {"limit": 50})

    def test_api_raises_on_nonzero_rc(self):
        with mock.patch.object(C.subprocess, "run") as m:
            m.return_value = mock.Mock(returncode=1, stdout="", stderr="boom")
            with self.assertRaises(RuntimeError):
                C._api("openapi/note/v1/list_notebook", {})


class TestParseImaRows(unittest.TestCase):
    """IMA 回读解析 —— 必须在换行被压平的情况下仍能切出行。"""

    def test_parse_flattened_content(self):
        """IMA 纯文本读取会把换行压平 → 解析不能依赖换行。"""
        flat = ("BTC 清算簇时间序列（回测用）用途：xxx"
                "| 2026-11-02 12:24:35 | 78,620.0 | 79000/62/+0.5 · 79200/42/+0.7 | "
                "77440/99/-1.5 | -0.37 | 偏空 | -4.9%~+3.9% | coinank-agg |"
                "| 2026-11-02 12:36:50 | 78,490.0 | 79000/63/+0.7 | "
                "77440/99/-1.3 · 77240/78/-1.6 | -0.36 | 偏空 | -4.8%~+4.1% | coinank-agg |")
        rows = C.parse_ima_rows(flat)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["ts_iso"], "2026-11-02 12:24:35")
        self.assertAlmostEqual(rows[0]["px"], 78620.0)
        self.assertEqual(rows[0]["suggestion"]["label"], "偏空")
        self.assertAlmostEqual(rows[0]["suggestion"]["score"], -0.37)
        self.assertEqual(len(rows[0]["above"]), 2)
        self.assertAlmostEqual(rows[0]["above"][0]["price"], 79000.0)
        self.assertAlmostEqual(rows[0]["above"][0]["strength_pct"], 62.0)
        self.assertAlmostEqual(rows[0]["above"][0]["dist_pct"], 0.5)
        self.assertEqual(len(rows[0]["below"]), 1)
        self.assertEqual(rows[1]["ts_iso"], "2026-11-02 12:36:50")

    def test_roundtrip_format_then_parse(self):
        rows = [_row(1000), _row(2000)]
        txt = C._format_rows(rows)
        back = C.parse_ima_rows(txt)
        self.assertEqual(len(back), 2)
        self.assertAlmostEqual(back[0]["px"], 78000.0)
        self.assertEqual(back[0]["suggestion"]["label"], "偏多")
        self.assertAlmostEqual(back[0]["suggestion"]["score"], 0.25)
        self.assertEqual(len(back[0]["above"]), 1)

    def test_header_not_parsed_as_row(self):
        """表头不能被误当成数据行。"""
        txt = C._note_header("T") + "\n" + C._format_rows([_row(1000)])
        rows = C.parse_ima_rows(txt)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["ts_iso"], "2026-11-02 12:00:00")


if __name__ == "__main__":
    unittest.main(verbosity=2)
