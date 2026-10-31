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
import os, re, sys, json, time, glob, subprocess
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from typing import Optional, Tuple

# 让 scripts/ 下的模块能 import
SKILL_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SKILL_ROOT / "scripts"))
DATA_DIR = Path("/root/data")           # gate_fetch 默认
PAGE_PATH = Path(__file__).resolve().parent / "index.html"
MARKET_PAGE_PATH = Path(__file__).resolve().parent / "market.html"


# ============================ 老师单分析（IMA 当数据库） ============================
# 2026-10-28 liusir：「你就把 ima 当做数据库去存数据就行」
# 数据底座在 IMA KB（scripts/teacher_kb_store.py 写，脚本 teacher_analysis.py 算）。
# 网页读 IMA 而不是直接读本地 jsonl —— 这样网页看到的永远是「落过盘的那份」，
# 跟 liusir 在 IMA 里翻到的内容一致。
_TA_CACHE: dict = {"ts": 0.0, "data": None, "err": None}
_TA_CACHE_TTL = 300.0   # 5min：IMA 读一次要两次 HTTP（list + media_info + 抓正文）


def build_teacher_analysis_api() -> dict:
    """读 IMA 里的最新老师单分析。

    失败不抛：返回 {error, stale} —— 前端能显示「IMA 读取失败」而不是整页崩。
    IMA 挂了时退回本地实时计算（标注 source=local_fallback），保证卡片不空。
    """
    now = time.time()
    if _TA_CACHE["data"] and now - _TA_CACHE["ts"] < _TA_CACHE_TTL:
        return _TA_CACHE["data"]
    try:
        from teacher_kb_store import read_latest_analysis
        data = read_latest_analysis()
        if not data:
            raise RuntimeError("IMA KB 里没有老师单分析文档")
        data["source"] = "ima"
        data["stale"] = False
    except Exception as e:
        # IMA 不可用 → 本地兜底（数据仍是真实的，只是没落 IMA）
        try:
            from teacher_analysis import build_teacher_analysis as _local
            data = _local()
            data["source"] = "local_fallback"
            data["stale"] = True
            data["error"] = f"IMA 读取失败，已用本地实时计算兜底：{e}"
        except Exception as e2:
            return {"error": f"老师单分析不可用（IMA: {e}；本地: {e2}）"}
    _TA_CACHE["ts"], _TA_CACHE["data"] = now, data
    return data


# ============================ 行情剧震检测（事件触发分析） ============================
# 2026-10-28 liusir：行情更新不能只靠固定 15min 刷新，
#   「出现行情剧烈波动的时候去分析，或者出现大幅下跌的时候去分析」。
# 阈值来自近 30 天真实 15m K 线分位数（scripts 里可复算）：
#   15m 单根跌幅 p1% = -0.485% → 取 -0.5%（近 30 天 0.8 次/天）
#   1h  累计跌幅 p1% = -0.984% → 取 -1.0%（近 30 天 0.9 次/天）
#   15m 单根涨幅 p99% = +0.532% → 取 +0.5%
# 也就是说：每天大概 1~2 次会触发，不会刷屏。
SHOCK_CACHE: dict = {"ts": 0.0, "data": None}
SHOCK_TTL = 60.0
SHOCK_15M_DOWN = -0.5      # %
SHOCK_15M_UP = 0.5        # %
SHOCK_1H_DOWN = -1.0      # %


def build_shock_state() -> dict:
    """检测最近一段行情是否发生「剧烈波动 / 大幅下跌」。

    返回 {shock: bool, reasons: [...], drops: {...}, last_shock_ts}
    shock 只在「刚刚发生」的那一段窗口内为真（默认 15min），避免一直亮着。
    """
    now = time.time()
    if SHOCK_CACHE["data"] and now - SHOCK_CACHE["ts"] < SHOCK_TTL:
        return SHOCK_CACHE["data"]

    out = {"shock": False, "reasons": [], "drops": {}, "checked_ts": int(now),
           "last_shock_ts": None, "thresholds": {
               "15m_down": SHOCK_15M_DOWN, "15m_up": SHOCK_15M_UP, "1h_down": SHOCK_1H_DOWN}}

    try:
        from teacher_analysis import _bars
        b15 = _bars("15m")
        if len(b15) >= 10:
            closes = [float(b[4]) for b in b15]
            # 最新一根收盘 vs 上一根
            d15 = (closes[-1] - closes[-2]) / closes[-2] * 100
            # 最近 4 根（1h）
            d1h = (closes[-1] - closes[-5]) / closes[-5] * 100
            out["drops"] = {"m15_pct": round(d15, 3), "h1_pct": round(d1h, 3),
                            "last_px": closes[-1]}
            reasons = []
            if d15 <= SHOCK_15M_DOWN:
                reasons.append(f"15m 大跌 {d15:.2f}%")
            if d15 >= SHOCK_15M_UP:
                reasons.append(f"15m 急涨 {d15:.2f}%")
            if d1h <= SHOCK_1H_DOWN:
                reasons.append(f"1h 大跌 {d1h:.2f}%")
            out["reasons"] = reasons
            out["shock"] = bool(reasons)
    except Exception as e:
        out["error"] = str(e)

    SHOCK_CACHE["ts"], SHOCK_CACHE["data"] = now, out
    return out


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
    """胜率：**journal 真相口径**（strategy_truth.py），列表与汇总同源。

    ★ 2026-10-26 修：原来用 `category == "profit"` 判赢，但 BE_STOPPED（打保本）
      的净额常常是 **负数**（只够覆盖手续费，实测 5 笔 -0.85/-0.86U），
      却被标成 category=profit 算「赢」→ 胜率虚高 10.7pp（51.1% vs 真实 40.4%）。
      判据改为**净额符号**：net_usd > 0 才算赢，= 0 算保本（不算赢也不算输）。

    ★ 2026-10-30 修：**数据源从 events 换成 journal 真相口径**
      原来读 position_events.jsonl（events 是通知流，可能过期），跟
      「交易明细」列表（同一屏）口径不一致 → 汇总卡和列表合计对不上。
      现在两者都走 strategy_truth（同一份真相），必然自洽。
      同时剔掉：双胞胎重复单 + 出场数据被 risk_guard 重写的 4 笔（净值虚高 208U）。
    """
    truth = _strategy_truth()
    counted = truth["records"]          # 已结算可信记录（净值有值）

    def _is_win(r: dict) -> bool:
        n = r.get("net_usd")
        return n is not None and n > 0

    def _is_loss(r: dict) -> bool:
        n = r.get("net_usd")
        return n is not None and n < 0

    wins = [r for r in counted if _is_win(r)]
    losses = [r for r in counted if _is_loss(r)]
    breakeven = [r for r in counted if r.get("net_usd") == 0]
    total = len(counted)
    win_n = len(wins)

    # 最近 20 笔胜率
    closed_sorted = sorted(counted, key=lambda r: r.get("ts", 0), reverse=True)[:20]
    recent_win = sum(1 for r in closed_sorted if _is_win(r))

    # 近 7 天
    week_ago = time.time() - 7 * 86400
    week = [r for r in counted if (r.get("ts", 0) or 0) >= week_ago]
    week_win = sum(1 for r in week if _is_win(r))

    # 累计净盈亏
    total_pnl = sum(r.get("net_usd", 0) for r in counted)

    return {
        "total": total,
        "wins": win_n,
        "losses": len(losses),
        "breakeven": len(breakeven),
        "winrate_pct": round(win_n / total * 100, 1) if total else None,
        "winrate_basis": "net_usd>0",
        "recent_20": {"total": len(closed_sorted), "wins": recent_win,
                       "winrate_pct": round(recent_win / len(closed_sorted) * 100, 1) if closed_sorted else None},
        "last_7d": {"total": len(week), "wins": week_win,
                     "winrate_pct": round(week_win / len(week) * 100, 1) if week else None},
        "net_usd_total": round(total_pnl, 2),
        # ★ 2026-10-30：汇总行要展示的字段（跟交易明细面板同一套口径）
        "pending": truth["pending"],
        "total_all": truth["summary"]["total"],
        "win_rate": f"{win_n}/{total}" if total else "0/0",
        "suspect_n": len(truth["excluded"]),
        "latest_5": [
            {"id": r.get("id", "")[:35], "outcome": r.get("outcome", "?"),
             "category": r.get("category", "?"), "net_usd": round(r.get("net_usd", 0), 2),
             "ts": r.get("ts")}
            for r in sorted(counted, key=lambda r: r.get("ts", 0), reverse=True)[:5]
        ],
    }




