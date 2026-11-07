#!/usr/bin/env python3
"""
子账户组合检查（scripts/research_portfolio.py）的算法自检：用几条手工编的资金曲线，和手算的答案对。

用法：
  .venv/bin/python scripts/check_portfolio_math.py                       # 只跑手工例子（不需要任何数据，几秒钟）
  .venv/bin/python scripts/check_portfolio_math.py --run-dir user_data/research/synthetic/run_<时间>
      # 另外拿一次研究结果核对：每个策略自己的资金曲线回撤和 check_drawdown 逐位一致；
      # 只放一个策略、权重 100% 的"组合"，收益/回撤和排名表里这个策略的数字一致；各子账户贡献加起来 = 组合收益

另外核对回撤上限（项目硬规则，2026-11-07 起 15%）：check_drawdown.py 的 DEFAULT_MAX_DD_PCT 和 scripts/_common.sh 的
EASYTRADE_DEFAULT_MAX_DD_PCT 相同；环境变量 MAX_DD_PCT / plan.json / --max-dd 的优先顺序；单个策略和组合的判定都按这个上限。

退出码：0 = 全部通过；1 = 有不对的。
"""

from __future__ import annotations

import argparse
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path


SCRIPTS_DIR = Path(__file__).resolve().parent
ROOT_DIR = SCRIPTS_DIR.parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import numpy as np  # noqa: E402

import check_drawdown as cd  # noqa: E402
import research_portfolio as rp  # noqa: E402


FAILS: list[str] = []
COUNT = 0
DAY = 86400
T0 = int(datetime(2025, 1, 1, tzinfo=timezone.utc).timestamp())


def _close(a, b, tol: float) -> bool:
    if a is None or b is None:
        return a is None and b is None
    a, b = float(a), float(b)
    if math.isinf(a) or math.isinf(b):
        return a == b
    return abs(a - b) <= tol


def _show(v):
    if isinstance(v, (list, tuple)):
        return [_show(x) for x in v]
    return float(v) if isinstance(v, np.floating) else v


def check(name: str, got, want, tol: float = 1e-9) -> None:
    global COUNT
    COUNT += 1
    if isinstance(want, (list, tuple)):
        got = list(got) if got is not None else None
        ok = got is not None and len(got) == len(want) and all(_close(a, b, tol) for a, b in zip(got, want))
    elif isinstance(want, str):
        ok = got == want
    else:
        ok = _close(got, want, tol)
    print(f"  {'通过' if ok else '不对'}  {name}：得到 {_show(got)}，应为 {_show(want)}")
    if not ok:
        FAILS.append(name)


def days(n: int) -> list[int]:
    return [T0 + k * DAY for k in range(n)]


# ----------------------------------------------------------------------------
def test_flat_and_linear() -> None:
    print("1. 一条直线上涨 + 一条一直不动（平均分，各 500 USDT）")
    a = rp.curve_from_points(days(10), [1000 + 10 * (k + 1) for k in range(10)], start=1000)  # 10 天涨到 1100
    b = rp.flat_curve(1000)  # 一笔交易都没有
    comb = rp.combine([a, b], [0.5, 0.5], T0, T0 + 10 * DAY)
    check("组合最后的权益 = 500×1.1 + 500", comb["final"], 1050.0)
    check("组合回撤（一直在涨）", rp.max_dd(comb["low"], comb["end"], comb["start"]), 0.0)
    dv = rp.daily_values(comb["G"], comb["g"], comb["end"], comb["start"], T0, T0 + 10 * DAY)
    check("每天 00:00 的组合权益", list(dv), [1000 + 5 * k for k in range(11)])
    check("年化（10 天赚 5%）", rp.cagr(0.05, 10), 1.05 ** 36.5 - 1)
    sub = [rp.daily_returns(rp.daily_values(comb["G"], comb["g"], se, ss, T0, T0 + 10 * DAY))
           for se, ss in zip(comb["sub_end"], comb["sub_start"])]
    m = rp.corr_matrix(sub)
    check("相关系数：一直不动的那个算不出来（None）", [m[0][0], m[0][1], m[1][1]], [1.0, None, None])


