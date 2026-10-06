---
name: btc-gate-intraday-strategy
description: 'Gate.io USDT 本位（正向/线性，type=direct，如 BTC_USDT）永续合约日内短线双向交易系统：多周期(15m/1h/4h/1d) TD9 + EMA144(位置) + EMA169(斜率) + 趋势线(低点-低点/高点-高点) + RSI金叉死叉 + 量能确认 + 横盘预警，含真实数据获取、回测引擎、蒙特卡洛显著性检验、杠杆爆仓建模，可一键输出当前行情分析与多空双向进场/出场点位。默认 target=1000 点 (TP1=400 / TP2=600)，BE 锁保留。⚠️ 默认入场是**左侧挂单**（在支撑位下方等回调），用户做**右侧交易**（突破后追）时需自行把 entry_break 升为主入场。不支持币本位 inverse 合约。Use when user trades USDT-margined crypto perpetual futures intraday (当天进当天出), uses TD9/TD Sequential or EMA144/EMA169, does both long and short, uses high leverage (20x-100x+), asks for win-rate optimization, wants entry/exit price levels, wants trendline (low-low / high-high) levels, or wants to fetch Gate.io market data (REST/WebSocket). 触发词：合约短线、日内交易、TD9、EMA144、EMA169、趋势线、低点连低点、抄底逃顶、多空双向、百倍杠杆、爆仓、Gate 数据、Gate.io、胜率优化、量价分析、RSI金叉死叉、USDT本位、进出场点位、横盘预警。'
agent_created: true
---

> ⚠️ **本 skill 是纯模拟盘，不入真单。**
> - journal 里的 `pending` / `filled` 都是**本地跟踪**，不会下单到 Gate.io
> - 当前没有 API key，整个流程只跑：信号检测 → scan → TG 推送 → journal 模拟跟踪 → Settler 模拟结算
> - 真要下单需要在 `gate_fetch.py` 加 `private_api`（HMAC 签名）+ `close_position()` 函数
> - 历史 8 次结算全部是模拟价（用本地 1m K 线 high/low 算的，不是真实成交价）

# BTC 合约日内短线双向策略系统（Gate.io · **USDT 本位**）

## 适用对象

- 交易标的：**Gate.io USDT 本位永续**（正向/线性，`type=direct`，默认 `BTC_USDT`）
- ⛔ **不使用币本位（inverse）合约**（如 `BTC_USD`）：计价单位是 BTC、1 张固定 100 USD、
  维持保证金率 0.5%、最高 100x，与本 skill 的仓位与强平公式不兼容。
  启动时 `assert_usdt_linear()` 会校验 `type == "direct"`，误传币本位直接报错退出。
- 计价与面值：盈亏/保证金均为 **USDT**；BTC_USDT 的 `quanto_multiplier = 0.0001`，
  即 **1 张 = 0.0001 BTC，名义价值 = 价格 × 0.0001 USDT**
- 周期体系：`1d` 战略 → `4h` 战术 → `1h` 主信号 → `15m` 入场执行
- 风格：日内（当天进当天出），**多空双向**，高杠杆（20x~200x）
- 核心组件：TD9（TD Sequential）+ EMA144 + RSI 金叉死叉 + 量能

### 接口前缀（固定，不要改）

| 用途 | 地址 |
|---|---|
| REST | `https://api.gateio.ws/api/v4/futures/usdt/*` |
| WebSocket | `wss://fx-ws.gateio.ws/v4/ws/usdt` |

（币本位的 `/futures/btc/*` 与 `/ws/btc` 一律不使用）

### 数据源确认：全部取自**永续合约**，不是现货

历史 K 线与实时报价都走合约接口，**不存在现货数据混入**。已实测三方交叉验证（同一根 4h K 线，
ts=1790884800 / 2026-10-01 20:00 UTC）：

| 来源 | 开 | 高 | 低 | 收 | 成交量 |
|---|---|---|---|---|---|
| 合约 `/futures/usdt/candlesticks` | 84,624.1 | 84,863.0 | 84,448.0 | 84,831.0 | 34,333,263 |
| 回测缓存 `data/BTC_USDT_*.json` | 84,624.1 | 84,863.0 | 84,448.0 | 84,831.0 | 34,333,263 |
| 现货 `/spot/candlesticks`（**对照，未使用**） | 84,682.2 | 84,906.8 | 84,485.0 | 84,882.0 | 35,948,262 |

- 现货接口**从不出现在任何脚本里**（`grep -r "spot" scripts/` 为空）。
- 合约与现货通常差 0.02%~0.06%（基差），且两者**字段名不同**：合约返回对象 `{t,o,h,l,c,v,sum}`，
  现货返回数组 `[ts, quote_volume, close, high, low, open, base_volume]` —— 混用会静默错位，务必留意。
- `scan.py --brief` 头部第 2 行会打印数据源自检：`数据源 Gate.io 永续 /futures/usdt/* · type=direct ｜ 标记价 … ｜ 资金费率 …`。
- 只有 `mark_price` / `funding_rate` / `open_interest` 这类字段才是合约特有，看到它们即可确认数据正确。

## 环境

```bash
# 依赖：pandas numpy requests websocket-client
# 解释器不写死绝对路径 —— 由部署环境决定（venv 激活后用 python3 即可）
PY=${PYTHON:-python3}
```

脚本在 `scripts/`，参考文档在 `references/`。

> **本 skill 内的所有路径都是相对路径，不含任何机器专属绝对路径。**
>
> 约定的两个变量（下文所有命令都基于它们）：
> ```bash
> cd <项目根>/BTC_system        # 唯一需要你指定的一步
> PY=python3                    # 或用 ${PYTHON:-python3}，venv 激活后直接用 python3
> SK=skills/btc-gate-intraday-strategy   # 相对项目根
> $PY $SK/scripts/scan.py BTC_USDT --brief
> ```
> 之所以能这么写：每个脚本都用 `os.path.dirname(os.path.abspath(__file__))`
> 定位自身（`watch.py` / `journal.py` / `scan.py` 调子脚本同理），
> 数据目录由 `gate_fetch._detect_project_dir()` 自动探测（向上找含 `data/BTC_USDT_*.json`
> 或 `.workbuddy` 的层）。因此 skill 放在 `项目/skills/` 还是 `项目/.workbuddy/skills/`、
> 项目搬到哪台机器，都无需改任何路径。
> 想完全摆脱 cwd 依赖可设环境变量：`BTC_DATA_DIR=<任意目录>` 指定缓存/日志位置。

### 数据维护：缓存每次执行都会自动刷新

**回测缓存默认不需要手动管理 —— 每次执行回测脚本都会先把它追到最新。**

缓存目录优先级：`$BTC_DATA_DIR` > 项目根 `data/`（`gate_fetch.py` 已自动定位）。

```bash
$PY $SK/scripts/update_data.py            # 增量更新全部默认周期（15m/1h/4h/1d）
$PY $SK/scripts/update_data.py --status   # 只看新鲜度，不联网（缺几根一目了然）
$PY $SK/scripts/update_data.py --force    # 删掉缓存全量重拉
$PY $SK/scripts/update_data.py --tfs 1h,4h --counts 20000,5000
$PY $SK/scripts/update_data.py --contract ETH_USDT
```

**更新机制（三条路径，都已实测）**：

| 情况 | 做法 | HTTP 请求数 |
|---|---|---|
| 有缓存且落后 ≤1000 根 | **增量**：只拉最新 1000 根，按 `ts` 合并去重，裁到最新 `total` 根 | **每周期 1 次** |
| 缓存落后 >1000 根（有缺口） | 自动降级为**全量分页重拉** | `ceil(total/1000)` 次 |
| 无缓存 / `--force` | 全量分页重拉 | `ceil(total/1000)` 次 |

实测：4 个周期全更新只花 **4 次请求 / 约 24 秒**；每次都跑也不会触及限频（配额 200 次/10s）。

- 缓存**按滚动窗口**保存（只留最新 `total` 根），调用契约为
  `fetch_ohlcv(contract, interval, total, cache=True, refresh=True, verbose=False)`
- 写盘用**原子替换**（先 `.tmp` 再 `os.replace`），中断不会损坏缓存
- 每次都会丢弃**尚未收盘的最后一根**，避免信号闪烁
- **离线模式**：`BTC_NO_REFRESH=1 python fib_test.py 4h`（不联网，沿用上次缓存）
- `scan.py` 走 `cache=False`（实时直连），本来就是最新的，不走这套缓存

---

## 一、先看红线：杠杆与爆仓（最重要）

**决定生死的是「名义敞口 ÷ 账户权益」，不是软件里填的杠杆数字。**
杠杆只是"允许你开多大"的上限。Gate.io BTC_USDT 维持保证金率 = **0.3%**。

全仓满仓口径下，强平阈值 = `1/倍数 - 0.003`：

| 名义/账户 | 初始保证金率 | 强平阈值（反向波动） | 本策略被打穿率 |
|---|---|---|---|
| 200x | 0.50% | **0.20%** | **92.6%** |
| 100x | 1.00% | **0.70%** | **40.7%** |
| 50x  | 2.00% | 1.70% | 7.4% |
| 20x  | 5.00% | 4.70% | 0% |
| ≤10x | ≥10%  | ≥9.7% | 0% |

> 打穿率基于本策略 27 笔真实交易的 **MAE（最大不利偏移）** 分布实测：
> 中位数 0.617%、P90 1.350%、最大 1.901%。

**结论：100x 满仓，约 4 成单子会死于正常噪音，而非判断错误。**
建议名义敞口 ≤ 20 倍账户（把 100x 杠杆只当作"保证金占用很低"的工具，不是"开满"的许可）。

**铁律**：止损距离必须 **< 强平阈值**，否则一定在打到止损前先爆仓。
`scan.py` 会自动做这个检查并给出降杠杆建议。

### 1.1 全仓 vs 逐仓：两张完全不同的生死表（极易用混）

| 模式 | 保证金来源 | 强平距离由什么决定 | 100x 档位下的典型值 |
|---|---|---|---|
| **逐仓 isolated** | 名义 ÷ 杠杆档位 | **只看杠杆档位，与张数无关** | `(1/100-0.003)/0.997` = **0.70%**（固定） |
| **全仓 cross** | 整个账户权益 | **只看名义敞口 ÷ 账户** | 名义 1.25x → **约 80%**（几乎不可能强平） |

强平价公式（维持保证金率 `m`）：
- 逐仓 多：`P* = P0(1-1/L)/(1-m)`　空：`P* = P0(1+1/L)/(1+m)`
- 全仓 多：`P* = P0(1-1/N)/(1-m)`，其中 `N = 名义/账户`；`N ≤ 1` 时不会强平

**实操含义**：想在 100x 档位下活命，**必须用全仓 + 控制名义敞口**（`--mode cross`）。
若用逐仓，档位就得降到 ≤ 67x（止损 0.8% 时）甚至 ≤ 20x。
`scan.py` 两种口径都算并并排展示。

---

## 二、策略规则（回测实证版）

### 2.1 分层原则 —— 最容易搞错的一点

**EMA144 + EMA169 双带判定趋势**：位置用 EMA144（短期更敏感），斜率用 EMA169（169 比 144 长 17%，更平滑、噪音更少）。

```
1d  → 战略方向（1d EMA144 位置 + 4h EMA169 斜率）
4h  → 战术方向（4h EMA144 位置 + 4h EMA169 斜率）   ← 双带在这一层用
1h  → 主信号（TD9 + 等确认 + RSI交叉 + EMA169 斜率过滤）
15m → 入场执行（找精确进场点）
```

**为什么 144 判位置、169 判斜率**：1h 周期 EMA144 = 6 天均线，反应灵敏适合"价格相对均线在不在上方"；EMA169 ≈ 7 天均线，更平滑适合"趋势方向有没有掉头"。组合效果：减少"价格在 EMA144 上但 EMA169 还在下行"的假多头排列。

⚠️ **绝不能要求"本周期价格站上 EMA144"**：TD9 抄底信号天然出现在价格已跌破 EMA 之后。强制 `close > EMA144` 与"抄底"自相矛盾，实测会把 1h 信号从 ~110 个砍到 **4 个**，直接枯竭。

### 2.2 入场（做多；做空完全镜像）

| # | 条件 | 说明 |
|---|---|---|
| ① | 1h 出现 **TD9 buy setup 完成** | 连续 9 根收盘 < 4 根前收盘 |
| ② | **等连续 2 根 1h 收阳** | ⭐ 核心 alpha 来源，见下 |
| ③ | 4h 价格在 4h EMA144 之上 | 顺势（位置判定用 144） |
| ④ | 4h EMA169 斜率 > 0 | 趋势向上（斜率判定用 169） |
| ⑤ | **RSI 金叉**（RSI6 上穿 RSI14） | 动能转向确认 |
| ⑥ | **量能确认**（量比 > 1.2） | 成交量 / MA20 |

**采用打分制，不要全票通过。** 实测：

| 配置 | 笔数 | 胜率 | EV | PF |
|---|---|---|---|---|
| 仅 M4 双条件（③④） | 9 | 44.4% | +0.36% | **2.07** |
| +RSI 金叉（⑤） | 12 | 41.7% | +0.15% | 1.34 |
| +放量（⑥） | 12 | 41.7% | +0.15% | 1.34 |
| +RSI + 放量（⑤⑥） | 21 | 38.1% | **-0.07%** | 0.87 |

> 条件全部强制叠加 → 1h 上只剩 **1 个信号**，无法统计。
> **建议：得分 ≥ 4/6 才动手；③④ 是基础分，⑤⑥ 是加分项。**

### 2.2b RSI 的定位：**排雷器，不是信号源**（2026-10-02 专项实证）

配置：4h 核心信号（TD9 + 2 根确认）+ 止损 0.8% + 目标 500 点 + 移动止损 0.5%

| 分组 | n | 胜率 | EV | PF |
|---|---|---|---|---|
| 基准（不加 RSI） | 13 | 53.8% | +0.05% | 1.23 |
| 其中 **RSI 交叉通过**的 | 8 | 75.0% | **+0.20%** | 2.24 |
| 其中 **被 RSI 挡掉**的 | 5 | 20.0% | **−0.19%** | 0.33 |

