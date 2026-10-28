#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
★ 2026-10-29 回归测试：出场/止盈/平仓卡静默丢失 + 训练/生产无标识

三个 bug（都是 liusir 10-29 发现）：
  ① push_once() 没加 [训练]/[生产] 前缀 → 入场/止盈/平仓卡全无标识，
     生产单和训练单在同一个 chat 里长得一模一样。
  ② risk_guard 用实时 ticker 抢先结算（settler 用 1m K 回放，最坏延迟 60s），
     结算后把 status 改成 settled → settler 的 settle_all 只处理 status=='pending'
     → position_tracker._notify_close 永远不被调用 → **平仓卡永久丢失**。
     实测 10-25 之后 12 笔结算全部无卡（生产 7 + 训练 5），含 -15.41U 的 SL。
  ③ risk_guard 的 TP1 分支只改内存状态、从不推卡 → TP1 部分止盈卡丢失
     （settler 看到 tp1_hit=True 就走「跳过重复 partial」分支，永不补推）。
     实测 10-22 ~ 10-26 有 6 笔 TP1 命中零卡片。

另外修了两处口径问题：
  ④ BE 零位移噪音门只看 exit_px≈entry、不看净利 → 连 +23.47U / +24.88U 的
     真盈利通知一起吞掉。
  ⑤ _notify_partial 用 `contracts // PARTS` 反推本段卖出张数，
     settler 路径（先减仓再传）和 risk_guard 路径（不减）口径不一致 →
     同一笔单在两条路径下卡上张数对不上。

跑法：python3 scripts/tests/test_notify_missing_cards.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(SCRIPTS.parent))

# ★ 审计日志隔离：本测试会真的走 telegram.push_once 的发送路径（urlopen 被 mock，
#   但 _audit 会照写）→ 不隔离的话会把「ok=true 的假推送」写进生产
#   /root/data/tg_audit.log，污染事后溯源（10-29 实测污染 6 行）。
os.environ["BTCTG_AUDIT"] = str(Path(tempfile.gettempdir()) / "btc_tg_audit_test.log")

FAILS = []


def check(cond: bool, label: str, extra: str = "") -> None:
    if cond:
        print(f"  ✅ {label}")
    else:
        print(f"  ❌ {label} {extra}")
        FAILS.append(label)


def _reset_dedup():
    import telegram as T
    with T._DEDUP_LOCK:
        T._IDEMPOTENT_SEEN.clear()
        T._LAST_PUSH_TS = 0.0


# ------------------------------------------------------------------
print("\n【1】push_once 必须带 [生产]/[训练] 标识")
import telegram as T  # noqa: E402

_reset_dedup()
_sent = []


class _FakeResp:
    def read(self):
        return b'{"ok":true}'

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _fake_urlopen(req, timeout=None):
    # JSON 默认 ensure_ascii=True → 中文会变成 \uXXXX 转义。
    # 断言要针对**解码后**的文本，所以这里先 json.loads 取 text 字段。
    import json as _json
    _sent.append(_json.loads(req.data.decode("utf-8")).get("text", ""))
    return _FakeResp()


_real_urlopen = T.urllib.request.urlopen
T.urllib.request.urlopen = _fake_urlopen
try:
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("TRAINING_MODE", None)
        T.push_once("BTC-1:fill", "🎯 <b>BTC 已成交入场</b>\n做多 616 张", token="x", chat_id="y")
    prod_text = _sent[-1]

    _reset_dedup()
    with patch.dict(os.environ, {"TRAINING_MODE": "1"}):
        T.push_once("BTC-2:fill", "🎯 <b>BTC 已成交入场</b>\n做多 616 张", token="x", chat_id="y")
    train_text = _sent[-1]
finally:
    T.urllib.request.urlopen = _real_urlopen

check("[生产]" in prod_text, "push_once 生产卡带 [生产] 标识", prod_text[:120])
check("[训练]" in train_text, "push_once 训练卡带 [训练] 标识", train_text[:120])
check("已成交入场" in prod_text and "已成交入场" in train_text,
      "加前缀不影响卡片正文")

# ------------------------------------------------------------------
print("\n【2】risk_guard 抢先结算后必须推平仓卡")
from watch import risk_guard as rg  # noqa: E402
import position_tracker as pt  # noqa: E402

_rec = {
    "id": "BTC_USDT-TEST-long", "side": "long", "entry": 81187.5,
    "tp1": 81999.4, "tp2": 82405.3, "sl": 80918.6,
    "status": "pending", "filled": True,
    "fill_ts": 1793222880, "fill_px": 81187.5, "contracts": 616,
}


def _run_close(rec):
    """跑 risk_guard 的 SL 结算 + 推卡路径，返回被推的卡片文本列表"""
    pushed = []
    with patch("position_tracker.push_once_telegram",
               side_effect=lambda k, t, *a, **kw: pushed.append((k, t))), \
         patch("position_tracker.load_token", return_value="fake"), \
         patch("position_tracker.load_chat", return_value="fake"):
        rg._apply_sl(rec, last=80900.0, now_ts=1793222900)
        rg._notify_close_from_record(rec)
    return pushed


