#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Telegram 推送幂等 + 限流 回归测试
==================================

2026-10-27 liusir：入场提醒被刷屏淹没（40 秒内 19 条重复「部分止盈 TP1」，
当天 45 条推送里入场提醒只有 1 条）。

核心口径（2026-10-27 定稿）：
  - 去重用**显式幂等 key**（持仓id:事件类型），**不用卡片文本指纹**
    （文本指纹要剥数字才匹配价格变化 → 同方向不同持仓的入场提醒会被误杀）
  - 同一笔单的同一类事件只推一次
  - 不同持仓 / 不同事件类型照常推
  - 限流只作用在非幂等的噪音类推送；入场/平仓走 push_once，不受限流影响

跑法：python3 scripts/tests/test_telegram_dedup.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS))

import telegram as T  # noqa: E402

FAILS = []


def check(cond: bool, label: str, extra: str = "") -> None:
    if cond:
        print(f"  ✅ {label}")
    else:
        print(f"  ❌ {label} {extra}")
        FAILS.append(label)


def reset():
    with T._DEDUP_LOCK:
        T._IDEMPOTENT_SEEN.clear()
        T._LAST_PUSH_TS = 0.0


def card_fill(sid="BTC-1"):
    return (f"🎯 <b>BTC 已成交入场</b>\n"
            f"做空 616 张 @ <b>79,998.6</b>（成交价）\n"
            f"持仓 id={sid}")


def card_partial(sid="BTC-1"):
    return (f"🟢 <b>BTC 部分止盈 TP1</b>\n"
            f"做空 411 张入场 @ <b>81,105.2</b>\n"
            f"持仓 id={sid}")


# ------------------------------------------------------------------
print("\n【1】幂等 key：同一 key 第二次被拦")
reset()
now = 1_000_000.0
a1, w1 = T._idempotent_check("BTC-1:tp1", now)
a2, w2 = T._idempotent_check("BTC-1:tp1", now + 5)
check(a1, "第一次放行", w1)
check(not a2, "第二次（同 key 5s 后）被拦", w2)
check("幂等" in w2, "原因标注为幂等", w2)

print("\n【2】不同 key 照常放行 —— 不会误杀不同持仓的入场提醒")
reset()
a3, w3 = T._idempotent_check("BTC-1:fill", now)
a4, w4 = T._idempotent_check("BTC-2:fill", now + 1)   # 不同持仓，只差 1 秒
check(a3 and a4, "两个不同持仓的入场提醒都放行", f"{w3} / {w4}")

print("\n【3】同持仓不同事件类型 → 都放行")
reset()
a5, _ = T._idempotent_check("BTC-1:fill", now)
a6, _ = T._idempotent_check("BTC-1:tp1", now + 1)
a7, _ = T._idempotent_check("BTC-1:close", now + 2)
check(a5 and a6 and a7, "入场/TP1/平仓 三类事件都放行")

print("\n【4】超过幂等窗口 → 重新放行")
reset()
T._idempotent_check("BTC-1:tp1", now)
a8, w8 = T._idempotent_check("BTC-1:tp1", now + T.DEDUP_WINDOW + 10)
check(a8, "窗口外重新放行", w8)

print("\n【5】限流只作用在非幂等推送")
reset()
r1, rw1 = T._rate_limit_check(now)
r2, rw2 = T._rate_limit_check(now + 3)
check(r1, "第一次限流检查放行", rw1)
check(not r2, "3 秒后第二次被限流", rw2)
check("限流" in rw2, "原因标注为限流", rw2)

# ------------------------------------------------------------------
print("\n【6】端到端：走真实 push_once —— 入场必达 + 重复被拦")
reset()
_sent = []
_real_urlopen = T.urllib.request.urlopen


class _FakeResp:
    def __init__(self, body=b'{"ok":true}'):
        self._b = body

    def read(self):
        return self._b

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _fake_urlopen(req, timeout=None):
    _sent.append(req.data.decode("utf-8"))
    return _FakeResp()


T.urllib.request.urlopen = _fake_urlopen
try:
    # 先推一条噪音（占用限流窗口）
    T.push(card_partial("BTC-noise"), token="x", chat_id="y")
    # 2 秒后入场提醒（走 push_once，不受限流影响）
    r_entry = T.push_once("BTC-1:fill", card_fill("BTC-1"), token="x", chat_id="y")
    # 同一笔单同一类事件重复推 → 被幂等拦
    r_dup = T.push_once("BTC-1:fill", card_fill("BTC-1"), token="x", chat_id="y")
    # 不同持仓的入场提醒 → 照常推
    r_entry2 = T.push_once("BTC-2:fill", card_fill("BTC-2"), token="x", chat_id="y")
finally:
    T.urllib.request.urlopen = _real_urlopen

check(r_entry, "入场提醒在 2 秒间隔后仍然发出（未被限流挡掉）")
check(not r_dup, "同一笔单同一类事件重复推送被幂等拦下")
check(r_entry2, "不同持仓的入场提醒照常发出")
_decoded = [json.loads(s).get("text", "") for s in _sent]
check(len(_sent) == 3, f"实际发出 3 条（噪音 + 入场 + 另一持仓入场），发出 {len(_sent)} 条")
check(sum(1 for t in _decoded if "已成交入场" in t) == 2,
      "两条入场提醒都在（不同持仓未被误杀）")

print("\n【7】真实刷屏回放：同一笔单反复触发 TP1 → 只推 1 次")
reset()
_sent.clear()
T.urllib.request.urlopen = _fake_urlopen
try:
    for i in range(19):
        T.push_once("BTC-old:partial:TP1",
                    card_partial("BTC-old").replace("81,105.2", f"{81105.2 + i*0.1:.1f}"),
                    token="x", chat_id="y")
finally:
    T.urllib.request.urlopen = _real_urlopen
check(len(_sent) == 1, f"19 次触发只发出 1 条（实际 {len(_sent)}）")

print("\n【8】推送失败 → 撤销幂等标记，允许重试")
reset()
_sent.clear()


def _fail_urlopen(req, timeout=None):
    raise OSError("network down")


T.urllib.request.urlopen = _fail_urlopen
try:
    r_fail = T.push_once("BTC-9:fill", card_fill("BTC-9"), token="x", chat_id="y")
finally:
    T.urllib.request.urlopen = _real_urlopen
check(not r_fail, "推送失败返回 False")
reset()
_sent.clear()
T.urllib.request.urlopen = _fake_urlopen
try:
    r_retry = T.push_once("BTC-9:fill", card_fill("BTC-9"), token="x", chat_id="y")
finally:
    T.urllib.request.urlopen = _real_urlopen
check(r_retry, "失败后允许重试（幂等标记已撤销）")

# ------------------------------------------------------------------
print("\n" + "=" * 60)
if FAILS:
    print(f"❌ 失败 {len(FAILS)} 项：")
    for f in FAILS:
        print("   -", f)
    sys.exit(1)
print("✅ 全部通过")
