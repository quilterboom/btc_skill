"""
v2_journal.py — v2.1 训练版虚拟持仓 journal
==========================================

liusir 2026-10-14 拍板：
  - 训练版 v2.1 触发后要能模拟持仓 + 算 PnL（"训练也有加仓，不开仓怎么知道是赚还是亏"）
  - v2.1 自己探索 SL/TP（"绝大多数不会依照自己的想法来"）
  - 回填昨天的历史数据（30 条触发判定 → 模拟 → 看赚亏）

设计：
  ┌──────────────────────────────────────────┐
  │ v2.1 触发（v2_decisions.jsonl）          │
  │   triggered=True → 调 open_v2_position  │
  └──────────────────────────────────────────┘
            ↓
  ┌──────────────────────────────────────────┐
  │ open_v2_position()                       │
  │   - 用 ATR 自适应算 SL（0.4%-1.5% 区间）│
  │   - TP1/TP2 保守（0.5%/1.0%）           │
  │   - 减仓 50% + 推保本（跟实盘一致）     │
  │   - 仓位 50U × 100x = 603 张             │
  │   - duplicate_blocked（同向持仓）       │
  │   - 写 v2_journal.jsonl                  │
  └──────────────────────────────────────────┘
            ↓
  ┌──────────────────────────────────────────┐
  │ settle_v2_all() （每 15min 跑一次）      │
  │   - 拉 1m K 线回放 pending 单            │
  │   - 判定 TP1 / SL / BE / 持仓中         │
  │   - 算 PnL（含 4.85U taker fee）         │
  └──────────────────────────────────────────┘

字段约定：
  - entry 用 v2.1 触发时的 snapshot.px
  - SL 用 ATR 自适应（atr_period=14）
  - TP1/TP2 固定 0.5%/1.0%（保守，"不会依照自己的想法来"）
  - 减仓段 = TP1 段 + 剩余段；TP1 命中后 active_sl=entry（保本）
  - net_usd = gross_usd - 4.85 (taker fee, 跟实盘 2026-10-08 一致)
"""
from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

# 训练版虚拟持仓落盘位置（跟实盘 journal + v2_decisions 完全独立）
TRAINING_DIR = Path("/root/data/training")


def _journal_path() -> Path:
    """★ 2026-10-26 修复：V2_JOURNAL_PATH 原来是**导入时**算死的模块级常量
    `TRAINING_DIR / "v2_journal.jsonl"`，测试 patch TRAINING_DIR 时它不会跟着变
    → 单测把假单写进**真实** /root/data/training/v2_journal.jsonl（实测残留一条
    v2-D-TEST-1-long，pending+filled+side=long，命中 cooldown「同向 pending+filled
    一律跳过」守卫，把后续所有做多触发全堵死 —— 23 条 long 触发 0 条能开仓）。

    同 position_tracker.EVENTS 的教训：路径必须**调用时**按 TRAINING_DIR 现算，
    不能提前算成常量，否则 patch 隔离失效。"""
    return TRAINING_DIR / "v2_journal.jsonl"


# 兼容旧引用（仅用于打印/展示；读写一律走 _journal_path()）
V2_JOURNAL_PATH = TRAINING_DIR / "v2_journal.jsonl"
TRAINING_DIR.mkdir(parents=True, exist_ok=True)

