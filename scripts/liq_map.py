#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
liq_map.py — CoinAnk 清算地图取数 + 策略参考（2026-11-02 改为网页版数据源）

★ 数据源变更（2026-11-02，liusir 要求「用抓取网页数据的方式获取」）
──────────────────────────────────────────────────────────────
原方案走 CoinAnk 官方 OpenAPI `https://open-api.coinank.com/api/liqMap/getLiqMap`，
需要 apikey 且是 Plan4/VIP4 套餐 → 拿不到 key 就一直「不可用」。

实际查明：**网页版图表用的是同一个接口路径，只是换了 host + 自带前端鉴权**：
  - host: `https://api.coinank.com`（网页端 axios baseURL，非 open-api）
  - 路径: `/api/liqMap/getLiqMap`（单交易所）/ `/api/liqMap/getAggLiqMap`（多交易所聚合）
  - 鉴权: header `coinank-apikey` = 前端 JS 里写死的常量 + 当前时间戳算出来的
          （算法见 `_derive_apikey`），有效期约 ±4 分钟 → **每次请求现算**
  - 另外必须带 header `client: web` 和 `web-version: 102`（缺任一 → system error）

→ 不需要付费 apikey，也不需要登录，直接可用。响应结构与原 OpenAPI **完全一致**
  （symbol / prices[] / lastPrice / lastIndex / x25..x100[]），所以解析逻辑不变。

若将来配了 COINANK_API_KEY（买了 Plan4），仍优先走官方 OpenAPI（更稳定、属于官方授权），
web 源作为兜底。

定位：**只做参考输入，不做决策门**。清算簇是「价格磁吸/瀑布风险」的上下文，
挂进信号 meta（liq_ref）供回测归因；上不上决策杠杆必须另行回测（没数字不下结论）。

用法：
    from liq_map import liq_reference
    ref = liq_reference(last_price=80000.0)
    if ref["available"]:
        ref["clusters_above"] / ref["clusters_below"]   # [{price, strength_pct, dist_pct, ...}]

术语（按 CoinAnk 官方读图说明，2026-11-02 页面原文核对）：
  - 现价**下方**的清算簇 = **多单**清算簇（多头被清算的位置）
  - 现价**上方**的清算簇 = **空单**清算簇（空头被清算的位置）
  - 强度是**相对值**（占全图最大桶的 %），不是精确待清算张数/金额。