做空同向验证：通过 13 笔 EV +0.05%（PF 1.27）／被挡掉 10 笔 EV −0.18%（PF 0.44）。
**RSI 挡掉的确实是垃圾单**——动能没转向就抄底，等于接下跌中的刀。

但三件事必须说清：

1. **RSI 裸用没有 alpha。** 4h/1h × 金叉/死叉/超买/超卖 共 8 组，EV 全部为负
   （−0.13% ~ −0.26%），且与「随机择时」基准线几乎重合，置换检验 p 全部 >0.05。
   唯一 p<0.05 的（1h 金叉，p=0.005）EV 仍是 −0.14%，盖不住 0.101% 的双边成本。
2. **不要拿 RSI 平仓。** 4h 做空加「RSI14>70 即平」EV −0.13%→−0.30%，
   加「RSI 死叉即平」→−0.57%；做多端无改善。固定 500 点 + 移动止损 0.5% 更好。
3. **不要把 RSI 当硬门槛。** 它会砍掉 38% 的信号，而置换检验 p≈0.11~0.14 未达显著
   （样本仅 8~13 笔）→ 过拟合风险大于收益。

**正确用法**：保留为 6 分制里的 1 分加分项（现状即如此），它的价值是**降低交易频率、
滤掉最差的一批**，而不是提供入场时机。入场时机由 TD9 + 2 根确认提供。

### 2.3 ⭐ 为什么"等 2 根确认"是核心

同一批 TD9 信号，只改入场时机（4h 周期，SL3%/TP6%）：

| 入场方式 | 笔数 | 胜率 | EV | PF | p 值 |
|---|---|---|---|---|---|
| 见信号直接冲 | 47 | 27.7% | **-0.71%** | 0.69 | 0.876 |
| 等 1 根收阳 | 32 | 34.4% | -0.10% | 0.95 | — |
| 等 1 根突破信号根高点 | 26 | 42.3% | +0.60% | 1.33 | — |
| **等 2 根连续收阳** | 13 | **69.2%** | **+2.94%** | **4.01** | **0.012 ✅** |

机制：TD9 完成只代表"下跌衰竭的**条件成立**"，不代表已经衰竭。
BTC 常走第 10、11、12 根阴线。等 2 根收阳 = 让市场自己证明跌停了。

**TD9 的 alpha 窗口只有 1-3 根 K 线**（事件研究：超额在第 3 根达峰 +0.273%，
第 6 根转负，第 48 根 -1.36%）。它是**超短期均值回归信号，不是趋势信号**。

### 2.4 出场

- 止损 = **1.5 × ATR**（1h ATR 中位约 1.5%，即约 2%），下限 0.8%
- 止盈 = **2 × 止损**（1:2）
- 日内：UTC 00:00 前必平

实测权衡（双向 27 笔，SL1%/TP2%）：

| 出场 | 胜率 | EV | MAE 中位 |
|---|---|---|---|
| 当日必平 | 40.7% | +0.03% | 0.62% |
| 持有 24 根不限日 | 48.1% | +0.19% | 0.76% |
| 持有 72 根不限日 | 48.1% | +0.29% | 1.00% |

> **强制日内平仓会牺牲收益**（少约 0.2%/笔），但把隔夜跳空风险压到最低。
> 日内风格优先选当日必平，接受略低的 EV 换取风险可控。

### 2.5 双向注意

实测做空显著弱于做多（1h，min_score=2 + RSI）：

| 方向 | 笔数 | 胜率 | EV | PF |
|---|---|---|---|---|
| 做多 | 12 | 41.7% | +0.15% | 1.34 |
| 做空 | 15 | 33.3% | **-0.28%** | 0.56 |

→ BTC 长期向上，做空是逆水行舟。**做空需提高门槛（得分 ≥5/6）或减半仓位。**

### 2.6 横盘预警（2026-10-03 加）

`scan.py --brief` 起手会自动判定横盘，**横盘时量能异动假突破风险高**（前向 0% 胜率，因为 BE 锁把 TP1 吃完的利润瞬间收回）。

判定：`1h close` 距 `1h EMA144` 偏离 < **0.5%**（阈值放宽到 0.5% 才能命中实际行情）
- 判定为横盘时，Brief 头部追加 `· ⚠️ 横盘（建议减仓或观望）` 标签
- 完整模式在 ⑥ 综合研判追加一行 `· 横盘预警：1h close 距 1h EMA144 偏离 X.XX% → 建议减仓 50% 或直接观望`
- payload 加 `sideways: bool` 字段，watch / 外部脚本可读

**实战用法**：横盘期间量能异动触发的告警**建议把仓位减半**，或直接跳过本次信号。

### 2.7 趋势线（低点-低点 / 高点-高点，2026-10-03 加）

经典趋势线战法：取最近的 swing low pivot 做最小二乘拟合（上升趋势线），或 swing high pivot（下降趋势线）。
未来 bar `x` 处的趋势线值 = `slope * x + intercept`。

**算法实现**（`indicators.py`）：
- `trendline_low_low(low, is_l, conf_l, t, min_touches=3, max_lookback=200)` → 上升趋势线 dict 或 None
- `trendline_high_high(high, is_h, conf_h, t, min_touches=3, max_lookback=200)` → 下降趋势线 dict 或 None
- 默认 `min_touches=3`（最少 3 个 pivot 才拟合，**2 个 pivot 容易被噪音牵着走**——曾用 2 拟合 1h 趋势线，斜率被极端高点拉扯到 -100 pts/h 的非真实值）
- 返回 dict 字段：`slope`（pts/bar）、`intercept`、`value_now`、`pivots=[(idx, price), ...]`

**调用方式**：
```python
from indicators import swing_pivots, trendline_low_low, trendline_high_high
is_h, conf_h, is_l, conf_l = swing_pivots(high, low, k=5)
t = len(high) - 1
tl_low = trendline_low_low(low, is_l, conf_l, t, min_touches=3, max_lookback=200)
if tl_low:
    slope_per_day = tl_low['slope'] * (1440 if tf=='15m' else 24)
    future_val = tl_low['slope'] * (t + 16) + tl_low['intercept']  # 16 根 bar 后
```

**实战用法**：把趋势线当作动态支撑/阻力，价格回调到上升趋势线 = 潜在做多入场；冲到下降趋势线 = 潜在做空入场。
**当前实现状态**：算法已加，但**未集成进 scan.py**（未自动输出到 BRIEF 或打分）。需要先做 §2.8-A 选项再批量看实战效果。

### 2.8 入场方向：左侧（默认）vs 右侧

`scan.py` 每个 plan 同时给两个入场价：

| 字段 | 触发条件 | 类型 |
|---|---|---|
| `entry_limit` | 等价格**回调到位**（挂在支撑位下方 / 阻力位上方） | **左侧**（逆势/回调） |
| `entry_break` | 价格**突破后**追价（阻力位 × 1.05×ATR） | **右侧**（顺势/追涨杀跌） |

**当前默认行为**（scan.py:622 纪律第 1 条 + journal 实际 fill_px）：
- 默认推 `entry_limit` 挂单（post-only 限价，maker 返佣）
- journal 6 单实际成交 fill_px 全部等于 entry_limit → **实际走左侧**
- entry_break 在 BRIEF 报告里"备选"位置出现（`scan.py:570-571`）

**用户实际习惯（2026-10-03 确认）**：右侧交易——突破阻力位后才追，不做左侧回调挂单。
**当前策略与用户习惯冲突**：
- 左侧挂单被填 = 价格回调到位 → 80% 概率横盘里立刻被 BE 收回 → **-$0.85**
- 左侧挂单没填 = 价格直接涨上去 → **完全错过趋势**（如 10-03 02:41 那单挂 84,091，price 一路冲到 85,000+，但 entry 不动）

**当前解决方案**：暂未实现 `--side right` 开关（用户尚未明确要求改），需要时再加。
**临时做法**：手动把 BRIEF 报告里的"突破追"价格作为实际入场。

### 2.9 环境过滤（反直觉）

按日线 EMA144 斜率分位分层（4h 周期）：

| 环境 | 胜率 | EV | PF |
|---|---|---|---|
| 强空头 <P20 | 27.3% | -0.78% | 0.66 |
| 震荡 P40-60 | 27.3% | -0.73% | 0.68 |
| **弱多头 P60-80** | **50.0%** | **+1.26%** | **1.79** |
| 强多头 >P80 | 14.3% | **-1.88%** | **0.31** |

> **抛物线暴涨末期（>P80）的 TD9 往往不是回调，是见顶第一波。**
> 情绪最亢奋时不要抄底。

**实现状态（2026-10-06）**：✅ 已写入 `scripts/scan.py` —— `compute_env_regime_penalty()`
- 强多头（1d EMA144 斜率 > P80）+ 做多 → 砍 2 分
- 强空头（< P20）+ 做空 → 砍 2 分
- 用最近 60 根 1d 斜率算 P20/P80 阈值（lookback 可调）
- **回测验证**（journal 17 笔 10-02~10-06 long-only 样本）：拦 8 笔 score≥4 → **-21.23U → +42.09U（改善 +63.32U）**

**坑**（2026-10-06 实盘教训）：
- 1d EMA144 斜率分位在 10-02~10-06 期间**全程在 P80 以上**（P97~P98 强多头）
- 当时 scan 没有任何过滤 → 10-05 22:09 那笔 -44.85 SL_STOPPED 正是 P98 强多头末期做多
- **教训**：文档和代码脱节 → 文档里写的"反直觉"必须实接代码，不能只放在 SKILL.md 里装样子

### 2.7 斐波那契回调（Fib）—— 定位是「挂单尺」，不是「信号源」

**实现（`indicators.py`）**：`swing_pivots(high, low, k=5)` 识别 ZigZag 摆动高低点，
**pivot 需右侧 k 根走完才确认（确认时间 = i+k）**，`last_swing_leg()` 只取已确认的段，
因此无未来函数。`fib_retr(lo, hi)` 给回调支撑、`fib_retr_up(lo, hi)` 给反弹阻力。
回调档位 0.236 / 0.382 / 0.500 / 0.618 / 0.786。

**实证结论（4h，5000 根；回测脚本 `fib_test.py`）**：

| 用法 | 笔数 | 胜率 | EV | PF | MAE 中位 |
|---|---|---|---|---|---|
| 裸 Fib 黄金区做多（0.382~0.618） | 705 | 48.7% | **-0.17%** | 0.76 | 0.93% |
| 裸 Fib 做空 | 661 | 46.4% | -0.23% | 0.68 | 0.95% |
| 裸 Fib 显著性 | — | — | — | — | **p = 0.814 ❌** |
| **裸 TD9 做多（对照组）** | 58 | 39.7% | **-0.28%** | 0.72 | **1.45%** |
| **Fib 过滤后的 TD9 做多** | 106 | 51.9% | **+0.14%** | **1.25** | **0.68%** |
| 同上 + maker 挂单 | 69 | 62.3% | +0.27% | 1.58 | 0.70% |

三条必须记住的结论：

1. **裸 Fib 没有 alpha**（p = 0.814，跑不赢随机）。它不预测方向。
2. **Fib 的真正价值是压缩 MAE**：给 TD9 加一道"必须在 Fib 回调位附近才动手"的过滤，
   MAE 中位从 **1.45% → 0.68%（腰斩）**，EV 由负转正。**对 100x 杠杆而言这比胜率更重要** ——
   MAE 1.45% 远超 100x 强平阈值 0.70%（必爆），0.68% 才勉强能活。
3. **只在 4h 用，1h 完全失效**：1h 上 Fib+TD9 做多所有档位 EV 全负，
   邻近参数网格 12 组 **0% 为正**（4h 上 12 组 **100% 为正**）。周期特异性极强。

其他细节：
- **深回调优于黄金区**（反直觉）：4h 单档位 PF 依次为 0.236→0.72、0.382→0.57、
  0.5→0.79、0.618→0.94、**0.786→1.21**。流行的"0.618 黄金位最优"在本样本不成立。
- **做空方向无效**：Fib + TD9 逃顶做空 EV **-0.61%**、PF 0.31。做空别用 Fib。
- **共振不是必需**：Fib 位 ±0.4% 内有 EMA/枢轴（有共振 PF 1.19）vs 无共振（PF 1.34），
  差异不显著 —— 有共振更好看，但不必强求。
- **pivot 滞后是代价**：k=5 意味着 4h 上约 20 小时才确认一个新摆动高点，
  所以 Fib 段在快速上涨中会显得"落后"。这是无未来函数的必然代价，接受它。

**落地用法**：`scan.py` 已把 4h/1h 的 Fib 回调位并入关键价位候选，
`--brief` 头部会打印 `Fib(4h) 82,506 → 85,629 上涨段·回调做多 ｜ 0.5=… 0.618=… 0.786=…`。
**把它当作"在哪挂单"的坐标，而不是"要不要开仓"的理由** —— 要不要开仓仍由 TD9 + 2 根确认决定。

---

## 三、成本：日内短线的隐形杀手

Gate.io BTC_USDT 实测：**taker 0.075% / maker -0.01%（返佣）**

| 成交方式 | 胜率 | EV | PF | 总收益 |
|---|---|---|---|---|
| 全 taker（市价进出） | 40.7% | +0.03% | 1.07 | +0.9% |
| 混合（一挂一吃） | 40.7% | +0.12% | 1.29 | +3.2% |
| **全 maker（挂单，返佣）** | **44.4%** | **+0.20%** | **1.56** | **+5.5%** |

> **EV 相差 6 倍以上。日内高频必须挂单（maker），别吃单。**
> 用 post-only 挂单等成交，让对手方付给你 0.01%。

**实现状态（2026-10-06）**：`scripts/position_tracker.py` 已按 **maker** 写死——
```python
FEE_USD = -1.0   # 5000U 名义 × 0.01% × 2 = 净返佣 1U
```
**坑**（2026-10-06 决策）：
- 之前 `FEE_USD = 4.85` 是 taker 双边 0.075% × 5000U × 2
- 但 scan 一律推 post-only 挂单，理论上 entry+exit 都能拿到 0.01% 返佣
- **风险**：如果实际 Gate 上挂单被 taker 吃掉（如 deep 极端行情）→ 仍是 -4.85U 而非 -1U
- **验证方法**：下一笔 fill 后看 journal 的 `net_usd`：
  - 接近 **+1U** → maker 假设成立
  - 接近 **-4.85U** → 实际是 taker 成交，需要改回 FEE_USD = 4.85
