# -*- coding: utf-8 -*-
"""
Gate.io 永续合约行情数据获取模块
=================================
提供三类能力：
  1. fetch_ohlcv()      —— REST 分页拉取完整历史 K 线（自动翻页、自动去重、本地缓存）
  2. fetch_public()     —— 其他公共 REST 接口（资金费率、合约信息、强平单等）
  3. GateWS             —— WebSocket 实时订阅（K线 / 逐笔 / 标记价 / 资金费率 / 强平单）

官方文档: https://www.gate.io/docs/developers/apiv4/zh_CN/
REST 根地址: https://api.gateio.ws/api/v4
WS  地址:    wss://fx-ws.gateio.ws/v4/ws/usdt   (USDT 本位永续)

⚠️ 本模块只支持 USDT 本位（settle=usdt, contract type=direct，如 BTC_USDT）。
   不支持币本位/反向合约（BTC_USD, type=inverse）—— 其计价、张数面值、
   维持保证金率（0.5%）与强平公式全部不同，GateWS 已硬锁 settle="usdt"。

公共接口无需 API Key。
"""
from __future__ import annotations

import json
import os
import time
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional

BASE = "https://api.gateio.ws/api/v4"
# 备用域名（主域名不通时切换）
MIRRORS = [
    "https://api.gateio.ws/api/v4",
    "https://api.gateio.la/api/v4",
    "https://gateapi.io/api/v4",
]

# interval -> 秒
INTERVAL_SEC = {
    "10s": 10, "1m": 60, "5m": 300, "15m": 900, "30m": 1800,
    "1h": 3600, "2h": 7200, "4h": 14400, "6h": 21600, "8h": 28800,
    "12h": 43200, "1d": 86400, "7d": 604800, "30d": 2592000,
}

_HERE = os.path.dirname(os.path.abspath(__file__))          # .../scripts
_SKILL_DIR = os.path.dirname(_HERE)                          # .../btc-gate-intraday-strategy
_PROJECT_DIR = os.path.dirname(os.path.dirname(
    os.path.dirname(_SKILL_DIR)))                            # 兜底（.../.workbuddy/skills/... → 根）


def _detect_project_dir(start: str) -> str:
    """
    从 skill 目录向上找真正的项目根：该层存在 data/ 且里面有 BTC_USDT_*.json 缓存，
    或该层有 .workbuddy / .git。这样 skill 放在 <项目>/skills/... 或
    <项目>/.workbuddy/skills/... 都能定位正确。
    """
    cur = start
    for _ in range(6):
        d = os.path.join(cur, "data")
        if os.path.isdir(d):
            try:
                if any(f.startswith("BTC_USDT_") and f.endswith(".json")
                       for f in os.listdir(d)):
                    return cur
            except Exception:
                pass
            if os.path.isdir(os.path.join(cur, ".workbuddy")) or \
               os.path.isdir(os.path.join(cur, ".git")):
                return cur
        nxt = os.path.dirname(cur)
        if nxt in ("/", "", cur):
            break
        cur = nxt
    return _PROJECT_DIR


# 缓存目录优先级：环境变量 BTC_DATA_DIR > 自动探测的项目根 data/ > 兜底
DATA_DIR = (os.environ.get("BTC_DATA_DIR")
            or os.path.join(_detect_project_dir(_SKILL_DIR), "data"))

REQUEST_LOG: List[str] = []    # 记录本次会话发出的 HTTP 路径（便于统计请求数）


def _http_get(base: str, path: str, params: Optional[Dict] = None, timeout: int = 20) -> Any:
    url = f"{base}{path}"
    if params:
        url += "?" + urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
    req = urllib.request.Request(url, headers={
        "Accept": "application/json",
        "User-Agent": "btc-quant/1.0",
    })
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8")
    return json.loads(raw) if raw else None


def fetch_public(path: str, params: Optional[Dict] = None, base: str = BASE, retries: int = 3) -> Any:
    """调用任意公共 REST 接口，带多域名重试。"""
    REQUEST_LOG.append(path)
    last_err = None
    for attempt in range(retries):
        for b in MIRRORS if attempt == 0 else [base]:
            try:
                return _http_get(b, path, params)
            except Exception as e:  # noqa
                last_err = e
                time.sleep(0.3)
        time.sleep(0.5)
    raise RuntimeError(f"请求失败 {path}: {last_err}")


