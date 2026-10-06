# 查询"当前活跃策略"的对账流程

> 这套流程是踩了两次才写下来的——用户问"目前生效的策略有哪些"时，
> 只看 `status` 字段会误报；真正含义在 `pos_state` + `filled` + `partial_exit_count` 的组合里。
> 未来再被问类似问题，先跑这个流程。

## 一、为什么不能信 `status` 字段一个

`status` 取值看似清晰：

| status | 含义 |
|---|---|
| `pending` | 还没成交的挂单 |
| `filled` | 已成交未结算 |
| `settled` | 已结算（含部分止盈 TP1）|
| `invalidated` | 被作废 |

但实际状态是**正交的二维状态机**：

- `status='settled'` + `pos_state='TP1_PARTIAL'` + `tp1_hit=True` → **TP1 已触，剩 2/3 部位还在等 TP2**
- `status='pending'` + `filled=True` → 已成交但还没触发任何 TP/SL
- `status='pending'` + `filled=False` → 挂单中（entry 没到）

误把第二种说成"挂着还没进"，或漏掉第一种已经锁利的情况，
都会让用户对实际敞口失去正确判断。

## 二、对账清单（每次被问"现在策略状态"时跑）

按这个顺序查，别跳：

```python
import json
with open('/root/data/journal.jsonl') as f:
    recs = [json.loads(ln.strip()) for ln in f if ln.strip()]

# 1. 去重：同一个 id 可能被 scan / jump 重复写入，只看最后一条
seen = {}
for r in recs:
    seen[r['id']] = r          # 后写覆盖前写
recs = list(seen.values())

# 2. 分类：实际活跃 = pending + filled + settled 但 pos_state=TP1_PARTIAL
active = [
    r for r in recs
    if r.get('status') in ('pending', 'filled')
    or (r.get('status') == 'settled' and r.get('pos_state') == 'TP1_PARTIAL')
]

# 3. 对每条标出"实际部位 + 阶段"
for r in active:
    filled = r.get('filled', False)
    pos_state = r.get('pos_state', '?')
    tp1_hit = r.get('tp1_hit', False)
    contracts = r.get('contracts', 591)

    if not filled:
        stage = '挂单未成交'
        effective_qty = 0      # 还没仓位
    elif tp1_hit:
        stage = f'TP1 已锁利，剩 {contracts * 2 // 3} 张'
        effective_qty = contracts * 2 // 3
    else:
        stage = f'已成交，{contracts} 张等 TP1'
        effective_qty = contracts
```

**关键提醒**：
- `status='settled'` **不意味着全平**——可能只是 TP1 锁利，剩余 2/3 仍在跟踪
- `tp1_hit=True` 才是"已部分止盈"的唯一权威标志
- `active_sl` 才是当前止损位（TP1 触发后会移到 BE，不再是原 `sl`）

## 三、典型对账输出

实际跑一遍会出现这样的"看起来矛盾但完全正常"的组合：

```
id=BTC_USDT-1791060667-long
  status=settled          ← 看着像"已了结"
  pos_state=TP1_PARTIAL   ← 实际还有 2/3 部位
  tp1_hit=True            ← TP1 已锁利
  active_sl=84638.2       ← SL 已移到 BE
```

→ **应该告诉用户**：TP1 已触发锁利（+X U），剩 2/3（~394 张）继续等 TP2，止损锁本到 BE。

## 四、回答用户时的口径

| 用户问 | 应该答 |
|---|---|
| "目前生效的策略有哪些" | 列出 `active` 全部；每条说清楚**阶段**（挂单 / 已成交 / TP1_PARTIAL）+ **剩余部位** |
| "还有单子吗" | 同上，但特别强调 `filled=False` 的挂单（容易漏报） |
| "已经 TP2 了吧" | **先查 `pos_state` 和 `tp1_hit`，不要凭记忆答** |
| "新策略怎么又来了" | 可能是同一 id 的多次写入（scan + jump 各自写一条），去重后再答 |

## 五、踩过的坑（2026-10-04 实测）

- ❌ 凭记忆说"84,638.2 还挂着，filled=True"——实际 TP1 已在 14:22 触发
- ❌ 把 `status='settled'` 等同于"全平"——实际只是 TP1 锁利
- ❌ 没去重直接读 journal 重复 id——把多条同 id 记录当成不同策略报

## 六、自动化建议

未来可以加个 `scripts/list_active.py`：

```bash
$PY scripts/list_active.py
# 输出：每条 active 的 id / 阶段 / entry / 当前价距离各档位 / 累计浮盈
```

避免每次手动跑上面那段 Python。当前没写，下次有需要时再加。
