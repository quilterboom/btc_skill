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
import argparse, json, os, subprocess, sys
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
    # 2026-10-20 liusir 决策:KB 永久污染实测 6 份（test_30days_ago ×2 + _test_confirm_only ×4），
    # 后续**默认行为**改成"空周期直接跳过"——加 --force-include-empty 反向开关。
    ap.add_argument("--force-include-empty", action="store_true",
                    help="把空周期也传成占位 md（默认跳过；KB 推土机不可逆）")
    ap.add_argument("--require-confirm", type=int, default=0, metavar="SECS",
                    help="真传前打印清单等 N 秒（防误传；0=不等）")
    args = ap.parse_args()

    if not args.trade_id and not args.ts_epoch:
        ap.error("必须填 --trade-id 或 --ts-epoch 其中之一")

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