def _is_test_id(rid: str) -> bool:
    """测试/合成 id 判定：不让假数据进胜率统计。

    2026-10-05：TEST- / test- 前缀。
    2026-10-25 加：**合成 BTC_USDT-<短数字> id**（如 BTC_USDT-1000-short）。
      背景：单测用这种 id 写过真实 /root/data/position_events.jsonl（patch 被 reload
      覆盖的 bug），而它们既不带 TEST- 前缀、又以 BTC_USDT- 开头 →
      同时穿过 _winrate 的前缀过滤和 _all_settled 的白名单，
      实测把胜率从 38.2%(n=68) 拉成 37.1%(n=70)、合计净利从 +56.78U 虚成 +40.66U。
      判定规则：生产 id 的数字段是 10 位 unix 秒（≥1e9，形如 1792854921），
      合成 id 的数字段是短序号（1000/1002/1003…）→ 数字段 < 1e9 一律当测试数据。
    """
    rid = rid or ""
    if rid.startswith("TEST") or rid.startswith("test-"):
        return True
    if "TEST" in rid.upper() and rid.upper().startswith("BTC_USDT-TEST"):
        return True
    # 数字段必须是可信的 unix 时间戳（生产 id 都是 10 位秒级）
    m = re.match(r"^[A-Za-z_]+-(\d+)-", rid)
    if m:
        try:
            return int(m.group(1)) < 1_000_000_000
        except ValueError:
            return True
    return False


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
        if _is_test_id(rid):
            continue   # 2026-10-25：合成短数字段 id（BTC_USDT-1000-*）也拦下
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


def _strategy_truth(env: str = "prod") -> dict:
    """平台策略单真相口径（列表 + 汇总同源）。

    ★ 2026-10-30 新增。口径细则见 strategy_truth.py 的模块 docstring。
    env="prod" 读 /root/data/journal.jsonl
    env="training" 读 /root/data/training/journal.jsonl
    """
    import strategy_truth as _st
    path = _st.TRAIN_JOURNAL if env == "training" else (_st.DATA_DIR / "journal.jsonl")
    return _st.strategy_truth(path)


def _suspect_records(excluded_ids: dict) -> list:
    """把被剔除的记录（双胞胎 / 出场数据被重写）从原始账本取回来。

    这些单**要显示在列表里**（用户有权看到它们），只是：
      - 不进汇总统计
      - 行上标「数据存疑」角标，鼠标悬停给出原因
    """
    if not excluded_ids:
        return []
    recs = _read_jsonl(DATA_DIR / "journal.jsonl")
    return [r for r in recs if (r.get("id") or "") in excluded_ids]


def _missed_records() -> list:
    """未进场（MISSED / 作废）的记录 —— 默认不显示，?include_missed=1 才带出来。"""
    recs = _read_jsonl(DATA_DIR / "journal.jsonl")
    out = []
    for r in recs:
        if r.get("outcome") in ("MISSED", "NOISE_INVALIDATED", "DUPLICATE_BLOCKED"):
            out.append(r)
    return out


def _test_records() -> list:
    """测试/合成 id 的记录 —— 默认不显示，?include_test=1 才带出来。"""
    recs = _read_jsonl(DATA_DIR / "journal.jsonl")
    return [r for r in recs if _is_test_id(r.get("id", ""))]


def _settlements_api(path: str) -> dict:
    """
    分页结算列表：
      GET /api/settlements?page=1&size=20&include_test=0
        page: 1-indexed（默认 1）
        size: 每页条数（默认 20，最大 100）
        include_test: 0 默认（过滤 TEST- 前缀）；1 显示全部
      返回 {items, total, page, size, has_more, included_test}

    ★ 2026-10-30：**数据源换成 journal 真相口径**（strategy_truth.py）
      原来列表读 position_events.jsonl（events），跟同页的汇总卡（_winrate）
      口径不一致 → 同一屏上「列表合计」和「汇总数字」对不上（实测差 271U）。
      现在列表和汇总**同源同口径**（都出自 strategy_truth），必然自洽。

      被剔除的记录（双胞胎 / 出场数据被重写）**仍然照常出现在列表里**
      —— 用户要看到它们，只是标一个「数据存疑」角标且不进汇总。
    """
    from urllib.parse import urlparse, parse_qs
    q = parse_qs(urlparse(path).query)
    page = max(1, int(q.get("page", ["1"])[0]))
    size = min(100, max(1, int(q.get("size", ["20"])[0])))
    include_test = q.get("include_test", ["0"])[0] in ("1", "true", "yes")
    include_missed = q.get("include_missed", ["0"])[0] in ("1", "true", "yes")

    # ★ 真相口径一次取全（列表 + 汇总共用）
    truth = _strategy_truth()
    trusted = truth["records"]
    excluded_ids = {e["id"]: e["reason"] for e in truth["excluded"]}

    # 「待复核」记录（双胞胎 / 出场被重写）也要显示 —— 从原始账本取回来
    suspect_recs = _suspect_records(excluded_ids)

    all_recs = list(trusted)
    if include_missed:
        all_recs += _missed_records()
    if include_test:
        all_recs += _test_records()
    all_recs += suspect_recs

    total_all = len(trusted) + truth["pending"]
    test_n = sum(1 for r in all_recs if _is_test_id(r.get("id", "")))

    recs_sorted = sorted(all_recs, key=lambda r: r.get("ts", 0), reverse=True)
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
        suspect = excluded_ids.get(rid)
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
            # ★ 2026-10-30：数据存疑标记（双胞胎 / 出场数据被重写）
            #   前端据此显示角标并说明「未计入统计」
            "suspect": suspect,
            "suspect_reason": (
                "重复单（双胞胎），未计入统计" if suspect == "twin"
                else "出场价/出场时间被系统重写，净值存疑，未计入统计" if suspect == "rewritten_exit"
                else ""
            ),
        })

    s = truth["summary"]
    return {
        "items": items,
        "total": total,
        "page": page,
        "size": size,
        "has_more": end < total,
        "included_test": include_test,
        "filtered_test_n": test_n,
        "total_all": total_all,
        # ★ 2026-10-30：汇总与列表同源（strategy_truth）
        "summary": {
            "total": s["total"],
            "settled": s["settled"],
            "pending": s["pending"],
            "net_usd": s["net_usd"],
            "win_rate": s["win_rate"],
            "wins": s["wins"],
            "losses": s["losses"],
            "suspect_n": len([e for e in truth["excluded"]]),
        },
        "suspect_ids": sorted(excluded_ids.keys()),
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
        # ★ 2026-10-26 新增：分代明细（旧代不再污染当前代判断，但保留可见）
        # 2026-10-17 liusir: 借鉴 LuxAlgo/trade-journal
        "edge_score": _edge_score(),
        "pnl_calendar": _pnl_calendar(),
        "mistakes": _mistakes_summary(),
    }


