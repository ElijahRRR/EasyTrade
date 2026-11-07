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
      - 持仓数量和平均开仓价：一次开仓、一次平仓的交易用 freqtrade 记的 amount / open_rate；
        有加仓（多次开仓）或分批平仓（多次平仓）的交易（例如 NFI），按结果里逐笔订单（orders）的成交时间
        重建每个时段的持仓数量和均价（和 freqtrade 自己的算法相同），分批平仓的已实现盈亏记在成交那一刻，
        整笔平仓时再记剩下的部分（合计 = freqtrade 的 profit_abs）。旧版 freqtrade 的结果没有 orders 时
        退回旧方法（最后的数量和均价套用整个持仓期间，对加仓策略只是近似值），表格末尾会说明。
        自检：.venv/bin/python scripts/check_drawdown_math.py
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


def _parse_ts(value: Any) -> float:
    """freqtrade 的时间字符串 → unix 秒。不带时区的（数据库 / API 的 to_json 是这样）按 UTC。解析不了抛 ValueError。"""
    from datetime import datetime, timezone

    dt = datetime.fromisoformat(str(value))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


# ---- 逐笔订单重建持仓（加仓 / 分批平仓）-------------------------------------------
# freqtrade 结果里每笔交易的 amount / open_rate 是【最后】的持仓数量和平均开仓价（平仓前最后一次重算的值）。
# 一次开仓、一次平仓的交易，这就是整个持仓期间的仓位；有加仓（多次开仓）或分批平仓（多次平仓）的交易，
# 持仓中间的数量和均价都不一样。这里按 orders 列表里已成交的订单，按成交时间重建每一段的持仓：
#   开仓单：数量 += a，成本 += a × 成交价，均价 = 成本 / 数量
#   平仓单：已实现盈亏 = 方向 × a × (成交价 - 均价) - 开仓费率 × a × 均价 - 平仓费率 × a × 成交价 (+ 这段的资金费)，
#           数量 -= a，成本 -= a × 均价（均价不变）
# 和 freqtrade 2026.10 的 LocalTrade.recalc_trade_from_orders / calculate_profit 同一算法（没有 FtPrecise 的十进制精度，
# 误差 1e-12 量级）。杠杆不进公式：仓位的名义金额就是 数量 × 价格（freqtrade 也是这样）。
ORDERS_FLAT_TOL = 1e-6  # 已平仓的交易：重建后剩下的数量 / 最大持仓 超过这个比例 = 订单记录对不上，退回旧算法

# trade_info() 给每笔交易标的 orders_mode：
#   "single"  一次开仓、一次平仓（按 amount / open_rate 算，和以前逐位相同）
#   "multi"   有加仓或分批平仓，已按订单重建持仓
#   "open"    还没平仓（模拟盘 / 实盘的持仓；只在给了 open_until 时出现），按订单重建
#   "missing" 结果里没有 orders（旧版 freqtrade 的结果文件）：按旧算法（最后的数量和均价）算，加仓策略只是近似值
#   "bad"     orders 对不上（例如没有已成交的开仓单、平完以后还剩仓位）：按旧算法算
ORDERS_MODES = ("single", "multi", "open", "missing", "bad")

# 没有 orders 的结果，在本进程里第一次碰到时往 stderr 说一次（research_report.py 等调用方用）；
# check_drawdown.py 自己运行时（main）关掉它，改在表格末尾说一次。
_ORDERS_NOTE_TO_STDERR = True
_ORDERS_NOTE_DONE = False

ORDERS_MISSING_NOTE = (
    "提醒：有些回测结果里没有逐笔订单记录（orders，旧版 freqtrade 的结果文件），这些交易的含浮亏回撤按旧方法算"
    "（每笔交易最后的数量和平均开仓价套用整个持仓期间）：一次开满的交易是准的，加仓 / 分批平仓的交易只是近似值"
)


def _note_orders_missing() -> None:
    global _ORDERS_NOTE_DONE
    if _ORDERS_NOTE_TO_STDERR and not _ORDERS_NOTE_DONE:
        _ORDERS_NOTE_DONE = True
        print(ORDERS_MISSING_NOTE, file=sys.stderr)


