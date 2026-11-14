"""100 个候选策略（第 6 轮初筛）。参数网格在第一次真实运行之前定死，第一组 = 文献 / 原作者的默认值。

每个策略：编号、中文名、类别、来源、周期、参数网格、build(P, s, **参数) → 每根每币的目标仓位（账户百分比）。
  类别：趋势 = 单币时间序列趋势；回归 = 单币均值回归；截面 = 多强空弱（多空对冲）或只做多轮动；
        因子 = WorldQuant《101 Formulaic Alphas》（Kakushadze 2015，arXiv:1601.00991）里的公式，按原式截面排名；
        市场 = 择时 / 日历 / 配对。
  tested：前五轮已经测过的同一类（参数或周期不同），结果不算新证据，报告里会标出来。

改编说明（101 Alphas 原文是美股日线）：原式里的 delta(close) 换成收益率（币价相差几个数量级，差价没法比），
vwap 用 (最高+最低+收盘)/3 近似，adv20 = 20 天平均成交额（USDT）。
"""
from __future__ import annotations

import numpy as np
import pandas as pd

import ind as I
from engine import Settings, weights_ts, weights_xs

BTC = "BTC/USDT:USDT"
ETH = "ETH/USDT:USDT"
STRATEGIES: list[dict] = []


def strat(sid, name, family, source, grid, tf="1d", tested=None):
    def deco(fn):
        STRATEGIES.append(dict(id=sid, name=name, family=family, source=source, grid=grid, tf=tf,
                               tested=tested, build=fn))
        return fn
    return deco


def TS(P, s, sig):
    return weights_ts(P, sig, s)


def XS(P, s, score, reb, long_only=False, q=None):
    return weights_xs(P, score, s, reb, long_only=long_only, quantile=q)


def _col(P, name):
    return P.c[name] if name in P.c.columns else P.c.iloc[:, 0]


def _only(P, cols, series_sig):
    out = pd.DataFrame(0.0, index=P.c.index, columns=P.c.columns)
    for k, v in zip(cols, series_sig):
        if k in out.columns:
            out[k] = v
    return out


def _rowmask(w, mask):
    return w.mul(mask.astype(float).reindex(w.index).fillna(0.0), axis=0)


def _btc_trend(P, n):
    b = _col(P, BTC)
    return b > I.sma(b, n)


def _ls(cond_long, cond_short):
    return cond_long.astype(float) - cond_short.astype(float)


def _beta(P, n):
    r = P.ret()
    b = r[BTC] if BTC in r.columns else r.mean(axis=1)
    cov = r.rolling(n, min_periods=n).cov(b)
    var = b.rolling(n, min_periods=n).var()
    return cov.div(var, axis=0), r, b


def _vwap(P):
    return (P.h + P.l + P.c) / 3


# =====================================================================
# 趋势（单币时间序列）
# =====================================================================
@strat("T01", "均线交叉（SMA 快慢线）", "趋势", "经典；freqtrade-strategies 多个示例策略",
       [dict(f=20, sl=100), dict(f=10, sl=50), dict(f=50, sl=200)])
def t01(P, s, f, sl):
    a, b = I.sma(P.c, f), I.sma(P.c, sl)
    return TS(P, s, _ls(a > b, a < b))


@strat("T02", "指数均线交叉（EMA）", "趋势", "经典 EMA crossover",
       [dict(f=12, sl=26), dict(f=20, sl=60), dict(f=30, sl=120)])
def t02(P, s, f, sl):
    a, b = I.ema(P.c, f), I.ema(P.c, sl)
    return TS(P, s, _ls(a > b, a < b))


@strat("T03", "价格在均线上做多、下做空", "趋势", "Faber 2007 时间序列择时（单均线）",
       [dict(n=100), dict(n=50), dict(n=200)])
def t03(P, s, n):
    m = I.sma(P.c, n)
    return TS(P, s, _ls(P.c > m, P.c < m))


@strat("T04", "时间序列动量（过去 N 天涨跌）", "趋势", "Moskowitz, Ooi, Pedersen 2012",
       [dict(n=90), dict(n=30), dict(n=180)], tested="TsmomTrend（第 2、3 轮）")
def t04(P, s, n):
    r = P.c / P.c.shift(n) - 1
    return TS(P, s, np.sign(r))


@strat("T05", "多周期动量合成", "趋势", "Hurst, Ooi, Pedersen 2017《A Century of Evidence on Trend-Following》",
       [dict(ns=(20, 60, 120, 250)), dict(ns=(10, 30, 90))])
def t05(P, s, ns):
    sig = sum(np.sign(P.c / P.c.shift(n) - 1) for n in ns) / len(ns)
    return TS(P, s, sig)


@strat("T06", "唐奇安通道突破（海龟）", "趋势", "Dennis/Faith《Way of the Turtle》",
       [dict(e=55, x=20), dict(e=20, x=10), dict(e=100, x=50)], tested="DonchianTrend（第 2、3 轮）")
def t06(P, s, e, x):
    hh, ll = I.ts_max(P.h, e).shift(1), I.ts_min(P.l, e).shift(1)
    xh, xl = I.ts_max(P.h, x).shift(1), I.ts_min(P.l, x).shift(1)
    return TS(P, s, I.latch(P.c > hh, P.c < xl, P.c < ll, P.c > xh))


@strat("T07", "肯特纳通道突破", "趋势", "Chester Keltner / Linda Raschke 版（EMA ± ATR 倍数）",
       [dict(n=20, k=2.0), dict(n=50, k=2.0), dict(n=20, k=3.0)])