"""
from __future__ import annotations

import base64
import json
import os
import re
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

try:
    from typing import TypeGuard
except ImportError:            # Python < 3.10
    from typing_extensions import TypeGuard  # type: ignore

# ── 数据源 ────────────────────────────────────────────────────────────────
WEB_API_BASE = "https://api.coinank.com"
OPEN_API_BASE = "https://open-api.coinank.com"
SINGLE_PATH = "/api/liqMap/getLiqMap"      # 单交易所（含杠杆分组 x25..x100）
AGG_PATH = "/api/liqMap/getAggLiqMap"       # 多交易所聚合（baseCoin=BTC）

DATA_DIR = Path(os.environ.get("BTC_DATA_DIR") or "/root/data")
CACHE_PATH = DATA_DIR / "liq_map_cache.json"
AUTH_CACHE_PATH = DATA_DIR / "coinank_auth.json"
CACHE_TTL = 600          # 10min：清算地图变化慢，别打爆上游
AUTH_CACHE_TTL = 7 * 86400   # 7 天：前端常量一般不变，变了会自动重新发现

DEFAULT_SYMBOL = "BTCUSDT"
DEFAULT_EXCHANGE = "Binance"
DEFAULT_INTERVAL = "1d"
DEFAULT_BASE_COIN = "BTC"

LEVERAGE_KEYS = ("x25", "x30", "x40", "x50", "x60", "x70", "x80", "x90", "x100")
# CoinAnk 图例分组（页面读图说明原文）：蓝=低杠杆 25–40x，紫=中 50–70x，橙=高 80–100x
LEVERAGE_GROUPS = {
    "low": ("x25", "x30", "x40"),
    "mid": ("x50", "x60", "x70"),
    "high": ("x80", "x90", "x100"),
}
AGG_EXCHANGES = ("Binance", "Bybit", "Okex", "Aster", "Hyperliquid", "Lighter")

NEAR_BAND = 0.002        # 距现价 ±0.2% 内的桶不算「上方/下方」簇
SIDE_RANGE = 0.10        # 只看 ±10% 范围内的簇（实际会被数据覆盖范围裁剪）
TOP_N = 3                # 每侧最强 N 个簇
MIN_SEP_USD = 200        # 簇之间最小价格间隔（原始曲线 $10 一档，相邻档是同一簇）
SMOOTH_USD = 6.0        # 峰值检测前的平滑半窗（美元，跨档位间距通用）
CLUSTER_RAW_WINDOW_USD = 12.0   # 回退到原始峰值时的窗口半宽（美元）

# ── 前端鉴权материал（来自 coinank.com 前端 JS bundle）────────────────────
# 这些常量在网页前端 bundle 里写死；CoinAnk 改了就通过 _discover_auth() 重新发现。
_AUTH_SEED = "b2d903dd-b31e-c547-d299-b6d07b7631ab"
_AUTH_SALT = "2222222222222"
_AUTH_SUFFIX = "347"
_AUTH_VERSION = "102"
_AUTH_CLIENT = "web"
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
_LIQ_MAP_PAGE = "https://coinank.com/zh/chart/derivatives/liq-map/binance/btcusdt/1d"


# ── 基础工具 ──────────────────────────────────────────────────────────────
def _api_key() -> str:
    """官方 OpenAPI apikey（可选）。配了就优先走官方（Plan4 套餐才有效）。"""
    return (os.environ.get("COINANK_API_KEY") or "").strip()


def _unavailable(reason: str) -> Dict:
    return {"available": False, "reason": reason, "source": "coinank"}


def _valid_payload(data: Any) -> TypeGuard[Dict]:
    """校验清算地图 payload：prices 必须是非空数字列表。

    上游/畸形响应都从这里挡掉 —— 宁可报「不可用」，也不能拿坏数据算出假簇。
    """
    if not isinstance(data, dict):
        return False
    prices = data.get("prices")
    if not isinstance(prices, list) or not prices:
        return False
    for p in prices[:50]:            # 抽查前 50 个即可判类型
        if isinstance(p, bool) or not isinstance(p, (int, float)):
            return False
    return True


# 请求预算：单次采集最多发几个 HTTP 请求。
# ★ 教训（2026-11-02 15:20）：上游按 IP 配额限流，测试时高频打了几十次 →
#   整个 /api/* 命名空间被封（code=403 "please sub api to get data"），
#   浏览器真实会话也一样挂（说明是 IP 级，不是鉴权问题）。
#   采集器必须**每轮只发固定几个请求**（agg + single 各 1 个），失败时也不许重试放大。
MAX_REQUESTS_PER_COLLECT = 2

# 配额封禁识别：这两个 msg 与「鉴权失败」是**不同**的错误形状，必须分开。
#   system error                        → 鉴权/常量过期 → 触发常量重发现
#   please sub api to get data (403)   → 配额封禁 → 退避，绝不重试
QUOTA_MSGS = ("please sub api to get data",)
AUTH_MSGS = ("system error",)


class QuotaBlocked(Exception):
    """上游配额封禁（IP 级）。→ 退避，不要重试。"""


# 配额封禁退避：封禁期间**一个 HTTP 请求都不发**。
# 退避窗口随连续封禁次数指数增长（10min → 30min → 2h → 6h，上限 6h）。
QUOTA_BACKOFF_BASE = 600
QUOTA_BACKOFF_MAX = 6 * 3600
QUOTA_STATE_PATH = Path(os.environ.get("BTC_DATA_DIR") or "/root/data") / "liq_map_quota_state.json"


def _quota_state() -> Dict:
    try:
        if QUOTA_STATE_PATH.exists():
            st = json.loads(QUOTA_STATE_PATH.read_text(encoding="utf-8"))
            return st if isinstance(st, dict) else {}
    except Exception:
        pass
    return {}


def _quota_save(st: Dict) -> None:
    try:
        QUOTA_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = str(QUOTA_STATE_PATH) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(st, f)
        os.replace(tmp, QUOTA_STATE_PATH)
    except Exception:
        pass


def quota_blocked_until() -> float:
    """返回退避截止时间戳（0 = 未封禁）。"""
    st = _quota_state()
    until = float(st.get("until") or 0)
    return until if until > _now() else 0.0


def _quota_record_block() -> int:
    """记录一次封禁，返回下次允许请求的时间戳。"""
    st = _quota_state()
    streak = int(st.get("streak") or 0) + 1
    delay = min(QUOTA_BACKOFF_BASE * (3 ** (streak - 1)), QUOTA_BACKOFF_MAX)
    until = _now() + delay
    _quota_save({"streak": streak, "until": until, "last_block": _now()})
    return until


def _quota_clear() -> None:
    if _quota_state():
        _quota_save({})


def _now() -> float:
    return time.time()


# ── 前端鉴权材料：常量 + 兜底自动发现 ─────────────────────────────────────
def _auth_material() -> Dict:
    """取 {seed, salt, suffix, version, client}。优先用本地缓存/内置值。

    前端常量被 CoinAnk 改掉时（表现为接口一律返回 system error），
    由 _discover_auth() 重新从线上 bundle 里挖出来并落盘。
    """
    mat = {"seed": _AUTH_SEED, "salt": _AUTH_SALT, "suffix": _AUTH_SUFFIX,
           "version": _AUTH_VERSION, "client": _AUTH_CLIENT}
    try:
        if AUTH_CACHE_PATH.exists():
            raw = json.loads(AUTH_CACHE_PATH.read_text(encoding="utf-8"))
            if _now() - float(raw.get("ts") or 0) < AUTH_CACHE_TTL:
                for k in ("seed", "salt", "suffix", "version", "client"):
                    if raw.get(k):
                        mat[k] = str(raw[k])
    except Exception:
        pass
    return mat


def _save_auth_material(mat: Dict) -> None:
    try:
        AUTH_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = str(AUTH_CACHE_PATH) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"ts": _now(), **mat}, f)
        os.replace(tmp, AUTH_CACHE_PATH)
    except Exception:
        pass


def _derive_apikey(mat: Dict) -> str:
    """复刻前端 `Kb()`：base64( seed 变形 + '|' + (now_ms + salt) + suffix )

    前端原文（压缩后）：
        $R(e) = e.substring(0,8)
        NR(e,t) = e.replace(t,"").concat(t)      # 把前 8 位挪到末尾
        zR() = String(Date.now() + <salt>)
        qR(e) = e.concat("<suffix>")
        jR(e,t) = e + "|" + t
        FR(e) = btoa(e)
    """
    seed = str(mat["seed"])
    head = seed[:8]
    body = seed.replace(head, "") + head
    n = str(int(_now() * 1000) + int(mat["salt"])) + str(mat["suffix"])
    raw = body + "|" + n
    return base64.b64encode(raw.encode("utf-8")).decode("ascii")


def _discover_auth() -> Optional[Dict]:
    """从线上页面 bundle 里重新发现前端鉴权常量。

    页面 HTML 里列出全部 chunk；含 `coinank-apikey` 的 chunk 同时带 `MR="..."` 常量。
    挖到后落盘缓存。失败返回 None（调用方继续用旧材料）。
    """
    try:
        req = urllib.request.Request(_LIQ_MAP_PAGE, headers={
            "User-Agent": _UA,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        })
        with urllib.request.urlopen(req, timeout=20) as resp:
            html = resp.read().decode("utf-8", "replace")
    except Exception:
        return None
    chunks = sorted(set(re.findall(r"/_nuxt/[A-Za-z0-9_.-]+\.js", html)))
    if not chunks:
        return None

    uuid_re = re.compile(r'MR\s*=\s*"([^"]+)"')
    salt_re = re.compile(r"Date\.now\(\)\s*\+\s*(\d+)")
    ver_re = re.compile(r'Gb\s*=\s*"(\d+)"')
    suffix_re = re.compile(r'\.concat\("(\d{1,4})"\)')

    from concurrent.futures import ThreadPoolExecutor

    def _grab(path: str) -> Optional[str]:
        try:
            r = urllib.request.Request("https://s.coinank.com" + path, headers={"User-Agent": _UA})
            with urllib.request.urlopen(r, timeout=15) as resp:
                return resp.read().decode("utf-8", "replace")
        except Exception:
            return None

    with ThreadPoolExecutor(max_workers=12) as pool:
        for text in pool.map(_grab, chunks):
            if not text or "coinank-apikey" not in text:
                continue
            m = uuid_re.search(text)
            if not m:
                continue
            seed = m.group(1)
            sm = salt_re.search(text)
            vm = ver_re.search(text)
            salt = sm.group(1) if sm else _AUTH_SALT
            version = vm.group(1) if vm else _AUTH_VERSION
            cands = [_AUTH_SUFFIX] + [c for c in suffix_re.findall(text) if c != _AUTH_SUFFIX]
            mat = {"seed": seed, "salt": salt, "suffix": _AUTH_SUFFIX,
                   "version": version, "client": _AUTH_CLIENT}
            if _probe(mat):
                _save_auth_material(mat)
                return mat
            for suf in cands:
                mat["suffix"] = suf
                if _probe(mat):
                    _save_auth_material(mat)
                    return mat
            mat["suffix"] = _AUTH_SUFFIX
    return None


# ── HTTP ──────────────────────────────────────────────────────────────────
def _http_json(url: str, headers: Dict[str, str], timeout: int = 20) -> Tuple[bool, Dict]:
    """返回 (是否拿到可解析响应体, 响应体 dict)。网络异常不抛错。"""
    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", "replace")
        try:
            return True, json.loads(body)
        except Exception:
            return False, {}
    except urllib.error.HTTPError as e:
        try:
            body = e.read().decode("utf-8", "replace")
            return False, json.loads(body)
        except Exception:
            return False, {}
    except Exception:
        return False, {}


def _probe(mat: Dict) -> bool:
    """校验一份鉴权材料能否打通接口（用于自动发现时试候选）。"""
    ok, body = _http_json(
        f"{WEB_API_BASE}{SINGLE_PATH}?exchange={DEFAULT_EXCHANGE}&symbol={DEFAULT_SYMBOL}&interval={DEFAULT_INTERVAL}",
        _web_headers(mat))
    return bool(ok and isinstance(body, dict) and body.get("success") and
                isinstance(body.get("data"), dict) and body["data"].get("prices"))


def _web_headers(mat: Dict) -> Dict[str, str]:
    return {
        "coinank-apikey": _derive_apikey(mat),
        "client": str(mat["client"]),
        "web-version": str(mat["version"]),
        "token": "",
        "User-Agent": _UA,
        "Referer": _LIQ_MAP_PAGE,
        "Origin": "https://coinank.com",
        "Accept": "application/json",
    }


# ── 取数 ──────────────────────────────────────────────────────────────────
def _cache_read(kind: str, symbol: str, exchange: str) -> Optional[Dict]:
    try:
        if not CACHE_PATH.exists():
            return None
        raw = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
        blob = (raw.get("data") or {}).get(kind)
        if not blob:
            return None
        if _now() - float(blob.get("ts") or 0) >= CACHE_TTL:
            return None
        if blob.get("symbol") != symbol or blob.get("exchange") != exchange:
            return None
        if not _valid_payload(blob.get("payload")):
            return None
        return blob
    except Exception:
        return None


def _cache_write(kind: str, symbol: str, exchange: str, data: Dict) -> None:
    try:
        raw = {"ts": _now(), "data": {}}
        if CACHE_PATH.exists():
            try:
                raw = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
                raw.setdefault("data", {})
            except Exception:
                pass
        raw["data"][kind] = {"ts": _now(), "symbol": symbol, "exchange": exchange, "payload": data}
        CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = str(CACHE_PATH) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(raw, f)
        os.replace(tmp, CACHE_PATH)
    except Exception:
        pass


def _fetch_open_api(path: str, params: Dict, key: str) -> Dict:
    qs = "&".join(f"{k}={v}" for k, v in params.items())
    ok, body = _http_json(f"{OPEN_API_BASE}{path}?{qs}", {"apikey": key})
    if not ok or not isinstance(body, dict) or not body.get("success"):
        return {}
    data = body.get("data")
    return data if isinstance(data, dict) else {}


def _fetch_web(path: str, params: Dict, mat: Dict) -> Tuple[Dict, bool]:
    """web 源取数。返回 (data, 是否疑似鉴权失败)。

    鉴权失败表现为 success=false + msg='system error' → 触发重新发现常量。
    ★ 配额封禁（msg='please sub api to get data', code=403）**不是**鉴权问题 →
      抛 QuotaBlocked，调用方必须退避而不是重试（重试只会加深封禁）。
    """
    qs = "&".join(f"{k}={v}" for k, v in params.items())
    ok, body = _http_json(f"{WEB_API_BASE}{path}?{qs}", _web_headers(mat))
    if not isinstance(body, dict):
        return {}, False
    if body.get("success"):
        data = body.get("data")
        if _valid_payload(data):
            return dict(data), False
        return {}, False
    msg = str(body.get("msg") or "").lower()
    if any(q in msg for q in QUOTA_MSGS):
        raise QuotaBlocked(f"上游配额封禁: {body.get('msg')}")
    auth_fail = any(a in msg for a in AUTH_MSGS) or body.get("code") in ("403", 403)
    return {}, auth_fail


def fetch_liq_map(symbol: str = DEFAULT_SYMBOL, exchange: str = DEFAULT_EXCHANGE,
                  interval: str = DEFAULT_INTERVAL, force: bool = False) -> Dict:
    """拉单交易所清算地图原始数据（形状与官方 OpenAPI 一致）。

    带文件缓存（TTL 10min，跨 scan 子进程共享）。永不抛错：失败返回 available=False。
    """
    key = _api_key()
    cached = None if force else _cache_read("single", symbol, exchange)
    if cached is not None:
        out = dict(cached["payload"])
        out.update({"available": True, "source": "coinank", "cached": True})
        return out

    params = {"exchange": exchange, "symbol": symbol, "interval": interval}
    data: Dict = {}
    source = "coinank-web"
    blocked_until = quota_blocked_until()
    if blocked_until:
        return _unavailable(f"上游配额封禁中，退避至 "
                            f"{time.strftime('%H:%M:%S', time.localtime(blocked_until))}")
    if key:
        data = _fetch_open_api(SINGLE_PATH, params, key)
        if data:
            source = "coinank-openapi"
    if not data:
        mat = _auth_material()
        try:
            data, auth_fail = _fetch_web(SINGLE_PATH, params, mat)
        except QuotaBlocked:
            until = _quota_record_block()
            return _unavailable(f"上游配额封禁，退避至 "
                                f"{time.strftime('%H:%M:%S', time.localtime(until))}")
        if auth_fail and not data:
            fresh = _discover_auth()
            if fresh:
                try:
                    data, _ = _fetch_web(SINGLE_PATH, params, fresh)
                except QuotaBlocked:
                    until = _quota_record_block()
                    return _unavailable(f"上游配额封禁，退避至 "
                                        f"{time.strftime('%H:%M:%S', time.localtime(until))}")
    if data:
        _quota_clear()
    if not data or not _valid_payload(data):
        return _unavailable("上游无数据（CoinAnk 接口未返回清算地图）")

    _cache_write("single", symbol, exchange, data)
    out = dict(data)
    out.update({"available": True, "source": source, "cached": False})
    return out


def fetch_agg_liq_map(base_coin: str = DEFAULT_BASE_COIN, interval: str = DEFAULT_INTERVAL,
                      force: bool = False) -> Dict:
    """拉多交易所聚合清算地图（网页版下方那张「BTC 清算地图」）。

    返回 data 含 prices[] / lastPrice / lastIndex + 每个交易所一条等长数组。
    """
    cached = None if force else _cache_read("agg", base_coin, base_coin)
    if cached is not None:
        out = dict(cached["payload"])
        out.update({"available": True, "source": "coinank", "cached": True})
        return out

    params = {"baseCoin": base_coin, "interval": interval}
    blocked_until = quota_blocked_until()
    if blocked_until:
        return _unavailable(f"上游配额封禁中，退避至 "
                            f"{time.strftime('%H:%M:%S', time.localtime(blocked_until))}")
    mat = _auth_material()
    try:
        data, auth_fail = _fetch_web(AGG_PATH, params, mat)
    except QuotaBlocked:
        until = _quota_record_block()
        return _unavailable(f"上游配额封禁，退避至 "
                            f"{time.strftime('%H:%M:%S', time.localtime(until))}")
    source = "coinank-web"
    if auth_fail and not data:
        fresh = _discover_auth()
        if fresh:
            try:
                data, _ = _fetch_web(AGG_PATH, params, fresh)
            except QuotaBlocked:
                until = _quota_record_block()
                return _unavailable(f"上游配额封禁，退避至 "
                                    f"{time.strftime('%H:%M:%S', time.localtime(until))}")
    if data:
        _quota_clear()
    if not data or not _valid_payload(data):
        return _unavailable("上游无数据（CoinAnk 聚合清算地图未返回）")

    _cache_write("agg", base_coin, base_coin, data)
    out = dict(data)
    out.update({"available": True, "source": source, "cached": False})
    return out


# ── 峰值/簇检测 ───────────────────────────────────────────────────────────
def _smooth(vals: List[float], prices: List[float], half_window_usd: float = SMOOTH_USD) -> List[float]:
    """按**价格**窗口平滑（不是按档位数）。

    档位间距会变（网页版 $2 一档，测试 fixture $500 一档）——按档位平滑会让窗口
    覆盖到完全不同的价格范围，把真实峰值抹平。按美元窗口才对所有间距一致。
    """
    n = len(vals)
    out: List[float] = []
    for i in range(n):
        p = prices[i]
        lo = i
        hi = i
        while lo > 0 and p - prices[lo - 1] <= half_window_usd:
            lo -= 1
        while hi + 1 < n and prices[hi + 1] - p <= half_window_usd:
            hi += 1
        out.append(sum(vals[lo:hi + 1]) / (hi - lo + 1))
    return out


def _plateau_maxima(series: List[float]) -> List[int]:
    """局部极大值下标（平台取中点，避免同一平台重复计峰）。"""
    n = len(series)
    out: List[int] = []
    i = 0
    while i < n:
        j = i
        while j + 1 < n and series[j + 1] == series[i]:
            j += 1
        left = series[i - 1] if i > 0 else -1.0
        right = series[j + 1] if j + 1 < n else -1.0
        if series[i] > left and series[i] > right:
            out.append((i + j) // 2)
        i = j + 1
    return out


def _find_clusters(prices: List[float], totals: List[float], px: float,
                   min_sep_usd: int = MIN_SEP_USD) -> List[Dict]:
    """从强度曲线里找「簇」：平滑 → 局部极大 → 按最小价格间隔去重 → 回退到原始最大值。

    原始曲线每 $10 一档且相邻档常常同属一个清算带，直接取 top-N 会给出
    78450/78460/78470 这种「三个簇其实是同一片」的结果，所以必须做峰值 + 间隔筛选。
    """
    if not prices or not totals:
        return []
    mx = max(totals) or 1.0
    sm = _smooth(totals, prices)
    peaks = _plateau_maxima(sm)
    picked: List[int] = []
    for i in sorted(peaks, key=lambda z: -sm[z]):
        if all(abs(prices[i] - prices[j]) >= min_sep_usd for j in picked):
            picked.append(i)
    clusters = []
    for i in picked:
        # 回退到原始曲线上的峰值：窗口必须**窄且按价格算**。
        # 用「±若干档」会在档间距变化时跨进隔壁簇，把 A 簇的强度算到 B 簇头上。
        lo = i
        hi = i
        while lo > 0 and prices[i] - prices[lo - 1] <= CLUSTER_RAW_WINDOW_USD:
            lo -= 1
        while hi + 1 < len(totals) and prices[hi + 1] - prices[i] <= CLUSTER_RAW_WINDOW_USD:
            hi += 1
        raw = max(range(lo, hi + 1), key=lambda z: totals[z])
        price = float(prices[raw])
        strength = float(totals[raw])
        if strength <= 0:
            continue
        clusters.append({
            "price": round(price, 2),
            "strength_pct": round(strength / mx * 100, 1),
            "dist_pct": round((price / px - 1.0) * 100, 2),
            "raw": strength,
        })
    clusters.sort(key=lambda c: -c["strength_pct"])
    return clusters


def _leverage_mix(single: Dict, price: float) -> Dict:
    """某价格档上的杠杆构成（low/mid/high 三组占比 %）。"""
    out = {"low_pct": None, "mid_pct": None, "high_pct": None}
    try:
        prices = single.get("prices") or []
        if not prices:
            return out
        best = min(range(len(prices)), key=lambda i: abs(float(prices[i]) - price))
        groups = {}
        for name, keys in LEVERAGE_GROUPS.items():
            s = 0.0
            for k in keys:
                arr = single.get(k) or []
                if best < len(arr):
                    try:
                        s += float(arr[best])
                    except (TypeError, ValueError):
                        pass
            groups[name] = s
        tot = sum(groups.values())
        if tot > 0:
            for name in groups:
                out[f"{name}_pct"] = round(groups[name] / tot * 100, 1)
    except Exception:
        pass
    return out


# ── 对外接口 ──────────────────────────────────────────────────────────────
def liq_reference(last_price: Optional[float], symbol: str = DEFAULT_SYMBOL,
                  exchange: str = DEFAULT_EXCHANGE, interval: str = DEFAULT_INTERVAL,
                  base_coin: str = DEFAULT_BASE_COIN) -> Dict:
    """给策略的清算地图参考：现价上方/下方最强清算簇。

    返回：
      available=False → {"available": False, "reason": ...}（调用方如实显示不可用）
      available=True  → {
        "last_price": 上游 lastPrice,
        "clusters_above": [{price, strength_pct, dist_pct, leverage, exchanges}]  空单清算簇
        "clusters_below": 同上                                              多单清算簇
        "coverage_pct": [数据下沿距现价%, 数据上沿距现价%]（实际覆盖范围）
        "as_of": 数据时间,
      }
    ★ 强度是相对值（占全图最大桶的 %），不是精确合约数——CoinAnk 明确说
      清算图展示的是「相对强度」而非精确待清算张数。
    """
    agg = fetch_agg_liq_map(base_coin=base_coin, interval=interval)
    single = fetch_liq_map(symbol=symbol, exchange=exchange, interval=interval)
    # ★ 把已取到的原始 payload 带出来（供采集器存原始曲线）——
    #   否则调用方为了拿 raw 又要重新 fetch 一遍，请求数翻倍。
    #   配额是 IP 级限流的，请求数就是命脉。
    raw_payloads = {
        "agg": {k: v for k, v in agg.items()
                if k not in ("available", "source", "cached")} if agg.get("available") else {},
        "single": {k: v for k, v in single.items()
                   if k not in ("available", "source", "cached")} if single.get("available") else {},
    }

    # 优先用聚合图（6 家交易所共识，比单家稳）；拿不到就退回单交易所
    if agg.get("available") and agg.get("prices"):
        prices = [float(p) for p in agg["prices"]]
        exchanges = [e for e in AGG_EXCHANGES if isinstance(agg.get(e), list)]
        totals = [0.0] * len(prices)
        per_ex = {}
        for e in exchanges:
            arr = agg[e]
            col = []
            for i in range(min(len(prices), len(arr))):
                try:
                    v = float(arr[i])
                except (TypeError, ValueError):
                    v = 0.0
                totals[i] += v
                col.append(v)
            per_ex[e] = col
        px_up = float(agg.get("lastPrice") or 0)
        src = "coinank-agg"
    elif single.get("available") and single.get("prices"):
        prices = [float(p) for p in single["prices"]]
        totals = [0.0] * len(prices)
        per_ex = {}
        for k in LEVERAGE_KEYS:
            arr = single.get(k) or []
            for i in range(min(len(prices), len(arr))):
                try:
                    totals[i] += float(arr[i])
                except (TypeError, ValueError):
                    continue
        px_up = float(single.get("lastPrice") or 0)
        src = "coinank-single"
    else:
        reason = (agg.get("reason") if not agg.get("available") else "") or \
                 (single.get("reason") if not single.get("available") else "") or "未知原因"
        return _unavailable(reason)

    px = float(last_price or px_up or 0)
    if px <= 0 or not prices:
        return _unavailable("无现价基准或价格档位为空")

    clusters = _find_clusters(prices, totals, px)

    def _decorate(c: Dict) -> Dict:
        out = dict(c)
        if src == "coinank-agg" and per_ex:
            best = min(range(len(prices)), key=lambda i: abs(prices[i] - c["price"]))
            out["exchanges"] = sum(1 for e in per_ex if best < len(per_ex[e]) and per_ex[e][best] > 0)
            out["exchange_total"] = len(per_ex)
        else:
            out["leverage"] = _leverage_mix(single, c["price"])
        return out

    above = [_decorate(c) for c in clusters if c["dist_pct"] / 100.0 > NEAR_BAND]
    below = [_decorate(c) for c in clusters if c["dist_pct"] / 100.0 < -NEAR_BAND]
    # 范围裁剪（±10%）——实际可用范围受上游覆盖限制，见 coverage_pct
    above = [c for c in above if c["dist_pct"] <= SIDE_RANGE * 100]
    below = [c for c in below if c["dist_pct"] >= -SIDE_RANGE * 100]
    above.sort(key=lambda c: -c["strength_pct"])
    below.sort(key=lambda c: -c["strength_pct"])

    cov_lo = (min(prices) / px - 1.0) * 100
    cov_hi = (max(prices) / px - 1.0) * 100
    return {
        "available": True,
        "source": src,
        "last_price": px,
        "upstream_last_price": px_up,
        "clusters_above": above[:TOP_N],
        "clusters_below": below[:TOP_N],
        "coverage_pct": [round(cov_lo, 2), round(cov_hi, 2)],
        "suggestion": compute_suggestion(above[:TOP_N], below[:TOP_N]),
        "raw_payloads": raw_payloads,
        "as_of": int(_now()),
    }


# 多空建议的中性带：|倾向分| 小于这个值就说「中性」，避免 1~2% 的强度差被当信号
SUGGESTION_DEADBAND = 0.15


def compute_suggestion(above: List[Dict], below: List[Dict],
                       deadband: float = SUGGESTION_DEADBAND) -> Dict:
    """清算地图多空建议。

    ★ 口径（liusir 2026-11-02 指定）：**参考值 = 上方/下方强度中的数值最大值**，
      用它做分母归一化，得到 -1 ~ +1 的倾向分：

          参考值 = max(上方最强簇强度, 下方最强簇强度)
          倾向分 = (上方最强簇强度 - 下方最强簇强度) / 参考值

      分母取「数值最大值」而不是固定值或两者之和 → 得分恒在 [-1, 1]，
      且不会因为某一侧特别强就把另一侧压成 0（除法分母永远是较大的那个）。

    方向语义（CoinAnk 官方读图口径）：
      - 现价**上方** = 空单清算簇（空头被清算，价格涨上去时爆空 → 上行磁吸）
      - 现价**下方** = 多单清算簇（多头被清算，价格跌下去时爆多 → 下行瀑布）

    所以：上方更强 → 偏多；下方更强 → 偏空。

    返回：
      available=False → 两侧都没有簇
      available=True  → {"suggestion": "偏多"/"偏空"/"中性", "score": -1~+1,
                          "reference_value": 参考值, "reference_side": "above"/"below",
                          "above": {...}, "below": {...}}
    """
    def _best(lst: List[Dict]) -> Optional[Dict]:
        return max(lst, key=lambda c: c.get("raw") or 0.0) if lst else None

    a = _best(above)
    b = _best(below)
    if not a and not b:
        return {"available": False, "reason": "上下两侧均无显著清算簇"}

    a_raw = float((a or {}).get("raw") or 0.0)
    b_raw = float((b or {}).get("raw") or 0.0)
    # ★ 参考值 = 数值最大值
    ref = max(a_raw, b_raw)
    if ref <= 0:
        return {"available": False, "reason": "清算簇强度为 0，无法给出建议"}

    score = (a_raw - b_raw) / ref        # ∈ [-1, 1]
    if score > deadband:
        label = "偏多"
    elif score < -deadband:
        label = "偏空"
    else:
        label = "中性"

    def _side(c: Optional[Dict]) -> Optional[Dict]:
        if not c:
            return None
        return {"price": c.get("price"), "strength_pct": c.get("strength_pct"),
                "dist_pct": c.get("dist_pct"),
                "share_of_ref_pct": round(float(c.get("raw") or 0.0) / ref * 100, 1)}

    return {
        "available": True,
        "suggestion": label,
        "score": round(score, 3),
        "reference_value": ref,
        "reference_side": "上方" if a_raw >= b_raw else "下方",
        "reference_side_key": "above" if a_raw >= b_raw else "below",
        "above": _side(a),
        "below": _side(b),
        "deadband": deadband,
    }


def format_suggestion(sug: Dict) -> str:
    """多空建议渲染成一行中文（拿不到就如实说）。"""
    if not sug or not sug.get("available"):
        return f"多空建议：不可用（{(sug or {}).get('reason', '未知原因')}）"
    return (f"多空建议 {sug['suggestion']}（倾向分 {sug['score']:+.2f}，"
            f"参考值 {sug['reference_value']/1e6:.0f}M 取自{sug['reference_side']}侧）")


def format_reference(ref: Dict, side: str = "long") -> str:
    """渲染成一行/一块中文参考文本（拿不到就如实说）。"""
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
    sug_txt = ""
    if ref.get("suggestion"):
        sug_txt = f"｜{format_suggestion(ref['suggestion'])}"
    return (f"清算地图参考（{ref['source']}）：上方空单清算簇 {_fmt(ref['clusters_above'])} ｜ "
            f"下方多单清算簇 {_fmt(ref['clusters_below'])}{cov_txt}{sug_txt}")


if __name__ == "__main__":
    # 手动探针：python3 liq_map.py [现价]
    import sys
    px = float(sys.argv[1]) if len(sys.argv) > 1 else None
    r = liq_reference(px)
    print(json.dumps(r, ensure_ascii=False, indent=2))
    print(format_reference(r))