def _order_fill(o: dict[str, Any], entry_side: str) -> tuple[float, bool, float, float, float] | None:
    """一张订单 → (成交时间秒, 是不是开仓单, 成交数量, 成交均价, 记在这张单上的资金费)；没成交 / 读不出来返回 None。

    回测结果是精简格式（amount、safe_price、ft_is_entry、order_filled_timestamp）；模拟盘 / 实盘数据库的
    Trade.to_json() 是完整格式，多了 filled、ft_fee_base、is_open、funding_fee 等，有就用（和 freqtrade 一样
    按 filled - ft_fee_base 算数量，跳过还挂着的单）。
    """
    if not isinstance(o, dict) or o.get("is_open") is True:
        return None
    filled = _num(o.get("filled"))
    if filled is not None:
        amount = filled - (_num(o.get("ft_fee_base")) or 0.0)
    else:
        amount = _num(o.get("amount"))
    price = next((v for v in (_num(o.get(k)) for k in ("safe_price", "average", "price")) if v), None)
    if amount is None or amount <= 0 or price is None or price <= 0:
        return None
    ms = _num(o.get("order_filled_timestamp"))
    ts = ms / 1000.0 if ms is not None else None
    if ts is None and o.get("order_filled_date"):
        try:
            ts = _parse_ts(o["order_filled_date"])
        except ValueError:
            ts = None
    if ts is None and filled is not None:
        # 完整格式里部分成交后撤掉的单可能没有成交时间：用下单时间
        ms = _num(o.get("order_timestamp"))
        ts = ms / 1000.0 if ms is not None else None
    if ts is None:
        return None
    if isinstance(o.get("ft_is_entry"), bool):
        entry = o["ft_is_entry"]
    else:
        entry = str(o.get("ft_order_side") or "") == entry_side
    return ts, entry, amount, price, _num(o.get("funding_fee")) or 0.0


def rebuild_position(t: dict[str, Any]) -> dict[str, Any]:
    """按 orders 重建一笔交易的持仓变化。t 是 freqtrade 回测结果格式的一笔交易（dict）。

    返回 dict：
      status    "single" / "multi" / "open" / "missing" / "bad"（见 ORDERS_MODES；这里的 "open" = 平完以后还有仓位），
                或 "empty"（没有已成交的开仓单）
      legs      [(开始秒, 结束秒或 None, 数量, 均价, 到这段开始为止已实现的分批盈亏)]，每段持仓不变；
                最后一段的结束 = 最后一张平仓单的成交时间（还有仓位时 None）
      exits     [(成交秒, 已实现盈亏)]：每张平仓单（含最后一张）
      entries / n_exits  已成交的开仓 / 平仓单数；max_amount  最大持仓数量；avg  最后的均价
    """
    orders = t.get("orders")
    if not isinstance(orders, list) or not orders:
        return {"status": "missing"}
    is_short = bool(t.get("is_short"))
    d = -1.0 if is_short else 1.0
    entry_side = "sell" if is_short else "buy"
    fee_open = float(t.get("fee_open") or 0.0)
    fee_close = float(t.get("fee_close") or 0.0)
    fills = []
    for idx, o in enumerate(orders):
        f = _order_fill(o, entry_side)
        if f is not None:
            fills.append((f[0], idx, *f[1:]))
    fills.sort(key=lambda x: (x[0], x[1]))  # 按成交时间；同一时间按列表顺序（= freqtrade 的顺序）
    pos = stake = avg = 0.0
    funding = realized = max_pos = 0.0
    n_in = n_out = 0
    states: list[tuple[float, float, float, float]] = []  # (时间, 成交后的数量, 均价, 已实现)
    exits: list[tuple[float, float]] = []
    for ts, _idx, entry, amount, price, fund in fills:
        funding += fund
        if entry:
            pos += amount
            stake += price * amount
            if pos > 0:
                avg = stake / pos
            n_in += 1
        else:
            if n_in == 0:
                return {"status": "bad"}
            pnl = d * amount * (price - avg) - fee_open * amount * avg - fee_close * amount * price + funding
            funding = 0.0
            pos -= amount
            stake -= avg * amount
            realized += pnl
            exits.append((ts, pnl))
            n_out += 1
        max_pos = max(max_pos, pos)
        states.append((ts, pos, avg, realized))
    if n_in == 0:
        return {"status": "empty"}  # 没有已成交的开仓单（模拟盘里开仓单还挂着）
    tol = ORDERS_FLAT_TOL * max_pos
    flat = abs(pos) <= tol
    if pos < -tol:
        return {"status": "bad"}
    legs: list[tuple[float, float | None, float, float, float]] = []
    for k, (ts, amount, a, r) in enumerate(states):
        end = states[k + 1][0] if k + 1 < len(states) else None
        if amount <= tol or (end is not None and end <= ts):
            continue  # 平掉了 / 同一时刻马上又变了的状态：没有持仓时段
        legs.append((ts, end, amount, a, r))
    if not flat:
        status = "open"
    elif n_in == 1 and n_out == 1:
        status = "single"
    else:
        status = "multi"
    return {"status": status, "legs": legs, "exits": exits, "entries": n_in, "n_exits": n_out,
            "max_amount": max_pos, "avg": avg, "remaining": pos}