- 当前 95 个 unit test 全过，net_usd 期望值已同步 +5.85U

---

## 四、工作流

### 盘中：行情分析 + 交易点位（主入口）

```bash
$PY $SK/scripts/scan.py BTC_USDT --lev 100 --bal 1000 --mode cross
$PY $SK/scripts/scan.py BTC_USDT --lev 20 --mode isolated      # 逐仓口径
$PY $SK/scripts/scan.py BTC_USDT --risk 0.02 --json p.json --html p.html
$PY $SK/scripts/scan.py BTC_USDT --brief    # ⭐ 极简：只给「当前偏向的那一侧」
```

**用户要求简短时务必用 `--brief`**，输出只有一块：
`头部一行（现价/时间/趋势/量能/判定）` → `阻力3档` → `现价` → `支撑3档` →
**单个方向**的 入场（挂单价 + 突破/跌破追价）、止损、T1/T2/T3 出场、仓位 → 一句结论。

⚠️ **`--brief` 只输出一个方向**：按 `main_side` 决定 ——
得分高的一侧优先；打平时按趋势结构（1d/4h 是否站上 EMA144 + 4h 斜率）定多空。
用户明确要求「当前偏多就只给多，偏空就只给空」，不要同时输出两套点位。
`--json` 也只给 `plan`（单侧）+ `side` 字段，不再有 `plan_long`/`plan_short`。
完整模式（不加 `--brief`）仍保留双向对照，主方向标 ★。

参数：`--lev` 杠杆档位（默认100）｜`--bal` 账户余额U（默认1000）｜
`--risk` 单笔风险占账户比例（默认0.01）｜`--mode` cross全仓/isolated逐仓｜
`--json` 结构化输出｜`--html` 可视化报告

一次执行输出 7 段：
① 多周期趋势结构 ② 多空双向 6 项打分 ③ TD9 倒计时（还差几根完成）
④ 支撑/阻力（日线枢轴 + 各周期 EMA144/EMA34 + 结构高低点，带距现价%）
⑤ **双向交易计划**（挂单入场 / 追价触发 / 止损 / T1-T2-T3 止盈 / 强平价 / 建议张数与保证金）
⑥ 综合研判（趋势、位置、动能、波动、结论）⑦ 执行纪律

**入场价取"现价下方最近的支撑"（做多）/ "上方最近的阻力"（做空）**，
止盈逐级挂到下一个阻力/支撑位，并强制单调（T1<T2<T3）。
止损 = max(1.5×ATR, 次级结构位±0.3ATR)，夹在 **0.8%~3%**。

判定规则：核心①②（TD9 完成 + 2 根确认）**必须同时满足**且总分 ≥4 才动手；
否则输出"无信号 · 观望"，此时给出的双向点位属于**区间交易参考**，需降仓位快进快出。

### ⭐ 目标：默认 1000 点封顶（`--target`）

2026-10-02 用户要求"目标 1000 点，TP1=400，TP2=600"，不再用默认 500。`--target 1000` 是当前默认。
T3 = 入场 ±1000 点**硬顶**，T1/T2 是在 1000 点以内最近的合格结构位（没有就按 40%/60% 目标分批，
即 400 / 600 点）。原 500 点组合的 EV 表（见 git log 旧版）已被替换；新组合的 forward 验证显示
**TP1 从 200→400 后，BE 触发数减半**（横盘期间单子能等到趋势展开）。

⚠️ **小目标 / 大止损代价不变**，移动止损 0.5% 仍是必要条件（BE 锁保留，TP1 后 SL 移到 entry）。

⚠️ **这会造成「小目标 / 大止损」，必须知道代价**（回测实证，4h 核心信号 n=13）：

| 止损 | 目标 | 胜率 | EV | PF |
|---|---|---|---|---|
| 0.80% (≈690点) | 500 点 | 61.5% | **-0.05%** | 0.84 |
| 0.80% + **移动止损 0.5%** | 500 点 | 53.8% | **+0.05%** | 1.23 |
| 0.80% + 移动止损 0.3% | 500 点 | 61.5% | +0.02% | 1.09 |
| 1.00% + 移动止损 0.5% | 500 点 | 53.8% | +0.02% | 1.07 |

结论：
1. 500 点目标配 0.8% 止损 → R:R 仅 1:0.73，**保本胜率要 58%**（scan 会自动算并显示）
2. **不加移动止损就是负 EV**；加 0.5% 移动止损后转正（让赢家跑过 500 点，输家仍 -1R）
3. **1h 上 500 点目标全部为负**（最好 -0.14%/笔）→ 500 点目标只在 4h 信号 + 移动止损下成立
4. 止损收紧到 0.4% 会让胜率掉到 38% 且 EV 更差 —— 不要为了凑 R:R 去压缩止损
   （1h MAE 中位 0.617% ≈530 点，止损 350 点以内会被噪音扫掉）

所以默认组合：**4h 信号 + 止损 0.8% + 目标 500 点 + 移动止损 0.5%**。

### ⭐ 每次扫描自动记账：胜率复盘 + 保守优化（`journal.py`）

**scan.py 每次执行都会**（`--no-journal` 或 `BTC_NO_JOURNAL=1` 可关）：
① 先用 15m K 线**回放结算**所有未完成的旧信号 → ② 再把本次信号写入 `data/journal.jsonl`。
所以"跑一次扫描"= 记一笔账，不需要额外操作。

```bash
$PY $SK/scripts/journal.py stats     # 绩效总览 + 分层胜率（自动先结算）
$PY $SK/scripts/journal.py list      # 逐笔明细（含 R / MAE / MFE / 持仓小时）
$PY $SK/scripts/journal.py recheck   # 用最新数据重跑回测，看 alpha 是否衰减
$PY $SK/scripts/journal.py tune      # 生成优化建议（只写 tuning_suggested.json，不生效）
$PY $SK/scripts/journal.py apply     # 采纳建议 → tuning.json，scan.py 下次自动应用
```

**结算口径（防自欺）**：
- 挂单没成交（48h 没回到挂单价）→ 记 `MISSED`，**不计入胜率**，只统计成交率
- 同一根 15m K 线内既到止损又到止盈 → **保守判 `SL*`（-1R）**
- 成交后 24h 仍未触发 → 按收盘价平仓，记 `TIMEOUT`
- 成本按 post-only 计（maker 往返 -0.02%，相对止损距离折算成 R）

**判据排序：EV(R) > 胜率。** 本策略 T1 常 >1R，胜率 40% 但 EV 正是正常的高赔率形态；
只有 **EV<0、连亏≥5 笔、或 PF<1** 才动参数，不要因为胜率难看就改策略。

**优化是保守且带门槛的**（防过拟合）：

| 样本量 | 允许的动作 |
|---|---|
| n < 15 | **只允许减仓**，不允许加权/放宽门槛 |
| 15 ≤ n < 30 | 可加减仓位、可提门槛 |
| n ≥ 30 | 才允许放宽门槛、放大仓位 |

可调的只有 4 个量：`score_threshold`（得分门槛）、`side_size`（多/空仓位系数）、
`risk_scale`（全局风险缩放）、`core_required`。
**核心逻辑（TD9 + 2 根确认）不会被自动关闭** —— 样本不足时只提示人工复核。
小样本一律附 Wilson 95% 置信区间，避免拿 3 笔的 100% 胜率当真。

> 实盘日志一天最多 1-2 笔，涨到 30 笔要几个月。**判断策略是否失效主要靠 `recheck`**
> （几秒内用全样本重跑回测），日志的作用是**校准实盘 vs 回测的偏离**（滑点、挂不到单、执行偏差）。

### ⭐ 服务器常驻：异动监控（watch.py）· 两条通道

放服务器上 7×24 跑，**量能突增**或 **RSI 越界**任一命中 → 自动跑 scan.py。

```bash
$PY $SK/scripts/watch.py once                 # 诊断：打印量能分位 + 当前 RSI，看会不会触发
$PY $SK/scripts/watch.py run                  # 前台常驻（Ctrl-C 退出）
$PY $SK/scripts/watch.py start                # ⭐ 后台守护（写 data/watch.pid + data/watch.log）
$PY $SK/scripts/watch.py start --ratio 5 --z 4 --cooldown 3600 --rsi-tf 1h
$PY $SK/scripts/watch.py start --no-rsi       # 只跑量能通道
$PY $SK/scripts/watch.py status               # 是否活着 + 最近 15 行日志
$PY $SK/scripts/watch.py stop                 # 按 pidfile 停止
```

**通道 A：量能突增**（WS 订阅 1m K 线，双条件 + 冷却限流）
- `ratio = 当前根量 / 近 120 根中位数 ≥ --ratio`（默认 4.0）
- `z = (量 − 均值)/标准差 ≥ --z`（默认 3.5）
- 或 `ratio ≥ 2×阈值` 直接触发；`--min-vol` 可设绝对量门槛防凌晨低基数误报
- `--cooldown` 默认 1800s，这是**主要限流阀**

触发频率实测（BTC_USDT 1m，窗口 120，999 根样本 ≈16.6h）：

| 阈值 | 命中次数 | 折算 |
|---|---|---|
| ratio≥3 & z≥3 | 36 | 每 28 分钟一次（~52 次/天）**太吵** |
| **ratio≥4 & z≥3.5（默认）** | ~25 | 冷却 30min 后约 ~20 次/天 |
| ratio≥5 & z≥4 | 19 | 每 53 分钟一次（~27 次/天）|

**通道 B：RSI 越界** —— **固定以 1 小时数据判定**（`--rsi-tf 1h` 为默认值），
用 WS 最新价合成未收盘根，所以是准实时的 1h RSI，不必等整点收盘。

```bash
--rsi-tf 1h          # 判定周期（固定默认 1h；可选 15m/4h 覆盖）
--rsi-period 14      # RSI 周期
--rsi-low 30 --rsi-high 70     # 阈值
--rsi-interval 300   # 检查间隔（默认 5 分钟；每轮 1 次 REST）
--rsi-cooldown 3600  # 冷却（超买/超卖两侧**独立计时**，互不屏蔽）
--rsi-hysteresis 3   # 滞回缓冲（见下）
--no-rsi             # 关闭本通道
```

⚠️ **穿越触发（edge trigger）+ 滞回上锁**，两道闸门防刷屏：
1. 只有「上一轮在界内 → 本轮越界」才报一次；长期待在超卖区不会每 5 分钟刷屏。
2. 报完即上锁，必须先退回**阈值 ∓ 滞回**以内（默认 30+3=33 / 70−3=67）才重新武装。
   否则 69.9↔70.1 的边界抖动会连报。
3. 启动时只取基准值不触发；仅当**启动那一刻已在越界区**才上锁（避免吞掉首次真实穿越）。

**1h 触发频率实测**（RSI14，9011 根 ≈ 375 天逐根回放）：

| 滞回 H | 超卖 | 超买 | 合计 | 次/天 | 压缩 | 中位间隔 |
|---|---|---|---|---|---|---|
| 0（不设） | 150 | 134 | 284 | 0.76 | — | 19h |
| 1 | 140 | 127 | 267 | 0.71 | 6% | 21h |
| **3（默认）** | 125 | 104 | **229** | **0.61** | **19%** | **28h** |
| 5 | 113 | 96 | 209 | 0.56 | 26% | 32h |

即默认约 **每 1.6 天一次**，不吵；想要更安静用 `--rsi-hysteresis 5`。
注意 1h 上 RSI14 极少触及极端区（375 天里最低 10.6、最高 91.9），最短间隔只有 2h，
所以滞回是必须的，不能只靠冷却。

> 🚨 **RSI 报警是「提醒」不是「信号」**：§2.2b 实测 RSI 裸用 8 组全负 EV、与随机择时重合。
> 触发只是让你去看一眼 scan 的完整分析（告警日志里也印了这句话），**不要照着反手做**。
> 真要动手，仍然按 TD9 + 2 根确认 + 6 分制打分来。

告警会标注来源与方向：量能侧 `放量上涨 / 放量下跌 / 放量滞涨（吸收）`；
RSI 侧 `RSI 超卖 🟢 / RSI 超买 🔴`。两类都存档 `data/alerts/alert-*.json`（含 `kind` 字段：
`volume` / `rsi`）。通知：设 `BTC_WATCH_WEBHOOK=<url>` 会 POST JSON（企业微信/飞书/Discord 均可）。

**systemd 部署**（Linux 服务器）：

```ini
# /etc/systemd/system/btc-watch.service
[Unit]
Description=BTC volume anomaly watcher
After=network.target

[Service]
Type=forking
# 用 %h/%i 之类占位符或直接写你自己的部署目录；脚本内部路径全是相对的，
# 只要 WorkingDirectory 指到项目根即可（相对路径都以它为基准）
WorkingDirectory=%h/BTC_system
Environment=PYTHONUNBUFFERED=1
ExecStart=%h/venv/bin/python skills/btc-gate-intraday-strategy/scripts/watch.py start --ratio 4 --z 3.5 --cooldown 1800
ExecStop=%h/venv/bin/python skills/btc-gate-intraday-strategy/scripts/watch.py stop
PIDFile=%h/BTC_system/data/watch.pid
Restart=always
RestartSec=30

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload && sudo systemctl enable --now btc-watch
journalctl -u btc-watch -f      # 或直接 tail -f data/watch.log
```

守护进程自带：断线重连（退避 5→30s）、5 分钟无推送判定假死强制重连、
每 2 分钟一行心跳、USDT 本位合约校验、触发时自动带 `--target`。

### ⭐ 改代码后：跑测试（必跑，别偷懒）

任何对 `config.py` / `position_tracker.py` / `watch/*` / `telegram.py` 的修改，
**改完先跑测试再重启 watch**：

```bash
cd scripts && python3 -m unittest discover tests -v
# 期望：Ran 29 tests in 0.009s  → OK
```

