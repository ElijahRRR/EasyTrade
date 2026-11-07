#!/usr/bin/env python3
"""
【合成测试数据】生成器 —— 只用来检查流程能不能跑通，不是真实行情！

!!! 用它跑出来的回测收益、回撤、胜率全部没有意义，不能用来评价或挑选策略 !!!

用途：在连不上交易所的机器上（例如本项目的开发容器），也能把
  回测 -> 回撤检查 -> lookahead / recursive 分析
整条流程跑一遍，确认配置、脚本、策略代码没有报错。

生成的内容（freqtrade 磁盘格式，文件名、列名、K 线类型都按 freqtrade 2026.10 源码确定）：
  user_data/data/okx-synthetic/futures/<币>_USDT_USDT-<周期>-futures.feather   K 线（5m 15m 1h 4h 1d；
      5m / 15m 默认只有最近 730 天，见 --short-tf-days）
  user_data/data/okx-synthetic/futures/<币>_USDT_USDT-1h-mark.feather          标记价格（只有最近约 92 天）
  user_data/data/okx-synthetic/futures/<币>_USDT_USDT-1h-funding_rate.feather  资金费率（只有最近约 92 天，每 8 小时一条）
  user_data/data/okx-synthetic/synthetic_markets.json   假的交易对规格 + 杠杆档位（给 scripts/ft_offline.py 用）
  user_data/data/okx-synthetic/SYNTHETIC_DATA_README.txt  标记文件：说明这是合成数据

为什么单独放在 okx-synthetic 目录、不放进 user_data/data/okx：
  freqtrade download-data 会在已有文件的末尾"增量追加"。如果合成数据放在真实数据目录里，
  以后下载真实数据时会接在假数据后面，真假混在一起而且很难发现。所以这里强制分开，
  并且拒绝往没有标记文件的目录里写。

行情模型（尽量像真的，但完全是随机数）：
  * 日级"行情状态"马尔可夫链：牛市 / 熊市 / 震荡 / 急跌 / 急涨，每段持续几天到几个月
  * 波动率聚集（小时级随机波动率）+ 肥尾收益（t 分布）+ 日内时段效应（美盘/亚盘更活跃，周末清淡）
  * 全市场共同因子 + 各币的 beta 和独立波动（山寨币波动更大）
  * 不定期的"连环爆仓"：单根 5 分钟 K 线放量几十到几百倍并大幅下跌（偶尔是轧空上涨），
    急跌/熊市里更频繁 —— 这样 VolumeSpikeShort / BigTrendShort / DailyThreeSoldiersShort 都会有信号
  * 每个币一段 1 小时的"交易所维护"缺口（测试 freqtrade 补齐缺失 K 线）
  * 价格按 OKX 的最小价格跳动取整，成交量按合约张数取整，成交量单位是"币的数量"（与 freqtrade 一致）
  * 有几个币"上线晚"（--listings，默认 SOL 2021-06-01、AVAX 2022-09-15、ADA 2024-06-01）：那天之前没有任何数据，
    用来测试研究脚本对上线晚的币的处理（scripts/research.sh：样本外开始前数据够 365 天的保留，不够的去掉）

默认区间（第 3 轮起）：2020-01-01 ~ 今天，8 个币。研究从 2021-01-01 开始，前面那一年只用来给指标预热，
和 VPS 上真实数据的情况一样（下载时多下约 600 天给指标预热）。

用法（需要 freqtrade 环境，因为直接用 freqtrade 自己的数据写入代码保证格式正确）：
  .venv/bin/python scripts/make_synthetic_data.py                 # 默认 8 个币、2020-01-01 至今（3 个币上线晚）
  .venv/bin/python scripts/make_synthetic_data.py --days 730 --listings ""   # 第 2 轮的样子：最近 2 年、所有币同一天开始
  .venv/bin/python scripts/make_synthetic_data.py --days 365 --seed 7
  .venv/bin/python scripts/make_synthetic_data.py --futures-history-days 0   # 标记价格/资金费率覆盖全部区间
  然后：make synthetic-test   （或 bash scripts/synthetic_test.sh），见 README「用合成数据自检」
"""

from __future__ import annotations

