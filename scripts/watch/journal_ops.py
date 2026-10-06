#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
journal 操作工具：被 JumpTracker 和 Settler 共用。
"""
from __future__ import annotations
import os, json, time
from pathlib import Path
from typing import Optional

from gate_fetch import DATA_DIR
from .common import log


def invalidate_other_pending(keep_id: Optional[str], reason: str) -> int:
    """
    把 journal 里除 keep_id 外的所有 pending 标记为 invalidated。
    返回被作废的数量。线程安全：写整个文件用 .tmp 原子替换。
    """
    jpath = Path(DATA_DIR) / "journal.jsonl"
    if not jpath.exists():
        return 0
    now = int(time.time())
    lines = open(jpath, encoding="utf-8").readlines()
    out = []
    n = 0
    for ln in lines:
        try:
            r = json.loads(ln.strip())
            if r.get("status") == "pending" and r.get("id") != keep_id:
                r["status"] = "invalidated"
                r["invalidated_ts"] = now
                r["invalidated_reason"] = reason
                out.append(json.dumps(r, ensure_ascii=False) + "\n")
                n += 1
            else:
                out.append(ln if ln.endswith("\n") else ln + "\n")
        except Exception:
            out.append(ln if ln.endswith("\n") else ln + "\n")
    tmp = jpath.with_suffix(".jsonl.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        f.writelines(out)
    tmp.replace(jpath)
    return n


def find_active(contract: Optional[str] = None) -> Optional[dict]:
    """找最近一条 active（pending 或 filled）的 journal 记录"""
    jpath = Path(DATA_DIR) / "journal.jsonl"
    if not jpath.exists():
        return None
    try:
        recs = [json.loads(ln.strip()) for ln in open(jpath, encoding="utf-8").readlines() if ln.strip()]
    except Exception:
        return None
    for r in reversed(recs):
        if r.get("status") not in ("pending", "filled"):
            continue
        if contract and r.get("contract") != contract:
            continue
        return r
    return None
