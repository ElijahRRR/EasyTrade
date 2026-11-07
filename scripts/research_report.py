#!/usr/bin/env python3
"""
第 2 步"策略研究"的辅助脚本（由 scripts/research.sh 调用，一般不用直接运行）。

子命令：
  timeframes   列出研究需要下载的K线周期（研究配置的周期 + 各策略自己的周期 + 细节周期）
  prepare      检查已下载的数据，去掉历史不够的币，切分 训练段 / 样本外 / 走步验证 区间，读取策略信息
  gate         默认参数在训练段是否值得调参（训练段都不赚钱，就不靠调参去"救"它）
  hyperopt     运行 freqtrade hyperopt（带一个 freqtrade 2026.10 并行 bug 的绕过，见 _patch_pickle_magic）
  best-params  从 hyperopt 结果文件（.fthypt）取出最优参数，写成 freqtrade 的策略参数文件
  report       汇总成中文排名表：summary.txt / summary.csv / summary.json
               回撤上限按 plan.json 里记的值判（研究运行时的 --max-dd / MAX_DD_PCT，默认 15% = 项目硬规则）；
               加 --max-dd 12 可以按别的上限重新判一次（不重跑回测、不改 plan.json，报告抬头会注明）
               第 5 轮：读预注册登记表 configs/preregistration.json（--prereg 可以换文件；没有这个文件 = 全部按默认规则），
               登记的策略用自己的盈亏因子门槛（min_pf）和"算失败的情况"（extra_checks），每一段都查盈亏因子和交易数；
               另外几张只报告的表：每段资金费合计和资金费为 0 的交易占比、平均每币每年交易数、能交易的币、
               逐年表（每个日历年的收益和含浮亏回撤）、2020 留出段（RESEARCH_PRE_HOLDOUT，jobs.tsv 里的段名 preholdout）、
               事件集中度（去掉盈利最多的几个入场日后重算）。自检：scripts/check_report_round5.py

账户回撤口径和 scripts/check_drawdown.py 完全一样（直接调用它的函数）：
账户权益 = 已平仓余额 + 所有持仓的浮动盈亏，逐根K线计算。
"""

from __future__ import annotations

import argparse
import ast
import csv
import json
import math
import re
import shlex
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any


SCRIPTS_DIR = Path(__file__).resolve().parent
ROOT_DIR = SCRIPTS_DIR.parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import check_drawdown as cd
import research_portfolio  # 子账户组合检查（render 末尾调用）


MAX_LOSS = 100000  # freqtrade hyperopt 给"交易数不够"的组合的损失值
DEFAULT_START_BALANCE = 1000.0
VARIANT_CN = {"default": "默认参数", "opt": "优化参数"}
# 截面策略（XSMomentum）回测日志里的固定标记：前 K 名的币因为仓位低于最小下单额开不出来 / 被 freqtrade 放大。
# 和 user_data/strategies/XSMomentum.py 的 MIN_STAKE_SKIP_TAG / MIN_STAKE_BUMP_TAG 相同，改的话两边一起改
MIN_STAKE_SKIP_TAG = "[最小下单额:开不出来]"
MIN_STAKE_BUMP_TAG = "[最小下单额:放大]"
# "低风险版"和原版：信号完全相同、只有仓位不同。报告里逐笔对比，核对"仓位减半"是不是真的成立
LOWRISK_OF = {"BigTrendShortLowRisk": "BigTrendShort"}


# ----------------------------------------------------------------------------
# 小工具
# ----------------------------------------------------------------------------
def _day(text: str) -> date:
    text = text.strip().replace("-", "")
    return date(int(text[:4]), int(text[4:6]), int(text[6:8]))


def _ymd(d: date) -> str:
    return d.strftime("%Y%m%d")


def _iso(d: date) -> str:
    return d.isoformat()


def _tr(a: date, b: date) -> str:
    return f"{_ymd(a)}-{_ymd(b)}"


def _pct(v: float | None, signed: bool = False) -> str:
    if v is None:
        return "-"
    return f"{v * 100:+.2f}%" if signed else f"{v * 100:.2f}%"


def _num(v: Any, nd: int = 2) -> str:
    if v is None:
        return "-"
    if isinstance(v, float) and math.isinf(v):
        return "∞"
    return f"{v:.{nd}f}"


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")


def _detail_for(tf: str, mode: str) -> str:
    """细节周期（--timeframe-detail）：auto = 比 1h 大的周期用 1h 模拟K线内部走势；none = 不用。"""
    tf_sec = cd._tf_seconds(tf)
    if not tf or tf_sec <= 0 or mode in ("", "none", "0", "no"):
        return ""
    if mode == "auto":
        return "1h" if tf_sec > 3600 else ""
    return mode if 0 < cd._tf_seconds(mode) < tf_sec else ""


# ----------------------------------------------------------------------------
# 读策略信息（不导入策略代码，只做语法分析：快，也不会因为策略报错而中断）
# ----------------------------------------------------------------------------
def _load_classes(sdir: Path) -> tuple[dict[str, tuple[ast.ClassDef, Path]], dict[Path, dict[str, Any]]]:
    """返回 (类名 -> (类定义, 文件), 解析失败的文件 -> {line, msg, text})。"""
    classes: dict[str, tuple[ast.ClassDef, Path]] = {}
    broken: dict[Path, dict[str, Any]] = {}
    for f in sorted(sdir.glob("*.py")):
        try:
            text = f.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError) as exc:
            broken[f] = {"line": 0, "msg": f"读不出这个文件（{exc.__class__.__name__}）", "text": ""}
            continue
        try:
            tree = ast.parse(text, filename=str(f))
        except SyntaxError as exc:
            broken[f] = {"line": int(exc.lineno or 0), "msg": str(exc.msg or "语法错误"), "text": text}
            continue
        for node in tree.body:
            if isinstance(node, ast.ClassDef) and node.name not in classes:
                classes[node.name] = (node, f)
    return classes, broken


def _broken_file_for(name: str, broken: dict[Path, dict[str, Any]]) -> tuple[Path, dict[str, Any]] | None:
    """没找到的策略是不是在一个解析失败的文件里（文件名相同，或文件里有 class <名字>）。"""
    pat = re.compile(rf"^[ \t]*class[ \t]+{re.escape(name)}\b", re.MULTILINE)
    for f, err in broken.items():
        if f.stem == name or pat.search(err.get("text") or ""):
            return f, err
    return None


def _base_names(node: ast.ClassDef) -> list[str]:
    names = []
    for b in node.bases:
        if isinstance(b, ast.Name):
            names.append(b.id)
        elif isinstance(b, ast.Attribute):
            names.append(b.attr)
    return names


def _chain(name: str, classes: dict, seen: set[str] | None = None) -> list[ast.ClassDef]:
    """类本身 + 同目录里能找到的父类（子类在前）。"""
    seen = seen if seen is not None else set()
    if name in seen or name not in classes:
        return []
    seen.add(name)
    node = classes[name][0]
    out = [node]
    for b in _base_names(node):
        out.extend(_chain(b, classes, seen))
    return out


def _const(node: ast.AST | None) -> Any:
    return node.value if isinstance(node, ast.Constant) else None


def _class_info(name: str, classes: dict) -> dict[str, Any]:
    chain = _chain(name, classes)
    attrs: dict[str, Any] = {}
    params: dict[str, str] = {}
    is_strategy = False
    for node in reversed(chain):  # 先父类后子类，子类覆盖
        if "IStrategy" in _base_names(node):
            is_strategy = True
        for st in node.body:
            if isinstance(st, ast.Assign):
                targets = [t.id for t in st.targets if isinstance(t, ast.Name)]
                value = st.value
            elif isinstance(st, ast.AnnAssign) and isinstance(st.target, ast.Name) and st.value is not None:
                targets = [st.target.id]
                value = st.value
            else:
                continue
            callee = ""
            if isinstance(value, ast.Call):
                fn = value.func
                callee = fn.id if isinstance(fn, ast.Name) else (fn.attr if isinstance(fn, ast.Attribute) else "")
            for t in targets:
                if callee.endswith("Parameter"):
                    kw = {k.arg: k.value for k in value.keywords if k.arg}  # type: ignore[union-attr]
                    if _const(kw.get("optimize")) is False:
                        params.pop(t, None)
                        continue
                    space = _const(kw.get("space"))
                    if not space and t.split("_")[0] in ("buy", "sell", "protection"):
                        space = t.split("_")[0]
                    if space:
                        params[t] = str(space)
                else:
                    params.pop(t, None)
                    c = _const(value)
                    if c is not None:
                        attrs[t] = c
    return {
        "found": bool(chain),
        "is_strategy": is_strategy,
        "file": str(classes[name][1]) if name in classes else "",
        "timeframe": str(attrs.get("timeframe") or ""),
        "startup": int(attrs.get("startup_candle_count") or 0),
        "params": params,
        # 只调 buy / sell 两类参数（入场、离场），不动止损/ROI 等（那些由策略的风控决定）
        "spaces": [s for s in ("buy", "sell") if s in params.values()],
    }


def scan_strategies(sdir: Path, names: list[str], detail_mode: str) -> dict[str, dict[str, Any]]:
    classes, broken = _load_classes(sdir)
    out = {}
    for n in names:
        info = _class_info(n, classes)
        info["detail"] = _detail_for(info["timeframe"], detail_mode)
        info["syntax_error"] = None
        if not info["found"]:
            hit = _broken_file_for(n, broken)
            if hit is not None:
                f, err = hit
                info["syntax_error"] = {"file": str(f), "line": err["line"], "msg": err["msg"]}
        out[n] = info
    return out


def _syntax_error_text(err: dict[str, Any]) -> str:
    where = f"文件 {err['file']}" + (f" 第 {err['line']} 行" if err.get("line") else "")
    return f"代码有语法错误（{where}：{err.get('msg') or '语法错误'}）"


def cmd_timeframes(a: argparse.Namespace) -> int:
    info = scan_strategies(Path(a.strategy_dir), a.strategies.split(), a.detail)
    if a.print == "download-start":
        # 下载从哪天开始 = 研究开始日期往前推"最长的指标预热"（max(策略的 startup_candle_count, --warmup-candles) 根K线），
        # 这样训练段第一天指标就是热的、和后面各段一样（交易所没有那么早的数据时，有多少给多少）
        days = 0
        for i in info.values():
            tf_sec = cd._tf_seconds(i["timeframe"]) if i["timeframe"] else 0
            if i["found"] and tf_sec > 0:
                days = max(days, math.ceil(max(int(i["startup"]), int(a.warmup_candles)) * tf_sec / 86400))
        print(_ymd(_day(a.start) - timedelta(days=days)))
        return 0
    tfs = set(a.timeframes.split())
    for i in info.values():
        tfs.update(t for t in (i["timeframe"], i["detail"]) if t)
    print(" ".join(sorted(tfs, key=cd._tf_seconds)))
    return 0


# ----------------------------------------------------------------------------
# prepare：数据检查 + 区间切分
# ----------------------------------------------------------------------------
def _data_file(datadir: Path, pair: str, tf: str, mode: str, fmt: str) -> Path:
    sub = datadir / "futures" if mode == "futures" else datadir
    suffix = "-futures" if mode == "futures" else ""
    return sub / f"{cd._pair_file(pair)}-{tf.replace('M', 'Mo')}{suffix}.{fmt}"


def _scan_file(path: Path, tf: str, fmt: str) -> tuple[datetime, datetime, float] | None:
    """返回 (第一根K线时间, 最后一根K线结束时间, 覆盖率)。"""
    import pandas as pd

    if not path.is_file():
        return None
    try:
        if fmt == "feather":
            df = pd.read_feather(path, columns=["date"])
        else:
            df = pd.read_parquet(path, columns=["date"])
    except Exception:  # noqa: BLE001 — 文件坏了按"没有数据"处理
        return None
    if df.empty:
        return None
    dates = pd.to_datetime(df["date"], utc=True)
    first = dates.iloc[0].to_pydatetime()
    last = dates.iloc[-1].to_pydatetime()
    tf_sec = cd._tf_seconds(tf)
    expected = (last - first).total_seconds() / tf_sec + 1 if tf_sec > 0 else len(df)
    return first, last + timedelta(seconds=tf_sec), len(df) / max(expected, 1.0)


def _seg_label(label: str) -> str:
    if label == "train":
        return "训练段"
    if label == "test":
        return "样本外"
    if label == "holdout":
        return "没看过的一段"
    if label.startswith("wf") and label.endswith("train"):
        return f"走步第 {label[2:-5]} 折训练窗口"
    if label.startswith("wf"):
        return f"走步第 {label[2:]} 折"
    return label


def _ceil_day(dt: datetime) -> date:
    d = dt.date()
    return d if dt == datetime(d.year, d.month, d.day, tzinfo=dt.tzinfo) else d + timedelta(days=1)


def seen_warning(S: date, T0: date, E: date, seen: date | None, holdout: tuple[date, date] | None) -> list[str]:
    """样本外有没有被以前的研究看过（第 3 轮复查）：返回要写进 data_check.txt / summary.txt 的几行，没有就是空列表。

    seen = 以前的研究（第 2 轮）用过的数据从哪天开始（RESEARCH_SEEN_FROM，默认 2024-01-01）。
    第 3 轮的策略选择（去掉 1h 策略、BigTrendShortLowRisk 的仓位等）是看着第 2 轮在这段数据上的结果定的，
    所以落在这段里的样本外不是独立证据。
    """
    if seen is None or seen > E:
        return []
    out: list[str] = []
    if seen <= T0:
        out.append(f"!! 样本外这段第 2 轮已经看过（第 2 轮用的是 {seen} 以后的数据）：第 3 轮的策略选择 / BigTrendShortLowRisk 的仓位")
        out.append("   是看着它定的，所以样本外不是独立证据（不是\"从没见过\"）。")
    else:
        out.append(f"!! 样本外里 {seen} 以后的那部分第 2 轮已经看过：第 3 轮的策略选择 / BigTrendShortLowRisk 的仓位是看着它定的，")
        out.append("   那部分不是独立证据。")
    if holdout is not None:
        out.append(f"   真正没看过的只有 {_iso(holdout[0])} ~ {_iso(holdout[1])}（训练段的前一部分，\"没看过的一段\"），以它为准。")
    elif S >= seen:
        out.append("   这次的全部数据第 2 轮都看过，没有\"没看过的一段\"：结果只能当参考，不能当证据。")
    return out


