"""
XSFundingCarry -- 资金费截面套息 (Cross-Sectional Funding Carry), 多空市场中性, 日线, 每周一调仓。

第 5 轮预注册策略: 规格见 docs/round5-preregistration.md 第 2.1 节 (冻结, 正式运行之前和之后都不改任何数值;
运行之后的任何改动 = 新的类名, 例如 XSFundingCarry2, 写进登记表 configs/preregistration.json)。

思路: 资金费 = 加杠杆的一方付给另一方的钱。做空资金费最高的永续 (多头拥挤, 空头收钱),
      做多资金费最低的永续 (空头拥挤 / 费率为负, 多头收钱), 收的是 "给加杠杆的人提供杠杆" 的报酬;
      多空两边名义金额相同, 大盘涨跌大致抵消。

数据: 1d K 线 + 1h 标记价格 + 资金费率 (freqtrade 的 K 线类型 funding_rate, 每次结算一行: date + funding_rate)。
      研究 (2026-11-08 起, David 的决定, 在任何真实数据回测之前; docs/round5-preregistration.md 6.5 节):
      在美国 VPS 上用 OKX USDT 永续 (configs/config.okx-research-funding.json + pairs.okx-research.json, 60 个币),
      单独的数据目录 user_data/data/okx-funding (freqtrade 不读配置里的 datadir, 研究脚本会加 --datadir)。
      OKX 接口只给最近约 3 个月的资金费, 更早的用 OKX 官方历史归档 (2022-03 起, scripts/okx_funding_archive.py 导入),
      所以研究从 2022-04-01 开始、没有 2020 留出段; OKX 也只有最近约 3 个月的标记价格, 更早的用同一个币的 1h 合约K线代替
      (只影响资金费金额, 约 0.1%)。模拟盘 / 实盘仍然打算在币安上跑: 上实盘之前要用币安数据回测或币安模拟盘复核
      (configs/config.binance-research.json + pairs.binance-research.json)。

规则 (每个调仓日按当时的数据算; 调仓时刻 T = 周一 00:00 UTC; 信号K线 = 周日那根日线, 它在 T 收盘):
  币池  先选 "至少 60 根日K线 (min_daily_candles)" 而且 "T 之前 7 天资金费记录完整" 的币,
        再按过去 30 天成交额 (成交量 x 收盘价, 逐日求和, 30 根日K线) 取前 30 个 (universe_top_n)。
        合格的币不足 15 个 (min_universe) 时, 这一周不开新仓。N = 进入币池的币数。
  信号  F7 = 结算时间在 [T - 7 天, T) 之内的所有资金费率之和 (只用结算时间严格早于 T 的记录)。
        用求和不用平均: 8h、4h、1h 结算的币可以直接比较。
  入场  周一 00:00 开盘。N 个币按 F7 从低到高排, n = floor(N / 3) (bucket = 三分位)。
        做多 F7 最低的 n 个, 做空 F7 最高的 n 个。
  离场  只在调仓日检查: 做多的币不在最低的 n 个里了就平仓, 做空的币不在最高的 n 个里了就平仓 (不在币池里了也算),
        仍在组内就继续拿着。没有止盈。
  止损  每条腿初始止损 = 入场价 -/+ 3 x ATR20(1d), 最远 25%, 不移动 (easytrade_risk 的 ATR 止损, 吊灯止损关闭)。
        被止损的币下一个调仓日仍在对应的组里, 可以重新开仓。
  仓位  不用 "每笔 1% 风险"。总名义仓位 G = 0.4 x 账户权益, 多空各 0.2 x 账户权益;
        同一边内按 1/σ20 分配 (σ20 = 信号K线为止 20 个日对数收益率的标准差), 权重在这一边的 n 个币里归一;
        保证金 = 名义金额 / 杠杆 (2 倍, 逐仓), 再乘以混入类的回撤降仓系数 (权益比最高点低 >= 8% 时 x 0.5)。
        组合热度上限 8% (max_portfolio_risk, 和 XSMomentum 相同; 算法沿用 easytrade_risk)。
        研究时 max_open_trades = 20 (configs/research-overrides/XSFundingCarry.json; 每边最多 floor(30/3) = 10 条腿)。
        已经拿着的腿不加减仓 (规格里没有), 名义金额随价格漂移, 下一次换仓时新开的腿按当时的权益重新算。
  参数 (3 个, 预注册, 不调): lookback_days = 7, bucket = "tercile", rebalance_days = 7。
  固定常量 (预注册, 冻结): 币池前 30 个、至少 60 根日K线、不足 15 个不开仓、30 天成交额、σ20 倒数加权、
        G = 0.4、止损 3 x ATR20 最远 25%。

数据完整性 (实现细节, 规格只说 "7 天资金费记录完整"):
  完整 = 窗口 [T - 7 天, T) 里至少有 21 条结算 (7 天 / 8 小时), 窗口开始到第一条、相邻两条结算之间、以及最后一条到 T, 间隔都不超过 8 小时
        (funding_max_interval_hours: 币安最长的结算周期)。8h 的币少一条就不算完整; 结算周期中途从 8h 改成 4h / 1h 的币照样算完整。
  回测里的预热陷阱 (freqtrade 源码已核实): dp.get_pair_dataframe / historic_ohlcv 给其他周期加载的预热是同样的 "根数"
  (get_required_startup 直接返回 startup_candle_count), 所以 1d 策略 startup = 130 时, 1h 资金费只往前多加载 130 小时
  (约 5.4 天, 不够 7 天窗口)。所以回测 / 分析时资金费改读数据目录里完整的资金费文件 (_funding_disk, 见下面 "实现说明"),
  完整性按真实的结算记录判断, 和预热根数、回测从哪天开始无关。不要求 60 天资金费历史 (规格只要求 7 天窗口完整)。

实现说明 (为什么没有未来函数):
  - 截面框架照搬 XSMomentum: populate_indicators(某个币) 读币池里每个币的日线 (实盘 dp.ohlcv 只读不复制, 回测 dp.historic_ohlcv)
    和资金费 (照搬 FundingContrarian._load_funding: dp.get_pair_dataframe(币, dp.get_funding_rate_timeframe(), candle_type="funding_rate");
    不用 @informative, 它遇到空数据会报错), 只在调仓信号K线上算 F7 / 币池 / 排名 / 权重, 整张表按数据指纹缓存, 其他币直接复用。
    第 d 行 (信号K线, 在 T = d + 1 天收盘) 只用到 d 及以前的日线和结算时间 < T 的资金费。
  - 回测里 freqtrade 按根数裁掉每个币开头 startup_candle_count 根日线 (converter.trim_dataframe), 这几根不能交易,
    排名时也跳过 (和 XSMomentum._xs_backtest_trim 相同; 实盘不裁)。所以回测里上线晚的币要等 预热根数 天才进币池。
  - 回测 / 分析时 dp 读到的资金费只有 "回测区间 + 130 小时预热", 而且在回测区间结束时间截断 (date <= 结束时间),
    可是最后一根日线 (date = 结束时间) 的收盘在 1 天以后。lookahead-analysis / recursive-analysis 把区间截到某根日线时,
    那一行的 F7 就会比完整回测少几条结算, 被误报成 "未来函数"; 回测区间从周日开始时第一周的窗口也不完整。
    所以回测 / 分析时再读同一个数据目录里完整的资金费文件 (_funding_disk), 只留结算时间早于 "最后一根日线收盘" 的记录,
    每个调仓时刻 T 仍然只用结算时间 < T 的那几条, 不会用到收盘以后的数据; 实盘不读磁盘 (交易所缓存里就是最新的)。
  - F7、成交额、σ20、权重都只按窗口里的值直接求和 (math.fsum), 不用累加和 / pandas rolling 的递推, 所以结果和
    数据从哪天开始加载无关 (lookahead-analysis 按 "完全相等" 比较, 递推的舍入误差 1e-17 也会被报成未来函数)。
  - lookahead-analysis / recursive-analysis 把白名单缩成一个币; 币池这时用配置里的完整交易对列表 (同 XSMomentum)。
  - 每边名额 (confirm_trade_entry): 同方向已有的持仓里, 这一次调仓仍在组内的占着名额, 超过 n 就不开;
    这一次调仓要平掉的 (已经不在组内) 和新的腿在同一个开盘价成交, 不占名额。XSMomentum 有缓冲区 (掉进缓冲区的还拿着),
    这里没有缓冲区, 不排除它们的话回测结果会取决于 freqtrade 处理交易对的顺序 (多空互换的币会被挡掉)。

实盘 / 模拟盘:
  - 币池 = 机器人白名单, 必须和研究时排名的是同一批币 (研究结果目录的 pairs.effective.json)。
  - informative_pairs 返回每个币的 (币, 资金费周期 1h, "funding_rate"), freqtrade 每小时刷新资金费历史 (币安每次最多 1000 条)。
    模拟盘要核实: 1h 结算的币内存里有完整 7 天 (168 条) 记录 (docs/round5-preregistration.md 4 节)。
  - 晚到的日线: 和 XSMomentum 一样, 有币 "昨天的K线有了、最新一根还没到" 时最多等 300 秒再排名 (xs_late_candle_wait)。
  - 最小下单额: 币安 BTC 100 USDT、ETH 20 USDT, 1000 USDT 资金时这两条腿常常开不出来; custom_stake_amount 记一行带
    "[最小下单额:开不出来]" 的日志, 研究报告按它数次数 (标记和 XSMomentum、scripts/research_report.py 相同)。
说明文档: docs/round5-preregistration.md 2.1 节。
"""

