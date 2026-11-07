"""
XSMomentum -- 截面动量 (Cross-Sectional Momentum), 多空双向, 日线。

和 TsmomTrend (时间序列动量: 每个币自己和自己的过去比) 不同, 截面动量是 "币和币之间比":
每天把白名单里所有币按 "最近几个月涨得有多稳" 排名, 做多排在最前面的 K 个, 做空排在最后面的 K 个。
大盘整体涨跌对多空两边的影响大致抵消, 赚的是 "强的继续比弱的强" 这部分钱。

动量分数 (逐根已收盘日线, 每个币单独算, 无未来函数):
  先跳过最近 skip_days (默认 1) 天: 只用到 "昨天" 为止的收盘价 (最近一天的涨跌在加密货币里常常会反转, 是噪音)
  z_L   = ln(C[t-1] / C[t-1-L]) / (日波动率 x sqrt(L))        L = 30, 60, 90 天
          日波动率 = 到 t-1 为止最近 vol_window (60) 天对数收益率的标准差
          (除以 sqrt(L) 让三个长度的 z 处在同一个量级, 三个长度权重相同)
  score = (z_30 + z_60 + z_90) / 3                             (= "风险调整后的动量")

截面排名 (每天一次, 对整个币池):
  当天有分数的币 (至少要有 skip + 90 + 1 = 92 根日线历史) 才参加排名; N = 参加排名的币数
  rank_long  = 从高到低的名次 (1 = 分数最高);  rank_short = 从低到高的名次 (1 = 分数最低)

入场 (只在调仓日):
  做多: rank_long <= K   (K = top_k, 默认 3)        做空: rank_short <= K
  每边最多同时 K 笔 (confirm_trade_entry): 还拿着的单子 (哪怕已经掉到缓冲区里) 占着名额, 有空名额才开新仓;
          同一天候选比空名额多时, 按 freqtrade 处理交易对的顺序 (白名单顺序) 先到先得, 候选都在前 K 名以内
  调仓日: 只有 "入场那天" (= 信号 K 线的下一天) 是 rebalance_weekday (默认 0 = 周一, UTC) 才开新仓,
          也就是周日那根日线收盘后、周一 00:00 开盘入场; -1 = 每天都可以开新仓
  N < min_ranked_pairs (默认 6) 时不开新仓 (币太少, 排名没有意义)
  可选 BTC 大盘过滤 use_btc_regime (默认关): BTC 日线收盘 < SMA200 时不开多, > SMA200 时不开空

离场 (每天检查, 带缓冲区 = 迟滞, 减少换手):
  多单: rank_long > K + B 才平   (B = rank_buffer, 默认 3; 也就是掉出前 6 名才走)
  空单: rank_short > K + B 才平
  币太少时自动收窄: K 最多 N // 2, B 最多 (N - 2K) // 2, 保证 "多头离场线" 不会碰到 "空头入场区"
  止损: 初始 = 入场价 -/+ stop_atr_mult (3) x ATR20 (最远 25%, easytrade_risk 的上限);
        之后吊灯止损 = 持仓以来最高价 (做空 = 最低价) -/+ 吊灯距离, 只收紧不放宽 (easytrade_risk 共用风控)。
        吊灯距离 = min(trail_atr_mult (4) x 入场 ATR, 4/3 x 初始止损距离): 平时就是 4 x ATR;
        3 x ATR 超过 25% 上限、初始止损被截短时 (ATR > 约 8.3%, 2021/2022 年那种行情), 吊灯止损按同样比例收近 (et_trailing_stop)。
        止损只防极端行情 (例如 "动量崩溃": 强币急跌、弱币暴涨), 正常靠排名离场

仓位 (easytrade_risk.EasyTradeRiskMixin, 和第二步其他新策略相同):
  每笔打到止损亏总资金 risk_per_trade (1%), 杠杆 2 倍 (硬上限 5 倍), 账户回撤 >= 8% 后新仓减半;
  组合热度上限 max_portfolio_risk = 8%: 有吊灯止损时, 每笔 "从现价打到止损还会亏多少" 最多是开仓风险的约 4/3 倍
  (止损离持仓以来最好价格最远 = 吊灯距离 <= 4/3 x 初始止损距离), 已有的 5 笔最多 5 x 1% x 4/3 ≈ 6.7%,
  第 6 笔还有 1% 以上的空间, 热度上限不会挡住多空各 3 笔的调仓 (初始止损被 25% 上限截短时也一样, 见上面 "吊灯距离");
  (2026-11-07 账户回撤上限 8% -> 15% 时 easytrade_risk 的默认风险放大到 2 倍, 这里的热度上限也跟着从 4% 改成 8%,
   比例不变; 以前是 0.5% / 4%)
  没有吊灯止损时, 赚钱的仓位离止损越来越远, 热度会把新仓 (往往是另一边) 挡掉, 多空就不平衡了
  max_open_trades 至少要 2K (默认 6)。配置里比 2K 小时 (研究配置是 5), K 会自动降到 max_open_trades // 2,
  保证多空笔数上限相同, 日志里有警告。想按 K = 3 测, 回测加 --max-open-trades 6。
  最小下单额: 仓位低于交易所最小下单额、freqtrade 放大 30% 也不够时, 这一笔开不出来 (freqtrade 自己不提示),
  这个币却还占着前 K 名, 那个名额空到下一个调仓日。custom_stake_amount 会把这种情况记一行日志
  (带 "[最小下单额:开不出来]"), 研究报告按策略、按区间数出有几次 (见 docs/strategies-round3.md 1.4)。

实盘 / 模拟盘:
  - 币池 = 机器人配置里的交易对 (白名单)。必须和研究时排名的是同一批币 (研究结果目录的 pairs.effective.json;
    币安用 configs/config.binance-xsmomentum.json, 它带 pairs.binance-research.json 和 max_open_trades 6)。
    拿 20 个币的 pairs.binance-usdt-perp.json 去跑, "前 3 / 后 3 名" 就不是研究过的那个策略了 (启动日志里会警告)。
  - 交易所的K线不是所有币同时到: freqtrade 2026.10 刚收盘的那根K线, 在收盘后 15 秒内拿到、又没有更新的K线时会先丢掉,
    拉取失败的币也要等下一轮 (5 秒) 再拉。所以 00:00 UTC 刚过的那一两轮, 可能有个别币的最新日线还没到。
    这时排名少了一个币, 别的币的名次全都错一位。处理: 有币 "昨天的K线有了、最新一根还没到" 并且离收盘不到
    xs_late_candle_wait (300 秒) 时, 最新一行不出任何信号 (不开仓、也不按排名平仓), 等它到了再一起排名;
    超过 300 秒还没到, 就按其余的币排名 (日志里有警告)。实盘里每一轮都重新分析所有币 (bot_start 里把
    process_only_new_candles 关掉), 晚到的K线一到, 所有币都会用完整的排名重算 (仍在 10 分钟入场时效之内)。

实现说明 (为什么没有未来函数):
  - populate_indicators(某个币) 里用 self.dp 读取币池里每个币的日线 (回测时是整段历史 + 预热),
    每个币逐根算分数 (rolling / shift, 第 t 行只用到 t-1 及更早的收盘价), 再按日期对齐, 每个日期在币之间排名。
    第 t 行的名次只用到 "所有币在 t 这一天及以前" 的数据, 所以整段一次算完也不会用到未来数据。
  - 排名表只算一次, 按 "币池里每个币的 (最后一根K线日期, K线根数)" + 回测区间 + 预热长度 缓存, 其他币直接复用,
    不会每个币重算一遍; 任何一个币的数据变了 (例如实盘里晚到的K线), 缓存就失效、重新算。
  - 回测 / 分析时直接读磁盘历史 (dp.historic_ohlcv), 实盘读交易所刷新到的已收盘 K 线 (dp.ohlcv, 只读不复制)。
  - 回测时 freqtrade 把每个币数据开头的 startup_candle_count 根当预热裁掉 (上线晚的币 = 上线后的头几个月不能交易),
    这几根也不参加排名, 免得一个开不了仓的币占着前 K 名的位置 (_xs_backtest_trim; 实盘不裁)。
  - freqtrade 的 lookahead-analysis / recursive-analysis 会把白名单缩成被检查的那一个币; 截面排名需要整个币池,
    所以这两个工具运行时 (runmode = util_no_exchange) 币池 = 配置里的完整交易对列表。其他模式一律用当前白名单。
    "每边最多 K 笔" 是组合层面的名额限制 (和 max_open_trades 一样), 这两个工具运行时不检查
    (lookahead-analysis 自己也把 max_open_trades 改成不限, 原因相同: 只留一个币时, 名额会让交易时间对不上)。

可 hyperopt 的参数 2 个 (第三轮默认不调参): top_k (buy) / rank_buffer (sell)。回看长度 30/60/90、波动率 60 天固定不调。
说明文档: docs/strategies-round3.md "XSMomentum"。
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

from freqtrade.enums import RunMode
from freqtrade.exchange import timeframe_to_prev_date, timeframe_to_seconds
from freqtrade.persistence import Trade
from freqtrade.strategy import IntParameter, IStrategy

from easytrade_risk import KEY_ATR, KEY_INIT_STOP, EasyTradeRiskMixin, atr, positive_or_none
from rust_port_common import to_epoch_seconds


logger = logging.getLogger(__name__)

# 配置里的交易对可能写成正则 (例如 ".*/USDT:USDT"), 这种不是具体的币, 不能直接读数据
_PAIR_REGEX_CHARS = re.compile(r"[*?\[\]()|+\\^$]")
# 日志里的固定标记: 研究报告 (scripts/research_report.py) 按它数 "因为最小下单额开不出来" 的次数, 改的话两边一起改
MIN_STAKE_SKIP_TAG = "[最小下单额:开不出来]"
MIN_STAKE_BUMP_TAG = "[最小下单额:放大]"
# freqtrade 最多把仓位放大 30% 去凑最小下单额 (freqtrade/wallets.py validate_stake_amount)
FT_MIN_STAKE_ADJUST = 1.3


# ---------------------------------------------------------------------------
# 纯函数 (向量化, 只用当前及更早的 K 线)
# ---------------------------------------------------------------------------


def momentum_score(
    df: DataFrame, lookbacks: tuple[int, ...], vol_window: int, skip: int
) -> np.ndarray:
    """单个币逐根的风险调整动量分数。第 t 行只用到第 t - skip 行及更早的收盘价; 历史不够 = NaN。"""
    close = df["close"].astype("float64")
    logc = np.log(close.where(close > 0)).shift(int(skip))
    r = logc.diff()
    sigma = r.rolling(int(vol_window), min_periods=int(vol_window)).std()
    sigma = sigma.where(sigma > 0)
    zs = [(logc - logc.shift(int(L))) / (sigma * math.sqrt(int(L))) for L in lookbacks]
    score = sum(zs) / len(zs)
    out = score.to_numpy(dtype="float64", copy=True)  # copy: pandas 3 (copy-on-write) 返回只读视图
    out[~np.isfinite(out)] = np.nan
    return out


def cross_section_table(
    frames: dict[str, DataFrame | None],
    lookbacks: tuple[int, ...],
    vol_window: int,
    skip: int,
    skip_first: int = 0,
) -> dict[str, Any]:
    """币池里每个币的分数按日期对齐, 每个日期在币之间排名。

    返回 {"score", "rank_long", "rank_short": 宽表 (行 = 日期的 UTC 秒数, 列 = 币), "n": 每天参加排名的币数}。
    每一行只用到这一天及以前的数据 (分数本身就是这样算的, 排名只在同一行内比较), 无未来函数。
    名次并列时按币名顺序 (method="first"), 结果确定、可复现。
    skip_first > 0: 每个币自己的前 skip_first 根不参加排名 (回测时 freqtrade 会把每个币开头这么多根当预热裁掉,
    那几天这个币根本不能交易; 见 XSMomentum._xs_backtest_trim)。只看每个币自己数据的开头, 和未来无关。
    """
    scores: dict[str, pd.Series] = {}
    for pair in sorted(frames):
        df = frames[pair]
        if df is None or df.empty:
            continue
        idx = to_epoch_seconds(df["date"])
        vals = momentum_score(df, lookbacks, vol_window, skip)
        if skip_first > 0:
            vals[: int(skip_first)] = np.nan
        ser = pd.Series(vals, index=idx, dtype="float64")
        scores[pair] = ser[~ser.index.duplicated(keep="last")]
    if not scores:
        return {}
    wide = pd.DataFrame(scores).sort_index()
    return {
        "score": wide,
        "rank_long": wide.rank(axis=1, ascending=False, method="first"),
        "rank_short": wide.rank(axis=1, ascending=True, method="first"),
        "n": wide.notna().sum(axis=1),
    }


def sma_regime(df: DataFrame | None, period: int) -> pd.Series | None:
    """收盘 > SMA = +1, < SMA = -1, 相等 = 0, 数据不够 = NaN。索引 = 日期的 UTC 秒数。"""
    if df is None or df.empty:
        return None
    close = df["close"].astype("float64")
    sma = close.rolling(int(period), min_periods=int(period)).mean()
    reg = np.where(sma.isna(), np.nan, np.sign(close - sma))
    ser = pd.Series(reg, index=to_epoch_seconds(df["date"]), dtype="float64")
    return ser[~ser.index.duplicated(keep="last")]


# ---------------------------------------------------------------------------
# 策略
# ---------------------------------------------------------------------------


class XSMomentum(EasyTradeRiskMixin, IStrategy):
    INTERFACE_VERSION = 3

    timeframe = "1d"
    can_short = True
    # 分数要 skip 1 + 最长回看 90 + 1 = 92 根; ATR20 (Wilder 递推) 约 130 根后起点误差 < 0.1%。
    # 打开 BTC 过滤时 SMA200 要 200 根: 从数据开头算起的回测, 前约 70 天 BTC 状态是 "不知道", 那段时间不开新仓。
    startup_candle_count = 130
    # 回测 / 分析: 每根K线只分析一次。实盘 / 模拟盘: bot_start 里改成 False (每一轮都重新分析, 见文件开头 "实盘 / 模拟盘")
    process_only_new_candles = True

    minimal_roi = {"0": 100.0}
    trailing_stop = False
    use_custom_stoploss = True
    use_exit_signal = True  # 排名离场 + custom_exit (止损补救) 需要它
    exit_profit_only = False
    ignore_roi_if_entry_signal = False
    stoploss = -0.50  # 兜底 (2 倍杠杆 = 价格反向 25%), 成交后换成 ATR 止损

    # ---------------- 可 hyperopt 的参数 (第三轮默认不调) ----------------
    top_k = IntParameter(2, 5, default=3, space="buy")  # 多空各做几个
    rank_buffer = IntParameter(1, 6, default=3, space="sell")  # 掉出前 K + B 名才平

    # ---------------- 固定参数 ----------------
    lookbacks = (30, 60, 90)
    vol_window = 60
    skip_days = 1
    min_ranked_pairs = 6
    # 入场那天是星期几才开新仓 (UTC): 0 = 周一 ... 6 = 周日; -1 = 每天
    rebalance_weekday = 0
    use_btc_regime = False
    btc_sma_period = 200
    enable_long = True
    enable_short = True

    # ---------------- 风控 (easytrade_risk) ----------------
    atr_period = 20
    atr_column = "atr"
    stop_atr_mult = 3.0
    # 吊灯止损: 持仓以来最高价 - 吊灯距离 (做空反之), 吊灯距离 = min(4 x 入场 ATR, 4/3 x 初始止损距离) (et_trailing_stop)。
    # 比初始止损宽 1/3, 只在赚了一段之后才开始收紧; 同时把每笔的热度限制在开仓风险的约 4/3 倍以内 (见文件开头 "仓位")
    trail_atr_mult = 4.0
    # 组合热度上限 = (2K - 1) x risk_per_trade x 4/3 + 一笔的空间, 取整: K = 3、每笔 1% 时 5 x 1% x 4/3 + 1% ≈ 7.7% -> 8%
    # (随 risk_per_trade 按比例改; scripts/check_live_guards.py 核对它挡不住第 2K 笔)
    max_portfolio_risk = 0.08
    et_name = "xsmom"

    # ---------------- 实盘 ----------------
    # 有币的最新日线还没到时, 最多等多少秒 (从K线收盘算起); 要比 ignore_buying_expired_candle_after (600) 短, 留出入场时间
    xs_late_candle_wait = 300
    # 实盘币池少于这么多个币时, 启动日志里警告 (研究用的是约 60 个币, configs/pairs.binance-research.json)
    xs_live_min_universe = 40

    plot_config = {
        "main_plot": {},
        "subplots": {
            "Score": {"xs_score": {"color": "#2f80ed"}},
            "Rank": {
                "xs_rank_long": {"color": "#27ae60"},
                "xs_rank_short": {"color": "#eb5757"},
                "xs_n": {"color": "#828282"},
            },
        },
    }

    # ======================================================================
    # 币池 / 数据
    # ======================================================================

    def informative_pairs(self):
        # 白名单里的币本来就按 1d 刷新 (策略周期就是 1d); 只有 BTC 过滤需要额外的 BTC 日线
        if self.use_btc_regime:
            return [(self.btc_pair, self.timeframe)]
        return []

    def bot_start(self, **kwargs: Any) -> None:
        super().bot_start(**kwargs)
        if not self._et_is_live():
            return
        # 实盘: 每一轮都重新分析所有币。默认 (True) 时一个币只在它自己有新K线时才重新分析,
        # 别的币的最新日线晚到几秒, 先分析的币就一直用着少一个币的排名 (见文件开头 "实盘 / 模拟盘")。
        # 排名表按数据指纹缓存, 数据没变时只是查一下缓存, 多花的时间很少。
        self.process_only_new_candles = False
        try:
            universe = self.xs_universe()
        except Exception as exc:  # noqa: BLE001 — 只是提示, 读不到白名单不影响运行
            logger.warning("%s: 读不到白名单: %s", self.et_name, exc)
            return
        logger.info("%s: 实盘排名币池 = 当前白名单 %d 个币 (必须和研究时排名的是同一批币: 研究结果目录的 pairs.effective.json)",
                    self.et_name, len(universe))
        if len(universe) < int(self.xs_live_min_universe):
            logger.warning(
                "%s: 实盘币池只有 %d 个币, 研究时用的是约 60 个 (configs/pairs.okx-research.json)。截面排名的 \"前 %d / 后 %d 名\" "
                "在小币池里不是研究过的那个策略; 币安请用 configs/config.binance-xsmomentum.json (带 pairs.binance-research.json)",
                self.et_name, len(universe), int(self.top_k.value), int(self.top_k.value),
            )

    def _xs_now(self) -> datetime:
        """现在的 UTC 时间 (实盘判断 "最新K线是不是晚到" 用; 自检脚本里会换成假的时间)。"""
        return datetime.now(timezone.utc)

    def _xs_analysis_mode(self) -> bool:
        """lookahead-analysis / recursive-analysis (它们会把白名单缩成一个币)。"""
        dp = getattr(self, "dp", None)
        mode = dp.runmode if dp is not None else self.config.get("runmode")
        return str(getattr(mode, "value", mode)) == RunMode.UTIL_NO_EXCHANGE.value

    def xs_universe(self) -> list[str]:
        """参加排名的币池 = 当前白名单 (分析工具运行时再加上配置里的完整交易对列表)。"""
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
        """一个币的日线 (只含已收盘 K 线)。读不到返回 None, 这个币不参加排名。只读, 不能改返回的数据。"""
        try:
            if self._et_is_live():
                # 只读不复制: 实盘每一轮每个币都要看一遍整个币池 (60 x 60 次), 复制太浪费
                ohlcv = getattr(self.dp, "ohlcv", None)
                if ohlcv is not None:
                    df = ohlcv(pair=pair, timeframe=self.timeframe, copy=False)
                else:
                    df = self.dp.get_pair_dataframe(pair=pair, timeframe=self.timeframe)
            else:
                # 回测 / hyperopt / 分析: 直接读磁盘历史 (和回测主数据同一段, 含预热)。
                # 不用 get_pair_dataframe: 同一个进程里先回测过别的策略时, 它会按上一次回测结束的时间截掉最后一根。
                df = self.dp.historic_ohlcv(pair=pair, timeframe=self.timeframe)
        except Exception as exc:
            df = None
            logger.warning("%s: 读取 %s %s 失败, 这个币不参加排名: %s", self.et_name, pair, self.timeframe, exc)
        if df is None or df.empty:
            warned = self.__dict__.setdefault("_xs_missing_warned", set())
            if pair not in warned:
                warned.add(pair)
                logger.warning("%s: 没有 %s %s 数据, 这个币不参加排名", self.et_name, pair, self.timeframe)
            return None
        return df

    def _xs_backtest_trim(self) -> int:
        """回测 / hyperopt / 分析时, freqtrade 把每个币数据开头的 startup_candle_count 根当预热裁掉 (按根数, 每个币各自裁;
        freqtrade.data.converter.trim_dataframe)。对数据完整的币, 裁掉的正好是回测开始日期之前那段, 不影响排名;
        对上线晚的币, 裁掉的是它上线后的头几个月 (研究脚本给日线策略最多 600 根预热时约 1.6 年), 这段时间它在回测里不能开仓。
        它如果照样参加排名, 就会占着前 K 名的位置却开不了仓, 其他币也补不上 (实盘里不存在这种情况)。
        所以排名时也跳过每个币的前这么多根, 回测里排名的币池 = 当时真能交易的币。实盘 / 模拟盘不裁, 返回 0。"""
        if self._et_is_live():
            return 0
        try:
            n = int(self.config.get("startup_candle_count") or self.startup_candle_count or 0)
        except (TypeError, ValueError):
            n = int(self.startup_candle_count or 0)
        return max(0, n)

    @staticmethod
    def _xs_fingerprint(pair: str, df: DataFrame | None) -> tuple[str, int | None, int]:
        """一个币的数据指纹: (币, 最后一根K线日期的 UTC 秒数, K线根数)。没有数据 = (币, None, 0)。"""
        if df is None or df.empty:
            return pair, None, 0
        # 只取最后一个时间戳 (实盘每一轮要算 币池 x 币池 次, 不能每次都转换整列); 不带时区的按 UTC, 和 to_epoch_seconds 相同
        return pair, int(pd.Timestamp(df["date"].iat[-1]).timestamp()), int(len(df))

    def _xs_pending(self, fingerprint: tuple) -> tuple[int | None, list[str]]:
        """实盘: 有没有币 "昨天的K线有了、最新一根还没到" (交易所晚到几秒 / 这一轮拉取失败)。

        返回 (最新K线的日期 UTC 秒数, 还在等的币)。离最新K线收盘超过 xs_late_candle_wait 秒就不再等 (返回空列表)。
        回测 / 分析: 永远是 (None, [])。
        """
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
                    "%s: %s 的最新日线 (%s) 收盘 %d 秒后还没到, 不再等了, 这一天按其余 %d 个币排名",
                    self.et_name, " ".join(late), day, int(waited), len(lasts) - len(late),
                )
            return newest, []
        if ("wait", newest) not in logged:
            logged.add(("wait", newest))
            if len(logged) > 100:
                logged.clear()
            logger.info(
                "%s: %s 的最新日线 (%s) 还没到, 先不出信号 (不开仓、不按排名平仓), 最多等到收盘后 %d 秒",
                self.et_name, " ".join(late), day, int(self.xs_late_candle_wait),
            )
        return newest, late

    def _xs_table(self, ref: DataFrame) -> dict[str, Any] | None:
        """整个币池的排名表 (缓存: 同一批数据只算一次, 其他币直接复用)。

        缓存的键是 "每个币的数据指纹" (最后一根K线日期 + 根数), 不是调用这个函数的那个币的最后日期:
        实盘里别的币的K线晚到时, 指纹变了, 排名表就重新算, 不会拿着少一个币的旧排名表。
        返回的 dict 里 "pending_date" 不是 None 时 = 实盘还在等个别币的最新K线, 这一天的那一行不能出信号。
        """
        universe = tuple(self.xs_universe())
        if not universe or ref.empty:
            return None
        frames = {p: self._xs_frame(p) for p in universe}
        if self.use_btc_regime and self.btc_pair not in frames:
            frames[self.btc_pair] = self._xs_frame(self.btc_pair)
        fingerprint = tuple(self._xs_fingerprint(p, frames[p]) for p in sorted(frames))
        key = (
            universe,
            fingerprint,
            str(self.config.get("timerange")),
            int(self.config.get("startup_candle_count") or 0),
            bool(self.use_btc_regime),
        )
        cache = self.__dict__.setdefault("_xs_cache", {})
        if key not in cache:
            ranked = {p: frames[p] for p in universe}
            table = cross_section_table(
                ranked, tuple(self.lookbacks), int(self.vol_window), int(self.skip_days), self._xs_backtest_trim()
            )
            if table and self.use_btc_regime:
                reg = sma_regime(frames.get(self.btc_pair), int(self.btc_sma_period))
                if reg is None:
                    logger.warning("%s: 没有 %s 日线, BTC 过滤视为 '不知道' (不开新仓)", self.et_name, self.btc_pair)
                table["btc_regime"] = reg
            if len(cache) >= 4:
                cache.clear()  # 实盘每天一两份, 只留最近几份
            cache[key] = table or None
            if table:
                n = table["n"]
                logger.info(
                    "%s: 截面排名表: 币池 %d 个, 有数据 %d 个, 最多同时排名 %d 个, 日期到 %s",
                    self.et_name, len(universe), table["score"].shape[1], int(n.max()) if len(n) else 0,
                    pd.Timestamp(int(n.index[-1]), unit="s", tz="UTC").date() if len(n) else "-",
                )
        table = cache[key]
        if not table:
            return None
        pending_date, _late = self._xs_pending(fingerprint)
        return {**table, "pending_date": pending_date if _late else None}

    # ======================================================================
    # K / B (按币数和 max_open_trades 收窄)
    # ======================================================================

    def _k_b(self, n: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        k = int(self.top_k.value)
        b = int(self.rank_buffer.value)
        try:
            mot = float(self.config.get("max_open_trades", -1))
        except (TypeError, ValueError):
            mot = -1.0
        if math.isfinite(mot) and mot > 0 and k > int(mot) // 2:
            k_cap = int(mot) // 2
            if not self.__dict__.get("_xs_mot_warned"):
                self.__dict__["_xs_mot_warned"] = True
                logger.warning(
                    "%s: max_open_trades = %d 不够多空各 %d 笔, 本次按多空各 %d 笔 (K = %d) 运行。"
                    "要按 K = %d 测, 请把 max_open_trades 设成 %d (回测加 --max-open-trades %d)",
                    self.et_name, int(mot), k, k_cap, k_cap, k, 2 * k, 2 * k,
                )
            k = k_cap
        n0 = np.nan_to_num(np.asarray(n, dtype="float64"), nan=0.0)
        k_eff = np.minimum(float(k), np.floor(n0 / 2))
        b_eff = np.clip(np.minimum(float(b), np.floor((n0 - 2 * k_eff) / 2)), 0.0, None)
        return k_eff, b_eff

    # ======================================================================
    # freqtrade 接口
    # ======================================================================

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["atr"] = atr(dataframe, self.atr_period)
        n = len(dataframe)
        cols = {c: np.full(n, np.nan) for c in ("xs_score", "xs_rank_long", "xs_rank_short", "xs_n", "btc_regime")}
        table = self._xs_table(dataframe) if getattr(self, "dp", None) is not None else None
        if table:
            idx = to_epoch_seconds(dataframe["date"])
            pair = metadata["pair"]
            if pair in table["score"].columns:
                cols["xs_score"] = table["score"][pair].reindex(idx).to_numpy(dtype="float64")
                cols["xs_rank_long"] = table["rank_long"][pair].reindex(idx).to_numpy(dtype="float64")
                cols["xs_rank_short"] = table["rank_short"][pair].reindex(idx).to_numpy(dtype="float64")
            cols["xs_n"] = table["n"].reindex(idx).to_numpy(dtype="float64")
            reg = table.get("btc_regime")
            if reg is not None:
                cols["btc_regime"] = reg.reindex(idx).to_numpy(dtype="float64")
            if table.get("pending_date") is not None:
                # 实盘: 还在等个别币的最新K线, 最新那一行的排名不完整 -> 不出信号 (币数 = NaN: 不开仓、不按排名平仓)
                cols["xs_n"] = np.where(idx == int(table["pending_date"]), np.nan, cols["xs_n"])
        for c, v in cols.items():
            dataframe[c] = v
        return dataframe

    def _rebalance_mask(self, dataframe: DataFrame) -> np.ndarray:
        wd = int(self.rebalance_weekday)
        if wd < 0:
            return np.ones(len(dataframe), dtype=bool)
        # 信号 K 线收盘 = 下一根开盘 = 入场时刻; 看入场那天是星期几
        entry_day = pd.to_datetime(dataframe["date"], utc=True) + pd.Timedelta(
            seconds=timeframe_to_seconds(self.timeframe)
        )
        return (entry_day.dt.day_of_week == wd).to_numpy()

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        rl = dataframe["xs_rank_long"].to_numpy(dtype="float64")
        rs = dataframe["xs_rank_short"].to_numpy(dtype="float64")
        n = dataframe["xs_n"].to_numpy(dtype="float64")
        k_eff, _b = self._k_b(n)
        ok = (
            self._rebalance_mask(dataframe)
            & (np.nan_to_num(n, nan=0.0) >= int(self.min_ranked_pairs))
            & (k_eff >= 1)
            & (dataframe["volume"] > 0).to_numpy()
            & dataframe["atr"].notna().to_numpy()
        )
        long_sig = ok & (rl <= k_eff) & bool(self.enable_long)  # NaN 比较 = False
        short_sig = ok & (rs <= k_eff) & bool(self.enable_short)
        if self.use_btc_regime:
            reg = dataframe["btc_regime"].to_numpy(dtype="float64")
            long_sig &= reg > 0  # NaN (不知道) -> 不开
            short_sig &= reg < 0
        dataframe["enter_long"] = long_sig.astype("int64")
        dataframe["enter_short"] = short_sig.astype("int64")
        tags = np.full(len(dataframe), None, dtype=object)
        tags[long_sig] = "xsmom_long"
        tags[short_sig] = "xsmom_short"
        dataframe["enter_tag"] = pd.Series(tags, index=dataframe.index, dtype=object)
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        rl = dataframe["xs_rank_long"].to_numpy(dtype="float64")
        rs = dataframe["xs_rank_short"].to_numpy(dtype="float64")
        k_eff, b_eff = self._k_b(dataframe["xs_n"].to_numpy(dtype="float64"))
        thr = k_eff + b_eff
        active = k_eff >= 1
        # 名次不知道 (NaN: 当天没有分数) 时不因为排名离场, 交给止损
        exit_long = active & (rl > thr)
        exit_short = active & (rs > thr)
        dataframe["exit_long"] = exit_long.astype("int64")
        dataframe["exit_short"] = exit_short.astype("int64")
        tags = np.full(len(dataframe), None, dtype=object)
        tags[exit_long | exit_short] = "xsmom_rank_out"
        dataframe["exit_tag"] = pd.Series(tags, index=dataframe.index, dtype=object)
        return dataframe

    # ======================================================================
    # 风控 (在 easytrade_risk 的基础上)
    # ======================================================================

    def et_trailing_stop(self, trade: Trade, last_row: pd.Series) -> float | None:
        """吊灯止损: 持仓以来最高价 (做空 = 最低价) -/+ 吊灯距离。

        吊灯距离 = min(trail_atr_mult x 入场 ATR, trail_atr_mult / stop_atr_mult x 初始止损距离)。
        平时初始止损 = 3 x ATR, 两者相等 (= 4 x ATR)。3 x ATR 超过 25% 上限、初始止损被截短时 (ATR > 约 8.3%),
        吊灯止损按同样比例收近 (= 初始止损距离的 4/3 倍), 否则每笔 "从现价打到止损还会亏多少" 能到开仓风险的 1.6 倍以上,
        8% 的组合热度上限 (5 x 1% x 1.6 = 8%) 会挡住第 6 笔, 多空就不平衡了。只用交易记录里存的入场 ATR / 初始止损和已收盘K线, 无未来函数。
        """
        mult = positive_or_none(self._pv("trail_atr_mult", 0.0))
        a = positive_or_none(trade.get_custom_data(KEY_ATR))
        if mult is None or a is None:
            return None
        best = self.et_best_price_since_entry(trade)
        if best is None:
            return None
        dist = mult * a
        init = positive_or_none(trade.get_custom_data(KEY_INIT_STOP))
        smult = positive_or_none(self._pv("stop_atr_mult"))
        open_rate = positive_or_none(trade.open_rate)
        if init is not None and smult is not None and open_rate is not None:
            init_dist = abs(open_rate - init)
            if init_dist > 0:
                dist = min(dist, mult / smult * init_dist)
        return best + dist if trade.is_short else best - dist

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
        """仓位 = easytrade_risk 的按止损距离定仓; 另外: 仓位比交易所最小下单额小时记一行日志。

        freqtrade 会把差得不多的仓位放大到最小下单额 (最多 +30%), 再不够就不开这一笔, 而且不提示。
        对截面策略来说, 开不出来的币仍然占着前 K 名, 别的币补不上, 那个名额空到下一个调仓日。
        日志带固定标记 (MIN_STAKE_SKIP_TAG / MIN_STAKE_BUMP_TAG), 研究报告按它数次数; 同一个币同一根K线只记一次。
        """
        stake = super().custom_stake_amount(
            pair=pair, current_time=current_time, current_rate=current_rate, proposed_stake=proposed_stake,
            min_stake=min_stake, max_stake=max_stake, leverage=leverage, entry_tag=entry_tag, side=side, **kwargs,
        )
        try:
            ms = positive_or_none(min_stake)
            if stake > 0 and ms is not None and stake < ms:
                seen = self.__dict__.setdefault("_xs_min_stake_seen", set())
                key = (pair, timeframe_to_prev_date(self.timeframe, current_time))
                if key not in seen:
                    if len(seen) > 2000:
                        seen.clear()
                    seen.add(key)
                    log = logger.warning if self._et_is_live() else logger.info
                    # 标记和币名放在最前面: freqtrade 的日志会把太长的行折成几行, 研究报告按标记数次数、按币名分类
                    if stake * FT_MIN_STAKE_ADJUST < ms:
                        log("%s %s %s: 按风险算的保证金 %.2f USDT 比交易所最小下单额 %.2f USDT 小太多 (freqtrade 最多放大 30%%), "
                            "这一笔开不出来; 这个币仍然占着排名名额, 那个名额空到下一个调仓日",
                            MIN_STAKE_SKIP_TAG, self.et_name, pair, stake, ms)
                    else:
                        log("%s %s %s: 按风险算的保证金 %.2f USDT 比交易所最小下单额 %.2f USDT 小, freqtrade 会放大到 %.2f "
                            "(这一笔实际风险比设计的大, 最多 +30%%)",
                            MIN_STAKE_BUMP_TAG, self.et_name, pair, stake, ms, ms)
        except Exception as exc:  # noqa: BLE001 — 只是记日志, 不能影响下单
            logger.debug("%s %s 检查最小下单额失败: %s", self.et_name, pair, exc)
        return stake

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
        """每边最多 K 笔: 同方向已有的持仓 (包括掉进缓冲区还没平的) 占着名额, 没有空名额就不开。"""
        if self._xs_analysis_mode():
            return True
        try:
            row = self.et_last_row(pair)
            n = np.array([np.nan if row is None else row.get("xs_n", np.nan)], dtype="float64")
            k_eff = int(self._k_b(n)[0][0])
            is_short = side == "short"
            held = [t for t in Trade.get_trades_proxy(is_open=True) if bool(t.is_short) == is_short and t.pair != pair]
            if len(held) >= k_eff:
                log = logger.info if self._et_is_live() else logger.debug
                log("%s %s: %s 已有 %d 笔 (每边最多 %d 笔), 这次不开", self.et_name, pair,
                    "做空" if is_short else "做多", len(held), k_eff)
                return False
            return True
        except Exception as exc:
            logger.error("%s %s confirm_trade_entry 出错, 跳过入场: %s", self.et_name, pair, exc)
            return False