def test_drawdown_diversification() -> None:
    print("2. 两个子账户此消彼长：组合回撤比各自的都小")
    a = rp.curve_from_points(days(4), [1050, 1100, 1045, 1100], start=1000)
    b = rp.curve_from_points(days(4), [950, 1000, 1000, 1050], start=1000)
    check("A 自己的回撤 (1100-1045)/1100", rp.curve_max_dd(a), 55 / 1100)
    check("B 自己的回撤 (1000-950)/1000", rp.curve_max_dd(b), 0.05)
    comb = rp.combine([a, b], [0.5, 0.5], T0, T0 + 4 * DAY)
    check("组合每天收盘权益", list(comb["end"]), [1000, 1050, 1022.5, 1075])
    check("组合回撤 (1050-1022.5)/1050", rp.max_dd(comb["low"], comb["end"], comb["start"]), 27.5 / 1050)
    check("组合收益", comb["final"] / comb["start"] - 1, 0.075)
    print("3. K线内部的最低点（浮亏）也算进回撤")
    a2 = rp.curve_from_points(days(4), [1050, 1100, 1045, 1100], lows=[1040, 1050, 1000, 1045], start=1000)
    comb2 = rp.combine([a2, b], [0.5, 0.5], T0, T0 + 4 * DAY)
    check("组合回撤 (1050 - (500+500))/1050", rp.max_dd(comb2["low"], comb2["end"], comb2["start"]), 50 / 1050)
    print("4. 权重不等：A 30%、B 70%（缩放系数 = 子账户资金 / 1000）")
    comb3 = rp.combine([a, b], [0.3, 0.7], T0, T0 + 4 * DAY)
    check("组合每天收盘权益", list(comb3["end"]), [0.3 * x + 0.7 * y for x, y in zip(a["end"], b["end"])])
    one = rp.combine([a], [0.3], T0, T0 + 4 * DAY)
    check("只放一个、缩小到 30%：回撤不变", rp.max_dd(one["low"], one["end"], one["start"]), 55 / 1100)
    check("只放一个、缩小到 30%：最后的权益", one["final"], 330.0)


def test_correlation() -> None:
    print("5. 相关系数")
    va, vb = [1000.0], [1000.0]
    for k in range(20):
        up = k % 2 == 0
        va.append(va[-1] * (1.01 if up else 0.99))
        vb.append(vb[-1] * (0.99 if up else 1.01))
    ra, rb = rp.daily_returns(va), rp.daily_returns(vb)
    m = rp.corr_matrix([ra, rb, ra * 2])
    check("每天正好反着走 → -1", m[0][1], -1.0, 1e-12)
    check("同一个策略放大两倍 → +1", m[0][2], 1.0, 1e-12)
    print("6. 风险平价权重 ∝ 1/波动率")
    check("波动率 2% 和 1% → 1/3、2/3", rp.risk_parity([0.02, 0.01]), [1 / 3, 2 / 3])
    check("有一个算不出波动率 → 不做（None）", rp.risk_parity([0.02, None]), None)
    print("7. Sharpe / 盈亏因子")
    check("日收益 1%、2%、3%：均值 2%、标准差 1% → 2×√365", rp.sharpe([0.01, 0.02, 0.03]), 2 * math.sqrt(365))
    check("一直不动 → 没有 Sharpe", rp.sharpe([0.0, 0.0, 0.0]), None)
    check("两年赚 21% → 年化 10%", rp.cagr(0.21, 730), 0.10, 1e-3)
    # A 的交易 +10 / -5、B 的交易 +4 / -8，各 50% → 缩放后 +5 / -2.5 / +2 / -4
    check("按子账户资金缩放后的盈亏因子 7 / 6.5", rp.profit_factor([5, -2.5, 2, -4]), 7 / 6.5)
    check("只有盈利 → ∞", rp.profit_factor([1.0, 2.0]), math.inf)