def t07(P, s, n, k):
    m, a = I.ema(P.c, n), I.atr(P.h, P.l, P.c, n)
    return TS(P, s, I.latch(P.c > m + k * a, P.c < m, P.c < m - k * a, P.c > m))


@strat("T08", "布林带突破", "趋势", "John Bollinger；突破上轨做多、回到中轨平仓",
       [dict(n=20, k=2.0), dict(n=50, k=2.0)])
def t08(P, s, n, k):
    lo, mid, up = I.bollinger(P.c, n, k)
    return TS(P, s, I.latch(P.c > up, P.c < mid, P.c < lo, P.c > mid))


@strat("T09", "MACD 信号线交叉", "趋势", "Gerald Appel",
       [dict(f=12, sl=26, g=9), dict(f=24, sl=52, g=18)])
def t09(P, s, f, sl, g):
    _, _, h = I.macd(P.c, f, sl, g)
    return TS(P, s, np.sign(h))


@strat("T10", "MACD 零轴", "趋势", "Gerald Appel（MACD 线在零轴上方做多）",
       [dict(f=12, sl=26), dict(f=24, sl=52)])
def t10(P, s, f, sl):
    m, _, _ = I.macd(P.c, f, sl, 9)
    return TS(P, s, np.sign(m))


@strat("T11", "Supertrend", "趋势", "Olivier Seban；freqtrade-strategies Supertrend",
       [dict(n=10, k=3.0), dict(n=20, k=3.0), dict(n=10, k=4.0)])
def t11(P, s, n, k):
    return TS(P, s, I.supertrend_dir(P.h, P.l, P.c, n, k))


@strat("T12", "抛物线 SAR", "趋势", "Welles Wilder 1978；je-suis-tm/quant-trading Parabolic SAR",
       [dict(af=0.02, mx=0.2), dict(af=0.01, mx=0.1)])
def t12(P, s, af, mx):
    return TS(P, s, I.psar_dir(P.h, P.l, af, mx))


@strat("T13", "ADX 过滤的 DI 交叉", "趋势", "Welles Wilder 1978（DMI/ADX）",
       [dict(n=14, th=20), dict(n=14, th=25), dict(n=28, th=20)])
def t13(P, s, n, th):
    pdi, ndi, adx = I.dmi(P.h, P.l, P.c, n)
    return TS(P, s, _ls((pdi > ndi) & (adx > th), (pdi < ndi) & (adx > th)))


@strat("T14", "Aroon 振荡器", "趋势", "Tushar Chande 1995",
       [dict(n=25), dict(n=50)])
def t14(P, s, n):
    a = I.aroon_osc(P.h, P.l, n)
    return TS(P, s, I.latch(a > 50, a < 0, a < -50, a > 0))


@strat("T15", "一目均衡表（价格在云上 + 转换线>基准线）", "趋势", "细田悟一；freqtrade-strategies Ichimoku 类",
       [dict(a=9, b=26, c=52), dict(a=20, b=60, c=120)])
def t15(P, s, a, b, c):
    ten = (I.ts_max(P.h, a) + I.ts_min(P.l, a)) / 2
    kij = (I.ts_max(P.h, b) + I.ts_min(P.l, b)) / 2
    sa = ((ten + kij) / 2).shift(b)
    sb = ((I.ts_max(P.h, c) + I.ts_min(P.l, c)) / 2).shift(b)
    top, bot = np.maximum(sa, sb), np.minimum(sa, sb)
    return TS(P, s, _ls((P.c > top) & (ten > kij), (P.c < bot) & (ten < kij)))


@strat("T16", "平均K线（Heikin-Ashi）连续同色", "趋势", "je-suis-tm/quant-trading Heikin-Ashi",
       [dict(k=2), dict(k=3)])
def t16(P, s, k):
    ho, hc = I.heikin_ashi(P.o, P.h, P.l, P.c)
    g = (hc > ho).astype(float)
    r = (hc < ho).astype(float)
    ug = g.rolling(k, min_periods=k).sum() >= k
    ur = r.rolling(k, min_periods=k).sum() >= k
    return TS(P, s, I.latch(ug, ur, ur, ug))


@strat("T17", "KAMA 自适应均线方向", "趋势", "Perry Kaufman《Trading Systems and Methods》",
       [dict(n=10), dict(n=20)])
def t17(P, s, n):
    k = I.kama(P.c, n)
    up = (k > k.shift(1)) & (P.c > k)
    dn = (k < k.shift(1)) & (P.c < k)
    return TS(P, s, I.latch(up, dn, dn, up))


@strat("T18", "Hull 均线斜率", "趋势", "Alan Hull 2005",
       [dict(n=55), dict(n=20), dict(n=100)])
def t18(P, s, n):
    h = I.hma(P.c, n)
    return TS(P, s, np.sign(h - h.shift(1)))


@strat("T19", "TRIX", "趋势", "Jack Hutson",
       [dict(n=15), dict(n=30)])
def t19(P, s, n):
    t = I.ema(I.ema(I.ema(np.log(P.c), n), n), n)
    return TS(P, s, np.sign(t - t.shift(1)))


@strat("T20", "回归斜率 t 值（趋势强度）", "趋势", "统计趋势检验；类似 Clenow《Stocks on the Move》的回归动量",
       [dict(n=60, th=2.0), dict(n=30, th=2.0), dict(n=90, th=2.0)])
def t20(P, s, n, th):
    t = I.lin_slope_t(P.c, n)
    return TS(P, s, _ls(t > th, t < -th))


