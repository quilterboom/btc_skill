#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
训练环境 · 数据修复 + 策略优化落地
===================================

2026-10-27 liusir：「分析亏损的策略原因，并提出优化方案，在训练环境进行优化和回测」

本脚本**只动训练环境**（/root/data/training/），不碰生产。

三步：

  第 1 步 修数据
    把「未成交却被记为已结算」和「已结算但 net_usd 缺失」的记录修好或标出来。
    依据：生产侧同一 bug 在 10-22 已手动修复过，note 里有完整的正确回放过程。

  第 2 步 重算战绩
    用修好后的口径重算训练环境战绩（赢 = net_usd > 0）。

  第 3 步 优化对照
    对每个候选规则跑 5 道验证（阈值敏感性 / 时间外推 / 方向分开 /
    被过滤组分布 / 集中度）。任一关不过 → 标记「不通过」，
    **不自动落地到生产**，只输出结论供 liusir 拍板。

用法：
    python3 train_env_optimize.py --dry-run     # 只报告，不改文件（默认）
    python3 train_env_optimize.py --apply       # 执行第 1 步的数据修复
    python3 train_env_optimize.py --json        # 机器可读输出
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

SKILL_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SKILL_ROOT / "scripts"))
# strategy_truth.py 在 web/ 目录（跟 web/server.py 同源）——
# 生产侧要用它的严格口径（剔双胞胎/出场被重写），必须显式加进去，
# 否则 import 失败退回宽松口径 → 生产样本 59 笔（web 同源口径是 48 笔）。
sys.path.insert(0, str(SKILL_ROOT / "web"))

TRAIN_JOURNAL = Path("/root/data/training/journal.jsonl")
PROD_JOURNAL = Path("/root/data/journal.jsonl")
BACKUP_DIR = Path("/root/data/training/_backups")


def read_journal(path: Path) -> list[dict]:
    out = []
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def write_journal(path: Path, recs: list[dict]) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in recs) + "\n",
                   encoding="utf-8")
    tmp.replace(path)


# ================================================================ 第 1 步：修数据
def classify(recs: list[dict]) -> dict:
    """把账本记录分成 4 类。

    A. 正常已结算      status=settled 且 net_usd 非空 且 fill_ts 非空
    B. 未成交的假结算  status=settled 且 net_usd 非空 但 fill_ts 为空
                       → 从统计中剔除；标记 data_quality=phantom_settlement
    C. 结算卡死        status=settled 但 net_usd 为空
                       → 标记 data_quality=settle_stuck，待人工/重放修复
    D. 未成交的作废    status=settled 且 net_usd==0 且 fill_ts 为空
                       → 这是 MISSED（正常语义），保留但标记以便区分
    """
    buckets = defaultdict(list)
    for r in recs:
        if r.get("status") != "settled":
            buckets["not_settled"].append(r)
            continue
        net = r.get("net_usd")
        filled = bool(r.get("fill_ts"))
        if net is None:
            buckets["stuck"].append(r)
        elif not filled:
            if abs(float(net)) < 1e-9:
                buckets["missed"].append(r)
            else:
                buckets["phantom"].append(r)
        else:
            buckets["ok"].append(r)
    return dict(buckets)


def repair(recs: list[dict]) -> tuple[list[dict], list[dict]]:
    """给异常记录打 data_quality 标记（不删除、不改净值 —— 净值由回放决定）。

    返回 (修好的记录, 修复动作列表)
    """
    out = []
    actions = []
    for r in recs:
        r = dict(r)
        if r.get("status") != "settled":
            out.append(r)
            continue
        net = r.get("net_usd")
        filled = bool(r.get("fill_ts"))
        if net is None:
            r["data_quality"] = "settle_stuck"
            r["note"] = (r.get("note") or "") + (
                " | [修复 2026-10-27] settled 但 net_usd 缺失 → 结算卡死，"
                "该笔不计入任何统计（生产同 bug 于 10-22 手动修复过）")
            actions.append({"id": r.get("id"), "action": "mark_settle_stuck",
                            "net_usd": None})
        elif not filled and abs(float(net)) > 1e-9:
            r["data_quality"] = "phantom_settlement"
            r["note"] = (r.get("note") or "") + (
                " | [修复 2026-10-27] 从未成交(fill_ts 为空)却被记为已结算，"
                "该笔不计入任何统计（_close 的 sanity check 对 fill_ts 为空不生效）")
            actions.append({"id": r.get("id"), "action": "mark_phantom",
                            "net_usd": net})
        elif not filled:
            r["data_quality"] = "missed_not_filled"
        out.append(r)
    return out, actions


