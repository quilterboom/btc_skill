# BTC 合约日内策略（Gate.io · USDT 本位永续）

> **适合谁读**：懂 BTC 永续合约、想用代码化方法跑日内短线的人。
> **前置知识**：略懂 K 线（5m/1h/4h/1d）、杠杆/保证金、止盈止损。
> **10 分钟能上手**：看完 §1 + §3，照 §6 的命令跑一次 scan.py 即可。

---

## 1. 一页看懂策略（最关键）

**一句话定位**：Gate.io USDT 本位永续 BTC，**日内短线双向**，靠多周期联动（5m/1h/4h/1d）+ 量能异动 + RSI 通道 + 环境过滤筛选信号，**右侧突破追**为主。

### 1.1 什么时候**做**

四道闸**全部通过**才进场（任意一条不满足 → 跳过本次）：

| 闸 | 通过条件 | 文件 |
|---|---|---|
| ① 量能异动 | 当前 1m 成交量 ≥ 历史 120 根中位 × 4 倍，且 z-score ≥ 3.5 | `watch.py` |
| ② RSI 通道 | 1h RSI14 越界（<30 或 >70），穿越后冷却 1h | `watch.py` |
| ③ 环境过滤 | 1d EMA144 斜率分位在 P20~P80 之间（不强多不强空） | `scan.py` |
| ④ 价格结构 | 5m/1h/4h 三周期打分 ≥ 4 分（EMA/Fib/TD9/支撑）| `scan.py` |

### 1.2 什么时候**绝对不做**

- **强多头末期**（1d EMA144 > P80）→ 强多不做多（回测 EV -1.88%/笔）
- **强空头末期**（1d EMA144 < P20）→ 强空不做空（回测 EV -0.78%/笔）
- **横盘震荡**（4h ATR 比 1d ATR < 0.3）→ 假突破多，跳过
- **同方向已有持仓距离 < 500 点** → 不重复开

### 1.3 风险红线（先看这条）

| 项 | 当前默认值 | 含义 |
|---|---|---|
| 杠杆 | 100x | 工具，不是开仓许可 |
| 保证金 | 50 USDT | 固定 |
| 名义敞口 | 50 × 100 = 5000 USDT | **绝对上限** |
| 单笔止损 | ATR 1.5×（约 -0.4%）| 自适应 |
| 强平距离 | 100x 全仓 = -0.70% | **止损必须 < 0.70%**，否则先爆仓 |

> 50U 账户 + 100x 满仓 ≈ 名义 5000U，4 成单会死于正常噪音而非判断错（基于 27 笔实测 MAE）。
> 新手建议：**先跑 1 周回测 + 1 周小额实盘**，再考虑加仓。

### 1.4 一笔交易的生命周期

```
① watch.py 实时监控 K 线 + 量能 + RSI
   └─ 满足条件 ① 或 ② → 触发 scan
② scan.py 跑一次（5 秒内）
   ├─ 拉 5m/1h/4h/1d K 线
   ├─ 打分（含环境过滤）
   └─ 通过 → 推送 TG 卡片：方向、点位、仓位
③ 手动/自动下单（默认左侧挂单 post-only；可选右侧突破追）
④ journal 记录 + settler 守护
   ├─ T1 +400 点触发 → 平 1/3，SL 移到开仓价（保本）
   ├─ T2 +600 点触发 → 再平 1/3
   ├─ T3 +1000 点触发 → 全平
   └─ SL 触发 → 全平
⑤ 24h 未触发任何点位 → 强制平仓
```

### 1.5 R:R 与胜率（最关键数字）

| 模式 | R:R | 保本胜率 | 实测胜率 |
|---|---|---|---|
| 默认（T3 1000 点 vs SL 0.4%）| 1:2.91 | 26% | ~17%（10-02~10-06）|
| 之前老版本（T3 1000 vs SL 0.8%）| 1:1.45 | 41% | ~17% |

> **当前配置正 R:R**，但胜率仍低于保本线 → 净亏。原因：胜率不靠参数提，靠"避开烂环境"提。
> 环境过滤（闸③）拦掉了 P98 那种 -1.88% EV 的烂局，**剩下的应该接近盈亏平衡**。

---

## 2. 策略规则详解

### 2.1 分层原则（最容易搞错的一点）

> **大周期定方向，小周期定入场**。方向错了，4h/1d 信号再强也不能做。

| 层级 | 时间框架 | 作用 |
|---|---|---|
| 战略 | 1d | 判定"现在是多头市场 / 空头市场 / 震荡" → 环境过滤 |
| 战术 | 4h | EMA 多空排列 + Fib 回调位 + 关键阻力支撑 |
| 主信号 | 1h | RSI 通道 + TD9 计数 |
| 执行 | 5m/15m | 量能异动 + EMA 排列确认 + 入场点位 |

### 2.2 入场（做多；做空完全镜像）

**打分项**（每满足一条 +1，满分 7）：