import argparse
import json
import math
import shutil
import sys
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUT = ROOT / "user_data" / "data" / "okx-synthetic"
MARKER_NAME = "SYNTHETIC_DATA_README.txt"
MARKETS_NAME = "synthetic_markets.json"
DEFAULT_TIMEFRAMES = ("5m", "15m", "1h", "4h", "1d")
BARS_PER_DAY = 288  # 5 分钟 K 线

# 交易对规格（数值参考 OKX USDT 永续的公开规格，只用来让 freqtrade 能算精度/最小下单量）
#   p0 = 起始价, tick = 最小价格跳动, ct = 每张合约对应的币数, lot = 最小下单张数,
#   beta = 对全市场因子的弹性, idio = 独立日波动, qv = 5 分钟平均成交额 (USDT), lev = 最大杠杆
PAIR_SPECS: dict[str, dict[str, float]] = {
    "BTC": {"p0": 70000.0, "tick": 0.1, "ct": 0.01, "lot": 0.01, "beta": 1.00, "idio": 0.004, "qv": 25e6, "lev": 100},
    "ETH": {"p0": 2500.0, "tick": 0.01, "ct": 0.1, "lot": 0.01, "beta": 1.15, "idio": 0.012, "qv": 12e6, "lev": 100},
    "SOL": {"p0": 170.0, "tick": 0.01, "ct": 1.0, "lot": 0.01, "beta": 1.40, "idio": 0.020, "qv": 4e6, "lev": 50},
    "XRP": {"p0": 0.55, "tick": 0.0001, "ct": 100.0, "lot": 1.0, "beta": 1.10, "idio": 0.022, "qv": 2.5e6, "lev": 75},
    "DOGE": {"p0": 0.17, "tick": 0.00001, "ct": 1000.0, "lot": 1.0, "beta": 1.50, "idio": 0.025, "qv": 2e6, "lev": 75},
    "LINK": {"p0": 12.0, "tick": 0.001, "ct": 1.0, "lot": 1.0, "beta": 1.30, "idio": 0.020, "qv": 0.8e6, "lev": 50},
    "ADA": {"p0": 0.45, "tick": 0.0001, "ct": 100.0, "lot": 1.0, "beta": 1.25, "idio": 0.022, "qv": 1.5e6, "lev": 75},
    "AVAX": {"p0": 30.0, "tick": 0.001, "ct": 1.0, "lot": 1.0, "beta": 1.45, "idio": 0.024, "qv": 1.2e6, "lev": 50},
}
DEFAULT_START = "20200101"
# 上线晚的币（那天之前没有数据）：一个在研究开始后不久、一个在训练段中间（都保留），一个离样本外太近（会被去掉）
DEFAULT_LISTINGS = "SOL:20210601 AVAX:20220915 ADA:20240601"
# 5m / 15m 只有 5m 策略和 --detail 用到，6 年全生成又大又慢（每个币 70 多万根）
DEFAULT_SHORT_TF_DAYS = 730
SHORT_TFS = ("5m", "15m")

# 行情状态：日漂移 mu、日波动 sigma、持续天数范围、爆仓事件概率（每天）与下跌占比、成交量倍数
REGIMES: dict[str, dict] = {
    "bull": {"mu": 0.0035, "sigma": 0.028, "days": (20, 80), "ev": 0.04, "down": 0.45, "vol": 1.0},
    "bear": {"mu": -0.0040, "sigma": 0.034, "days": (20, 70), "ev": 0.08, "down": 0.85, "vol": 1.2},
    "chop": {"mu": 0.0, "sigma": 0.020, "days": (10, 40), "ev": 0.03, "down": 0.60, "vol": 0.8},
    "crash": {"mu": -0.0250, "sigma": 0.060, "days": (3, 9), "ev": 0.40, "down": 0.92, "vol": 2.2},
    "rally": {"mu": 0.0180, "sigma": 0.050, "days": (3, 9), "ev": 0.15, "down": 0.30, "vol": 1.8},
}
TRANSITIONS: dict[str, tuple[tuple[str, ...], tuple[float, ...]]] = {
    "bull": (("chop", "bear", "crash", "rally"), (0.45, 0.20, 0.20, 0.15)),
    "bear": (("chop", "bull", "crash", "rally"), (0.40, 0.15, 0.30, 0.15)),
    "chop": (("bull", "bear", "crash", "rally"), (0.35, 0.35, 0.18, 0.12)),
    "crash": (("bear", "chop", "rally"), (0.50, 0.35, 0.15)),
    "rally": (("bull", "chop", "crash"), (0.50, 0.35, 0.15)),
}