def test_grid_mapping() -> None:
    print("8. 日线曲线放到小时时间轴上：开始前 = 初始资金，结束后 = 最后的权益")
    c = rp.curve_from_points([T0 + DAY, T0 + 2 * DAY], [1010, 1020], lows=[1005, 1015], start=1000)
    G = np.arange(T0, T0 + 4 * DAY, 3600, dtype="int64")
    low, end = rp.on_grid(c, G)
    check("第 1 天（还没开始）", [low[0], end[23]], [1000, 1000])
    check("第 2 天任何一个小时", [low[24], end[30], end[47]], [1005, 1010, 1010])
    check("第 4 天（结束以后）", [low[72], end[95]], [1020, 1020])
    dv = rp.daily_values(G, 3600, end, 1000, T0, T0 + 4 * DAY)
    check("每天 00:00 的权益 = 前一天最后一小时收盘", list(dv), [1000, 1000, 1010, 1020, 1020])


def test_notional() -> None:
    print("9. 最小下单额")
    check("止损兜底 -60% → freqtrade 多留 1.5 倍（上限）", rp.min_stake_reserve(-0.6), 1.5)
    check("止损 -2% → 1.05 / 0.98", rp.min_stake_reserve(-0.02), 1.05 / 0.98)
    check("8 USDT vs 7.5 → 够", rp.classify_notional(8, 7.5), "ok")
    check("6 USDT vs 7.5 → 放大（6×1.3 ≥ 7.5）", rp.classify_notional(6, 7.5), "bump")
    check("5 USDT vs 7.5 → 开不出来", rp.classify_notional(5, 7.5), "skip")


def test_candle_curve() -> None:
    print("10. 逐根K线的资金曲线：手算 + 和 check_drawdown.mtm_drawdown_candles 逐位一致")
    import pandas as pd

    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        (d / "futures").mkdir()

        def write(pair: str, rows: list[tuple[float, float, float, float]]) -> None:
            df = pd.DataFrame({
                "date": pd.to_datetime([T0 + 3600 * k for k in range(len(rows))], unit="s", utc=True),
                "open": [r[0] for r in rows], "high": [r[1] for r in rows],
                "low": [r[2] for r in rows], "close": [r[3] for r in rows], "volume": 1.0,
            })
            df.to_feather(d / "futures" / f"{cd._pair_file(pair)}-1h-futures.feather")

        # AAA：开 100，最低 95 / 90 / 98，第 4 根收在 105
        write("AAA/USDT:USDT", [(100, 101, 100, 100), (100, 100, 95, 96), (96, 97, 90, 92), (92, 106, 98, 105),
                                (105, 106, 104, 105), (105, 106, 104, 105)])
        # BBB：先涨后跌（给做空的那笔用）
        write("BBB/USDT:USDT", [(50, 51, 49, 50), (50, 53, 50, 52), (52, 56, 51, 55), (55, 55, 48, 49),
                                (49, 50, 47, 48), (48, 49, 46, 47)])

        def trade(pair, o, c, rate, close_rate, short=False, amount=1.0, fee=0.0, reason="exit_signal"):
            profit = (-1 if short else 1) * amount * (close_rate - rate) - fee * amount * (rate + close_rate)
            return cd._trade_info({
                "pair": pair, "open_date": datetime.fromtimestamp(T0 + 3600 * o, tz=timezone.utc).isoformat(),
                "close_date": datetime.fromtimestamp(T0 + 3600 * c, tz=timezone.utc).isoformat(),
                "is_short": short, "amount": amount, "open_rate": rate, "close_rate": close_rate,
                "fee_open": fee, "fee_close": fee, "profit_abs": profit, "min_rate": rate, "max_rate": rate,
                "exit_reason": reason,
            })

        one = [trade("AAA/USDT:USDT", 0, 4, 100, 105)]
        c1 = rp.candle_curve(one, 1000, "1h", d, "feather", "futures")
        check("一笔多单：最低权益 1000 + (90 - 100) → 回撤 1%", rp.curve_max_dd(c1), 0.01)
        check("一笔多单：每根K线最低权益", list(c1["low"]), [1000, 995, 990, 998, 1005, 1005])
        check("一笔多单：每根K线收盘权益", list(c1["end"]), [1000, 996, 992, 1005, 1005, 1005])
        two = one + [trade("BBB/USDT:USDT", 1, 3, 52, 55, short=True, amount=2.0, fee=0.0005, reason="stop_loss"),
                     trade("BBB/USDT:USDT", 3, 5, 49, 47, short=True, amount=3.0, fee=0.0005)]
        for name, infos, start in [("一笔", one, 1000.0), ("三笔（有做空、有止损、有手续费）", two, 1000.0),
                                   ("三笔、初始资金 300", two, 300.0)]:
            c = rp.candle_curve(infos, start, "1h", d, "feather", "futures")
            ref = cd.mtm_drawdown_candles(infos, start, "1h", d, "feather", "futures")
            check(f"{name}：回撤和 check_drawdown 一致", rp.curve_max_dd(c), ref[0], 1e-15)
        # 子账户：同样的交易放进 500 USDT 的子账户 = 曲线 × 0.5
        c2 = rp.candle_curve(two, 1000, "1h", d, "feather", "futures")
        comb = rp.combine([c2], [0.5], T0, T0 + 6 * 3600)
        check("缩放到一半：最后的权益", comb["final"], 0.5 * c2["final"])
        check("缩放到一半：回撤不变", rp.max_dd(comb["low"], comb["end"], comb["start"]), rp.curve_max_dd(c2), 1e-15)


