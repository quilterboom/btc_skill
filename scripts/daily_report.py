#!/usr/bin/env python3
"""
BTC 每日复盘 (liusir 2026-10-10 立)
- 拉取过去 24h (默认"昨天")的 BTC 行情 + 15m 形态信号扫描 + 主信号 score + journal 变更
- 输出 JSON 报告到 /root/data/daily_reports/report-YYYYMMDD.json
- 输出 Markdown 卡片到 stdout (供 cron agent 推送)

支持:
  python3 daily_report.py                 # 默认报告 "昨天" CST
  python3 daily_report.py --date 2026-10-09  # 指定日期
  python3 daily_report.py --json /tmp/x.json  # 自定义输出
"""
from __future__ import annotations
import argparse, json, os, sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

# 让脚本能找到同级模块
SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

from gate_fetch import fetch_ohlcv  # noqa: E402
import cost_basis_view as _cbv  # noqa: E402
from scan import (  # noqa: E402
    _detect_15m_pattern, _30m_trend_at,
    _is_shooting_star_15m, _is_hammer_15m,
    detect_all_patterns_15m, PATTERN_REGISTRY,
)

CST = timezone(timedelta(hours=8))
DATA_DIR = Path('/root/data')
JOURNAL = DATA_DIR / 'journal.jsonl'
REPORT_DIR = DATA_DIR / 'daily_reports'
REPORT_DIR.mkdir(parents=True, exist_ok=True)


# ---------- 数据拉取 ----------
def fetch_window(date_str: str) -> tuple[list, list, int, int]:
    """拉取指定日期的 15m 和 30m K 线. 返回 (rows_15m, rows_30m, start_ts, end_ts)"""
    # date_str 形如 "2026-10-09"
    y, m, d = map(int, date_str.split('-'))
    start = int(datetime(y, m, d, 0, 0, tzinfo=CST).timestamp())
    end = int(datetime(y, m, d + 1, 0, 0, tzinfo=CST).timestamp())  # 次日 0 点
    rows_15m = fetch_ohlcv('BTC_USDT', '15m', total=200, end_ts=end)
    rows_30m = fetch_ohlcv('BTC_USDT', '30m', total=100, end_ts=end)
    return rows_15m, rows_30m, start, end


def filter_window(rows: list, start: int, end: int) -> list:
    """filter K 线到 [start, end) CST 窗口"""
    return [r for r in rows if start <= r[0] < end]


def scan_patterns(rows_15m: list, start: int, end: int) -> list[dict]:
    """扫描窗口内所有 15m 形态信号 (形态 + 后根 + 30m 趋势不逆势)"""
    out = []
    for i in range(1, len(rows_15m) - 1):
        row = rows_15m[i]
        ts_s = row[0]
        if not (start <= ts_s < end):
            continue
        o, h, l, c = row[1], row[2], row[3], row[4]
        is_ss = _is_shooting_star_15m(o, h, l, c)
        is_hm = _is_hammer_15m(o, h, l, c)
        if not (is_ss or is_hm):
            continue
        nxt = rows_15m[i + 1]
        # ★ 2026-10-30 修（口径与生产不一致）：报告原来对流星只要求「后根1 阴」，
        #   生产 scan.py::_detect_15m_pattern 要求「后根1 阴 + 后根2 阴 + 后根2.close < 后根1.close」
        #   （连续 2 阴线）。口径不一致 → 报告报出的"理论可触发"笔数虚高，
        #   实测 10-29 的 19:45 流星：后根2 是阳线，生产根本不会触发，报告却算进 3 笔。
        #   现在与生产口径对齐（见 references/2026-10-30-15m-pattern-stale-trigger-bug.md）。
        if is_ss:
            # 流星: 连续 2 阴线且后根2 收盘 < 后根1 收盘
            if i + 2 >= len(rows_15m):
                continue
            nxt2 = rows_15m[i + 2]
            confirmed = (nxt[4] < nxt[1] and nxt2[4] < nxt2[1] and nxt2[4] < nxt[4])
        else:
            # 锤: 后根阳线
            confirmed = nxt[4] > nxt[1]
        if not confirmed:
            continue
        try:
            tr = _30m_trend_at(ts_s)
        except Exception:
            tr = 0
        tr_str = {1: '▲多', -1: '▼空', 0: '─中'}.get(tr, '?')
        # 30m 不逆势
        blocked = (is_ss and tr == 1) or (is_hm and tr == -1)
        out.append({
            'ts': ts_s,
            'time_cst': datetime.fromtimestamp(ts_s, CST).strftime('%m-%d %H:%M'),
            'kind': 'short' if is_ss else 'long',
            'kind_label': '做空' if is_ss else '做多',
            'close': c,
            'next_change_pct': (nxt[4] - nxt[1]) / nxt[1] * 100,
            '30m_trend': tr_str,
            '30m_blocked': blocked,
        })
    return out


