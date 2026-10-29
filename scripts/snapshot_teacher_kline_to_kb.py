#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
snapshot_teacher_kline_to_kb.py
================================
liusir 10-20 决策：手动触发式脚本，把指定老师单入场前后±N 小时的多周期 K 线
（1m / 15m / 1h / 4h）打包成 4 个 markdown，上传到 IMA KB「BTC 交易策略 →
k 线数据」文件夹。

不在 cron / watch 路径里运行，必须由 liusir 口头触发后再执行。

核心约束（KB SOP 的 #1）：
- IMA KB 不支持 .json 直传，必须用 markdown 包裹（```json code 块）
- 必须先重读 SDK 知识（KB 文件夹可能重建）
- 重名检测 → 自动追加时间戳
- **KB 推土机不可逆**：10-20 测试时意外污染 6 份（test_30days_ago ×2 + _test_confirm_only ×4）
  → 默认行为改成「空周期直接跳过」，加 --force-include-empty 才传空 md。
- **必须 liusir 口头触发**：没听到指令不准跑（10-20 决策）
- 任何 API 错误立刻停止，不兜底

Usage:

    # 1) 按 trade_id（自动从 /root/data/teacher_trades.jsonl 读 entry 时戳）
    python3 snapshot_teacher_kline_to_kb.py --trade-id 8

    # 2) 按 trade_id + 自定义窗口（默认 ±2h）
    python3 snapshot_teacher_kline_to_kb.py --trade-id 8 --before-hours 4 --after-hours 4

    # 3) 按手动 epoch（不走 teacher_trades.jsonl）
    python3 snapshot_teacher_kline_to_kb.py --ts-epoch 1761701400 --label "liusir_2025-10-15_22-30"

    # 4) 只打包不上传（先在 /tmp 看 markdown 长什么样）
    python3 snapshot_teacher_kline_to_kb.py --trade-id 8 --dry-run

设计原则（liusir 10-20 决策）：
- 必须由 liusir 手动触发 → 没听到指令不准跑
- 默认 ±2h 是「覆盖入场 ±TP1 触发 ±主信号消失」的健康窗口
- K 线窗口增长是免费的（拉 /futures/usdt/candlesticks），
  唯一成本是 markdown 体积 — 100 根 1m + 30 根 15m + 5 根 1h + 5 根 4h ≈ 200KB md
"""
from __future__ import annotations
import argparse, json, os, re, subprocess, sys
from datetime import datetime, timezone, timedelta, time as _time_mod
import time
from pathlib import Path

# ---- 路径配置（与 btc-gate-intraday-strategy 项目同源）------------------
PROJ = Path("/root/skills/btc-gate-intraday-strategy")
SCRIPTS = PROJ / "scripts"
sys.path.insert(0, str(SCRIPTS))

import gate_fetch  # noqa: E402

# ---- KB 固定目标（liusir 10-20 自己建的文件夹）-------------------------
# ⚠️ 2026-10-23：IMA 把 KB id 格式改成了 `ima_kb_XXXXXXXX`（8 位字母数字），
#    老的 `Q8ymwg...=` 形式已失效（报 code=220004 invalid or outdated
#    knowledge_base_id）。新 ID 是 `ima_kb_AX5HhgtL`。
#    但 ID 还会再变，所以下面加了自动解析兜底：常量失效时按名称重新查。
KB_ID = "ima_kb_AX5HhgtL"
KB_NAME = "BTC 交易策略"        # 自动解析时按名称匹配
FOLDER_ID = "folder_7518163923109819"   # "k 线数据" — liusir 手动建
CLI = open(os.path.expanduser("~/.config/ima/client_id")).read().strip()
APIK = open(os.path.expanduser("~/.config/ima/api_key")).read().strip()
OPTS = json.dumps({"clientId": CLI, "apiKey": APIK})


def resolve_kb_id(fallback: str = KB_ID, name: str = KB_NAME) -> str:
    """
    KB id 会因平台改版失效（2026-10-23 就换过一次格式）。先试 fallback，
    失效就按名称重新查 get_addable_knowledge_base_list 拿新的。
    """
    def _call(api_path, body):
        cmd = ["node", "/root/.hermes/skills/ima-skill/ima_api.cjs", api_path,
               json.dumps(body), OPTS]
        try:
            p = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
            out = p.stdout.strip()
            if not out:
                return None
            return json.loads(out)
        except Exception:
            return None

    # 1) 直接试 fallback 是否有效
    r = _call("openapi/wiki/v1/get_knowledge_base", {"ids": [fallback]})
    if r and r.get("code") == 0:
        return fallback

    # 2) 按名称重新查
    r = _call("openapi/wiki/v1/get_addable_knowledge_base_list", {"limit": 50})
    if r and r.get("code") == 0:
        for kb in (r.get("data", {}) or {}).get("addable_knowledge_base_list", []):
            if kb.get("name") == name:
                print(f"[kb] 缓存的 KB id 已失效，自动解析到新 id: {kb['id']} ({name})")
                return kb["id"]
    return fallback

CST = timezone(timedelta(hours=8))
TEACHER_TRADES_PATH = Path("/root/data/teacher_trades.jsonl")

# 4 个核心周期，与日常 btc-trader-view 的口径一致
TFS = ["1m", "15m", "1h", "4h"]

# 各周期需要的根数（±2h）：1m=240+1=241 / 15m=8+1 / 1h=4+1 / 4h=4+1
TF_COUNTS = {"1m": 250, "15m": 16, "1h": 8, "4h": 8}

# ---- 工具：teacher_trades.jsonl 读取 -------------------------------------
def load_teacher_trade(trade_id: int) -> dict:
    if not TEACHER_TRADES_PATH.exists():
        raise FileNotFoundError(f"teacher_trades.jsonl 不存在: {TEACHER_TRADES_PATH}")
    with open(TEACHER_TRADES_PATH, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            d = json.loads(line)
            if int(d.get("trade_id", -1)) == trade_id:
                return d
    raise LookupError(f"trade_id={trade_id} 不在 {TEACHER_TRADES_PATH}")


def parse_event_ts_to_epoch(ts_str: str, fallback_date: str) -> int:
    """
    events[].ts 是粗略字符串（"2026-10-15 22:11" / "2026-10-15 (后续)"）。
    若能解析就转 epoch；解析失败用 fallback_date 的 00:00 CST 兜底。
    """
    ts_str = (ts_str or "").strip()
    # 试解 "2026-10-15 22:11"
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            dt = datetime.strptime(ts_str, fmt)
            return int(dt.replace(tzinfo=CST).timestamp())
        except ValueError:
            continue
    # 试 "2026-10-15"
    try:
        dt = datetime.strptime(ts_str, "%Y-%m-%d")
        return int(dt.replace(tzinfo=CST).timestamp())
    except ValueError:
        pass
    # 兜底：老师单常在 22:00 CST 入场，多数 ts 字符串 (近期)/(后续) 等无法解析，
    # 此时默认值不要取 0:00（那一天才开始的最早 1m cache 可能不够覆盖），取 22:00 更稳。
    dt = datetime.strptime(fallback_date.split(" ")[0], "%Y-%m-%d").replace(hour=22)
    return int(dt.replace(tzinfo=CST).timestamp())


def resolve_trade_window(trade_id: int, before_h: float, after_h: float) -> tuple[int, int, str]:
    """
    返回 (start_epoch, end_epoch, label)
    """
    t = load_teacher_trade(trade_id)
    entry_evt = next((e for e in t.get("events", []) if e.get("type") == "entry"), None)
    if not entry_evt:
        raise ValueError(f"trade_id={trade_id} 没有 type=entry 事件")
    ts_str = entry_evt.get("ts") or t.get("date") or ""
    base = parse_event_ts_to_epoch(ts_str, t.get("date", "2026-01-01"))
    label = f"teacher_{trade_id}_{datetime.fromtimestamp(base, CST).strftime('%Y%m%d_%H%M')}"
    return int(base - before_h * 3600), int(base + after_h * 3600), label


# ---- 工具：拉 K 线 + 裁窗口 -----------------------------------------------
def fetch_window(tf: str, start: int, end: int) -> list:
    """
    拉 tf 在 [start, end] 区间内的 K 线。

    关键：fetch_ohlcv 的滚动窗口只覆盖「最新 N 根」，对历史窗口（老师单入场时间
    经常 1+ 天前）需要走 _download_full 分页拉历史。end_ts 设为窗口末端，
    total 设为足够多，覆盖窗口长度 + 余量。
    """
    need = TF_COUNTS[tf]
    interval_sec = gate_fetch.INTERVAL_SEC[tf]
    # 窗口内的根数 + 前后各放 20% 余量
    window_len = int((end - start) / interval_sec)
    fetch_total = max(need, window_len + window_len // 5 + 10)
    try:
        # 先用 _download_full 直接拿历史段（不滚动到最新），end_ts 设为窗口末端
        # 这样能拉到 1+ 天前的 1m K 线，而 fetch_ohlcv 的滚动窗口会被新数据覆盖丢失
        raw = gate_fetch._download_full("BTC_USDT", tf, fetch_total, end_ts=end, verbose=False)
        bars = [raw[k] for k in sorted(raw)]
    except Exception as e:
        print(f"  [warn] _download_full 失败 ({e}),回退到 fetch_ohlcv 滚动模式")
        rows = gate_fetch.fetch_ohlcv("BTC_USDT", tf, need, cache=False, refresh=False, verbose=False)
        bars = [r for r in rows if start <= r[0] <= end]
        return bars
    return [r for r in bars if start <= r[0] <= end]


# ---- 工具：KB 流程（复用今天跑通的 /tmp/upload_kline_v2.py 范式）---------
def run_one_md(label: str, tf: str, bars: list, base_epoch: int, dry_run: bool) -> tuple[str, str]:
    """构造 1 个 tf 的 markdown，返回 (file_name, md_content)。dry_run=只构造不传。"""
    if not bars:
        days_ago = (int(datetime.now(CST).timestamp()) - base_epoch) / 86400
        # Gate.io 公共接口 1m 历史窗口实测至少 5+ 天（10-20 实测 teacher #8 在 4.6 天前仍拉到 241 根）。
        # 阈值降到 7 天：超过 7 天基本无解（gate 会 400 / empty array）。
        if days_ago > 7:
            reason = (
                f"⚠️ 超出 Gate.io 公共接口 1m 历史保留窗口（约 5-7 天）。"
                f"本脚本窗口在 {days_ago:.1f} 天前，"
                f"**没办法从 gate.io 公共接口补回** — 这一窗口的 K 线需要预先落盘的本地缓存。"
                f"如果当时有 watch.py 在跑，local `/root/data/` 下应该有缓存；"
                f"否则就只能跳过。"
            )
        else:
            reason = (
                "⚠️ 本地缓存无数据（窗口内可能没拉取到）。"
                "重跑前先 `python3 update_data.py --tfs {tf} --force` 强制刷新缓存；"
                "再不行就跑 `python3 gate_fetch.py fetch_ohlcv BTC_USDT {tf} 8000` 看 raw 返回。"
            )
        md = (
            f"# {label} — {tf} 窗口\n\n"
            f"- **入场基准时戳**: `{base_epoch}` "
            f"({datetime.fromtimestamp(base_epoch, CST).strftime('%Y-%m-%d %H:%M:%S')} CST)\n"
            f"- **距今天数**: {days_ago:.1f} 天\n"
            f"- **窗口根数**: 0\n"
            f"- **数据源**: /futures/usdt/candlesticks (gate.io USDT 永续)\n"
            f"- **抓取时间**: {datetime.now(CST).strftime('%Y-%m-%d %H:%M:%S')} CST\n\n"
            f"## 状态\n\n"
            f"{reason}\n"
        )
        title = f"{label} {tf}（无数据）"
    else:
        # 简洁表格 + 完整 JSON 块
        from datetime import datetime as _dt
        rows_md = []
        for b in bars:
            ts, o, h, l, c, v = b[0], b[1], b[2], b[3], b[4], b[5] if len(b) > 5 else 0
            dt_cst = _dt.fromtimestamp(ts, CST).strftime("%m-%d %H:%M")
            rows_md.append(f"| {dt_cst} | {o:.2f} | {h:.2f} | {l:.2f} | {c:.2f} | {v} |")
        table = "\n".join(rows_md)

        ts_first = bars[0][0]
        ts_last = bars[-1][0]
        dt_first = _dt.fromtimestamp(ts_first, CST).strftime("%Y-%m-%d %H:%M:%S")
        dt_last = _dt.fromtimestamp(ts_last, CST).strftime("%Y-%m-%d %H:%M:%S")

        md = (
            f"# {label} — {tf} 窗口\n\n"
            f"- **入场基准时戳**: `{base_epoch}` ({_dt.fromtimestamp(base_epoch, CST).strftime('%Y-%m-%d %H:%M:%S')} CST)\n"
            f"- **窗口根数**: {len(bars)}\n"
            f"- **首根**: {dt_first} CST\n"
            f"- **末根**: {dt_last} CST\n"
            f"- **数据源**: /futures/usdt/candlesticks (gate.io USDT 永续)\n"
            f"- **抓取时间**: {_dt.now(CST).strftime('%Y-%m-%d %H:%M:%S')} CST\n\n"
            f"## 时:开:高:低:收:量\n\n"
            f"| 时戳 (CST) | O | H | L | C | V |\n"
            f"|---|---|---|---|---|---|\n"
            f"{table}\n\n"
            f"## 原始 JSON\n\n"
            f"```json\n{json.dumps(bars, ensure_ascii=False, indent=1)}\n```\n"
        )
        title = f"{label} {tf}"

    file_name = f"{title}.md"
    if dry_run:
        out_path = f"/tmp/_dry_{file_name}"
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(md)
        print(f"[dry-run] {file_name} -> {out_path} ({len(md)} bytes)")
        return (file_name, md)
    return (file_name, md)


def upload_md_to_kb(file_name: str, md_content: str, base_epoch: int) -> str:
    """KB 流程：写 tmp → preflight → check_repeated_names → create_media → cos → add_knowledge"""
    SKILL_DIR = "/root/.hermes/skills/ima-skill"
    tmp = "/tmp/_snap.md"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(md_content)

    # ★ 2026-10-23：KB id 平台改版会失效（老格式 Q8ymwg...= 已报 code=220004）。
    #   每次上传前解析一次，失效就按名称自动取新的，避免脚本静默写错地方。
    kb_id = resolve_kb_id()

    # preflight
    r = subprocess.run(
        ["node", f"{SKILL_DIR}/knowledge-base/scripts/preflight-check.cjs", "--file", tmp],
        capture_output=True, text=True
    )
    if r.returncode != 0:
        raise RuntimeError(f"preflight failed: {r.stderr}")
    meta = json.loads(r.stdout)
    if not meta.get("pass"):
        raise RuntimeError(f"preflight pass=false: {meta}")
    file_size = int(meta["file_size"])
    content_type = meta["content_type"]
    media_type = int(meta["media_type"])

    # check_repeated_names
    chk = {"params":[{"name":file_name,"media_type":media_type}],
           "knowledge_base_id":kb_id,"folder_id":FOLDER_ID}
    r = subprocess.run(["node", f"{SKILL_DIR}/ima_api.cjs",
                        "openapi/wiki/v1/check_repeated_names",
                        json.dumps(chk), OPTS], capture_output=True, text=True)
    chk_resp = json.loads(r.stdout)
    if chk_resp.get("data", {}).get("results", [{}])[0].get("is_repeated"):
        # append timestamp
        ts = datetime.now().strftime("%Y%m%d%H%M%S")
        file_name = file_name.replace(".md", f"_{ts}.md")
        print(f"  → 重名，自动追加时间戳: {file_name}")

    # create_media
    cr = {"file_name":file_name,"file_size":file_size,"content_type":content_type,
          "knowledge_base_id":kb_id,"file_ext":"md","folder_id":FOLDER_ID,"media_type":media_type}
    r = subprocess.run(["node", f"{SKILL_DIR}/ima_api.cjs",
                        "openapi/wiki/v1/create_media",
                        json.dumps(cr), OPTS], capture_output=True, text=True)
    cr_resp = json.loads(r.stdout)
    if cr_resp.get("code") != 0:
        raise RuntimeError(f"create_media 失败: {cr_resp}")
    data = cr_resp["data"]
    media_id = data["media_id"]
    cred = data["cos_credential"]

    # COS upload
    upload_cmd = [
        "node", f"{SKILL_DIR}/knowledge-base/scripts/cos-upload.cjs",
        "--file", tmp,
        "--secret-id", cred["secret_id"],
        "--secret-key", cred["secret_key"],
        "--token", cred["token"],
        "--bucket", cred["bucket_name"],
        "--region", cred["region"],
        "--cos-key", cred["cos_key"],
        "--content-type", content_type,
        "--start-time", cred["start_time"],
        "--expired-time", cred["expired_time"],
        "--timeout", "600000",
    ]
    r = subprocess.run(upload_cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"cos upload 失败: {r.stderr or r.stdout}")

    # add_knowledge
    add = {"media_type":media_type,"media_id":media_id,"title":file_name,
           "knowledge_base_id":kb_id,"folder_id":FOLDER_ID,
           "file_info":{"cos_key":cred["cos_key"],"file_size":file_size,"file_name":file_name}}
    r = subprocess.run(["node", f"{SKILL_DIR}/ima_api.cjs",
                        "openapi/wiki/v1/add_knowledge",
                        json.dumps(add), OPTS], capture_output=True, text=True)
    add_resp = json.loads(r.stdout)
    if add_resp.get("code") != 0:
        raise RuntimeError(f"add_knowledge 失败: {add_resp}")
    return file_name


# ---- 单文件归档模式（2026-10-29 liusir 需求）-------------------------------
# 旧结构：每笔老师单 4 份 md（4 周期各一份）→ 散、只覆盖 4 笔。
# 新结构：每笔老师单 **1 份 md**，内部按 1m/15m/1h/4h 分区；拿不到的周期
# **明确标注不可得原因**，不静默省略。文件名 `老师单K线_#{id}_{date}.md`。
_TS_RE = re.compile(r"(\d{4}-\d{2}-\d{2})(?:[ T~](\d{2}:\d{2}))?")


