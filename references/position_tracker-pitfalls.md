# position_tracker.py 陷阱集（2026-10-04 真实事件）

> `scripts/position_tracker.py` 是持仓模拟器（Settler 巡检依赖它）。
> 这份文档记录**真实生产事件**里踩到的 5 个陷阱，避免未来重复犯。

## 一、TP1/TP2 持久化不对称（2026-10-04 真实事件）

**症状**：用户说"已经 TP2 了"，但 journal 里 `outcome=TP1, pos_state=TP1_PARTIAL`，Settler 没主动推 TG。

**根因**：`position_tracker._settle_one` 里两个状态变更路径不一致：

| 触发点 | 显式持久化？ | 依赖 settle_all 写回？ |
|---|---|---|
| TP1 触发 (line 198-224) | ✓ 立即写 journal | 仍依赖 |
| TP2 触发 (line 225-231) | ✗ 没有 | 依赖 |
| BE_STOPPED (line 195) | ✗ 没有 | 依赖 |
| SL_STOPPED (line 195) | ✗ 没有 | 依赖 |
| TIMEOUT (line 235) | ✗ 没有 | 依赖 |

**问题**：settle_all 在 line 392 `if n_done: _write_journal(recs)` 才写回。如果中间：
- `n_done = 0`（TP1 触发了，但 mutate sig 没 return）→ **TP1 写回，TP2 不写**
- Settler 进程被 kill → **所有 in-flight 状态变更丢失**

**修复**：每个状态变更点都必须镜像 TP1 的"立即持久化"模式：

```python
# TP2 触发分支（line 225-231）应该在 return 之前加：
if t_tp2 and cur_tp1_hit and not cur_tp2_hit:
    sig["tp2_hit"] = True
    sig["tp2_hit_ts"] = ts
    sig["tp2_hit_px"] = tp2
    sig["last_check_ts"] = ts
    # ★ 镜像 TP1：立即持久化
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

**经验法则**：任何"先 mutate sig + 再 return res 让 settle_all 写回"的模式都是脆弱的——**关键状态变更必须立即持久化**。

## 二、tp2_hit 重置的隐藏 bug（debug 时容易踩）

**症状**：手动重置 sig 跑 `_settle_one` 测 TP2 路径时，`cur_tp2_hit = True` 在函数启动时就 True——直接走 TP3 分支。

**根因**：`cur_tp2_hit = bool(sig.get("tp2_hit"))` 在函数 line 124 从 sig 读。如果 sig 残留了 `tp2_hit=True`，下一行扫描就开始就跳。

**调试时容易踩**——之前某次跑过 _settle_one，sig 被 in-place mutate（line 226-228 写 tp2_hit 到 sig），但**sig 是 dict() 拷贝，line 382 `r.update(res)` 只更新 recs 里的 r**，sig 不会被自动清理。

**调试清单**（重置 sig 时必须清零的字段）：
```python
sig['status'] = 'pending'
sig['tp1_hit'] = False
sig['tp2_hit'] = False
for k in ['tp1_hit_ts', 'tp1_hit_px', 'tp2_hit_ts', 'tp2_hit_px',
          'exit_ts', 'exit_px', 'outcome', 'net_usd', 'gross_usd',
          'note', 'hold_h', 'last', 'mae', 'mfe', 'partial_exits',
          'R', 'net_R']:
    if k in sig: del sig[k]
sig['pos_state'] = 'OPEN'
sig['active_sl'] = sig['sl']
sig['last_check_ts'] = 0
if 'partial_exit_count' in sig: del sig['partial_exit_count']
```

## 三、24h TIMEOUT 边界（MAX_HOLD = 24 * 3600）

**位置**：line 234 `if sig.get("filled") and ts - sig["fill_ts"] > MAX_HOLD`

**边界**：
- `MAX_HOLD = 24 * 3600 = 86400 秒 = 24h`
- 触发条件：`ts - fill_ts > 86400`（严格大于）
- 含义：fill_ts 是成交时刻，**24h 整点之后**第一根 K 线就 TIMEOUT

**风险**：日内策略持仓超过 24h 会被强制平仓（按 K 线 close 价）。
- 实际 84,638.2 那条持仓 9.2h 后 TP2_DONE（远短于 24h）
- 但如果 TP2 没触发，挂单状态会一直 hold 到 24h

**调整建议**：如果做多日持仓，把 `MAX_HOLD` 调到更长。

## 四、同根双触防自欺（line 187-192）

**逻辑**：
- `if t_sl and t_tp1 and not cur_tp1_hit`: SL/TP1 同根 → 走 "SL"（保守判，未触发 TP1 时不锁利润）
- `if cur_tp1_hit and not cur_tp2_hit and t_sl and t_tp2`: BE/TP2 同根 → 走 "TP2_DONE"（已锁利润，激进判）
- `if cur_tp2_hit and t_sl and tp2 > 0`: 这是 TP3 逻辑（用 tp2 作为 exit_px）—— **tp2_hit 时 SL 触发就当 TP3 平**

**坑**：line 191 的 "TP3" 实际上是用 tp2 作为 exit_px——**TP3 不存在时这个分支永远不进**。

## 五、`last_check_ts` 卡死的 4 种情况

如果 `_settle_one` 没 return，循环结束 return None——journal 里 `last_check_ts` 停在最后一根检查的 K 线：

1. **TP1 触发后第一根 K 线触 BE**：settle_one 走 `if t_sl: return _close(...)`（line 195），OK 正常退出
2. **TP1 触发后第一根 K 线触 TP2**：走 `if t_tp2 ... return _close(...)`（line 231），OK
3. **TP1 触发后第一根 K 线什么都没触**：`sig["last_check_ts"] = ts`（line 237）+ continue——**OK，会写到 journal**（因为 line 209-219 显式持久化）
4. **TP1 触发但 K 线碰巧同时触 SL/TP1**（防自欺）：走 `if t_sl and t_tp1 and not cur_tp1_hit: return "SL"`——**但 sig 上的 tp1_hit=True 已经被 line 200 设了**，且 line 209-219 已经写 journal

**关键问题**：case 4 里 sig 的 tp1_hit=True 写回 journal，但 outcome="SL"——**下次扫描时 cur_tp1_hit=True，会直接走 line 191 TP3 分支**（如果 SL 又触发）。

**修复方向**：case 4 应该在 return 之前撤销 tp1_hit 设置，或者把 case 4 的判定提到 TP1 触发判定之前。

## 六、调试时常用命令

```bash
# 看 journal 当前所有 active
python3 -c "
import json
for r in [json.loads(l) for l in open('/root/data/journal.jsonl') if l.strip()]:
    if r.get('status') in ('pending', 'filled'):
        print(f'{r[\"id\"]} status={r[\"status\"]} entry={r[\"entry\"]} filled={r.get(\"filled\")} tp1_hit={r.get(\"tp1_hit\")} pos_state={r.get(\"pos_state\")}')
