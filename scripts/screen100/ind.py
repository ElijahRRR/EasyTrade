"""指标和截面算子（全部只用当根及以前的数据，没有未来函数）。

每个函数的输入是 DataFrame（行 = 时间，列 = 币）或 Series，输出同形状。
名字尽量和 WorldQuant《101 Formulaic Alphas》（Kakushadze 2015）里的算子一致：
  rank = 同一时刻截面排名（0~1），ts_rank = 自己过去 n 根里的排名，delta = 差分，delay = 滞后 ...
"""
from __future__ import annotations

import numpy as np
import pandas as pd

EPS = 1e-12


# ---------- 基础 ----------
def sma(x, n):
    return x.rolling(n, min_periods=n).mean()


def ema(x, n):
    return x.ewm(span=n, adjust=False, min_periods=n).mean()


def wilder(x, n):
    return x.ewm(alpha=1.0 / n, adjust=False, min_periods=n).mean()


def stddev(x, n):
    return x.rolling(n, min_periods=n).std()


def delay(x, n=1):
    return x.shift(n)


def delta(x, n=1):
    return x - x.shift(n)


def ts_sum(x, n):
    return x.rolling(n, min_periods=n).sum()


def ts_min(x, n):
    return x.rolling(n, min_periods=n).min()


def ts_max(x, n):
    return x.rolling(n, min_periods=n).max()


def ts_argmax(x, n):
    return x.rolling(n, min_periods=n).apply(np.argmax, raw=True) + 1


def ts_argmin(x, n):
    return x.rolling(n, min_periods=n).apply(np.argmin, raw=True) + 1


def ts_rank(x, n):
    """当根在过去 n 根（含当根）里的百分位排名（0~1）。"""
    return x.rolling(n, min_periods=n).rank(pct=True)


def correlation(x, y, n):
    c = x.rolling(n, min_periods=n).corr(y)
    return c.replace([np.inf, -np.inf], np.nan)


def covariance(x, y, n):
    return x.rolling(n, min_periods=n).cov(y)


def decay_linear(x, n):
    w = np.arange(1, n + 1, dtype=float)
    w /= w.sum()
    return x.rolling(n, min_periods=n).apply(lambda a: float(np.dot(a, w)), raw=True)


def rank(x):
    """截面排名（每一行在币之间排，0~1）。"""
    return x.rank(axis=1, pct=True)


def scale(x):
    return x.div(x.abs().sum(axis=1) + EPS, axis=0)


def signedpower(x, a):
    return np.sign(x) * (x.abs() ** a)


def zscore(x, n):
    return (x - sma(x, n)) / (stddev(x, n) + EPS)


def xs_demean(x):
    return x.sub(x.mean(axis=1), axis=0)


# ---------- 技术指标 ----------
def true_range(h, l, c):
    pc = c.shift(1)
    a = h - l
    b = (h - pc).abs()
    d = (l - pc).abs()
    if isinstance(c, pd.DataFrame):
        return np.maximum(np.maximum(a, b), d)
    return pd.concat([a, b, d], axis=1).max(axis=1)


def atr(h, l, c, n):
    return wilder(true_range(h, l, c), n)


def rsi(c, n):
    d = c.diff()
    up = wilder(d.clip(lower=0), n)
    dn = wilder((-d).clip(lower=0), n)
    return 100 - 100 / (1 + up / (dn + EPS))


def macd(c, f, s, sig):
    m = ema(c, f) - ema(c, s)
    sg = ema(m, sig)
    return m, sg, m - sg


def bollinger(c, n, k):
    m = sma(c, n)
    sd = stddev(c, n)
    return m - k * sd, m, m + k * sd


def stoch_k(h, l, c, n):
    lo, hi = ts_min(l, n), ts_max(h, n)
    return 100 * (c - lo) / (hi - lo + EPS)


def williams_r(h, l, c, n):
    return stoch_k(h, l, c, n) - 100


def cci(h, l, c, n):
    tp = (h + l + c) / 3
    m = sma(tp, n)
    md = tp.rolling(n, min_periods=n).apply(lambda a: np.mean(np.abs(a - a.mean())), raw=True)
    return (tp - m) / (0.015 * md + EPS)


def mfi(h, l, c, v, n):
    tp = (h + l + c) / 3
    mf = tp * v
    pos = mf.where(tp > tp.shift(1), 0.0)
    neg = mf.where(tp < tp.shift(1), 0.0)
    return 100 - 100 / (1 + ts_sum(pos, n) / (ts_sum(neg, n) + EPS))


def dmi(h, l, c, n):
    up = h.diff()
    dn = -l.diff()
    pdm = up.where((up > dn) & (up > 0), 0.0)
    ndm = dn.where((dn > up) & (dn > 0), 0.0)
    tr = wilder(true_range(h, l, c), n)
    pdi = 100 * wilder(pdm, n) / (tr + EPS)
    ndi = 100 * wilder(ndm, n) / (tr + EPS)
    dx = 100 * (pdi - ndi).abs() / (pdi + ndi + EPS)
    return pdi, ndi, wilder(dx, n)


def aroon_osc(h, l, n):
    up = 100 * (ts_argmax(h, n + 1) - 1) / n
    dn = 100 * (ts_argmin(l, n + 1) - 1) / n
    return up - dn


