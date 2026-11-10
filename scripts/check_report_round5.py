#!/usr/bin/env python3
"""
第 5 轮研究报告新增功能的自检（scripts/research_report.py、scripts/research_portfolio.py）：
用手工编的交易、K线、日志和登记表，和手算的答案对。不联网、不需要真实数据，十几秒。

用法：
  .venv/bin/python scripts/check_report_round5.py

核对的内容：
  1. 登记表：读文件（允许 // 注释）、默认文件不存在 = 空登记表、指定的文件不存在 / 写坏了 = 报错；
     min_pf 的几种写法和"待定"标记；写错的检查（不认识的类型、缺参数）= 这一行判未通过
  2. min_pf 覆盖：同一组结果，默认门槛 1.2 不通过、登记表 1.1 通过；没登记的策略判定和以前一样
  3. 登记的策略每一段都查盈亏因子和交易数（预注册第 3.3 节），没登记的不查
  4. 每种附加检查：funding_net_positive、zero_funding_share_invalid（无效）、max_trades_per_pair_year、
     min_tradable_pairs（测不了；日志行被折开也认得出）、drop_top_days、count_tags_exclude（hedge 不算交易数）
  5. 逐年表的收益和回撤（手算；回撤的峰值从年初起算）
  6. 2020 留出段：≥ 30 笔 赚钱且回撤达标 = 通过；亏损 = 失败；< 30 笔只报告；回测失败 = 失败
  7. 端到端：假的研究目录（回测结果 zip + K线 + 日志）→ research_report.py report --prereg
  8. research_portfolio.py --run-dirs：一个 OKX 目录 + 一个币安目录合并，标注"跨交易所近似"，K线各读各的数据目录；
     和参考趋势策略的相关系数表
  9. research_costs.py（成本诊断，2026-11-10 加）：手算的成本、资金费、扣成本前收益和盈亏因子；读假的研究目录、各段的顺序

退出码：0 = 全部通过；1 = 有不对的。
"""

from __future__ import annotations

import contextlib
import csv
import io
import json
import math
import sys
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


SCRIPTS_DIR = Path(__file__).resolve().parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import check_drawdown as cd  # noqa: E402
import research_portfolio as rp  # noqa: E402
import research_report as rr  # noqa: E402


FAILS: list[str] = []
COUNT = 0
H = 3600
DAY = 86400
SETTINGS = {"max_dd": cd.DEFAULT_MAX_DD_PCT, "min_pf": 1.2, "min_trades": 30, "pf_ratio": 0.6, "max_leverage": 5.0}


def _close(a: Any, b: Any, tol: float) -> bool:
    if a is None or b is None:
        return a is None and b is None
    if isinstance(b, bool) or not isinstance(b, (int, float)) or not isinstance(a, (int, float)):
        return a == b
    a, b = float(a), float(b)
    if math.isinf(a) or math.isinf(b):
        return a == b
    return abs(a - b) <= tol


def check(name: str, got: Any, want: Any, tol: float = 1e-9) -> None:
    global COUNT
    COUNT += 1
    if isinstance(want, (list, tuple)):
        got = list(got) if got is not None else None
        ok = got is not None and len(got) == len(want) and all(_close(a, b, tol) for a, b in zip(got, want))
    elif isinstance(want, (str, bool)) or want is None:
        ok = got == want
    else:
        ok = _close(got, want, tol)
    print(f"  {'通过' if ok else '不对'}  {name}：得到 {got!r}，应为 {want!r}")
    if not ok:
        FAILS.append(name)


def ok(name: str, cond: bool, detail: Any = "") -> None:
    global COUNT
    COUNT += 1
    print(f"  {'通过' if cond else '不对'}  {name}" + (f"（{detail}）" if detail != "" and not cond else ""))
    if not cond:
        FAILS.append(name)


# ----------------------------------------------------------------------------
# 造数据
# ----------------------------------------------------------------------------
def ts(text: str) -> int:
    return int(datetime.fromisoformat(text).replace(tzinfo=timezone.utc).timestamp())