# ---------------------------------------------------------------------------
# 生成
# ---------------------------------------------------------------------------


def regime_path(n_days: int, rng: np.random.Generator) -> list[str]:
    """逐日的行情状态。"""
    out: list[str] = []
    cur = str(rng.choice(["bull", "bear", "chop"]))
    while len(out) < n_days:
        lo, hi = REGIMES[cur]["days"]
        out.extend([cur] * int(rng.integers(lo, hi + 1)))
        nxt, probs = TRANSITIONS[cur]
        cur = str(rng.choice(nxt, p=probs))
    return out[:n_days]


def intraday_profile(index: pd.DatetimeIndex) -> tuple[np.ndarray, np.ndarray]:
    """日内时段效应：返回 (波动倍数, 成交量倍数)。美盘开盘、亚盘开盘最活跃，周末清淡。"""
    hour = index.hour.to_numpy() + index.minute.to_numpy() / 60.0
    us = np.exp(-0.5 * ((hour - 14.5) / 1.5) ** 2)  # 13:30 UTC 美股开盘前后
    asia = np.exp(-0.5 * ((hour - 1.0) / 1.2) ** 2)  # 01:00 UTC 亚盘
    eu = np.exp(-0.5 * ((hour - 8.0) / 1.5) ** 2)  # 08:00 UTC 欧盘
    act = 0.75 + 0.55 * us + 0.30 * asia + 0.20 * eu
    weekend = index.day_of_week.to_numpy() >= 5
    vol_mult = act * np.where(weekend, 0.80, 1.0)
    volu_mult = act**1.5 * np.where(weekend, 0.60, 1.0)
    # 归一化，使平均值约为 1
    return vol_mult / vol_mult.mean(), volu_mult / volu_mult.mean()


def stochastic_vol(n_bars: int, rng: np.random.Generator) -> np.ndarray:
    """小时级 AR(1) 对数波动率 -> 每根 5 分钟 K 线的波动倍数（波动率聚集）。"""
    n_hours = n_bars // 12 + 2
    phi, eta = 0.985, 0.12
    shocks = rng.normal(0.0, eta, size=n_hours)
    h = np.empty(n_hours)
    h[0] = 0.0
    for i in range(1, n_hours):
        h[i] = phi * h[i - 1] + shocks[i]
    var = eta**2 / (1 - phi**2)
    mult = np.exp(h - var / 2.0)
    return np.repeat(mult, 12)[:n_bars]


def t_noise(rng: np.random.Generator, size: int, df: float = 4.0) -> np.ndarray:
    """单位方差的 t 分布噪声（肥尾）。"""
    return rng.standard_t(df, size=size) / math.sqrt(df / (df - 2.0))


def pick_event_bars(day_idx: int, n: int, rng: np.random.Generator) -> np.ndarray:
    """某一天里选 n 根 5 分钟 K 线作为事件（偏向美盘时段）。"""
    w = np.ones(BARS_PER_DAY)
    w[156:204] *= 3.0  # 13:00-17:00 UTC
    w /= w.sum()
    return day_idx * BARS_PER_DAY + rng.choice(BARS_PER_DAY, size=n, replace=False, p=w)


def build_market(n_bars: int, n_days: int, regimes: list[str], rng: np.random.Generator):
    """全市场因子（5 分钟对数收益）+ 全市场爆仓事件。"""
    mu_d = np.array([REGIMES[r]["mu"] for r in regimes])
    sig_d = np.array([REGIMES[r]["sigma"] for r in regimes])
    mu = np.repeat(mu_d, BARS_PER_DAY)[:n_bars] / BARS_PER_DAY
    sig = np.repeat(sig_d, BARS_PER_DAY)[:n_bars] / math.sqrt(BARS_PER_DAY)

    events: list[tuple[int, float, float]] = []  # (bar, 对数收益冲击, 成交量倍数)
    for d, r in enumerate(regimes):
        cfg = REGIMES[r]
        if rng.random() < cfg["ev"]:
            for bar in pick_event_bars(d, int(rng.integers(1, 3)), rng):
                down = rng.random() < cfg["down"]
                size = rng.uniform(0.008, 0.035)
                vmult = math.exp(rng.uniform(math.log(8), math.log(150)))
                events.append((int(bar), -size if down else size * 0.8, vmult))
    return mu, sig, events