def _edge_score() -> dict:
    """Edge Score（参考 trade-journal v2 公式）

    ★ 2026-10-26 改：原来算混代全量（旧代 33 笔 -95.59U 混在里面），
    分数被拖低看不出当前代真实健康度。改成**只算当前代**（15m 形态信号）。
    """
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
        from edge_score import compute_edge_score
        from era_split import split_by_era
        recs = _read_jsonl(DATA_DIR / "journal.jsonl")
        cur = (split_by_era(recs).get("current_15m") or [])
        es = compute_edge_score(cur)
        out = es.to_dict() if es else {"error": "不足 5 笔有效"}
        out["scope"] = "current_15m"
        return out
    except Exception as e:
        return {"error": str(e)}


def _pnl_calendar() -> dict:
    """P&L 日历（每日盈亏 + 红/绿/平 + 最大单日）"""
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
        from pnl_calendar import compute_pnl_calendar
        recs = _read_jsonl(DATA_DIR / "journal.jsonl")
        return compute_pnl_calendar(recs, days=30)
    except Exception as e:
        return {"error": str(e)}


def _mistakes_summary() -> dict:
    """错误归因（按 mistake 分类统计 top3）

    ★ 2026-10-31 修：**数据源换成 strategy_truth 可信记录**。
      原来直接读 journal 原始行（含 invalidated / MISSED / 双胞胎 / 出场被重写的单），
      分母被撑大，且这些行会被自动打上错误标签。
      实测旧口径：36 笔「TP1 后没推保本」占 50.0% —— 但那 36 笔的 active_sl
      全部 == entry（保本确实推了），是 BE_STOPPED 被打错标签（见 scripts/mistakes.py）。
      现在与胜率卡同源（_strategy_truth），同屏数字必然自洽。
    """
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
        from mistakes import summarize_mistakes
        truth = _strategy_truth()
        res = summarize_mistakes(truth["records"])
        res["excluded"] = len(truth["excluded"])
        # MISSED（信号出了没成交）不在 truth["records"] 里（被当非成交剔掉了），
        # 单独从原始账本数，不然「未成交」永远是 0。
        raw = _read_jsonl(DATA_DIR / "journal.jsonl")
        res["not_filled"] = sum(1 for r in raw if r.get("outcome") == "MISSED")
        return res
    except Exception as e:
        return {"error": str(e)}


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
        # 2026-10-17 加：训练版缺 btc 实时价，导致训练持仓卡片"最新价"显示的是
        # signal 触发时的 last 价 (12+ 小时前)，不是当前 BTC 价格
        "btc": _btc_last(),
        "btc_meta": _btc_meta(),
        # 2026-10-18：训练策略-效果矩阵（按 signal_source 聚合）
        "strategy_matrix": _strategy_matrix_training(),
        # 2026-10-18：最近 10 笔训练明细（按时间倒序）
        "recent_training_trades": _recent_training_trades(),
    }


def _strategy_matrix_training() -> list:
    """按 signal_source 聚合 5 objective。

    输出: [{"source": "15m_pattern", "n": 13, "net_usd": -41.05, "win_rate": 0.0,
            "pf": 0.0, "max_dd_pct": 0.0, "trades": [...ids...]}]
    """
    p = Path("/root/data/training/journal.jsonl")
    if not p.exists():
        return []
    groups: dict = {}
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        if r.get("status") != "settled":
            continue
        src = r.get("signal_source") or "uncategorized"
        g = groups.setdefault(src, {"n": 0, "wins": 0, "gross_profit": 0.0,
                                     "gross_loss": 0.0, "net_usd": 0.0,
                                     "dd_sum": 0.0, "dd_n": 0, "ids": []})
        g["n"] += 1
        net = r.get("net_usd") or 0
        g["net_usd"] += net
        if net > 0:
            g["wins"] += 1
            g["gross_profit"] += net
        else:
            g["gross_loss"] += abs(net)
        if r.get("max_drawdown_pct"):
            g["dd_sum"] += abs(float(r["max_drawdown_pct"]))
            g["dd_n"] += 1
        g["ids"].append(r.get("id"))
    out = []
    for src, g in sorted(groups.items(), key=lambda kv: -kv[1]["n"]):
        wr = g["wins"] / g["n"] if g["n"] else 0
        pf = g["gross_profit"] / g["gross_loss"] if g["gross_loss"] > 0 else (
            float("inf") if g["gross_profit"] > 0 else 0)
        dd = g["dd_sum"] / g["dd_n"] if g["dd_n"] else 0
        out.append({
            "source": src,
            "n": g["n"],
            "net_usd": round(g["net_usd"], 2),
            "win_rate": round(wr * 100, 1),
            "pf": round(pf, 2) if isinstance(pf, float) and pf != float("inf") else "∞",
            "max_dd_pct": round(dd * 100, 2),
        })
    return out