29 个测试覆盖：
- `should_skip_new_signal` 全部边界（500 / 501 / 反向 / 多条 / 损坏 JSON / 已成交）
- `_settle_one` long/short 完整路径（部分止盈 / BE 锁保留 / 同根双触防自欺 / 张数损益）
- 关键常量合理性（`ENTRY_MIN_DIST_PTS` / `DEFAULT_TARGET_PTS` / `SETTLER_INTERVAL_SEC`）

为什么必跑：这次重构里 9 个真实事件 bug 全是生产发现（日志回顾 + 用户吐槽），
没有测试意味着"修一个 bug 引入另一个"无法快速发现。

### 盘后 / 策略迭代：回测

```bash
$PY $SK/scripts/update_data.py --status   # 先看缓存是否最新（可选）
$PY $SK/scripts/daytrade.py 1h            # 主信号周期可选 15m / 1h / 4h
```

跑完输出：组件增量测试、双向对比、止损止盈扫描、手续费敏感性、
日内 vs 隔夜、以及基于真实 MAE 的爆仓建模表。

### 更新缓存（回测脚本会自动做，也可手动跑）

```bash
$PY $SK/scripts/update_data.py            # 增量更新：每周期只发 1 次请求
$PY $SK/scripts/update_data.py --status   # 查看是否缺根/落后多久
$PY $SK/scripts/update_data.py --force    # 全量重拉
```

所有回测脚本（`daytrade.py` / `fib_test.py` / `backtest.load()`）**每次执行都会先增量刷新**，
不会用到过期数据。离线时用 `BTC_NO_REFRESH=1`。

> Gate 历史深度上限：1h ≈ 9000 根（375 天）、4h = 5000 根（833 天）、1d = 1500 根（4 年），
> 所以 1h 即使设 `total=20000` 实际也只能拿到 ~9000 根。长周期验证用 1d。

### 抓数据

```python
from gate_fetch import (fetch_ohlcv, fetch_ticker, fetch_funding_rate,
                        fetch_liq_orders, update_all, cache_freshness, GateWS)

bars = fetch_ohlcv("BTC_USDT", "1h", total=5000)                 # 自动增量更新 + 滚动窗口缓存
bars = fetch_ohlcv("BTC_USDT", "1h", 5000, refresh=False)        # 离线：只读本地缓存
update_all({"1h": 20000, "4h": 5000})                            # 批量刷新
cache_freshness()                                                # 查看各周期落后多少根
```

### 验证：这个结果是不是运气

```bash
$PY $SK/scripts/fib_test.py 4h   # 斐波那契策略：档位扫描 + Fib×TD9 + 共振 + 置换检验 + 参数网格
$PY $SK/scripts/fib_test.py 1h   # 换周期交叉验证（务必做，Fib 结论周期特异性极强）
```

**p < 0.05 才算显著。** 样本 < 30 笔的结论一律标注"待验证"。
**任何只在单一周期成立、换周期就翻车的结论，一律视为未证实。**

---

## 五、已知局限（必须告知用户）

1. **样本量小**：最优组合仅 13 笔，参数网格中仅 25% 组合为正，存在过拟合风险。
2. **多重检验**：测 5 个主策略后，Bonferroni 校正阈值应为 0.01，p=0.012 属"边缘显著"。
3. **Gate 历史深度有限**：1h ≈ 9000 根（375 天）、4h = 5000 根（833 天）、1d = 1500 根（4 年）。
   长周期验证请用 1d，或自行落库积累。
4. **1h 弱于 4h**：TD9 在 1h 上噪音大（同样逻辑 45.5% / +0.48%，4h 是 69.2% / +2.94%）。
   若用户坚持日内，需接受更低胜率或只用 1h 做入场执行、4h 出信号。
5. **Fib 结论未达统计显著**：4h 上 Fib+TD9 做多 p = 0.068（边缘），1h 上完全失效
   （网格 0% 为正）。Fib 只能作为**位置工具**使用，不可作为开仓理由。
6. **pivot 确认滞后**：k=5 的摆动点在 4h 上约 20 小时才确认，趋势加速期 Fib 段会明显落后于价格。

## 六、运维陷阱 / 必修补丁（2026-10-04 真实事件）

> 这节是踩过的坑，下次别再栽。三个互相耦合的 bug，**任何一个复发都会让你"收不到入场后的所有推送"**。

### 1. Settler 线程静默死亡 → 你只收到入场提醒，没有 TP/SL 提醒

**症状**：触发 → 收到入场卡片 → 之后 TP1 触发/TP2 触发/SL 扫掉/保本上移 → 全部不推。

**根因**：`watch.py` 启动时会拉起 4 条线程（WS / RSI 检查 / 心跳 / **Settler 持仓结算**）。Settler 在 `_settler_loop()` 里循环调 `position_tracker.settle_all()`，是 TP/SL 推送的唯一入口。原版启动后没任何启动哨兵，线程静默死亡后日志里**完全没有痕迹**——只能从「用户长期收不到 TP/SL 推送」反推。

**修复**：在 `_settler_loop()` 顶部加启动哨兵 + import 异常兜底：

```python
def _settler_loop() -> None:
    log("[settler] 线程启动")            # ★ 启动哨兵
    try:
        from position_tracker import settle_all
    except Exception as e:
        log(f"[settler] 导入 position_tracker 失败（线程退出）: {e}")
        return
    log("[settler] 持仓结算线程已就绪（每 5s 巡检）")
    while True:
        try:
            n_done, n_still = settle_all(verbose=False)
            if n_done > 0:
                log(f"[settler] 结算 {n_done} 条｜仍 pending {n_still} 条")
        except Exception as e:
            log(f"[settler] 巡检异常: {e}")
        time.sleep(5)
```

**验证**：重启 `watch.py start`，30 秒内日志里必须出现两行：
```
[settler] 线程启动
[settler] 持仓结算线程已就绪（每 5s 巡检）
```
否则 Settler 没活着——**TP/SL 推送链全断**。

### 2. 同方向 entry 距离 < 500 点的策略无限堆叠

**症状**：`journal.jsonl` 里出现 N 条同方向、entry 距离 < 500 点的 pending/filled 单，每天累积，永远不结算。

**根因**：准入规则 1/2/3 只写在 `watch.py:JumpTracker._run` 的 180 秒监测里（`entry 距上一单 < 500 点 → 跳过`）。但：
- RSI 触发走 `fire_rsi → emit_scan`，**完全绕开 JumpTracker**
- scan.py 直接调 `journal.log_signal`，冷却期内只"刷新上一条"（看 `recs[-1]`），不查全局同方向距离
- JumpTracker 监测窗口内作废 `status == "pending"`，**对 `status == "filled"` 已经成交的**完全无效

**修复**：在 `scan.py` 写 journal 之前加全局准入守卫——查同合约+同方向+status ∈ {pending, filled} 的所有活跃单，任一 entry 距离新 entry < 500 点直接跳过：

```python
# scan.py 写 journal 前
_skipped_reason = None
try:
    _new_entry = float(_plm["entry_limit"])
    _all_recs = _J._read()
    _sim_pending = [r for r in _all_recs
                    if r.get("contract") == CONTRACT
                    and r.get("side") == main_side
                    and r.get("status") in ("pending", "filled")]
    if _sim_pending:
        _near = min(_sim_pending, key=lambda r: abs(float(r.get("entry", 0)) - _new_entry))
        _dist = abs(_new_entry - float(_near["entry"]))
        if _dist < 500:
            _skipped_reason = (f"同方向已有 {len(_sim_pending)} 条策略活跃，"
                               f"最近 entry={float(_near['entry']):.1f} 距离新策略 {_dist:.0f} 点 < 500")
except Exception:
    pass

if _skipped_reason:
    jstat = "skipped_duplicate"
    P(f"\n  ⚠️ 准入守卫：{_skipped_reason} → 跳过本次 journal 写入")
else:
    jstat = _J.log_signal(...)
```

### 3. 历史堆积的同向单怎么清理

如果 Settler 已经死了几小时，journal 里可能累积了多条同方向 pending。手动清理（保留最新一条，其余 `invalidated`）：

```python
import json, os, time
JOURNAL = '/root/data/journal.jsonl'
with open(JOURNAL) as f:
    recs = [json.loads(ln.strip()) for ln in f if ln.strip()]
pending = sorted([r for r in recs if r.get('status') == 'pending'],
                 key=lambda r: r.get('ts', 0))
keep = pending[-1]
now = int(time.time())
for r in recs:
    if r.get('status') == 'pending' and r.get('id') != keep['id']:
        r['status'] = 'invalidated'
        r['invalidated_ts'] = now
        r['invalidated_reason'] = '历史累积清理（Settler 失效期间堆积）'
tmp = JOURNAL + '.tmp'
with open(tmp, 'w', encoding='utf-8') as f:
    for r in recs:
        f.write(json.dumps(r, ensure_ascii=False) + '\n')
os.replace(tmp, JOURNAL)
```

### 4. 验证清单（每次重启 watch 必跑）

```bash
# 1) Settler 真活着？
tail -f data/watch.log | grep settler
# 30 秒内必须看到两行哨兵

# 2) journal 是否被重复污染？
python3 -c "
import json
from collections import Counter
recs = [json.loads(ln.strip()) for ln in open('data/journal.jsonl') if ln.strip()]
print(Counter(r.get('status') for r in recs))
pend = [r for r in recs if r.get('status') == 'pending']
print(f'pending {len(pend)} 条')
for r in pend:
    print(f'  {r[\"side\"]} entry={r[\"entry\"]:.1f} filled={r.get(\"filled\", False)}')
"
# pending ≤ 1，且若有多条则 entry 距离应 ≥ 500 点

# 3) 主动推一笔 TP/SL 测推送链？
python3 position_tracker.py --settle --verbose
# 若 0 条且有 pending 是正常的（价还没到），不要慌。
# 等价破 TP1/SL 后 grep "settler" 应能看到 [settler] 结算 N 条
```

### 5. 这三个 bug 的耦合关系

```
Settler 死了（bug 1）
   ↓
TP/SL 永远不结算，filled 单卡 pending
   ↓
journal 累积多条同方向 filled（bug 2 没守住的副作用）
   ↓
重启 Settler 后，每条 pending 都会被巡检，但同 entry 距离近的会重复触发 TP1 推送（5 条都触发 1 次）
   ↓
TG 收到 5 条几乎同时的 TP1 提醒，体验混乱
```

**所以三个 bug 必须同时修**——只修 Settler 不修守卫，下一次 Settler 死的时候又会累积。

### 6. 跳空监测卡片在准入规则触发时被吞掉

**症状**：量能异动后收到入场卡片，但 60 秒后**收不到"监测结束"卡片**（作废/保留判定）。

**根因**：`JumpTracker._run` 60 秒窗口结束后，本应先推卡片，再处理 journal 准入规则 1/3。实际是**准入规则 1/3 用 `return` 退出整个函数**——卡片已经构造好了但没机会推出去。

**修复**：准入规则只控制"是否写 journal"（用 `_write_journal_ok` flag），不再用 `return`。卡片永远推，准入规则跳过时在卡片末尾追加一行说明：

```python
# 不再 return
if _skip_reason:
    card += f"\n\n⏸ 准入规则：{_skip_reason}（journal 未写入）"

# 推送永远执行
if tg_token:
    push_telegram(card, tg_token, tg_chat)
```

**验证**：等下次量能异动触发，60 秒后看 TG 应该收到"✅ BTC 跳空监测结束"或"🚨 BTC 策略作废"卡片，并附"⏸ 准入规则"说明行。

### 7. jump 写 journal 时 TP1 用的是 scan payload 的旧值，跟当前 watch target 不一致

**症状**：journal 里 entry=84,638 但 TP1=85,502.7（+864 点），跟你手动算的 entry + 40% × target = 85,038 差 464 点。涨到 85,038 不会触发 TP1 推送，必须涨到 85,502 才推。

**根因**：`JumpTracker._run` 写 journal 时直接从 `pl`（scan payload 的 plan）取 `tp1`：
```python
"tp1": pl.get("tp1", 0),     # ← scan 跑的时候用的 target 可能是 1000
"tp1_pct": 0.24,             # ← 硬编码错误（应该是 1.02，不是 0.24）
"sl_pct": 0.80,              # ← 硬编码，sl_pct 数值是百分比所以 0.80 也不对（应该 0.80 没大问题但硬编码本身不对）
```
但 jump 写 journal 时 watch 进程的 `--target` 可能是 500。两边 target 不一致，scan 算出的 TP1 是用它的 target 算的（或取阻力位），跟当前 watch 配置无关。

**修复**：
1. `start_or_restart` 加 `target_pts` 参数，从 `fire()` 调用时把 `w.target` 传进去
2. `_run` 写 journal 时用 `target_pts` 按"40%/60% target"算法重算 TP1/TP2，不取 `pl.tp1`
3. `tp1_pct`/`sl_pct` 用真实 `(tp1/entry - 1) * 100` 计算，不硬编码

```python
# watch.py fire() 调用 JumpTracker 时传 target_pts
JumpTracker.instance().start_or_restart(
    ...,
    target_pts=w.target,   # ★ 当前 watch 的 target
)

# watch.py JumpTracker._run 写 journal 时重算
_entry = float(pl.get("entry_limit", pl.get("entry", 0)))
_target = float(target_pts)
if side == "long":
    _tp1 = _entry + 0.40 * _target
    _tp2 = _entry + 0.60 * _target
else:
    _tp1 = _entry - 0.40 * _target
    _tp2 = _entry - 0.60 * _target
_tp1_pct = abs(_tp1 / _entry - 1) * 100
rec = {
    ...,
    "tp1": round(_tp1, 1),
    "tp2": round(_tp2, 1),
    "tp1_pct": round(_tp1_pct, 4),
    "sl_pct": round(abs(_sl / _entry - 1) * 100, 4),
    "target_pts": _target,
    ...
}
```