def cmd_prepare(a: argparse.Namespace) -> int:
    from freqtrade.configuration.load_config import load_from_files

    run_dir = Path(a.run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    cfg = load_from_files([a.config])
    exch = str((cfg.get("exchange") or {}).get("name") or "")
    datadir = Path(a.datadir) if a.datadir else Path("user_data") / "data" / exch
    mode = str(cfg.get("trading_mode") or "spot")
    fmt = str(cfg.get("dataformat_ohlcv") or "feather")
    whitelist = list((cfg.get("exchange") or {}).get("pair_whitelist") or [])
    lines: list[str] = []

    def out(msg: str = "") -> None:
        print(msg)
        lines.append(msg)

    if fmt not in ("feather", "parquet"):
        print(f"错误：研究脚本只支持 feather / parquet 格式的K线数据（配置里是 {fmt}）", file=sys.stderr)
        return 1
    if not whitelist:
        print(f"错误：配置 {a.config} 里没有交易对（exchange.pair_whitelist 为空）", file=sys.stderr)
        return 1

    # ---- 策略 ----
    names = a.strategies.split()
    strategies = scan_strategies(Path(a.strategy_dir), names, a.detail)
    found = [n for n in names if strategies[n]["found"]]
    broken = {n: strategies[n]["syntax_error"] for n in names if not strategies[n]["found"] and strategies[n]["syntax_error"]}
    missing = [n for n in names if not strategies[n]["found"] and n not in broken]
    if not found:
        print(f"错误：在 {a.strategy_dir} 里一个能用的策略都没有：{' '.join(names)}", file=sys.stderr)
        for n, err in broken.items():
            print(f"  - {n}：{_syntax_error_text(err)}", file=sys.stderr)
        return 1

    # ---- 数据 ----
    req_tfs = set(a.timeframes.split())
    for n in found:
        req_tfs.update(t for t in (strategies[n]["timeframe"], strategies[n]["detail"]) if t)
    req_tfs_sorted = sorted(req_tfs, key=cd._tf_seconds)
    req_start = _day(a.start)

    scans: dict[str, dict[str, Any]] = {}
    dropped: dict[str, str] = {}
    # 去掉的原因按类合并成一行显示；每个币自己的日期放这里（显示成 "ZEC(2025-11-06 起)"）
    dropped_detail: dict[str, str] = {}
    for pair in whitelist:
        per_tf = {}
        for tf in req_tfs_sorted:
            per_tf[tf] = _scan_file(_data_file(datadir, pair, tf, mode, fmt), tf, fmt)
        lacking = [tf for tf, v in per_tf.items() if v is None]
        if lacking:
            dropped[pair] = f"没有 {'/'.join(lacking)} 数据"
            continue
        thin = [tf for tf, v in per_tf.items() if v[2] < 0.9]  # type: ignore[index]
        if thin:
            dropped[pair] = f"{'/'.join(thin)} 数据缺口太多（缺了 10% 以上的K线）"
            continue
        scans[pair] = {
            "first": max(v[0] for v in per_tf.values()),  # type: ignore[index]
            "end": min(v[1] for v in per_tf.values()),  # type: ignore[index]
        }
    if not scans:
        print(
            f"错误：配置里的 {len(whitelist)} 个交易对在 {datadir} 下都没有完整的 {' '.join(req_tfs_sorted)} 数据。"
            "先下载数据：去掉 --no-download 重新运行 make research（它会自动下载）。",
            file=sys.stderr,
        )
        return 1

    # ---- 上线晚的币不再整个去掉（第 3 轮起）----
    # 以前：比开始日期晚 30 天以上才有数据的币全部去掉 —— 数据从 2021 年开始时，2022 年以后上线的币就全没了。
    # 现在：只要"样本外开始之前"至少有 --min-train-days 天（默认 365）"能交易"的历史就保留（见下面"上线晚的币"）；
    #   回测区间对所有币都一样，上线晚的币要等 第一根K线 + 这一段的指标预热 之后才开始交易（freqtrade 按根数裁掉预热K线）。
    # 中途断了（已下架/没更新）的币照样去掉；历史太短的币照样去掉。
    ref_end = max(v["end"] for v in scans.values())
    alive: dict[str, dict[str, Any]] = {}
    for pair, v in scans.items():
        if v["end"] < ref_end - timedelta(days=3):
            dropped[pair] = "数据中途断了（可能已下架或没更新）"
            dropped_detail[pair] = f"到 {v['end'].date()} 为止"
        else:
            alive[pair] = v
    if not alive:
        print("错误：没有一个交易对的数据能覆盖研究区间。", file=sys.stderr)
        return 1
    # 研究区间 S ~ E：从"最早有数据的币"开始（不早于 --start），到大家都有数据的最后一天。
    # 比 S 更早的K线（下载时多下的那段）只用来给指标预热，不参与交易。
    data_first = _ceil_day(min(v["first"] for v in alive.values()))
    S = max(req_start, data_first)
    E = min(v["end"] for v in alive.values()).date()
    total = (E - S).days
    if total < 120:
        print(f"错误：数据区间只有 {total} 天（{S} ~ {E}），太短，没法切分训练段/样本外。", file=sys.stderr)
        return 1

    # ---- 切分（按 UTC 自然日；freqtrade 的 --timerange 结束日期是"包含"的，
    #      所以前一段在后一段开始的前一天结束，两段不共用任何一根K线）----
    test_days = max(30, round(total * a.test_fraction))
    T0 = E - timedelta(days=test_days)
    train = (S, T0 - timedelta(days=1))
    test = (T0, E)

    # ---- 以前的研究已经看过的数据（第 3 轮复查）----
    # 第 2 轮用的是 2024-01-01 以后的数据，第 3 轮的策略选择（例如 BigTrendShortLowRisk 的仓位、去掉 1h 策略）是看着它定的。
    # 第 3 轮的样本外（最后约 1/3）整段都在这里面，所以不是独立证据。真正没人看过的只有 S ~ seen_from 前一天，
    # 它在训练段里；单独回测一次（"holdout" 这一段，只用默认参数），报告里单独列出来。
    seen = _day(a.seen_from) if a.seen_from else None
    holdout: tuple[date, date] | None = None
    if seen is not None and S < seen:
        h_end = min(train[1], seen - timedelta(days=1))
        if (h_end - S).days + 1 >= 60:
            holdout = (S, h_end)

    # ---- 每个策略的周期 / 预热天数（下面"保留哪些币"要用）----
    def warm_days_of(info: dict[str, Any], candles: int) -> int:
        tf_sec = cd._tf_seconds(info["timeframe"]) if info["timeframe"] else 0
        return math.ceil(int(candles) * tf_sec / 86400) if tf_sec else 0

    # 训练段从数据开头开始，只给策略自己的预热长度（见下面"每一段回测的指标预热长度"）；预热最长的策略决定"哪天能交易"
    w_train = max((warm_days_of(strategies[n], strategies[n]["startup"]) for n in found), default=0)

    # ---- 上线晚的币：样本外开始前要有至少 min_train 天"能交易"的历史 ----
    # freqtrade 回测按根数裁掉每个币自己开头的预热K线，一个币在训练段里要到 第一根K线 + 预热 之后才能交易。
    # 所以按"能交易的天数"算，不按"有数据的天数"算：只按有数据算时，留下来的币可能在训练段里一笔都没做、到样本外才加入，
    # 训练段和样本外用的就不是同一批币了。
    min_train = max(0, int(a.min_train_days))
    min_train_note = ""
    if min_train > (T0 - S).days:
        min_train_note = (
            f"训练段只有 {(T0 - S).days} 天，比 --min-train-days {min_train} 天还短，"
            f"改成要求整个训练段都有数据（{(T0 - S).days} 天）"
        )
        min_train = (T0 - S).days
    kept = []
    for pair, v in alive.items():
        p_trade = max(S, _ceil_day(v["first"]) + timedelta(days=w_train))
        hist = (T0 - p_trade).days
        if hist < min_train:
            dropped[pair] = (
                f"上线太晚：样本外开始（{T0}）前能交易的时间不到 {min_train} 天（第一根K线之后还要等 {w_train} 天预热才能交易）"
            )
            dropped_detail[pair] = f"{v['first'].date()} 起，能交易 {max(hist, 0)} 天"
        else:
            kept.append(pair)
    if not kept:
        print(
            f"错误：没有一个交易对在样本外开始（{T0}）前有 {min_train} 天以上能交易的历史（第一根K线 + {w_train} 天预热之后）"
            "（可以用 RESEARCH_MIN_TRAIN_DAYS 调小这个要求，或者用更长的数据）。",
            file=sys.stderr,
        )
        return 1
    # 每个币的第一根K线（各周期取最晚的那个；去掉的币也记下来，plan.json 里看得到）
    pair_first = {p: _iso(v["first"].date()) for p, v in scans.items()}

    folds: list[dict[str, Any]] = []
    k_req = max(0, a.folds)
    wf_note = ""
    if a.wf_test_days > 0:
        # 固定窗口（第 3 轮默认）：每折测试 wf_test_days 天，前面训练 wf_train_days 天，从数据末尾往前排
        fold_len = int(a.wf_test_days)
        train_len = int(a.wf_train_days) if a.wf_train_days > 0 else round(a.wf_train_ratio * fold_len)
        k_fit = max(0, (total - train_len) // fold_len) if fold_len > 0 else 0
        if k_req and k_fit < k_req:
            if k_fit >= 1:
                wf_note = (
                    f"数据只放得下 {k_fit} 折（每折测试 {fold_len} 天，第一折前面还要留 {train_len} 天训练窗口），"
                    f"要求的 {k_req} 折减成 {k_fit} 折"
                )
            else:
                wf_note = f"数据太短：一共 {total} 天，放不下 1 折（训练 {train_len} 天 + 测试 {fold_len} 天）"
            k_req = k_fit
    else:
        # 自动（第 2 轮的做法）：最近 wf_oos_fraction 的时间平分成 K 折，训练窗口 = 测试窗口 × wf_train_ratio
        fold_len = int(total * a.wf_oos_fraction // k_req) if k_req else 0
        if k_req and fold_len < 20:
            wf_note = f"数据太短（每折测试窗口只有 {fold_len} 天），不做走步验证"
            k_req = 0
        train_len = int(a.wf_train_days) if a.wf_train_days > 0 else round(a.wf_train_ratio * fold_len)
    if k_req:
        W0 = E - timedelta(days=k_req * fold_len)
        for k in range(1, k_req + 1):
            ts = W0 + timedelta(days=(k - 1) * fold_len)
            te = E if k == k_req else W0 + timedelta(days=k * fold_len) - timedelta(days=1)
            tr_s = S if a.wf_mode == "anchored" else max(S, ts - timedelta(days=train_len))
            folds.append({"k": k, "train": (tr_s, ts - timedelta(days=1)), "test": (ts, te)})

    # ---- 每个策略的预热 / 走步可行性 ----
    # freqtrade 从 --timerange 开始日期往前多加载 startup_candle_count 根K线；最早的数据（所有保留的币里最早的那根，
    # 可能比研究开始日期 S 还早，那是下载时为预热多下的）晚于"开始日期 - 预热"时，它把回测开始时间往后推到 最早数据 + 预热。
    kept_first = _ceil_day(min(scans[p]["first"] for p in kept))
    for n in found:
        info = strategies[n]
        tf_sec = cd._tf_seconds(info["timeframe"]) if info["timeframe"] else 0
        warm = math.ceil(info["startup"] * tf_sec / 86400) if tf_sec else 0
        info["warmup_days"] = warm
        eff0 = kept_first + timedelta(days=warm)
        info["train_effective_start"] = _iso(max(eff0, train[0]))
        info["train_effective_days"] = max(0, (train[1] - max(eff0, train[0])).days + 1)
        ok = []
        for f in folds:
            win = (f["train"][1] - f["train"][0]).days + 1
            eff = (f["train"][1] - max(eff0, f["train"][0])).days + 1
            ok.append(1 if eff >= max(30, 0.5 * win) else 0)
        info["wf_ok"] = ok

    # ---- 每一段回测的指标预热长度 ----
    # freqtrade 每次回测只在 --timerange 开始前多加载 startup_candle_count 根K线算指标。
    # EMA200 这类递推指标 300 根时还带着约 37% 的"起点误差"（talib 的 EMA 从前 200 根的简单平均起步，
    # 之后只递推了 100 次：(1 - 2/201)^100 ≈ 0.37），各段结果会随切分点变化，和连续回测、实盘都对不上。
    # 所以：从中间开始的段（样本外、走步各折），开始前有多少历史就多给多少，最多 warmup_candles 根（默认 600，残留约 2%，
    # 也差不多是 OKX 实盘 freqtrade 内存里保留的K线数：startup + 300）。"有多少历史"从最早的数据算起。
    # 从数据开头开始的段（训练段、"没看过的一段"）只给策略自己的预热长度：freqtrade 按根数裁掉每个币自己开头的预热K线，
    # 给 600 根时，研究开始前历史不到 600 天的币（例如 2020 年才上线的币）要在训练段里闲置到 第一根K线 + 600 天，
    # 2021 年的牛市就只剩几个币能交易了（第 3 轮复查发现）。训练段是第一段，不存在"和前一段对不上"的问题。
    # 实现：给这一段的 freqtrade 命令多加一个 --config warmup/<策略>__<段>.json，里面只有 startup_candle_count。
    warm_dir = run_dir / "warmup"
    warm_dir.mkdir(parents=True, exist_ok=True)
    seg_starts: dict[str, date] = {"train": train[0], "test": test[0]}
    if holdout is not None:
        seg_starts["holdout"] = holdout[0]
    for f in folds:
        seg_starts[f"wf{f['k']}"] = f["test"][0]
        seg_starts[f"wf{f['k']}train"] = f["train"][0]
    want_base = max(0, int(a.warmup_candles))
    for n in found:
        info = strategies[n]
        tf_sec = cd._tf_seconds(info["timeframe"]) if info["timeframe"] else 0
        startup = int(info["startup"])
        want = max(startup, want_base)
        wc: dict[str, int] = {}
        for label, d0 in seg_starts.items():
            if d0 <= S:
                nwarm = startup  # 从数据开头开始的段：只用策略自己的预热（见上面）
            else:
                avail = int((d0 - kept_first).days * 86400 // tf_sec) if tf_sec > 0 else 0
                nwarm = max(startup, min(want, avail))
            wc[label] = nwarm
            frag = warm_dir / f"{n}__{label}.json"
            if nwarm > startup:
                _write_json(frag, {"startup_candle_count": nwarm})
            elif frag.exists():
                frag.unlink()
        info["warmup_candles"] = wc
        info["file_stem"] = Path(info["file"]).stem if info["file"] else n
        info["hyperopt_block"] = ""

    # freqtrade 只认"策略文件名.json"这个参数文件：同一个文件里有两个要调参的策略时，没法各自保存参数
    by_file: dict[str, list[str]] = {}
    for n in found:
        if strategies[n]["spaces"]:
            by_file.setdefault(strategies[n]["file"], []).append(n)
    for fpath, group in by_file.items():
        if len(group) > 1:
            for n in group:
                others = "、".join(x for x in group if x != n)
                strategies[n]["hyperopt_block"] = (
                    f"和 {others} 写在同一个文件 {Path(fpath).name} 里；freqtrade 只读和策略文件同名的一份参数文件，"
                    "两个策略没法各自用调出来的参数，所以都不调参（想调参就把它们放到不同的 .py 文件）"
                )

    # ---- 输出 ----
    pairs_frag = run_dir / "pairs.effective.json"
    pairs_frag.write_text(
        "{\n"
        "    // 本次研究实际使用的交易对（research_report.py prepare 自动生成，不要手改）。\n"
        "    // 由研究配置里的交易对去掉\"历史数据不够\"的币得到，见同目录 data_check.txt。\n"
        '    "exchange": {\n'
        '        "pair_whitelist": [\n'
        + ",\n".join(f'            "{p}"' for p in kept)
        + "\n        ]\n    }\n}\n",
        encoding="utf-8",
    )

    def rng(t: tuple[date, date]) -> str:
        return f"{_iso(t[0])} ~ {_iso(t[1])}"

    settings = json.loads(a.settings) if a.settings else {}
    hyperopt_on = settings.get("hyperopt") in (1, "1", True)
    out(f"数据检查（数据目录 {datadir}，需要的周期：{' '.join(req_tfs_sorted)}）")
    out(f"  配置里的交易对 {len(whitelist)} 个：保留 {len(kept)} 个，去掉 {len(dropped)} 个。")
    if dropped:
        out("  去掉的币（数据不够，硬留着会让结果失真）：")
        by_reason: dict[str, list[str]] = {}
        for pair, why in dropped.items():
            # 原因相同的合并成一行；每个币自己的日期写在币名后面
            coin = pair.split("/")[0] + (f"({dropped_detail[pair]})" if pair in dropped_detail else "")
            by_reason.setdefault(why, []).append(coin)
        for why, coins in by_reason.items():
            out(f"    - {why}（{len(coins)} 个）：{' '.join(coins)}")
    if min_train_note:
        out(f"  注意：{min_train_note}")
    if "BTC/USDT:USDT" in whitelist and "BTC/USDT:USDT" not in kept:
        out("  !! 注意：BTC 被去掉了。用 BTC 判断大盘的策略（BigTrendBoth、DonchianTrend 等）会出问题，先补齐 BTC 的数据。")
    elif "BTC/USDT:USDT" in kept and scans["BTC/USDT:USDT"]["first"].date() > S:
        out(
            f"  !! 注意：BTC 的数据 {scans['BTC/USDT:USDT']['first'].date()} 才开始，比研究开始（{S}）晚；"
            "用 BTC 判断大盘的策略在那之前不会开仓。"
        )

    # ---- 每个币从哪天开始"能交易"：看清楚每一段时间里到底有几个币（覆盖情况 / 幸存者偏差）----
    # freqtrade 回测按"根数"裁掉每个币自己开头的预热K线（freqtrade.data.converter.trim_dataframe，每个币各自裁）：
    # 一段回测里，一个币从 max(这一段开始, 第一根K线 + 这一段的预热) 起才能交易。数据完整的币裁掉的正好是这一段开始之前那截；
    # 上线晚的币裁掉的是它上线后的头几百根。按预热最长的策略算（预热短的策略早一点）。
    def seg_warm(label: str) -> int:
        return max((warm_days_of(strategies[n], strategies[n]["warmup_candles"][label]) for n in found), default=0)

    def trade_from(pair: str, label: str, d0: date) -> date:
        return max(d0, _ceil_day(scans[pair]["first"]) + timedelta(days=seg_warm(label)))

    seg_list: list[tuple[str, date]] = [("train", train[0])]
    if holdout is not None:
        seg_list.append(("holdout", holdout[0]))
    seg_list.append(("test", test[0]))
    seg_list += [(f"wf{f['k']}", f["test"][0]) for f in folds]
    tradable = {label: {p: trade_from(p, label, d0) for p in kept} for label, d0 in seg_list}

    out("")
    out(
        f"每个币在训练段里从哪天开始能交易（保留的 {len(kept)} 个；= 第一根K线 + 训练段预热 {seg_warm('train')} 天，"
        f"不早于训练段开始 {S}；按预热最长的策略算，第一根K线的日期在 plan.json 的 pairs_first）："
    )
    groups: dict[str, list[str]] = {}
    for p in sorted(kept, key=lambda x: (tradable["train"][x], x)):
        d0 = tradable["train"][p]
        coin = p.split("/")[0]
        if d0 <= S:
            groups.setdefault(f"训练段开始（{S}）就能交易", []).append(coin)
        else:
            groups.setdefault(f"{d0.year} 年才开始能交易", []).append(f"{coin}({d0.strftime('%m-%d')})")
    for key, coins in groups.items():
        out(f"  {key}（{len(coins)} 个）：{' '.join(coins)}")
    marks = sorted({S, T0, *(date(y, 1, 1) for y in range(S.year + 1, E.year + 1))})
    cov = []
    for d0 in marks:
        n_have = sum(1 for p in kept if scans[p]["first"].date() <= d0)
        label = "train" if d0 < T0 else "test"
        n_trade = sum(1 for p in kept if tradable[label][p] <= d0)
        tag = "训练段开始 " if d0 == S else ("样本外开始 " if d0 == T0 else "")
        cov.append(f"{tag}{d0} {n_trade}/{n_have}")
    out("  每个时间点 能交易/有数据 的币：" + " → ".join(cov))
    out(f"  （{S} 能交易的币 {sum(1 for p in kept if tradable['train'][p] <= S)} 个；"
        "样本外开始之前按训练段的回测算，之后按样本外的回测算）")
    out("  每一段第一天就能交易的币（上线晚的币要等 第一根K线 + 这一段的预热 之后才开始交易；"
        "训练段只用策略自己的预热，其他段最多 " + str(want_base) + " 根）：")
    for label, d0 in seg_list:
        n_ok = sum(1 for p in kept if tradable[label][p] <= d0)
        late = sorted((p for p in kept if tradable[label][p] > d0), key=lambda x: (tradable[label][x], x))
        txt = f"    {_seg_label(label)}（{d0} 起，预热 {seg_warm(label)} 天）：{n_ok}/{len(kept)} 个"
        if late and label not in ("train", "holdout"):  # 这两段和上面"训练段里从哪天开始能交易"相同
            shown = " ".join(f"{p.split('/')[0]}({tradable[label][p]})" for p in late[:12])
            txt += f"；晚开始的：{shown}" + (f" 等 {len(late)} 个" if len(late) > 12 else "")
        out(txt)
    test_late = [p for p in kept if tradable["test"][p] > T0]
    if test_late:
        out(f"  !! 样本外有 {len(test_late)} 个币不是第一天就能交易（上面列出了从哪天开始）：训练段和样本外用的币不完全一样，")
        out("     比较两段结果时记得这一点；想让两段完全一样，调大 RESEARCH_MIN_TRAIN_DAYS。")
    out("  实盘里上线晚的币只要等策略自己的预热（日线 130 ~ 300 天）；研究里样本外和走步各段给的预热更长，所以晚开始得更多。")
    out("  !! 幸存者偏差：交易对是今天挑的\"现在还在交易的大币\"，这几年里下架或归零的币（例如 2022 年崩盘的 LUNA、FTT）")
    out("     根本不在列表里。历史越长，这个偏差越大，结果偏乐观。")

    out("")
    out("区间切分（UTC，按天）：")
    pre_days = (S - kept_first).days
    out(
        f"  全部数据  {rng((S, E))}（{total} 天）"
        + (f"；{S} 之前还有约 {pre_days} 天K线：不交易，只用来给指标预热" if pre_days > 0 else "")
    )
    out(f"  训练段    {rng(train)}（{(train[1] - train[0]).days + 1} 天，约前 {1 - a.test_fraction:.0%}；调参时只用这一段）")
    out(f"  样本外    {rng(test)}（{(test[1] - test[0]).days + 1} 天，最后约 {a.test_fraction:.0%}；调参时没用过这段）")
    for line in seen_warning(S, T0, E, seen, holdout):
        out("            " + line)
    if holdout is not None:
        out(f"  没看过的一段 {rng(holdout)}（{(holdout[1] - holdout[0]).days + 1} 天，训练段的前一部分）：每个策略用默认参数单独回测一次，"
            "报告里单独一张表；它也要赚钱、回撤达标才算通过")
    if folds and hyperopt_on and any(f["train"][1] >= test[0] for f in folds):
        late = "、".join(str(f["k"]) for f in folds if f["train"][1] >= test[0])
        out(f"            注意：走步第 {late} 折会用这段时间里的数据重新调参，所以\"优化参数\"那一行的样本外和走步结果不是两份独立证据")
    if folds:
        how = "扩展窗口：每折都从数据开头训练" if a.wf_mode == "anchored" else f"滚动窗口：每折先训练 {train_len} 天"
        out(f"  走步验证  {len(folds)} 折（{how}，再测试后面 {fold_len} 天），覆盖 {rng((folds[0]['test'][0], E))}：")
        for f in folds:
            out(f"    第 {f['k']} 折  训练 {rng(f['train'])}  →  测试 {rng(f['test'])}")
        if wf_note:
            out(f"            注意：{wf_note}")
        if not hyperopt_on:
            out("            不调参时，走步各折和训练段/样本外是同一段行情，只是切成小段、每段从头开始，")
            out("            看策略是不是每段行情都赚钱（还是只靠某一段大行情），不是额外的独立证据")
    else:
        out(f"  走步验证  不做{'（' + wf_note + '）' if wf_note else ''}")
    warm_lines = [
        f"    - {n}（{strategies[n]['timeframe']}）：预热 {strategies[n]['warmup_days']} 天；"
        f"最早有数据的币从 {strategies[n]['train_effective_start']} 开始交易，上线晚的币要等 第一根K线 + {strategies[n]['warmup_days']} 天"
        + (
            f"；走步第 {'、'.join(str(i + 1) for i, v in enumerate(strategies[n]['wf_ok']) if not v)} 折训练窗口扣掉预热后太短，"
            "这几折不调参、用默认参数"
            if folds and hyperopt_on and not all(strategies[n]["wf_ok"])
            else ""
        )
        for n in found
        if strategies[n]["warmup_days"] >= 3
    ]
    if warm_lines:
        out("  指标需要先\"预热\"（例如 EMA200），预热期不交易（训练段的预热 = 策略自己的预热长度）：")
        for w in warm_lines:
            out(w)
    if want_base > 0:
        out(f"  样本外、走步各折这些从中间开始的段，开始前最多多加载 {want_base} 根K线算指标（有多少历史就给多少；"
            "最少 = 策略自己的预热长度）。")
        out("    EMA200 这类指标要几百根K线才能\"忘掉\"起点，只给 300 根时各段的信号会随切分点变化，和连续回测、实盘对不上。")
        out("    训练段（和\"没看过的一段\"）从数据开头开始，只给策略自己的预热：给多了，研究开始前历史不够长的币会在训练段里闲置更久。")
        out("    每段实际用了多少根记在 plan.json 的 warmup_candles 里。")
    blocked = [n for n in found if strategies[n].get("hyperopt_block")]
    if blocked:
        out("")
        for n in blocked:
            out(f"!! {n} 不调参：{strategies[n]['hyperopt_block']}")
    not_strat = [n for n in found if not strategies[n]["is_strategy"]]
    if not_strat:
        out("")
        out(f"!! 这些类看起来不是 freqtrade 策略（没有继承 IStrategy），回测多半会失败：{' '.join(not_strat)}")
    if broken:
        out("")
        out("!! 这些策略的代码有语法错误，没法研究（跳过，结果表里也不会有它们；改好后重新运行）：")
        for n, err in broken.items():
            out(f"    - {n}：{_syntax_error_text(err)}")
    if missing:
        out("")
        out(f"!! 没找到这些策略（跳过，结果表里也不会有它们；检查名字拼写，类名要和代码里 class 后面的名字一样）：{' '.join(missing)}")
    (run_dir / "data_check.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")

    plan = {
        "created": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "synthetic": bool(a.synthetic),
        "config": a.config,
        "pairs_fragment": str(pairs_frag),
        "datadir": str(datadir),
        "exchange": exch,
        "trading_mode": mode,
        "dataformat": fmt,
        "timeframes": req_tfs_sorted,
        "pairs": kept,
        "pairs_dropped": {p: why + (f"（{dropped_detail[p]}）" if p in dropped_detail else "") for p, why in dropped.items()},
        "pairs_config_count": len(whitelist),
        "pairs_first": pair_first,
        "data_first": _iso(kept_first),
        "min_train_days": min_train,
        "data_range": [_iso(S), _iso(E)],
        "train": [_iso(train[0]), _iso(train[1])],
        "test": [_iso(test[0]), _iso(test[1])],
        # 以前的研究（第 2 轮）看过的数据从哪天开始；holdout = 第 3 轮真正没看过的一段（训练段的前一部分，只用默认参数回测）
        "seen_from": _iso(seen) if seen is not None else None,
        "holdout": [_iso(holdout[0]), _iso(holdout[1])] if holdout is not None else None,
        "seen_warning": seen_warning(S, T0, E, seen, holdout),
        "test_fraction": a.test_fraction,
        "wf": {
            "mode": a.wf_mode,
            "train_days": train_len,
            "test_days": fold_len,
            "note": wf_note,
            "requested_folds": max(0, a.folds),
            "folds": [
                {"k": f["k"], "train": [_iso(f["train"][0]), _iso(f["train"][1])],
                 "test": [_iso(f["test"][0]), _iso(f["test"][1])]}
                for f in folds
            ],
        },
        "strategies_order": found,
        "strategies_missing": missing,
        "strategies_broken": broken,
        "warmup_candles": want_base,
        "strategies": {n: strategies[n] for n in found},
        "settings": settings,
    }
    _write_json(run_dir / "plan.json", plan)

    sh = [
        "# research_report.py prepare 自动生成，由 research.sh 读取",
        f"DATA_START={_iso(S)}",
        f"DATA_END={_iso(E)}",
        f"TRAIN_RANGE={_tr(*train)}",
        f"TEST_RANGE={_tr(*test)}",
        f"HOLDOUT_RANGE={_tr(*holdout) if holdout is not None else ''}",
        f"WF_COUNT={len(folds)}",
        f"PAIR_COUNT={len(kept)}",
        f"PLAN_DATADIR={shlex.quote(str(datadir))}",
        f"PLAN_STRATEGIES={shlex.quote(' '.join(found))}",
        f"PLAN_DROPPED={shlex.quote(' '.join(list(broken) + missing))}",
    ]
    for f in folds:
        sh.append(f"WF_TRAIN_{f['k']}={_tr(*f['train'])}")
        sh.append(f"WF_TEST_{f['k']}={_tr(*f['test'])}")
    for n in found:
        i = strategies[n]
        sh.append(f"S_{n}_TF={shlex.quote(i['timeframe'])}")
        sh.append(f"S_{n}_DETAIL={shlex.quote(i['detail'])}")
        sh.append(f"S_{n}_SPACES={shlex.quote(' '.join(i['spaces']))}")
        sh.append(f"S_{n}_WF_OK={shlex.quote(' '.join(str(v) for v in i['wf_ok']))}")
        sh.append(f"S_{n}_FILESTEM={shlex.quote(i['file_stem'])}")
        sh.append(f"S_{n}_HO_BLOCK={shlex.quote(i['hyperopt_block'])}")
    (run_dir / "plan.sh").write_text("\n".join(sh) + "\n", encoding="utf-8")
    return 0


# ----------------------------------------------------------------------------
# 读取回测结果
# ----------------------------------------------------------------------------
def load_plan(run_dir: Path) -> dict[str, Any]:
    return json.loads((run_dir / "plan.json").read_text(encoding="utf-8"))


def load_jobs(run_dir: Path) -> list[dict[str, str]]:
    path = run_dir / "jobs.tsv"
    cols = ["strategy", "variant", "segment", "timerange", "status", "dir", "log"]
    rows = []
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            parts = line.split("\t")
            if len(parts) >= len(cols):
                rows.append(dict(zip(cols, parts)))
    return rows


def load_notes(run_dir: Path) -> list[tuple[str, str, str]]:
    path = run_dir / "notes.tsv"
    out = []
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            parts = line.split("\t", 2)
            if len(parts) == 3:
                out.append((parts[0], parts[1], parts[2]))
    return out


def _profit_factor(profits: list[float]) -> float | None:
    gains = sum(p for p in profits if p > 0)
    losses = -sum(p for p in profits if p < 0)
    if not profits:
        return None
    if losses <= 0:
        return math.inf if gains > 0 else None
    return gains / losses


def evaluate_dir(plan: dict[str, Any], strategy: str, result_dir: str) -> dict[str, Any] | None:
    """读一个回测结果目录（每个目录只放一次回测），用 check_drawdown 的口径评估。"""
    d = Path(result_dir)
    files = sorted(d.glob("backtest-result-*.zip")) if d.is_dir() else []
    if not files:
        return None
    f = files[-1]
    try:
        stats = cd.load_result_file(f)
    except Exception:  # noqa: BLE001
        return None
    if not stats or not stats.get("strategy"):
        return None
    st = stats["strategy"].get(strategy) or next(iter(stats["strategy"].values()))
    cfg = cd.load_result_config(f)
    s = plan.get("settings", {})
    r = cd.evaluate(
        strategy, st, f, cd.settings_max_dd_pct(s), float(s.get("max_leverage", 5.0)),
        int(s.get("min_trades", 30)), cfg, Path(plan["datadir"]),
    )
    r["profit_factor"] = _profit_factor([float(t.get("profit_abs") or 0.0) for t in (st.get("trades") or [])])
    r["_st"] = st
    r["_cfg"] = cfg
    return r


def _job(jobs: list[dict[str, str]], strategy: str, variant: str, segment: str) -> dict[str, str] | None:
    hits = [j for j in jobs if j["strategy"] == strategy and j["variant"] == variant and j["segment"] == segment]
    return hits[-1] if hits else None


def eval_job(plan, jobs, strategy, variant, segment) -> dict[str, Any] | None:
    j = _job(jobs, strategy, variant, segment)
    if not j or j["status"] != "ok":
        return None
    return evaluate_dir(plan, strategy, j["dir"])


# ----------------------------------------------------------------------------
# 走步验证拼接 / 组合叠加
# ----------------------------------------------------------------------------
def _closed_dd(infos: list[dict[str, Any]], start: float) -> float:
    bal = peak = start
    worst = 0.0
    for i in sorted(infos, key=lambda x: x["close"]):
        bal += i["profit"]
        peak = max(peak, bal)
        if peak > 0:
            worst = max(worst, (peak - bal) / peak)
    return worst


def _mtm_dd(plan, infos, start: float, timeframe: str, cfg: dict[str, Any]) -> tuple[float, str]:
    if not infos:
        return 0.0, ""
    exact = cd.mtm_drawdown_candles(
        infos, start, timeframe, Path(plan["datadir"]),
        str(cfg.get("dataformat_ohlcv") or plan.get("dataformat") or "feather"),
        str(cfg.get("trading_mode") or plan.get("trading_mode") or "futures"),
    )
    if exact is not None:
        return exact[0], f"K线({exact[1]})"
    return cd.mtm_drawdown_bound(infos, start), "估"


def stitch_folds(plan, folds: list[dict[str, Any] | None]) -> dict[str, Any] | None:
    """把走步验证各折的测试结果按时间顺序复利拼成一条资金曲线。

    每折回测都从 1000 USDT 重新开始；拼接时把第 k 折的每笔交易按"第 k 折开始时的账户权益 / 1000"放大或缩小，
    相当于这一折接着上一折结束时的资金继续跑。然后用 check_drawdown 的逐根K线算法算整条曲线的回撤。
    """
    have = [r for r in folds if r is not None]
    if not have:
        return None
    start = float(have[0].get("starting_balance") or DEFAULT_START_BALANCE)
    equity = start
    infos: list[dict[str, Any]] = []
    fold_returns: list[float | None] = []
    for r in folds:
        if r is None:
            fold_returns.append(None)
            continue
        sb = float(r.get("starting_balance") or start)
        fb = float(r.get("final_balance") or sb)
        factor = equity / sb if sb > 0 else 1.0
        for t in r["_st"].get("trades") or []:
            i = cd._trade_info(t)
            if i is None:
                continue
            i["amount"] *= factor
            i["profit"] *= factor
            infos.append(i)
        ret = fb / sb - 1 if sb > 0 else 0.0
        fold_returns.append(ret)
        equity *= 1 + ret
    tf = str(have[0].get("timeframe_detail") or have[0].get("timeframe") or "")
    dd_mtm, method = _mtm_dd(plan, infos, start, tf, have[0]["_cfg"])
    dd_closed = _closed_dd(infos, start)
    levs = [r["leverage_max"] for r in have if r.get("leverage_max") is not None]
    return {
        "profit_total": equity / start - 1,
        "profit_factor": _profit_factor([i["profit"] for i in infos]),
        "trades": len(infos),
        "dd_closed": dd_closed,
        "dd_mtm": dd_mtm,
        "dd_mtm_method": method,
        "dd_worst": max(dd_closed, dd_mtm),
        "leverage_max": max(levs) if levs else None,
        "fold_returns": fold_returns,
        "folds_missing": sum(1 for r in folds if r is None),
    }


def overlay(plan, results: list[dict[str, Any]]) -> dict[str, Any] | None:
    """组合检查：几个策略的样本外交易放进同一个账户（各自按整个账户下单），直接叠加盈亏。"""
    if len(results) < 2:
        return None
    start = float(results[0].get("starting_balance") or DEFAULT_START_BALANCE)
    infos = []
    for r in results:
        infos.extend(i for i in (cd._trade_info(t) for t in r["_st"].get("trades") or []) if i is not None)
    tfs = [str(r.get("timeframe_detail") or r.get("timeframe") or "") for r in results]
    tf = min((t for t in tfs if cd._tf_seconds(t) > 0), key=cd._tf_seconds, default="")
    dd_mtm, method = _mtm_dd(plan, infos, start, tf, results[0]["_cfg"])
    dd_closed = _closed_dd(infos, start)
    return {
        "profit_total": sum(i["profit"] for i in infos) / start,
        "profit_factor": _profit_factor([i["profit"] for i in infos]),
        "trades": len(infos),
        "dd_closed": dd_closed,
        "dd_mtm": dd_mtm,
        "dd_mtm_method": method,
        "dd_worst": max(dd_closed, dd_mtm),
    }


# ----------------------------------------------------------------------------
# 资金费估算（OKX 只能下载最近约 3 个月的资金费率，更早的持仓 freqtrade 按 0 算）
# ----------------------------------------------------------------------------
FUNDING_EST_RATE = 0.0001  # 每 8 小时 0.01%：OKX / 币安永续的"中性"资金费率
_FUNDING_FIRST_CACHE: dict[str, dict[str, float | None]] = {}


def funding_first_dates(plan: dict[str, Any]) -> dict[str, float | None]:
    """每个交易对资金费率数据的第一条时间（秒）；没有数据 = None。"""
    key = str(plan.get("datadir"))
    if key in _FUNDING_FIRST_CACHE:
        return _FUNDING_FIRST_CACHE[key]
    import pandas as pd

    datadir = Path(plan["datadir"])
    sub = datadir / "futures" if plan.get("trading_mode") == "futures" else datadir
    fmt = str(plan.get("dataformat") or "feather")
    out: dict[str, float | None] = {}
    for pair in plan.get("pairs") or []:
        first: float | None = None
        for f in sorted(sub.glob(f"{cd._pair_file(pair)}-*-funding_rate.{fmt}")):
            try:
                df = pd.read_feather(f, columns=["date"]) if fmt == "feather" else pd.read_parquet(f, columns=["date"])
            except Exception:  # noqa: BLE001 — 读不出来按没有数据处理
                df = None
            if df is not None and not df.empty:
                t = pd.to_datetime(df["date"], utc=True).min().timestamp()
                first = t if first is None else min(first, t)
        out[pair] = first
    _FUNDING_FIRST_CACHE[key] = out
    return out


def funding_estimate(plan: dict[str, Any], r: dict[str, Any] | None) -> dict[str, Any] | None:
    """按每 8 小时 0.01% 估算"没有资金费数据那段持仓时间"的资金费（做多付、做空收）。

    有资金费数据的时间段 freqtrade 已经按真实费率算进收益里了，这里只补没有数据的部分。只用来提醒，不参与判定。
    """
    if r is None or plan.get("trading_mode") != "futures":
        return None
    firsts = funding_first_dates(plan)
    start = float(r.get("starting_balance") or DEFAULT_START_BALANCE)
    adj = 0.0
    hold_all = hold_missing = 0.0
    for t in r["_st"].get("trades") or []:
        i = cd._trade_info(t)
        if i is None:
            continue
        first = firsts.get(i["pair"])
        end_missing = i["close"] if first is None else min(i["close"], first)
        missing_h = max(0.0, end_missing - i["open"]) / 3600
        hold_all += max(0.0, i["close"] - i["open"]) / 3600
        hold_missing += missing_h
        notional = i["amount"] * i["open_rate"]
        adj -= i["dir"] * notional * FUNDING_EST_RATE * missing_h / 8
    known = [v for v in firsts.values() if v is not None]
    return {
        "adj_abs": adj,
        "adj_pct": adj / start if start > 0 else 0.0,
        "profit_total_est": float(r.get("profit_total") or 0.0) + (adj / start if start > 0 else 0.0),
        "missing_share": hold_missing / hold_all if hold_all > 0 else 0.0,
        "first_funding": datetime.fromtimestamp(min(known), tz=timezone.utc).date().isoformat() if known else None,
    }


# ----------------------------------------------------------------------------
# 第 5 轮：预注册登记表（configs/preregistration.json）
# ----------------------------------------------------------------------------
# 登记表里每个策略可以有自己的盈亏因子门槛（min_pf）和"算失败的情况"（extra_checks，预注册第 2 节）。
# 没登记的策略（第 1 ~ 4 轮的）照旧按 plan.json 的 settings 判，结果和以前逐字相同。
# 登记表的格式（integrator 写，见 docs/round5-preregistration.md）：
#   {"round": 5, "spec_doc": "...", "strategies": {"<类名>": {"file", "sha256", "venue", "stage", "params",
#     "min_pf": 1.2 | {"value": 1.1, "pending_decision": true, "note": "..."},
#     "min_pf_pending_decision": true | "说明"（可选，和上面对象写法二选一）,
#     "extra_checks": [{"type": ...}, ...]}}, "changes": [...]}
PREREG_DEFAULT = ROOT_DIR / "configs" / "preregistration.json"
CHECK_TYPES = (
    "funding_net_positive", "zero_funding_share_invalid", "max_trades_per_pair_year",
    "min_tradable_pairs", "drop_top_days", "count_tags_exclude",
)
# 登记表里的段名 -> 研究脚本的段名
SEG_ALIAS = {"unseen": "holdout", "holdout": "holdout", "oos": "test", "test": "test", "train": "train", "wf": "wf"}
SEG_CN5 = {"train": "训练段", "holdout": "没看过的一段", "test": "样本外", "wf": "走步拼接", "all": "全期",
           "preholdout": "2020 留出段"}
VERDICT_CN = {"PASS": "通过", "FAIL": "未通过", "INVALID": "无效", "UNTESTABLE": "测不了", "ERROR": "出错"}
VERDICT_ORDER = {"PASS": 0, "FAIL": 1, "UNTESTABLE": 2, "INVALID": 3, "ERROR": 4}
# 2020 留出段（预注册第 3.3 节，RESEARCH_PRE_HOLDOUT）：jobs.tsv 里的段名（两种写法都认）
PRE_HOLDOUT_SEGMENTS = ("preholdout", "pre_holdout")
# 事件集中度表（只报告）：没登记 drop_top_days 的策略也按预注册第 4 节第 6 条的默认做法列出来
EVENT_TOP_DAYS_DEFAULT = 3
EVENT_SEGMENTS_DEFAULT = ("holdout", "test")
# PremiumFade 日志里的固定标记（和 user_data/strategies/PremiumFade.py 相同，改的话两边一起改）：
# 有这种标记的策略，"能交易的币" = 数据条件满足过（z 有值的K线 > 0）的币
# 最小下单额提醒里"前 K 名、名额空到下一个调仓日"的说法只适用于按调仓日排名的截面策略
XS_SLOT_STRATEGIES = ("XSMomentum", "XSFundingCarry")
SPOT_MISSING_TAG = "[现货数据:没有]"
SPOT_COVERAGE_TAG = "[现货数据:覆盖]"
_COVERAGE_USABLE_RE = re.compile(r"有值的\s*K\s*线\s*(\d+)\s*根")


def _read_jsonc(path: Path) -> Any:
    """JSON（允许 // 注释和多余的逗号，和 configs/ 下的配置一样）。"""
    text = path.read_text(encoding="utf-8")
    try:
        return json.loads(text)
    except ValueError:
        import rapidjson  # freqtrade 自带

        return rapidjson.loads(text, parse_mode=rapidjson.PM_COMMENTS | rapidjson.PM_TRAILING_COMMAS)


def load_prereg(path: str | Path | None = None) -> dict[str, Any]:
    """读登记表。返回 {"path", "sha256", "data", "error", "explicit"}。

    path 为空 = configs/preregistration.json（没有这个文件 = 空登记表，全部按默认规则）；
    给了 path 但文件不存在 / 读不出来、或默认文件读不出来 = error（报告不生成：登记表坏了，第 5 轮的检查会悄悄不做）。
    """
    import hashlib

    explicit = bool(path)
    p = Path(path) if path else PREREG_DEFAULT
    out: dict[str, Any] = {"path": str(p), "sha256": "", "data": {}, "error": "", "explicit": explicit, "found": False}
    if not p.is_file():
        if explicit:
            out["error"] = f"找不到登记表 {p}"
        return out
    try:
        raw = p.read_bytes()
        data = _read_jsonc(p)
    except Exception as exc:  # noqa: BLE001
        out["error"] = f"登记表 {p} 读不出来（{exc.__class__.__name__}: {exc}）"
        return out
    if not isinstance(data, dict) or not isinstance(data.get("strategies", {}), dict):
        out["error"] = f"登记表 {p} 的格式不对（要有 \"strategies\": {{类名: {{...}}}}）"
        return out
    out.update(found=True, data=data, sha256=hashlib.sha256(raw).hexdigest())
    return out


def prereg_snapshot(plan: dict[str, Any] | None) -> dict[str, Any] | None:
    """正式运行（plan.json 的 prereg.formal_run 为真）用运行时记下的登记表快照判，不用现在的登记表。

    快照 = scripts/check_prereg.py 写进 plan.json 的每个登记策略的 registry_entry（运行时登记表里这个策略的原文）。
    预注册要求规则在回测之前定下来：正式运行以后再改登记表（比如 min_pf），不能改变这次运行的结论。
    不是正式运行、或者 plan.json 里没有快照 = None（照旧按现在读到的登记表判）。"""
    pr = (plan or {}).get("prereg")
    if not isinstance(pr, dict) or pr.get("formal_run") is not True:
        return None
    sts = pr.get("strategies")
    if not isinstance(sts, dict):
        return None
    ents = {n: r["registry_entry"] for n, r in sts.items()
            if isinstance(r, dict) and r.get("registered") and isinstance(r.get("registry_entry"), dict)}
    if not ents:
        return None
    path = str(pr.get("registry") or PREREG_DEFAULT)
    if not Path(path).is_absolute():
        path = str(ROOT_DIR / path)
    return {"path": path, "sha256": str(pr.get("registry_sha256") or ""),
            "data": {"round": pr.get("registry_round"), "strategies": ents},
            "error": "", "explicit": False, "found": True, "snapshot": True}


def prereg_for_plan(plan: dict[str, Any] | None, live: dict[str, Any]) -> dict[str, Any]:
    """正式运行 → 运行时快照（记下现在读到的登记表，抬头里提醒有没有改过）；否则 = live。"""
    snap = prereg_snapshot(plan)
    if snap is None:
        return live
    snap.update(live_path=live.get("path"), live_sha256=live.get("sha256") or "", live_error=live.get("error") or "",
                live_found=bool(live.get("found")))
    return snap


def _pos_number(v: Any) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) and f > 0 else None


def prereg_rules(prereg: dict[str, Any] | None, strat: str, s: dict[str, Any]) -> dict[str, Any]:
    """一个策略在登记表里的规则（没登记 = 默认：min_pf 用 settings，没有附加检查）。

    返回 {"registered", "min_pf", "min_pf_source", "min_pf_pending", "min_pf_note", "checks", "exclude_tags",
          "errors"（登记表写错的地方：这一行判未通过）, "venue", "stage"}。"""
    default_pf = float(s.get("min_pf", 1.2))
    out: dict[str, Any] = {
        "registered": False, "min_pf": default_pf, "min_pf_source": "默认", "min_pf_pending": False,
        "min_pf_note": "", "checks": [], "exclude_tags": [], "errors": [], "venue": "", "stage": "",
    }
    entry = ((prereg or {}).get("data") or {}).get("strategies", {}).get(strat)
    if entry is None:
        return out
    out["registered"] = True
    if not isinstance(entry, dict):
        out["errors"].append("登记表里这个策略的内容不是 {...}")
        return out
    # venue 可以写一个交易所，也可以写列表（和 scripts/check_prereg.py 一样，两个阶段在不同交易所跑的策略）
    raw_v = entry.get("venues", entry.get("venue"))
    out["venue"] = "、".join(str(v) for v in raw_v if v) if isinstance(raw_v, list) else str(raw_v or "")
    out["stage"] = str(entry.get("stage") or "")
    mp = entry.get("min_pf")
    pending: Any = entry.get("min_pf_pending_decision")
    if isinstance(mp, dict):
        val = mp.get("value")
        pending = pending or mp.get("pending_decision")
        out["min_pf_note"] = str(mp.get("note") or "")
    else:
        val = mp
    pd_any = entry.get("pending_decision")
    if pd_any is True or (isinstance(pd_any, (list, tuple)) and "min_pf" in pd_any):
        pending = pending or True
    elif isinstance(pd_any, dict) and "min_pf" in pd_any:
        pending = pending or (pd_any.get("min_pf") or True)
    if isinstance(pending, str) and pending and not out["min_pf_note"]:
        out["min_pf_note"] = pending
    out["min_pf_pending"] = bool(pending)
    if val is not None:
        f = _pos_number(val)
        if f is None:
            out["errors"].append(f"登记表里 min_pf 不是正数（{val!r}）")
        else:
            out["min_pf"], out["min_pf_source"] = f, "登记表"
    checks = entry.get("extra_checks") or []
    if not isinstance(checks, list):
        out["errors"].append("登记表里 extra_checks 不是列表")
        checks = []
    for c in checks:
        t = c.get("type") if isinstance(c, dict) else None
        if t not in CHECK_TYPES:
            out["errors"].append(f"登记表里有这个脚本不认识的检查 {t if t else c!r}（没法判定）")
            continue
        n: dict[str, Any] = {"type": t}
        if t == "zero_funding_share_invalid":
            v = _pos_number(c.get("max_share"))
            if v is None or v > 1:
                out["errors"].append(f"{t} 的 max_share 要是 0 ~ 1 之间的数（{c.get('max_share')!r}）")
                continue
            n["max_share"] = v
        elif t in ("max_trades_per_pair_year", "min_tradable_pairs"):
            v = _pos_number(c.get("value"))
            if v is None:
                out["errors"].append(f"{t} 的 value 要是正数（{c.get('value')!r}）")
                continue
            n["value"] = v
        elif t == "drop_top_days":
            k = c.get("n")
            segs = c.get("segments")
            if not isinstance(k, int) or isinstance(k, bool) or k < 1:
                out["errors"].append(f"{t} 的 n 要是正整数（{k!r}）")
                continue
            if not isinstance(segs, list) or not segs or any(x not in SEG_ALIAS for x in segs):
                out["errors"].append(f"{t} 的 segments 要是 {sorted(SEG_ALIAS)} 里的名字（{segs!r}）")
                continue
            n["n"] = k
            n["segments"] = list(dict.fromkeys(SEG_ALIAS[x] for x in segs))
        elif t == "count_tags_exclude":
            tags = c.get("tags")
            if not isinstance(tags, list) or not tags or not all(isinstance(x, str) and x for x in tags):
                out["errors"].append(f"{t} 的 tags 要是非空的字符串列表（{tags!r}）")
                continue
            n["tags"] = list(tags)
            out["exclude_tags"] = list(dict.fromkeys(out["exclude_tags"] + list(tags)))
        out["checks"].append(n)
    return out


def _check_text(c: dict[str, Any]) -> str:
    t = c["type"]
    if t == "funding_net_positive":
        return "全期资金费合计必须是净收入"
    if t == "zero_funding_share_invalid":
        return f"{c['max_share']:.0%} 以上的交易资金费为 0 = 无效（资金费没记进去，先修数据）"
    if t == "max_trades_per_pair_year":
        return f"平均每个币每年交易超过 {c['value']:g} 笔 = 失败"
    if t == "min_tradable_pairs":
        return f"能交易的币少于 {c['value']:g} 个 = 测不了"
    if t == "drop_top_days":
        return f"去掉盈利最多的 {c['n']} 个入场日后，{'、'.join(SEG_CN5[x] for x in c['segments'])}不能亏"
    if t == "count_tags_exclude":
        return f"交易数（30 笔门槛、每币每年交易数）不数 enter_tag 为 {'、'.join(c['tags'])} 的交易"
    return t


def _trades_of(r: dict[str, Any] | None) -> list[dict[str, Any]]:
    return list(((r or {}).get("_st") or {}).get("trades") or [])


def counted_trades(trades: list[dict[str, Any]], exclude: list[str] | tuple[str, ...]) -> int:
    """交易数口径：不数 enter_tag 在 exclude 里的交易（NewListingShortHedged 的 hedge 对冲腿）。"""
    ex = set(exclude or ())
    return sum(1 for t in trades if str(t.get("enter_tag") or "") not in ex)


def funding_stats(trades: list[dict[str, Any]]) -> dict[str, Any]:
    """funding_fees 合计（freqtrade：正数 = 收到、负数 = 付出）和资金费为 0 的交易数（None 也算 0）。"""
    total = 0.0
    zero = 0
    for t in trades:
        v = cd._num(t.get("funding_fees"))
        if v is None or v == 0.0:
            zero += 1
        else:
            total += v
    return {"total": total, "zero": zero, "n": len(trades), "zero_share": (zero / len(trades)) if trades else None}


def _log_records(text: str) -> list[str]:
    """一条日志（以时间开头的那行 + 后面的续行）拼成一个字符串（freqtrade 会把长行折开，见 min_stake_counts）。"""
    records: list[list[str]] = []
    for line in text.splitlines():
        if _LOG_RECORD_START.match(line) or not records:
            records.append([line])
        else:
            records[-1].append(line)
    return ["".join(r) for r in records]


def pair_capability(plan: dict[str, Any], jobs: list[dict[str, str]], strat: str) -> dict[str, Any]:
    """"能交易的币"：数据检查后保留的币（plan.json 的 pairs），再去掉策略在日志里标记为"没有数据 / 数据条件从没满足"的币
    （PremiumFade 的 [现货数据:没有] / [现货数据:覆盖] 标记；没有这种标记的策略 = 全部保留的币）。各段日志取并集。"""
    pairs = list(plan.get("pairs") or [])
    usable: dict[str, int] = {}
    missing: set[str] = set()
    tagged = False
    for j in jobs:
        if j["strategy"] != strat or j["variant"] != "default" or j["status"] != "ok" or not j.get("log"):
            continue
        try:
            text = Path(j["log"]).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if SPOT_MISSING_TAG not in text and SPOT_COVERAGE_TAG not in text:
            continue
        for rec in _log_records(text):
            has_cov, has_miss = SPOT_COVERAGE_TAG in rec, SPOT_MISSING_TAG in rec
            if not (has_cov or has_miss):
                continue
            head = rec.split(" - ", 3)[-1]
            m = _MIN_STAKE_PAIR_RE.search(head)
            if not m:
                continue
            pair = m.group(1)
            tagged = True
            if has_cov:
                hits = _COVERAGE_USABLE_RE.findall(rec)
                usable[pair] = max(usable.get(pair, 0), int(hits[-1]) if hits else 0)
            else:
                missing.add(pair)
    if tagged:
        tradable = [p for p in pairs if usable.get(p, 0) > 0]
        how = "日志里数据条件满足过的币（现货数据）"
    else:
        tradable = pairs
        how = "数据检查后保留的币"
    return {"tradable": tradable, "n": len(tradable), "pairs": len(pairs), "how": how, "tagged": tagged,
            "not_tradable": [p for p in pairs if p not in tradable]}


def _seg_window(plan: dict[str, Any], seg: str) -> tuple[date, date] | None:
    if seg in ("train", "test"):
        w = plan.get(seg)
    elif seg == "holdout":
        w = plan.get("holdout")
    else:
        w = None
    if not w:
        return None
    return date.fromisoformat(str(w[0])[:10]), date.fromisoformat(str(w[1])[:10])


def pair_years(plan: dict[str, Any], strat: str, seg: str, pairs: list[str], spot: bool = False) -> float | None:
    """这些币在这一段里"能交易"的时间合计（年）：每个币从 max(这一段开始, 第一根K线 + 这一段的预热) 到这一段结束
    （和 prepare 里"每个币从哪天开始能交易"同一个算法）。
    spot = True（PremiumFade 这种要现货数据才能交易的策略，数据检查里有现货覆盖时）：开始再和"现货第一根 + 预热"取晚的，
    结束和"现货最后一根"取早的（现货晚开始 / 早结束的币分母不虚高；中间零星缺几根不扣）。"""
    win = _seg_window(plan, seg)
    if win is None:
        return None
    info = (plan.get("strategies") or {}).get(strat) or {}
    tf_sec = cd._tf_seconds(str(info.get("timeframe") or ""))
    wc = (info.get("warmup_candles") or {}).get(seg, info.get("startup") or 0)
    warm = math.ceil(int(wc) * tf_sec / 86400) if tf_sec > 0 else 0
    firsts = plan.get("pairs_first") or {}
    spot_cov = ((plan.get("spot_coverage") or {}).get("pairs") or {}) if spot else {}
    total = 0
    for p in pairs:
        f = firsts.get(p)
        p0, p1 = win[0], win[1]
        if f:
            p0 = max(p0, date.fromisoformat(str(f)[:10]) + timedelta(days=warm))
        sc = spot_cov.get(p) or {}
        if sc.get("spot_first"):
            p0 = max(p0, date.fromisoformat(str(sc["spot_first"])[:10]) + timedelta(days=warm))
        if sc.get("spot_last"):
            p1 = min(p1, date.fromisoformat(str(sc["spot_last"])[:10]))
        total += max(0, (p1 - p0).days + 1)
    return total / 365.25


def drop_top_days(trades: list[tuple[float, float]], start: float, n: int) -> dict[str, Any]:
    """事件集中度：按入场日期（UTC）分组，去掉盈利最多的 n 天（只去盈利为正的天）的全部交易，重算这一段的收益。

    trades = [(开仓时间秒, 盈亏 USDT)]。近似：其余交易的盈亏不变（没有按去掉以后的资金重新缩放仓位）。"""
    by_day: dict[str, list[float]] = {}
    for ts, p in trades:
        d = datetime.fromtimestamp(ts, tz=timezone.utc).date().isoformat()
        by_day.setdefault(d, []).append(p)
    sums = {d: sum(v) for d, v in by_day.items()}
    top = sorted((d for d, v in sums.items() if v > 0), key=lambda d: (-sums[d], d))[:n]
    rest = [p for ts, p in trades if datetime.fromtimestamp(ts, tz=timezone.utc).date().isoformat() not in top]
    total = sum(p for _ts, p in trades)
    dropped = sum(sums[d] for d in top)
    return {
        "n": n, "days": [{"day": d, "profit": sums[d], "trades": len(by_day[d])} for d in top],
        "trades": len(trades), "trades_after": len(rest),
        "profit_before": total / start if start > 0 else 0.0,
        "profit_after": (total - dropped) / start if start > 0 else 0.0,
        "pf_after": _profit_factor(rest),
        "share": dropped / total if total > 0 else None,
    }


def _segment_trades(r: dict[str, Any] | None) -> tuple[list[tuple[float, float]], float]:
    out = []
    for t in _trades_of(r):
        try:
            out.append((cd._parse_ts(t["open_date"]), float(t.get("profit_abs") or 0.0)))
        except (KeyError, ValueError):
            continue
    return out, float((r or {}).get("starting_balance") or DEFAULT_START_BALANCE)


def _wf_trades(folds: list[dict[str, Any] | None]) -> tuple[list[tuple[float, float]], float]:
    infos, start, _missing = research_portfolio.stitched_infos(folds)
    return [(i["open"], i["profit"]) for i in infos], start


def calendar_years(plan: dict[str, Any], row: dict[str, Any]) -> dict[str, Any] | None:
    """逐年表（只报告）：训练段 + 样本外两次回测按时间顺序复利拼成一条资金曲线（样本外的交易按"训练段结束时的权益 /
    样本外回测的初始资金"缩放，和走步拼接同一口径），每个日历年：
      收益 = 年底权益 / 年初权益 - 1（权益含浮亏，取那个时间点之前最后一根已收盘K线的收盘权益）；
      回撤 = 这一年里账户权益从最高点到最低点的跌幅，峰值从年初权益起算（和 check_drawdown.py 同一个公式：
             逐根K线、含浮亏，再和只看已平仓余额的回撤取大的；scripts/nfi_yearly_report.py 每年单独回测也是这样判）。
    训练段最后还开着的仓位在训练段结束时被平掉（freqtrade 回测的做法），所以跨训练段 / 样本外那一年不是一次连续回测。"""
    import numpy as np

    parts = [r for r in (row.get("train"), row.get("test")) if r is not None]
    if not parts:
        return None
    start = float(parts[0].get("starting_balance") or DEFAULT_START_BALANCE)
    equity = start
    infos: list[dict[str, Any]] = []
    for r in parts:
        sb = float(r.get("starting_balance") or start)
        fb = float(r.get("final_balance") or sb)
        factor = equity / sb if sb > 0 else 1.0
        for t in _trades_of(r):
            i = cd._trade_info(t)
            if i is None:
                continue
            i["amount"] *= factor
            i["profit"] *= factor
            infos.append(i)
        equity *= fb / sb if sb > 0 else 1.0
    tf = str(parts[0].get("timeframe_detail") or parts[0].get("timeframe") or "")
    cfg = parts[0].get("_cfg") or {}
    curve = None
    if infos:
        curve = cd.equity_curve_candles(
            infos, start, tf, Path(plan["datadir"]),
            str(cfg.get("dataformat_ohlcv") or plan.get("dataformat") or "feather"),
            str(cfg.get("trading_mode") or plan.get("trading_mode") or "futures"),
        )
        if curve is None:
            curve = research_portfolio.closed_curve(infos, start)
    else:
        curve = research_portfolio.flat_curve(start)
    # 区间按 plan.json 的切分（训练段开始 ~ 样本外结束）；只有一段有结果时只算那一段
    d0 = date.fromisoformat(str(plan["train" if row.get("train") is not None else "test"][0])[:10])
    d1 = date.fromisoformat(str(plan["test" if row.get("test") is not None else "train"][1])[:10])
    G, g, end, low = curve["grid"], curve["g"], curve["end"], curve["low"]
    closes = sorted(curve.get("closes") or [], key=lambda x: x[0])
    years = []
    for y in range(d0.year, d1.year + 1):
        a = max(date(y, 1, 1), d0)
        b = min(date(y + 1, 1, 1), d1 + timedelta(days=1))
        ta = research_portfolio._day_ts(a)
        tb = research_portfolio._day_ts(b)
        ea, eb = (float(x) for x in research_portfolio.value_at(G, g, end, curve["start"], [ta, tb]))
        m = (G >= ta) & (G < tb) if len(G) else np.zeros(0, dtype=bool)
        dd_mtm = cd.curve_max_dd({"low": low[m], "end": end[m], "start": ea}) if len(G) else 0.0
        bal0 = curve["start"] + sum(p for t, p in closes if t <= ta)
        yc = [(t, p) for t, p in closes if ta < t <= tb]
        dd_closed = research_portfolio.closed_dd(yc, bal0)
        years.append({
            "year": y, "start": _iso(a), "end": _iso(b - timedelta(days=1)),
            "full": a == date(y, 1, 1) and b == date(y + 1, 1, 1),
            "return": eb / ea - 1 if ea > 0 else None, "equity_start": ea, "equity_end": eb,
            "dd_mtm": dd_mtm, "dd_closed": dd_closed, "dd_worst": max(dd_mtm, dd_closed), "trades_closed": len(yc),
        })
    return {"method": curve["method"], "start": start, "years": years}


def pre_holdout_job(jobs: list[dict[str, str]], strat: str) -> dict[str, str] | None:
    for seg in PRE_HOLDOUT_SEGMENTS:
        j = _job(jobs, strat, "default", seg)
        if j is not None:
            return j
    return None


def round5_stats(plan: dict[str, Any], jobs: list[dict[str, str]], row: dict[str, Any],
                 rules: dict[str, Any]) -> dict[str, Any]:
    """一行的第 5 轮附加统计（资金费、每币每年交易数、能交易的币、事件集中度、逐年表、2020 留出段）。"""
    strat, variant = row["strategy"], row["variant"]
    ex = rules["exclude_tags"]
    segs = {"train": row.get("train"), "holdout": row.get("holdout"), "test": row.get("test")}
    out: dict[str, Any] = {"funding": {}, "tpy": {}, "event": {}}
    # 资金费：每一段的 funding_fees 合计（USDT，每段回测都从 1000 USDT 开始）和资金费为 0 的交易占比
    for seg, r in segs.items():
        if r is not None:
            out["funding"][seg] = funding_stats(_trades_of(r))
    if row.get("folds"):
        fl = [t for f in row["folds"] if f is not None for t in _trades_of(f)]
        if any(f is not None for f in row["folds"]):
            out["funding"]["wf"] = funding_stats(fl)
    if segs["train"] is not None and segs["test"] is not None:
        out["funding"]["all"] = funding_stats(_trades_of(segs["train"]) + _trades_of(segs["test"]))
    # 能交易的币 / 有交易的币 / 每币每年交易数（交易数按 count_tags_exclude 的口径）
    cap = pair_capability(plan, jobs, strat)
    traded: set[str] = set()
    ex_set = set(ex)
    for r in (segs["train"], segs["test"]):
        traded.update(str(t.get("pair")) for t in _trades_of(r) if str(t.get("enter_tag") or "") not in ex_set)
    cap["traded"] = len(traded)
    out["pairs"] = cap
    tot_n, tot_py = 0, 0.0
    for seg in ("train", "holdout", "test"):
        r = segs[seg]
        py = pair_years(plan, strat, seg, cap["tradable"], spot=bool(cap.get("tagged"))) if r is not None else None
        n = counted_trades(_trades_of(r), ex) if r is not None else None
        out["tpy"][seg] = {"trades": n, "pair_years": py, "value": (n / py) if (n is not None and py) else None}
        if seg in ("train", "test") and n is not None and py is not None:
            tot_n += n
            tot_py += py
    if segs["train"] is not None and segs["test"] is not None:
        out["tpy"]["all"] = {"trades": tot_n, "pair_years": tot_py, "value": tot_n / tot_py if tot_py > 0 else None}
    # 事件集中度
    want: dict[str, int] = {}
    for c in rules["checks"]:
        if c["type"] == "drop_top_days":
            for s_ in c["segments"]:
                want[s_] = max(want.get(s_, 0), int(c["n"]))
    for s_ in EVENT_SEGMENTS_DEFAULT:
        want.setdefault(s_, EVENT_TOP_DAYS_DEFAULT)
    for s_, n in want.items():
        if s_ == "wf":
            if row.get("folds") and any(f is not None for f in row["folds"]):
                tl, st = _wf_trades(row["folds"])
                out["event"][s_] = drop_top_days(tl, st, n)
        elif segs.get(s_) is not None:
            tl, st = _segment_trades(segs[s_])
            out["event"][s_] = drop_top_days(tl, st, n)
    # 逐年表、2020 留出段：只看默认参数
    if variant == "default":
        try:  # 只报告的表：算不出来不影响判定，表里写"出错"
            out["years"] = calendar_years(plan, row)
        except Exception as exc:  # noqa: BLE001
            out["years"] = {"error": f"{exc.__class__.__name__}: {exc}", "years": [], "method": ""}
        j = pre_holdout_job(jobs, strat)
        if j is not None:
            ph = evaluate_dir(plan, strat, j["dir"]) if j["status"] == "ok" else None
            if ph is not None:
                ph["trades_counted"] = counted_trades(_trades_of(ph), ex)
            out["pre_holdout"] = {"status": j["status"], "timerange": j.get("timerange", ""), "result": ph}
    return out


def settings_for(s: dict[str, Any], rules: dict[str, Any]) -> dict[str, Any]:
    """这个策略用的研究参数：登记表里有自己的门槛（min_pf）就换成它；没登记 = settings 原样（结果和以前逐字相同）。"""
    return s if rules["min_pf_source"] == "默认" else {**s, "min_pf": rules["min_pf"]}


def set_counted(row: dict[str, Any], rules: dict[str, Any]) -> None:
    """judge() 之前：交易数口径（count_tags_exclude，例如不数 hedge 对冲腿）。只在登记了要排除的标签时设
    trades_counted，没登记的行一个字段都不加（判定和以前逐字相同）。"""
    row["rules"] = rules
    ex = rules["exclude_tags"]
    if not ex:
        return
    for seg in ("train", "test", "holdout"):
        if row.get(seg) is not None:
            row[seg]["trades_counted"] = counted_trades(_trades_of(row[seg]), ex)
    if row.get("wf") is not None:
        row["wf"]["trades_counted"] = sum(counted_trades(_trades_of(f), ex) for f in row.get("folds") or [] if f)


def finish_round5(plan: dict[str, Any], jobs: list[dict[str, str]], row: dict[str, Any], rules: dict[str, Any],
                  s: dict[str, Any]) -> None:
    """judge() 之后：算附加统计（row["r5"]），再按登记表的附加检查和 2020 留出段定结论。"""
    row["r5"] = round5_stats(plan, jobs, row, rules)
    apply_round5(row, rules, s, plan)


def apply_round5(row: dict[str, Any], rules: dict[str, Any], s: dict[str, Any], plan: dict[str, Any]) -> None:
    """judge() 之后：登记表的附加检查 + 2020 留出段的判定，最后定这一行的结论。

    无效（INVALID）> 测不了（UNTESTABLE）> 未通过 / 通过；出错（ERROR，一个结果都没有）不变。"""
    r5 = row["r5"]
    reasons: list[str] = row["reasons"]
    invalid: list[str] = []
    untestable: list[str] = []
    checks_out: list[dict[str, Any]] = []
    max_dd = cd.settings_max_dd_pct(s) / 100
    min_trades = int(s.get("min_trades", 30))
    for e in rules["errors"]:
        reasons.append(f"登记表写错：{e}")
    for c in rules["checks"]:
        t = c["type"]
        res = {"type": t, "text": _check_text(c), "result": "PASS", "detail": ""}
        if t == "funding_net_positive":
            fa = r5["funding"].get("all")
            if fa is None:
                res.update(result="FAIL", detail="训练段或样本外没有结果，算不出全期资金费合计")
            else:
                ftr, fte = r5["funding"]["train"]["total"], r5["funding"]["test"]["total"]
                res["detail"] = f"全期资金费合计 {fa['total']:+.2f} USDT（训练段 {ftr:+.2f} + 样本外 {fte:+.2f}）"
                if not fa["total"] > 0:
                    res["result"] = "FAIL"
            if res["result"] == "FAIL":
                reasons.append(f"机制检查：{res['detail']}，不是净收入：赚的不是资金费套息")
        elif t == "zero_funding_share_invalid":
            fa = r5["funding"].get("all")
            if fa is None or not fa["n"]:
                res.update(result="-", detail="没有交易，算不出占比")
            else:
                res["detail"] = f"全期 {fa['n']} 笔交易里 {fa['zero']} 笔资金费为 0（{fa['zero_share']:.1%}）"
                if fa["zero_share"] >= c["max_share"]:
                    res["result"] = "INVALID"
                    invalid.append(f"{res['detail']}，达到 {c['max_share']:.0%}：资金费多半没有记进去，先修数据（这一行不算通过也不算失败）")
        elif t == "max_trades_per_pair_year":
            ta = r5["tpy"].get("all")
            if ta is None or ta["value"] is None:
                res.update(result="FAIL", detail="训练段或样本外没有结果，算不出平均每个币每年的交易数")
                reasons.append(f"交易频率检查：{res['detail']}")
            else:
                res["detail"] = (f"全期平均每个币每年 {ta['value']:.2f} 笔（{ta['trades']} 笔 ÷ {ta['pair_years']:.1f} 个\"币·年\"，"
                                 f"{r5['pairs']['n']} 个能交易的币）")
                if ta["value"] > c["value"]:
                    res["result"] = "FAIL"
                    reasons.append(f"交易频率检查：{res['detail']}，超过 {c['value']:g} 笔：信号并不\"极端\"，按失败记录")
        elif t == "min_tradable_pairs":
            pc = r5["pairs"]
            res["detail"] = f"能交易的币 {pc['n']} 个（{pc['how']}；保留的币 {pc['pairs']} 个，有交易的 {pc['traded']} 个）"
            if pc["n"] < c["value"]:
                res["result"] = "UNTESTABLE"
                untestable.append(f"{res['detail']}，少于 {c['value']:g} 个：测不了，不算通过")
        elif t == "drop_top_days":
            bad, parts = [], []
            for seg in c["segments"]:
                if seg == "holdout" and row.get("variant") != "default":
                    continue
                ev = r5["event"].get(seg)
                if ev is None:
                    bad.append(f"{SEG_CN5[seg]}没有结果")
                    continue
                days = "、".join(f"{d['day']}（{d['profit']:+.1f}）" for d in ev["days"]) or "没有盈利的日子"
                parts.append(f"{SEG_CN5[seg]} {_pct(ev['profit_before'], True)} → {_pct(ev['profit_after'], True)}（去掉 {days}）")
                if not ev["profit_after"] > 0:
                    bad.append(f"{SEG_CN5[seg]}去掉盈利最多的 {c['n']} 个入场日后收益 {_pct(ev['profit_after'], True)}")
            res["detail"] = "；".join(parts)
            if bad:
                res["result"] = "FAIL"
                for b in bad:
                    reasons.append(f"集中度检查：{b}")
        elif t == "count_tags_exclude":
            res.update(result="-", detail=f"交易数只数 enter_tag 不是 {'、'.join(c['tags'])} 的交易")
        checks_out.append(res)
    # 2020 留出段（预注册第 3.3 节）：交易 ≥ 30 笔时必须赚钱、回撤 ≤ 上限，否则失败；不足 30 笔只报告
    ph = r5.get("pre_holdout")
    if ph is not None and row.get("variant") == "default":
        r = ph.get("result")
        if r is None:
            ph["verdict"] = "ERROR"
            reasons.append("2020 留出段回测失败或没有结果")
        else:
            n = int(r.get("trades_counted", r["trades"]))
            if n < min_trades:
                ph["verdict"] = "REPORT"
            elif r["profit_total"] > 0 and r["dd_worst"] <= max_dd:
                ph["verdict"] = "PASS"
            else:
                ph["verdict"] = "FAIL"
                if r["profit_total"] <= 0:
                    reasons.append(f"2020 留出段（{r['start']}~{r['end']}，{n} 笔）亏损 {_pct(r['profit_total'], True)}")
                if r["dd_worst"] > max_dd:
                    reasons.append(f"2020 留出段回撤 {_pct(r['dd_worst'])} 超过 {max_dd * 100:g}%")
    row["checks"] = checks_out
    row["invalid_reasons"] = invalid
    row["untestable_reasons"] = untestable
    if row["verdict"] == "ERROR":
        return
    if invalid:
        row["verdict"] = "INVALID"
    elif untestable:
        row["verdict"] = "UNTESTABLE"
    else:
        row["verdict"] = "FAIL" if reasons else "PASS"


# ----------------------------------------------------------------------------
# 判定
# ----------------------------------------------------------------------------
def _counted(r: dict[str, Any]) -> int:
    """交易数门槛用的笔数（第 5 轮 count_tags_exclude：不数对冲腿；没登记 = 全部交易，和以前一样）。"""
    return int(r.get("trades_counted", r["trades"]))


def judge(row: dict[str, Any], s: dict[str, Any], wf_enabled: bool, holdout_enabled: bool = False,
          all_segments: bool = False, exclude_tags: list[str] | None = None) -> None:
    """s = 研究参数（plan.json 的 settings；第 5 轮登记的策略 min_pf 换成登记表里的值）。

    all_segments（第 5 轮登记的策略）：预注册第 3.3 节"训练段、没看过的一段、样本外、走步拼接，每一段都要同时满足：
    赚钱、回撤 ≤ 上限、PF ≥ 门槛、至少 30 笔"——训练段和没看过的一段也查盈亏因子和交易数（以前只查样本外和走步）。
    exclude_tags：交易数门槛不数的 enter_tag（只影响原因里的说明文字；笔数用各段的 trades_counted）。"""
    max_dd = cd.settings_max_dd_pct(s) / 100
    max_lev = float(s.get("max_leverage", 5.0))
    min_pf = float(s.get("min_pf", 1.2))
    min_trades = int(s.get("min_trades", 30))
    ratio = float(s.get("pf_ratio", 0.6))
    tr, te, wf = row["train"], row["test"], row["wf"]
    reasons: list[str] = []
    cnt_note = f"只数 enter_tag 不是 {'、'.join(exclude_tags)} 的；" if exclude_tags else ""
    if te is None:
        reasons.append("样本外回测失败或没有结果")
    else:
        if te["trades"] == 0:
            reasons.append("样本外一笔交易都没有")
        if te["dd_worst"] > max_dd:
            reasons.append(f"样本外回撤 {_pct(te['dd_worst'])} 超过 {max_dd * 100:g}%")
        if te["trades"] > 0 and te["profit_total"] <= 0:
            reasons.append(f"样本外亏损 {_pct(te['profit_total'], True)}")
        pf = te["profit_factor"]
        if te["trades"] > 0 and (pf is None or pf < min_pf):
            reasons.append(f"样本外盈亏因子 {_num(pf)} < {min_pf:g}")
        if te["trades"] > 0 and _counted(te) < min_trades:
            reasons.append(f"样本外只有 {_counted(te)} 笔交易（{cnt_note}< {min_trades}）")
        if te.get("leverage_max") is not None and te["leverage_max"] > max_lev + 1e-9:
            reasons.append(f"杠杆 {te['leverage_max']:g}x 超过 {max_lev:g}x")
    # 没看过的一段（第 3 轮唯一没被以前的研究看过的数据）：只对默认参数那一行判（优化参数是在包含它的训练段上调的）
    if holdout_enabled and row.get("variant") == "default":
        ho = row.get("holdout")
        if ho is None:
            reasons.append("没看过的一段回测失败或没有结果")
        elif ho["trades"] == 0:
            reasons.append("没看过的一段一笔交易都没有")
        else:
            if ho["profit_total"] <= 0:
                reasons.append(f"没看过的一段（{ho['start']}~{ho['end']}）亏损 {_pct(ho['profit_total'], True)}")
            if ho["dd_worst"] > max_dd:
                reasons.append(f"没看过的一段回撤 {_pct(ho['dd_worst'])} 超过 {max_dd * 100:g}%")
            if all_segments:
                if ho["profit_factor"] is None or ho["profit_factor"] < min_pf:
                    reasons.append(f"没看过的一段盈亏因子 {_num(ho['profit_factor'])} < {min_pf:g}")
                if _counted(ho) < min_trades:
                    reasons.append(f"没看过的一段只有 {_counted(ho)} 笔交易（{cnt_note}< {min_trades}）")
    if tr is None:
        reasons.append("训练段回测失败或没有结果")
    else:
        if tr["trades"] == 0:
            reasons.append("训练段一笔交易都没有")
        elif tr["profit_total"] <= 0:
            reasons.append(f"训练段本身就亏损 {_pct(tr['profit_total'], True)}（样本外赚钱多半是运气/行情）")
        if tr["dd_worst"] > max_dd:
            reasons.append(f"训练段回撤 {_pct(tr['dd_worst'])} 超过 {max_dd * 100:g}%")
        if all_segments and tr["trades"] > 0:
            if tr["profit_factor"] is None or tr["profit_factor"] < min_pf:
                reasons.append(f"训练段盈亏因子 {_num(tr['profit_factor'])} < {min_pf:g}")
            if _counted(tr) < min_trades:
                reasons.append(f"训练段只有 {_counted(tr)} 笔交易（{cnt_note}< {min_trades}）")
        tpf, opf = tr["profit_factor"], (te or {}).get("profit_factor")
        if te is not None and tpf is not None and not math.isinf(tpf) and tpf > 0 and opf is not None \
                and not math.isinf(opf) and opf < ratio * tpf:
            reasons.append(f"样本外明显退化：盈亏因子 训练 {tpf:.2f} → 样本外 {opf:.2f}（不到训练段的 {ratio:g} 倍）")
    if wf_enabled:
        if wf is None:
            reasons.append("走步验证没有结果")
        else:
            if wf["dd_worst"] > max_dd:
                reasons.append(f"走步回撤 {_pct(wf['dd_worst'])} 超过 {max_dd * 100:g}%")
            if wf["profit_total"] <= 0:
                reasons.append(f"走步验证亏损 {_pct(wf['profit_total'], True)}")
            if wf["trades"] > 0 and (wf["profit_factor"] is None or wf["profit_factor"] < min_pf):
                reasons.append(f"走步盈亏因子 {_num(wf['profit_factor'])} < {min_pf:g}")
            if _counted(wf) < min_trades:
                reasons.append(f"走步只有 {_counted(wf)} 笔交易（{cnt_note}< {min_trades}）")
            if wf["folds_missing"]:
                reasons.append(f"走步有 {wf['folds_missing']} 折没有结果")
    row["reasons"] = reasons
    if te is None and tr is None:
        row["verdict"] = "ERROR"
    else:
        row["verdict"] = "FAIL" if reasons else "PASS"


def _params_text(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    flat: dict[str, Any] = {}
    for space in ("buy", "sell"):
        flat.update((data.get("params") or {}).get(space) or {})
    return flat


def _params_meta(path: Path) -> dict[str, Any]:
    """params/<策略>__<段>.json 里 research 那一块（调参跑了几轮等）。"""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data.get("research") or {}


def _fmt_params(p: dict[str, Any]) -> str:
    return "、".join(f"{k}={v}" for k, v in p.items()) if p else "-"


_MIN_STAKE_PAIR_RE = re.compile(r"([A-Z0-9]+/[A-Z0-9]+(?::[A-Z0-9]+)?)")
_LOG_RECORD_START = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}")


def min_stake_counts(log_path: str | None) -> dict[str, Any]:
    """数回测日志里截面策略记的"最小下单额"标记（XSMomentum.custom_stake_amount 记的，同一个币同一根K线只记一次）。

    freqtrade 会把太长的日志行折成几行（中文按字折，标记本身也可能被折开），所以按"一条日志"（以时间开头的那行 + 后面的续行）
    拼起来再找标记；币名在这条日志的第一行里。
    返回 {"skip": 开不出来的次数, "bump": 被 freqtrade 放大的次数, "skip_pairs": {币: 次数}}。日志读不到 = 全是 0。
    """
    out: dict[str, Any] = {"skip": 0, "bump": 0, "skip_pairs": {}}
    if not log_path:
        return out
    try:
        text = Path(log_path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return out
    if "最小下单额" not in text:
        return out
    records: list[list[str]] = []
    for line in text.splitlines():
        if _LOG_RECORD_START.match(line) or not records:
            records.append([line])
        else:
            records[-1].append(line)
    for rec in records:
        joined = "".join(rec)
        if MIN_STAKE_SKIP_TAG in joined:
            out["skip"] += 1
            head = rec[0].split(" - ", 3)[-1]  # 去掉 "时间 - 模块 - 级别 - " 前缀
            m = _MIN_STAKE_PAIR_RE.search(head)
            if m:
                coin = m.group(1).split("/")[0]
                out["skip_pairs"][coin] = out["skip_pairs"].get(coin, 0) + 1
        elif MIN_STAKE_BUMP_TAG in joined:
            out["bump"] += 1
    return out


def _trade_key(t: dict[str, Any]) -> tuple[str, str]:
    return str(t.get("pair")), str(t.get("open_date") or t.get("open_timestamp"))


def compare_lowrisk(low: dict[str, Any] | None, base: dict[str, Any] | None) -> dict[str, Any] | None:
    """低风险版 vs 原版（信号相同、只有仓位不同），逐笔对比同一段回测的交易。

    返回：原版有、低风险版没开出来的（多半是仓位太小凑不够最小下单量）；低风险版多出来的；
    共同的交易里保证金之比（应该约 0.5）、和原版一样大的笔数（合约张数取整后都是最少的那一档，风险没减半）。
    """
    if not low or not base:
        return None
    lt = {_trade_key(t): t for t in (low["_st"].get("trades") or [])}
    bt = {_trade_key(t): t for t in (base["_st"].get("trades") or [])}
    missing = [bt[k] for k in bt if k not in lt]
    extra = [k for k in lt if k not in bt]
    ratios: list[float] = []
    same = 0
    for k in bt:
        if k not in lt:
            continue
        b_st, l_st = float(bt[k].get("stake_amount") or 0.0), float(lt[k].get("stake_amount") or 0.0)
        b_am, l_am = float(bt[k].get("amount") or 0.0), float(lt[k].get("amount") or 0.0)
        if b_st > 0:
            ratios.append(l_st / b_st)
        if b_am > 0 and abs(l_am - b_am) <= 1e-9 * b_am:
            same += 1
    ratios.sort()
    miss_pairs: dict[str, int] = {}
    for t in missing:
        coin = str(t.get("pair")).split("/")[0]
        miss_pairs[coin] = miss_pairs.get(coin, 0) + 1
    return {
        "base_trades": len(bt), "low_trades": len(lt), "shared": len(bt) - len(missing),
        "missing": len(missing), "missing_win": sum(1 for t in missing if float(t.get("profit_abs") or 0.0) > 0),
        "missing_profit": sum(float(t.get("profit_abs") or 0.0) for t in missing),
        "missing_pairs": dict(sorted(miss_pairs.items(), key=lambda kv: -kv[1])),
        "extra": len(extra), "same_size": same,
        "ratio_min": ratios[0] if ratios else None, "ratio_max": ratios[-1] if ratios else None,
        "ratio_median": ratios[len(ratios) // 2] if ratios else None,
    }


def _lowrisk_text(seg: str, c: dict[str, Any], base_name: str) -> str:
    where = _seg_label(seg)
    txt = f"和 {base_name} 逐笔对比（{where}）：{base_name} {c['base_trades']} 笔"
    if c["missing"]:
        coins = "、".join(f"{k} {v}" for k, v in list(c["missing_pairs"].items())[:5])
        txt += (f"，其中 {c['missing']} 笔这里没开出来（仓位太小、凑不够最小下单量：{coins}；"
                f"其中 {c['missing_win']} 笔是赚钱的，合计 {c['missing_profit']:+.1f} USDT）")
    if c["extra"]:
        txt += f"；这里多出 {c['extra']} 笔"
    if c["shared"]:
        txt += f"；共同的 {c['shared']} 笔"
        if c["same_size"]:
            txt += f"里 {c['same_size']} 笔仓位和原版一样大（合约张数取整后都是最少的一档，风险没减半）"
        if c["ratio_median"] is not None:
            txt += f"，保证金是原版的 {c['ratio_min']:.2f} ~ {c['ratio_max']:.2f} 倍（中位数 {c['ratio_median']:.2f}）"
    if c["missing"] or c["same_size"] or (c["ratio_median"] is not None and abs(c["ratio_median"] - 0.5) > 0.1):
        txt += "。\"回撤、收益大约减半\"只在每笔都正常减半时成立，这一段不完全是这样"
    return txt


def _year_returns(r: dict[str, Any] | None) -> dict[int, float]:
    """每年平仓的盈亏 ÷ 初始资金（按平仓日期分年；不复利，只用来看哪一年赚、哪一年亏）。"""
    out: dict[int, float] = {}
    if not r:
        return out
    start = float(r.get("starting_balance") or DEFAULT_START_BALANCE)
    for t in r["_st"].get("trades") or []:
        cd_ = str(t.get("close_date") or "")[:4]
        if cd_.isdigit() and start > 0:
            out[int(cd_)] = out.get(int(cd_), 0.0) + float(t.get("profit_abs") or 0.0) / start
    return dict(sorted(out.items()))


def build_rows(plan: dict[str, Any], jobs, notes, prereg: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """prereg = load_prereg() 的结果（第 5 轮登记表）；None = 读默认的 configs/preregistration.json（读不出来按空登记表）。"""
    run_dir = Path(plan["_run_dir"])
    folds = plan["wf"]["folds"]
    s = plan.get("settings", {})
    wf_enabled = bool(folds)
    holdout_enabled = bool(plan.get("holdout"))
    if prereg is None:
        prereg = load_prereg()
        if prereg.get("error"):
            prereg = {**prereg, "data": {}}
        prereg = prereg_for_plan(plan, prereg)
    rows = []
    for strat in plan["strategies_order"]:
        info = plan["strategies"][strat]
        rules = prereg_rules(prereg, strat, s)
        s_row = settings_for(s, rules)
        ex_tags = rules["exclude_tags"]
        variants = ["default"]
        if _job(jobs, strat, "opt", "test") or _job(jobs, strat, "opt", "train"):
            variants.append("opt")
        default_folds = [eval_job(plan, jobs, strat, "default", f"wf{f['k']}") for f in folds]
        default_holdout = eval_job(plan, jobs, strat, "default", "holdout") if holdout_enabled else None
        for variant in variants:
            row: dict[str, Any] = {
                "strategy": strat,
                "timeframe": info["timeframe"] or "?",
                "variant": variant,
                "train": eval_job(plan, jobs, strat, variant, "train"),
                "test": eval_job(plan, jobs, strat, variant, "test"),
                # 没看过的一段只用默认参数回测（优化参数是在包含它的训练段上调的，对它不是独立检验）
                "holdout": default_holdout if variant == "default" else None,
                "notes": [],
                "params": {},
                "fold_params": [],
                "ho_epochs": None,
            }
            for n_strat, n_var, msg in notes:
                if n_strat == strat and n_var in (variant, "*"):
                    row["notes"].append(msg)
            fold_res = default_folds
            if variant == "opt":
                fold_res = []
                fold_epochs: list[int | None] = []
                for f in folds:
                    k = f["k"]
                    r = eval_job(plan, jobs, strat, "opt", f"wf{k}")
                    p = run_dir / "params" / f"{strat}__wf{k}.json"
                    if r is None:
                        r = default_folds[k - 1]
                        if not any(n.startswith(f"走步第 {k} 折") for n in row["notes"]):
                            row["notes"].append(f"走步第 {k} 折没有调参结果，这一折用默认参数")
                        row["fold_params"].append(None)
                        fold_epochs.append(None)
                    else:
                        row["fold_params"].append(_params_text(p) if p.is_file() else None)
                        fold_epochs.append(_params_meta(p).get("epochs_evaluated") if p.is_file() else None)
                    fold_res.append(r)
                main_p = run_dir / "params" / f"{strat}__main.json"
                row["params"] = _params_text(main_p) if main_p.is_file() else {}
                row["ho_epochs"] = {
                    "main": _params_meta(main_p).get("epochs_evaluated") if main_p.is_file() else None,
                    "folds": fold_epochs,
                }
            row["folds"] = fold_res
            row["wf"] = stitch_folds(plan, fold_res) if wf_enabled else None
            set_counted(row, rules)
            if variant == "opt" and holdout_enabled:
                row["notes"].append(
                    "优化参数是在训练段（包含\"没看过的一段\"）上调出来的，没看过的一段对它不是独立检验；那一段只看默认参数那一行"
                )
            # 截面策略：前 K 名的币因为最小下单额开不出来（名额空到下一个调仓日）/ 被放大 的次数（数回测日志里的标记）
            segs_ms = ["train", "test"] + (["holdout"] if variant == "default" and holdout_enabled else [])
            segs_ms += [f"wf{f['k']}" for f in folds]
            ms: dict[str, Any] = {}
            for seg in segs_ms:
                j = _job(jobs, strat, variant, seg)
                if j is None and variant == "opt" and seg.startswith("wf"):
                    j = _job(jobs, strat, "default", seg)  # 这一折没调参，用的是默认参数
                if j and j["status"] == "ok":
                    c = min_stake_counts(j.get("log"))
                    if c["skip"] or c["bump"]:
                        ms[seg] = c
            row["min_stake"] = ms
            if ms:
                wf_skip = sum(c["skip"] for k, c in ms.items() if k.startswith("wf"))
                wf_bump = sum(c["bump"] for k, c in ms.items() if k.startswith("wf"))
                parts = [f"{_seg_label(k)} {c['skip']} 次" + (f"（另有 {c['bump']} 次被放大）" if c["bump"] else "")
                         for k, c in ms.items() if not k.startswith("wf")]
                if wf_skip or wf_bump:
                    parts.append(f"走步各折合计 {wf_skip} 次" + (f"（另有 {wf_bump} 次被放大）" if wf_bump else ""))
                coins: dict[str, int] = {}
                for c in ms.values():
                    for k, v in c["skip_pairs"].items():
                        coins[k] = coins.get(k, 0) + v
                top = "、".join(f"{k} {v}" for k, v in sorted(coins.items(), key=lambda kv: -kv[1])[:6])
                if strat in XS_SLOT_STRATEGIES:
                    row["notes"].append(
                        "最小下单额：排名进了前 K 名、但按风险算的仓位比交易所最小下单额小太多（freqtrade 最多放大 30%）开不出来的次数："
                        + "；".join(parts) + (f"（按币：{top}）" if top else "")
                        + "。开不出来的币还占着名额，那个名额空到下一个调仓日，回测结果是\"少开了几笔\"的结果；"
                        "实盘资金越少越严重（日志里带 " + MIN_STAKE_SKIP_TAG + " 的行）"
                    )
                else:
                    # 第 5 轮的事件型策略（PremiumFade、CapitulationRebound、NewListingShortHedged）：没有"前 K 名 / 调仓日"，只是少开了这一笔
                    row["notes"].append(
                        "最小下单额：有信号、但按风险算的仓位比交易所最小下单额小太多（freqtrade 最多放大 30%）开不出来的次数："
                        + "；".join(parts) + (f"（按币：{top}）" if top else "")
                        + "。这些信号这一次没有开仓，回测结果是\"少开了几笔\"的结果；"
                        "实盘资金越少越严重（日志里带 " + MIN_STAKE_SKIP_TAG + " 的行）"
                    )
            judge(row, s_row, wf_enabled, holdout_enabled, all_segments=rules["registered"], exclude_tags=ex_tags)
            if row["verdict"] == "ERROR":
                # 一个结果都没有：多半是策略代码报错。第一条原因直接指向日志，"没有可调参数"之类的提醒没有意义
                failed = [j for j in jobs if j["strategy"] == strat and j["variant"] == variant and j["status"] == "fail"]
                row["notes"] = [n for n in row["notes"] if "没有可调参数" not in n]
                if failed:
                    row["reasons"].insert(0, "回测运行出错（多半是策略代码报错）")
                    row["notes"].append(f"报错信息看结果目录里的 logs/{Path(failed[0]['log']).name}")
            # 第 5 轮：附加统计（只报告）+ 登记表的附加检查 + 2020 留出段（有结果时）
            finish_round5(plan, jobs, row, rules, s_row)
            tr = row["train"]
            if tr and tr["trades"] == 0 and strat == "FundingContrarian":
                row["notes"].append("资金费率历史 OKX 只有最近约 3 个月，训练段没有数据，这个策略在那段时间不开仓（策略的设计）")
            for seg in ("train", "test"):
                r = row[seg]
                if r and r.get("dd_mtm_method") == "估":
                    row["notes"].append(f"{'训练段' if seg == 'train' else '样本外'}的含浮亏回撤只是估算（找不到K线数据）")
            row["funding"] = funding_estimate(plan, row["test"])
            fe, te = row["funding"], row["test"]
            if fe and te and te["trades"] > 0 and fe["missing_share"] > 0:
                if te["profit_total"] > 0 and fe["profit_total_est"] <= 0:
                    row["notes"].append(
                        f"资金费：样本外 {fe['missing_share']:.0%} 的持仓时间没有资金费数据（按 0 算了）；按每 8 小时 0.01% 估算后，"
                        f"样本外收益 {_pct(te['profit_total'], True)} → {_pct(fe['profit_total_est'], True)}，赚的钱可能全被资金费吃掉"
                    )
                elif te["profit_total"] > 0 and fe["adj_pct"] < -0.25 * te["profit_total"]:
                    row["notes"].append(
                        f"资金费：按每 8 小时 0.01% 估算，没有资金费数据的那段持仓还要付约 {_pct(-fe['adj_pct'])}，"
                        f"样本外收益 {_pct(te['profit_total'], True)} → {_pct(fe['profit_total_est'], True)}"
                    )
            row["formal_run"] = formal_of(plan, strat)
            rows.append(row)
    # 低风险版 vs 原版：逐笔对比，核对"仓位减半"在这份数据（真实的合约张数 / 最小下单量）上是不是真的成立
    by_default = {r["strategy"]: r for r in rows if r["variant"] == "default"}
    for low_name, base_name in LOWRISK_OF.items():
        low, base = by_default.get(low_name), by_default.get(base_name)
        if not low or not base:
            continue
        low["lowrisk_vs"] = {"base": base_name, "segments": {}}
        for seg in ("train", "holdout", "test"):
            c = compare_lowrisk(low.get(seg), base.get(seg))
            if c is not None:
                low["lowrisk_vs"]["segments"][seg] = c
                low["notes"].append(_lowrisk_text(seg, c, base_name))
    rows.sort(key=lambda r: (VERDICT_ORDER[r["verdict"]], -((r["test"] or {}).get("profit_total") or -9.0)))
    for i, r in enumerate(rows, 1):
        r["rank"] = i
    return rows


# ----------------------------------------------------------------------------
# 输出
# ----------------------------------------------------------------------------
def _dropped_text(plan: dict[str, Any]) -> str:
    parts = [f"{n}（{_syntax_error_text(err)}）" for n, err in (plan.get("strategies_broken") or {}).items()]
    parts += [f"{n}（没找到这个策略）" for n in plan.get("strategies_missing") or []]
    return "；".join(parts)


def _epochs_text(ep: dict[str, Any], s: dict[str, Any]) -> str:
    want = int(s.get("epochs") or 0)
    stop_why = "到时间停止" if int(s.get("timeout") or 0) > 0 else "没跑满"

    def one(n: Any) -> str:
        if n is None:
            return "没调参"
        n = int(n)
        return f"{n} 轮" + (f"（{stop_why}）" if want and n < want else "")

    txt = f"训练段 {one(ep.get('main'))}"
    if ep.get("folds"):
        txt += "；走步各折 " + " / ".join(one(n) for n in ep["folds"])
    return txt


def _holdout_table(plan: dict[str, Any], rows: list[dict[str, Any]]) -> list[str]:
    """"没看过的一段"（第 3 轮唯一没被以前的研究看过的数据）：每个策略默认参数的结果，外加每年的平仓盈亏。"""
    ho_ = plan["holdout"]
    L = ["", f"没看过的一段（{ho_[0]} ~ {ho_[1]}，默认参数）：第 2 轮只用了 {plan.get('seen_from') or '?'} 以后的数据，"
         "第 3 轮的选择也没看过这段，是这次唯一的独立证据（训练段的一部分，不是额外的数据）："]
    years = sorted({y for r in rows if r["variant"] == "default" for y in _year_returns(r.get("holdout"))})
    headers = [("策略", False), ("收益", True), ("盈亏因子", True), ("回撤", True), ("交易", True)]
    headers += [(f"{y} 年", True) for y in years]
    headers += [("结论", False)]
    body = []
    max_dd = cd.settings_max_dd_pct(plan.get("settings")) / 100
    for r in rows:
        if r["variant"] != "default":
            continue
        ho = r.get("holdout")
        if not ho:
            body.append([r["strategy"], "-", "-", "-", "-"] + ["-"] * len(years) + ["没有结果"])
            continue
        yr = _year_returns(ho)
        ok = ho["trades"] > 0 and ho["profit_total"] > 0 and ho["dd_worst"] <= max_dd
        body.append([
            r["strategy"], _pct(ho["profit_total"], True), _num(ho["profit_factor"]), _pct(ho["dd_worst"]), str(ho["trades"]),
        ] + [_pct(yr.get(y), True) if y in yr else "-" for y in years] + [
            "赚钱、回撤达标" if ok else ("没有交易" if ho["trades"] == 0 else "亏损或回撤超标"),
        ])
    widths = [max(cd._width(h), *(cd._width(row[i]) for row in body)) if body else cd._width(h)
              for i, (h, _) in enumerate(headers)]
    sep = "+" + "+".join("-" * (w + 2) for w in widths) + "+"
    L += [sep, "| " + " | ".join(cd._pad(h, widths[i]) for i, (h, _) in enumerate(headers)) + " |", sep]
    for row in body:
        L.append("| " + " | ".join(cd._pad(c, widths[i], headers[i][1]) for i, c in enumerate(row)) + " |")
    L.append(sep)
    L.append("  （每年 = 那一年平仓的盈亏 ÷ 初始资金，不复利，只用来看哪一年赚、哪一年亏；收益、回撤是整段的。"
             "\"通过\"要求这一段也赚钱、回撤达标）")
    return L


def _text_table(headers: list[tuple[str, bool]], body: list[list[str]], indent: str = "") -> list[str]:
    widths = [max(cd._width(h), *(cd._width(row[i]) for row in body)) if body else cd._width(h)
              for i, (h, _) in enumerate(headers)]
    sep = indent + "+" + "+".join("-" * (w + 2) for w in widths) + "+"
    out = [sep, indent + "| " + " | ".join(cd._pad(h, widths[i]) for i, (h, _) in enumerate(headers)) + " |", sep]
    for row in body:
        out.append(indent + "| " + " | ".join(cd._pad(c, widths[i], headers[i][1]) for i, c in enumerate(row)) + " |")
    out.append(sep)
    return out


def _rel(path: str) -> str:
    try:
        return str(Path(path).resolve().relative_to(ROOT_DIR))
    except ValueError:
        return str(path)


def _default_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [r for r in rows if r["variant"] == "default"]


def _prereg_changed_line(prereg: dict[str, Any], plan: dict[str, Any] | None) -> list[str]:
    """运行时（scripts/check_prereg.py 写进 plan.json 的 "prereg"）记下的登记表 sha256 和现在读到的不一样 = 提醒。
    只提醒：报告按现在读到的登记表判。"""
    pr = (plan or {}).get("prereg")
    if not isinstance(pr, dict):
        return []
    if prereg.get("snapshot"):
        then = str(prereg.get("sha256") or "").lower()
        now = str(prereg.get("live_sha256") or "").lower()
        live = _rel(str(prereg.get("live_path") or ""))
        if prereg.get("live_error"):
            return [f"!! 现在的登记表读不出来（{prereg['live_error']}）；这次是正式运行，按 plan.json 里运行时的登记表快照判，不受影响。"]
        if then == now:
            return []
        t = f"{then[:12]}…" if then else "没有登记表"
        n = f"{now[:12]}…" if now else "没有登记表"
        return [f"!! 登记表在这次正式运行之后改过：运行时 {t}，现在 {live} 是 {n}。这次是正式运行，"
                "本报告按 plan.json 里运行时的登记表快照判，现在的登记表不用；正式运行以后的改动要写进登记表的 changes。"]
    then = str(pr.get("registry_sha256") or "").lower()
    now = str(prereg.get("sha256") or "").lower()
    if not then and not now:
        return []
    if then == now:
        return []
    t = f"{then[:12]}…" if then else "没有登记表"
    n = f"{now[:12]}…" if now else "没有登记表"
    return [f"!! 登记表在这次运行之后改过：运行时 {t}，现在 {n}。本报告按现在读到的登记表判；"
            "预注册要求规则在回测之前定下来，请先确认改动是不是在正式运行之前做的。"]


def _formal_lines(plan: dict[str, Any] | None) -> list[str]:
    """这次是不是正式的预注册运行（scripts/check_prereg.py 写进 plan.json 的 prereg）。plan.json 没有 prereg = 不写。"""
    pr = (plan or {}).get("prereg")
    if not isinstance(pr, dict):
        return []
    commit = str(((pr.get("git") or {}).get("commit")) or "")
    ctxt = f"git 提交 {commit[:10]}" if commit else "读不到 git 提交号"
    note = str(pr.get("formal_note") or "")
    if pr.get("formal_run") is True:
        return [f"正式运行：是（{ctxt}；{note}）"]
    L = []
    if pr.get("allow_unregistered") or pr.get("problems"):
        L.append("!! 不是正式的预注册运行"
                 + ("（用了 RESEARCH_ALLOW_UNREGISTERED=1）" if pr.get("allow_unregistered") else "")
                 + f"：{note or '登记检查有不一致'}（{ctxt}）。这份报告不能当第 5 轮的正式结论用。")
        for x in (pr.get("problems") or [])[:8]:
            L.append(f"   - {x}")
    else:
        L.append(f"正式运行：否（{ctxt}）：{note or 'plan.json 里没有记下原因'}")
        for x in [x for x in (pr.get("not_formal_reasons") or []) if x not in note][:8]:
            L.append(f"   - {x}")
    return L


def formal_of(plan: dict[str, Any] | None, strat: str) -> str:
    """CSV 的 formal_run 列：登记的策略 = 是 / 否；plan.json 没有 prereg 或者策略没登记 = 空。"""
    pr = (plan or {}).get("prereg")
    if not isinstance(pr, dict):
        return ""
    rec = (pr.get("strategies") or {}).get(strat)
    if not isinstance(rec, dict) or not rec.get("registered"):
        return ""
    return "是" if (pr.get("formal_run") is True and rec.get("formal") is True) else "否"


def _prereg_header(prereg: dict[str, Any] | None, rows: list[dict[str, Any]], s: dict[str, Any],
                   plan: dict[str, Any] | None = None) -> list[str]:
    """报告抬头：第 5 轮登记表（哪个文件、登记了哪些策略、各自的门槛和附加检查、待定的决定）。"""
    if prereg is None:
        return []
    path = _rel(prereg["path"])
    changed = _prereg_changed_line(prereg, plan)
    if not prereg.get("found"):
        return [f"预注册登记表：没有 {path}，全部策略按上面的默认规则判（没有第 5 轮的附加检查）"] + changed
    seen: dict[str, dict[str, Any]] = {}
    for r in rows:
        ru = r.get("rules") or {}
        if ru.get("registered") and r["strategy"] not in seen:
            seen[r["strategy"]] = ru
    sha = (prereg.get("sha256") or "")[:12]
    if not seen:
        return [f"预注册登记表：{path}（sha256 {sha}…）里没有这次研究的策略，全部按上面的默认规则判"] + changed
    min_trades = int(s.get("min_trades", 30))
    snap = "（运行时快照，plan.json 里记下的）" if prereg.get("snapshot") else ""
    L = _formal_lines(plan)
    L += [f"预注册登记表：{path}{snap}（sha256 {sha}…）。登记的策略按登记表判（预注册第 2 节\"算失败的情况\"和第 3.3 节）：",
         f"  - 每一段（训练段、没看过的一段、样本外、走步拼接）都要 盈亏因子 ≥ 门槛、交易 ≥ {min_trades} 笔"
         "（第 3.3 节原文；没登记的策略照旧只对样本外和走步查这两条）"]
    for name, ru in seen.items():
        where = ""
        if ru.get("venue"):
            where = f"（{ru['venue']}" + (f"，第 {ru['stage']} 阶段" if ru.get("stage") else "") + "）"
        parts = [f"盈亏因子门槛 {ru['min_pf']:g}" + ("（登记表）" if ru["min_pf_source"] == "登记表" else "（默认）")
                 + ("（!! 待定）" if ru["min_pf_pending"] else "")]
        parts += [_check_text(c) for c in ru["checks"]]
        if ru["errors"]:
            parts.append("!! 登记表写错（这一行判未通过）：" + "；".join(ru["errors"]))
        L.append(f"  - {name}{where}：" + "；".join(parts))
    for name, ru in seen.items():
        if ru["min_pf_pending"]:
            L.append(
                f"!! 待定（要在正式运行之前定下来，不能看完结果再改）：{name} 的盈亏因子门槛这次按 {ru['min_pf']:g} 判"
                + (f"（{ru['min_pf_note']}）" if ru["min_pf_note"] else "（市场中性的策略用 1.1，还是保持 1.2）")
            )
    return L + changed


def _usdt(v: float | None) -> str:
    return "-" if v is None else f"{v:+.2f}"


def _round5_tables(plan: dict[str, Any], rows: list[dict[str, Any]]) -> list[str]:
    """第 5 轮新增的几张表：资金费 / 每币每年交易 / 能交易的币；逐年表；2020 留出段；事件集中度（都在排名表后面）。"""
    s = plan.get("settings", {})
    max_dd = cd.settings_max_dd_pct(s) / 100
    min_trades = int(s.get("min_trades", 30))
    drows = [r for r in _default_rows(rows) if r.get("r5")]
    if not drows:
        return []
    L: list[str] = ["", "第 5 轮附加统计（默认参数；只报告，登记表里有对应检查的策略才参与判定）："]
    headers = [("策略", False), ("资金费 训练段", True), ("没看过的一段", True), ("样本外", True), ("走步", True),
               ("全期", True), ("资金费为 0 的交易", True), ("每币每年交易 训练段", True), ("没看过的一段", True),
               ("样本外", True), ("全期", True), ("能交易的币", True), ("有交易的币", True)]
    body = []
    for r in drows:
        f, t, pc = r["r5"]["funding"], r["r5"]["tpy"], r["r5"]["pairs"]

        def tv(seg: str, t: dict[str, Any] = t) -> str:
            v = (t.get(seg) or {}).get("value")
            return "-" if v is None else f"{v:.2f}"

        za = (f.get("all") or {}).get("zero_share")
        body.append([
            r["strategy"], _usdt((f.get("train") or {}).get("total")), _usdt((f.get("holdout") or {}).get("total")),
            _usdt((f.get("test") or {}).get("total")), _usdt((f.get("wf") or {}).get("total")),
            _usdt((f.get("all") or {}).get("total")), "-" if za is None else f"{za:.1%}",
            tv("train"), tv("holdout"), tv("test"), tv("all"), f"{pc['n']}/{pc['pairs']}", str(pc["traded"]),
        ])
    L.extend(_text_table(headers, body))
    L.append("  （资金费 = 这一段所有交易的 funding_fees 合计，USDT，正数 = 收到、负数 = 付出；每段回测都从 1000 USDT 开始，"
             "走步 = 各折直接相加；全期 = 训练段 + 样本外。")
    if str(plan.get("exchange") or "okx").lower() == "okx":
        L.append("   OKX 只有最近约 3 个月的资金费率，更早的持仓 funding_fees 是 0，所以 OKX 的\"资金费为 0\"占比一般很高。")
    L.append("   每币每年交易 = 交易数 ÷ 能交易的币在这一段里能交易的时间合计（年；第一根K线 + 预热之后才算）；"
             "交易数按登记表的口径（例如不数 hedge 对冲腿）。")
    L.append("   能交易的币 = 数据检查后保留的币，再去掉策略日志里标记\"没有数据 / 数据条件从没满足\"的币（PremiumFade 的现货数据）；"
             "有交易的币 = 训练段或样本外至少有一笔交易的币）")

    # ---- 逐年表 ----
    yrows = [r for r in drows if (r["r5"].get("years") or {}).get("years")]
    yerr = [f"{r['strategy']}（{r['r5']['years']['error']}）" for r in drows if (r["r5"].get("years") or {}).get("error")]
    if yerr:
        L.append("")
        L.append("!! 逐年表出错（只影响这张表，不影响判定）：" + "；".join(yerr))
    if yrows:
        ys = yrows[0]["r5"]["years"]["years"]
        L.append("")
        L.append("逐年表（默认参数；训练段 + 样本外按时间顺序复利拼成一条资金曲线；每格 = 这一年的 收益 / 回撤；只报告，判定仍按段）：")
        headers = [("策略", False)] + [
            ((f"{y['year']} 年" if y["full"] else f"{y['year']} 年（{y['start'][5:]}~{y['end'][5:]}）"), True) for y in ys
        ] + [("回撤算法", False)]
        body = []
        for r in yrows:
            cy = r["r5"]["years"]
            by = {y["year"]: y for y in cy["years"]}
            cells = []
            for y in ys:
                v = by.get(y["year"])
                cells.append("-" if v is None or v["return"] is None else f"{_pct(v['return'], True)} / {_pct(v['dd_worst'])}")
            body.append([r["strategy"]] + cells + [cy["method"] or "-"])
        L.extend(_text_table(headers, body))
        L.append("  （收益 = 年底权益 ÷ 年初权益 - 1（权益含浮亏）；回撤 = 这一年里账户权益从最高点到最低点的跌幅，峰值从年初权益起算，"
                 "逐根K线、含浮亏，和只看平仓的取大的——和 check_drawdown.py 同一口径（scripts/nfi_yearly_report.py 也是这样判）。")
        L.append(f"   样本外的交易按训练段结束时的权益缩放后接上；训练段结束时还开着的仓位被回测平掉，所以 {plan['test'][0][:4]} 年"
                 "不是一次连续回测。回撤超过上限的年份只是提醒，不改变结论）")

    # ---- 2020 留出段 ----
    phrows = [r for r in drows if r["r5"].get("pre_holdout") is not None]
    ph_plan = plan.get("pre_holdout")
    if phrows or ph_plan:
        win = f"{ph_plan[0]} ~ {ph_plan[1]}" if isinstance(ph_plan, (list, tuple)) and len(ph_plan) == 2 else "2020 年"
        L.append("")
        L.append(f"2020 留出段（{win}；从来没用过，主运行之后每个策略只跑一次，更早的数据只用来预热；回撤口径和 check_drawdown.py 相同）：")
        headers = [("策略", False), ("区间", False), ("交易", True), ("收益", True), ("盈亏因子", True), ("回撤", True), ("结论", False)]
        body = []
        for r in drows:
            ph = r["r5"].get("pre_holdout")
            if ph is None:
                body.append([r["strategy"], "-", "-", "-", "-", "-", "没有跑"])
                continue
            x = ph.get("result")
            if x is None:
                body.append([r["strategy"], ph.get("timerange") or "-", "-", "-", "-", "-", "回测失败或没有结果"])
                continue
            n = int(x.get("trades_counted", x["trades"]))
            verdict = {"PASS": "赚钱、回撤达标", "FAIL": "未通过（亏损或回撤超标）",
                       "REPORT": f"不足 {min_trades} 笔：只报告"}.get(ph.get("verdict"), "-")
            body.append([r["strategy"], f"{x['start']}~{x['end']}", str(n) + ("" if n == x["trades"] else f"（共 {x['trades']}）"),
                         _pct(x["profit_total"], True), _num(x["profit_factor"]), _pct(x["dd_worst"]), verdict])
        L.extend(_text_table(headers, body))
        L.append(f"  （交易 ≥ {min_trades} 笔时必须赚钱、回撤 ≤ {max_dd * 100:g}%，否则算失败；不足 {min_trades} 笔只报告，"
                 "不算通过也不算失败）")

    # ---- 事件集中度 ----
    ev_body = []
    for r in drows:
        reg = {}
        for c in (r.get("rules") or {}).get("checks") or []:
            if c["type"] == "drop_top_days":
                for sg in c["segments"]:
                    reg[sg] = c["n"]
        for seg, ev in r["r5"]["event"].items():
            days = " ".join(f"{d['day']}({d['profit']:+.1f})" for d in ev["days"]) or "没有盈利的日子"
            verdict = "只报告"
            if seg in reg:
                verdict = "通过" if ev["profit_after"] > 0 else "未通过"
            ev_body.append([r["strategy"], SEG_CN5.get(seg, seg), _pct(ev["profit_before"], True), str(ev["trades"]),
                            days, "-" if ev["share"] is None else f"{ev['share']:.0%}",
                            _pct(ev["profit_after"], True), _num(ev["pf_after"]), verdict])
    if ev_body:
        L.append("")
        L.append(f"事件集中度（默认参数；按入场日期（UTC）分组，去掉盈利最多的几天的全部交易后重算这一段的收益；"
                 f"没登记的策略按 {EVENT_TOP_DAYS_DEFAULT} 天只报告）：")
        headers = [("策略", False), ("区间", False), ("收益", True), ("交易", True), ("盈利最多的几天（入场日 盈亏 USDT）", False),
                   ("占这一段盈利", True), ("去掉后收益", True), ("去掉后PF", True), ("结论", False)]
        L.extend(_text_table(headers, ev_body))
        L.append("  （近似：其余交易的盈亏不变，没有按去掉以后的资金重新算仓位；只去盈利为正的日子。"
                 "登记了 drop_top_days 的策略：去掉以后不赚钱 = 未通过）")
    return L


def render(plan: dict[str, Any], rows: list[dict[str, Any]], stage: str, prereg: dict[str, Any] | None = None) -> str:
    s = plan.get("settings", {})
    max_dd = cd.settings_max_dd_pct(s)
    L: list[str] = []
    title = "第 2 步策略研究结果"
    if stage == "interim":
        title += "（默认参数阶段：调参还没跑完，最终结果以运行结束时的表为准）"
    elif stage == "interrupted":
        title += "（运行被中断：只汇总了已经跑完的部分）"
    L.append(f"================ {title} ================")
    if plan.get("synthetic"):
        L.append("!! 合成数据（随机行情）：下面的收益、回撤数字没有任何意义，只用来检查流程能不能跑通 !!")
    L.append(f"运行目录：{plan['_run_dir']}")
    fee = s.get("fee") or "交易所默认"
    fee_txt = f"{float(fee) * 100:.3g}%/边" if fee != "交易所默认" else fee
    L.append(
        f"配置：{plan['config']}    交易对：{len(plan['pairs'])} 个（配置里 {plan['pairs_config_count']} 个，"
        f"去掉 {len(plan['pairs_dropped'])} 个历史不够的）    手续费+滑点：{fee_txt}    初始资金：1000 USDT"
    )
    dr, tr_, te_ = plan["data_range"], plan["train"], plan["test"]
    ho_ = plan.get("holdout")
    L.append(f"数据区间：{dr[0]} ~ {dr[1]}    训练段：{tr_[0]} ~ {tr_[1]}    样本外：{te_[0]} ~ {te_[1]}（调参时没用过这段）")
    for line in plan.get("seen_warning") or []:
        L.append("          " + line)
    if ho_:
        L.append(f"没看过的一段：{ho_[0]} ~ {ho_[1]}（训练段的前一部分，默认参数单独回测；下面排名表后面单独一张表）")
    wf = plan["wf"]
    late = [f["k"] for f in wf["folds"] if f["train"][1] >= te_[0]]
    if late and s.get("hyperopt") in (1, "1", True):
        L.append(
            f"          注意：走步第 {'、'.join(str(k) for k in late)} 折会用样本外这段时间的数据重新调参，"
            "所以\"优化参数\"那一行的样本外和走步结果不是两份独立证据（默认参数那一行不受影响）"
        )
    if wf["folds"]:
        if s.get("hyperopt") in (1, "1", True):
            how = "扩展窗口" if wf["mode"] == "anchored" else f"滚动：每折先用前 {wf['train_days']} 天调参"
            how += f"，再测后面 {wf['test_days']} 天"
        else:
            how = f"每折测 {wf['test_days']} 天，不调参、直接用默认参数"
        L.append(
            f"走步验证：{len(wf['folds'])} 折（{how}），"
            f"覆盖 {wf['folds'][0]['test'][0]} ~ {wf['folds'][-1]['test'][1]}"
        )
    else:
        L.append(f"走步验证：没有做{('（' + wf['note'] + '）') if wf.get('note') else ''}")
    if s.get("hyperopt") in (1, "1", True):
        secs = int(s.get("timeout") or 0)
        jobs_txt = "全部 CPU 核" if str(s.get("jobs") or "-1") == "-1" else f"{s.get('jobs')} 个进程"
        limit = "不限时" if secs <= 0 else (f"最多 {secs} 秒" if secs < 120 else f"最多 {secs // 60} 分钟")
        L.append(
            f"调参：hyperopt 只用训练窗口，每次 {s.get('epochs')} 轮（{limit}，到时间就用已跑完的轮数），目标 {s.get('loss')}，"
            f"随机种子 {s.get('seed')}（走步第 k 折用 {s.get('seed')}+k），并行 {jobs_txt}；"
            "只调入场/离场参数（buy/sell）"
        )
        L.append(
            "      轮数跑满、并行进程数相同时结果可以复现；到时间停止的那次跑了几轮取决于机器快慢，每行说明里有实际轮数"
        )
    else:
        L.append(f"调参：没有做（{s.get('hyperopt_reason') or 'RESEARCH_HYPEROPT=0'}），只有默认参数的结果")
    if plan.get("warmup_candles"):
        L.append(
            f"指标预热：每段回测开始前最多多加载 {plan['warmup_candles']} 根K线（数据开头那段只能用策略自己的预热长度），"
            "EMA200 等指标基本不受切分点影响"
        )
    dropped = _dropped_text(plan)
    if dropped:
        L.append(f"!! 要求研究、但表里没有的策略：{dropped}")
    firsts = [v for v in funding_first_dates(plan).values() if v is not None] if plan.get("trading_mode") == "futures" else []
    if plan.get("trading_mode") == "futures":
        ff = datetime.fromtimestamp(min(firsts), tz=timezone.utc).date().isoformat() if firsts else None
        if str(plan.get("exchange") or "okx").lower() == "okx":
            L.append(
                f"资金费：OKX 只能下载最近约 3 个月的资金费率（这次数据里最早的一条在 {ff or '（没有资金费数据）'}），"
                "在那之前持仓的资金费按 0 算，拿得久的趋势策略会显得偏好；"
            )
        else:  # 币安等：资金费历史从下载开始日期起都有（第 5 轮 XSFundingCarry）
            L.append(
                f"资金费：{plan.get('exchange')} 的资金费率数据里最早的一条在 {ff or '（没有资金费数据）'}，"
                "在那之前持仓的资金费按 0 算（之后的按真实费率算进收益）；"
            )
        L.append(
            "      \"样本外扣资金费\"列按每 8 小时 0.01%（做多付、做空收）估算了这一部分，只用来提醒、不参与\"通过\"判定。"
            "趋势策略样本外只是小赚的，不能算已经证明能赚钱"
        )
    override = cd.max_dd_override_note(s)
    if override:
        L.append(override)
    L.append(
        f"回撤上限：{max_dd:g}% {cd.max_dd_rule_note(max_dd)}。回撤 = 账户权益（已平仓余额 + 持仓浮动盈亏）"
        "从最高点到最低点的跌幅，逐根K线；组合检查看的是所有子账户加起来的总权益"
    )
    L.append(
        f"通过标准（样本外和走步拼接结果都要满足）：账户权益回撤（含浮亏）≤ {max_dd:g}%、总收益 > 0、"
        f"盈亏因子 ≥ {float(s.get('min_pf', 1.2)):g}、交易 ≥ {int(s.get('min_trades', 30))} 笔、杠杆 ≤ {float(s.get('max_leverage', 5)):g}x；"
    )
    L.append(
        f"                                          训练段也要赚钱、回撤 ≤ {max_dd:g}%；"
        f"样本外盈亏因子不低于训练段的 {float(s.get('pf_ratio', 0.6)):g} 倍"
        + (f"；没看过的一段也要赚钱、回撤 ≤ {max_dd:g}%（只看默认参数）" if ho_ else "")
    )
    L.extend(_prereg_header(prereg, rows, s, plan))
    L.append("")

    headers = [
        ("排名", True), ("结论", False), ("策略", False), ("周期", False), ("参数", False),
        ("训练收益", True), ("训练PF", True), ("训练回撤", True),
        ("样本外收益", True), ("样本外扣资金费", True), ("样本外PF", True), ("样本外回撤", True), ("样本外交易", True),
        ("走步收益", True), ("走步回撤", True), ("主要原因", False),
    ]
    body = []
    for r in rows:
        tr, te, w = r["train"] or {}, r["test"] or {}, r["wf"] or {}
        fe = r.get("funding") or {}
        verdict = VERDICT_CN[r["verdict"]]
        main_reasons = (r.get("invalid_reasons") or []) + (r.get("untestable_reasons") or []) + r["reasons"]
        body.append([
            str(r["rank"]), verdict, r["strategy"], r["timeframe"], VARIANT_CN[r["variant"]],
            _pct(tr.get("profit_total"), True), _num(tr.get("profit_factor")), _pct(tr.get("dd_worst")),
            _pct(te.get("profit_total"), True), _pct(fe.get("profit_total_est"), True) if fe else "-",
            _num(te.get("profit_factor")), _pct(te.get("dd_worst")),
            str(te["trades"]) if te else "-",
            _pct(w.get("profit_total"), True) if w else "-", _pct(w.get("dd_worst")) if w else "-",
            main_reasons[0] if main_reasons else "全部达标",
        ])
    widths = [
        max(cd._width(h), *(cd._width(row[i]) for row in body)) if body else cd._width(h)
        for i, (h, _) in enumerate(headers)
    ]
    sep = "+" + "+".join("-" * (w + 2) for w in widths) + "+"
    L.append(sep)
    L.append("| " + " | ".join(cd._pad(h, widths[i]) for i, (h, _) in enumerate(headers)) + " |")
    L.append(sep)
    for row in body:
        L.append("| " + " | ".join(cd._pad(c, widths[i], headers[i][1]) for i, c in enumerate(row)) + " |")
    L.append(sep)

    if ho_:
        L.extend(_holdout_table(plan, rows))
    L.extend(_round5_tables(plan, rows))

    L.append("")
    L.append("说明（每一行的细节）：")
    for r in rows:
        L.append(f"- [{r['rank']}] {r['strategy']}（{r['timeframe']}，{VARIANT_CN[r['variant']]}）")
        tr, te, w = r["train"], r["test"], r["wf"]
        if tr or te:
            seg = []
            if tr:
                seg.append(f"训练 {tr['start']}~{tr['end']}（{tr['trades']} 笔）")
            ho = r.get("holdout")
            if ho:
                seg.append(f"没看过的一段 {ho['start']}~{ho['end']}（{ho['trades']} 笔，收益 {_pct(ho['profit_total'], True)}，"
                           f"盈亏因子 {_num(ho['profit_factor'])}，回撤 {_pct(ho['dd_worst'])}）")
            if te:
                seg.append(f"样本外 {te['start']}~{te['end']}（{te['trades']} 笔，胜率 {_pct(te.get('winrate'))}，"
                           f"回撤 平仓 {_pct(te['dd_closed'])} / 含浮亏 {_pct(te['dd_mtm'])} {te['dd_mtm_method']}）")
            L.append("    区间：" + "；".join(seg))
        if w:
            fr = " / ".join("没结果" if x is None else _pct(x, True) for x in w["fold_returns"])
            good = sum(1 for x in w["fold_returns"] if x is not None and x > 0)
            L.append(
                f"    走步：每折收益 {fr}（{good}/{len(w['fold_returns'])} 折赚钱），拼接后 {w['trades']} 笔，"
                f"盈亏因子 {_num(w['profit_factor'])}，回撤 {_pct(w['dd_worst'])} {w['dd_mtm_method']}"
            )
        fe = r.get("funding")
        if fe and te and te["trades"] > 0 and fe["missing_share"] > 0:
            L.append(
                f"    资金费估算：样本外 {fe['missing_share']:.0%} 的持仓时间没有资金费数据（按 0 算了），"
                f"按每 8 小时 0.01% 补算约 {fe['adj_abs']:+.2f} USDT（{_pct(fe['adj_pct'], True)}）"
            )
        if r["variant"] == "opt":
            L.append(f"    训练段调出来的参数：{_fmt_params(r['params'])}")
            ep = r.get("ho_epochs") or {}
            L.append(f"    调参实际跑的轮数（每次设定 {s.get('epochs')} 轮）：{_epochs_text(ep, s)}")
            fps = [p for p in r["fold_params"] if p]
            if fps:
                keys = sorted({k for p in fps for k in p})
                spread = "；".join(
                    f"{k} " + "/".join(str(p.get(k, '-')) if p else "默认" for p in r["fold_params"]) for k in keys
                )
                L.append(f"    走步各折参数（越稳定越好，跳来跳去说明是在拟合噪音）：{spread}")
        ru = r.get("rules") or {}
        if ru.get("registered"):
            L.append(f"    盈亏因子门槛：{ru['min_pf']:g}（{ru['min_pf_source']}）" + ("  !! 待定，正式运行前要定下来" if ru["min_pf_pending"] else ""))
        for c in r.get("checks") or []:
            res = {"PASS": "通过", "FAIL": "未通过", "INVALID": "无效", "UNTESTABLE": "测不了", "-": "（不判）"}.get(c["result"], c["result"])
            L.append(f"    附加检查（登记表）：{c['text']} → {res}" + (f"：{c['detail']}" if c["detail"] else ""))
        for x in r.get("invalid_reasons") or []:
            L.append(f"    无效：{x}")
        for x in r.get("untestable_reasons") or []:
            L.append(f"    测不了：{x}")
        for x in r["reasons"]:
            L.append(f"    未通过：{x}")
        for x in dict.fromkeys(r["notes"]):
            L.append(f"    提醒：{x}")

    # ---- 组合检查 ----
    L.append("")
    best: dict[str, dict[str, Any]] = {}
    for r in rows:
        if r["verdict"] == "PASS" and r["test"] is not None:
            cur = best.get(r["strategy"])
            if cur is None or (cur["variant"] == "opt" and r["variant"] == "default"):
                best[r["strategy"]] = r  # 两种都通过时优先用默认参数（自由度更少）
    combo = overlay(plan, [r["test"] for r in best.values()])
    if combo:
        names = " + ".join(f"{r['strategy']}（{VARIANT_CN[r['variant']]}）" for r in best.values())
        ok = combo["dd_worst"] * 100 <= max_dd and combo["profit_total"] > 0
        L.append(f"组合检查（把通过的 {len(best)} 个策略放进同一个 1000 USDT 账户同时跑，样本外区间）：")
        L.append(f"    {names}")
        L.append(
            f"    合计收益 {_pct(combo['profit_total'], True)}，盈亏因子 {_num(combo['profit_factor'])}，"
            f"{combo['trades']} 笔，回撤 平仓 {_pct(combo['dd_closed'])} / 含浮亏 {_pct(combo['dd_mtm'])} "
            f"{combo['dd_mtm_method']} → {'回撤达标' if ok else '组合后回撤超标或不赚钱，不能这样一起上'}"
        )
        L.append("    （近似算法：各策略按自己的回测交易直接叠加；没考虑两个策略同时开仓时保证金是否够用）")
    elif best:
        L.append("组合检查：只有 1 个策略通过，不需要组合检查。")
    else:
        L.append("组合检查（同一个账户叠加）：没有策略通过，没有做；下面的子账户组合检查不要求单个策略先通过。")

    # ---- 子账户组合检查（第 3 轮）：每个策略一个子账户、资金分开，看合起来的总权益 ----
    # 算法、挑选规则、输出（portfolio.txt/csv/json）全在 scripts/research_portfolio.py；出错只影响这一节
    L.append("")
    L.extend(research_portfolio.report_lines(plan, rows))

    passed = sum(1 for r in rows if r["verdict"] == "PASS")
    L.append("")
    L.append(f"合计：{len(rows)} 行（{len(plan['strategies_order'])} 个策略），通过 {passed} 行。")
    n_inv = sum(1 for r in rows if r["verdict"] == "INVALID")
    n_unt = sum(1 for r in rows if r["verdict"] == "UNTESTABLE")
    if n_inv or n_unt:
        L.append(f"      另有 无效 {n_inv} 行（资金费没记进去，先修数据）、测不了 {n_unt} 行（能交易的币不够）：都不算通过。")
    if dropped:
        L.append(f"!! 还有要求研究、但没有跑的策略：{dropped}")
    L.append("注：")
    L.append("  - 回撤 = 账户权益（余额 + 持仓浮动盈亏）从最高点到最低点的跌幅，逐根K线计算，取\"平仓\"和\"含浮亏\"两种口径中较大的，")
    L.append("    和 make check 口径相同。PF = 盈亏因子 = 总盈利 ÷ 总亏损。")
    L.append("  - 走步收益 = 各折测试结果按时间顺序复利拼接；走步回撤 = 拼接后整条资金曲线的最大回撤。")
    if any(r["variant"] == "opt" for r in rows):
        L.append("    优化参数那一行的走步：每一折都只用这一折自己的训练窗口重新调参，再去测后面没见过的那段。")
    else:
        L.append("    这次没有调参：走步各折和训练段/样本外是同一段行情切成小段，看的是\"是不是每段都赚钱\"，不是额外的独立证据。")
    L.append("  - 样本外扣资金费 = 样本外收益 + 按每 8 小时 0.01% 估算的、没有资金费数据那段持仓的资金费（做多付、做空收；只是估算，不参与判定）。")
    if plan.get("seen_warning"):
        L.append("  - 样本外这段以前的研究已经看过（见开头），第 3 轮的选择是看着它定的：样本外\"通过\"不等于有了独立证据，"
                 + ("以\"没看过的一段\"为准。" if ho_ else "这次没有没看过的数据。"))
    L.append("  - 交易对是站在今天挑的\"还活着的大币\"，有幸存者偏差，结果偏乐观；不要为了让表格变好看反复改参数、重跑。")
    if plan.get("synthetic"):
        L.append("  - 再说一次：这是合成数据，数字没有意义。")
    return "\n".join(L)


CSV_FIELDS = [
    "rank", "verdict", "strategy", "timeframe", "variant",
    "train_start", "train_end", "train_trades", "train_profit", "train_pf", "train_dd",
    "test_start", "test_end", "test_trades", "test_winrate", "test_profit", "test_profit_funding_est", "test_pf",
    "test_dd_closed", "test_dd_mtm", "test_dd", "test_leverage_max",
    "holdout_start", "holdout_end", "holdout_trades", "holdout_profit", "holdout_pf", "holdout_dd",
    "wf_trades", "wf_profit", "wf_pf", "wf_dd", "wf_fold_returns",
    "params", "reasons", "notes",
    # 第 5 轮（加在最后，前面的列不变）
    "min_pf_used", "min_pf_source", "min_pf_pending", "trades_counted_test",
    "funding_train", "funding_holdout", "funding_test", "funding_wf", "funding_all", "funding_zero_share_all",
    "tpy_train", "tpy_holdout", "tpy_test", "tpy_all", "pairs_tradable", "pairs_traded",
    "pre_holdout_start", "pre_holdout_end", "pre_holdout_trades", "pre_holdout_profit", "pre_holdout_pf",
    "pre_holdout_dd", "pre_holdout_verdict",
    "event_holdout_after", "event_test_after", "years", "extra_checks", "invalid_reasons", "untestable_reasons",
    "formal_run",
]


def _csv_row(r: dict[str, Any]) -> dict[str, Any]:
    tr, te, w = r["train"] or {}, r["test"] or {}, r["wf"] or {}
    ho = r.get("holdout") or {}

    def f(v: Any) -> Any:
        return "inf" if isinstance(v, float) and math.isinf(v) else v

    return {
        "rank": r["rank"], "verdict": r["verdict"], "strategy": r["strategy"], "timeframe": r["timeframe"],
        "variant": r["variant"],
        "train_start": tr.get("start"), "train_end": tr.get("end"), "train_trades": tr.get("trades"),
        "train_profit": tr.get("profit_total"), "train_pf": f(tr.get("profit_factor")), "train_dd": tr.get("dd_worst"),
        "test_start": te.get("start"), "test_end": te.get("end"), "test_trades": te.get("trades"),
        "test_winrate": te.get("winrate"), "test_profit": te.get("profit_total"),
        "test_profit_funding_est": (r.get("funding") or {}).get("profit_total_est"), "test_pf": f(te.get("profit_factor")),
        "test_dd_closed": te.get("dd_closed"), "test_dd_mtm": te.get("dd_mtm"), "test_dd": te.get("dd_worst"),
        "test_leverage_max": te.get("leverage_max"),
        "holdout_start": ho.get("start"), "holdout_end": ho.get("end"), "holdout_trades": ho.get("trades"),
        "holdout_profit": ho.get("profit_total"), "holdout_pf": f(ho.get("profit_factor")), "holdout_dd": ho.get("dd_worst"),
        "wf_trades": w.get("trades"), "wf_profit": w.get("profit_total"), "wf_pf": f(w.get("profit_factor")),
        "wf_dd": w.get("dd_worst"),
        "wf_fold_returns": " ".join("" if x is None else f"{x:.4f}" for x in w.get("fold_returns") or []),
        "params": json.dumps(r["params"], ensure_ascii=False) if r["params"] else "",
        "reasons": "；".join(r["reasons"]), "notes": "；".join(dict.fromkeys(r["notes"])),
        **_csv_round5(r),
    }


def _csv_round5(r: dict[str, Any]) -> dict[str, Any]:
    r5 = r.get("r5") or {}
    ru = r.get("rules") or {}
    fu, tp, ev = r5.get("funding") or {}, r5.get("tpy") or {}, r5.get("event") or {}
    ph = r5.get("pre_holdout") or {}
    phr = ph.get("result") or {}
    te = r.get("test") or {}
    years = (r5.get("years") or {}).get("years") or []

    def g(d: dict[str, Any], seg: str, key: str) -> Any:
        return (d.get(seg) or {}).get(key)

    return {
        "min_pf_used": ru.get("min_pf"), "min_pf_source": ru.get("min_pf_source"),
        "min_pf_pending": ru.get("min_pf_pending"),
        "trades_counted_test": te.get("trades_counted", te.get("trades")),
        "funding_train": g(fu, "train", "total"), "funding_holdout": g(fu, "holdout", "total"),
        "funding_test": g(fu, "test", "total"), "funding_wf": g(fu, "wf", "total"), "funding_all": g(fu, "all", "total"),
        "funding_zero_share_all": g(fu, "all", "zero_share"),
        "tpy_train": g(tp, "train", "value"), "tpy_holdout": g(tp, "holdout", "value"), "tpy_test": g(tp, "test", "value"),
        "tpy_all": g(tp, "all", "value"),
        "pairs_tradable": (r5.get("pairs") or {}).get("n"), "pairs_traded": (r5.get("pairs") or {}).get("traded"),
        "pre_holdout_start": phr.get("start"), "pre_holdout_end": phr.get("end"),
        "pre_holdout_trades": phr.get("trades_counted", phr.get("trades")), "pre_holdout_profit": phr.get("profit_total"),
        "pre_holdout_pf": "inf" if isinstance(phr.get("profit_factor"), float) and math.isinf(phr["profit_factor"])
        else phr.get("profit_factor"),
        "pre_holdout_dd": phr.get("dd_worst"), "pre_holdout_verdict": ph.get("verdict"),
        "event_holdout_after": g(ev, "holdout", "profit_after"), "event_test_after": g(ev, "test", "profit_after"),
        "years": json.dumps({str(y["year"]): [y["return"], y["dd_worst"]] for y in years}) if years else "",
        "extra_checks": json.dumps([{k: c[k] for k in ("type", "result")} for c in r.get("checks") or []],
                                   ensure_ascii=False) if r.get("checks") else "",
        "invalid_reasons": "；".join(r.get("invalid_reasons") or []),
        "untestable_reasons": "；".join(r.get("untestable_reasons") or []),
        "formal_run": r.get("formal_run", ""),
    }


def _strip(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {k: _strip(v) for k, v in obj.items() if not str(k).startswith("_")}
    if isinstance(obj, list):
        return [_strip(v) for v in obj]
    if isinstance(obj, float) and (math.isinf(obj) or math.isnan(obj)):
        return str(obj)
    return obj


def cmd_report(a: argparse.Namespace) -> int:
    run_dir = Path(a.run_dir)
    plan = load_plan(run_dir)
    plan["_run_dir"] = str(run_dir)
    cd.override_settings_max_dd(plan.setdefault("settings", {}), a.max_dd)
    jobs = load_jobs(run_dir)
    if not any(j["status"] == "ok" for j in jobs):
        print("没有任何成功的回测结果，无法生成报告（看 logs/ 目录里的日志）。")
        return 3
    prereg = load_prereg(a.prereg or None)
    if prereg["error"] and prereg_snapshot(plan) is None:
        print(f"错误：{prereg['error']}。登记表坏了，第 5 轮策略的门槛和附加检查会悄悄不做，所以不生成报告；"
              f"修好以后重新生成（不用重跑回测）：.venv/bin/python scripts/research_report.py report --run-dir {run_dir}",
              file=sys.stderr)
        return 2
    prereg = prereg_for_plan(plan, prereg)
    rows = build_rows(plan, jobs, load_notes(run_dir), prereg)
    text = render(plan, rows, a.stage, prereg)
    print(text)
    (run_dir / "summary.txt").write_text(text + "\n", encoding="utf-8")
    with (run_dir / "summary.csv").open("w", newline="", encoding="utf-8-sig") as fh:  # utf-8-sig：Excel 认中文
        w = csv.DictWriter(fh, fieldnames=CSV_FIELDS)
        w.writeheader()
        for r in rows:
            w.writerow(_csv_row(r))
    _write_json(run_dir / "summary.json", {
        "stage": a.stage, "plan": _strip(plan), "rows": _strip(rows),
        "preregistration": {
            **{k: prereg[k] for k in ("path", "sha256", "found")},
            "snapshot": bool(prereg.get("snapshot")), "live_sha256": prereg.get("live_sha256"),
            **{k: ((plan.get("prereg") or {}).get(k)) for k in ("formal_run", "formal_note", "not_formal_reasons",
                                                                 "problems", "allow_unregistered")},
            "commit": (((plan.get("prereg") or {}).get("git")) or {}).get("commit"),
            "formal_by_strategy": {r["strategy"]: r.get("formal_run", "") for r in _default_rows(rows)},
        },
    })
    print()
    print(f"报告已保存：{run_dir / 'summary.txt'}（表格）  {run_dir / 'summary.csv'}（Excel 可打开）")
    return 0


# ----------------------------------------------------------------------------
# gate / best-params / hyperopt
# ----------------------------------------------------------------------------
def cmd_gate(a: argparse.Namespace) -> int:
    """默认参数在这个训练窗口值不值得调参。

    训练段（--segment train）：交易数够、而且赚钱才调参。
    走步第 k 折的训练窗口（--segment wf<k>train --trades-only）：只看交易数够不够 hyperopt 的 --min-trades，
    不够的话每一轮都会被判成"交易太少"，调参只会白白跑满时间、或者挑出"交易最多"的参数。
    """
    run_dir = Path(a.run_dir)
    plan = load_plan(run_dir)
    where = _seg_label(a.segment)
    r = eval_job(plan, load_jobs(run_dir), a.strategy, "default", a.segment)
    if r is None:
        print(f"默认参数在{where}的回测失败，跳过调参")
        return 1
    if r["trades"] < a.min_trades:
        print(f"默认参数在{where}只有 {r['trades']} 笔交易（少于调参要求的 {a.min_trades} 笔），调参没有意义，跳过")
        return 1
    if a.trades_only:
        print(f"默认参数在{where}有 {r['trades']} 笔交易，可以调参")
        return 0
    if r["profit_total"] <= 0:
        print(f"默认参数在{where}亏损 {_pct(r['profit_total'], True)}，按研究纪律不靠调参去\"救\"它，跳过调参")
        return 1
    print(f"默认参数{where}收益 {_pct(r['profit_total'], True)}、{r['trades']} 笔、盈亏因子 {_num(r['profit_factor'])}，可以调参")
    return 0


def _read_fthypt(path: Path) -> list[dict[str, Any]]:
    epochs = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if isinstance(e, dict) and "loss" in e:
            epochs.append(e)
    return epochs


def cmd_best_params(a: argparse.Namespace) -> int:
    path = Path(a.fthypt)
    epochs = _read_fthypt(path) if path.is_file() else []
    if not epochs:
        print("hyperopt 一轮都没跑完，没有参数")
        return 1

    def loss(e: dict[str, Any]) -> float:
        try:
            v = float(e.get("loss"))
        except (TypeError, ValueError):
            return math.inf
        return v if math.isfinite(v) else math.inf

    valid = [e for e in epochs if loss(e) < MAX_LOSS - 1]
    if not valid:
        print(f"跑了 {len(epochs)} 轮，但没有一轮的交易数达到要求（--min-trades），没有可用的参数")
        return 1
    bests = [e for e in valid if e.get("is_best")]
    best = bests[-1] if bests else min(valid, key=loss)
    details = best.get("params_details") or {}
    params = {sp: details[sp] for sp in ("buy", "sell") if details.get(sp)}
    if not params:
        print("最优结果里没有 buy / sell 参数（策略没有可调参数？）")
        return 1
    rm = best.get("results_metrics") or {}
    content = {
        "strategy_name": a.strategy,
        "params": params,
        "ft_stratparam_v": 1,
        "export_time": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "research": {
            "label": a.label,
            "fthypt": str(path),
            "epoch": best.get("current_epoch"),
            "epochs_evaluated": len(epochs),
            "loss": loss(best),
            "train_window": [rm.get("backtest_start"), rm.get("backtest_end")],
            "train_trades": rm.get("total_trades"),
            "train_profit_total": rm.get("profit_total"),
            "train_profit_factor": rm.get("profit_factor"),
            "train_max_drawdown_account": rm.get("max_drawdown_account"),
        },
    }
    for target in [a.out, a.copy]:
        if target:
            _write_json(Path(target), content)
    flat = {k: v for sp in params.values() for k, v in sp.items()}
    print(
        f"最优参数（第 {best.get('current_epoch')} 轮，共跑了 {len(epochs)} 轮）：{_fmt_params(flat)}；"
        f"训练窗口 收益 {_pct(rm.get('profit_total'), True)}、{rm.get('total_trades')} 笔、"
        f"盈亏因子 {_num(rm.get('profit_factor'))}"
    )
    return 0


def _patch_pickle_magic() -> None:
    """绕过 freqtrade 2026.10 hyperopt 多进程（-j > 1）的一个 bug。

    freqtrade 的 HyperOptimizer.hyperopt_pickle_magic() 会把策略所有父类所在的模块登记为"按值序列化"，
    递归时只在遇到 IStrategy 时停下。我们的策略写法是 class X(EasyTradeRiskMixin, IStrategy)，
    而 EasyTradeRiskMixin 的父类是 object —— 于是 Python 内置模块 builtins 也被登记了，
    之后连 slice 这样的内置类型都没法序列化，hyperopt 一开就报
    "PicklingError: Could not pickle the task to send it to the workers"。
    这里换成同样的逻辑，只是跳过 object / 内置模块。只影响本进程，不改 freqtrade 文件，也不改策略。
    """
    try:
        import cloudpickle
        from freqtrade.optimize.hyperopt import hyperopt_optimizer as hoo
    except ImportError:
        return  # 没装 hyperopt 依赖：让 freqtrade 自己报错
    if not hasattr(hoo.HyperOptimizer, "hyperopt_pickle_magic"):
        return

    def safe_pickle_magic(self, bases):
        for cls in bases:
            if cls is object or getattr(cls, "__module__", "") == "builtins" or cls.__name__ == "IStrategy":
                continue
            mod = sys.modules.get(cls.__module__)
            if mod is not None:
                cloudpickle.register_pickle_by_value(mod)
            safe_pickle_magic(self, cls.__bases__)

    hoo.HyperOptimizer.hyperopt_pickle_magic = safe_pickle_magic


def cmd_hyperopt(argv: list[str]) -> int:
    offline = False
    if argv and argv[0] == "--offline":
        offline, argv = True, argv[1:]
    if argv and argv[0] == "--":
        argv = argv[1:]
    _patch_pickle_magic()
    if offline:
        import ft_offline  # 合成数据：不联网（同 backtest_all.sh 的做法）

        return ft_offline.main(["hyperopt", *argv])
    from freqtrade.main import main as ft_main

    try:
        ft_main(["hyperopt", *argv])
    except SystemExit as exc:
        return int(exc.code or 0) if not isinstance(exc.code, str) else 1
    return 0


# ----------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "hyperopt":
        return cmd_hyperopt(argv[1:])

    p = argparse.ArgumentParser(description="第 2 步策略研究辅助脚本（由 scripts/research.sh 调用）")
    sub = p.add_subparsers(dest="cmd", required=True)

    def common_strat(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--strategies", required=True)
        sp.add_argument("--strategy-dir", default="user_data/strategies")
        sp.add_argument("--timeframes", default="1h 4h 1d")
        sp.add_argument("--detail", default="auto")

    sp = sub.add_parser("timeframes")
    common_strat(sp)
    # --print download-start：不列周期，改为打印"下载从哪天开始"（研究开始日期 - 最长的指标预热），给 research.sh 下载用
    sp.add_argument("--print", choices=["timeframes", "download-start"], default="timeframes")
    sp.add_argument("--start", default="20210101")
    sp.add_argument("--warmup-candles", type=int, default=600)

    sp = sub.add_parser("prepare")
    common_strat(sp)
    sp.add_argument("--config", required=True)
    sp.add_argument("--run-dir", required=True)
    sp.add_argument("--datadir", default="")
    sp.add_argument("--start", default="20210101")
    sp.add_argument("--min-train-days", type=int, default=365,
                    help="上线晚的币：样本外开始前至少要有这么多天数据才保留（不够的去掉）")
    sp.add_argument("--test-fraction", type=float, default=1 / 3)
    sp.add_argument("--folds", type=int, default=6)
    sp.add_argument("--wf-train-days", type=int, default=365, help="走步每折训练窗口天数（0 = 测试窗口 × --wf-train-ratio）")
    sp.add_argument("--wf-test-days", type=int, default=180,
                    help="走步每折测试窗口天数（0 = 自动：最近 --wf-oos-fraction 的时间平分成 K 折）")
    sp.add_argument("--wf-train-ratio", type=float, default=2.0)
    sp.add_argument("--wf-oos-fraction", type=float, default=0.5)
    sp.add_argument("--wf-mode", choices=["rolling", "anchored"], default="rolling")
    sp.add_argument("--synthetic", action="store_true")
    sp.add_argument("--warmup-candles", type=int, default=600,
                    help="每段回测开始前最多多加载多少根K线预热指标（不少于策略自己的 startup_candle_count）")
    sp.add_argument("--settings", default="", help="JSON：研究参数（写进 plan.json，报告用）")
    sp.add_argument("--seen-from", default="",
                    help="以前的研究已经看过的数据从哪天开始（YYYYMMDD；空 = 不检查）。"
                         "样本外落在这段里时报告会提醒；这天之前的训练段单独回测一次（没看过的一段）")

    sp = sub.add_parser("gate")
    sp.add_argument("--run-dir", required=True)
    sp.add_argument("--strategy", required=True)
    sp.add_argument("--segment", default="train", help="train，或走步训练窗口 wf<k>train")
    sp.add_argument("--min-trades", type=int, default=20, help="和 hyperopt 的 --min-trades 一样")
    sp.add_argument("--trades-only", action="store_true", help="只检查交易数，不要求赚钱（走步各折用）")

    sp = sub.add_parser("best-params")
    sp.add_argument("--fthypt", required=True)
    sp.add_argument("--strategy", required=True)
    sp.add_argument("--out", required=True)
    sp.add_argument("--copy", default="")
    sp.add_argument("--label", default="")

    sp = sub.add_parser("report")
    sp.add_argument("--run-dir", required=True)
    sp.add_argument("--stage", choices=["final", "interim", "interrupted"], default="final")
    sp.add_argument("--max-dd", type=cd.max_dd_arg, default=None,
                    help="按这个回撤上限（%%）重新判定（默认 = plan.json 里记的值；不重跑回测、不改 plan.json）")
    sp.add_argument("--prereg", default="",
                    help="第 5 轮预注册登记表（默认 configs/preregistration.json；没有这个文件 = 全部按默认规则判）")

    a = p.parse_args(argv)
    return {
        "timeframes": cmd_timeframes,
        "prepare": cmd_prepare,
        "gate": cmd_gate,
        "best-params": cmd_best_params,
        "report": cmd_report,
    }[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