@strat("T21", "Dual Thrust 区间突破（日线版）", "趋势", "Michael Chalek；je-suis-tm/quant-trading Dual Thrust",
       [dict(n=4, k=0.5), dict(n=4, k=0.7), dict(n=10, k=0.5)])
def t21(P, s, n, k):
    hh, lc = I.ts_max(P.h, n).shift(1), I.ts_min(P.c, n).shift(1)
    hc, ll = I.ts_max(P.c, n).shift(1), I.ts_min(P.l, n).shift(1)
    rng = np.maximum(hh - lc, hc - ll)
    up, dn = P.c > P.o + k * rng, P.c < P.o - k * rng
    return TS(P, s, I.latch(up, dn, dn, up))


@strat("T22", "Vortex 指标", "趋势", "Botes & Siepman 2010",
       [dict(n=14), dict(n=28)])
def t22(P, s, n):
    vmp = (P.h - P.l.shift(1)).abs()
    vmm = (P.l - P.h.shift(1)).abs()
    tr = I.ts_sum(I.true_range(P.h, P.l, P.c), n)
    vip, vim = I.ts_sum(vmp, n) / tr, I.ts_sum(vmm, n) / tr
    return TS(P, s, np.sign(vip - vim))


@strat("T23", "Coppock 曲线（只做多）", "趋势", "Edwin Coppock 1962（月线指标，按日线等比例换算）",
       [dict(a=14, b=11, w=10), dict(a=28, b=22, w=20)])
def t23(P, s, a, b, w):
    cp = I.wma(P.c.pct_change(a, fill_method=None) + P.c.pct_change(b, fill_method=None), w)
    return TS(P, s, (cp > 0).astype(float))


@strat("T24", "Elder 动力系统", "趋势", "Alexander Elder《Come Into My Trading Room》",
       [dict(n=13), dict(n=26)])
def t24(P, s, n):
    e = I.ema(P.c, n)
    _, _, h = I.macd(P.c, 12, 26, 9)
    up = (e > e.shift(1)) & (h > h.shift(1))
    dn = (e < e.shift(1)) & (h < h.shift(1))
    return TS(P, s, I.latch(up, dn, dn, up))


@strat("T25", "低波动时才跟趋势", "趋势", "波动率择时 + 动量（Moreira & Muir 2017 的思路）",
       [dict(n=90, vn=30), dict(n=60, vn=30)])
def t25(P, s, n, vn):
    r = P.ret()
    v = I.stddev(r, vn)
    calm = v < v.rolling(365, min_periods=180).median()
    return TS(P, s, np.sign(P.c / P.c.shift(n) - 1).where(calm, 0.0))


@strat("T26", "只做多：价格在长均线之上", "趋势", "Faber 2007（只做多版本）",
       [dict(n=200), dict(n=100)])
def t26(P, s, n):
    return TS(P, s, (P.c > I.sma(P.c, n)).astype(float))


@strat("T27", "只做多：金叉", "趋势", "经典 50/200 金叉",
       [dict(f=50, sl=200), dict(f=20, sl=100)])
def t27(P, s, f, sl):
    return TS(P, s, (I.sma(P.c, f) > I.sma(P.c, sl)).astype(float))


@strat("T28", "Larry Williams 波动突破（持有 1 天）", "趋势", "Larry Williams《Long-Term Secrets to Short-Term Trading》",
       [dict(k=0.5), dict(k=0.7)])
def t28(P, s, k):
    pr = (P.h - P.l).shift(1)
    return TS(P, s, _ls(P.c > P.o + k * pr, P.c < P.o - k * pr))


@strat("T29", "N 日新高突破 + ATR 追踪止损", "趋势", "Chuck LeBeau 吊灯止损（Chandelier Exit）",
       [dict(n=50, k=3.0), dict(n=20, k=3.0)])
def t29(P, s, n, k):
    hh, ll = I.ts_max(P.c, n).shift(1), I.ts_min(P.c, n).shift(1)
    a = I.atr(P.h, P.l, P.c, 22).values
    c = P.c.values
    el, es = (P.c > hh).values, (P.c < ll).values
    out = np.zeros(c.shape)
    for j in range(c.shape[1]):
        st, ext = 0, np.nan
        for i in range(c.shape[0]):
            if np.isnan(c[i, j]) or np.isnan(a[i, j]):
                out[i, j] = st if not np.isnan(c[i, j]) else 0
                continue
            if st == 1:
                ext = max(ext, c[i, j])
                if c[i, j] < ext - k * a[i, j]:
                    st = 0
            elif st == -1:
                ext = min(ext, c[i, j])
                if c[i, j] > ext + k * a[i, j]:
                    st = 0
            elif el[i, j]:
                st, ext = 1, c[i, j]
            elif es[i, j]:
                st, ext = -1, c[i, j]
            out[i, j] = st
    return TS(P, s, pd.DataFrame(out, index=P.c.index, columns=P.c.columns))


@strat("T30", "区间位置趋势（随机指标高位做多）", "趋势", "George Lane 随机指标的趋势用法",
       [dict(n=20), dict(n=50)])
def t30(P, s, n):
    k = I.stoch_k(P.h, P.l, P.c, n)
    return TS(P, s, I.latch(k > 80, k < 50, k < 20, k > 50))


@strat("T31", "4 小时 EMA 交叉", "趋势", "经典 EMA crossover（4h）",
       [dict(f=20, sl=60), dict(f=50, sl=200)], tf="4h")
def t31(P, s, f, sl):
    a, b = I.ema(P.c, f), I.ema(P.c, sl)
    return TS(P, s, _ls(a > b, a < b))