def _parse_ts_loose(ts_str: str) -> int | None:
    """events[].ts 是粗略字符串（"2026-10-27 22:05 左右 (liusir 口径)" /
    "2026-10-09 ~21:50" / "2026-10-16 ~17:30 左右" / "2026-10-19 (近期)"），
    正则抽出日期+分钟再转 epoch；抽不出返回 None（绝不猜）。

    ★ 注意：只抽到日期没抽到时刻时，用**该 trade 的已知最早时刻**兜底会错得很远
      （#4/#5 的 entry ts 只有日期 "2026-10-09" → 00:00，窗口整体错 22 小时）。
      所以这里返回 None，由调用方决定兜底策略。
    """
    if not ts_str:
        return None
    m = _TS_RE.search(str(ts_str))
    if not m:
        return None
    day = m.group(1)
    hm = m.group(2)
    if not hm:
        return None                      # 只有日期 → 不猜时刻
    try:
        return int(datetime.strptime(f"{day} {hm}", "%Y-%m-%d %H:%M").replace(tzinfo=CST).timestamp())
    except ValueError:
        return None


def _parse_date_only(ts_str: str) -> str | None:
    """只取日期部分（"2026-10-09 (持仓中)" → "2026-10-09"）"""
    if not ts_str:
        return None
    m = _TS_RE.search(str(ts_str))
    return m.group(1) if m else None


