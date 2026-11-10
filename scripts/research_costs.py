#!/usr/bin/env python3
"""研究结果的成本诊断：成本占多少、资金费占多少（预注册 3.5 节第 2 条"只做诊断"）。只读一个研究结果目录，不跑回测、不改判定。

用法（VPS 上，研究跑完以后）：
  .venv/bin/python scripts/research_costs.py --run-dir user_data/research/run_<时间>
  .venv/bin/python scripts/research_costs.py --run-dir user_data/research/run_<时间> --strategies "PremiumFade"

每个策略、每一段（默认参数；训练段、没看过的一段、样本外、走步各折和合计、2020 留出段）逐笔算：
  成本   = 开仓名义金额（amount × open_rate）× fee_open + 平仓名义金额（amount × close_rate）× fee_close
           （freqtrade 回测就是这样扣手续费的；研究脚本的 --fee 0.0007 = 手续费 + 滑点，每边）
  资金费 = funding_fees（正数 = 收到）
  净收益 = profit_abs 合计（已经扣了成本、算了资金费）
  扣成本前 = 净收益 + 成本；价格部分 = 扣成本前 − 资金费
  扣成本前 PF = 每笔（profit_abs + 这一笔的成本）再算盈亏因子
每段回测都从 1000 USDT 开始；走步合计 = 各折直接相加（不复利）。这些数只用来设计下一轮，不改变这一轮的判定。
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path
from typing import Any

SCRIPTS_DIR = Path(__file__).resolve().parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import check_drawdown as cd  # noqa: E402
import research_report as rr  # noqa: E402

SEG_ORDER = ["train", "holdout", "test"] + [f"wf{i}" for i in range(1, 21)] + ["preholdout"]
SEG_CN = {"train": "训练段", "holdout": "没看过的一段", "test": "样本外", "preholdout": "研究开始前的留出段", "wf": "走步合计"}


def seg_name(seg: str) -> str:
    if seg in SEG_CN:
        return SEG_CN[seg]
    if seg.startswith("wf") and seg[2:].isdigit():
        return f"走步第 {seg[2:]} 折"
    return seg


def _f(v: Any) -> float:
    x = cd._num(v)
    return float(x) if x is not None and math.isfinite(float(x)) else 0.0


def trade_costs(t: dict[str, Any]) -> tuple[float, float]:
    """一笔交易的（成本, 开仓名义金额）。"""
    amount = abs(_f(t.get("amount")))
    open_rate = _f(t.get("open_rate"))
    close_rate = _f(t.get("close_rate")) or open_rate
    notional = amount * open_rate
    cost = notional * _f(t.get("fee_open")) + amount * close_rate * _f(t.get("fee_close"))
    return cost, notional


def summarize(trades: list[dict[str, Any]]) -> dict[str, Any]:
    n = len(trades)
    cost = notional = funding = net = 0.0
    pre: list[float] = []
    nets: list[float] = []
    for t in trades:
        c, nt = trade_costs(t)
        p = _f(t.get("profit_abs"))
        cost += c
        notional += nt
        funding += _f(t.get("funding_fees"))
        net += p
        nets.append(p)
        pre.append(p + c)
    gross = net + cost
    return {
        "trades": n, "avg_notional": (notional / n) if n else None, "cost": cost, "funding": funding, "net": net,
        "pre_cost": gross, "price": gross - funding,
        "pf": rr._profit_factor(nets), "pf_pre_cost": rr._profit_factor(pre),
        "cost_share": (cost / gross) if gross > 0 else None,
    }


def collect(run_dir: Path, strategies: list[str] | None) -> dict[str, list[tuple[str, dict[str, Any]]]]:
    plan = rr.load_plan(run_dir)
    jobs = rr.load_jobs(run_dir)
    names = strategies or list(dict.fromkeys(j["strategy"] for j in jobs))
    out: dict[str, list[tuple[str, dict[str, Any]]]] = {}
    for name in names:
        segs = [j["segment"] for j in jobs if j["strategy"] == name and j["variant"] == "default" and j["status"] == "ok"]
        segs = sorted(set(segs), key=lambda s: (SEG_ORDER.index(s) if s in SEG_ORDER else len(SEG_ORDER), s))
        rows: list[tuple[str, dict[str, Any]]] = []
        wf_trades: list[dict[str, Any]] = []
        for seg in segs:
            r = rr.eval_job(plan, jobs, name, "default", seg)
            trades = rr._trades_of(r)
            if seg.startswith("wf"):
                wf_trades += trades
            rows.append((seg, summarize(trades)))
        if wf_trades:
            idx = next((i for i, (s, _) in enumerate(rows) if s == "preholdout"), len(rows))
            rows.insert(idx, ("wf", summarize(wf_trades)))
        out[name] = rows
    return out


def render(data: dict[str, list[tuple[str, dict[str, Any]]]], run_dir: Path) -> list[str]:
    lines = [f"成本诊断（只读 {run_dir}；每段从 1000 USDT 开始，单位 USDT；只用来设计下一轮，不改变判定）："]
    headers = [("策略", False), ("段", False), ("交易", True), ("平均名义", True), ("成本", True), ("资金费", True),
               ("净收益", True), ("扣成本前", True), ("价格部分", True), ("成本占扣成本前", True), ("净 PF", True),
               ("扣成本前 PF", True)]
    body = []
    for name, rows in data.items():
        for seg, s in rows:
            body.append([
                name, seg_name(seg), str(s["trades"]), rr._num(s["avg_notional"], 1), rr._num(s["cost"], 1),
                f"{s['funding']:+.1f}", f"{s['net']:+.1f}", f"{s['pre_cost']:+.1f}", f"{s['price']:+.1f}",
                rr._pct(s["cost_share"]) if s["cost_share"] is not None else "-",
                rr._num(s["pf"]), rr._num(s["pf_pre_cost"]),
            ])
    lines += rr._text_table(headers, body)
    lines += [
        "  成本 = 开仓名义 × fee_open + 平仓名义 × fee_close（研究的 --fee = 手续费 + 滑点，每边）；扣成本前 = 净收益 + 成本；价格部分 = 扣成本前 − 资金费。",
        "  资金费：OKX 只有最近约 3 个月的资金费（运行 C 用了历史归档），更早的持仓是 0；\"成本占扣成本前\"只在扣成本前赚钱时有数。",
    ]
    return lines


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--run-dir", required=True, help="研究结果目录 user_data/research/run_<时间>")
    ap.add_argument("--strategies", default="", help="只看这几个策略（空格或逗号分隔；默认全部）")
    ap.add_argument("--json", default="", help="另外写一份 JSON（可选）")
    a = ap.parse_args()
    run_dir = Path(a.run_dir)
    if not (run_dir / "plan.json").is_file() or not (run_dir / "jobs.tsv").is_file():
        print(f"参数错误：{run_dir} 不是研究结果目录（没有 plan.json / jobs.tsv）", file=sys.stderr)
        return 2
    names = [x for x in a.strategies.replace(",", " ").split() if x] or None
    data = collect(run_dir, names)
    if not any(data.values()):
        print("没有找到回测结果（jobs.tsv 里没有状态 ok 的默认参数回测）", file=sys.stderr)
        return 3
    print("\n".join(render(data, run_dir)))
    if a.json:
        Path(a.json).write_text(json.dumps({k: [{"segment": s, **v} for s, v in rows] for k, rows in data.items()},
                                           ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