def apply_events(rets, vol_boost, volu_mult, events, beta, rng):
    """把事件写进某个币的收益 / 成交量：事件 K 线大幅波动 + 巨量，之后十几根 K 线余波递减。"""
    n = len(rets)
    for bar, shock, vmult in events:
        if bar >= n - 1:
            continue
        rets[bar] += shock * beta * rng.uniform(0.7, 1.3)
        volu_mult[bar] *= vmult * rng.uniform(0.6, 1.4)
        tail = np.arange(1, 25)
        idx = bar + tail
        idx = idx[idx < n]
        decay = np.exp(-tail[: len(idx)] / 6.0)
        vol_boost[idx] *= 1.0 + 1.5 * decay
        volu_mult[idx] *= 1.0 + 0.08 * vmult * decay


def integrate_with_reversion(rets: np.ndarray, p0: float, band: float = math.log(3.0)) -> np.ndarray:
    """把收益累加成对数价格。价格离起始价超过 3 倍 (上下) 时, 每天往回拉一点,
    避免两年后跑到离谱的价位 (在区间内完全不干预)。"""
    out = np.empty(len(rets))
    x = 0.0  # log(价格 / 起始价)
    for d0 in range(0, len(rets), BARS_PER_DAY):
        seg = rets[d0 : d0 + BARS_PER_DAY]
        excess = abs(x) - band
        adj = -math.copysign(0.03 * excess, x) / len(seg) if excess > 0 else 0.0
        path = x + np.cumsum(seg + adj)
        out[d0 : d0 + len(seg)] = path
        x = float(path[-1])
    return math.log(p0) + out


def round_to(x: np.ndarray, step: float) -> np.ndarray:
    return np.round(x / step) * step


def build_pair(
    base: str,
    spec: dict[str, float],
    index: pd.DatetimeIndex,
    regimes: list[str],
    market: tuple,
    sv_market: np.ndarray,
    act_vol: np.ndarray,
    act_volu: np.ndarray,
    gap: tuple[int, int],
    rng: np.random.Generator,
) -> pd.DataFrame:
    n = len(index)
    mu, sig, m_events = market
    beta = spec["beta"]
    m_noise = t_noise(rng, n)
    idio_sig = spec["idio"] / math.sqrt(BARS_PER_DAY)
    vol_boost = np.ones(n)
    volu_mult = np.ones(n)

    # 自己独有的事件（例如单币利空），频率较低
    own_events: list[tuple[int, float, float]] = []
    for d, r in enumerate(regimes):
        if rng.random() < 0.015 + 0.02 * (r in ("bear", "crash")):
            for bar in pick_event_bars(d, 1, rng):
                down = rng.random() < 0.7
                own_events.append(
                    (int(bar), (-1 if down else 0.8) * rng.uniform(0.01, 0.05), math.exp(rng.uniform(math.log(8), math.log(120))))
                )
    sv_own = stochastic_vol(n, rng) ** 0.5

    # 收益 = beta * 市场 + 独立部分；波动 = 状态波动 x 波动聚集 x 日内时段
    shock = np.zeros(n)
    apply_events(shock, vol_boost, volu_mult, m_events + own_events, beta, rng)
    rets = (
        beta * (mu + sig * sv_market * act_vol * vol_boost * m_noise)
        + idio_sig * sv_own * act_vol * vol_boost * t_noise(rng, n)
        + shock
    )
    close = np.exp(integrate_with_reversion(rets, spec["p0"]))

    tick = spec["tick"]
    close = np.maximum(round_to(close, tick), tick)
    open_ = np.concatenate([[round_to(np.array([spec["p0"]]), tick)[0]], close[:-1]])
    bar_sig = (beta * sig + idio_sig) * sv_market * act_vol * vol_boost
    up_wick = np.abs(rng.normal(0.0, 0.6, n)) * bar_sig * np.maximum(open_, close)
    dn_wick = np.abs(rng.normal(0.0, 0.6, n)) * bar_sig * np.minimum(open_, close)
    high = round_to(np.maximum(open_, close) + up_wick, tick)
    low = np.maximum(round_to(np.minimum(open_, close) - dn_wick, tick), tick)
    high = np.maximum(high, np.maximum(open_, close))
    low = np.minimum(low, np.minimum(open_, close))

    # 成交额 (USDT) = 平均 x 时段 x 行情状态 x |收益| 放大 x 对数正态噪声；成交量 = 成交额 / 价格（币的数量）
    reg_volu = np.repeat(np.array([REGIMES[r]["vol"] for r in regimes]), BARS_PER_DAY)[:n]
    abs_z = np.abs(rets) / np.maximum(bar_sig, 1e-12)
    notional = (
        spec["qv"]
        * act_volu
        * reg_volu
        * volu_mult
        * (0.6 + 0.4 * np.minimum(abs_z, 6.0))
        * rng.lognormal(-0.08, 0.4, n)
    )
    lot_coins = spec["lot"] * spec["ct"]
    volume = np.maximum(round_to(notional / close, lot_coins), lot_coins)

    df = pd.DataFrame(
        {"date": index, "open": open_, "high": high, "low": low, "close": close, "volume": volume}
    )
    # 交易所维护：这段时间没有 K 线（freqtrade 读取时会自动补成平盘 K 线）
    g0, g1 = gap
    return df.drop(index=range(g0, g1)).reset_index(drop=True)