pushed = _run_close(dict(_rec))
check(len(pushed) == 1, f"risk_guard 结算后推 1 张平仓卡（实际 {len(pushed)}）")
if pushed:
    key, text = pushed[0]
    check(key.endswith(":close"), f"幂等 key 是 {{id}}:close（实际 {key}）")
    check("平仓" in text, "卡片是平仓卡")
    check("-15" in text or "净利" in text, "卡片带净利数字")
    # 标识不在这一层加：position_tracker 走 push_once_telegram（已 mock），
    # [生产]/[训练] 前缀由 telegram.push_once 统一加（见【1】）→ 两处共用同一路径。

# 幂等：同一 id 再推一次，push_once 自己会拦
_reset_dedup()
pushed2 = _run_close(dict(_rec))
check(len(pushed2) == 1, "同 id 平仓卡走 push_once → 幂等窗口内只推一次")

# ------------------------------------------------------------------
print("\n【3】risk_guard 的 TP1 分支必须推部分止盈卡")
_pushed_tp1 = []


def _run_tp1(rec):
    out = []
    with patch("position_tracker.push_once_telegram",
               side_effect=lambda k, t, *a, **kw: out.append((k, t))), \
         patch("position_tracker.load_token", return_value="fake"), \
         patch("position_tracker.load_chat", return_value="fake"):
        rg._try_partial_close(rec, 82010.0, 50, "tp1_hit @ 82010.0")
    return out


_reset_dedup()
out = _run_tp1(dict(_rec))
check(len(out) == 1, f"risk_guard TP1 触发推 1 张部分止盈卡（实际 {len(out)}）")
if out:
    key, text = out[0]
    check(key.endswith(":partial:TP1"), f"幂等 key 是 {{id}}:partial:TP1（实际 {key}）")
    check("部分止盈" in text, "卡片是部分止盈卡")
    # 张数口径：616 张原始量 → 1/3 = 205 张
    check("205 张" in text, f"张数口径 = 原始量//3 = 205 张（实际卡：{text[:200]}）")

# ------------------------------------------------------------------
print("\n【4】BE 零位移噪音门不能吞真收益（净利 >= 5U 必须推）")
_reset_dedup()


def _run_close_notify(sig, res):
    out = []
    with patch("position_tracker.push_once_telegram",
               side_effect=lambda k, t, *a, **kw: out.append((k, t))), \
         patch("position_tracker.load_token", return_value="fake"), \
         patch("position_tracker.load_chat", return_value="fake"):
        pt._notify_close(sig, res)
    return out


_sig = {"id": "BTC_USDT-TEST2-long", "side": "long", "entry": 79400.0,
        "tp1": 81999.4, "tp2": 82405.3}
# 真实案例：TP1 已锁利，最后保本被打 → exit_px == entry，但净利 +23.47U
out = _run_close_notify(_sig, {"outcome": "BE_STOPPED", "category": "profit",
                               "net_usd": 23.47, "exit_px": 79400.0,
                               "exit_ts": 1793222900, "contracts": 420,
                               "partial_exits": 2})
check(len(out) == 1, f"零位移但净利 +23.47U → 必须推卡（实际 {len(out)}）")

# 真噪音：净利 3.15U + 零位移 → 该跳
out = _run_close_notify(_sig, {"outcome": "BE_STOPPED", "category": "profit",
                               "net_usd": 3.15, "exit_px": 79400.0,
                               "exit_ts": 1793222900, "contracts": 420,
                               "partial_exits": 2})
check(len(out) == 0, f"零位移 + 净利 3.15U → 真噪音，跳过（实际推了 {len(out)}）")

# ------------------------------------------------------------------
print("\n【5】_notify_partial 张数口径由调用方显式传，不靠反推")
_reset_dedup()
_sig2 = {"id": "BTC_USDT-TEST3-long", "side": "long", "entry": 80000.0,
         "tp1": 81000.0, "tp2": 82000.0, "_orig_contracts": 600}
out = []
with patch("position_tracker.push_once_telegram",
           side_effect=lambda k, t, *a, **kw: out.append((k, t))), \
     patch("position_tracker.load_token", return_value="fake"), \
     patch("position_tracker.load_chat", return_value="fake"):
    # settler 路径：先把 contracts 减到 2/3（400），再传进来
    pt._notify_partial(_sig2, "TP1", 81000.0, 400, 1793222900, qty=200)
check(len(out) == 1, "带 qty 参数能推卡")
if out:
    _, text = out[0]
    check("200 张" in text, f"卡上写实际卖出 200 张（实际：{text[:200]}）")
    # 锁利金额也要按 200 张算：200 * 1000 点 * 0.0001 = 20.00U
    check("+20.00U" in text, f"锁利金额按 200 张算 = +20.00U（实际：{text[:200]}）")

# ------------------------------------------------------------------
print("\n" + "=" * 60)
if FAILS:
    print(f"❌ {len(FAILS)} 项失败：")
    for f in FAILS:
        print(f"   - {f}")
    sys.exit(1)
print("✅ 全部通过")