# ----------------------------------------------------------------------------
def test_selection() -> None:
    print("11. 默认挑选规则：只看训练段；训练段日收益高度相关的两个只留一个")
    plan = {"train": ["2025-01-01", "2025-01-04"], "test": ["2025-02-01", "2025-02-10"], "wf": {"folds": []}}

    def row(name: str, tr: dict, te: dict) -> dict:
        return {"strategy": name, "variant": "default", "train": tr, "test": te}

    good_tr = {"trades": 40, "profit_total": 0.05, "profit_factor": 1.3, "dd_worst": 0.04}
    lose_tr = {"trades": 40, "profit_total": -0.02, "profit_factor": 0.9, "dd_worst": 0.05}
    great_te = {"trades": 40, "profit_total": 0.20, "profit_factor": 2.0, "dd_worst": 0.02}
    bad_te = {"trades": 40, "profit_total": -0.10, "profit_factor": 0.5, "dd_worst": 0.12}
    rows = [row("A", good_tr, bad_te), row("B", lose_tr, great_te), row("C", {**good_tr, "profit_total": 0.02}, bad_te)]
    sel = rp.select(rows, [], cd.DEFAULT_MAX_DD_PCT / 100)
    check("训练段赚钱的 A、C 入选（样本外再差也不影响挑选），训练段亏钱的 B 不选（样本外再好也不选）",
          ",".join(sel["selected"]), "A,C")
    # 去重：A 和 C 的训练段资金曲线一模一样（相关系数 1），留训练段 收益/回撤 比更好的 A；D 和它们不相关
    a = rp.curve_from_points(days(4), [1050, 1100, 1045, 1100], start=1000)
    d = rp.curve_from_points(days(4), [990, 1000, 1010, 990], start=1000)
    mem = [{"curve": a}, {"curve": a}, {"curve": d}]
    rows3 = [rows[0], rows[2], row("D", {**good_tr, "profit_total": 0.01}, bad_te)]
    keep, dropped = rp.drop_correlated(plan, rows3, mem)
    check("A、C 一模一样 → 去掉 C（收益/回撤 比差），D 留下", [rows3[i]["strategy"] for i in keep] == ["A", "D"], True)
    check("去掉的说明：C 像 A，相关系数 1", (dropped[0]["strategy"], dropped[0]["like"], round(dropped[0]["corr"], 9)) == ("C", "A", 1.0), True)


