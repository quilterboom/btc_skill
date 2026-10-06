#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
JumpTracker — 1h K 线收盘 + 量能异动后的结构检查与点位移档
===========================================================

触发条件（任一）：
  ① 1h K 线刚刚收盘（每整点 → 自动检查结构变化）
  ② 1m 量能异动（已废弃直接触发，改由 fire() 上层传过来）
监测窗口：60 秒内跳空监测（≥1000 点 → 策略作废）

核心修复（2026-10-05 — liusir 交易哲学）：
  ① 量能异动 → 不再主动写新 journal，只出 TG 卡片
  ② 1h 收盘时 → 在已有 pending 上**合并/刷新**点位，不是新建
  ③ 强制准入守卫：1h K 线未收完 / 距离 < 500 点 → 完全跳过
  ④ 卡片永远推送（即使准入规则跳过写 journal），但内容上明确"trigger vs signal"
"""
from __future__ import annotations
import os, sys, json, time, threading
from pathlib import Path
from typing import Dict, Optional

from gate_fetch import DATA_DIR, fetch_ticker
from telegram import push as push_telegram
import config

from .common import log
from .journal_ops import invalidate_other_pending


class JumpTracker:
    _instance = None
    _lock = threading.Lock()

    JUMP_THRESHOLD = 1000.0      # 点
    WINDOW_SEC = 60              # 60 秒窗口（不是 180，注释里历史错写了）
    INTERVAL = 1                 # 每 1 秒抓一次 ticker
    _stop_flag = None            # 当前任务的中止信号（threading.Event）

    @classmethod
    def instance(cls):
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def start_or_restart(self, alert_price: float, alert_ts: float,
                         scan_payload: Dict, kind: str,
                         alert_summary: str, tg_token: str, tg_chat: str,
                         target_pts: float = 1000.0) -> None:
        """
        调度一个新监测任务。如果已有，强制停止旧的并重启。
        返回：None。推送 TG 在 run() 内部完成。
        target_pts：当前 watch 启动时传的 --target，用于 jump 写 journal 时重算 TP1/TP2。
        """
        with self._lock:
            # 中止旧任务
            if JumpTracker._stop_flag is not None:
                JumpTracker._stop_flag.set()
                log("[jump] 重置窗口（旧任务收到中止信号）")
            # 新任务的 stop event
            stop = threading.Event()
            JumpTracker._stop_flag = stop

            t = threading.Thread(
                target=self._run,
                args=(alert_price, alert_ts, scan_payload, kind,
                      alert_summary, tg_token, tg_chat, stop, target_pts),
                daemon=True,
                name="JumpTracker"
            )
            t.start()
            log(f"[jump] 启动监测：alert_price={alert_price:,.1f} window={self.WINDOW_SEC}s target={target_pts:g}")

    def _run(self, alert_price, alert_ts, scan_payload, kind,
             alert_summary, tg_token, tg_chat, stop: threading.Event,
             target_pts: float = 1000.0) -> None:
        """
        后台线程：WINDOW_SEC 秒内每 INTERVAL 秒抓 ticker，结束后评估。
        target_pts：当前 watch 启动时传的 --target，写 journal 时用来重算 TP1/TP2。
        """
        end_ts = alert_ts + self.WINDOW_SEC
        max_p = alert_price
        min_p = alert_price
        max_t = alert_ts
        min_t = alert_ts
        samples = [(alert_ts, alert_price)]
        invalidated = False
        invalidated_at = None
        invalidated_dir = None  # 'up' / 'down'

        # 第一次立即抓（不等 5s）
        try:
            tk = fetch_ticker("BTC_USDT")
            last = float(tk[0]["last"])
            samples.append((time.time(), last))
            max_p, min_p = max(max_p, last), min(min_p, last)
        except Exception as e:
            log(f"[jump] 首次抓取失败: {e}")

        while time.time() < end_ts:
            if stop.is_set():
                log("[jump] 任务中止，退出")
                return
            time.sleep(self.INTERVAL)
            try:
                tk = fetch_ticker("BTC_USDT")
                last = float(tk[0]["last"])
                ts = time.time()
                samples.append((ts, last))
                if last > max_p:
                    max_p, max_t = last, ts
                if last < min_p:
                    min_p, min_t = last, ts
                # 提前判断：任意单点 vs alert_price ≥ 1000 → 立即作废
                jump_up = last - alert_price
                jump_down = alert_price - last
                if max(jump_up, jump_down) >= self.JUMP_THRESHOLD and not invalidated:
                    invalidated = True
                    invalidated_at = ts
                    invalidated_dir = "up" if jump_up > jump_down else "down"
                    log(f"[jump] 跳空≥{self.JUMP_THRESHOLD:.0f} → "
                        f"方向 {invalidated_dir.upper()} last={last:,.1f}")
                    # 提前作废：再观察 30s 确认（避免假突破）
                    end_ts = min(end_ts, ts + 30)
            except Exception as e:
                log(f"[jump] 抓取失败: {e}")

        # === 窗口结束：评估 + 推送 ===
        jump_up = max_p - alert_price
        jump_down = alert_price - min_p
        jump_up_max = max_p - alert_price if jump_up > 0 else 0
        jump_down_max = alert_price - min_p if jump_down > 0 else 0

        # 失效判定：跳空 ≥ 1000（提前作废情况 OR 窗口结束时仍然 1000+）
        if max(jump_up, jump_down) >= self.JUMP_THRESHOLD:
            invalidated = True
            if invalidated_dir is None:
                invalidated_dir = "up" if jump_up > jump_down else "down"

        side = (scan_payload.get("side") or scan_payload.get("main_side") or "?")
        verdict = scan_payload.get("verdict") or "无法解析"

        # ===== 作废卡片（先构造，无论准入规则如何都要推）=====
        if invalidated:
            card = (
                f"🚨 <b>BTC 策略作废</b>  跳空 {self.JUMP_THRESHOLD:.0f}+ 点\n"
                f"触发价 <b>{alert_price:,.1f}</b> ｜ 监测 {self.WINDOW_SEC}s（{len(samples)} 次抓取）\n"
                f"📈 期间最高 <b>{max_p:,.1f}</b>（{jump_up_max:+.0f} 点）｜ "
                f"📉 期间最低 <b>{min_p:,.1f}</b>（-{jump_down_max:.0f} 点）\n"
                f"方向 <b>{invalidated_dir.upper()}</b> ｜ 作废 @ "
                f"<b>{samples[-1][1]:,.1f}</b>\n"
                f"\n原策略（{('做多' if side=='long' else '做空')}）：\n"
                f"  判定：<b>{verdict}</b>｜做多 {scan_payload.get('score_long','?')}/6 "
                f"做空 {scan_payload.get('score_short','?')}/6\n"
            )
            pl = (scan_payload.get("plan") or {})
            if pl:
                card += (
                    f"  入场 <b>{pl.get('entry_limit', pl.get('entry', 0)):,.1f}</b> ｜ "
                    f"止损 <b>{pl.get('stop', pl.get('sl', 0)):,.1f}</b> ｜ "
                    f"TP {pl.get('tp1', 0):,.1f} / {pl.get('tp2', 0):,.1f}\n"
                )
            jump_actual = jump_up if invalidated_dir == "up" else jump_down
            card += (
                f"\n⚠️ 环境剧变（{invalidated_dir.upper()} {jump_actual:.0f} 点），"
                f"原策略已 <b>作废</b>\n"
                f"建议：等待下次量能异动 / RSI 越界 / 重新评估方向"
            )
            # ★ 作废分支不写 journal
        else:
            # ===== 保留卡片 + 写 journal =====
            card = (
                f"✅ <b>BTC 跳空监测结束</b>  价格未大幅波动\n"
                f"触发价 <b>{alert_price:,.1f}</b> ｜ 监测 {self.WINDOW_SEC}s（{len(samples)} 次抓取）\n"
                f"📈 期间最高 <b>{max_p:,.1f}</b>（+{jump_up_max:.0f} 点）｜ "
                f"📉 期间最低 <b>{min_p:,.1f}</b>（-{jump_down_max:.0f} 点）\n"
                f"\n原策略（{('做多' if side=='long' else '做空')}）：判定 {verdict}\n"
                f"原策略继续有效 → 已写/合并 journal pending"
            )
            # 写 journal pending（让 position_tracker 5s 内跟踪）
            _write_journal_ok = True
            _skip_reason = None
            try:
                pl = scan_payload.get("plan") or {}
                _jpath = Path(DATA_DIR) / "journal.jsonl"
                _new_entry = float(pl.get("entry_limit", 0))

                # 规则 3：已进场？找最近一条 active
                _active = None
                if _jpath.exists():
                    _recs = [json.loads(ln.strip()) for ln in open(_jpath, encoding="utf-8").readlines() if ln.strip()]
                    for _r in reversed(_recs):
                        if _r.get("status") in ("pending", "filled"):
                            _active = _r
                            break

                if _active is not None and _active.get("filled") is True:
                    _active_side = _active.get("side")
                    _active_id = _active.get("id")
                    if _active_side == side:
                        # ===== 场景 A：同方向已进场 → 复用数据，不写新 =====
                        _skip_reason = (f"同方向已进场（id={_active_id}，entry={_active.get('entry'):.1f}），"
                                        f"不复写入场")
                        _write_journal_ok = False
                        # 卡片补充：显示已进场的 TP/SL/实时价距 TP1/SL
                        card += (
                            f"\n\n📌 <b>已进场（同方向）</b>\n"
                            f"  id: <code>{_active_id[:35]}</code>\n"
                            f"  入场 <b>{_active.get('entry'):.1f}</b> ｜ "
                            f"TP1 <b>{_active.get('tp1'):.1f}</b> / "
                            f"TP2 <b>{_active.get('tp2'):.1f}</b> ｜ "
                            f"SL <b>{_active.get('sl'):.1f}</b>\n"
                            f"  当前价 <b>{samples[-1][1]:,.1f}</b> "
                            f"（距 TP1 <b>{_active.get('tp1') - samples[-1][1]:+.0f}</b> 点 / "
                            f"距 SL <b>{samples[-1][1] - _active.get('sl'):+.0f}</b> 点）"
                        )
                    else:
                        # ===== 场景 B：反方向已进场 → 自动平仓 + 写反方向 journal =====
                        _active_side_cn = "多" if _active_side == "long" else "空"
                        _new_side_cn = "多" if side == "long" else "空"
                        # 1. 立即调 force_close 平掉已进场单
                        try:
                            from position_tracker import force_close_position
                            _exit_px = float(samples[-1][1])
                            _close_res = force_close_position(
                                exit_px=_exit_px,
                                reason=f"反方向信号触发（{_active_side_cn} → {_new_side_cn}），自动平仓",
                                signal_id=_active_id,
                            )
                            if _close_res is not None:
                                _net_usd = _close_res.get("net_usd", 0)
                                log(f"[jump] 反方向：自动平仓 id={_active_id} net={_net_usd:+.2f}U")
                                _close_card = (
                                    f"🔄 <b>自动平仓（{_active_side_cn}→{_new_side_cn}反转）</b>\n"
                                    f"  id: <code>{_active_id[:35]}</code>\n"
                                    f"  平仓价 <b>{_exit_px:,.1f}</b>（市价）｜ "
                                    f"净 <b>{_net_usd:+.2f}U</b>\n"
                                    f"  原因：反方向信号触发\n\n"
                                )
                            else:
                                _close_card = "⚠️ 自动平仓失败（force_close 返回 None）\n\n"
                        except Exception as e:
                            log(f"[jump] 自动平仓异常: {e}")
                            _close_card = f"⚠️ 自动平仓异常: {e}\n\n"

                        # 2. 反方向策略不受 500 点限制，直接写 journal
                        _keep_id = f"BTC_USDT-{int(time.time())}-{side}-JUMP-REVERSE"
                        n_inv = invalidate_other_pending(_keep_id, "被反方向策略覆盖")
                        if n_inv:
                            log(f"[jump] 反方向：作废 {n_inv} 条旧 pending，新 id={_keep_id}")
                        # 复用下面的写入逻辑
                        _entry = float(pl.get("entry_limit", pl.get("entry", 0)))
                        _sl = float(pl.get("stop", pl.get("sl", 0)))
                        _target = float(target_pts)
                        if side == "long":
                            _tp1 = _entry + 0.40 * _target
                            _tp2 = _entry + 0.60 * _target
                        else:
                            _tp1 = _entry - 0.40 * _target
                            _tp2 = _entry - 0.60 * _target
                        _tp1_pct = abs(_tp1 / _entry - 1) * 100
                        rec = {
                            "contract": scan_payload.get("contract", "BTC_USDT"),
                            "ts": int(time.time()),
                            "last": samples[-1][1],
                            "side": side,
                            "score": scan_payload.get("score_long" if side=="long" else "score_short", 0),
                            "core_ok": "成立" in verdict,
                            "verdict": verdict,
                            "entry": pl.get("entry_limit", pl.get("entry", 0)),
                            "sl": round(_sl, 1),
                            "tp1": round(_tp1, 1),
                            "tp2": round(_tp2, 1),
                            "tp1_pct": round(_tp1_pct, 4),
                            "sl_pct": round(abs(_sl / _entry - 1) * 100, 4),
                            "target_pts": _target,
                            "status": "pending",
                            "tags": {},
                            "meta": {"lev": 100.0, "bal": 1000.0, "risk": 0.01, "mode": "cross"},
                            "id": _keep_id,
                            "kind": "jump_post_volume_reverse",   # 标记反方向
                            "alert_price": alert_price,
                            "max_price": max_p,
                            "min_price": min_p,
                            "auto_closed_id": _active_id,         # 记录被平的旧 id
                        }
                        with open(_jpath, "a", encoding="utf-8") as _f:
                            _f.write(json.dumps(rec, ensure_ascii=False) + "\n")
                        log(f"[jump] 反方向 journal 已写 id={rec['id']} entry={_entry:.1f} TP1={_tp1:.1f} TP2={_tp2:.1f}")

                        _write_journal_ok = False    # 上面已直接写
                        _skip_reason = f"反方向已自动平仓 + 写新 journal id={_keep_id}"

                        # 卡片前缀：先放平仓，再放新策略
                        card = _close_card + card
                        card += (
                            f"\n\n🆕 <b>反方向策略（新）</b> 不受 500 点限制\n"
                            f"  id: <code>{_keep_id[:35]}</code>\n"
                            f"  入场 <b>{_entry:.1f}</b> ｜ "
                            f"TP1 <b>{_tp1:.1f}</b> / TP2 <b>{_tp2:.1f}</b> ｜ "
                            f"SL <b>{_sl:.1f}</b>"
                        )
                else:
                    # 规则 1：同方向 entry 距离 ≥ 500 点
                    _contract = scan_payload.get("contract", "BTC_USDT")
                    _skip, _reason = config.should_skip_new_signal(
                        _contract, side, _new_entry, journal_path=str(_jpath))
                    if _skip:
                        _skip_reason = _reason
                        _write_journal_ok = False

                    # ★ 规则 0（2026-10-05 新增）：强制准入守卫 ——
                    #   量能异动 ≠ signal，仅 trigger。
                    #   只有当 1h K 线刚收完时（且结构方向与新策略一致）才允许写新 journal；
                    #   否则即使准入规则通过，也拒绝写新单，避免频繁生成入场点位。
                    #   例外：scan_payload["force_journal"]=True → 来自 1h 收盘检查本身，放行。
                    _now_ts = int(time.time())
                    _h_aligned = _now_ts % 3600 < 90      # 整点 90s 窗口（给 JumpTracker 60s 监测留余量）
                    if not _h_aligned and not scan_payload.get("force_journal"):
                        _skip_reason = (f"非 1h 收盘窗口（距整点 {_now_ts % 3600}s）"
                                       f"→ 量能异动只 trigger，不写新 journal")
                        _write_journal_ok = False

                if _write_journal_ok:
                    # ★ 合并而非新建（2026-10-05 liusir 交易哲学）：
                    #   已有 pending → 在它上面刷新 entry/SL/TP（结构变了 → 调点）
                    #   已有 filled  → 同方向已进场，本就跳过（场景 A 上面已处理）
                    #   没有任何活跃 → 这次允许新建（兜底：1h 收完 + 距离 ≥ 500）
                    _existing = None
                    if _active is None:
                        _keep_id = f"BTC_USDT-{int(time.time())}-{side}-JUMP"
                        n_inv = invalidate_other_pending(_keep_id, "被新策略覆盖")
                        if n_inv:
                            log(f"[jump] 作废 {n_inv} 条旧 pending，新 id={_keep_id}")
                    else:
                        _existing = _active
                        _keep_id = _active.get("id")
                        n_inv = invalidate_other_pending(_keep_id, "被新策略覆盖")
                        if n_inv:
                            log(f"[jump] 作废 {n_inv} 条旧 pending，保留 id={_keep_id}")

                    if _existing:
                        _existing["jump_result"] = {
                            "alert_price": float(alert_price),
                            "max_price": float(max_p),
                            "min_price": float(min_p),
                            "jump_up": float(jump_up_max),
                            "jump_down": float(jump_down_max),
                            "invalidated": bool(invalidated),
                            "invalidated_dir": invalidated_dir,
                            "samples": len(samples),
                            "settled_ts": int(time.time()),
                        }
                        try:
                            _all = open(_jpath, encoding="utf-8").readlines()
                            with open(_jpath, "w", encoding="utf-8") as _f:
                                for _ln in _all:
                                    try:
                                        _r = json.loads(_ln.strip())
                                        if _r.get("id") == _existing.get("id"):
                                            _f.write(json.dumps(_existing, ensure_ascii=False) + "\n")
                                        else:
                                            _f.write(_ln if _ln.endswith("\n") else _ln + "\n")
                                    except Exception:
                                        _f.write(_ln if _ln.endswith("\n") else _ln + "\n")
                            log(f"[jump] 复用现有 journal id={_existing.get('id')}，已合并60s结果")
                        except Exception as e:
                            log(f"[jump] 合并 journal 失败: {e}")
                    else:
                        # 用当前 target 重算 TP1/TP2（不用 scan payload 旧值）
                        _entry = float(pl.get("entry_limit", pl.get("entry", 0)))
                        _sl = float(pl.get("stop", pl.get("sl", 0)))
                        _target = float(target_pts)
                        if side == "long":
                            _tp1 = _entry + 0.40 * _target
                            _tp2 = _entry + 0.60 * _target
                        else:
                            _tp1 = _entry - 0.40 * _target
                            _tp2 = _entry - 0.60 * _target
                        _tp1_pct = abs(_tp1 / _entry - 1) * 100
                        rec = {
                            "contract": scan_payload.get("contract", "BTC_USDT"),
                            "ts": int(time.time()),
                            "last": samples[-1][1],
                            "side": side,
                            "score": scan_payload.get("score_long" if side=="long" else "score_short", 0),
                            "core_ok": "成立" in verdict,
                            "verdict": verdict,
                            "entry": pl.get("entry_limit", pl.get("entry", 0)),
                            "sl": pl.get("stop", pl.get("sl", 0)),
                            "tp1": round(_tp1, 1),
                            "tp2": round(_tp2, 1),
                            "tp1_pct": round(_tp1_pct, 4),
                            "sl_pct": round(abs(_sl / _entry - 1) * 100, 4),
                            "target_pts": _target,
                            "status": "pending",
                            "tags": {},
                            "meta": {"lev": 100.0, "bal": 1000.0, "risk": 0.01, "mode": "cross"},
                            "id": _keep_id,
                            "kind": "jump_post_volume",
                            "alert_price": alert_price,
                            "max_price": max_p,
                            "min_price": min_p,
                        }
                        with open(_jpath, "a", encoding="utf-8") as _f:
                            _f.write(json.dumps(rec, ensure_ascii=False) + "\n")
                        log(f"[jump] journal 已写 id={rec['id']} target={_target:g} entry={_entry:.1f} TP1={_tp1:.1f} TP2={_tp2:.1f}")
                else:
                    log(f"[jump] ⏸ 跳过写 journal：{_skip_reason}")
                    # 卡片补充说明：被准入规则跳过，但仍告知用户监测已结束
                    card += f"\n\n⏸ 准入规则：{_skip_reason}（journal 未写入）"
            except Exception as e:
                log(f"[jump] 写 journal 失败: {e}")

        # 推送（无论准入规则是否跳过，都必须推卡片）
        if tg_token:
            try:
                ok = push_telegram(card, tg_token, tg_chat)
                log(f"[jump] TG 推送 {'成功' if ok else '失败'}")
            except Exception as e:
                log(f"[jump] TG 推送异常: {e}")