def scan_pattern_ungated(rows_15m: list, start: int, end: int) -> list[dict]:
    """只扫形态 (不看后根 + 30m), 用于报告"原始形态分布" """
    out = []
    for i in range(1, len(rows_15m) - 1):
        row = rows_15m[i]
        ts_s = row[0]
        if not (start <= ts_s < end):
            continue
        o, h, l, c = row[1], row[2], row[3], row[4]
        is_ss = _is_shooting_star_15m(o, h, l, c)
        is_hm = _is_hammer_15m(o, h, l, c)
        if not (is_ss or is_hm):
            continue
        out.append({
            'ts': ts_s,
            'time_cst': datetime.fromtimestamp(ts_s, CST).strftime('%m-%d %H:%M'),
            'kind': 'short' if is_ss else 'long',
            'kind_label': '做空' if is_ss else '做多',
            'close': c,
        })
    return out


def scan_all_patterns_7kinds(rows_15m: list, start: int, end: int,
                              forward_bars: int = 4,
                              abs_move_pts: float = 50.0) -> dict:
    """
    2026-10-11 加：扩展到 7 种 K 线形态 + 算"反转成功率".

    逻辑:
      - 扫窗口内每根 15m K 线, 用 detect_all_patterns_15m 识别 7 种形态
      - 对每个匹配, 计算未来 forward_bars 根 (默认 4 根 = 1h) 内:
          * bias='long'  形态 → 看后续 close 涨幅是否 ≥ abs_move_pts (默认 50 点)
          * bias='short' 形态 → 看后续 close 跌幅是否 ≥ abs_move_pts
          * bias=None    形态 (犹豫态) → 不算反转, 仅记分布频次
      - 每个形态的"反转成功率" = 反转成功数 / 总触发数
      - 反转成功对 bias 方向 (做多 vs 做空) 分开计

    返回 dict:
      {
        "by_kind": {
          "锤子":      {"count": N, "long_success": X, "long_total": Y, "short_success": 0, "short_total": 0, "bias": "long"},
          "蜻蜓十字":  {...},
          "墓碑十字":  {"bias": "short", ...},
          ...
        },
        "by_hour": {
          hour_0_3:  {... 各形态数 ...},
          hour_4_7:  {...},
          ...
        },
        "examples": [   # 取最强的 8 条样本（带成功/失败详情）
          {"ts": ..., "kind": "...", "bias": "...", "success": True/False, "move_pts": ..., "future_closes": [...]}
        ],
        "params": {"forward_bars": N, "abs_move_pts": M, "total_bars_scanned": K}
      }

    注：bias=None 的形态 (纺锤/十字星/长腿) 不算成功率, 只计入 by_kind['count']
    """
    out = {
        "by_kind": {},
        "by_hour": {},
        "examples": [],
        "params": {
            "forward_bars": forward_bars,
            "abs_move_pts": abs_move_pts,
            "total_bars_scanned": 0,
        },
    }
    # 初始化 by_kind
    for name_zh, name_en, fn, bias, strength in PATTERN_REGISTRY:
        out["by_kind"][name_zh] = {
            "name_en": name_en,
            "bias": bias,
            "strength": strength,
            "count": 0,
            "long_success": 0, "long_total": 0,
            "short_success": 0, "short_total": 0,
        }
    # 初始化 by_hour（4 个时间段）
    hour_buckets = [(0, 8, "00-08"), (8, 16, "08-16"), (16, 24, "16-24")]
    for _, _, tag in hour_buckets:
        out["by_hour"][tag] = {name_zh: 0 for name_zh, *_ in PATTERN_REGISTRY}

    matched_examples = []  # 全部匹配，带详情，最后按强度 / 移动点数排序取 top 8
    total_scanned = 0
    for i in range(len(rows_15m)):
        row = rows_15m[i]
        ts_s = row[0]
        if not (start <= ts_s < end):
            continue
        total_scanned += 1
        o, h, l, c = row[1], row[2], row[3], row[4]
        patterns = detect_all_patterns_15m(o, h, l, c)
        if not patterns:
            continue
        # 算未来 forward_bars 的最大变动
        future_max_above = 0.0
        future_max_below = 0.0
        future_closes = []
        for j in range(1, forward_bars + 1):
            if i + j >= len(rows_15m):
                break
            f = rows_15m[i + j]
            future_closes.append(float(f[4]))
            move_up = float(f[2]) - c   # future high - 当前 close
            move_dn = c - float(f[3])   # 当前 close - future low
            future_max_above = max(future_max_above, move_up)
            future_max_below = max(future_max_below, move_dn)
        if not future_closes:
            continue  # 没有后根数据，跳过
        # 算 hour bucket
        hour_local = (datetime.fromtimestamp(ts_s, CST).hour)
        for h_lo, h_hi, tag in hour_buckets:
            if h_lo <= hour_local < h_hi:
                hour_tag = tag
                break
        else:
            hour_tag = "16-24"
        # 每个匹配的形态都计入一次（家族重叠允许）
        for p in patterns:
            name_zh = p["name_zh"]
            bias = p["bias"]
            strength = p["strength"]
            entry = out["by_kind"][name_zh]
            entry["count"] += 1
            out["by_hour"][hour_tag][name_zh] += 1
            # 算反转成功
            if bias == "long":
                entry["long_total"] += 1
                if future_max_above >= abs_move_pts:
                    entry["long_success"] += 1
            elif bias == "short":
                entry["short_total"] += 1
                if future_max_below >= abs_move_pts:
                    entry["short_success"] += 1
            matched_examples.append({
                "ts": ts_s,
                "time_cst": datetime.fromtimestamp(ts_s, CST).strftime('%m-%d %H:%M'),
                "kind": name_zh,
                "bias": bias,
                "strength": strength,
                "success_long": future_max_above >= abs_move_pts,
                "success_short": future_max_below >= abs_move_pts,
                "move_up_pts": round(future_max_above, 1),
                "move_dn_pts": round(future_max_below, 1),
                "future_high_pcts": [round((cl - c) / c * 100, 2) for cl in future_closes],
            })

    out["params"]["total_bars_scanned"] = total_scanned

    # examples: 取最强的 8 条（强度高 + 移动点大）
    matched_examples.sort(
        key=lambda x: (-x["strength"], -max(x["move_up_pts"], x["move_dn_pts"]))
    )
    out["examples"] = matched_examples[:8]

    return out