def _cache_path(contract: str, interval: str, total: int) -> str:
    return os.path.join(DATA_DIR, f"{contract}_{interval}_{total}.json")


def _pack(raw: List[Dict]) -> Dict[int, List]:
    """把 Gate 返回对象规整成 {ts: [ts,o,h,l,c,v,sum]}"""
    out: Dict[int, List] = {}
    for b in raw:
        t = int(b["t"])
        out[t] = [t, float(b["o"]), float(b["h"]), float(b["l"]),
                  float(b["c"]), float(b["v"]), float(b.get("sum", 0))]
    return out


def _drop_unclosed(rows: List[List], step: int) -> List[List]:
    """丢弃尚未收盘的最后一根（避免信号闪烁）"""
    if rows and rows[-1][0] + step > int(time.time()):
        return rows[:-1]
    return rows


def _write_cache(path: str, rows: List[List]) -> None:
    """原子写：先写 .tmp 再 replace，避免中断导致缓存文件损坏"""
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(rows, f)
    os.replace(tmp, path)


def _read_cache(path: str) -> List[List]:
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r") as f:
            rows = json.load(f)
    except Exception:
        return []
    return [r for r in rows if isinstance(r, list) and len(r) >= 6]


def _download_full(contract, interval, total, end_ts=None, verbose=True) -> Dict[int, List]:
    """全量分页下载（Gate 的 from/to 与 limit 不可同时传，只能 to + limit 向前翻页）"""
    step = INTERVAL_SEC[interval]
    end = int(end_ts or time.time())
    end = end - (end % step)
    bars: Dict[int, List] = {}
    page, cur_end, guard = 1000, end + step, 0
    while len(bars) < total and guard < 100:
        guard += 1
        limit = min(page, total - len(bars))
        try:
            raw = fetch_public(
                "/futures/usdt/candlesticks",
                {"contract": contract, "interval": interval, "limit": limit, "to": cur_end},
            )
        except RuntimeError as e:
            if verbose:
                print("  拉取中断:", e)
            break
        if not raw:
            break
        got = _pack(raw)
        if got:
            bars.update(got)
            oldest = min(got)
        else:
            oldest = cur_end
        if len(raw) < limit:      # 已到数据起点
            break
        cur_end = oldest - step
        time.sleep(0.15)          # 礼貌限频（公共接口 ~200次/10s）
    return bars


def fetch_ohlcv(
    contract: str = "BTC_USDT",
    interval: str = "1h",
    total: int = 5000,
    end_ts: Optional[int] = None,
    cache: bool = True,
    refresh: bool = True,
    verbose: bool = False,
) -> List[List]:
    """
    获取 K 线，返回升序列表 [[ts, open, high, low, close, volume, sum], ...]

    缓存策略（默认每次执行都会把数据追到最新）：
      refresh=True + 已有缓存  → **增量更新**：只拉最新 1000 根（1 次 HTTP），
                                与旧缓存按 ts 合并去重，再裁到最新 total 根
      缓存太旧出现缺口        → 自动降级为全量重拉
      cache=False             → 不用缓存，直接拉实时（每周期 1 次请求）

    :param contract: 合约名，如 BTC_USDT（USDT 本位永续）
    :param interval: 10s/1m/5m/15m/30m/1h/4h/8h/1d/...
    :param total:    需要多少根（滚动窗口，只保留最新 total 根）
    :param refresh:  是否联网追加最新数据（False = 只读本地，离线模式）
    :param verbose:  打印更新情况
    """
    if interval not in INTERVAL_SEC:
        raise ValueError(f"不支持的周期: {interval}")
    step = INTERVAL_SEC[interval]

    cpath = _cache_path(contract, interval, total)
    old = _read_cache(cpath) if cache else []
    old.sort(key=lambda r: r[0])
    old_last = old[-1][0] if old else None

    if not cache:
        out = _download_full(contract, interval, total, end_ts, verbose)
        added = len(out)
    elif not old:
        out = _download_full(contract, interval, total, end_ts, verbose)
        added = len(out)
        if verbose:
            print(f"  [{interval}] 首次全量拉取 {added} 根 → {cpath}")
    elif not refresh:
        out = {r[0]: r for r in old}
        added = 0
        if verbose:
            print(f"  [{interval}] 离线模式，沿用缓存 {len(out)} 根")
    else:
        # ---- 增量更新：只拉最新一页，合并去重 ----
        n_fetch = min(1000, max(total, 1))
        try:
            new = _pack(fetch_public(
                "/futures/usdt/candlesticks",
                {"contract": contract, "interval": interval,
                 "limit": n_fetch, "to": int(time.time())},
            ))
        except RuntimeError as e:
            new = {}
            if verbose:
                print(f"  [{interval}] 增量更新失败，沿用缓存: {e}")

        merged = {r[0]: r for r in old}
        if new:
            oldest_new = min(new)
            if oldest_new > old_last + step:
                # 缓存与最新数据之间存在缺口（太久没更新）→ 全量重拉
                if verbose:
                    print(f"  [{interval}] 缓存落后过多（缺口 "
                          f"{(oldest_new - old_last) / step:.0f} 根），全量重拉")
                out = _download_full(contract, interval, total, end_ts, verbose)
                added = len(out)
            else:
                added = sum(1 for t in new if t not in merged)
                merged.update(new)
                out = merged
                if verbose:
                    print(f"  [{interval}] 增量 +{added} 根 "
                          f"（本地 {len(old)} → {len(merged)}）")
        else:
            out = merged
            added = 0

    rows = [out[k] for k in sorted(out)]
    rows = _drop_unclosed(rows, step)
    rows = rows[-total:]                       # 滚动窗口：只保留最新 total 根
    if cache and rows:
        _write_cache(cpath, rows)
    return rows


