"""
CapitulationRebound -- 全市场去杠杆暴跌日之后买入 (强制平仓后的流动性补偿, 事件反转), 只做多, 日线。
第 5 轮预注册策略 (规格冻结, 不调参)。规格原文: docs/round5-preregistration.md 第 2.3 节 (和第 4 节 "CapitulationRebound")。

信号 (第 t 天日线收盘时, 三个条件同时满足; 逐根已收盘日线, 无未来函数):
  r_t = ln(C_t / C_{t-1}) <= -k x sigma20      (k = k_sigma = 3)
        sigma20 = 第 t 天之前 20 天日对数收益的标准差 (t-20 ~ t-1, 不含第 t 天; pandas 样本标准差)
  V_t >= volume_mult x 前 20 天成交量的中位数    (volume_mult = 2; t-20 ~ t-1, 不含第 t 天)
  BTC/USDT:USDT 同一天 r_t < 0                  (BTC 日线, 同周期按日期精确对齐)

同一天多个信号: 按 r_t / sigma20 从最负往上排, 最多取前 5 个 (max_signals_per_day)。
  截面表 (XSMomentum 那种): 币池里每个币逐根算信号和分数, 按日期对齐后在同一天里排名, 整张表只算一次、按数据指纹缓存。
  名次并列时按币名顺序 (method="first")。之后还受组合热度上限 6% 和 max_open_trades 6 的限制
  (研究时叠加 configs/research-overrides/CapitulationRebound.json); 名额不够时按 freqtrade 处理交易对的顺序 (白名单顺序)
  先到先得, 候选都在当天前 5 名以内 (和 XSMomentum 的做法相同)。

入场: 第 t+1 天开盘做多 (信号K线的下一根)。开仓成交时把 C_{t-1}、L_t、C_t 存进交易记录 (custom data, 重启不丢)。

离场 (哪个先到按哪个):
  某天收盘价 >= C_{t-1} (暴跌前一天的收盘价): 下一根开盘平仓 (custom_exit 和上一根已收盘日线比较, 离场原因 cr_prev_close)
  持有满 5 根日线 (max_hold_days; easytrade_risk 的 et_timeout)
  止损: 在 "L_t - 1 x ATR20" 和 "入场价 - 3 x ATR20" 里取离入场价更近的那个, 而且不低于 0.8 x C_t
        = max(L_t - ATR20, 入场价 - 3 x ATR20, 0.8 x C_t)。L_t 是暴跌当天的最低价, ATR20 用信号K线 (第 t 天) 的值。
        实现: et_structure_stop 返回 max(L_t - ATR20, 0.8 x C_t), stop_atr_mult = 3, 混入类取离入场价更近的那个;
        止损不移动。混入类另外的上限: 最远 25% (实际上 0.8 x C_t 已经更近); 开盘价已经在这个止损价下面 = 不开这一笔。

仓位 (easytrade_risk 默认值): 每笔打到止损亏总资金 1%, 组合热度 <= 6%, 账户回撤 >= 8% 后新仓减半, 杠杆 2 倍 (硬上限 5 倍)。

参数 (3 个, 冻结): k_sigma = 3.0, volume_mult = 2.0, max_hold_days = 5。
固定常量 (同样冻结): BTC 当天下跌、每天最多 5 个、20 日窗口、上面的止损规则、回到前一天收盘价就离场。

实现说明 (和 XSMomentum 相同的框架):
  - 回测 / 分析时读磁盘历史 (dp.historic_ohlcv, 和回测主数据同一段, 含预热), 实盘读交易所刷新到的已收盘日线 (dp.ohlcv)。
  - 回测时 freqtrade 把每个币开头的 startup_candle_count 根当预热裁掉 (按根数, 每个币各自裁), 那几天这个币不能交易;
    排名时也跳过这几根, 免得一个开不了仓的币占着 "当天前 5 名" 的位置。实盘不裁。
  - lookahead-analysis / recursive-analysis 会把白名单缩成一个币; 截面排名要用整个币池, 所以这两个工具运行时
    币池 = 配置里的完整交易对列表。
  - 中途下架的币 (第二阶段 RESEARCH_KEEP_DELISTED=1): freqtrade 回测里它没有K线以后就不再处理, 还开着的多单要到这一段
    结束才按最后一根K线的开盘价 force_exit。从最后一根K线收盘起这笔多单不再算进组合热度 (cr_data_ended);
    它占着的 freqtrade 名额 (6 个里的 1 个) 和保证金要到这一段结束才放出来, 策略改不了。
  - 实盘: 每一轮都重新分析 (bot_start 里关掉 process_only_new_candles)。有币 "前一天的K线有了、最新一根还没到" 时,
    离收盘不到 xs_late_candle_wait (300 秒) 就先不出信号, 等它到了再一起排名 (和 XSMomentum 相同, 只影响实盘)。
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any

import numpy as np
import pandas as pd
from pandas import DataFrame

from freqtrade.enums import RunMode
from freqtrade.exchange import timeframe_to_prev_date, timeframe_to_seconds
from freqtrade.persistence import Trade
from freqtrade.strategy import IStrategy

from easytrade_risk import EasyTradeRiskMixin, atr, positive_or_none


logger = logging.getLogger(__name__)

# 配置里的交易对可能写成正则 (例如 ".*/USDT:USDT"), 这种不是具体的币, 不能直接读数据
_PAIR_REGEX_CHARS = re.compile(r"[*?\[\]()|+\\^$]")
# 和 XSMomentum / scripts/research_report.py 相同的 "最小下单额" 标记 (研究报告按它数次数)
MIN_STAKE_SKIP_TAG = "[最小下单额:开不出来]"
MIN_STAKE_BUMP_TAG = "[最小下单额:放大]"
FT_MIN_STAKE_ADJUST = 1.3  # freqtrade 最多把仓位放大 30% 去凑最小下单额 (freqtrade/wallets.py)

EXIT_PREV_CLOSE = "cr_prev_close"  # 收盘价回到暴跌前一天的收盘价 C_{t-1} 或以上

# trade custom data 的键 (开仓成交时写入)
KEY_PREV_CLOSE = "cr_prev_close"  # C_{t-1}
KEY_CRASH_LOW = "cr_crash_low"  # L_t
KEY_CRASH_CLOSE = "cr_crash_close"  # C_t
KEY_SIGNAL_DATE = "cr_signal_date"  # 第 t 天 (信号K线) 的日期

_EPOCH = pd.Timestamp("1970-01-01", tz="UTC")


# ---------------------------------------------------------------------------
# 纯函数 (向量化, 第 t 行只用到第 t 行及更早的数据)
# ---------------------------------------------------------------------------


def epoch_seconds(dates: pd.Series) -> np.ndarray:
    """freqtrade 的 date 列 -> 整数 UTC 秒 (不依赖 ns / ms 精度)。"""
    return ((pd.to_datetime(dates, utc=True) - _EPOCH) // pd.Timedelta(seconds=1)).to_numpy(dtype="int64")


def log_return(df: DataFrame) -> pd.Series:
    """逐根日对数收益 ln(C_t / C_{t-1}); 价格 <= 0 = NaN。"""
    c = pd.to_numeric(df["close"], errors="coerce").astype("float64")
    return np.log(c.where(c > 0)).diff()


def capitulation_features(df: DataFrame, window: int) -> DataFrame:
    """单个币逐根: r (当天对数收益)、sigma (前 window 天对数收益的标准差, 不含当天)、vmed (前 window 天成交量中位数, 不含当天)。"""
    r = log_return(df)
    sigma = r.shift(1).rolling(int(window), min_periods=int(window)).std()
    sigma = sigma.where(sigma > 0)  # 0 = 价格一直没动 (没有成交 / 补出来的K线), 倍数没有意义
    v = pd.to_numeric(df["volume"], errors="coerce").astype("float64")
    vmed = v.shift(1).rolling(int(window), min_periods=int(window)).median()
    return pd.DataFrame({"r": r.to_numpy(), "sigma": sigma.to_numpy(), "vol": v.to_numpy(), "vmed": vmed.to_numpy()})


def capitulation_table(
    frames: dict[str, DataFrame | None],
    btc: DataFrame | None,
    k_sigma: float,
    volume_mult: float,
    window: int,
    skip_first: int = 0,
) -> dict[str, Any]:
    """币池里每个币的暴跌信号和分数 r / sigma, 按日期对齐, 同一天在有信号的币之间排名 (1 = 最负)。

    返回 {"score": 宽表 (行 = 日期的 UTC 秒数, 列 = 币; 没有信号 = NaN), "rank": 同形状名次, "n": 每天有信号的币数,
          "btc_r": BTC 当天对数收益}。每一行只用到这一天及以前的数据, 排名只在同一行内比较, 无未来函数。
    skip_first > 0: 每个币自己的前 skip_first 根不参加排名 (回测时 freqtrade 按根数裁掉的预热, 那几天这个币不能交易)。
    BTC 没有数据 = 所有日子都不满足 "BTC 当天下跌", 没有信号。
    """
    if btc is not None and not btc.empty:
        btc_r = pd.Series(log_return(btc).to_numpy(), index=epoch_seconds(btc["date"]), dtype="float64")
        btc_r = btc_r[~btc_r.index.duplicated(keep="last")]
    else:
        btc_r = pd.Series(dtype="float64")
    scores: dict[str, pd.Series] = {}
    for pair in sorted(frames):
        df = frames[pair]
        if df is None or df.empty:
            continue
        idx = epoch_seconds(df["date"])
        f = capitulation_features(df, window)
        b = btc_r.reindex(idx).to_numpy(dtype="float64")
        with np.errstate(invalid="ignore", divide="ignore"):
            sig = (
                (f["r"].to_numpy() <= -float(k_sigma) * f["sigma"].to_numpy())
                & (f["vol"].to_numpy() >= float(volume_mult) * f["vmed"].to_numpy())
                & (b < 0)
            )
            score = np.where(sig, f["r"].to_numpy() / f["sigma"].to_numpy(), np.nan)
        score[~np.isfinite(score)] = np.nan
        if skip_first > 0:
            score[: int(skip_first)] = np.nan
        ser = pd.Series(score, index=idx, dtype="float64")
        scores[pair] = ser[~ser.index.duplicated(keep="last")]
    if not scores:
        return {}
    wide = pd.DataFrame(scores).sort_index()
    return {
        "score": wide,
        "rank": wide.rank(axis=1, ascending=True, method="first"),
        "n": wide.notna().sum(axis=1),
        "btc_r": btc_r,
    }


# ---------------------------------------------------------------------------
# 策略
# ---------------------------------------------------------------------------


class CapitulationRebound(EasyTradeRiskMixin, IStrategy):
    INTERFACE_VERSION = 3

    timeframe = "1d"
    can_short = False
    startup_candle_count = 60
    # 回测 / 分析: 每根K线只分析一次。实盘 / 模拟盘: bot_start 里改成 False (见文件开头 "实现说明")
    process_only_new_candles = True

    minimal_roi = {"0": 100}
    trailing_stop = False
    use_custom_stoploss = True
    use_exit_signal = True  # custom_exit (回到前一天收盘价 / 超时 / 止损补救) 需要它
    exit_profit_only = False
    ignore_roi_if_entry_signal = False
    stoploss = -0.50  # 兜底 (2 倍杠杆 = 价格反向 25%), 成交后换成规格里的止损

    # ---------------- 参数 (3 个, 冻结, 不调) ----------------
    k_sigma = 3.0
    volume_mult = 2.0
    max_hold_days = 5

    # ---------------- 固定常量 (冻结) ----------------
    window_days = 20  # sigma20 和成交量中位数的窗口
    max_signals_per_day = 5
    struct_atr_mult = 1.0  # L_t - 1 x ATR20
    stop_floor_frac = 0.8  # 止损不低于 0.8 x C_t

    # ---------------- 风控 (easytrade_risk) ----------------
    atr_period = 20
    atr_column = "atr"
    stop_atr_mult = 3.0  # 入场价 - 3 x ATR20
    max_hold_candles = max_hold_days
    # risk_per_trade 1% / max_portfolio_risk 6% / dd_throttle 8% / max_leverage_param 2: 用混入类默认值
    # btc_pair = "BTC/USDT:USDT" (混入类默认值)
    et_name = "caprebound"

    # ---------------- 实盘 (不影响回测) ----------------
    # 有币的最新日线还没到时, 最多等多少秒 (从K线收盘算起); 要比 ignore_buying_expired_candle_after (600) 短
    xs_late_candle_wait = 300

    plot_config = {
        "main_plot": {},
        "subplots": {
            "Shock": {"cr_shock": {"color": "#eb5757"}},
            "Rank": {"cr_rank": {"color": "#2f80ed"}, "cr_n": {"color": "#828282"}},
        },
    }

    # ======================================================================
    # 币池 / 数据 (和 XSMomentum 相同的做法)
    # ======================================================================

    def informative_pairs(self):
        # 白名单里的币本来就按 1d 刷新; BTC 日线 (同周期) 可能不在白名单里
        return [(self.btc_pair, self.timeframe)]

    def bot_start(self, **kwargs: Any) -> None:
        super().bot_start(**kwargs)
        if self._et_is_live():
            # 实盘: 每一轮都重新分析所有币, 晚到的日线一到, 所有币都按完整的截面重新排名 (表按数据指纹缓存)
            self.process_only_new_candles = False

    def _xs_now(self) -> datetime:
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
        """一个币的日线 (只含已收盘 K 线)。读不到返回 None。只读, 不能改返回的数据。"""
        try:
            if self._et_is_live():
                ohlcv = getattr(self.dp, "ohlcv", None)
                if ohlcv is not None:
                    df = ohlcv(pair=pair, timeframe=self.timeframe, copy=False)
                else:
                    df = self.dp.get_pair_dataframe(pair=pair, timeframe=self.timeframe)
            else:
                # 回测 / 分析: 直接读磁盘历史 (和回测主数据同一段, 含预热)
                df = self.dp.historic_ohlcv(pair=pair, timeframe=self.timeframe)
        except Exception as exc:  # noqa: BLE001 — 读不到就不参加排名
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
        """回测 / 分析时 freqtrade 按根数裁掉每个币开头的 startup_candle_count 根; 排名时也跳过。实盘返回 0。"""
        if self._et_is_live():
            return 0
        try:
            n = int(self.config.get("startup_candle_count") or self.startup_candle_count or 0)
        except (TypeError, ValueError):
            n = int(self.startup_candle_count or 0)
        return max(0, n)

    @staticmethod
    def _xs_fingerprint(pair: str, df: DataFrame | None) -> tuple[str, int | None, int]:
        if df is None or df.empty:
            return pair, None, 0
        return pair, int(pd.Timestamp(df["date"].iat[-1]).timestamp()), int(len(df))

    def _xs_pending(self, fingerprint: tuple) -> tuple[int | None, list[str]]:
        """实盘: 有没有币 "前一天的K线有了、最新一根还没到"。回测 / 分析: 永远是 (None, [])。"""
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
                logger.warning("%s: %s 的最新日线 (%s) 收盘 %d 秒后还没到, 不再等了, 这一天按其余 %d 个币排名",
                               self.et_name, " ".join(late), day, int(waited), len(lasts) - len(late))
            return newest, []
        if ("wait", newest) not in logged:
            logged.add(("wait", newest))
            if len(logged) > 100:
                logged.clear()
            logger.info("%s: %s 的最新日线 (%s) 还没到, 先不出信号, 最多等到收盘后 %d 秒",
                        self.et_name, " ".join(late), day, int(self.xs_late_candle_wait))
        return newest, late

    def _xs_table(self, ref: DataFrame) -> dict[str, Any] | None:
        """整个币池的暴跌信号排名表 (按 "每个币的数据指纹" 缓存, 同一批数据只算一次)。"""
        universe = tuple(self.xs_universe())
        if not universe or ref.empty:
            return None
        frames = {p: self._xs_frame(p) for p in universe}
        btc = frames[self.btc_pair] if self.btc_pair in frames else self._xs_frame(self.btc_pair)
        fp_frames = {**frames, self.btc_pair: btc}
        fingerprint = tuple(self._xs_fingerprint(p, fp_frames[p]) for p in sorted(fp_frames))
        key = (
            universe,
            fingerprint,
            str(self.config.get("timerange")),
            int(self.config.get("startup_candle_count") or 0),
        )
        cache = self.__dict__.setdefault("_xs_cache", {})
        if key not in cache:
            if btc is None:
                logger.warning("%s: 没有 %s %s 数据, \"BTC 当天下跌\" 无法判断, 不开仓", self.et_name, self.btc_pair, self.timeframe)
            table = capitulation_table(
                frames, btc, float(self.k_sigma), float(self.volume_mult), int(self.window_days), self._xs_backtest_trim()
            )
            if len(cache) >= 4:
                cache.clear()
            cache[key] = table or None
            if table:
                n = table["n"]
                logger.info(
                    "%s: 暴跌信号截面表: 币池 %d 个, 有数据 %d 个, 有信号的日子 %d 天 (共 %d 个信号, 一天最多 %d 个), 日期到 %s",
                    self.et_name, len(universe), table["score"].shape[1], int((n > 0).sum()), int(n.sum()),
                    int(n.max()) if len(n) else 0,
                    pd.Timestamp(int(n.index[-1]), unit="s", tz="UTC").date() if len(n) else "-",
                )
        table = cache[key]
        if not table:
            return None
        pending_date, late = self._xs_pending(fingerprint)
        return {**table, "pending_date": pending_date if late else None}

    # ======================================================================
    # freqtrade 接口
    # ======================================================================

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        pair = metadata["pair"]
        dataframe["atr"] = atr(dataframe, self.atr_period)
        f = capitulation_features(dataframe, int(self.window_days))
        dataframe["cr_r"] = f["r"].to_numpy()
        dataframe["cr_sigma"] = f["sigma"].to_numpy()
        dataframe["cr_vmed"] = f["vmed"].to_numpy()
        with np.errstate(invalid="ignore", divide="ignore"):
            dataframe["cr_shock"] = f["r"].to_numpy() / f["sigma"].to_numpy()  # r / sigma20 (画图 / 排查用)
        dataframe["cr_prev_close"] = pd.to_numeric(dataframe["close"], errors="coerce").shift(1).to_numpy()  # C_{t-1}
        n = len(dataframe)
        cols = {c: np.full(n, np.nan) for c in ("cr_btc_r", "cr_score", "cr_rank", "cr_n")}
        table = self._xs_table(dataframe) if getattr(self, "dp", None) is not None else None
        if table:
            idx = epoch_seconds(dataframe["date"])
            if pair in table["score"].columns:
                cols["cr_score"] = table["score"][pair].reindex(idx).to_numpy(dtype="float64")
                cols["cr_rank"] = table["rank"][pair].reindex(idx).to_numpy(dtype="float64")
            cols["cr_n"] = table["n"].reindex(idx).to_numpy(dtype="float64")
            cols["cr_btc_r"] = table["btc_r"].reindex(idx).to_numpy(dtype="float64")
            if table.get("pending_date") is not None:
                # 实盘: 还在等个别币的最新日线, 最新那一行的排名不完整 -> 不出信号
                cols["cr_rank"] = np.where(idx == int(table["pending_date"]), np.nan, cols["cr_rank"])
        for c, v in cols.items():
            dataframe[c] = v
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        rank = dataframe["cr_rank"].to_numpy(dtype="float64")
        with np.errstate(invalid="ignore"):
            long_sig = rank <= int(self.max_signals_per_day)  # NaN (没有信号) -> False
        dataframe["enter_long"] = long_sig.astype("int64")
        dataframe["enter_short"] = 0
        tags = np.full(len(dataframe), None, dtype=object)
        tags[long_sig] = "cr_long"
        dataframe["enter_tag"] = pd.Series(tags, index=dataframe.index, dtype=object)
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        # 离场全部在 custom_exit (回到 C_{t-1} 要和这笔交易自己的 C_{t-1} 比) / 超时 / 止损里
        dataframe["exit_long"] = 0
        dataframe["exit_short"] = 0
        return dataframe

    # ======================================================================
    # 止损 / 离场
    # ======================================================================

    def et_structure_stop(self, row: pd.Series, is_short: bool) -> float | None:
        """结构止损 = max(L_t - 1 x ATR20, 0.8 x C_t) (row = 信号K线 = 暴跌当天)。混入类再和 入场价 - 3 x ATR20 取更近的。"""
        if is_short:
            return None
        low = positive_or_none(row.get("low"))
        a = positive_or_none(row.get(self.atr_column))
        c = positive_or_none(row.get("close"))
        if low is None or a is None or c is None:
            return None
        return max(low - float(self.struct_atr_mult) * a, float(self.stop_floor_frac) * c)

    def _cr_store_levels(self, trade: Trade) -> float | None:
        """把信号K线的 C_{t-1}、L_t、C_t 存进交易记录, 返回 C_{t-1}。已经存过就直接返回。"""
        stored = positive_or_none(trade.get_custom_data(KEY_PREV_CLOSE))
        if stored is not None:
            return stored
        row = self.et_signal_row_for_trade(trade)
        if row is None:
            return None
        prev_close = positive_or_none(row.get("cr_prev_close"))
        if prev_close is None:
            return None
        trade.set_custom_data(KEY_PREV_CLOSE, float(prev_close))
        low = positive_or_none(row.get("low"))
        close = positive_or_none(row.get("close"))
        if low is not None:
            trade.set_custom_data(KEY_CRASH_LOW, float(low))
        if close is not None:
            trade.set_custom_data(KEY_CRASH_CLOSE, float(close))
        trade.set_custom_data(KEY_SIGNAL_DATE, str(pd.Timestamp(row.get("date")).isoformat()))
        return float(prev_close)

    def order_filled(self, pair: str, trade: Trade, order: Any, current_time: datetime, **kwargs: Any) -> None:
        try:
            if getattr(order, "ft_order_side", None) == trade.entry_side and trade.nr_of_successful_entries <= 1:
                if self._cr_store_levels(trade) is None:
                    logger.warning("%s %s: 找不到信号K线, 没能记下暴跌前一天的收盘价 (之后再试)", self.et_name, pair)
        except Exception as exc:  # noqa: BLE001 — 只是记录, 不能影响成交
            logger.warning("%s %s: 记录暴跌K线价格失败: %s", self.et_name, pair, exc)

    def custom_exit(
        self,
        pair: str,
        trade: Trade,
        current_time: datetime,
        current_rate: float,
        current_profit: float,
        **kwargs: Any,
    ) -> str | None:
        """先走混入类 (止损补救 / 回测跳空 / 超时), 再看 "上一根已收盘日线的收盘价 >= C_{t-1}"。"""
        reason = super().custom_exit(
            pair=pair, trade=trade, current_time=current_time, current_rate=current_rate,
            current_profit=current_profit, **kwargs,
        )
        if reason:
            return reason
        prev_close = self._cr_store_levels(trade)
        last = self.et_last_row(pair)
        if prev_close is None or last is None:
            return None
        last_close = positive_or_none(last.get("close"))
        entry_candle = pd.Timestamp(timeframe_to_prev_date(self.timeframe, trade.open_date_utc))
        # 只看开仓之后收盘的日线 (开仓那根及以后; 信号K线本身收盘价一定低于 C_{t-1})
        if last_close is not None and pd.Timestamp(last["date"]) >= entry_candle and last_close >= prev_close:
            return EXIT_PREV_CLOSE
        return None

    # ======================================================================
    # 组合热度: 回测里数据已经结束的币 (中途下架) 不算
    # ======================================================================

    def cr_data_ended(self, pair: str, current_time: datetime | None) -> float | None:
        """回测 / 分析: 这个币的数据在 current_time 之前已经结束 (中途下架, 第二阶段 RESEARCH_KEEP_DELISTED=1 保留的
        LUNA / FTT 这类币) 时返回 freqtrade 最后给它的平仓价 (最后一根K线的开盘价), 否则 None。

        freqtrade 回测里一个币没有K线以后就不再处理它 (optimize/backtesting.py validate_row), 还开着的多单
        要到这一段结束才按最后一根K线的开盘价 force_exit。真实的交易所下架时按结算价强制平仓,
        所以从最后一根K线收盘起这笔多单不再算进组合热度 (浮动盈亏按那个 force_exit 价格算)。
        它占着的 freqtrade 名额 (max_open_trades 6 里的 1 个) 和保证金要到这一段结束才放出来, 策略改不了。
        中间缺几根K线 (后面还有数据) 不算结束。实盘永远返回 None (交易所自己平掉下架合约的仓位)。
        """
        if current_time is None or self._et_is_live() or getattr(self, "dp", None) is None:
            return None
        try:
            df = self.dp.historic_ohlcv(pair=pair, timeframe=self.timeframe)
        except Exception:  # noqa: BLE001
            return None
        if df is None or df.empty:
            return None
        last = int(pd.Timestamp(df["date"].iat[-1]).timestamp())
        if current_time.timestamp() < last + int(timeframe_to_seconds(self.timeframe)):
            return None
        return positive_or_none(df["open"].iat[-1]) or 0.0

    def et_drawdown(self, current_time: datetime) -> float:
        # 记下 "现在" 给 et_portfolio_state 判断哪些币的数据已经结束 (混入类的 et_portfolio_state 没有时间参数)
        self.__dict__["_cr_now"] = current_time
        return super().et_drawdown(current_time)

    def et_portfolio_state(self) -> tuple[float, float]:
        """(组合热度, 浮动盈亏): 和混入类相同, 只是回测里数据已经结束的币不算热度 (见 cr_data_ended)。"""
        now = self.__dict__.get("_cr_now")
        heat = 0.0
        upnl = 0.0
        buf = float(self.sizing_cost_buffer)
        for t in Trade.get_trades_proxy(is_open=True):
            amount = float(t.amount or 0.0)
            for o in getattr(t, "open_orders", []) or []:
                if getattr(o, "ft_order_side", None) == t.entry_side:
                    amount += float(o.safe_remaining or 0.0)
            if amount <= 0:
                continue
            ended = self.cr_data_ended(t.pair, now)
            if ended is not None:
                price = ended or float(t.open_rate)
                upnl += amount * ((t.open_rate - price) if t.is_short else (price - t.open_rate))
                continue
            price = self.et_last_close(t.pair) or float(t.open_rate)
            stop = self.et_trade_stop(t)
            if stop is None:
                cap = self.et_stop_cap(t.leverage)
                stop = price * (1 + cap) if t.is_short else price * (1 - cap)
            dist = (stop - price) if t.is_short else (price - stop)
            heat += amount * (max(dist, 0.0) + price * buf)
            upnl += amount * ((t.open_rate - price) if t.is_short else (price - t.open_rate))
        return heat, upnl

    # ======================================================================
    # 仓位: easytrade_risk 的按止损距离定仓; 另外记 "最小下单额" 日志 (和 XSMomentum 同一个标记)
    # ======================================================================

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
        self.__dict__["_cr_now"] = current_time
        stake = super().custom_stake_amount(
            pair=pair, current_time=current_time, current_rate=current_rate, proposed_stake=proposed_stake,
            min_stake=min_stake, max_stake=max_stake, leverage=leverage, entry_tag=entry_tag, side=side, **kwargs,
        )
        try:
            ms = positive_or_none(min_stake)
            if stake > 0 and ms is not None and stake < ms:
                seen = self.__dict__.setdefault("_cr_min_stake_seen", set())
                key = (pair, timeframe_to_prev_date(self.timeframe, current_time))
                if key not in seen:
                    if len(seen) > 2000:
                        seen.clear()
                    seen.add(key)
                    log = logger.warning if self._et_is_live() else logger.info
                    if stake * FT_MIN_STAKE_ADJUST < ms:
                        log("%s %s %s: 按风险算的保证金 %.2f USDT 比交易所最小下单额 %.2f USDT 小太多 (freqtrade 最多放大 30%%), "
                            "这一笔开不出来", MIN_STAKE_SKIP_TAG, self.et_name, pair, stake, ms)
                    else:
                        log("%s %s %s: 按风险算的保证金 %.2f USDT 比交易所最小下单额 %.2f USDT 小, freqtrade 会放大到 %.2f "
                            "(这一笔实际风险比设计的大, 最多 +30%%)", MIN_STAKE_BUMP_TAG, self.et_name, pair, stake, ms, ms)
        except Exception as exc:  # noqa: BLE001 — 只是记日志, 不能影响下单
            logger.debug("%s %s 检查最小下单额失败: %s", self.et_name, pair, exc)
        return stake