- 5m EMA144 > EMA169（多头排列）
- 1h EMA144 > EMA169
- 4h EMA144 > EMA169
- 现价附近 0.5% 内有 Fib 0.382 支撑位
- 现价附近 0.5% 内有 1h EMA34 支撑
- 5m 或 1h 出现 TD9 + 2 根确认
- 1h 缩量后放量确认

**减分项**：
- 1h 跌幅 ≥ 1.5% 做多 → -1（下跌中段反弹）
- 1d 强多头做多 → -2（环境过滤）
- 1d 强空头做空 → -2

**入场方式**：

| 方向 | 触发 | 风险 |
|---|---|---|
| **左侧（默认）**| 回踩 EMA34 / Fib 0.382 时 post-only 挂单 | 不触发就错过 |
| **右侧（可选）**| 突破前高/前低立即追 | 假突破被套 |

### 2.3 出场（分 3 段部分止盈）

```
T1: +400 点 → 平 1/3，SL 移到开仓价（保本）
T2: +600 点 → 再平 1/3
T3: +1000 点 → 平剩余 1/3
止损: -344 点（约 -0.40%，ATR 自适应）
```

**关键**：T1 触发 ≠ 全平。是平 1/3 后**移动 SL 到 entry**——后续回到 entry 时 BE_STOPPED（保本止损，扣手续费约净亏 1U）。

### 2.4 RSI 的定位：**排雷器，不是信号源**

RSI 越界本身**不是开仓理由**，它的作用是：
- **拦掉抄顶摸底**：RSI 已经 <30 别再追空，已经 >70 别再追多
- **强制冷却**：触发后 1h 内不再触发同方向
- **辅助过滤**：环境过滤 + RSI = 双重把关

### 2.5 环境过滤（反直觉，2026-10-06 加）

> 实盘 17 笔（10-02~10-06）亏 -21.23U 的**主因**。
> 回测验证：P80 以上强多头做多，胜率 14.3%、EV -1.88%。

**逻辑**：
- 1d EMA144 斜率 ≈ 60 天趋势强度
- P80 = 趋势最猛的时候 → 容易"涨到顶了"
- P20 = 跌得最惨的时候 → 容易"跌到底了"
- **P20~P80 之间的"中庸趋势"才是顺势赚钱的环境**

**代码**：`scan.py` 内 `compute_env_regime_penalty()`，打分块接入。

---

## 3. 工作流

### 3.1 盘中：行情分析 + 交易点位

```bash
cd /root/skills/btc-gate-intraday-strategy/scripts
python3 scan.py --brief --target 1000
```

**输出关键字段**：
- 现价 + 时间
- 多周期 EMA 排列状态
- 量能 / 情绪上下文
- Fib 支撑阻力位
- **判定**：信号成立 / 无信号 · 观望 + 理由
- 入场（左侧挂单价）+ 突破追（右侧价）+ 止损 + TP1/TP2/T3
- 仓位（张数 / 名义）+ R:R + 保本胜率

### 3.2 服务器常驻：异动监控（watch.py）

后台跑 `watch.py run`（不要用 `start`，会脱离 systemd 跟踪）：
- WS 订阅 BTC_USDT 1m K 线
- 量能异动监控（cooldown 1800s = 30 分钟）
- RSI 通道监控（cooldown 3600s = 1 小时）
- 触发 → 跑 scan → 推送 TG 卡片

**systemd 服务**（详见 §7）：
- `btc-watch.service` — 主守护
- `btc-status-web.service` — 3003 端口状态面板

### 3.3 改代码后必跑

```bash
# 期望输出：Ran 95 tests → OK
python3 -m unittest discover -s tests
```

### 3.4 盘后 / 策略迭代：回测

```bash
python3 backtest.py
```

每次回测会**自动记账**到 journal，下次扫描时显示近 12 笔胜率 + EV。

---

## 4. 成本（决定胜率门槛）

| 项 | 数值 | 计算 |
|---|---|---|
| 名义 | 5000 USDT | 50U 保证金 × 100x |
| 单边手续费 | taker 0.05% / maker -0.01% 返佣 | 名义 5000U → 2.5U / -0.5U |
| 双边往返 | taker 5U / maker -1U | TP1 = +5.84U → 净 +0.84U / +6.84U |
| **post-only 挂单必走 maker** | -1U/笔 | 远超 1U 时才有利 |

> TP1 触发 → 移 BE → 被打回 → **净 -1U**（白触发，扣手续费）
> 这是 5 笔 -0.85U 的根因。**TP1 必须 ≥ 800 点**（名义到手 ≥ 8U）才能覆盖 -1U + 滑点。

---

## 5. 已知局限（必须告知用户）