# v2.1 自己探索的 SL/TP 规则（liusir 2026-10-14 拍板）
V2_SLTB_RULES = {
    "atr_period": 14,             # ATR 周期
    "atr_tf": "15m",              # ★ 2026-10-23 改：1m → 15m
                                  #   1m ATR 中位 0.044%，×2.3 倍数也只有 0.10%，
                                  #   比原来的 0.4% 下限还窄 4 倍 → 必被扫。
                                  #   15m ATR 中位 0.21%，噪声小得多，且与策略开发口径一致。
    "sl_atr_mult": 4.0,           # ★ 2026-10-23 新增：SL = 4.0 × ATR(15m)
    "tp1_atr_mult": 2.0,          # ★ 2026-10-23 新增：TP1 = 2.0 × ATR(15m)
    "tp2_atr_mult": 8.0,          # ★ 2026-10-23 新增：TP2 = 8.0 × ATR(15m)
    "sl_min_pct": 0.006,          # SL 最小 0.6%（原 0.4%）
    "sl_max_pct": 0.030,          # SL 最大 3.0%（原 1.5%）
    "tp1_pct": 0.005,             # ↓ 旧口径，仅当 atr_atr_mult 关闭时用
    "tp2_pct": 0.010,             # ↓ 旧口径，仅当 atr_atr_mult 关闭时用
    "use_atr_mult": True,         # ★ 2026-10-23 新增开关：True=用 ATR 倍数，False=用固定 tp1_pct/tp2_pct
    "be_at_tp1": True,            # ★ 2026-10-23 新增：TP1 后是否推保本
                                  #   True = 保本（策略开发回测推荐：胜率高、净高）
                                  #   False = 不推保本（净更高但胜率掉一半，尾部风险大）
    # ==========================================================
    # ★ 2026-10-23 策略改造（13 个月 1015 信号回测结论）
    #   依据：dev_v2_full_opt.py / dev_v2_dim_test.py / dev_v2_target_search.py
    #         dev_v2_oversold.py / dev_v2_independent.py / dev_v2_final_config.py
    #   发现 1：TP/SL 出场压垮慢漂移信号 —— 168 组 TP/SL 参数 0/168 训练测试同时为正，
    #           而 0 参数纯时间退出（48h）训练 +918 / 测试 +770 都为正。
    #           根因：v2.1 做多 edge 是 48-72h 慢漂移（+0.56%/72h），
    #                 TP/SL 用波动率尺度，在慢信号上波动率是噪声 → 止损被噪声打掉。
    #   发现 2：做空 13 个月 579 信号 42% 准确率、平均 -0.173%，稳定负向 → 关闭。
    #   发现 3：做多是「超跌反弹」——**分界线在 7 日涨幅 0%**（不是 1%）：
    #             t7<=0%  → 48h 后上涨 78~83%
    #             t7 0~1.5% → 48h 后上涨 38.9%（分界另一侧）
    #   最终配置（7 道检验全过）：
    #     long only + 7日涨幅 <= 0% + 48h 时间退出
    #     历史 n=18  胜率 77.8%  净 +1879.04  Bootstrap 95%CI [+1064, +2725]
    #     ✓ 去重 4/6/8/12/18/24h 六种间隔下都成立（胜率 77.8~84.6%）
    #     ✓ 走步 4/4 段为正
    #     ✓ 随机对照 p=0.006（优于随机 99 分位）
    #     ✓ 前后半 77.8% / 77.8%
    #     ✓ 与随机入场有协同（随机入场+t7过滤仅 55.6%，v2.1+t7过滤 77.8%）
    #     ✓ 滑点 0.3% + 手续费 6U 下仍显著为正
    #     ✓ 2026-05 之后（严格时间外）n=10 胜率 80.0% 净 +974
    #     ✓ 集中度：去掉最赚 2 笔后仍 +1371
    #   风险：n=18 偏小；阈值来自扫描（多重比较）；需真实前瞻数据确认。
    # ==========================================================
    "long_only": True,            # ★ 只做多（做空 13 个月数据证明稳定负向）
    "regime_max_7d": 0.0,         # ★ 只在 7 日涨幅 <= 0% 时开多（超跌反弹）
                                  #   注意：0 表示「7 日没涨」，不是「涨了 1% 以内」
    "vol_min_pct": 0.65,          # ★ 2026-10-24 新增：5 日波动率闸门（百分数 = 0.65%）
                                  #   依据：20 笔按入场波动率分组 ——
                                  #     高波动(0.76%~1.34%) 10 笔 70% / +742U
                                  #     低波动(0.52%~0.67%) 10 笔 40% / +56U
                                  #   OLS 净利~波动率 t=2.20 (df=18, p<0.05) R²=0.212
                                  #   阈值扫描 0.55%~0.90% 全部显著为正（不是挑峰值）：
                                  #     0.55%→n=17 58.8% +836U  CI[+246,+1477]
                                  #     0.65%→n=13 61.5% +844U  CI[+238,+1472]
                                  #     0.75%→n=10 70.0% +742U  CI[+197,+1241]
                                  #   低波动下该策略从无优势（前半 40%/+92U，后半 40%/−36U）。
    "use_vol_gate": True,         # 2026-10-24 开关：False=关闭波动率闸门（回滚用）
    "time_exit_hours": 48,        # ★ 48h 时间退出（替代 TP/SL）
    "use_time_exit": True,        # ★ True=按时间退出；False=旧 TP/SL 逻辑
    "partial_pct": 0.50,          # 减仓 50%
    "bal": 50.0,                  # 模拟资金 50U（跟实盘一致）
    "lev": 100,                   # 100x（跟实盘一致）
    "taker_fee_usd": 4.85,        # taker 手续费
    "cooldown_secs": 1800,        # 30min cooldown（同向持仓）
    "min_dist_pts": 500,          # 同向 entry 最小距离（防同一信号反复开仓）
    "min_contracts": 1,           # 2026-10-15 加：仓位数下限 (50U×100x / 0.0001 = 0.5 张
                                  #   int() 抹零变 0 张 → gross=0 → 净亏只有手续费；不真实)
}

QUANT_MULT = 0.0001  # 1 张 = 0.0001 BTC


# ============================================================
# ATR 自适应 SL 计算（liusir 10-14: "v2.1 自己摸索一套 SL"）
# ============================================================
def compute_atr_pct(contract: str = "BTC_USDT", ts: int = None, period: int = 14,
                    clamp: bool = True) -> float:
    """
    用最近 period 根 K 线的 ATR（average true range）算百分比。
    - True Range = max(high-low, |high-prev_close|, |low-prev_close|)
    - ATR = mean(TR, period)
    - atr_pct = ATR / close

    ★ 2026-10-23 改造：
      1) 周期从 1m 改为 V2_SLTB_RULES['atr_tf']（默认 15m）
         —— 1m ATR 中位仅 0.044%，任何合理倍数都窄于原 0.4% 下限，必被扫。
      2) 新增 clamp 参数：False 时返回**未 clamp** 的原始 atr_pct。
         —— 原来这个函数内部就 clamp 到 [sl_min, sl_max]，调用方拿到的永远是
            clamp 后的值，无法再乘倍数。新口径需要「原始 ATR% → ×倍数 → 再 clamp」。

    返回的 atr_pct 是绝对值（如 0.0063 = 0.63%）。
    """
    try:
        sys.path.insert(0, "/root/skills/btc-gate-intraday-strategy/scripts")
        from gate_fetch import fetch_ohlcv

        # 取触发时刻前 period+1 根 K 线（周期由 atr_tf 决定，默认 15m）
        atr_tf = V2_SLTB_RULES.get("atr_tf", "15m")
        step = {"1m": 60, "5m": 300, "15m": 900, "1h": 3600}.get(atr_tf, 900)
        end_ts_arg = ts if ts is not None else int(time.time())
        bars = fetch_ohlcv(contract=contract, interval=atr_tf, total=period + 5,
                            end_ts=end_ts_arg + step, verbose=False)
        if len(bars) < period + 1:
            return V2_SLTB_RULES["sl_min_pct"]  # 数据不够用最小值

        # 截取窗口
        if ts:
            window = [b for b in bars if b[0] <= ts][-period - 1:]
        else:
            window = bars[-(period + 1):]

        if len(window) < period + 1:
            return V2_SLTB_RULES["sl_min_pct"]

        # 算 TR
        trs = []
        for i in range(1, len(window)):
            high = window[i][2]
            low = window[i][3]
            prev_close = window[i - 1][4]
            tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
            trs.append(tr)

        if not trs:
            return V2_SLTB_RULES["sl_min_pct"]

        atr = sum(trs) / len(trs)
        last_close = window[-1][4]
        if last_close <= 0:
            return V2_SLTB_RULES["sl_min_pct"]

        atr_pct = atr / last_close
        if not clamp:
            return atr_pct      # ★ 原始值（供调用方 ×倍数 后再 clamp）
        # clamp 到 [sl_min, sl_max]
        return max(V2_SLTB_RULES["sl_min_pct"],
                   min(V2_SLTB_RULES["sl_max_pct"], atr_pct))
    except Exception as e:
        # fallback 用固定最小值
        return V2_SLTB_RULES["sl_min_pct"]


