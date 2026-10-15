#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
BTC 策略运行状态面板（3003）
===========================

一个 0 依赖的 HTTP 服务：单文件 HTML + JSON API。
- GET /            → 页面
- GET /api/status  → 聚合状态（进程/策略/数据/BTC/胜率）

设计哲学（liusir 偏好）：
- 小而美，不冗余。一个端点、一个页面，不搞路由不搞框架。
- 浅色主题（与 PM 系统统一）
- 文案清晰、状态机明确
"""
from __future__ import annotations
import os, sys, json, time, glob, subprocess
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from typing import Optional, Tuple

# 让 scripts/ 下的模块能 import
SKILL_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SKILL_ROOT / "scripts"))
DATA_DIR = Path("/root/data")           # gate_fetch 默认
PAGE_PATH = Path(__file__).resolve().parent / "index.html"


# ============================ 数据聚合 ============================

def _read_jsonl(path: Path):
    if not path.exists():
        return []
    try:
        return [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    except Exception:
        return []


def _file_age_sec(path: Path) -> float | None:
    if not path.exists():
        return None
    return time.time() - path.stat().st_mtime


def _check_process(pid_path: Path) -> dict:
    """读 pid 文件 + 检查进程是否真活着

    Fallback: pid 文件不存在时（systemd 接管后不再写），用 pgrep 找 watch.py run。
    """
    if not pid_path.exists():
        # Fallback: pgrep -f 找 watch.py run（systemd 接管后路径唯一标识）
        try:
            out = subprocess.run(
                ["pgrep", "-f", "watch.py run"],
                capture_output=True, text=True, timeout=5,
            )
            pids = [int(p) for p in out.stdout.strip().split() if p.isdigit()]
            # 过滤掉 grep 自身（一般不会，但兜底）
            my_pid = os.getpid()
            pids = [p for p in pids if p != my_pid]
            if pids:
                # 取最小 PID（最长寿的那个）
                pid = min(pids)
                try:
                    os.kill(pid, 0)
                    return {"alive": True, "pid": pid, "source": "pgrep"}
                except ProcessLookupError:
                    pass
        except Exception:
            pass
        return {"alive": False, "reason": "no pid file + pgrep miss", "pid": None}
    try:
        pid = int(pid_path.read_text().strip())
    except Exception:
        return {"alive": False, "reason": "bad pid file", "pid": None}
    try:
        os.kill(pid, 0)   # 不真发信号，只检查可发
        return {"alive": True, "pid": pid, "source": "pidfile"}
    except ProcessLookupError:
        return {"alive": False, "reason": "process not found", "pid": pid}
    except PermissionError:
        return {"alive": True, "pid": pid, "note": "permission denied (still ok)"}


def _heartbeat_age_sec() -> float | None:
    """settler_heartbeat.json → 距上次心跳的秒数（反映 watch 线程是否活着）"""
    p = DATA_DIR / "settler_heartbeat.json"
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        ts = float(data.get("ts", 0))
        if ts <= 0:
            return None
        return time.time() - ts
    except Exception:
        return None


def _last_fire_age_sec() -> float | None:
    p = DATA_DIR / "last_fire.json"
    if not p.exists():
        return None
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        v = float(d.get("BTC_USDT", 0) or 0)
        if v <= 0:
            return None
        return time.time() - v
    except Exception:
        return None


def _btc_last() -> float | None:
    """最新 BTC 现价。优先级：
      1. btc_ticker.json（watch 实时写，ticker 的 last，每秒刷新）
      2. BTC_USDT_1m_8000.json（1m K 线 close，watch 没启动时 fallback）
    """
    tp = DATA_DIR / "btc_ticker.json"
    if tp.exists():
        try:
            d = json.loads(tp.read_text(encoding="utf-8"))
            last = d.get("last")
            ts = d.get("ts", 0)
            # ticker 超过 30s 没更新就视为 watch 没在跑，fallback
            if last and (time.time() - ts) < 30:
                return float(last)
        except Exception:
            pass
    p = DATA_DIR / "BTC_USDT_1m_8000.json"
    if not p.exists():
        return None
    try:
        arr = json.loads(p.read_text(encoding="utf-8"))
        if isinstance(arr, list) and arr:
            last = arr[-1]
            if isinstance(last, list) and len(last) >= 5:
                return float(last[4])   # close（fallback，可能滞后 60s+）
    except Exception:
        pass
    return None


def _btc_meta() -> dict:
    """附带 mark / index / funding / 24h 成交量 / 更新时间"""
    tp = DATA_DIR / "btc_ticker.json"
    if tp.exists():
        try:
            d = json.loads(tp.read_text(encoding="utf-8"))
            ts = d.get("ts", 0)
            if (time.time() - ts) < 30:
                return {
                    "mark": d.get("mark_price"),
                    "index": d.get("index_price"),
                    "funding": d.get("funding_rate"),
                    "vol_24h_base": d.get("volume_24h_base"),
                    "change_pct": d.get("change_percentage"),
                    "stale": False,
                }
        except Exception:
            pass
    return {"stale": True}


def _journal_state() -> dict:
    """最新策略状态：找最近一条"仍在持仓"的 journal

    2026-10-05 修正（语义对齐 position_tracker）：
      - status="pending" + filled=True → 已成交持仓（含 TP1/TP2 部分止盈）→ active 优先
      - status="pending" + filled=False → 挂单中 → active（若无持仓时）
      - status="settled" → 已结束
    """
    recs = _read_jsonl(DATA_DIR / "journal.jsonl")
    if not recs:
        return {"active": None, "recent_invalidated": 0, "total": 0}

    # 优先：最近一条 pending + filled=True（已成交持仓，含部分止盈）
    holding = None
    for r in reversed(recs):
        if r.get("status") == "pending" and r.get("filled") is True:
            holding = r
            break

    # 兜底：最近一条 pending（挂单中）
    pending = None
    if not holding:
        for r in reversed(recs):
            if r.get("status") == "pending":
                pending = r
                break

    invalidated = sum(1 for r in recs if r.get("status") == "invalidated")
    return {
        "active": holding or pending,
        "recent_invalidated": invalidated,
        "total": len(recs),
    }


def _journal_state_training() -> dict:
    """训练环境策略状态（2026-10-11 加）"""
    recs = _read_jsonl(Path("/root/data/training/journal.jsonl"))
    if not recs:
        return {"active": None, "recent_invalidated": 0, "total": 0}

    holding = None
    for r in reversed(recs):
        if r.get("status") == "pending" and r.get("filled") is True:
            holding = r
            break

    pending = None
    if not holding:
        for r in reversed(recs):
            if r.get("status") == "pending":
                pending = r
                break

    invalidated = sum(1 for r in recs if r.get("status") == "invalidated")
    return {
        "active": holding or pending,
        "recent_invalidated": invalidated,
        "total": len(recs),
    }


def _winrate() -> dict:
    """胜率：从 position_events.jsonl + journal 算（dedupe by id，取最新）
    规则：category == "profit" 算赢；其它算输
    """
    ev = _read_jsonl(DATA_DIR / "position_events.jsonl")
    # dedupe by id, 取最新一条（ts 最大）
    by_id: dict[str, dict] = {}
    for r in ev:
        rid = r.get("id")
        ts = r.get("ts", 0)
        if rid and ts >= by_id.get(rid, {}).get("ts", -1):
            by_id[rid] = r

    # 2026-10-05：过滤掉测试前缀（TEST-/test-）的事件，不进 winrate 统计
    # 2026-10-07 liusir 规则：category=skip（MISSED/未进场）也不进 winrate
    closed = [r for r in by_id.values()
              if not (r.get("id", "").startswith("TEST")
                      or r.get("id", "").startswith("test-"))
              and r.get("category") != "skip"]
    wins = [r for r in closed if r.get("category") == "profit"]
    losses = [r for r in closed if r.get("category") == "loss"]
    total = len(closed)
    win_n = len(wins)

    # 最近 20 笔胜率
    closed_sorted = sorted(closed, key=lambda r: r.get("ts", 0), reverse=True)[:20]
    recent_win = sum(1 for r in closed_sorted if r.get("category") == "profit")

    # 近 7 天
    week_ago = time.time() - 7 * 86400
    week = [r for r in closed if r.get("ts", 0) >= week_ago]
    week_win = sum(1 for r in week if r.get("category") == "profit")

    # 累计净盈亏
    total_pnl = sum(r.get("net_usd", 0) for r in closed)

    return {
        "total": total,
        "wins": win_n,
        "losses": total - win_n,
        "winrate_pct": round(win_n / total * 100, 1) if total else None,
        "recent_20": {"total": len(closed_sorted), "wins": recent_win,
                       "winrate_pct": round(recent_win / len(closed_sorted) * 100, 1) if closed_sorted else None},
        "last_7d": {"total": len(week), "wins": week_win,
                     "winrate_pct": round(week_win / len(week) * 100, 1) if week else None},
        "net_usd_total": round(total_pnl, 2),
        "latest_5": [
            {"id": r.get("id", "")[:35], "outcome": r.get("outcome", "?"),
             "category": r.get("category", "?"), "net_usd": round(r.get("net_usd", 0), 2),
             "ts": r.get("ts")}
            for r in sorted(closed, key=lambda r: r.get("ts", 0), reverse=True)[:5]
        ],
    }


def _is_test_id(rid: str) -> bool:
    """测试前缀判定：TEST- / test- 开头的 id 都视为测试/演示数据"""
    rid = rid or ""
    return rid.startswith("TEST") or rid.startswith("test-")


def _all_settled() -> list:
    """读 position_events.jsonl → dedupe by id（取最新一条 ts） → 返回 list.

    2026-10-11 加：双保险 — 即使 _append_event 白名单漏了，前端这一层也过滤
    掉非 BTC_USDT- 前缀的事件（防御历史 B-* / test-* 残留）。
    """
    ev = _read_jsonl(DATA_DIR / "position_events.jsonl")
    by_id: dict[str, dict] = {}
    for r in ev:
        rid = r.get("id")
        if not rid or not rid.startswith("BTC_USDT-"):
            continue   # 防御：非生产前缀的事件一律不进展示
        ts = r.get("ts", 0)
        if ts >= by_id.get(rid, {}).get("ts", -1):
            by_id[rid] = r
    return list(by_id.values())


def _outcome_label(outcome: str, pos_state: str = "") -> str:
    """把 outcome/pos_state 翻译成中文标签

    2026-10-05：用户要求展示用中文，去掉 TP2_DONE 等英文 enum。
    """
    s = pos_state or outcome or ""
    if s in ("TP1_PARTIAL",):
        return "TP1 部分止盈"
    if s in ("TP2_PARTIAL",):
        return "TP2 部分止盈"
    if s in ("TP2_DONE",):
        return "全部止盈 (TP2)"
    if s in ("TP1",):                         # 老 enum 兼容
        return "TP1 部分止盈"
    if s in ("BE_STOPPED",):
        return "保本止损"
    if s in ("SL_STOPPED", "SL", "SL*"):
        return "止损"
    if s in ("TIMEOUT",):
        return "超时平仓"
    if s in ("MCLOSE",):
        return "趋势反转平仓"
    if s in ("MISSED",):
        return "48h 未成交"
    return s or "—"


def _journal_lookup() -> dict:
    """读 journal.jsonl → 按 id 索引最新一条（用于 join 详情）"""
    recs = _read_jsonl(DATA_DIR / "journal.jsonl")
    by_id: dict[str, dict] = {}
    for r in recs:
        rid = r.get("id")
        if not rid:
            continue
        ts = int(r.get("ts") or 0)
        if ts >= int(by_id.get(rid, {}).get("ts") or 0):
            by_id[rid] = r
    return by_id


def _settlements_api(path: str) -> dict:
    """
    分页结算列表：
      GET /api/settlements?page=1&size=20&include_test=0
        page: 1-indexed（默认 1）
        size: 每页条数（默认 20，最大 100）
        include_test: 0 默认（过滤 TEST- 前缀）；1 显示全部
      返回 {items, total, page, size, has_more, included_test}
    """
    from urllib.parse import urlparse, parse_qs
    q = parse_qs(urlparse(path).query)
    page = max(1, int(q.get("page", ["1"])[0]))
    size = min(100, max(1, int(q.get("size", ["20"])[0])))
    include_test = q.get("include_test", ["0"])[0] in ("1", "true", "yes")
    include_missed = q.get("include_missed", ["0"])[0] in ("1", "true", "yes")

    all_recs = _all_settled()
    total_all = len(all_recs)
    test_n = sum(1 for r in all_recs if _is_test_id(r.get("id", "")))

    # 默认过滤：去掉 TEST 前缀；2026-10-07 liusir 规则：category=skip（MISSED/未进场）也不算结算
    if include_test:
        recs = all_recs
    else:
        recs = [r for r in all_recs if not _is_test_id(r.get("id", ""))]
    if not include_missed:
        recs = [r for r in recs if r.get("category") != "skip"]

    recs_sorted = sorted(recs, key=lambda r: r.get("ts", 0), reverse=True)
    total = len(recs_sorted)
    start = (page - 1) * size
    end = start + size
    page_items = recs_sorted[start:end]

    # 2026-10-05：join journal.jsonl 取 side/entry/pos_state，用中文 label
    jmap = _journal_lookup()

    items = []
    for r in page_items:
        rid = r.get("id", "")
        jr = jmap.get(rid, {})
        side = jr.get("side") or _side_from_id(rid)
        entry = jr.get("entry") or r.get("entry")
        exit_px = jr.get("exit_px") or r.get("exit_px")
        pos_state = jr.get("pos_state") or ""
        outcome_raw = r.get("outcome", "?")
        label = _outcome_label(outcome_raw, pos_state)
        items.append({
            "id": rid,
            "side": side,
            "side_cn": "做多" if side == "long" else ("做空" if side == "short" else "—"),
            "outcome": label,                # 中文 label（前端直接用）
            "outcome_raw": outcome_raw,      # 保留原 enum（debug 用）
            "category": r.get("category", "?"),
            "net_usd": round(r.get("net_usd", 0), 2),
            "entry": round(entry, 1) if isinstance(entry, (int, float)) else None,
            "exit_px": round(exit_px, 1) if isinstance(exit_px, (int, float)) else None,
            "exit_ts": r.get("exit_ts"),
            "ts": r.get("ts"),
            "reason": r.get("reason", ""),
        })

    return {
        "items": items,
        "total": total,
        "page": page,
        "size": size,
        "has_more": end < total,
        "included_test": include_test,
        "filtered_test_n": test_n,
        "total_all": total_all,
    }


def _settlements_api_training(path: str) -> dict:
    """训练环境结算列表（2026-10-11 加）"""
    from urllib.parse import urlparse, parse_qs
    q = parse_qs(urlparse(path).query)
    page = max(1, int(q.get("page", ["1"])[0]))
    size = min(100, max(1, int(q.get("size", ["20"])[0])))

    # 训练环境数据
    all_recs = _read_jsonl(Path("/root/data/training/position_events.jsonl"))
    total_all = len(all_recs)

    recs_sorted = sorted(all_recs, key=lambda r: r.get("ts", 0), reverse=True)
    total = len(recs_sorted)
    start = (page - 1) * size
    end = start + size
    page_items = recs_sorted[start:end]

    # join 训练 journal 取 side/entry
    jmap = {}
    for r in _read_jsonl(Path("/root/data/training/journal.jsonl")):
        if r.get("id"):
            jmap[r["id"]] = r

    items = []
    for r in page_items:
        rid = r.get("id", "")
        jr = jmap.get(rid, {})
        side = jr.get("side") or _side_from_id(rid)
        entry = jr.get("entry") or r.get("entry")
        exit_px = jr.get("exit_px") or r.get("exit_px")
        items.append({
            "id": rid,
            "side": side,
            "side_cn": "做多" if side == "long" else ("做空" if side == "short" else "—"),
            "outcome": r.get("outcome", "?"),
            "category": r.get("category", "?"),
            "net_usd": round(r.get("net_usd", 0), 2),
            "entry": round(entry, 1) if isinstance(entry, (int, float)) else None,
            "exit_px": round(exit_px, 1) if isinstance(exit_px, (int, float)) else None,
            "exit_ts": r.get("exit_ts"),
            "ts": r.get("ts"),
            # 2026-10-12 加：训练策略分类用（前端按 signal_source 聚类）
            "signal_source": jr.get("signal_source") or r.get("signal_source") or "—",
            "verdict": jr.get("verdict") or "—",
            "entry_ts": jr.get("entry_ts") or jr.get("promoted_ts") or r.get("ts"),
            "score": jr.get("score"),
        })

    return {
        "items": items,
        "total": total,
        "page": page,
        "size": size,
        "has_more": end < total,
        "total_all": total_all,
    }


def _side_from_id(rid: str) -> str:
    """fallback：id 里通常带 -long / -short 后缀"""
    if rid.endswith("-long"):
        return "long"
    if rid.endswith("-short"):
        return "short"
    return ""


def build_status() -> dict:
    pid_info = _check_process(DATA_DIR / "watch.pid")
    hb_age = _heartbeat_age_sec()
    last_fire_age = _last_fire_age_sec()
    return {
        "ts": int(time.time()),
        "ts_str": time.strftime("%Y-%m-%d %H:%M:%S"),
        "process": {
            "pid": pid_info["pid"],
            "alive": pid_info["alive"],
            "reason": pid_info.get("reason"),
            "heartbeat_age_sec": round(hb_age, 1) if hb_age is not None else None,
            "settler_ok": hb_age is not None and hb_age < 30,   # 5s 巡检，30s 内都健康
        },
        "watch_state": {
            "last_fire_age_sec": round(last_fire_age, 0) if last_fire_age is not None else None,
            "cooldown_ok": last_fire_age is None or last_fire_age >= 1800,
        },
        "btc": _btc_last(),
        "btc_meta": _btc_meta(),
        "strategy": _journal_state(),
        "winrate": _winrate(),
    }


def _wave_analysis_training() -> dict:
    """艾略特波浪分析（2026-10-12 加）：1d/4h/1h 三周期浪形 + 斐波那契位"""
    try:
        from gate_fetch import fetch_ohlcv
        from elliott_wave import analyze
        out = {}
        for tf in ("1d", "4h", "1h"):
            raw = fetch_ohlcv("BTC_USDT", tf, 200, cache=True)
            if not raw:
                continue
            ts = [b[0] for b in raw]
            hi = [b[2] for b in raw]
            lo = [b[3] for b in raw]
            cl = [b[4] for b in raw]
            out[tf] = analyze(hi, lo, ts, float(cl[-1]), tf)
        return out
    except Exception as e:
        return {"error": str(e)}


def build_status_training() -> dict:
    """训练环境状态（2026-10-11 加）"""
    # 2026-10-12 补：前端训练版 card 也走相同 render 路径，要 ts_str
    hb_age = _heartbeat_age_sec_training()
    recs = _read_jsonl(Path("/root/data/training/journal.jsonl"))
    pending_count = sum(1 for r in recs if r.get("status") != "settled")
    settled_count = sum(1 for r in recs if r.get("status") == "settled")
    # 2026-10-12 加：累计净利（settled 单 net_usd 之和，None 视为 0）
    net_usd = sum((r.get("net_usd") or 0) for r in recs if r.get("status") == "settled")
    return {
        "ts": int(time.time()),
        "ts_str": time.strftime("%Y-%m-%d %H:%M:%S"),
        "env": "training",
        "process": {
            "heartbeat_age_sec": round(hb_age, 1) if hb_age is not None else None,
            "settler_ok": hb_age is not None and hb_age < 30,
        },
        "strategy": _journal_state_training(),
        "settler_counters": {
            "pending": pending_count,
            "settled": settled_count,
            "net_usd": round(net_usd, 2),
        },
        "waves": _wave_analysis_training(),
    }


def _heartbeat_age_sec_training() -> float | None:
    """训练 settler 心跳文件 age（2026-10-12 加）"""
    p = Path("/root/data/training/settler_heartbeat.json")
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        ts = float(data.get("ts", 0))
        if ts <= 0:
            return None
        return time.time() - ts
    except Exception:
        return None


# ============================ 老师直播单 API ============================
# 2026-10-14 liusir 拍板：网页端展示 + 录入
# 数据源: /root/data/teacher_trades.jsonl

TEACHER_TRADES_PATH = Path("/root/data/teacher_trades.jsonl")


def _read_teacher_trades() -> list:
    """读所有老师直播单"""
    if not TEACHER_TRADES_PATH.exists():
        return []
    out = []
    with open(TEACHER_TRADES_PATH, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def _next_teacher_trade_id(trades: list) -> int:
    if not trades:
        return 1
    return max((t.get("trade_id") or 0) for t in trades) + 1


def _write_teacher_trades(trades: list) -> None:
    """原子写"""
    tmp = TEACHER_TRADES_PATH.with_suffix(".jsonl.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        for t in trades:
            f.write(json.dumps(t, ensure_ascii=False) + "\n")
    os.replace(tmp, TEACHER_TRADES_PATH)


def build_status_teacher() -> dict:
    """老师口头单状态聚合（前端老师单卡片用）

    老数据 result.open 可能缺失：用 net_usd != None 视为已结算兜底。
    """
    trades = _read_teacher_trades()

    def _is_open(t):
        r = t.get("result") or {}
        if r.get("open") is False:
            return False
        if r.get("net_usd") is not None:
            return False
        return True

    settled = [t for t in trades if not _is_open(t)]
    pending = [t for t in trades if _is_open(t)]
    net_usd = sum((t.get("result") or {}).get("net_usd") or 0 for t in settled)
    wins = sum(1 for t in settled if (t.get("result") or {}).get("win"))
    return {
        "ts": int(time.time()),
        "ts_str": time.strftime("%Y-%m-%d %H:%M:%S"),
        "total": len(trades),
        "settled": len(settled),
        "pending": len(pending),
        "net_usd": round(net_usd, 2),
        "win_rate": f"{wins}/{len(settled)}" if settled else "0/0",
        "trades": trades,   # 完整列表（前端表格渲染）
    }


def teacher_add_trade(payload: dict) -> tuple[int, dict]:
    """新增一笔老师直播单。
    payload 必填字段：
      date (YYYY-MM-DD HH:MM), contract, side, entry_zone,
      tp1, tp2=None, sl_initial=None, qty_pct_partial=50, size_contracts=None, notional_usd=5000
    """
    required = ["date", "contract", "side", "entry_zone", "tp1"]
    missing = [k for k in required if payload.get(k) is None or payload.get(k) == ""]
    if missing:
        return 400, {"error": f"missing fields: {missing}"}

    if payload["side"] not in ("long", "short"):
        return 400, {"error": f"side must be long/short, got {payload['side']}"}

    trades = _read_teacher_trades()
    new_id = _next_teacher_trade_id(trades)

    qty_pct_partial = int(payload.get("qty_pct_partial") or 50)
    if qty_pct_partial not in (30, 50, 70, 100):
        return 400, {"error": f"qty_pct_partial must be 30/50/70/100, got {qty_pct_partial}"}

    entry = float(payload["entry_zone"])
    tp1 = float(payload["tp1"])
    tp2 = payload.get("tp2")
    tp2_f = float(tp2) if tp2 is not None else None
    sl_initial = payload.get("sl_initial")
    sl_initial_f = float(sl_initial) if sl_initial is not None else entry  # 默认保本

    if payload["side"] == "long":
        tp1_pct = (tp1 - entry) / entry * 100
        tp2_pct = ((tp2_f - entry) / entry * 100) if tp2_f else None
        sl_pct_initial = ((sl_initial_f - entry) / entry * 100) if sl_initial_f else 0.0
    else:
        tp1_pct = (entry - tp1) / entry * 100
        tp2_pct = ((entry - tp2_f) / entry * 100) if tp2_f else None
        sl_pct_initial = ((entry - sl_initial_f) / entry * 100) if sl_initial_f else 0.0

    sig = {
        "trade_id": new_id,
        "date": payload["date"],
        "contract": payload["contract"],
        "side": payload["side"],
        "entry_zone": entry,
        "sl_initial": sl_initial_f,
        "sl_pct_initial": sl_pct_initial,
        "tp1": tp1,
        "tp1_pct": round(tp1_pct, 4),
        "tp2": tp2_f,
        "tp2_pct": round(tp2_pct, 4) if tp2_pct is not None else None,
        "rr_initial": None,
        "fill_price": entry,
        "trigger_reason": payload.get("trigger_reason") or "liusir 口头录入（网页端）",
        "size_contracts": payload.get("size_contracts"),
        "notional_usd": payload.get("notional_usd", 5000),
        "leverage": payload.get("leverage", 100),
        "events": [
            {
                "ts": payload["date"],
                "type": "entry",
                "px": entry,
                "note": payload.get("entry_note") or "liusir 直播单进场（网页录入）",
            }
        ],
        "result": {
            "net_usd": None,
            "outcome": "持仓中（已录入，待 TP1 / 打保本）",
            "notes": "",
            "open": True,
        },
        "data_quality": f"liusir {payload['date']} 口头录入；减仓比例 {qty_pct_partial}%（待 TP1 命中后回填）",
    }

    trades.append(sig)
    _write_teacher_trades(trades)
    return 200, {"trade_id": new_id, "status": "pending", "ts": int(time.time())}


def teacher_add_event(trade_id: int, payload: dict) -> tuple[int, dict]:
    """追加事件（TP1 减仓 / 打保本 / 手动平）
    payload 字段：type, px, qty_pct, note, [net_usd]
    net_usd 不传则 server 根据 events 自动算（2026-10-15 加）
    """
    ev_type = payload.get("type")
    if ev_type not in ("tp1_partial", "remaining_close", "remaining_pending"):
        return 400, {"error": f"type must be tp1_partial/remaining_close/remaining_pending, got {ev_type}"}

    trades = _read_teacher_trades()
    for t in trades:
        if t.get("trade_id") == trade_id:
            px = payload.get("px")
            if px is None:
                return 400, {"error": "px required"}
            px_f = float(px)

            t["events"].append({
                "ts": payload.get("ts") or time.strftime("%Y-%m-%d %H:%M"),
                "type": ev_type,
                "px": px_f,
                "qty_pct": payload.get("qty_pct"),
                "note": payload.get("note") or "",
            })

            # 落 result（如果是平仓事件）
            if ev_type in ("remaining_close", "tp1_partial"):
                net_usd_input = payload.get("net_usd")
                if net_usd_input is not None:
                    # 用户显式传入 → 信任（优先于自动算）
                    t["result"]["net_usd"] = float(net_usd_input)
                else:
                    # 2026-10-15 加：自动按生产公式算 PnL
                    # 生产公式：contracts = round(notional / (entry * QUANT_MULT))
                    # partial gross = (tp1_px - entry) × partial_contracts × QUANT_MULT
                    # remaining gross 视 remaining_close 价 vs entry
                    auto_net = _compute_trade_net_usd(t)
                    if auto_net is not None:
                        t["result"]["net_usd"] = round(auto_net, 2)
                if ev_type == "remaining_close":
                    t["result"]["open"] = False
                    t["result"]["outcome"] = payload.get("outcome") or "盈利（TP1 + 保本/手动平）"
                    t["result"]["notes"] = payload.get("note") or ""
                    t["result"]["win"] = (t["result"].get("net_usd") or 0) > 0
                    t["result"]["close_ts_estimate"] = payload.get("ts") or time.strftime("%Y-%m-%d %H:%M")
                else:
                    # TP1 减仓：仍持仓中，但更新 notes
                    t["result"]["outcome"] = f"持仓中（TP1 已减仓 {payload.get('qty_pct')}% + 剩余 {100 - int(payload.get('qty_pct') or 0)}% 保本）"

            _write_teacher_trades(trades)
            return 200, {"trade_id": trade_id, "status": t["result"].get("open"), "events_count": len(t["events"])}

    return 404, {"error": f"trade_id {trade_id} not found"}


# 2026-10-15 加：teacher_trades 自动算 PnL（跟生产 position_tracker 同公式）
# 常量（与 position_tracker.py 保持一致）
_TEACHER_QUANT_MULT = 0.0001        # 1 张 = 0.0001 BTC
_TEACHER_FEE_USD = 4.85             # taker 往返收费


def _compute_trade_net_usd(t: dict) -> Optional[float]:
    """
    根据 trade 完整 events 数组算 net_usd：
      contracts = round(notional / (entry * QUANT_MULT))
      partial gross = (tp1_px - entry) × partial_contracts × QUANT_MULT
      remaining gross = (remaining_px - entry) × remaining_contracts × QUANT_MULT
      fee = 4.85U（全程一笔，无论 tp1 partial + remaining close）
      net = total_gross - fee

    长/短方向：用 trade.side。
    return None 当 events 不完整（缺 entry 或两个减仓事件都没）。
    """
    side = t.get("side")
    entry = t.get("fill_price") or t.get("entry_zone")
    notional = t.get("notional_usd") or 5000
    if not entry or not side or side not in ("long", "short"):
        return None
    # 区间成交（如 '82400-82600'）或非数值 → 自动算不出来，跳过返回 None 由用户手填
    if not isinstance(entry, (int, float)):
        return None
    if not isinstance(notional, (int, float)):
        return None

    evs = t.get("events") or []
    if not evs:
        return None

    contracts = round(notional / (entry * _TEACHER_QUANT_MULT))

    # 平仓事件类型（2026-10-15 起包含历史别名：sl_breakeven = remaining_close, tp2_full = remaining_close）
    _CLOSE_TYPES = {"tp1_partial", "remaining_close", "remaining_pending", "sl_breakeven", "tp2_full"}

    total_gross = 0.0
    consumed_pct = 0
    for ev in evs:
        et = ev.get("type")
        ep = ev.get("px")
        qp = ev.get("qty_pct")
        if et == "entry":
            continue
        if ep is None:
            continue
        # 旧数据可能没 qty_pct，按 consumed_pct 推断
        if qp is None:
            qp = 100 - consumed_pct
        qty_pct = int(qp)
        # 防御：单仓位合约只能分一次。同一 type 重复触发只算第一条
        # 比如 remaining_pending + remaining_close 都是 same 仓位 (30%)，
        # 那只看首次出现
        if et in _CLOSE_TYPES:
            ev_contracts = contracts * qty_pct / 100.0
            if side == "long":
                leg_gross = (ep - entry) * ev_contracts * _TEACHER_QUANT_MULT
            else:
                leg_gross = (entry - ep) * ev_contracts * _TEACHER_QUANT_MULT
            # 防 trade 7 模式：remaining_pending 不计 gross（持仓还没出，仅注记 pending → 仓位已被 tp1 partial 扣完）
            if et == "remaining_pending":
                consumed_pct += qty_pct  # 仅更新"已被分配"，不算 gross
                continue
            total_gross += leg_gross
            consumed_pct += qty_pct

    if consumed_pct == 0:
        return None

    net = total_gross - _TEACHER_FEE_USD
    return round(net, 2)


# ============================ HTTP ============================

class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass    # 静音 access log（避免 watch.log 噪声）

    def do_GET(self):
        if self.path == "/" or self.path.startswith("/index"):
            try:
                body = PAGE_PATH.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)
            except Exception as e:
                self.send_error(500, str(e))
            return
        if self.path == "/api/status":
            try:
                payload = build_status()
                body = json.dumps(payload, ensure_ascii=False).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(body)
            except Exception as e:
                self.send_error(500, str(e))
            return
        if self.path == "/api/status/training":
            try:
                payload = build_status_training()
                body = json.dumps(payload, ensure_ascii=False).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)
            except Exception as e:
                self.send_error(500, str(e))
            return
        if self.path.startswith("/api/settlements/training"):
            try:
                payload = _settlements_api_training(self.path)
                body = json.dumps(payload, ensure_ascii=False).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)
            except Exception as e:
                self.send_error(500, str(e))
            return
        if self.path.startswith("/api/settlements"):
            try:
                payload = _settlements_api(self.path)
                body = json.dumps(payload, ensure_ascii=False).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(body)
            except Exception as e:
                self.send_error(500, str(e))
            return
        # ===== 老师直播单：GET 列表 =====
        if self.path == "/api/teacher/trades":
            try:
                payload = build_status_teacher()
                body = json.dumps(payload, ensure_ascii=False).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(body)
            except Exception as e:
                self.send_error(500, str(e))
            return
        self.send_error(404, "not found")

    def do_POST(self):
        """老师直播单录入 / 追加事件"""
        if self.path == "/api/teacher/trades":
            try:
                length = int(self.headers.get("Content-Length", 0))
                raw = self.rfile.read(length) if length > 0 else b""
                payload = json.loads(raw.decode("utf-8")) if raw else {}
                status, body = teacher_add_trade(payload)
                body_bytes = json.dumps(body, ensure_ascii=False).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(body_bytes)
            except json.JSONDecodeError as e:
                self.send_error(400, f"invalid JSON: {e}")
            except Exception as e:
                self.send_error(500, str(e))
            return

        # /api/teacher/trades/<id>/events
        if self.path.startswith("/api/teacher/trades/") and self.path.endswith("/events"):
            try:
                # 解析 trade_id
                # /api/teacher/trades/123/events
                parts = self.path.strip("/").split("/")
                # ['api', 'teacher', 'trades', '123', 'events']
                if len(parts) != 5:
                    self.send_error(400, "bad path")
                    return
                trade_id = int(parts[3])
                length = int(self.headers.get("Content-Length", 0))
                raw = self.rfile.read(length) if length > 0 else b""
                payload = json.loads(raw.decode("utf-8")) if raw else {}
                status, body = teacher_add_event(trade_id, payload)
                body_bytes = json.dumps(body, ensure_ascii=False).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(body_bytes)
            except ValueError as e:
                self.send_error(400, f"bad trade_id: {e}")
            except json.JSONDecodeError as e:
                self.send_error(400, f"invalid JSON: {e}")
            except Exception as e:
                self.send_error(500, str(e))
            return

        self.send_error(404, "not found")

    def do_OPTIONS(self):
        """CORS 预检（前端 fetch 用）"""
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()


def main(host: str = "0.0.0.0", port: int = 3003):
    print(f"[btc-status] 监听 http://{host}:{port}")
    print(f"[btc-status] DATA_DIR = {DATA_DIR}")
    print(f"[btc-status] PAGE = {PAGE_PATH}")
    httpd = HTTPServer((host, port), Handler)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("[btc-status] 退出")


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=3003)
    a = ap.parse_args()
    main(a.host, a.port)