@strat("T32", "4 小时唐奇安突破", "趋势", "海龟规则（4h）",
       [dict(e=120, x=60), dict(e=60, x=30)], tf="4h", tested="DonchianTrend（日线，第 2、3 轮）")
def t32(P, s, e, x):
    hh, ll = I.ts_max(P.h, e).shift(1), I.ts_min(P.l, e).shift(1)
    xh, xl = I.ts_max(P.h, x).shift(1), I.ts_min(P.l, x).shift(1)
    return TS(P, s, I.latch(P.c > hh, P.c < xl, P.c < ll, P.c > xh))


# =====================================================================
# 均值回归（单币）
# =====================================================================
@strat("M01", "Connors RSI(2)（200 日均线过滤）", "回归", "Larry Connors《Short Term Trading Strategies That Work》",
       [dict(th=10), dict(th=5), dict(th=15)])
def m01(P, s, th):
    r2, m200, m5 = I.rsi(P.c, 2), I.sma(P.c, 200), I.sma(P.c, 5)
    return TS(P, s, I.latch((P.c > m200) & (r2 < th), P.c > m5, (P.c < m200) & (r2 > 100 - th), P.c < m5))


@strat("M02", "RSI(14) 超买超卖", "回归", "Welles Wilder 1978",
       [dict(lo=30, hi=70), dict(lo=25, hi=75), dict(lo=20, hi=80)])
def m02(P, s, lo, hi):
    r = I.rsi(P.c, 14)
    return TS(P, s, I.latch(r < lo, r > 50, r > hi, r < 50))


@strat("M03", "布林带回归", "回归", "John Bollinger；je-suis-tm/quant-trading Bollinger Bands Pattern Recognition 的简化版",
       [dict(n=20, k=2.0), dict(n=20, k=2.5), dict(n=50, k=2.0)])
def m03(P, s, n, k):
    lo, mid, up = I.bollinger(P.c, n, k)
    return TS(P, s, I.latch(P.c < lo, P.c > mid, P.c > up, P.c < mid))


@strat("M04", "Z 分数回归", "回归", "统计均值回归（Chan《Algorithmic Trading》）",
       [dict(n=20), dict(n=10), dict(n=40)])
def m04(P, s, n):
    z = I.zscore(P.c, n)
    return TS(P, s, I.latch(z < -2, z > 0, z > 2, z < 0))


@strat("M05", "威廉 %R", "回归", "Larry Williams",
       [dict(n=14, lo=-90, hi=-10), dict(n=14, lo=-95, hi=-5)])
def m05(P, s, n, lo, hi):
    w = I.williams_r(P.h, P.l, P.c, n)
    return TS(P, s, I.latch(w < lo, w > -50, w > hi, w < -50))


@strat("M06", "随机指标 KD 超买超卖", "回归", "George Lane",
       [dict(n=14, lo=20, hi=80), dict(n=14, lo=10, hi=90)])
def m06(P, s, n, lo, hi):
    k = I.stoch_k(P.h, P.l, P.c, n)
    d = I.sma(k, 3)
    return TS(P, s, I.latch((d < lo) & (k > d), k > 50, (d > hi) & (k < d), k < 50))


@strat("M07", "CCI 回归", "回归", "Donald Lambert 1980",
       [dict(n=20, th=100), dict(n=20, th=200)])
def m07(P, s, n, th):
    c = I.cci(P.h, P.l, P.c, n)
    return TS(P, s, I.latch(c < -th, c > 0, c > th, c < 0))


@strat("M08", "IBS 收盘位置（持有 1 天）", "回归", "Pagonidis《The IBS Effect》",
       [dict(lo=0.2, hi=0.8), dict(lo=0.1, hi=0.9)])
def m08(P, s, lo, hi):
    ibs = (P.c - P.l) / (P.h - P.l + I.EPS * P.c)
    return TS(P, s, _ls(ibs < lo, ibs > hi))


@strat("M09", "连续 N 天下跌后买（持有 3 天）", "回归", "Connors 连跌规则",
       [dict(k=3), dict(k=4), dict(k=5)])
def m09(P, s, k):
    d = (P.c < P.c.shift(1)).astype(float).rolling(k, min_periods=k).sum() >= k
    u = (P.c > P.c.shift(1)).astype(float).rolling(k, min_periods=k).sum() >= k
    return TS(P, s, I.hold_for(d, 3) - I.hold_for(u, 3))


@strat("M10", "累积 RSI(2)", "回归", "Connors & Alvarez《High Probability ETF Trading》",
       [dict(th=35), dict(th=25)])
def m10(P, s, th):
    r2 = I.rsi(P.c, 2)
    cum = r2 + r2.shift(1)
    m200 = I.sma(P.c, 200)
    return TS(P, s, I.latch((P.c > m200) & (cum < th), r2 > 65, (P.c < m200) & (cum > 200 - th), r2 < 35))


@strat("M11", "MFI 资金流量", "回归", "Quong & Soudack",
       [dict(lo=20, hi=80), dict(lo=10, hi=90)])
def m11(P, s, lo, hi):
    m = I.mfi(P.h, P.l, P.c, P.v, 14)
    return TS(P, s, I.latch(m < lo, m > 50, m > hi, m < 50))


@strat("M12", "偏离均线太远就反向", "回归", "均线乖离率",
       [dict(n=20, x=0.15), dict(n=20, x=0.10), dict(n=50, x=0.20)])
def m12(P, s, n, x):
    m = I.sma(P.c, n)
    dev = P.c / m - 1
    return TS(P, s, I.latch(dev < -x, P.c > m, dev > x, P.c < m))


