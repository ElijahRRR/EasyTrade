"""第 7 轮：最近一年（2025-11 ~ 2026-11）公开推广、现有数据能测、前六轮没测过的 6 个候选（docs/round7-promoted.md）。

和第 6 轮用同一个引擎、同一套门槛（configs/screen7.json 里 "catalog": "catalog7"）。参数网格在第一次真实运行之前定死。
推广材料大多没有给出完整规则，下面每个策略开头都写了"原文怎么说、这里怎么补"，补的部分用的是常见默认值，不是调出来的。

  R01 / R02  AdaptiveTrend（arXiv:2602.11708）4 小时版 / 日线版
  R03        流动性扫荡 + 位移K线 + 缺口回踩（ICT / Smart Money Concepts）
  R04        多指标打分趋势（TrendRider 一类的"八项指标打分"）
  R05        网格（中性合约网格 / 只做多的现货式网格）
  R06        马丁格尔加仓（DCA Martingale，越跌越买、回到均价上方一点就全平）

1 小时策略（R03 ~ R06）的近似：信号、止损、止盈、网格成交都只在每根 1 小时K线收盘时判断，成交价 = 收盘价。
真实的网格 / 止损单是在K线内部成交的，所以这里的结果只算初筛；通过的要像第 6 轮那样改写成 freqtrade 版再正式测。
资金费率套利（清单第 10 条）不在这里：对冲以后价格盈亏按 0 算，套门槛没有意义，单独用 carry7.py 估算每年净收益。
"""
from __future__ import annotations

import numpy as np
import pandas as pd

import ind as I
from catalog import TS
from engine import _vol

STRATEGIES: list[dict] = []
NEUTRAL: set[str] = set()  # 这一轮没有多空对冲类（中性网格不是对冲，照普通门槛 PF ≥ 1.2）

# 自检第二步：合成行情里每个币围绕自己的中枢来回摆 → 网格应当通过，趋势类不应当通过
SELF_TEST = {"plant": "ou", "label": "植入均值回归（每个币围绕中枢来回摆）的合成行情 → R05 网格应当通过，R01 趋势不应当通过",
             "only": "R01 R05 R06", "must_pass": "R05", "must_not_pass": "R01"}


def strat(sid, name, family, source, grid, tf="1d", tested=None):
    def deco(fn):
        STRATEGIES.append(dict(id=sid, name=name, family=family, source=source, grid=grid, tf=tf,
                               tested=tested, build=fn))
        return fn
    return deco


def _df(P, a):
    return pd.DataFrame(a, index=P.c.index, columns=P.c.columns)


# =====================================================================
# R01 / R02  AdaptiveTrend
# =====================================================================
# 原文（Bui & Nguyen 2026，Binance 永续 150+ 个币，6 小时K线，自报 2022 ~ 2024 样本外夏普 2.41、回撤 12.7%）：
#   * 进场：动量 MOM = P_t / P_{t-L} - 1 > θ 做多，< -θ 做空；
#   * 出场：追踪止损 S_t = max(S_{t-1}, P_t - α·ATR_t)，收盘跌破就平（空头反过来）；
#   * 每月第一天：市值前 15 名是做多候选，市值靠后的是做空候选；每个币用上个月的数据网格搜索 (L, θ, α)，
#     上个月夏普 ≥ 1.3（多）/ 1.7（空）才留下；多头一共占 70%、空头 30%，组内等权；窗口之间留 24 小时。
# 原文没写、这里补的：L / θ / α 的搜索范围（下面三行）、ATR 周期（14 根）、做空候选个数（成交额最小的 15 个）；
#   市值没有数据，用过去 30 天平均成交额代替（和第 6 轮 X07 规模因子一样）；没有 6 小时数据，用 4 小时（原文 4 小时版自报夏普 2.08）
#   和日线（自报 1.63）；仓位不用原文的"70% / 选中的币数"（选中 1 ~ 2 个币时单币会占到 35% ~ 70%，超出项目风控），
#   改成和其他候选一样按波动率定仓，空头每笔是多头的 0.3 / 0.7 倍。
AT_DAYS = (2, 4, 7, 14)
AT_THETA = (0.03, 0.06, 0.10)
AT_ALPHA = (2.0, 2.5, 3.5)
AT_TOP = 15


