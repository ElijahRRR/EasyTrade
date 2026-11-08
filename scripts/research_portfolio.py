#!/usr/bin/env python3
"""
子账户组合检查（第 3 轮）：几个日线策略各自在一个币安子账户里跑（每个子账户一个 freqtrade 机器人），
总资金按权重分开，看"所有子账户加起来"的账户权益。

一般不用直接运行：scripts/research_report.py 的 report 会调用它，结果在 summary.txt 排名表下面的
"子账户组合检查"一节，另存 portfolio.txt / portfolio.csv / portfolio.json（在运行目录里）。
也可以对已经跑完的研究目录单独再算一次（不重跑回测，几秒到一两分钟）：
  .venv/bin/python scripts/research_portfolio.py --run-dir user_data/research/run_<时间>
  .venv/bin/python scripts/research_portfolio.py --run-dir <目录> --strategies "BigTrendShortLowRisk,TsmomTrend,XSMomentum"
  .venv/bin/python scripts/research_portfolio.py --run-dir <目录> --weights riskparity
  .venv/bin/python scripts/research_portfolio.py --run-dir <目录> --max-dd 12      # 按别的回撤上限重新判（不改 plan.json）
  # 第 5 轮：合并几个研究目录（例如币安服务器上跑的 XSFundingCarry + OKX 上跑的其他策略），结果标注"跨交易所近似"，
  # 写到第一个目录的 portfolio-merged.txt/csv/json（--out-dir 可以换目录）；区间切分和判定按第一个目录
  .venv/bin/python scripts/research_portfolio.py --run-dirs <OKX 的目录> <币安的目录> --weights riskparity

模型：
  * freqtrade 不能在同一个账户里同时跑两个带杠杆的机器人，所以每个策略一个子账户、一个机器人；
    总资金 C（默认 1000 USDT）按权重 w_i 分到各子账户，之后子账户之间不挪钱（不做再平衡）。
  * 研究脚本里每个策略都是用整个 1000 USDT 单独回测的。策略按"账户权益 × 每笔风险 ÷ 止损距离"定仓，
    仓位和账户权益成正比，所以分到 C·w_i 的子账户，资金曲线就是 策略自己的资金曲线 × (C·w_i / 1000)。
    （没考虑最小下单量：钱少了以后有些小单开不出来，见下面"最小下单额检查"。）
  * 每个策略的资金曲线 = 已平仓余额 + 持仓浮动盈亏，逐根K线计算，直接用 scripts/check_drawdown.py 的
    equity_curve_candles（加仓 / 分批平仓的交易按逐笔订单重建持仓；scripts/check_portfolio_math.py 核对和排名表回撤逐位一致）。
  * 各子账户曲线放到同一条按小时的时间轴上（没有持仓时保持上一个值），相加 = 组合权益曲线。
    回撤 = 组合权益从最高点到最低点的跌幅（每根K线内取各子账户"最不利价格"时的权益相加，偏保守）。
  * 区间：训练段、没看过的一段（训练段里以前的研究没用过的那部分，有的话）、样本外、
    走步拼接（各折按时间顺序复利拼接，和排名表的"走步"同一口径）分别算。
  * 回撤上限 = 这次研究 plan.json 里记的 max_dd（scripts/research.sh 的 --max-dd / RESEARCH_MAX_DD / MAX_DD_PCT，
    默认 15% = 项目硬规则，定义在 scripts/check_drawdown.py；第 2 轮的结果目录里记的是 8%，重算时仍按 8% 判；
    单独运行时加 --max-dd 可以按别的上限重新判，plan.json 不改）。下面写作"上限"。
  * 挑哪几个策略（没有用 RESEARCH_PORTFOLIO 指定时）：默认参数在【训练段】赚钱、盈亏因子 ≥ 1.0、回撤 ≤ 上限；
    训练段日收益相关系数 ≥ 0.7 的两个只留一个（留训练段 收益/回撤 比更好的）。不看样本外：
    "用样本外挑、再用样本外判"等于自己给自己打分。
  * 权重：equal = 平均分（默认）；riskparity = 按每个策略在训练段的日收益波动率的倒数分（只用训练段，绝不用样本外）。
  * 另外报告（只报告，不用来挑选、不参与判定）：每个候选策略和参考趋势策略（TsmomTrend、DonchianTrend、XSMomentum、
    BigTrendShort，这次有结果的）的训练段日收益相关系数（预注册第 3.4 节）。
  * 通过标准和单个策略相同：样本外和走步拼接都要 组合总权益的含浮亏回撤 ≤ 上限、总收益 > 0、盈亏因子 ≥ 1.2（按组合里
    所有交易、按子账户资金缩放后的盈亏算）、交易 ≥ 30 笔、杠杆 ≤ 5x；训练段要赚钱、回撤 ≤ 上限；
    样本外盈亏因子不低于训练段的 0.6 倍；没看过的一段（有的话）也要赚钱、回撤 ≤ 上限。
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import re
import sys
import traceback
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any


SCRIPTS_DIR = Path(__file__).resolve().parent
ROOT_DIR = SCRIPTS_DIR.parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import check_drawdown as cd  # noqa: E402


DEFAULT_CAPITAL = 1000.0
DAY = 86400
# 默认挑选规则：默认参数那一行【训练段】赚钱、盈亏因子 ≥ 1.0、回撤 ≤ 上限（默认 15%）（接近不赚不亏的也算 —— 组合要看的就是它们）。
# 只看训练段：样本外留给判定（以前是按样本外挑的，等于用同一段数据挑了再判，组合"通过"就是循环论证）
SELECT_MIN_PF = 1.0
# 两个子账户日收益相关系数超过这个值：基本是同一个策略，放在一起分散不了风险
HIGH_CORR = 0.7
# 币安 U 本位合约的最小名义金额（下单金额 = 数量 × 价格）。大部分币 5 USDT；BTC、ETH 高一些。
# 写代码时（2026-11）的大概值，交易所会改，以币安当时的规则为准。
MIN_NOTIONAL_DEFAULT = 5.0
MIN_NOTIONAL = {"BTC/USDT:USDT": 100.0, "ETH/USDT:USDT": 20.0}
# freqtrade 算最小下单额时多留的余量：(1 + amount_reserve_percent 5%) / (1 - |stoploss|)，最多 1.5 倍
# （freqtrade/exchange/exchange.py _get_stake_amount_limit）
AMOUNT_RESERVE_PERCENT = 0.05
# freqtrade 最多把仓位放大 30% 去凑最小下单额，再不够就不开这一笔（freqtrade/wallets.py validate_stake_amount）
MIN_STAKE_ADJUST = 1.3
# 第 5 轮：每个候选策略和这几个参考趋势策略的训练段日收益相关系数（只报告，不用来挑选；预注册第 3.4 节）
REF_TREND = ("TsmomTrend", "DonchianTrend", "XSMomentum", "BigTrendShort")
CROSS_EXCHANGE_LABEL = "跨交易所近似"
SEG_CN = {"train": "训练段", "holdout": "没看过的一段", "test": "样本外", "wf": "走步拼接"}
SEG_ORDER = ("train", "holdout", "test", "wf")
WEIGHTS_CN = {"equal": "平均分", "riskparity": "风险平价（按训练段日收益波动率的倒数分）"}


# ----------------------------------------------------------------------------
# 小工具
# ----------------------------------------------------------------------------
def parse_names(text: Any) -> list[str]:
    """"A,B C、D" -> ["A", "B", "C", "D"]（去重，保持顺序）。"""
    if not text:
        return []
    if isinstance(text, (list, tuple)):
        text = ",".join(str(x) for x in text)
    out: list[str] = []
    for n in re.split(r"[\s,，、;；]+", str(text)):
        if n and n not in out:
            out.append(n)
    return out


def _pct(v: float | None, signed: bool = False) -> str:
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return "-"
    return f"{v * 100:+.2f}%" if signed else f"{v * 100:.2f}%"


def _num(v: float | None, nd: int = 2) -> str:
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return "-"
    if math.isinf(v):
        return "∞"
    return f"{v:.{nd}f}"


def _day_ts(d: str | date) -> int:
    if isinstance(d, str):
        d = date.fromisoformat(d[:10])
    return int(datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp())


def _clean(obj: Any) -> Any:
    """JSON：inf / nan -> 字符串，numpy 数字 -> python 数字。"""
    if isinstance(obj, dict):
        return {str(k): _clean(v) for k, v in obj.items() if not str(k).startswith("_")}
    if isinstance(obj, (list, tuple)):
        return [_clean(v) for v in obj]
    if hasattr(obj, "item") and not isinstance(obj, (str, bytes)):
        try:
            obj = obj.item()
        except (AttributeError, ValueError):
            pass
    if isinstance(obj, float) and (math.isinf(obj) or math.isnan(obj)):
        return str(obj)
    return obj


def profit_factor(profits: list[float]) -> float | None:
    """和 research_report._profit_factor 相同：总盈利 ÷ 总亏损。"""
    if not profits:
        return None
    gains = sum(p for p in profits if p > 0)
    losses = -sum(p for p in profits if p < 0)
    if losses <= 0:
        return math.inf if gains > 0 else None
    return gains / losses


# ----------------------------------------------------------------------------
# 单个策略的资金曲线（逐根K线，含浮亏）
# ----------------------------------------------------------------------------
# 曲线 = dict：
#   grid  每根K线的开始时间（秒，int64 数组；没有交易时为空）  g  K线长度（秒）
#   low   这根K线里账户权益的最低值（持仓按最不利价格）       end  这根K线收盘时的账户权益
#   start 初始资金   final  最后的账户权益（全部平仓后）       method  "K线(1h)" / "估(只按平仓)" / ""
#   closes [(平仓时间, 盈亏)]（算"平仓回撤"用）
def flat_curve(start: float) -> dict[str, Any]:
    import numpy as np

    e = np.zeros(0)
    return {"grid": np.zeros(0, dtype="int64"), "g": 3600, "low": e, "end": e, "start": float(start),
            "final": float(start), "method": "", "closes": []}


def candle_curve(
    infos: list[dict[str, Any]],
    start_balance: float,
    timeframe: str,
    datadir: Path,
    data_format: str,
    trading_mode: str,
) -> dict[str, Any] | None:
    """逐根K线的账户权益曲线：直接用 scripts/check_drawdown.py 的 equity_curve_candles（排名表回撤的同一个函数；
    加仓 / 分批平仓的交易按逐笔订单重建持仓）。缺K线数据时返回 None。"""
    return cd.equity_curve_candles(infos, start_balance, timeframe, datadir, data_format, trading_mode)


def closed_curve(infos: list[dict[str, Any]], start_balance: float, g_sec: int = 3600) -> dict[str, Any]:
    """找不到K线数据时的退路：只按已平仓余额（不含浮亏，回撤会偏小）。"""
    import numpy as np

    if not infos:
        return flat_curve(start_balance)
    t0 = int(min(t["open"] for t in infos) // g_sec * g_sec)
    t1 = int(max(t["close"] for t in infos) // g_sec * g_sec + g_sec)
    grid = np.arange(t0, t1 + 1, g_sec, dtype="int64")
    closed_end = np.zeros(len(grid))
    for tr in infos:
        closed_end[int(np.searchsorted(grid, tr["close"] - g_sec, side="left")):] += tr["profit"]
    e = start_balance + closed_end
    return {"grid": grid, "g": g_sec, "low": e, "end": e.copy(), "start": float(start_balance),
            "final": float(start_balance + sum(t["profit"] for t in infos)), "method": "估(只按平仓)",
            "closes": [(t["close"], t["profit"]) for t in infos]}


def curve_from_points(times: list[int], ends: list[float], lows: list[float] | None = None,
                      g_sec: int = DAY, start: float | None = None) -> dict[str, Any]:
    """手工曲线（测试用）：times = 每根K线开始时间，ends / lows = 收盘时 / K线内最低的权益。"""
    import numpy as np

    end = np.asarray(ends, dtype="float64")
    low = np.asarray(lows if lows is not None else ends, dtype="float64")
    s = float(start if start is not None else end[0])
    return {"grid": np.asarray(times, dtype="int64"), "g": int(g_sec), "low": low, "end": end, "start": s,
            "final": float(end[-1]), "method": "手工", "closes": []}


def curve_max_dd(curve: dict[str, Any]) -> float:
    """单条曲线自己的最大回撤（和 check_drawdown.mtm_drawdown_candles 同一公式）。"""
    return max_dd(curve["low"], curve["end"], curve["start"])


def max_dd(low: Any, end: Any, start: float) -> float:
    """峰值 = 之前各根K线收盘权益的最高值（不低于初始资金）；回撤 = (峰值 - 这根K线里的最低权益) / 峰值。"""
    import numpy as np

    low = np.asarray(low, dtype="float64")
    end = np.asarray(end, dtype="float64")
    if len(end) == 0:
        return 0.0
    peak = np.maximum.accumulate(np.concatenate([[start], end]))[:-1]
    peak = np.maximum(peak, start)
    dd = np.where(peak > 0, (peak - np.minimum(low, end)) / peak, 0.0)
    return float(max(dd.max(), 0.0))


def closed_dd(closes: list[tuple[float, float]], start: float) -> float:
    """只看已平仓余额的回撤（和 research_report._closed_dd 相同）。closes = [(平仓时间, 盈亏)]。"""
    bal = peak = start
    worst = 0.0
    for _t, p in sorted(closes, key=lambda x: x[0]):
        bal += p
        peak = max(peak, bal)
        if peak > 0:
            worst = max(worst, (peak - bal) / peak)
    return worst


# ----------------------------------------------------------------------------
# 合并（子账户 -> 组合）
# ----------------------------------------------------------------------------
def on_grid(curve: dict[str, Any], G: Any) -> tuple[Any, Any]:
    """把一条曲线放到公共时间轴 G（每根K线的开始时间）上：还没开始 = 初始资金，结束以后 = 最后的权益，
    中间取覆盖这个时间点的那根K线（没有持仓时保持上一个值）。"""
    import numpy as np

    n = len(curve["grid"])
    if n == 0:
        f = np.full(len(G), curve["start"], dtype="float64")
        return f, f.copy()
    idx = np.searchsorted(curve["grid"], G, side="right") - 1
    before = idx < 0
    after = G >= curve["grid"][-1] + curve["g"]
    ic = np.clip(idx, 0, n - 1)
    low = curve["low"][ic].astype("float64")
    end = curve["end"][ic].astype("float64")
    low[before] = end[before] = curve["start"]
    low[after] = end[after] = curve["final"]
    return low, end


def combine(curves: list[dict[str, Any]], scales: list[float], t_start: int, t_end: int) -> dict[str, Any]:
    """各子账户曲线 × 缩放系数（= 子账户资金 / 策略回测的初始资金），放到公共时间轴上相加。

    t_start / t_end：区间开始 / 结束（秒，结束不含）。公共时间轴的K线长度 = 各曲线里最短的（日线策略一般是 1h）。
    """
    import numpy as np

    gs = [c["g"] for c in curves if len(c["grid"])] or [3600]
    g = int(min(gs))
    lo = min([t_start] + [int(c["grid"][0]) for c in curves if len(c["grid"])])
    hi = max([t_end] + [int(c["grid"][-1]) + c["g"] for c in curves if len(c["grid"])])
    lo = lo // g * g
    G = np.arange(lo, hi, g, dtype="int64")
    sub_low, sub_end = [], []
    for c, k in zip(curves, scales):
        lw, en = on_grid(c, G)
        sub_low.append(lw * k)
        sub_end.append(en * k)
    start = float(sum(c["start"] * k for c, k in zip(curves, scales)))
    final = float(sum(c["final"] * k for c, k in zip(curves, scales)))
    return {
        "G": G, "g": g, "t_start": int(t_start), "t_end": int(t_end),
        "low": np.sum(sub_low, axis=0) if sub_low else np.zeros(len(G)),
        "end": np.sum(sub_end, axis=0) if sub_end else np.zeros(len(G)),
        "sub_end": sub_end, "sub_low": sub_low,
        "start": start, "final": final,
        "sub_start": [c["start"] * k for c, k in zip(curves, scales)],
    }


def value_at(G: Any, g: int, end: Any, start: float, T: Any) -> Any:
    """时间点 T（秒，可以是数组）的账户权益 = T 之前最后一根已收盘K线的收盘权益（T 在所有K线之前 = 初始资金）。"""
    import numpy as np

    T = np.asarray(T, dtype="int64")
    if len(G) == 0:
        return np.full(T.shape, float(start))
    j = np.searchsorted(np.asarray(G) + g, T, side="right") - 1
    vals = np.asarray(end, dtype="float64")[np.clip(j, 0, len(G) - 1)]
    return np.where(j >= 0, vals, float(start))


def daily_values(G: Any, g: int, end: Any, start: float, t_start: int, t_end: int) -> Any:
    """每天 00:00 UTC 的权益（从区间开始那天到区间结束），用来算日收益。"""
    import numpy as np

    days = np.arange(t_start, t_end + 1, DAY, dtype="int64")
    return value_at(G, g, end, start, days)


def daily_returns(values: Any) -> Any:
    import numpy as np

    v = np.asarray(values, dtype="float64")
    if len(v) < 2:
        return np.zeros(0)
    prev = v[:-1]
    return np.where(prev > 0, v[1:] / np.where(prev > 0, prev, 1.0) - 1.0, 0.0)


def sharpe(returns: Any) -> float | None:
    """日收益 Sharpe，按一年 365 天年化（无风险利率按 0）。波动为 0 时没有意义，返回 None。"""
    import numpy as np

    r = np.asarray(returns, dtype="float64")
    if len(r) < 2:
        return None
    sd = float(np.std(r, ddof=1))
    if sd <= 1e-15:
        return None
    return float(np.mean(r) / sd * math.sqrt(365))


def vol(returns: Any) -> float | None:
    import numpy as np

    r = np.asarray(returns, dtype="float64")
    if len(r) < 2:
        return None
    sd = float(np.std(r, ddof=1))
    return sd if sd > 1e-15 else None


def cagr(total_return: float, days: float) -> float | None:
    if days <= 0 or total_return <= -1:
        return None
    return (1 + total_return) ** (365.0 / days) - 1


def corr_matrix(series: list[Any]) -> list[list[float | None]]:
    """日收益的皮尔逊相关系数矩阵；某个子账户整段都没有波动（一笔交易都没有）时那一行是 None。"""
    import numpy as np

    n = len(series)
    out: list[list[float | None]] = [[None] * n for _ in range(n)]
    for i in range(n):
        for j in range(n):
            a, b = np.asarray(series[i]), np.asarray(series[j])
            if len(a) < 2 or len(a) != len(b) or vol(a) is None or vol(b) is None:
                continue
            out[i][j] = 1.0 if i == j else float(np.corrcoef(a, b)[0, 1])
    return out


def risk_parity(vols: list[float | None]) -> list[float] | None:
    """权重 ∝ 1 / 波动率。有任何一个波动率算不出来（训练段没有交易）就返回 None（改用平均分）。"""
    if not vols or any(v is None or v <= 0 for v in vols):
        return None
    inv = [1.0 / float(v) for v in vols]  # type: ignore[arg-type]
    s = sum(inv)
    return [x / s for x in inv]


def min_stake_reserve(stoploss: Any) -> float:
    """freqtrade 在最小下单额上多留的余量倍数。"""
    try:
        sl = abs(float(stoploss))
    except (TypeError, ValueError):
        return 1.5
    r = (1 + AMOUNT_RESERVE_PERCENT) / (1 - sl) if sl != 1 else 1.5
    return max(min(r, 1.5), 1.0)


def classify_notional(notional: float, min_notional: float) -> str:
    """ok = 够最小下单额；bump = freqtrade 会放大到最小下单额（不超过 30%，实际风险比设计的大）；skip = 不开这一笔。"""
    if notional >= min_notional:
        return "ok"
    if notional * MIN_STAKE_ADJUST >= min_notional:
        return "bump"
    return "skip"


def _quantile(xs: list[float], q: float) -> float | None:
    if not xs:
        return None
    s = sorted(xs)
    pos = (len(s) - 1) * q
    lo = int(math.floor(pos))
    hi = min(lo + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (pos - lo)


# ----------------------------------------------------------------------------
# 从研究结果里取每个策略的交易
# ----------------------------------------------------------------------------
def _infos(r: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not r:
        return []
    return [i for i in (cd._trade_info(t) for t in r["_st"].get("trades") or []) if i is not None]


def stitched_infos(folds: list[dict[str, Any] | None]) -> tuple[list[dict[str, Any]], float, int]:
    """走步各折按时间顺序复利拼接（和 research_report.stitch_folds 同一口径）：第 k 折的交易按
    "第 k 折开始时的权益 / 这一折回测的初始资金" 缩放。返回 (交易, 初始资金, 缺了几折)。"""
    have = [r for r in folds if r is not None]
    start = float(have[0].get("starting_balance") or DEFAULT_CAPITAL) if have else DEFAULT_CAPITAL
    equity = start
    out: list[dict[str, Any]] = []
    missing = 0
    for r in folds:
        if r is None:
            missing += 1
            continue
        sb = float(r.get("starting_balance") or start)
        fb = float(r.get("final_balance") or sb)
        factor = equity / sb if sb > 0 else 1.0
        for i in _infos(r):
            i["amount"] *= factor
            i["profit"] *= factor
            out.append(i)
        equity *= fb / sb if sb > 0 else 1.0
    return out, start, missing


def _segment_window(plan: dict[str, Any], seg: str) -> tuple[str, str] | None:
    if seg == "train":
        return plan["train"][0], plan["train"][1]
    if seg == "test":
        return plan["test"][0], plan["test"][1]
    if seg == "holdout":
        ho = plan.get("holdout")
        return (ho[0], ho[1]) if ho else None
    folds = (plan.get("wf") or {}).get("folds") or []
    if not folds:
        return None
    return folds[0]["test"][0], folds[-1]["test"][1]


def _member_segment(plan: dict[str, Any], row: dict[str, Any], seg: str) -> dict[str, Any]:
    """一个策略在一个区间里的交易、初始资金、资金曲线。

    合并多个运行目录（--run-dirs）时，row["_plan"] 是这个策略自己那次研究的 plan.json：K线从它自己的数据目录读
    （OKX 和币安的K线不在同一个目录）。"""
    own = row.get("_plan") or plan
    if seg == "wf":
        infos, start, missing = stitched_infos(row.get("folds") or [])
        have = [r for r in row.get("folds") or [] if r is not None]
        ref = have[0] if have else None
        ok = bool(have)
    else:
        ref = row.get(seg)
        infos, start, missing = _infos(ref), float((ref or {}).get("starting_balance") or DEFAULT_CAPITAL), 0
        ok = ref is not None
    curve = None
    if ok and infos:
        tf = str(ref.get("timeframe_detail") or ref.get("timeframe") or "")
        cfg = ref.get("_cfg") or {}
        curve = candle_curve(
            infos, start, tf, Path(own["datadir"]),
            str(cfg.get("dataformat_ohlcv") or own.get("dataformat") or "feather"),
            str(cfg.get("trading_mode") or own.get("trading_mode") or "futures"),
        )
        if curve is None:
            curve = closed_curve(infos, start)
    if curve is None:
        curve = flat_curve(start)
    levs = []
    for r in ([x for x in row.get("folds") or [] if x is not None] if seg == "wf" else [ref] if ref else []):
        if r.get("leverage_max") is not None:
            levs.append(float(r["leverage_max"]))
    stoploss = ((ref or {}).get("_st") or {}).get("stoploss")
    return {"ok": ok, "infos": infos, "start": start, "curve": curve, "folds_missing": missing,
            "leverage_max": max(levs) if levs else None, "stoploss": stoploss}


# ----------------------------------------------------------------------------
# 挑策略 / 一个区间的组合结果
# ----------------------------------------------------------------------------
def select(rows: list[dict[str, Any]], explicit: list[str], max_dd: float) -> dict[str, Any]:
    """挑组合成员。没有指定时按默认规则：默认参数那一行【训练段】赚钱、PF ≥ 1.0、回撤 ≤ max_dd（不看样本外）。
    相关系数太高的去重在 build() 里做（要用到资金曲线）。"""
    by_name = {r["strategy"]: r for r in rows if r.get("variant") == "default"}
    cands = []
    for r in rows:
        if r.get("variant") != "default":
            continue
        tr = r.get("train")
        ok = bool(tr) and tr["trades"] > 0 and tr["profit_total"] > 0 \
            and tr["profit_factor"] is not None and tr["profit_factor"] >= SELECT_MIN_PF and tr["dd_worst"] <= max_dd
        cands.append({
            "strategy": r["strategy"], "picked_by_rule": ok,
            "train_profit": (tr or {}).get("profit_total"), "train_pf": (tr or {}).get("profit_factor"),
            "train_dd": (tr or {}).get("dd_worst"), "train_trades": (tr or {}).get("trades"),
        })
    missing: list[str] = []
    if explicit:
        picked = []
        for n in explicit:
            r = by_name.get(n)
            if r is None:
                missing.append(f"{n}（这次研究里没有这个策略）")
            elif r.get("test") is None and r.get("train") is None:
                missing.append(f"{n}（回测没有结果）")
            else:
                picked.append(n)
        how = "explicit"
    else:
        picked = [c["strategy"] for c in cands if c["picked_by_rule"]]
        how = "rule"
    return {"how": how, "explicit": explicit, "selected": picked, "missing": missing, "candidates": cands,
            "rows": [by_name[n] for n in picked]}


def _train_score(row: dict[str, Any]) -> tuple[float, float]:
    """训练段 收益/回撤 比（越大越好），一样时比收益。"""
    tr = row.get("train") or {}
    p = float(tr.get("profit_total") or 0.0)
    dd = float(tr.get("dd_worst") or 0.0)
    return (p / dd if dd > 1e-9 else (math.inf if p > 0 else 0.0)), p


def drop_correlated(plan: dict[str, Any], rows: list[dict[str, Any]], mem: list[dict[str, Any]]
                    ) -> tuple[list[int], list[dict[str, Any]]]:
    """默认挑选规则的去重：训练段日收益相关系数 ≥ HIGH_CORR 的两个策略基本是同一个（例如 BigTrendShort 和
    BigTrendShortLowRisk），只留训练段 收益/回撤 比更好的那个。只用训练段。返回 (留下的下标, 去掉的说明)。"""
    win = _segment_window(plan, "train")
    if win is None or len(rows) < 2:
        return list(range(len(rows))), []
    t_start, t_end = _day_ts(win[0]), _day_ts(win[1]) + DAY
    rets = [daily_returns(daily_values(*_unit(m["curve"]), t_start, t_end)) for m in mem]
    order = sorted(range(len(rows)), key=lambda i: _train_score(rows[i]), reverse=True)
    kept: list[int] = []
    dropped: list[dict[str, Any]] = []
    for i in order:
        hit = None
        for j in kept:
            c = corr_matrix([rets[i], rets[j]])[0][1]
            if c is not None and c >= HIGH_CORR:
                hit = (j, c)
                break
        if hit is None:
            kept.append(i)
        else:
            dropped.append({"strategy": rows[i]["strategy"], "like": rows[hit[0]]["strategy"], "corr": hit[1]})
    return sorted(kept), dropped


def evaluate_segment(plan: dict[str, Any], rows: list[dict[str, Any]], seg: str, weights: list[float],
                     capital: float, mem: list[dict[str, Any]] | None = None) -> dict[str, Any] | None:
    """一个区间的组合结果。mem = 各策略这一段的 _member_segment（已经算过就传进来，省得再读一遍K线）。"""
    win = _segment_window(plan, seg)
    if win is None:
        return None
    t_start, t_end = _day_ts(win[0]), _day_ts(win[1]) + DAY
    days = (t_end - t_start) / DAY
    if mem is None:
        mem = [_member_segment(plan, r, seg) for r in rows]
    scales = [capital * w / m["start"] if m["start"] > 0 else 0.0 for w, m in zip(weights, mem)]
    comb = combine([m["curve"] for m in mem], scales, t_start, t_end)
    profits = [i["profit"] * k for m, k in zip(mem, scales) for i in m["infos"]]
    closes = [(t, p * k) for m, k in zip(mem, scales) for (t, p) in m["curve"]["closes"]]
    dd_mtm = max_dd(comb["low"], comb["end"], comb["start"])
    dd_closed = closed_dd(closes, comb["start"])
    total = comb["final"] / comb["start"] - 1 if comb["start"] > 0 else 0.0
    dv = daily_values(comb["G"], comb["g"], comb["end"], comb["start"], t_start, t_end)
    dr = daily_returns(dv)
    sub_dr = [daily_returns(daily_values(comb["G"], comb["g"], se, ss, t_start, t_end))
              for se, ss in zip(comb["sub_end"], comb["sub_start"])]
    methods = sorted({m["curve"]["method"] for m in mem if m["curve"]["method"]})
    cg = cagr(total, days)
    dd_worst = max(dd_mtm, dd_closed)
    levs = [m["leverage_max"] for m in mem if m["leverage_max"] is not None]
    out: dict[str, Any] = {
        "segment": seg, "start": win[0], "end": win[1], "days": days,
        "capital": capital, "final": comb["final"], "profit_total": total, "profit_abs": comb["final"] - comb["start"],
        "cagr": cg, "dd_mtm": dd_mtm, "dd_closed": dd_closed, "dd_worst": dd_worst,
        "dd_mtm_method": "、".join(methods), "profit_factor": profit_factor(profits), "trades": len(profits),
        "sharpe": sharpe(dr), "calmar": (cg / dd_worst) if (cg is not None and dd_worst > 0) else None,
        "leverage_max": max(levs) if levs else None,
        "corr": corr_matrix(sub_dr),
        "folds_missing": {r["strategy"]: m["folds_missing"] for r, m in zip(rows, mem) if m["folds_missing"]},
        "no_result": [r["strategy"] for r, m in zip(rows, mem) if not m["ok"]],
        "members": [],
    }
    for r, m, w, k in zip(rows, mem, weights, scales):
        c = m["curve"]
        own_ret = c["final"] / c["start"] - 1 if c["start"] > 0 else 0.0
        own_dr = daily_returns(daily_values(*_unit(c), t_start, t_end))  # 缩放不改变日收益率
        out["members"].append({
            "strategy": r["strategy"], "weight": w, "capital": capital * w, "has_result": m["ok"],
            "return": own_ret, "contrib_abs": (c["final"] - c["start"]) * k,
            "contrib_pct": (c["final"] - c["start"]) * k / capital if capital > 0 else 0.0,
            "dd_mtm": curve_max_dd(c), "dd_closed": closed_dd(c["closes"], c["start"]),
            "dd_mtm_method": c["method"], "trades": len(m["infos"]),
            "profit_factor": profit_factor([i["profit"] for i in m["infos"]]),
            "vol_daily": vol(own_dr), "sharpe": sharpe(own_dr),
            "leverage_max": m["leverage_max"],
            "_infos": m["infos"], "_scale": k, "_stoploss": m["stoploss"],
        })
    if seg == "wf":
        rets = []
        for f in plan["wf"]["folds"]:
            a, b = _day_ts(f["test"][0]), _day_ts(f["test"][1]) + DAY
            va, vb = value_at(comb["G"], comb["g"], comb["end"], comb["start"], [a, b])
            rets.append(float(vb / va - 1) if va > 0 else None)
        out["fold_returns"] = rets
    return out


def _unit(c: dict[str, Any]) -> tuple[Any, int, Any, float]:
    """一条曲线自己（不缩放）放到它自己的时间轴上：(G, g, end, start)，给 daily_values 用。"""
    import numpy as np

    if len(c["grid"]) == 0:
        return np.zeros(0, dtype="int64"), c["g"], np.zeros(0), c["start"]
    return c["grid"], c["g"], c["end"], c["start"]


def strategy_vols(plan: dict[str, Any], mem: list[dict[str, Any]], seg: str = "train") -> list[float | None]:
    """每个策略自己在某一段（默认训练段）的日收益波动率（不缩放，和权重无关）。mem = 各策略这一段的 _member_segment。"""
    win = _segment_window(plan, seg)
    if win is None:
        return [None] * len(mem)
    t_start, t_end = _day_ts(win[0]), _day_ts(win[1]) + DAY
    return [vol(daily_returns(daily_values(*_unit(m["curve"]), t_start, t_end))) if m["ok"] else None for m in mem]


def notional_check(seg_res: dict[str, Any], capital: float) -> dict[str, Any]:
    """按子账户资金缩放后的每笔名义仓位，和币安最小下单额比（估算）。"""
    out: dict[str, Any] = {"members": [], "capital_needed_95": None, "capital_needed_all": None}
    need95, needall = [], []
    for m in seg_res["members"]:
        reserve = min_stake_reserve(m["_stoploss"])
        nots, ratios = [], []
        counts = {"ok": 0, "bump": 0, "skip": 0}
        short_pairs: dict[str, int] = {}
        for i in m["_infos"]:
            full = i["amount"] * i["open_rate"] * m["_scale"]  # 这个子账户里这一笔的名义仓位
            need = MIN_NOTIONAL.get(i["pair"], MIN_NOTIONAL_DEFAULT) * reserve
            nots.append(full)
            kind = classify_notional(full, need)
            counts[kind] += 1
            if kind != "ok":
                coin = i["pair"].split("/")[0]
                short_pairs[coin] = short_pairs.get(coin, 0) + 1
            if full > 0:
                ratios.append(need / full)  # 总资金至少要放大到现在的几倍，这一笔才够
        q95 = _quantile(ratios, 0.95)
        qall = max(ratios) if ratios else None
        out["members"].append({
            "strategy": m["strategy"], "capital": m["capital"], "trades": len(nots), "reserve": reserve,
            "notional_median": _quantile(nots, 0.5), "notional_min": min(nots) if nots else None,
            "ok": counts["ok"], "bump": counts["bump"], "skip": counts["skip"],
            "short_pairs": dict(sorted(short_pairs.items(), key=lambda kv: -kv[1])),
            "capital_needed_95": capital * q95 if q95 is not None else None,
            "capital_needed_all": capital * qall if qall is not None else None,
        })
        if q95 is not None:
            need95.append(capital * q95)
        if qall is not None:
            needall.append(capital * qall)
    out["capital_needed_95"] = max(need95) if need95 else None
    out["capital_needed_all"] = max(needall) if needall else None
    return out


def reference_correlations(plan: dict[str, Any], rows: list[dict[str, Any]],
                           cache: dict[str, dict[str, Any]] | None = None) -> dict[str, Any]:
    """每个候选策略（不是参考策略的，默认参数那一行）和参考趋势策略（REF_TREND 里这次有结果的）的训练段日收益相关系数。
    只报告，不参与挑选和判定。cache = {策略名: 训练段 _member_segment}（已经算过的就不再读K线）。
    日收益：每个策略自己的资金曲线（不缩放）在训练段（plan.json 的 train，合并时按第一个运行目录）每天 00:00 UTC 的权益。"""
    by_name: dict[str, dict[str, Any]] = {}
    for r in rows:
        if r.get("variant") == "default" and r["strategy"] not in by_name:
            by_name[r["strategy"]] = r
    refs = [n for n in REF_TREND if n in by_name]
    cands = [n for n in by_name if n not in REF_TREND]
    out: dict[str, Any] = {"refs": refs, "candidates": cands, "matrix": [], "trades": {}, "skip": ""}
    win = _segment_window(plan, "train")
    if not refs:
        out["skip"] = f"这次没有参考策略（{'、'.join(REF_TREND)}）的结果"
        return out
    if not cands:
        out["skip"] = "这次只有参考策略，没有别的候选"
        return out
    if win is None:
        out["skip"] = "没有训练段"
        return out
    t_start, t_end = _day_ts(win[0]), _day_ts(win[1]) + DAY
    cache = cache if cache is not None else {}
    rets: dict[str, Any] = {}
    for n in refs + cands:
        r = by_name[n]
        if r.get("train") is None:
            rets[n] = None
            continue
        m = cache.get(n)
        if m is None:
            m = cache[n] = _member_segment(plan, r, "train")
        out["trades"][n] = len(m["infos"])
        rets[n] = daily_returns(daily_values(*_unit(m["curve"]), t_start, t_end))
    for c in cands:
        line = []
        for f in refs:
            if rets[c] is None or rets[f] is None:
                line.append(None)
            else:
                line.append(corr_matrix([rets[c], rets[f]])[0][1])
        out["matrix"].append(line)
    out["window"] = [win[0], win[1]]
    return out


def judge(res: dict[str, Any], s: dict[str, Any], wf_enabled: bool) -> list[str]:
    """和 research_report.judge 同一套标准，原因前面加"组合"。"""
    max_dd = cd.settings_max_dd_pct(s) / 100
    max_lev = float(s.get("max_leverage", 5.0))
    min_pf = float(s.get("min_pf", 1.2))
    min_trades = int(s.get("min_trades", 30))
    ratio = float(s.get("pf_ratio", 0.6))
    tr, te, wf = res["segments"].get("train"), res["segments"].get("test"), res["segments"].get("wf")
    ho = res["segments"].get("holdout")
    reasons: list[str] = []
    if te is None:
        reasons.append("组合样本外没有结果")
    else:
        if te["trades"] == 0:
            reasons.append("组合样本外一笔交易都没有")
        if te["dd_worst"] > max_dd:
            reasons.append(f"组合样本外回撤 {_pct(te['dd_worst'])} 超过 {max_dd * 100:g}%")
        if te["trades"] > 0 and te["profit_total"] <= 0:
            reasons.append(f"组合样本外亏损 {_pct(te['profit_total'], True)}")
        if te["trades"] > 0 and (te["profit_factor"] is None or te["profit_factor"] < min_pf):
            reasons.append(f"组合样本外盈亏因子 {_num(te['profit_factor'])} < {min_pf:g}")
        if 0 < te["trades"] < min_trades:
            reasons.append(f"组合样本外只有 {te['trades']} 笔交易（< {min_trades}）")
        if te["leverage_max"] is not None and te["leverage_max"] > max_lev + 1e-9:
            reasons.append(f"杠杆 {te['leverage_max']:g}x 超过 {max_lev:g}x")
        if te["no_result"]:
            reasons.append(f"样本外缺 {'、'.join(te['no_result'])} 的结果（按一直空仓算了）")
    if ho is not None:
        if ho["trades"] == 0:
            reasons.append("组合在没看过的一段一笔交易都没有")
        elif ho["profit_total"] <= 0:
            reasons.append(f"组合在没看过的一段（{ho['start']}~{ho['end']}）亏损 {_pct(ho['profit_total'], True)}")
        if ho["dd_worst"] > max_dd:
            reasons.append(f"组合在没看过的一段回撤 {_pct(ho['dd_worst'])} 超过 {max_dd * 100:g}%")
        if ho["no_result"]:
            reasons.append(f"没看过的一段缺 {'、'.join(ho['no_result'])} 的结果（按一直空仓算了）")
    elif (res.get("_plan_holdout")):
        reasons.append("组合在没看过的一段没有结果")
    if tr is None:
        reasons.append("组合训练段没有结果")
    else:
        if tr["trades"] == 0:
            reasons.append("组合训练段一笔交易都没有")
        elif tr["profit_total"] <= 0:
            reasons.append(f"组合训练段本身就亏损 {_pct(tr['profit_total'], True)}")
        if tr["dd_worst"] > max_dd:
            reasons.append(f"组合训练段回撤 {_pct(tr['dd_worst'])} 超过 {max_dd * 100:g}%")
        if tr["no_result"]:
            reasons.append(f"训练段缺 {'、'.join(tr['no_result'])} 的结果（按一直空仓算了）")
        tpf, opf = tr["profit_factor"], (te or {}).get("profit_factor")
        if te is not None and tpf is not None and not math.isinf(tpf) and tpf > 0 and opf is not None \
                and not math.isinf(opf) and opf < ratio * tpf:
            reasons.append(f"组合样本外明显退化：盈亏因子 训练 {tpf:.2f} → 样本外 {opf:.2f}（不到训练段的 {ratio:g} 倍）")
    if wf_enabled:
        if wf is None:
            reasons.append("组合走步没有结果")
        else:
            if wf["dd_worst"] > max_dd:
                reasons.append(f"组合走步回撤 {_pct(wf['dd_worst'])} 超过 {max_dd * 100:g}%")
            if wf["profit_total"] <= 0:
                reasons.append(f"组合走步亏损 {_pct(wf['profit_total'], True)}")
            if wf["trades"] > 0 and (wf["profit_factor"] is None or wf["profit_factor"] < min_pf):
                reasons.append(f"组合走步盈亏因子 {_num(wf['profit_factor'])} < {min_pf:g}")
            if wf["trades"] < min_trades:
                reasons.append(f"组合走步只有 {wf['trades']} 笔交易（< {min_trades}）")
            if wf["folds_missing"]:
                reasons.append("走步有折没有结果：" + "、".join(f"{k} 缺 {v} 折" for k, v in wf["folds_missing"].items()))
    return reasons


# ----------------------------------------------------------------------------
# 总入口
# ----------------------------------------------------------------------------
def build(plan: dict[str, Any], rows: list[dict[str, Any]], strategies: Any = None, weights_mode: str | None = None,
          capital: float = DEFAULT_CAPITAL) -> dict[str, Any]:
    s = plan.get("settings") or {}
    if strategies is None:
        strategies = s.get("portfolio") or os.environ.get("RESEARCH_PORTFOLIO", "")
    mode = (weights_mode or s.get("portfolio_weights") or os.environ.get("RESEARCH_PORTFOLIO_WEIGHTS") or "equal").lower()
    mode = mode.replace("-", "").replace("_", "")
    if mode not in WEIGHTS_CN:
        raise ValueError(f"权重方式只能是 equal 或 riskparity（收到：{mode}）")
    max_dd = cd.settings_max_dd_pct(s) / 100
    sel = select(rows, parse_names(strategies), max_dd)
    res: dict[str, Any] = {
        "capital": capital, "weights_mode": mode, "selection": {k: v for k, v in sel.items() if k != "rows"},
        "segments": {}, "verdict": "SKIP", "reasons": [], "notes": [],
    }
    if plan.get("_merged"):
        res["merged"] = plan["_merged"]
    else:
        res["prereg"] = _formal_of_plan(plan, rows)
    members = sel["rows"]
    train_mem = [_member_segment(plan, r, "train") for r in members]
    # 和参考趋势策略的相关系数（只报告）：已经算过的训练段曲线直接用
    res["ref_corr"] = reference_correlations(plan, rows, {r["strategy"]: m for r, m in zip(members, train_mem)})
    # 第 5 轮（预注册第 3.4 节）：登记过的策略即使是指定的（--strategies），也按训练段相关系数 ≥ 0.7 去重；
    # 没有登记的策略指定时照旧不去重（和以前一样），只在下面提醒训练段高度相关
    registered = [r["strategy"] for r in members if (r.get("rules") or {}).get("registered")]
    dedupe = len(members) >= 2 and (sel["how"] == "rule" or bool(registered))
    res["selection"]["dedupe"] = dedupe
    res["selection"]["registered"] = registered
    if dedupe:
        keep, dropped = drop_correlated(plan, members, train_mem)
        res["selection"]["dropped_corr"] = dropped
        members = [members[i] for i in keep]
        train_mem = [train_mem[i] for i in keep]
        sel["selected"] = [r["strategy"] for r in members]
        res["selection"]["selected"] = sel["selected"]
    n = len(members)
    if n == 0 or (sel["how"] == "rule" and n < 2):
        res["skip_reason"] = (
            "没有指定策略，也没有策略符合默认挑选条件" if n == 0 else
            "符合默认挑选条件的策略（去掉高度相关的以后）只有 1 个，组合不了"
        )
        return res
    mem_cache = {seg: ([_member_segment(plan, r, seg) for r in members] if seg != "train" else train_mem)
                 for seg in SEG_ORDER if _segment_window(plan, seg) is not None}
    vols = strategy_vols(plan, mem_cache["train"], "train")
    rp = risk_parity(vols)
    res["train_vol"] = dict(zip(sel["selected"], vols))
    res["riskparity_weights"] = dict(zip(sel["selected"], rp)) if rp else None
    weights = [1.0 / n] * n
    if mode == "riskparity":
        if rp:
            weights = rp
        else:
            bad = [nm for nm, v in zip(sel["selected"], vols) if v is None]
            res["weights_fallback"] = f"要求风险平价，但 {'、'.join(bad)} 训练段没有交易（算不出波动率），改用平均分"
            res["weights_mode_used"] = "equal"
    res.setdefault("weights_mode_used", mode)
    res["weights"] = dict(zip(sel["selected"], weights))
    if res["weights_mode_used"] == "riskparity":
        few = [f"{nm}（{len(m['infos'])} 笔）" for nm, m in zip(sel["selected"], mem_cache["train"])
               if len(m["infos"]) < int(s.get("min_trades", 30))]
        if few:
            res["notes"].append(
                f"风险平价：{'、'.join(few)} 训练段交易很少，波动率低多半是因为大部分时间空仓，不代表风险小，分到的钱可能偏多"
            )
    wf_enabled = bool((plan.get("wf") or {}).get("folds"))
    res["_plan_holdout"] = bool(plan.get("holdout"))
    for seg in SEG_ORDER:
        if seg == "wf" and not wf_enabled:
            continue
        r = evaluate_segment(plan, members, seg, weights, capital, mem_cache.get(seg))
        if r is not None:
            res["segments"][seg] = r
    res["reasons"] = judge(res, s, wf_enabled)
    res["verdict"] = "FAIL" if res["reasons"] else "PASS"
    if "test" in res["segments"]:
        res["notional"] = notional_check(res["segments"]["test"], capital)
    if "train" in res["segments"]:
        res["notional_train"] = notional_check(res["segments"]["train"], capital)
    # 提醒
    names = sel["selected"]
    c_tr = (res["segments"].get("train") or {}).get("corr")
    c_te = (res["segments"].get("test") or {}).get("corr")
    for i in range(n):
        for j in range(i + 1, n):
            a = c_tr[i][j] if c_tr else None
            b = c_te[i][j] if c_te else None
            if not dedupe and sel["how"] == "explicit" and a is not None and a >= HIGH_CORR:
                res["notes"].append(
                    f"!! {names[i]} 和 {names[j]} 训练段日收益相关系数 {a:.2f}（≥ {HIGH_CORR:g}）：默认挑选规则会只留一个，"
                    "指定策略（--strategies）时不去重，两个都在组合里"
                )
            if b is not None and b >= HIGH_CORR:
                res["notes"].append(
                    f"{names[i]} 和 {names[j]} 样本外日收益相关系数 {b:.2f}（≥ {HIGH_CORR:g}）：基本是同涨同跌，"
                    "放在一起分散不了风险，最多留一个"
                )
            elif a is not None and b is not None and abs(a - b) >= 0.4:
                res["notes"].append(
                    f"{names[i]} 和 {names[j]} 的相关系数 训练段 {a:.2f} → 样本外 {b:.2f}，变化很大：这个\"低相关\"靠不住"
                )
    est: dict[str, list[str]] = {}
    for seg, sr in res["segments"].items():
        for m in sr["members"]:
            if str(m["dd_mtm_method"]).startswith("估"):
                est.setdefault(m["strategy"], []).append(SEG_CN[seg])
    if est:
        res["notes"].append(
            "、".join(f"{k}（{'/'.join(v)}）" for k, v in est.items())
            + " 找不到K线数据，资金曲线只按已平仓余额算（不含浮亏），组合回撤会偏小"
        )
    if sel["how"] == "rule":
        res["notes"].append(
            "这几个策略是按训练段结果挑的（默认规则），所以组合的训练段（包括其中\"没看过的一段\"）数字偏乐观；"
            "样本外和走步拼接没有参与挑选"
        )
    else:
        res["notes"].append(
            "组合成员是自己指定的：如果是看着样本外结果挑的，组合的样本外数字一样偏乐观（用同一段数据挑了再判）"
        )
    if plan.get("seen_warning"):
        res["notes"].append(
            "样本外这段以前的研究已经看过，第 3 轮的策略选择是看着它定的：组合样本外\"通过\"不是独立证据"
            + ("，以\"没看过的一段\"为准" if plan.get("holdout") else "")
        )
    if s.get("hyperopt") not in (1, "1", True) and wf_enabled:
        res["notes"].append("不调参时，走步拼接和训练段/样本外是同一段行情，不是额外的独立证据")
    return res


def _table(headers: list[tuple[str, bool]], body: list[list[str]], indent: str = "    ") -> list[str]:
    widths = [max(cd._width(h), *(cd._width(r[i]) for r in body)) if body else cd._width(h)
              for i, (h, _) in enumerate(headers)]
    sep = indent + "+" + "+".join("-" * (w + 2) for w in widths) + "+"
    out = [sep, indent + "| " + " | ".join(cd._pad(h, widths[i]) for i, (h, _) in enumerate(headers)) + " |", sep]
    for r in body:
        out.append(indent + "| " + " | ".join(cd._pad(c, widths[i], headers[i][1]) for i, c in enumerate(r)) + " |")
    out.append(sep)
    return out


def _formal_of_plan(plan: dict[str, Any], rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    """这个研究目录是不是正式的预注册运行（plan.json 的 prereg，scripts/check_prereg.py 写的）。
    里面没有登记过的策略 = None（不提）。"""
    reg = list(dict.fromkeys(r["strategy"] for r in rows if (r.get("rules") or {}).get("registered")))
    if not reg:
        return None
    pr = plan.get("prereg")
    if not isinstance(pr, dict):
        return {"strategies": reg, "formal": False, "note": "plan.json 里没有预注册凭证"}
    return {"strategies": reg, "formal": pr.get("formal_run") is True, "note": str(pr.get("formal_note") or ""),
            "allow_unregistered": bool(pr.get("allow_unregistered")), "problems": list(pr.get("problems") or [])}


def _formal_status_lines(res: dict[str, Any], plan: dict[str, Any]) -> list[str]:
    """组合成员里有登记过的策略时，写清楚来源的研究目录是不是正式的预注册运行。"""
    mg = res.get("merged")
    names = set(res.get("selection", {}).get("selected") or [])
    if mg:
        items = [(x["run_dir"], x.get("prereg")) for x in mg["runs"]]
    else:
        items = [(str(plan.get("_run_dir") or "?"), res.get("prereg"))]
    L = []
    for d, fp in items:
        if not fp or not (set(fp["strategies"]) & names):
            continue
        if fp["formal"]:
            L.append(f"正式运行：{d} 是正式的预注册运行（{'、'.join(fp['strategies'])}）")
        else:
            L.append(f"!! 不是正式的预注册运行：{d}（{'、'.join(fp['strategies'])}）：{fp['note'] or '没有记下原因'}"
                     + ("；用了 RESEARCH_ALLOW_UNREGISTERED=1" if fp.get("allow_unregistered") else "")
                     + "。这个组合结果不能当第 5 轮的正式结论用。")
    return L


def _merged_lines(res: dict[str, Any]) -> list[str]:
    """--run-dirs 合并了几个运行目录：抬头标明"跨交易所近似"（或"多个运行目录合并"），列出每个目录的交易所、策略、区间。"""
    mg = res.get("merged")
    if not mg:
        return []
    L = [f"!! {mg['label']}：组合成员来自 {len(mg['runs'])} 个运行目录（按 --run-dirs 的顺序，同名策略用前面那个）："]
    for x in mg["runs"]:
        L.append(f"   - {x['run_dir']}（{x['exchange'] or '?'}）：{'、'.join(x['strategies']) or '没有结果'}；"
                 f"训练段 {x['train'][0]}~{x['train'][1]}，样本外 {x['test'][0]}~{x['test'][1]}"
                 + (f"，没看过的一段 {x['holdout'][0]}~{x['holdout'][1]}" if x.get("holdout") else ""))
    if mg.get("cross_exchange"):
        L.append("   不同交易所的手续费、资金费、K线都不一样，子账户分在不同交易所；各自的资金曲线按时间对齐后直接相加，")
        L.append("   只是近似，不是同一个账户、同一份数据的结果。")
    L.append(f"   区间、挑选、判定按第一个目录（{mg['runs'][0]['run_dir']}）的切分；别的目录的策略在它自己的区间以外按空仓算。")
    for d in mg.get("window_diff") or []:
        L.append(f"   !! {d}")
    for d in mg.get("dups") or []:
        L.append(f"   提醒：{d}")
    return L


def _ref_corr_lines(res: dict[str, Any]) -> list[str]:
    rc = res.get("ref_corr")
    if not rc:
        return []
    if rc.get("skip"):
        return [f"和参考趋势策略的相关系数（训练段日收益，只报告）：没有算（{rc['skip']}）", ""]
    w = rc.get("window") or ["?", "?"]
    L = [f"和参考趋势策略的相关系数（训练段 {w[0]}~{w[1]} 的日收益，皮尔逊；只报告，不用来挑选，也不参与判定）："]
    heads = [("候选策略", False)] + [(f, True) for f in rc["refs"]]
    body = [[c] + ["-" if v is None else f"{v:.2f}" for v in line] for c, line in zip(rc["candidates"], rc["matrix"])]
    L.extend(_table(heads, body))
    L.append("    （\"-\" = 训练段没有结果或一笔交易都没有，算不出来；"
             + (f"{res['merged']['label']}：不同交易所的策略日收益放在一起比，" if res.get("merged") else "")
             + f"≥ {HIGH_CORR:g} 基本是同一种收益来源）")
    L.append("")
    return L


def render_lines(res: dict[str, Any], plan: dict[str, Any]) -> list[str]:
    s = plan.get("settings") or {}
    max_dd = cd.settings_max_dd_pct(s)
    cap = res["capital"]
    L: list[str] = ["================ 子账户组合检查（每个策略一个币安子账户、一个机器人，资金分开） ================"]
    if plan.get("synthetic"):
        L.append("!! 合成数据：下面的数字没有意义，只检查流程 !!")
    L.extend(_merged_lines(res))
    L.extend(_formal_status_lines(res, plan))
    L.append(
        f"做法：{cap:g} USDT 按权重分到各个子账户，每个子账户单独跑一个策略，之后子账户之间不挪钱；看所有子账户加起来的总权益。"
    )
    L.append(
        "      每个子账户的资金曲线 = 这个策略自己回测（1000 USDT）的资金曲线 × 分到的比例"
        "（策略按账户权益的比例定仓，可以直接缩放）；"
    )
    L.append("      含浮亏、逐根K线（和上面的回撤同一套算法），各子账户按小时对齐后相加。没考虑最小下单额（见下面的检查）。")
    override = cd.max_dd_override_note(s)
    if override:
        L.append(override)
    L.append(f"回撤上限：{max_dd:g}% {cd.max_dd_rule_note(max_dd)}；判的是所有子账户加起来的总权益，不是每个子账户各自的回撤。")
    sel = res["selection"]
    if sel["how"] == "explicit":
        L.append(f"挑选：按指定（RESEARCH_PORTFOLIO）：{'、'.join(sel['explicit'])}")
        if sel.get("dedupe"):
            L.append(f"      里面有登记过的第 5 轮策略（{'、'.join(sel.get('registered') or [])}）：按预注册第 3.4 节，"
                     f"训练段日收益相关系数 ≥ {HIGH_CORR:g} 的两个只留一个（留训练段 收益/回撤 比更好的）")
            for d in sel.get("dropped_corr") or []:
                L.append(f"      去掉 {d['strategy']}：和 {d['like']} 训练段日收益相关系数 {d['corr']:.2f}（≥ {HIGH_CORR:g}），"
                         f"留 {d['like']}")
            if not sel.get("dropped_corr"):
                L.append("      没有去掉任何策略（两两之间训练段相关系数都 < " + f"{HIGH_CORR:g}，或者算不出来）")
    else:
        L.append(
            f"挑选：默认参数那一行 训练段赚钱、训练段盈亏因子 ≥ {SELECT_MIN_PF:.1f}、训练段回撤 ≤ {max_dd:g}% 的策略"
            f"（接近不赚不亏的也算；只看训练段，样本外留给判定），训练段日收益相关系数 ≥ {HIGH_CORR:g} 的两个只留一个；"
            "自己指定：RESEARCH_PORTFOLIO=\"A,B,C\""
        )
        picked = [c["strategy"] for c in sel["candidates"] if c["picked_by_rule"]]
        others = [
            f"{c['strategy']}（训练段 {_pct(c['train_profit'], True)}、PF {_num(c['train_pf'])}、回撤 {_pct(c['train_dd'])}）"
            for c in sel["candidates"] if not c["picked_by_rule"]
        ]
        L.append(f"      符合条件：{'、'.join(picked) if picked else '没有'}")
        for d in sel.get("dropped_corr") or []:
            L.append(
                f"      去掉 {d['strategy']}：和 {d['like']} 训练段日收益相关系数 {d['corr']:.2f}（≥ {HIGH_CORR:g}），"
                f"基本是同一个策略，留训练段 收益/回撤 比更好的 {d['like']}"
            )
        if others:
            L.append(f"      不符合：{'；'.join(others)}")
    for m in sel["missing"]:
        L.append(f"      !! 没法放进组合：{m}")
    if res["verdict"] == "SKIP":
        L.extend(_ref_corr_lines(res))
        L.append(f"结论：没有做（{res.get('skip_reason')}）。")
        L.append("      想看某几个策略合起来怎么样，可以自己指定，例如 make research RESEARCH_PORTFOLIO=\"BigTrendShortLowRisk,TsmomTrend\" ARGS=\"--no-download\"，")
        L.append("      或者不重跑回测：.venv/bin/python scripts/research_portfolio.py --run-dir <这次的运行目录> --strategies \"A,B\"")
        return L

    names = sel["selected"]
    mode_used = res.get("weights_mode_used", res["weights_mode"])
    wtxt = " / ".join(f"{nm} {res['weights'][nm]:.0%}（{cap * res['weights'][nm]:.0f} USDT）" for nm in names)
    L.append(f"权重：{WEIGHTS_CN[mode_used]}：{wtxt}")
    if res.get("weights_fallback"):
        L.append(f"      !! {res['weights_fallback']}")
    rp = res.get("riskparity_weights")
    if mode_used == "equal" and rp:
        L.append(
            "      换成风险平价（按训练段日收益波动率的倒数分，只用训练段）会是："
            + " / ".join(f"{nm} {rp[nm]:.0%}" for nm in names)
            + "（RESEARCH_PORTFOLIO_WEIGHTS=riskparity）"
        )
    L.append("")

    segs = res["segments"]
    headers = [("区间", False), ("日期", False), ("总收益", True), ("年化", True), ("回撤(含浮亏)", True),
               ("回撤(平仓)", True), ("盈亏因子", True), ("交易", True), ("Sharpe", True), ("Calmar", True)]
    body = []
    for seg in SEG_ORDER:
        r = segs.get(seg)
        if not r:
            continue
        body.append([
            SEG_CN[seg], f"{r['start']}~{r['end']}", _pct(r["profit_total"], True), _pct(r["cagr"], True),
            _pct(r["dd_mtm"]) + (f" {r['dd_mtm_method']}" if r["dd_mtm_method"] else ""), _pct(r["dd_closed"]),
            _num(r["profit_factor"]), str(r["trades"]), _num(r["sharpe"]), _num(r["calmar"]),
        ])
    L.append(f"组合（{len(names)} 个子账户合计 {cap:g} USDT）：")
    L.extend(_table(headers, body))
    wf = segs.get("wf")
    if wf and wf.get("fold_returns"):
        fr = wf["fold_returns"]
        good = sum(1 for x in fr if x is not None and x > 0)
        L.append(f"    走步每折组合收益：{' / '.join(_pct(x, True) for x in fr)}（{good}/{len(fr)} 折赚钱）")
    L.append("")

    # 各子账户
    L.append(f"各子账户（\"贡献\" = 这个子账户赚/亏的钱占总资金 {cap:g} USDT 的百分比，几个子账户的贡献加起来 = 组合总收益；"
             "\"收益\"\"回撤\"是子账户自己的，和上面排名表里这个策略的数字相同）：")
    headers = [("#", True), ("策略", False), ("权重", True), ("资金", True)]
    for seg in SEG_ORDER:
        if seg in segs:
            headers += [(f"{SEG_CN[seg]}收益", True), (f"{SEG_CN[seg]}贡献", True), (f"{SEG_CN[seg]}回撤", True),
                        (f"{SEG_CN[seg]}交易", True)]
    body = []
    for i, nm in enumerate(names):
        row = [str(i + 1), nm, f"{res['weights'][nm]:.0%}", f"{cap * res['weights'][nm]:.0f}"]
        for seg in SEG_ORDER:
            if seg in segs:
                m = segs[seg]["members"][i]
                row += [_pct(m["return"], True), _pct(m["contrib_pct"], True),
                        _pct(max(m["dd_mtm"], m["dd_closed"])), str(m["trades"])]
        body.append(row)
    L.extend(_table(headers, body))
    L.append("")

    # 相关系数
    L.append(f"子账户日收益相关系数（越接近 1 越是同涨同跌、分散不了风险；接近 0 或负数才有分散效果；≥ {HIGH_CORR:g} 算高）：")
    for seg in SEG_ORDER:
        r = segs.get(seg)
        if not r:
            continue
        L.append(f"    {SEG_CN[seg]}：")
        heads = [("", False)] + [(f"[{j + 1}]", True) for j in range(len(names))]
        body = [[f"[{i + 1}] {nm}"] + ["-" if v is None else f"{v:.2f}" for v in r["corr"][i]] for i, nm in enumerate(names)]
        L.extend(_table(heads, body, indent="      "))
    L.append("    （\"-\" = 这一段这个子账户一笔交易都没有，算不出来）")
    L.append("")
    L.extend(_ref_corr_lines(res))

    # 最小下单额
    nc = res.get("notional")
    if nc:
        L.append(
            f"最小下单额检查（样本外，估算）：币安 U 本位合约每笔名义金额（数量 × 价格）大部分币至少 {MIN_NOTIONAL_DEFAULT:g} USDT，"
            "BTC 约 100、ETH 约 20 USDT（以币安当时的规则为准）；"
        )
        rs = sorted({round(m["reserve"], 2) for m in nc["members"]})
        rtxt = f"{rs[0]:g} 倍" if len(rs) == 1 else f"{rs[0]:g} ~ {rs[-1]:g} 倍"
        L.append(
            f"      freqtrade 还会在这上面多留余量（这几个策略是 {rtxt}），差得不多时把仓位放大最多 30% 去凑，再不够就不开这一笔。"
        )
        L.append("      每笔仓位按回测里的实际仓位缩放到子账户资金（仓位 ≈ 账户 × 每笔风险 ÷ 止损距离）：")
        for m in nc["members"]:
            if m["trades"] == 0:
                L.append(f"    - {m['strategy']}（子账户 {m['capital']:.0f} USDT）：样本外没有交易")
                continue
            line = (
                f"    - {m['strategy']}（子账户 {m['capital']:.0f} USDT）：每笔名义仓位 中位数 {m['notional_median']:.1f} USDT、"
                f"最小 {m['notional_min']:.1f} USDT；{m['trades']} 笔里 "
            )
            if m["bump"] == 0 and m["skip"] == 0:
                line += "都够最小下单额"
            else:
                coins = "、".join(f"{c} {k} 笔" for c, k in list(m["short_pairs"].items())[:5])
                bump = f"{m['bump']} 笔会被放大（实际风险比设计的大）" if m["bump"] else "0 笔会被放大"
                line += f"{m['skip']} 笔开不出来、{bump}，只有 {m['ok']} 笔原样下单（不够的：{coins}）"
            L.append(line)
        if nc["capital_needed_95"] is not None:
            bad = sum(m["skip"] + m["bump"] for m in nc["members"])
            verdict = "现在的资金够" if nc["capital_needed_95"] <= cap else f"现在 {cap:g} USDT 不够"
            L.append(
                f"    → 按现在的权重，要让每个子账户 95% 的交易都够最小下单额，总资金至少约 {nc['capital_needed_95']:.0f} USDT"
                f"（{verdict}；每一笔都够：约 {nc['capital_needed_all']:.0f} USDT）"
            )
            if bad:
                L.append(
                    "    !! 有交易会开不出来或被放大：上面的组合结果比实盘乐观/不准。上线前按子账户的实际资金单独回测一次"
                    "（--dry-run-wallet 填子账户资金）再确认"
                )
        L.append("    （只按最小名义金额估算；合约数量还要按交易所的步长取整，小仓位时误差更大。")
        L.append("     只看得到 1000 USDT 回测里开出来的交易：1000 USDT 时就已经开不出来的那几笔这里看不到，"
                 "见排名表说明里 XSMomentum 的\"最小下单额\"提醒和 BigTrendShortLowRisk 的逐笔对比）")
        L.append("")

    verdict = {"PASS": "通过", "FAIL": "未通过"}[res["verdict"]]
    if res.get("merged"):
        verdict += f"（{res['merged']['label']}）"
    L.append(f"组合结论：{verdict}（标准和单个策略相同：样本外和走步拼接 回撤 ≤ {max_dd:g}%、赚钱、盈亏因子 ≥ "
             f"{float(s.get('min_pf', 1.2)):g}、交易 ≥ {int(s.get('min_trades', 30))} 笔；训练段赚钱、回撤 ≤ {max_dd:g}%；"
             f"样本外盈亏因子不低于训练段的 {float(s.get('pf_ratio', 0.6)):g} 倍"
             + (f"；没看过的一段赚钱、回撤 ≤ {max_dd:g}%" if "holdout" in segs else "") + "）")
    for x in res["reasons"]:
        L.append(f"    未通过：{x}")
    for x in dict.fromkeys(res["notes"]):
        L.append(f"    提醒：{x}")
    return L


CSV_FIELDS = [
    "segment", "start", "end", "item", "weight", "capital", "return", "contrib_pct", "contrib_abs", "cagr",
    "dd_mtm", "dd_closed", "dd_worst", "dd_method", "profit_factor", "trades", "sharpe", "calmar", "leverage_max",
]


def csv_rows(res: dict[str, Any]) -> list[dict[str, Any]]:
    out = []
    for seg, r in res.get("segments", {}).items():
        out.append({
            "segment": seg, "start": r["start"], "end": r["end"], "item": "组合", "weight": 1.0, "capital": r["capital"],
            "return": r["profit_total"], "contrib_pct": r["profit_total"], "contrib_abs": r["profit_abs"],
            "cagr": r["cagr"], "dd_mtm": r["dd_mtm"], "dd_closed": r["dd_closed"], "dd_worst": r["dd_worst"],
            "dd_method": r["dd_mtm_method"], "profit_factor": r["profit_factor"], "trades": r["trades"],
            "sharpe": r["sharpe"], "calmar": r["calmar"], "leverage_max": r["leverage_max"],
        })
        for m in r["members"]:
            out.append({
                "segment": seg, "start": r["start"], "end": r["end"], "item": m["strategy"], "weight": m["weight"],
                "capital": m["capital"], "return": m["return"], "contrib_pct": m["contrib_pct"],
                "contrib_abs": m["contrib_abs"], "cagr": None, "dd_mtm": m["dd_mtm"], "dd_closed": m["dd_closed"],
                "dd_worst": max(m["dd_mtm"], m["dd_closed"]), "dd_method": m["dd_mtm_method"],
                "profit_factor": m["profit_factor"], "trades": m["trades"], "sharpe": m["sharpe"], "calmar": None,
                "leverage_max": m["leverage_max"],
            })
    for r in out:
        for k, v in r.items():
            if isinstance(v, float) and math.isinf(v):
                r[k] = "inf"
    return out


def write_outputs(run_dir: Path, res: dict[str, Any], lines: list[str], stem: str = "portfolio") -> None:
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / f"{stem}.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    with (run_dir / f"{stem}.csv").open("w", newline="", encoding="utf-8-sig") as fh:  # utf-8-sig：Excel 认中文
        w = csv.DictWriter(fh, fieldnames=CSV_FIELDS)
        w.writeheader()
        for r in csv_rows(res):
            w.writerow(r)
    names = res["selection"]["selected"]
    data = dict(res)
    data["segments"] = {k: {**v, "corr": {"names": names, "matrix": v["corr"]}} for k, v in res.get("segments", {}).items()}
    (run_dir / f"{stem}.json").write_text(
        json.dumps(_clean(data), ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8"
    )


def report_lines(plan: dict[str, Any], rows: list[dict[str, Any]], strategies: Any = None,
                 weights_mode: str | None = None, write: bool = True) -> list[str]:
    """research_report.render 调用：返回要加进 summary.txt 的几行，并写 portfolio.txt/csv/json。出错不影响排名表。"""
    try:
        res = build(plan, rows, strategies, weights_mode)
        lines = render_lines(res, plan)
        if write and plan.get("_run_dir"):
            write_outputs(Path(plan["_run_dir"]), res, lines)
            lines.append("    （明细：portfolio.txt / portfolio.csv / portfolio.json）")
        return lines
    except Exception as exc:  # noqa: BLE001 —— 组合检查出错不能让整张排名表出不来
        fr = traceback.extract_tb(exc.__traceback__)[-1] if exc.__traceback__ else None
        where = f"{Path(fr.filename).name} 第 {fr.lineno} 行" if fr else "?"
        return [
            "================ 子账户组合检查 ================",
            f"!! 组合检查出错，没有结果（排名表不受影响）：{exc.__class__.__name__}: {exc}（{where}）",
            "   可以单独重算：.venv/bin/python scripts/research_portfolio.py --run-dir <运行目录>",
        ]


# ----------------------------------------------------------------------------
# 合并多个运行目录（第 5 轮：XSFundingCarry 在币安那台服务器上跑，其他策略在 OKX 上跑）
# ----------------------------------------------------------------------------
_WINDOW_KEYS = (("train", "训练段"), ("test", "样本外"), ("holdout", "没看过的一段"))


def merge_runs(loaded: list[tuple[dict[str, Any], list[dict[str, Any]]]]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """loaded = [(plan, rows)]，按 --run-dirs 的顺序（plan 里要有 _run_dir）。返回 (合并后的 plan, 合并后的 rows)。

    合并后的 plan = 第一个目录的 plan（区间切分、settings、挑选和判定都按它），加 _merged（每个目录的交易所、策略、区间，
    区间不一样的地方，重名被跳过的策略，标签"跨交易所近似"/"多个运行目录合并（近似）"）。
    每一行带上 _plan = 它自己那次研究的 plan（读K线用它自己的数据目录）。同一个策略（同一个参数版本）在几个目录里都有时用前面那个。"""
    if not loaded:
        raise ValueError("没有运行目录")
    primary = loaded[0][0]
    runs, out_rows, dups, wdiff = [], [], [], []
    seen: dict[tuple[str, str], str] = {}
    for plan, rows in loaded:
        name = Path(str(plan.get("_run_dir") or "?")).name
        kept = []
        for r in rows:
            key = (r["strategy"], r.get("variant", "default"))
            if key in seen:
                if r.get("variant", "default") == "default":
                    dups.append(f"{r['strategy']} 在 {seen[key]} 和 {name} 里都有，用 {seen[key]} 的")
                continue
            seen[key] = name
            r["_plan"] = plan
            kept.append(r)
            out_rows.append(r)
        runs.append({
            "run_dir": str(plan.get("_run_dir") or "?"), "exchange": str(plan.get("exchange") or ""),
            "strategies": list(dict.fromkeys(r["strategy"] for r in kept)),
            "train": list(plan["train"]), "test": list(plan["test"]),
            "holdout": list(plan["holdout"]) if plan.get("holdout") else None,
            "synthetic": bool(plan.get("synthetic")),
            "prereg": _formal_of_plan(plan, kept),
        })
        if plan is not primary:
            for k, cn in _WINDOW_KEYS:
                a, b = primary.get(k), plan.get(k)
                if (list(a) if a else None) != (list(b) if b else None):
                    wdiff.append(f"{name} 的{cn}（{'~'.join(b) if b else '没有'}）和第一个目录（{'~'.join(a) if a else '没有'}）不一样")
            fa = [f["test"] for f in (primary.get("wf") or {}).get("folds") or []]
            fb = [f["test"] for f in (plan.get("wf") or {}).get("folds") or []]
            if fa != fb:
                wdiff.append(f"{name} 的走步各折和第一个目录不一样：走步拼接按各自的折算，每折收益按第一个目录的折切")
            for key in ("fee", "max_dd"):
                va, vb = (primary.get("settings") or {}).get(key), (plan.get("settings") or {}).get(key)
                if va != vb:
                    wdiff.append(f"{name} 的 {key} = {vb}，第一个目录 = {va}（判定按第一个目录）")
    exchanges = list(dict.fromkeys(x["exchange"] for x in runs))
    cross = len(exchanges) > 1
    merged = dict(primary)
    merged["_merged"] = {
        "label": CROSS_EXCHANGE_LABEL if cross else "多个运行目录合并（近似）",
        "cross_exchange": cross, "exchanges": exchanges, "runs": runs, "dups": dups, "window_diff": wdiff,
    }
    merged["synthetic"] = any(x["synthetic"] for x in runs)
    return merged, out_rows


def load_runs(run_dirs: list[Path]) -> list[tuple[dict[str, Any], list[dict[str, Any]]]]:
    import research_report as rr

    out = []
    for d in run_dirs:
        plan = rr.load_plan(d)
        plan["_run_dir"] = str(d)
        jobs = rr.load_jobs(d)
        if not any(j["status"] == "ok" for j in jobs):
            raise ValueError(f"{d} 里没有任何成功的回测结果")
        out.append((plan, rr.build_rows(plan, jobs, rr.load_notes(d))))
    return out


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="子账户组合检查：对已经跑完的研究目录重算组合结果（不重跑回测）")
    p.add_argument("--run-dir", default=None, help="研究结果目录，例如 user_data/research/run_20261107_120000")
    p.add_argument("--run-dirs", nargs="+", default=None,
                   help="合并几个研究目录（例如一个 OKX 的、一个币安的）的策略一起做组合检查，结果标注\"跨交易所近似\"；"
                        "区间和判定按第一个目录；输出 portfolio-merged.txt/csv/json（默认写在第一个目录里）")
    p.add_argument("--out-dir", default=None, help="--run-dirs 时输出写到哪个目录（默认第一个目录）")
    p.add_argument("--strategies", default=None, help='放进组合的策略，例如 "A,B,C"（默认：plan.json / RESEARCH_PORTFOLIO / 默认挑选规则）')
    p.add_argument("--weights", choices=["equal", "riskparity"], default=None, help="equal = 平均分（默认），riskparity = 按训练段波动率倒数分")
    p.add_argument("--no-write", action="store_true", help="只打印，不写 portfolio.txt/csv/json")
    p.add_argument("--max-dd", type=cd.max_dd_arg, default=None,
                   help="按这个回撤上限（%%）重新判定和挑选（默认 = plan.json 里记的值；plan.json 不改）")
    a = p.parse_args(argv)
    if not a.run_dir and not a.run_dirs:
        p.error("要给 --run-dir（一个研究目录）或 --run-dirs（合并几个研究目录）")
    os.chdir(ROOT_DIR)  # plan.json / jobs.tsv 里的路径都相对项目根目录
    import research_report as rr

    if a.run_dirs:
        dirs = ([Path(a.run_dir)] if a.run_dir else []) + [Path(x) for x in a.run_dirs]
        dirs = list(dict.fromkeys(dirs))
        try:
            plan, rows = merge_runs(load_runs(dirs))
        except (OSError, ValueError) as exc:
            print(f"错误：{exc}")
            return 3
        out_dir = Path(a.out_dir) if a.out_dir else dirs[0]
        cd.override_settings_max_dd(plan.setdefault("settings", {}), a.max_dd)
        plan["_run_dir"] = str(out_dir)
        res = build(plan, rows, a.strategies, a.weights)
        lines = render_lines(res, plan)
        print("\n".join(lines))
        if not a.no_write:
            write_outputs(out_dir, res, lines, stem="portfolio-merged")
            print(f"\n已保存：{out_dir / 'portfolio-merged.txt'}  {out_dir / 'portfolio-merged.csv'}  "
                  f"{out_dir / 'portfolio-merged.json'}")
        return 0

    run_dir = Path(a.run_dir)
    plan = rr.load_plan(run_dir)
    plan["_run_dir"] = str(run_dir)
    cd.override_settings_max_dd(plan.setdefault("settings", {}), a.max_dd)
    jobs = rr.load_jobs(run_dir)
    if not any(j["status"] == "ok" for j in jobs):
        print("没有任何成功的回测结果。")
        return 3
    rows = rr.build_rows(plan, jobs, rr.load_notes(run_dir))
    res = build(plan, rows, a.strategies, a.weights)
    lines = render_lines(res, plan)
    print("\n".join(lines))
    if not a.no_write:
        write_outputs(run_dir, res, lines)
        print(f"\n已保存：{run_dir / 'portfolio.txt'}  {run_dir / 'portfolio.csv'}  {run_dir / 'portfolio.json'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
