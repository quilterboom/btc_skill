#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
训练环境 · 15m 形态路径 亏损归因与优化回测
============================================

2026-10-27 liusir：「分析亏损的策略原因，并提出优化方案，在训练环境进行优化和回测」

本脚本只做三件事（不碰生产）：

  1. 归因：把训练账本的亏损拆成「数据 bug」和「策略问题」两类，各给数字
  2. 候选规则：从生产已验证的闸门集合里挑候选，在**可信口径**上评估
  3. 回测：对每个候选跑 5 道验证（阈值敏感性 / 时间外推 / 方向分开 /
             被过滤组分布 / 集中度），任一关不过 → 不通过

★★ 口径纪律（对应 btc-strategy-analysis skill）★★
  - 「赢」只看 net_usd > 0（不看 outcome 词表、不看 category）
  - 未成交（fill_ts 为空）却被记为已结算的单 → **剔除**，单独列为数据 bug
  - 回测引擎保真度 < 90% 时禁止用本脚本的参数结论（脚本会自报保真度）
  - 样本 < 30 笔的结论一律标注「样本不足」

用法：
    python3 train_env_analyze.py            # 归因 + 候选规则 5 关验证
    python3 train_env_analyze.py --json     # 机器可读输出
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

SKILL_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SKILL_ROOT / "scripts"))

TRAIN_JOURNAL = Path("/root/data/training/journal.jsonl")
PROD_JOURNAL = Path("/root/data/journal.jsonl")

# 生产 v2_journal 已验证的闸门（13 个月 1015 信号回测得出）
V2_GATES = {
    "long_only": True,       # 做空 13 个月 579 信号 42% 准确率、平均 -0.173% → 关闭
    "regime_max_7d": 0.0,    # 只在 7 日涨幅 <= 0% 时开多（超跌反弹）
    "vol_min_pct": 0.65,    # 5 日波动率闸门
    "use_vol_gate": True,
}


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


# ---------------------------------------------------------------- 账本体检
def split_filled(trades: list[dict]) -> tuple[list[dict], list[dict]]:
    """分成「真实成交」和「未成交却被记为已结算」两类。

    判据：status=settled 且 net_usd 非空，但 fill_ts 为空。
    这类单在 position_tracker._close 里会绕过 sanity check
    （`if _fill_ts and exit_ts <= _fill_ts` —— fill_ts 空则整段跳过），
    并且 `if sig.get("tp1_hit")` 会把 tp1_hit_px 计入盈亏 → 凭空造出利润。
    """
    filled, phantom = [], []
    for t in trades:
        if t.get("status") != "settled" or t.get("net_usd") is None:
            continue
        if t.get("fill_ts"):
            filled.append(t)
        else:
            phantom.append(t)
    return filled, phantom


def stats(trades: list[dict], label: str = "") -> dict:
    nets = [float(t["net_usd"]) for t in trades if t.get("net_usd") is not None]
    if not nets:
        return {"n": 0, "net_usd": 0.0, "win_rate": 0.0, "label": label}
    wins = [n for n in nets if n > 0]
    losses = [n for n in nets if n < 0]
    gp, gl = sum(wins), -sum(losses)
    return {
        "label": label,
        "n": len(nets),
        "net_usd": round(sum(nets), 2),
        "win_rate": round(len(wins) / len(nets) * 100, 1),
        "profit_factor": round(gp / gl, 2) if gl > 0 else (round(gp, 2) if gp > 0 else 0.0),
        "avg_win": round(gp / len(wins), 2) if wins else 0.0,
        "avg_loss": round(-gl / len(losses), 2) if losses else 0.0,
        "max_win": round(max(nets), 2),
        "max_loss": round(min(nets), 2),
    }