def iso(t: float) -> str:
    return datetime.fromtimestamp(t, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S+00:00")


def trade(pair: str, o: float, c: float, open_rate: float, close_rate: float, amount: float = 1.0, short: bool = False,
          funding: float = 0.0, tag: str = "long", reason: str = "exit_signal") -> dict[str, Any]:
    """freqtrade 回测结果格式的一笔交易（一次开仓、一次平仓，手续费 0；profit_abs 含资金费，和 freqtrade 一样）。"""
    d = -1.0 if short else 1.0
    profit = d * amount * (close_rate - open_rate) + funding
    return {
        "pair": pair, "open_date": iso(o), "close_date": iso(c), "open_timestamp": int(o * 1000),
        "close_timestamp": int(c * 1000), "is_short": short, "amount": amount, "open_rate": open_rate,
        "close_rate": close_rate, "fee_open": 0.0, "fee_close": 0.0, "profit_abs": profit,
        "profit_ratio": profit / (amount * open_rate), "funding_fees": funding, "enter_tag": tag,
        "exit_reason": reason, "leverage": 1.0, "min_rate": min(open_rate, close_rate),
        "max_rate": max(open_rate, close_rate), "stake_amount": amount * open_rate,
        "orders": [
            {"amount": amount, "safe_price": open_rate, "ft_is_entry": True, "order_filled_timestamp": int(o * 1000)},
            {"amount": amount, "safe_price": close_rate, "ft_is_entry": False, "order_filled_timestamp": int(c * 1000)},
        ],
    }


def gen(n_win: int, n_loss: int, first: str, pairs: list[str], win: float = 1.0, loss: float = 1.0,
        tag: str = "long", funding: float = 0.0, step_days: float = 1.0) -> list[dict[str, Any]]:
    """n_win 笔各赚 win、n_loss 笔各亏 loss（USDT，含资金费 funding），一天一笔，轮流用 pairs 里的币。"""
    out = []
    t0 = ts(first)
    for k in range(n_win + n_loss):
        p = win if k < n_win else -loss
        o = t0 + int(k * step_days * DAY)
        out.append(trade(pairs[k % len(pairs)], o, o + 12 * H, 100.0, 100.0 + p - funding, funding=funding, tag=tag))
    return out


def seg(trades: list[dict[str, Any]], start: str, end: str, dd: float = 0.01, balance: float = 1000.0) -> dict[str, Any]:
    """一段回测的评估结果（evaluate_dir 返回的格式）。"""
    prof = [float(t["profit_abs"]) for t in trades]
    return {
        "trades": len(trades), "profit_total": sum(prof) / balance, "profit_factor": rr._profit_factor(prof),
        "dd_worst": dd, "dd_closed": dd, "dd_mtm": dd, "dd_mtm_method": "K线(1h)", "leverage_max": 1.0,
        "start": start, "end": end, "starting_balance": balance, "final_balance": balance + sum(prof), "winrate": 0.5,
        "timeframe": "1d", "timeframe_detail": "1h", "_st": {"trades": trades},
        "_cfg": {"dataformat_ohlcv": "feather", "trading_mode": "futures"},
    }


def mkrow(name: str, train: Any, test: Any, holdout: Any = None) -> dict[str, Any]:
    return {"strategy": name, "timeframe": "1d", "variant": "default", "train": train, "test": test,
            "holdout": holdout, "wf": None, "folds": [], "notes": [], "params": {}, "fold_params": [], "ho_epochs": None}


def mkplan(datadir: Path, pairs: list[str], **kw: Any) -> dict[str, Any]:
    plan = {
        "datadir": str(datadir), "dataformat": "feather", "trading_mode": "futures", "exchange": "okx",
        "pairs": pairs, "pairs_first": {p: "2020-01-01" for p in pairs}, "pairs_config_count": len(pairs),
        "pairs_dropped": {}, "train": ["2021-01-01", "2022-12-31"], "test": ["2023-01-01", "2023-12-31"],
        "holdout": ["2021-01-01", "2021-12-31"], "data_range": ["2021-01-01", "2023-12-31"],
        "strategies": {}, "strategies_order": [], "settings": dict(SETTINGS), "wf": {"folds": [], "mode": "rolling"},
    }
    plan.update(kw)
    return plan


def add_strategy(plan: dict[str, Any], name: str, warm: int = 0, tf: str = "1d") -> None:
    plan["strategies"][name] = {"timeframe": tf, "startup": warm, "detail": "1h",
                                "warmup_candles": {"train": warm, "test": warm, "holdout": warm}}
    if name not in plan["strategies_order"]:
        plan["strategies_order"].append(name)


def reg(strategies: dict[str, Any]) -> dict[str, Any]:
    return {"found": True, "path": "test", "sha256": "0" * 64, "error": "", "data": {"round": 5, "strategies": strategies}}


def run(plan: dict[str, Any], row: dict[str, Any], prereg: dict[str, Any] | None, jobs: list | None = None,
        holdout: bool = True) -> dict[str, Any]:
    """和 research_report.build_rows 里同样的顺序：规则 → 交易数口径 → judge → 附加统计和附加检查。"""
    s = plan["settings"]
    rules = rr.prereg_rules(prereg, row["strategy"], s)
    s_row = rr.settings_for(s, rules)
    rr.set_counted(row, rules)
    rr.judge(row, s_row, False, holdout, all_segments=rules["registered"], exclude_tags=rules["exclude_tags"])
    rr.finish_round5(plan, list(jobs or []), row, rules, s_row)
    return row


def write_candles(datadir: Path, pair: str, start: str, end: str, special: dict[int, tuple] | None = None,
                  price: float = 100.0) -> None:
    """1h K线（开高低收都是 price，high/low 各差 1%），special = {时间秒: (open, high, low, close)} 改指定的那几根。"""
    import pandas as pd

    (datadir / "futures").mkdir(parents=True, exist_ok=True)
    t0, t1 = ts(start), ts(end)
    times = list(range(t0, t1, H))
    rows = []
    for t in times:
        o, h, lo, c = (special or {}).get(t, (price, price * 1.01, price * 0.99, price))
        rows.append((o, h, lo, c))
    df = pd.DataFrame({
        "date": pd.to_datetime(times, unit="s", utc=True),
        "open": [r[0] for r in rows], "high": [r[1] for r in rows], "low": [r[2] for r in rows],
        "close": [r[3] for r in rows], "volume": 1.0,
    })
    df.to_feather(datadir / "futures" / f"{cd._pair_file(pair)}-1h-futures.feather")
    # 1d 文件只放一根（evaluate 只读 1h；有些地方按文件判断"有没有这个周期"）
    df.iloc[:1].to_feather(datadir / "futures" / f"{cd._pair_file(pair)}-1d-futures.feather")


def write_result(out_dir: Path, strategy: str, trades: list[dict[str, Any]], start: str, end: str,
                 exchange: str = "okx") -> None:
    """一个回测结果目录：backtest-result-<时间>.zip 里有统计 json 和 _config.json（和 freqtrade 2026.10 一样的结构）。"""
    out_dir.mkdir(parents=True, exist_ok=True)
    prof = [float(t["profit_abs"]) for t in trades]
    infos = [i for i in (cd._trade_info(t) for t in trades) if i is not None]
    st = {
        "trades": trades, "total_trades": len(trades), "starting_balance": 1000.0, "dry_run_wallet": 1000.0,
        "final_balance": 1000.0 + sum(prof), "profit_total": sum(prof) / 1000.0, "profit_total_abs": sum(prof),
        "max_drawdown_account": cd.closed_drawdown(infos, 1000.0), "max_relative_drawdown": 0.0,
        "timeframe": "1d", "timeframe_detail": "1h", "backtest_start": f"{start} 00:00:00", "backtest_end": f"{end} 00:00:00",
        "winrate": (sum(1 for p in prof if p > 0) / len(prof)) if prof else 0.0, "stake_currency": "USDT",
        "stoploss": -0.25, "max_open_trades": 6,
    }
    stem = "backtest-result-2026-11-07_00-00-00"
    cfg = {"exchange": {"name": exchange}, "dataformat_ohlcv": "feather", "trading_mode": "futures"}
    with zipfile.ZipFile(out_dir / f"{stem}.zip", "w") as zf:
        zf.writestr(f"{stem}.json", json.dumps({"strategy": {strategy: st}, "strategy_comparison": []}))
        zf.writestr(f"{stem}_config.json", json.dumps(cfg))


def write_run(run_dir: Path, plan: dict[str, Any], results: dict[tuple[str, str], tuple[list, str]],
              logs: dict[tuple[str, str], str] | None = None, failed: list[tuple[str, str]] | None = None) -> None:
    """一个假的研究目录：plan.json、jobs.tsv、notes.tsv、bt/<策略>__default__<段>/ 里的回测结果、logs/。
    results = {(策略, 段): (交易, 时间范围 "YYYYMMDD-YYYYMMDD")}。"""
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "logs").mkdir(exist_ok=True)
    lines = []
    for (strat, segname), (trades, tr) in results.items():
        bt = run_dir / "bt" / f"{strat}__default__{segname}"
        a, b = tr.split("-")
        write_result(bt, strat, trades, f"{a[:4]}-{a[4:6]}-{a[6:]}", f"{b[:4]}-{b[4:6]}-{b[6:]}", plan.get("exchange", "okx"))
        log = run_dir / "logs" / f"{strat}__default__{segname}.log"
        log.write_text((logs or {}).get((strat, segname), "2026-11-07 00:00:00,000 - x - INFO - ok\n"), encoding="utf-8")
        lines.append("\t".join([strat, "default", segname, tr, "ok", str(bt), str(log)]))
    for strat, segname in failed or []:
        log = run_dir / "logs" / f"{strat}__default__{segname}.log"
        log.write_text("error\n", encoding="utf-8")
        lines.append("\t".join([strat, "default", segname, "20200101-20201231", "fail", str(run_dir / "bt" / "none"), str(log)]))
    (run_dir / "jobs.tsv").write_text("\n".join(lines) + "\n", encoding="utf-8")
    (run_dir / "notes.tsv").write_text("", encoding="utf-8")
    (run_dir / "plan.json").write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")


# ----------------------------------------------------------------------------
# 1. 登记表
# ----------------------------------------------------------------------------
def test_prereg_file(tmp: Path) -> None:
    print("1. 登记表：读文件、默认文件不存在、写坏了；min_pf 的写法和待定标记；写错的检查")
    f = tmp / "prereg.json"
    f.write_text('{\n  // 注释\n  "round": 5,\n  "strategies": {"A": {"min_pf": 1.1, "extra_checks": [],},},\n}\n',
                 encoding="utf-8")
    p = rr.load_prereg(f)
    check("允许 // 注释和多余的逗号：读得出来", (p["found"], p["error"], p["data"]["strategies"]["A"]["min_pf"]) == (True, "", 1.1), True)
    check("记下文件的 sha256", len(p["sha256"]), 64)
    check("指定的文件不存在 → 报错", bool(rr.load_prereg(tmp / "nope.json")["error"]), True)
    saved = rr.PREREG_DEFAULT
    try:
        rr.PREREG_DEFAULT = tmp / "nope.json"
        d = rr.load_prereg()
        check("默认的 configs/preregistration.json 不存在 → 空登记表、不报错", (d["found"], d["error"]), (False, ""))
        bad = tmp / "bad.json"
        bad.write_text("{ 这不是 json", encoding="utf-8")
        rr.PREREG_DEFAULT = bad
        check("默认文件写坏了 → 报错（不能悄悄不做第 5 轮的检查）", bool(rr.load_prereg()["error"]), True)
        check("research_report.py report 遇到坏登记表 → 退出码 2",
              _quiet(rr.main, ["report", "--run-dir", str(_tiny_run(tmp)), "--prereg", str(bad)]), 2)
    finally:
        rr.PREREG_DEFAULT = saved

    R = rr.prereg_rules(None, "X", SETTINGS)
    check("没登记：门槛 = settings 的 1.2、没有附加检查", (R["registered"], R["min_pf"], R["min_pf_source"], R["checks"]),
          (False, 1.2, "默认", []))
    pr = reg({
        "A": {"min_pf": 1.1, "min_pf_pending_decision": "等 David 定"},
        "B": {"min_pf": {"value": 1.1, "pending_decision": True, "note": "市场中性"}},
        "C": {"min_pf": 1.2, "pending_decision": ["min_pf"]},
        "D": {"extra_checks": [{"type": "max_trades_per_pair_year", "value": 30}]},
        "E": {"extra_checks": [{"type": "whatever"}, {"type": "min_tradable_pairs"},
                               {"type": "drop_top_days", "n": 3, "segments": ["unseen", "nope"]},
                               {"type": "zero_funding_share_invalid", "max_share": 1.5}],
              "min_pf": "abc"},
        "F": {"extra_checks": [{"type": "drop_top_days", "n": 3, "segments": ["unseen", "oos"]},
                               {"type": "count_tags_exclude", "tags": ["hedge"]}]},
    })
    a, b, c, d_, e, f_ = (rr.prereg_rules(pr, n, SETTINGS) for n in "ABCDEF")
    check("A：数字写法 1.1 + min_pf_pending_decision（说明文字）", (a["min_pf"], a["min_pf_source"], a["min_pf_pending"], a["min_pf_note"]),
          (1.1, "登记表", True, "等 David 定"))
    check("B：对象写法 {value, pending_decision, note}", (b["min_pf"], b["min_pf_pending"], b["min_pf_note"]), (1.1, True, "市场中性"))
    check("C：pending_decision 列表里有 min_pf", (c["min_pf"], c["min_pf_pending"]), (1.2, True))
    check("D：没写 min_pf → 默认 1.2，不是待定", (d_["min_pf"], d_["min_pf_source"], d_["min_pf_pending"], d_["checks"][0]["value"]),
          (1.2, "默认", False, 30.0))
    check("E：不认识的类型、缺 value、段名写错、max_share > 1、min_pf 不是数字 → 5 条写错", len(e["errors"]), 5)
    check("F：drop_top_days 的段名 unseen/oos → holdout/test；count_tags_exclude → 要排除的标签",
          (f_["checks"][0]["segments"], f_["exclude_tags"]), (["holdout", "test"], ["hedge"]))