def _recent_training_trades(n: int = 10) -> list:
    """训练 journal 最近 n 笔（按 exit_ts 倒序；未结算的按 ts 倒序）"""
    p = Path("/root/data/training/journal.jsonl")
    if not p.exists():
        return []
    rows = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        rows.append(r)
    rows.sort(key=lambda r: r.get("exit_ts") or r.get("ts") or 0, reverse=True)
    out = []
    for r in rows[:n]:
        out.append({
            "id": r.get("id"),
            "ts": r.get("ts"),
            "ts_str": time.strftime("%m-%d %H:%M", time.localtime(r.get("ts"))) if r.get("ts") else "—",
            "side": r.get("side"),
            "status": r.get("status"),
            "outcome": r.get("outcome"),
            "entry": r.get("entry"),
            "sl": r.get("sl"),
            "tp1": r.get("tp1"),
            "tp2": r.get("tp2"),
            "exit_px": r.get("exit_px"),
            "exit_ts": r.get("exit_ts"),
            "exit_ts_str": time.strftime("%m-%d %H:%M", time.localtime(r.get("exit_ts"))) if r.get("exit_ts") else "—",
            "net_usd": r.get("net_usd"),
            "category": r.get("category"),
            "signal_source": r.get("signal_source"),
        })
    return out


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


def build_status_teacher(path: str = "") -> dict:
    """老师口头单状态聚合（前端老师单卡片用）

    2026-10-18 加：支持 ?page=&size=&cat=&side=&sort= 参数
      cat:  all | profit | loss | flat | open
      side: all | long | short
      sort: time_desc | time_asc | pnl_desc | pnl_asc | move_desc | move_asc
    返回 {ts, ts_str, total, settled, pending, net_usd, win_rate, trades,
          page, size, has_more, filtered_n}

    老数据 result.open 可能缺失：用 net_usd != None 视为已结算兜底。
    """
    from urllib.parse import urlparse, parse_qs
    q = parse_qs(urlparse(path).query) if path else {}
    page = max(1, int(q.get("page", ["1"])[0]))
    size = min(100, max(1, int(q.get("size", ["20"])[0])))
    cat = (q.get("cat", ["all"])[0] or "all").lower()
    side = (q.get("side", ["all"])[0] or "all").lower()
    sort = (q.get("sort", ["time_desc"])[0] or "time_desc").lower()

    trades = _read_teacher_trades()

    def _is_open(t):
        r = t.get("result") or {}
        if r.get("open") is False:
            return False
        if r.get("net_usd") is not None:
            return False
        return True

    def _has_pnl(t):
        """★ 2026-10-28：net_usd 为 null 的单（#11 K线冲突未决）**不算已结算**
        —— 旧逻辑只看 result.open False 就把它算进胜率分母，胜率被虚高。
        口径：net_usd 有值 = 可计入统计；net_usd=null = 挂着待定。"""
        r = t.get("result") or {}
        return r.get("net_usd") is not None

    # 分类：profit/loss/flat/open —— 单笔维度，全程统计不受过滤影响
    def _cat_of(t):
        if _is_open(t):
            return "open"
        n = ((t.get("result") or {}).get("net_usd"))
        if n is None:
            return "flat"
        return "profit" if n > 0 else "loss" if n < 0 else "flat"

    # 应用过滤
    filtered = [t for t in trades if (cat == "all" or _cat_of(t) == cat) and (side == "all" or t.get("side") == side)]
    filtered_n = len(filtered)

    # 排序
    def _sort_key(t):
        r = t.get("result") or {}
        if sort == "pnl_desc":
            return -(r.get("net_usd") or 0)
        if sort == "pnl_asc":
            return r.get("net_usd") or 0
        if sort == "time_asc":
            return -(t.get("trade_id") or 0)  # asc 用 trade_id 反向
        if sort == "move_desc":
            entry = t.get("fill_price") or t.get("entry_zone")
            evs = t.get("events") or []
            last = evs[-1] if evs else {}
            px = last.get("px") if isinstance(last, dict) else None
            try:
                e = float(entry) if entry is not None else 0
                p = float(px) if px is not None else e
                raw = p - e
                signed = -raw if t.get("side") == "short" else raw
                return -abs(signed / e * 100) if e else 0
            except Exception:
                return 0
        # 默认 time_desc
        return -(t.get("trade_id") or 0)

    if sort == "move_asc":
        # move_asc: abs 从小到大 → 取负号的反向
        filtered.sort(key=lambda t: -_sort_key(t))
    else:
        filtered.sort(key=_sort_key)

    # 分页
    start = (page - 1) * size
    end = start + size
    page_items = filtered[start:end]

    # 顶部分类/胜率/累计净利（始终基于已结算全集，不受过滤影响）
    # ★ 2026-10-28：已结算 = net_usd 有值的单。#11 这类 net_usd=null 的
    # （K 线冲突未决、结果待定）不进胜率分母，也不进累计净利 —— 否则胜率虚高
    settled = [t for t in trades if _has_pnl(t)]
    pending = [t for t in trades if not _has_pnl(t)]
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
        "trades": page_items,
        "page": page,
        "size": size,
        "has_more": end < filtered_n,
        "filtered_n": filtered_n,
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
    # ★ 2026-10-24 修：原来 sl_initial 缺省直接落成 entry（保本价），
    #   导致老师**原始止损在录入那一刻就丢了** —— 事后看每一单都像
    #   "SL=entry、风险 0 点"，得出"老师从不设真止损"的错误结论。
    #   现在改成显式 null（=老师没给初始止损），由后续 TP1 事件单独记录
    #   "挪到保本"这件事（sl_after_tp1），两者不再互相覆盖。
    sl_initial_f = float(sl_initial) if sl_initial is not None else None

    if payload["side"] == "long":
        tp1_pct = (tp1 - entry) / entry * 100
        tp2_pct = ((tp2_f - entry) / entry * 100) if tp2_f else None
        sl_pct_initial = ((sl_initial_f - entry) / entry * 100) if sl_initial_f else None
    else:
        tp1_pct = (entry - tp1) / entry * 100
        tp2_pct = ((entry - tp2_f) / entry * 100) if tp2_f else None
        sl_pct_initial = ((entry - sl_initial_f) / entry * 100) if sl_initial_f else None

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

# 出场事件类型（会吃仓位、产生 gross）
#   tp1_partial / tp2_partial / remaining_close / tp2_full / sl_breakeven / close_sl
# 历史别名：sl_breakeven = remaining_close，tp2_full = remaining_close
_TEACHER_CLOSE_TYPES = {
    "tp1_partial", "tp2_partial", "remaining_close", "tp2_full",
    "sl_breakeven", "close_sl",
}
# 保本腿：只有 status == "TRIGGERED" 才是"被打保本出场"；
# 没有 status 的 breakeven_stop 是"把止损移到保本"这个动作本身（不是出场腿）
_TEACHER_BE_TYPES = {"breakeven_stop"}
# 只占仓位、不产生 gross（仓位已分配但还没平）
_TEACHER_PENDING_TYPES = {"remaining_pending"}
# 触发标记（记录"止损被触发"这一事实，但不是出场腿，不重复计盈亏）
_TEACHER_TRIGGER_TYPES = {"sl_triggered"}
# 非出场事件（建仓腿 / 盘前标记）
_TEACHER_ENTRY_TYPES = {"entry", "add_position", "pre"}


