# v2_journal contracts=0 bug 复现 & 修法

**发现时间**：2026-10-15
**影响范围**：训练 daemon 写的 `/root/data/training/v2_journal.jsonl`（8 单）

## 症状

训练 v2 单 `net_usd` 全部 `-4.85U`（同一个值）。
看似每单都"亏手续费"，其实**根本不是真实 PnL**。

排查路径：`grep ... v2_journal.jsonl` → 看 `contracts` 字段 → 全 0。

## 两个 bug 叠在一起

### Bug 1：算出来的 contracts 没写进 sig dict

`scripts/v2_journal.py` line 207-211：

```python
notional = bal * lev          # = 50 * 100 = 5000
contracts = int(notional * QUANT_MULT)   # = int(5000 * 0.0001) = int(0.5) = 0
```

然后 `sig = { "id": ..., "ts": ..., "side": ..., ... }` —— **没塞 `contracts` 字段**。

但 settler 在 297-303 行 fallback 重算：

```python
contracts = sig.get("contracts") or int(V2_SLTB_RULES["bal"] * V2_SLTB_RULES["lev"] * QUANT_MULT)
sig["contracts"] = contracts  # 写回
```

→ fallback 用错的公式重算，结果还是 0。所以**根本没救**。

### Bug 2：公式单位错配

```python
contracts = int(notional * QUANT_MULT)
        # = 5000 USD * 0.0001 (BTC/张)   ← 美元 × BTC  = 完全错的单位
        # = 0.5 张（floored to 0）
```

正确公式（跟生产 `position_tracker.py:_contracts()` 同源）：

```python
contracts = int(round(notional / (entry * QUANT_MULT)))
        # = 5000 / (83300 * 0.0001)
        # = 5000 / 8.33 = ~600 张
```

`entry * QUANT_MULT` 才是"一张合约代表的名义 USD"。

## 修法（已落地）

`scripts/v2_journal.py` 改三处：

1. `V2_SLTB_RULES` 加 `min_contracts=1`（防止 entry 极端高估时 int 抹零）
2. 公式 `contracts = max(1, round(notional / (entry * QUANT_MULT)))`
3. sig dict 加 `"contracts": contracts` 和 `"notional_usd": notional`

**新数据示例**（entry=83300）：

- `notional=5000U`、`contracts=600张`、`1% 变动 PnL=50U`（= 5000 × 1%）

**回填 8 单历史数据**：

- 不回填 — 样本太小（8 单），回填 commit 历史会污染"v2.1 后台攒数据 + 2 周后做对照"的对照
- 历史 `net_usd` 字段标记"已知偏差"即可

## 怎么测

```python
# 在 scripts/v2_journal.py 顶部临时加
from v2_journal import V2_SLTB_RULES, QUANT_MULT
entry = 83300
contracts = max(1, round(V2_SLTB_RULES['bal'] * V2_SLTB_RULES['lev'] / (entry * QUANT_MULT)))
# 期望 contracts=600
```

## 教训

1. **公式对照**：写新开仓函数时永远 pull `position_tracker._contracts()` 的写法，因为它是生产同源
2. **settler fallback 不是补救**：fallback 的"无则填某默认值"会掩盖字段缺失的 bug。
   一旦发现 net_usd 全同值，先看 contracts 字段，不要先怀疑 fee
3. **整数抹零防御**：所有 contracts 公式后跟 `max(1, ...)`，防止小数 < 1 时被 int 抹零变成 0 张