# ================================================================ 第 2 步：重算战绩
def stats(rows: list[dict], label: str = "") -> dict:
    nets = [float(r["net_usd"]) for r in rows if r.get("net_usd") is not None]
    if not nets:
        return {"label": label, "n": 0, "net_usd": 0.0, "win_rate": 0.0}
    wins = [n for n in nets if n > 0]
    losses = [n for n in nets if n < 0]
    gp, gl = sum(wins), -sum(losses)
    return {
        "label": label,
        "n": len(nets),
        "net_usd": round(sum(nets), 2),
        "win_rate": round(len(wins) / len(nets) * 100, 1),
        "profit_factor": round(gp / gl, 2) if gl > 0 else (round(gp, 2) if gp > 0 else 0.0),
        "max_win": round(max(nets), 2),
        "max_loss": round(min(nets), 2),
    }


def clean_stats(recs: list[dict]) -> dict:
    """可信口径：只统计「真实成交且已结算」的记录。"""
    ok = [r for r in recs
          if r.get("status") == "settled"
          and r.get("net_usd") is not None
          and r.get("fill_ts")]
    by_side = defaultdict(list)
    by_src = defaultdict(list)
    for r in ok:
        by_side[r.get("side")].append(r)
        by_src[r.get("signal_source") or "(无来源)"].append(r)
    return {
        "total": stats(ok, "真实成交已结算"),
        "by_side": {k: stats(v, k) for k, v in sorted(by_side.items())},
        "by_source": {k: stats(v, k) for k, v in sorted(by_src.items())},
        "rows": ok,
    }


# ================================================================ 第 3 步：优化对照
def load_gates():
    """从 K 线算入场时刻可算的闸门特征（锚在 ts 之前最后一根已收线）。"""
    import numpy as np
    import kline_store
    from pathlib import Path as P

    data = P("/root/data")
    b4, _ = kline_store.load_bars(data, "BTC_USDT", "4h")
    arr4 = np.array([[int(r[0]), float(r[4])] for r in b4])

    def regime_7d(ts):
        i = int(np.searchsorted(arr4[:, 0], ts, side="right") - 1)
        if i < 42:
            return None
        return (float(arr4[i, 1]) / float(arr4[i - 42, 1]) - 1) * 100

    def vol_5d(ts):
        i = int(np.searchsorted(arr4[:, 0], ts, side="right") - 1)
        if i < 43:
            return None
        seg = arr4[i - 42:i + 1, 1]
        return float(np.std(np.diff(seg) / seg[:-1]) * 100)

    return regime_7d, vol_5d


def load_bars_15m():
    """15m K 线（本地缓存）。追单位置判定用。缓存不可用返回 None。"""
    try:
        import kline_store
        import gate_fetch
        from pathlib import Path as P
        rows, _depth = kline_store.load_bars(P("/root/data"), "BTC_USDT", "15m")
        return [(int(r[0]), float(r[1]), float(r[2]), float(r[3]),
                 float(r[4]), float(r[5])) for r in rows]
    except Exception:
        return None


def chase_pos(rec: dict, bars: list) -> float:
    """入场价在 24h 同向区间的位置（方向归一，跟 mistakes._detect_chase 同口径）。

    long: 0 = 贴 24h 高点（追高）  1 = 贴 24h 低点
    short: 0 = 贴 24h 低点（杀跌） 1 = 贴 24h 高点
    返回 None = K 线不足（覆盖不够宁可不判，不猜）。
    """
    if not bars:
        return None
    ts = rec.get("fill_ts") or rec.get("ts")
    entry = rec.get("entry")
    if ts is None or entry is None:
        return None
    ts, entry = float(ts), float(entry)
    win = [(float(b[2]), float(b[3])) for b in bars
           if (ts - 86400) <= float(b[0]) < ts]
    if len(win) < 20:
        return None
    hi = max(w[0] for w in win)
    lo = min(w[1] for w in win)
    rng = hi - lo
    if rng <= 0:
        return None
    if rec.get("side") == "long":
        return (hi - entry) / rng
    if rec.get("side") == "short":
        return (entry - lo) / rng
    return None


