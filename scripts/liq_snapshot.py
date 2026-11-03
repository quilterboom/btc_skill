#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""
liq_snapshot.py — 读**本地**清算簇快照（唯一的取数口是采集器）
================================================================

★ 2026-11-03：CoinAnk 全面下线
  - CoinAnk 网页版接口按 **IP 配额**限流，本机已被封（code=403，
    "please sub api to get data"）→ 再打多少都是 403，只会拖慢一切。
  - 数据源换成 **Hyperliquid 官方 API**（公开、免鉴权、无限频），
    给的是**真实持仓的真实清算价**（`clearinghouseState.liquidationPx`），
    比 CoinAnk 的「持仓结构模型输出」更硬。
  - 因此：**任何运行期路径都不许再向上游发请求**。
    采集器（`liq_map_collector.py`）是唯一取数口，其余全部读本地快照。

**为什么单独一个模块**：scan.py（训练扫描，每 15 分钟跑一次）原来直接调
`liq_map.liq_reference()` → 每轮都打上游 → 这是把 IP 打进封禁名单的主犯。
现在 scan 只读本地快照文件，零上游请求。

**快照行结构**（`liq_map_snapshots.jsonl`，采集器写）
```
{
  "ts": 1793674669, "ts_iso": "2026-11-03 11:11:09", "px": 76727.5,
  "source": "hyperliquid-real",
  "coverage_pct": [-9.3, 1.2],
  "above": [{"price": 77650.0, "strength_pct": 70.6, "dist_pct": 1.2, "exchanges": null}],
  "below": [{"price": 69600.0, "strength_pct": 100.0, "dist_pct": -9.29, "exchanges": null}],
  "suggestion": {"label": "偏空", "score": -0.294, "reference_value": 100.0, "reference_side": "下方"},
  "sampled_addresses": 60, "pool_size": 63, "sampled_positions": 41
}
```
**强度语义**（与 CoinAnk 旧口径一致）：`strength_pct` = 该桶名义 / 全图最大桶 × 100，
是**相对值**，不是精确待清算张数。

**方向语义**（CoinAnk 官方读图口径，HL 沿用）：
  - 现价**上方** = 空单清算簇（涨上去爆空 → 上行磁吸）
  - 现价**下方** = 多单清算簇（跌下去爆多 → 下行瀑布）
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Dict, List, Optional

DATA_DIR = Path(os.environ.get("BTC_DATA_DIR") or "/root/data")
STORE = DATA_DIR / "liq_map_snapshots.jsonl"

# 快照多久算「过期」—— 采集器每小时一次（一天 24 次，liusir 定的频率上限），
# 2 小时还没更新 = 采集器出问题了。
STALE_AFTER_SEC = 2 * 3600


def read_rows() -> List[Dict]:
    """读本地快照序列（追加式 jsonl，坏行跳过）。"""
    if not STORE.exists():
        return []
    rows: List[Dict] = []
    try:
        for line in STORE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(r, dict) and r.get("ts"):
                rows.append(r)
    except Exception:
        return []
    rows.sort(key=lambda r: int(r.get("ts") or 0))
    return rows


def latest_row() -> Optional[Dict]:
    """最新一条快照（没有就 None）。"""
    rows = read_rows()
    return rows[-1] if rows else None


def row_to_reference(row: Optional[Dict], compact: bool = False) -> Dict:
    """快照行 → 策略参考结构（兼容旧 `liq_map.liq_reference()` 的形状）。

    拿不到数据时返回 `available=False` + 原因 —— 调用方如实显示不可用，不编造。

    `compact=True` → 去掉随时间变化���字段（age_sec / stale / as_of_iso），
    用于写进 journal meta 做归因（写进去就该是那一次扫描看到的事实，
    不该带一个每次读都变的 age）。
    """
    if not row:
        return {"available": False, "reason": "本地还没有清算簇快照（采集器每小时采一次）",
                "source": "local-snapshot"}
    age = int(time.time() - int(row.get("ts") or 0))
    sug = row.get("suggestion") or {}
    ref = {
        "available": True,
        "source": row.get("source") or "local-snapshot",
        "last_price": row.get("px"),
        "clusters_above": row.get("above") or [],
        "clusters_below": row.get("below") or [],
        "coverage_pct": row.get("coverage_pct"),
        "suggestion": {
            "available": bool(sug.get("label")),
            "suggestion": sug.get("label"),
            "score": sug.get("score"),
            "reference_value": sug.get("reference_value"),
            "reference_side": sug.get("reference_side"),
        },
        "as_of": int(row.get("ts") or 0),
        "age_sec": age,
        "stale": age > STALE_AFTER_SEC,
        # 采样口径（HL 是抽样，样本量决定这条快照的可信度）
        "sampled_addresses": row.get("sampled_addresses"),
        "sampled_positions": row.get("sampled_positions"),
        "pool_size": row.get("pool_size"),
    }
    if compact:
        for k in ("age_sec", "stale", "as_of_iso"):
            ref.pop(k, None)
    return ref


def format_reference(ref: Dict) -> str:
    """渲染成一行中文参考文本（拿不到就如实说）。"""
    if not ref or not ref.get("available"):
        return f"清算地图：不可用（{(ref or {}).get('reason', '未知原因')}）"

    def _fmt(clusters: List[Dict]) -> str:
        if not clusters:
            return "无显著簇"
        out = []
        for c in clusters:
            txt = f"{c['price']:,.0f}(强度{c['strength_pct']:.0f}%,{c['dist_pct']:+.1f}%)"
            if c.get("exchanges") is not None:
                txt += f"/{c.get('exchange_total')}家"
            out.append(txt)
        return " · ".join(out)

    cov = ref.get("coverage_pct")
    cov_txt = f"｜覆盖 {cov[0]:+.1f}%~{cov[1]:+.1f}%" if cov else ""
    sug = ref.get("suggestion") or {}
    sug_txt = ""
    if sug.get("available"):
        sug_txt = (f"｜多空建议 {sug['suggestion']}"
                   f"（倾向分 {sug['score']:+.2f}，参考值取自{sug.get('reference_side')}侧）")
    samp = ref.get("sampled_positions")
    samp_txt = f"｜样本 {samp} 笔持仓" if samp else ""
    stale = "（已过期）" if ref.get("stale") else ""
    return (f"真实清算图（{ref['source']}）{stale}：上方空单清算簇 {_fmt(ref['clusters_above'])} ｜ "
            f"下方多单清算簇 {_fmt(ref['clusters_below'])}{cov_txt}{sug_txt}{samp_txt}")


if __name__ == "__main__":
    print(json.dumps(row_to_reference(latest_row()), ensure_ascii=False, indent=2))
