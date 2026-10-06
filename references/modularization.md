# 拆包与模块化决策（modularization）

> 适用对象：BTC skill 的 `scripts/` 目录。
> 适用时机：单文件 > 800 行 + 多个独立关注点（WS / 持仓 / 跳空 / CLI）共存。

## 一、为什么拆

**症状**：
- `watch.py` 一度 1132 行，4 个独立模块（WS 量能 / RSI / JumpTracker / VolumeWatcher）+ CLI
- `position_tracker.py` 循环引用 `watch.py` 的 TG 函数（`_load_tg_token` / `push_telegram`）
- 修一个 bug 要在 800+ 行里找位，单元测试覆盖几乎为零

**触发拆分的两个铁律**：
1. **文件 > 800 行**：单文件已经很难阅读（哪怕你写得好，3 个月后回看不认识）
2. **多个独立关注点**：WS 订阅、信号判定、跳空监测、持仓结算、CLI——它们各自演化、改动半径互不重叠

**什么时候不要拆**：
- < 400 行的工具脚本（拆完反而增加跨文件跳转）
- 单一关注点的内部实现（拆开会模糊边界）

## 二、实际拆分路径

### 第一刀：抽 telegram 模块（最先做）
**为什么**：`telegram` 是**最被共用的工具**（watch + position_tracker + scan 都有推送），先把它独立出来能立刻打破循环依赖。

**做法**：
```python
# scripts/telegram.py
def load_token() -> Optional[str]: ...
def load_chat(default: str = ...) -> str: ...
def push(card: str, token: Optional[str] = None, chat_id: Optional[str] = None) -> bool: ...
def format_card(kind: str, alert: Dict, scan_payload: Optional[Dict]) -> str: ...
```

**收益**：
- position_tracker.py 不再需要 `from watch import ...`
- watch / scan / 未来的 cron 脚本都能直接 `from telegram import push`
- 单元测试能 mock `telegram.push` 而不触碰 watch.py

### 第二刀：抽共享常量到 config.py
**为什么**：scan / watch 都用 `500 点同方向距离` 这个 magic number，但写在不同地方。改一次要同步 3 处。

**做法**：
```python
# scripts/config.py
ENTRY_MIN_DIST_PTS = 500          # 同方向入场点最小距离
DEFAULT_TARGET_PTS = 1000         # 默认目标点数
SETTLER_INTERVAL_SEC = 5          # Settler 巡检间隔
JUMP_WINDOW_SEC = 60              # 跳空监测窗口

def should_skip_new_signal(contract, side, new_entry, *, min_dist_pts=ENTRY_MIN_DIST_PTS, journal_path=...) -> tuple[bool, str]:
    """同方向已有 pending/filled 且距离 < min_dist_pts → 跳过"""
    ...
```

**收益**：
- 一处定义，三处使用（scan / watch / journal 准入规则）
- 单元测试覆盖 `should_skip_new_signal` 的所有边界（距离 0 / 500 / 501 / 方向反转 / 已成交 / 损坏 JSON）

### 第三刀：watch.py 拆成包
**触发**：`watch.py` 1132 行 + 4 个独立关注点（量能 / RSI / 跳空 / 结算）+ 跨文件互相 import。

**结构**：
```
scripts/
  watch.py                # 27 行薄壳，转发到 runner.main()
  watch/                  # 子包
    __init__.py           # 包入口 + 兼容 shim
    common.py             # log + 路径常量（被所有子模块用）
    journal_ops.py        # journal 文件操作（被 jump / settler 共用）
    jump.py               # JumpTracker
    volume.py             # VolumeWatcher
    rsi.py                # RsiWatcher
    settler.py            # 持仓结算线程
    runner.py             # emit_scan + fire + run + daemonize + CLI
```

**关键设计**：

#### 1. **薄壳 + 兼容 shim 是必须的**
```python
# scripts/watch.py（薄壳）
from watch.runner import main
if __name__ == "__main__":
    sys.exit(main())
```
```python
# scripts/watch/__init__.py（兼容 shim）
from telegram import load_token as _load_tg_token  # 老代码 `from watch import _load_tg_token`
from .volume import VolumeWatcher                   # 老代码 `from watch import VolumeWatcher`
```
**为什么**：外部脚本（systemd / cron / 文档）写死了 `python3 watch.py start`，改包名会全断。薄壳保持 CLI 兼容。