def build_rows(recs: list, r7f, v5f, bars) -> list:
    """把账本记录变成可验证的行（含各入场侧特征）。"""
    rows = []
    for r in recs:
        ts = r.get("ts", 0)
        sl_pct = r.get("sl_pct")
        tp1_pct = r.get("tp1_pct")
        drift = None
        if sl_pct and tp1_pct is not None and float(sl_pct) > 0:
            drift = abs(float(tp1_pct)) / float(sl_pct)
        tg = r.get("tags") or {}
        rows.append({
            "t": r, "net": float(r["net_usd"]), "side": r.get("side"), "ts": ts,
            "r7": r7f(ts), "v5": v5f(ts),
            "trend": tg.get("trend"), "drift": drift,
            "chase": chase_pos(r, bars),
            "cause": None,   # 亏损主因（生产侧填，训练侧用于对齐）
        })
    return rows


def derive_cause_candidates(prod_rows: list) -> list:
    """从**生产亏损原因**派生候选规则（2026-11-03 liusir 指令）。

    每条候选 = 一个「入场侧可执行过滤」，对应一种生产亏损主因。
    产出顺序按生产亏损笔数降序 —— 优化优先级 = 生产亏得最多的那个错因。

    返回 [(名称, 说明, 过滤函数, 关联错因)]。
    """
    from loss_causes_input import CAUSE_LEVERS

    # 生产各错因笔数（决定优先级）
    cnt = Counter()
    for r in prod_rows:
        if r.get("cause"):
            cnt[r["cause"]] += 1

    cands = []

    # ① 逆势入场（counter_trend）：账本 tags.trend 与 side 相反
    #
    # ★ 2026-11-03 重要修正：**「逆势」对不同策略含义不同**。
    #   hammer（left_side_oversold_bounce）本身就是「超跌反弹」= 逆势买入设计，
    #   它天生就在空头排列里做多 —— 所以对 hammer 策略用「逆势不入场」过滤，
    #   等于把整个策略滤光（实测训练 8 笔全被过滤，保留 0 笔）。
    #   反过来对 15m 形态（顺势突破）来说，逆势入场才是错误。
    #   → 规则必须**按信号源分别验证**，不能全样本一刀切。
    #   这里给 counter_trend 候选加 source_scope，验证时按来源分开。
    if cnt.get("COUNTER_TREND") or cnt.get("FULL_STOP"):
        def _f_counter_trend(r):
            side, trend = r.get("side"), r.get("trend")
            if not trend:
                return False
            return ((side == "long" and trend == "空头排列")
                    or (side == "short" and trend == "多头排列"))
        cands.append((
            "E 逆势不入场（counter_trend：与排列相反方向不开仓）",
            "生产亏损主因之一：逆势入场 + 方向错（止损打满）"
            "｜★ 注意：对 hammer 策略这是设计而非错误，保留组可能为 0",
            _f_counter_trend, "COUNTER_TREND"))
        cands.append((
            "F 逆势不入场 + 追单不入场（counter_trend + chase）",
            "方向过滤与入场时机过滤叠加"
            "｜★ 同上：hammer 策略本身逆势，这条会滤光整个策略",
            lambda r: (_f_counter_trend(r)
                       or (r.get("chase") is not None and r["chase"] < 0.2)),
            "COUNTER_TREND+CHASE_ENTRY"))

    # ② 追单入场（chase）：入场价贴近 24h 同向极值
    if cnt.get("CHASE_ENTRY"):
        cands.append((
            "G 追单不入场（chase：entry 距 24h 同向极值 < 20%）",
            "生产亏损主因之一：追高/杀跌入场",
            lambda r: r.get("chase") is not None and r["chase"] < 0.2,
            "CHASE_ENTRY"))

    # ③ 计划配置（plan_drift）：|TP1%| < SL% → R:R < 1，结构上必亏
    if cnt.get("PLAN_DRIFT"):
        cands.append((
            "H R:R < 1 不入场（plan_drift：|TP1%|/SL% < 1）",
            "生产亏损主因之一：止盈比止损还近",
            lambda r: r.get("drift") is not None and r["drift"] < 1.0,
            "PLAN_DRIFT"))

    return cands


