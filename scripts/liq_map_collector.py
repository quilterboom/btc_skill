#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""
liq_map_collector.py — 清算簇时间序列采集器
================================================

**目的**（liusir 2026-11-02）：把清算簇数据存下来，**以备后续回测使用**
—— 回答「信号对清算簇位置的敏感性」：信号触发时，上方/下方清算簇有多近、多强、
倾向分偏多还是偏空，对后续盈亏有没有区分度。

**数据源：Hyperliquid 官方 API（2026-11-03 换）**
──────────────────────────────────────────────────────────
CoinAnk 网页版接口按 **IP 配额**限流，本机已被封（code=403 "please sub api to
get data"）→ 再打多少都是 403。CoinAnk 的清算图本身也是「持仓结构的模型输出」。
Hyperliquid 官方 API 公开、免鉴权、无限频，给的是**真实持仓的真实清算价**
（`clearinghouseState.liquidationPx`）。→ `scripts/liq_map.py` 已删除，
运行期**任何路径都不再向上游发请求**；采集器是唯一取数口。

**频率：每小时一次（一天 24 次）** —— liusir 2026-11-03 定的上限。
HL 无限频，但没必要更密：清算价分布变化慢，采集更密只是浪费。

**两层存储，各有分工**
──────────────────────────────────────────────────────────
| 层 | 位置 | 内容 | 为什么 |
|---|---|---|---|
| 回测主源 | 本地 `/root/data/liq_map_snapshots.jsonl` | 每次快照一行（派生特征） | 回测脚本要按 ts join 信号/账本，API 慢+限频，本地才是回测该读的 |
| 原始明细 | 本地 `/root/data/liq_map_raw/<ts>.json.gz` | 该次快照的持仓清算价明细 | 簇参数（分箱宽度/死区）还没被回测验证过 → 留原始数据，以后换参数重算，不用重新等历史 |
| 异地归档 | IMA 笔记（append） | 派生特征行（markdown 表） | 磁盘坏了数据还在；notes 支持追加，不像 KB 那样每次多一个永久条目 |

**为什么 IMA 放笔记而不是 KB**（KB 是 add-only 推土机，不可删不可改）：
- 清算簇是**持续增长的时间序列**。按 ima-skill 的决策矩阵：「持续追加的时间线 → notes 笔记」。
- 若按快照上传 KB，一天 24 份 = 一年 8760 个永久条目 —— 正是 10-20 那次污染事故的形状。
- notes 追加到**同一篇**，条目数恒为 1；`append_doc` 到顶（100009）时自动滚到下一篇
  （标题带周期后缀），仍是 append-only 版本链。

**用法**
```bash
python3 liq_map_collector.py --collect      # 采一次快照 → 本地 jsonl + 原始明细
python3 liq_map_collector.py --sync-ima     # 把本地未同步的行追加到 IMA 笔记
python3 liq_map_collector.py --both         # 采集 + （若到点）同步
python3 liq_map_collector.py --status       # 本地统计 + 待同步行数
```

systemd：服务 `btc-liq-map-collector.service`，定时器 `btc-liq-map-collector.timer`
（每小时采集一次）。