def _at_states(c, a, mom, theta, alpha, side):
    """沿时间循环、各币并行。side = +1 只做多 / -1 只做空。返回每根收盘时的持仓状态（0 / 1）。"""
    T, N = c.shape
    st = np.zeros(N, dtype=bool)
    stop = np.full(N, np.nan)
    out = np.zeros((T, N), dtype=np.int8)
    with np.errstate(invalid="ignore"):
        for i in range(T):
            ci, ai, mi = c[i], a[i], mom[i]
            ok = np.isfinite(ci) & np.isfinite(ai)
            if side > 0:
                ns = ci - alpha * ai
                stop = np.where(st & ok, np.fmax(stop, ns), stop)
                ex = st & ok & (ci < stop)
                en = ~st & ok & (mi > theta)
            else:
                ns = ci + alpha * ai
                stop = np.where(st & ok, np.fmin(stop, ns), stop)
                ex = st & ok & (ci > stop)
                en = ~st & ok & (mi < -theta)
            st = (st & ~ex) | en
            stop = np.where(en, ns, stop)
            out[i] = st
    return out


def _adaptive_trend(P, s, gl, gs):
    c = P.c.values
    a = I.atr(P.h, P.l, P.c, 14).values
    r = np.nan_to_num(P.ret().values, nan=0.0)
    idx = P.c.index
    per_day = int(round(P.bars_per_year / 365.0))
    T, N = c.shape
    month = (idx.year * 12 + idx.month).values
    months = np.unique(month)
    mpos = np.searchsorted(months, month)
    # 上个月的统计不用最后一天（原文：窗口之间留 24 小时）
    use = np.asarray(idx.day != idx.days_in_month)
    ann = np.sqrt(P.bars_per_year)
    combos = [(d, th, al) for d in AT_DAYS for th in AT_THETA for al in AT_ALPHA]
    states = {1: [], -1: []}
    sharpe = {1: [], -1: []}
    onehot = np.zeros((len(months), T))
    onehot[mpos[use], np.where(use)[0]] = 1.0
    cnt = onehot.sum(axis=1)[:, None]
    for d, th, al in combos:
        mom = (P.c / P.c.shift(d * per_day) - 1).values
        for side in (1, -1):
            st = _at_states(c, a, mom, th, al, side)
            held = np.vstack([np.zeros((1, N)), st[:-1]]) * side
            chg = np.abs(np.diff(np.vstack([np.zeros((1, N)), st]), axis=0))
            br = held * r - chg * s.fee
            m1 = onehot @ br / np.maximum(cnt, 1)
            m2 = onehot @ (br * br) / np.maximum(cnt, 1)
            sd = np.sqrt(np.maximum(m2 - m1 * m1, 0.0))
            with np.errstate(invalid="ignore", divide="ignore"):
                sh = np.where(sd > 0, m1 / sd * ann, -np.inf)
            states[side].append(st)
            sharpe[side].append(sh)
    # 成交额排名：用上个月最后一根为止的 30 天平均成交额
    dvm = P.dv.rolling(30 * per_day, min_periods=10 * per_day).mean().values
    sig = np.zeros((T, N))
    for k in range(1, len(months)):
        rows = np.where(mpos == k)[0]
        prev = np.where(mpos == k - 1)[0]
        if len(rows) == 0 or len(prev) == 0:
            continue
        dv = dvm[prev[-1]]
        have = np.where(np.isfinite(dv))[0]
        if len(have) < 2 * AT_TOP:
            continue
        order = have[np.argsort(dv[have], kind="mergesort")]
        for side, cand, gate, wgt in ((1, order[-AT_TOP:], gl, 1.0), (-1, order[:AT_TOP], gs, -0.3 / 0.7)):
            sh = np.stack([x[k - 1] for x in sharpe[side]])  # 组合 × 币：上个月的夏普
            best = np.argmax(sh[:, cand], axis=0)
            for j, b in zip(cand, best):
                if sh[b, j] >= gate and np.isfinite(sh[b, j]):
                    sig[rows, j] += wgt * states[side][b][rows, j]
    return TS(P, s, _df(P, sig))


AT_SRC = "Bui & Nguyen 2026《AdaptiveTrend》arXiv:2602.11708（原文 6 小时K线；市值用成交额代替；仓位按波动率）"
AT_GRID = [dict(gl=1.3, gs=1.7), dict(gl=0.0, gs=0.0)]