@strat("M13", "肯特纳通道回归", "回归", "Keltner 通道的反向用法",
       [dict(n=20, k=2.0), dict(n=20, k=3.0)])
def m13(P, s, n, k):
    m, a = I.ema(P.c, n), I.atr(P.h, P.l, P.c, n)
    return TS(P, s, I.latch(P.c < m - k * a, P.c > m, P.c > m + k * a, P.c < m))


@strat("M14", "ConnorsRSI 复合指标", "回归", "Connors Research 2012（RSI3 + 连涨跌 RSI + 涨幅百分位）",
       [dict(th=10), dict(th=5)])
def m14(P, s, th):
    up = (P.c > P.c.shift(1)).astype(int) - (P.c < P.c.shift(1)).astype(int)
    st = up.copy().astype(float)
    uv, sv = up.to_numpy(copy=True), st.to_numpy(copy=True)
    for j in range(uv.shape[1]):
        k = 0
        for i in range(uv.shape[0]):
            d = uv[i, j]
            k = (k + 1 if k > 0 else 1) if d > 0 else ((k - 1 if k < 0 else -1) if d < 0 else 0)
            sv[i, j] = k
    st = pd.DataFrame(sv, index=P.c.index, columns=P.c.columns)
    crsi = (I.rsi(P.c, 3) + I.rsi(st, 2) + 100 * I.ts_rank(P.ret(), 100)) / 3
    return TS(P, s, I.latch(crsi < th, crsi > 50, crsi > 100 - th, crsi < 50))


@strat("M15", "单日暴跌后反弹（N 倍波动）", "回归", "极端收益反转（短线）",
       [dict(k=2.0, h=3), dict(k=3.0, h=3), dict(k=2.0, h=1)])
def m15(P, s, k, h):
    r = P.ret()
    sd = I.stddev(r, 60).shift(1)
    return TS(P, s, I.hold_for(r < -k * sd, h) - I.hold_for(r > k * sd, h))


@strat("M16", "上升趋势里买回调（只做多）", "回归", "趋势过滤 + 布林 %B 回调",
       [dict(t=100, n=20), dict(t=50, n=20)])
def m16(P, s, t, n):
    lo, mid, up = I.bollinger(P.c, n, 2.0)
    pb = (P.c - lo) / (up - lo + I.EPS * P.c)
    return TS(P, s, I.latch((P.c > I.sma(P.c, t)) & (pb < 0), pb > 0.5))


@strat("M17", "4 小时 RSI(2) 回调买（日线上升趋势）", "回归", "Connors RSI(2) 的 4h 版",
       [dict(th=10), dict(th=5)], tf="4h")
def m17(P, s, th):
    r2 = I.rsi(P.c, 2)
    up = P.c > I.sma(P.c, 300)
    return TS(P, s, I.latch(up & (r2 < th), r2 > 70))


@strat("M18", "4 小时布林带回归", "回归", "布林带回归（4h）",
       [dict(n=20, k=2.0), dict(n=40, k=2.5)], tf="4h")
def m18(P, s, n, k):
    lo, mid, up = I.bollinger(P.c, n, k)
    return TS(P, s, I.latch(P.c < lo, P.c > mid, P.c > up, P.c < mid))


# =====================================================================
# 截面（多强空弱 / 只做多轮动）
# =====================================================================
@strat("X01", "截面动量（每周调仓）", "截面", "Liu, Tsyvinski, Wu 2022《Common Risk Factors in Cryptocurrency》",
       [dict(n=28), dict(n=14), dict(n=56)], tested="XSMomentum（第 3 轮）")
def x01(P, s, n):
    return XS(P, s, P.c / P.c.shift(n) - 1, 7)


@strat("X02", "截面动量跳过最近一段", "截面", "Jegadeesh & Titman 1993（12-1 月动量的加密版）",
       [dict(n=28, k=7), dict(n=90, k=7), dict(n=180, k=30)])
def x02(P, s, n, k):
    return XS(P, s, P.c.shift(k) / P.c.shift(n) - 1, 7)


@strat("X03", "截面短期反转（买最近跌得多的）", "截面", "Lehmann 1990；加密短期反转",
       [dict(n=7, reb=7), dict(n=3, reb=3), dict(n=1, reb=1)])
def x03(P, s, n, reb):
    return XS(P, s, -(P.c / P.c.shift(n) - 1), reb)


@strat("X04", "低波动（多低波、空高波）", "截面", "Ang et al. 2006；低波动异象",
       [dict(n=30), dict(n=90)])
def x04(P, s, n):
    return XS(P, s, -I.stddev(P.ret(), n), 7)


@strat("X05", "彩票效应 MAX（空最近暴涨过的）", "截面", "Bali, Cakici, Whitelaw 2011；Li et al. 2021 加密 MAX",
       [dict(n=30), dict(n=60)])
def x05(P, s, n):
    return XS(P, s, -I.ts_max(P.ret(), n), 7)


@strat("X06", "成交额增长", "截面", "Liu & Tsyvinski 2021《Risks and Returns of Cryptocurrency》成交量因子",
       [dict(sign=1), dict(sign=-1)])
def x06(P, s, sign):
    g = np.log(I.sma(P.dv, 7) / (I.sma(P.dv, 28) + I.EPS))
    return XS(P, s, sign * g, 7)


@strat("X07", "规模（成交额大小）", "截面", "Liu, Tsyvinski, Wu 2022 规模因子（用成交额代替市值）",
       [dict(sign=-1), dict(sign=1)])
