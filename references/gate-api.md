# Gate.io 期货 API 参考（实测验证 2026-10-02 · BTC_USDT）

## 基础

| 项 | 值 |
|---|---|
| REST 根地址 | `https://api.gateio.ws/api/v4` |
| 备用域名 | `https://api.gateio.la/api/v4`、`https://gateapi.io/api/v4` |
| WebSocket（USDT 本位，**唯一使用**） | `wss://fx-ws.gateio.ws/v4/ws/usdt` |

> ⚠️ 本 skill **只使用 USDT 本位（正向/线性）合约**，REST 前缀固定 `/futures/usdt/*`，WS 固定 `/ws/usdt`。
> 币本位（`/futures/btc/*`、`/ws/btc`，`type=inverse`）不予使用：其以 BTC 计价、
> 张数面值固定 100 USD、维持保证金率 0.5%、最高杠杆 100x，与本 skill 的仓位/强平公式不兼容。
> `assert_usdt_linear()` 会在启动时校验 `type == "direct"`，误传币本位合约直接报错退出。
| 鉴权 | 行情类**无需** API Key；下单/账户需 KEY + 时间戳 + HMAC-SHA512 |
| 限频 | 公共接口约 200 次 / 10 秒（按 IP + 路径） |

## 合约类型对照（实测）

| 项目 | **USDT 本位（本 skill 使用）** | 币本位（不使用） |
|---|---|---|
| 示例合约 | `BTC_USDT` | `BTC_USD` |
| REST 前缀 | `/futures/usdt/*` | `/futures/btc/*` |
| `type` 字段 | `direct`（正向/线性） | `inverse`（反向） |
| 计价/盈亏单位 | **USDT** | BTC |
| 1 张面值 | `quanto_multiplier` = 0.0001 BTC（名义 = 价×0.0001 USDT） | 固定 100 USD |
| 维持保证金率 | 0.003（0.3%） | 0.005（0.5%） |
| 最高杠杆 | 200x | 100x |
| taker/maker | 0.075% / -0.01% | 不同 |

## 合约参数（BTC_USDT 实测，USDT 本位）

| 字段 | 值 |
|---|---|
| `leverage_max` | 200 |
| `maintenance_rate` | **0.003**（0.3%） |
| `taker_fee_rate` | **0.00075**（0.075%） |
| `maker_fee_rate` | **-0.0001**（返佣 0.01%） |
| `quanto_multiplier` | 0.0001（1 张 = 0.0001 BTC） |
| `funding_interval` | 28800（8 小时） |
| `order_price_round` | 0.1 |

> ⚠️ taker 是 **0.075%** 不是 0.05% —— 很多资料写错，日内高频成本影响巨大。

## 常用 REST 接口

| 用途 | 接口 | 参数 |
|---|---|---|
| K 线 | `/futures/usdt/candlesticks` | contract, interval, limit, to |
| 最新行情 | `/futures/usdt/tickers` | contract（含标记价、资金费率、未平仓量） |
| 资金费率历史 | `/futures/usdt/funding_rate` | contract, limit |
| 强平单（爆仓） | `/futures/usdt/liq_orders` | contract, limit |
| 合约元信息 | `/futures/usdt/contracts/{contract}` | — |
| 订单簿 | `/futures/usdt/order_book` | contract, limit |

K 线周期：`10s 1m 5m 15m 30m 1h 2h 4h 6h 8h 12h 1d 7d 30d`

K 线返回字段：`t`(秒,开始时间) `o` `h` `l` `c` `v`(张数) `sum`(成交额)

## ⚠️ 三个实测踩坑（官方文档未强调）

### 坑 1：`from`/`to` 不能和 `limit` 同时传

同时传会直接 **HTTP 400**。

```python
# ❌ 错误
{"contract":"BTC_USDT","interval":"1h","limit":1000,"from":ts1,"to":ts2}   # 400

# ✅ 正确：只用 to + limit 逐页向前翻
to = end_ts
while True:
    raw = get("/futures/usdt/candlesticks",
              {"contract":"BTC_USDT","interval":"1h","limit":1000,"to":to})
    oldest = min(r["t"] for r in raw)
    if len(raw) < 1000: break
    to = oldest - 3600
```