@strat("R01", "AdaptiveTrend 4 小时版（每月按上月夏普挑币和参数，多 70% 空 30%，ATR 追踪止损）", "趋势", AT_SRC, AT_GRID, tf="4h")
def r01(P, s, gl, gs):
    return _adaptive_trend(P, s, gl, gs)


@strat("R02", "AdaptiveTrend 日线版", "趋势", AT_SRC, AT_GRID, tf="1d")
def r02(P, s, gl, gs):
    return _adaptive_trend(P, s, gl, gs)


# =====================================================================
# R03  流动性扫荡 + 位移K线 + 缺口回踩（ICT / Smart Money Concepts）
# =====================================================================
# 原文（backtrex 2026-10 的"5 年回测"，只测了欧元和黄金，自报 PF 1.68、回撤 14.3%）：
#   价格刺破前高 / 前低（扫流动性）→ 2 根之内出现实体占 70% 以上的反向K线（位移）→ 位移留下缺口（FVG），
#   回踩缺口中点进场；止损放在被扫的价位外面一点，止盈至少 1.5 倍风险；只在伦敦、纽约开盘时段做。
# 这里补的：外汇的"点"换成 0.1 倍 ATR(14)；前高 / 前低 = 之前 n 根的最高 / 最低；缺口回踩最多等 12 根；
#   时段过滤照原文写的 UTC 2 ~ 5 点和 7 ~ 10 点（kz=1），加密货币 24 小时交易，所以另给不过滤的版本（kz=0）。
def _sweep_one(o, h, l, c, a, lvl_hi, lvl_lo, hour_ok, rr, wait=12):
    T = len(c)
    out = np.zeros(T)
    buf = 0.1
    # 最近一次扫荡
    s_bar_hi = s_bar_lo = -10
    s_lvl_hi = s_ext_hi = s_lvl_lo = s_ext_lo = 0.0
    # 等回踩的缺口：方向、中点、止损、过期
    p_dir, p_mid, p_stop, p_exp = 0, 0.0, 0.0, -1
    pos, stop, tp = 0, 0.0, 0.0
    for i in range(2, T):
        ci, hi, li, ai = c[i], h[i], l[i], a[i]
        if ci != ci or ai != ai or lvl_hi[i] != lvl_hi[i]:
            out[i] = pos
            continue
        # ---- 持仓：先看止损，再看止盈（同一根都碰到按止损算）
        if pos == 1:
            if li <= stop or hi >= tp:
                pos = 0
        elif pos == -1:
            if hi >= stop or li <= tp:
                pos = 0
        # ---- 记录扫荡
        if hi > lvl_hi[i] + buf * ai:
            s_bar_hi, s_lvl_hi, s_ext_hi = i, lvl_hi[i], hi
        elif i - s_bar_hi <= 2 and hi > s_ext_hi:
            s_ext_hi = hi
        if li < lvl_lo[i] - buf * ai:
            s_bar_lo, s_lvl_lo, s_ext_lo = i, lvl_lo[i], li
        elif i - s_bar_lo <= 2 and li < s_ext_lo:
            s_ext_lo = li
        # ---- 上一根是位移K线、这一根确认缺口 → 挂起等回踩
        j = i - 1
        rng = h[j] - l[j]
        if rng > 0 and hour_ok[j]:
            if (j - s_bar_hi <= 2 and c[j] < o[j] and (o[j] - c[j]) >= 0.7 * rng and c[j] < s_lvl_hi
                    and hi < l[j - 1]):
                p_dir, p_mid, p_stop, p_exp = -1, (hi + l[j - 1]) / 2, s_ext_hi + buf * a[j], i + wait
            elif (j - s_bar_lo <= 2 and c[j] > o[j] and (c[j] - o[j]) >= 0.7 * rng and c[j] > s_lvl_lo
                    and li > h[j - 1]):
                p_dir, p_mid, p_stop, p_exp = 1, (li + h[j - 1]) / 2, s_ext_lo - buf * a[j], i + wait
        # ---- 回踩进场（这一根碰到中点，按收盘价进）
        if pos == 0 and p_dir != 0:
            if i > p_exp:
                p_dir = 0
            elif p_dir == -1 and hi >= p_mid and i > j:
                risk = p_stop - ci
                if risk > 0 and hi < p_stop:
                    pos, stop, tp = -1, p_stop, ci - rr * risk
                p_dir = 0
            elif p_dir == 1 and li <= p_mid and i > j:
                risk = ci - p_stop
                if risk > 0 and li > p_stop:
                    pos, stop, tp = 1, p_stop, ci + rr * risk
                p_dir = 0
        out[i] = pos
    return out