def _teacher_entry_basis(t: dict) -> Optional[float]:
    """入场价基准（多腿建仓用加权均价，单腿用 fill_price）"""
    res = t.get("result") or {}
    avg = res.get("avg_entry")
    if isinstance(avg, (int, float)):
        return float(avg)
    evs = t.get("events") or []
    legs = [
        (e.get("px"), e.get("qty_pct"))
        for e in evs
        if e.get("type") in ("entry", "add_position") and isinstance(e.get("px"), (int, float))
    ]
    if len(legs) > 1:  # 多腿 → 按 qty_pct 加权
        num = sum(px * (q if isinstance(q, (int, float)) else 0) for px, q in legs)
        den = sum(q if isinstance(q, (int, float)) else 0 for _px, q in legs)
        if den:
            return num / den
    entry = t.get("fill_price") or t.get("entry_zone")
    return float(entry) if isinstance(entry, (int, float)) else None


def _teacher_contracts(t: dict, entry_basis: float) -> int:
    """仓位张数：优先用 size_contracts（与名义推导接近时才采信），否则按名义推导"""
    notional = t.get("notional_usd")
    if not isinstance(notional, (int, float)):
        notional = 5000
    derived = round(notional / (entry_basis * _TEACHER_QUANT_MULT))
    size = t.get("size_contracts")
    if isinstance(size, (int, float)) and size > 0:
        # size_contracts 语义历史上不统一（#13=总仓 614、#10=单腿 312），
        # 只在与名义推导接近时采信，否则以名义推导为准
        if abs(size - derived) <= max(2, derived * 0.05):
            return int(size)
    return derived


def _compute_trade_net_usd(t: dict) -> Optional[float]:
    """
    根据 trade 完整 events 数组算 net_usd：
      contracts = round(notional / (entry_basis * QUANT_MULT))
      partial gross = (tp1_px - entry_basis) × partial_contracts × QUANT_MULT
      remaining gross = (remaining_px - entry_basis) × remaining_contracts × QUANT_MULT
      fee = 4.85U（全程一笔，无论 tp1 partial + remaining close）
      net = total_gross - fee

    长/短方向：用 trade.side。

    ★ 2026-10-28 修的四个语义坑（全部来自真实老师单，详见 skill btc-position-ops）：
      1. 多腿建仓（add_position）：entry_basis 必须是加权均价，不能用首腿 fill_price
      2. NOT_TRIGGERED 占位事件（目标价从未触达）：**不计 gross、不占仓位**
      3. breakeven_stop 两种语义：无 status = "把止损移到保本"（不是出场腿）；
         status=TRIGGERED = "真的被打保本出场"
      4. **缺 qty_pct 的出场腿不能 fallback 成 100%**（旧代码用 100-consumed_pct，
         当它是第一条出场腿时 → 整段仓位算成 100%，净利虚高十几 U）
         正确口径：用「100% - 其余明确腿之和」反推；反推不出来 → 返回 None，不猜
    return None 当 events 不完整（缺 entry / 无法确定各腿比例 / 没有出场腿）。
    """
    side = t.get("side")
    if side not in ("long", "short"):
        return None
    entry_basis = _teacher_entry_basis(t)
    if entry_basis is None:
        # 区间成交（如 '82400-82600'）或非数值 → 自动算不出来，跳过返回 None 由用户手填
        return None

    evs = t.get("events") or []
    if not evs:
        return None

    contracts = _teacher_contracts(t, entry_basis)

    # ── 第一遍：分类事件 + 收集各腿比例 ──────────────────────────────
    legs = []          # {px, qty, kind}
    explicit_sum = 0   # 已明确的**出场腿**比例之和（pending 不计入——它是同一批仓位的占位）
    missing = []       # 缺 qty_pct 的出场腿
    for ev in evs:
        et = ev.get("type")
        ep = ev.get("px")
        qp = ev.get("qty_pct")
        if et in _TEACHER_ENTRY_TYPES:
            continue
        if et in _TEACHER_TRIGGER_TYPES:
            continue          # 触发标记，不是出场腿
        if ev.get("status") == "NOT_TRIGGERED":
            continue          # 目标价从未触达 → 不计 gross、不占仓位
        if ep is None or not isinstance(ep, (int, float)):
            continue
        if et in _TEACHER_BE_TYPES:
            # breakeven_stop：status=TRIGGERED → 真的被打保本出场；
            # 无 status → 只是"把止损移到保本"这个动作，不吃仓位
            if ev.get("status") != "TRIGGERED":
                continue
            kind = "close"
        elif et in _TEACHER_PENDING_TYPES:
            # remaining_pending：仓位已分配但还没平 → 只占仓位，不产生 gross
            kind = "pending"
        elif et in _TEACHER_CLOSE_TYPES:
            kind = "close"
        else:
            continue
        leg_qty = int(qp) if isinstance(qp, (int, float)) else None
        if leg_qty is None:
            missing.append(len(legs))
        elif kind == "close":
            explicit_sum += leg_qty
        legs.append({"px": ep, "qty": leg_qty, "kind": kind})

    if not legs:
        return None

    # ── 第二遍：补齐缺 qty_pct 的腿 ────────────────────────────────
    if missing:
        if len(missing) > 1:
            return None      # 多条腿都缺比例 → 无法唯一反推，不猜
        rest = 100 - explicit_sum
        if rest <= 0:
            return None      # 明确腿已超 100% → 数据自相矛盾，不猜
        legs[missing[0]]["qty"] = rest
    elif explicit_sum > 100:
        # 出场腿比例加起来超 100% → 数据自相矛盾（如 tp2_partial 写成 50% 但实际是 25%）
        return None          # 不猜，交给人修数据

    # ── 第三遍：算 gross ───────────────────────────────────────────
    total_gross = 0.0
    consumed_pct = 0
    for leg in legs:
        qty_pct = leg["qty"]
        if leg["kind"] == "pending":
            consumed_pct += qty_pct   # 只占仓位，不产生 gross
            continue
        ev_contracts = contracts * qty_pct / 100.0
        if side == "long":
            leg_gross = (leg["px"] - entry_basis) * ev_contracts * _TEACHER_QUANT_MULT
        else:
            leg_gross = (entry_basis - leg["px"]) * ev_contracts * _TEACHER_QUANT_MULT
        total_gross += leg_gross
        consumed_pct += qty_pct

    if consumed_pct == 0:
        return None

    net = total_gross - _TEACHER_FEE_USD
    return round(net, 2)


