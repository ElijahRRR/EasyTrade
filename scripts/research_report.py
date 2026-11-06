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


MAX_LOSS = 100000  # freqtrade hyperopt 给"交易数不够"的组合的损失值
DEFAULT_START_BALANCE = 1000.0
VARIANT_CN = {"default": "默认参数", "opt": "优化参数"}


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
    if label.startswith("wf") and label.endswith("train"):
        return f"走步第 {label[2:-5]} 折训练窗口"
    if label.startswith("wf"):
        return f"走步第 {label[2:]} 折"
    return label


def _ceil_day(dt: datetime) -> date:
    d = dt.date()
    return d if dt == datetime(d.year, d.month, d.day, tzinfo=dt.tzinfo) else d + timedelta(days=1)


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
    tol = timedelta(days=a.max_start_gap_days)

    scans: dict[str, dict[str, Any]] = {}
    dropped: dict[str, str] = {}
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

    req_start_dt = datetime(req_start.year, req_start.month, req_start.day, tzinfo=timezone.utc)
    ref_start = max(req_start_dt, min(v["first"] for v in scans.values()))
    ref_end = max(v["end"] for v in scans.values())
    kept = []
    for pair, v in scans.items():
        if v["first"] > ref_start + tol:
            late = (v["first"].date() - ref_start.date()).days
            dropped[pair] = f"数据从 {v['first'].date()} 才开始（比 {ref_start.date()} 晚 {late} 天，可能上线晚）"
        elif v["end"] < ref_end - timedelta(days=3):
            dropped[pair] = f"数据在 {v['end'].date()} 就断了（可能已下架或没更新）"
        else:
            kept.append(pair)
    if not kept:
        print("错误：没有一个交易对的数据能覆盖研究区间。", file=sys.stderr)
        return 1
    S = max(req_start, _ceil_day(max(scans[p]["first"] for p in kept)))
    E = min(scans[p]["end"] for p in kept).date()
    total = (E - S).days
    if total < 120:
        print(f"错误：共同数据区间只有 {total} 天（{S} ~ {E}），太短，没法切分训练段/样本外。", file=sys.stderr)
        return 1

    # ---- 切分（按 UTC 自然日；freqtrade 的 --timerange 结束日期是"包含"的，
    #      所以前一段在后一段开始的前一天结束，两段不共用任何一根K线）----
    test_days = max(30, round(total * a.test_fraction))
    T0 = E - timedelta(days=test_days)
    train = (S, T0 - timedelta(days=1))
    test = (T0, E)

    folds: list[dict[str, Any]] = []
    k_req = max(0, a.folds)
    fold_len = int(total * a.wf_oos_fraction // k_req) if k_req else 0
    wf_note = ""
    if k_req and fold_len < 20:
        wf_note = f"数据太短（每折测试窗口只有 {fold_len} 天），不做走步验证"
        k_req = 0
    train_len = round(a.wf_train_ratio * fold_len)
    if k_req:
        W0 = E - timedelta(days=k_req * fold_len)
        for k in range(1, k_req + 1):
            ts = W0 + timedelta(days=(k - 1) * fold_len)
            te = E if k == k_req else W0 + timedelta(days=k * fold_len) - timedelta(days=1)
            tr_s = S if a.wf_mode == "anchored" else max(S, ts - timedelta(days=train_len))
            folds.append({"k": k, "train": (tr_s, ts - timedelta(days=1)), "test": (ts, te)})

    # ---- 每个策略的预热 / 走步可行性 ----
    for n in found:
        info = strategies[n]
        tf_sec = cd._tf_seconds(info["timeframe"]) if info["timeframe"] else 0
        warm = math.ceil(info["startup"] * tf_sec / 86400) if tf_sec else 0
        info["warmup_days"] = warm
        eff0 = S + timedelta(days=warm)
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
    # 所以：每一段开始前有多少历史就多给多少，最多 warmup_candles 根（默认 600，残留约 2%，
    # 也差不多是 OKX 实盘 freqtrade 内存里保留的K线数：startup + 300）。数据开头那一段只能用策略自己的预热长度。
    # 实现：给这一段的 freqtrade 命令多加一个 --config warmup/<策略>__<段>.json，里面只有 startup_candle_count。
    warm_dir = run_dir / "warmup"
    warm_dir.mkdir(parents=True, exist_ok=True)
    seg_starts: dict[str, date] = {"train": train[0], "test": test[0]}
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
            avail = int((d0 - S).days * 86400 // tf_sec) if tf_sec > 0 else 0
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

    out(f"数据检查（数据目录 {datadir}，需要的周期：{' '.join(req_tfs_sorted)}）")
    out(f"  配置里的交易对 {len(whitelist)} 个：保留 {len(kept)} 个，去掉 {len(dropped)} 个。")
    if dropped:
        out("  去掉的币（历史数据不够，硬留着会让区间变短或结果失真）：")
        by_reason: dict[str, list[str]] = {}
        for pair, why in dropped.items():
            # "没有 1h 数据" 这类原因相同的合并成一行；带日期的原因每个币不一样，各占一行
            by_reason.setdefault(why, []).append(pair.split("/")[0])
        for why, coins in by_reason.items():
            out(f"    - {why}（{len(coins)} 个）：{' '.join(coins)}")
    if "BTC/USDT:USDT" in whitelist and "BTC/USDT:USDT" not in kept:
        out("  !! 注意：BTC 被去掉了。用 BTC 判断大盘的策略（BigTrendBoth、DonchianTrend 等）会出问题，先补齐 BTC 的数据。")
    out("")
    out("区间切分（UTC，按天）：")
    out(f"  全部数据  {rng((S, E))}（{total} 天）")
    out(f"  训练段    {rng(train)}（{(train[1] - train[0]).days + 1} 天，调参只用这一段）")
    out(f"  样本外    {rng(test)}（{(test[1] - test[0]).days + 1} 天，最后约 {a.test_fraction:.0%}；训练段调出的参数从没见过这段）")
    settings = json.loads(a.settings) if a.settings else {}
    if folds and settings.get("hyperopt") in (1, "1", True) and any(f["train"][1] >= test[0] for f in folds):
        late = "、".join(str(f["k"]) for f in folds if f["train"][1] >= test[0])
        out(f"            注意：走步第 {late} 折会用这段时间里的数据重新调参，所以\"优化参数\"那一行的样本外和走步结果不是两份独立证据")
    if folds:
        how = "扩展窗口：每折都从数据开头训练" if a.wf_mode == "anchored" else f"滚动窗口：每折先训练 {train_len} 天"
        out(f"  走步验证  {len(folds)} 折（{how}，再测试后面 {fold_len} 天）：")
        for f in folds:
            out(f"    第 {f['k']} 折  训练 {rng(f['train'])}  →  测试 {rng(f['test'])}")
    else:
        out(f"  走步验证  不做{'（' + wf_note + '）' if wf_note else ''}")
    warm_lines = [
        f"    - {n}（{strategies[n]['timeframe']}）：预热 {strategies[n]['warmup_days']} 天，"
        f"训练段实际从 {strategies[n]['train_effective_start']} 开始"
        + (
            f"；走步第 {'、'.join(str(i + 1) for i, v in enumerate(strategies[n]['wf_ok']) if not v)} 折训练窗口扣掉预热后太短，"
            "这几折不调参、用默认参数"
            if folds and not all(strategies[n]["wf_ok"])
            else ""
        )
        for n in found
        if strategies[n]["warmup_days"] >= 3
    ]
    if warm_lines:
        out("  指标需要先\"预热\"（例如 EMA200），预热期不交易，freqtrade 会自动把开始时间往后推：")
        for w in warm_lines:
            out(w)
    if want_base > 0:
        out(f"  每一段回测开始前，最多多加载 {want_base} 根K线算指标（有多少历史就给多少；数据开头那一段只能用策略自己的预热长度）。")
        out("    EMA200 这类指标要几百根K线才能\"忘掉\"起点，只给 300 根时各段的信号会随切分点变化，和连续回测、实盘对不上。")
        out("    离数据开头太近的段给不满（最少 = 策略自己的预热长度）；每段实际用了多少根记在 plan.json 的 warmup_candles 里。")
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
        "pairs_dropped": dropped,
        "pairs_config_count": len(whitelist),
        "data_range": [_iso(S), _iso(E)],
        "train": [_iso(train[0]), _iso(train[1])],
        "test": [_iso(test[0]), _iso(test[1])],
        "test_fraction": a.test_fraction,
        "wf": {
            "mode": a.wf_mode,
            "train_days": train_len,
            "test_days": fold_len,
            "note": wf_note,
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
        strategy, st, f, float(s.get("max_dd", 8.0)), float(s.get("max_leverage", 5.0)),
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
# 判定
# ----------------------------------------------------------------------------
def judge(row: dict[str, Any], s: dict[str, Any], wf_enabled: bool) -> None:
    max_dd = float(s.get("max_dd", 8.0)) / 100
    max_lev = float(s.get("max_leverage", 5.0))
    min_pf = float(s.get("min_pf", 1.2))
    min_trades = int(s.get("min_trades", 30))
    ratio = float(s.get("pf_ratio", 0.6))
    tr, te, wf = row["train"], row["test"], row["wf"]
    reasons: list[str] = []
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
        if 0 < te["trades"] < min_trades:
            reasons.append(f"样本外只有 {te['trades']} 笔交易（< {min_trades}）")
        if te.get("leverage_max") is not None and te["leverage_max"] > max_lev + 1e-9:
            reasons.append(f"杠杆 {te['leverage_max']:g}x 超过 {max_lev:g}x")
    if tr is None:
        reasons.append("训练段回测失败或没有结果")
    else:
        if tr["trades"] == 0:
            reasons.append("训练段一笔交易都没有")
        elif tr["profit_total"] <= 0:
            reasons.append(f"训练段本身就亏损 {_pct(tr['profit_total'], True)}（样本外赚钱多半是运气/行情）")
        if tr["dd_worst"] > max_dd:
            reasons.append(f"训练段回撤 {_pct(tr['dd_worst'])} 超过 {max_dd * 100:g}%")
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
            if wf["trades"] < min_trades:
                reasons.append(f"走步只有 {wf['trades']} 笔交易（< {min_trades}）")
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


def build_rows(plan: dict[str, Any], jobs, notes) -> list[dict[str, Any]]:
    run_dir = Path(plan["_run_dir"])
    folds = plan["wf"]["folds"]
    s = plan.get("settings", {})
    wf_enabled = bool(folds)
    rows = []
    for strat in plan["strategies_order"]:
        info = plan["strategies"][strat]
        variants = ["default"]
        if _job(jobs, strat, "opt", "test") or _job(jobs, strat, "opt", "train"):
            variants.append("opt")
        default_folds = [eval_job(plan, jobs, strat, "default", f"wf{f['k']}") for f in folds]
        for variant in variants:
            row: dict[str, Any] = {
                "strategy": strat,
                "timeframe": info["timeframe"] or "?",
                "variant": variant,
                "train": eval_job(plan, jobs, strat, variant, "train"),
                "test": eval_job(plan, jobs, strat, variant, "test"),
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
            judge(row, s, wf_enabled)
            if row["verdict"] == "ERROR":
                # 一个结果都没有：多半是策略代码报错。第一条原因直接指向日志，"没有可调参数"之类的提醒没有意义
                failed = [j for j in jobs if j["strategy"] == strat and j["variant"] == variant and j["status"] == "fail"]
                row["notes"] = [n for n in row["notes"] if "没有可调参数" not in n]
                if failed:
                    row["reasons"].insert(0, "回测运行出错（多半是策略代码报错）")
                    row["notes"].append(f"报错信息看结果目录里的 logs/{Path(failed[0]['log']).name}")
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
            rows.append(row)
    vr = {"PASS": 0, "FAIL": 1, "ERROR": 2}
    rows.sort(key=lambda r: (vr[r["verdict"]], -((r["test"] or {}).get("profit_total") or -9.0)))
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


def render(plan: dict[str, Any], rows: list[dict[str, Any]], stage: str) -> str:
    s = plan.get("settings", {})
    max_dd = float(s.get("max_dd", 8.0))
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
    L.append(f"数据区间：{dr[0]} ~ {dr[1]}    训练段：{tr_[0]} ~ {tr_[1]}    样本外：{te_[0]} ~ {te_[1]}（训练段调出的参数从没见过这段）")
    wf = plan["wf"]
    late = [f["k"] for f in wf["folds"] if f["train"][1] >= te_[0]]
    if late and s.get("hyperopt") in (1, "1", True):
        L.append(
            f"          注意：走步第 {'、'.join(str(k) for k in late)} 折会用样本外这段时间的数据重新调参，"
            "所以\"优化参数\"那一行的样本外和走步结果不是两份独立证据（默认参数那一行不受影响）"
        )
    if wf["folds"]:
        how = "扩展窗口" if wf["mode"] == "anchored" else f"滚动：每折先用前 {wf['train_days']} 天调参"
        L.append(
            f"走步验证：{len(wf['folds'])} 折（{how}，再测后面 {wf['test_days']} 天），"
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
        L.append(
            f"资金费：OKX 只能下载最近约 3 个月的资金费率（这次数据里最早的一条在 {ff or '（没有资金费数据）'}），"
            "在那之前持仓的资金费按 0 算，拿得久的趋势策略会显得偏好；"
        )
        L.append(
            "      \"样本外扣资金费\"列按每 8 小时 0.01%（做多付、做空收）估算了这一部分，只用来提醒、不参与\"通过\"判定。"
            "趋势策略样本外只是小赚的，不能算已经证明能赚钱"
        )
    L.append(
        f"通过标准（样本外和走步拼接结果都要满足）：含浮亏回撤 ≤ {max_dd:g}%、总收益 > 0、"
        f"盈亏因子 ≥ {float(s.get('min_pf', 1.2)):g}、交易 ≥ {int(s.get('min_trades', 30))} 笔、杠杆 ≤ {float(s.get('max_leverage', 5)):g}x；"
    )
    L.append(
        f"                                          训练段也要赚钱、回撤 ≤ {max_dd:g}%；"
        f"样本外盈亏因子不低于训练段的 {float(s.get('pf_ratio', 0.6)):g} 倍"
    )
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
        verdict = {"PASS": "通过", "FAIL": "未通过", "ERROR": "出错"}[r["verdict"]]
        body.append([
            str(r["rank"]), verdict, r["strategy"], r["timeframe"], VARIANT_CN[r["variant"]],
            _pct(tr.get("profit_total"), True), _num(tr.get("profit_factor")), _pct(tr.get("dd_worst")),
            _pct(te.get("profit_total"), True), _pct(fe.get("profit_total_est"), True) if fe else "-",
            _num(te.get("profit_factor")), _pct(te.get("dd_worst")),
            str(te["trades"]) if te else "-",
            _pct(w.get("profit_total"), True) if w else "-", _pct(w.get("dd_worst")) if w else "-",
            r["reasons"][0] if r["reasons"] else "全部达标",
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

    L.append("")
    L.append("说明（每一行的细节）：")
    for r in rows:
        L.append(f"- [{r['rank']}] {r['strategy']}（{r['timeframe']}，{VARIANT_CN[r['variant']]}）")
        tr, te, w = r["train"], r["test"], r["wf"]
        if tr or te:
            seg = []
            if tr:
                seg.append(f"训练 {tr['start']}~{tr['end']}（{tr['trades']} 笔）")
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
        L.append("组合检查：没有策略通过，没有做。")

    passed = sum(1 for r in rows if r["verdict"] == "PASS")
    L.append("")
    L.append(f"合计：{len(rows)} 行（{len(plan['strategies_order'])} 个策略），通过 {passed} 行。")
    if dropped:
        L.append(f"!! 还有要求研究、但没有跑的策略：{dropped}")
    L.append("注：")
    L.append("  - 回撤 = 账户权益（余额 + 持仓浮动盈亏）从最高点到最低点的跌幅，逐根K线计算，取\"平仓\"和\"含浮亏\"两种口径中较大的，")
    L.append("    和 make check 口径相同。PF = 盈亏因子 = 总盈利 ÷ 总亏损。")
    L.append("  - 走步收益 = 各折测试结果按时间顺序复利拼接；走步回撤 = 拼接后整条资金曲线的最大回撤。")
    L.append("    优化参数那一行的走步：每一折都只用这一折自己的训练窗口重新调参，再去测后面没见过的那段。")
    L.append("  - 样本外扣资金费 = 样本外收益 + 按每 8 小时 0.01% 估算的、没有资金费数据那段持仓的资金费（做多付、做空收；只是估算，不参与判定）。")
    L.append("  - 交易对是站在今天挑的\"还活着的大币\"，有幸存者偏差，结果偏乐观；不要为了让表格变好看反复改参数、重跑。")
    if plan.get("synthetic"):
        L.append("  - 再说一次：这是合成数据，数字没有意义。")
    return "\n".join(L)


CSV_FIELDS = [
    "rank", "verdict", "strategy", "timeframe", "variant",
    "train_start", "train_end", "train_trades", "train_profit", "train_pf", "train_dd",
    "test_start", "test_end", "test_trades", "test_winrate", "test_profit", "test_profit_funding_est", "test_pf",
    "test_dd_closed", "test_dd_mtm", "test_dd", "test_leverage_max",
    "wf_trades", "wf_profit", "wf_pf", "wf_dd", "wf_fold_returns",
    "params", "reasons", "notes",
]


def _csv_row(r: dict[str, Any]) -> dict[str, Any]:
    tr, te, w = r["train"] or {}, r["test"] or {}, r["wf"] or {}

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
        "wf_trades": w.get("trades"), "wf_profit": w.get("profit_total"), "wf_pf": f(w.get("profit_factor")),
        "wf_dd": w.get("dd_worst"),
        "wf_fold_returns": " ".join("" if x is None else f"{x:.4f}" for x in w.get("fold_returns") or []),
        "params": json.dumps(r["params"], ensure_ascii=False) if r["params"] else "",
        "reasons": "；".join(r["reasons"]), "notes": "；".join(dict.fromkeys(r["notes"])),
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
    jobs = load_jobs(run_dir)
    if not any(j["status"] == "ok" for j in jobs):
        print("没有任何成功的回测结果，无法生成报告（看 logs/ 目录里的日志）。")
        return 3
    rows = build_rows(plan, jobs, load_notes(run_dir))
    text = render(plan, rows, a.stage)
    print(text)
    (run_dir / "summary.txt").write_text(text + "\n", encoding="utf-8")
    with (run_dir / "summary.csv").open("w", newline="", encoding="utf-8-sig") as fh:  # utf-8-sig：Excel 认中文
        w = csv.DictWriter(fh, fieldnames=CSV_FIELDS)
        w.writeheader()
        for r in rows:
            w.writerow(_csv_row(r))
    _write_json(run_dir / "summary.json", {"stage": a.stage, "plan": _strip(plan), "rows": _strip(rows)})
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

    sp = sub.add_parser("prepare")
    common_strat(sp)
    sp.add_argument("--config", required=True)
    sp.add_argument("--run-dir", required=True)
    sp.add_argument("--datadir", default="")
    sp.add_argument("--start", default="20240101")
    sp.add_argument("--max-start-gap-days", type=int, default=30)
    sp.add_argument("--test-fraction", type=float, default=1 / 3)
    sp.add_argument("--folds", type=int, default=4)
    sp.add_argument("--wf-train-ratio", type=float, default=2.0)
    sp.add_argument("--wf-oos-fraction", type=float, default=0.5)
    sp.add_argument("--wf-mode", choices=["rolling", "anchored"], default="rolling")
    sp.add_argument("--synthetic", action="store_true")
    sp.add_argument("--warmup-candles", type=int, default=600,
                    help="每段回测开始前最多多加载多少根K线预热指标（不少于策略自己的 startup_candle_count）")
    sp.add_argument("--settings", default="", help="JSON：研究参数（写进 plan.json，报告用）")

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