**修复历史 journal**：jump 路径新代码只对**修复后**的 trigger 生效，已有 journal 需要手动重算：
```python
import json, os
JOURNAL = '/root/data/journal.jsonl'
with open(JOURNAL) as f:
    recs = [json.loads(ln.strip()) for ln in f if ln.strip()]
for r in recs:
    if r.get('status') == 'pending':
        entry = r['entry']
        target = 1000  # 或当前 watch 启动值
        r['tp1'] = round(entry + 0.40 * target, 1)
        r['tp2'] = round(entry + 0.60 * target, 1)
        r['tp1_pct'] = round(abs(r['tp1']/entry - 1) * 100, 4)
        r['target_pts'] = target
tmp = JOURNAL + '.tmp'
with open(tmp, 'w', encoding='utf-8') as f:
    for r in recs:
        f.write(json.dumps(r, ensure_ascii=False) + '\n')
os.replace(tmp, JOURNAL)
```

**验证**：重启后跑一次 `python3 scan.py BTC_USDT --brief --target 1000`，看 TP1/TP2 应是 entry + 400/600。等下次 jump 触发后 `grep "[jump] journal 已写" data/watch.log` 应看到 target 字段对得上当前 watch 配置。

### 8. 跨进程配置不一致：scan 跑时的 target ≠ jump 写 journal 时的 target

**症状**：watch 启动 `--target 500`，但 scan 内部硬编码默认 `--target 1000`（`scan.py:212`），且 jump 路径直接拿 scan payload 的 TP1。结果 journal 写出的 TP1 是 1000 的算法，与 watch 配置的 500 不一致。

**根因**：多层调用（watch → emit_scan → scan.py → journal.py）的默认参数没对齐，每个文件有自己的默认值。

**修复原则**：
- `scan.py` 默认 target 应跟 `watch.py` 默认 target 一致（建议都 1000）
- jump 路径必须用**当前 watch 的 target**，不能用 scan payload 的旧值（已通过 bug 7 修复）
- 启动 watch 时明确传 `--target`，不要依赖默认值

**验证清单**（加到 §4）：
```bash
# 4) target 配置对齐？
grep "scan --target" data/watch.log | tail -1     # 当前 watch 用的 target
python3 scan.py BTC_USDT --brief --target 1000 --json /tmp/test.json && \
  python3 -c "import json; d=json.load(open('/tmp/test.json')); print(f'TP1={d[\"plan\"][\"tp1\"]} TP2={d[\"plan\"][\"tp2\"]}')"
# TP1/TP2 应 = entry + 400/600（target=1000）或 entry + 200/300（target=500）
```

### 9. Settler 巡检精度边界（5 秒 vs 1m K 线延迟）

**症状**：价瞬时穿过 TP1/SL（比如 09:34:30 涨到 85,040），但 Settler 1 分钟后才推触发。

**根因**：Settler 不订阅实时 ticker，巡检时**只拉 1m K 线回放**：

```python
# position_tracker.py:364
bars = fetch_ohlcv("BTC_USDT", "1m", 8000, cache=True)
# 对每根 K 线用 high/low 判定 TP/SL
```

具体边界：
- ✅ **1m K 线已收盘的极值**会被捕到（Settler 每 5 秒跑 + 拉 8000 根回放，1m 内最多漏掉 1 根）
- ❌ **当根 K 线（未收盘）内的瞬时极值**不一定会被即时判定——要等下一根 K 线收盘才能判定
- 实际最坏延迟：**5 秒（巡检间隔）+ 60 秒（K 线收盘延迟）** ≈ 65 秒

**修复决策**：当前 50U 名义 + 日内 24h 强平的下，没必要做到秒级实时。65 秒延迟对 TP1 锁利没本质影响（移动 BE 是要的不是速度）。如果未来要做更大名义或更短周期，可改成"巡检时拉 ticker + WS 推送即时判定"。

**验证**：

```bash
# Settler 是否在跑？
grep "settler" data/watch.log | tail -5
# 必须看到 "线程启动" + "持仓结算线程已就绪"

# 边界实测：手动触发一次假 TP1 推送
python3 -c "
import json
with open('/root/data/journal.jsonl') as f:
    for ln in f:
        r = json.loads(ln.strip())
        if r.get('status') == 'pending':
            r['tp1'] = 1.0  # 改成 1 美元，下一秒必触发
            break
open('/root/data/journal.jsonl', 'w').writelines(
    open('/root/data/journal.jsonl').readlines()[:-1] +
    [json.dumps(r, ensure_ascii=False) + '\n']
)"
# 60 秒内应该收到 TP1 推送
```


### 10. 模块化与解耦（2026-10-04 重构）

**症状**：watch.py 一度 **1132 行**，4 个独立关注点（量能 / RSI / 跳空 / 持仓结算）+ CLI + TG 推送全部塞在一个文件里；`position_tracker.py` 循环引用 `watch._load_tg_token` / `push_telegram`。修一个 bug 要在 800+ 行里找位置。

**重构路径**（按依赖顺序拆，每步都验证不破）：

| 顺序 | 动作 | 收益 | 行数变化 |
|---|---|---|---|
| 1 | 抽出 `telegram.py`（独立推送模块） | position_tracker 不再依赖 watch | +191 / -159 |
| 2 | 抽出 `config.py`（共享常量 + `should_skip_new_signal`） | 准入规则单一来源 | +104 / -14 |
| 3 | 写 29 个单元测试（test_config + test_position_tracker） | 改一个 bug 不会偷偷破另一个 | +379 |
| 4 | watch.py 拆包 → `watch/{jump,volume,rsi,settler,runner,...}.py` | 单文件最大 303 行 | watch.py 1132 → 27 |

**重构后的依赖图**（单向，无环）：

```
position_tracker ──┐
                    ├──→ telegram
watch/runner ───────┤
watch/jump ─────────┤──→ config
watch/settler ──────┤──→ watch/{common,journal_ops,...}
watch.py (薄壳) ───→ watch/runner.main()
```

**关键设计原则**：

1. **薄壳 + 兼容 shim 是必须的**：外部脚本写死 `python3 watch.py start`，改包名会全断。`scripts/watch.py` 27 行转发到 `watch.runner.main()`，`watch/__init__.py` 提供老 import（`from watch import JumpTracker`）的兼容 shim。
2. **共享代码放 `common.py`**：log 函数、路径常量、UI 符号——避免子模块互相依赖时循环 import。
3. **延迟导入打破历史循环**：`watch/settler.py` 用 `from position_tracker import settle_all` 写在函数体内（不是模块顶部），避开 watch ↔ position_tracker 循环。
4. **拆包完成必须写测试**：否则拆完你也不知道有没有破——这是这次重构**最大的教训**。

**重构前必跑 / 重构后必跑**：

```bash
# 1. 单元测试必须全过
cd scripts && python3 -m unittest discover tests -v
# Ran 29 tests in 0.009s  → OK

# 2. watch CLI 兼容（外部 systemd / cron 都靠这个）
python3 watch.py once          # 诊断：打印量能分位 + RSI
python3 watch.py start --target 1000   # 守护进程
python3 watch.py status        # 看进程 + 最近日志
python3 watch.py stop          # 按 pidfile 停

# 3. 关键不变量
grep "settler" data/watch.log | tail -3   # 必须看到 "线程启动" + "就绪"
python3 scan.py BTC_USDT --brief --target 1000   # 准入守卫仍工作（journal 不堆叠）
```

**踩到的坑**（详细决策见 `references/modularization.md`）：

- 拆完后**忘了删 watch.py 的死代码**（159 行 telegram 函数定义），又重新来一遍——验证 `wc -l` + `grep "^def _load"` 看到 0 才是干净。
- jump 路径的 **TP1/TP2 用 scan payload 旧值**——拆包时顺手用当前 target 重算（见 §6.7）。
- 拆包前 `WINDOW_SEC = 60` 注释里写 180（历史错）—— 拆包时一起改。

> 💡 **什么时候拆 / 什么时候不拆**：单文件 > 800 行 + 多个独立关注点共存 → 拆；
> < 400 行的工具脚本 / 单一关注点的内部实现 → 不拆（拆完反而增加跨文件跳转）。


### 11. TG HTML 模式裸 `<` / `>` / `&` 直接报 400（2026-10-04 真实事件）

**症状**：监测卡片已构造好，准入规则追加了一句 `距离 106 点 < 500`，TG 推送返回 400 Bad Request：
```
❌ TG 推送失败: HTTP Error 400: Bad Request
{"ok":false,"error_code":400,"description":"Bad Request: can't parse entities: Unsupported start tag "" at byte offset 411"}
```
你**收不到"监测结束"卡片**——但不是卡片被吞（之前 §6.6 修过），是 HTTP 请求被 TG API 直接拒绝。

**根因**：Telegram Bot API 的 `parse_mode=HTML` 模式下，任何裸的 `<` / `>` / `&` 都会被 HTML 解析器当成标签开始。`"< 500"` 看起来像 `<500>` 标签开始，但 TG 不认识 → 400。

**触发场景**：
- 准入规则提示文案里出现数字比较：`距离 106 点 < 500` / `score > 3`
- 任何金额/分数带 `<` `>` `&` 的描述（"毛利 & 损耗" / "5x & 10x"）
- 任何非合法标签的 `<...>` 内容

**修复**：在 `telegram.push()` 里加自动转义层。

**方案 A（不推荐，手动）**：写卡片时把所有 `<` 改成 `＜` 全角、或改文字"小于 / 大于"。
- ❌ 容易漏，特别是新写的脚本/卡片
- ❌ 用户体验差（全角看着别扭）

**方案 B（推荐，自动）**：在 `telegram.push()` 内部调 `_escape_html()`：
- ✅ 调用方写 `< 500` 没事，函数自动转 `<`
- ✅ 合法标签 `<b>` / `<code>` / `<a href="...">` 全部保留
- ✅ 已有合法实体 `&` 等不会被双重转义
- ✅ 单元测试覆盖 17 个边界（test_telegram.py）

实现思路：
```python
def _escape_html(text):
    # 1. 抽出合法标签/实体成 placeholder（不转义）
    # 2. 转义剩余的 < > &
    # 3. 还原 placeholder
```

**修复后验证**：
```bash
cd scripts && python3 -m unittest tests.test_telegram
# Ran 21 tests in 0.008s  → OK
```

**教训**：HTML 模式 vs MarkdownV2 vs 纯文本三种 `parse_mode` 各有坑：
- HTML：裸 `<` `>` `&` 必转义，否则 400（这次的坑）
- MarkdownV2：更多字符要转义（`.` `(` `)` `-` 等都要 `\`）
- 纯文本：完全无格式，不能用 `<b>` 加粗

我们用 HTML 模式是因为支持富文本且转义范围最小（只 3 个字符）。但**任何新写卡片的代码都必须经过 `_escape_html()`**——而 `_escape_html()` 已内置在 `telegram.push()` 里，所有调用方自动受益。

> 💡 **未来加新卡片脚本**：直接 `from telegram import push` 调 `_escape_html` 自动生效。不要直接 `urllib.request.urlopen` 拼 body——会绕过转义层。


### 12. 持仓 TP2 已经触发，但没主动推 TG 卡片（2026-10-04 真实事件）

**症状**：用户凭直觉说"我记得已经 TP2 了"——我手动查 K 线证实：
- 14:22 CST TP1 触发（确认）
- 17:29 CST TP2 触发（确认，K 线 high ≥ 85,238.2）
- journal 里 `outcome=TP1, pos_state=TP1_PARTIAL` —— **不是 TP2_DONE**
- 用户没收到任何 TP/SL 推送卡片

**根因（`_settle_one` 状态机漏写）**：

TP1 触发路径（line 198-224）有显式"立即持久化到 journal"：
```python
sig["tp1_hit"] = True
sig["tp1_hit_ts"] = ts
sig["tp1_hit_px"] = tp1
sig["active_sl"] = entry
sig["pos_state"] = "TP1_PARTIAL"
sig["last_check_ts"] = ts
_notify_partial(sig, "TP1", tp1, contracts, ts)
# ★ 立即把 TP1 状态持久化到 journal
try:
    _recs = _read_journal()
    for _r in _recs:
        if _r.get("id") == sig.get("id"):
            _r.update(...)
            break
    if _matched:
        _write_journal(_recs)
except Exception as _e:
    pass
cur_tp1_hit = True
cur_sl = entry
continue
```

TP2 触发路径（line 225-231）**只设 sig 字段 + return，依赖 settle_all 的 `if n_done` 写回**：
```python
if t_tp2 and cur_tp1_hit and not cur_tp2_hit:
    sig["tp2_hit"] = True
    sig["tp2_hit_ts"] = ts
    sig["tp2_hit_px"] = tp2
    return _close(sig, "TP2_DONE", tp2, ts, ...)  # ← 没有显式写回
```

如果中间出现任何打断（sigil mutate 但 res 没 return），TP1 已写 + TP2 不写 → journal 卡在 TP1_PARTIAL。

**症状诊断三步**：
```bash
# 1. 看 K 线是否真的触发了 TP2
python3 -c "
import sys; sys.path.insert(0, '/root/skills/btc-gate-intraday-strategy/scripts')
from gate_fetch import fetch_ohlcv
bars = fetch_ohlcv('BTC_USDT', '1m', 8000, cache=False)
for b in bars:
    if float(b[2]) >= 85238.2:  # TP2 价
        ts_cst = datetime.fromtimestamp(int(b[0]), tz=timezone.utc) + timedelta(hours=8)
        print(f'[{ts_cst.strftime("%m-%d %H:%M")}] H={float(b[2]):,.1f}')
"

# 2. 手动跑 settle_one 看是否返回 TP2_DONE
python3 -c "
import sys, json; sys.path.insert(0, '/root/skills/btc-gate-intraday-strategy/scripts')
from position_tracker import settle_all
print(settle_all(verbose=True))  # (n_done, n_still)
"

# 3. 看 journal 当前 status
python3 -c "
import json
for r in [json.loads(l) for l in open('/root/data/journal.jsonl') if l.strip()]:
    if r.get('entry') == 84638.2:
        print(f'status={r["status"]} outcome={r.get("outcome")} pos_state={r.get("pos_state")}')