@strat("R03", "流动性扫荡 + 位移K线 + 缺口回踩（1 小时）", "回归",
       "ICT / Smart Money Concepts；backtrex 2026-10《Liquidity Sweep Strategy: 5-Year Backtest》（原文只测了外汇和黄金）",
       [dict(n=24, rr=1.5, kz=1), dict(n=24, rr=1.5, kz=0), dict(n=48, rr=2.5, kz=0), dict(n=48, rr=1.5, kz=1)], tf="1h")
def r03(P, s, n, rr, kz):
    a = I.atr(P.h, P.l, P.c, 14).values
    lvl_hi = I.ts_max(P.h, n).shift(1).values
    lvl_lo = I.ts_min(P.l, n).shift(1).values
    hr = P.c.index.hour.values
    hour_ok = np.isin(hr, (2, 3, 4, 7, 8, 9)) if kz else np.ones(len(hr), dtype=bool)
    O, H, L, C = P.o.values, P.h.values, P.l.values, P.c.values
    out = np.zeros(C.shape)
    for j in range(C.shape[1]):
        out[:, j] = _sweep_one(O[:, j], H[:, j], L[:, j], C[:, j], a[:, j], lvl_hi[:, j], lvl_lo[:, j], hour_ok, rr)
    return TS(P, s, _df(P, out))


# =====================================================================
# R04  多指标打分趋势
# =====================================================================
# 原文（darkvolg/Trading 的 TrendRider，Bybit 永续 13 个币，1 小时，自报回测胜率 67.9%）：EMA 交叉、RSI、MACD 柱、ADX、
#   布林带、ATR、成交量比、行情状态八项各 0 ~ 2 分，高分才开仓；出场有分段的早期止损、24 小时时间出场、
#   趋势破坏出场、调参得到的止盈表和止损。具体阈值在作者的策略文件里，而且每次调参都会变，README 没有给。
# 这里补的：八项的打分规则用下面的常见写法（16 分折算成 10 分）；出场只保留能说清楚的三条：
#   趋势破坏（EMA20 / EMA50 反向）、时间出场、收盘价离进场价超过 2 倍 ATR 止损（持有满 4 根后收紧到 1 倍）；出场后停 2 根。
def _scores(P):
    c, h, l, v = P.c, P.h, P.l, P.v
    e20, e50, e200, e720 = I.ema(c, 20), I.ema(c, 50), I.ema(c, 200), I.ema(c, 720)
    rsi = I.rsi(c, 14)
    hist = I.macd(c, 12, 26, 9)[2]
    pdi, mdi, adx = I.dmi(h, l, c, 14)
    lo, mid, up = I.bollinger(c, 20, 2.0)
    bw = (up - lo) / mid
    atrp = I.atr(h, l, c, 14) / c
    q20 = atrp.rolling(720, min_periods=240).quantile(0.2)
    q80 = atrp.rolling(720, min_periods=240).quantile(0.8)
    q95 = atrp.rolling(720, min_periods=240).quantile(0.95)
    vr = v / I.sma(v, 20)
    f = lambda x: x.astype(float)
    volreg = f(atrp < q95) + f((atrp > q20) & (atrp < q80))
    volume = f(vr > 1.0) + f(vr > 1.5)
    strong = f(adx > 20) + f(adx > 30)
    widen = bw > bw.shift(5)
    lng = (f(e20 > e50) + f((e20 > e50) & (e50 > e200))
           + f((rsi >= 40) & (rsi < 80)) + f((rsi >= 50) & (rsi < 70))
           + f(hist > 0) + f((hist > 0) & (hist > hist.shift(1)))
           + strong.where(pdi > mdi, 0.0)
           + f(c > mid) + f((c > mid) & widen)
           + volreg + volume
           + f(c > e720) + f((c > e720) & (e720 > e720.shift(24))))
    sht = (f(e20 < e50) + f((e20 < e50) & (e50 < e200))
           + f((rsi > 20) & (rsi <= 60)) + f((rsi > 30) & (rsi <= 50))
           + f(hist < 0) + f((hist < 0) & (hist < hist.shift(1)))
           + strong.where(mdi > pdi, 0.0)
           + f(c < mid) + f((c < mid) & widen)
           + volreg + volume
           + f(c < e720) + f((c < e720) & (e720 < e720.shift(24))))
    return lng * 10 / 16, sht * 10 / 16, e20, e50, atrp * c