def resample(df5: pd.DataFrame, tf: str) -> pd.DataFrame:
    rule = {"15m": "15min", "1h": "1h", "4h": "4h", "1d": "1D", "5m": None}[tf]
    if rule is None:
        return df5
    agg = {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
    out = (
        df5.set_index("date")
        .resample(rule, label="left", closed="left")
        .agg(agg)
        .dropna(subset=["open"])
        .reset_index()
    )
    return out


def build_mark(df1h: pd.DataFrame, rng: np.random.Generator, tick: float) -> pd.DataFrame:
    """标记价格：成交价 + 很小的基差噪声（标记价格 K 线没有成交量）。"""
    basis = 1 + rng.normal(0.0, 0.0003, len(df1h))
    m = df1h.copy()
    for c in ("open", "high", "low", "close"):
        m[c] = np.maximum(round_to(m[c].to_numpy() * basis, tick), tick)
    m["high"] = m[["open", "high", "low", "close"]].max(axis=1)
    m["low"] = m[["open", "high", "low", "close"]].min(axis=1)
    m["volume"] = 0.0
    return m


def build_funding(df1h: pd.DataFrame, rng: np.random.Generator) -> pd.DataFrame:
    """资金费率：每 8 小时一次 (00/08/16 UTC)。上涨行情偏正、下跌行情偏负，上下限 +/-0.75%。"""
    s = df1h.set_index("date")["close"]
    trend = np.log(s / s.shift(72)).fillna(0.0)  # 过去 3 天涨跌
    at = s.index[s.index.hour % 8 == 0]
    rate = 0.0001 + 0.0004 * np.tanh(trend.loc[at].to_numpy() / 0.05) + rng.normal(0.0, 0.00005, len(at))
    rate = np.clip(np.round(rate, 8), -0.0075, 0.0075)
    return pd.DataFrame({"date": at, "funding_rate": rate})


# ---------------------------------------------------------------------------
# 交易对规格 / 杠杆档位（给 scripts/ft_offline.py，代替连不上的交易所接口）
# ---------------------------------------------------------------------------


def market_entry(base: str, spec: dict[str, float]) -> dict:
    sym = f"{base}/USDT:USDT"
    tick = spec["tick"]
    return {
        "id": f"{base}-USDT-SWAP",
        "symbol": sym,
        "base": base,
        "quote": "USDT",
        "settle": "USDT",
        "baseId": base,
        "quoteId": "USDT",
        "settleId": "USDT",
        "type": "swap",
        "spot": False,
        "margin": False,
        "swap": True,
        "future": False,
        "option": False,
        "index": None,
        "active": True,
        "contract": True,
        "linear": True,
        "inverse": False,
        "subType": "linear",
        "contractSize": spec["ct"],
        "expiry": None,
        "expiryDatetime": None,
        "strike": None,
        "optionType": None,
        "taker": 0.0005,
        "maker": 0.0002,
        "percentage": True,
        "tierBased": True,
        "feeSide": "get",
        "precision": {"amount": spec["lot"], "price": tick, "cost": None, "base": None, "quote": None},
        "limits": {
            "leverage": {"min": 1.0, "max": float(spec["lev"])},
            "amount": {"min": spec["lot"], "max": None},
            "price": {"min": None, "max": None},
            "cost": {"min": None, "max": None},
            "market": {"min": None, "max": None},
        },
        "marginModes": {"cross": True, "isolated": True},
        "created": None,
        "info": {"instId": f"{base}-USDT-SWAP", "note": "SYNTHETIC - not real exchange data"},
    }


def leverage_tiers(spec: dict[str, float]) -> list[dict]:
    """杠杆档位（名义价值单位 USDT），格式与 freqtrade 解析后的 LeverageTier 相同。"""
    first = 50_000.0 * max(spec["qv"] / 1e6, 1.0)
    tiers = []
    lo = 0.0
    lev = float(spec["lev"])
    mmr = 0.004
    for _ in range(6):
        hi = first if lo == 0 else lo * 4
        tiers.append(
            {"minNotional": lo, "maxNotional": hi, "maintenanceMarginRate": mmr, "maxLeverage": lev, "maintAmt": None}
        )
        lo, lev, mmr = hi, max(lev / 2.0, 1.0), mmr * 2
    return tiers


# ---------------------------------------------------------------------------


MARKER_TEXT = """\
!!! 这是【合成测试数据】，不是真实行情 !!!
THIS DIRECTORY CONTAINS SYNTHETIC (RANDOMLY GENERATED) MARKET DATA - NOT REAL.

由 scripts/make_synthetic_data.py 生成，生成时间 {now} UTC，随机种子 {seed}。
交易对：{pairs}
区间：{start} ~ {end}（{days} 天），周期：{tfs}
{short_tf}
上线晚的币（那天之前没有数据）：{listings}
标记价格/资金费率：{fut}

只用来检查回测流程（配置、脚本、策略代码）能否跑通。
用这些数据得到的收益、回撤、胜率没有任何意义，不能用来评价策略。
真实回测请用 scripts/download_okx_data.sh 下载的 user_data/data/okx 数据。
可以随时删除整个目录：rm -rf user_data/data/okx-synthetic
"""


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="生成合成测试数据（仅用于流程自检，不是真实行情）")
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT, help="输出目录（默认 user_data/data/okx-synthetic）")
    ap.add_argument("--start", default=None, help=f"开始日期 YYYYMMDD（默认 {DEFAULT_START}；写了 --days 时不用）")
    ap.add_argument("--days", type=int, default=None, help="天数（从结束日期往前倒推；写了就不看 --start）")
    ap.add_argument("--end", default=None, help="结束日期 YYYYMMDD（默认今天 UTC 0 点）")
    ap.add_argument("--seed", type=int, default=42, help="随机种子（同一种子生成的数据完全相同）")
    ap.add_argument("--pairs", default=" ".join(PAIR_SPECS), help=f"币种，空格分隔（可选：{' '.join(PAIR_SPECS)}）")
    ap.add_argument("--timeframes", default=" ".join(DEFAULT_TIMEFRAMES), help="周期（默认 5m 15m 1h 4h 1d）")
    ap.add_argument(
        "--futures-history-days",
        type=int,
        default=92,
        help="标记价格/资金费率只生成最近 N 天（模仿 OKX 只有约 3 个月历史）；0 = 覆盖全部区间",
    )
    ap.add_argument(
        "--listings",
        default=DEFAULT_LISTINGS,
        help=f'上线晚的币，空格分隔的 币:YYYYMMDD（那天之前没有数据），默认 "{DEFAULT_LISTINGS}"；"" = 所有币同一天开始',
    )
    ap.add_argument(
        "--short-tf-days",
        type=int,
        default=DEFAULT_SHORT_TF_DAYS,
        help=f"5m / 15m 只生成最近 N 天（默认 {DEFAULT_SHORT_TF_DAYS}）；0 = 覆盖全部区间",
    )
    args = ap.parse_args(argv)

    try:
        from freqtrade.data.history import get_datahandler
        from freqtrade.enums import CandleType
        from freqtrade.exchange import Exchange
        from freqtrade.exchange.okx import Okx
    except ImportError:
        print("错误：需要 freqtrade 环境。请用 .venv/bin/python 运行本脚本（见 README）。", file=sys.stderr)
        return 2

    out: Path = args.out.resolve()
    real_dir = (ROOT / "user_data" / "data" / "okx").resolve()
    if out == real_dir or real_dir in out.parents:
        print(f"错误：不能把合成数据写进真实数据目录 {real_dir}。", file=sys.stderr)
        return 2
    if out.exists() and any(out.rglob("*.feather")) and not (out / MARKER_NAME).is_file():
        print(f"错误：{out} 里已有数据文件且没有合成数据标记，可能是真实数据，拒绝覆盖。", file=sys.stderr)
        return 2
    bases = [b.upper() for b in args.pairs.split()]
    unknown = [b for b in bases if b not in PAIR_SPECS]
    if unknown:
        print(f"错误：不支持的币种 {unknown}，可选：{list(PAIR_SPECS)}", file=sys.stderr)
        return 2
    tfs = args.timeframes.split()
    bad_tf = [t for t in tfs if t not in ("5m", "15m", "1h", "4h", "1d")]
    if bad_tf:
        print(f"错误：不支持的周期 {bad_tf}（可选 5m 15m 1h 4h 1d）", file=sys.stderr)
        return 2

    # 标记价格 / 资金费率的周期和 K 线类型以 freqtrade 本版本的 OKX 设置为准（2026.10: 都是 1h）
    ft_has = {**Exchange._ft_has_default, **Okx._ft_has, **Okx._ft_has_futures}
    mark_tf = ft_has["mark_ohlcv_timeframe"]
    fund_tf = ft_has["funding_fee_timeframe"]
    mark_type = CandleType.from_string(ft_has["mark_ohlcv_price"])
    for need in {mark_tf, fund_tf} - {"1h"}:
        print(f"错误：本脚本只会生成 1h 的标记价格/资金费率，但 freqtrade 需要 {need}", file=sys.stderr)
        return 2

    def _ymd(txt: str, what: str) -> pd.Timestamp:
        try:
            return pd.Timestamp(datetime.strptime(txt, "%Y%m%d"), tz="UTC")
        except ValueError:
            raise SystemExit(f"错误：{what} 要写成 YYYYMMDD（收到：{txt}）") from None

    end = _ymd(args.end, "--end") if args.end else pd.Timestamp.now(tz="UTC").floor("D")
    if args.days is not None:
        start = end - pd.Timedelta(days=args.days)
    else:
        start = _ymd(args.start or DEFAULT_START, "--start")
    n_days = int((end - start).days)
    if n_days < 30:
        print(f"错误：区间只有 {n_days} 天（{start.date()} ~ {end.date()}），太短", file=sys.stderr)
        return 2
    index = pd.date_range(start, end, freq="5min", inclusive="left")

    listings: dict[str, pd.Timestamp] = {}
    for item in args.listings.split():
        coin, _, day = item.partition(":")
        coin = coin.upper()
        if coin not in PAIR_SPECS or not day:
            print(f"错误：--listings 写成 币:YYYYMMDD，例如 SOL:20210601（收到：{item}）", file=sys.stderr)
            return 2
        listings[coin] = _ymd(day, f"--listings {coin}")
    for coin, t in listings.items():
        if coin in bases and not (start < t < end - pd.Timedelta(days=30)):
            print(f"错误：{coin} 的上线日期 {t.date()} 要在 {start.date()} 和结束前 30 天之间", file=sys.stderr)
            return 2
    short_start = start if args.short_tf_days <= 0 else max(start, end - pd.Timedelta(days=args.short_tf_days))

    # 参数都检查完了再删旧数据
    if out.exists():
        shutil.rmtree(out)
    (out / "futures").mkdir(parents=True)
    rng = np.random.default_rng(args.seed)
    dh = get_datahandler(out, "feather")

    regimes = regime_path(n_days, rng)
    market = build_market(len(index), n_days, regimes, rng)
    sv_market = stochastic_vol(len(index), rng)
    act_vol, act_volu = intraday_profile(index)
    # 维护缺口：所有币同一个小时（选在区间中段的某个周二 02:00 UTC）
    mid = index[len(index) // 2].normalize()
    while mid.day_of_week != 1:
        mid += pd.Timedelta(days=1)
    g0 = int(index.get_indexer([mid + pd.Timedelta(hours=2)])[0])
    gap = (g0, g0 + 12)

    fut_start = start if args.futures_history_days <= 0 else end - pd.Timedelta(days=args.futures_history_days)
    markets: dict[str, dict] = {}
    tiers: dict[str, list[dict]] = {}
    counts = {r: regimes.count(r) for r in REGIMES}
    print("合成测试数据（不是真实行情）")
    print(f"  区间 {start.date()} ~ {end.date()}（{n_days} 天），种子 {args.seed}")
    print("  行情状态天数：" + "，".join(f"{k} {v}" for k, v in counts.items()))
    print(f"  全市场爆仓事件：{len(market[2])} 次")

    for base in bases:
        spec = PAIR_SPECS[base]
        pair = f"{base}/USDT:USDT"
        df5 = build_pair(base, spec, index, regimes, market, sv_market, act_vol, act_volu, gap, rng)
        if base in listings:
            # 上线晚：那天之前没有任何K线（随机数照常生成，这样其他币的数据不受影响）
            df5 = df5[df5["date"] >= listings[base]].reset_index(drop=True)
        for tf in tfs:
            out_df = resample(df5, tf)
            if tf in SHORT_TFS:
                out_df = out_df[out_df["date"] >= short_start].reset_index(drop=True)
            dh.ohlcv_store(pair, tf, out_df, CandleType.FUTURES)
        df1h = resample(df5, "1h")
        recent = df1h[df1h["date"] >= fut_start].reset_index(drop=True)
        dh.ohlcv_store(pair, mark_tf, build_mark(recent, rng, spec["tick"]), mark_type)
        dh.ohlcv_store(pair, fund_tf, build_funding(df1h, rng).query("date >= @fut_start"), CandleType.FUNDING_RATE)
        markets[pair] = market_entry(base, spec)
        tiers[pair] = leverage_tiers(spec)
        d1 = resample(df5, "1d")
        print(
            f"  {pair:<15} {'从 ' + str(listings[base].date()) + ' 起' if base in listings else '':<16}"
            f"起始 {df5['open'].iloc[0]:>10g}  最终 {df5['close'].iloc[-1]:>12.6g}  "
            f"最高 {df5['high'].max():>12.6g}  最低 {df5['low'].min():>12.6g}  "
            f"日线阴线占比 {(d1['close'] < d1['open']).mean():.0%}"
        )

    (out / MARKETS_NAME).write_text(
        json.dumps(
            {
                "_note": "SYNTHETIC markets for scripts/ft_offline.py only - not real exchange specs",
                "exchange": "okx",
                "markets": markets,
                "leverage_tiers": tiers,
            },
            ensure_ascii=False,
            indent=1,
        ),
        encoding="utf-8",
    )
    (out / MARKER_NAME).write_text(
        MARKER_TEXT.format(
            now=datetime.now(UTC).strftime("%Y-%m-%d %H:%M"),
            seed=args.seed,
            pairs=" ".join(f"{b}/USDT:USDT" for b in bases),
            start=start.date(),
            end=end.date(),
            days=n_days,
            tfs=" ".join(tfs),
            short_tf=(
                f"5m / 15m：只有 {short_start.date()} 之后"
                if short_start > start and any(t in SHORT_TFS for t in tfs)
                else "所有周期都覆盖全部区间"
            ),
            listings="、".join(f"{c} {t.date()}" for c, t in listings.items() if c in bases) or "没有",
            fut=f"只有 {fut_start.date()} 之后（{mark_tf}）" if args.futures_history_days > 0 else "覆盖全部区间",
        ),
        encoding="utf-8",
    )
    size_mb = sum(f.stat().st_size for f in out.rglob("*") if f.is_file()) / 1e6
    print(f"已写入 {out}（{size_mb:.1f} MB）")
    print("下一步：bash scripts/synthetic_test.sh   （或 make synthetic-test）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
