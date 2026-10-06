#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
watch 编排层：emit_scan + fire + fire_rsi + run + daemonize + main
===============================================================
"""
from __future__ import annotations
import os, sys, json, time, signal, argparse, subprocess, threading
from pathlib import Path
from typing import Dict, List, Optional

from gate_fetch import DATA_DIR, GateWS, fetch_ticker, assert_usdt_linear
from telegram import load_token as _load_tg_token, load_chat as _load_tg_chat, push as push_telegram, format_card as format_tg_card

from .common import log, NO, WARN, LOG, PID, ALERT_DIR, SCAN, STALE_SEC
from .jump import JumpTracker
from .volume import VolumeWatcher
from .rsi import RsiWatcher
from .settler import start_settler


# ============================ 触发 ============================
def emit_scan(contract: str, tf: str, kind: str, a: Dict, target: float,
              do_scan: bool, extra: List[str], hook_text: str,
              push_tg: bool = True) -> Dict:
    """公共触发动作：跑 scan.py → 存档告警 → 可选 webhook"""
    out = {"alert": a, "kind": kind, "contract": contract, "tf": tf,
           "fired_at": int(time.time()), "scan": None}
    if do_scan:
        jf = os.path.join(ALERT_DIR, f"scan-{time.strftime('%Y%m%d-%H%M%S')}.json")
        os.makedirs(ALERT_DIR, exist_ok=True)
        cmd = [sys.executable, SCAN, contract, "--brief", "--target", str(target),
               "--json", jf] + extra
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
            txt = (r.stdout or "") + (r.stderr or "")
            for ln in txt.strip().splitlines():
                log("   " + ln)
            out["scan"] = {"cmd": " ".join(cmd[1:]), "rc": r.returncode, "output": txt}
            if os.path.exists(jf):
                out["scan"]["payload"] = json.load(open(jf, encoding="utf-8"))
        except Exception as e:
            log(f"{NO} 触发 scan 失败: {e}")
            out["scan"] = {"error": str(e)}

    os.makedirs(ALERT_DIR, exist_ok=True)
    fp = os.path.join(ALERT_DIR, f"alert-{time.strftime('%Y%m%d-%H%M%S')}.json")
    json.dump(out, open(fp, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    log(f"   告警已存档 {fp}")

    url = os.environ.get("BTC_WATCH_WEBHOOK")
    if url:
        try:
            import urllib.request
            body = json.dumps({"text": hook_text}, ensure_ascii=False).encode()
            urllib.request.urlopen(urllib.request.Request(
                url, data=body, headers={"Content-Type": "application/json"}), timeout=10)
            log("   webhook 已推送")
        except Exception as e:
            log(f"{WARN} webhook 失败: {e}")

    # Telegram 直推（绕过 cron 延迟，覆盖量能 + RSI 两条通道）
    if not push_tg:
        log("   TG 推送跳过（push_tg=False，本通道选择不推）")
        return out
    tg_token = _load_tg_token()
    tg_chat = _load_tg_chat()
    if tg_token:
        try:
            scan_payload = (out.get("scan") or {}).get("payload")
            # 找当前合约已进场单：position_tracker 用 status="pending" + filled=true
            # 表示「已成交未平仓」，不是字面意义的"挂单"。这是上面那次修过的语义。
            _override = None
            try:
                from gate_fetch import DATA_DIR
                _jpath = os.path.join(DATA_DIR, "journal.jsonl")
                if os.path.exists(_jpath):
                    _jrecs = [json.loads(ln) for ln in open(_jpath, encoding="utf-8") if ln.strip()]
                    for _jr in reversed(_jrecs):
                        if (_jr.get("contract") == contract
                                and _jr.get("status") == "pending"
                                and _jr.get("filled") is True):
                            _override = _jr
                            break
            except Exception:
                _override = None

            card = format_tg_card(kind, out, scan_payload, override_active=_override)
            ok = push_telegram(card, tg_token, tg_chat)
            log(f"   TG 推送 {'成功' if ok else '失败'}")
        except Exception as e:
            log(f"{NO} TG 推送异常: {e}")
    return out


# ============================ 1h 收盘检查 ============================
# 2026-10-05：每小时整点后跑一次结构评估。
# 量能异动是 trigger，1h K 线收完才是真"信号"——结构变了才考虑动 journal。
# 2026-10-05：趋势检查周期从 1h 改成 15min（liusir 觉得 1h 太久了）
# 15 分钟一个窗口，整点 :00 / :15 / :30 / :45 触发，窗口内 90s 节流
PERIOD_SEC = 900          # 15 分钟
WINDOW_SEC = 90           # 窗口宽度
_last_periodic_bucket: int = 0   # 模块级去重
_LAST_TREND_PATH = os.path.join(DATA_DIR, "last_periodic_trend.json")


def _load_last_trend() -> str:
    """读上次周期检查的 trend tag（"多头排列" / "空头排列" / "震荡" / 空字符串）。"""
    try:
        if os.path.exists(_LAST_TREND_PATH):
            obj = json.load(open(_LAST_TREND_PATH, encoding="utf-8"))
            return obj.get("trend", "")
    except Exception:
        pass
    return ""


def _save_last_trend(trend: str, hh_mm: str) -> None:
    """写本次 trend tag 供下次对比。trend 为空也写——避免每次空读。"""
    try:
        json.dump({"trend": trend, "hh_mm": hh_mm,
                   "ts": int(time.time())},
                  open(_LAST_TREND_PATH, "w", encoding="utf-8"))
    except Exception as e:
        log(f"{WARN} 写 last_periodic_trend 失败: {e}")


def fire_periodic(w: VolumeWatcher) -> None:
    """周期检查（每 PERIOD_SEC 秒一次）：扫结构 + 推 TG + 评估能否写/合并 journal。

    之前叫 fire_hourly（固定 3600s），liusir 觉得 1h 太久了 → 改 15min。
    触发条件：当前 ts 落在 PERIOD_SEC 整桶 + WINDOW_SEC 窗口内。
    同一桶只跑一次。
    """
    now_ts = int(time.time())
    bucket = now_ts - (now_ts % PERIOD_SEC)
    if now_ts - bucket > WINDOW_SEC:
        return
    global _last_periodic_bucket
    if _last_periodic_bucket == bucket:
        return
    _last_periodic_bucket = bucket

    hh_mm = time.strftime("%H:%M")
    log("=" * 74)
    log(f"⏰ 周期检查  t={hh_mm}  (每 {PERIOD_SEC // 60} 分钟)  开始评估结构变化")
    log("=" * 74)

    # 模拟一个"alert"给 emit_scan 用（trigger 来源 = 周期检查，不是量能/RSI）
    try:
        from gate_fetch import fetch_ticker
        tk = fetch_ticker(w.contract)[0]
        last_px = float(tk["last"])
    except Exception as e:
        log(f"{WARN} fetch_ticker 失败: {e}")
        return

    # 读上次 trend 用于卡片对比（转换信号判定）
    prev_trend = _load_last_trend()

    # 决定本次是否推 TG：只有趋势真的发生反转才推（prev 存在 + prev ≠ cur）
    # - 首次跑（prev 为空）→ 不推（避免开局噪声）
    # - 同趋势延续 → 不推（避免每 15 分钟骚扰）
    # - 反转 / 破位 / 走弱 → 推一张"转换信号"卡片
    _will_push_tg = False    # 默认抑制；scan 跑完后再确认

    a = {
        "kind": "periodic_close",
        "ts": now_ts,
        "close": last_px,
        "vol": 0,
        "ratio": 0,
        "z": 0,
        "tag": f"周期检查 @ {hh_mm}",
        "prev_trend": prev_trend,   # 注入给 format_card 做转换信号判定
    }
    out = emit_scan(w.contract, "1h", "periodic", a, w.target, w.do_scan, w.extra,
                    f"[{w.contract}] 周期检查 @ {last_px:,.1f}",
                    push_tg=_will_push_tg)

    scan_payload = (out.get("scan") or {}).get("payload") or {}

    # 持久化本次 trend，供下次对比（转换信号判定）
    try:
        cur_trend = (scan_payload.get("tags") or {}).get("trend", "")
        _save_last_trend(cur_trend, hh_mm)
    except Exception as e:
        log(f"{WARN} 持久化 last_trend 失败: {e}")

    # ★ 转换信号判定：prev 存在 且 prev ≠ cur 才补推一张 TG
    # emit_scan 已经以 push_tg=False 跑过了（避免无脑推），现在补推一张"反转"
    if prev_trend and cur_trend and prev_trend != cur_trend:
        try:
            from telegram import format_card as _fmt
            _card = _fmt("periodic", {"alert": a}, scan_payload)
            _tok = _load_tg_token()
            _chat = _load_tg_chat()
            if _tok:
                ok = push_telegram(_card, _tok, _chat)
                log(f"   TG 转换信号推送 {'成功' if ok else '失败'}  ({prev_trend} → {cur_trend})")
        except Exception as e:
            log(f"{WARN} 转换信号推送异常: {e}")
    else:
        log(f"   TG 跳过（prev={prev_trend or '∅'} cur={cur_trend or '∅'}，无反转）")

    # ★ 趋势反转检测：持仓方向与新趋势相反 → 立即 force_close
    try:
        _check_trend_reversal(w.contract, scan_payload)
    except Exception as e:
        log(f"{WARN} 趋势反转检查异常: {e}")

    # 2026-10-06：周期检查不再调度 JumpTracker
    # 原因：周期检查只是想知道趋势变了没。60s 实时抓 ticker + 推「跳空监测结束」卡片是
    # 量能异动/RSI 触发的逻辑，不该套到周期检查上——它会每 15 分钟骚扰一次 + 另起线程。
    # 量能/RSI 触发仍照常调度 JumpTracker（它们才是 trigger-as-signal 哲学该用的入口）。
    log("   [jump] 周期检查跳过 JumpTracker 调度（按 liusir 要求不推跳空检测）")


def _check_trend_reversal(contract: str, scan_payload: dict) -> None:
    """
    趋势反转检测：持仓方向 vs 新趋势方向。
    - 多头排列（trend_tag 含"多头"）但持仓是 short → 反 → 立即 force_close
    - 空头排列（trend_tag 含"空头"）但持仓是 long → 反 → 立即 force_close
    - 震荡 + 任一持仓 → 不动（震荡不算反转，规则不破坏）
    - 同方向持仓 → 不动
    """
    if not scan_payload:
        return
    tags = scan_payload.get("tags") or {}
    trend = tags.get("trend", "")
    if not trend:
        return

    # 解析新趋势方向
    if "多头" in trend:
        new_dir = "long"
    elif "空头" in trend:
        new_dir = "short"
    else:
        log(f"   趋势震荡 ({trend}) → 不触发反转检查")
        return

    # 找当前持仓
    from gate_fetch import DATA_DIR
    from position_tracker import force_close_position
    jpath = os.path.join(DATA_DIR, "journal.jsonl")
    if not os.path.exists(jpath):
        return
    try:
        recs = [json.loads(ln) for ln in open(jpath, encoding="utf-8") if ln.strip()]
    except Exception:
        return
    active = None
    for r in reversed(recs):
        if r.get("status") == "pending" and r.get("filled") is True:
            active = r
            break
    if not active:
        return

    pos_side = active.get("side")
    if pos_side == new_dir:
        log(f"   趋势 {trend} 与持仓 {pos_side} 同方向 → 不动")
        return

    # 反方向！立即平仓
    cur_px = float(scan_payload.get("last") or 0)
    log(f"🚨 趋势反转：新趋势 {trend} ({new_dir}) vs 持仓 {pos_side} → 立即强制平仓")
    res = force_close_position(
        exit_px=cur_px,
        reason=f"趋势反转：{trend}（{new_dir}）vs 持仓 {pos_side}",
        signal_id=active.get("id"),
        use_latest_filled=False,
        verbose=False,
    )
    if res:
        net = res.get("net_usd", 0)
        log(f"   ✓ 已平 id={active.get('id')} entry={res.get('entry')} → exit={res.get('exit_px')} net={net:+.2f}U")
    else:
        log(f"   ⚠️ 平仓失败：force_close 返回 None（journal 中找不到这条 active）")


def fire(w: VolumeWatcher, a: Dict) -> None:
    """
    量能异动 → 提醒 + scan 报告（不写新 journal）

    设计哲学（2026-10-05）：量能异动是「trigger」不是「signal」。
    它只该告诉用户「环境变了，请看 1h 结构」——真正的入场点位由 1h K 线收完时决定。
    写新 journal 是 1h 收盘检查的职责，不是量能异动的职责。

    last_fire 仍按 cooldown 限流，但走 mark_fired() 持久化路径，
    修复重启 watch → last_fire 归零 → 冷却失效的 bug。
    """
    now = time.time()
    if now - w.last_fire < w.cooldown:
        log(f"{WARN} 量能异动但冷却中（{w.cooldown-(now-w.last_fire):.0f}s 内已触发）→ 跳过")
        return
    w.mark_fired()           # ★ 内存 + 磁盘同时更新（修重启清零 bug）
    w.n_alert += 1
    log("=" * 74)
    log(f"🚨 量能异动  {w.contract} {w.tf}  量 {a['vol']:,.0f} 张 = 中位 {a['ratio']:.2f}× "
        f"｜ z={a['z']:.1f} ｜ {a.get('tag','')}")
    log(f"   价格 {a['close']:,.1f} ｜ 窗口内占比 {a['pct_of_window']:.1f}% "
        f"（历史中位 {a['median']:,.0f} 张）")
    log("   ⚠️ 这是「trigger」不是「signal」——提醒你看 1h K 线结构，"
        f"不主动写新 journal（入场点位等 1h K 线收完再定）")
    log("=" * 74)

    # 跑 scan 出报告 + 推 TG，但不再调度 JumpTracker 写 journal
    out = emit_scan(w.contract, w.tf, "volume", a, w.target, w.do_scan, w.extra,
                    f"[{w.contract}] 量能异动 {a['ratio']:.1f}× z={a['z']:.1f} "
                    f"{a.get('tag','')} @ {a['close']:,.1f}")

    # ★ 不再调 JumpTracker.start_or_restart()——避免「量能异动 = 写新 pending」
    # 卡片里 TG 推送由 emit_scan 内部走 format_tg_card 完成；卡片里会显示
    # 「这是提醒不是信号」，提示用户去看 1h。


# 2026-10-05：ticker 实时落盘（节流 1s 一次），供 web server 读"BTC 现价"
_TICKER_PATH = Path(DATA_DIR) / "btc_ticker.json"
_TICKER_LAST_DUMP = 0.0


def _dump_ticker(it: Dict) -> None:
    """把 ticker 关键字段写盘。节流：1s 最多写一次。"""
    global _TICKER_LAST_DUMP
    now = time.time()
    if now - _TICKER_LAST_DUMP < 1.0:
        return
    _TICKER_LAST_DUMP = now
    try:
        _TICKER_PATH.write_text(
            json.dumps({
                "ts": int(now),
                "contract": it.get("contract", "BTC_USDT"),
                "last": it.get("last"),
                "mark_price": it.get("mark_price"),
                "index_price": it.get("index_price"),
                "funding_rate": it.get("funding_rate"),
                "volume_24h_base": it.get("volume_24h_base"),
                "change_percentage": it.get("change_percentage"),
            }, ensure_ascii=False),
            encoding="utf-8",
        )
    except Exception:
        pass


def fire_rsi(rw: RsiWatcher, a: Dict) -> None:
    """RSI 越界 → emit_scan + 调度 JumpTracker 60s 跳空监测"""
    icon = "🟢" if a["hit"] == "oversold" else "🔴"
    log("=" * 74)
    log(f"{icon} RSI {a['side']}  {rw.contract} {a['tf']} RSI{a['period']} "
        f"{a['prev']:.1f} → {a['val']:.1f}  （阈值 <{a['low']:g} / >{a['high']:g}）")
    log(f"   最新价 {a['px']:,.1f}" if a.get("px") else "   最新价 —")
    log(f"   已上锁：本侧需先回到 {a['rearm']:.1f} "
        f"（滞回 {a['hyst']:g}）以内才会再次报警，防边界抖动")
    log(f"   ⚠️ 这是「提醒」不是「信号」——实测 RSI 裸用无 alpha，"
        f"触发只是让你看一眼 scan，不要照着反手做")
    log("=" * 74)
    px_txt = f" @ {a['px']:,.1f}" if a.get("px") else ""
    hook = f"[{rw.contract}] RSI{a['period']}({a['tf']}) {a['side']} {a['val']:.1f}{px_txt}"
    out = emit_scan(rw.contract, a["tf"], a["kind"], a, rw.target, rw.do_scan, rw.extra, hook)

    # 2026-10-06 恢复（liusir 要求）：RSI 越界是趋势真变的信号，调 JumpTracker 跑 60s 跳空监测
    # - 量能触发仍不调（频率太高），周期检查也不调（每 15 分钟骚扰）
    # - RSI 触发是最合适的入口：信号明确 + 频次可控 + 是真正的"环境变了"
    try:
        from .jump import JumpTracker
        _tok = _load_tg_token()
        _chat = _load_tg_chat()
        scan_payload = out.get("scan", {}).get("payload", {}) if isinstance(out, dict) else {}
        alert_price = float(a.get("px") or a.get("close") or 0)
        if alert_price > 0 and scan_payload and _tok and _chat:
            JumpTracker.instance().start_or_restart(
                alert_price=alert_price,
                alert_ts=int(time.time()),
                scan_payload=scan_payload,
                kind=f"rsi_{a['side']}",
                alert_summary=hook,
                tg_token=_tok,
                tg_chat=_chat,
                target_pts=rw.target,
            )
    except Exception as e:
        log(f"{WARN} JumpTracker 调度失败: {e}")


# ============================ 运行 ============================
def run(w: VolumeWatcher, rw: Optional[RsiWatcher] = None) -> None:
    try:
        ct = assert_usdt_linear(w.contract)
        log(f"合约校验通过：{w.contract} type={ct.get('type')} "
            f"quanto={ct.get('quanto_multiplier')}")
    except Exception as e:
        log(f"{NO} {e}")
        return
    w.prime()
    log(f"开始监控 {w.contract} @ {w.tf} ｜ 阈值 ratio≥{w.ratio_thr} 且 z≥{w.z_thr} "
        f"（或 ratio≥{w.ratio_thr*2}）｜ 冷却 {w.cooldown}s ｜ 触发→scan --target {w.target:g}")
    if rw:
        log(f"RSI 通道开启：{rw.tf} RSI{rw.period} ｜ 越界 <{rw.low:g} 或 >{rw.high:g} "
            f"（穿越触发）｜ 每 {rw.interval:g}s 检查 ｜ 两侧独立冷却 {rw.cooldown:g}s")
        rw.check()          # 先取一次基准值，避免启动瞬间误触发
    else:
        log("RSI 通道：关闭（--no-rsi）")

    def handler(ch, d):
        if ch == "futures.tickers":                 # 实时价 → 合成 RSI 当前根
            items = d if isinstance(d, list) else [d]
            for it in items:
                if isinstance(it, dict):
                    if rw:
                        rw.set_last_price(it.get("last") or it.get("mark_price"))
                    # 2026-10-05：每 1s 把 ticker 落盘，供 web server 用作"BTC 现价"
                    # 之前 web 是读 1m K 线 close，最长滞后 60s；现在用 ticker 的 last（实时）
                    _dump_ticker(it)
            return
        if ch != "futures.candlesticks":
            return
        w.n_msg += 1
        if w.n_msg == 1:
            log(f"✅ 已收到第一条 {w.tf} K 线推送（订阅成功）")
        items = d if isinstance(d, list) else [d]
        for it in items:
            if not isinstance(it, dict):
                continue
            if rw:
                rw.set_last_price(it.get("c"))
            a = w.on_candle(it)
            if a:
                fire(w, a)

    stop = threading.Event()

    def ws_loop():
        while not stop.is_set():
            try:
                ws = GateWS("usdt", on_message=handler)
                ws.subscribe([("futures.candlesticks", [w.tf, w.contract]),
                              ("futures.tickers", [w.contract])])
                w._ws = ws
                ws.run_forever()                  # GateWS.run_forever() 不接受参数
                fail = 0
            except Exception as e:
                fail += 1
                if fail <= 3 or fail % 10 == 0:   # 避免刷屏
                    log(f"{WARN} WS 异常({fail}): {e}")
            if not stop.is_set():
                time.sleep(min(5 * (1 + fail // 5), 30))

    threading.Thread(target=ws_loop, daemon=True).start()
    # Settler（持仓结算）线程
    start_settler()
    fail = 0
    hb = 0
    try:
        while True:
            time.sleep(30)
            hb += 1
            # ★ 周期检查（每 30s 心跳时探测一次，PERIOD_SEC 整桶后 WINDOW_SEC 窗口）
            try:
                fire_periodic(w)
            except Exception as e:
                log(f"{WARN} fire_periodic 异常: {e}")
            if time.time() - w.last_msg > STALE_SEC:
                log(f"{WARN} {STALE_SEC}s 无推送 → 判定连接假死，强制重连")
                try:
                    w._ws._ws.close()
                except Exception:
                    pass
                w.last_msg = time.time()
            if rw and time.time() - rw.last_check >= rw.interval:
                ra = rw.check()
                if ra:
                    fire_rsi(rw, ra)
            if hb % 4 == 0:                       # 每 2 分钟一行心跳
                tail = ""
                if rw:
                    v = rw.prev_val
                    v_txt = f"{v:.1f}" if v is not None else "—"
                    tail = (f"｜ RSI{rw.period}({rw.tf}) {v_txt}"
                            f" 已触发 {rw.n_alert} 次")
                log(f"…心跳（收到推送 {w.n_msg} 条，已触发 {w.n_alert} 次，"
                    f"窗口 {len(w.hist)} 根，中位 {w._med():,.0f} 张{tail}）")
    except KeyboardInterrupt:
        log("收到 Ctrl-C，退出")
        stop.set()


def daemonize() -> None:
    """标准双 fork 守护进程化"""
    if os.fork() > 0:
        os._exit(0)
    os.setsid()
    if os.fork() > 0:
        os._exit(0)
    sys.stdout.flush(); sys.stderr.flush()
    fd = os.open(LOG, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    os.dup2(fd, 1); os.dup2(fd, 2)
    os.close(0)


# ============================ CLI ============================
def build_argparser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(add_help=False)
    ap.add_argument("cmd", nargs="?", default="run")
    ap.add_argument("--contract", default="BTC_USDT")
    ap.add_argument("--tf", default="1m", help="监控周期，默认 1m（可选 5m/15m）")
    ap.add_argument("--window", type=int, default=120, help="历史窗口根数（默认 120）")
    ap.add_argument("--ratio", type=float, default=4.0,
                    help="量/中位数 倍数阈值（默认4.0；实测触发频率见 SKILL.md）")
    ap.add_argument("--z", type=float, default=3.5, help="z-score 阈值（默认3.5）")
    ap.add_argument("--min-vol", type=float, default=0.0, help="绝对量门槛（张），防低基数误报")
    ap.add_argument("--cooldown", type=float, default=1800.0,
                    help="触发冷却秒（默认1800=30分钟，这是主要限流阀）")
    ap.add_argument("--target", type=float, default=500.0, help="传给 scan 的目标点数")
    ap.add_argument("--no-scan", action="store_true", help="只告警，不调用 scan.py")
    ap.add_argument("--rsi-tf", default="1h",
                    help="RSI 判定周期（固定默认 1h：以 1 小时数据为准；可选 15m/4h）")
    ap.add_argument("--rsi-period", type=int, default=14, help="RSI 周期（默认 14）")
    ap.add_argument("--rsi-low", type=float, default=30.0, help="超卖阈值（默认 30）")
    ap.add_argument("--rsi-high", type=float, default=70.0, help="超买阈值（默认 70）")
    ap.add_argument("--rsi-cooldown", type=float, default=3600.0,
                    help="RSI 触发冷却秒（默认 3600；超买/超卖两侧独立计）")
    ap.add_argument("--rsi-interval", type=float, default=300.0,
                    help="RSI 检查间隔秒（默认 300）")
    ap.add_argument("--rsi-hysteresis", type=float, default=3.0,
                    help="滞回缓冲（默认 3.0）：触发后需退回阈值∓缓冲才重新武装，防边界抖动")
    ap.add_argument("--no-rsi", action="store_true", help="关闭 RSI 通道，只跑量能")
    ap.add_argument("--lev", type=float, default=None)
    ap.add_argument("--bal", type=float, default=None)
    ap.add_argument("--risk", type=float, default=None)
    return ap


def cmd_stop() -> int:
    """按 pidfile 停止守护进程"""
    if not os.path.exists(PID):
        print("没有运行中的 watch（无 pidfile）"); return 0
    pid = int(open(PID).read().strip())
    try:
        os.kill(pid, signal.SIGTERM); print(f"已停止 pid={pid}")
    except ProcessLookupError:
        print("进程已不存在")
    os.remove(PID); return 0


def cmd_status() -> int:
    """打印状态 + 最近日志"""
    # watch 进程状态
    if os.path.exists(PID):
        pid = int(open(PID).read().strip())
        alive = True
        try:
            os.kill(pid, 0)
        except OSError:
            alive = False
        print(f"watch pid={pid}  {'运行中' if alive else '已退出'}")
    else:
        print("watch 未运行")

    # Settler 健康检查（通过心跳文件判断，不只看进程活着）
    from watch.settler import is_alive as settler_is_alive
    settler = settler_is_alive(max_age_sec=30)
    if settler is True:
        print("settler: ✓ 健康（心跳新鲜）")
    elif settler is False:
        print("settler: ✗ 心跳过期（最后心跳 > 30s 前，巡检可能已死）")
    else:
        print("settler: ? 无心跳文件（未启动 / 刚启动）")

    if os.path.exists(LOG):
        print("\n最近日志：")
        print("\n".join(open(LOG, encoding="utf-8").read().strip().splitlines()[-15:]))
    return 0


def cmd_once(a, w, rw) -> int:
    """诊断模式：打印量能分位 + RSI，看会不会触发"""
    w.prime()
    c = w.cur
    probe = w.judge(c)
    med = w._med()
    print(f"\n  {a.contract} {a.tf} 量能诊断")
    print(f"  最近一根量 {c['v']:,.0f} 张 = 中位({med:,.0f}) 的 "
          f"{c['v']/med:.2f}× ｜ 收盘 {c['c']:,.1f}")
    h = sorted(w.hist)
    print(f"  窗口分位：P50={h[len(h)//2]:,.0f}  P90={h[int(len(h)*0.9)]:,.0f}  "
          f"P99={h[min(len(h)-1,int(len(h)*0.99))]:,.0f} 最大={h[-1]:,.0f} 张")
    print(f"  阈值：ratio≥{a.ratio} 且 z≥{a.z}（或 ratio≥{a.ratio*2}）｜ "
          f"min_vol={a.min_vol:g}")
    print(f"  {'🚨 会触发' if probe else '✅ 当前不会触发'} ｜ "
          f"告警存档 {ALERT_DIR} ｜ 日志 {LOG}")
    if rw:
        print(f"\n  {a.contract} {a.rsi_tf} RSI{a.rsi_period} 诊断")
        try:
            tk = fetch_ticker(a.contract)
            if isinstance(tk, list) and tk:
                rw.set_last_price(tk[0].get("last") or tk[0].get("mark_price"))
            v = rw.value()
            if v is None:
                print("  RSI 计算失败（数据不足）")
            else:
                zone = ("超卖区" if v < a.rsi_low else
                        "超买区" if v > a.rsi_high else "中性区")
                print(f"  当前 RSI = {v:.1f}（{zone}）｜ 最新价 "
                      f"{rw.last_price or c['c']:,.1f}")
                print(f"  阈值 <{a.rsi_low:g} / >{a.rsi_high:g} ｜ 穿越触发 ｜ "
                      f"滞回 {a.rsi_hysteresis:g}（回到 {a.rsi_low+a.rsi_hysteresis:g} / "
                      f"{a.rsi_high-a.rsi_hysteresis:g} 才重新武装）")
                print(f"  检查间隔 {a.rsi_interval:g}s ｜ 两侧独立冷却 {a.rsi_cooldown:g}s")
                print(f"  {'🚨 已在' if v < a.rsi_low or v > a.rsi_high else '✅ 当前不在'}"
                      f"越界区（常驻时需发生「穿越」才会报警）")
        except Exception as e:
            print(f"  RSI 诊断失败: {e}")
    print()
    return 0


def main() -> int:
    ap = build_argparser()
    a = ap.parse_args()

    extra = []
    if a.lev: extra += ["--lev", str(a.lev)]
    if a.bal: extra += ["--bal", str(a.bal)]
    if a.risk: extra += ["--risk", str(a.risk)]

    if a.cmd == "stop":
        return cmd_stop()
    if a.cmd == "status":
        return cmd_status()

    w = VolumeWatcher(a.contract, a.tf, a.window, a.ratio, a.z, a.min_vol,
                      a.cooldown, a.target, not a.no_scan, extra)
    rw = None if a.no_rsi else RsiWatcher(
        a.contract, a.rsi_tf, a.rsi_period, a.rsi_low, a.rsi_high,
        a.rsi_cooldown, a.rsi_interval, a.rsi_hysteresis,
        a.target, not a.no_scan, extra)

    if a.cmd == "once":
        return cmd_once(a, w, rw)

    if a.cmd == "start":
        if os.path.exists(PID):
            print(f"已在运行（pid={open(PID).read().strip()}），用 stop 先停"); return 1
        daemonize()
        open(PID, "w").write(str(os.getpid()))
        log(f"watch 守护进程启动 pid={os.getpid()}  {' '.join(sys.argv[1:])}")
        run(w, rw)
        return 0

    if a.cmd == "run":
        run(w, rw)
        return 0

    print(__doc__); return 0