def trade_info(
    t: dict[str, Any], *, open_until: float | None = None, rebuild: str = "auto"
) -> dict[str, Any] | None:
    """一笔交易（freqtrade 回测结果格式的 dict）→ 算回撤用的 info；读不出来返回 None。

    open_until：还没平仓的交易（close_date 为空，模拟盘 / 实盘的持仓）按持有到这个时间（unix 秒）算浮亏；
                不给时这种交易返回 None（和以前一样跳过）。
    rebuild：   "auto"（默认）= 有加仓 / 分批平仓时按订单重建持仓，一次开仓一次平仓的照旧；
                "never" = 全部按旧算法（只用来对比新旧结果）；"always" = 一次开平的也走重建（只用来自检）。
    info 的旧字段（pair open close dir amount open_rate close_rate fee_open fee_close profit min_rate max_rate
    stop_exit）含义不变：amount / open_rate 仍是 freqtrade 记的最后的数量和均价。调用方可以把 amount 和 profit
    按同一个比例缩放（走步拼接、子账户），重建的各段持仓和分批盈亏会跟着按 amount / _amount_ref 缩放。
    新字段：orders_mode（见 ORDERS_MODES）、is_open；按订单重建的交易另有 legs / partials / _amount_ref。
    """
    try:
        amount = float(t["amount"])
        open_rate = float(t["open_rate"])
        close_raw = t.get("close_date")
        is_open = close_raw is None or str(close_raw).strip() in ("", "None", "NaT")
        if is_open and open_until is None:
            return None
        info = {
            "pair": t["pair"],
            "open": _parse_ts(t["open_date"]),
            "close": float(open_until) if is_open else _parse_ts(close_raw),
            "dir": -1.0 if t.get("is_short") else 1.0,
            "amount": amount,
            "open_rate": open_rate,
            "close_rate": float(t.get("close_rate") or open_rate),
            "fee_open": float(t.get("fee_open") or 0.0),
            "fee_close": float(t.get("fee_close") or 0.0),
            "profit": 0.0 if is_open else float(t.get("profit_abs") or 0.0),
            "min_rate": float(t.get("min_rate") or open_rate),
            "max_rate": float(t.get("max_rate") or open_rate),
            "stop_exit": (not is_open) and any(k in str(t.get("exit_reason", "")) for k in ("stop", "liquidation")),
        }
    except (KeyError, TypeError, ValueError):
        return None
    info["is_open"] = is_open
    rb = rebuild_position(t) if rebuild != "never" or is_open else {"status": "single"}
    mode = rb["status"]
    if mode == "missing":
        _note_orders_missing()
    if is_open:
        if mode == "empty":
            return None  # 开仓单还没成交：没有持仓
        if mode == "open":
            # 最后一段一直持有（legs 的最后一段结束 = None）；已实现的分批盈亏 = 全部平仓单
            info.update(orders_mode="open", legs=list(rb["legs"]), partials=list(rb["exits"]), _amount_ref=amount,
                        profit=float(sum(p for _ts, p in rb["exits"])))
            return info
        if amount <= 0:
            return None
        # 没有可用的订单记录（或者订单显示已经平完、close_date 却是空的）：按现在的数量和均价从开仓一直持有，
        # 已实现的分批盈亏（有 realized_profit 的话）记在开仓时
        realized = _num(t.get("realized_profit")) or 0.0
        info.update(orders_mode="missing" if mode == "missing" else "bad",
                    legs=[(info["open"], None, amount, open_rate, realized)],
                    partials=[(info["open"], realized)] if realized else [], _amount_ref=amount, profit=realized)
        return info
    if mode in ("open", "empty"):
        mode = "bad"  # 有平仓时间，订单却显示还有仓位 / 没有成交的开仓单：对不上
    if mode == "multi" or (rebuild == "always" and mode == "single"):
        legs = list(rb["legs"])
        # 最后一段持有到 close_date（和旧算法、和 freqtrade 记的平仓时间一致），最后一张平仓单的盈亏
        # 不单独记：平仓时记 profit_abs - 已记的分批盈亏（这样资金费、四舍五入都和 freqtrade 的总盈亏对得上）。
        # 开仓、平仓都在同一时刻成交（同一根K线里开了又平）时没有持仓时段：legs 为空，只记盈亏（和旧算法一样）
        if legs:
            a, _b, amt, avg, r = legs[-1]
            legs[-1] = (a, info["close"], amt, avg, r)
        # 和整笔平仓同一时刻（同一根K线里）成交的分批平仓并进平仓时记的那部分
        partials = [(ts, p) for ts, p in rb["exits"][:-1] if ts < info["close"]]
        info.update(orders_mode=mode, legs=legs, partials=partials, _amount_ref=amount)
    else:
        info["orders_mode"] = mode
    return info


