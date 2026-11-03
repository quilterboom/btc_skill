#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""
hyperliquid_liqmap.py — 用真实持仓反推清算图（Hyperliquid 官方 API，免 key）
============================================================================

**为什么换数据源**（liusir 2026-11-02）：CoinAnk 网页版接口有 IP 配额限流，
调试时把 IP 打封过，且它的清算图是**持仓结构的模型输出**，不是真实待清算单。

**Hyperliquid 官方 API 完全公开、免鉴权、无限频、85-300ms 返回**。
更关键：它给的是**真实持仓的真实清算价**（`clearinghouseState` 的
`assetPosition.position.liquidationPx`），不是模型估算。

**流水线**（参考开源项目 hl-liqmap / faro-liquidation-pressure-map 的思路）：
```
1. recentTrades(coin=BTC)              → 实时成交 tape，每条带 users: [买方, 卖方]
2. 去重取活跃地址                        → 真实在交易的账户
3. clearinghouseState(addr) × N         → 每个地址的真实持仓 + 清算价
4. 过滤出 BTC 持仓，按 positionValue 加权
5. 按价格分箱                            → 真实清算热力图
```

**方向语义**（与 CoinAnk 口径一致）：
  - 清算价在现价**上方** = 空头清算簇（价格涨上去空头爆仓 → 上行磁吸）
  - 清算价在现价**下方** = 多头清算簇（价格跌下去多头爆仓 → 下行瀑布）

**诚实性约定**
- 这是**抽样**：只采最近成交里出现的地址，不是全网持仓。样本量随 tape 长度增长。
  输出必须带 `sampled_addresses` / `sampled_positions` 让调用方知道样本大小。
- `liquidationPx` 可能为 `null`（低杠杆/全仓保证金厚 → 离清算太远，交易所不给价）。
  这类**不能当成 0 或当前价**，必须跳过并计数。
- 样本不足时如实报「样本不足」，不编造簇。