"
```

**修复**（在 `position_tracker.py` line 231 之前插入显式持久化）：
```python
if t_tp2 and cur_tp1_hit and not cur_tp2_hit:
    sig["tp2_hit"] = True
    sig["tp2_hit_ts"] = ts
    sig["tp2_hit_px"] = tp2
    # ★ 镜像 TP1 行为：立即持久化到 journal（避免 settle_all 中断时丢失）
    try:
        _recs = _read_journal()
        for _r in _recs:
            if _r.get("id") == sig.get("id"):
                _r.update({k: v for k, v in sig.items() if v not in (None, "")})
                break
        _write_journal(_recs)
    except Exception:
        pass
    return _close(sig, "TP2_DONE", tp2, ts, contracts, side, entry, "profit")
```

**为什么这个 bug 难发现**：
- TP1 触发时通常 SL 不会立刻打（触 TP1 时价格远离 SL）
- 平时测试用 `_settle_one` 单独跑能正确返回 TP2_DONE
- 但在生产环境，settle_all 每 5 秒跑一次，sig 一直被 mutate + return 中间态

**教训**：任何"先 mutate sig + 再 return res 让 settle_all 写回"的模式都是脆弱的——**关键状态变更必须立即持久化**。

### 13. patch tool 会反转 HTML entity 字符串（工具 quirks，必看）

**症状**：写 `replace("&", "&")` 在 Python 里完全没效果——`"&"` 和 `"&"` 在 patch 工具的字符串序列化中被反转成同一个字符。

**示例**（写文件时本意）：
```python
text = text.replace("&", "&").replace("<", "<").replace(">", ">")
```
**实际写入后**：
```python
text = text.replace("&", "&").replace("<", "<").replace(">", ">")
```
——三个 replace 调用全部无效！

**解决方案**：用 `chr()` 拼接 entity 字符：
```python
AMP = chr(38) + "amp;"   # → &
LT  = chr(38) + "lt;"    # → <
GT  = chr(38) + "gt;"    # → >
text = text.replace("&", AMP).replace("<", LT).replace(">", GT)
```
或直接用 ` ` placeholder：
```python
text = text.replace("\x26", "\x26amp;")  # & + amp;
```

**测试用例必须用 helper 函数构造 entity**：
```python
def E(s):
    """构造 HTML entity 期望值（避免 patch 工具反转 < / > / &）"""
    AMP = chr(38) + "amp;"
    LT  = chr(38) + "lt;"
    GT  = chr(38) + "gt;"
    return s.replace("&", AMP).replace("<", LT).replace(">", GT)

self.assertEqual(_escape_html("100 < 200"), E("100 < 200"))
```

**这个坑影响**：
- 任何包含 `<` / `>` / `&` 的字符串字面量都会被反转
- 写 HTML entity 转义器、Telegram HTML 卡片、JSON 模板时尤其要小心
- 测试断言字符串里的 `<` / `&` 等期望值会被反转成裸字符，导致断言永远通过（false negative）

**验证修复是否生效**：
```bash
grep "replace.*&" scripts/telegram.py
# 看到 chr(38) + "amp;" 才说明正确，不是 "→ &" 这种被反转的
```



### 14. Settler 静默死亡第二弹：sig in-place mutate 不写回 journal（2026-10-04 真实事件）

**症状**：你 84,638.2 的多单在 **17:29 CST 触发了 TP2**（H 到了 85,345，远超 TP2=85,238.2）——但**你没收到任何 TP/SL 推送**。看 journal：
```
status: settled
outcome: TP2_DONE         ← 是 TP2 不是 TP1
exit_ts: 17:29 CST        ← 正确
net_usd: +26.67           ← 正确
```
**数据是对的，但 TG 推送没发**。直到你 22:35 问我"是不是 TP2 了"，我手动跑 `settle_all` 才真正写回 status。

**根因（双重 bug）**：

#### Bug A：Settler 静默死亡
- 11:46:31 启动哨兵 + 就绪日志
- 之后 **8.5 小时完全静默**——watch.log 里**零**巡检日志
- 14:00~22:35 期间本来应该跑 ~6000 次巡检（每 5 秒），**全部缺失**
- 第一版"启动哨兵 + 异常兜底"只修了两类死法（线程没起来 / 抛异常），**第三类（hung up 无异常，如 SIGSEGV/OOM/GIL 死锁）仍能跑过去**

#### Bug B：TP1 触发后 mutate sig 但不写回 journal
`_settle_one` 在 TP1 触发时（line 198-224）做了**两件事**：
1. 写 `tp1_hit=True, pos_state=TP1_PARTIAL, active_sl=BE` 到 sig（line 200-204）
2. 立即写回 journal（line 209-219）—— 这部分 OK

但 line 222-224 `cur_tp1_hit=True, cur_sl=entry, continue`——**继续循环扫下一根 K 线**。

如果下一根 K 线**没有触发 TP2**（价格波动不够大），循环一直扫到 K 线尽头 → `return None`（line 239）。settle_all 那边的 `if res: ... else: n_still += 1`——**只数 n_still，不写回 journal**。

但 `sig["last_check_ts"]` 已经被 line 206 设置成 TP1 触发时刻（14:22）——**这一行成功写回 journal**（因为 line 209-219 的写回）。

**所以 journal 里有 `last_check_ts=14:22` 但 status 还是 pending**——下次扫描从 14:22 开始，TP2 那根 K 线（17:29）会触发，**理论上应该 return _close 写回**——但因为 **Bug A（Settler 死了），settle_all 根本没跑**。

直到 22:35 你问我，我才手动触发。

**修复**：

1. **Settler 心跳哨兵 + 文件**（`watch/settler.py`）：
   - 每轮循环写心跳到 `data/settler_heartbeat.json`（即使 n_done=0）
   - 每 60 轮（5 分钟）打一行日志心跳
   - 新增 `is_alive(max_age_sec=30)` 函数，外部脚本能查 Settler 是否活着
   - `watch.py status` 显示 settler 健康状态

2. **journal 写入策略简化**（保留现状，**显式比隐式好**）：
   - TP1 触发已经显式写回（line 209-219）✓
   - TP2 触发直接 `return _close` → settle_all 那边的 `r.update(res)` 会写回 ✓
   - 当前 bug 实际是 **Settler 死了导致 settle_all 没跑**，所以根本没机会触发 TP2

**核心教训**：启动哨兵只保证"线程到了第一行"——**心跳哨兵才保证"线程在循环里"**。任何长跑线程都应该同时有这两种哨兵。

**验证**：
```bash
# 1. 跑测试（新增 11 个 settler 测试）
cd scripts && python3 -m unittest tests.test_settler -v
# Ran 11 tests in 0.019s  → OK

# 2. 检查 settler 健康（status 命令现在显示）
python3 watch.py status
# watch pid=XXXX  运行中
# settler: ✓ 健康（心跳新鲜）

# 3. 手动读心跳文件
cat data/settler_heartbeat.json
# {"ts": 1791125325, "n_iter": 1, "n_done_total": 0, "n_still": 1, "alive": true}

# 4. 5 分钟后日志应出现
grep "settler 心跳" data/watch.log | tail -3
# [10-04 22:54:46] [settler] 心跳 #60（累计结算 1 条｜当前 pending 1 条）
```

**未来加 cron 监控**（建议）：
```bash
# 每分钟检查一次心跳，死了就报警
* * * * * python3 -c "
from watch.settler import is_alive
import sys
ok = is_alive(max_age_sec=120)
if ok is False:
    print('Settler 心跳过期！', file=sys.stderr)
    sys.exit(1)
"
```

> 💡 **为什么不直接修 hang 的根因**：可能是 SIGSEGV（native 代码崩溃）、OOM killer、Python GIL 死锁、第三方 C 扩展卡住——**任何一种都不在 Python 层能彻底防住**。心跳哨兵是"防御纵深"——主线程死了还能从外部发现。


### 13. scan/jump 信号显示规则：同方向复用进场数据，反方向自动平仓 + 不受 500 点限制（2026-10-04 真实需求）

**症状**：你已经有 filled 多单（id=BTC_USDT-1791060667-long @ 84,638.2）时，scan 仍然算出新点位 85,179.2 / TP 85,579.2 推 TG 卡片——你看到两个 entry，不知道以哪个为准。

**用户规则**（2026-10-04 你给的）：
- **同方向已有进场**：TG 卡片**整张复用**已进场数据（entry/TP/SL/contracts），不展示 scan 新算的点位
- **反方向触发**：自动平掉已进场单（模拟平仓，不调交易所 API），写反方向 journal（**不受 500 点距离限制**），推 TG 卡片说明"自动平仓 + 反方向新策略"

**实现**：

#### 1) `telegram.format_card(kind, alert, scan_payload, override_active=None)`

新增 `override_active` 参数。如果传入已进场 journal 条：
- **同方向**：用其 entry/TP/SL/contracts 替换 scan_payload 中的字段，整张卡片复用进场数据
- **反方向**：保留 scan_payload（反方向点位有效），但加 marker "📌 已进场（反方向 long）｜ id=... → 自动平仓 + 写反方向 journal"

```python
# telegram.py
if override_active:
    _active_side = override_active.get("side")
    if _active_side == side:
        _overridden_plan = {  # 用进场数据
            "entry_limit": override_active.get("entry"),
            "sl": override_active.get("sl"),
            "tp1/tp2/tp3": override_active.get("tp1/tp2/tp3"),
            "contracts": override_active.get("contracts"),
            ...
        }
```

#### 2) `watch/runner.py:emit_scan()` 自动找 override_active

```python
# 找最近一条 status=filled 的同合约 journal 条
_override = None
try:
    _jpath = os.path.join(DATA_DIR, "journal.jsonl")
    _jrecs = [json.loads(ln) for ln in open(_jpath) if ln.strip()]
    for _jr in reversed(_jrecs):
        if _jr.get("contract") == contract and _jr.get("status") == "filled":
            _override = _jr
            break
except Exception: pass

card = format_tg_card(kind, out, scan_payload, override_active=_override)
```

#### 3) `scan.py` + `watch/jump.py` 反方向自动平仓

scan 路径和 jump 路径在准入守卫后、写 journal 前都加这段：

```python
# 找最近一条 filled 反向单
_jrecs = _J._read()
_filled_opposite = None
for _jr in reversed(_jrecs):
    if (_jr.get("contract") == CONTRACT
            and _jr.get("status") == "filled"
            and _jr.get("side") != main_side):
        _filled_opposite = _jr
        break

if _filled_opposite is not None:
    from position_tracker import force_close_position
    _close_res = force_close_position(
        exit_px=float(last),   # 市价
        reason=f"scan/jump 反方向信号触发，自动平仓",
        signal_id=_filled_opposite.get("id"),
    )
    # 之后正常写新反方向 journal（不受 500 点限制）
```

#### 4) `position_tracker.force_close_position(exit_px, reason, signal_id)`

新增函数（不入真单，纯本地模拟平仓）：
- 找指定 id（无则取最新 filled）的 journal 条
- 调 `_close()` 算 PnL
- 标记 `status=settled, outcome=MCLOSE`
- 写回 journal + 推 TG 卡片（_notify_close）
- 写 position_events.jsonl

**关键**：force_close_position 是**模拟**（不入真单），与"持仓跟踪模拟器（不入真单）"的设计一致。

**测试覆盖**（7 个新测试）：
- `test_no_override_uses_scan_data`（无 override 用 scan）
- `test_same_side_override_replaces_entry`（同方向 → 用 84,638.2，不用 85,179.2）
- `test_same_side_override_replaces_tp_sl`（同方向 TP/SL 全部替换）
- `test_same_side_override_replaces_qty`（同方向张数替换为 591）
- `test_reverse_side_keeps_scan_data`（反方向保留 scan + 标记）
- `test_no_active_no_marker`（无 override 不显示标记）
- `test_override_for_rsi_kind`（RSI 卡片也生效）

加上 force_close_position 的 6 个测试 = **74 个测试全过**。

**TG 卡片对比示例**：

修复前（同方向已进场）：
```
🚨 BTC 量能异动  放量
ratio 5.00× ｜ z 4.00 ｜ 现价 85,000.0
判定：临界（再等 1 根确认）
方向：long (long 4 / short 1)
入场 85,179.2 ｜ 止损 84,497.7 (0.8)        ← scan 算的新点位
止盈  T1 85,579.2 / T2 85,779.2 / T3 86,179.2  ← 用户困惑
仓位 146 张
```

修复后（同方向已进场）：
```
🚨 BTC 量能异动  放量
ratio 5.00× ｜ z 4.00 ｜ 现价 85,000.0
判定：临界（再等 1 根确认）
方向：long (long 4 / short 1)
入场 84,638.2 ｜ 止损 83,961.0 (0.8)         ← 沿用进场数据
止盈  T1 85,038.2 / T2 85,238.2 / T3 85,638.2 ← 用户不困惑
仓位 591 张
📌 已进场（同方向）｜ id=BTC_USDT-1791060667-long
   沿用进场 entry/TP/SL（scan 不重复计算）
```

修复后（反方向触发）：
```
🚨 BTC 量能异动  放量
判定：临界
方向：short
入场 85,179.2 ｜ ...                         ← 反方向点位有效
📌 已进场（反方向 long）｜ id=BTC_USDT-1791060667-long
   触发反方向信号 → 自动平仓 + 写反方向 journal

（之前另推一条卡片）🔄 自动平仓（多→空反转）
  id: BTC_USDT-1791060667-long
  平仓价 85,000.0（市价）｜ 净 +362.0U
  原因：反方向信号触发