def _trade_info(t: dict[str, Any]) -> dict[str, Any] | None:
    """兼容旧名字（research_report.py / research_portfolio.py / check_portfolio_math.py 在用）= trade_info(t)。"""
    return trade_info(t)


def _leg_scale(tr: dict[str, Any]) -> float:
    """调用方把 amount / profit 缩放过（走步拼接等）时，重建的持仓和分批盈亏按同一比例缩放。"""
    ref = tr.get("_amount_ref")
    return tr["amount"] / ref if ref else 1.0


def _upnl(tr: dict[str, Any], price: Any) -> Any:
    """浮动盈亏（含开仓手续费和预估平仓手续费）。price 可以是数字或 numpy 数组。"""
    gross = tr["dir"] * tr["amount"] * (price - tr["open_rate"])
    return gross - tr["fee_open"] * tr["amount"] * tr["open_rate"] - tr["fee_close"] * tr["amount"] * price


def _upnl_leg(d: float, amount: Any, avg: Any, fee_open: float, fee_close: float, price: Any) -> Any:
    """和 _upnl 同一公式，数量和均价用重建的这一段持仓。"""
    gross = d * amount * (price - avg)
    return gross - fee_open * amount * avg - fee_close * amount * price


def _trade_mae(tr: dict[str, Any], bad: float) -> float:
    """估算法用：这笔交易持仓期间对账户最不利的盈亏（<= 0）。按订单重建的交易取各段里最差的
    （各段已实现的分批盈亏 + 这段持仓在最差价格的浮亏），一次开平的交易和以前一样。"""
    if "legs" not in tr:
        return min(_upnl(tr, bad), 0.0)
    f = _leg_scale(tr)
    worst = min(
        f * r + _upnl_leg(tr["dir"], f * amt, avg, tr["fee_open"], tr["fee_close"], bad)
        for _a, _b, amt, avg, r in tr["legs"]
    )
    return min(worst, 0.0)