# ============================================================
# 写入函数（原子：先写 tmp 再 replace）
# ============================================================
def compute_regime_7d(ts: int = None, px: float = None) -> float | None:
    """
    ★ 2026-10-23 新增：行情状态指标 —— 入场前 7 日 BTC 涨跌幅（小数，如 0.012 = +1.2%）。

    用途：v2.1 做多是「超跌反弹」信号，只在 7 日涨幅 <1% 时有效。
          实测（13 个月，long only 去重12h）：
            7d<1%  → n=30 胜率 70.0% 净 +1771  CI [+571,+2985] 显著
            7d>=1% → n=56 胜率 39.3% 净  -83  ← 反向检验通过
    返回 None = 数据不足（调用方应放行，宁可多做不做错）。
    """
    try:
        sys.path.insert(0, "/root/skills/btc-gate-intraday-strategy/scripts")
        from gate_fetch import fetch_ohlcv

        end_ts_arg = ts if ts is not None else int(time.time())
        # 拉 200 根 1h ≈ 8.3 天，足够覆盖 7 日窗口
        bars = fetch_ohlcv(contract="BTC_USDT", interval="1h", total=200,
                            end_ts=end_ts_arg + 3600, verbose=False)
        if not bars:
            return None
        target = end_ts_arg - 7 * 86400
        # 取 ts <= target 的最后一根（历史价），与 dev 脚本口径一致
        prev = None
        for b in bars:
            if b[0] <= target:
                prev = b
            else:
                break
        if prev is None or not prev[4]:
            return None
        cur_px = px
        if cur_px is None:
            # 用最接近 end_ts 的那根收盘
            cur = bars[-1]
            for b in bars:
                if b[0] <= end_ts_arg:
                    cur = b
            cur_px = cur[4]
        return (cur_px - prev[4]) / prev[4]
    except Exception:
        return None


# ★ 2026-10-24 新增：波动率闸门的取值函数
#
# 口径必须与回测完全一致，否则闸门行为会偏离验证过的结论：
#   - 只用**已收盘** 4h bar（bar.ts + 4h <= ts），不用未收盘 bar 的 close
#   - 取最后 30 根 = 5 日
#   - vol = 相邻 close 收益率的样本标准差（ddof=1）× 100，单位 %
#
# ★ 不要用 `bisect_right(tss, ts)-1` 取 bar —— 那会取到「起始 ts <= ts 但还没收盘」
#   的 bar，是未来信息（见 btc-strategy-dev skill `references/backtest-lookahead-traps.md`）。
def compute_vol_5d(ts: int | None = None, period: int = 30) -> float | None:
    """
    入场前 5 日已实现波动率（百分数，如 0.65 = 0.65%）。

    用途：v2.1 是「超跌反弹」策略，需要振幅来收割。低波动窄幅震荡下无优势：
            高波动(0.76%~1.34%) → 10 笔 70% 胜率 / +742U
            低波动(0.52%~0.67%) → 10 笔 40% 胜率 / +56U
          净利~波动率 OLS t=2.20 (df=18, p<0.05)，R²=0.212。

    返回 None = 已收盘 K 线不足 period 根（调用方应放行，
    与 compute_regime_7d 的「数据不足宁可多做不做错」口径一致）。
    """
    try:
        import math
        sys.path.insert(0, "/root/skills/btc-gate-intraday-strategy/scripts")
        from gate_fetch import fetch_ohlcv

        end_ts_arg = ts if ts is not None else int(time.time())
        bar_sec = 4 * 3600
        # 多拉一些，保证过滤掉未收盘 bar 后仍有 period 根
        bars = fetch_ohlcv(contract="BTC_USDT", interval="4h", total=period + 15,
                            end_ts=end_ts_arg + bar_sec, verbose=False)
        if not bars:
            return None
        # 严格：只保留已收盘 bar（bar.ts + 4h <= ts）
        closed = [b for b in bars if b[0] + bar_sec <= end_ts_arg]
        if len(closed) < period:
            return None
        closed = closed[-period:]
        closes = [float(b[4]) for b in closed]
        rets = [(closes[i] - closes[i - 1]) / closes[i - 1] for i in range(1, len(closes))]
        if len(rets) < 2:
            return None
        mean = sum(rets) / len(rets)
        var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)   # ddof=1，与回测一致
        return math.sqrt(var) * 100
    except Exception:
        return None


