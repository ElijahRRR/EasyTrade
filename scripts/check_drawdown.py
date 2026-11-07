#!/usr/bin/env python3
"""
回测结果风控检查：账户整体最大回撤（含浮亏）是否 <= 15%（默认 = 项目硬规则），杠杆是否 <= 5 倍。

回撤上限（项目硬规则）：2026-11-07 起是 15%（以前是 8%，第 1、2 轮的结果都是按 8% 判的）。
  默认值只在本文件的 DEFAULT_MAX_DD_PCT 定义（shell 脚本用 scripts/_common.sh 里同样的值，
  scripts/check_portfolio_math.py 会核对两边一致）；research_report.py / research_portfolio.py 也从这里取。
  临时改，不用改代码：环境变量 MAX_DD_PCT=12（所有脚本都认），或者命令行 --max-dd 12（命令行优先）。

用法：
  .venv/bin/python scripts/check_drawdown.py                      # 检查最近一次回测（推荐用 venv 的 python，见下）
  .venv/bin/python scripts/check_drawdown.py user_data/backtest_results/run_20250101_120000
                                                                  # 检查一个批次目录里的全部结果
  .venv/bin/python scripts/check_drawdown.py some-result.zip other.zip     # 检查指定文件
  .venv/bin/python scripts/check_drawdown.py <目录> --max-dd 15 --max-leverage 5 --min-trades 30
  MAX_DD_PCT=12 .venv/bin/python scripts/check_drawdown.py <目录>          # 回撤上限改成 12%（留余量）
  .venv/bin/python scripts/check_drawdown.py <目录> --csv summary.csv --json summary.json
  .venv/bin/python scripts/check_drawdown.py <目录> --datadir user_data/data/okx   # 指定K线数据目录

回撤口径（"整体最大回撤"，看的是整个账户的资金曲线，不是单笔亏损）：
  * 回撤(平仓)：只按已平仓交易累计的账户余额曲线计算，
                取 freqtrade 的 max_drawdown_account 和 max_relative_drawdown 中较大者。
  * 回撤(含浮亏)：账户权益 = 已平仓余额 + 所有持仓的浮动盈亏，逐根K线计算。
      - 有 pandas 且找得到回测用的K线数据时（用 .venv/bin/python 运行即可）：
        逐根K线按"对持仓最不利的价格"（多单用最低价、空单用最高价）计算浮亏，并扣除预估平仓手续费，
        峰值用K线收盘时的权益。结果标为"K线"。
      - 否则（例如 Mac 自带 python3 没有 pandas）：用每笔交易持仓期间到过的最差价格
        （freqtrade 记录的 max_rate / min_rate）估算，假设同时持有的仓位同时到达最差点，
        但没有计入"浮盈回吐"。只是参考值，结果标为"估"。
      说明：freqtrade 报告里的 "Max % of account underwater (balance)"（wallet_stats）
      在合约模式下只统计已平仓余额，不含浮亏，所以这里不用它。
  * 判定：两者中较大的那个 <= 上限才算通过（更保守）。

退出码：0 = 全部通过；1 = 有未通过；3 = 没找到回测结果。
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import sys
import unicodedata
import zipfile
from pathlib import Path
from typing import Any


ROOT_DIR = Path(__file__).resolve().parent.parent
DEFAULT_RESULTS_DIR = ROOT_DIR / "user_data" / "backtest_results"
LAST_RESULT_FILE = ".last_result.json"


# ----------------------------------------------------------------------------
# 硬规则：账户回撤上限（默认值只在这里定义）
# ----------------------------------------------------------------------------
# 账户权益（已平仓余额 + 持仓浮动盈亏）从最高点到最低点的最大跌幅上限，单位 %。
# 2026-11-07 David 把它从 8% 放宽到 15%（杠杆上限 5 倍不变）。
# 改默认值：改这里，同时改 scripts/_common.sh 的 EASYTRADE_DEFAULT_MAX_DD_PCT（check_portfolio_math.py 核对两边相同）。
DEFAULT_MAX_DD_PCT = 15.0
# 第 1、2 轮用的上限：只用来在报告里说明历史（那两轮的结果目录 plan.json 里记的是 8），判定不用它
LEGACY_MAX_DD_PCT = 8.0
# 临时改上限的环境变量（shell 脚本、research_report.py、research_portfolio.py 都认）；命令行 --max-dd 优先
MAX_DD_ENV = "MAX_DD_PCT"


def parse_max_dd_pct(value: Any, source: str = "回撤上限") -> float:
    """把 "15" / "15%" / 15.0 转成百分数 15.0；不是 0 ~ 100 之间的数字就报错（ValueError，中文说明）。"""
    text = str(value).strip().rstrip("%").strip()
    try:
        pct = float(text)
    except ValueError:
        raise ValueError(f"{source}要是数字（单位 %，例如 15），收到：{value!r}") from None
    if not (math.isfinite(pct) and 0 < pct <= 100):
        raise ValueError(f"{source}要在 0 ~ 100 之间（单位 %，例如 15），收到：{value!r}")
    return pct


def default_max_dd_pct() -> float:
    """默认回撤上限（%）：环境变量 MAX_DD_PCT 有值就用它，否则 DEFAULT_MAX_DD_PCT（15）。"""
    raw = os.environ.get(MAX_DD_ENV, "").strip()
    return parse_max_dd_pct(raw, f"环境变量 {MAX_DD_ENV} ") if raw else DEFAULT_MAX_DD_PCT


def settings_max_dd_pct(settings: dict[str, Any] | None) -> float:
    """研究结果目录（plan.json 的 settings）记下的回撤上限（%）；没记的旧结果用 default_max_dd_pct()。

    研究跑的时候用的上限会写进 plan.json，所以重新生成旧报告时仍按当时的上限判（第 2 轮是 8%），
    不会因为默认值改了就把历史结果改判。
    """
    value = (settings or {}).get("max_dd")
    if value is None or str(value).strip() == "":
        return default_max_dd_pct()
    return parse_max_dd_pct(value, "plan.json 里的 max_dd ")


def override_settings_max_dd(settings: dict[str, Any], pct: float | None) -> None:
    """按别的上限重新判一次已经跑完的研究（research_report.py report / research_portfolio.py 的 --max-dd）。

    只改内存里的 settings（plan.json 不改，下次不加 --max-dd 还是按原来记的值判）；
    原来记的值存进 max_dd_recorded，报告抬头用 max_dd_override_note() 注明。pct = None 时什么都不做。
    """
    if pct is None:
        return
    if "max_dd_recorded" not in settings:
        settings["max_dd_recorded"] = settings.get("max_dd")
    settings["max_dd"] = pct


def max_dd_override_note(settings: dict[str, Any] | None) -> str:
    """用了 --max-dd 重新判定时，返回一句说明（报告抬头用）；没有重新判定时返回空字符串。"""
    s = settings or {}
    if "max_dd_recorded" not in s:
        return ""
    rec = s.get("max_dd_recorded")
    rec_txt = f"{parse_max_dd_pct(rec):g}%" if rec is not None and str(rec).strip() != "" else "没有记（按当时的默认值）"
    return (
        f"!! 重新判定：这次研究运行时记下的回撤上限是 {rec_txt}，这里按 --max-dd {settings_max_dd_pct(s):g}% 重新判；"
        "回测本身没有重跑，plan.json 没有改"
    )


def max_dd_rule_note(max_dd: float) -> str:
    """一句话说明这次用的回撤上限和项目硬规则（DEFAULT_MAX_DD_PCT）的关系，报告抬头用。"""
    rule, old = DEFAULT_MAX_DD_PCT, LEGACY_MAX_DD_PCT
    if abs(max_dd - rule) < 1e-9:
        return f"= 项目硬规则（2026-11-07 起 {rule:g}%；第 1、2 轮是按 {old:g}% 判的）"
    if abs(max_dd - old) < 1e-9:
        return f"= 2026-11-07 以前的旧上限；现在的硬规则是 {rule:g}%"
    if max_dd < rule:
        return f"比项目硬规则 {rule:g}% 更严（自己设的，例如留余量）"
    return f"!! 比项目硬规则 {rule:g}% 宽：这里的\"通过\"不代表符合硬规则"


# ----------------------------------------------------------------------------
# 读取 freqtrade 回测结果
# ----------------------------------------------------------------------------
def _is_stats(obj: Any) -> bool:
    return isinstance(obj, dict) and isinstance(obj.get("strategy"), dict)


def load_result_file(path: Path) -> dict[str, Any] | None:
    """读取一个回测结果（.zip 或 .json），返回 freqtrade 的统计 dict。"""
    if path.suffix == ".zip":
        with zipfile.ZipFile(path) as zf:
            names = [n for n in zf.namelist() if n.endswith(".json")]
            # 统计文件和 zip 同名：backtest-result-<时间>.json
            preferred = f"{path.stem}.json"
            ordered = sorted(names, key=lambda n: (n != preferred, n))
            for name in ordered:
                try:
                    data = json.loads(zf.read(name).decode("utf-8"))
                except (ValueError, UnicodeDecodeError):
                    continue
                if _is_stats(data):
                    return data
        return None
    if path.suffix == ".json":
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (ValueError, UnicodeDecodeError):
            return None
        return data if _is_stats(data) else None
    return None


def load_result_config(path: Path) -> dict[str, Any]:
    """回测时用的配置（zip 里的 *_config.json），用来找交易所名称、数据格式等。没有就返回空 dict。"""
    if path.suffix != ".zip":
        return {}
    try:
        with zipfile.ZipFile(path) as zf:
            for name in zf.namelist():
                if name.endswith("_config.json"):
                    return json.loads(zf.read(name).decode("utf-8"))
    except (OSError, ValueError, zipfile.BadZipFile):
        pass
    return {}


def collect_files(inputs: list[str]) -> list[Path]:
    files: list[Path] = []
    if not inputs:
        # 最近一次回测：freqtrade 把 .last_result.json 写在结果所在目录。
        # backtest_all.sh 的结果在 run_<时间>/ 子目录里，所以两处都找，取最新的一个（不含 synthetic/ 合成数据）。
        lasts = [DEFAULT_RESULTS_DIR / LAST_RESULT_FILE, *DEFAULT_RESULTS_DIR.glob(f"run_*/{LAST_RESULT_FILE}")]
        lasts = sorted((f for f in lasts if f.is_file()), key=lambda f: f.stat().st_mtime, reverse=True)
        for last in lasts:
            try:
                latest = json.loads(last.read_text(encoding="utf-8")).get("latest_backtest")
            except ValueError:
                continue
            if latest and (last.parent / latest).is_file():
                return [last.parent / latest]
        inputs = [str(DEFAULT_RESULTS_DIR)]

    for item in inputs:
        p = Path(item)
        if p.is_dir():
            for f in sorted(p.iterdir()):
                if f.name.startswith(".") or f.name.endswith(".meta.json"):
                    continue
                if f.suffix in (".zip", ".json") and f.name.startswith("backtest-result"):
                    files.append(f)
        elif p.is_file():
            files.append(p)
        else:
            print(f"警告：找不到 {p}", file=sys.stderr)
    return files


# ----------------------------------------------------------------------------
# 计算 / 判定
# ----------------------------------------------------------------------------
def _num(value: Any) -> float | None:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(f) else f


# ----------------------------------------------------------------------------
# 含浮亏的账户权益回撤
# ----------------------------------------------------------------------------
_TF_SECONDS = {"m": 60, "h": 3600, "d": 86400, "w": 604800}


def _tf_seconds(tf: str) -> int:
    try:
        return int(tf[:-1]) * _TF_SECONDS[tf[-1]]
    except (KeyError, ValueError, IndexError):
        return 0


def _pair_file(pair: str) -> str:
    for ch in ["/", " ", ".", "@", "$", "+", ":"]:  # 与 freqtrade misc.pair_to_filename 相同
        pair = pair.replace(ch, "_")
    return pair


def _trade_info(t: dict[str, Any]) -> dict[str, Any] | None:
    from datetime import datetime

    try:
        amount = float(t["amount"])
        open_rate = float(t["open_rate"])
        info = {
            "pair": t["pair"],
            "open": datetime.fromisoformat(str(t["open_date"])).timestamp(),
            "close": datetime.fromisoformat(str(t["close_date"])).timestamp(),
            "dir": -1.0 if t.get("is_short") else 1.0,
            "amount": amount,
            "open_rate": open_rate,
            "close_rate": float(t.get("close_rate") or open_rate),
            "fee_open": float(t.get("fee_open") or 0.0),
            "fee_close": float(t.get("fee_close") or 0.0),
            "profit": float(t.get("profit_abs") or 0.0),
            "min_rate": float(t.get("min_rate") or open_rate),
            "max_rate": float(t.get("max_rate") or open_rate),
            "stop_exit": any(k in str(t.get("exit_reason", "")) for k in ("stop", "liquidation")),
        }
    except (KeyError, TypeError, ValueError):
        return None
    return info


def _upnl(tr: dict[str, Any], price: Any) -> Any:
    """浮动盈亏（含开仓手续费和预估平仓手续费）。price 可以是数字或 numpy 数组。"""
    gross = tr["dir"] * tr["amount"] * (price - tr["open_rate"])
    return gross - tr["fee_open"] * tr["amount"] * tr["open_rate"] - tr["fee_close"] * tr["amount"] * price


def mtm_drawdown_bound(trades: list[dict[str, Any]], start_balance: float) -> float:
    """估算（只用标准库，没有 pandas 时用）：权益最低点 ≈ 已平仓余额 - 同时持有仓位各自的最大浮亏之和。

    每笔交易持仓期间的最差价格来自 freqtrade 记录的 min_rate / max_rate，并假设同时持有的仓位
    在同一时刻到达最差点（这一点偏保守）；峰值只用已平仓余额（不计浮盈回吐，这一点可能偏乐观）。
    精确值请用 .venv/bin/python 运行（逐根K线计算）。
    """
    events: list[tuple[float, int, int]] = []  # (时间, 0=平仓 1=开仓, 序号)；同一时刻先平后开
    for i, tr in enumerate(trades):
        events.append((tr["open"], 1, i))
        events.append((tr["close"], 0, i))
    events.sort()
    closed = start_balance
    peak = start_balance
    open_set: set[int] = set()
    worst = 0.0
    for _ts, kind, i in events:
        if kind == 0:
            open_set.discard(i)
            closed += trades[i]["profit"]
        else:
            open_set.add(i)
        mae = 0.0
        for j in open_set:
            tr = trades[j]
            bad = tr["max_rate"] if tr["dir"] < 0 else tr["min_rate"]
            if tr["stop_exit"]:
                # max_rate/min_rate 含触发止损那根K线越过止损价的影线，账户实际只亏到止损成交价
                bad = min(bad, tr["close_rate"]) if tr["dir"] < 0 else max(bad, tr["close_rate"])
            mae += min(_upnl(tr, bad), 0.0)
        peak = max(peak, closed)
        if peak > 0:
            worst = max(worst, (peak - (closed + mae)) / peak)
    return worst


def mtm_drawdown_candles(
    trades: list[dict[str, Any]],
    start_balance: float,
    timeframe: str,
    datadir: Path,
    data_format: str,
    trading_mode: str,
) -> tuple[float, str] | None:
    """逐根K线的账户权益回撤（需要 pandas 和回测用的K线文件）。缺数据时返回 None。"""
    try:
        import numpy as np
        import pandas as pd
    except ImportError:
        return None
    if not trades:
        return 0.0, ""
    tf_sec = _tf_seconds(timeframe)
    if tf_sec <= 0:
        return None
    ext = {"feather": "feather", "parquet": "parquet"}.get(data_format)
    if ext is None:
        return None
    sub = datadir / "futures" if trading_mode == "futures" else datadir
    suffix = "-futures" if trading_mode == "futures" else ""
    # 日线/4h 策略用 1h K线算浮亏更细（有 1h 数据时）；5m 等小周期用自己的周期
    grid_tfs = (["1h"] if tf_sec > 3600 else []) + [timeframe]

    def load(pair: str, tf: str):
        f = sub / f"{_pair_file(pair)}-{tf.replace('M', 'Mo')}{suffix}.{ext}"
        if not f.is_file():
            return None
        df = pd.read_feather(f) if ext == "feather" else pd.read_parquet(f)
        dates = pd.to_datetime(df["date"], utc=True)
        secs = ((dates - pd.Timestamp("1970-01-01", tz="UTC")) // pd.Timedelta(seconds=1)).to_numpy("int64")
        return secs, df["high"].to_numpy("float64"), df["low"].to_numpy("float64"), df["close"].to_numpy("float64")

    grid_tf = None
    candles: dict[str, Any] = {}
    for tf in grid_tfs:
        loaded = {p: load(p, tf) for p in {t["pair"] for t in trades}}
        if all(v is not None for v in loaded.values()):
            grid_tf, candles = tf, loaded
            break
    if grid_tf is None:
        return None
    g_sec = _tf_seconds(grid_tf)
    t0 = int(min(t["open"] for t in trades) // g_sec * g_sec)
    t1 = int(max(t["close"] for t in trades) // g_sec * g_sec + g_sec)
    grid = np.arange(t0, t1 + 1, g_sec, dtype="int64")
    n = len(grid)
    unreal_low = np.zeros(n)  # 这根K线内持仓的最差浮动盈亏之和
    unreal_end = np.zeros(n)  # 这根K线收盘时仍持有的仓位的浮动盈亏之和
    closed_before = np.zeros(n)  # 这根K线开始前已平仓的盈亏
    closed_end = np.zeros(n)  # 这根K线结束时已平仓的盈亏
    for tr in trades:
        secs, high, low, close = candles[tr["pair"]]
        # 持仓期间与K线 [T, T+g) 有重叠：T < 平仓时间 且 T + g > 开仓时间
        k0 = int(np.searchsorted(grid, tr["open"] - g_sec, side="right"))
        k1 = int(np.searchsorted(grid, tr["close"], side="left"))
        if k1 > k0:
            ts = grid[k0:k1]
            pos = np.searchsorted(secs, ts)
            pos = np.clip(pos, 0, len(secs) - 1)
            have = secs[pos] == ts  # 缺K线的时间段按开仓价处理（浮动盈亏 0）
            bad = np.where(have, high[pos] if tr["dir"] < 0 else low[pos], tr["open_rate"])
            last = ts + g_sec > tr["close"]  # 平仓发生在这根K线内部
            if tr["stop_exit"] and last.any():
                # 止损单在止损价成交，价格越过止损价的那部分账户并没有承受
                cap = tr["close_rate"]
                bad = np.where(last, np.minimum(bad, cap) if tr["dir"] < 0 else np.maximum(bad, cap), bad)
            unreal_low[k0:k1] += _upnl(tr, bad)
            hold = ts + g_sec < tr["close"]  # 这根K线收盘时仍持有（恰好在收盘时平仓的算已平仓）
            if hold.any():
                end_px = np.where(have, close[pos], tr["open_rate"])
                unreal_end[k0:k1] += np.where(hold, _upnl(tr, end_px), 0.0)
        # 已实现盈亏
        kb = int(np.searchsorted(grid, tr["close"], side="left"))
        closed_before[kb:] += tr["profit"]  # T >= 平仓时间：这根K线开始前已平仓
        ke = int(np.searchsorted(grid, tr["close"] - g_sec, side="left"))
        closed_end[ke:] += tr["profit"]  # T + g >= 平仓时间：这根K线结束时已平仓
    e_low = start_balance + closed_before + unreal_low
    e_end = start_balance + closed_end + unreal_end
    peak = np.maximum.accumulate(np.concatenate([[start_balance], e_end]))[:-1]
    peak = np.maximum(peak, start_balance)
    dd = np.where(peak > 0, (peak - np.minimum(e_low, e_end)) / peak, 0.0)
    return float(max(dd.max(), 0.0)), grid_tf


def evaluate(
    name: str,
    st: dict[str, Any],
    source: Path,
    max_dd: float,
    max_lev: float,
    min_trades: int,
    config: dict[str, Any] | None = None,
    datadir: Path | None = None,
) -> dict[str, Any]:
    wallet = st.get("wallet_stats") or {}
    trades = st.get("trades") or []
    config = config or {}

    dd_closed_vals = [_num(st.get("max_drawdown_account")), _num(st.get("max_relative_drawdown"))]
    dd_closed = max((v for v in dd_closed_vals if v is not None), default=0.0)

    # 含浮亏的回撤：优先逐根K线精确计算，做不到就用 min_rate/max_rate 保守估计
    infos = [i for i in (_trade_info(t) for t in trades) if i is not None]
    start_balance = _num(st.get("starting_balance")) or _num(st.get("dry_run_wallet")) or 0.0
    dd_mtm: float | None = None
    mtm_method = ""
    if infos and start_balance > 0:
        if datadir is None:
            exch = str((config.get("exchange") or {}).get("name") or "").lower()
            datadir = ROOT_DIR / "user_data" / "data" / exch if exch else None
        exact = None
        if datadir is not None:
            exact = mtm_drawdown_candles(
                infos,
                start_balance,
                str(st.get("timeframe_detail") or st.get("timeframe") or ""),
                datadir,
                str(config.get("dataformat_ohlcv") or "feather"),
                str(config.get("trading_mode") or "futures"),
            )
        if exact is not None:
            dd_mtm, grid_tf = exact
            mtm_method = f"K线({grid_tf})"
        else:
            dd_mtm = mtm_drawdown_bound(infos, start_balance)
            mtm_method = "估"
    dd_worst = max(dd_closed, dd_mtm or 0.0)

    leverages = [_num(t.get("leverage")) for t in trades]
    leverages = [v for v in leverages if v is not None]
    lev_max = max(leverages) if leverages else None

    total_trades = int(st.get("total_trades") or 0)
    profit_total = _num(st.get("profit_total")) or 0.0

    reasons: list[str] = []
    notes: list[str] = []
    if total_trades == 0:
        reasons.append("没有任何交易，无法评估")
    if dd_worst * 100 > max_dd:
        reasons.append(f"最大回撤 {dd_worst:.2%} 超过上限 {max_dd:g}%")
    if lev_max is not None and lev_max > max_lev + 1e-9:
        reasons.append(f"最大杠杆 {lev_max:g}x 超过上限 {max_lev:g}x")
    if mtm_method == "估":
        notes.append(
            "回撤(含浮亏)只是估算（找不到K线数据或没有 pandas）；"
            "用 .venv/bin/python 运行并确认 --datadir 正确，才能得到逐根K线的精确值"
        )
    if 0 < total_trades < min_trades:
        notes.append(f"交易数只有 {total_trades} 笔（< {min_trades}），统计上不可靠")
    if total_trades > 0 and profit_total <= 0:
        notes.append("回测期内总收益 <= 0，回撤达标也没有意义")

    return {
        "result": "FAIL" if reasons else "PASS",
        "strategy": name,
        "timeframe": st.get("timeframe", ""),
        "timeframe_detail": st.get("timeframe_detail", "") or "",
        "start": str(st.get("backtest_start", ""))[:10],
        "end": str(st.get("backtest_end", ""))[:10],
        "trades": total_trades,
        "winrate": _num(st.get("winrate")),
        "profit_total": profit_total,
        "profit_total_abs": _num(st.get("profit_total_abs")),
        "profit_factor": _num(st.get("profit_factor")),
        "sharpe": _num(st.get("sharpe")),
        "calmar": _num(st.get("calmar")),
        "sharpe_wallet": _num(wallet.get("sharpe")),
        "calmar_wallet": _num(wallet.get("calmar")),
        "dd_closed": dd_closed,
        "dd_mtm": dd_mtm,
        "dd_mtm_method": mtm_method,
        "dd_worst": dd_worst,
        "dd_abs": _num(st.get("max_drawdown_abs")),
        "drawdown_start": st.get("drawdown_start", ""),
        "drawdown_end": st.get("drawdown_end", ""),
        "leverage_max": lev_max,
        "starting_balance": _num(st.get("starting_balance")),
        "final_balance": _num(st.get("final_balance")),
        "stake_currency": st.get("stake_currency", "USDT"),
        "reasons": reasons,
        "notes": notes,
        "source": str(source),
    }


# ----------------------------------------------------------------------------
# 输出（中文表格，按显示宽度对齐）
# ----------------------------------------------------------------------------
def _width(text: str) -> int:
    return sum(2 if unicodedata.east_asian_width(c) in ("W", "F") else 1 for c in text)


def _pad(text: str, width: int, right: bool = False) -> str:
    gap = " " * max(width - _width(text), 0)
    return gap + text if right else text + gap


def _pct(v: float | None) -> str:
    return "-" if v is None else f"{v * 100:.2f}%"


def _f(v: float | None, nd: int = 2) -> str:
    if v is None:
        return "-"
    if math.isinf(v):
        return "∞"
    return f"{v:.{nd}f}"


def render_table(rows: list[dict[str, Any]], max_dd: float, max_lev: float) -> str:
    headers = [
        ("结论", False), ("策略", False), ("周期", False), ("回测区间", False),
        ("交易数", True), ("胜率", True), ("总收益", True), ("盈亏因子", True),
        ("Sharpe", True), ("Calmar", True), ("回撤(平仓)", True), ("回撤(含浮亏)", True),
        ("最大杠杆", True),
    ]
    body = []
    for r in rows:
        tf = r["timeframe"] + (f"/{r['timeframe_detail']}" if r["timeframe_detail"] else "")
        body.append([
            "通过" if r["result"] == "PASS" else "未通过",
            r["strategy"],
            tf,
            f"{r['start']}~{r['end']}",
            str(r["trades"]),
            _pct(r["winrate"]),
            _pct(r["profit_total"]),
            _f(r["profit_factor"]),
            _f(r["sharpe"]),
            _f(r["calmar"]),
            _pct(r["dd_closed"]),
            _pct(r["dd_mtm"]) + (f" {r['dd_mtm_method']}" if r["dd_mtm_method"] else ""),
            "-" if r["leverage_max"] is None else f"{r['leverage_max']:g}x",
        ])
    widths = [
        max(_width(h), *(_width(row[i]) for row in body)) if body else _width(h)
        for i, (h, _) in enumerate(headers)
    ]
    sep = "+" + "+".join("-" * (w + 2) for w in widths) + "+"
    lines = [
        f"账户整体最大回撤上限（含浮亏）：{max_dd:g}% {max_dd_rule_note(max_dd)}    杠杆上限：{max_lev:g}x",
        sep,
        "| " + " | ".join(_pad(h, widths[i]) for i, (h, _) in enumerate(headers)) + " |",
        sep,
    ]
    for row in body:
        lines.append(
            "| "
            + " | ".join(_pad(cell, widths[i], headers[i][1]) for i, cell in enumerate(row))
            + " |"
        )
    lines.append(sep)

    detail = []
    for r in rows:
        msgs = [f"未通过：{x}" for x in r["reasons"]] + [f"提醒：{x}" for x in r["notes"]]
        if msgs:
            detail.append(f"- {r['strategy']}（{r['timeframe']}）")
            detail.extend(f"    {m}" for m in msgs)
    if detail:
        lines.append("")
        lines.append("说明：")
        lines.extend(detail)

    passed = sum(1 for r in rows if r["result"] == "PASS")
    lines.append("")
    lines.append(f"合计：{len(rows)} 个结果，通过 {passed} 个，未通过 {len(rows) - passed} 个。")
    lines.append(
        "注：回撤(平仓)只看已平仓交易；回撤(含浮亏)按账户权益（余额+持仓浮动盈亏）计算，"
        "标\"K线\"为逐根K线计算，标\"估\"为估算。两者都必须 <= 上限才算通过。"
    )
    return "\n".join(lines)


CSV_FIELDS = [
    "result", "strategy", "timeframe", "timeframe_detail", "start", "end", "trades",
    "winrate", "profit_total", "profit_total_abs", "profit_factor", "sharpe", "calmar",
    "sharpe_wallet", "calmar_wallet", "dd_closed", "dd_mtm", "dd_mtm_method", "dd_worst", "dd_abs",
    "drawdown_start", "drawdown_end", "leverage_max", "starting_balance", "final_balance",
    "stake_currency", "reasons", "notes", "source",
]


def write_csv(rows: list[dict[str, Any]], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # utf-8-sig：让 Excel 正确识别中文
    with path.open("w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.DictWriter(fh, fieldnames=CSV_FIELDS)
        writer.writeheader()
        for r in rows:
            out = dict(r)
            out["reasons"] = "；".join(r["reasons"])
            out["notes"] = "；".join(r["notes"])
            writer.writerow({k: out.get(k) for k in CSV_FIELDS})


def max_dd_arg(text: str) -> float:
    try:
        return parse_max_dd_pct(text, "--max-dd ")
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from None


def main(argv: list[str] | None = None) -> int:
    try:
        dd_default = default_max_dd_pct()
    except ValueError as exc:
        print(f"参数错误：{exc}", file=sys.stderr)
        return 2
    parser = argparse.ArgumentParser(
        description=f"检查 freqtrade 回测结果的账户整体最大回撤（含浮亏，默认上限 {dd_default:g}%）和杠杆（默认上限 5x）。"
    )
    parser.add_argument("paths", nargs="*", help="回测结果 .zip/.json 文件或目录；不填 = 最近一次回测")
    parser.add_argument(
        "--max-dd", type=max_dd_arg, default=dd_default,
        help=f"最大回撤上限，单位 %%（默认 {dd_default:g}：环境变量 {MAX_DD_ENV}，没设时 = 项目硬规则 "
             f"{DEFAULT_MAX_DD_PCT:g}）",
    )
    parser.add_argument("--max-leverage", type=float, default=5.0, help="杠杆上限（默认 5）")
    parser.add_argument("--min-trades", type=int, default=30, help="交易数少于此值时给出提醒（默认 30）")
    parser.add_argument("--csv", type=Path, help="另存为 CSV（Excel 可直接打开）")
    parser.add_argument("--json", type=Path, help="另存为 JSON")
    parser.add_argument(
        "--datadir",
        type=Path,
        help="回测用的K线数据目录（默认 user_data/data/<交易所>），用来逐根K线计算含浮亏回撤",
    )
    args = parser.parse_args(argv)

    files = collect_files(args.paths)
    rows: list[dict[str, Any]] = []
    for f in files:
        try:
            stats = load_result_file(f)
        except (OSError, zipfile.BadZipFile) as exc:
            print(f"警告：读取 {f} 失败：{exc}", file=sys.stderr)
            continue
        if not stats:
            continue
        cfg = load_result_config(f)
        for name, st in stats["strategy"].items():
            rows.append(
                evaluate(
                    name, st, f, args.max_dd, args.max_leverage, args.min_trades, cfg, args.datadir
                )
            )

    if not rows:
        print("没有找到回测结果。先运行回测（bash scripts/backtest_all.sh），或指定结果文件/目录。")
        return 3

    rows.sort(key=lambda r: (r["result"] != "PASS", r["dd_worst"]))
    print(render_table(rows, args.max_dd, args.max_leverage))

    if args.csv:
        write_csv(rows, args.csv)
        print(f"\nCSV 已保存：{args.csv}")
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"JSON 已保存：{args.json}")

    return 1 if any(r["result"] == "FAIL" for r in rows) else 0


if __name__ == "__main__":
    sys.exit(main())