def mtm_drawdown_bound(trades: list[dict[str, Any]], start_balance: float) -> float:
    """估算（只用标准库，没有 pandas 时用）：权益最低点 ≈ 已平仓余额 - 同时持有仓位各自的最大浮亏之和。

    每笔交易持仓期间的最差价格来自 freqtrade 记录的 min_rate / max_rate，并假设同时持有的仓位
    在同一时刻到达最差点（这一点偏保守）；峰值只用已平仓余额（不计浮盈回吐，这一点可能偏乐观）。
    加仓 / 分批平仓的交易按订单重建的各段持仓分别算，取最差的一段。
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
            mae += _trade_mae(tr, bad)
        peak = max(peak, closed)
        if peak > 0:
            worst = max(worst, (peak - (closed + mae)) / peak)
    return worst


def _candle_arrays(data: Any) -> tuple[Any, Any, Any, Any]:
    """K线 → (开始时间秒 int64, high, low, close)。data 可以是 DataFrame（有 date/high/low/close 列）或这 4 个数组。"""
    import numpy as np
    import pandas as pd

    if isinstance(data, pd.DataFrame):
        dates = pd.to_datetime(data["date"], utc=True)
        secs = ((dates - pd.Timestamp("1970-01-01", tz="UTC")) // pd.Timedelta(seconds=1)).to_numpy("int64")
        return (secs, data["high"].to_numpy("float64"), data["low"].to_numpy("float64"),
                data["close"].to_numpy("float64"))
    secs, high, low, close = data
    return (np.asarray(secs, dtype="int64"), np.asarray(high, dtype="float64"),
            np.asarray(low, dtype="float64"), np.asarray(close, dtype="float64"))


def load_candles(
    pairs: set[str] | list[str], timeframe: str, datadir: Path, data_format: str, trading_mode: str
) -> tuple[str, dict[str, Any]] | None:
    """读回测用的K线文件，返回 (实际用的周期, {交易对: (时间秒, high, low, close)})；缺文件 / 没有 pandas 返回 None。

    日线/4h 策略用 1h K线算浮亏更细（有 1h 数据时）；5m 等小周期用自己的周期。
    """
    try:
        import pandas as pd
    except ImportError:
        return None
    tf_sec = _tf_seconds(timeframe)
    if tf_sec <= 0:
        return None
    ext = {"feather": "feather", "parquet": "parquet"}.get(data_format)
    if ext is None:
        return None
    sub = datadir / "futures" if trading_mode == "futures" else datadir
    suffix = "-futures" if trading_mode == "futures" else ""
    grid_tfs = (["1h"] if tf_sec > 3600 else []) + [timeframe]

    def load(pair: str, tf: str):
        f = sub / f"{_pair_file(pair)}-{tf.replace('M', 'Mo')}{suffix}.{ext}"
        if not f.is_file():
            return None
        df = pd.read_feather(f) if ext == "feather" else pd.read_parquet(f)
        return _candle_arrays(df)

    for tf in grid_tfs:
        loaded = {p: load(p, tf) for p in set(pairs)}
        if all(v is not None for v in loaded.values()):
            return tf, loaded
    return None


def flat_curve(start: float) -> dict[str, Any]:
    """没有交易时的资金曲线（和 research_portfolio.flat_curve 相同）。"""
    import numpy as np

    e = np.zeros(0)
    return {"grid": np.zeros(0, dtype="int64"), "g": 3600, "low": e, "end": e, "start": float(start),
            "final": float(start), "method": "", "closes": []}


def _add_legs(tr, grid, g_sec, candle, unreal_low, unreal_end, d_before, d_end) -> None:
    """按订单重建过持仓的一笔交易（加仓 / 分批平仓 / 还没平仓）加到资金曲线上。

    freqtrade 回测里订单的成交时间 = 成交那根K线的开始时间 T，成交价在这根K线的范围里。所以每根K线 [T, T+g)：
      * K线内最低：和这根K线有重叠的每一段持仓（成交时间 = T 的订单从这根K线起算）各算一次
        "这段开始前、这根K线里已实现的分批盈亏 + 这段持仓在最不利价格的浮亏"，取最差的
        （偏保守：最不利价格可能出现在这段开始之前）。
      * 收盘时（T+g）：成交时间 < T+g 的订单算已成交（T+g 成交的加仓 / 分批平仓属于下一根K线，
        不能用这根K线的收盘价去估它的成交价），按这时的持仓和收盘价算浮亏；分批盈亏同样按成交时间 < T+g 记。
      * 整笔平仓（最后一张平仓单）和旧算法一样：平仓时间 <= T+g 就算这根K线收盘时已平仓，记 profit - 已记的分批盈亏。
      * 分批平仓的已实现盈亏：成交时间 <= T 的算进"这根K线开始前已实现"。
    只有一段持仓、开始 = 开仓时间、结束 = 平仓时间时，结果和旧算法一样（第一段的开始也是按 < T+g 算，和旧算法相同）。
    已实现盈亏记成增量（d_before / d_end 的第 k 个 = 从第 k 根K线起加上），调用方最后累加一次（分批平仓多时快很多）。
    """
    import numpy as np

    secs, high, low, close = candle
    f = _leg_scale(tr)
    d, fo, fc = tr["dir"], tr["fee_open"], tr["fee_close"]
    legs = tr["legs"]
    inf = float("inf")
    starts = np.array([lg[0] for lg in legs], dtype="float64")
    ends = np.array([inf if lg[1] is None else lg[1] for lg in legs], dtype="float64")
    amts = np.array([lg[2] for lg in legs], dtype="float64") * f
    avgs = np.array([lg[3] for lg in legs], dtype="float64")
    rcum = np.array([lg[4] for lg in legs], dtype="float64") * f
    p_ts = np.array([p[0] for p in tr["partials"]], dtype="float64")
    p_pnl = np.array([p[1] for p in tr["partials"]], dtype="float64") * f
    p_cum = np.concatenate([[0.0], np.cumsum(p_pnl)])

    def k_range(a: float, b: float) -> tuple[int, int]:
        k0 = int(np.searchsorted(grid, a - g_sec, side="right"))
        # 还没平仓的最后一段：到 open_until（= info 的 close）之前开始的K线为止
        k1 = int(np.searchsorted(grid, tr["close"] if b == inf else b, side="left"))
        return k0, k1

    ranges = [k_range(float(a), float(b)) for a, b in zip(starts, ends)]
    if legs:
        K0 = min(r[0] for r in ranges)
        K1 = max(r[1] for r in ranges)
    else:
        K0 = K1 = 0
    if K1 > K0:
        ts_all = grid[K0:K1]
        pos = np.clip(np.searchsorted(secs, ts_all), 0, len(secs) - 1)
        have = secs[pos] == ts_all  # 缺K线的时间段按这段的均价处理（浮动盈亏 = -手续费）
        worst_px = high[pos] if d < 0 else low[pos]
        r_before = p_cum[np.searchsorted(p_ts, ts_all, side="right")]  # 这根K线开始前（<= T）已实现的分批盈亏
        low_arr = np.full(K1 - K0, inf)
        last_j = len(legs) - 1
        for j, (k0, k1) in enumerate(ranges):
            if k1 <= k0:
                continue
            sl = slice(k0 - K0, k1 - K0)
            bad = np.where(have[sl], worst_px[sl], avgs[j])
            if j == last_j and tr["stop_exit"] and ends[j] != inf:
                last = ts_all[sl] + g_sec > ends[j]  # 平仓发生在这根K线内部：止损价以外的影线账户没有承受
                if last.any():
                    cap = tr["close_rate"]
                    bad = np.where(last, np.minimum(bad, cap) if d < 0 else np.maximum(bad, cap), bad)
            cand = (rcum[j] - r_before[sl]) + _upnl_leg(d, amts[j], avgs[j], fo, fc, bad)
            low_arr[sl] = np.minimum(low_arr[sl], cand)
        low_arr = np.where(np.isfinite(low_arr), low_arr, 0.0)
        unreal_low[K0:K1] += low_arr
        tau = (ts_all + g_sec).astype("float64")
        jj = np.searchsorted(starts, tau, side="left") - 1  # 收盘前（开始 < T+g）开始的最后一段
        jc = np.clip(jj, 0, None)
        # 这一段在 T+g 还持有：结束 > T+g；结束 = T+g 时，中间的订单属于下一根K线（还持有），
        # 最后一段（整笔平仓）和旧算法一样算已平仓
        final = (jc == last_j) & (not tr.get("is_open"))
        act = (jj >= 0) & ((tau < ends[jc]) | ((tau == ends[jc]) & ~final))
        end_px = np.where(have, close[pos], avgs[jc])
        unreal_end[K0:K1] += np.where(act, _upnl_leg(d, amts[jc], avgs[jc], fo, fc, end_px), 0.0)
    if len(p_ts):
        np.add.at(d_before, np.searchsorted(grid, p_ts, side="left"), p_pnl)  # T >= 成交时间
        np.add.at(d_end, np.searchsorted(grid, p_ts - g_sec, side="right"), p_pnl)  # T + g > 成交时间
    if not tr.get("is_open"):
        rest = tr["profit"] - float(p_cum[-1])
        d_before[int(np.searchsorted(grid, tr["close"], side="left"))] += rest
        d_end[int(np.searchsorted(grid, tr["close"] - g_sec, side="left"))] += rest


def equity_curve_candles(
    trades: list[dict[str, Any]],
    start_balance: float,
    timeframe: str,
    datadir: Path | None,
    data_format: str = "feather",
    trading_mode: str = "futures",
    *,
    candles: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """逐根K线的账户权益曲线（trades = trade_info() 的结果列表）。缺K线数据 / 没有 pandas 时返回 None。

    candles：不从 datadir 读文件，直接给 {交易对: DataFrame(date, high, low, close) 或 (时间秒, high, low, close)}，
             周期 = timeframe（模拟盘报告用）。
    返回 {"grid": 每根K线开始时间（秒）, "g": 周期秒数, "low": 每根K线内最低权益, "end": 收盘权益,
          "start": 初始资金, "final": 初始资金 + 已实现盈亏合计, "method": "K线(5m)" 这类, "closes": [(平仓时间, 盈亏)]}
    （和 research_portfolio.candle_curve 的返回格式相同）。回撤用 curve_max_dd(曲线)。
    """
    try:
        import numpy as np
        import pandas  # noqa: F401
    except ImportError:
        return None
    if not trades:
        return flat_curve(start_balance)
    if _tf_seconds(timeframe) <= 0:
        return None
    pairs = {t["pair"] for t in trades}
    if candles is not None:
        if not all(p in candles for p in pairs):
            return None
        grid_tf = timeframe
        data = {p: _candle_arrays(candles[p]) for p in pairs}
    else:
        if datadir is None:
            return None
        loaded = load_candles(pairs, timeframe, datadir, data_format, trading_mode)
        if loaded is None:
            return None
        grid_tf, data = loaded
    g_sec = _tf_seconds(grid_tf)
    t0 = int(min(t["open"] for t in trades) // g_sec * g_sec)
    closed_ts = [t["close"] for t in trades if not t.get("is_open")]
    open_ts = [t["close"] for t in trades if t.get("is_open")]
    if open_ts:
        # 还有没平仓的交易：曲线到 open_until 之前开始的那根K线为止，不管已平仓的交易平在哪里。
        # 持仓的浮亏只算到这根K线（_add_legs 的 k1），再往后加K线（例如在最后一根K线里刚平掉一笔，
        # 旧写法会多加平仓后那一根）只有已实现盈亏、没有持仓的浮亏，峰值和回撤会算错。
        # open_until 以后才平的交易，平仓盈亏不进曲线（模拟盘报告把它们直接算进现在的权益）。
        t1 = max(t0, int(math.ceil(max(open_ts) / g_sec) * g_sec - g_sec))
    else:
        t1 = int(max(closed_ts) // g_sec * g_sec + g_sec) if closed_ts else t0
    grid = np.arange(t0, t1 + 1, g_sec, dtype="int64")
    n = len(grid)
    unreal_low = np.zeros(n)  # 这根K线内持仓的最差浮动盈亏之和
    unreal_end = np.zeros(n)  # 这根K线收盘时仍持有的仓位的浮动盈亏之和
    closed_before = np.zeros(n)  # 这根K线开始前已实现的盈亏
    closed_end = np.zeros(n)  # 这根K线结束时已实现的盈亏
    d_before = np.zeros(n + 1)  # 按订单重建的交易：已实现盈亏的增量（最后累加）
    d_end = np.zeros(n + 1)
    has_legs = False
    for tr in trades:
        if "legs" in tr:
            _add_legs(tr, grid, g_sec, data[tr["pair"]], unreal_low, unreal_end, d_before, d_end)
            has_legs = True
            continue
        # 一次开仓、一次平仓（或没有订单记录）：旧算法，逐位不变
        secs, high, low, close = data[tr["pair"]]
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
    if has_legs:  # 只有一次开平的交易时不动这两个数组（和以前逐位相同）
        closed_before += np.cumsum(d_before)[:n]
        closed_end += np.cumsum(d_end)[:n]
    return {
        "grid": grid,
        "g": g_sec,
        "low": start_balance + closed_before + unreal_low,
        "end": start_balance + closed_end + unreal_end,
        "start": float(start_balance),
        "final": float(start_balance + sum(t["profit"] for t in trades)),
        "method": f"K线({grid_tf})",
        "closes": [(t["close"], t["profit"]) for t in trades if not t.get("is_open")],
        "tf": grid_tf,
    }


def curve_max_dd(curve: dict[str, Any]) -> float:
    """一条资金曲线的最大回撤（比例，0.12 = 12%）：峰值 = 之前各根K线收盘权益的最高值（不低于初始资金），
    回撤 = (峰值 - 这根K线里的最低权益) / 峰值。"""
    import numpy as np

    e_low, e_end, start_balance = curve["low"], curve["end"], curve["start"]
    if len(e_end) == 0:
        return 0.0
    peak = np.maximum.accumulate(np.concatenate([[start_balance], e_end]))[:-1]
    peak = np.maximum(peak, start_balance)
    dd = np.where(peak > 0, (peak - np.minimum(e_low, e_end)) / peak, 0.0)
    return float(max(dd.max(), 0.0))


def mtm_drawdown_candles(
    trades: list[dict[str, Any]],
    start_balance: float,
    timeframe: str,
    datadir: Path,
    data_format: str,
    trading_mode: str,
) -> tuple[float, str] | None:
    """逐根K线的账户权益回撤（需要 pandas 和回测用的K线文件）。缺数据时返回 None。返回 (回撤比例, 用的K线周期)。"""
    try:
        import numpy  # noqa: F401
        import pandas  # noqa: F401
    except ImportError:
        return None
    if not trades:
        return 0.0, ""
    curve = equity_curve_candles(trades, start_balance, timeframe, datadir, data_format, trading_mode)
    if curve is None:
        return None
    return curve_max_dd(curve), curve["tf"]


def closed_drawdown(trades: list[dict[str, Any]], start_balance: float) -> float:
    """只看已平仓余额的回撤（每笔交易的盈亏记在平仓时间；和 research_report._closed_dd 相同）。"""
    bal = peak = start_balance
    worst = 0.0
    for i in sorted((t for t in trades if not t.get("is_open")), key=lambda x: x["close"]):
        bal += i["profit"]
        peak = max(peak, bal)
        if peak > 0:
            worst = max(worst, (peak - bal) / peak)
    return worst


def orders_summary(infos: list[dict[str, Any]]) -> dict[str, int]:
    """各种 orders_mode 的交易笔数（见 ORDERS_MODES）。"""
    out = {m: 0 for m in ORDERS_MODES}
    for i in infos:
        m = i.get("orders_mode")
        if m in out:
            out[m] += 1
    return out


def account_equity_drawdown(
    trades: list[dict[str, Any]],
    start_balance: float,
    timeframe: str,
    datadir: Path | None = None,
    *,
    data_format: str = "feather",
    trading_mode: str = "futures",
    candles: dict[str, Any] | None = None,
    as_of: float | None = None,
) -> dict[str, Any]:
    """一个账户（一组交易）的含浮亏权益回撤，给回测以外的调用方用（例如模拟盘报告、回撤看门狗）。

    trades：freqtrade 回测结果格式的交易 dict 列表（每笔要有 pair open_date close_date amount open_rate
            fee_open fee_close is_short profit_abs exit_reason min_rate max_rate，以及 orders 列表）。
            还没平仓的交易 close_date = None，按持有到 as_of 算浮亏（as_of 不给时用当前时间）。
    K线：给 candles（{交易对: DataFrame 或 (时间秒, high, low, close)}，周期 = timeframe），或 datadir（读文件）。
         两个都没有 / 缺K线时退回估算（min_rate / max_rate）。
    返回 {"dd": 历史最大回撤, "dd_now": 最后一根K线的回撤, "equity_now": 最后一根K线收盘权益, "peak": 峰值权益,
          "dd_closed": 只按已平仓余额的回撤, "method": "K线(5m)" / "估", "curve": 曲线或 None,
          "trades": 用到的笔数, "skipped": 读不出来的笔数, "orders": orders_summary(...)}
    回撤都是比例（0.15 = 15%）；和上限比用 DEFAULT_MAX_DD_PCT / 100。
    method = "估"（没有K线）时只有 dd 有意义：dd_now = 0，equity_now = 初始资金 + 已实现盈亏（不含浮亏），peak = 初始资金。
    有还没平仓的交易时，曲线（和 equity_now / dd_now）只到 as_of 之前开始的那根K线：as_of 以后才平掉的交易的平仓盈亏不在里面
    （调用方自己加，例如 forward_report.compute）。
    还没平仓的交易：freqtrade 的 profit_abs 不用（只是最后一次分批平仓的盈亏），已实现部分按订单重建；
    还没记到订单上的资金费（funding_fee_running）不计入。
    """
    import time

    if as_of is None:
        as_of = time.time()
    infos = [i for i in (trade_info(t, open_until=as_of) for t in trades) if i is not None]
    out: dict[str, Any] = {
        "trades": len(infos), "skipped": len(trades) - len(infos), "orders": orders_summary(infos),
        "dd_closed": closed_drawdown(infos, start_balance), "curve": None,
        "dd": 0.0, "dd_now": 0.0, "equity_now": None, "peak": float(start_balance), "method": "",
    }
    if not infos:
        out["equity_now"] = float(start_balance)
        return out
    curve = None
    if candles is not None or datadir is not None:
        curve = equity_curve_candles(infos, start_balance, timeframe, datadir, data_format, trading_mode,
                                     candles=candles)
    if curve is not None and len(curve["end"]):
        import numpy as np

        ends = curve["end"]
        peak = max(float(start_balance), float(np.max(ends)))
        # 最后一根K线的回撤：峰值只看它之前各根的收盘权益（和 curve_max_dd 同一口径）
        prev_peak = max(float(start_balance), float(np.max(ends[:-1])) if len(ends) > 1 else float(start_balance))
        last = float(min(curve["low"][-1], ends[-1]))
        out.update(curve=curve, method=curve["method"], dd=curve_max_dd(curve), equity_now=float(curve["end"][-1]),
                   peak=peak, dd_now=max(0.0, (prev_peak - last) / prev_peak) if prev_peak > 0 else 0.0)
    else:
        out.update(method="估", dd=mtm_drawdown_bound(infos, start_balance),
                   equity_now=float(start_balance + sum(i["profit"] for i in infos)))
    return out


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
    om = orders_summary(infos)
    if om["bad"]:
        notes.append(
            f"有 {om['bad']} 笔交易的订单记录对不上（例如平完以后还剩仓位），这几笔按旧方法"
            "（最后的数量和平均开仓价）算浮亏，只是近似值"
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
        # 按订单重建持仓的笔数（加仓 / 分批平仓）、订单对不上的笔数、没有订单记录的笔数
        "orders_multi": om["multi"],
        "orders_bad": om["bad"],
        "orders_missing": om["missing"],
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
    multi = sum(int(r.get("orders_multi") or 0) for r in rows)
    if multi:
        lines.append(
            f"    有加仓 / 分批平仓的交易 {multi} 笔：按逐笔订单重建了每个时段的持仓数量和平均开仓价，"
            "分批平仓的盈亏记在成交那一刻（不是按最后的均价套整个持仓期间）。"
        )
    if any(r.get("orders_missing") for r in rows):
        lines.append(ORDERS_MISSING_NOTE + "。")
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
    global _ORDERS_NOTE_TO_STDERR
    _ORDERS_NOTE_TO_STDERR = False  # 没有订单记录的提醒改在表格末尾说一次
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