from __future__ import annotations

import logging
import math
import re
from datetime import datetime, timezone
from typing import Any

import numpy as np
import pandas as pd
from pandas import DataFrame

from freqtrade.enums import CandleType, RunMode
from freqtrade.exchange import timeframe_to_prev_date, timeframe_to_seconds
from freqtrade.persistence import Trade
from freqtrade.strategy import IStrategy

from easytrade_risk import EasyTradeRiskMixin, atr, positive_or_none
from rust_port_common import to_epoch_seconds


logger = logging.getLogger(__name__)

FUNDING_CANDLE_TYPE = "funding_rate"
# 配置里的交易对可能写成正则 (例如 ".*/USDT:USDT"), 这种不是具体的币, 不能直接读数据
_PAIR_REGEX_CHARS = re.compile(r"[*?\[\]()|+\\^$]")
# 日志里的固定标记: 和 XSMomentum.py、scripts/research_report.py 相同, 研究报告按它数 "因为最小下单额开不出来" 的次数
MIN_STAKE_SKIP_TAG = "[最小下单额:开不出来]"
MIN_STAKE_BUMP_TAG = "[最小下单额:放大]"
# freqtrade 最多把仓位放大 30% 去凑最小下单额 (freqtrade/wallets.py validate_stake_amount)
FT_MIN_STAKE_ADJUST = 1.3
DAY_SECONDS = 86400
# 调仓日的起点: 1970-01-05 00:00 UTC 是周一; rebalance_days = 7 时调仓日 = 每个周一
REBALANCE_ANCHOR = 4 * DAY_SECONDS
# bucket 参数 -> 分几组 (三分位 = 3 组, 只做最低和最高那一组)
BUCKETS = {"tercile": 3}


# ---------------------------------------------------------------------------
# 纯函数 (向量化, 只用当前及更早的数据)
# ---------------------------------------------------------------------------


def rebalance_signal_mask(epochs: np.ndarray, tf_seconds: int, rebalance_days: int) -> np.ndarray:
    """信号K线 (开盘时间 d, UTC 秒) 的收盘 T = d + 1 根K线 是调仓时刻时为 True。

    调仓时刻 = UTC 0 点, 并且离 1970-01-05 (周一) 的天数是 rebalance_days 的整数倍 (7 = 每个周一 00:00)。
    """
    t = np.asarray(epochs, dtype="int64") + int(tf_seconds)
    days = (t - REBALANCE_ANCHOR) // DAY_SECONDS
    return (t % DAY_SECONDS == 0) & (days % int(rebalance_days) == 0)


