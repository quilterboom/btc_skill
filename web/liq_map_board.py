#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
liq_map_board.py — 清算地图的网页 payload 构造
================================================

给前端（market.html「清算簇」卡片）提供数据。

★ 关键约束：**这里绝对不向上游发请求**。
  上游 CoinAnk 网页版接口是 IP 配额限流的（见 skill
  `web-app-api-extraction/references/coinank-quota-block.md`），
  一次调试高频打几十次就能把整个 `/api/*` 封掉。
  → 网页只读**采集器已经落盘的本地序列**（`liq_map_snapshots.jsonl`），
    采集器（每 10 分钟一次）是唯一的取数口。

★ 为什么展示的是「派生簇」而不是原始强度曲线：
  原始曲线 3000+ 档、每 $10 一档，浏览器画出来是锯齿噪声，没人看得懂。
  真正有用的是**簇**（局部极大 + 最小间隔去重后的 3~6 个点），
  以及**簇位置随时间怎么动**（簇在靠近还是在远离现价）。
  原始曲线留在本地 raw 文件里，回测要换参数时再重算。

**返回结构**
```json
{
  "available": true,
  "source": "local-snapshot",
  "last_price": 78620.0,
  "as_of": "2026-11-02 15:10:50",
  "as_of_ts": 1793603450,
  "age_sec": 420,
  "stale": false,
  "suggestion": {"label": "偏空", "score": -0.32, "reference_side": "下方",
                 "reference_value": 1144978972.0},
  "coverage_pct": [-4.9, 3.9],
  "above": [{"price":..., "strength_pct":..., "dist_pct":..., "exchanges":6}],
  "below": [...],
  "history": [{"ts":..., "ts_iso":..., "score":..., "label":...,
               "above_top":..., "below_top":..., "px":...}],
  "cluster_drift": {"above": {"from":..., "to":..., "delta":...}, "below": {...}}
}
```
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Dict, List, Optional

DATA_DIR = Path(os.environ.get("BTC_DATA_DIR") or "/root/data")
STORE = DATA_DIR / "liq_map_snapshots.jsonl"

# 快照多久算「过期」（采集器 10 分钟一次 → 20 分钟还没更新 = 异常）
STALE_AFTER_SEC = 20 * 60
# 展示的历史条数（前端画「簇在动」的小趋势）
HISTORY_LIMIT = 48

_CACHE: dict = {"ts": 0.0, "data": None}
_CACHE_TTL = 30.0     # 30s：前端刷新别把磁盘读爆（K 线接口也是 60s 缓存）


def _read_snapshots() -> List[Dict]:
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


def _cluster(r: Dict, side: str) -> List[Dict]:
    out = []
    for c in (r.get(side) or []):
        try:
            out.append({
                "price": round(float(c.get("price")), 1),
                "strength_pct": round(float(c.get("strength_pct")), 1),
                "dist_pct": round(float(c.get("dist_pct")), 2),
                "exchanges": c.get("exchanges"),
            })
        except (TypeError, ValueError):
            continue
    return out


def _history(rows: List[Dict]) -> List[Dict]:
    """给前端画「簇位置随时间怎么动」用。"""
    out = []
    for r in rows[-HISTORY_LIMIT:]:
        sug = r.get("suggestion") or {}
        above = _cluster(r, "above")
        below = _cluster(r, "below")
        out.append({
            "ts": int(r.get("ts") or 0),
            "ts_iso": r.get("ts_iso"),
            "px": r.get("px"),
            "score": sug.get("score"),
            "label": sug.get("label"),
            "above_top": above[0]["price"] if above else None,
            "below_top": below[0]["price"] if below else None,
            "above_dist": above[0]["dist_pct"] if above else None,
            "below_dist": below[0]["dist_pct"] if below else None,
        })
    return out


def _drift(hist: List[Dict]) -> Dict:
    """簇在靠近还是在远离现价（用最近两条快照的最强簇距离差）。"""
    if len(hist) < 2:
        return {}
    a, b = hist[-2], hist[-1]
    out = {}
    for side in ("above", "below"):
        d0, d1 = a.get(f"{side}_dist"), b.get(f"{side}_dist")
        if d0 is None or d1 is None:
            continue
        out[side] = {
            "from": round(float(d0), 2),
            "to": round(float(d1), 2),
            "delta": round(float(d1) - float(d0), 2),
            # 距离缩小 = 簇在靠近现价 = 磁吸更近
            "approaching": bool(abs(d1) < abs(d0)),
        }
    return out


def build_liq_map_board(force: bool = False) -> Dict:
    """构造清算簇 payload。永不抛错 —— 拿不到就返回 available=False。"""
    now = time.time()
    if not force and _CACHE["data"] is not None and now - _CACHE["ts"] < _CACHE_TTL:
        return _CACHE["data"]

    rows = _read_snapshots()
    if not rows:
        payload = {"available": False,
                   "reason": "还没有清算簇快照（采集器每 10 分钟采一次，稍后刷新）",
                   "source": "local-snapshot"}
        _CACHE["ts"] = now
        _CACHE["data"] = payload
        return payload

    last = rows[-1]
    ts = int(last.get("ts") or 0)
    age = int(now - ts) if ts else None
    sug = last.get("suggestion") or {}
    hist = _history(rows)
    payload = {
        "available": True,
        "source": "local-snapshot",
        "last_price": last.get("px"),
        "as_of": last.get("ts_iso"),
        "as_of_ts": ts,
        "age_sec": age,
        "stale": bool(age is not None and age > STALE_AFTER_SEC),
        "suggestion": {
            "label": sug.get("label"),
            "score": sug.get("score"),
            "reference_side": sug.get("reference_side"),
            "reference_value": sug.get("reference_value"),
        },
        "coverage_pct": last.get("coverage_pct"),
        "above": _cluster(last, "above"),
        "below": _cluster(last, "below"),
        "history": hist,
        "cluster_drift": _drift(hist),
        "sample_count": len(rows),
    }
    _CACHE["ts"] = now
    _CACHE["data"] = payload
    return payload


if __name__ == "__main__":
    print(json.dumps(build_liq_map_board(force=True), ensure_ascii=False, indent=2))