def fetch_ohlcv_realtime(
    contract: str = "BTC_USDT",
    interval: str = "1h",
    total: int = 5000,
) -> List[List]:
    """
    实时数据（保留未收 K 线）—— 2026-10-08 liusir 决策

    与 fetch_ohlcv 的差异：
      - 不调用 _drop_unclosed → 保留正在收的最后一根 K 线
      - 默认 cache=False → 不污染 cache
      - 用途：实时分析、市场监测；**不能用于生产信号计算**（未收 K 线会闪烁）

    返回：[[ts, open, high, low, close, volume, sum], ...]（含最后一根未收）
    """
    if interval not in INTERVAL_SEC:
        raise ValueError(f"不支持的周期: {interval}")
    step = INTERVAL_SEC[interval]
    # 拉最新 2 根（包含未收）
    try:
        fresh = _pack(fetch_public(
            "/futures/usdt/candlesticks",
            {"contract": contract, "interval": interval, "limit": 2, "to": int(time.time())},
        ))
    except Exception as e:
        # 失败时回退到 fetch_ohlcv
        return fetch_ohlcv(contract, interval, total, cache=False)
    if not fresh:
        return fetch_ohlcv(contract, interval, total, cache=False)
    # 历史数据用 fetch_ohlcv 拿（保留 cache）
    rows_hist = fetch_ohlcv(contract, interval, total, cache=True, refresh=True, verbose=False)
    # fresh 最新一根替换
    fresh_sorted = [fresh[k] for k in sorted(fresh)]
    live_bar = fresh_sorted[-1]
    if not rows_hist:
        return [list(live_bar)]
    # 检查 live_bar 是不是真的"未收"
    last_ts = live_bar[0]
    if last_ts + step > int(time.time()):
        # 替换最后一根
        if rows_hist and rows_hist[-1][0] == last_ts:
            rows = rows_hist[:-1] + [list(live_bar)]
        else:
            rows = rows_hist + [list(live_bar)]
            rows = rows[-total:]
    else:
        rows = rows_hist
    return rows