def _resolve_entry_epoch(trade: dict) -> int:
    """定位老师单入场时刻。

    优先级：entry 事件 ts 里的完整时刻 → entry ts 的日期 + events 里最接近的
    时刻 → trade.date 的时刻 → 该日期的 22:00（老师多在 22 点前后进场）。
    全部推不出来才用 22:00 兜底，且**必须在 data_quality 里留痕**。
    """
    t = trade
    entry_evt = next((e for e in t.get("events", []) if e.get("type") == "entry"), None)
    raw = (entry_evt.get("ts") if entry_evt else "") or t.get("date") or ""
    ep = _parse_ts_loose(raw)
    if ep is not None:
        return ep
    day = _parse_date_only(raw) or _parse_date_only(t.get("date") or "")
    if not day:
        day = "2026-01-01"
    # 从所有 events 里找同一天的另一个完整时刻（比 00:00 靠谱得多）
    cands = []
    for e in t.get("events", []):
        x = _parse_ts_loose(e.get("ts") or "")
        if x is not None and _parse_date_only(e.get("ts") or "") == day:
            cands.append(x)
    if cands:
        return min(cands)
    return int(datetime.strptime(day, "%Y-%m-%d").replace(hour=22, tzinfo=CST).timestamp())


def resolve_trade_window_full(trade_id: int, before_h: float, after_h: float):
    """单文件归档窗口 = 入场前 before_h → **真实出场后 after_h**（不是入场后 after_h）。

    老师单经常跨夜（#15 入场 22:05、次日 05:31 才平），只按入场 ±2h 会把出场那段
    K 线切掉。open 单的右边界取 now（窗口必须盖到现在，见 skill「窗口右边界」）。
    返回 (start, end, label, trade_dict)
    """
    t = load_teacher_trade(trade_id)
    if not any(e.get("type") == "entry" for e in t.get("events", [])):
        raise ValueError(f"trade_id={trade_id} 没有 type=entry 事件")
    base = _resolve_entry_epoch(t)

    res = t.get("result") or {}
    now = int(datetime.now(CST).timestamp())
    # 出场锚点：优先 remaining_close / close_sl / tp2_full 等真实出场事件
    CLOSE_TYPES = {"remaining_close", "close_sl", "tp2_full", "sl_breakeven"}
    close_ts = [x for x in (_parse_ts_loose(e.get("ts")) for e in t.get("events", [])
                            if e.get("type") in CLOSE_TYPES) if x]
    if res.get("open") is True:
        end_ts = now                       # 未平 → 窗口盖到现在
    elif close_ts:
        end_ts = max(close_ts)
    else:
        ev_ts = [x for x in (_parse_ts_loose(e.get("ts")) for e in t.get("events", [])) if x]
        end_ts = max(max(ev_ts), base) if ev_ts else base
    label = f"teacher_{trade_id}_{datetime.fromtimestamp(base, CST).strftime('%Y%m%d_%H%M')}"
    return int(base - before_h * 3600), int(end_ts + after_h * 3600), label, t