**口径沿用 `hyperliquid_liqmap.py`**（分箱 + 多空建议参考值=数值最大值），
本文件不重算逻辑，只负责「取 → 存 → 同步」。
"""
from __future__ import annotations

import argparse
import gzip
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple

CST = timezone(timedelta(hours=8))
SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import hyperliquid_liqmap as hl  # noqa: E402

DATA_DIR = Path(os.environ.get("BTC_DATA_DIR") or "/root/data")
STORE = DATA_DIR / "liq_map_snapshots.jsonl"       # 派生特征（回测主源）
RAW_DIR = DATA_DIR / "liq_map_raw"                 # 原始强度曲线（gz）
SYNC_STATE = DATA_DIR / "liq_map_sync_state.json"  # IMA 同步游标
LOG = DATA_DIR / "liq_map_collect.log"

SKILL_DIR = os.path.expanduser("~/.hermes/skills/ima-skill")
NOTE_TITLE_BASE = "BTC 清算簇时间序列（回测用 · Hyperliquid 真实持仓）"
NOTE_FOLDER_NAME = "AI"          # 与其它 BTC 笔记同笔记本
IMA_SYNC_MIN_INTERVAL = 6 * 3600  # IMA 追加最小间隔（防限频；每小时采一次，攒够再推）
RAW_KEEP_DAYS = 90              # 原始明细保留天数（每份很小，90 天也占不了多少）


# ---------------------------------------------------------------- IMA 调用
def _opts() -> str:
    cli = Path(os.path.expanduser("~/.config/ima/client_id")).read_text().strip()
    key = Path(os.path.expanduser("~/.config/ima/api_key")).read_text().strip()
    return json.dumps({"clientId": cli, "apiKey": key})


def _api(api_path: str, body: dict) -> dict:
    """调 IMA API。失败即抛（不兜底、不静默）。"""
    cmd = ["node", str(Path(SKILL_DIR) / "ima_api.cjs"), api_path, json.dumps(body), _opts()]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    out = (r.stdout or "").strip()
    err = (r.stderr or "").strip()
    if r.returncode != 0:
        raise RuntimeError(f"IMA 调用失败 {api_path}: rc={r.returncode} {err[:400]}")
    try:
        resp = json.loads(out)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"IMA 返回非 JSON {api_path}: {out[:300]}") from e
    if resp.get("code") != 0:
        raise RuntimeError(f"IMA 业务错误 {api_path}: code={resp.get('code')} msg={resp.get('msg')}")
    return resp.get("data") or {}


def _find_notebook(name: str) -> Optional[str]:
    """按名字找笔记本 folder_id（笔记本 ID 也是会话态，每次现取）。

    ⚠️ list_notebook 的 limit 范围是 (0, 20]（**不是** 50 —— 传 50 回 code=51）。
    """
    cursor = "0"
    while True:
        data = _api("openapi/note/v1/list_notebook", {"cursor": cursor, "limit": 20})
        for nb in data.get("note_folder_infos") or []:
            if nb.get("name") == name:
                return nb.get("folder_id")
        if data.get("is_end"):
            return None
        cursor = data.get("next_cursor") or ""
        if not cursor:
            return None


def _find_note_by_title(title: str) -> Optional[str]:
    data = _api("openapi/note/v1/search_note",
                {"search_type": 0, "query_info": {"title": title}, "start": 0, "end": 20})
    for info in data.get("search_note_infos") or []:
        nb = (info.get("note_book_info") or {})
        if nb.get("title") == title:
            return nb.get("note_id")
    return None


def _create_note(title: str, folder_id: Optional[str]) -> str:
    body = {"content_format": 1, "content": _note_header(title)}
    if folder_id:
        body["folder_id"] = folder_id
    data = _api("openapi/note/v1/import_doc", body)
    note_id = data.get("note_id")
    if not note_id:
        raise RuntimeError(f"import_doc 没返回 note_id: {data}")
    return note_id


def _note_header(title: str) -> str:
    """新笔记的表头（说明列含义 + 数据来源）。"""
    return (
        f"# {title}\n\n"
        "**用途**：清算簇时间序列归档，供后续回测「信号对清算簇位置的敏感性」分析。\n\n"
        "**数据源**：Hyperliquid 官方 API（`api.hyperliquid.xyz/info`，免鉴权、无限频）——"
        "取真实持仓的**真实清算价**（`clearinghouseState.liquidationPx`）。\n"
        "每小时采一次快照（一天 24 次）。\n\n"
        "**方向语义**：现价**上方**=空单清算簇（涨上去爆空 → 上行磁吸），"
        "现价**下方**=多单清算簇（跌下去爆多 → 下行瀑布）。\n\n"
        "**多空建议口径**：参考值 = 上方/下方强度中的数值最大值；"
        "倾向分 = (上方-下方)/参考值 ∈ [-1,1]，死区 0.15。\n\n"
        "**强度口径**：相对值（占全图最大桶的 %），不是精确待清算张数。\n\n"
        "**抽样说明**：样本 = 最近成交里的活跃地址（跨轮累积 + 轮换），"
        "**不是全网持仓**；样本量随列末记录。\n\n"
        "**列说明**：\n"
        "| 时间(CST) | 现价 | 上方空单簇(价/强度%/距离%) | 下方多单簇(价/强度%/距离%) | 倾向分 | 倾向 | 覆盖 | 数据源 |\n"
        "|---|---|---|---|---|---|---|---|\n"
    )


def _format_rows(rows: List[Dict]) -> str:
    """派生特征行 → markdown 表行。

    ⚠️ IMA 的 `get_doc_content`（target_content_format=0）**读回时会把换行压平**
      （实测：14343 字符的既有笔记只有 1 个换行）。存储侧没问题，是纯文本读取格式的锅。
      所以每行必须**自定界**：行首固定 `| YYYY-MM-DD HH:MM:SS |`，回读时按这个模式切，
      不能指望换行分隔（见 `parse_ima_rows`）。
    """
    out = []
    for r in rows:
        def _side(lst: List[Dict]) -> str:
            if not lst:
                return "—"
            return " · ".join(f"{c['price']:.0f}/{c['strength_pct']:.0f}/{c['dist_pct']:+.1f}"
                              for c in lst)
        sug = r.get("suggestion") or {}
        cov = r.get("coverage_pct") or []
        cov_s = f"{cov[0]:+.1f}%~{cov[1]:+.1f}%" if len(cov) == 2 else "—"
        out.append(
            f"| {r.get('ts_iso', '')} | {r.get('px', 0):,.1f} | {_side(r.get('above') or [])} | "
            f"{_side(r.get('below') or [])} | {sug.get('score', 0):+.2f} | {sug.get('label', '—')} | "
            f"{cov_s} | {r.get('source', '')} |")
    return "\n".join(out)


# 回读时切行用：行首必须是「| 2026-11-02 12:24:35 |」
_ROW_RE = re.compile(
    r"\|\s*(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\s*\|"
    r"\s*([\d,.]+)\s*\|"
    r"\s*([^|]*)\|"
    r"\s*([^|]*)\|"
    r"\s*([+-][\d.]+)\s*\|"
    r"\s*([^|]*)\|"
    r"\s*([^|]*)\|"
    r"\s*([^|]*)\|"
)


def parse_ima_rows(content: str) -> List[Dict]:
    """从 IMA 笔记正文解析回派生特征行（不依赖换行，见 _format_rows 的说明）。"""
    out = []
    for m in _ROW_RE.finditer(content or ""):
        ts_iso, px, above, below, score, label, cov, source = m.groups()
        def _parse_side(txt: str) -> List[Dict]:
            txt = (txt or "").strip()
            if not txt or txt == "—":
                return []
            res = []
            for part in txt.split("·"):
                part = part.strip()
                if not part:
                    continue
                bits = part.split("/")
                if len(bits) != 3:
                    continue
                try:
                    res.append({"price": float(bits[0]), "strength_pct": float(bits[1]),
                                "dist_pct": float(bits[2])})
                except ValueError:
                    continue
            return res
        try:
            score_v = float(score)
        except ValueError:
            score_v = 0.0
        try:
            px_v = float(px.replace(",", ""))
        except ValueError:
            px_v = 0.0
        out.append({"ts_iso": ts_iso, "px": px_v, "above": _parse_side(above),
                    "below": _parse_side(below), "suggestion": {"score": score_v,
                    "label": label.strip()}, "coverage_pct": cov.strip(),
                    "source": source.strip()})
    return out


# ---------------------------------------------------------------- 本地存取
def _read_store() -> List[Dict]:
    if not STORE.exists():
        return []
    rows = []
    for line in STORE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue          # 坏行跳过，不让一行坏数据毁掉整个序列
    return rows


def _append_store(row: Dict) -> bool:
    """追加一行。**同一 ts 只留一行**（定时器与手动触发可能撞在同一秒）。

    返回是否真的写入了。重复行对回测是硬伤 —— 会把那一秒的快照算两遍。
    """
    ts = int(row.get("ts") or 0)
    existing = _read_store()
    if existing and int(existing[-1].get("ts") or 0) == ts:
        return False
    STORE.parent.mkdir(parents=True, exist_ok=True)
    with open(STORE, "a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")
    return True


def _read_state() -> Dict:
    if not SYNC_STATE.exists():
        return {}
    try:
        return json.loads(SYNC_STATE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _write_state(st: Dict) -> None:
    SYNC_STATE.parent.mkdir(parents=True, exist_ok=True)
    tmp = str(SYNC_STATE) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=2)
    os.replace(tmp, SYNC_STATE)


def _log(msg: str) -> None:
    line = f"[{datetime.now(CST).strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line, flush=True)
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


# ---------------------------------------------------------------- 采集
def _save_raw(ts: int, ref: Dict) -> Optional[str]:
    """原始明细落盘（gz）。以后换分箱宽度/簇参数可直接重算，不用重新等历史。

    HL 侧存的是**样本内每个持仓的清算价明细**（而不是 CoinAnk 的强度曲线）——
    这才是真值：从明细出发，任何分箱/聚类口径都能离线重算。
    """
    try:
        RAW_DIR.mkdir(parents=True, exist_ok=True)
        payload = {
            "ts": ts,
            "ts_iso": datetime.fromtimestamp(ts, CST).isoformat(),
            "source": ref.get("source"),
            "px": ref.get("px"),
            "sampled_addresses": ref.get("sampled_addresses"),
            "pool_size": ref.get("pool_size"),
            "sampled_positions": ref.get("sampled_positions"),
            "skipped_null_liq_px": ref.get("skipped_null_liq_px"),
            "bin_usd": ref.get("bin_usd"),
            # 明细：每个持仓的方向 / 清算价 / 名义 / 杠杆
            "positions": ref.get("positions") or [],
        }
        fname = datetime.fromtimestamp(ts, CST).strftime("%Y%m%d_%H%M%S") + ".json.gz"
        path = RAW_DIR / fname
        tmp = str(path) + ".tmp"
        with gzip.open(tmp, "wt", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
        os.replace(tmp, path)
        return fname
    except Exception as e:
        _log(f"⚠️ 原始明细落盘失败（不影响派生特征）: {e}")
        return None


def _prune_raw(keep_days: int = RAW_KEEP_DAYS) -> int:
    """清理过期的原始曲线文件（只删 raw，不动 jsonl —— jsonl 是回测主源，永久保留）。

    10min 一份 ≈ 47KB → 约 2.35GB/年。不清理的话磁盘迟早满。
    """
    if keep_days <= 0 or not RAW_DIR.exists():
        return 0
    cutoff = time.time() - keep_days * 86400
    n = 0
    for f in RAW_DIR.glob("*.json.gz"):
        try:
            if f.stat().st_mtime < cutoff:
                f.unlink()
                n += 1
        except OSError:
            continue
    return n


def collect(force: bool = False) -> Optional[Dict]:
    """采一次快照：写派生特征行 + 原始曲线。返回行（不可用时 None）。

    ★ 2026-11-03 数据源换成 Hyperliquid 真实持仓（`hyperliquid_liqmap`）：
      CoinAnk 网页版接口按 IP 配额限流，实测已被封（code=403），且它的清算图是
      **持仓结构的模型输出**；Hyperliquid 官方 API 公开、免鉴权、无限频，
      给的是**真实持仓的真实清算价**（clearinghouseState.liquidationPx）。
      → 采集器不再碰 CoinAnk（IP 已封，打多少都是 403）。

    ★ 请求预算：HL 侧每轮 = 1（allMids）+ 1（recentTrades）+ N（每地址 1 次
      clearinghouseState）。N 由 `hyperliquid_liqmap.QUERY_MAX` 封顶（60），
      地址池跨轮累积 + 轮换，样本量随时间增长但请求量有界。
    """
    ref = hl.build_liqmap()
    if not ref.get("available"):
        _log(f"⏭️  清算地图不可用：{ref.get('reason')}")
        return None

    ts = int(time.time())
    sug = ref.get("suggestion") or {}
    row = {
        "ts": ts,
        "ts_iso": datetime.fromtimestamp(ts, CST).strftime("%Y-%m-%d %H:%M:%S"),
        "px": float(ref.get("px") or 0),
        "source": ref.get("source"),
        "coverage_pct": ref.get("coverage_pct"),
        "above": [{"price": c["price"], "strength_pct": c["strength_pct"],
                   "dist_pct": c["dist_pct"], "exchanges": c.get("exchanges")}
                  for c in (ref.get("above") or [])],
        "below": [{"price": c["price"], "strength_pct": c["strength_pct"],
                   "dist_pct": c["dist_pct"], "exchanges": c.get("exchanges")}
                  for c in (ref.get("below") or [])],
        "suggestion": {
            "label": sug.get("suggestion"),
            "score": sug.get("score"),
            "reference_value": sug.get("reference_value"),
            "reference_side": sug.get("reference_side"),
        },
        # 采样口径必须落盘：HL 是**抽样**（只查最近成交里的活跃地址），
        # 不记样本量的话，下游无法判断这条快照的可信度。
        "sampled_addresses": ref.get("sampled_addresses"),
        "pool_size": ref.get("pool_size"),
        "sampled_positions": ref.get("sampled_positions"),
        "skipped_null_liq_px": ref.get("skipped_null_liq_px"),
    }

    # 原始数据落盘（gz）—— HL 侧存「样本内每个持仓的清算价明细」，
    # 以后换分箱宽度/簇参数可直接重算，不用重新等历史。
    raw_file = _save_raw(ts, ref)
    if raw_file:
        row["raw_file"] = raw_file

    if not _append_store(row):
        _log(f"⏭️  同一时刻快照已存在（ts={ts}），跳过重复写入")
        return row
    pruned = _prune_raw()
    _log(f"✅ 已采集 ts={ts} px={row['px']:.1f} "
         f"倾向={row['suggestion'].get('label')}({row['suggestion'].get('score')})"
         f"｜样本 {ref.get('sampled_positions')} 持仓/{ref.get('sampled_addresses')} 地址"
         f"（池 {ref.get('pool_size')}）"
         + (f" raw={raw_file}" if raw_file else "")
         + (f"｜清理过期 raw {pruned} 份" if pruned else ""))
    return row


# ---------------------------------------------------------------- IMA 同步
def _pending_rows() -> List[Dict]:
    """本地有、IMA 还没推的行（按 ts 升序）。"""
    st = _read_state()
    synced_ts = int(st.get("synced_ts") or 0)
    rows = [r for r in _read_store() if int(r.get("ts") or 0) > synced_ts]
    rows.sort(key=lambda r: int(r.get("ts") or 0))
    return rows


def _ensure_note(st: Dict) -> Tuple[str, str]:
    """拿到可追加的笔记（note_id, title）。没有就建。"""
    title = st.get("note_title") or NOTE_TITLE_BASE
    note_id = st.get("note_id")
    if note_id:
        return note_id, title
    # 笔记 ID 也可能失效 → 先按标题找，找不到再新建
    note_id = _find_note_by_title(title)
    if not note_id:
        folder_id = _find_notebook(NOTE_FOLDER_NAME)
        note_id = _create_note(title, folder_id)
        _log(f"📝 新建 IMA 笔记「{title}」")
    return note_id, title


def sync_ima(force: bool = False) -> Dict:
    """把本地未同步的行追加到 IMA 笔记。"""
    st = _read_state()
    last = int(st.get("last_sync_ts") or 0)
    if not force and last and (time.time() - last) < IMA_SYNC_MIN_INTERVAL:
        _log(f"⏭️  距上次同步不足 {IMA_SYNC_MIN_INTERVAL // 3600}h，跳过")
        return {"synced": 0, "skipped": True}

    rows = _pending_rows()
    if not rows:
        _log("ℹ️  没有待同步行")
        return {"synced": 0, "skipped": True}

    note_id, title = _ensure_note(st)
    content = _format_rows(rows)
    if not content.strip():
        return {"synced": 0, "skipped": True}

    try:
        _api("openapi/note/v1/append_doc",
             {"note_id": note_id, "content_format": 1, "content": "\n" + content + "\n"})
    except RuntimeError as e:
        if "100009" in str(e):
            # 笔记到顶 → 滚到下一篇（标题带周期后缀），仍是 append-only 版本链
            period = datetime.now(CST).strftime("%Y%m")
            new_title = f"{NOTE_TITLE_BASE} {period}-{len(rows)}"
            _log(f"📄 笔记已满，滚到新篇「{new_title}」")
            note_id = _create_note(new_title, _find_notebook(NOTE_FOLDER_NAME))
            title = new_title
            _api("openapi/note/v1/append_doc",
                 {"note_id": note_id, "content_format": 1, "content": "\n" + content + "\n"})
        else:
            raise

    synced_ts = max(int(r.get("ts") or 0) for r in rows)
    _write_state({"note_id": note_id, "note_title": title,
                  "synced_ts": synced_ts, "last_sync_ts": int(time.time()),
                  "synced_rows": int(st.get("synced_rows") or 0) + len(rows)})
    _log(f"☁️  已同步 {len(rows)} 行到 IMA 笔记「{title}」（synced_ts={synced_ts}）")
    return {"synced": len(rows), "note_title": title, "synced_ts": synced_ts}


def read_from_ima() -> List[Dict]:
    """从 IMA 笔记回读归档行（异地副本 → 本地，回测时也能用）。"""
    st = _read_state()
    note_id = st.get("note_id")
    if not note_id:
        note_id, _ = _ensure_note(st)
    data = _api("openapi/note/v1/get_doc_content",
                {"note_id": note_id, "target_content_format": 0})
    content = (data or {}).get("content") or ""
    return parse_ima_rows(content)


# ---------------------------------------------------------------- CLI
def status() -> None:
    rows = _read_store()
    st = _read_state()
    pending = _pending_rows()
    print(f"本地派生特征：{len(rows)} 行  → {STORE}")
    if rows:
        print(f"  时间范围：{rows[0]['ts_iso']} ~ {rows[-1]['ts_iso']}")
    raws = sorted(RAW_DIR.glob("*.json.gz")) if RAW_DIR.exists() else []
    print(f"原始曲线：{len(raws)} 份  → {RAW_DIR}")
    print(f"待同步：{len(pending)} 行")
    print(f"IMA 同步状态：note_id={st.get('note_id')} title={st.get('note_title')} "
          f"synced_ts={st.get('synced_ts')} last_sync={st.get('last_sync_ts')}")
    if rows:
        print("最新一行：")
        print("  " + json.dumps(rows[-1], ensure_ascii=False))


def main() -> int:
    ap = argparse.ArgumentParser(description="清算簇时间序列采集器")
    ap.add_argument("--collect", action="store_true", help="采一次快照")
    ap.add_argument("--sync-ima", action="store_true", help="同步到 IMA 笔记")
    ap.add_argument("--both", action="store_true", help="采集 + 同步")
    ap.add_argument("--status", action="store_true", help="状态统计")
    ap.add_argument("--read-ima", action="store_true", help="从 IMA 笔记回读归档行（JSON 输出）")
    ap.add_argument("--prune", action="store_true", help="只清理过期原始曲线")
    ap.add_argument("--keep-days", type=int, default=RAW_KEEP_DAYS,
                    help=f"原始曲线保留天数（默认 {RAW_KEEP_DAYS}，0=不清理）")
    ap.add_argument("--force", action="store_true", help="忽略同步间隔强制同步")
    a = ap.parse_args()

    if not any([a.collect, a.sync_ima, a.both, a.status, a.prune, a.read_ima]):
        a.both = True

    if a.status:
        status()
        return 0
    if a.read_ima:
        rows = read_from_ima()
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return 0
    if a.prune:
        if a.keep_days <= 0:
            _log("🧹 保留天数=0 → 不清理")
        else:
            n = _prune_raw(a.keep_days)
            _log(f"🧹 清理过期原始曲线 {n} 份（保留 {a.keep_days} 天）")
        return 0
    if a.collect or a.both:
        collect()
    if a.sync_ima or a.both:
        try:
            sync_ima(force=a.force)
        except Exception as e:
            _log(f"⚠️ IMA 同步失败（本地数据已安全保存）: {e}")
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