def cache_freshness(tf_counts: Dict[str, int] = None, contract: str = "BTC_USDT") -> List[Dict]:
    """查看各周期缓存新鲜度（不联网刷新）"""
    tf_counts = tf_counts or {"15m": 8000, "1h": 20000, "4h": 5000, "1d": 1500}
    res = []
    now = int(time.time())
    for tf, total in tf_counts.items():
        p = _cache_path(contract, tf, total)
        rows = _read_cache(p)
        if not rows:
            res.append({"tf": tf, "total": total, "bars": 0, "exists": False,
                        "last_ts": None, "lag_sec": None, "missing_bars": None})
            continue
        rows.sort(key=lambda r: r[0])
        step = INTERVAL_SEC[tf]
        last_ts = rows[-1][0]
        # 已收盘的最新一根应在 now-step 处
        expected = (now // step) * step - step
        missing = int((expected - last_ts) / step)
        res.append({"tf": tf, "total": total, "bars": len(rows), "exists": True,
                    "last_ts": last_ts, "lag_sec": now - (last_ts + step),
                    "missing_bars": max(0, missing)})
    return res


def update_all(tf_counts: Dict[str, int] = None, contract: str = "BTC_USDT",
               force: bool = False, verbose: bool = True) -> Dict[str, List]:
    """批量刷新多个周期；force=True 忽略旧缓存全量重拉"""
    tf_counts = tf_counts or {"15m": 8000, "1h": 20000, "4h": 5000, "1d": 1500}
    out = {}
    order = sorted(tf_counts, key=lambda t: INTERVAL_SEC[t])
    for tf in order:
        total = tf_counts[tf]
        if force:
            p = _cache_path(contract, tf, total)
            if os.path.exists(p):
                os.remove(p)
        rows = fetch_ohlcv(contract, tf, total, cache=True, refresh=True, verbose=verbose)
        out[tf] = rows
    return out


# -------------------- 其他常用公共数据 --------------------

def fetch_funding_rate(contract: str = "BTC_USDT", limit: int = 100) -> List[Dict]:
    """历史资金费率（每 8h 结算一次）。"""
    return fetch_public("/futures/usdt/funding_rate", {"contract": contract, "limit": limit})


def fetch_contract(contract: str = "BTC_USDT") -> Dict:
    """合约元信息：最小变动价位、合约面值、维持保证金率等（USDT 本位）。"""
    return fetch_public(f"/futures/usdt/contracts/{contract}")


def assert_usdt_linear(contract: str = "BTC_USDT") -> Dict:
    """
    断言该合约为 USDT 本位正向合约（type == "direct"），否则抛错。
    返回合约元信息。用于防止误用币本位（type == "inverse"）参数。
    """
    c = fetch_contract(contract)
    if c.get("type") != "direct":
        raise ValueError(
            f"{contract} 不是 USDT 本位正向合约（type={c.get('type')}）。"
            "本 skill 仅支持 USDT 本位（/futures/usdt/*），币本位 inverse 合约"
            "计价单位、张数面值与维持保证金率均不同，请勿混用。")
    return c


def fetch_orderbook(contract: str = "BTC_USDT", limit: int = 20) -> Dict:
    """订单簿快照。"""
    return fetch_public("/futures/usdt/order_book", {"contract": contract, "limit": limit})


def fetch_liq_orders(contract: str = "BTC_USDT", limit: int = 100) -> List[Dict]:
    """强平委托历史（爆仓单）—— 抄底策略的重要参考。"""
    return fetch_public("/futures/usdt/liq_orders", {"contract": contract, "limit": limit})


def fetch_ticker(contract: str = "BTC_USDT") -> List[Dict]:
    """最新行情（含标记价、资金费率、未平仓量）。"""
    return fetch_public("/futures/usdt/tickers", {"contract": contract})


# -------------------- WebSocket 实时订阅 --------------------

class GateWS:
    """
    极简 Gate.io 期货 WebSocket 客户端（公共频道，无需鉴权）

    用法（注意：channel 填具体频道名，不是 futures.subscribe）:
        ws = GateWS()
        ws.on_message = lambda ch, d: print(ch, d)
        ws.subscribe([
            ("futures.candlesticks", ["1h", "BTC_USDT"]),
            ("futures.tickers",      ["BTC_USDT"]),
            ("futures.trades",       ["BTC_USDT"]),
            ("futures.liq_orders",   ["BTC_USDT"]),
            ("futures.funding_rate", ["BTC_USDT"]),
        ])
        ws.run_forever()
    """
    # 仅 USDT 本位；币本位 (/ws/btc) 不在本 skill 支持范围
    URLS = {"usdt": "wss://fx-ws.gateio.ws/v4/ws/usdt"}

    def __init__(self, settle: str = "usdt", on_message=None, ping_interval: int = 10):
        if settle != "usdt":
            raise ValueError("本 skill 仅支持 USDT 本位合约（settle='usdt'）；"
                             "币本位 type=inverse 计价与强平规则不同，不予支持")
        self.url = self.URLS[settle]
        self.on_message = on_message or (lambda ch, d: None)
        self.ping_interval = ping_interval
        self._ws = None
        self._subs: List = []

    def subscribe(self, channels):
        """channels: [(频道名, payload列表), ...]，如 ("futures.tickers", ["BTC_USDT"])"""
        self._subs = channels
        if self._ws:
            self._send_subscribe()

    def _send_subscribe(self):
        for ch, payload in self._subs:
            self._ws.send(json.dumps({
                "time": int(time.time()),
                "channel": ch,
                "event": "subscribe",
                "payload": payload,
            }))

    def run_forever(self):
        import websocket  # pip install websocket-client
        self._ws = websocket.WebSocketApp(
            self.url,
            on_open=lambda w: self._on_open(w),
            on_message=lambda w, m: self._on_msg(w, m),
            on_error=lambda w, e: print("WS error:", e),
            on_close=lambda w, a, b: print("WS closed"),
        )
        self._ws.run_forever(ping_interval=self.ping_interval)

    def _on_open(self, w):
        print(f"[WS] 已连接 {self.url}")
        self._ws = w
        self._send_subscribe()

    def _on_msg(self, w, msg: str):
        try:
            d = json.loads(msg)
        except Exception:
            return
        ev = d.get("event")
        if ev == "subscribe":
            return
        if ev == "update" and d.get("result"):
            ch = d.get("channel", "")
            res = d["result"]
            # tickers / funding_rate 等频道的 result 是列表，逐条回调方便使用
            if isinstance(res, list) and ch != "futures.candlesticks":
                for k in res:
                    self.on_message(ch, k)
            else:
                self.on_message(ch, res)
        elif ev == "error":
            print("[WS] 错误:", d)


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="Gate.io USDT 本位永续 K 线拉取/更新")
    ap.add_argument("interval", nargs="?", default="1h")
    ap.add_argument("total", nargs="?", type=int, default=5000)
    ap.add_argument("--contract", default="BTC_USDT")
    ap.add_argument("--no-refresh", action="store_true", help="离线模式：只读本地缓存")
    ap.add_argument("--force", action="store_true", help="忽略缓存，全量重拉")
    ap.add_argument("--status", action="store_true", help="只看缓存新鲜度，不联网")
    a = ap.parse_args()

    import datetime
    _U = getattr(datetime, "UTC", None) or datetime.timezone.utc

    if a.status:
        print(f"缓存目录: {DATA_DIR}\n")
        print(f"{'周期':<6}{'根数':>8}{'末根(UTC)':>20}{'落后':>12}{'缺口':>8}")
        print("-" * 56)
        for r in cache_freshness({"15m": 8000, "1h": 20000, "4h": 5000, "1d": 1500}, a.contract):
            if not r["exists"]:
                print(f"{r['tf']:<6}{'-':>8}{'（无缓存）':>20}")
                continue
            lag = r["lag_sec"]
            lag_s = f"{lag/3600:.1f}h" if lag >= 3600 else f"{lag/60:.0f}min"
            ts = datetime.datetime.fromtimestamp(r["last_ts"], _U).strftime("%Y-%m-%d %H:%M")
            flag = "" if r["missing_bars"] == 0 else "  ⚠️"
            print(f"{r['tf']:<6}{r['bars']:>8}{ts:>20}{lag_s:>12}{r['missing_bars']:>8}{flag}")
        raise SystemExit(0)

    if a.force:
        p = _cache_path(a.contract, a.interval, a.total)
        if os.path.exists(p):
            os.remove(p)
    bars = fetch_ohlcv(a.contract, a.interval, a.total,
                       cache=True, refresh=not a.no_refresh, verbose=True)
    print(f"\n{a.interval} 共 {len(bars)} 根"
          f"（HTTP 请求 {len(REQUEST_LOG)} 次）")
    if bars:
        t0 = datetime.datetime.fromtimestamp(bars[0][0], _U).strftime("%Y-%m-%d %H:%M")
        t1 = datetime.datetime.fromtimestamp(bars[-1][0], _U).strftime("%Y-%m-%d %H:%M")