def load_journal_window(start: int, end: int) -> list[dict]:
    """加载 journal 在 [start, end) 内的所有记录"""
    if not JOURNAL.exists():
        return []
    out = []
    for line in JOURNAL.read_text().splitlines():
        if not line.strip():
            continue
        s = json.loads(line)
        ts = s.get('ts', 0)
        if start <= ts < end:
            out.append(s)
    return out


# ---------- 老师策略 TP 对照 (非生产, 只算不算) ----------
def simulate_teacher_TP(journal_entries: list, rows_15m: list, start_ts: int, end_ts: int) -> dict:
    """
    用老师 TP 体系重算 v4.2 同一批 entry 的 PnL:
      - TP1 = entry + 0.6% (近似 594 点), 50% 减半
      - 推保本: SL 移到 entry
      - 剩余段: 不设 TP2, 看 8h 内是否回到 entry (打保本走)
    假设: notional = 5000U, fee = 4.85U, leverage = 100x
    返回: {entries_compared: int, v4_2_net_usd: float, teacher_TP_net_usd: float, diff: float, breakdown: [...]}
    """
    rows = [r for r in rows_15m if start_ts <= r[0] < end_ts]
    if not rows:
        return {'note': '无 K 线数据, 跳过对照'}
    # ★ 2026-11-07：仓位张数不再在这里手算——改走 _cbv.canonical_contracts(cost_px)。
    #   名义 = 5000U（含杠杆），**不要**再除一次 LEV（老代码就是这么错的：
    #   仓位小 100 倍 → 老师 TP 盈亏被压扁 → 卡片结论方向反掉）。
    FEE = 4.85
    teacher_total = 0.0; v42_total = 0.0
    breakdown = []
    null_net = []        # ★ 2026-10-25: 已结算但 net_usd 缺失的单，单独暴露不静默计 0
    ghost_skipped = []   # ★ 2026-11-06: 幽灵成交（从未发生），不参与对照
    for e in journal_entries:
        if e.get('status') != 'settled' or e.get('outcome') in (None, ''):
            # pending 跳, 等下期再比
            continue
        # ★ 2026-11-06：幽灵成交（从未真正发生过的 hammer 交易）不参与对照。
        #   它没有真实成交价，teacher TP 体系无从复算；算进去等于给一笔
        #   不存在的交易编一个「老师 TP 表现」。
        if _cbv.is_ghost_fill(e):
            ghost_skipped.append({
                'journal_id': e.get('id'),
                'time_cst': datetime.fromtimestamp(e.get('ts') or 0, CST).strftime('%m-%d %H:%M'),
                'side': e.get('side'),
                'outcome': e.get('outcome'),
                'net_usd': e.get('net_usd'),
            })
            continue
        entry = e.get('entry')
        if not entry: continue
        # ★ 2026-11-07 成本基准：老师 TP 复算必须用**真实成交价**（fill_px 优先）。
        #   这笔是 hammer 秒级市价（计划 entry 75885.7 → 实际成交 76023.0，追价 +137.3 点）。
        #   用计划 entry 复算 = 假设老师以没成交的价格进场 = 两边口径不一致
        #   （v4.2 侧走 _cbv.net_of(e) 已是 fill 口径，老师侧却是 entry 口径）。
        cost_px = _cbv.cost_basis_of(e)
        if not cost_px:
            cost_px = float(entry)
        cost_px = float(cost_px)
        # ★ 2026-11-04：净值取真实成本基准（fill_basis 优先，账面兜底）。
        #   hammer 市价追价成交的记录账面 net_usd 按计划 entry 记（口径陈旧）。
        v42_net = _cbv.net_of(e)
        # ★ 2026-10-25 修：已结算但 net_usd 为 null 不许默默当 0 计入合计。
        #   老写法 `e.get('net_usd', 0) or 0` 会把 null 记成 0，v4.2 净利虚高、
        #   卡片上也看不到这笔（实测 10-24 23:15 那笔 -15.12U 就这样消失）。
        if e.get('net_usd') is None:
            null_net.append({
                'journal_id': e.get('id'),
                'time_cst': datetime.fromtimestamp(e.get('ts') or 0, CST).strftime('%m-%d %H:%M'),
                'side': e.get('side'),
                'outcome': e.get('outcome'),
                'exit_px': e.get('exit_px'),
                'exit_ts': e.get('exit_ts'),
            })
            continue          # 不计入 v4_2_total / teacher_total，单独列出
        v42_total += v42_net
        # 老师 TP: 找 entry 后最近一根 >= entry*1.006 的 15m (TP1)
        # ★ 2026-11-07：TP1 目标价与保本止损都必须挂在**真实成本基准** cost_px 上，
        #   不是计划 entry——老师 TP 体系是「进场后 +0.6% 减半、剩余推保本」，
        #   进场价就是实际成交价。挂错基准 = 整条 TP 路径偏移。
        # ★ 2026-11-07 修 **空单 TP1 目标价方向反了**：
        #   原来不管多空都写 `cost_px * 1.006`（在成本价**上方**）。
        #   空单要跌 0.6% 才到 TP1 = `cost_px * 0.994`。
        #   写成 1.006 时空单的 TP1 永远不可能被触及（价格要涨 0.6% 才对空单有利）。
        _side = e.get('side') or 'long'
        _is_long = (_side == 'long')
        tp1_target = cost_px * (1.006 if _is_long else 0.994)
        sl = cost_px
        # SL 段: 找 entry 后最低 <= entry 的 15m
        # ★ 2026-11-07 修 **仓位张数 100 倍错误**：
        #   老写法 `(NOTIONAL / LEV) / (entry * 0.0001) / 2` 把名义又除了一次杠杆。
        #   杠杆只决定保证金（50U），名义已经是 5000U——再除一次 = 仓位小 100 倍。
        #   实测：75885.7 入场 → 老写法只算出 3.29 张（半仓），真实是 329.44 张。
        #   后果：老师 TP 的毛利被压缩 100 倍 → 卡片「老师 TP vs v4.2」差异方向都反了
        #   （昨天那笔显示老师 TP 差 1.40U，实际老师 TP 明显更好）。
        #   交叉验证：teacher_trades.jsonl 里老师真实单 size_contracts 与本口径一致
        #   （#18: 78000 → 641 张）。
        half_contracts = _cbv.canonical_contracts(cost_px) / 2
        tp1_hit = False; tp1_px = None; sl_hit = False; sl_px = None
        tp1_reached_idx = None
        # ★ 2026-11-07 修 **空单方向反了**（本函数此前只按多头写）：
        #   原来对 short 也用 `r[2] >= tp1_target`（high 触达）判 TP1 命中、
        #   用 `r[3] <= cost_px`（low 触达）判保本止损 —— **两个方向都反了**。
        #   空单要跌才赚：TP1 应该是 low 触达，保本止损应该是 high 触达。
        #   后果不是数字偏一点，是**结论方向反掉**：
        #     实测 BTC_USDT-1792798212-short（v4.2 +37.13U 的赢家）：
        #       持仓期 0 根 bar 出现 high>=cost*1.006（老代码永远判它 TP1 未中）
        #       持仓期 20 根 bar 出现 low <=cost*0.994（真正的 TP1，老代码漏判）
        #     → 这类单在日报「老师TP对照」里显示成「老师 TP 没优势」，
        #       实际是方向 bug 制造的假象。
        #   影响面：可信单里 32% 是空单（实测 15/47）——这部分对照结论全是错的。
        _tp1_touch = (lambda r: r[2] >= tp1_target) if _is_long else (lambda r: r[3] <= tp1_target)
        _be_touch = (lambda r: r[3] <= cost_px) if _is_long else (lambda r: r[2] >= cost_px)
        _tp1_px_of = (lambda r: r[2]) if _is_long else (lambda r: r[3])
        _pnl_sign = 1.0 if _is_long else -1.0
        for i, r in enumerate(rows):
            if r[0] < (e.get('ts') or 0): continue
            if r[0] > end_ts: break
            if not tp1_hit and _tp1_touch(r):
                tp1_hit = True; tp1_px = _tp1_px_of(r)
                tp1_reached_idx = i
                # 立即推保本 (SL -> cost_px)
                break
            if _be_touch(r):
                sl_hit = True; sl_px = (r[3] if _is_long else r[2])
                break
        # TP1 段 PnL
        if tp1_hit:
            # tp1_px 在 tp1_hit 为真时必有值；显式取局部变量让类型收敛
            _tp1_px = tp1_px if tp1_px is not None else cost_px
            tp1_pnl = half_contracts * 0.0001 * (_tp1_px - cost_px) * _pnl_sign
            # 剩余段: 从 tp1_reached_idx 往后, 看是否回成本价 (打保本走)
            remain_pnl = 0
            for j in range(tp1_reached_idx + 1, len(rows)):
                r = rows[j]
                if r[0] > end_ts: break
                if _be_touch(r):
                    # 剩余打保本
                    remain_pnl = 0
                    break
                # 8h 内没回 = 仍持仓 (标记为'open')
                if j == len(rows) - 1:
                    remain_pnl = half_contracts * 0.0001 * (r[4] - cost_px) * _pnl_sign
                    break
            gross = tp1_pnl + remain_pnl
            teacher_net = gross - FEE
            teacher_total += teacher_net
            breakdown.append({
                'journal_id': e.get('id'),
                'entry': entry,
                'cost_basis': round(cost_px, 1),
                'v4_2_net': v42_net,
                'teacher_net': round(teacher_net, 2),
                'tp1_hit': tp1_hit,
                'tp1_px': tp1_px,
                'remain_outcome': '保本' if remain_pnl == 0 else ('持仓估' if not tp1_hit else '?'),
            })
        else:
            # TP1 未触, 跟 v4.2 一样走 SL
            teacher_total += v42_net  # 近似
            breakdown.append({
                'journal_id': e.get('id'),
                'entry': entry,
                'v4_2_net': v42_net,
                'teacher_net': v42_net,
                'tp1_hit': False,
                'note': 'TP1 未触, 与 v4.2 相同 (无差异可对照)',
            })
    return {
        'method': '老师 TP 体系 (50% 减半 + 推保本)',
        'entries_compared': len(breakdown),
        'v4_2_net_usd': round(v42_total, 2),
        'teacher_TP_net_usd': round(teacher_total, 2),
        'diff_usd': round(teacher_total - v42_total, 2),
        'note': '对照只算不算, 不污染 v4.2',
        'breakdown': breakdown,
        # ★ 2026-10-25: 显式暴露「已结算但 net 缺失」的单，不静默计 0
        'null_net_settlements': null_net,
        'null_net_count': len(null_net),
        # ★ 2026-11-06: 显式暴露「幽灵成交」（从未发生，不计入对照）
        'ghost_skipped': ghost_skipped,
        'ghost_skipped_count': len(ghost_skipped),
    }