def _log_gate_block(ts: int, gate: str, value, threshold, decision_id: str | None = None) -> None:
    """
    ★ 2026-10-24：开仓闸门挡掉时落一笔，让「为什么没开仓」可查。
    否则闸门是隐形的 —— liusir 需要能回答「为什么又没开单」（同 10-24 止损质疑的教训）。
    """
    try:
        p = TRAINING_DIR / "v2_gate_log.jsonl"
        rec = {"ts": int(ts), "ts_str": datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S"),
               "gate": gate, "value": value, "threshold": threshold,
               "decision_id": decision_id}
        with p.open("a") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass


def gate_status(ts: int | None = None) -> dict:
    """
    ★ 2026-10-24：给前端/CLI 用的闸门当前状态（只读，不改任何状态）。
    返回 {"vol_5d_pct", "vol_min_pct", "vol_pass", "regime_7d_pct", "regime_max_7d",
          "regime_pass", "blocked_24h"}
    """
    ts = ts if ts is not None else int(time.time())
    vol = compute_vol_5d(ts=ts)
    vmin = V2_SLTB_RULES.get("vol_min_pct")
    r7 = compute_regime_7d(ts=ts)
    rmax = V2_SLTB_RULES.get("regime_max_7d")
    blocked = 0
    try:
        p = TRAINING_DIR / "v2_gate_log.jsonl"
        if p.exists():
            for line in p.read_text().splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                if ts - rec.get("ts", 0) <= 24 * 3600:
                    blocked += 1
    except Exception:
        pass
    return {
        "vol_5d_pct": round(vol, 3) if vol is not None else None,
        "vol_min_pct": vmin,
        "vol_pass": (vol is not None and vmin is not None) and vol >= vmin,
        "regime_7d_pct": round(r7 * 100, 3) if r7 is not None else None,
        "regime_max_7d": rmax,
        "regime_pass": (r7 is not None and rmax is not None) and r7 <= rmax,
        "blocked_24h": blocked,
    }


def _is_test_id(rid: str) -> bool:
    """测试/合成 id 判定（与 web/server.py::_is_test_id 同口径）。

    真实 id 的时间戳有两种形态（实测校准）：
      · v2-v2_20261022_221528-long      → decision_id = v2_YYYYMMDD_HHMMSS
      · BTC_USDT-1792854921-short       → 10 位 unix 秒
    测试残留形如 v2-D-TEST-1-long / BTC_USDT-1000-short（短序号、无时间戳）。
    判定：认「带真实时间戳」为真，其余一律当测试 —— 宁可漏判不许误杀真单。"""
    rid = rid or ""
    if "TEST" in rid.upper():
        return True
    import re as _re
    if _re.search(r"_20\d{6}_\d{6}", rid):        # v2_20261022_221528
        return False
    if _re.search(r"-\d{10}-", rid):               # 10 位 unix 秒
        return False
    return True


def _read_journal() -> list:
    _p = _journal_path()
    if not _p.exists():
        return []
    out = []
    with open(_p) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def _write_journal(jrnl: list) -> None:
    """原子写：tmp + replace（跟实盘 journal 一致）"""
    _p = _journal_path()
    tmp_path = _p.with_suffix(".jsonl.tmp")
    with open(tmp_path, "w") as f:
        for sig in jrnl:
            f.write(json.dumps(sig, ensure_ascii=False) + "\n")
    os.replace(tmp_path, _p)