# ============================================
# 新增数据源: OI / 盘口 / 多空比 (2026-10-07 liusir B 方案)
# ============================================

# OI 历史（用于计算变化率）
_OI_HISTORY = []  # [(timestamp, oi_value)]


def fetch_oi(contract="BTC_USDT"):
    """获取当前 OI + 5分钟前 OI，返回变化率"""
    import time
    now = time.time()

    # 拉当前 OI
    stats = fetch_public("/futures/usdt/contract_stats", {"contract": contract, "limit": 1})
    if not stats:
        return {"oi_current": None, "oi_change_pct": 0, "signal": 0}

    oi_current = float(stats[0].get("open_interest", 0))
    lsr_taker = float(stats[0].get("lsr_taker", 1))
    lsr_account = float(stats[0].get("lsr_account", 1))

    # 存历史
    _OI_HISTORY.append((now, oi_current))

    # 只保留最近 10 分钟
    cutoff = now - 600
    while _OI_HISTORY and _OI_HISTORY[0][0] < cutoff:
        _OI_HISTORY.pop(0)

    # 找 5 分钟前的 OI
    target_time = now - 300
    oi_5m_ago = None
    for ts, val in reversed(_OI_HISTORY):
        if ts <= target_time:
            oi_5m_ago = val
            break

    if oi_5m_ago and oi_5m_ago > 0:
        oi_change_pct = (oi_current - oi_5m_ago) / oi_5m_ago * 100
    else:
        oi_change_pct = 0

    # 信号: OI 变化率 > 0.5% → 资金进场 +1
    signal = 1 if oi_change_pct > 0.5 else 0

    return {
        "oi_current": oi_current,
        "oi_change_pct": oi_change_pct,
        "lsr_taker": lsr_taker,
        "lsr_account": lsr_account,
        "signal": signal
    }