```

|> 💡 **核心改动**：telegram.format_card 接收 override_active 后，整张卡片的**数据源**变成已进场 journal——scan 算的只在反方向时生效。这种"展示层 vs 决策层"分离让卡片永远反映**真实持仓**，不会误导。

### 15. 量能异动是 trigger 不是 signal（2026-10-05 交易哲学修正）

**症状**：每隔 30 分钟（量能异动 cooldown 默认）就刷一条新入场到 journal / TG，即使行情结构没变；用户长期收到"信号"但实际是噪声 → 决策疲劳 + journal 堆积。

**根因**：原 `watch.runner.fire()` 把"量能异动"当成"信号"——冷却通过 → 立即调 `emit_scan` → 调 `JumpTracker` 写新 pending。但用户的实际交易习惯是右侧突破：入场点位等 **1h K 线收完 + 结构变化**才定。30 分钟一次的量能异动只是提醒"环境变了"。

**修复**：把 `fire()` 拆成"提醒 + scan 报告"，不再主动写 journal。JumpTracker 的"写新 journal"路径加一道 1h 收盘窗口门控（只在整点附近 60s 才允许写）。

```python
# scripts/watch/runner.py:fire() 核心改动
def fire(w: VolumeWatcher, a: Dict) -> None:
    now = time.time()
    if now - w.last_fire < w.cooldown:
        log(f"{WARN} 量能异动但冷却中（{w.cooldown-(now-w.last_fire):.0f}s 内已触发）→ 跳过")
        return
    w.mark_fired()           # ★ 内存 + 磁盘同时更新（修重启清零 bug，见 §16）
    # ... 跑 emit_scan 出报告 + 推 TG ...
    # ★ 不再调 JumpTracker.start_or_restart()
    # 卡片里 TG 推送由 emit_scan 内部走 format_tg_card 完成，标"trigger 不是 signal"。

# scripts/watch/jump.py 写 journal 路径前加 1h 窗口门控
_now_ts = int(time.time())
_h_aligned = _now_ts % 3600 < 60      # 距整点 60s 内才算 1h 刚收完
if not _h_aligned:
    _skip_reason = (f"非 1h 收盘窗口（距整点 {_now_ts % 3600}s）"
                   f"→ 量能异动只 trigger，不写新 journal")
    _write_journal_ok = False
```

**验证**：触发量能异动后看 TG 卡片应显示"⚠️ 这是 trigger 不是 signal"；看 journal.jsonl pending 数不应增长（除非 1h 刚收完）。

### 16. last_fire 进程重启清零 → 冷却失效（2026-10-05 真实 bug）

**症状**：watch 重启后第一笔量能异动距离上次触发只有 113 秒（wisk.log 实测），而不是期望的 1800 秒。等于"重启就能绕过冷却刷一张单"。

**根因**：`VolumeWatcher.last_fire = 0.0` 是 `__init__` 里的默认值，**纯内存**，进程退出就丢。每次 `watch.py start` 启动一个新进程，last_fire 都从 0 开始重计。

**修复**：持久化到 `DATA_DIR/last_fire.json`，按 contract 分 key。原子写（`.tmp + os.replace`）。

```python
# scripts/watch/volume.py
_LAST_FIRE_PATH = os.path.join(DATA_DIR, "last_fire.json")

def _load_last_fire(contract: str) -> float:
    try:
        if os.path.exists(_LAST_FIRE_PATH):
            return float(json.load(open(_LAST_FIRE_PATH)).get(contract, 0.0) or 0.0)
    except Exception:
        pass
    return 0.0

def _save_last_fire(contract: str, ts: float) -> None:
    try:
        old = json.load(open(_LAST_FIRE_PATH)) if os.path.exists(_LAST_FIRE_PATH) else {}
    except Exception:
        old = {}
    old[contract] = float(ts)
    tmp = _LAST_FIRE_PATH + ".tmp"
    json.dump(old, open(tmp, "w", encoding="utf-8"))
    os.replace(tmp, _LAST_FIRE_PATH)

class VolumeWatcher:
    def __init__(self, ...):
        # ★ 从磁盘读，恢复上次进程的 last_fire
        self.last_fire = _load_last_fire(self.contract)
        ...

    def mark_fired(self) -> None:
        """更新 last_fire（内存 + 磁盘两处都要改）"""
        self.last_fire = time.time()
        _save_last_fire(self.contract, self.last_fire)

# watch/runner.py:fire() 改用 mark_fired() 而不是直接赋值
w.mark_fired()           # 代替 w.last_fire = now
```

**清理遗留假数据**：测试时若伪造过 last_fire.json 文件，重启前 `rm` 掉让干净值生效（`last_fire=0` 才是新启动正确状态）。

**验证**：重启 watch 一次 + 触发量能异动 → 检查 `cat /root/data/last_fire.json` 应含合约 key 和当前 ts；再次重启 + 立刻触发应被冷却挡住（`grep "量能异动但冷却中" data/watch.log`）。

### 17. `status=pending + filled=true` 不是已结算，是"持仓跟踪中"（2026-10-05 语义澄清）

**症状**：watch 状态面板把一条 `status=pending, filled=true, fill_ts=已设`, status=pending` 的 journal 显示成"挂单中"——用户看着像"刚挂还没成交"，但实际上已经成交了 12 分钟、还在持仓监控中（等 TP/SL）。

**根因**：position_tracker 的 `status` 字段不是单一维度，是 **2D 状态机的投影**（详见 `references/active-positions-query.md`）：
- `status="pending"` + `filled=false` → 真挂单未成交
- `status="pending"` + `filled=true` → 已成交 + 还在持仓（settle_all 还在巡检）
- `status="settled"` → 已平仓（但**还要看 `outcome`/`pos_state` 决定是否还有 2/3 部位**）

**任何展示层代码**（前端 / TG 卡片 / cron 报告）只看 `status` 都会把这个语义错读。

**修复**（前端展示层）：

```javascript
// web/index.html 渲染策略卡片
let statusPill, statusText;
if (a.status === "filled" || (a.status === "pending" && a.filled === true)) {
  statusPill = `<span class="pill ok">已进场</span>`;
  statusText = "已进场（持仓中）";
} else if (a.status === "pending") {
  statusPill = `<span class="pill warn">挂单中</span>`;
  statusText = "挂单未成交";
} else {
  statusPill = `<span class="pill idle">${a.status}</span>`;
  statusText = a.status;
}
```

**推广原则**：写"当前策略"/"活跃持仓"任何展示/查询逻辑前，**必须**先看 `references/active-positions-query.md`——里面列了完整的 2D 状态机映射。直接抄 `status=="pending"` 当成"挂单中"是常见踩坑。

### 18. `web/` 状态面板（3003 端口，2026-10-05 新增）

**适用场景**：不开浏览器到 Gate.io、不开 telegram、想快速看一眼"watch 还活着？策略啥状态？最近胜率多少？"。

**启动**：

```bash
cd /root/skills/btc-gate-intraday-strategy/web
python3 server.py --port 3003          # 前台跑（调试）
nohup python3 server.py --port 3003 > /tmp/btc_status.log 2>&1 &  # 后台跑
```

**页面包含**（每 1s 自动刷新）：
1. **进程状态** — watch pid + settler 心跳（运行中 / 心跳异常 / 未运行）
3. **策略冷却** — 距上次量能异动多久（已就绪 / 冷却中 / 无历史）
3. **数据更新** — 1m / 1h / 4h K 线最后 close + 距今多久
4. **BTC 现价** — 从本地 1m K 线最后一根读（避免远程 rate limit）
5. **当前策略** — pending/filled journal（注意 §17 的 2D 状态语义）
6. **胜率三联** — 总胜率 / 最近 20 笔 / 近 7 天（保本也算赢，按 `category=profit` 计）
7. **最近 5 笔结算** — id（截断 35 字符）/ 类别 / 净 USD / 退出时间

**胜率算法**（用户明确确认）：

```python
# web/server.py:_winrate()
# 从 position_events.jsonl + journal 算（dedupe by id，取最新）
# 计算 entry="category == 'profit' 算赢（含保本止损 BE_STOPPED）"
```

注意 `BE_STOPPED` 的 `net_usd` 可能为负（手续费吃掉了 TP1 锁利），但 `category=profit` 是 position_tracker 的归类——别按 net_usd > 0 重写。

**数据源全部本地**（`/root/data/*.jsonl + *.json`），**不调 Gate.io API**——面板本身不会触发远程 rate limit。

**未来增强**（如果需要）：
- 缓存策略事件推送（TG → 数据库）→ 面板显示推送历史
- 加图表（推荐 Chart.js，不引第三方打包）展示最近 24h BTC + 触发时间点
- 加 cron 自动拉起（现在手动 nohup）

### 19. 量能异动 ≠ 入场信号，1h K 线收完才是（2026-10-05 真实事件）

**症状**：之前 `fire()` 把量能异动当成入场信号——每次触发都跑 JumpTracker 写新 journal。结果每 30 分钟就生成一条新入场点位（用户在 TG 卡片堆里看到入场条目刷屏），但**真正的入场决策应该看 1h K 线收完 + 结构是否变**。

**修复**（双层）：

**第一层**：`fire()` 不再调度 JumpTracker 写 journal，只推"trigger 不是 signal"卡片：

```python
# scripts/watch/runner.py:fire()
def fire(w, a):
    if now - w.last_fire < w.cooldown:
        log("冷却中 → 跳过"); return
    w.mark_fired()
    # 跑 emit_scan 出报告 + 推 TG，但不调 JumpTracker
    out = emit_scan(...)
    # ★ 不再调 JumpTracker.start_or_restart()
    log("⚠️ 这是 trigger 不是 signal——提醒你看 1h K 线")
```

**第二层**：主循环加 `fire_hourly()`——每整点 60s 窗口内自动评估：

```python
# scripts/watch/runner.py:fire_hourly()
def fire_hourly(w):
    now_ts = int(time.time())
    if now_ts % 3600 >= 60:        # 不在整点后 60s 窗口内
        return
    hour_bucket = now_ts - (now_ts % 3600)
    if getattr(w, "_last_hourly_bucket", 0) == hour_bucket:   # 同一小时已触发
        return
    w._last_hourly_bucket = hour_bucket

    # 跑 scan + 推 TG + 检查趋势反转 + 调度 jump
    out = emit_scan(w.contract, "1h", "hourly", alert, ...)
    scan_payload = (out.get("scan") or {}).get("payload") or {}
    _check_trend_reversal(w.contract, scan_payload)     # ★ 详见 §6.21
    scan_payload["force_journal"] = True                # 跳过 jump 自己的 1h 门控
    JumpTracker.instance().start_or_restart(...)
```

**jump.py 的 1h 门控**（`scan_payload["force_journal"]=True` 时放行）：

```python
# scripts/watch/jump.py:写 journal 前
_h_aligned = _now_ts % 3600 < 90                       # 90s 窗口（给 jump 60s 留余量）
if not _h_aligned and not scan_payload.get("force_journal"):
    _skip_reason = "非 1h 收盘窗口"; _write_journal_ok = False
```

**验证**：连续 3 天观察 watch.log 应看到每整点都有"⏰ 1h K 线收盘检查"日志；journal.jsonl 中 pending 数应远低于"每 30min 一条"的频率。

### 20. 趋势反转 vs 持仓方向：反方向立即平仓（2026-10-05 真实事件）

**症状**：策略做多 long，但 1h K 线收盘后 4h EMA169 斜率转负 + close 跌穿 4h EMA144 → 趋势明确从多头 → 空头。此时还死拿 long 不动 = 扛单。

**修复**：在 `fire_hourly()` 内嵌 `_check_trend_reversal()`：

```python
# scripts/watch/runner.py
def _check_trend_reversal(contract, scan_payload):
    if not scan_payload: return
    tags = scan_payload.get("tags") or {}
    trend = tags.get("trend", "")
    if "多头" in trend: new_dir = "long"
    elif "空头" in trend: new_dir = "short"
    else: return                                                # 震荡不算反转

    # 找当前持仓（注意 §17 的 2D 语义：pending + filled=True）
    active = next((r for r in reversed(recs)
                   if r.get("status") == "pending" and r.get("filled") is True), None)
    if not active or active.get("side") == new_dir:
        return                                                  # 同方向或无持仓

    # 反方向！立即 force_close
    force_close_position(exit_px=scan_payload["last"],
                         reason=f"趋势反转：{trend} ({new_dir}) vs 持仓 {active['side']}")
```

**验证**：journal.jsonl 中应在每次整点跑完后有一个对应 id 的 settled 记录（reason="趋势反转..."）；TG 推送应收到 `🔄 自动平仓` 卡片。

### 21. "已进场"查询统一改用 `filled=True` 而非 `status=filled`（2026-10-05 多次踩同一坑）

**症状**：3 个不同位置用 `status == "filled"` 查"已成交"却查不到——因为 position_tracker 的真实生产语义是 `status="pending" + filled=True`（详见 §17）。3 处都"看起来对"但都错：

| # | 文件:行 | 影响 |
|---|---|---|
| 1 | `watch/runner.py:76`（em it_scan 找 override_active） | TG 卡片永远不显示"已进场实况"块 |
| 2 | `watch/jump.py:197`（找 _active 检查反向） | 反方向信号触发时自动平仓跑不到 |
| 3 | `position_tracker.force_close_position()` line 381/388（找 target） | 反方向平仓 API 直接返回 None |
| 4 | `config.py:97 should_skip_new_signal()` 查"已成交" | 准入守卫失效——1h 整点 fire_hourly 触发时，**已在持仓的同方向新单被允许再写一条**，journal 累积第二条同向单 |

**症状**（新增 #4）：journal 出现两条同方向 long（一个 `status="pending"+filled=true` 已成交，一个 `status="pending"+filled=false` 新挂单）—— 两个看似独立的活跃单。前端显示"持仓中 + 挂单中"共存。

**根因**：`should_skip_new_signal` 内 `if st == "filled": sim.append(r)` 漏掉生产语义的 `pending+filled=True`，返回 `(False, "")`，jump.py 规则 1 放行，fire_hourly 写第二条。

**修复**：第 4 处也改为同一判别：

```python
is_filled = bool(r.get("filled"))
if st == "pending" and is_filled:
    sim.append(r)        # 已成交未平仓
elif st == "pending":
    # pending 超 MAX_PEND_SEC 才跳过
    if now - ts <= MAX_PEND_SEC:
        sim.append(r)
```

**修复**：4 处全改为同一判别：

```python
# 唯一正确的"已成交未平仓"判别
if r.get("status") == "pending" and r.get("filled") is True:
    # 已成交、settle_all 还在巡检、可能已触 TP1 锁利
```

**配套修复**：测试 `test_force_close.py` / `test_config.py` 的种子数据也得改——之前测试用 `status="filled"` 是错的，测出的"绿"也是假绿。**测试种子必须用真实生产语义**才能保证 force_close / should_skip 的修改真的被覆盖。