def kama(c, n, fast=2, slow=30):
    """Kaufman 自适应均线（逐列循环）。"""
    out = pd.DataFrame(np.nan, index=c.index, columns=c.columns)
    fsc, ssc = 2 / (fast + 1), 2 / (slow + 1)
    change = (c - c.shift(n)).abs()
    vol = c.diff().abs().rolling(n, min_periods=n).sum()
    er = (change / (vol + EPS)).values
    sc = (er * (fsc - ssc) + ssc) ** 2
    cv = c.to_numpy(copy=True)
    ov = out.to_numpy(copy=True)
    for j in range(cv.shape[1]):
        k = np.nan
        for i in range(cv.shape[0]):
            if np.isnan(cv[i, j]) or np.isnan(sc[i, j]):
                k = np.nan if np.isnan(cv[i, j]) else k
                continue
            k = cv[i, j] if np.isnan(k) else k + sc[i, j] * (cv[i, j] - k)
            ov[i, j] = k
    return pd.DataFrame(ov, index=c.index, columns=c.columns)


def wma(x, n):
    return decay_linear(x, n)


def hma(c, n):
    return wma(2 * wma(c, max(n // 2, 1)) - wma(c, n), max(int(np.sqrt(n)), 1))


def supertrend_dir(h, l, c, n, mult):
    """Supertrend 方向：+1 / -1（逐列循环，标准算法）。"""
    a = atr(h, l, c, n).values
    hv, lv, cv = h.values, l.values, c.values
    mid = (hv + lv) / 2
    ub0, lb0 = mid + mult * a, mid - mult * a
    out = np.full(cv.shape, np.nan)
    for j in range(cv.shape[1]):
        ub = lb = np.nan
        d = 1
        for i in range(cv.shape[0]):
            if np.isnan(ub0[i, j]) or np.isnan(cv[i, j]):
                continue
            pc = cv[i - 1, j] if i > 0 else np.nan
            nub = ub0[i, j] if (np.isnan(ub) or ub0[i, j] < ub or (not np.isnan(pc) and pc > ub)) else ub
            nlb = lb0[i, j] if (np.isnan(lb) or lb0[i, j] > lb or (not np.isnan(pc) and pc < lb)) else lb
            if not np.isnan(ub):
                if d == 1 and cv[i, j] < nlb:
                    d = -1
                elif d == -1 and cv[i, j] > nub:
                    d = 1
            ub, lb = nub, nlb
            out[i, j] = d
    return pd.DataFrame(out, index=c.index, columns=c.columns)


def psar_dir(h, l, af0, afmax):
    """抛物线 SAR 方向：+1 / -1。"""
    hv, lv = h.values, l.values
    out = np.full(hv.shape, np.nan)
    for j in range(hv.shape[1]):
        idx = np.where(~np.isnan(hv[:, j]) & ~np.isnan(lv[:, j]))[0]
        if len(idx) < 3:
            continue
        i0 = idx[0]
        d, sar, ep, af = 1, lv[i0, j], hv[i0, j], af0
        for i in idx[1:]:
            sar = sar + af * (ep - sar)
            if d == 1:
                if lv[i, j] < sar:
                    d, sar, ep, af = -1, ep, lv[i, j], af0
                elif hv[i, j] > ep:
                    ep, af = hv[i, j], min(af + af0, afmax)
            else:
                if hv[i, j] > sar:
                    d, sar, ep, af = 1, ep, hv[i, j], af0
                elif lv[i, j] < ep:
                    ep, af = lv[i, j], min(af + af0, afmax)
            out[i, j] = d
    return pd.DataFrame(out, index=h.index, columns=h.columns)


def lin_slope_t(c, n):
    """过去 n 根对数价格对时间回归的斜率 t 值（趋势强度）。"""
    y = np.log(c)
    t = pd.Series(np.arange(len(c), dtype=float), index=c.index)
    tt = pd.DataFrame({k: t for k in c.columns})
    cov = covariance(y, tt, n)
    vt = tt.rolling(n, min_periods=n).var()
    b = cov / (vt + EPS)
    r = correlation(y, tt, n)
    r2 = (r ** 2).clip(upper=0.999999)
    return r * np.sqrt((n - 2) / (1 - r2))


def heikin_ashi(o, h, l, c):
    hc = (o + h + l + c) / 4
    ho = pd.DataFrame(np.nan, index=o.index, columns=o.columns)
    ov, cv, hov = o.to_numpy(copy=True), hc.to_numpy(copy=True), ho.to_numpy(copy=True)
    for j in range(ov.shape[1]):
        prev = np.nan
        for i in range(ov.shape[0]):
            if np.isnan(ov[i, j]):
                continue
            prev = (ov[i, j] + cv[i, j]) / 2 if np.isnan(prev) else (prev + cv[i - 1, j]) / 2
            hov[i, j] = prev
    return pd.DataFrame(hov, index=o.index, columns=o.columns), hc


# ---------- 状态机 ----------
def latch(enter_long, exit_long, enter_short=None, exit_short=None):
    """进场/出场信号 → 持仓状态 +1 / 0 / -1（逐列循环；同一根同时有进场和出场时，出场优先，下一根再看进场）。"""
    el = enter_long.fillna(False).values.astype(bool)
    xl = exit_long.fillna(False).values.astype(bool)
    es = enter_short.fillna(False).values.astype(bool) if enter_short is not None else np.zeros_like(el)
    xs = exit_short.fillna(False).values.astype(bool) if exit_short is not None else np.zeros_like(el)
    out = np.zeros(el.shape)
    for j in range(el.shape[1]):
        s = 0
        for i in range(el.shape[0]):
            if s == 1 and xl[i, j]:
                s = 0
            elif s == -1 and xs[i, j]:
                s = 0
            elif s == 0:
                if el[i, j]:
                    s = 1
                elif es[i, j]:
                    s = -1
            out[i, j] = s
    return pd.DataFrame(out, index=enter_long.index, columns=enter_long.columns)


def hold_for(trigger, n, sign=1.0):
    """触发后持有 n 根（含触发那根的下一根起由引擎自动滞后一根）。"""
    t = trigger.fillna(False).astype(float)
    return (t.rolling(n, min_periods=1).max() > 0).astype(float) * sign