def attach_prod_causes(prod_rows: list, bars) -> None:
    """给生产行标上亏损主因（原地写 rows[i]["cause"]）。"""
    from mistakes import classify_loss_cause
    for r in prod_rows:
        rec = r["t"]
        net = rec.get("net_usd")
        if net is None or float(net) >= 0:
            continue
        r["cause"] = (classify_loss_cause(rec, bars) or ["OTHER"])[0]


def _cross_conflict(res: dict) -> dict:
    """双样本一致性检查：训练保留组 vs 生产保留组的净利方向是否相反。

    训练样本小（9 笔）、生产样本大（48 笔）。如果一条规则在生产上
    明显改善、在训练上把净利推向反面 —— 那多半是训练样本太小的噪声，
    **不能上线**。反之训练改善、生产反向同理。
    """
    out = {"conflict": False, "train_delta": None, "prod_delta": None}
    tr = res.get("keep", {})
    full = res.get("full", {})
    if not tr or not full:
        return out
    tr_delta = tr.get("net_usd", 0) - full.get("net_usd", 0)
    out["train_delta"] = round(tr_delta, 2)

    pr = res.get("prod") or {}
    if "keep" in pr and "full" in pr:
        p_delta = pr["keep"].get("net_usd", 0) - pr["full"].get("net_usd", 0)
        out["prod_delta"] = round(p_delta, 2)
        # 两边都有效（delta > 0）不算冲突；一边正向一边负向 = 冲突
        if tr_delta * p_delta < 0:
            out["conflict"] = True
    return out


