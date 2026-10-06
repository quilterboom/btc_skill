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
import os, sys, json, time, glob
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path

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
    """读 pid 文件 + 检查进程是否真活着"""
    if not pid_path.exists():
        return {"alive": False, "reason": "no pid file", "pid": None}
    try:
        pid = int(pid_path.read_text().strip())
    except Exception:
        return {"alive": False, "reason": "bad pid file", "pid": None}
    try:
        os.kill(pid, 0)   # 不真发信号，只检查可发
        return {"alive": True, "pid": pid}
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
    closed = [r for r in by_id.values()
              if not (r.get("id", "").startswith("TEST")
                      or r.get("id", "").startswith("test-"))]
    wins = [r for r in closed if r.get("category") == "profit"]
    losses = [r for r in closed if r.get("category") != "profit"]
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
    """读 position_events.jsonl → dedupe by id（取最新一条 ts） → 返回 list"""
    ev = _read_jsonl(DATA_DIR / "position_events.jsonl")
    by_id: dict[str, dict] = {}
    for r in ev:
        rid = r.get("id")
        ts = r.get("ts", 0)
        if rid and ts >= by_id.get(rid, {}).get("ts", -1):
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

    all_recs = _all_settled()
    total_all = len(all_recs)
    test_n = sum(1 for r in all_recs if _is_test_id(r.get("id", "")))

    # 默认过滤
    if include_test:
        recs = all_recs
    else:
        recs = [r for r in all_recs if not _is_test_id(r.get("id", ""))]

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
        self.send_error(404, "not found")


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