1. **R:R 正但胜率不够**：当前配置 R:R 1:2.91（正），但实测胜率 17% < 保本线 26% → 净亏。改胜率靠**避开烂环境**，不靠参数。
2. **没有日亏损上限**：连亏多少笔都还能开，需要人工设停。
3. **没有最大并发仓位数**：理论可同时挂多笔，建议人工 ≤ 1。
4. **24h 强制平**：超时不管盈亏都平，可能错过大波段。
5. **依赖网络**：WS 断了不会自动重连（systemd 会拉起但需要 ~5s 冷启动）。
6. **小数据样本**：27 笔实盘不足以做统计推断，回测数据是 4h+1d 上千笔。

---

## 6. 快速上手

### 6.1 安装

```bash
# 依赖
pip install pandas numpy requests websocket-client

# 配置 Gate.io API key
export GATEIO_API_KEY="..."
export GATEIO_API_SECRET="..."

# 配置 TG 推送（可选）
# /root/.hermes/secrets/btc_tg.json 存 bot token + chat id
```

### 6.2 跑一次扫描

```bash
cd /root/skills/btc-gate-intraday-strategy/scripts
python3 scan.py --brief --target 1000
```

### 6.3 启动监控

```bash
systemctl --user start btc-watch.service
systemctl --user start btc-status-web.service
```

### 6.4 验证

```bash
# 期望输出：process.alive=true, process.pid=XXXX
curl -s http://localhost:3003/api/status | python3 -m json.tool
```

---

## 7. 运维要点（出问题看这节）

### 7.1 systemd 服务的三个坑（真实事件）

| 坑 | 症状 | 解决 |
|---|---|---|
| `watch.py start` 双 fork | systemd 1 秒就 inactive(dead)，启动失败 | **改用 `run`**（不 daemonize）|
| `/usr/bin/env python3` 缺模块 | `ModuleNotFoundError: No module named 'websocket'` | **写死 venv 路径**：`/root/.hermes/.../venv/bin/python3` |
| `watch.pid` 残留 | 重启时"已在运行" | `rm -f /root/data/watch.pid` |

**完整 service 文件**：`web/server.py:_check_process()` 加 `pgrep` fallback（subprocess import 别忘），systemd 用 `WatchdogSec=60s` 自动看门。

### 7.2 每次重启必跑的验证

```bash
# 1. watch 真活着？
journalctl --user -u btc-watch.service --since "-1m" | grep -E "心跳|settler"

# 2. WS 收到推送？
journalctl --user -u btc-watch.service --since "-1m" | grep "已收到第一条"

# 3. settler 心跳新鲜？
journalctl --user -u btc-watch.service --since "-2m" | grep "settler"

# 4. web status 面板？
curl -s http://localhost:3003/api/status | python3 -m json.tool
```

### 7.3 改代码 → 重启服务

```bash
# 改完任何 .py 文件后：
systemctl --user restart btc-watch.service
systemctl --user restart btc-status-web.service

# Python 标准库 HTTP server 不支持 hot reload，必须 restart
```

---

## 8. 文件清单

| 文件 | 作用 |
|---|---|
| `scripts/scan.py` | 一次性扫描（手动跑）|
| `scripts/watch.py` | 常驻守护（systemd）|
| `scripts/position_tracker.py` | 持仓跟踪（TP/SL 判定）|
| `scripts/journal.py` | 交易记录（append-only）|
| `scripts/telegram.py` | TG 卡片推送 |
| `scripts/backtest.py` | 策略回测 |
| `scripts/tests/` | 单元测试（95 个）|
| `web/server.py` | HTTP API（端口 3003）|
| `web/index.html` | 状态面板前端 |
| `references/` | 数据源参考（API、字段名）|
| `SKILL.md` | 本文档 |
| `.gitignore` | 排除缓存/.env/日志 |

---

## 9. 历史决策日志（想知道为什么这样改，看这里）

详细历史 bug 修复记录见 `SKILL-history-2026-10.md`（单独文件，避免污染主文档）。
本节只列**关键决策**和**回测结论**：

| 日期 | 决策 | 依据 |
|---|---|---|
| 2026-10-06 | 加环境过滤（P80 强多砍 2 分）| 回测 17 笔 -21.23U，10 笔死在 P98 环境 |
| 2026-10-06 | SL 0.8% → 0.4%（ATR 自适应）| 1.5×ATR = 0.4~0.5%，0.8% 下限形同虚设 |
| 2026-10-06 | FEE 写死 -1U（maker 返佣）| 名义 5000U × 0.01% × 2 |
| 2026-10-05 | T1（30 分钟同方向拦截）| 防重复开仓 |
| 2026-10-04 | Settler 心跳 + 线程守护 | 之前静默死亡 |
| 2026-10-04 | 加 500 点最小间距 | 防止同向堆叠 |
| 2026-10-02 | RSI 改定位为"排雷器" | RSI 单独用 EV 为负 |

---

> **不要凭印象改参数**。所有阈值要么来自回测（`backtest.py`），要么来自实盘 ≥ 30 笔统计。
> 当前数据样本小（27 笔实盘），新参数建议先在回测上验证 ≥ 1000 笔再看收益分布。