def validate(rows: list[dict], name: str, pred) -> dict:
    """5 道验证。pred(row) -> True = 过滤掉。"""
    keep = [r for r in rows if not pred(r)]
    drop = [r for r in rows if pred(r)]
    res = {"name": name, "full": stats([r["t"] for r in rows], "全样本"),
           "keep": stats([r["t"] for r in keep], "保留"),
           "drop": stats([r["t"] for r in drop], "过滤")}

    srt = sorted(rows, key=lambda r: r["ts"])
    mid = len(srt) // 2
    res["time_extrapolation"] = []
    for lbl, seg in (("前半", srt[:mid]), ("后半", srt[mid:])):
        res["time_extrapolation"].append({
            "label": lbl,
            "all": stats([r["t"] for r in seg], f"{lbl}全样本"),
            "keep": stats([r["t"] for r in seg if not pred(r)], f"{lbl}保留"),
            "drop": stats([r["t"] for r in seg if pred(r)], f"{lbl}过滤"),
        })

    res["by_side"] = []
    for side in ("long", "short"):
        seg = [r for r in rows if r["side"] == side]
        res["by_side"].append({
            "side": side,
            "all": stats([r["t"] for r in seg], f"{side}全样本"),
            "keep": stats([r["t"] for r in seg if not pred(r)], f"{side}保留"),
            "drop": stats([r["t"] for r in seg if pred(r)], f"{side}过滤"),
        })

    res["dropped_profile"] = {
        "n": len(drop),
        "by_side": dict(Counter(r["side"] for r in drop)),
        "by_source": dict(Counter(r["t"].get("signal_source") or "(无来源)" for r in drop)),
    }

    if drop:
        nets = sorted(r["net"] for r in drop)
        tot = sum(nets)
        worst = min(nets)
        without = sum(n for n in nets if n != worst)
        res["concentration"] = {
            "net": round(tot, 2), "worst": round(worst, 2),
            "worst_share_pct": round(abs(worst) / abs(tot) * 100, 1) if tot else 0.0,
            "without_worst": round(without, 2),
            "sign_holds": (without < 0) == (tot < 0),
        }
    else:
        res["concentration"] = None

    reasons = []
    if not drop:
        reasons.append("不拦任何单")
    else:
        # ★ 2026-11-03 新增关1「策略保留」：过滤掉 ≥90% 的样本 = 滤光整个策略，
        #   不是「优化」。hammer 这类逆势策略尤其容易踩：
        #   「逆势不入场」会把超跌反弹的单全滤掉（实测训练 8 笔 → 保留 0 笔）。
        #   滤光 = 该规则对这个策略不适用，不是「有效」。
        if len(rows) >= 5 and len(keep) / len(rows) < 0.10:
            reasons.append(
                "关1 策略保留失败（保留 %d/%d 笔 = %.0f%%，滤光整个策略，"
                "该规则与策略设计相悖）" % (len(keep), len(rows),
                                        len(keep) / len(rows) * 100))
        for h in res["time_extrapolation"]:
            if h["drop"]["n"] and h["keep"]["net_usd"] <= h["all"]["net_usd"]:
                reasons.append(f"关2 时间外推失败（{h['label']}保留未改善）")
        for s in res["by_side"]:
            if s["drop"]["n"] and s["keep"]["net_usd"] < s["all"]["net_usd"]:
                reasons.append(f"关3 方向分开失败（{s['side']}保留恶化）")
        c = res["concentration"]
        if c and not c["sign_holds"]:
            reasons.append("关5 集中度失败（去掉最差一笔后符号翻转）")
    res["passed"] = not reasons
    res["fail_reasons"] = reasons
    return res


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="执行数据修复（写回训练账本）")
    ap.add_argument("--dry-run", action="store_true", help="只报告不写（默认行为）")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    recs = read_journal(TRAIN_JOURNAL)
    prod = read_journal(PROD_JOURNAL)
    before = classify(recs)
    fixed, actions = repair(recs)
    after = classify(fixed)

    clean = clean_stats(fixed)
    # ★ 2026-11-03：生产侧改用 strategy_truth 严格口径（剔双胞胎 / 出场被重写 /
    #   未成交），与 web 胜率卡、错误归因卡同源。
    #   原来用宽松口径（status=settled + net 非空 + fill_ts）→ 59 笔，
    #   而 web 同源口径是 48 笔 —— 同一份报告里两个生产样本数，对不上账。
    #   宽松口径会把双胞胎当独立样本，重复计入 PnL。
    prod_truth = _prod_truth_records()
    prod_clean = clean_stats(prod_truth)

    r7f, v5f = load_gates()
    bars = load_bars_15m()

    # ---- 训练同源样本（候选规则验证环境）----
    rows = build_rows(clean["rows"], r7f, v5f, bars)

    # ---- 生产样本（亏损原因 = 优化输入；生产账本只读）----
    prod_rows = build_rows(prod_clean["rows"], r7f, v5f, bars)
    attach_prod_causes(prod_rows, bars)

    cands = [
        ("A 只做多（long_only）",
         lambda r: r["side"] == "short", None),
        ("B 做空且 5 日波动 < 0.65% 不开仓（vol_min_pct，v2_journal 口径）",
         lambda r: r["side"] == "short" and r["v5"] is not None and r["v5"] < 0.65, None),
        ("C 做多且 7 日涨幅 > 0% 不开仓（regime_max_7d）",
         lambda r: r["side"] == "long" and r["r7"] is not None and r["r7"] > 0.0, None),
        ("D A + B（只做多 + 做空需高波动）",
         lambda r: r["side"] == "short" and (r["v5"] is None or r["v5"] < 0.65), None),
    ]
    # ★ 2026-11-03：E/F/G/H 由**生产亏损原因**派生（liusir 指令：
    #   「训练环境的策略调整输入还要包括生产环境亏损策略的原因」）
    for nm, desc, pd, cause in derive_cause_candidates(prod_rows):
        cands.append((nm + "｜" + desc, pd, cause))

    results = []
    for nm, pd, cause in cands:
        try:
            res = validate(rows, nm, pd)
            res["cause"] = cause
            # ★ 生产样本对照（2026-11-03）：候选规则在生产账本上的效果。
            #   生产样本量更大（48 笔可信 vs 训练 9 笔），且亏损原因就是从
            #   生产来的 —— 所以「生产上有效 + 训练上不反向」才算过。
            #   生产只读，不改任何生产文件。
            try:
                res["prod"] = validate(prod_rows, nm + "（生产对照）", pd)
            except Exception as e:
                res["prod"] = {"error": str(e)}
            # 双样本一致性：训练保留组净利方向不许跟生产相反
            res["cross_sample_conflict"] = _cross_conflict(res)
            results.append(res)
        except Exception as e:
            results.append({"name": nm, "error": str(e), "passed": False,
                            "cause": cause})

    out = {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "applied": False,
        "before": {k: len(v) for k, v in before.items()},
        "after": {k: len(v) for k, v in after.items()},
        "actions": actions,
        "train_clean": clean["total"],
        "train_by_side": clean["by_side"],
        "train_by_source": clean["by_source"],
        "prod_clean": prod_clean["total"],
        "sample_note": "训练真实成交样本 %d 笔；< 30 笔 → 所有规则结论均需标注「样本不足」" % len(rows),
        "candidates": results,
    }

    if args.apply and actions:
        BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        bak = BACKUP_DIR / ("journal.jsonl.bak-" + stamp)
        shutil.copy2(TRAIN_JOURNAL, bak)
        write_journal(TRAIN_JOURNAL, fixed)
        out["applied"] = True
        out["backup"] = str(bak)

    if args.json:
        out["train_rows"] = [{k: v for k, v in r.items() if k != "t"} for r in rows]
        print(json.dumps(out, ensure_ascii=False, indent=1))
        return

    print("=" * 96)
    print("训练环境 · 数据修复 + 优化对照" + ("（已应用）" if out["applied"] else "（试运行，未改文件）"))
    print("=" * 96)
    print("修复前记录分类:", out["before"])
    print("修复后记录分类:", out["after"])
    if actions:
        print("修复动作 %d 条:" % len(actions))
        for a in actions:
            print("    %-32s %-22s net_usd=%s" % (a["id"], a["action"], a["net_usd"]))
    else:
        print("无需修复。")
    print()
    print("训练环境 · 可信口径战绩（真实成交 + 已结算）：%s" % _fmt(clean["total"]))
    for k, v in clean["by_side"].items():
        print("    按方向 %-8s %s" % (k, _fmt(v)))
    for k, v in clean["by_source"].items():
        print("    按来源 %-24s %s" % (k, _fmt(v)))
    print()
    print("生产环境 · 同口径：%s" % _fmt(prod_clean["total"]))
    print()
    # ★ 2026-11-03：生产亏损原因 = 训练策略调整的输入（liusir 指令）
    print("=" * 96)
    print("生产亏损原因 · 策略调整输入（生产账本只读）")
    print("=" * 96)
    print(_loss_cause_block(prod_rows, bars))
    print()
    print("=" * 96)
    print("候选规则 · 5 道验证（%s）" % out["sample_note"])
    print("=" * 96)
    for r in results:
        if "error" in r:
            print("\n%s → 错误: %s" % (r["name"], r["error"]))
            continue
        print("\n%s" % r["name"])
        print("    全样本 %s" % _fmt(r["full"]))
        print("    保留   %s" % _fmt(r["keep"]))
        print("    过滤   %s" % _fmt(r["drop"]))
        for h in r["time_extrapolation"]:
            print("    关2 %s 全样本 %s / 保留 %s / 过滤 %s" % (
                h["label"], _fmt(h["all"]), _fmt(h["keep"]), _fmt(h["drop"])))
        for s in r["by_side"]:
            print("    关3 %-5s 全样本 %s / 保留 %s / 过滤 %s" % (
                s["side"], _fmt(s["all"]), _fmt(s["keep"]), _fmt(s["drop"])))
        dp = r["dropped_profile"]
        print("    关4 被过滤 %d 笔，方向 %s，来源 %s" % (dp["n"], dp["by_side"], dp["by_source"]))
        c = r["concentration"]
        if c:
            print("    关5 集中度：净 %+.2f，最差 %+.2f（占 %.0f%%），去最差 %+.2f，符号成立=%s" % (
                c["net"], c["worst"], c["worst_share_pct"], c["without_worst"],
                "是" if c["sign_holds"] else "否"))
        # ★ 生产对照（2026-11-03）：同一规则在生产账本上的效果
        pr = r.get("prod") or {}
        if "keep" in pr:
            print("    ── 生产对照（%d 笔可信样本，只读）" % pr.get("full", {}).get("n", 0))
            print("       全样本 %s" % _fmt(pr.get("full", {})))
            print("       保留   %s" % _fmt(pr.get("keep", {})))
            print("       过滤   %s" % _fmt(pr.get("drop", {})))
        cc = r.get("cross_sample_conflict") or {}
        if cc.get("conflict"):
            print("    ★ 双样本冲突：训练保留组 Δ%+.2fU vs 生产保留组 Δ%+.2fU（方向相反，不可上线）" % (
                cc.get("train_delta") or 0.0, cc.get("prod_delta") or 0.0))
        elif cc.get("prod_delta") is not None:
            print("    双样本：训练 Δ%+.2fU ｜ 生产 Δ%+.2fU（方向一致）" % (
                cc.get("train_delta") or 0.0, cc.get("prod_delta") or 0.0))
        print("    ⇒ %s" % ("通过" if r["passed"] else "不通过：" + "；".join(r["fail_reasons"])))
    print()
    if not args.apply:
        print("提示：加 --apply 执行第 1 步的数据修复（会自动备份账本）。")