def _quiet(fn: Any, *args: Any) -> Any:
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        return fn(*args)


def _tiny_run(tmp: Path) -> Path:
    d = tmp / "tiny_run"
    if not (d / "plan.json").is_file():
        data = tmp / "tiny_data"
        write_candles(data, "AAA/USDT:USDT", "2021-01-01", "2021-01-10")
        plan = mkplan(data, ["AAA/USDT:USDT"], train=["2021-01-01", "2021-01-04"], test=["2021-01-05", "2021-01-09"],
                      holdout=None)
        add_strategy(plan, "T")
        write_run(d, plan, {("T", "test"): (gen(1, 0, "2021-01-05", ["AAA/USDT:USDT"]), "20210105-20210109")})
    return d


# ----------------------------------------------------------------------------
# 2./3. min_pf 覆盖、每一段都查
# ----------------------------------------------------------------------------
def test_min_pf_and_segments(tmp: Path) -> None:
    print("2. min_pf 覆盖：样本外盈亏因子 1.15，默认 1.2 不通过、登记表 1.1 通过；没登记的判定和以前一样")
    P = ["AAA/USDT:USDT", "BBB/USDT:USDT"]
    plan = mkplan(tmp / "nodata", P)
    add_strategy(plan, "S")

    def fresh() -> dict[str, Any]:
        return mkrow("S", seg(gen(30, 20, "2021-02-01", P), "2021-01-01", "2022-12-31"),  # 训练段 PF 1.5（1.15 ≥ 0.6 × 1.5）
                     seg(gen(23, 20, "2023-01-02", P), "2023-01-01", "2023-12-31"),
                     seg(gen(25, 15, "2021-02-01", P), "2021-01-01", "2021-12-31"))

    r0 = run(plan, fresh(), None)
    check("没登记：样本外 PF 1.15 < 1.2 → 未通过，原因和以前的写法一样", (r0["verdict"], r0["reasons"]),
          ("FAIL", ["样本外盈亏因子 1.15 < 1.2"]))
    check("没登记：结论列显示\"未通过\"", rr.VERDICT_CN[r0["verdict"]], "未通过")
    r1 = run(plan, fresh(), reg({"S": {"min_pf": 1.1, "min_pf_pending_decision": True}}))
    check("登记表 min_pf 1.1 → 通过", (r1["verdict"], r1["reasons"]), ("PASS", []))
    hdr = "\n".join(rr._prereg_header(reg({"S": {"min_pf": 1.1, "min_pf_pending_decision": True}}), [r1], SETTINGS))
    ok("报告抬头标出\"待定\"和这次按 1.1 判", "!! 待定" in hdr and "按 1.1 判" in hdr, hdr)
    ok("运行时没记登记表（plan.json 没有 prereg）→ 不提醒改过", "改过" not in hdr, hdr)
    same = "\n".join(rr._prereg_header(reg({"S": {"min_pf": 1.1}}), [r1], SETTINGS, {"prereg": {"registry_sha256": "0" * 64}}))
    ok("运行时记下的登记表 sha256 和现在一样 → 不提醒", "改过" not in same, same)
    diff = "\n".join(rr._prereg_header(reg({"S": {"min_pf": 1.1}}), [r1], SETTINGS, {"prereg": {"registry_sha256": "1" * 64}}))
    ok("运行时记下的登记表 sha256 和现在不一样 → 抬头提醒\"登记表在这次运行之后改过\"", "!! 登记表在这次运行之后改过" in diff, diff)
    vs = rr.prereg_rules(reg({"S": {"venues": ["okx", "binance"], "stage": "1"}}), "S", SETTINGS)
    check("venue 写成列表（和 check_prereg.py 一样）", (vs["venue"], vs["stage"], vs["errors"]), ("okx、binance", "1", []))
    r2 = run(plan, fresh(), reg({"S": {"min_pf": 1.2}}))
    check("登记表 min_pf 1.2（写明） → 和默认一样不通过", (r2["verdict"], r2["reasons"]), ("FAIL", ["样本外盈亏因子 1.15 < 1.2"]))

    print("3. 登记的策略每一段（训练段、没看过的一段）也查盈亏因子和交易数；没登记的不查（和以前一样）")

    def weak() -> dict[str, Any]:
        return mkrow("S", seg(gen(22, 20, "2021-02-01", P), "2021-01-01", "2022-12-31"),       # 训练段 PF 1.10
                     seg(gen(30, 10, "2023-01-02", P), "2023-01-01", "2023-12-31"),           # 样本外 PF 3.0
                     seg(gen(15, 5, "2021-02-01", P), "2021-01-01", "2021-12-31"))            # 没看过的一段只有 20 笔

    w0 = run(plan, weak(), None)
    check("没登记：训练段 PF 1.10、没看过的一段 20 笔 → 照旧通过", (w0["verdict"], w0["reasons"]), ("PASS", []))
    w1 = run(plan, weak(), reg({"S": {"min_pf": 1.2}}))
    check("登记了：两条新原因", w1["reasons"], ["没看过的一段只有 20 笔交易（< 30）", "训练段盈亏因子 1.10 < 1.2"])
    check("登记了：未通过", w1["verdict"], "FAIL")


# ----------------------------------------------------------------------------
# 4. 每种附加检查
# ----------------------------------------------------------------------------
def test_funding(tmp: Path) -> None:
    print("4a. funding_net_positive：全期（训练段 + 样本外）资金费合计必须 > 0")
    P = ["AAA/USDT:USDT"]
    plan = mkplan(tmp / "nodata", P)
    add_strategy(plan, "F")
    pr = reg({"F": {"extra_checks": [{"type": "funding_net_positive"}]}})

    def row(f_train: float, f_test: float) -> dict[str, Any]:
        return mkrow("F", seg(gen(30, 10, "2021-02-01", P, funding=f_train), "2021-01-01", "2022-12-31"),
                     seg(gen(30, 10, "2023-01-02", P, funding=f_test), "2023-01-01", "2023-12-31"),
                     seg(gen(30, 10, "2021-02-01", P), "2021-01-01", "2021-12-31"))

    r = run(plan, row(0.25, -0.5), pr)
    check("资金费合计：训练段 40 × 0.25 = +10，样本外 40 × -0.5 = -20", (r["r5"]["funding"]["train"]["total"],
          r["r5"]["funding"]["test"]["total"], r["r5"]["funding"]["all"]["total"]), [10.0, -20.0, -10.0])
    check("全期 -10 USDT → 未通过（机制检查）", (r["verdict"], any(x.startswith("机制检查") for x in r["reasons"])), ("FAIL", True))
    r = run(plan, row(0.25, 0.1), pr)
    check("全期 +14 USDT → 通过", (r["verdict"], r["checks"][0]["result"]), ("PASS", "PASS"))
    r = run(plan, row(0.25, -0.25), pr)
    check("正好 0 → 不是净收入 → 未通过", r["verdict"], "FAIL")

    print("4b. zero_funding_share_invalid：≥ 95% 的交易资金费为 0 → 无效（不算通过也不算失败）")
    pz = reg({"F": {"extra_checks": [{"type": "zero_funding_share_invalid", "max_share": 0.95}]}})

    def zrow(n_zero: int) -> dict[str, Any]:
        tr = gen(30, 20, "2021-02-01", P, funding=0.0)      # 50 笔、资金费都是 0
        te = gen(30, 20, "2023-01-02", P, funding=0.01)     # 50 笔
        for t in te[: max(0, n_zero - 50)]:
            t["funding_fees"] = 0.0
        return mkrow("F", seg(tr, "2021-01-01", "2022-12-31"), seg(te, "2023-01-01", "2023-12-31"),
                     seg(gen(30, 10, "2021-02-01", P), "2021-01-01", "2021-12-31"))

    for n_zero, want in ((96, "INVALID"), (95, "INVALID"), (94, "PASS")):
        r = run(plan, zrow(n_zero), pz)
        check(f"100 笔里 {n_zero} 笔资金费为 0 → {rr.VERDICT_CN[want]}", (r["verdict"], r["r5"]["funding"]["all"]["zero_share"]),
              (want, n_zero / 100))
    bad = zrow(96)
    bad["test"]["dd_worst"] = 0.5
    r = run(plan, bad, pz)
    check("无效优先：同时回撤超标也显示\"无效\"，原因照样记下", (r["verdict"], len(r["reasons"]) > 0, len(r["invalid_reasons"])),
          ("INVALID", True, 1))
    ok("没登记这个检查：96% 为 0 也只报告", run(plan, zrow(96), None)["verdict"] == "PASS")