# ============================ 行情分析（BTC 多周期 4 维） ============================
# 2026-10-18 liusir 要求：在 BTC 现价右侧展示当前行情分析卡，每 15min 一次（前端定时）
# + 手动 button。服务端不缓存（按需 4 个 gate 公共 API，~1-2s 一次），前端缓存 15min。
#
# 4 维：
#   1) 趋势（15m + 1h + 4h EMA 方向 + 收敛发散）
#   2) 波动率（1h 真实波幅 ATR-14 vs 长期均值，%）
#   3) 关键位（24h 最高最低 + 距现价，+1% / -1% 阈值）
#   4) 资金费率（最新 + 上次，方向）

def _ema(values, n):
    if not values:
        return None
    k = 2 / (n + 1)
    e = values[0]
    for v in values[1:]:
        e = v * k + e * (1 - k)
    return e


def _atr(klines, n):
    """Average True Range（n 周期）。klines: [[t, o, h, l, c, v], ...]。"""
    if len(klines) < 2:
        return None
    trs = []
    prev_c = float(klines[0][4])
    for k in klines[1:]:
        h, l, c = float(k[2]), float(k[3]), float(k[4])
        tr = max(h - l, abs(h - prev_c), abs(l - prev_c))
        trs.append(tr)
        prev_c = c
    if not trs:
        return None
    return sum(trs[-n:]) / min(n, len(trs))


def _v2_gate_safe() -> dict:
    """
    ★ 2026-10-24：取 v2.1 开仓闸门状态，失败也不拖垮行情分析端点。
    v2_journal 只在训练环境跑（TRAINING_MODE=1），但面板要显示闸门状态，
    所以这里只读取值，不下单、不写任何 journal。
    """
    try:
        import v2_journal
        st = v2_journal.gate_status()
        st["ok"] = True
        return st
    except Exception as e:
        return {"ok": False, "error": f"闸门状态读取失败: {e}",
                "vol_5d_pct": None, "vol_min_pct": None, "vol_pass": None,
                "blocked_24h": None}


def build_market_analysis() -> dict:
    """BTC 行情多周期分析（4 维）。详见模块顶部注释。"""
    try:
        sys.path.insert(0, str(SKILL_ROOT / "scripts"))
        from gate_fetch import fetch_public, fetch_ticker
    except Exception as e:
        return {"error": f"gate_fetch 不可用：{e}",
                "ts": int(time.time()), "ts_str": time.strftime("%Y-%m-%d %H:%M:%S")}

    def _klines(interval, limit):
        try:
            data = fetch_public(
                "/futures/usdt/candlesticks",
                {"contract": "BTC_USDT", "interval": interval, "limit": limit},
            )
            out = []
            for k in data:
                out.append([
                    float(k["t"]),
                    float(k["o"]),
                    float(k["h"]),
                    float(k["l"]),
                    float(k["c"]),
                    float(k.get("v", 0)),
                ])
            return out
        except Exception:
            return []

    def _trend_from(closes):
        if len(closes) < 30:
            return "flat", None, None
        ema20 = _ema(closes, 20)
        ema50 = _ema(closes, 50) if len(closes) >= 50 else ema20
        diff_pct = (ema20 - ema50) / ema50 * 100 if ema50 else 0
        if diff_pct > 0.15:
            return "up", ema20, ema50
        if diff_pct < -0.15:
            return "down", ema20, ema50
        return "flat", ema20, ema50

    k15 = _klines("15m", 60)
    k1h = _klines("1h", 60)
    k4h = _klines("4h", 60)

    trend_15m, _, _ = _trend_from([k[4] for k in k15])
    trend_1h, ema20_1h, ema50_1h = _trend_from([k[4] for k in k1h])
    trend_4h, ema20_4h, ema50_4h = _trend_from([k[4] for k in k4h])

    trends = [trend_15m, trend_1h, trend_4h]
    if len(set(trends)) == 1:
        convergence = "converging" if trends[0] != "flat" else "flat"
    else:
        convergence = "diverging"

    atr_1h = _atr(k1h, 14)
    last_px = float(k1h[-1][4]) if k1h else None
    atr_pct = (atr_1h / last_px * 100) if (atr_1h and last_px) else None
    if atr_pct is not None:
        if atr_pct < 0.30:
            regime = "low"
        elif atr_pct > 1.00:
            regime = "high"
        else:
            regime = "normal"
    else:
        regime = "normal"

    key_levels = {"high_24h": None, "low_24h": None,
                  "dist_to_high_pct": None, "dist_to_low_pct": None,
                  "near_resistance": False, "near_support": False}
    try:
        tk = fetch_ticker("BTC_USDT")
        if tk:
            t = tk[0]
            hi = float(t.get("high_24h") or 0)
            lo = float(t.get("low_24h") or 0)
            last_tk = float(t.get("last") or 0) or last_px
            key_levels["high_24h"] = hi
            key_levels["low_24h"] = lo
            if hi and last_tk:
                d_hi = (hi - last_tk) / last_tk * 100
                key_levels["dist_to_high_pct"] = round(d_hi, 2)
                key_levels["near_resistance"] = 0 < d_hi < 0.5
            if lo and last_tk:
                d_lo = (last_tk - lo) / last_tk * 100
                key_levels["dist_to_low_pct"] = round(d_lo, 2)
                key_levels["near_support"] = 0 < d_lo < 0.5
    except Exception:
        pass

    funding = {"current": None, "previous": None, "sign": "neutral"}
    try:
        from gate_fetch import fetch_funding_rate
        fr = fetch_funding_rate("BTC_USDT", limit=2)
        if len(fr) >= 1:
            funding["current"] = float(fr[0].get("r", 0))
        if len(fr) >= 2:
            funding["previous"] = float(fr[1].get("r", 0))
        cur = funding["current"]
        if cur is not None:
            if cur > 0.0001:
                funding["sign"] = "long_pay"
            elif cur < -0.0001:
                funding["sign"] = "short_pay"
            else:
                funding["sign"] = "neutral"
    except Exception:
        pass

    return {
        "ts": int(time.time()),
        "ts_str": time.strftime("%Y-%m-%d %H:%M:%S"),
        "last_px": last_px,
        "trend": {
            "15m": trend_15m, "1h": trend_1h, "4h": trend_4h,
            "convergence": convergence,
            "ema20_1h": round(ema20_1h, 2) if ema20_1h else None,
            "ema50_1h": round(ema50_1h, 2) if ema50_1h else None,
            "ema20_4h": round(ema20_4h, 2) if ema20_4h else None,
            "ema50_4h": round(ema50_4h, 2) if ema50_4h else None,
        },
        "volatility": {
            "atr_1h": round(atr_1h, 2) if atr_1h else None,
            "atr_pct": round(atr_pct, 3) if atr_pct is not None else None,
            "regime": regime,
        },
        "key_levels": key_levels,
        "funding": funding,
        # ★ 2026-10-24：v2.1 开仓闸门状态。
        #   与上面的 volatility（1h ATR 口径）**不是同一个指标**，别混：
        #     volatility.regime = 1h ATR / 价，用于行情提示（低波/高波/正常）
        #     v2_gate.vol_5d_pct = 近 5 日 4h 已实现波动率，用于 v2.1 开仓闸门
        #   阈值 0.65%：低波动下 v2.1（超跌反弹）从无优势。
        #   绝对不能让它抛异常拖垮整个 market_analysis 端点 → try/except 兜底。
        "v2_gate": _v2_gate_safe(),
        "advisory": _advisory(ma_trend_15m=trend_15m, ma_trend_1h=trend_1h,
                              ma_trend_4h=trend_4h, ma_convergence=convergence,
                              ma_vol_regime=regime, ma_vol_pct=atr_pct,
                              ma_funding_sign=funding.get("sign"),
                              ma_funding_cur=funding.get("current"),
                              ma_near_res=key_levels.get("near_resistance"),
                              ma_near_sup=key_levels.get("near_support"),
                              ma_payload={
                                  "ts_str": time.strftime("%Y-%m-%d %H:%M:%S"),
                                  "last_px": last_px,
                                  "trend": {"15m": trend_15m, "1h": trend_1h, "4h": trend_4h,
                                            "convergence": convergence,
                                            "ema20_1h": round(ema20_1h, 2) if ema20_1h else None,
                                            "ema50_1h": round(ema50_1h, 2) if ema50_1h else None,
                                            "ema20_4h": round(ema20_4h, 2) if ema20_4h else None,
                                            "ema50_4h": round(ema50_4h, 2) if ema50_4h else None},
                                  "volatility": {"atr_pct": round(atr_pct, 3) if atr_pct is not None else None,
                                                 "regime": regime},
                                  "key_levels": key_levels,
                                  "funding": funding,
                              }),
    }