def x07(P, s, sign):
    return XS(P, s, sign * np.log(I.sma(P.dv, 30) + 1), 7)


@strat("X08", "Amihud 非流动性", "截面", "Amihud 2002",
       [dict(n=30), dict(n=90)])
def x08(P, s, n):
    return XS(P, s, I.sma(P.ret().abs() / (P.dv + 1), n), 7)


@strat("X09", "低贝塔（BAB，相对 BTC）", "截面", "Frazzini & Pedersen 2014《Betting Against Beta》",
       [dict(n=60), dict(n=120)])
def x09(P, s, n):
    b, _, _ = _beta(P, n)
    return XS(P, s, -b, 7)


@strat("X10", "低特质波动（剔除 BTC 后）", "截面", "Ang et al. 2006 特质波动率",
       [dict(n=30), dict(n=90)])
def x10(P, s, n):
    b, r, m = _beta(P, 90)
    e = r - b.mul(m, axis=0)
    return XS(P, s, -I.stddev(e, n), 7)


@strat("X11", "偏度（多负偏、空正偏）", "截面", "Boyer, Mitton, Vorkink 2010 预期偏度",
       [dict(n=30), dict(n=90)])
def x11(P, s, n):
    return XS(P, s, -P.ret().rolling(n, min_periods=n).skew(), 7)


@strat("X12", "接近 N 日新高", "截面", "George & Hwang 2004（52 周新高效应）",
       [dict(n=180), dict(n=90), dict(n=365)])
def x12(P, s, n):
    return XS(P, s, P.c / I.ts_max(P.h, n), 7)


@strat("X13", "残差动量（剔除 BTC 贝塔）", "截面", "Blitz, Huij, Martens 2011",
       [dict(n=28), dict(n=56)])
def x13(P, s, n):
    b, r, m = _beta(P, 90)
    e = r - b.shift(1).mul(m, axis=0)
    return XS(P, s, I.ts_sum(e, n) / (I.stddev(e, n) + I.EPS), 7)


@strat("X14", "夏普动量（涨幅 / 波动）", "截面", "风险调整动量（Rachev et al. 2007）",
       [dict(n=28), dict(n=90)])
def x14(P, s, n):
    r = P.ret()
    return XS(P, s, I.sma(r, n) / (I.stddev(r, n) + I.EPS), 7)


@strat("X15", "量价相关（多负相关的）", "截面", "量价背离（101 Alphas 里多个因子的共同成分）",
       [dict(n=30), dict(n=14)])
def x15(P, s, n):
    return XS(P, s, -I.correlation(P.ret(), np.log(P.dv + 1).diff(), n), 7)


@strat("X16", "低下行贝塔", "截面", "Ang, Chen, Xing 2006 下行风险",
       [dict(n=90), dict(n=180)])
