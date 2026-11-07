#!/usr/bin/env python3
"""
NFI X8 逐年回测（scripts/research_nfi_yearly.sh，make research-nfi-yearly）的辅助脚本：选交易对、检查数据、出中文报告。
说明：docs/nfi-yearly.md

子命令（一般由 research_nfi_yearly.sh 调用，不用自己运行）：
  pairs   从研究配置的交易对列表里取前 N 个（BTC 一定在里面），写 <结果目录>/pairs.json；
          打印每个周期要从哪天开始下载（NFI 的指标预热：OKX 上 480 根，1d 周期就是 480 天）
  plan    下载完以后检查每个币每一年有没有数据，定下每一年的回测区间（今年 = 到数据的最后一天），
          写 plan.json / plan.sh / data_check.txt
  report  读每一次回测的结果，出每个版本、每一年一行的中文表格 + 结论（summary.txt / summary.csv / summary.json）

重新出报告（不重跑回测）：
  .venv/bin/python scripts/nfi_yearly_report.py report --run-dir user_data/research/nfi_yearly/run_<时间>
  .venv/bin/python scripts/nfi_yearly_report.py report --run-dir <结果目录> --max-dd 12    # 按别的回撤上限重新判

回撤口径和 scripts/check_drawdown.py 完全一样（直接调用它的 evaluate()，不另外实现）：
  回撤(含浮亏) = 账户权益（已平仓余额 + 持仓浮动盈亏）从最高点到最低点的跌幅；回撤(平仓) = 只看已平仓交易。
  加过仓（DCA）的交易怎么算浮亏，以 check_drawdown.py 开头的说明为准。

退出码：0 = 完成（不管结论是不是"可以上模拟盘"）；1 = 参数错误、找不到结果目录 / plan.json、数据检查失败；2 = 其他出错。
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import shlex
import sys
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any


sys.path.insert(0, str(Path(__file__).resolve().parent))
import check_drawdown as cd  # noqa: E402  —— 回撤、杠杆的口径全部从这里来


# ----------------------------------------------------------------------------
# 常量（都和 user_data/strategies/benchmarks/BenchmarkCaps.py、NFI X8 固定版本对应）
# ----------------------------------------------------------------------------
# 三个版本的杠杆封顶（倍）。改 BenchmarkCaps.py 的 _easytrade_leverage_cap 时这里一起改
CLASS_CAPS: dict[str, float] = {
    "NostalgiaForInfinityX8Lev1": 1.0,
    "NostalgiaForInfinityX8Lev2": 2.0,
    "NostalgiaForInfinityX8Capped": 5.0,
}
DEFAULT_CLASSES = ["NostalgiaForInfinityX8Lev1", "NostalgiaForInfinityX8Lev2", "NostalgiaForInfinityX8Capped"]
# NFI X8（v18.0.214）在 OKX 上的 startup_candle_count（源码 __init__ 里 exchange_name in ["okx", "okex"] → 480）。
# freqtrade 回测时每个参考周期都按"这么多根 × 这个周期"往前多加载数据，所以 1d 要 480 天、4h 要 80 天
NFI_STARTUP_CANDLES_OKX = 480
TIMEFRAMES = ["5m", "15m", "1h", "4h", "1d"]  # 5m 主周期 + NFI 的 info_timeframes（BTC 另外用 4h，BTC 在交易对里）
MAIN_TF = "5m"
FIRST_YEAR_DEFAULT = 2021

# 判定规则的默认值（写进 plan.json，以后重新出报告仍按当时的值判；report 可以用参数改）
DEFAULT_MAX_LOSING_YEARS = 1
DEFAULT_MAX_YEAR_LOSS_PCT = 5.0
DEFAULT_MIN_FULL_YEARS = 3
# 一年里至少这么多比例的币有完整数据，这一年才算"完整年份"
FULL_YEAR_MIN_PAIR_SHARE = 0.5

VERDICT_GO = "可以上模拟盘"
VERDICT_NO = "不建议上模拟盘"

IN_SAMPLE_WARNING = [
    "!! 先看这一段：NostalgiaForInfinity X8 这个文件是 2026-09-06 才建的，到 2026-11-07 已经改了约 1200 次（其中约 490 次的标题是 \"add protection\"），",
    "   作者一直用最近几年的行情调它。所以这里的每一年（包括 2021、2022）都可能是作者调参时看过的数据（样本内）。",
    "   这张表\"通过\"只说明\"没出大问题\"（合理性检查），不能证明以后能赚钱；\"不通过\"才是有用的信息（连它自己调过的年份都扛不住）。",
    "   真正的样本外检验是接下来 2 ~ 3 个月在币安的模拟盘（前向测试）。",
]


# ----------------------------------------------------------------------------
# 小工具
# ----------------------------------------------------------------------------
def _ymd(d: date) -> str:
    return d.strftime("%Y%m%d")


def _iso(d: date) -> str:
    return d.isoformat()


def _pct(v: float | None, signed: bool = False) -> str:
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return "-"
    return f"{v * 100:+.2f}%" if signed else f"{v * 100:.2f}%"


def _num(v: float | None, nd: int = 2) -> str:
    if v is None:
        return "-"
    if math.isinf(v):
        return "∞"
    return f"{v:.{nd}f}"


def _usdt(v: float | None) -> str:
    """带正负号的 USDT 金额，一位小数（-0.0 显示成 +0.0）。"""
    x = v or 0.0
    if abs(x) < 0.05:
        x = 0.0
    return f"{x:+.1f}"


def _strip(obj: Any) -> Any:
    """JSON 里不能有 inf / nan：换成字符串（同 research_report.py）。"""
    if isinstance(obj, dict):
        return {k: _strip(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_strip(v) for v in obj]
    if isinstance(obj, float) and (math.isinf(obj) or math.isnan(obj)):
        return str(obj)
    return obj


def _write_json(path: Path, data: Any) -> None:
    path.write_text(json.dumps(_strip(data), ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")


def _short(cls: str) -> str:
    return cls.replace("NostalgiaForInfinityX8", "") or cls


def _cap_text(cap: float) -> str:
    return f"{cap:g} 倍"


def _coin(pair: str) -> str:
    return pair.split("/")[0]


def _parse_years(text: str, first_default: int, today: date) -> list[int]:
    years: set[int] = set()
    for tok in (text or "").replace(",", " ").split():
        if "-" in tok:
            a, _, b = tok.partition("-")
            lo, hi = int(a), int(b)
            years.update(range(min(lo, hi), max(lo, hi) + 1))
        else:
            years.add(int(tok))
    if not years:
        years = set(range(first_default, today.year + 1))
    bad = [y for y in years if y < 2017 or y > today.year]
    if bad:
        raise ValueError(f"年份要在 2017 ~ {today.year} 之间（收到：{' '.join(map(str, sorted(bad)))}）")
    return sorted(years)


def _load_config(path: str) -> dict[str, Any]:
    from freqtrade.configuration.load_config import load_from_files

    return load_from_files([path])


def _btc_pair(cfg: dict[str, Any]) -> str:
    stake = str(cfg.get("stake_currency") or "USDT")
    mode = str(cfg.get("trading_mode") or "spot")
    return f"BTC/{stake}:{stake}" if mode in ("futures", "margin") else f"BTC/{stake}"


def _data_file(datadir: Path, pair: str, tf: str, mode: str, fmt: str) -> Path:
    sub = datadir / "futures" if mode == "futures" else datadir
    suffix = "-futures" if mode == "futures" else ""
    return sub / f"{cd._pair_file(pair)}-{tf.replace('M', 'Mo')}{suffix}.{fmt}"


def _span(datadir: Path, pair: str, tf: str, mode: str, fmt: str) -> tuple[datetime, datetime, int] | None:
    """本地K线的第一根、最后一根（UTC）和根数；没有文件或读不出来返回 None。用 freqtrade 自己的数据读取代码。"""
    if not _data_file(datadir, pair, tf, mode, fmt).is_file():
        return None
    from freqtrade.data.history import get_datahandler
    from freqtrade.enums import CandleType

    ct = CandleType.FUTURES if mode == "futures" else CandleType.SPOT
    try:
        mn, mx, n = get_datahandler(datadir, fmt).ohlcv_data_min_max(pair, tf, ct)
    except Exception:  # noqa: BLE001 —— 文件坏了按没有数据算
        return None
    if not n:
        return None
    return mn.astimezone(UTC), mx.astimezone(UTC), int(n)


def _warmup_days(tf: str) -> int:
    """为了让某一年 1 月 1 日的指标是热的，这个周期要往前多下载几天（NFI 在 OKX 上预热 480 根；至少 31 天）。"""
    secs = cd._tf_seconds(tf)
    return max(31, math.ceil(NFI_STARTUP_CANDLES_OKX * secs / 86400) + 5)


# ----------------------------------------------------------------------------
# pairs：选交易对 + 下载计划
# ----------------------------------------------------------------------------
def cmd_pairs(a: argparse.Namespace) -> int:
    today = datetime.now(UTC).date()
    try:
        years = _parse_years(a.years, a.first_year, today)
    except ValueError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1
    cfg = _load_config(a.config)
    mode = str(cfg.get("trading_mode") or "spot")
    fmt = str(cfg.get("dataformat_ohlcv") or "feather")
    btc = _btc_pair(cfg)
    wl = [str(p) for p in ((cfg.get("exchange") or {}).get("pair_whitelist") or [])]
    if not wl:
        print(f"错误：研究配置 {a.config} 里没有交易对", file=sys.stderr)
        return 1
    if a.require_data:
        # 合成数据：只用本地有 5m 数据的币（研究列表里大部分币合成数据里没有）
        datadir = Path(a.datadir)
        wl = [p for p in wl if _data_file(datadir, p, MAIN_TF, mode, fmt).is_file()]
        if btc not in wl:
            print(f"错误：{datadir} 里没有 {btc} 的 {MAIN_TF} 数据（NFI 要看 BTC）", file=sys.stderr)
            return 1
    if a.n == "all":
        picked = list(wl)
    else:
        try:
            n = int(a.n)
        except ValueError:
            print(f"错误：--n 要是正整数或 all（收到：{a.n}）", file=sys.stderr)
            return 1
        if n < 1:
            print("错误：--n 至少 1", file=sys.stderr)
            return 1
        picked = wl[:n]
    if btc not in picked:
        # NFI 每个币都要看 BTC 的 4h（BTC 不在前 N 个里时换掉最后一个）
        picked = [btc] + (picked[:-1] if a.n != "all" and len(picked) >= 1 else picked)
    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "{",
        "    // 自动生成（scripts/research_nfi_yearly.sh → scripts/nfi_yearly_report.py pairs），不要手改。",
        f"    // NFI X8 逐年回测用的交易对：{a.config} 的交易对列表取前 {len(picked)} 个（{a.n}；BTC 一定在里面，NFI 要看 BTC）。",
        "    // 叠加在研究配置后面（第二个 --config），只替换交易对列表，其他设置（手续费、资金、逐仓等）都来自研究配置。",
        '    "exchange": {',
        '        "pair_whitelist": [',
        ",\n".join(f"            {json.dumps(p)}" for p in picked),
        "        ]",
        "    }",
        "}",
    ]
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")

    # 下载计划：每个周期从"第一年 1 月 1 日 - 预热天数"开始；同一天开始的周期合成一组
    first = date(years[0], 1, 1)
    groups: dict[str, list[str]] = {}
    for tf in TIMEFRAMES:
        groups.setdefault(_ymd(first - timedelta(days=_warmup_days(tf))), []).append(tf)
    dl = "|".join(f"{start}:{' '.join(tfs)}" for start, tfs in sorted(groups.items(), reverse=True))

    def emit(k: str, v: Any) -> None:
        print(f"{k}={shlex.quote(str(v))}")

    emit("NY_PAIRS", " ".join(picked))
    emit("NY_PAIRS_N", len(picked))
    emit("NY_PAIRS_COINS", " ".join(_coin(p) for p in picked))
    emit("NY_YEARS", " ".join(map(str, years)))
    emit("NY_DL_GROUPS", dl)
    return 0


# ----------------------------------------------------------------------------
# plan：数据检查 + 每一年的回测区间
# ----------------------------------------------------------------------------
def cmd_plan(a: argparse.Namespace) -> int:
    run_dir = Path(a.run_dir)
    pairs_file = run_dir / "pairs.json"
    if not pairs_file.is_file():
        print(f"错误：找不到 {pairs_file}（先运行 pairs 子命令）", file=sys.stderr)
        return 1
    today = datetime.now(UTC).date()
    try:
        years = _parse_years(a.years, a.first_year, today)
    except ValueError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1
    classes = a.classes.split() if a.classes else list(DEFAULT_CLASSES)
    unknown = [c for c in classes if c not in CLASS_CAPS]
    if unknown:
        print(f"错误：不认识的版本 {' '.join(unknown)}（可选：{' '.join(CLASS_CAPS)}）", file=sys.stderr)
        return 1
    try:
        max_dd = cd.parse_max_dd_pct(a.max_dd, "--max-dd ") if a.max_dd else cd.default_max_dd_pct()
    except ValueError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1

    cfg = _load_config(a.config)
    import rapidjson

    pairs_cfg = rapidjson.loads(
        pairs_file.read_text(encoding="utf-8"), parse_mode=rapidjson.PM_COMMENTS | rapidjson.PM_TRAILING_COMMAS
    )
    pairs = [str(p) for p in pairs_cfg["exchange"]["pair_whitelist"]]
    mode = str(cfg.get("trading_mode") or "spot")
    fmt = str(cfg.get("dataformat_ohlcv") or "feather")
    exch = str((cfg.get("exchange") or {}).get("name") or "").lower()
    datadir = Path(a.datadir) if a.datadir else Path("user_data") / "data" / exch
    btc = _btc_pair(cfg)

    spans: dict[str, dict[str, Any]] = {}
    for p in pairs:
        spans[p] = {tf: _span(datadir, p, tf, mode, fmt) for tf in TIMEFRAMES}
    main_last = [s[MAIN_TF][1] for s in spans.values() if s[MAIN_TF]]
    if not main_last:
        print(f"错误：{datadir} 里这些币一个 {MAIN_TF} 数据都没有（下载失败？看 logs/download.log）", file=sys.stderr)
        return 1
    # 今年的区间到哪天为止（不含这一天）：最新一根 5m K线结束的那一刻所在的日期 —— 最后一天没走完就不要它（也方便复现）
    tf5 = timedelta(seconds=cd._tf_seconds(MAIN_TF))
    data_end_day = (max(main_last) + tf5).date()

    year_plans: list[dict[str, Any]] = []
    for y in years:
        y0, y1 = date(y, 1, 1), date(y + 1, 1, 1)
        end = min(y1, data_end_day)
        yp: dict[str, Any] = {
            "year": y, "start": _iso(y0), "end": _iso(end), "last_day": _iso(end - timedelta(days=1)),
            "timerange": f"{_ymd(y0)}-{_ymd(end)}",
            "calendar_complete": y < today.year and end == y1, "pairs": {}, "skip": "",
        }
        if end <= y0:
            yp["skip"] = f"没有数据（数据只到 {_iso(data_end_day - timedelta(days=1))}）"
            yp["full"] = False
            year_plans.append(yp)
            continue
        t0 = datetime(y, 1, 1, tzinfo=UTC)
        t1 = datetime(end.year, end.month, end.day, tzinfo=UTC)
        n_full = 0
        for p in pairs:
            s5 = spans[p][MAIN_TF]
            info: dict[str, Any] = {"status": "none", "first": "", "last": "", "warmup_short": [], "missing_tf": []}
            if s5 is not None:
                first, last = s5[0], s5[1]
                lo, hi = max(first, t0), min(last + tf5, t1)
                if hi > lo:
                    full = first <= t0 + timedelta(days=1) and last + tf5 >= t1 - timedelta(days=1)
                    info.update(status="full" if full else "partial", first=_iso(lo.date()), last=_iso((hi - tf5).date()))
            if info["status"] != "none":
                for tf in TIMEFRAMES[1:]:
                    st = spans[p][tf]
                    if st is None:
                        info["missing_tf"].append(tf)
                        continue
                    # 这个币年初已经在交易（5m 数据从年初前就有），参考周期却开始得太晚：年初那段指标是冷的
                    need = t0 - timedelta(seconds=cd._tf_seconds(tf) * NFI_STARTUP_CANDLES_OKX)
                    if s5 is not None and s5[0] <= t0 and st[0] > need + timedelta(days=1):
                        info["warmup_short"].append(tf)
            if info["status"] == "full":
                n_full += 1
            yp["pairs"][p] = info
        btc_full = yp["pairs"].get(btc, {}).get("status") == "full"
        yp["n_full"] = n_full
        yp["btc_full"] = btc_full
        yp["full"] = bool(yp["calendar_complete"] and btc_full and n_full >= FULL_YEAR_MIN_PAIR_SHARE * len(pairs))
        if n_full == 0 and all(i["status"] == "none" for i in yp["pairs"].values()):
            yp["skip"] = "这些币这一年一根 5m K线都没有"
        year_plans.append(yp)

    settings = {
        "max_dd": max_dd,
        "fee": a.fee,
        "max_losing_years": DEFAULT_MAX_LOSING_YEARS,
        "max_year_loss_pct": DEFAULT_MAX_YEAR_LOSS_PCT,
        "min_full_years": DEFAULT_MIN_FULL_YEARS,
        "runner": a.runner,
        "synthetic": bool(a.synthetic),
    }
    plan = {
        "created": datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC"),
        "run_dir": str(run_dir),
        "config": a.config,
        "pairs_config": str(pairs_file),
        "datadir": str(datadir),
        "exchange": exch,
        "trading_mode": mode,
        "dataformat": fmt,
        "btc_pair": btc,
        "pairs": pairs,
        "classes": classes,
        "caps": {c: CLASS_CAPS[c] for c in classes},
        "years": year_plans,
        "data_end_day": _iso(data_end_day),
        "today": _iso(today),
        "nfi_startup_candles": NFI_STARTUP_CANDLES_OKX,
        "strategy_files": a.strategy_files or "",
        "settings": settings,
    }
    _write_json(run_dir / "plan.json", plan)

    run_years = [yp["year"] for yp in year_plans if not yp["skip"]]
    sh = [
        "# 自动生成（scripts/nfi_yearly_report.py plan），scripts/research_nfi_yearly.sh 用 source 读入",
        f"NY_CLASSES={shlex.quote(' '.join(classes))}",
        f"NY_RUN_YEARS={shlex.quote(' '.join(map(str, run_years)))}",
        f"NY_SKIP_YEARS={shlex.quote(' '.join(str(yp['year']) for yp in year_plans if yp['skip']))}",
        f"NY_PLAN_DATADIR={shlex.quote(str(datadir))}",
        f"NY_PLAN_FEE={shlex.quote(str(a.fee))}",
    ]
    for yp in year_plans:
        sh.append(f"NY_TR_{yp['year']}={shlex.quote(yp['timerange'])}")
    (run_dir / "plan.sh").write_text("\n".join(sh) + "\n", encoding="utf-8")

    text = render_data_check(plan)
    (run_dir / "data_check.txt").write_text(text + "\n", encoding="utf-8")
    print(text)
    return 0


def _year_data_lines(plan: dict[str, Any], yp: dict[str, Any], indent: str = "  ") -> list[str]:
    """一年里数据不全 / 没有数据 / 预热不足的币，一行一类。"""
    out: list[str] = []
    pinfo = yp.get("pairs") or {}
    none = [_coin(p) for p, i in pinfo.items() if i["status"] == "none"]
    part = [f"{_coin(p)}({i['first']}~{i['last']})" for p, i in pinfo.items() if i["status"] == "partial"]
    miss = [f"{_coin(p)}(缺 {'/'.join(i['missing_tf'])})" for p, i in pinfo.items() if i.get("missing_tf")]
    warm = [f"{_coin(p)}({'/'.join(i['warmup_short'])})" for p, i in pinfo.items() if i.get("warmup_short")]
    if none:
        out.append(f"{indent}这一年没有数据：{' '.join(none)}（多半是那时还没上线；这一年等于少了这些币）")
    if part:
        out.append(f"{indent}只有一部分时间有数据：{' '.join(part)}")
    if miss:
        out.append(f"{indent}!! 缺参考周期的数据（NFI 要用，回测可能出错或少了这个币的信号）：{' '.join(miss)}")
    if warm:
        out.append(
            f"{indent}参考周期的预热数据不够（年初那段这些周期的指标是冷的；NFI 在 OKX 上每个周期预热 "
            f"{plan.get('nfi_startup_candles', NFI_STARTUP_CANDLES_OKX)} 根，1d 就是 480 天）：{' '.join(warm)}"
        )
    return out


def _yp_last(yp: dict[str, Any]) -> str:
    """这一年回测的最后一天（含）。"""
    if yp.get("last_day"):
        return str(yp["last_day"])
    return _iso(date.fromisoformat(str(yp["end"])) - timedelta(days=1))


def _last_day(plan: dict[str, Any]) -> str:
    return _iso(date.fromisoformat(str(plan["data_end_day"])) - timedelta(days=1))


def render_data_check(plan: dict[str, Any]) -> str:
    L = [
        "================ NFI X8 逐年回测：数据检查 ================",
        f"数据目录：{plan['datadir']}    数据最后一个完整的日子：{_last_day(plan)}（今年的区间到这一天为止）",
        f"交易对（{len(plan['pairs'])} 个）：{' '.join(_coin(p) for p in plan['pairs'])}",
        "",
    ]
    for yp in plan["years"]:
        if yp["skip"]:
            L.append(f"{yp['year']}：跳过 —— {yp['skip']}")
            continue
        kind = "完整的一年" if yp["full"] else ("今年（到数据最后一天）" if not yp["calendar_complete"] else "数据不完整")
        L.append(
            f"{yp['year']}：{yp['start']} ~ {_yp_last(yp)}  {kind}；"
            f"整年有数据的币 {yp.get('n_full', 0)}/{len(plan['pairs'])}"
            + ("" if yp.get("btc_full") else "  !! BTC 这一年数据不全")
        )
        L.extend(_year_data_lines(plan, yp, "    "))
    L.append("")
    L.append(
        f"\"完整的一年\" = 已经过完的日历年 + BTC 整年有数据 + 至少 {FULL_YEAR_MIN_PAIR_SHARE:.0%} 的币整年有数据；"
        "判定里的\"完整年份\"只数这些年。"
    )
    return "\n".join(L)


# ----------------------------------------------------------------------------
# report：读结果 → 每个版本每年一行 → 结论
# ----------------------------------------------------------------------------
def load_plan(run_dir: Path) -> dict[str, Any]:
    return json.loads((run_dir / "plan.json").read_text(encoding="utf-8"))


def load_jobs(run_dir: Path) -> dict[tuple[str, int], dict[str, str]]:
    """jobs.tsv：版本、年份、区间、状态、结果目录、日志。同一个（版本, 年份）有多行时取最后一行（--resume 重跑过）。"""
    jobs: dict[tuple[str, int], dict[str, str]] = {}
    path = run_dir / "jobs.tsv"
    if not path.is_file():
        return jobs
    for line in path.read_text(encoding="utf-8").splitlines():
        parts = line.split("\t")
        if len(parts) < 6:
            continue
        try:
            year = int(parts[1])
        except ValueError:
            continue
        jobs[(parts[0], year)] = {
            "class": parts[0], "year": parts[1], "timerange": parts[2], "status": parts[3], "out": parts[4], "log": parts[5],
            "rc": parts[6] if len(parts) > 6 else "",
        }
    return jobs


def _result_file(out_dir: Path) -> Path | None:
    files = [f for f in cd.collect_files([str(out_dir)]) if f.suffix == ".zip"] if out_dir.is_dir() else []
    return sorted(files, key=lambda f: f.stat().st_mtime)[-1] if files else None


def _profit_factor(profits: list[float]) -> float | None:
    """盈亏因子 = 总盈利 ÷ 总亏损（按每笔交易的 profit_abs，和 research_report.py 相同）。"""
    gains = sum(p for p in profits if p > 0)
    losses = -sum(p for p in profits if p < 0)
    if not profits:
        return None
    if losses <= 0:
        return math.inf if gains > 0 else None
    return gains / losses


def _orders(t: dict[str, Any]) -> list[dict[str, Any]]:
    return [o for o in (t.get("orders") or []) if isinstance(o, dict)]


def _worst_floating(t: dict[str, Any]) -> tuple[float, float] | None:
    """一笔已平仓交易持仓期间最多亏了多少：(亏损 USDT >= 0, 那一段持仓里价格相对均价逆向走了多少)。读不出来返回 None。

    亏损 = -check_drawdown._trade_mae（含这段之前分批平仓已实现的盈亏和手续费）；止损单的影线按止损价截断。"""
    info = cd.trade_info(t)
    if info is None:
        return None
    d = info["dir"]
    bad = info["max_rate"] if d < 0 else info["min_rate"]
    if info["stop_exit"]:
        # 止损那根K线越过止损价的影线，账户并没有承受（同 check_drawdown.mtm_drawdown_bound）
        bad = min(bad, info["close_rate"]) if d < 0 else max(bad, info["close_rate"])
    loss = -cd._trade_mae(info, bad)
    legs = info.get("legs")
    if legs:
        f = cd._leg_scale(info)
        _pnl, avg = min(
            (f * r + cd._upnl_leg(d, f * amt, a, info["fee_open"], info["fee_close"], bad), a)
            for _s, _e, amt, a, r in legs
        )
    else:
        avg = info["open_rate"]
    move = ((bad - avg) / avg if d < 0 else (avg - bad) / avg) if avg > 0 else 0.0
    return max(loss, 0.0), max(move, 0.0)


def trade_extremes(trades: list[dict[str, Any]], start_balance: float) -> dict[str, Any]:
    """每一年的交易明细统计：最差单笔浮亏（估）、最长持仓、加仓笔数、年底被强制平掉的仓位。

    最差单笔浮亏（估算，只用回测结果，不读K线）：和 check_drawdown.py 的估算法同一个算法（_trade_mae）——
    加仓 / 分批平仓的交易按订单重建每一段持仓（数量、均价、这段之前分批平仓已实现的盈亏），
    每一段都按 freqtrade 记的整个持仓期间最低价 / 最高价算（含手续费），取最差的一段。
    最差价格不一定出现在最差的那一段里，所以只会算多、不会算少（偏保守）；先减仓认亏再低价加仓（NFI 的 derisk + grind）
    这种情况，减仓时已实现的亏损也算在里面。
    """
    worst: dict[str, Any] | None = None
    longest: dict[str, Any] | None = None
    n_dca = 0
    n_partial = 0
    fe_n = 0
    fe_profit = 0.0
    for t in trades:
        try:
            open_rate = float(t["open_rate"])
            o_ts = datetime.fromisoformat(str(t["open_date"]))
            c_ts = datetime.fromisoformat(str(t["close_date"]))
        except (KeyError, TypeError, ValueError):
            continue
        if open_rate <= 0:
            continue
        short = bool(t.get("is_short"))
        orders = _orders(t)
        if sum(1 for o in orders if o.get("ft_is_entry")) > 1:
            n_dca += 1
        if sum(1 for o in orders if not o.get("ft_is_entry")) > 1:
            n_partial += 1
        wf = _worst_floating(t)
        loss, move = wf if wf is not None else (0.0, 0.0)
        if wf is not None and (worst is None or loss > worst["loss"]):
            worst = {
                "loss": loss,
                "loss_pct": loss / start_balance if start_balance > 0 else None,
                "price_move": move,
                "pair": str(t.get("pair", "")),
                "side": "空" if short else "多",
                "open": o_ts.strftime("%Y-%m-%d"),
                "close": c_ts.strftime("%Y-%m-%d"),
                "leverage": cd._num(t.get("leverage")),
                "dca": sum(1 for o in orders if o.get("ft_is_entry")),
                "profit_abs": cd._num(t.get("profit_abs")),
            }
        days = (c_ts - o_ts).total_seconds() / 86400
        if longest is None or days > longest["days"]:
            longest = {"days": days, "pair": str(t.get("pair", "")), "open": o_ts.strftime("%Y-%m-%d"),
                       "close": c_ts.strftime("%Y-%m-%d"), "profit_abs": cd._num(t.get("profit_abs"))}
        if str(t.get("exit_reason", "")) == "force_exit":
            fe_n += 1
            fe_profit += cd._num(t.get("profit_abs")) or 0.0
    return {
        "worst": worst, "longest": longest, "dca_trades": n_dca, "partial_exit_trades": n_partial,
        "force_exit_trades": fe_n, "force_exit_profit": fe_profit,
        "force_exit_profit_pct": fe_profit / start_balance if start_balance > 0 else None,
    }


def evaluate_job(plan: dict[str, Any], cls: str, job: dict[str, str] | None) -> dict[str, Any]:
    """一次回测（一个版本 × 一年）→ 一行。没跑 / 出错时只有 status。"""
    s = plan["settings"]
    cap = float(plan["caps"].get(cls, CLASS_CAPS.get(cls, 5.0)))
    row: dict[str, Any] = {"class": cls, "cap": cap, "status": "todo", "error": ""}
    if job is None:
        return row
    row["status"] = job["status"]
    row["log"] = job.get("log", "")
    if job["status"] != "ok":
        if job.get("rc") == "-1":
            row["error"] = f"freqtrade 没有生成回测结果（退出码 0，多半是配置错误；日志：{job.get('log')}）"
        else:
            row["error"] = f"回测出错（退出码 {job.get('rc') or '?'}，日志：{job.get('log')}）"
        return row
    f = _result_file(Path(job["out"]))
    if f is None:
        row.update(status="fail", error=f"回测说跑完了，但结果目录里没有结果文件（{job['out']}）")
        return row
    stats = cd.load_result_file(f)
    st = (stats or {}).get("strategy", {}).get(cls) if stats else None
    if st is None and stats:
        st = next(iter(stats.get("strategy", {}).values()), None)
    if st is None:
        row.update(status="fail", error=f"读不出回测结果（{f}）")
        return row
    cfg = cd.load_result_config(f)
    # 和 research_report.py 的 evaluate_dir 同样的调用方式（回撤、杠杆口径全部来自 check_drawdown.py）
    ev = cd.evaluate(cls, st, f, cd.settings_max_dd_pct(s), cap, 30, cfg, Path(plan["datadir"]))
    start_balance = cd._num(st.get("starting_balance")) or cd._num(st.get("dry_run_wallet")) or 0.0
    ext = trade_extremes(st.get("trades") or [], start_balance)
    dd_mtm = ev.get("dd_mtm")
    dd_closed = ev.get("dd_closed")
    row.update(
        source=str(f),
        start=ev.get("start"), end=ev.get("end"),
        trades=int(ev.get("trades") or 0),
        winrate=ev.get("winrate"),
        profit_total=ev.get("profit_total"),
        profit_total_abs=ev.get("profit_total_abs"),
        profit_factor=_profit_factor([cd._num(t.get("profit_abs")) or 0.0 for t in (st.get("trades") or [])]),
        dd_mtm=dd_mtm,
        dd_mtm_method=ev.get("dd_mtm_method") or "",
        dd_closed=dd_closed,
        dd_gap=(dd_mtm - dd_closed) if dd_mtm is not None and dd_closed is not None else None,
        dd_worst=ev.get("dd_worst"),
        leverage_max=ev.get("leverage_max"),
        starting_balance=start_balance,
        final_balance=ev.get("final_balance"),
        max_open_trades=st.get("max_open_trades_setting", st.get("max_open_trades")),
        ev_notes=list(ev.get("notes") or []),
        **ext,
    )
    return row


def judge_class(plan: dict[str, Any], cls: str, rows: list[dict[str, Any]]) -> dict[str, Any]:
    """一个版本的结论。规则（全部满足才是"可以上模拟盘"，写在报告抬头）：
      1. 计划里的每一年都跑完了，没有回测出错；
      2. 每一年（包括今年这种不完整的年份）账户回撤（含浮亏、取平仓和含浮亏中较大的）≤ 上限；
      3. 每一年的最大杠杆 ≤ 这个版本的封顶（不满足说明封顶没生效，结果作废）；
      4. 完整年份至少 min_full_years 个，每个完整年份都有交易；
      5. 亏损的年份（包括今年）最多 max_losing_years 个，而且每个亏损年都亏不到 max_year_loss_pct%；
      6. 各年收益连乘 > 0。
    """
    s = plan["settings"]
    max_dd = float(s["max_dd"])
    max_losing = int(s.get("max_losing_years", DEFAULT_MAX_LOSING_YEARS))
    max_loss = float(s.get("max_year_loss_pct", DEFAULT_MAX_YEAR_LOSS_PCT))
    min_full = int(s.get("min_full_years", DEFAULT_MIN_FULL_YEARS))
    cap = float(plan["caps"].get(cls, CLASS_CAPS.get(cls, 5.0)))
    reasons: list[str] = []
    notes: list[str] = []
    ran = [r for r in rows if r["status"] == "ok"]
    failed = [r for r in rows if r["status"] == "fail"]
    todo = [r for r in rows if r["status"] == "todo"]
    if failed:
        reasons.append(f"{len(failed)} 年回测出错（{'、'.join(str(r['year']) for r in failed)}），没法判断")
    if todo:
        reasons.append(f"还有 {len(todo)} 年没跑（{'、'.join(str(r['year']) for r in todo)}）")
    for r in ran:
        y = r["year"]
        if r.get("dd_worst") is not None and r["dd_worst"] * 100 > max_dd + 1e-9:
            reasons.append(
                f"{y} 年账户回撤 {_pct(r['dd_worst'])} 超过 {max_dd:g}%"
                f"（含浮亏 {_pct(r.get('dd_mtm'))}、只看平仓 {_pct(r.get('dd_closed'))}，取大的）"
            )
        if r.get("leverage_max") is not None and r["leverage_max"] > cap + 1e-9:
            reasons.append(f"{y} 年最大杠杆 {r['leverage_max']:g} 倍超过这个版本的封顶 {cap:g} 倍（封顶没生效，这个版本的结果作废）")
        if r.get("trades", 0) == 0:
            if r.get("full"):
                reasons.append(f"{y} 年一笔交易都没有（多半是数据有问题），没法判断")
            else:
                notes.append(f"{y} 年一笔交易都没有（不是完整的一年）")
    full = [r for r in ran if r.get("full")]
    if len(full) < min_full:
        reasons.append(f"完整年份只有 {len(full)} 个（要求至少 {min_full} 个）")
    losing = [r for r in ran if (r.get("profit_total") or 0.0) < 0]
    if len(losing) > max_losing:
        listed = "、".join(str(r["year"]) + " " + _pct(r["profit_total"], True) for r in losing)
        reasons.append(f"亏损的年份有 {len(losing)} 个（{listed}），最多允许 {max_losing} 个")
    for r in losing:
        if r["profit_total"] * 100 < -max_loss - 1e-9:
            reasons.append(f"{r['year']} 年亏 {_pct(r['profit_total'], True)}，超过允许的 -{max_loss:g}%")
    chain = None
    if ran:
        chain = 1.0
        for r in ran:
            chain *= 1.0 + (r.get("profit_total") or 0.0)
        chain -= 1.0
        if chain <= 0:
            reasons.append(f"各年收益连乘 {_pct(chain, True)} ≤ 0")
    for r in rows:
        if r["status"] == "ok" and not r.get("full"):
            notes.append(f"{r['year']} 年不是完整的一年（{r.get('kind', '')}）：只用来查回撤、杠杆和亏损，不算完整年份")
    worst_year = min(ran, key=lambda r: r.get("profit_total") or 0.0) if ran else None
    max_dd_row = max(ran, key=lambda r: r.get("dd_worst") or 0.0) if ran else None
    return {
        "class": cls,
        "cap": cap,
        "verdict": VERDICT_NO if reasons else VERDICT_GO,
        "reasons": reasons,
        "notes": notes,
        "years_run": len(ran),
        "years_planned": len(rows),
        "full_years": len(full),
        "losing_years": [r["year"] for r in losing],
        "chain_return": chain,
        "worst_year": worst_year["year"] if worst_year else None,
        "worst_year_return": worst_year.get("profit_total") if worst_year else None,
        "max_dd_year": max_dd_row["year"] if max_dd_row else None,
        "max_dd": max_dd_row.get("dd_worst") if max_dd_row else None,
    }


def build(plan: dict[str, Any], jobs: dict[tuple[str, int], dict[str, str]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    rows: list[dict[str, Any]] = []
    verdicts: list[dict[str, Any]] = []
    for cls in plan["classes"]:
        crow: list[dict[str, Any]] = []
        for yp in sorted(plan["years"], key=lambda x: int(x["year"])):
            y = int(yp["year"])
            if yp["skip"]:
                r = {"class": cls, "cap": float(plan["caps"][cls]), "status": "skip", "error": yp["skip"]}
            else:
                r = evaluate_job(plan, cls, jobs.get((cls, y)))
            pinfo = yp.get("pairs") or {}
            r.update(
                year=y,
                timerange=yp["timerange"],
                full=bool(yp.get("full")),
                kind=("没有数据，跳过" if yp["skip"] else "完整的一年" if yp.get("full")
                      else ("今年，到 " + _yp_last(yp) if not yp["calendar_complete"] else "数据不完整")),
                period=f"{yp['start'][5:]}~{_yp_last(yp)[5:]}",
                pairs_total=len(plan["pairs"]),
                pairs_not_full=[p for p, i in pinfo.items() if i["status"] != "full"],
                pairs_none=[p for p, i in pinfo.items() if i["status"] == "none"],
            )
            crow.append(r)
        considered = [r for r in crow if r["status"] != "skip"]
        verdicts.append(judge_class(plan, cls, considered))
        rows.extend(crow)
    return rows, verdicts


def _render_table(headers: list[tuple[str, bool]], body: list[list[str]]) -> list[str]:
    widths = [max(cd._width(h), *(cd._width(row[i]) for row in body)) if body else cd._width(h)
              for i, (h, _) in enumerate(headers)]
    sep = "+" + "+".join("-" * (w + 2) for w in widths) + "+"
    L = [sep, "| " + " | ".join(cd._pad(h, widths[i]) for i, (h, _) in enumerate(headers)) + " |", sep]
    for row in body:
        L.append("| " + " | ".join(cd._pad(c, widths[i], headers[i][1]) for i, c in enumerate(row)) + " |")
    L.append(sep)
    return L


def _status_text(r: dict[str, Any]) -> str:
    return {"skip": "跳过（没有数据）", "fail": "回测出错", "todo": "还没跑"}.get(r["status"], r["status"])


def render(plan: dict[str, Any], rows: list[dict[str, Any]], verdicts: list[dict[str, Any]], stage: str) -> str:
    s = plan["settings"]
    max_dd = float(s["max_dd"])
    L: list[str] = []
    title = "================ NFI X8 逐年回测（低杠杆版本，第 4 轮） ================"
    if stage == "interim":
        title += "\n（中间结果：还没全部跑完，最终结论看最后那张表）"
    elif stage == "interrupted":
        title += "\n（中途按了 Ctrl+C：只用已经跑完的部分；没跑的年份算\"还没跑\"）"
    L.append(title)
    if s.get("synthetic"):
        L.append("!! 合成数据流程自检：用的是随机生成的行情和一个替身策略（不是 NFI），下面的数字没有任何意义 !!")
    L.extend(IN_SAMPLE_WARNING)
    L.append("")
    override = cd.max_dd_override_note(s)
    if override:
        L.append(override)
    L.append(f"运行目录：{plan['run_dir']}    数据最后一个完整的日子：{_last_day(plan)}")
    L.append(
        f"交易对（{len(plan['pairs'])} 个，每一年都一样）：{' '.join(_coin(p) for p in plan['pairs'])}"
    )
    fee = s.get("fee")
    fee_txt = "交易所费率" if fee in (None, "", "none") else f"{float(fee) * 100:.2f}%/边"
    L.append(
        f"每一年单独回测一次、每次都从 1000 USDT 重新开始（研究配置 {plan['config']} + configs/research-overrides/<版本>.json，"
        f"同时最多 6 个仓位）；手续费+滑点 {fee_txt}；5m 主周期 + 15m/1h/4h/1d 参考周期 + BTC"
    )
    L.append(
        f"回撤上限：{max_dd:g}% {cd.max_dd_rule_note(max_dd)}。回撤 = 账户权益（已平仓余额 + 持仓浮动盈亏）从最高点到最低点的跌幅，"
        "口径同 make check（scripts/check_drawdown.py）"
    )
    L.append(
        f"判定（每个版本一个结论，全部满足才是\"{VERDICT_GO}\"）：每一年都跑完；每一年（含今年）账户回撤（含浮亏）≤ {max_dd:g}%；"
        f"每一年最大杠杆 ≤ 这个版本的封顶；"
    )
    L.append(
        f"      完整年份至少 {s.get('min_full_years', DEFAULT_MIN_FULL_YEARS)} 个、每个都有交易；"
        f"亏损的年份（含今年）最多 {s.get('max_losing_years', DEFAULT_MAX_LOSING_YEARS)} 个、每个都亏不到 "
        f"{float(s.get('max_year_loss_pct', DEFAULT_MAX_YEAR_LOSS_PCT)):g}%；各年收益连乘 > 0"
    )
    L.append("")

    headers = [
        ("年份", False), ("区间", False), ("收益", True), ("PF", True), ("交易", True), ("胜率", True),
        ("回撤(含浮亏)", True), ("回撤(平仓)", True), ("差距", True), ("最差单笔(估)", True), ("最长持仓", True),
        ("杠杆", True), ("加仓笔数", True), ("年底未平", True), ("缺数据的币", True),
    ]
    vmap = {v["class"]: v for v in verdicts}
    for cls in plan["classes"]:
        v = vmap[cls]
        crow = [r for r in rows if r["class"] == cls]
        L.append(f"---- {cls}（杠杆封顶 {_cap_text(v['cap'])}）    结论：{v['verdict']} ----")
        body = []
        for r in crow:
            rng = r.get("period") or r["timerange"].replace("-", "~")
            nf = f"{len(r['pairs_not_full'])}/{r['pairs_total']}" if r.get("pairs_total") and r["status"] != "skip" else "-"
            if r["status"] != "ok":
                body.append([str(r["year"]), rng, _status_text(r)] + ["-"] * (len(headers) - 4) + [nf])
                continue
            w = r.get("worst") or {}
            lg = r.get("longest") or {}
            fe = f"{r.get('force_exit_trades', 0)}" + (
                f"（{_pct(r.get('force_exit_profit_pct'), True)}）" if r.get("force_exit_trades") else ""
            )
            body.append([
                str(r["year"]) + ("" if r.get("full") else "*"),
                rng,
                _pct(r.get("profit_total"), True),
                _num(r.get("profit_factor")),
                str(r.get("trades", 0)),
                _pct(r.get("winrate")),
                _pct(r.get("dd_mtm")) + (f" {r['dd_mtm_method']}" if r.get("dd_mtm_method") else ""),
                _pct(r.get("dd_closed")),
                "-" if r.get("dd_gap") is None else f"{r['dd_gap'] * 100:+.2f}",
                "-" if not w else _pct(-(w.get("loss_pct") or 0.0), True),
                "-" if not lg else f"{lg['days']:.1f} 天",
                "-" if r.get("leverage_max") is None else f"{r['leverage_max']:g}x",
                str(r.get("dca_trades", 0)),
                fe,
                nf,
            ])
        L.extend(_render_table(headers, body))
        L.append(f"  结论：{v['verdict']}")
        for x in v["reasons"]:
            L.append(f"    原因：{x}")
        for x in v["notes"]:
            L.append(f"    提醒：{x}")
        detail = []
        for r in crow:
            msgs = []
            if r["status"] != "ok":
                if r.get("error"):
                    msgs.append(r["error"])
            else:
                w = r.get("worst")
                if w and w.get("loss", 0) > 0:
                    lev_txt = "?" if w.get("leverage") is None else f"{w['leverage']:g}"
                    dca_txt = f"，开仓+加仓共 {w['dca']} 次" if w.get("dca", 0) > 1 else ""
                    pa = w.get("profit_abs") or 0.0
                    msgs.append(
                        f"最差单笔（估）：{_coin(w['pair'])} {w['side']}单 {w['open']} 开、{w['close']} 平，"
                        f"价格最多逆向 {w['price_move'] * 100:.1f}%（{lev_txt} 倍{dca_txt}），按订单重建的持仓估最多亏 "
                        f"{w['loss']:.1f} USDT（占初始资金 {(w.get('loss_pct') or 0) * 100:.1f}%），"
                        f"最后{'赚' if pa >= 0 else '亏'} {abs(pa):.1f} USDT"
                    )
                lg = r.get("longest")
                if lg:
                    msgs.append(
                        f"最长持仓：{_coin(lg['pair'])} {lg['open']} ~ {lg['close']}（{lg['days']:.1f} 天，最后 {_usdt(lg.get('profit_abs'))} USDT）"
                    )
                if r.get("force_exit_trades"):
                    msgs.append(
                        f"年底（回测结束时）还开着 {r['force_exit_trades']} 笔，按最后价格强制平掉，合计 {_usdt(r['force_exit_profit'])} USDT"
                        "（这一年的收益已经算上了它们的浮亏/浮盈）"
                    )
                if r.get("dd_gap") is not None and r["dd_gap"] > 0.05:
                    msgs.append(
                        f"含浮亏回撤比只看平仓的大 {r['dd_gap'] * 100:.1f} 个百分点：亏损的仓位拿着不平（等反弹），平仓记录看起来很好看"
                    )
                msgs.extend(r.get("ev_notes") or [])
            if r.get("pairs_none"):
                msgs.append(f"这一年没有数据的币：{' '.join(_coin(p) for p in r['pairs_none'])}")
            part = [p for p in r.get("pairs_not_full") or [] if p not in (r.get("pairs_none") or [])]
            if part:
                msgs.append(f"这一年只有部分时间有数据的币：{' '.join(_coin(p) for p in part)}")
            if msgs:
                detail.append(f"  - {r['year']}（{r['kind']}）")
                detail.extend(f"      {m}" for m in msgs)
        if detail:
            L.append("  每一年的细节：")
            L.extend(detail)
        L.append("")

    L.append("================ 汇总（每个版本一行） ================")
    sh = [("版本", False), ("杠杆封顶", True), ("结论", False), ("跑完", True), ("完整年份", True), ("各年连乘", True),
          ("亏损年", False), ("最差一年", True), ("最大回撤(含浮亏)", True), ("主要原因", False)]
    body = []
    for v in verdicts:
        body.append([
            v["class"],
            _cap_text(v["cap"]),
            v["verdict"],
            f"{v['years_run']}/{v['years_planned']}",
            str(v["full_years"]),
            _pct(v["chain_return"], True),
            "、".join(map(str, v["losing_years"])) or "没有",
            "-" if v["worst_year"] is None else f"{v['worst_year']} {_pct(v['worst_year_return'], True)}",
            "-" if v["max_dd_year"] is None else f"{_pct(v['max_dd'])}（{v['max_dd_year']}）",
            v["reasons"][0] if v["reasons"] else "全部满足",
        ])
    L.extend(_render_table(sh, body))
    L.append("")
    L.append("怎么看：")
    L.append("  * 年份后面带 * 的不是完整的一年（今年到数据最后一天，或者那一年的数据不全）：只查回撤、杠杆、亏损，不算完整年份。")
    L.append("  * 收益 = 这一年从 1000 USDT 开始的总收益；各年连乘 = 假设每年都按这一年的收益率滚下去（每年其实都是重新开始的）。")
    L.append("  * 回撤(含浮亏) 后面的\"K线(5m)\"= 逐根 5 分钟K线算的；\"估\"= 找不到K线时的估算。差距 = 含浮亏 - 平仓（百分点）："
             "差距大 = 拿着亏损的仓位不平，平仓记录（胜率、PF）看起来比账户实际好。")
    L.append("  * 最差单笔(估) = 某一笔交易持仓期间最多亏了初始资金的百分之多少（含浮亏、手续费和中途减仓已实现的亏损；只用回测记录估算："
             "加过仓 / 减过仓的按订单重建每一段持仓，每段都按整个持仓期间最差的价格算，取最差的一段，所以只会估多、不会估少）。"
             "加仓笔数 = 这一年开仓后又加过仓的交易数（NFI 的 rebuy / grind）。")
    L.append("  * 加过仓的交易：回撤(含浮亏)的算法见 scripts/check_drawdown.py 开头的说明。")
    L.append("  * 年底未平 = 回测结束时还开着、被按最后价格强制平掉的笔数（括号里是它们合计占初始资金的盈亏）。NFI 会长时间拿着亏损的仓位，"
             "这一项能看出\"年底账面上压着多少亏损\"。")
    L.append("  * 缺数据的币 = 这一年没有整年数据的币 / 全部币（多半是那时还没上线）；这一年等于在更少的币上跑。")
    L.append("  * 交易对是今天还活着的大币（幸存者偏差），越早的年份越偏乐观；OKX 只有最近约 3 个月的资金费率，更早的按 0 算。")
    L.append(f"  * 再说一次：这里\"{VERDICT_GO}\"只是合理性检查（这些年份可能都是 NFI 作者看过的），"
             "真正的检验是币安模拟盘；\"不建议\"说明连这些年份都扛不住。")
    return "\n".join(L)


CSV_FIELDS = [
    "class", "cap", "verdict", "year", "timerange", "full", "kind", "status", "profit_total", "profit_total_abs",
    "profit_factor", "trades", "winrate", "dd_mtm", "dd_mtm_method", "dd_closed", "dd_gap", "dd_worst",
    "worst_trade_loss_pct", "worst_trade_loss_usdt", "worst_trade_price_move", "worst_trade_pair", "worst_trade_open",
    "longest_hold_days", "longest_hold_pair", "leverage_max", "dca_trades", "partial_exit_trades",
    "force_exit_trades", "force_exit_profit", "pairs_total", "pairs_not_full", "error", "source",
]


def _csv_row(r: dict[str, Any], verdict: str) -> dict[str, Any]:
    w = r.get("worst") or {}
    lg = r.get("longest") or {}
    out = {k: r.get(k) for k in CSV_FIELDS}
    out.update(
        verdict=verdict,
        worst_trade_loss_pct=w.get("loss_pct"), worst_trade_loss_usdt=w.get("loss"),
        worst_trade_price_move=w.get("price_move"), worst_trade_pair=w.get("pair"), worst_trade_open=w.get("open"),
        longest_hold_days=lg.get("days"), longest_hold_pair=lg.get("pair"),
        pairs_not_full=" ".join(r.get("pairs_not_full") or []),
    )
    return out


def cmd_report(a: argparse.Namespace) -> int:
    run_dir = Path(a.run_dir)
    if not (run_dir / "plan.json").is_file():
        print(f"错误：{run_dir} 里没有 plan.json（不是 NFI 逐年回测的结果目录？）", file=sys.stderr)
        return 1
    plan = load_plan(run_dir)
    s = plan.setdefault("settings", {})
    cd.override_settings_max_dd(s, a.max_dd)
    if s.get("max_dd") in (None, ""):
        s["max_dd"] = cd.default_max_dd_pct()
    for key, val in (("max_losing_years", a.max_losing_years), ("max_year_loss_pct", a.max_year_loss),
                     ("min_full_years", a.min_full_years)):
        if val is not None:
            s[key] = val
    jobs = load_jobs(run_dir)
    rows, verdicts = build(plan, jobs)
    text = render(plan, rows, verdicts, a.stage)
    print(text)
    (run_dir / "summary.txt").write_text(text + "\n", encoding="utf-8")
    vmap = {v["class"]: v["verdict"] for v in verdicts}
    with (run_dir / "summary.csv").open("w", newline="", encoding="utf-8-sig") as fh:
        wr = csv.DictWriter(fh, fieldnames=CSV_FIELDS)
        wr.writeheader()
        for r in rows:
            wr.writerow(_csv_row(r, vmap.get(r["class"], "")))
    _write_json(run_dir / "summary.json", {
        "stage": a.stage, "settings": s, "pairs": plan["pairs"], "data_end_day": plan["data_end_day"],
        "verdicts": verdicts, "rows": rows,
    })
    print(f"\n（表格另存：{run_dir / 'summary.txt'}、summary.csv、summary.json）")
    return 0


# ----------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="NFI X8 逐年回测的辅助脚本（选交易对、数据检查、中文报告）")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("pairs", help="选交易对，写 pairs.json，打印下载计划")
    sp.add_argument("--config", required=True)
    sp.add_argument("--out", required=True)
    sp.add_argument("--n", required=True, help="前 N 个币，或 all")
    sp.add_argument("--years", default="")
    sp.add_argument("--first-year", type=int, default=FIRST_YEAR_DEFAULT)
    sp.add_argument("--datadir", default="")
    sp.add_argument("--require-data", action="store_true", help="只用本地有 5m 数据的币（合成数据自检用）")

    sp = sub.add_parser("plan", help="检查数据、定下每一年的回测区间")
    sp.add_argument("--run-dir", required=True)
    sp.add_argument("--config", required=True)
    sp.add_argument("--datadir", default="")
    sp.add_argument("--years", default="")
    sp.add_argument("--first-year", type=int, default=FIRST_YEAR_DEFAULT)
    sp.add_argument("--classes", default="")
    sp.add_argument("--fee", default="0.0007")
    sp.add_argument("--max-dd", default="")
    sp.add_argument("--runner", default="")
    sp.add_argument("--strategy-files", default="", help="策略文件的 sha256（记进 plan.json）")
    sp.add_argument("--synthetic", action="store_true")

    sp = sub.add_parser("report", help="出中文报告")
    sp.add_argument("--run-dir", required=True)
    sp.add_argument("--stage", choices=["final", "interim", "interrupted"], default="final")
    sp.add_argument("--max-dd", type=cd.max_dd_arg, default=None,
                    help="按别的回撤上限重新判（%%；不改 plan.json）")
    sp.add_argument("--max-losing-years", type=int, default=None, help=f"最多允许几个亏损年（默认 {DEFAULT_MAX_LOSING_YEARS}）")
    sp.add_argument("--max-year-loss", type=float, default=None,
                    help=f"亏损年最多亏多少 %%（默认 {DEFAULT_MAX_YEAR_LOSS_PCT:g}）")
    sp.add_argument("--min-full-years", type=int, default=None, help=f"至少几个完整年份（默认 {DEFAULT_MIN_FULL_YEARS}）")

    a = ap.parse_args(argv)
    try:
        if a.cmd == "pairs":
            return cmd_pairs(a)
        if a.cmd == "plan":
            return cmd_plan(a)
        return cmd_report(a)
    except (OSError, ValueError, KeyError) as exc:
        print(f"出错：{exc.__class__.__name__}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
