"""向量化初筛引擎：信号 → 仓位 → 逐根收益（扣手续费、资金费）→ 分段指标。

和 freqtrade 回测的约定对齐：
  * K线的 date 是开盘时间；第 t 根收盘后才算信号，仓位从第 t+1 根开始持有（赚 close[t+1]/close[t]）。
  * 手续费+滑点每边 0.07%（和 research.sh 的 RESEARCH_FEE 一样），按仓位变化量收。
  * 资金费：持多付正费率、持空收正费率；没有数据的时段按 0（和研究配置 futures_funding_rate=0 一样）。
  * 回撤含浮亏：每根K线用多头的最低价、空头的最高价算"这一根最差的时候"账户值（和 check_drawdown.py 同一个意思，
    但这里是整个账户逐根算，不是逐笔）。

仓位大小（项目风控：单笔风险约 1%、总敞口有上限、杠杆 ≤ 5x；这里更保守，默认总杠杆 ≤ 1x）：
  * 单币时间序列策略（ts）：进场那根按波动率定仓位 = min(单币上限, 每日目标波动 / 该币近 30 天日波动)，
    持仓期间不再调整；所有仓位加起来超过总杠杆上限时按比例缩小。
  * 截面策略（xs）：每 k 根调一次仓，按得分排名，多最强的一组、空最弱的一组（或只做多），
    组内按波动率倒数分配，多空各占总杠杆的一半（多空对冲）。
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from statistics import NormalDist

import numpy as np
import pandas as pd

from data import Panel

ND = NormalDist()


@dataclass
class Settings:
    fee: float = 0.0007
    gross_max: float = 1.0
    pos_cap: float = 0.25
    target_daily_vol: float = 0.004
    vol_span_days: int = 30
    xs_quantile: float = 0.2
    xs_min_side: int = 2
    xs_min_names: int = 8
    min_history_days: int = 60
    heat_max: float = 0.024  # 所有仓位"日波动贡献"之和上限 = 6 个满仓位 × 0.4%（项目规则：单笔约 1%、总共约 6%）


@dataclass
class SimResult:
    index: pd.DatetimeIndex
    ret: np.ndarray          # 每根净收益（账户百分比）
    worst: np.ndarray        # 每根最差时刻的收益（含浮亏）
    cost: np.ndarray
    fund: np.ndarray
    gross: np.ndarray        # 每根持仓总杠杆
    pnl: np.ndarray          # 每根每币的盈亏（账户百分比）
    tid: np.ndarray          # 每根每币属于哪一笔交易（-1 = 空仓）
    bars_per_year: float
    extra: dict = field(default_factory=dict)


def _vol(panel: Panel, s: Settings) -> pd.DataFrame:
    per_day = panel.bars_per_year / 365.0
    r = panel.ret()
    v = r.ewm(span=int(s.vol_span_days * per_day), min_periods=int(10 * per_day)).std()
    return v * math.sqrt(per_day)  # 换算成日波动


def _history_ok(panel: Panel, s: Settings) -> pd.DataFrame:
    per_day = panel.bars_per_year / 365.0
    n = panel.c.notna().cumsum()
    return n >= s.min_history_days * per_day


def weights_ts(panel: Panel, sig: pd.DataFrame, s: Settings) -> pd.DataFrame:
    sig = sig.reindex_like(panel.c).fillna(0.0).clip(-1, 1)
    ok = panel.c.notna() & _history_ok(panel, s)
    sig = sig.where(ok, 0.0)
    vol = _vol(panel, s)
    size = (s.target_daily_vol / vol).clip(upper=s.pos_cap)
    changed = sig.ne(sig.shift(1)) & sig.ne(0)
    entry_size = size.where(changed).ffill()
    entry_vol = vol.where(changed).ffill()
    w = (sig * entry_size).where(sig != 0, 0.0).fillna(0.0)
    # 总量上限：用进场时的波动率算"热度"，只有仓位数变了才缩放，持仓期间不会每天微调
    g = w.abs().sum(axis=1)
    heat = (w.abs() * entry_vol).sum(axis=1)
    f = np.minimum(1.0, np.minimum(s.gross_max / g.where(g > 0), s.heat_max / heat.where(heat > 0))).fillna(1.0)
    return w.mul(f, axis=0)


def weights_xs(panel: Panel, score: pd.DataFrame, s: Settings, rebalance: int, long_only: bool = False,
               quantile: float | None = None, gross: float | None = None) -> pd.DataFrame:
    q = quantile or s.xs_quantile
    G = s.gross_max if gross is None else gross
    score = score.reindex_like(panel.c)
    ok = panel.c.notna() & _history_ok(panel, s) & score.notna() & np.isfinite(score)
    vol = _vol(panel, s)
    inv = 1.0 / vol.where(vol > 0)
    W = np.zeros(panel.c.shape)
    sv, okv, iv = score.values, ok.values, inv.values
    n = len(panel.c)
    last = np.zeros(panel.c.shape[1])
    for i in range(n):
        if i % rebalance == 0:
            m = okv[i] & np.isfinite(iv[i])
            idx = np.where(m)[0]
            w = np.zeros(panel.c.shape[1])
            if len(idx) >= s.xs_min_names:
                k = max(s.xs_min_side, int(math.floor(len(idx) * q)))
                order = idx[np.argsort(sv[i, idx], kind="mergesort")]
                top, bot = order[-k:], order[:k]
                if long_only:
                    w[top] = iv[i, top] / iv[i, top].sum() * G
                else:
                    w[top] = iv[i, top] / iv[i, top].sum() * G / 2
                    w[bot] = -iv[i, bot] / iv[i, bot].sum() * G / 2
                heat = float(np.sum(np.abs(w[idx]) / iv[i, idx]))
                if heat > s.heat_max:
                    w *= s.heat_max / heat
            last = w
        W[i] = last
    w = pd.DataFrame(W, index=panel.c.index, columns=panel.c.columns)
    return w.where(panel.c.notna(), 0.0)


def simulate(panel: Panel, w: pd.DataFrame, s: Settings, fee: float | None = None) -> SimResult:
    fee = s.fee if fee is None else fee
    wv = w.reindex_like(panel.c).fillna(0.0).values
    c = panel.c.values
    r = np.nan_to_num(panel.ret().values, nan=0.0)
    pc = np.vstack([np.full((1, c.shape[1]), np.nan), c[:-1]])
    lo = np.nan_to_num(panel.l.values / pc - 1, nan=0.0)
    hi = np.nan_to_num(panel.h.values / pc - 1, nan=0.0)
    weff = np.vstack([np.zeros((1, wv.shape[1])), wv[:-1]])  # 第 t 根持有的是 t-1 根收盘时定的仓位
    fund = np.nan_to_num(panel.funding.values, nan=0.0)
    dw = np.abs(np.diff(np.vstack([np.zeros((1, wv.shape[1])), wv]), axis=0))
    cost_now = dw * fee  # 第 t 根收盘时调仓的费用记在第 t 根
    gain = weff * r
    fpay = -weff * fund
    pnl = gain + fpay - cost_now
    worst_pos = np.where(weff > 0, weff * lo, weff * hi)
    # 交易编号：方向变了（含开仓/平仓）就是新的一笔。手续费归到调仓后那一笔（平仓的归到原来那笔）
    sign = np.sign(wv)
    prev = np.vstack([np.zeros((1, sign.shape[1])), sign[:-1]])
    newtrade = (sign != prev) & (sign != 0)
    cnt = np.cumsum(newtrade, axis=0)
    ncol = sign.shape[1]
    base = (np.arange(ncol) * (len(sign) + 1))[None, :]
    tid_dec = np.where(sign != 0, cnt + base, -1)  # 第 t 根收盘时决定的仓位属于哪笔
    tid_hold = np.vstack([np.full((1, ncol), -1), tid_dec[:-1]])  # 第 t 根持有的那笔
    # 第 t 根的盈亏：持仓收益归 tid_hold；调仓费用：有新仓位归 tid_dec，否则（平仓）归 tid_hold
    tid = np.where(tid_hold >= 0, tid_hold, tid_dec)
    # 把只有费用、没有持仓的（开仓那根）归到新仓位
    cost_tid = np.where(tid_dec >= 0, tid_dec, tid_hold)
    pnl_hold = gain + fpay
    ret = pnl.sum(axis=1)
    worst = worst_pos.sum(axis=1) + fpay.sum(axis=1) - cost_now.sum(axis=1)
    worst = np.minimum(worst, ret)
    return SimResult(index=panel.c.index, ret=ret, worst=worst, cost=cost_now.sum(axis=1), fund=fpay.sum(axis=1),
                     gross=np.abs(weff).sum(axis=1), pnl=np.stack([pnl_hold, -cost_now]),
                     tid=np.stack([tid, cost_tid]), bars_per_year=panel.bars_per_year)


# ---------- 分段指标 ----------
def window_mask(index: pd.DatetimeIndex, a: pd.Timestamp, b: pd.Timestamp) -> np.ndarray:
    return np.asarray((index >= a) & (index < b))


def trade_pnls(res: SimResult, mask: np.ndarray) -> np.ndarray:
    vals = res.pnl[:, mask, :].ravel()
    ids = res.tid[:, mask, :].ravel()
    keep = (ids >= 0) & (vals != 0)
    if not keep.any():
        return np.zeros(0)
    ids, vals = ids[keep], vals[keep]
    u, inv = np.unique(ids, return_inverse=True)
    return np.bincount(inv, weights=vals)


def dd_of(ret: np.ndarray, worst: np.ndarray) -> float:
    if len(ret) == 0:
        return 0.0
    eq = np.cumprod(1 + ret)
    prev = np.concatenate([[1.0], eq[:-1]])
    low = prev * (1 + worst)
    peak = np.maximum.accumulate(np.concatenate([[1.0], eq]))[:-1]
    peak = np.maximum(peak, prev)
    return float(max(0.0, np.max(1 - np.minimum(low, eq) / peak)))


def metrics(ret: np.ndarray, worst: np.ndarray, tp: np.ndarray, bars_per_year: float,
            cost: np.ndarray | None = None, fund: np.ndarray | None = None, gross: np.ndarray | None = None) -> dict:
    n = len(ret)
    tot = float(np.prod(1 + ret) - 1) if n else 0.0
    yrs = n / bars_per_year if n else 0.0
    sd = float(np.std(ret, ddof=1)) if n > 2 else 0.0
    sharpe = float(np.mean(ret) / sd * math.sqrt(bars_per_year)) if sd > 0 else 0.0
    pos, neg = tp[tp > 0].sum(), -tp[tp < 0].sum()
    pf = float(pos / neg) if neg > 0 else (math.inf if pos > 0 else None)
    out = {
        "ret": tot,
        "cagr": float((1 + tot) ** (1 / yrs) - 1) if yrs > 0 and tot > -1 else None,
        "sharpe": sharpe,
        "dd": dd_of(ret, worst),
        "pf": pf,
        "trades": int(len(tp)),
        "win": float((tp > 0).mean()) if len(tp) else None,
        "days": n * 365.0 / bars_per_year,
    }
    if cost is not None:
        out["cost"] = float(cost.sum())
    if fund is not None:
        out["funding"] = float(fund.sum())
    if gross is not None and n:
        out["gross_avg"] = float(gross.mean())
    return out


def seg_metrics(res: SimResult, a: pd.Timestamp, b: pd.Timestamp) -> dict:
    m = window_mask(res.index, a, b)
    return metrics(res.ret[m], res.worst[m], trade_pnls(res, m), res.bars_per_year,
                   res.cost[m], res.fund[m], res.gross[m])


def to_daily(res: SimResult) -> pd.Series:
    s = pd.Series(res.ret, index=res.index)
    if res.bars_per_year <= 366:
        return s
    return (1 + s).groupby(s.index.floor("1D")).prod() - 1


# ---------- 多重检验：Deflated Sharpe Ratio（Bailey & López de Prado 2014） ----------
def expected_max_sr(var_sr: float, n_trials: int) -> float:
    """n 次独立试验、各自真实 Sharpe 都是 0 时，最好那个的 Sharpe 期望（每根口径）。"""
    if n_trials <= 1 or var_sr <= 0:
        return 0.0
    g = 0.5772156649
    return math.sqrt(var_sr) * ((1 - g) * ND.inv_cdf(1 - 1 / n_trials) + g * ND.inv_cdf(1 - 1 / (n_trials * math.e)))


def psr(ret: np.ndarray, sr0: float) -> float:
    """Probabilistic Sharpe Ratio：真实 Sharpe 大于 sr0 的概率（每根口径，考虑偏度、峰度）。"""
    r = np.asarray(ret, dtype=float)
    n = len(r)
    if n < 30:
        return 0.0
    sd = r.std(ddof=1)
    if sd <= 0:
        return 0.0
    sr = r.mean() / sd
    z = (r - r.mean()) / sd
    skew = float(np.mean(z ** 3))
    kurt = float(np.mean(z ** 4))
    den = 1 - skew * sr + (kurt - 1) / 4 * sr * sr
    if den <= 0:
        return 0.0
    return float(ND.cdf((sr - sr0) * math.sqrt(n - 1) / math.sqrt(den)))
