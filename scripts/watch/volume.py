#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
VolumeWatcher — 1m 量能突增检测
==============================

维护 1m 量能滚动窗口：
- ratio = 当前根量 / 近 N 根中位数 ≥ --ratio（默认 4.0）
- z = (当前根量 - 均值) / 标准差 ≥ --z（默认 3.5）
- 或 ratio ≥ 2×阈值 直接触发
- --min_vol 设绝对量门槛防凌晨低基数误报
- --cooldown 限流（默认 1800s）

last_fire 持久化（2026-10-05）：持久到 DATA_DIR/last_fire.json，
按 contract 分组。修复"watch 重启 → last_fire 归零 → 冷却失效"的 bug。
"""
from __future__ import annotations
import os, json, time
from collections import deque
from typing import Dict, Optional

from gate_fetch import fetch_ohlcv, DATA_DIR

from .common import log

_LAST_FIRE_PATH = os.path.join(DATA_DIR, "last_fire.json")


def _load_last_fire(contract: str) -> float:
    """读历史 last_fire（key=合约名）。文件损坏返回 0（不抛错）。"""
    try:
        if os.path.exists(_LAST_FIRE_PATH):
            data = json.load(open(_LAST_FIRE_PATH, encoding="utf-8"))
            return float(data.get(contract, 0.0) or 0.0)
    except Exception:
        pass
    return 0.0


def _save_last_fire(contract: str, ts: float) -> None:
    """写 last_fire（按合约分 key，原子写避免半文件）"""
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        # 先读旧值（保留其他合约），再写新值
        old = {}
        if os.path.exists(_LAST_FIRE_PATH):
            try:
                old = json.load(open(_LAST_FIRE_PATH, encoding="utf-8"))
            except Exception:
                old = {}
        old[contract] = float(ts)
        tmp = _LAST_FIRE_PATH + ".tmp"
        json.dump(old, open(tmp, "w", encoding="utf-8"))
        os.replace(tmp, _LAST_FIRE_PATH)
    except Exception as e:
        log(f"{WARN if 'WARN' in dir() else '⚠️'} last_fire 持久化失败: {e}")


class VolumeWatcher:
    def __init__(self, contract="BTC_USDT", tf="1m", window=120, ratio=4.0, z=3.5,
                 min_vol=0.0, cooldown=1800.0, target=1000.0, do_scan=True, extra=None):
        self.contract, self.tf = contract, tf
        self.window, self.ratio_thr, self.z_thr = window, ratio, z
        self.min_vol, self.cooldown = min_vol, cooldown
        self.target, self.do_scan = target, do_scan
        self.extra = list(extra or [])
        self.hist: deque = deque(maxlen=window)      # 已收盘的整分根量
        self.cur: Dict = {}                          # 当前正在走的那根
        # ★ 从磁盘读，恢复上次进程的 last_fire（修重启清零 bug）
        self.last_fire = _load_last_fire(self.contract)
        self.last_msg = time.time()
        self.n_alert = 0
        self.n_msg = 0

    def mark_fired(self) -> None:
        """更新 last_fire（内存 + 磁盘两处都要改）"""
        self.last_fire = time.time()
        _save_last_fire(self.contract, self.last_fire)

    def prime(self) -> int:
        """初始化：用 REST 拉历史 1m 量（最后一根留作"当前根"，不进历史）"""
        rows = fetch_ohlcv(self.contract, self.tf, self.window + 5, cache=False)
        for r in rows[:-1]:
            self.hist.append(float(r[5]))
        if rows:
            self.cur = {"ts": int(rows[-1][0]), "v": float(rows[-1][5]),
                        "c": float(rows[-1][4])}
            self.prev_close = float(rows[-2][4]) if len(rows) > 1 else float(rows[-1][4])
        log(f"历史量能窗口已载入 {len(self.hist)} 根（{self.tf}）｜"
            f"中位 {self._med():,.0f} 张")
        return len(self.hist)

    def _med(self) -> float:
        v = sorted(self.hist)
        n = len(v)
        if not n:
            return 0.0
        return v[n // 2] if n % 2 else (v[n // 2 - 1] + v[n // 2]) / 2

    def on_candle(self, d: Dict) -> Optional[Dict]:
        """WS 回调。Gate futures.candlesticks: {'t':..,'v':..,'c':..,'h':..,'l':..,'o':..,'sum':..}"""
        ts = int(float(d.get("t", 0)))
        vol = float(d.get("v", 0) or 0)
        close = float(d.get("c", 0) or 0)
        if ts <= 0:
            return None
        self.last_msg = time.time()
        if not self.cur or ts > self.cur["ts"]:
            # 上一根走完 → 固化并判定
            prev = self.cur
            self.cur = {"ts": ts, "v": vol, "c": close}
            if prev:
                self.hist.append(prev["v"])
                self.prev_close = prev["c"]
                return self.judge(prev)
        else:
            self.cur = {"ts": ts, "v": vol, "c": close}
        return None

    def judge(self, bar: Dict) -> Optional[Dict]:
        hist = list(self.hist)
        if len(hist) < 30:
            return None
        vol = bar["v"]
        med = sorted(hist)[len(hist) // 2] or 1.0
        mean = sum(hist) / len(hist)
        sd = (sum((x - mean) ** 2 for x in hist) / len(hist)) ** 0.5 or 1e-9
        ratio = vol / med
        z = (vol - mean) / sd
        pct = vol / sum(hist) * 100
        hit = (ratio >= self.ratio_thr and z >= self.z_thr) or ratio >= self.ratio_thr * 2
        if vol < self.min_vol:
            hit = False
        d = {"ts": int(bar["ts"]), "vol": vol, "close": bar["c"],
             "median": med, "mean": mean, "sd": sd,
             "ratio": ratio, "z": z, "pct_of_window": pct,
             "hit": bool(hit), "min_vol_gate": self.min_vol}
        if hit:
            d["tag"] = self.tag(bar)
        return d if hit else None

    def tag(self, bar: Dict) -> str:
        """放量 + 价格怎么走 → 方向标注"""
        prev_c = getattr(self, "prev_close", None)
        c = bar["c"]
        dp = 0.0
        if prev_c:
            dp = (c / prev_c - 1) * 100
        if abs(dp) < 0.03:
            return f"放量滞涨（吸收/换手，价格 {dp:+.2f}%）"
        return f"放量{'上涨' if dp > 0 else '下跌'} {dp:+.2f}%"