def fetch_orderbook_imbalance(contract="BTC_USDT", limit=20):
    """获取盘口买卖比"""
    ob = fetch_public("/futures/usdt/order_book", {"contract": contract, "limit": limit})
    if not ob:
        return {"bid_vol": 0, "ask_vol": 0, "ratio": 1.0, "signal_long": 0, "signal_short": 0}

    bids = ob.get("bids", [])
    asks = ob.get("asks", [])

    bid_vol = sum(float(b.get("s", 0)) for b in bids)
    ask_vol = sum(float(a.get("s", 0)) for a in asks)

    ratio = bid_vol / ask_vol if ask_vol > 0 else float("inf")

    # 信号: 买卖比 > 1.5 → 做多 +1; < 0.67 → 做空 +1
    signal_long = 1 if ratio > 1.5 else 0
    signal_short = 1 if ratio < 0.67 else 0

    return {
        "bid_vol": bid_vol,
        "ask_vol": ask_vol,
        "ratio": ratio,
        "signal_long": signal_long,
        "signal_short": signal_short
    }


def fetch_lsr_signal(contract="BTC_USDT"):
    """获取多空比信号（反指逻辑）"""
    stats = fetch_public("/futures/usdt/contract_stats", {"contract": contract, "limit": 1})
    if not stats:
        return {"lsr": 1.0, "signal_long": 0, "signal_short": 0}

    lsr_taker = float(stats[0].get("lsr_taker", 1))
    lsr_account = float(stats[0].get("lsr_account", 1))

    # 用 taker 比（更敏感）
    lsr = lsr_taker

    # 反指逻辑:
    # 2.0-3.0: 正常看多 → 做多 +1
    # >3.0: 过热 → 做空 +1 (反指)
    # 0.33-0.5: 正常看空 → 做空 +1
    # <0.33: 过热 → 做多 +1 (反指)

    if 2.0 <= lsr <= 3.0:
        signal_long, signal_short = 1, 0
    elif lsr > 3.0:
        signal_long, signal_short = 0, 1  # 反指做空
    elif 0.33 <= lsr <= 0.5:
        signal_long, signal_short = 0, 1
    elif lsr < 0.33:
        signal_long, signal_short = 1, 0  # 反指做多
    else:
        signal_long, signal_short = 0, 0  # 中间区域，不加分

    return {
        "lsr": lsr,
        "signal_long": signal_long,
        "signal_short": signal_short
    }