def test_max_dd_rule() -> None:
    print("12. 回撤上限（项目硬规则）：只在一个地方定义；环境变量 / plan.json / --max-dd 的优先顺序；判定按它来")
    import research_report as rr

    rule = cd.DEFAULT_MAX_DD_PCT
    check("check_drawdown.DEFAULT_MAX_DD_PCT = 15（2026-11-07 起；以前 8）", rule, 15.0)
    check("第 1、2 轮的旧上限只用来写说明：LEGACY_MAX_DD_PCT = 8", cd.LEGACY_MAX_DD_PCT, 8.0)
    common = (SCRIPTS_DIR / "_common.sh").read_text(encoding="utf-8")
    m = re.search(r"^EASYTRADE_DEFAULT_MAX_DD_PCT=([0-9.]+)\s*$", common, re.M)
    check("scripts/_common.sh 的 EASYTRADE_DEFAULT_MAX_DD_PCT 和它相同", float(m.group(1)) if m else None, rule)
    bash = shutil.which("bash")
    if bash:
        env = {k: v for k, v in os.environ.items() if k != cd.MAX_DD_ENV}
        cmd = [bash, "-c", 'source "$1" >/dev/null && printf %s "$MAX_DD_PCT"', "_", str(SCRIPTS_DIR / "_common.sh")]
        got = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=60).stdout.strip()
        check("shell 脚本：没设 MAX_DD_PCT 时 = 15", float(got) if got else None, rule)
        got12 = subprocess.run(cmd, env={**env, cd.MAX_DD_ENV: "12"}, capture_output=True, text=True, timeout=60).stdout.strip()
        check("shell 脚本：MAX_DD_PCT=12 时 = 12（不被默认值盖掉）", float(got12) if got12 else None, 12.0)

    saved = os.environ.pop(cd.MAX_DD_ENV, None)
    try:
        check("Python：没设 MAX_DD_PCT 时默认 15", cd.default_max_dd_pct(), rule)
        check("plan.json 没记 max_dd（很旧的结果）：用默认", cd.settings_max_dd_pct({}), rule)
        os.environ[cd.MAX_DD_ENV] = "12"
        check("MAX_DD_PCT=12：默认变成 12", cd.default_max_dd_pct(), 12.0)
        check("plan.json 记了 8（第 2 轮）：仍按 8 判，不受环境变量影响", cd.settings_max_dd_pct({"max_dd": 8}), 8.0)
        check("check_drawdown --max-dd 15%（带 % 也认）", cd.parse_max_dd_pct("15%"), 15.0)
        bad = [v for v in ("abc", "0", "-3", "150", "nan") if not _raises(cd.parse_max_dd_pct, v)]
        check("不是 0 ~ 100 之间的数字都报错（abc / 0 / -3 / 150 / nan）", ",".join(bad), "")
        os.environ[cd.MAX_DD_ENV] = "abc"
        check("MAX_DD_PCT=abc：check_drawdown 报参数错误、退出码 2（不会悄悄用默认值）", cd.main(["--max-leverage", "5"]), 2)
    finally:
        os.environ.pop(cd.MAX_DD_ENV, None)
        if saved is not None:
            os.environ[cd.MAX_DD_ENV] = saved

    s = {"max_dd": 8}
    cd.override_settings_max_dd(s, 15.0)
    check("--max-dd 15 重新判第 2 轮的结果：上限变成 15，原来记的 8 留着", (s["max_dd"], s["max_dd_recorded"]) == (15.0, 8), True)
    check("重新判定时报告抬头有说明", "重新判定" in cd.max_dd_override_note(s) and "8%" in cd.max_dd_override_note(s), True)
    check("没有重新判定时没有说明", cd.max_dd_override_note({"max_dd": 15}), "")
    check("报告抬头：15% = 项目硬规则", "项目硬规则" in cd.max_dd_rule_note(15.0), True)
    check("报告抬头：比硬规则宽时打 !! 提醒", cd.max_dd_rule_note(20.0).startswith("!!"), True)

    # 判定：训练段 / 样本外 / 走步 回撤 10%（在 8% 和 15% 之间），其他都达标 —— 15% 时通过，8% 时不通过
    seg = {"trades": 40, "profit_total": 0.05, "profit_factor": 1.5, "dd_worst": 0.10, "leverage_max": 2.0}
    wf = {**seg, "folds_missing": 0}
    for cap, want in ((15, "PASS"), (8, "FAIL")):
        row = {"variant": "default", "train": dict(seg), "test": dict(seg), "wf": dict(wf)}
        rr.judge(row, {"max_dd": cap}, wf_enabled=True)
        check(f"单个策略 回撤 10%、上限 {cap}%：{'通过' if want == 'PASS' else '不通过'}", row["verdict"], want)
    tr10 = {"trades": 40, "profit_total": 0.05, "profit_factor": 1.3, "dd_worst": 0.10}
    rows = [{"strategy": n, "variant": "default", "train": tr10, "test": tr10} for n in ("A", "B")]
    check("组合挑选：训练段回撤 10% 的策略，上限 15% 时入选", ",".join(rp.select(rows, [], 0.15)["selected"]), "A,B")
    check("组合挑选：上限 8% 时不入选", ",".join(rp.select(rows, [], 0.08)["selected"]), "")
    pseg = {**seg, "no_result": []}
    res = {"segments": {"train": dict(pseg), "test": dict(pseg), "wf": {**pseg, "folds_missing": {}}}}
    check("组合判定：总权益回撤 10%、上限 15%：没有回撤方面的原因",
          [x for x in rp.judge(res, {"max_dd": 15}, True) if "回撤" in x], [])
    check("组合判定：上限 8%：样本外、训练段、走步三条回撤原因",
          len([x for x in rp.judge(res, {"max_dd": 8}, True) if "回撤" in x]), 3)