def build_combined_md(trade: dict, bars_by_tf: dict, label: str,
                      start: int, end: int, unavailable: dict) -> tuple[str, str]:
    """把一笔老师单的 4 个周期 K 线 + 交易事实打成**一份** markdown。"""
    _dt = datetime
    tid = trade.get("trade_id")
    date = trade.get("date", "")
    side_cn = "做多" if trade.get("side") == "long" else "做空"
    res = trade.get("result") or {}

    def _fmt(x, nd=1):
        return "—" if x is None else f"{float(x):,.{nd}f}"

    # ★ 减仓比例一律取 events[].qty_pct —— 顶层 tp1_pct 字段在老单里存的是
    #   **涨幅百分比**（如 #4 的 0.72 = TP1 涨幅 0.72%），不是减仓比例。
    evs = trade.get("events") or []
    tp1_qty_pct = None
    tp1_move_pct = trade.get("tp1_pct")
    _e1 = next((e for e in evs if e.get("type") == "tp1_partial"), None)
    if _e1 is not None and isinstance(_e1.get("qty_pct"), (int, float)):
        tp1_qty_pct = _e1.get("qty_pct")
    sl_after = trade.get("sl_after_tp1")
    if sl_after is None:
        _e = next((e for e in evs if e.get("type") == "breakeven_stop"), None)
        sl_after = _e.get("px") if _e else None
    n_contracts = trade.get("size_contracts")
    if n_contracts is None and trade.get("entry_zone") and trade.get("notional_usd"):
        try:
            n_contracts = round(float(trade["notional_usd"]) / (float(trade["entry_zone"]) * 0.0001))
        except (TypeError, ValueError):
            n_contracts = None

    head = [
        f"# 老师单K线 #{tid} {date}（{trade.get('contract','BTC_USDT')} {side_cn}）",
        "",
        "> **单文件归档**：本笔老师单的多周期 K 线集中在这一份 md 里（不再按周期拆文件）。",
        f"> 生成时间：{_dt.now(CST).strftime('%Y-%m-%d %H:%M:%S')} CST ｜ "
        "数据源：gate.io `/futures/usdt/candlesticks`（USDT 永续）",
        "",
        "## 一、交易事实",
        "",
        f"- **trade_id**: {tid} ｜ **日期**: {date}",
        f"- **方向**: {side_cn} ｜ **入场**: {_fmt(trade.get('entry_zone'))}",
        f"- **TP1**: {_fmt(trade.get('tp1'))}"
        + (f"（减 {tp1_qty_pct}%）" if trade.get("tp1") and tp1_qty_pct is not None else "")
        + (f"［涨幅 {tp1_move_pct}%］" if tp1_move_pct is not None else ""),
        f"- **TP2**: {_fmt(trade.get('tp2'))}",
        f"- **初始止损**: {_fmt(trade.get('sl_initial'))}"
        + ("（liusir 未给，不编造）" if trade.get("sl_initial") is None else ""),
        f"- **TP1 后止损**: {_fmt(sl_after)}"
        + ("（保本=入场价）" if sl_after is not None else "（未记录）"),
        f"- **名义/杠杆/张数**: {trade.get('notional_usd','—')}U / {trade.get('leverage','—')}x / {n_contracts or '—'} 张",
        f"- **结果**: {res.get('outcome','—')}"
        + (f" ｜ **净盈亏**: {res.get('net_usd')}U" if res.get("net_usd") is not None else
           f" ｜ **已实现部分**: +{res.get('net_usd_realized_partial', 0)}U（未结算，net_usd 留 null）"),
        "",
        "### 事件时间线",
        "",
        "| 时间 (CST) | 类型 | 价格 | 数量 | 说明 |",
        "|---|---|---|---|---|",
    ]
    for e in (trade.get("events") or []):
        note = (e.get("note") or "").replace("\n", " ").replace("|", "｜")
        if len(note) > 120:
            note = note[:120] + "…"
        head.append(
            f"| {e.get('ts') or '（未触发）'} | {e.get('type','—')} | {_fmt(e.get('px'))} "
            f"| {e.get('qty_pct','—')} | {note} |"
        )
    head += ["", f"### 数据质量 / 口径", "", "```text", (trade.get("data_quality") or "（无）"), "```", ""]

    head += [
        "## 二、K 线窗口",
        "",
        f"- **窗口**: {_dt.fromtimestamp(start, CST).strftime('%Y-%m-%d %H:%M')} ~ "
        f"{_dt.fromtimestamp(end, CST).strftime('%Y-%m-%d %H:%M')} CST"
        f"（左边界=入场前 padding ｜ 右边界="
        f"{'到现在（本单未平）' if res.get('open') is True else '真实出场后 padding'}）",
        f"- **各周期根数**: " + " / ".join(
            f"{tf} {len(bars_by_tf.get(tf) or [])} 根" for tf in TFS),
        "",
    ]

    body = []
    for tf in TFS:
        bars = bars_by_tf.get(tf) or []
        body.append("")
        body.append(f"## {tf}（{_TF_CN.get(tf, tf)}）")
        body.append("")
        if not bars:
            why = unavailable.get(tf, "窗口内无数据")
            body.append(f"⚠️ **{tf} 数据不可得** —— {why}")
            body.append("")
            body.append(f"→ 不静默省略：这一周期在本笔窗口内没有可用 K 线，"
                        f"其余周期照常收录，需要 {tf} 粒度时用相邻周期兜底。")
            body.append("")
            continue
        body.append(f"- 根数 **{len(bars)}** ｜ 首根 `{_dt.fromtimestamp(bars[0][0], CST).strftime('%Y-%m-%d %H:%M:%S')}`"
                    f" ｜ 末根 `{_dt.fromtimestamp(bars[-1][0], CST).strftime('%Y-%m-%d %H:%M:%S')}` CST")
        body.append("")
        body.append("| 时戳 (CST) | O | H | L | C | V |")
        body.append("|---|---|---|---|---|---|")
        for b in bars:
            ts, o, h, l, c = b[0], b[1], b[2], b[3], b[4]
            v = b[5] if len(b) > 5 else 0
            body.append(f"| {_dt.fromtimestamp(ts, CST).strftime('%m-%d %H:%M')} "
                        f"| {o:.2f} | {h:.2f} | {l:.2f} | {c:.2f} | {v} |")
        body.append("")
        body.append("### 原始 JSON")
        body.append("")
        body.append("```json")
        body.append(json.dumps(bars, ensure_ascii=False, indent=1))
        body.append("```")

    md = "\n".join(head + body) + "\n"
    file_name = f"老师单K线_#{tid}_{date.split(' ')[0]}.md"
    return file_name, md