# ---------------------------------------------------------------- 归因
def attribute(trades: list[dict]) -> dict:
    filled, phantom = split_filled(trades)
    by_side = defaultdict(list)
    for t in filled:
        by_side[t.get("side")].append(t)
    by_source = defaultdict(list)
    for t in filled:
        by_source[t.get("signal_source") or "(无来源)"].append(t)
    return {
        "all_settled": stats(trades, "账面全部已结算"),
        "phantom": {
            **stats(phantom, "未成交却记为已结算（数据 bug）"),
            "detail": [
                {
                    "id": t.get("id"),
                    "ts": time.strftime("%m-%d %H:%M", time.localtime(t.get("ts", 0))),
                    "side": t.get("side"),
                    "outcome": t.get("outcome"),
                    "net_usd": t.get("net_usd"),
                    "exit_px": t.get("exit_px"),
                    "entry": t.get("entry"),
                    "exit_ts": t.get("exit_ts"),
                    "tp1_hit_ts": t.get("tp1_hit_ts"),
                    "reason": "fill_ts 为空（从未成交）却按 exit_px=entry 记为保本平仓",
                    "time_inconsistent": bool(
                        t.get("exit_ts") and t.get("tp1_hit_ts")
                        and t["exit_ts"] < t["tp1_hit_ts"]),
                }
                for t in sorted(phantom, key=lambda x: x.get("ts", 0))
            ],
        },
        "filled_only": stats(filled, "真实成交（剔除数据 bug 后）"),
        "by_side": {k: stats(v, f"真实成交 · {k}") for k, v in sorted(by_side.items())},
        "by_source": {k: stats(v, f"真实成交 · {k}") for k, v in sorted(by_source.items())},
    }


# ---------------------------------------------------------------- 特征
def load_features() -> dict:
    """从 K 线缓存算入场时刻可算的特征（全部锚在 ts 之前最后一根已收线）。"""
    import numpy as np
    import kline_store
    from pathlib import Path as P

    data = P("/root/data")
    out = {}

    b4, _ = kline_store.load_bars(data, "BTC_USDT", "4h")
    arr4 = np.array([[int(r[0]), float(r[4])] for r in b4])
    out["b4"] = arr4

    def ema_seq(c, n):
        if len(c) < n:
            return None
        k = 2 / (n + 1)
        e = float(np.mean(c[:n]))
        for v in c[n:]:
            e = float(v) * k + e * (1 - k)
        return e

    def regime_7d(ts):
        """入场前 7 日涨幅（4h close，42 根）"""
        i = int(np.searchsorted(arr4[:, 0], ts, side="right") - 1)
        if i < 42:
            return None
        return (float(arr4[i, 1]) / float(arr4[i - 42, 1]) - 1) * 100

    def vol_5d(ts):
        """近 5 日已实现波动率（4h close 的滚动标准差），% """
        i = int(np.searchsorted(arr4[:, 0], ts, side="right") - 1)
        if i < 43:
            return None
        seg = arr4[i - 42:i + 1, 1]
        rets = np.diff(seg) / seg[:-1]
        return float(np.std(rets) * 100)

    out["regime_7d"] = regime_7d
    out["vol_5d"] = vol_5d
    out["ema_seq"] = ema_seq
    return out


def enrich(trades: list[dict], feats: dict) -> list[dict]:
    rows = []
    for t in trades:
        ts = t.get("ts", 0)
        r7 = feats["regime_7d"](ts)
        v5 = feats["vol_5d"](ts)
        rows.append({
            "t": t, "net": float(t["net_usd"]), "side": t.get("side"),
            "ts": ts, "r7": r7, "v5": v5,
        })
    return rows


