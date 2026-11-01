#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
liq_map.py — CoinAnk 清算地图取数 + 策略参考（2026-11-01 liusir 要求）

数据源：CoinAnk OpenAPI `GET /api/liqMap/getLiqMap`
  https://open-api.coinank.com/api/liqMap/getLiqMap?symbol=BTCUSDT&exchange=Binance&interval=1d
  响应：{success, code, data: {symbol, prices[], lastPrice, lastIndex, x25[]..x100[]}}
  prices = 价格档位（整数），x25..x100 = 各杠杆倍数的清算强度（与 prices 对齐）
  ★ 需要 apikey（header），且是 Plan4/VIP4 套餐 —— key 从环境变量 COINANK_API_KEY 读。
    没有 key / 拉不到 → 返回 available=False，**绝不编造数据**（拿不到就如实说拿不到）。

定位：**只做参考输入，不做决策门**。清算簇是「价格磁吸/瀑布风险」的上下文，
挂进信号 meta（liq_ref）供回测归因；上不上决策杠杆必须另行回测（没数字不下结论）。

用法：
    from liq_map import liq_reference
    ref = liq_reference(last_price=80000.0)
    if ref["available"]:
        ref["clusters_above"] / ref["clusters_below"]   # [{price, strength_pct, dist_pct}]