_TF_CN = {"1m": "1 分钟", "15m": "15 分钟", "1h": "1 小时", "4h": "4 小时"}


def build_all_md(trades: list, before_h: float, after_h: float) -> tuple[str, str]:
    """**全部老师单合成一份 md**（liusir 2026-10-29 要求）。

    结构：总览表 → 每笔一节（交易事实 / 事件时间线 / 数据质量 / 各周期 K 线）
    拿不到的周期明确标注不可得原因，不静默省略。
    """
    _dt = datetime
    now_s = _dt.now(CST).strftime("%Y-%m-%d %H:%M:%S")

    # ── 先算每笔的窗口 + 取数 ────────────────────────────────────────
    sections = []
    for t in trades:
        tid = t.get("trade_id")
        try:
            start, end, label, tr = resolve_trade_window_full(tid, before_h, after_h)
        except Exception as e:
            print(f"  ⚠️ #{tid} 跳过：{e}")
            continue
        bars_by_tf, unavailable = {}, {}
        for tf in TFS:
            bars = fetch_window(tf, start, end)
            if bars:
                bars_by_tf[tf] = bars
                continue
            days_ago = (int(_dt.now(CST).timestamp()) - start) / 86400
            if tf == "1m" and days_ago > 7:
                unavailable[tf] = (
                    f"本笔窗口在 {days_ago:.1f} 天前，已超出 Gate.io 公共接口约 7 天历史保留窗口；"
                    f"本地 1m 缓存只覆盖最近约 5 天（10-24 起）→ **1m 拉不回来**。"
                    f"用 15m 兜底（15m 本地 cache 覆盖 08-07 起，全 16 笔可得）。"
                )
            else:
                unavailable[tf] = f"窗口内无数据（{days_ago:.1f} 天前，缓存缺失或超出保留窗口）"
        sections.append((tr, start, end, bars_by_tf, unavailable))

    # ── 总览表 ───────────────────────────────────────────────────────
    settled = [t for t in trades if (t.get("result") or {}).get("net_usd") is not None]
    pending = [t for t in trades if (t.get("result") or {}).get("net_usd") is None]
    net_total = sum((t.get("result") or {}).get("net_usd") or 0 for t in settled)
    wins = sum(1 for t in settled if ((t.get("result") or {}).get("net_usd") or 0) > 0)

    head = [
        "# 老师单 K 线归档（全部老师单 · 单文件）",
        "",
        f"> **单文件归档**：全部 {len(trades)} 笔老师单的多周期 K 线集中在这一份 md 里。",
        f"> 生成时间：{now_s} CST ｜ 数据源：gate.io `/futures/usdt/candlesticks`（USDT 永续）",
        f"> 窗口规则：每笔取 **入场前 {before_h:g}h → 真实出场后 {after_h:g}h**"
        f"（未平的单盖到现在）；拿不到的周期**明确标注原因**，不静默省略。",
        "",
        "## 总览",
        "",
        f"- **总单数**: {len(trades)} ｜ **已结算**: {len(settled)} ｜ **持仓中**: {len(pending)}",
        f"- **累计净利**: {net_total:+.2f}U ｜ **胜率**: {wins}/{len(settled)}"
        + (f"（{wins/len(settled)*100:.1f}%）" if settled else ""),
        "",
        "| # | 日期 | 方向 | 入场 | TP1 | TP2 | 结果 | 净盈亏 | 1m | 15m | 1h | 4h |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for tr, start, end, bbt, unav in sections:
        tid = tr.get("trade_id")
        res = tr.get("result") or {}
        side = "多" if tr.get("side") == "long" else "空"
        n = res.get("net_usd")
        net_s = f"{n:+.2f}U" if n is not None else "持仓中"
        cnt = {tf: len(bbt.get(tf) or []) for tf in TFS}
        cells = []
        for tf in TFS:
            cells.append(f"{cnt[tf]}" if cnt[tf] else "—")
        head.append(
            f"| {tid} | {tr.get('date','')} | {side} | {tr.get('entry_zone','')} "
            f"| {tr.get('tp1') or '—'} | {tr.get('tp2') or '—'} "
            f"| {res.get('outcome','—')} | {net_s} | " + " | ".join(cells) + " |"
        )
    head += ["", "> 「—」= 该周期在本笔窗口内不可得，原因见该笔的对应分区。", ""]

    # ── 每笔一节 ─────────────────────────────────────────────────────
    body = []
    for tr, start, end, bbt, unav in sections:
        tid = tr.get("trade_id")
        res = tr.get("result") or {}
        evs = tr.get("events") or []
        side_cn = "做多" if tr.get("side") == "long" else "做空"

        # ★ 减仓比例一律取 events[].qty_pct —— 顶层 tp1_pct 字段在老单里存的是
        #   **涨幅百分比**（如 #4 的 0.72 = TP1 涨幅 0.72%），不是减仓比例。
        #   两者混用会输出「减 0.72%」这种错值。涨幅另列，语义分开。
        tp1_qty_pct = None
        tp1_move_pct = tr.get("tp1_pct")
        e1 = next((x for x in evs if x.get("type") == "tp1_partial"), None)
        if e1 is not None and isinstance(e1.get("qty_pct"), (int, float)):
            tp1_qty_pct = e1.get("qty_pct")
        sl_after = tr.get("sl_after_tp1")
        if sl_after is None:
            e = next((x for x in evs if x.get("type") == "breakeven_stop"), None)
            sl_after = e.get("px") if e else None
        n_contracts = tr.get("size_contracts")
        if n_contracts is None and tr.get("entry_zone") and tr.get("notional_usd"):
            try:
                n_contracts = round(float(tr["notional_usd"]) / (float(tr["entry_zone"]) * 0.0001))
            except (TypeError, ValueError):
                n_contracts = None       # 区间入场（如 '82400-82600'）算不出张数，不臆造

        def _f(x, nd=1):
            if x is None:
                return "—"
            try:
                return f"{float(x):,.{nd}f}"
            except (TypeError, ValueError):
                return str(x)          # 区间入场（'82400-82600'）原样显示，不编造

        body.append("")
        body.append("---")
        body.append("")
        body.append(f"## 老师单 #{tid} ｜ {tr.get('date','')} ｜ {side_cn}")
        body.append("")
        body.append(f"- **入场**: {_f(tr.get('entry_zone'))}"
                    f" ｜ **TP1**: {_f(tr.get('tp1'))}"
                    + (f"（减 {tp1_qty_pct}%）" if tr.get("tp1") and tp1_qty_pct is not None else "")
                    + (f"［涨幅 {tp1_move_pct}%］" if tp1_move_pct is not None else "")
                    + f" ｜ **TP2**: {_f(tr.get('tp2'))}")
        body.append(f"- **初始止损**: {_f(tr.get('sl_initial'))}"
                    + ("（liusir 未给，不编造）" if tr.get("sl_initial") is None else "")
                    + f" ｜ **TP1 后止损**: {_f(sl_after)}"
                    + ("（保本=入场价）" if sl_after is not None else "（未记录）"))
        body.append(f"- **名义/杠杆/张数**: {tr.get('notional_usd','—')}U / {tr.get('leverage','—')}x / {n_contracts or '—'} 张")
        body.append(f"- **结果**: {res.get('outcome','—')}"
                    + (f" ｜ **净盈亏**: {res.get('net_usd')}U" if res.get("net_usd") is not None
                       else f" ｜ **已实现部分**: +{res.get('net_usd_realized_partial', 0)}U（未结算，net_usd 留 null）"))
        body.append(f"- **K 线窗口**: {_dt.fromtimestamp(start, CST).strftime('%Y-%m-%d %H:%M')} ~ "
                    f"{_dt.fromtimestamp(end, CST).strftime('%Y-%m-%d %H:%M')} CST"
                    f"（右边界={'到现在（本单未平）' if res.get('open') is True else '真实出场后'}）")
        body.append("")
        body.append("**事件时间线**")
        body.append("")
        body.append("| 时间 (CST) | 类型 | 价格 | 数量 | 说明 |")
        body.append("|---|---|---|---|---|")
        for e in evs:
            note = (e.get("note") or "").replace("\n", " ").replace("|", "｜")
            if len(note) > 110:
                note = note[:110] + "…"
            body.append(f"| {e.get('ts') or '（未触发）'} | {e.get('type','—')} | {_f(e.get('px'))} "
                        f"| {e.get('qty_pct','—')} | {note} |")
        body.append("")
        body.append("<details><summary>数据质量 / 口径（点击展开）</summary>")
        body.append("")
        body.append("```text")
        body.append(tr.get("data_quality") or "（无）")
        body.append("```")
        body.append("")
        body.append("</details>")
        body.append("")
        for tf in TFS:
            bars = bbt.get(tf) or []
            body.append(f"### {tf}（{_TF_CN.get(tf, tf)}）")
            body.append("")
            if not bars:
                body.append(f"⚠️ **{tf} 数据不可得** —— {unav.get(tf, '窗口内无数据')}")
                body.append("")
                body.append(f"> 不静默省略：这一周期在本笔窗口内没有可用 K 线，"
                            f"其余周期照常收录，需要 {tf} 粒度时用相邻周期兜底。")
                body.append("")
                continue
            body.append(f"根数 **{len(bars)}** ｜ 首根 `{_dt.fromtimestamp(bars[0][0], CST).strftime('%Y-%m-%d %H:%M:%S')}`"
                        f" ｜ 末根 `{_dt.fromtimestamp(bars[-1][0], CST).strftime('%Y-%m-%d %H:%M:%S')}` CST")
            body.append("")
            body.append("| 时戳 (CST) | O | H | L | C | V |")
            body.append("|---|---|---|---|---|---|")
            for b in bars:
                v = b[5] if len(b) > 5 else 0
                body.append(f"| {_dt.fromtimestamp(b[0], CST).strftime('%m-%d %H:%M')} "
                            f"| {b[1]:.2f} | {b[2]:.2f} | {b[3]:.2f} | {b[4]:.2f} | {v} |")
            body.append("")
            body.append("<details><summary>原始 JSON</summary>")
            body.append("")
            body.append("```json")
            body.append(json.dumps(bars, ensure_ascii=False, indent=1))
            body.append("```")
            body.append("")
            body.append("</details>")
            body.append("")

    md = "\n".join(head + body) + "\n"
    file_name = f"老师单K线_全部{len(sections)}笔_{_dt.now(CST).strftime('%Y%m%d')}.md"
    return file_name, md


# ---- 主流程 ---------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser(description="手动把老师单入场窗口的 K 线推到 IMA KB")
    ap.add_argument("--trade-id", type=int, help="老师单 ID（从 teacher_trades.jsonl 读取）")
    ap.add_argument("--ts-epoch", type=int, help="自定义入场 epoch（不用 trade_id 时填）")
    ap.add_argument("--label", type=str, help="--ts-epoch 时的标签，如 liusir_2025-10-15_22-30")
    ap.add_argument("--before-hours", type=float, default=2.0, help="入场前抓多少小时，默认 2")
    ap.add_argument("--after-hours",  type=float, default=2.0, help="入场后抓多少小时，默认 2")
    ap.add_argument("--tfs", type=str, default=",".join(TFS), help=f"要传的周期，默认 {TFS}")
    ap.add_argument("--dry-run", action="store_true", help="只构造 markdown 不上传，预览用")
    # 2026-10-29 liusir 需求：每笔老师单只写**一份** md（含多周期分区），
    # 不再每周期拆一份。拿不到的周期明确标注不可得原因，不静默省略。
    ap.add_argument("--combined", action="store_true",
                    help="单文件归档模式：一笔老师单 = 一份 md（1m/15m/1h/4h 分区）")
    ap.add_argument("--all", action="store_true",
                    help="全部老师单合成**一份** md（--combined 的多笔版，liusir 2026-10-29 要求）")
    # 2026-10-20 liusir 决策:KB 永久污染实测 6 份（test_30days_ago ×2 + _test_confirm_only ×4），
    # 后续**默认行为**改成"空周期直接跳过"——加 --force-include-empty 反向开关。
    ap.add_argument("--force-include-empty", action="store_true",
                    help="把空周期也传成占位 md（默认跳过；KB 推土机不可逆）")
    ap.add_argument("--require-confirm", type=int, default=0, metavar="SECS",
                    help="真传前打印清单等 N 秒（防误传；0=不等）")
    args = ap.parse_args()

    if not args.trade_id and not args.ts_epoch and not args.all:
        ap.error("必须填 --trade-id 或 --ts-epoch 其中之一（或用 --all 归档全部老师单）")

    # ── 全部老师单合成一份 md（liusir 2026-10-29）────────────────────
    if args.all:
        trades = []
        with open(TEACHER_TRADES_PATH, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    trades.append(json.loads(line))
        trades.sort(key=lambda t: int(t.get("trade_id") or 0))
        print(f">>> 全部老师单合成单文件归档：{len(trades)} 笔")
        file_name, md = build_all_md(trades, args.before_hours, args.after_hours)
        nb = len(md.encode("utf-8"))
        out_path = f"/tmp/_dry_{file_name}"
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(md)
        print(f"  文件名 : {file_name}")
        print(f"  体积   : {nb:,} bytes ({nb/1024/1024:.2f} MB) — IMA 单文件上限 50MB")
        print(f"  -> {out_path}")
        if args.dry_run:
            print("\n[dry-run] 未上传 KB（KB 推土机不可逆）。确认后去掉 --dry-run 真传。")
            return 0
        real_name = upload_md_to_kb(file_name, md, int(datetime.now(CST).timestamp()))
        print(f"\n=== 已上传 ===\n  ✓ {real_name}")
        return 0

    # ── 单文件归档模式（2026-10-29）───────────────────────────────────
    if args.combined:
        if not args.trade_id:
            ap.error("--combined 需要 --trade-id（按笔归档）")
        start, end, label, trade = resolve_trade_window_full(
            args.trade_id, args.before_hours, args.after_hours)
        print(f">>> 单文件归档 trade #{args.trade_id} ｜ 窗口 "
              f"{datetime.fromtimestamp(start, CST).strftime('%m-%d %H:%M')} ~ "
              f"{datetime.fromtimestamp(end, CST).strftime('%m-%d %H:%M')} CST")
        bars_by_tf: dict = {}
        unavailable: dict = {}
        for tf in TFS:
            bars = fetch_window(tf, start, end)
            if bars:
                bars_by_tf[tf] = bars
                continue
            days_ago = (int(datetime.now(CST).timestamp()) - start) / 86400
            if tf == "1m" and days_ago > 7:
                unavailable[tf] = (
                    f"本笔窗口在 {days_ago:.1f} 天前，已超出 Gate.io 公共接口约 7 天历史保留窗口；"
                    f"本地 1m 缓存只覆盖最近约 5 天（10-24 起）→ **1m 拉不回来**。"
                    f"用 15m 兜底（15m 本地 cache 覆盖 08-07 起，全 15 笔可得）。"
                )
            else:
                unavailable[tf] = f"窗口内无数据（{days_ago:.1f} 天前，缓存缺失或超出保留窗口）"
        file_name, md = build_combined_md(trade, bars_by_tf, label, start, end, unavailable)
        nb = len(md.encode("utf-8"))
        out_path = f"/tmp/_dry_{file_name}"
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(md)
        print(f"  文件名 : {file_name}")
        print(f"  体积   : {nb:,} bytes ({nb/1024:.1f} KB)")
        for tf in TFS:
            n = len(bars_by_tf.get(tf) or [])
            print(f"    {tf:>4} : {f'{n} 根' if n else '⚠️ 不可得 — ' + unavailable.get(tf, '')[:60]}")
        print(f"  -> {out_path}")
        if args.dry_run:
            print("\n[dry-run] 未上传 KB（KB 推土机不可逆）。确认后去掉 --dry-run 真传。")
            return 0
        real_name = upload_md_to_kb(file_name, md, start)
        print(f"\n=== 已上传 ===\n  ✓ {real_name}")
        return 0

    if args.trade_id:
        start, end, label = resolve_trade_window(args.trade_id, args.before_hours, args.after_hours)
        # end 用 trade_id 自动算的，不需要再传 --label
    else:
        start = int(args.ts_epoch - args.before_hours * 3600)
        end = int(args.ts_epoch + args.after_hours * 3600)
        label = args.label or f"manual_{args.ts_epoch}"

    # 同步一下数据（force=False，但会增量拉）
    print(f">>> 同步 K 线缓存({args.tfs})...")
    gate_fetch.fetch_ohlcv("BTC_USDT", "1m", TF_COUNTS["1m"], cache=True, refresh=True, verbose=False)
    gate_fetch.fetch_ohlcv("BTC_USDT", "15m", TF_COUNTS["15m"], cache=True, refresh=True, verbose=False)
    gate_fetch.fetch_ohlcv("BTC_USDT", "1h", TF_COUNTS["1h"], cache=True, refresh=True, verbose=False)
    gate_fetch.fetch_ohlcv("BTC_USDT", "4h", TF_COUNTS["4h"], cache=True, refresh=True, verbose=False)

    results = []
    # 真传前先扫一遍所有 tf,把「会传」的清单摆出来，等 --require-confirm 秒（防 10-20 误传 test_30days_ago 那种 KB 永久污染）
    if not args.dry_run and args.require_confirm > 0:
        print(f"\n[confirm] 将扫描以下 tf，列出真实可传条目（KB 推土机不可逆 — 请认真看）：")
        print(f"  - trade_id={args.trade_id or '-'} ts={args.ts_epoch or '-'} label={label}")
        print(f"  - 窗口: {datetime.fromtimestamp(start, CST).strftime('%Y-%m-%d %H:%M')} ~ {datetime.fromtimestamp(end, CST).strftime('%Y-%m-%d %H:%M')} CST (±{args.before_hours}h/+{args.after_hours}h)")
        plan = []
        for tf in args.tfs.split(","):
            tf = tf.strip()
            if tf not in TF_COUNTS:
                continue
            tmp_bars = fetch_window(tf, start, end)
            if not tmp_bars and not args.force_include_empty:
                plan.append(("SKIP", tf, 0))
            elif not tmp_bars:
                plan.append(("WARN", tf, 0))   # 空但有 --force-include-empty，照传
            else:
                plan.append(("UPLOAD", tf, len(tmp_bars)))
        for action, tf, n in plan:
            print(f"  [{action}] {tf:>4}  根数 {n}")
        up_count = sum(1 for a,_,_ in plan if a == "UPLOAD")
        warn_count = sum(1 for a,_,_ in plan if a == "WARN")
        print(f"\n小计：{up_count} 份 UPLOAD  +  {warn_count} 份 WARN(空但照传)")
        print(f"等待 {args.require_confirm} 秒后开始上传... Ctrl-C 中断")
        time.sleep(args.require_confirm)

    for tf in args.tfs.split(","):
        tf = tf.strip()
        if tf not in TF_COUNTS:
            print(f"⚠️ 跳过不支持的周期 {tf}")
            continue
        bars = fetch_window(tf, start, end)
        # 默认行为（liusir 10-20 决策）：空周期直接跳过，防 KB 永久污染。
        # 真要把空周期写进 KB 时显式加 --force-include-empty。
        if not bars and not args.force_include_empty and not args.dry_run:
            print(f"  ⏭️  {tf} 窗口 0 根（超 Gate 窗口或缓存缺失），默认跳过")
            print(f"      如确需上传这份空占位 md,加 --force-include-empty")
            results.append(("SKIP", f"{tf} (empty)"))
            continue
        file_name, md = run_one_md(label, tf, bars, args.ts_epoch or (start + int((end - start) / 2)), args.dry_run)
        if args.dry_run:
            results.append(("DRY", file_name))
            continue
        try:
            real_name = upload_md_to_kb(file_name, md, args.ts_epoch or start)
            results.append(("OK", real_name))
            print(f"  ✓ {real_name}")
        except Exception as e:
            results.append(("FAIL", f"{file_name}: {e}"))
            print(f"  ✗ {file_name}: {e}")

    print(f"\n=== 汇总 ===")
    for s, n in results:
        print(f"  [{s}] {n}")

    # dry-run 模式额外提示
    if args.dry_run:
        print(f"\n[dry-run] markdown 写在 /tmp/_dry_*.md；")
        print(f"  确认 OK 后去掉 --dry-run 真传。")
        print(f"  默认行为是：能拉到 K 线的 tf 上传，拉不到的（空周期）自动跳过。")
        print(f"  若要把空周期也传成占位 md（KB 永久污染风险），加 --force-include-empty。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