"

# 手动跑 settle_all（不通过 watcher）
python3 -c "
import sys; sys.path.insert(0, '/root/skills/btc-gate-intraday-strategy/scripts')
from position_tracker import settle_all
print(settle_all(verbose=True))
"

# 手动跑 _settle_one（带 reset）
python3 -c "
import sys, json; sys.path.insert(0, '/root/skills/btc-gate-intraday-strategy/scripts')
from position_tracker import _settle_one, fetch_ohlcv

with open('/root/data/journal.jsonl') as f:
    recs = [json.loads(l) for l in f if l.strip()]

sig = None
for r in recs:
    if r.get('id') == 'YOUR_ID':
        sig = dict(r)
        break

# 完整重置
sig['status'] = 'pending'
sig['tp1_hit'] = False
sig['tp2_hit'] = False
for k in ['tp1_hit_ts', 'tp1_hit_px', 'tp2_hit_ts', 'tp2_hit_px',
          'exit_ts', 'exit_px', 'outcome', 'net_usd', 'gross_usd',
          'note', 'hold_h', 'last', 'mae', 'mfe', 'partial_exits']:
    if k in sig: del sig[k]
sig['pos_state'] = 'OPEN'
sig['active_sl'] = sig['sl']
sig['last_check_ts'] = 0

bars = fetch_ohlcv('BTC_USDT', '1m', 8000, cache=False)
res = _settle_one(sig, bars)
print(res if res else 'still active')
"
```

## 七、5m/15m EMA 突破预警 + 70% 部分平仓（2026-10-05 liusir 策略）

**新增函数**：`position_tracker._check_ema_break_warning(sig, ts, side, entry, cur_px, fill_px)`。

**触发条件（全部满足才触发）**：

1. 已成交：`sig["filled"] is True`
2. 当前盈利：`side=="long" and cur_px > fill_px`（或 short 反之）
3. **5m** 收盘价双跌破 EMA144 + EMA169（同方向做空跌破；做多涨破）
4. **15m** 收盘价双跌破 EMA144 + EMA169

**触发动作**：

- 平 70% 仓位（按 `EMA_WARN_PARTIAL = 0.7`）
- SL 移到 BE（`sig["active_sl"] = entry`）—— **不扣手续费**（4.85U 等全平时一次性扣）
- 写 `position_events.jsonl` kind=`ema_break_partial_close`
- 推 TG 卡片「🟡 BTC 趋势反转预警 · 部分止盈 70%」
- 设 `sig["ema_warning_done"] = True` —— **同笔持仓只触发一次**

**接入位置**：`position_tracker._settle_one()` 主循环内、SL/TP 判定**之前**（每 5s settler 巡检 + 每根 1m K 线都过一遍）。

**关键坑**：

- 部分平仓时**不扣手续费**——保持"出场一次性扣 4.85U"的现有规则
- SL 移到 BE = 开仓价，**不上移到 TP1**——跟现有 TP1 路径一致
- `fetch_ohlcv(..., cache=True)` 第一次会联网拉 5m/15m 数据；网络失败时静默 return False（不阻塞 settle）
- 5m K 线文件**必须预先存在**——首次调用会写 `data/BTC_USDT_5m_500.json`

**测试**：`tests/test_position_tracker.py` 新增 `TestEmaBreakWarning` 类，5 个测试覆盖：盈利触发 / 不盈利不触发 / 只 5m 跌破不触发 / 做空镜像 / 只触发一次（`ema_warning_done` 幂等）。

## 八、相关章节

- SKILL.md §6.21 — `filled=True` 而非 `status=filled`（同语义陷阱多次出现，**2026-10-05 第 4 次踩到 config.py:97**）
- SKILL.md §6.13 — patch tool HTML entity 反转坑