# ============================================================
# 开仓：v2.1 触发 → 模拟开仓
# ============================================================
def open_v2_position(decision: dict) -> dict | None:
    """
    v2.1 触发后调：开一笔虚拟持仓。

    参数：
      decision: v2_decisions.jsonl 里的一条判定（含 ts_epoch, bias, snapshot.px, decision_id）

    返回：
      新开的虚拟单 dict（写入 journal 后返回）；None = 被 duplicate_blocked
    """
    bias = decision["bias"]
    if bias not in ("long", "short"):
        return None  # neutral 不开仓

    # ==========================================================
    # ★ 2026-10-23 方向过滤 + 行情过滤（策略改造，见 V2_SLTB_RULES 注释）
    #   做空关闭：13 个月 579 信号 42% 准确率 / 平均 -0.173%，稳定负向。
    #   行情过滤：做多是「超跌反弹」，只在 7 日涨幅 <1% 时有效。
    #   反向检验：7d>=1% → n=56 胜率 39.3% 净 -83（确实变差，过滤方向正确）。
    # ==========================================================
    if V2_SLTB_RULES.get("long_only") and bias == "short":
        return None

    ts_epoch = decision["ts_epoch"]
    px = decision["snapshot"]["px"]
    decision_id = decision["decision_id"]

    _regime_max = V2_SLTB_RULES.get("regime_max_7d")
    # ★ 2026-10-24 修复潜伏 bug：_r7 原来只在 `bias == "long"` 分支里赋值，
    #   但下面写 journal 记录时无条件引用 `regime_7d_at_open: _r7`
    #   → 一旦 long_only 关掉、开空单就 UnboundLocalError 崩溃。
    #   （当前 long_only=True 所以没暴露；是 tests/test_v2_vol_gate.py 写用例时挖出来的。）
    #   先置 None，让「没算」和「算出来是 0」可区分。
    _r7 = None
    if _regime_max is not None and bias == "long":
        _r7 = compute_regime_7d(ts=ts_epoch, px=px)
        # ★ 比较用「>」而不是「>=」：regime_max_7d = 0.0 意为「7 日涨幅 <= 0% 才开仓」
        #   （含恰好 0% 的情况）。t7=0 恰好落在分界线上，实测 18 单里有 2 单 t7 极接近 0。
        # _r7 is None（数据不足）时放行：宁可多做不做错
        if _r7 is not None and _r7 > _regime_max:
            return None

    # ==========================================================
    # ★ 2026-10-24 波动率闸门（见 references/edge-decay-volatility-regime.md）
    #   v2.1 是「超跌反弹」，需要振幅来收割；低波动窄幅震荡下从无优势。
    #   13 个月 20 笔实测：高波动 10 笔 70%/+742U，低波动 10 笔 40%/+56U。
    #   净利~波动率 OLS t=2.20 (df=18, p<0.05)。
    #   只挡 long（与 regime_max_7d 同口径）：回测样本全部是多头，
    #   空头下该闸门是否有效**未经验证**，不要一并套用。
    # ==========================================================
    _vmin = V2_SLTB_RULES.get("vol_min_pct")
    _vol = None        # 记进 journal 供事后审计闸门判据（同 _r7 的处理）
    if (V2_SLTB_RULES.get("use_vol_gate", True)
            and _vmin is not None and bias == "long"):
        _vol = compute_vol_5d(ts=ts_epoch)
        # 数据不足时放行（同 compute_regime_7d 口径：宁可多做不做错）
        if _vol is not None and _vol < _vmin:
            _log_gate_block(ts_epoch, "vol_min", round(_vol, 3), _vmin, decision_id)
            return None

    # cooldown 检查：30min 内同向持仓不重开（只看 pending，invalidated 不影响）
    #
    # ★ 2026-10-23 修复：同步生产 config.py 的 2026-10-11 修复（当时漏同步到训练环境）。
    #   问题 1：老代码只看时间窗，不看"是否还在持仓" → 同向 pending+filled 的持仓
    #          平仓前就可以再开新单。实测 10-16 19:15/19:45、10-19 16:30/17:15、
    #          10-20 05:00/05:45/06:30/07:45 全是同一方向堆叠开仓。
    #   问题 2：cooldown 边界 `ts_epoch - sig_ts < cooldown_secs` 不含等于
    #          → 30 分钟整就放行（10-16 19:15 → 19:45 正好 1800s）。
    #   修复：
    #     ① 同向 pending+filled 持仓 → **不论时间窗都跳过**（等平仓再开新单）
    #     ② cooldown 边界改 `<=`
    #     ③ 同向 entry 距离过近也跳过（min_dist_pts 兜底）
    jrnl = _read_journal()
    _same_side_active = None
    for sig in reversed(jrnl):
        if sig.get("status") != "pending":
            continue  # settled/invalidated 都不参与 cooldown 判定
        # ★ 2026-10-26 防御：测试残留 id 不许参与 cooldown 判定。
        #   背景：v2-D-TEST-1-long（单测写的假单，id 数字段不是 unix 秒）以
        #   pending+filled 常驻 journal，命中下面①的「同向 pending+filled 一律跳过」
        #   → 23 条 long 触发 0 条能开仓，v2 训练链路静默瘫痪 2 周。
        #   真实 id 形如 v2-v2_20261022_221528-long（decision_id 带 14 位时间戳）。
        if _is_test_id(sig.get("id") or ""):
            continue
        if sig.get("side") == bias and sig.get("signal_source") == "v2_aggregate":
            # ★ ① 同向 pending + filled → 不论时间窗都跳过
            #   ★ 2026-10-26 加陈旧豁免：filled 但已远超 time_exit 窗口还没平的，
            #     说明结算链路没跟上（不是真在持仓），不该永久堵死新单。
            if sig.get("filled", False):
                _fill_ts = int(sig.get("fill_ts") or sig.get("ts") or 0)
                _stale_after = int(V2_SLTB_RULES.get("time_exit_hours", 48)) * 3600 * 2
                if _fill_ts and ts_epoch - _fill_ts <= _stale_after:
                    return None
                # 超窗 → 落一条审计日志再放行（不静默）
                _log_gate_block(ts_epoch, "stale_filled_bypass", sig.get("id"),
                                f"fill_ts 超 {_stale_after}s 未平，不阻塞新单",
                                sig.get("decision_id"))
                continue
            # ② cooldown（用持仓的开仓 ts，不是决策 ts）
            sig_ts = sig.get("ts") or sig.get("fill_ts") or 0
            if ts_epoch - sig_ts <= V2_SLTB_RULES["cooldown_secs"]:
                return None
            if _same_side_active is None:
                _same_side_active = sig
    # ★ ③ 同向 entry 距离过近也跳过（防"同一信号被反复扫到"给新 ID）
    if _same_side_active is not None:
        _near_entry = float(_same_side_active.get("entry") or 0)
        if _near_entry > 0 and abs(px - _near_entry) < V2_SLTB_RULES.get("min_dist_pts", 500):
            return None

    # 算 ATR 自适应 SL
    #
    # ★ 2026-10-23 改造（策略开发结论，见 btc-strategy-dev skill）：
    #   旧口径：sl_pct = compute_atr_pct()（1m ATR，函数内已 clamp）
    #          → 1m ATR 中位 0.044% << sl_min 0.4% → **全部 46 单撞下限**
    #          → SL 恒为 0.4%，TP1 0.5% / TP2 1.0%，TP2/SL = 2.5 但止损极窄 → 必被扫
    #   新口径：raw_atr（不 clamp）→ ×倍数 → 再 clamp
    #          SL  = 4.0 × ATR(15m)  ≈ 0.84%（clamp 到 [0.6%, 3.0%]）
    #          TP1 = 2.0 × ATR(15m)  ≈ 0.42%
    #          TP2 = 8.0 × ATR(15m)  ≈ 1.68%
    #          → TP2/SL = 2.0，TP1/SL = 0.5（TP1 先落袋一部分，符合分批止盈结构）
    use_mult = V2_SLTB_RULES.get("use_atr_mult", True)
    raw_atr = compute_atr_pct(ts=ts_epoch, clamp=False)
    if use_mult:
        atr_pct = raw_atr   # meta 里记原始 ATR%
        sl_pct = min(max(raw_atr * V2_SLTB_RULES.get("sl_atr_mult", 4.0),
                          V2_SLTB_RULES["sl_min_pct"]), V2_SLTB_RULES["sl_max_pct"])
        tp1_pct = raw_atr * V2_SLTB_RULES.get("tp1_atr_mult", 2.0)
        tp2_pct = raw_atr * V2_SLTB_RULES.get("tp2_atr_mult", 8.0)
    else:
        atr_pct = compute_atr_pct(ts=ts_epoch)
        sl_pct = atr_pct
        tp1_pct = V2_SLTB_RULES["tp1_pct"]
        tp2_pct = V2_SLTB_RULES["tp2_pct"]

    # 算绝对价位
    if bias == "long":
        sl = px * (1 - sl_pct)
        tp1 = px * (1 + tp1_pct)
        tp2 = px * (1 + tp2_pct)
    else:  # short
        sl = px * (1 + sl_pct)
        tp1 = px * (1 - tp1_pct)
        tp2 = px * (1 - tp2_pct)

    # 仓位：跟生产 position_tracker._contracts() 一致
    # 名义 = bal × lev = 5000U；张数 = max(1, round(notional / (entry × QUANT_MULT)))
    # 例如 entry=83330: 5000 / (83330 * 0.0001) = 5000 / 8.333 ≈ 600 张
    # 2026-10-15 修：原来直接 int(notional * QUANT_MULT) 是错的（notional 跟 QUANT_MULT 单位错配），
    # 得出来 0 张，gross=0，每单净亏只剩手续费。同步生产算法 + 加 min_contracts 下限。
    bal = V2_SLTB_RULES["bal"]
    lev = V2_SLTB_RULES["lev"]
    notional = bal * lev
    contracts_raw = int(round(notional / (px * QUANT_MULT)))
    contracts = max(V2_SLTB_RULES.get("min_contracts", 1), contracts_raw)

    sig_id = f"v2-{decision_id}-{bias}"
    sig = {
        "id": sig_id,
        "contract": decision.get("contract", "BTC_USDT"),
        "ts": ts_epoch,
        "side": bias,
        "verdict": f"v2.1 触发 ({decision.get('consensus', '?')}, norm={decision['norm']:.3f})",
        "signal_source": "v2_aggregate",
        "decision_id": decision_id,
        "contracts": contracts,           # 2026-10-15 加：之前漏写 → settler fallback 算回 0 张
        "notional_usd": notional,
        "score": 0,
        "core_ok": False,
        "entry": px,
        "entry_break": px,
        "sl": sl,
        "tp1": tp1,
        "tp2": tp2,
        "sl_pct": sl_pct,
        "tp1_pct": tp1_pct,
        "tp2_pct": tp2_pct,
        "tp_mode": "v2_explore",
        "status": "pending",
        "filled": False,
        "tags": {
            "consensus": decision.get("consensus"),
            "norm": decision["norm"],
            "composite_score": decision.get("composite_score"),
        },
        "meta": {
            "bal": bal,
            "lev": lev,
            "risk": 0.01,
            "mode": "cross",
            "atr_at_open": atr_pct,
            "atr_period": V2_SLTB_RULES["atr_period"],
            "atr_tf": V2_SLTB_RULES.get("atr_tf", "15m"),
            # ★ 2026-10-23：出场模式标记。只有显式标 "time" 的新单才走时间退出，
            #   旧单（无此字段）继续走 TP/SL —— 不追溯改历史单语义。
            "exit_mode": ("time" if V2_SLTB_RULES.get("use_time_exit") else "tp_sl"),
            "time_exit_hours": (V2_SLTB_RULES.get("time_exit_hours")
                                if V2_SLTB_RULES.get("use_time_exit") else None),
            "regime_7d_at_open": _r7,   # 记录开仓时的 7 日涨幅，供事后审计过滤是否生效
            # ★ 2026-10-24：记录开仓时的 5 日波动率 + 闸门阈值，供事后审计
            "vol_5d_at_open": _vol,
            "vol_min_pct": V2_SLTB_RULES.get("vol_min_pct") if V2_SLTB_RULES.get("use_vol_gate") else None,
            "sl_atr_mult": V2_SLTB_RULES.get("sl_atr_mult") if use_mult else None,
            "tp1_atr_mult": V2_SLTB_RULES.get("tp1_atr_mult") if use_mult else None,
            "tp2_atr_mult": V2_SLTB_RULES.get("tp2_atr_mult") if use_mult else None,
        },
    }

    jrnl.append(sig)
    _write_journal(jrnl)
    return sig