### 坑 2：WS 订阅的 `channel` 填具体频道名，不是 `futures.subscribe`

```python
# ❌ 错误 —— 静默失败，一条推送都收不到
{"channel":"futures.subscribe","event":"subscribe","payload":["tickers","BTC_USDT"]}

# ✅ 正确
{"time":123,"channel":"futures.tickers","event":"subscribe","payload":["BTC_USDT"]}
{"time":123,"channel":"futures.candlesticks","event":"subscribe","payload":["1m","BTC_USDT"]}
```

可订阅频道：`futures.tickers` / `futures.trades` / `futures.candlesticks` /
`futures.liq_orders` / `futures.funding_rate` / `futures.order_book` / `futures.book_ticker`

推送实测（15 秒）：trades 111 条、tickers 12 条、candlesticks(1m) 7 条。
`liq_orders` / `funding_rate` 属低频事件，长时间无推送属**正常**。

### 坑 2.5：WS 的 `result` 有时是列表，有时是对象

`futures.candlesticks` 的 `result` 是**数组**，而 `futures.tickers` / `futures.funding_rate`
的 `result` 也是**数组**（内含 1 个对象）。直接当 dict 用会取不到 `last` / `mark_price`。
`GateWS._on_msg` 已统一逐条解包回调：

```python
res = d["result"]
if isinstance(res, list) and ch != "futures.candlesticks":
    for k in res: on_message(ch, k)     # tickers / funding_rate → 逐个对象
else:
    on_message(ch, res)
```

另外注意 `_drop_unclosed`：REST K 线的**最后一根往往未收盘**，
Gate 会返回它且数值还在变动，若不丢掉会导致信号闪烁。

### 坑 3：历史深度有限

| 周期 | 可获取根数 | 覆盖 |
|---|---|---|
| 15m | 8000 | ~83 天 |
| 1h | ~9000 | **375 天** |
| 4h | 5000 | 833 天 |
| 1d | 1500 | 4 年 |

长周期回测优先用 `1d`，或自行落库积累。

## 缓存与增量更新（每次执行都刷新）

```python
from gate_fetch import fetch_ohlcv, update_all, cache_freshness

# 默认 refresh=True：先增量拉新再读，保证拿到的是最新数据
bars = fetch_ohlcv("BTC_USDT", "4h", total=5000)
bars = fetch_ohlcv("BTC_USDT", "4h", 5000, refresh=False)     # 离线：只读缓存

update_all({"15m": 8000, "1h": 20000, "4h": 5000, "1d": 1500})  # 批量刷新
cache_freshness()                                              # 查看落后几根
```

| 情况 | 行为 | HTTP 请求 |
|---|---|---|
| 有缓存、落后 ≤1000 根 | **增量**：拉最新 1000 根，按 `ts` 合并去重，裁到最新 `total` 根 | 1 次/周期 |
| 落后 >1000 根（有缺口） | 自动降级**全量分页重拉** | `ceil(total/1000)` 次 |
| 无缓存 / `--force` | 全量分页重拉 | `ceil(total/1000)` 次 |

- 缓存目录：`$BTC_DATA_DIR` > 项目根 `data/`
- 原子写盘（`.tmp` → `os.replace`），中断不损坏
- 滚动窗口：只保留最新 `total` 根
- 离线开关：`BTC_NO_REFRESH=1`（对 `backtest.load()` / `fib_test.py` 生效）

CLI：
```bash
$PY scripts/update_data.py --status     # 新鲜度表：根数/末根/落后时间/缺几根
$PY scripts/update_data.py              # 增量更新全部默认周期
$PY scripts/update_data.py --force      # 全量重拉
```

## 最小示例

```python
from gate_fetch import fetch_ohlcv, GateWS

# 历史（首次全量；之后每次增量刷新，缓存到 data/）
bars = fetch_ohlcv("BTC_USDT", "4h", total=5000)

# 实时
ws = GateWS(on_message=lambda ch, d: print(ch, d))
ws.subscribe([
    ("futures.candlesticks", ["1m", "BTC_USDT"]),
    ("futures.tickers",      ["BTC_USDT"]),
    ("futures.liq_orders",   ["BTC_USDT"]),
])
ws.run_forever()
```