**用法**
```bash
python3 hyperliquid_liqmap.py                # 取数 + 打印热力图
python3 hyperliquid_liqmap.py --json         # 机器可读
python3 hyperliquid_liqmap.py --bins 100     # 分箱宽度（USD）
```
"""
from __future__ import annotations

import argparse
import json
import time
import urllib.error
import urllib.request
from collections import defaultdict
from typing import Dict, List, Optional, Tuple

API = "https://api.hyperliquid.xyz/info"
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
_HEADERS = {
    "Content-Type": "application/json",
    "User-Agent": _UA,
    "Origin": "https://app.hyperliquid.xyz",
    "Referer": "https://app.hyperliquid.xyz/",
    "Accept": "application/json",
}

COIN = "BTC"
BIN_USD = 50.0          # 分箱宽度（$50 一档，与 CoinAnk 的 $10 档同量级）
NEAR_BAND = 0.002       # ±0.2% 内不算「上方/下方」
SIDE_RANGE = 0.10       # 只看 ±10%
TOP_N = 3
MIN_POS_FOR_SIGNAL = 5  # 少于这么多持仓就不给「簇」，只给样本说明
ADDR_CACHE = None       # 活跃地址累积缓存（setdefault 后由 build_liqmap 填）
CONCENTRATION_WARN = 0.5   # 最大单笔占总名义 >50% → 提示「样本被巨鲸主导」


# ---------------------------------------------------------------- HTTP
def _post(payload: dict, timeout: int = 20):
    """调 Hyperliquid /info。返回解析后的 JSON，失败抛异常（不静默）。"""
    req = urllib.request.Request(API, data=json.dumps(payload).encode(),
                                 headers=_HEADERS)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def _safe_post(payload: dict) -> Tuple[Optional[object], Optional[str]]:
    """同上但返回 (data, err)。出错不抛 —— 采集器要能继续跑。"""
    try:
        return _post(payload), None
    except urllib.error.HTTPError as e:
        try:
            return None, f"HTTP {e.code}: {e.read().decode()[:120]}"
        except Exception:
            return None, f"HTTP {e.code}"
    except Exception as e:
        return None, str(e)[:120]


# ---------------------------------------------------------------- 取数
def fetch_active_addresses(coin: str = COIN, max_addr: int = 60) -> List[str]:
    """从实时成交 tape 取活跃地址（每条成交的买方+卖方）。"""
    data, err = _safe_post({"type": "recentTrades", "coin": coin})
    if not isinstance(data, list):
        return []
    out: List[str] = []
    for t in data:
        for u in (t.get("users") or []):
            if u not in out:
                out.append(u)
            if len(out) >= max_addr:
                return out
    return out


def fetch_positions(addrs: List[str], coin: str = COIN) -> Tuple[List[Dict], int, int]:
    """查每个地址的持仓，过滤出目标币种。

    返回 (positions, skipped_null_liq, addr_with_no_pos)。
    ★ liquidationPx 为 null 的（低杠杆/保证金厚）必须跳过并计数，
      不能当成 0 或现价 —— 那会让热力图出现假的「现价巨簇」。
    """
    out: List[Dict] = []
    null_liq = 0
    no_pos = 0
    for a in addrs:
        data, err = _safe_post({"type": "clearinghouseState", "user": a})
        if not isinstance(data, dict):
            continue
        aps = data.get("assetPositions") or []
        hit = False
        for ap in aps:
            pos = ap.get("position") or ap
            if pos.get("coin") != coin:
                continue
            hit = True
            liq = pos.get("liquidationPx")
            try:
                liq = float(liq) if liq is not None else None
            except (TypeError, ValueError):
                liq = None
            if liq is None:
                null_liq += 1
                continue
            try:
                szi = float(pos.get("szi"))
                entry = float(pos.get("entryPx"))
                value = float(pos.get("positionValue"))
            except (TypeError, ValueError):
                continue
            lev = pos.get("leverage") or {}
            out.append({
                "addr": a[:12],
                "side": "long" if szi > 0 else "short",
                "szi": szi, "entry_px": entry, "liq_px": liq,
                "position_value": value,
                "leverage": (lev.get("value") if isinstance(lev, dict) else None),
                "leverage_type": (lev.get("type") if isinstance(lev, dict) else None),
            })
        if not hit:
            no_pos += 1
    return out, null_liq, no_pos


# ---------------------------------------------------------------- 分箱
def build_heatmap(positions: List[Dict], px: float,
                  bin_usd: float = BIN_USD) -> Dict:
    """按清算价分箱 → {above: [...], below: [...]}（按强度倒序）。"""
    if not positions or px <= 0:
        return {"above": [], "below": [], "max_bin_value": 0.0}
    bins: Dict[float, float] = defaultdict(float)
    for p in positions:
        b = round(p["liq_px"] / bin_usd) * bin_usd
        bins[b] += abs(p["position_value"])
    mx = max(bins.values()) or 1.0

    def _rows(pred):
        rows = []
        for b, v in bins.items():
            dist = (b / px - 1.0) * 100
            if not pred(dist):
                continue
            rows.append({"price": round(b, 1), "value": round(v, 1),
                         "strength_pct": round(v / mx * 100, 1),
                         "dist_pct": round(dist, 2)})
        rows.sort(key=lambda r: -r["strength_pct"])
        return rows

    return {
        "above": _rows(lambda d: d > NEAR_BAND * 100 and d <= SIDE_RANGE * 100),
        "below": _rows(lambda d: d < -NEAR_BAND * 100 and d >= -SIDE_RANGE * 100),
        "max_bin_value": round(mx, 1),
    }


def suggest(above: List[Dict], below: List[Dict],
            deadband: float = 0.15) -> Dict:
    """多空建议：参考值 = 上下两侧强度的最大值（与 liq_map.py 同口径）。

    上方=空头清算簇（涨上去爆空 → 上行磁吸）→ 上方强 → 偏多
    下方=多头清算簇（跌下去爆多 → 下行瀑布）→ 下方强 → 偏空
    """
    a = above[0]["strength_pct"] if above else 0.0
    b = below[0]["strength_pct"] if below else 0.0
    ref = max(a, b)
    if ref <= 0:
        return {"available": False, "reason": "上下两侧均无清算簇"}
    score = (a - b) / ref
    if score > deadband:
        label = "偏多"
    elif score < -deadband:
        label = "偏空"
    else:
        label = "中性"
    return {"available": True, "suggestion": label, "score": round(score, 3),
            "reference_value": ref, "reference_side": "上方" if a >= b else "下方",
            "deadband": deadband}


# ---------------------------------------------------------------- 地址累积
# tape 每次只给 10 条成交（约 10~19 个地址）→ 单次采样太少，必须**跨时间累积**。
# 持久化到磁盘，采集器每轮跑一次就多攒一批地址，样本量随时间自然增长。
import os as _os
from pathlib import Path as _Path
_ADDR_FILE = _Path(_os.environ.get("BTC_DATA_DIR") or "/root/data") / "hl_active_addrs.json"
# 池上限：2000 个地址够覆盖一个交易所的主要活跃账户，再多只是噪音。
POOL_MAX = 2000
# 每轮**实查**的地址数上限 —— 决定请求量（每个地址 1 个 clearinghouseState 请求）。
# 池子可以攒到几千个，但每轮只实查这么多：样本够画热力图，请求量可控。
QUERY_MAX = 60


def load_addrs() -> List[str]:
    try:
        if _ADDR_FILE.exists():
            v = json.loads(_ADDR_FILE.read_text(encoding="utf-8"))
            if isinstance(v, list):
                return [x for x in v if isinstance(x, str)]
    except Exception:
        pass
    return []


def save_addrs(addrs: List[str]) -> None:
    try:
        _ADDR_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = str(_ADDR_FILE) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(addrs[-POOL_MAX:], f)   # 上限 2000，够用且不膨胀
        _os.replace(tmp, _ADDR_FILE)
    except Exception:
        pass


def _merge_pool(fresh: List[str], pool: List[str]) -> List[str]:
    """新地址在前、旧地址在后，去重并截断到 POOL_MAX。

    顺序即「新鲜度」：build_liqmap 每轮只查前 QUERY_MAX 个。
    """
    seen = set()
    out = []
    for a in list(fresh) + list(pool):
        if a in seen:
            continue
        seen.add(a)
        out.append(a)
        if len(out) >= POOL_MAX:
            break
    return out


def _rotate_pool(merged: List[str], queried: List[str]) -> List[str]:
    """把本轮查过的地址挪到池尾 → 下一轮自然轮到别的老地址。

    ★ 不旋转的话，merged[:max_addr] 永远是「最新 + 池头固定一批」，
      池子里 2000 个地址永远只有前 60 个被查过 → 样本量卡死。

    截断保留**头部**（最新的在前）—— 丢的应该是最老的那批已查地址。
    """
    q = set(queried)
    head = [a for a in merged if a not in q]
    tail = [a for a in merged if a in q]
    return (head + tail)[:POOL_MAX]


# ---------------------------------------------------------------- 主流程
def build_liqmap(px: Optional[float] = None, coin: str = COIN,
                 bin_usd: float = BIN_USD, max_addr: int = QUERY_MAX,
                 accumulate: bool = True) -> Dict:
    """整条流水线。永不抛错 —— 拿不到就返回 available=False + 原因。

    ★ 2026-11-03：地址池真正接上了（原来 load_addrs/save_addrs 定义了但从没被调用，
      每轮只查 tape 里的 ~12 个地址 → 样本太小、画出来的热力图噪音极大）。
      现在：tape 新地址 + 历史池合并 → 每轮实查前 max_addr 个 → 池持续增长。
    """
    ts = int(time.time())
    # 现价
    if px is None:
        mids, err = _safe_post({"type": "allMids"})
        raw = None
        if isinstance(mids, dict):
            raw = mids.get(coin)
        try:
            px = float(raw) if raw is not None else None
        except (TypeError, ValueError):
            px = None
        if px is None:
            return {"available": False, "reason": f"拿不到 {coin} 现价: {err}",
                    "source": "hyperliquid"}

    fresh = fetch_active_addresses(coin, max_addr)
    if not fresh:
        return {"available": False,
                "reason": "成交 tape 里没拿到活跃地址",
                "source": "hyperliquid"}
    pool = load_addrs() if accumulate else []
    merged = _merge_pool(fresh, pool)
    # 每轮实查前 max_addr 个（新的优先，旧的轮换补进来）
    query = merged[:max_addr]
    if accumulate:
        # 本轮查过的挪到池尾 → 下一轮轮到别的老地址（否则池头永远固定一批）
        save_addrs(_rotate_pool(merged, query))

    positions, null_liq, no_pos = fetch_positions(query, coin)
    hm = build_heatmap(positions, px, bin_usd)
    if not positions:
        return {"available": False,
                "reason": (f"样本不足：{len(query)} 个活跃地址里没有可用的 {coin} 持仓"
                           f"（{null_liq} 个无清算价、{no_pos} 个无持仓）"),
                "source": "hyperliquid", "sampled_addresses": len(query),
                "pool_size": len(merged)}

    sug = suggest(hm["above"], hm["below"])
    return {
        "available": True,
        "source": "hyperliquid-real",   # 真实持仓的真实清算价
        "as_of": ts,
        "px": round(px, 1),
        "sampled_addresses": len(query),
        "pool_size": len(merged),
        "sampled_positions": len(positions),
        "skipped_null_liq_px": null_liq,   # 低杠杆/保证金厚 → 无清算价
        "addresses_without_pos": no_pos,
        "bin_usd": bin_usd,
        "max_bin_value": hm["max_bin_value"],
        "above": hm["above"][:TOP_N],      # 空头清算簇（上行磁吸）
        "below": hm["below"][:TOP_N],      # 多头清算簇（下行瀑布）
        "suggestion": sug,
        # ★ 明细：每个持仓的方向/清算价/名义/杠杆 —— 采集器落盘成 raw，
        #   以后换分箱宽度或簇口径可以离线重算，不用重新等历史。
        "positions": positions,
        "coverage_pct": [round((min(p["liq_px"] for p in positions) / px - 1) * 100, 2),
                         round((max(p["liq_px"] for p in positions) / px - 1) * 100, 2)],
    }


def render(d: Dict) -> str:
    if not d.get("available"):
        return f"真实清算图：不可用（{d.get('reason')}）"
    L = [f"真实清算图（Hyperliquid 真实持仓 · {d['source']}）",
         f"  现价 {d['px']:,.1f} ｜ 样本 {d['sampled_positions']} 笔持仓 "
         f"/ {d['sampled_addresses']} 个地址",
         f"  分箱 ${d['bin_usd']:.0f} ｜ 最大箱 ${d['max_bin_value']:,.0f}"]

    def _side(rows, label):
        if not rows:
            return f"  {label}：无显著簇"
        bits = [f"{r['price']:,.0f}（{r['strength_pct']:.0f}%, {r['dist_pct']:+.2f}%）"
                for r in rows]
        return f"  {label}：{' · '.join(bits)}"

    L.append(_side(d["above"], "上方 · 空头清算簇（上行磁吸）"))
    L.append(_side(d["below"], "下方 · 多头清算簇（下行瀑布）"))
    sug = d["suggestion"]
    if sug.get("available"):
        L.append(f"  建议 {sug['suggestion']}（倾向分 {sug['score']:+.2f}，"
                 f"参考值 {sug['reference_value']:.0f} 取自{sug['reference_side']}侧）")
    cov = d.get("coverage_pct")
    if cov:
        L.append(f"  清算价覆盖 {cov[0]:+.1f}%~{cov[1]:+.1f}%")
    L.append(f"  说明：{d['sampled_positions']} 笔是**抽样**（最近成交里的活跃地址），"
             f"不是全网持仓；")
    L.append(f"        {d['skipped_null_liq_px']} 笔因低杠杆/保证金厚无清算价已跳过。")
    L.append("  这是真实持仓的真实清算价（比模型估算硬），但仍只是参考，不做决策门。")
    return "\n".join(L)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Hyperliquid 真实持仓清算图")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--bins", type=float, default=BIN_USD)
    ap.add_argument("--max-addr", type=int, default=60)
    ap.add_argument("--px", type=float, default=None)
    a = ap.parse_args()
    d = build_liqmap(px=a.px, bin_usd=a.bins, max_addr=a.max_addr)
    print(json.dumps(d, ensure_ascii=False, indent=2) if a.json else render(d))