# ============================================================
# Settler：每 15min 跑一次，回放 1m K 线更新持仓
# ============================================================
def settle_v2_all(verbose: bool = False) -> dict:
    """
    跟实盘 settle_all 类似，但只处理 v2_journal.jsonl。

    回放逻辑（跟实盘一致）：
      - pending 且 filled=False → 找触发 ts 之后的第一根 K 线 low/high 判定是否成交
      - pending 且 filled=True → 检查 TP1 / SL 是否触发
      - TP1 命中 → 推保本（active_sl=entry）+ partial_exit_count+1
      - SL 命中（active_sl 或 sl）→ 立刻 close
      - BE_STOPPED：SL 在 active_sl=entry 时被触发
      - TP2 命中 → 全部平仓

    返回：{'done': int, 'pending': int, 'net_usd': float}
    """
    import math
    sys.path.insert(0, "/root/skills/btc-gate-intraday-strategy/scripts")
    from gate_fetch import fetch_ohlcv

    jrnl = _read_journal()
    if not jrnl:
        return {"done": 0, "pending": 0, "net_usd": 0.0}

    # 一次性拉最新 1m K 线（满足所有 pending 的回放）
    # ★ 2026-10-23：2000 → 4000 根（66h），覆盖 48h 时间退出窗口 + 余量
    now_ts = int(time.time())
    bars = fetch_ohlcv(contract="BTC_USDT", interval="1m", total=4000,
                        end_ts=now_ts + 60, verbose=False)

    done = 0
    total_net = 0.0
    pending_count = 0

    for sig in jrnl:
        if sig.get("status") == "settled":
            continue
        if sig.get("outcome") == "DUPLICATE_BLOCKED":
            continue

        ts_open = sig["ts"]
        side = sig["side"]
        entry = sig["entry"]
        sl = sig["sl"]
        tp1 = sig["tp1"]
        tp2 = sig.get("tp2")
        contracts = sig.get("contracts") or int(V2_SLTB_RULES["bal"] *
                                               V2_SLTB_RULES["lev"] *
                                               QUANT_MULT)
        sig["contracts"] = contracts  # 写回

        # ★ 2026-10-23 出场模式（提前到这里，成交/过期判定也要用）
        #   旧单（meta 无 exit_mode 字段）继续走 TP/SL，不追溯改历史语义。
        _meta = sig.get("meta") or {}
        _use_time_exit = _meta.get("exit_mode") == "time"
        _time_exit_sec = (_meta.get("time_exit_hours")
                          or V2_SLTB_RULES.get("time_exit_hours", 48)) * 3600

        # 找 ts_open 之后的 K 线
        future = [b for b in bars if b[0] >= ts_open]
        if not future:
            pending_count += 1
            continue

        # 第一阶段：成交
        if not sig.get("filled"):
            for b in future:
                _, o, h, l, c, v, s = b
                if side == "long" and l <= entry:
                    sig["filled"] = True
                    sig["fill_ts"] = b[0]
                    sig["fill_px"] = entry
                    sig["last_check_ts"] = b[0]
                    break
                elif side == "short" and h >= entry:
                    sig["filled"] = True
                    sig["fill_ts"] = b[0]
                    sig["fill_px"] = entry
                    sig["last_check_ts"] = b[0]
                    break
            if not sig.get("filled"):
                # ★ 2026-10-23：时间退出模式下，未成交单也要过期 —— 否则 pending 永久堆积。
                if _use_time_exit and (now_ts - ts_open) >= _time_exit_sec:
                    sig["status"] = "settled"
                    sig["outcome"] = "EXPIRED_UNFILLED"
                    sig["filled"] = False
                    sig["net_usd"] = 0.0
                    sig["gross_usd"] = 0.0
                    sig["category"] = "breakeven"
                    sig["hold_h"] = (now_ts - ts_open) / 3600
                    done += 1
                    continue
                pending_count += 1
                continue

        # 第二阶段：TP/SL 回放
        active_sl = sig.get("active_sl") or sl
        tp1_hit = sig.get("tp1_hit", False)
        tp1_hit_px = sig.get("tp1_hit_px")
        tp1_hit_ts = sig.get("tp1_hit_ts")
        last_check = sig.get("last_check_ts", sig["fill_ts"])

        settled = False
        close_px = None
        close_ts = None
        outcome = None

        # ★ 2026-10-23 时间退出逻辑（_use_time_exit 已在上方判定）：
        #   成交后满 time_exit_hours → 按当根 K 线收盘价平仓，不看 TP/SL。
        #   依据：168 组 TP/SL 参数 0/168 训练测试同时为正，而 0 参数时间退出
        #         训练 +918 / 测试 +770 都为正（见 dev_v2_dim_test.py）。

        for b in future:
            if b[0] <= last_check:
                continue
            _, o, h, l, c, v, s = b

            if _use_time_exit:
                if sig.get("fill_ts") and (b[0] - sig["fill_ts"]) >= _time_exit_sec:
                    close_px = c          # 按当根 K 线收盘价平仓
                    close_ts = b[0]
                    outcome = "TIME_EXIT"
                    settled = True
                    break
                sig["last_check_ts"] = b[0]
                continue

            if side == "long":
                # SL 检查（active_sl 优先）
                if l <= active_sl:
                    close_px = active_sl
                    close_ts = b[0]
                    if math.isclose(active_sl, entry, abs_tol=1.0):
                        outcome = "BE_STOPPED"
                    else:
                        outcome = "SL_STOPPED"
                    settled = True
                    break
                # TP1 检查
                if not tp1_hit and h >= tp1:
                    tp1_hit = True
                    tp1_hit_px = tp1
                    tp1_hit_ts = b[0]
                    sig["tp1_hit"] = True
                    sig["tp1_hit_px"] = tp1
                    sig["tp1_hit_ts"] = b[0]
                    sig["partial_exit_count"] = sig.get("partial_exit_count", 0) + 1
                    # ★ 2026-10-23：保本改为可配置（V2_SLTB_RULES['be_at_tp1']）
                    #   True  = TP1 后推保本（active_sl=entry），回撤时保本出局
                    #   False = 不推保本，SL 留在原位 → 趋势延续能吃满 TP2，
                    #           但 TP1 后回撤会吃回浮盈（尾部风险）
                    if V2_SLTB_RULES.get("be_at_tp1", True):
                        sig["active_sl"] = entry  # 推保本
                        active_sl = entry
                # TP2 检查
                if tp2 and h >= tp2:
                    close_px = tp2
                    close_ts = b[0]
                    outcome = "TP2_DONE"
                    settled = True
                    break
            else:  # short
                if h >= active_sl:
                    close_px = active_sl
                    close_ts = b[0]
                    if math.isclose(active_sl, entry, abs_tol=1.0):
                        outcome = "BE_STOPPED"
                    else:
                        outcome = "SL_STOPPED"
                    settled = True
                    break
                if not tp1_hit and l <= tp1:
                    tp1_hit = True
                    tp1_hit_px = tp1
                    tp1_hit_ts = b[0]
                    sig["tp1_hit"] = True
                    sig["tp1_hit_px"] = tp1
                    sig["tp1_hit_ts"] = b[0]
                    sig["partial_exit_count"] = sig.get("partial_exit_count", 0) + 1
                    # ★ 2026-10-23：保本改为可配置（同上）
                    if V2_SLTB_RULES.get("be_at_tp1", True):
                        sig["active_sl"] = entry
                        active_sl = entry
                if tp2 and l <= tp2:
                    close_px = tp2
                    close_ts = b[0]
                    outcome = "TP2_DONE"
                    settled = True
                    break

            sig["last_check_ts"] = b[0]

        if settled:
            # 算 PnL
            contracts_orig = contracts
            if tp1_hit and outcome not in ("TP2_DONE",):
                # 部分平仓：TP1 段按 tp1 算，剩余按 close_px
                contracts_part = int(contracts_orig * V2_SLTB_RULES["partial_pct"])
                contracts_remain = contracts_orig - contracts_part
                if side == "long":
                    gross = (tp1_hit_px - entry) * contracts_part * QUANT_MULT + \
                            (close_px - entry) * contracts_remain * QUANT_MULT
                else:
                    gross = (entry - tp1_hit_px) * contracts_part * QUANT_MULT + \
                            (entry - close_px) * contracts_remain * QUANT_MULT
            else:
                # 全部按 close_px
                if side == "long":
                    gross = (close_px - entry) * contracts_orig * QUANT_MULT
                else:
                    gross = (entry - close_px) * contracts_orig * QUANT_MULT

            fee = V2_SLTB_RULES["taker_fee_usd"]
            net = gross - fee

            sig["exit_px"] = close_px
            sig["exit_ts"] = close_ts
            sig["outcome"] = outcome
            sig["status"] = "settled"
            sig["gross_usd"] = round(gross, 2)
            sig["net_usd"] = round(net, 2)
            sig["category"] = "profit" if net > 0 else ("breakeven" if abs(net) < 0.5 else "loss")
            sig["mae"] = 0  # 简化：不跟踪
            sig["mfe"] = 0
            sig["hold_h"] = (close_ts - ts_open) / 3600 if close_ts else 0
            total_net += net
            done += 1
            if verbose:
                print(f"  [v2 settle] {sig['id']}: {outcome} 净 {net:+.2f}U (entry={entry:.1f} exit={close_px:.1f})")
        else:
            pending_count += 1

    _write_journal(jrnl)
    return {"done": done, "pending": pending_count, "net_usd": round(total_net, 2)}