# ---------- 主报告 ----------
def journal_entry_view(s: dict) -> dict:
    """journal 明细条目 → 展示视图（含三段式净值字段）。

    ★ 2026-11-05：净值三段式——账面 / 可信(fill_basis 优先) / 差额。
    `net_usd` 保留账面原值（审计线索），展示层取 `net_usd_effective`。

    ★ 2026-11-06：加 `ghost_fill` 标记 —— 该笔是「从未真正发生过的 hammer
    交易」（幽灵成交），**不计入统计**。账本 net_usd 保留（审计线索）。
    """
    return {
        'id': s.get('id'),
        'ts': s.get('ts'),
        'time_cst': datetime.fromtimestamp(s.get('ts', 0), CST).strftime('%H:%M'),
        'signal_source': s.get('signal_source'),
        'tp_mode': s.get('tp_mode'),
        'side': s.get('side'),
        'entry': s.get('entry'),
        'status': s.get('status'),
        'outcome': s.get('outcome'),
        'net_usd': s.get('net_usd'),
        'net_usd_book': _cbv.book_net(s) if s.get('net_usd') is not None else None,
        'net_usd_effective': _cbv.effective_net(s),
        'net_usd_fill_basis': s.get('net_usd_fill_basis'),
        'basis_diff': _cbv.basis_diff(s),
        'basis_stale': _cbv.is_stale(s),
        # ★ 2026-11-06：幽灵成交标记（从未发生 → 不计入统计）
        'ghost_fill': _cbv.is_ghost_fill(s),
    }


