#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Telegram 推送（独立模块）
==========================

之前散落在 watch.py 里，被 position_tracker.py / watch.py / scan.py 共用。
抽出后：
- position_tracker.py 直接 `from telegram import ...`（不再依赖 watch）
- watch.py / 未来的 cron 脚本也能直接用

API：
  - load_token()        : 读 bot token
  - load_chat(default)  : 读 chat_id
  - push(text, ...)     : 发消息
  - format_card(...)    : 把 alert + scan_payload 压成 Telegram HTML 卡片
"""
from __future__ import annotations
import os, sys, json, time, urllib.request
from pathlib import Path
from typing import Optional, Dict

OK, NO, WARN = "✅", "❌", "⚠️"

# ============================ 路径 ============================
SECRETS_PATH = Path("/root/.hermes/secrets/btc_tg.json")
DEFAULT_CHAT_ID = "7097652385"

# 2026-10-06 加：所有 TG 推送留痕（应对"莫名推送"无法溯源的问题）
# 路径：/root/data/tg_audit.log
# 字段：ts, caller, char_len, first_line, ok
TG_AUDIT_PATH = Path(os.environ.get("BTCTG_AUDIT", "/root/data/tg_audit.log"))


def _audit(card: str, ok: bool, chat_id: str = "") -> None:
    """推送留痕：谁 + 何时 + 多长 + 第一行内容 + 是否成功"""
    try:
        ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
        # 提取 caller（用 inspect 拿到调用栈）
        caller = ""
        try:
            import inspect
            st = inspect.stack()
            for f in st[1:6]:
                fn = os.path.basename(f.filename)
                ln = f.lineno
                if fn not in ("telegram.py", "watch/__init__.py", "watch/runner.py", "<string>"):
                    caller = f"{fn}:{ln}"
                    break
        except Exception:
            pass
        # 第一行（卡片标题）
        first_line = card.strip().split("\n", 1)[0][:80] if card else ""
        line = json.dumps({
            "ts": ts,
            "ep": int(time.time()),
            "caller": caller,
            "len": len(card or ""),
            "chat": chat_id,
            "title": first_line,
            "ok": ok,
        }, ensure_ascii=False)
        with open(TG_AUDIT_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass  # 审计失败不能影响推送


# ============================ 凭证 ============================
def load_token() -> Optional[str]:
    """
    读取 Telegram bot token。优先级：
      1. /root/.hermes/secrets/btc_tg.json {"token": "...", "chat_id": "..."}
      2. BTC_TG_TOKEN 环境变量
      3. None（不推送）
    """
    if SECRETS_PATH.exists():
        try:
            return json.load(open(SECRETS_PATH))["token"]
        except Exception as e:
            print(f"{WARN} secrets 文件读取失败: {e}", file=sys.stderr)
    return os.environ.get("BTC_TG_TOKEN")


def load_chat(default: str = DEFAULT_CHAT_ID) -> str:
    """读 chat_id：secrets 文件 > BTC_TG_CHAT 环境变量 > 默认值"""
    if SECRETS_PATH.exists():
        try:
            cid = json.load(open(SECRETS_PATH)).get("chat_id")
            if cid:
                return str(cid)
        except Exception:
            pass
    return os.environ.get("BTC_TG_CHAT", default)


# ============================ 推送 ============================
def _escape_html(text: str) -> str:
    """
    转义 HTML 模式下裸的 < / > / &，防止 TG API 报 400。
    但保留合法标签（<b>、<i>、<code>、<pre>、<u>、<s>）和合法实体（& < >）。
    实现思路：先抽出合法标签/实体成 placeholder，转义剩余字符，再换回。
    """
    import re
    # 合法标签列表（TG HTML 支持的子集）
    ALLOWED_TAGS = ("b", "i", "u", "s", "code", "pre", "a")

    placeholders = []

    # 1. 保护合法开标签 <tag> 或 <tag attr="...">
    def _save_tag(m):
        placeholders.append(m.group(0))
        return f"\x00TAG{len(placeholders)-1}\x00"
    text = re.sub(r"</?\s*(?:" + "|".join(ALLOWED_TAGS) + r")\b[^>]*>", _save_tag, text)

    # 2. 保护合法实体 & < > "
    def _save_entity(m):
        placeholders.append(m.group(0))
        return f"\x00ENT{len(placeholders)-1}\x00"
    text = re.sub(r"&(?:amp|lt|gt|quot);", _save_entity, text)

    # 3. 转义剩余的 & < >
    text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    # 4. 换回 placeholder
    def _restore(m):
        kind, idx = m.group(1), int(m.group(2))
        return placeholders[idx]
    text = re.sub(r"\x00(TAG|ENT)(\d+)\x00", _restore, text)

    return text


def push(card: str, token: Optional[str] = None, chat_id: Optional[str] = None) -> bool:
    """
    直接 POST 到 Telegram Bot API。成功 True / 失败 False（永不抛）。
    token/chat_id 为 None 时自动用 load_token/load_chat 读。

    HTML 转义：调用方写的 <b>/<code> 等合法标签会被保留，但裸的 < / > / &
    （如 "106 点 < 500"、"score > 3"）会自动转成 < / > / &。
    避免 TG API 报 400: "can't parse entities: Unsupported start tag"。
    """
    if token is None:
        token = load_token()
    if chat_id is None:
        chat_id = load_chat()
    if not token or not chat_id:
        return False
    try:
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        body = json.dumps({
            "chat_id": chat_id,
            "text": _escape_html(card),
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }).encode("utf-8")
        req = urllib.request.Request(url, data=body,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            raw = resp.read().decode("utf-8")
            ok = '"ok":true' in raw
            if not ok:
                print(f"{WARN} TG 返回非 ok: {raw[:200]}", file=sys.stderr)
            _audit(card, ok, chat_id=chat_id)
            return ok
    except Exception as e:
        print(f"{NO} TG 推送失败: {e}", file=sys.stderr)
        _audit(card, False, chat_id=chat_id or "")
        return False


# ============================ 卡片格式化 ============================
def format_card(kind: str, alert: Dict, scan_payload: Optional[Dict],
                override_active: Optional[Dict] = None) -> str:
    """
    把 alert + scan 结果压成 6-10 行 Telegram 卡片（HTML 格式）。
    kind="volume" / "rsi" / "periodic"：标题/排版各有不同。
    字段缺失老实写"无法解析"，不编。

    override_active（2026-10-04 新增）：
      已进场的 journal 条。如果存在：
        - 同方向：用其 entry/TP/SL/contracts 替换 scan_payload 中的数据（整张卡片复用）
        - 反方向：仍展示 scan_payload（反方向点位有效）
        - 卡片加前缀 "📌 已进场（同方向/反方向）：id=XXX"
      这样同一合约已有进场时不会重复展示新点位，避免误读。
    """
    a = alert.get("alert", alert)            # emit_scan 包了一层 alert 字段
    if kind == "rsi":
        side = a.get("side", "")
        val = a.get("val", 0)
        prev = a.get("prev", 0)
        period = a.get("period", 14)
        tf = a.get("tf", "?")
        px = a.get("px", 0)
        icon = "🟢" if side == "超卖" else "🔴"
        title = f"{icon} <b>BTC RSI 越界</b>  {side}"
        lines = [title]
        lines.append(f"RSI{period}({tf}) {prev:.1f} → <b>{val:.1f}</b> ｜ 最新价 {px:,.1f}" if px else
                    f"RSI{period}({tf}) {prev:.1f} → <b>{val:.1f}</b>")
        lines.append("⚠️ 这是「提醒」不是「信号」：触发只说明有事发生，决策看下方 scan。")
    elif kind == "periodic":
        # 2026-10-06：周期检查卡片独立分支
        # 目的：让人一眼看出"这是定时趋势检查"，而不是被误读成量能异动。
        # 只展示两件事——当前趋势（多/空/震荡）+ 多空是否发生转换。
        tag = a.get("tag", "周期检查")     # "周期检查 @ HH:MM"
        close = a.get("close", 0)
        ts = a.get("ts", 0)
        hh_mm = time.strftime("%H:%M", time.localtime(ts)) if ts else time.strftime("%H:%M")
        lines = [f"🕐 <b>BTC 周期检查</b>  {hh_mm}（每 15 分钟）"]
        lines.append(f"现价 <b>{close:,.1f}</b>")
        lines.append("🧭 这是定时趋势检查，不是量能/RSI 触发。")

        # 趋势判定 + 转换信号（仅当 scan_payload 有 trend tag 时）
        if scan_payload:
            tags = scan_payload.get("tags") or {}
            cur_trend = tags.get("trend", "")
            if cur_trend:
                prev_trend = a.get("prev_trend", "")   # 由 fire_periodic 注入
                cur_label = "📈 多头" if "多头" in cur_trend else (
                            "📉 空头" if "空头" in cur_trend else "〰️ 震荡")
                lines.append(f"当前趋势：<b>{cur_label}</b>（{cur_trend}）")

                # 转换信号：本次 vs 上次 trend 不一致 → 提示
                if prev_trend and prev_trend != cur_trend:
                    prev_label = "📈 多头" if "多头" in prev_trend else (
                                "📉 空头" if "空头" in prev_trend else "〰️ 震荡")
                    if "多头" in cur_trend and "空头" in prev_trend:
                        icon, signal = "🔁", "空头 → 多头（潜在反转）"
                    elif "空头" in cur_trend and "多头" in prev_trend:
                        icon, signal = "🔁", "多头 → 空头（潜在反转）"
                    elif ("多头" in cur_trend or "空头" in cur_trend) and "震荡" in prev_trend:
                        icon, signal = "⚡", f"震荡 → {cur_label}（破位）"
                    elif ("多头" in prev_trend or "空头" in prev_trend) and "震荡" in cur_trend:
                        icon, signal = "⚠️", f"{prev_label} → 震荡（趋势走弱）"
                    else:
                        icon, signal = "🔁", f"{prev_label} → {cur_label}"
                    lines.append(f"{icon} <b>转换信号</b>：{signal}")
                elif prev_trend == cur_trend:
                    lines.append("⏸ 无转换（趋势延续）")
            # 关键阻力/支撑浓缩到一行（避免 1m 量那堆噪声）
            plan = (scan_payload.get("plan") or {})
            entry = plan.get("entry_limit") or plan.get("entry_price")
            stop = plan.get("sl") or plan.get("stop_price")
            if entry and stop:
                lines.append(f"关键位：支撑 <b>{stop:,.1f}</b> ｜ 阻力（entry 之上）{entry:,.1f}")
            else:
                lines.append("关键位：scan 未给出")
        return "\n".join(lines)
    else:
        ratio = a.get("ratio", 0)
        z = a.get("z", 0)
        tag = a.get("tag", "异动")
        close = a.get("close", 0)
        vol = a.get("vol", 0)
        lines = [f"🚨 <b>BTC 量能异动</b>  {tag}"]
        lines.append(f"ratio <b>{ratio:.2f}×</b> ｜ z <b>{z:.2f}</b> ｜ 现价 <b>{close:,.1f}</b>")
        lines.append(f"1m 量 {vol:,.0f} 张")
        lines.append("⚠️ 这是「提醒」不是「信号」：触发只说明有事发生，决策看下方 scan。")

    if not scan_payload:
        lines.append("")
        lines.append("⚠️ scan 未返回结果")
        return "\n".join(lines)

    # 情绪上下文（scan.py payload 里的 event_note / funding_note）
    mood = scan_payload.get("event_note") or scan_payload.get("funding_note")
    if mood:
        lines.append(f"⚠️ 情绪上下文：{mood}")

    verdict = scan_payload.get("verdict") or "无法解析"
    side = scan_payload.get("side") or scan_payload.get("main_side") or "?"
    sl = scan_payload.get("score_long")
    ss = scan_payload.get("score_short")
    score_str = f"long {sl} / short {ss}" if (sl is not None or ss is not None) else "?"

    plan = scan_payload.get("plan") or {}

    # ===== override_active 处理（2026-10-04 新增）=====
    # 已有进场时：同方向 → 整张卡片用进场数据；反方向 → 保留 scan 数据
    # 2026-10-05 加 fill 实况：成交价、成交后经过多久、当前 vs fill_px 距离、未实现盈亏
    _overridden_plan = None
    _overridden_marker = ""
    _fill_status_block = ""        # 新增：已进场的"实况"块
    if override_active:
        _active_side = override_active.get("side")
        _active_id = override_active.get("id", "?")
        _fill_px = override_active.get("fill_px")
        _fill_ts = override_active.get("fill_ts")
        if _active_side == side:
            # 同方向：用 override 数据
            _overridden_plan = {
                "entry_limit": override_active.get("entry"),
                "sl": override_active.get("sl"),
                "tp1": override_active.get("tp1"),
                "tp2": override_active.get("tp2"),
                "tp3": override_active.get("tp3"),
                "contracts": override_active.get("contracts"),
                "sl_pct": override_active.get("sl_pct"),
                "tp1_pct": override_active.get("tp1_pct"),
                "tp2_pct": override_active.get("tp2_pct"),
                "tp3_pct": override_active.get("tp3_pct"),
            }
            _overridden_marker = (f"\n📌 <b>已进场（同方向）</b> ｜ id=<code>{_active_id[:30]}</code>"
                                  f"\n   沿用进场 entry/TP/SL（scan 不重复计算）\n")
            # ★ 渲染 fill 实况：成交价 / 已过多久 / 距 TP/SL 当前盈亏
            if _fill_px is not None:
                _fill_time_str = ""
                if _fill_ts:
                    import time as _t
                    _elapsed = int(_t.time()) - int(_fill_ts)
                    _h = _elapsed // 3600
                    _m = (_elapsed % 3600) // 60
                    _fill_time_str = f"{_h}h{_m}min 前" if _h > 0 else f"{_m}min 前"

                _cur_px = scan_payload.get("last") if scan_payload else None
                _contracts = override_active.get("contracts") or 0
                _qty = _contracts * 0.0001   # BTC

                _dist_html = ""
                _pnl_html = ""
                if _cur_px is not None:
                    # 距 fill_px 的距离（带方向符号——跌写 + 因为方向对齐你之前说过）
                    if side == "long":
                        _diff = _cur_px - _fill_px
                    else:
                        _diff = _fill_px - _cur_px
                    _diff_pct = _diff / _fill_px * 100
                    _diff_sign = "+" if _diff >= 0 else ""    # 跌也是 +/- 显示，不用「跌 5 点 = +5」错向
                    _dist_html = f"当前 <b>{_cur_px:,.1f}</b> ({_diff_sign}{_diff:.0f} 点 / {_diff_sign}{_diff_pct:.2f}%)"

                    _gross = _diff * _qty
                    _net = _gross - 4.85     # 单笔成本（position_tracker 用 4.85U）
                    _net_sign = "+" if _net >= 0 else ""
                    _pnl_color = "🟢" if _net >= 0 else "🔴"
                    _pnl_html = f"  未实现 {_pnl_color} <b>{_net_sign}{_net:.2f}U</b>（已扣费 4.85U）"

                _fill_status_block = (
                    f"\n📍 <b>已进场实况</b>"
                    f"\n   成交价 <b>{_fill_px:,.1f}</b>"
                    + (f" ｜ {_fill_time_str}" if _fill_time_str else "")
                    + (f"\n   {_dist_html}" if _dist_html else "")
                    + (f"\n   张数 {_contracts}" + _pnl_html if _pnl_html else f"\n   张数 {_contracts}")
                    + "\n"
                )
        else:
            # 反方向：保留 scan 数据 + 标记
            _overridden_marker = (f"\n📌 <b>已进场（反方向 {_active_side}）</b> ｜ "
                                  f"id=<code>{_active_id[:30]}</code>"
                                  f"\n   触发反方向信号 → 自动平仓 + 写反方向 journal\n")
            # 反方向也会展示：fill_px + 浮盈亏，让用户知道现有持仓浮盈亏多少
            if _fill_px is not None:
                _cur_px = scan_payload.get("last") if scan_payload else None
                _fill_time_str = ""
                if _fill_ts:
                    import time as _t
                    _elapsed = int(_t.time()) - int(_fill_ts)
                    _h = _elapsed // 3600
                    _m = (_elapsed % 3600) // 60
                    _fill_time_str = f"{_h}h{_m}min 前" if _h > 0 else f"{_m}min 前"
                _dist_html = ""
                if _cur_px is not None:
                    if _active_side == "long":
                        _diff = _cur_px - _fill_px
                    else:
                        _diff = _fill_px - _cur_px
                    _diff_pct = _diff / _fill_px * 100
                    _diff_sign = "+" if _diff >= 0 else ""
                    _dist_html = f"  当前 <b>{_cur_px:,.1f}</b> ({_diff_sign}{_diff:.0f} 点 / {_diff_sign}{_diff_pct:.2f}%)"
                _fill_status_block = (
                    f"\n📍 <b>当前持仓（反方向）实况</b>"
                    f"\n   方向 {_active_side} ｜ 成交价 <b>{_fill_px:,.1f}</b>"
                    + (f" ｜ {_fill_time_str}" if _fill_time_str else "")
                    + _dist_html
                    + "\n"
                )

    _plan_src = _overridden_plan if _overridden_plan else plan
    entry = _plan_src.get("entry_limit") or _plan_src.get("entry_price")
    stop = _plan_src.get("sl") or _plan_src.get("stop_price")
    stop_pct = _plan_src.get("sl_pct") or _plan_src.get("stop_pct")
    exits = []
    for label, price_key, pct_key in [
        ("T1", "tp1", "tp1_pct"),
        ("T2", "tp2", "tp2_pct"),
        ("T3", "tp3", "tp3_pct"),
    ]:
        p = _plan_src.get(price_key)
        if p is not None:
            exits.append({"label": label, "price": p, "pct": _plan_src.get(pct_key, "?")})
    if not exits and isinstance(_plan_src.get("exits"), list):
        exits = _plan_src["exits"]
    qty = _plan_src.get("contracts") or _plan_src.get("position_qty") if isinstance(_plan_src.get("position"), dict) else _plan_src.get("contracts")
    notional = _plan_src.get("notional")

    # ===== 2026-10-06 表达优化 =====
    # 1. 判定 3 档分级（信号成立 / 临界 / 无信号），行动建议一眼能区别
    # 2. 价位加依据（来自 payload.support/resistance/tf）
    # 3. 判定语气更明确（"再等 1 根" / "放弃" / "按计划入场"）
    # 4. 临界 / 无信号 档：plan 标"参考"标记，避免误跟单
    verdict_s = str(verdict or "")
    is_critical = any(k in verdict_s for k in ("核心满足", "满足", "可进场", "信号成立"))
    is_boundary = "临界" in verdict_s            # 临界（再等 1 根确认）
    is_empty = "无信号" in verdict_s or "观望" in verdict_s

    # 顶部：行动建议行（颜色徽章）
    if is_critical:
        lines.append(f"🟢 <b>行动</b>：信号成立 → 按下方计划挂单")
        action_label = "🟢"
    elif is_boundary:
        lines.append(f"🟡 <b>行动</b>：趋势弱（{score_str}），下根未确认前 <b>不要进场</b>")
        action_label = "🟡"
    elif is_empty:
        lines.append(f"🔴 <b>行动</b>：无信号 → <b>空仓观望</b>，回归等待")
        action_label = "🔴"
    else:
        lines.append(f"⚪ 判定：{verdict}")
        action_label = "⚪"

    # 判定行（语气更明确）
    if is_critical:
        lines.append(f"判定：<b>{verdict_s}</b>")
    elif is_boundary:
        lines.append(f"判定：<b>{verdict_s}</b>（下根不反转就放弃）")
    elif is_empty:
        lines.append(f"判定：<b>{verdict_s}</b>（核心未满足，不应入场）")
    else:
        lines.append(f"判定：{verdict_s}")

    # 方向 + 6 分评分（保留）
    lines.append(f"方向：<b>{side}</b> ({score_str})")

    # ===== 价位依据 =====
    # 解析 plan 入场/止损/止盈是从哪个结构位/EMA 算出来的
    # 依据字段：payload.support/resistance（按距离现价最近的几条），payload.tf['1h']['atrp']/['ema144']
    _basis = []    # 每条依据（最近支撑/阻力 + EMA + ATR）
    try:
        sup_list = (scan_payload.get("support") or []) if scan_payload else []
        res_list = (scan_payload.get("resistance") or []) if scan_payload else []
        if sup_list:
            _s0 = sup_list[0]
            _basis.append(f"近支撑 {(_s0.get('name') or '支撑位')} {_s0.get('price', 0):,.0f}")
        if res_list:
            _r0 = res_list[0]
            _basis.append(f"近阻力 {(_r0.get('name') or '阻力位')} {_r0.get('price', 0):,.0f}")
        tf_data = (scan_payload.get("tf") or {}).get("1h") if scan_payload else None
        if tf_data and tf_data.get("ema144"):
            _basis.append(f"1h EMA144 {tf_data['ema144']:,.0f}")
        if tf_data and tf_data.get("atrp"):
            _basis.append(f"1h ATR {tf_data['atrp']:.2f}%")
        if scan_payload and scan_payload.get("sideways"):
            _basis.append("⚠️ 横盘")
        if _plan_src.get("target_pts"):
            _basis.append(f"目标 {_plan_src['target_pts']:,.0f} 点封顶")
    except Exception:
        pass

    if _basis:
        # 一行展示最多 3 个依据（挤不下就截断）
        lines.append("依据：" + " ｜ ".join(_basis[:4]))

    # ===== 入场 / 止损 =====
    # 临界 / 无信号档：plan 加「参考」标记
    _plan_tag = ""
    if is_boundary or is_empty:
        _plan_tag = " <i>（参考：判定非成立，不应跟单）</i>"

    if entry and stop:
        lines.append(f"入场 <b>{entry:,.1f}</b> ｜ 止损 <b>{stop:,.1f}</b> ({stop_pct or '?'}){_plan_tag}")
    else:
        lines.append(f"入场 / 止损：scan 未给出{_plan_tag}")

    if exits:
        tp_parts = []
        for e in exits:
            if isinstance(e, dict):
                lbl = e.get("label", "?")
                px = e.get("price", 0)
                pct = e.get("pct", "?")
                tp_parts.append(f"{lbl} {px:,.1f} ({pct})" if isinstance(px, (int, float))
                                else f"{lbl} {px} ({pct})")
        if tp_parts:
            lines.append("止盈  " + " / ".join(tp_parts) + _plan_tag)
    else:
        lines.append(f"止盈：scan 未给出{_plan_tag}")

    if qty:
        # 仓位加依据：风险% × 余额 / sl_pct，夹在 50× 余额上限
        _qty_basis = ""
        try:
            _lev = (_plan_src.get("notional_x") or 0)
            if _lev:
                _qty_basis = f"（{_lev:.1f}x 账户，止损 {_plan_src.get('sl_pct', '?')}% 满仓上限）"
        except Exception:
            pass
        lines.append(f"仓位 {qty} 张（名义 {notional or '?'} U）{_qty_basis}{_plan_tag}")

    # 把 override_active 标记 + fill 实况插到末尾
    if _overridden_marker:
        lines.append(_overridden_marker.rstrip("\n"))
    if _fill_status_block:
        lines.append(_fill_status_block.rstrip("\n"))

    return "\n".join(lines)