def funding_window_stats(
    fdates: np.ndarray,
    rates: np.ndarray,
    t_ends: np.ndarray,
    window_seconds: int,
    min_count: int,
    max_gap_seconds: int,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """每个调仓时刻 T (t_ends, UTC 秒): 结算时间在 [T - window, T) 里的资金费率之和、是否完整、条数。

    fdates: 结算时间 (UTC 秒, 升序, 不重复); rates: 对应的费率。
    完整 = 条数 >= min_count, 并且窗口开始 (T - window) 到第一条、窗口里相邻两条结算之间、最后一条到 T 的间隔
    都 <= max_gap_seconds (窗口开头缺一段的 4h / 1h 结算的币不算完整, 否则 F7 只是几天的和)。
    不完整时和 = NaN。只用结算时间严格早于 T 的记录。
    """
    t_ends = np.asarray(t_ends, dtype="int64")
    m = len(t_ends)
    total = np.full(m, np.nan)
    complete = np.zeros(m, dtype=bool)
    count = np.zeros(m, dtype="int64")
    fd = np.asarray(fdates, dtype="int64")
    if m == 0 or len(fd) == 0:
        return total, complete, count
    rt = np.asarray(rates, dtype="float64")
    gaps = np.diff(fd)
    lo = np.searchsorted(fd, t_ends - int(window_seconds), side="left")
    hi = np.searchsorted(fd, t_ends, side="left")  # 严格 < T
    for j in range(m):
        a, b = int(lo[j]), int(hi[j])
        k = b - a
        count[j] = k
        if k < max(1, int(min_count)):
            continue
        if fd[a] - (t_ends[j] - int(window_seconds)) > max_gap_seconds:
            continue  # leading gap: the record starts (or resumes) too late inside the window
        if t_ends[j] - fd[b - 1] > max_gap_seconds:
            continue
        if k >= 2 and int(gaps[a : b - 1].max()) > max_gap_seconds:
            continue
        # 只对窗口里的记录求和 (math.fsum 精确舍入): 结果只取决于窗口里的值, 和数据从哪天开始加载无关
        # (累加和相减会带上更早数据的舍入误差, lookahead / recursive-analysis 会把 1e-17 的差别也报出来)
        s = math.fsum(rt[a:b])
        if not math.isfinite(s):
            continue
        complete[j] = True
        total[j] = s
    return total, complete, count


def pair_rebalance_rows(
    df: DataFrame,
    fr: DataFrame | None,
    *,
    tf_seconds: int,
    rebalance_days: int,
    lookback_days: int,
    min_settlements: int,
    max_gap_seconds: int,
    volume_window: int,
    vol_window: int,
    skip_first: int = 0,
) -> DataFrame:
    """一个币在它自己的调仓信号K线上的数据 (索引 = 信号K线开盘时间的 UTC 秒)。

    n_candles = 到这根为止的日线根数; qv = 最近 volume_window 根的成交额 (成交量 x 收盘价) 之和;
    sigma = 最近 vol_window 个日对数收益率的标准差; f7 / complete / fcount = 资金费窗口 (见 funding_window_stats)。
    skip_first > 0: 这个币自己的前 skip_first 根不算 (回测里 freqtrade 把它们当预热裁掉, 不能交易)。
    """
    cols = ["n_candles", "qv", "sigma", "f7", "complete", "fcount"]
    if df is None or df.empty:
        return DataFrame(columns=cols, dtype="float64")
    ep = to_epoch_seconds(df["date"])
    close = df["close"].to_numpy(dtype="float64")
    qv_all = df["volume"].to_numpy(dtype="float64") * close
    with np.errstate(divide="ignore", invalid="ignore"):
        logc = np.log(np.where(close > 0, close, np.nan))
    reb = rebalance_signal_mask(ep, tf_seconds, rebalance_days)
    if skip_first > 0:
        reb[: int(skip_first)] = False
    idx = np.nonzero(reb)[0]
    t_end = ep[idx] + int(tf_seconds)
    # 成交额 / σ20 只在调仓行上按窗口直接算 (不用 pandas rolling 的递推): 结果只取决于窗口里的K线,
    # 和数据从哪天开始加载无关 (同 funding_window_stats 的说明)。窗口里有缺值 = NaN (同 rolling 的 min_periods = 窗口)。
    vw, sw = int(volume_window), int(vol_window)
    qv = np.full(len(idx), np.nan)
    sigma = np.full(len(idx), np.nan)
    for j, i in enumerate(idx):
        if i + 1 >= vw:
            win = qv_all[i + 1 - vw : i + 1]
            if np.isfinite(win).all():
                qv[j] = math.fsum(win)
        if i >= sw and sw >= 2:  # sw 个日收益率 = sw + 1 根收盘
            lc = logc[i - sw : i + 1]
            if np.isfinite(lc).all():
                r = np.diff(lc)
                mean = math.fsum(r) / sw
                sigma[j] = math.sqrt(math.fsum((r - mean) ** 2) / (sw - 1))  # 样本标准差 (ddof = 1, 同 pandas)
    if fr is not None and not fr.empty:
        fd = to_epoch_seconds(fr["fdate"])
        f7, complete, fcount = funding_window_stats(
            fd, fr["rate"].to_numpy(dtype="float64"), t_end, int(lookback_days) * DAY_SECONDS,
            int(min_settlements), int(max_gap_seconds),
        )
    else:
        f7 = np.full(len(idx), np.nan)
        complete = np.zeros(len(idx), dtype=bool)
        fcount = np.zeros(len(idx), dtype="int64")
    out = DataFrame(
        {
            "n_candles": (idx + 1).astype("float64"),
            "qv": qv,
            "sigma": sigma,
            "f7": f7,
            "complete": complete.astype("float64"),
            "fcount": fcount.astype("float64"),
        },
        index=ep[idx],
    )
    return out[~out.index.duplicated(keep="last")]


def carry_table(
    frames: dict[str, DataFrame | None],
    funding: dict[str, DataFrame | None],
    *,
    tf_seconds: int,
    rebalance_days: int,
    lookback_days: int,
    n_buckets: int,
    top_n: int,
    min_candles: int,
    min_settlements: int,
    max_gap_seconds: int,
    volume_window: int,
    vol_window: int,
    skip_first: int = 0,
) -> dict[str, Any]:
    """整个币池在每个调仓信号日期上的截面 (行 = 信号K线开盘时间的 UTC 秒, 列 = 币)。

    elig  = 至少 min_candles 根日线 + 资金费窗口完整 + 有成交额; univ = elig 里按成交额取前 top_n;
    N     = univ 的币数; nb = floor(N / n_buckets);
    rank_low / rank_high = F7 从低到高 / 从高到低的名次 (只在 univ 里排, 并列按币名顺序, 结果确定);
    group = +1 做多组 (rank_low <= nb) / -1 做空组 (rank_high <= nb) / 0 其他;
    weight = 同一组内 1/sigma 的归一权重 (sigma 不是正数的币没有权重)。
    每一行只用到这一天及以前的数据, 只在同一行内比较。
    """
    per: dict[str, DataFrame] = {}
    for pair in sorted(frames):
        df = frames[pair]
        if df is None or df.empty:
            continue
        per[pair] = pair_rebalance_rows(
            df, funding.get(pair), tf_seconds=tf_seconds, rebalance_days=rebalance_days,
            lookback_days=lookback_days, min_settlements=min_settlements, max_gap_seconds=max_gap_seconds,
            volume_window=volume_window, vol_window=vol_window, skip_first=skip_first,
        )
    per = {p: v for p, v in per.items() if not v.empty}
    if not per:
        return {}

    def wide(col: str) -> DataFrame:
        return DataFrame({p: v[col] for p, v in per.items()}).sort_index().astype("float64")

    f7 = wide("f7")
    qv = wide("qv")
    sigma = wide("sigma")
    elig = (
        (wide("n_candles") >= int(min_candles))
        & (wide("complete") == 1.0)
        & np.isfinite(qv)
        & np.isfinite(f7)
    )
    qv_rank = qv.where(elig).rank(axis=1, ascending=False, method="first")
    univ = elig & (qv_rank <= int(top_n))
    n = univ.sum(axis=1).astype("float64")
    nb = np.floor(n / int(n_buckets))
    f7u = f7.where(univ)
    rank_low = f7u.rank(axis=1, ascending=True, method="first")
    rank_high = f7u.rank(axis=1, ascending=False, method="first")
    nbv = nb.to_numpy()[:, None]
    long_g = rank_low.to_numpy() <= nbv  # NaN 比较 = False
    short_g = rank_high.to_numpy() <= nbv
    group = DataFrame(np.where(long_g, 1.0, np.where(short_g, -1.0, 0.0)), index=f7.index, columns=f7.columns)
    inv = (1.0 / sigma.where((sigma > 0) & np.isfinite(sigma))).to_numpy(dtype="float64")
    w = np.full(inv.shape, np.nan)
    for r in range(inv.shape[0]):
        for side in (long_g[r], short_g[r]):
            cols = np.nonzero(side & np.isfinite(inv[r]))[0]
            if len(cols):
                # 逐行精确求和 (math.fsum): 和表的行数、内存布局无关 (lookahead-analysis 按 "完全相等" 比较)
                w[r, cols] = inv[r, cols] / math.fsum(inv[r, cols])
    weight = DataFrame(w, index=f7.index, columns=f7.columns)
    return {
        "f7": f7.where(elig),
        "elig": elig.astype("float64"),
        "univ": univ.astype("float64"),
        "qv": qv,
        "sigma": sigma,
        "fcount": wide("fcount"),
        "rank_low": rank_low,
        "rank_high": rank_high,
        "group": group,
        "weight": weight,
        "n": n,
        "nb": nb,
        "n_elig": elig.sum(axis=1).astype("float64"),
    }


# ---------------------------------------------------------------------------
# 策略
# ---------------------------------------------------------------------------


class XSFundingCarry(EasyTradeRiskMixin, IStrategy):
    INTERFACE_VERSION = 3

    timeframe = "1d"
    can_short = True
    # 和 XSMomentum 一样: ATR20 (Wilder 递推) 约 130 根后起点误差 < 0.1%。资金费是 "每次结算一行",
    # 回测时 dp 只按 130 根 1h 额外加载 (约 5.4 天), 所以回测 / 分析时另读完整的资金费文件 (见文件开头 "预热陷阱")。
    startup_candle_count = 130
    # 回测 / 分析: 每根K线只分析一次。实盘 / 模拟盘: bot_start 里改成 False (每一轮都重新分析, 同 XSMomentum)
    process_only_new_candles = True

    minimal_roi = {"0": 100}
    trailing_stop = False
    use_custom_stoploss = True
    use_exit_signal = True  # 调仓离场 + custom_exit (止损补救) 需要它
    exit_profit_only = False
    ignore_roi_if_entry_signal = False
    # 兜底止损 (成交后马上换成 ATR 止损)。-0.51 而不是 -0.50: 混入类的止损上限 = min(0.25, |stoploss| / 杠杆 x 0.999, 0.8 / 杠杆),
    # 2 倍杠杆时 -0.50 只能到 24.975%, -0.51 时正好是规格里的 "最远 25%"。
    stoploss = -0.51

    # ---------------- 参数 (3 个, 预注册, 不调; 普通类属性, 不用 hyperopt 参数) ----------------
    lookback_days = 7  # F7 回看天数: [T - 7 天, T)
    bucket = "tercile"  # 三分位: n = floor(N / 3)
    rebalance_days = 7  # 每周调仓 (周一 00:00 UTC)

    # ---------------- 固定常量 (预注册, 冻结) ----------------
    universe_top_n = 30  # 按过去 30 天成交额取前 30 个
    min_daily_candles = 60  # 至少 60 根日K线
    min_universe = 15  # 合格的币不足 15 个: 这一周不开新仓
    volume_window_days = 30  # 成交额 = 最近 30 根日线 成交量 x 收盘价 之和
    vol_window_days = 20  # σ20: 最近 20 个日对数收益率的标准差 (同一边内按 1/σ20 分配)
    gross_exposure = 0.4  # G = 0.4 x 账户权益, 多空各一半

    # ---------------- 数据完整性 (实现细节, 见文件开头) ----------------
    funding_max_interval_hours = 8.0  # 币安最长的结算周期; 7 天至少 7 x 24 / 8 = 21 条

    # ---------------- 风控 (easytrade_risk) ----------------
    max_leverage_param = 2.0  # 2 倍逐仓 (硬上限 5 倍)
    atr_period = 20
    atr_column = "atr"
    stop_atr_mult = 3.0  # 初始止损 3 x ATR20
    trail_atr_mult = 0.0  # 不移动 (没有吊灯止损)
    max_stop_distance = 0.25  # 最远 25%
    max_hold_candles = 0  # 没有持仓超时
    max_portfolio_risk = 0.08  # 组合热度上限 8% (和 XSMomentum 相同)
    et_name = "xsfund"

    # ---------------- 实盘 ----------------
    # 有币的最新日线还没到时, 最多等多少秒 (从K线收盘算起); 要比 ignore_buying_expired_candle_after (600) 短
    xs_late_candle_wait = 300
    # 实盘币池少于这么多个币时, 启动日志里警告 (研究用的是 60 个币, configs/pairs.binance-research.json)
    xs_live_min_universe = 40

    plot_config = {
        "main_plot": {},
        "subplots": {
            "F7": {"xs_f7": {"color": "#2f80ed"}},
            "Group": {
                "xs_group": {"color": "#27ae60"},
                "xs_w": {"color": "#eb5757"},
            },
            "Universe": {
                "xs_n": {"color": "#828282"},
                "xs_nb": {"color": "#9b51e0"},
            },
        },
    }

    # ======================================================================
    # 币池 / 数据
    # ======================================================================

    def _n_buckets(self) -> int:
        b = BUCKETS.get(str(self.bucket))
        if b is None:
            raise ValueError(f"XSFundingCarry.bucket 只支持 {list(BUCKETS)} (收到 {self.bucket!r})")
        return int(b)

    def _funding_timeframe(self) -> str:
        try:
            return str(self.dp.get_funding_rate_timeframe())
        except Exception:
            return "1h"  # freqtrade 2026.10 所有交易所的默认值

    def informative_pairs(self):
        # 实盘 / 模拟盘: 让 freqtrade 每轮刷新资金费率历史 (回测不需要, dp 直接读磁盘)。日线本来就按策略周期刷新
        if getattr(self, "dp", None) is None:
            return []
        ftf = self._funding_timeframe()
        return [(p, ftf, FUNDING_CANDLE_TYPE) for p in self.dp.current_whitelist()]

    def _log_once(self, key: Any, level: int, msg: str, *args: Any) -> None:
        seen = self.__dict__.setdefault("_xs_logged", set())
        if key in seen:
            return
        if len(seen) > 5000:
            seen.clear()
        seen.add(key)
        logger.log(level, msg, *args)

    def bot_start(self, **kwargs: Any) -> None:
        super().bot_start(**kwargs)
        self._n_buckets()  # bucket 写错时启动就报错
        need = 2 * (int(self.universe_top_n) // self._n_buckets())
        try:
            mot = float(self.config.get("max_open_trades", -1))
        except (TypeError, ValueError):
            mot = -1.0
        if math.isfinite(mot) and 0 < mot < need:
            logger.warning(
                "%s: max_open_trades = %d, 放不下多空各 %d 条腿 (币池 %d 个的三分位)。研究时用 "
                "configs/research-overrides/XSFundingCarry.json (max_open_trades = %d); 名额不够时 freqtrade 按交易对顺序先到先得",
                self.et_name, int(mot), need // 2, int(self.universe_top_n), need,
            )
        if not self._et_is_live():
            return
        # 实盘: 每一轮都重新分析所有币 (同 XSMomentum: 晚到的日线一到, 所有币用完整的截面重算)
        self.process_only_new_candles = False
        try:
            universe = self.xs_universe()
        except Exception as exc:  # noqa: BLE001 — 只是提示
            logger.warning("%s: 读不到白名单: %s", self.et_name, exc)
            return
        logger.info("%s: 实盘币池 = 当前白名单 %d 个币 (必须和研究时排名的是同一批币: 研究结果目录的 pairs.effective.json)",
                    self.et_name, len(universe))
        if len(universe) < int(self.xs_live_min_universe):
            logger.warning(
                "%s: 实盘币池只有 %d 个币, 研究时用的是 60 个 (configs/pairs.binance-research.json); "
                "币池前 %d 个 / 三分位不是研究过的那个策略",
                self.et_name, len(universe), int(self.universe_top_n),
            )

    def _xs_now(self) -> datetime:
        """现在的 UTC 时间 (实盘判断 "最新K线是不是晚到" 用; 自检时可以换成假的时间)。"""
        return datetime.now(timezone.utc)

    def _xs_analysis_mode(self) -> bool:
        """lookahead-analysis / recursive-analysis (它们会把白名单缩成一个币)。"""
        dp = getattr(self, "dp", None)
        mode = dp.runmode if dp is not None else self.config.get("runmode")
        return str(getattr(mode, "value", mode)) == RunMode.UTIL_NO_EXCHANGE.value

    def xs_universe(self) -> list[str]:
        """参加截面的币池 = 当前白名单 (分析工具运行时再加上配置里的完整交易对列表)。"""
        if getattr(self, "dp", None) is None:
            return []
        pairs = list(self.dp.current_whitelist())
        if self._xs_analysis_mode():
            extra: list[Any] = []
            cfg_pairs = self.config.get("pairs")
            if isinstance(cfg_pairs, list):
                extra += cfg_pairs
            extra += (self.config.get("exchange") or {}).get("pair_whitelist") or []
            orig = self.config.get("original_config") or {}
            extra += (orig.get("exchange") or {}).get("pair_whitelist") or []
            pairs += [p for p in extra if isinstance(p, str) and not _PAIR_REGEX_CHARS.search(p)]
        return sorted(dict.fromkeys(pairs))

    def _xs_frame(self, pair: str) -> DataFrame | None:
        """一个币的日线 (只含已收盘 K 线)。读不到返回 None。只读, 不能改返回的数据 (同 XSMomentum._xs_frame)。"""
        try:
            if self._et_is_live():
                ohlcv = getattr(self.dp, "ohlcv", None)
                if ohlcv is not None:
                    df = ohlcv(pair=pair, timeframe=self.timeframe, copy=False)
                else:
                    df = self.dp.get_pair_dataframe(pair=pair, timeframe=self.timeframe)
            else:
                # 回测 / 分析: 直接读磁盘历史 (和回测主数据同一段, 含预热)。不用 get_pair_dataframe:
                # 同一个进程里先回测过别的策略时, 它会按上一次回测结束的时间截掉最后一根
                df = self.dp.historic_ohlcv(pair=pair, timeframe=self.timeframe)
        except Exception as exc:
            df = None
            logger.warning("%s: 读取 %s %s 失败, 这个币不参加截面: %s", self.et_name, pair, self.timeframe, exc)
        if df is None or df.empty:
            self._log_once(("no_1d", pair), logging.WARNING,
                           "%s: 没有 %s %s 数据, 这个币不参加截面", self.et_name, pair, self.timeframe)
            return None
        return df

    def _funding_disk(self, pair: str, until: int | None) -> DataFrame | None:
        """回测 / 分析: 数据目录里这个币完整的资金费文件, 只留结算时间 < until 的记录 (见文件开头 "预热陷阱" / "实现说明")。

        until = 最后一根日线收盘 (UTC 秒)。只读同一个数据目录里 freqtrade 下载的资金费文件, 不联网。
        """
        if until is None:
            return None
        cache = self.__dict__.setdefault("_xs_fund_disk", {})
        if pair not in cache:
            try:
                from freqtrade.data.history import get_datahandler

                dh = get_datahandler(self.config["datadir"], self.config.get("dataformat_ohlcv") or "feather")
                raw = dh.ohlcv_load(
                    pair, self._funding_timeframe(), CandleType.FUNDING_RATE,
                    timerange=None, fill_missing=False, warn_no_data=False,
                )
            except Exception as exc:  # noqa: BLE001 — 读不到就只按 dp 给的算
                logger.debug("%s %s: 读资金费文件失败: %s", self.et_name, pair, exc)
                raw = None
            cache[pair] = raw
        raw = cache[pair]
        if raw is None or raw.empty:
            return None
        col = "funding_rate" if "funding_rate" in raw.columns else ("open" if "open" in raw.columns else None)
        if col is None:
            return None
        d = pd.to_datetime(raw["date"], utc=True).astype("datetime64[ns, UTC]")
        up = pd.Timestamp(int(until), unit="s", tz="UTC")
        m = d < up
        if not m.any():
            return None
        return pd.DataFrame({"fdate": d[m].to_numpy(), "rate": pd.to_numeric(raw.loc[m, col], errors="coerce").to_numpy()})

    def _load_funding(self, pair: str, until: int | None = None) -> DataFrame | None:
        """读取这个币的资金费率历史, 返回 [fdate, rate] (按时间排序, 去重); 没有数据 / 出错返回 None。

        照搬 FundingContrarian._load_funding: dp.get_pair_dataframe(币, dp.get_funding_rate_timeframe(), candle_type="funding_rate")。
        实盘读交易所缓存 (informative_pairs 负责刷新); 回测 / 分析时 dp 只给 "回测区间 + 预热根数",
        所以再并上数据目录里完整的资金费文件 (_funding_disk, 只留最后一根日线收盘以前的结算)。
        """
        if getattr(self, "dp", None) is None:
            return None
        try:
            fr = self.dp.get_pair_dataframe(pair=pair, timeframe=self._funding_timeframe(), candle_type=FUNDING_CANDLE_TYPE)
        except Exception as exc:
            self._log_once(("fund_err", pair), logging.WARNING,
                           "%s %s: 读取资金费率失败, 这个币不进币池: %s", self.et_name, pair, exc)
            return None
        if fr is None or fr.empty:
            self._log_once(("fund_none", pair), logging.WARNING,
                           "%s %s: 没有资金费率数据, 这个币不进币池 (合约模式下载数据时会自动下载 funding_rate)",
                           self.et_name, pair)
            return None
        col = "funding_rate" if "funding_rate" in fr.columns else ("open" if "open" in fr.columns else None)
        if col is None:
            self._log_once(("fund_col", pair), logging.ERROR,
                           "%s %s: 资金费率数据没有 funding_rate 列 (列: %s), 这个币不进币池",
                           self.et_name, pair, list(fr.columns))
            return None
        out = pd.DataFrame({
            "fdate": pd.to_datetime(fr["date"], utc=True).astype("datetime64[ns, UTC]"),
            "rate": pd.to_numeric(fr[col], errors="coerce"),
        })
        if not self._et_is_live():
            disk = self._funding_disk(pair, until)
            if disk is not None and not disk.empty:
                disk["fdate"] = pd.to_datetime(disk["fdate"], utc=True).astype("datetime64[ns, UTC]")
                out = pd.concat([out, disk], ignore_index=True)
        out = out.dropna().sort_values("fdate").drop_duplicates("fdate", keep="last").reset_index(drop=True)
        if out.empty:
            return None
        return out

    def _xs_backtest_trim(self) -> int:
        """回测 / 分析时 freqtrade 按根数裁掉每个币开头的 startup_candle_count 根 (同 XSMomentum._xs_backtest_trim);
        这几根在回测里不能交易, 也不进币池。实盘 / 模拟盘返回 0。"""
        if self._et_is_live():
            return 0
        try:
            n = int(self.config.get("startup_candle_count") or self.startup_candle_count or 0)
        except (TypeError, ValueError):
            n = int(self.startup_candle_count or 0)
        return max(0, n)

    @staticmethod
    def _xs_fingerprint(pair: str, df: DataFrame | None, date_col: str = "date") -> tuple[str, int | None, int]:
        """一个币的数据指纹: (币, 最后一行日期的 UTC 秒, 行数)。没有数据 = (币, None, 0)。"""
        if df is None or df.empty:
            return pair, None, 0
        return pair, int(pd.Timestamp(df[date_col].iat[-1]).timestamp()), int(len(df))

    def _funding_fingerprint(self, universe: tuple[str, ...]) -> tuple:
        """实盘: 每个币资金费缓存的指纹 (交易所缓存只读不复制), 资金费刷新后截面重算。回测 / 分析: 数据不变, 空元组。"""
        if not self._et_is_live():
            return ()
        ftf = self._funding_timeframe()
        out = []
        for p in universe:
            try:
                df = self.dp.ohlcv(pair=p, timeframe=ftf, copy=False, candle_type=FUNDING_CANDLE_TYPE)
            except Exception:  # noqa: BLE001
                df = None
            out.append(self._xs_fingerprint(p, df))
        return tuple(out)

    def _xs_pending(self, fingerprint: tuple) -> tuple[int | None, list[str]]:
        """实盘: 有没有币 "昨天的K线有了、最新一根还没到" (同 XSMomentum._xs_pending)。回测 / 分析: (None, [])。"""
        if not self._et_is_live():
            return None, []
        lasts = {p: last for p, last, _n in fingerprint if last is not None}
        if not lasts:
            return None, []
        newest = max(lasts.values())
        tf = int(timeframe_to_seconds(self.timeframe))
        late = sorted(p for p, last in lasts.items() if last == newest - tf)
        if not late:
            return newest, []
        waited = self._xs_now().timestamp() - (newest + tf)
        day = pd.Timestamp(newest, unit="s", tz="UTC").date()
        logged = self.__dict__.setdefault("_xs_late_logged", set())
        if waited > float(self.xs_late_candle_wait):
            if ("gave_up", newest) not in logged:
                logged.add(("gave_up", newest))
                logger.warning(
                    "%s: %s 的最新日线 (%s) 收盘 %d 秒后还没到, 不再等了, 这一天按其余 %d 个币算截面",
                    self.et_name, " ".join(late), day, int(waited), len(lasts) - len(late),
                )
            return newest, []
        if ("wait", newest) not in logged:
            logged.add(("wait", newest))
            if len(logged) > 100:
                logged.clear()
            logger.info(
                "%s: %s 的最新日线 (%s) 还没到, 先不出信号 (不开仓、不调仓离场), 最多等到收盘后 %d 秒",
                self.et_name, " ".join(late), day, int(self.xs_late_candle_wait),
            )
        return newest, late

    def _xs_table(self, ref: DataFrame) -> dict[str, Any] | None:
        """整个币池的调仓截面表 (按 "每个币日线 + 资金费的数据指纹" 缓存, 其他币直接复用; 同 XSMomentum._xs_table)。

        返回的 dict 里 "pending_date" 不是 None 时 = 实盘还在等个别币的最新日线, 那一行不出信号。
        """
        universe = tuple(self.xs_universe())
        if not universe or ref.empty:
            return None
        frames = {p: self._xs_frame(p) for p in universe}
        fp_daily = tuple(self._xs_fingerprint(p, frames[p]) for p in universe)
        key = (
            universe,
            fp_daily,
            self._funding_fingerprint(universe),
            str(self.config.get("timerange")),
            int(self.config.get("startup_candle_count") or 0),
        )
        cache = self.__dict__.setdefault("_xs_cache", {})
        if key not in cache:
            tf = int(timeframe_to_seconds(self.timeframe))
            lasts = [last for _p, last, _n in fp_daily if last is not None]
            until = (max(lasts) + tf) if lasts else None  # 最后一根日线的收盘
            funding = {p: self._load_funding(p, until) for p in universe if frames[p] is not None}
            table = carry_table(
                frames, funding, tf_seconds=tf, rebalance_days=int(self.rebalance_days),
                lookback_days=int(self.lookback_days), n_buckets=self._n_buckets(), top_n=int(self.universe_top_n),
                min_candles=int(self.min_daily_candles),
                min_settlements=int(math.ceil(int(self.lookback_days) * 24 / float(self.funding_max_interval_hours))),
                max_gap_seconds=int(round(float(self.funding_max_interval_hours) * 3600)),
                volume_window=int(self.volume_window_days), vol_window=int(self.vol_window_days),
                skip_first=self._xs_backtest_trim(),
            )
            if len(cache) >= 4:
                cache.clear()
            cache[key] = table or None
            if table:
                n = table["n"]
                ok = int((n >= int(self.min_universe)).sum())
                logger.info(
                    "%s: 调仓截面表: 币池 %d 个, 有资金费 %d 个, %d 个调仓日里币池够 %d 个 (能开仓) 的有 %d 个, "
                    "币池最多 %d 个, 日期到 %s",
                    self.et_name, len(universe), len(funding), len(n), int(self.min_universe), ok,
                    int(n.max()) if len(n) else 0,
                    pd.Timestamp(int(n.index[-1]), unit="s", tz="UTC").date() if len(n) else "-",
                )
        table = cache[key]
        if not table:
            return None
        pending_date, late = self._xs_pending(fp_daily)
        table = {**table, "pending_date": pending_date if late else None}
        self.__dict__["_xs_last_table"] = table
        return table

    # ======================================================================
    # freqtrade 接口
    # ======================================================================

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["atr"] = atr(dataframe, self.atr_period)
        n_rows = len(dataframe)
        names = ("xs_f7", "xs_elig", "xs_univ", "xs_qv30", "xs_sigma20", "xs_fcount", "xs_rank_low", "xs_rank_high",
                 "xs_group", "xs_w", "xs_n", "xs_nb", "xs_n_elig")
        cols = {c: np.full(n_rows, np.nan) for c in names}
        table = self._xs_table(dataframe) if getattr(self, "dp", None) is not None else None
        if table:
            idx = to_epoch_seconds(dataframe["date"])
            pair = metadata["pair"]
            mapping = {
                "xs_f7": "f7", "xs_elig": "elig", "xs_univ": "univ", "xs_qv30": "qv", "xs_sigma20": "sigma",
                "xs_fcount": "fcount", "xs_rank_low": "rank_low", "xs_rank_high": "rank_high",
                "xs_group": "group", "xs_w": "weight",
            }
            if pair in table["group"].columns:
                for c, k in mapping.items():
                    cols[c] = table[k][pair].reindex(idx).to_numpy(dtype="float64")
            cols["xs_n"] = table["n"].reindex(idx).to_numpy(dtype="float64")
            cols["xs_nb"] = table["nb"].reindex(idx).to_numpy(dtype="float64")
            cols["xs_n_elig"] = table["n_elig"].reindex(idx).to_numpy(dtype="float64")
            on_table = np.isfinite(cols["xs_n"])
            # 调仓日有截面、但这个币不在表里 (没有资金费 / 被预热裁掉): 不在任何组里 (= 0)
            cols["xs_group"] = np.where(on_table & ~np.isfinite(cols["xs_group"]), 0.0, cols["xs_group"])
            if table.get("pending_date") is not None:
                # 实盘: 还在等个别币的最新日线 -> 最新那一行不出信号 (币池数 = NaN: 不开仓、不调仓离场)
                cols["xs_n"] = np.where(idx == int(table["pending_date"]), np.nan, cols["xs_n"])
        for c, v in cols.items():
            dataframe[c] = v
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        n = dataframe["xs_n"].to_numpy(dtype="float64")
        nb = dataframe["xs_nb"].to_numpy(dtype="float64")
        g = dataframe["xs_group"].to_numpy(dtype="float64")
        ok = (
            (np.nan_to_num(n, nan=0.0) >= int(self.min_universe))  # NaN (不是调仓日 / 实盘等K线) -> 不开
            & (np.nan_to_num(nb, nan=0.0) >= 1)
            & (dataframe["volume"] > 0).to_numpy()
            & dataframe["atr"].notna().to_numpy()
        )
        long_sig = ok & (g == 1.0)
        short_sig = ok & (g == -1.0)
        dataframe["enter_long"] = long_sig.astype("int64")
        dataframe["enter_short"] = short_sig.astype("int64")
        tags = np.full(len(dataframe), None, dtype=object)
        tags[long_sig] = "xsfund_long"
        tags[short_sig] = "xsfund_short"
        dataframe["enter_tag"] = pd.Series(tags, index=dataframe.index, dtype=object)
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        n = dataframe["xs_n"].to_numpy(dtype="float64")
        g = dataframe["xs_group"].to_numpy(dtype="float64")
        # 只在调仓日 (有截面的那一行) 检查; 一个币都不合格 (N = 0, 例如资金费数据断了) 时没有排名可比, 不因为调仓离场, 交给止损
        active = np.nan_to_num(n, nan=0.0) >= 1
        exit_long = active & (g != 1.0)
        exit_short = active & (g != -1.0)
        dataframe["exit_long"] = exit_long.astype("int64")
        dataframe["exit_short"] = exit_short.astype("int64")
        tags = np.full(len(dataframe), None, dtype=object)
        tags[exit_long | exit_short] = "xsfund_rebalance"
        dataframe["exit_tag"] = pd.Series(tags, index=dataframe.index, dtype=object)
        return dataframe

    # ======================================================================
    # 仓位 / 名额
    # ======================================================================

    def _xs_equity(self, upnl: float) -> float | None:
        """账户权益 = 钱包余额 (freqtrade 的 stake 币总额: 已平仓结果 + 占用的保证金) + 持仓浮动盈亏 (et_portfolio_state,
        按上一根已收盘K线)。不乘 tradable_balance_ratio: 规格的 G = 0.4 x 账户权益。"""
        wallets = getattr(self, "wallets", None)
        if wallets is None:
            return None
        try:
            bal = float(wallets.get_total(self.config["stake_currency"]))
        except Exception:
            return None
        return bal + float(upnl)

    def custom_stoploss(
        self,
        pair: str,
        trade: Trade,
        current_time: datetime,
        current_rate: float,
        current_profit: float,
        after_fill: bool,
        **kwargs: Any,
    ) -> float | None:
        """止损不移动 (trail_atr_mult = 0): 混入类在成交后把初始止损 (3 x ATR20, 最远 25%) 设好以后, 再调用也只会返回 None。
        所以设好一次以后直接返回 None, 结果完全相同, 只是省掉回测里每根K线 x 每条腿 的重复计算 (freqtrade 回测的
        trade custom data 是线性查找, 20 条腿 x 1h 细节K线时很慢)。没设成功 (返回 None) 的交易下一次照常交给混入类。"""
        done = self.__dict__.setdefault("_xs_stop_set", set())
        key = (pair, trade.open_date_utc)
        if not after_fill and key in done:
            return None
        sl = super().custom_stoploss(
            pair=pair, trade=trade, current_time=current_time, current_rate=current_rate,
            current_profit=current_profit, after_fill=after_fill, **kwargs,
        )
        if sl is not None:
            if len(done) > 20000:
                done.clear()
            done.add(key)
        return sl

    def _xs_log_min_stake(self, pair: str, current_time: datetime, stake: float, min_stake: float | None) -> None:
        """保证金比交易所最小下单额小时记一行带固定标记的日志 (同 XSMomentum; 同一个币同一根K线只记一次)。"""
        try:
            ms = positive_or_none(min_stake)
            if not (stake > 0 and ms is not None and stake < ms):
                return
            seen = self.__dict__.setdefault("_xs_min_stake_seen", set())
            key = (pair, timeframe_to_prev_date(self.timeframe, current_time))
            if key in seen:
                return
            if len(seen) > 2000:
                seen.clear()
            seen.add(key)
            log = logger.warning if self._et_is_live() else logger.info
            # 标记和币名放在最前面: freqtrade 的日志会把太长的行折成几行, 研究报告按标记数次数、按币名分类
            if stake * FT_MIN_STAKE_ADJUST < ms:
                log("%s %s %s: 按权重算的保证金 %.2f USDT 比交易所最小下单额 %.2f USDT 小太多 (freqtrade 最多放大 30%%), "
                    "这一笔开不出来; 这个币仍然在组里, 这条腿空到下一个调仓日",
                    MIN_STAKE_SKIP_TAG, self.et_name, pair, stake, ms)
            else:
                log("%s %s %s: 按权重算的保证金 %.2f USDT 比交易所最小下单额 %.2f USDT 小, freqtrade 会放大到 %.2f "
                    "(这条腿比设计的大, 最多 +30%%)",
                    MIN_STAKE_BUMP_TAG, self.et_name, pair, stake, ms, ms)
        except Exception as exc:  # noqa: BLE001 — 只是记日志, 不能影响下单
            logger.debug("%s %s 检查最小下单额失败: %s", self.et_name, pair, exc)

    def custom_stake_amount(
        self,
        pair: str,
        current_time: datetime,
        current_rate: float,
        proposed_stake: float,
        min_stake: float | None,
        max_stake: float,
        leverage: float,
        entry_tag: str | None,
        side: str,
        **kwargs: Any,
    ) -> float:
        """名义金额 = 0.2 x 账户权益 x 这一边的 1/σ20 权重; 保证金 = 名义金额 / 杠杆, 再乘回撤降仓系数, 受热度上限 8% 约束。

        不按 proposed_stake (= 资金 / max_open_trades) 截断, 只受 freqtrade 自己的 max_stake 限制。
        返回 0 = 不开这一笔; 出错也返回 0 (绝不满仓兜底)。
        """
        try:
            is_short = side == "short"
            row = self.et_last_row(pair)
            if row is None:
                logger.info("%s %s: 没有信号K线, 跳过", self.et_name, pair)
                return 0.0
            want = -1.0 if is_short else 1.0
            grp = row.get("xs_group")
            w = positive_or_none(row.get("xs_w"))
            if grp != want or w is None:
                logger.info("%s %s: 不在%s组里或没有 σ20 权重 (组 %s, 权重 %s), 跳过",
                            self.et_name, pair, "做空" if is_short else "做多", grp, row.get("xs_w"))
                return 0.0
            stop, _a = self.et_initial_stop(row, current_rate, is_short, leverage)
            if stop is None:
                logger.info("%s %s: 算不出有效止损 (没有 ATR), 跳过", self.et_name, pair)
                return 0.0
            dist = abs(stop - current_rate) / current_rate
            dist_for_sizing = max(dist, float(self.min_stop_distance)) + float(self.sizing_cost_buffer)

            heat, upnl = self.et_portfolio_state()
            equity = self._xs_equity(upnl)
            if equity is None or equity <= 0:
                logger.info("%s %s: 读不到账户权益, 跳过", self.et_name, pair)
                return 0.0
            notional = equity * float(self.gross_exposure) / 2.0 * w

            dd_start = float(self.dd_throttle_start or 0)
            if dd_start > 0:
                dd = self.et_drawdown(current_time)
                if dd >= dd_start:
                    notional *= float(self.dd_throttle_mult)
                    logger.info("%s %s: 账户回撤 %.2f%% >= %.2f%%, 这条腿名义金额 x %.2f",
                                self.et_name, pair, dd * 100, dd_start * 100, float(self.dd_throttle_mult))

            room = equity * float(self.max_portfolio_risk) - heat
            leg_risk = notional * dist_for_sizing
            if room <= 0 or room < leg_risk * float(self.min_risk_fraction):
                logger.info("%s %s: 组合热度 %.2f%% 已接近上限 %.2f%%, 跳过",
                            self.et_name, pair, heat / equity * 100, float(self.max_portfolio_risk) * 100)
                return 0.0
            if leg_risk > room:
                logger.info("%s %s: 组合热度上限 %.2f%%: 这条腿名义金额从 %.2f 减到 %.2f USDT",
                            self.et_name, pair, float(self.max_portfolio_risk) * 100, notional, room / dist_for_sizing)
                notional = room / dist_for_sizing

            lev = max(1.0, float(leverage or 1.0))
            stake = max(0.0, min(notional / lev, float(max_stake)))
            self._xs_log_min_stake(pair, current_time, stake, min_stake)
            return stake
        except Exception as exc:
            logger.error("%s custom_stake_amount 出错, 跳过入场: %s", self.et_name, exc)
            return 0.0

    def confirm_trade_entry(
        self,
        pair: str,
        order_type: str,
        amount: float,
        rate: float,
        time_in_force: str,
        current_time: datetime,
        entry_tag: str | None,
        side: str,
        **kwargs: Any,
    ) -> bool:
        """每边最多 n 条腿 (n = floor(N / 3), 信号K线上的值)。

        同方向已有的持仓里, 这一次调仓仍在组内的 (或者查不到的) 占着名额; 这一次调仓已经不在组内的
        会在同一个开盘价平掉, 不占名额 (见文件开头 "实现说明")。分析工具运行时不检查 (同 XSMomentum)。
        """
        if self._xs_analysis_mode():
            return True
        try:
            row = self.et_last_row(pair)
            nb = np.nan if row is None else row.get("xs_nb", np.nan)
            if nb is None or not np.isfinite(float(nb)) or float(nb) < 1:
                logger.info("%s %s: 信号K线上没有组大小, 这次不开", self.et_name, pair)
                return False
            is_short = side == "short"
            want = -1.0 if is_short else 1.0
            groups = None
            table = self.__dict__.get("_xs_last_table")
            if table and row is not None:
                sig = int(pd.Timestamp(row["date"]).timestamp())
                g = table.get("group")
                if g is not None and sig in g.index:
                    groups = g.loc[sig]
            held = 0
            for t in Trade.get_trades_proxy(is_open=True):
                if bool(t.is_short) != is_short or t.pair == pair:
                    continue
                gv = groups.get(t.pair, np.nan) if groups is not None else np.nan
                if gv is None or not np.isfinite(float(gv)) or float(gv) == want:
                    held += 1
            if held >= int(nb):
                log = logger.info if self._et_is_live() else logger.debug
                log("%s %s: %s已有 %d 条腿仍在组内 (每边最多 %d 条), 这次不开", self.et_name, pair,
                    "做空" if is_short else "做多", held, int(nb))
                return False
            return True
        except Exception as exc:
            logger.error("%s %s confirm_trade_entry 出错, 跳过入场: %s", self.et_name, pair, exc)
            return False