**推广原则**：在 watch/telegram/journal/position_tracker/config 任何代码里**凡是查"活跃已成交持仓"**，都用 `status="pending" and filled=True`。直接抄旧版 `status == "filled"` 的判断是常见踩坑。看到 `status == "filled"` 就要警觉——它可能是历史包袱。

**一次扫净的清单**（每次新增"活跃已成交持仓"判定前都要 grep）：

```bash
grep -rn 'status.*==.*"filled"' scripts/ tests/
grep -rn 'st == "filled"' scripts/
```

如果新出现任何一条，先确认是不是占位（settled 终态判断 = "settled"/"invalidated"），不是 → 改。

### 22. TG 卡片 override_active 加"已进场实况"块（2026-10-05 新增）

**场景**：持仓 long @85930.1 已成交 30 分钟 → 量能异动触发推送 → 用户想在卡片里直接看到"成交价 / 当前价 / 距今多久 / 未实现盈亏"。

**修复**（`telegram.format_card()` override 块）：

```python
# scripts/telegram.py
if _fill_px is not None and side == _active_side:       # 同方向 override
    _elapsed = int(time.time()) - _fill_ts
    _h, _m = _elapsed // 3600, (_elapsed % 3600) // 60
    _fill_time_str = f"{_h}h{_m}min 前" if _h else f"{_m}min 前"

    # 当前价 vs 成交价：方向符号对齐（避免"+164 点"写跌那种错向）
    if side == "long": _diff = _cur_px - _fill_px
    else:             _diff = _fill_px - _cur_px
    _sign = "+" if _diff >= 0 else ""

    _net_pnl = _diff * (_contracts * 0.0001) - 4.85     # 扣单笔费用
    _pnl_color = "🟢" if _net_pnl >= 0 else "🔴"

    _fill_status_block = (
        f"\n📍 <b>已进场实况</b>"
        f"\n   成交价 <b>{_fill_px:,.1f}</b> ｜ {_fill_time_str}"
        f"\n   当前 <b>{_cur_px:,.1f}</b> ({_sign}{_diff:.0f} 点 / {_sign}{_diff_pct:.2f}%)"
        f"\n   张数 {_contracts}  未实现 {_pnl_color} <b>{_sign}{_net_pnl:.2f}U</b>"
    )
```

**反方向 override** 同样展示（成交价 + 浮盈亏）让用户知道"现在还浮盈/亏多少"。

**坑**：找 caller 不能用 `status == "filled"`（见 §21）。`override_active` 必须从 `runner.py:em it_scan` 的 TG 推送路径传过去，且用对的语义查。

### 23. 回测验证保护规则：先证伪再上代码（2026-10-06 真实方法论）

**场景**：你在回测里发现 13 笔已进场策略总亏 -60 U，立刻头脑风暴出 3 个"修法"——同方向 30min 内拒入场、下跌反弹减分、1h 后浮亏减仓。直觉上三个都对，但**真上线前必须回测验证 PnL 是真改善**。

**踩坑教训**（这次的真实过程）：

**1. 头脑风暴的修法可能反直觉**——3 个建议里只有 1 个真有效：

| 修法 | 看似合理 | 实测结果 | 决策 |
|---|---|---|---|
| Task 1：同方向 30min 拒入场 | ✅ 拦重复 | ✅ **+40 U** | **上线** |
| Task 2：下跌中段反弹减分 | ✅ 防追涨杀跌 | ⚠️ 本样本无额外拦截 | **保留代码待样本验证** |
| Task 3：1h 后浮亏主动减仓 50% | ✅ 看起来"止损" | ❌ **-10 U**（震荡市反而亏） | **默认关闭** |

**Task 3 失败的根因**：回测发现笔 #5 / #13 都是"1h 时浮亏但后续涨了"——主动减仓把"未来会触 TP1 的单"砍在浮亏点上。震荡市这种"短期回调"非常常见，主动减仓等于**左侧杀跌**（违反用户右侧交易原则）。

**2. 回测脚本模板**（一次性，可用 `python3 scripts/backtest.py` 风格）：

```python
# 加载 journal.jsonl（已 fill 的真实历史）+ 1m K 线缓存
import json
from pathlib import Path

jp = Path("/root/data/journal.jsonl")
recs = [json.loads(ln) for ln in jp.read_text(encoding="utf-8").splitlines() if ln.strip()]
klines = json.load(open("/root/data/BTC_USDT_1m_8000.json"))
kmap = {int(k[0]): (float(k[2]), float(k[3]), float(k[4])) for k in klines}   # high, low, close

# 筛时间窗口
import datetime as dt
d_start = dt.datetime(2026, 10, 4).timestamp()
d_end   = dt.datetime(2026, 10, 7).timestamp()
# ⚠️ journal 的 fill_ts 是 watch tracker 写回字段，未填时默认 0（1970-01-01），
# 真实下单时间用 r["ts"]。这条今天(2026-10-06)踩坑了：fill_ts 全 0 会导致
# 所有"已 fill"记录都不在 10-04 窗口里，回测样本变 0。
filled = sorted(
    [r for r in recs if r.get("filled") and d_start <= r["ts"] < d_end],
    key=lambda r: r["ts"])

# 单笔模拟：fill_ts 后 4h 窗口内 SL/TP1/TP2 谁先触发
def simulate_one(r):
    fill_ts = int(r["fill_ts"])
    fill_px = float(r["fill_px"])
    sl, tp1, tp2 = float(r["sl"]), float(r["tp1"]), float(r["tp2"])
    contracts = float(r["contracts"])
    side = r["side"]
    sim_klines = [(ts, h, l, c) for ts, (h, l, c) in kmap.items()
                  if fill_ts + 60 <= ts <= fill_ts + 4 * 3600]
    if not sim_klines: return None
    # 判定：每根 K 线 high/low 同时刻度对照 TP/SL，谁先 hit
    tp1_hit = sl_hit = tp2_hit = False
    for ts, h, l, c in sim_klines:
        if side == "long":
            if not tp1_hit and not sl_hit:
                if h >= tp1 and l <= sl: tp1_hit = sl_hit = True; break
                elif h >= tp1: tp1_hit = True
                elif l <= sl: sl_hit = True; break
            else:
                if h >= tp2: tp2_hit = True; break
                elif l <= fill_px: sl_hit = True; break
        # short 镜像...
    # 按 TP1/TP2/SL/未触发算 PnL（参考 position_tracker.py:_close）

# 准入模拟（Task 1）
def would_skip_task1(admitted, candidate):
    ct = int(candidate["fill_ts"])
    for r in admitted:
        if r.get("side") != candidate.get("side"): continue
        if 0 <= (ct - int(r["fill_ts"])) < 1800:
            return True
    return False

# 跑四组对比：A（原策略）、B（+T1）、C（+T1+T2）、D（+T1+T2+T3）
admitted_a, admitted_b, ... = [], [], ...
for r in filled:
    admitted_a.append(r)                                            # A 全保留
    if not would_skip_task1(admitted_b, r): admitted_b.append(r)    # B 拦重复
    # C 加 T2 / D 加 T3 类似
total_a = sum(simulate_one(r)["pnl"] for r in admitted_a if simulate_one(r))
# ... 输出对比表
```

**3. 结论原则**：
- **改善 ≥ 5 U 且占原 PnL 10% 以上**才算真有效，< 这个阈值的"改善"很可能是回测噪声
- **震荡市占比高的样本**（如这次 5.5 天数据）里，T3 类"主动减仓"几乎一定反直觉——它假设"未来继续震荡"，但凡有几次单边趋势就亏回去
- **用户真实交易哲学优先**：右侧交易 + 让利润奔跑 = T3 默认关闭；只在**趋势市**（明确的 4h EMA144 斜率确认）才考虑启用
- **Task 2 的 1.5% 阈值**保留代码但本样本未验证——下次类似事件直接套，看是否真拦截

**4. 上线步骤**（必走，避免回测 PnL 改善但实盘更差）：
```bash
# 1. 加单测（先红后绿）
tests/test_config.py    → 加 should_skip_new_signal 时间窗口守卫测试（3 个）
tests/test_scan_filter.py → 加下跌反弹 penalty 函数测试（4 个）
tests/test_position_tracker.py → 加 PARTIAL_CLOSE 行为测试（3 个）

# 2. 跑全测试套
cd scripts && for f in tests/test_*.py; do python3 "$f" 2>&1 | tail -3; done
# 必须 82+ 个全过

# 3. 重启 watch（加载新代码）
python3 watch.py stop && sleep 2
python3 watch.py start --ratio 4 --z 3.5 --cooldown 1800 --rsi-tf 1h --target 1000 &

# 4. 看下一次 15min 周期卡片内容对不对
tail -f data/watch.log | grep -E "周期检查|周期性|periodic"

# 5. 验证准入守卫：手动跑 scan.py --brief --json 看准入理由是否出现
cd scripts && python3 scan.py BTC_USDT --brief --target 1000 2>&1 | grep -E "准入|⚠️|⛔"
```

**5. 存证**：把所有改动 + 回测结果 + 测试结果都写进 SKILL.md（就是这一节）。下次类似"我看到回测亏了，想加保护规则"的问题，可以直接参考这次的方法论——**先证伪再上代码**。

> 💡 **为什么不是直接相信头脑风暴**：交易直觉（"加个准入守卫应该不会错"）经常被回测证伪。**回测是 PnL 的裁判，不是直觉**。任何新规则上线前没有"在历史样本里跑过 + 净 PnL 改善 ≥ 5 U"这两条 → 默认关闭。
>
> 同样的 10-04 ~ 10-06 样本里，13 笔 PnL -60 U 是客观事实；任何保护规则净改善 < 5 U 都不能解释"为什么信它"。

## 七、文件清单

| 文件 | 作用 |
|---|---|
| `scripts/scan.py` | ⭐ **实时行情分析 + 多空双向交易计划生成器**（盘中主入口，支持 --json/--html/--brief） |
| `scripts/update_data.py` | ⭐ **K 线缓存更新器**：增量刷新 / 全量重拉 / 新鲜度检查（`--status`/`--force`/`--tfs`） |
| `scripts/gate_fetch.py` | Gate.io 数据获取（REST 增量+翻页 + WebSocket），含 `update_all`/`cache_freshness` |
| `scripts/indicators.py` | EMA / TD Sequential / RSI / ATR / **斐波那契（swing_pivots + fib_retr，无未来函数）** |
| `scripts/backtest.py` | 回测引擎 + `load()`（**每次自动增量刷新缓存**） |
| `scripts/daytrade.py` | 日内双向策略回测 + 爆仓建模 |
| `scripts/watch.py` | ⭐ **异动监控薄壳入口**（27 行，仅转发到 `watch.runner.main()`；CLI 兼容 `once/run/start/stop/status`） |
| `scripts/watch/` | watch 子包（实际逻辑）：`jump.py` / `volume.py` / `rsi.py` / `settler.py` / `runner.py` / `common.py` / `journal_ops.py` / `__init__.py`（兼容 shim） |
| `scripts/telegram.py` | ⭐ **Telegram 推送独立模块**（被 watch + position_tracker 共用：`load_token` / `load_chat` / `push` / `format_card`） |
| `scripts/config.py` | ⭐ **共享配置 + 准入规则**（`ENTRY_MIN_DIST_PTS` / `DEFAULT_TARGET_PTS` / `should_skip_new_signal()` 等） |
| `scripts/position_tracker.py` | ⭐ **持仓模拟器**：50U 名义 × 100x × 4.85U 手续费；3 段部分止盈 + BE 锁保留 + 状态机回放；`--settle`/`--report` |
| `scripts/journal.py` | ⭐ **信号日志 + 自动结算 + 分层胜率 + 保守优化**：`stats`/`list`/`recheck`/`tune`/`apply`（scan.py 每次自动调用） |
| `scripts/fib_test.py` | **斐波那契策略实证回测**：档位扫描 / 趋势过滤 / Fib×TD9 / 共振 / 置换检验 / 参数网格（`$PY scripts/fib_test.py 4h`，**每次自动更新数据**） |
| `scripts/tests/` | 单元测试：`test_config.py`（19，含 3 个 30min 准入守卫测试）+ `test_position_tracker.py`（27，含 3 个 PARTIAL_CLOSE 测试）+ `test_telegram.py`（21，含 7 个 override_active 卡片复用测试）+ `test_settler.py`（11）+ `test_force_close.py`（6）+ `test_scan_filter.py`（4，下跌反弹过滤测试）；合计 **88 个**；跑法 `python3 -m unittest discover tests` |
| `web/server.py` + `web/index.html` | ⭐ **状态面板**（3003 端口，单文件 HTTP 服务 + HTML）；1s 自动刷新；展示 watch 进程 / 策略 / K 线 / BTC 现价 / 胜率；详见 SKILL.md §6.18 |
| `references/gate-api.md` | Gate.io API 接口清单与**三个实测踩坑** |
| `references/risk-control.md` | 杠杆、保证金、爆仓公式与仓位管理 |
| `references/modularization.md` | ⭐ **watch.py 拆包 + telegram 抽出的设计决策**（什么时候拆、兼容层怎么写、踩过的坑；与 SKILL.md §6.10 互引） |
| `references/position_tracker-pitfalls.md` | ⭐ **`_settle_one` 状态机的 5 个真实陷阱**（TP1/TP2 持久化不对称、tp2_hit 重置、24h TIMEOUT 边界、同根双触防自欺、ticker 时间戳）；与 SKILL.md §6.12 互引 |
| `references/active-positions-query.md` | ⭐ **查询"当前活跃策略"的对账流程**——`status` 是 2D 状态机的一维，TP1_PARTIAL 锁利后 `status=settled` 但还有 2/3 部位；用户问"目前策略"时按这个流程答（避免把锁利当成全平、避免漏报挂单） |

> 💡 **怎么知道该拆包**：单文件 > 800 行 + 多个独立关注点（WS / RSI / 跳空 / 持仓结算 / CLI）+ 跨文件互相 import。
> 拆完一定保留薄壳 + `__init__.py` 兼容 shim，否则外部脚本（systemd / cron）会断。

⚠️ 所有产出均基于历史统计，**不构成投资建议**。合约高杠杆可致本金全损。