def _loss_cause_block(prod_rows: list, bars) -> str:
    """生产亏损原因摘要（错因分布 + 优化杠杆优先级）。"""
    from mistakes import CAUSE_TYPES
    from loss_causes_input import CAUSE_LEVERS

    losses = [r for r in prod_rows if r.get("cause")]
    if not losses:
        return "  （生产账本无亏损记录）"
    cnt = Counter()
    nets = defaultdict(float)
    for r in losses:
        cnt[r["cause"]] += 1
        nets[r["cause"]] += r["net"]
    n = len(losses)

    lines = ["  亏损 %d 笔，归因（互斥，每笔一个主因）：" % n]
    for c, k in cnt.most_common():
        meta = CAUSE_LEVERS.get(c, CAUSE_LEVERS["OTHER"])
        lines.append("    %-16s %2d 笔 %5.1f%%  %+7.2fU  → %s" % (
            CAUSE_TYPES.get(c, c), k, k / n * 100, nets[c], meta["lever"]))
    lines.append("")
    lines.append("  优化杠杆优先级（按生产亏损笔数）：")
    for c, k in cnt.most_common():
        meta = CAUSE_LEVERS.get(c, CAUSE_LEVERS["OTHER"])
        lines.append("    %-16s %2d 笔 · %s" % (CAUSE_TYPES.get(c, c), k, meta["lever"]))
        if meta.get("note"):
            lines.append("        %s" % meta["note"])
        lines.append("        入场侧可执行过滤: %s" % (meta.get("entry_filter") or "无（出场侧问题）"))
    return "\n".join(lines)


def _prod_truth_records() -> list:
    """生产可信记录（strategy_truth 严格口径，与 web 胜率卡同源）。

    2026-11-03：train_env_optimize 原用宽松口径（status=settled + net 非空 +
    fill_ts）→ 59 笔，而 web 同源口径 48 笔 —— 同一份报告里两个生产样本数。
    宽松口径把双胞胎当独立样本，PnL 重复计入。
    """
    try:
        import strategy_truth as st
        recs, _pending, _excl = st.truth_records(PROD_JOURNAL)
        return recs
    except Exception:
        return read_journal(PROD_JOURNAL)


def _fmt(s: dict) -> str:
    if not s or not s.get("n"):
        return "0 笔"
    return "%2d 笔 | 净 %+8.2fU | 胜率 %5.1f%% | 盈亏比 %s | 最大亏 %+.2f" % (
        s["n"], s["net_usd"], s["win_rate"],
        ("%.2f" % s["profit_factor"]) if s.get("profit_factor") is not None else "-",
        s.get("max_loss", 0),
    )


if __name__ == "__main__":
    main()