def test_reference_corr() -> None:
    print("12b. 第 5 轮：和参考趋势策略的相关系数（训练段日收益，只报告）；--run-dirs 合并的规则")
    plan = {"train": ["2025-01-01", "2025-01-21"], "test": ["2025-02-01", "2025-02-10"], "wf": {"folds": []}}
    up = [1000.0]
    for k in range(20):
        up.append(up[-1] * (1.01 if k % 3 else 0.98))
    down = [1000.0 * 1000.0 / v for v in up]  # 每天正好反着走
    ref = rp.curve_from_points(days(21), up, start=1000)
    same = rp.curve_from_points(days(21), [1000 + 2 * (v - 1000) for v in up], start=1000)
    inv = rp.curve_from_points(days(21), down, start=1000)
    rows = [{"strategy": n, "variant": "default", "train": {"trades": 5}, "test": None}
            for n in ("TsmomTrend", "DonchianTrend", "Same", "Inverse", "NoTrain")]
    rows[4]["train"] = None
    rows.append({"strategy": "Same", "variant": "opt", "train": {"trades": 5}, "test": None})  # 优化参数那一行不算
    cache = {"TsmomTrend": {"curve": ref, "infos": [1] * 5}, "DonchianTrend": {"curve": rp.flat_curve(1000), "infos": []},
             "Same": {"curve": same, "infos": [1] * 5}, "Inverse": {"curve": inv, "infos": [1] * 5}}
    rc = rp.reference_correlations(plan, rows, cache)
    check("参考策略 = 这次有的 TsmomTrend、DonchianTrend（XSMomentum、BigTrendShort 没有就不列）",
          ",".join(rc["refs"]), "TsmomTrend,DonchianTrend")
    check("候选 = 其余默认参数的策略", ",".join(rc["candidates"]), "Same,Inverse,NoTrain")
    # Same 的日收益不是 TsmomTrend 的倍数（权益放大两倍不等于收益率放大两倍），只要求同向、很高
    check("同涨同跌的候选：相关系数 > 0.99", 1.0 if rc["matrix"][0][0] > 0.99 else 0.0, 1.0)
    check("正好反着走的候选：相关系数 < -0.99", 1.0 if rc["matrix"][1][0] < -0.99 else 0.0, 1.0)
    check("参考策略一笔交易都没有（DonchianTrend）→ 算不出来", [rc["matrix"][0][1], rc["matrix"][1][1]], [None, None])
    check("候选训练段没有结果 → 算不出来", rc["matrix"][2], [None, None])
    none = rp.reference_correlations(plan, [r for r in rows if r["strategy"] in ("Same", "Inverse")], cache)
    check("没有参考策略 → 不算，说明原因", bool(none["skip"]) and not none["matrix"], True)

    def p(name: str, exch: str, test_end: str) -> dict:
        return {"_run_dir": f"/x/{name}", "exchange": exch, "train": ["2021-01-01", "2024-10-31"],
                "test": ["2024-11-01", test_end], "holdout": ["2021-01-01", "2023-12-31"], "wf": {"folds": []},
                "settings": {"max_dd": 15, "fee": 0.0007}}

    a_rows = [{"strategy": "TsmomTrend", "variant": "default"}, {"strategy": "PremiumFade", "variant": "default"}]
    b_rows = [{"strategy": "XSFundingCarry", "variant": "default"}, {"strategy": "TsmomTrend", "variant": "default"}]
    pa, pb = p("run_okx", "okx", "2026-11-07"), p("run_bn", "binance", "2026-11-05")
    merged, rows_m = rp.merge_runs([(pa, a_rows), (pb, b_rows)])
    mg = merged["_merged"]
    check("两个交易所 → 标注\"跨交易所近似\"", mg["label"], rp.CROSS_EXCHANGE_LABEL)
    check("合并后的策略（同名的用前面那个目录的）", ",".join(r["strategy"] for r in rows_m), "TsmomTrend,PremiumFade,XSFundingCarry")
    check("每一行带着它自己那次研究的 plan（读K线用自己的数据目录）", ",".join(r["_plan"]["exchange"] for r in rows_m),
          "okx,okx,binance")
    check("区间、settings 按第一个目录", (merged["test"][1], merged["exchange"]) == ("2026-11-07", "okx"), True)
    check("样本外结束日不一样 → 1 条提醒；重名 → 1 条提醒", (len(mg["window_diff"]), len(mg["dups"])), (1, 1))
    same_ex, _ = rp.merge_runs([(p("r1", "okx", "2026-11-07"), [{"strategy": "A", "variant": "default"}]),
                                (p("r2", "okx", "2026-11-07"), [{"strategy": "B", "variant": "default"}])])
    check("同一个交易所的两个目录 → \"多个运行目录合并（近似）\"，没有区间提醒",
          (same_ex["_merged"]["label"], same_ex["_merged"]["window_diff"]) == ("多个运行目录合并（近似）", []), True)