def x16(P, s, n):
    r = P.ret()
    m = r[BTC] if BTC in r.columns else r.mean(axis=1)
    dm = m.where(m < 0)
    rd = r.where(dm.notna().values[:, None].repeat(r.shape[1], 1))
    cov = rd.rolling(n, min_periods=n // 3).cov(dm)
    var = dm.rolling(n, min_periods=n // 3).var()
    return XS(P, s, -cov.div(var, axis=0), 7)


@strat("X17", "截面 RSI 排名", "截面", "RSI 用作截面强弱（动量 / 反转两种方向）",
       [dict(sign=1), dict(sign=-1)])
def x17(P, s, sign):
    return XS(P, s, sign * I.rsi(P.c, 14), 7)


@strat("X18", "放量涨的继续涨（量价动量）", "截面", "Gervais, Kaniel, Mingelgrin 2001 高成交量溢价",
       [dict(n=7), dict(n=14)])
def x18(P, s, n):
    spike = I.sma(P.dv, n) / (I.sma(P.dv, 90) + I.EPS)
    return XS(P, s, I.rank(P.c / P.c.shift(n) - 1) * I.rank(spike), 7)


@strat("X19", "与 BTC 协偏度", "截面", "Harvey & Siddique 2000 协偏度",
       [dict(n=90), dict(n=180)])
def x19(P, s, n):
    r = P.ret()
    m = r[BTC] if BTC in r.columns else r.mean(axis=1)
    md = m - I.sma(m, n)
    num = I.sma(r.sub(I.sma(r, n)).mul(md ** 2, axis=0), n)
    den = I.stddev(r, n).mul(I.sma(md ** 2, n), axis=0)  # 2026-11-14 修：以前 DataFrame*Series 按列对齐，全是 NaN，0 笔交易
    return XS(P, s, -(num / (den + I.EPS)), 7)


@strat("X20", "截面动量 + BTC 趋势过滤", "截面", "动量崩溃保护（Daniel & Moskowitz 2016 的思路）",
       [dict(n=28, t=100), dict(n=56, t=100)], tested="XSMomentum（第 3 轮，不带过滤）")
def x20(P, s, n, t):
    w = XS(P, s, P.c / P.c.shift(n) - 1, 7)
    return _rowmask(w, _btc_trend(P, t))


@strat("X21", "只做多：最强币轮动 + BTC 趋势过滤", "截面", "Antonacci《Dual Momentum》的加密版",
       [dict(n=28, q=0.1), dict(n=56, q=0.1), dict(n=28, q=0.2)])
def x21(P, s, n, q):
    w = XS(P, s, P.c / P.c.shift(n) - 1, 7, long_only=True, q=q)
    return _rowmask(w, _btc_trend(P, 100))


@strat("X22", "只做多：低波动篮子", "截面", "低波动异象（只做多）",
       [dict(n=30, filt=1), dict(n=90, filt=1), dict(n=30, filt=0)])
def x22(P, s, n, filt):
    w = XS(P, s, -I.stddev(P.ret(), n), 7, long_only=True)
    return _rowmask(w, _btc_trend(P, 100)) if filt else w


@strat("X23", "相对 BTC 强弱（币/BTC 比值在均线上方）", "截面", "相对强弱轮动",
       [dict(n=20), dict(n=50)])
def x23(P, s, n):
    lr = np.log(P.c).sub(np.log(_col(P, BTC)), axis=0)
    return XS(P, s, lr - I.sma(lr, n), 7)


@strat("X24", "截面趋势质量（回归 t 值）", "截面", "趋势平滑度动量（Clenow 回归动量的截面版）",
       [dict(n=90), dict(n=30)])
def x24(P, s, n):
    return XS(P, s, I.lin_slope_t(P.c, n), 7)


@strat("X25", "4 小时截面短期反转", "截面", "加密日内反转",
       [dict(n=6, reb=6), dict(n=18, reb=6)], tf="4h")
def x25(P, s, n, reb):
    return XS(P, s, -(P.c / P.c.shift(n) - 1), reb)


# =====================================================================
# 101 Formulaic Alphas（Kakushadze 2015）
# =====================================================================
ALPHA_GRID = [dict(reb=1), dict(reb=5)]
A_SRC = "Kakushadze 2015《101 Formulaic Alphas》"


@strat("A001", "Alpha#1", "因子", A_SRC, ALPHA_GRID)
def a001(P, s, reb):
    r = P.ret()
    x = I.stddev(r, 20).where(r < 0, P.c / I.sma(P.c, 20))  # 原式用 close；这里用 close/均线 免得币价量级不同
    return XS(P, s, I.rank(I.ts_argmax(I.signedpower(x, 2), 5)) - 0.5, reb)


@strat("A002", "Alpha#2", "因子", A_SRC, ALPHA_GRID)
def a002(P, s, reb):
    return XS(P, s, -I.correlation(I.rank(I.delta(np.log(P.v + 1), 2)), I.rank((P.c - P.o) / P.o), 6), reb)


@strat("A003", "Alpha#3", "因子", A_SRC, ALPHA_GRID)
def a003(P, s, reb):
    return XS(P, s, -I.correlation(I.rank(P.o), I.rank(P.v), 10), reb)


@strat("A004", "Alpha#4", "因子", A_SRC, ALPHA_GRID)
def a004(P, s, reb):
    return XS(P, s, -I.ts_rank(I.rank(P.l), 9), reb)


@strat("A006", "Alpha#6", "因子", A_SRC, ALPHA_GRID)
def a006(P, s, reb):
    return XS(P, s, -I.correlation(P.o, P.v, 10), reb)


@strat("A009", "Alpha#9", "因子", A_SRC, ALPHA_GRID)
def a009(P, s, reb):
    d = P.ret()
    cond_up = I.ts_min(d, 5) > 0
    cond_dn = I.ts_max(d, 5) < 0
    return XS(P, s, d.where(cond_up | cond_dn, -d), reb)


@strat("A012", "Alpha#12", "因子", A_SRC, ALPHA_GRID)
def a012(P, s, reb):
    return XS(P, s, np.sign(I.delta(P.v, 1)) * -P.ret(), reb)


@strat("A013", "Alpha#13", "因子", A_SRC, ALPHA_GRID)
def a013(P, s, reb):
    return XS(P, s, -I.rank(I.covariance(I.rank(P.c), I.rank(P.v), 5)), reb)


@strat("A016", "Alpha#16", "因子", A_SRC, ALPHA_GRID)
def a016(P, s, reb):
    return XS(P, s, -I.rank(I.covariance(I.rank(P.h), I.rank(P.v), 5)), reb)


@strat("A033", "Alpha#33", "因子", A_SRC, ALPHA_GRID)
def a033(P, s, reb):
    return XS(P, s, I.rank(-(1 - P.o / P.c)), reb)


@strat("A034", "Alpha#34", "因子", A_SRC, ALPHA_GRID)
def a034(P, s, reb):
    r = P.ret()
    return XS(P, s, I.rank((1 - I.rank(I.stddev(r, 2) / (I.stddev(r, 5) + I.EPS))) + (1 - I.rank(r))), reb)


@strat("A038", "Alpha#38", "因子", A_SRC, ALPHA_GRID)
def a038(P, s, reb):
    return XS(P, s, -I.rank(I.ts_rank(P.c, 10)) * I.rank(P.c / P.o), reb)


@strat("A041", "Alpha#41", "因子", A_SRC, ALPHA_GRID)
def a041(P, s, reb):
    vw = _vwap(P)
    return XS(P, s, (np.sqrt(P.h * P.l) - vw) / vw, reb)


@strat("A053", "Alpha#53", "因子", A_SRC, ALPHA_GRID)
def a053(P, s, reb):
    x = ((P.c - P.l) - (P.h - P.c)) / (P.c - P.l + I.EPS * P.c)
    return XS(P, s, -I.delta(x, 9), reb)


@strat("A101", "Alpha#101", "因子", A_SRC, ALPHA_GRID)
def a101(P, s, reb):
    return XS(P, s, (P.c - P.o) / (P.h - P.l + 0.001 * P.c), reb)


# =====================================================================
# 市场择时 / 日历 / 配对
# =====================================================================
@strat("C01", "星期效应（训练段里挑一天做多 BTC）", "市场", "加密星期效应（Caporale & Plastun 2019）",
       [dict(d=d) for d in range(7)])
def c01(P, s, d):
    b = pd.Series(0.0, index=P.c.index)
    nxt = (P.c.index + pd.Timedelta("1D")).dayofweek == d  # 第 t 根收盘后决定第 t+1 根（那天）持有
    b[nxt] = 1.0
    return TS(P, s, _only(P, [BTC], [b]))


@strat("C02", "月末月初做多 BTC", "市场", "月初效应（Ariel 1987；加密版）",
       [dict(a=2, b=3), dict(a=1, b=1)])
def c02(P, s, a, b):
    t = P.c.index + pd.Timedelta("1D")
    dim = t.days_in_month
    on = (t.day <= b) | (t.day > dim - a)
    return TS(P, s, _only(P, [BTC], [pd.Series(on.astype(float), index=P.c.index)]))


@strat("C03", "BTC 低波动时持有", "市场", "波动率择时（Moreira & Muir 2017）",
       [dict(n=30), dict(n=14)])
def c03(P, s, n):
    r = _col(P, BTC).pct_change(fill_method=None)
    v = I.stddev(r, n)
    on = (v < v.rolling(365, min_periods=180).median()).astype(float)
    return TS(P, s, _only(P, [BTC], [on]))


@strat("C04", "ETH/BTC 比值回归", "市场", "配对交易（Gatev, Goetzmann, Rouwenhorst 2006）",
       [dict(n=20), dict(n=60)])
def c04(P, s, n):
    if ETH not in P.c.columns or BTC not in P.c.columns:
        return pd.DataFrame(0.0, index=P.c.index, columns=P.c.columns)
    lr = np.log(P.c[ETH] / P.c[BTC])
    z = (lr - I.sma(lr, n)) / (I.stddev(lr, n) + I.EPS)
    st = I.latch((z < -2).to_frame(), (z > 0).to_frame(), (z > 2).to_frame(), (z < 0).to_frame()).iloc[:, 0]
    return TS(P, s, _only(P, [ETH, BTC], [st, -st]))


@strat("C05", "各币相对 BTC 的价差回归", "市场", "统计套利（Avellaneda & Lee 2010 的简化版）",
       [dict(n=20), dict(n=60)])
def c05(P, s, n):
    lr = np.log(P.c).sub(np.log(_col(P, BTC)), axis=0)
    return XS(P, s, -I.zscore(lr, n), 3)


@strat("C06", "山寨季轮动（山寨强就拿山寨篮子，否则拿 BTC）", "市场", "BTC 主导率轮动",
       [dict(n=30), dict(n=60)])
def c06(P, s, n):
    r = P.c / P.c.shift(n) - 1
    alts = [k for k in P.c.columns if k != BTC]
    alt_r = r[alts].median(axis=1)
    btc_r = r[BTC] if BTC in r.columns else r.mean(axis=1)
    risk_on = _btc_trend(P, 100)
    sig = pd.DataFrame(0.0, index=P.c.index, columns=P.c.columns)
    alt_on = (alt_r > btc_r) & risk_on
    for k in alts:
        sig[k] = alt_on.astype(float)
    if BTC in sig.columns:
        sig[BTC] = (~alt_on & risk_on).astype(float)
    return TS(P, s, sig)


@strat("C07", "市场宽度（多数币在均线上方就做多篮子）", "市场", "市场宽度指标（Zweig breadth）",
       [dict(n=50, up=0.6, dn=0.4), dict(n=20, up=0.7, dn=0.3)])
def c07(P, s, n, up, dn):
    above = (P.c > I.sma(P.c, n)).where(I.sma(P.c, n).notna())
    br = above.mean(axis=1)
    st = I.latch((br > up).to_frame(), (br < 0.5).to_frame(), (br < dn).to_frame(), (br > 0.5).to_frame()).iloc[:, 0]
    sig = pd.DataFrame(np.repeat(st.values[:, None], P.c.shape[1], 1), index=P.c.index, columns=P.c.columns)
    return TS(P, s, sig)


@strat("C08", "BTC 上涨趋势时做多高贝塔币", "市场", "贝塔择时",
       [dict(t=100), dict(t=50)])
def c08(P, s, t):
    b, _, _ = _beta(P, 90)
    w = XS(P, s, b, 7, long_only=True)
    return _rowmask(w, _btc_trend(P, t))


@strat("C09", "等权指数的时间序列动量（整个篮子一起多空）", "市场", "指数级 TSMOM",
       [dict(n=30), dict(n=90)])
def c09(P, s, n):
    idx = (1 + P.ret().mean(axis=1).fillna(0)).cumprod()
    sg = np.sign(idx / idx.shift(n) - 1)
    sig = pd.DataFrame(np.repeat(sg.values[:, None], P.c.shape[1], 1), index=P.c.index, columns=P.c.columns)
    return TS(P, s, sig)


@strat("C10", "4 小时时段效应（训练段里挑一个时段做多 BTC）", "市场", "加密日内季节性（Eross et al. 2019）",
       [dict(slot=k) for k in range(6)], tf="4h")
def c10(P, s, slot):
    nxt = ((P.c.index + pd.Timedelta("4h")).hour // 4) == slot
    b = pd.Series(nxt.astype(float), index=P.c.index)
    return TS(P, s, _only(P, [BTC], [b]))


def check_catalog() -> None:
    ids = [x["id"] for x in STRATEGIES]
    assert len(ids) == len(set(ids)), "编号重复"
    assert len(STRATEGIES) == 100, f"应当正好 100 个策略，现在 {len(STRATEGIES)} 个"