def _score_one(c, sl, ss, up, a, thr, hold):
    T = len(c)
    out = np.zeros(T)
    pos, entry, ea, age, cool = 0, 0.0, 0.0, 0, 0
    for i in range(T):
        ci = c[i]
        if ci != ci or a[i] != a[i] or sl[i] != sl[i]:
            out[i] = pos
            continue
        if pos != 0:
            age += 1
            move = (ci - entry) * pos
            if (up[i] != pos) or age >= hold or move < -2.0 * ea or (age >= 4 and move < -1.0 * ea):
                pos, cool = 0, 2
        elif cool > 0:
            cool -= 1
        elif sl[i] >= thr and sl[i] > ss[i]:
            pos, entry, ea, age = 1, ci, a[i], 0
        elif ss[i] >= thr and ss[i] > sl[i]:
            pos, entry, ea, age = -1, ci, a[i], 0
        out[i] = pos
    return out


@strat("R04", "多指标打分趋势（八项指标各 0 ~ 2 分，高分开仓，1 小时）", "趋势",
       "darkvolg/Trading 的 TrendRider（GitHub，2026）一类；阈值原文没给，用常见默认值重建",
       [dict(thr=7.5, hold=24), dict(thr=8.75, hold=24), dict(thr=7.5, hold=48)], tf="1h",
       tested="多个单指标趋势策略（第 6 轮 T 类）；这种八项打分的组合没测过")
def r04(P, s, thr, hold):
    lng, sht, e20, e50, a = _scores(P)
    up = np.where((e20 > e50).values, 1, -1)
    C, SL, SS, A = P.c.values, lng.values, sht.values, a.values
    out = np.zeros(C.shape)
    for j in range(C.shape[1]):
        out[:, j] = _score_one(C[:, j], SL[:, j], SS[:, j], up[:, j], A[:, j], thr, hold)
    return TS(P, s, _df(P, out))


# =====================================================================
# R05  网格
# =====================================================================
# 原文（OKX / 币安 / Pionex / Bitsgap 的现货网格、合约网格）：定一个价格区间，分成 n 档，跌一档买一份、涨一档卖一份。
#   平台不规定区间怎么选，也不默认带止损；评测文章提醒价格单边走出区间时会留下浮亏。
# 这里的写法：开网格时中枢 = 当时收盘价，半宽 = m × 该币 30 天日波动 × 价格；
#   mode=0 中性合约网格：仓位 = -(价格离中枢的档数 / n)，中枢下方做多、上方做空，到区间边上是 1 个满仓位；
#   mode=1 只做多（现货网格的合约版）：中枢处半仓，区间底满仓，区间顶空仓；
#   价格走出区间 25% 就全平（止损），停 24 小时后以新价格为中枢重开。"满仓位"的大小和其他候选一样按波动率定。
@strat("R05", "网格（中性合约网格 / 只做多网格，1 小时，走出区间止损后重开）", "回归",
       "OKX / 币安 / Pionex 等平台的网格机器人（Coin Bureau 2026-08、Koinly 2026 评测）",
       [dict(m=3.0, n=10, mode=0), dict(m=5.0, n=10, mode=0), dict(m=3.0, n=10, mode=1), dict(m=5.0, n=10, mode=1)], tf="1h")