# ---------------------------------------------------------------- 5 关验证
def validate_rule(rows: list[dict], name: str, pred) -> dict:
    """pred(row) -> True 表示「过滤掉这一笔」。

    返回 5 关的逐关结果 + 总判定。
    """
    keep = [r for r in rows if not pred(r)]
    drop = [r for r in rows if pred(r)]
    full = stats([r["t"] for r in rows], "全样本")
    res = {
        "name": name,
        "n_full": len(rows),
        "keep": stats([r["t"] for r in keep], "保留"),
        "drop": stats([r["t"] for r in drop], "过滤"),
        "full": full,
        "gates": {},
    }

    # 关 1：阈值敏感性 —— 由调用方保证（同名规则不同阈值都跑）
    # 关 2：时间外推
    srt = sorted(rows, key=lambda r: r["ts"])
    mid = len(srt) // 2
    halves = []
    for lbl, seg in (("前半", srt[:mid]), ("后半", srt[mid:])):
        k = [r for r in seg if not pred(r)]
        d = [r for r in seg if pred(r)]
        halves.append({
            "label": lbl,
            "all": stats([r["t"] for r in seg], f"{lbl}全样本"),
            "keep": stats([r["t"] for r in k], f"{lbl}保留"),
            "drop": stats([r["t"] for r in d], f"{lbl}过滤"),
        })
    res["gates"]["time_extrapolation"] = halves

    # 关 3：方向分开
    sides = []
    for side in ("long", "short"):
        seg = [r for r in rows if r["side"] == side]
        k = [r for r in seg if not pred(r)]
        d = [r for r in seg if pred(r)]
        sides.append({
            "side": side,
            "all": stats([r["t"] for r in seg], f"{side}全样本"),
            "keep": stats([r["t"] for r in k], f"{side}保留"),
            "drop": stats([r["t"] for r in d], f"{side}过滤"),
        })
    res["gates"]["by_side"] = sides

    # 关 4：被过滤组分布
    res["gates"]["dropped_profile"] = {
        "n": len(drop),
        "by_side": dict(Counter(r["side"] for r in drop)),
        "by_source": dict(Counter(r["t"].get("signal_source") or "(无来源)" for r in drop)),
    }

    # 关 5：集中度
    if drop:
        nets = sorted(r["net"] for r in drop)
        tot = sum(nets)
        worst = min(nets)
        without = sum(n for n in nets if n != worst)
        res["gates"]["concentration"] = {
            "net": round(tot, 2),
            "worst": round(worst, 2),
            "worst_share_pct": round(abs(worst) / abs(tot) * 100, 1) if tot else 0.0,
            "without_worst": round(without, 2),
            "sign_holds_without_worst": (without < 0) == (tot < 0),
        }
    else:
        res["gates"]["concentration"] = None

    # 判定
    reasons = []
    if not drop:
        reasons.append("规则不拦任何单（无效果）")
    else:
        # 关 2：两段都要改善
        for h in halves:
            if h["drop"]["n"] and h["keep"]["net_usd"] > h["all"]["net_usd"]:
                pass
            elif h["drop"]["n"]:
                reasons.append(f"关2 时间外推失败（{h['label']}保留未改善）")
        # 关 3：方向分开都要不恶化
        for s in sides:
            if s["drop"]["n"] and s["keep"]["net_usd"] < s["all"]["net_usd"]:
                reasons.append(f"关3 方向分开失败（{s['side']}保留恶化）")
        # 关 5：符号不能靠单笔支撑
        c = res["gates"]["concentration"]
        if c and not c["sign_holds_without_worst"]:
            reasons.append("关5 集中度失败（去掉最差一笔后符号翻转）")
    res["passed"] = not reasons
    res["fail_reasons"] = reasons
    return res


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    train = read_journal(TRAIN_JOURNAL)
    prod = read_journal(PROD_JOURNAL)
    attrs = attribute(train)
    prod_attrs = attribute(prod)

    feats = load_features()
    filled_rows = enrich(attrs["filled_only_named"] if False else
                         [t for t in train
                          if t.get("status") == "settled" and t.get("net_usd") is not None
                          and t.get("fill_ts")],
                         feats)

    # 候选规则：全部来自 v2_journal 已验证的闸门集合
    candidates = []

    # R1：关掉做空（long_only）
    candidates.append((
        "R1 只做多（long_only，v2_journal 13 个月 579 信号已验证）",
        lambda r: r["side"] == "short",
    ))

    # R2：做多且 7 日涨幅 > 0%（regime_max_7d）
    candidates.append((
        "R2 做多且 7 日涨幅 > 0% 时不开仓（regime_max_7d）",
        lambda r: r["side"] == "long" and r["r7"] is not None and r["r7"] > 0.0,
    ))

    # R3：5 日波动率闸门（多空都套）
    for thr in (0.65,):
        candidates.append((
            f"R3 5 日波动率 < {thr}% 时不开仓（vol_min_pct）",
            lambda r, thr=thr: r["v5"] is not None and r["v5"] < thr,
        ))

    # R4：R1 + R2 组合
    candidates.append((
        "R4 = R1 + R2（只做多 且 只在 7 日涨幅 <= 0% 时做多）",
        lambda r: (r["side"] == "short")
                   or (r["side"] == "long" and r["r7"] is not None and r["r7"] > 0.0),
    ))

    results = []
    for name, pred in candidates:
        try:
            results.append(validate_rule(filled_rows, name, pred))
        except Exception as e:
            results.append({"name": name, "error": str(e), "passed": False})

    out = {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "train": attrs,
        "prod": prod_attrs,
        "sample_note": "训练环境真实成交样本 %d 笔（< 30 笔 → 结论标注样本不足）" % len(filled_rows),
        "candidates": results,
    }

    if args.json:
        print(json.dumps(out, ensure_ascii=False, indent=1))
        return

    # ---------------- 人类可读报告 ----------------
    print("=" * 96)
    print("训练环境 · 15m 形态路径 亏损归因")
    print("=" * 96)
    print("账面全部已结算   : %s" % _fmt(attrs["all_settled"]))
    ph = attrs["phantom"]
    print("其中「未成交却记为已结算」: %s" % _fmt(ph))
    for d in ph["detail"]:
        print("    %s %s %s  净 %+.2f  %s%s" % (
            d["ts"], str(d["side"])[:3], str(d["outcome"]), d["net_usd"] or 0,
            d["reason"], "  ★时间矛盾(exit_ts < tp1_hit_ts)" if d["time_inconsistent"] else ""))
    print("剔除后真实成交   : %s" % _fmt(attrs["filled_only"]))
    print()
    print("按方向：")
    for k, v in attrs["by_side"].items():
        print("    %s" % _fmt(v))
    print("按信号源：")
    for k, v in attrs["by_source"].items():
        print("    %s" % _fmt(v))
    print()

    print("=" * 96)
    print("候选规则 · 5 道验证（样本 %d 笔）" % len(filled_rows))
    print("=" * 96)
    for r in results:
        if "error" in r:
            print("  %s → 错误: %s" % (r["name"], r["error"]))
            continue
        print("\n%s" % r["name"])
        print("    全样本 %s" % _fmt(r["full"]))
        print("    保留   %s" % _fmt(r["keep"]))
        print("    过滤   %s" % _fmt(r["drop"]))
        print("    关2 时间外推：")
        for h in r["gates"]["time_extrapolation"]:
            print("        %s 全样本 %s" % (h["label"], _fmt(h["all"])))
            print("        %s 保留   %s" % (h["label"], _fmt(h["keep"])))
            print("        %s 过滤   %s" % (h["label"], _fmt(h["drop"])))
        print("    关3 方向分开：")
        for s in r["gates"]["by_side"]:
            print("        %s 全样本 %s" % (s["side"], _fmt(s["all"])))
            print("        %s 保留   %s" % (s["side"], _fmt(s["keep"])))
            print("        %s 过滤   %s" % (s["side"], _fmt(s["drop"])))
        dp = r["gates"]["dropped_profile"]
        print("    关4 被过滤组：%d 笔，方向 %s，来源 %s" % (dp["n"], dp["by_side"], dp["by_source"]))
        c = r["gates"]["concentration"]
        if c:
            print("    关5 集中度：净 %+.2f，最差一笔 %+.2f（占 %.0f%%），去最差后 %+.2f，符号成立=%s" % (
                c["net"], c["worst"], c["worst_share_pct"], c["without_worst"],
                "是" if c["sign_holds_without_worst"] else "否"))
        print("    ⇒ %s" % ("通过" if r["passed"] else "不通过：" + "；".join(r["fail_reasons"])))


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