def build_report(date_str: str) -> dict:
    rows_15m, rows_30m, start, end = fetch_window(date_str)
    win_15m = filter_window(rows_15m, start, end)

    # 行情
    # ★ 2026-10-25 修：开盘价必须取第一根 15m 的 **open**（win_15m[0][1]），
    #   原来误用 win_15m[0][4]（第一根的 **close**）→ 开盘价/涨幅全错。
    if win_15m:
        first_c = win_15m[0][1]
        last_c = win_15m[-1][4]
        hi = max(r[2] for r in win_15m)
        lo = min(r[3] for r in win_15m)
        hi_t = datetime.fromtimestamp(next(r[0] for r in win_15m if r[2] == hi), CST).strftime('%H:%M')
        lo_t = datetime.fromtimestamp(next(r[0] for r in win_15m if r[3] == lo), CST).strftime('%H:%M')
        change = last_c - first_c
        if abs(change) < 200:
            trend = '震荡'
        elif change > 0:
            trend = '偏多'
        else:
            trend = '偏空'
        market = {
            'open_c': first_c,
            'close_c': last_c,
            'high': hi, 'high_time': hi_t,
            'low': lo, 'low_time': lo_t,
            'amplitude_pts': hi - lo,
            'amplitude_pct': (hi - lo) / first_c * 100,
            'change_pct': change / first_c * 100,
            'trend': trend,
            'n_15m_bars': len(win_15m),
        }
    else:
        market = {'n_15m_bars': 0, 'error': 'no data'}

    # 形态扫描
    patterns = scan_patterns(rows_15m, start, end)
    raw_patterns = scan_pattern_ungated(rows_15m, start, end)
    patterns_pass = [p for p in patterns if not p['30m_blocked']]
    # 2026-10-11 加: 7 种形态分布 + 反转成功率
    pattern_stats = scan_all_patterns_7kinds(rows_15m, start, end, forward_bars=4, abs_move_pts=50.0)

    # journal 窗口
    journal_window = load_journal_window(start, end)
    journal_15m = [s for s in journal_window if s.get('signal_source') == '15m_pattern']

    # 当前 score (实时, 不是昨天的; 只是给"今天"对比参考)
    real_score = {'note': '读不到历史 score, 当前仅实时'}

    return {
        'date': date_str,
        'window_cst': f"{date_str} 00:00 ~ {date_str} 23:59",
        'generated_at_cst': datetime.now(CST).strftime('%Y-%m-%d %H:%M:%S'),
        'market': market,
        'pattern_scan': {
            'raw_pattern_count': len(raw_patterns),
            'with_next_root_confirmed': len(patterns),
            'pass_30m_filter': len(patterns_pass),  # 这是"形态信号理论可触发数"
            'pass_list': patterns_pass,
            'raw_list': raw_patterns[:8],
            'note': ('触发后还需 1h score ≥ 4 + 形态 1 ≥ 5 守门才进 journal; '
                     '本报告只到"形态+后根+30m" 通过'),
        },
        # 2026-10-11 加: 7 种形态家族分布 + 反转成功率 (前瞻 4 根 / 50 点门槛)
        'pattern_distribution': pattern_stats,
        'journal_window': {
            'total_entries': len(journal_window),
            '15m_pattern_entries': len(journal_15m),
            'entries': [journal_entry_view(s) for s in journal_window],
        },
        'real_time_note': '当前 1h score 看 scan 输出; 昨天的历史 score 当前无法回放',
        'teacher_TP_comparison': simulate_teacher_TP(journal_window, rows_15m, start, end),
    }


