#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
RsiWatcher — RSI 越界监控
=========================

固定默认 1h 周期（--rsi-tf 可覆盖）：
- 穿越触发（上一轮在界内 → 本轮越界），两侧独立冷却
- 滞回上锁：触发后必须退回到「阈值 ∓ 滞回」以内才重新武装
- 用 WS 最新价合成未收盘当前根 → 准实时
"""
from __future__ import annotations
import time
from typing import Optional, Dict

from gate_fetch import fetch_ohlcv
from indicators import rsi as rsi_calc

from .common import log, WARN


class RsiWatcher:
    def __init__(self, contract="BTC_USDT", tf="1h", period=14, low=30.0, high=70.0,
                 cooldown=3600.0, interval=300.0, hyst=3.0,
                 target=1000.0, do_scan=True, extra=None):
        self.contract, self.tf, self.period = contract, tf, int(period)
        self.low, self.high = low, high
        self.cooldown, self.interval = cooldown, interval
        self.hyst = hyst                          # 滞回缓冲，防边界抖动重复报警
        self.target, self.do_scan = target, do_scan
        self.extra = list(extra or [])
        self.last_price: Optional[float] = None
        self.prev_val: Optional[float] = None     # 上一轮 RSI（用于穿越判定）
        self.last_fire_low = 0.0                  # 超卖侧冷却
        self.last_fire_high = 0.0                 # 超买侧冷却
        # 武装状态：触发后上锁，必须退回到「阈值 ∓ 滞回」以内才重新武装
        self.armed_low = True
        self.armed_high = True
        self.last_check = 0.0
        self.n_check = 0
        self.n_alert = 0

    def set_last_price(self, px) -> None:
        try:
            px = float(px)
            if px > 0:
                self.last_price = px
        except Exception:
            pass

    def value(self) -> Optional[float]:
        """当前 RSI 值（最新价合成到未收盘根）"""
        need = self.period * 8 + 40
        rows = fetch_ohlcv(self.contract, self.tf, need, cache=False)
        if len(rows) < self.period + 3:
            return None
        closes = [float(r[4]) for r in rows[:-1]]          # 已收盘
        tail = self.last_price or float(rows[-1][4])       # 未收盘根用实时价
        closes.append(tail)
        v = rsi_calc(closes, self.period)
        return None if not len(v) or v[-1] != v[-1] else float(v[-1])

    def check(self) -> Optional[Dict]:
        """返回 {'kind':..,'val':..,'prev':..,'side':..} 或 None"""
        self.n_check += 1
        self.last_check = time.time()
        try:
            val = self.value()
        except Exception as e:
            log(f"{WARN} RSI 计算失败: {e}")
            return None
        if val is None:
            return None
        prev = self.prev_val
        self.prev_val = val
        # 滞回：退出到「阈值 ∓ 缓冲」以内才重新武装（防止 69.9↔70.1 抖动刷屏）
        if val > self.low + self.hyst:
            self.armed_low = True
        if val < self.high - self.hyst:
            self.armed_high = True
        if prev is None:                                   # 首轮只记基准，不触发
            # 仅当「启动时已在越界区」才上锁；若在缓冲带内则保持武装，
            # 否则启动后的第一次真实穿越会被吞掉
            if val <= self.low:
                self.armed_low = False
            if val >= self.high:
                self.armed_high = False
            log(f"RSI 通道已就绪：{self.contract} {self.tf} RSI{self.period} = {val:.1f} "
                f"｜ 阈值 <{self.low:g} / >{self.high:g} ｜ 滞回 {self.hyst:g}"
                f"｜ 武装 超卖{'Y' if self.armed_low else 'N'}/超买{'Y' if self.armed_high else 'N'}")
            return None
        hit = None
        if prev >= self.low and val < self.low and self.armed_low:
            hit = "oversold"
        elif prev <= self.high and val > self.high and self.armed_high:
            hit = "overbought"
        if not hit:
            return None
        # 触发后上锁
        if hit == "oversold":
            self.armed_low = False
        else:
            self.armed_high = False
        now = time.time()
        if hit == "oversold":
            if now - self.last_fire_low < self.cooldown:
                log(f"{WARN} RSI 超卖({val:.1f})但冷却中"
                    f"（{self.cooldown-(now-self.last_fire_low):.0f}s 内已触发）→ 跳过")
                return None
            self.last_fire_low = now
        else:
            if now - self.last_fire_high < self.cooldown:
                log(f"{WARN} RSI 超买({val:.1f})但冷却中"
                    f"（{self.cooldown-(now-self.last_fire_high):.0f}s 内已触发）→ 跳过")
                return None
            self.last_fire_high = now
        self.n_alert += 1
        side = "超卖" if hit == "oversold" else "超买"
        return {"kind": "rsi", "side": side, "hit": hit, "val": val, "prev": prev,
                "tf": self.tf, "period": self.period, "low": self.low, "high": self.high,
                "hyst": self.hyst, "rearm": (self.low + self.hyst) if hit == "oversold"
                else (self.high - self.hyst),
                "px": self.last_price, "ts": int(now)}