"""
from __future__ import annotations

import json
import os
import time
import urllib.request
from pathlib import Path
from typing import Dict, List, Optional

API_URL = "https://open-api.coinank.com/api/liqMap/getLiqMap"
CACHE_PATH = Path(os.environ.get("BTC_DATA_DIR") or "/root/data") / "liq_map_cache.json"
CACHE_TTL = 600          # 10min：清算地图变化慢，别打爆上游
DEFAULT_SYMBOL = "BTCUSDT"
DEFAULT_EXCHANGE = "Binance"
DEFAULT_INTERVAL = "1d"
LEVERAGE_KEYS = ("x25", "x30", "x40", "x50", "x60", "x70", "x80", "x90", "x100")
NEAR_BAND = 0.002        # 距现价 ±0.2% 内的桶不算「上方/下方」簇
SIDE_RANGE = 0.10        # 只看 ±10% 范围内的簇
TOP_N = 3                # 每侧最强 N 个簇


def _api_key() -> str:
    return (os.environ.get("COINANK_API_KEY") or "").strip()


def _unavailable(reason: str) -> Dict:
    return {"available": False, "reason": reason, "source": "coinank"}


def fetch_liq_map(symbol: str = DEFAULT_SYMBOL, exchange: str = DEFAULT_EXCHANGE,
                  interval: str = DEFAULT_INTERVAL, force: bool = False) -> Dict:
    """拉清算地图原始数据。带文件缓存（TTL 10min，跨 scan 子进程共享）。

    永不抛错：失败返回 {"available": False, "reason": ...}。
    """
    key = _api_key()
    if not key:
        return _unavailable("COINANK_API_KEY 未配置（CoinAnk Plan4/VIP4 apikey）")

    # 文件缓存（scan.py 是每触发一个子进程，内存缓存没用）
    if not force:
        try:
            if CACHE_PATH.exists():
                raw = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
                if (raw.get("symbol") == symbol and raw.get("exchange") == exchange
                        and time.time() - float(raw.get("ts") or 0) < CACHE_TTL
                        and raw.get("data")):
                    out = dict(raw["data"])
                    out["available"] = True
                    out["source"] = "coinank"
                    out["cached"] = True
                    return out
        except Exception:
            pass

    url = f"{API_URL}?symbol={symbol}&exchange={exchange}&interval={interval}"
    try:
        req = urllib.request.Request(url, headers={"apikey": key})
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        return _unavailable(f"请求失败: {e}")
    data = body.get("data") if isinstance(body, dict) else None
    if not body or not body.get("success") or not isinstance(data, dict) or not data.get("prices"):
        code = body.get("code") if isinstance(body, dict) else "?"
        return _unavailable(f"上游返回无数据（code={code}，套餐权限或参数问题）")

    try:
        CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = str(CACHE_PATH) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"ts": time.time(), "symbol": symbol, "exchange": exchange,
                       "data": data}, f)
        os.replace(tmp, CACHE_PATH)
    except Exception:
        pass  # 缓存写失败不影响使用
    out = dict(data)
    out["available"] = True
    out["source"] = "coinank"
    out["cached"] = False
    return out


def liq_reference(last_price: Optional[float], symbol: str = DEFAULT_SYMBOL,
                  exchange: str = DEFAULT_EXCHANGE, interval: str = DEFAULT_INTERVAL) -> Dict:
    """给策略的清算地图参考：现价上方/下方 ±10% 内最强清算簇。

    返回：
      available=False → {"available": False, "reason": ...}（调用方如实显示不可用）
      available=True  → {
        "last_price": 上游 lastPrice,
        "clusters_above": [{price, strength_pct, dist_pct}]  按强度降序,
        "clusters_below": 同上,
        "max_liq": 全图最大桶强度（strength_pct 的分母）,
        "as_of": 数据时间,
      }
    ★ 强度是相对值（占全图最大桶的 %），不是精确合约数——CoinAnk 明确说
      清算图展示的是「相对强度」而非精确待清算张数。
    """
    m = fetch_liq_map(symbol=symbol, exchange=exchange, interval=interval)
    if not m.get("available"):
        return m
    try:
        prices = [float(p) for p in m.get("prices") or []]
        totals = [0.0] * len(prices)
        for k in LEVERAGE_KEYS:
            arr = m.get(k) or []
            for i, v in enumerate(arr[:len(totals)]):
                try:
                    totals[i] += float(v)
                except (TypeError, ValueError):
                    continue
        px = float(last_price or m.get("lastPrice") or 0)
        if px <= 0 or not prices:
            return _unavailable("无现价基准或价格档位为空")
        mx = max(totals) or 1.0
        above, below = [], []
        for p, s in zip(prices, totals):
            if s <= 0:
                continue
            d = p / px - 1.0
            if d > NEAR_BAND and d <= SIDE_RANGE:
                above.append({"price": p, "strength_pct": round(s / mx * 100, 1),
                              "dist_pct": round(d * 100, 2)})
            elif d < -NEAR_BAND and d >= -SIDE_RANGE:
                below.append({"price": p, "strength_pct": round(s / mx * 100, 1),
                              "dist_pct": round(d * 100, 2)})
        above.sort(key=lambda z: -z["strength_pct"])
        below.sort(key=lambda z: -z["strength_pct"])
        return {
            "available": True,
            "source": "coinank",
            "last_price": px,
            "clusters_above": above[:TOP_N],
            "clusters_below": below[:TOP_N],
            "max_liq": mx,
            "as_of": int(time.time()),
        }
    except Exception as e:
        return _unavailable(f"解析失败: {e}")


def format_reference(ref: Dict, side: str = "long") -> str:
    """渲染成一行/一块中文参考文本（拿不到就如实说）。"""
    if not ref or not ref.get("available"):
        return f"清算地图：不可用（{(ref or {}).get('reason', '未知原因')}）"
    def _fmt(clusters: List[Dict]) -> str:
        if not clusters:
            return "无显著簇"
        return " · ".join(f"{c['price']:,.0f}(强度{c['strength_pct']:.0f}%,{c['dist_pct']:+.1f}%)"
                          for c in clusters)
    return (f"清算地图参考（{ref['source']}）：上方 {len(ref['clusters_above'])} 簇 {_fmt(ref['clusters_above'])} ｜ "
            f"下方 {len(ref['clusters_below'])} 簇 {_fmt(ref['clusters_below'])}")


if __name__ == "__main__":
    # 手动探针：python3 liq_map.py [现价]
    import sys
    px = float(sys.argv[1]) if len(sys.argv) > 1 else None
    r = liq_reference(px)
    print(json.dumps(r, ensure_ascii=False, indent=2))
    print(format_reference(r))