#### 2. **共享代码放 common.py，不要在每个子模块重定义**
- `log()` 函数（统一日志格式）
- `HERE` / `SCAN` / `LOG` / `PID` / `ALERT_DIR` 路径常量
- `OK` / `NO` / `WARN` UI 符号

**为什么**：避免循环 import 子模块互相依赖 common 时各自 import。

#### 3. **跨模块共用的工具单独成文件**
- `journal_ops.py`：`invalidate_other_pending()` 被 `jump` 和 `settler` 共用
- `settler.py`：单独成文件，因为它是独立线程，且能被单元测试 mock

#### 4. **延迟导入避免循环依赖**
```python
# watch/settler.py
def settler_loop() -> None:
    from position_tracker import settle_all  # 延迟导入，避开 watch ↔ position_tracker 循环
```
**为什么**：position_tracker 又被 watch/runner 引用，如果 settle_all 在 settler 顶部 import，会形成环。

## 三、单元测试：拆包的副产品

**为什么一定要写测试**：
- 9 个真实事件 bug 全部发生在生产环境（日志回顾 + 用户吐槽才发现）
- 没有测试意味着"修一个 bug 引入另一个"无法快速发现
- 拆包后每个子模块都能独立测试（之前 watch.py 一坨根本测不动）

**覆盖策略**：
- **核心逻辑**：必测（`should_skip_new_signal` / `_settle_one` / `JumpTracker` 准入规则）
- **网络调用**：mock 掉（`patch("position_tracker._notify_fill")`）
- **副作用**：mock 掉（TG 推送 / journal 写入）

**跑法**：
```bash
cd scripts && python3 -m unittest discover tests -v
# Ran 29 tests in 0.009s  → OK
```

**覆盖率现状**：
| 模块 | 测试 | 备注 |
|---|---|---|
| `config.should_skip_new_signal` | 14 个 | 边界全覆盖（500 / 501 / 反向 / 多条 / 损坏 JSON） |
| `position_tracker._settle_one` | 15 个 | long/short + 部分止盈 + 同根双触 + 张数损益 |
| `watch/*` | 0 个 | 未来补（JumpTracker 准入逻辑、settler 心跳） |
| `scan.py make_plan` | 0 个 | 最重要但最复杂——接入真实历史 K 线回放 |

## 四、拆包前后对比

| 维度 | 拆包前 | 拆包后 |
|---|---|---|
| `watch.py` 行数 | 1132 | 27（薄壳） |
| 跨文件循环依赖 | 有（position_tracker ↔ watch） | 无（单向：所有脚本 → telegram） |
| 单元测试 | 0 | 29 |
| 单文件最大 | 1132 | watch/jump.py 303 |
| 修 bug 定位 | 翻全文件 | 直接打开对应子模块 |

## 五、决策清单（什么时候做什么）

- ✅ **模块 > 800 行**：拆
- ✅ **多个独立关注点**：拆
- ✅ **跨文件循环 import**：抽 telegram / config
- ✅ **单一关注点 < 400 行**：不拆
- ✅ **拆完一定写测试**：否则拆完你也不知道有没有破

## 六、回头看踩到的坑

1. **`watch.JUMP_THRESHOLD` 注释写错**（说 180s，实际 60s）—— 拆包时一起改了
2. **jump 路径的 TP1/TP2 用 scan payload 旧值** —— 拆包时用当前 target 重算（见 SKILL.md §6.7）
3. **position_tracker 还引用 `_load_tg_token` 等老函数** —— 用兼容 shim 解决，但强烈建议新代码直接 `from telegram import ...`
4. **拆完后忘了删 watch.py 的死代码**（159 行 telegram 函数定义）—— 用 `python3 -c "import ast; ast.parse(...)"` + `wc -l` 验证净减

## 七、相关章节

- SKILL.md §6 运维陷阱（10 节，全部基于真实事件）
- SKILL.md §7 文件清单（含 watch.py 薄壳 + watch/ 子包）