def render_md(report: dict) -> str:
    """生成 Telegram 友好的 Markdown 卡片 (上限 ~30 行)"""
    m = report['market']
    p = report['pattern_scan']
    j = report['journal_window']
    lines = []
    lines.append(f"📊 <b>BTC 每日复盘 · {report['date']}</b>")
    lines.append('')
    # 行情
    if 'error' not in m:
        lines.append(f"<b>行情</b>  开 {m['open_c']:.0f}  收 {m['close_c']:.0f}  ({m['change_pct']:+.2f}%)")
        lines.append(f"  高 {m['high']:.0f}@{m['high_time']}  低 {m['low']:.0f}@{m['low_time']}  振幅 {m['amplitude_pct']:.2f}%")
        lines.append(f"  整体: {m['trend']}  ({m['n_15m_bars']} 根 15m)")
    else:
        lines.append('行情: ❌ 无数据')
    lines.append('')
    # 形态信号
    lines.append(f"<b>15m 形态信号 (理论可触发)</b>")
    lines.append(f"  原始形态: {p['raw_pattern_count']} 笔")
    lines.append(f"  +后根确认: {p['with_next_root_confirmed']} 笔")
    lines.append(f"  +30m 不逆势: <b>{p['pass_30m_filter']}</b> 笔")
    if p['pass_list']:
        for x in p['pass_list']:
            lines.append(f"    · {x['time_cst']} {x['kind_label']} @ {x['close']:.0f} (30m:{x['30m_trend']})")
    lines.append('')
    # journal
    lines.append(f"<b>实盘 journal (昨日窗口)</b>")
    lines.append(f"  总新写入: {j['total_entries']} 条")
    if j['15m_pattern_entries'] > 0:
        lines.append(f"  其中 15m_pattern: {j['15m_pattern_entries']} 条 (策略已实盘触发)")
    if j['total_entries'] == 0:
        lines.append(f"  → watch 没出信号 = 守门未放行")
    if j['entries']:
        # ★ 2026-10-25 修：原来只取前 5 条，10-24 的 23:15 那笔 -15.12U 正好被截掉，
        #   卡片上看不见亏捧行。改为窗口内全列（上限 10 条），并标注 net 缺失的单。
        for e in j['entries'][:10]:
            # ★ 2026-11-05 修：这里原来直接读账面 net_usd，和同卡「老师 TP 对照」
            # 的 fill_basis 口径（_cbv.net_of）不一致 → 同一张卡上同一笔单
            # 显示两个净值（09:00 那笔：账面 -14.80U vs 可信 -39.04U）。
            # 统一走 cost_basis_view.effective_net（fill_basis 优先，账面兜底）。
            net = _cbv.effective_net(e)
            if net is None:
                # ★ 2026-11-06 修：`cancelled` 的单（如 HAMMER_EXPIRED 监测窗过期）
                #   不是「持仓中」——它从未进场。分三档说清楚，别让用户以为还挂着单。
                st = e.get('status')
                if st == 'settled':
                    net_s = '⚠️ net 缺失'
                elif st == 'cancelled':
                    net_s = '已作废（未进场）'
                elif st == 'pending':
                    net_s = '持仓中'
                else:
                    net_s = st or '未知'
            else:
                net_s = f"{net:+.2f}U"
                # 口径陈旧的单（hammer 追价成交）→ 账面/可信差额单列，不藏
                if e.get('basis_stale'):
                    net_s += f" (账面 {e.get('net_usd_book'):+.2f}U)"
            # ★ 2026-11-06：幽灵成交（从未真正发生过的 hammer 交易）→ 标注且不计入统计
            if e.get('ghost_fill'):
                net_s = f"⚠️ 幽灵成交（从未发生，未计入统计）账面 {net_s}"
            lines.append(f"    · {e['time_cst']} {e.get('side', '?'):4} {e.get('id', '?')[-15:]} → {e.get('outcome', '?')} ({net_s})")
        if len(j['entries']) > 10:
            lines.append(f"    … 共 {len(j['entries'])} 条，仅列前 10")
    lines.append('')
    # 待关注
    followups = []
    if m.get('trend') == '偏多' and p['pass_30m_filter'] > 0 and any(x['kind'] == 'long' for x in p['pass_list']):
        followups.append('⚠️ 多头形态触发但主信号 score 低 → 看 1h score 是否迟到')
    if p['pass_30m_filter'] == 0 and m.get('change_pct', 0) is not None and abs(m.get('change_pct', 0)) > 1:
        followups.append('⚠️ 行情有波动但形态信号 0 触发 → 验证形态定义是否过严')
    if j['total_entries'] > 0 and j['15m_pattern_entries'] == 0:
        followups.append('ℹ️ 主信号触发但 15m 形态未触发 → 两源独立性 OK')
    # ★ 2026-11-06：幽灵成交预警（从未真正发生过的 hammer 交易）
    n_ghost = sum(1 for e in j['entries'] if e.get('ghost_fill'))
    if n_ghost:
        gnet = sum(float(e.get('net_usd') or 0) for e in j['entries'] if e.get('ghost_fill'))
        followups.append(
            f'⚠️ {n_ghost} 笔幽灵成交（hammer 信号被 promoter 抢跑，从未进场）'
            f'账面 {gnet:+.2f}U 已从统计剔除 —— 昨天实盘实际没有这些单'
        )
    # 2026-10-11 加: 7 种形态家族的赢家/输家提示
    pd = report.get('pattern_distribution', {})
    if pd and pd.get('by_kind'):
        # 找形态样本 ≥ 3 + bias 明确 + 成功率 < 35% 或 > 65% 的家族
        for k, v in pd['by_kind'].items():
            if v['count'] < 3 or v['bias'] is None:
                continue
            tot = v['long_total'] if v['bias'] == 'long' else v['short_total']
            succ = v['long_success'] if v['bias'] == 'long' else v['short_success']
            if tot == 0:
                continue
            rate = succ / tot
            if rate >= 0.65:
                followups.append(f'✅ {k} 反转成功率 {rate*100:.0f}% ({succ}/{tot}) — 该形态在这段时间有效')
            elif rate <= 0.35:
                followups.append(f'❌ {k} 反转成功率 {rate*100:.0f}% ({succ}/{tot}) — 该形态失效, 别押')
    if followups:
        lines.append('<b>待关注</b>')
        for f in followups:
            lines.append(f'  {f}')
    lines.append('')
    # 2026-10-11 加: 7 种形态分布 + 反转成功率 (形态家族热力)
    pd = report.get('pattern_distribution', {})
    if pd and pd.get('by_kind'):
        lines.append('<b>形态家族分布 · 反转成功率</b> (前瞻 1h / ≥50 点)')
        lines.append(f'  扫了 {pd["params"]["total_bars_scanned"]} 根 15m K 线')
        lines.append('')
        lines.append('  | 形态       | 偏 | 强度 | 触发 | 反转成功率')
        lines.append('  |------------|----|------|------|----------')
        # 按 count 降序
        sorted_kinds = sorted(pd['by_kind'].items(), key=lambda kv: -kv[1]['count'])
        for k, v in sorted_kinds:
            if v['count'] == 0:
                continue
            bias_str = {'long': '多', 'short': '空', None: '—'}.get(v['bias'], '?')
            tot = v['long_total'] if v['bias'] == 'long' else v['short_total']
            succ = v['long_success'] if v['bias'] == 'long' else v['short_success']
            if v['bias'] is None or tot == 0:
                rate_str = 'n/a (犹豫态)'
            else:
                rate_str = f'{succ}/{tot} = {succ/tot*100:.0f}%'
            lines.append(f'  | {k:10} | {bias_str} | {v["strength"]}★ | {v["count"]:4} | {rate_str}')
        lines.append('')
        # 时段分布
        if pd.get('by_hour'):
            lines.append('  <b>时段分布 (CST)</b>')
            for tag in ('00-08', '08-16', '16-24'):
                bucket = pd['by_hour'].get(tag, {})
                if any(bucket.values()):
                    parts = [f'{k}={v}' for k, v in bucket.items() if v > 0]
                    lines.append(f'    {tag}: {" · ".join(parts) if parts else "(无)"}')
            lines.append('')
        # top examples (取最多 5 个, 跳过 moving 不够的)
        if pd.get('examples'):
            lines.append('  <b>最强形态样本 (top 5)</b>')
            for ex in pd['examples'][:5]:
                success = ex['success_long'] if ex['bias'] == 'long' else (ex['success_short'] if ex['bias'] == 'short' else None)
                if success is None:
                    tag = '⚪犹豫'
                elif success:
                    tag = '✅反转成功'
                else:
                    tag = '❌未反转'
                arrows = ' '.join(f'{p:+.2f}%' for p in ex['future_high_pcts'][:4])
                lines.append(f'    · {ex["time_cst"]} {ex["kind"]} 偏{ex["bias"] or "?"} {tag} 后4根:{arrows}')
            lines.append('')
    # 老师 TP 对照 (非生产, 不动 v4.2 真值)
    ttp = report.get('teacher_TP_comparison', {})
    if ttp and 'v4_2_net_usd' in ttp:
        lines.append('')
        lines.append(f"<b>老师 TP 对照</b> (监控, 非生产)")
        lines.append(f"  对照笔数: {ttp['entries_compared']}")
        lines.append(f"  v4.2 净: {ttp['v4_2_net_usd']:+.2f}U")
        lines.append(f"  老师 TP 重算: {ttp['teacher_TP_net_usd']:+.2f}U")
        diff = ttp['diff_usd']
        # ★ 2026-11-08 修：对照笔数 0 时 diff 恒为 0，原来的 `diff > 0 else '✗'`
        #   会打出「差异: ✗ +0.00U」——看着像「对照失败」，实际是**没东西可比**。
        #   0 笔对照时标「—（无对照样本）」，差异符号只在真有样本时给。
        if ttp['entries_compared'] == 0:
            diff_arrow = '—（无对照样本）'
        else:
            diff_arrow = '✓' if diff > 0 else ('✗' if diff < 0 else '=')
        lines.append(f"  差异: {diff_arrow} {diff:+.2f}U")
        if ttp.get('breakdown'):
            for b in ttp['breakdown'][:3]:
                jid = (b.get('journal_id') or '?')[-15:]
                lines.append(f"    · {jid} entry={b.get('entry')} v4.2={b['v4_2_net']:+.2f} 老师={b['teacher_net']:+.2f} {b.get('remain_outcome', '')}")
        # ★ 2026-10-25：已结算但 net_usd 缺失的单必须显式列出，不许静默当 0
        nn = ttp.get('null_net_settlements') or []
        if nn:
            lines.append(f"  ⚠️ <b>{len(nn)} 笔已结算但 net_usd 缺失</b>（未计入 v4.2 合计）:")
            for x in nn:
                lines.append(f"    · {(x.get('journal_id') or '?')[-15:]} {x.get('time_cst')} "
                             f"{x.get('side')} → {x.get('outcome')} exit={x.get('exit_px')}")
        else:
            lines.append(f"  ✅ 已结算单 net_usd 全部齐全（0 笔缺失）")
        # ★ 2026-11-06：幽灵成交（从未发生）必须显式列出——它不是「缺 net」
        gs = ttp.get('ghost_skipped') or []
        if gs:
            lines.append(f"  ⚠️ <b>{len(gs)} 笔幽灵成交未参与对照</b>（从未真正进场，不计入 v4.2 合计）:")
            for x in gs:
                lines.append(f"    · {(x.get('journal_id') or '?')[-15:]} {x.get('time_cst')} "
                             f"{x.get('side')} → {x.get('outcome')} 账面 {x.get('net_usd')}")
        lines.append(f"  <i>{ttp['note']}</i>")
    
    lines.append(f'<i>数据时间: {report["generated_at_cst"]} CST</i>')
    return '\n'.join(lines)


# ---------- CLI ----------
def parse_date_arg(s: str) -> str:
    """支持 'yesterday' / 'today' / 'YYYY-MM-DD'"""
    if s in ('yesterday', 'yday'):
        d = (datetime.now(CST) - timedelta(days=1)).strftime('%Y-%m-%d')
        return d
    if s == 'today':
        return datetime.now(CST).strftime('%Y-%m-%d')
    # 校验格式
    datetime.strptime(s, '%Y-%m-%d')
    return s


def main():
    ap = argparse.ArgumentParser(description='BTC 每日复盘 (liusir 2026-10-10)')
    ap.add_argument('--date', default='yesterday', help='日期 (yesterday / today / YYYY-MM-DD)')
    ap.add_argument('--json', default=None, help='JSON 输出路径')
    args = ap.parse_args()

    date_str = parse_date_arg(args.date)
    report = build_report(date_str)
    md = render_md(report)

    json_path = args.json or str(REPORT_DIR / f"report-{date_str.replace('-', '')}.json")
    Path(json_path).write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(f'[saved] {json_path}')
    print()
    print(md)


if __name__ == '__main__':
    main()