def _advisory(ma_trend_15m, ma_trend_1h, ma_trend_4h, ma_convergence,
              ma_vol_regime, ma_vol_pct, ma_funding_sign, ma_funding_cur,
              ma_near_res, ma_near_sup, ma_payload=None, mm_ns: str = "main") -> dict:
    """综合建议（主观启发式，2026-10-18 liusir 要求）

    规则按优先级（命中第一条返回）：
      1. 费率极端（|r| > 0.05%）→ "费率极端（多头/空头挤压）注意反向"
      2. 高波动（atr_pct > 1%）且 4h 趋势分歧 → "观望（高波动 + 趋势分歧）"
      3. 趋势收敛 + 1h 多 + 资金费率中性/负 → "偏多观察"
      4. 趋势收敛 + 1h 空 + 资金费率中性/正 → "偏空观察"
      5. 趋势收敛但 1h 平 → "趋势收敛但 1h 平，等方向"
      6. 趋势分歧 → "分歧，等共振"
      7. 4h 平 + 1h 任意 → "4h 平，仅 1h 信号"
      8. 全部平 → "无方向"
    + 关键位 modifier（命中主规则后追加）：
      - near_resistance → "近阻力" 红色提示
      - near_support → "近支撑" 绿色提示
    """
    rules = []
    main = None
    # 1. 费率极端
    if ma_funding_cur is not None and abs(ma_funding_cur) > 0.0005:
        if ma_funding_cur > 0:
            main = "费率极端（多头挤压）注意反向"
        else:
            main = "费率极端（空头挤压）注意反向"
    # 2. 高波动 + 趋势分歧
    elif ma_vol_pct is not None and ma_vol_pct > 1.0 and ma_convergence == "diverging":
        main = "观望（高波动 + 趋势分歧）"
    # 3-5. 收敛
    elif ma_convergence == "converging":
        if ma_trend_1h == "up" and ma_funding_sign in ("neutral", "short_pay"):
            main = "偏多观察"
        elif ma_trend_1h == "down" and ma_funding_sign in ("neutral", "long_pay"):
            main = "偏空观察"
        elif ma_trend_1h == "flat":
            main = "趋势收敛但 1h 平，等方向"
        else:
            main = "趋势收敛（多 1h / 费率反向）" if ma_trend_1h == "up" else "趋势收敛（空 1h / 费率反向）"
    # 6. 分歧
    elif ma_convergence == "diverging":
        main = "分歧，等共振"
    # 7. 4h 平
    elif ma_trend_4h == "flat":
        main = "4h 平，仅 1h 信号"
    # 8. 全部平
    else:
        main = "无方向"

    # 关键位 modifier
    if ma_near_res:
        main += " · 近阻力"
    elif ma_near_sup:
        main += " · 近支撑"

    # 颜色：偏多=绿 / 偏空=红 / 观望/分歧/平=灰 / 费率极端=琥珀
    if "偏多" in main or "近支撑" in main:
        color = "pos"
    elif "偏空" in main or "近阻力" in main:
        color = "neg"
    elif "费率极端" in main:
        color = "warn"
    else:
        color = "neu"

    # 风险 modifier
    if ma_vol_regime == "high":
        risks = "高波动"
    elif ma_vol_regime == "low":
        risks = "低波动（潜在突破）"
    else:
        risks = "正常波动"

    mm_text = _mm_advice(ma_payload, ns=mm_ns) if ma_payload else None
    return {
        "verdict": main,
        "color": color,
        "risk": risks,
        # 2026-10-18 加：mimo 生成的交易员口吻判读（None 时前端 fallback 到 verdict）
        "text": mm_text,
        "text_source": "mimo" if mm_text else "fallback",
    }


# mimo 判读缓存：按 namespace 隔离。
#   "main"  = /api/market_analysis（行情分析卡）
#   "board" = /api/market_board（行情大图）
# 两个端点的 payload 略有差异（取价口径/EMA 深度不同），共用一个缓存会导致
# 「图上写 80,145、AI 判读写 80,083」这种自相矛盾，所以分开存。
_MM_CACHES: dict = {"main": {"ts": 0, "text": None}, "board": {"ts": 0, "text": None}}
_MM_CACHE_TTL = 900  # mimo 生成的结果缓存 15min（跟前端 15min 自动刷新节奏一致；首请求慢由 nginx read_timeout 保障）