def r05(P, s, m, n, mode):
    c = P.c.values
    vol = _vol(P, s).values
    T, N = c.shape
    center = np.full(N, np.nan)
    half = np.full(N, np.nan)
    active = np.zeros(N, dtype=bool)
    cool = np.zeros(N, dtype=int)
    out = np.zeros((T, N))
    with np.errstate(invalid="ignore", divide="ignore"):
        for i in range(T):
            ci = c[i]
            ok = np.isfinite(ci) & np.isfinite(vol[i]) & (vol[i] > 0)
            start = ~active & ok & (cool == 0)
            center = np.where(start, ci, center)
            half = np.where(start, m * vol[i] * ci, half)
            cool = np.where(~active & ~start, np.maximum(cool - 1, 0), cool)
            active = active | start
            x = (ci - center) / half
            stop = active & np.isfinite(ci) & (np.abs(x) > 1.25)
            active = active & ~stop
            cool = np.where(stop, 24, cool)
            lvl = np.round(np.clip(x, -1, 1) * n) / n
            sig = -lvl if mode == 0 else (1 - lvl) / 2
            out[i] = np.where(active, np.nan_to_num(sig), 0.0)
    return TS(P, s, _df(P, out))


# =====================================================================
# R06  马丁格尔加仓（DCA Martingale）
# =====================================================================
# 原文（OKX 现货 / 合约马丁格尔、3Commas / Pionex 的 DCA 机器人）：开一份底仓，价格每跌一档就补一次仓，补仓金额按倍数放大，
#   均价回到盈利一小段就全部平掉、马上开下一轮；默认不带止损。
# 这里的写法：只做多；一档 = step × 该币 30 天日波动，止盈 = 均价上方 tp × 日波动；第 j 次补仓的份数 = mult^(j-1)
#   （Coin Bureau 的例子：100 → 100、200、400），最多补 nso 次；全部补完 = 1 个满仓位（大小按波动率定）；
#   sl=1 时，补完最后一次以后再跌一档就全平、停 24 小时（给它一个有限的最大亏损；sl=0 是平台默认的不止损）。
@strat("R06", "马丁格尔加仓（越跌越买，回到均价上方就全平，1 小时）", "回归",
       "OKX DCA Martingale、3Commas / Pionex DCA 机器人（Coin Bureau 2026-08 评测）",
       [dict(step=0.5, tp=0.4, mult=2.0, nso=4, sl=0), dict(step=1.0, tp=0.5, mult=2.0, nso=4, sl=0),
        dict(step=0.5, tp=0.4, mult=1.5, nso=6, sl=0), dict(step=0.5, tp=0.4, mult=2.0, nso=4, sl=1)], tf="1h")
def r06(P, s, step, tp, mult, nso, sl):
    c = P.c.values
    vol = _vol(P, s).values
    T, N = c.shape
    so = np.array([mult ** j for j in range(nso)])
    total = 1.0 + so.sum()
    units = np.zeros(N)
    avg = np.full(N, np.nan)
    last = np.full(N, np.nan)
    k = np.zeros(N, dtype=int)
    stepf = np.full(N, np.nan)
    tpf = np.full(N, np.nan)
    cool = np.zeros(N, dtype=int)
    out = np.zeros((T, N))
    with np.errstate(invalid="ignore", divide="ignore"):
        for i in range(T):
            ci = c[i]
            okp = np.isfinite(ci)
            flat = units == 0
            hit_tp = ~flat & okp & (ci >= avg * (1 + tpf))
            below = ~flat & okp & (ci <= last * (1 - stepf))
            hit_sl = below & (k >= nso) & bool(sl)
            close = hit_tp | hit_sl
            add = below & (k < nso) & ~close
            au = np.where(add, so[np.minimum(k, nso - 1)], 0.0)
            nu = units + au
            avg = np.where(add, (avg * units + ci * au) / np.where(nu > 0, nu, 1.0), avg)
            units = nu
            last = np.where(add, ci, last)
            k = k + add
            units = np.where(close, 0.0, units)
            k = np.where(close, 0, k)
            cool = np.where(hit_sl, 24, cool)
            opn = flat & okp & (cool == 0) & np.isfinite(vol[i]) & (vol[i] > 0)
            cool = np.where(flat & ~opn, np.maximum(cool - 1, 0), cool)
            units = np.where(opn, 1.0, units)
            avg = np.where(opn, ci, avg)
            last = np.where(opn, ci, last)
            stepf = np.where(opn, step * vol[i], stepf)
            tpf = np.where(opn, tp * vol[i], tpf)
            out[i] = units / total
    return TS(P, s, _df(P, out))


def check_catalog() -> None:
    ids = [x["id"] for x in STRATEGIES]
    assert len(ids) == len(set(ids)), "编号重复"
    assert len(STRATEGIES) == 6, f"第 7 轮应当正好 6 个策略，现在 {len(STRATEGIES)} 个"