def _raises(fn, *args) -> bool:
    try:
        fn(*args)
    except ValueError:
        return True
    return False


def check_run(run_dir: Path) -> None:
    print(f"13. 和一次研究结果核对：{run_dir}")
    os.chdir(ROOT_DIR)
    import research_report as rr

    plan = rr.load_plan(run_dir)
    plan["_run_dir"] = str(run_dir)
    rows = rr.build_rows(plan, rr.load_jobs(run_dir), rr.load_notes(run_dir))
    rows = [r for r in rows if r["variant"] == "default"]
    for r in rows:
        name = r["strategy"]
        res = rp.build(plan, [r], strategies=[name], weights_mode="equal")
        for seg in rp.SEG_ORDER:
            ref = r["wf"] if seg == "wf" else r.get(seg)
            got = res["segments"].get(seg)
            if ref is None or got is None:
                continue
            if ref.get("trades", 0) == 0:
                check(f"{name} {rp.SEG_CN[seg]}：没有交易 → 组合收益 0", got["profit_total"], 0.0)
                continue
            if str(ref.get("dd_mtm_method", "")).startswith("K线"):
                check(f"{name} {rp.SEG_CN[seg]}：含浮亏回撤 = 排名表", got["dd_mtm"], ref["dd_mtm"], 1e-12)
            want = ref["profit_total"]
            check(f"{name} {rp.SEG_CN[seg]}：收益 = 排名表", got["profit_total"], want, 1e-6)
            check(f"{name} {rp.SEG_CN[seg]}：交易数 = 排名表", got["trades"], ref["trades"], 0)
            if seg == "wf":
                check(f"{name} 走步：平仓回撤 = 排名表", got["dd_closed"], ref["dd_closed"], 1e-12)
    have = [r["strategy"] for r in rows if r.get("test") and r["test"]["trades"] > 0]
    if len(have) >= 2:
        for mode in ("equal", "riskparity"):
            res = rp.build(plan, rows, strategies=have, weights_mode=mode)
            check(f"{len(have)} 个策略（{mode}）：权重加起来 = 1", sum(res["weights"].values()), 1.0, 1e-12)
            for seg, sr in res["segments"].items():
                check(f"{len(have)} 个策略（{mode}）{rp.SEG_CN[seg]}：各子账户贡献加起来 = 组合收益",
                      sum(m["contrib_pct"] for m in sr["members"]), sr["profit_total"], 1e-9)
                check(f"{len(have)} 个策略（{mode}）{rp.SEG_CN[seg]}：交易数 = 各子账户之和",
                      sr["trades"], sum(m["trades"] for m in sr["members"]), 0)
                # 各子账户权益都是正数时，组合（含浮亏）回撤一定不超过最差的那个子账户（每个子账户都只跌了不到它自己的回撤）
                worst = max(m["dd_mtm"] for m in sr["members"])
                check(f"{len(have)} 个策略（{mode}）{rp.SEG_CN[seg]}：组合回撤不超过最差的子账户（{_p(sr['dd_mtm'])} ≤ {_p(worst)}）",
                      1.0 if sr["dd_mtm"] <= worst + 1e-12 else 0.0, 1.0)
        res = rp.build(plan, rows, strategies=have, weights_mode="riskparity")
        if res.get("riskparity_weights"):
            vols = [res["train_vol"][n] for n in have]
            inv = [1 / v for v in vols]
            check("风险平价权重只用训练段波动率", [res["weights"][n] for n in have], [x / sum(inv) for x in inv], 1e-12)


def _p(v: float) -> str:
    return f"{v * 100:.2f}%"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="子账户组合检查的算法自检")
    ap.add_argument("--run-dir", type=Path, help="另外拿这次研究的结果核对（可选）")
    a = ap.parse_args(argv)
    test_flat_and_linear()
    test_drawdown_diversification()
    test_correlation()
    test_grid_mapping()
    test_notional()
    test_candle_curve()
    test_selection()
    test_max_dd_rule()
    test_reference_corr()
    if a.run_dir:
        check_run(a.run_dir)
    print()
    if FAILS:
        print(f"有 {len(FAILS)} / {COUNT} 项不对：")
        for f in FAILS:
            print(f"  - {f}")
        return 1
    print(f"全部 {COUNT} 项通过。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