def _mm_advice(ma_payload: dict, ns: str = "main") -> "str | None":
    """调 mimo 生成 1 段交易员口吻的判读（2026-10-18 liusir 要求）

    输入：build_market_analysis() 的全部输出（trend / volatility / key_levels / funding + 4 维）
    输出：100-150 字中文判读，按「数据 → 分类 → 方向」三段式
    """
    import time as _t
    now = _t.time()
    cache = _MM_CACHES.setdefault(ns, {"ts": 0, "text": None})
    if cache["text"] and now - cache["ts"] < _MM_CACHE_TTL:
        return cache["text"]

    t = ma_payload.get("trend") or {}
    v = ma_payload.get("volatility") or {}
    k = ma_payload.get("key_levels") or {}
    f = ma_payload.get("funding") or {}
    last_px = ma_payload.get("last_px")
    hi = k.get("high_24h")
    lo = k.get("low_24h")
    funding_pct = (f.get("current") or 0) * 100
    fund_sign_zh = ("多头付费" if f.get("sign") == "long_pay"
                    else "空头付费" if f.get("sign") == "short_pay"
                    else "中性")

    data_summary = (
        f"现价 ${last_px:.0f}" if last_px else "现价未知"
    )
    if hi and lo and last_px:
        # 当前价在 24h 区间的位置（0%=最低，100%=最高）
        pos_pct = (last_px - lo) / (hi - lo) * 100 if hi > lo else 50
        data_summary += f" · 24h 区间 ${lo:.0f}-${hi:.0f}（当前位于 {pos_pct:.0f}%）"

    user_msg = f"""以下是 BTC 当前行情数据（{ma_payload.get('ts_str')}）：

{data_summary}
趋势：15m {t.get('15m')} / 1h {t.get('1h')} / 4h {t.get('4h')}（收敛性：{t.get('convergence')}）
      EMA20/50 (1h): {t.get('ema20_1h')} / {t.get('ema50_1h')}
      EMA20/50 (4h): {t.get('ema20_4h')} / {t.get('ema50_4h')}
波动率：1h ATR {v.get('atr_pct')}%（regime: {v.get('regime')}）
资金费率（8h）：{funding_pct:.4f}%（{fund_sign_zh}）
关键位：距 24h 高 {k.get('dist_to_high_pct')}% / 距 24h 低 {k.get('dist_to_low_pct')}%
        近阻力: {k.get('near_resistance')} / 近支撑: {k.get('near_support')}

请按「数据 → 分类 → 方向」三段式给一段 100-150 字的中文交易员判读：
1. **数据** 一句话说出当前最显眼的数据特征（位置 + 趋势方向 + 资金费率/波动是否异常）
2. **分类** 一句话定义当前是什么行情（趋势/区间/转折/突破前夜等）
3. **方向** 1-2 句指出近期最可能走的方向 + 关键位（突破哪里看多，跌破哪里看空，没信号就老实说观望）

口吻要求：像有 3-5 年经验的 BTC 交易员在群里发的简评，不绕弯不堆术语。少用 emoji。不用 markdown 加粗。用句号分句，不要换行。""".strip()

    try:
        import urllib.request
        import urllib.error
        mimo_url = "https://token-plan-cn.xiaomimimo.com/v1/chat/completions"
        mimo_model = "mimo-v2.6-pro"
        mimo_key = "tp-ct47anmx8cfumrz76520csausc2bho3vdaa6zohh1ib7lznm"
        body = json.dumps({
            "model": mimo_model,
            "max_tokens": 320,
            "temperature": 0.5,
            "messages": [
                {"role": "system", "content": "你是一个有 3-5 年经验的 BTC 合约交易员，习惯用一两句短话说清行情，不绕弯不堆术语。"},
                {"role": "user", "content": user_msg},
            ],
        }).encode("utf-8")
        req = urllib.request.Request(
            mimo_url, data=body,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {mimo_key}",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
        text = (data.get("choices") or [{}])[0].get("message", {}).get("content", "").strip()
        if not text:
            return None
        # 兜底：去掉首尾空白
        text = text.replace("\n", " ").strip()
        # 单段式：去掉可能的多余空行
        import re as _re
        text = _re.sub(r"\s+", " ", text)
        _MM_CACHES[ns]["text"] = text
        _MM_CACHES[ns]["ts"] = now
        return text
    except (urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError, KeyError, TimeoutError, OSError):
        # mimo 失败时返回 None，让调用方 fallback 到 advisory.verdict
        return None


def _build_market_board_payload() -> dict:
    """行情大图数据 + AI 判读。

    AI 判读走 _mm_advice(ns="board")：跟 /api/market_analysis 共用同一套
    _advisory 规则，但缓存命名空间独立（两个端点的取价口径略有差异，
    共用缓存会出现「图上 80,215 / 判读 80,083」这种自相矛盾）。
    """
    import market_board
    board = market_board.build_market_board()
    if board.get("error"):
        return board

    mp = board.get("ma_payload") or {}
    t = (mp.get("trend") or {})
    v = (mp.get("volatility") or {})
    k = (mp.get("key_levels") or {})
    f = (mp.get("funding") or {})

    adv = _advisory(
        ma_trend_15m=t.get("15m"), ma_trend_1h=t.get("1h"), ma_trend_4h=t.get("4h"),
        ma_convergence=t.get("convergence"),
        ma_vol_regime=v.get("regime"), ma_vol_pct=v.get("atr_pct"),
        ma_funding_sign=f.get("sign"), ma_funding_cur=f.get("current"),
        ma_near_res=k.get("near_resistance"), ma_near_sup=k.get("near_support"),
        ma_payload=mp, mm_ns="board",
    )
    board["advisory"] = adv
    return board


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
        # ===== 行情大图（独立页面，2026-10-27 liusir：一页只讲当前市场环境）=====
        if self.path == "/market" or self.path.startswith("/market.html"):
            try:
                body = MARKET_PAGE_PATH.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)
            except Exception as e:
                self.send_error(500, str(e))
            return
        # ===== 行情大图数据（多周期 K 线 + 指标 + 跨周期投票 + AI 判读）=====
        if self.path.startswith("/api/market_board"):
            try:
                payload = _build_market_board_payload()
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
        # ===== 老师直播单：GET 列表（支持分页 + 过滤 + 排序，2026-10-18） =====
        if self.path.startswith("/api/teacher/trades"):
            try:
                payload = build_status_teacher(self.path)
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
        # ===== 老师单分析（IMA 当数据库，2026-10-28） =====
        if self.path.startswith("/api/teacher/analysis"):
            try:
                payload = build_teacher_analysis_api()
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
        # ===== 行情剧震检测（事件触发，2026-10-28） =====
        if self.path.startswith("/api/market_shock"):
            try:
                payload = build_shock_state()
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
        # ===== 5 objective + Edge Score 历史趋势 (2026-10-17) =====
        # ===== 行情分析（prod 顶部 BTC 右侧，每 15min 刷新 + 手动） =====
        if self.path.startswith("/api/market_analysis"):
            try:
                payload = build_market_analysis()
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
        if self.path == "/api/metrics/history":
            try:
                sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
                from weekly_metrics_tracker import load_history
                history = load_history()
                body = json.dumps({"history": history}, ensure_ascii=False).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(body)
            except Exception as e:
                self.send_error(500, str(e))
            return
        # ===== 改造前后 baseline 对比 (2026-10-17) =====
        if self.path == "/api/metrics/baselines":
            try:
                sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
                from baseline_record import list_baselines
                baselines = list_baselines()
                body = json.dumps({"baselines": baselines}, ensure_ascii=False).encode()
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