def test_trades_per_pair_year(tmp: Path) -> None:
    print("4c. max_trades_per_pair_year：平均每个币每年交易数（按能交易的时间算）> 30 → 未通过；hedge 不算")
    P = ["AAA/USDT:USDT", "BBB/USDT:USDT"]
    plan = mkplan(tmp / "nodata", P)
    plan["pairs_first"]["BBB/USDT:USDT"] = "2022-01-01"
    add_strategy(plan, "Q", warm=10)
    py_tr = rr.pair_years(plan, "Q", "train", P)
    py_te = rr.pair_years(plan, "Q", "test", P)
    # AAA：训练段 730 天、样本外 365 天；BBB：2022-01-01 上线 + 10 天预热 → 训练段 2022-01-11 ~ 2022-12-31 = 355 天
    check("能交易的时间：训练段 (730 + 355) / 365.25 年", py_tr, (730 + 355) / 365.25, 1e-12)
    check("能交易的时间：样本外 (365 + 365) / 365.25 年", py_te, 730 / 365.25, 1e-12)
    total_py = (730 + 355 + 730) / 365.25
    limit_n = math.floor(30 * total_py)  # 149 笔：29.97 笔/币/年
    pr = reg({"Q": {"extra_checks": [{"type": "max_trades_per_pair_year", "value": 30},
                                     {"type": "count_tags_exclude", "tags": ["hedge"]}]}})

    def row(n_total: int, n_hedge: int = 0) -> dict[str, Any]:
        n_tr = n_total // 2
        tr = gen(n_tr - n_tr // 4, n_tr // 4, "2021-01-02", P, step_days=0.25)
        te = gen((n_total - n_tr) - (n_total - n_tr) // 4, (n_total - n_tr) // 4, "2023-01-02", P, step_days=0.25)
        te += gen(n_hedge, 0, "2023-06-01", ["BTC/USDT:USDT"], tag="hedge", step_days=0.5)
        return mkrow("Q", seg(tr, "2021-01-01", "2022-12-31"), seg(te, "2023-01-01", "2023-12-31"),
                     seg(gen(30, 10, "2021-02-01", P), "2021-01-01", "2021-12-31"))

    r = run(plan, row(limit_n), pr)
    check(f"{limit_n} 笔 → 每币每年 {limit_n / total_py:.3f} 笔 ≤ 30 → 通过", (r["verdict"], r["r5"]["tpy"]["all"]["value"]),
          ("PASS", limit_n / total_py), 1e-9)
    r = run(plan, row(limit_n + 1), pr)
    check(f"{limit_n + 1} 笔 → {(limit_n + 1) / total_py:.3f} 笔 > 30 → 未通过", r["verdict"], "FAIL")
    r = run(plan, row(limit_n, n_hedge=40), pr)
    check(f"{limit_n} 笔 + 40 笔 hedge 对冲腿 → hedge 不算，仍通过", (r["verdict"], r["r5"]["tpy"]["all"]["trades"]),
          ("PASS", limit_n))
    rn = run(plan, row(limit_n, n_hedge=40), reg({"Q": {"extra_checks": [{"type": "max_trades_per_pair_year", "value": 30}]}}))
    check("没登记 count_tags_exclude：hedge 也算 → 未通过", rn["verdict"], "FAIL")


def test_count_tags(tmp: Path) -> None:
    print("4d. count_tags_exclude：样本外 35 笔里 10 笔 hedge → 只数 25 笔 < 30 → 未通过；没登记照旧 35 笔")
    P = ["AAA/USDT:USDT"]
    plan = mkplan(tmp / "nodata", P)
    add_strategy(plan, "N")

    def row() -> dict[str, Any]:
        te = gen(20, 5, "2023-01-02", P, tag="nl_short") + gen(10, 0, "2023-06-01", ["BTC/USDT:USDT"], tag="hedge")
        return mkrow("N", seg(gen(30, 10, "2021-02-01", P), "2021-01-01", "2022-12-31"), seg(te, "2023-01-01", "2023-12-31"),
                     seg(gen(30, 10, "2021-02-01", P), "2021-01-01", "2021-12-31"))

    r = run(plan, row(), reg({"N": {"extra_checks": [{"type": "count_tags_exclude", "tags": ["hedge"]}]}}))
    check("登记了：原因写明只数非 hedge 的 25 笔", r["reasons"], ["样本外只有 25 笔交易（只数 enter_tag 不是 hedge 的；< 30）"])
    check("登记了：样本外 trades_counted = 25，trades 仍是 35", (r["test"]["trades_counted"], r["test"]["trades"]), (25, 35))
    r0 = run(plan, row(), None)
    check("没登记：35 笔够了，通过；不加 trades_counted 字段", (r0["verdict"], "trades_counted" in r0["test"]), ("PASS", False))


COV_LOG = (
    "2026-11-07 00:00:01,000 - PremiumFade - INFO - [现货数据:覆盖] premfade AAA/USDT:USDT: 现货 AAA/USDT 1h 读到 100 根; "
    "永续 100 根里和现货时间戳对上 (现货有成交) 100 根 (100.0%); 数据条件都满足且 z\n"
    "有值的K线 50 根 (2023-01-01 00:00:00+00:00 ~ 2023-12-31 00:00:00+00:00)\n"
    "2026-11-07 00:00:02,000 - PremiumFade - INFO - [现货数据:覆盖] premfade BBB/USDT:USDT: 现货 BBB/USDT 1h 读到 100 根; "
    "永续 100 根里和现货时间戳对上 (现货有成交) 0 根 (0.0%); 数据条件都满足且 z 有值的K\n"
    "线 0 根 (2023-01-01 00:00:00+00:00 ~ 2023-12-31 00:00:00+00:00)\n"
    "2026-11-07 00:00:03,000 - PremiumFade - WARNING - [现货数据:没有] premfade CCC/USDT:USDT: 没有现货 CCC/USDT 1h 数据, "
    "这个币一笔都不会开\n"
)
COV_LOG2 = (
    "2026-11-07 00:00:01,000 - PremiumFade - INFO - [现货数据:覆盖] premfade BBB/USDT:USDT: 现货 BBB/USDT 1h 读到 100 根; "
    "永续 100 根里和现货时间戳对上 (现货有成交) 90 根 (90.0%); 数据条件都满足且 z 有值的K线 20 根 (2021-01-01 ~ 2022-12-31)\n"
)


def test_tradable_pairs(tmp: Path) -> None:
    print("4e. min_tradable_pairs：能交易的币（PremiumFade 日志标记，各段取并集；被折开的日志行也认得出）")
    P = ["AAA/USDT:USDT", "BBB/USDT:USDT", "CCC/USDT:USDT", "DDD/USDT:USDT"]
    plan = mkplan(tmp / "nodata", P)
    add_strategy(plan, "PF", tf="1h")
    (tmp / "logs").mkdir(exist_ok=True)
    l1, l2, l3 = tmp / "logs" / "test.log", tmp / "logs" / "train.log", tmp / "logs" / "plain.log"
    l1.write_text(COV_LOG, encoding="utf-8")
    l2.write_text(COV_LOG2, encoding="utf-8")
    l3.write_text("2026-11-07 00:00:01,000 - X - INFO - 普通日志\n", encoding="utf-8")

    def job(seg_: str, log: Path) -> dict[str, str]:
        return {"strategy": "PF", "variant": "default", "segment": seg_, "timerange": "", "status": "ok", "dir": "", "log": str(log)}

    c1 = rr.pair_capability(plan, [job("test", l1)], "PF")
    check("只有样本外的日志：AAA 能交易（z 有值 50 根），BBB 0 根、CCC 没有现货、DDD 没出现 → 1 个",
          (c1["n"], c1["tradable"], c1["tagged"]), (1, ["AAA/USDT:USDT"], True))
    c2 = rr.pair_capability(plan, [job("test", l1), job("train", l2)], "PF")
    check("训练段日志里 BBB 有 20 根 → 并集 2 个", (c2["n"], c2["tradable"]), (2, ["AAA/USDT:USDT", "BBB/USDT:USDT"]))
    c3 = rr.pair_capability(plan, [job("test", l3)], "PF")
    check("日志里没有这种标记的策略：能交易的币 = 数据检查后保留的 4 个", (c3["n"], c3["tagged"]), (4, False))

    def row() -> dict[str, Any]:
        return mkrow("PF", seg(gen(30, 10, "2021-02-01", P[:2]), "2021-01-01", "2022-12-31"),
                     seg(gen(30, 10, "2023-01-02", P[:2]), "2023-01-01", "2023-12-31"),
                     seg(gen(30, 10, "2021-02-01", P[:2]), "2021-01-01", "2021-12-31"))

    jobs = [job("test", l1), job("train", l2)]
    r = run(plan, row(), reg({"PF": {"extra_checks": [{"type": "min_tradable_pairs", "value": 3}]}}), jobs)
    check("2 个 < 3 → 测不了", (r["verdict"], rr.VERDICT_CN[r["verdict"]], len(r["untestable_reasons"])), ("UNTESTABLE", "测不了", 1))
    r = run(plan, row(), reg({"PF": {"extra_checks": [{"type": "min_tradable_pairs", "value": 2}]}}), jobs)
    check("2 个 ≥ 2 → 通过", r["verdict"], "PASS")
    bad = row()
    bad["test"]["profit_total"] = -0.1
    r = run(plan, bad, reg({"PF": {"extra_checks": [{"type": "min_tradable_pairs", "value": 3}]}}), jobs)
    check("测不了优先于未通过（样本外亏损也显示\"测不了\"，原因照样记下）", (r["verdict"], len(r["reasons"])), ("UNTESTABLE", 1))


def test_drop_top_days(tmp: Path) -> None:
    print("4f. drop_top_days：按入场日期分组，去掉盈利最多的几天后重算")
    d = [ts(f"2023-03-0{k}") for k in range(1, 7)]
    trades = [(d[0] + 3600, 30.0), (d[0] + 7200, 20.0), (d[1], 40.0), (d[2], 30.0), (d[3], 10.0), (d[4], -20.0), (d[5], -5.0)]
    e3 = rr.drop_top_days(trades, 1000.0, 3)
    check("盈利最多的 3 天：03-01（+50，两笔）、03-02（+40）、03-03（+30）",
          [(x["day"], x["profit"], x["trades"]) == y for x, y in zip(e3["days"],
           [("2023-03-01", 50.0, 2), ("2023-03-02", 40.0, 1), ("2023-03-03", 30.0, 1)])], [True, True, True])
    check("去掉以前 +105 → +10.5%；去掉以后 105 - 120 = -15 → -1.5%", (e3["profit_before"], e3["profit_after"]), [0.105, -0.015])
    check("这 3 天占这一段盈利 120 / 105", e3["share"], 120 / 105)
    check("去掉以后剩 3 笔，PF = 10 / 25", (e3["trades_after"], e3["pf_after"]), [3, 0.4])
    e2 = rr.drop_top_days(trades, 1000.0, 2)
    check("只去 2 天：105 - 90 = +15 → +1.5%", e2["profit_after"], 0.015)
    few = rr.drop_top_days([(d[0], 5.0), (d[1], -1.0), (d[2], -1.0)], 1000.0, 3)
    check("盈利为正的只有 1 天：只去那 1 天（亏的日子不去）", (len(few["days"]), few["profit_after"]), (1, -0.002))

    P = ["AAA/USDT:USDT"]
    plan = mkplan(tmp / "nodata", P)
    add_strategy(plan, "C")
    pr = reg({"C": {"extra_checks": [{"type": "drop_top_days", "n": 3, "segments": ["unseen", "oos"]}]}})
    # 没看过的一段：40 笔各 +1、10 笔各 -1，每天一笔 → 去掉 3 天还有 +27；样本外：3 天各 +20、37 笔各 -1 → 去掉以后 -37
    ho = seg(gen(40, 10, "2021-02-01", P), "2021-01-01", "2021-12-31")
    te_tr = gen(3, 0, "2023-01-02", P, win=20.0) + gen(37, 0, "2023-02-01", P, win=0.0)
    for t in te_tr[3:]:
        t["profit_abs"] = -1.0
    te_tr = te_tr + gen(10, 0, "2023-05-01", P, win=4.0)
    te = seg(te_tr, "2023-01-01", "2023-12-31")
    row = mkrow("C", seg(gen(40, 10, "2021-02-01", P), "2021-01-01", "2022-12-31"), te, ho)
    r = run(plan, row, pr)
    ev_ho, ev_te = r["r5"]["event"]["holdout"], r["r5"]["event"]["test"]
    check("没看过的一段：+40 - 10 = +3.0% → 去掉 3 天 +2.7%", (ev_ho["profit_before"], ev_ho["profit_after"]), [0.03, 0.027])
    check("样本外：60 - 37 + 40 = +6.3% → 去掉 3 个 +20 的日子 → +0.3%", (ev_te["profit_before"], ev_te["profit_after"]),
          [0.063, 0.003], 1e-12)
    check("两段去掉以后都还赚钱 → 通过", r["verdict"], "PASS")
    te_tr2 = [dict(t) for t in te_tr[:40]]
    row2 = mkrow("C", seg(gen(40, 10, "2021-02-01", P), "2021-01-01", "2022-12-31"), seg(te_tr2, "2023-01-01", "2023-12-31"), ho)
    r = run(plan, row2, pr)
    check("样本外 60 - 37 = +2.3%，去掉 3 天后 -3.7% → 未通过（原因写明是样本外）",
          (r["verdict"], [x for x in r["reasons"] if x.startswith("集中度检查")]),
          ("FAIL", ["集中度检查：样本外去掉盈利最多的 3 个入场日后收益 -3.70%"]))
    row3 = mkrow("C", seg(gen(40, 10, "2021-02-01", P), "2021-01-01", "2022-12-31"), te, None)
    r = run(plan, row3, pr, holdout=False)
    check("没有没看过的一段 → 集中度检查做不了 → 未通过", any("没看过的一段没有结果" in x for x in r["reasons"]), True)


# ----------------------------------------------------------------------------
# 5. 逐年表
# ----------------------------------------------------------------------------
def test_calendar_years(tmp: Path) -> None:
    print("5. 逐年表：训练段 + 样本外复利拼接，每年的收益和含浮亏回撤（峰值从年初起算）")
    data = tmp / "cal_data"
    A = "AAA/USDT:USDT"
    special = {
        ts("2021-08-03T10:00:00"): (100.0, 100.0, 90.0, 100.0),    # 多单持仓时最低 90
        ts("2021-11-02T05:00:00"): (100.0, 125.0, 100.0, 100.0),   # 空单持仓时最高 125
        ts("2022-02-05T12:00:00"): (100.0, 100.0, 80.0, 100.0),    # 样本外多单持仓时最低 80
    }
    flat = {t: (100.0, 100.0, 100.0, 100.0) for t in range(ts("2021-07-01"), ts("2022-07-01"), H) if t not in special}
    write_candles(data, A, "2021-07-01", "2022-07-01", {**flat, **special})
    plan = mkplan(data, [A], train=["2021-07-01", "2021-12-31"], test=["2022-01-01", "2022-06-30"], holdout=None)
    add_strategy(plan, "Y")
    tr = [trade(A, ts("2021-08-01"), ts("2021-08-11"), 100.0, 120.0),                  # +20
          trade(A, ts("2021-11-01"), ts("2021-11-05"), 100.0, 105.0, short=True)]      # -5
    te = [trade(A, ts("2022-02-01"), ts("2022-02-10"), 100.0, 95.0, amount=2.0)]      # 样本外回测里 -10
    row = mkrow("Y", seg(tr, "2021-07-01", "2021-12-31"), seg(te, "2022-01-01", "2022-06-30"))
    cy = rr.calendar_years(plan, row)
    y21, y22 = cy["years"]
    check("回撤算法 = 逐根K线（1h）", cy["method"], "K线(1h)")
    check("2021 年（07-01 起，不是完整的一年）", (y21["year"], y21["full"], y21["start"], y21["end"]),
          (2021, False, "2021-07-01", "2021-12-31"))
    check("2021 年收益：1000 → 1015 = +1.5%", y21["return"], 0.015, 1e-12)
    check("2021 年回撤：峰值 1020（第一笔平仓后），空单浮亏 25 → (1020 - 995) / 1020", y21["dd_worst"], 25 / 1020, 1e-12)
    check("2021 年只看平仓的回撤 (1020 - 1015) / 1020", y21["dd_closed"], 5 / 1020, 1e-12)
    # 样本外的交易按 训练段结束时 1015 / 1000 = 1.015 缩放：数量 2.03，盈亏 -10.15，最低 80 时浮亏 2.03 × 20 = 40.6
    check("2022 年收益：-10.15 / 1015 = -1.0%", y22["return"], -0.01, 1e-12)
    check("2022 年回撤：峰值从年初 1015 起算（不是 2021 年的最高点 1020）→ 40.6 / 1015", y22["dd_worst"], 40.6 / 1015, 1e-12)
    check("每年平仓笔数 2 / 1", (y21["trades_closed"], y22["trades_closed"]), (2, 1))
    # 和 check_drawdown 同一公式：只有 2022 年那一笔、初始资金 1015 时，回撤逐位一样
    one = [cd._trade_info(trade(A, ts("2022-02-01"), ts("2022-02-10"), 100.0, 95.0, amount=2.03))]
    one[0]["profit"] = -10.15
    ref = cd.mtm_drawdown_candles(one, 1015.0, "1h", data, "feather", "futures")
    check("2022 年回撤 = check_drawdown.mtm_drawdown_candles（单独算这一年）", y22["dd_mtm"], ref[0], 1e-12)


# ----------------------------------------------------------------------------
# 6. 2020 留出段
# ----------------------------------------------------------------------------
def test_pre_holdout(tmp: Path) -> None:
    print("6. 2020 留出段：≥ 30 笔时必须赚钱、回撤 ≤ 15%；不足 30 笔只报告；回测失败 = 失败")
    data = tmp / "ph_data"
    A = "AAA/USDT:USDT"
    write_candles(data, A, "2020-01-01", "2021-01-01")
    plan = mkplan(data, [A], pre_holdout=["2020-01-01", "2020-12-31"])
    add_strategy(plan, "H")
    cases = {
        "win35": (gen(25, 10, "2020-01-02", [A]), "PASS", 0),
        "lose35": (gen(10, 25, "2020-01-02", [A]), "FAIL", 1),
        "lose20": (gen(5, 15, "2020-01-02", [A]), "REPORT", 0),
    }
    base = mkrow("H", seg(gen(30, 10, "2021-02-01", [A]), "2021-01-01", "2022-12-31"),
                 seg(gen(30, 10, "2023-01-02", [A]), "2023-01-01", "2023-12-31"),
                 seg(gen(30, 10, "2021-02-01", [A]), "2021-01-01", "2021-12-31"))
    for name, (trades, want, n_reason) in cases.items():
        d = tmp / "ph" / name
        write_result(d, "H", trades, "2020-01-01", "2020-12-31")
        jobs = [{"strategy": "H", "variant": "default", "segment": "preholdout", "timerange": "20200101-20201231",
                 "status": "ok", "dir": str(d), "log": ""}]
        row = json.loads(json.dumps(base, default=str))
        r = run(plan, row, None, jobs)
        ph = [x for x in r["reasons"] if x.startswith("2020 留出段")]
        check(f"{len(trades)} 笔、收益 {sum(t['profit_abs'] for t in trades):+.0f} USDT → {want}，原因 {n_reason} 条",
              (r["r5"]["pre_holdout"]["verdict"], len(ph), r["verdict"]), (want, n_reason, "FAIL" if n_reason else "PASS"))
    jobs = [{"strategy": "H", "variant": "default", "segment": "pre_holdout", "timerange": "20200101-20201231",
             "status": "fail", "dir": str(tmp / "ph" / "none"), "log": ""}]
    r = run(plan, json.loads(json.dumps(base, default=str)), None, jobs)
    check("回测失败（段名写成 pre_holdout 也认）→ 未通过", (r["verdict"], "2020 留出段回测失败或没有结果" in r["reasons"]), ("FAIL", True))
    r = run(plan, json.loads(json.dumps(base, default=str)), None, [])
    check("没有跑 2020 留出段 → 不影响结论", (r["verdict"], r["r5"].get("pre_holdout")), ("PASS", None))


# ----------------------------------------------------------------------------
# 7. 端到端：research_report.py report --prereg
# ----------------------------------------------------------------------------
def _e2e_run(tmp: Path, name: str, exchange: str, strategies: dict[str, dict[str, Any]], train: tuple[str, str],
             test: tuple[str, str], holdout: tuple[str, str] | None, logs: dict | None = None,
             pre: dict[str, list] | None = None) -> Path:
    """strategies = {策略: {"pairs": [...], "train": 交易, "test": 交易, "holdout": 交易}}；每个目录自己的K线数据目录。"""
    data = tmp / f"{name}_data"
    pairs = sorted({p for s in strategies.values() for p in s["pairs"]})
    for p in pairs:
        write_candles(data, p, "2020-01-01", "2024-01-02")
    plan = mkplan(data, pairs, exchange=exchange, train=list(train), test=list(test),
                  holdout=list(holdout) if holdout else None, data_range=[train[0], test[1]], synthetic=True,
                  config=f"configs/config.{exchange}-research.json", seen_warning=[], warmup_candles=0)
    plan["settings"]["fee"] = 0.0007
    if pre:
        plan["pre_holdout"] = ["2020-01-01", "2020-12-31"]
    results = {}
    for s_name, sd in strategies.items():
        add_strategy(plan, s_name)
        rng = {"train": train, "test": test, "holdout": holdout}
        for seg_name in ("train", "test", "holdout"):
            if sd.get(seg_name) is not None and rng[seg_name]:
                a, b = rng[seg_name]
                results[(s_name, seg_name)] = (sd[seg_name], a.replace("-", "") + "-" + b.replace("-", ""))
        if pre and s_name in pre:
            results[(s_name, "preholdout")] = (pre[s_name], "20200101-20201231")
    run_dir = tmp / name
    write_run(run_dir, plan, results, logs)
    return run_dir


def test_end_to_end(tmp: Path) -> None:
    print("7. 端到端：假的研究目录 → research_report.py report --prereg")
    A, B = "AAA/USDT:USDT", "BBB/USDT:USDT"
    tr, te, ho = ("2021-01-01", "2022-12-31"), ("2023-01-01", "2023-12-31"), ("2021-01-01", "2021-12-31")
    good = {"pairs": [A, B], "train": gen(30, 10, "2021-02-01", [A, B]), "test": gen(30, 10, "2023-01-02", [A, B]),
            "holdout": gen(30, 10, "2021-02-01", [A, B])}
    strategies = {
        "XSFundingCarryFx": {**good},                                # 资金费全是 0 → 无效
        "PremiumFadeFx": {**good},                                   # 能交易的币 1 个 < 20 → 测不了
        # 没登记：训练段 PF 1.5、样本外 PF 1.15 < 1.2 → 未通过
        "OldTrend": {**good, "train": gen(30, 20, "2021-02-01", [A, B]), "test": gen(23, 20, "2023-01-02", [A, B])},
    }
    logs = {("PremiumFadeFx", "test"): COV_LOG.replace("AAA/USDT:USDT", A)}
    run_dir = _e2e_run(tmp, "e2e_okx", "okx", strategies, tr, te, ho, logs, pre={"OldTrend": gen(5, 5, "2020-02-01", [A])})
    f = tmp / "e2e_prereg.json"
    f.write_text(json.dumps({"round": 5, "strategies": {
        "XSFundingCarryFx": {"venue": "binance", "min_pf": 1.1, "min_pf_pending_decision": True,
                             "extra_checks": [{"type": "funding_net_positive"},
                                              {"type": "zero_funding_share_invalid", "max_share": 0.95}]},
        "PremiumFadeFx": {"venue": "okx", "min_pf": 1.2,
                          "extra_checks": [{"type": "max_trades_per_pair_year", "value": 30},
                                           {"type": "min_tradable_pairs", "value": 20}]},
    }, "changes": []}), encoding="utf-8")
    out = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
        rc = rr.main(["report", "--run-dir", str(run_dir), "--prereg", str(f)])
    check("report 退出码 0", rc, 0)
    text = (run_dir / "summary.txt").read_text(encoding="utf-8")
    js = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
    v = {r["strategy"]: r["verdict"] for r in js["rows"]}
    check("结论：XSFundingCarryFx 无效、PremiumFadeFx 测不了、OldTrend 未通过",
          (v["XSFundingCarryFx"], v["PremiumFadeFx"], v["OldTrend"]), ("INVALID", "UNTESTABLE", "FAIL"))
    for frag in ("| 无效 ", "| 测不了 ", "!! 待定", "预注册登记表：", "第 5 轮附加统计", "逐年表", "事件集中度", "2020 留出段",
                 "另有 无效 1 行", "附加检查（登记表）"):
        ok(f"summary.txt 里有\"{frag}\"", frag in text)
    rows = list(csv.DictReader((run_dir / "summary.csv").open(encoding="utf-8-sig")))
    head = list(rows[0].keys())
    check("summary.csv：原来的列在前面、顺序不变，新列加在后面", head[:len(rr.CSV_FIELDS) - 31] == rr.CSV_FIELDS[:len(rr.CSV_FIELDS) - 31]
          and head[-2:] == ["untestable_reasons", "formal_run"] and "funding_all" in head, True)
    old = next(r for r in js["rows"] if r["strategy"] == "OldTrend")
    check("没登记的 OldTrend：原因就是以前那一条；2020 留出段 10 笔只报告", (old["reasons"], old["r5"]["pre_holdout"]["verdict"]),
          (["样本外盈亏因子 1.15 < 1.2"], "REPORT"))
    check("summary.json 记下用的登记表", (js["preregistration"]["found"], js["preregistration"]["path"]), (True, str(f)))
    check("plan.json 没有 prereg → formal_run 列为空、抬头不写正式运行", ({r["formal_run"] for r in rows}, "正式运行：" in text),
          ({""}, False))

    print("7b. 正式运行按运行时的登记表快照判（之后改登记表不改变结论）；不是正式运行的抬头要写清楚")
    plan_p = run_dir / "plan.json"
    plan0 = json.loads(plan_p.read_text(encoding="utf-8"))
    snap_entry = {"venue": "binance", "min_pf": 1.1}             # 运行时：没有附加检查
    live = tmp / "e2e_prereg_live.json"                           # 之后改过：门槛 5、加了资金费检查
    live.write_text(json.dumps({"round": 5, "strategies": {
        "XSFundingCarryFx": {"venue": "binance", "min_pf": 5.0,
                             "extra_checks": [{"type": "zero_funding_share_invalid", "max_share": 0.95}]}}}), encoding="utf-8")
    plan0["prereg"] = {"formal_run": True, "formal_note": "这是正式的预注册运行", "registry": str(f),
                       "registry_sha256": "ab" * 32, "git": {"commit": "0123456789abcdef"}, "problems": [],
                       "strategies": {"XSFundingCarryFx": {"registered": True, "formal": True, "registry_entry": snap_entry},
                                      "OldTrend": {"registered": False, "formal": False}}}
    plan_p.write_text(json.dumps(plan0), encoding="utf-8")
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        rc = rr.main(["report", "--run-dir", str(run_dir), "--prereg", str(live)])
    text = (run_dir / "summary.txt").read_text(encoding="utf-8")
    js = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
    xs = next(r for r in js["rows"] if r["strategy"] == "XSFundingCarryFx" and r["variant"] == "default")
    check("正式运行：按快照判（门槛 1.1、没有附加检查），不按之后改过的登记表（门槛 5、资金费检查）",
          (rc, xs["rules"]["min_pf"], xs["verdict"] != "INVALID"), (0, 1.1, True))
    for frag in ("运行时快照", "!! 登记表在这次正式运行之后改过", "正式运行：是", "git 提交 0123456789"):
        ok(f"正式运行的抬头里有\"{frag}\"", frag in text, text[:3000])
    rows = {r["strategy"]: r for r in csv.DictReader((run_dir / "summary.csv").open(encoding="utf-8-sig"))}
    check("formal_run 列：登记的 = 是、没登记的 = 空", (rows["XSFundingCarryFx"]["formal_run"], rows["OldTrend"]["formal_run"]),
          ("是", ""))
    check("summary.json 记下快照和正式运行", (js["preregistration"]["snapshot"], js["preregistration"]["formal_run"],
                                          js["preregistration"]["sha256"]), (True, True, "ab" * 32))
    broken = tmp / "e2e_prereg_broken.json"
    broken.write_text("{坏的", encoding="utf-8")
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        rc = rr.main(["report", "--run-dir", str(run_dir), "--prereg", str(broken)])
    text = (run_dir / "summary.txt").read_text(encoding="utf-8")
    check("正式运行：现在的登记表坏了 → 照样按快照生成报告，抬头提醒", (rc, "现在的登记表读不出来" in text), (0, True))

    plan0["prereg"].update(formal_run=False, allow_unregistered=True, formal_note="用了 RESEARCH_ALLOW_UNREGISTERED=1",
                           problems=["XSFundingCarryFx：交易所不对"])
    plan0["prereg"]["strategies"]["XSFundingCarryFx"]["formal"] = False
    plan_p.write_text(json.dumps(plan0), encoding="utf-8")
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        rc = rr.main(["report", "--run-dir", str(run_dir), "--prereg", str(live)])
    text = (run_dir / "summary.txt").read_text(encoding="utf-8")
    js = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
    xs = next(r for r in js["rows"] if r["strategy"] == "XSFundingCarryFx" and r["variant"] == "default")
    check("不是正式运行 → 按现在的登记表判（门槛 5）", (rc, xs["rules"]["min_pf"]), (0, 5.0))
    ok("不是正式运行（RESEARCH_ALLOW_UNREGISTERED=1）→ 抬头大字写\"!! 不是正式的预注册运行\"、列出不一致的地方",
       "!! 不是正式的预注册运行（用了 RESEARCH_ALLOW_UNREGISTERED=1）" in text and "交易所不对" in text, text[:3000])
    rows = {r["strategy"]: r for r in csv.DictReader((run_dir / "summary.csv").open(encoding="utf-8-sig"))}
    check("formal_run 列 = 否", rows["XSFundingCarryFx"]["formal_run"], "否")
    plan0["prereg"].update(allow_unregistered=False, problems=[], formal_note="文件和登记表一致，但不算正式运行：读不到 git",
                           not_formal_reasons=["读不到 git", "同一个类、同一个阶段已经正式运行过"])
    plan_p.write_text(json.dumps(plan0), encoding="utf-8")
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        rr.main(["report", "--run-dir", str(run_dir), "--prereg", str(live)])
    text = (run_dir / "summary.txt").read_text(encoding="utf-8")
    ok("一致但不算正式 → 抬头写\"正式运行：否\"和原因", "正式运行：否" in text and "已经正式运行过" in text, text[:3000])


# ----------------------------------------------------------------------------
# 8. research_portfolio.py --run-dirs
# ----------------------------------------------------------------------------
def test_run_dirs(tmp: Path) -> None:
    print("8. research_portfolio.py --run-dirs：OKX 目录 + 币安目录合并（跨交易所近似）；参考趋势策略相关系数")
    A, B, C = "AAA/USDT:USDT", "BBB/USDT:USDT", "CCC/USDT:USDT"
    tr, te, ho = ("2021-01-01", "2022-12-31"), ("2023-01-01", "2023-12-31"), ("2021-01-01", "2021-12-31")

    def strat(pairs: list[str], first_tr: str, first_te: str) -> dict[str, Any]:
        return {"pairs": pairs, "train": gen(30, 10, first_tr, pairs, win=3.0), "test": gen(30, 10, first_te, pairs, win=3.0),
                "holdout": gen(30, 10, first_tr, pairs, win=3.0)}

    okx = _e2e_run(tmp, "rd_okx", "okx", {"TsmomTrend": strat([A], "2021-02-01", "2023-01-02"),
                                           "CandOkx": strat([A], "2021-02-02", "2023-01-03")}, tr, te, ho)
    bn = _e2e_run(tmp, "rd_binance", "binance", {"CandBinance": strat([B, C], "2021-02-01", "2023-01-02"),
                                                  "TsmomTrend": strat([B], "2021-03-01", "2023-02-01")},
                  tr, ("2023-01-01", "2023-12-30"), ho)
    out_dir = tmp / "rd_out"
    out = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
        rc = rp.main(["--run-dirs", str(okx), str(bn), "--out-dir", str(out_dir), "--strategies", "CandOkx,CandBinance"])
    text = out.getvalue()
    check("退出码 0", rc, 0)
    ok("输出标注\"跨交易所近似\"", "!! 跨交易所近似" in text and "组合结论：" in text and "（跨交易所近似）" in text)
    ok("写出 portfolio-merged.txt/csv/json", all((out_dir / f"portfolio-merged.{x}").is_file() for x in ("txt", "csv", "json")))
    js = json.loads((out_dir / "portfolio-merged.json").read_text(encoding="utf-8"))
    check("组合成员：两个目录各一个", js["selection"]["selected"], ["CandOkx", "CandBinance"])
    methods = {m["strategy"]: m["dd_mtm_method"] for m in js["segments"]["test"]["members"]}
    check("币安目录的策略用它自己的数据目录读K线（不是\"估\"）", methods.get("CandBinance"), "K线(1h)")
    check("OKX 目录的策略也是", methods.get("CandOkx"), "K线(1h)")
    mg = js["merged"]
    check("合并信息：两个交易所、跨交易所", (mg["exchanges"], mg["cross_exchange"]), (["okx", "binance"], True))
    ok("重名的 TsmomTrend 用前面那个目录的，并且有提醒", any("TsmomTrend" in d and "rd_okx" in d for d in mg["dups"]), mg["dups"])
    ok("两个目录的样本外不一样 → 有提醒", any("样本外" in d for d in mg["window_diff"]), mg["window_diff"])
    rc_ = js["ref_corr"]
    check("参考策略 / 候选：TsmomTrend vs CandOkx、CandBinance", (rc_["refs"], rc_["candidates"]),
          (["TsmomTrend"], ["CandOkx", "CandBinance"]))
    ok("相关系数表在输出里", "和参考趋势策略的相关系数（训练段" in text)
    c = rc_["matrix"][0][0]
    ok("CandOkx 和 TsmomTrend 同一个币、同样的交易节奏（错开一天）：相关系数算得出来、在 -1 ~ 1 之间",
       c is not None and -1.0 <= c <= 1.0, c)

    print("8b. 指定策略（--strategies）：有登记过的第 5 轮策略 → 也按训练段相关系数 ≥ 0.7 去重；没登记的照旧不去重、只提醒")
    same = strat([A], "2021-02-01", "2023-01-02")
    dup = _e2e_run(tmp, "rd_dup", "okx", {"PremiumFade": same, "CapitulationRebound": json.loads(json.dumps(same)),
                                          "OldA": json.loads(json.dumps(same)), "OldB": json.loads(json.dumps(same))},
                   tr, te, ho)
    out = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
        rc = rp.main(["--run-dir", str(dup), "--strategies", "PremiumFade,CapitulationRebound"])
    text = out.getvalue()
    js = json.loads((dup / "portfolio.json").read_text(encoding="utf-8"))
    check("登记的两个策略资金曲线一样（相关系数 1）→ 只留一个", (rc, len(js["selection"]["selected"]),
                                                      len(js["selection"]["dropped_corr"])), (0, 1, 1))
    ok("输出写明按预注册第 3.4 节去重、去掉了哪个", "按预注册第 3.4 节" in text and "去掉 " in text, text[:2500])
    ok("plan.json 没有预注册凭证 → 写明不是正式的预注册运行", "!! 不是正式的预注册运行" in text, text[:2500])
    out = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
        rc = rp.main(["--run-dir", str(dup), "--strategies", "OldA,OldB", "--no-write"])
    text = out.getvalue()
    ok("没登记的两个策略指定 → 两个都留、大字提醒训练段高度相关",
       rc == 0 and "!! OldA 和 OldB 训练段日收益相关系数 1.00" in text and "按预注册第 3.4 节" not in text
       and "不是正式的预注册运行" not in text, text[:2500])


# ----------------------------------------------------------------------------
# 9. 成本诊断（scripts/research_costs.py）
# ----------------------------------------------------------------------------
def test_costs(tmp: Path) -> None:
    print("9. research_costs.py：成本、资金费、扣成本前收益（手算）")
    import research_costs as rc
    A = "AAA/USDT:USDT"
    o = ts("2023-01-02T00:00:00")
    # 做多 2 个，100 → 110，每边费率 0.001，资金费 -0.5：成本 0.2 + 0.22 = 0.42，净收益 20 - 0.42 - 0.5 = 19.08
    t1 = trade(A, o, o + DAY, 100.0, 110.0, amount=2.0, funding=-0.5)
    # 做空 1 个，50 → 55，每边费率 0.001，资金费 +0.3：成本 0.05 + 0.055 = 0.105，净收益 -5 - 0.105 + 0.3 = -4.805
    t2 = trade(A, o + 2 * DAY, o + 3 * DAY, 50.0, 55.0, amount=1.0, short=True, funding=0.3)
    for tr, cost in ((t1, 0.42), (t2, 0.105)):
        tr["fee_open"] = tr["fee_close"] = 0.001
        tr["profit_abs"] -= cost
    check("一笔的成本（做多）", rc.trade_costs(t1)[0], 0.42)
    check("一笔的成本（做空）", rc.trade_costs(t2)[0], 0.105)
    s = rc.summarize([t1, t2])
    check("合计：交易、平均名义、成本、资金费、净收益",
          [s["trades"], s["avg_notional"], s["cost"], s["funding"], s["net"]], [2, 125.0, 0.525, -0.2, 14.275])
    check("扣成本前 = 净收益 + 成本；价格部分 = 扣成本前 − 资金费", [s["pre_cost"], s["price"]], [14.8, 15.0])
    check("净 PF = 19.08 / 4.805；扣成本前 PF = 19.5 / 4.7", [s["pf"], s["pf_pre_cost"]], [19.08 / 4.805, 19.5 / 4.7])
    check("成本占扣成本前 = 0.525 / 14.8", s["cost_share"], 0.525 / 14.8)
    check("扣成本前亏钱 → 成本占比没有数", rc.summarize([t2])["cost_share"], None)
    check("没有交易 → 0 笔、平均名义没有数", [rc.summarize([])["trades"], rc.summarize([])["avg_notional"]], [0, None])
    run_dir = tmp / "e2e_okx"
    if not run_dir.is_dir():
        ok("端到端的假研究目录存在（第 7 节先建）", False)
        return
    data = rc.collect(run_dir, ["OldTrend"])
    segs = [seg for seg, _ in data["OldTrend"]]
    check("段的顺序：训练段、没看过的一段、样本外、2020 留出段", segs, ["train", "holdout", "test", "preholdout"])
    train = dict(data["OldTrend"])["train"]
    check("手续费 0 的假交易：成本 0、扣成本前 = 净收益", [train["cost"], train["pre_cost"] - train["net"]], [0.0, 0.0])
    lines = rc.render(data, run_dir)
    ok("表格里有\"扣成本前 PF\"和 2020 那一段的中文名", any("扣成本前 PF" in x for x in lines)
       and any("研究开始前的留出段" in x for x in lines))


# ----------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        test_prereg_file(tmp)
        test_min_pf_and_segments(tmp)
        test_funding(tmp)
        test_trades_per_pair_year(tmp)
        test_count_tags(tmp)
        test_tradable_pairs(tmp)
        test_drop_top_days(tmp)
        test_calendar_years(tmp)
        test_pre_holdout(tmp)
        test_end_to_end(tmp)
        test_run_dirs(tmp)
        test_costs(tmp)
    print()
    if FAILS:
        print(f"不通过：有 {len(FAILS)} / {COUNT} 项不对：")
        for f in FAILS:
            print(f"  - {f}")
        return 1
    print(f"通过：全部 {COUNT} 项。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