# ============================================================
# 统计：最近 N 单 / 全局 PnL
# ============================================================
def stats_v2_trades() -> dict:
    """
    训练 v2 虚拟持仓统计。

    ★ 2026-10-23 新增 by_strategy 分组：
      改造后训练 journal 里同时存在旧策略单（meta 无 exit_mode）和新策略单
      （meta.exit_mode == "time"）。混在一起统计会掩盖新策略的真实表现，
      所以额外返回按策略分组的胜率/净值。
    """
    jrnl = _read_journal()
    settled = [j for j in jrnl if j.get("status") == "settled"]
    pending = [j for j in jrnl if j.get("status") == "pending"]
    blocked = [j for j in jrnl if j.get("outcome") == "DUPLICATE_BLOCKED"]

    total_net = sum(j.get("net_usd", 0) for j in settled)
    wins = sum(1 for j in settled if j.get("net_usd", 0) > 0)
    win_rate = f"{wins}/{len(settled)} = {wins/max(1,len(settled))*100:.1f}%"

    from collections import Counter
    outcomes = Counter(j.get("outcome") for j in settled)

    # ★ 按策略版本分组：新策略 = meta.exit_mode == "time"（2026-10-23 上线）
    by_strategy = {}
    for label, pred in (
        ("new_time_exit", lambda j: (j.get("meta") or {}).get("exit_mode") == "time"),
        ("old_tp_sl", lambda j: (j.get("meta") or {}).get("exit_mode") != "time"),
    ):
        grp = [j for j in settled if pred(j)]
        if not grp:
            by_strategy[label] = {"n": 0}
            continue
        g_wins = sum(1 for j in grp if j.get("net_usd", 0) > 0)
        g_net = sum(j.get("net_usd", 0) for j in grp)
        by_strategy[label] = {
            "n": len(grp),
            "wins": g_wins,
            "win_rate": f"{g_wins}/{len(grp)} = {g_wins/len(grp)*100:.1f}%",
            "net_usd": round(g_net, 2),
            "outcomes": dict(Counter(j.get("outcome") for j in grp)),
        }

    return {
        "total": len(jrnl),
        "settled": len(settled),
        "pending": len(pending),
        "duplicate_blocked": len(blocked),
        "by_strategy": by_strategy,
        "total_net_usd": round(total_net, 2),
        "win_rate": win_rate,
        "outcomes": dict(outcomes),
    }


if __name__ == "__main__":
    print("=== v2_journal.py 测试 ===")
    stats = stats_v2_trades()
    for k, v in stats.items():
        print(f"  {k}: {v}")
    print(f"\n落盘位置: {V2_JOURNAL_PATH}")
