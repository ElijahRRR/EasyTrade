"""
VolSqueezeBreakout -- 波动收缩 (布林带缩进肯特纳通道, "squeeze") 之后的放量突破, 多空双向, 默认 1h (也可 4h)。

思路: 波动率有 "收缩 -> 扩张" 的周期。价格窄幅横盘一段时间后, 第一次放量冲出横盘区间,
      往往会走出一段; 没放量、或者已经冲得太远的突破不追。

逻辑 (逐根已收盘 K 线, 无未来函数):
  收缩  布林带 (20 根, 2 倍标准差) 整个缩进肯特纳通道 (EMA20 +/- 1.5 x ATR20) 里 = squeeze 中。
        要求: 在信号 K 线之前的 3 根里, 曾经连续 squeeze 至少 min_squeeze_bars (6) 根。
  突破  做多: 收盘价 > 前 range_period 根 K 线的最高价 (横盘区间上沿, 不含当根), 且收阳
        做空: 收盘价 < 前 range_period 根 K 线的最低价, 且收阴
  放量  本根成交量 / 前 50 根成交量的中位数 >= vol_ratio_min
        * 只用 "比值": 同一个币自己和自己比, 成交量单位 (张 / 币 / USDT) 互相抵消,
          不依赖任何绝对成交量门槛 (OKX 成交量单位问题见 docs/okx-volume-units.md)
  不追  收盘价离区间边沿超过 max_extension_atr (1.5) 个 ATR14 的不追 (已经冲太远, 容易回落)
  趋势  只顺日线方向: 上一根已收盘日线的收盘价在日线 EMA50 上方才做多, 下方才做空
        (use_trend_filter = False 可关闭)
  止损  入场价 -/+ stop_atr_mult x ATR14, 和横盘区间另一侧取更近的那个
  离场  (1) 吊灯止损: 持仓以来最高价 (做空 = 最低价) -/+ trail_atr_mult x 入场 ATR, 只收紧
        (2) 突破失败: 收盘价回到布林中轨 (SMA20) 另一侧 -> 下一根开盘平仓
        (3) 超时: 持有满 max_hold_candles 根 (默认 72 根: 1h = 3 天, 4h = 12 天)
  仓位  easytrade_risk: 每笔打到止损亏总资金 0.5%, 组合热度 <= 3%, 杠杆 <= 2 (硬上限 5)

可 hyperopt 的参数 4 个 (只在训练段调参): range_period / vol_ratio_min / stop_atr_mult / trail_atr_mult。
周期: 默认 1h; 4h 用 --timeframe 4h (参数单位是 "根 K 线", 4h 是另一套系统, 要单独验证)。
说明文档: docs/strategies-step2.md "非趋势类 / VolSqueezeBreakout"。
"""

from __future__ import annotations

import logging

import numpy as np
import pandas as pd
from pandas import DataFrame

from freqtrade.exchange import timeframe_to_seconds
from freqtrade.strategy import DecimalParameter, IntParameter, IStrategy

from easytrade_risk import EasyTradeRiskMixin, atr, ema


logger = logging.getLogger(__name__)


def _run_length(flag: pd.Series) -> pd.Series:
    """连续为 True 的根数 (当前这根为 False 时 = 0)。只用当前及更早的 K 线。"""
    f = flag.fillna(False).astype(bool)
    groups = (~f).cumsum()
    return f.astype("int64").groupby(groups).cumsum()


class VolSqueezeBreakout(EasyTradeRiskMixin, IStrategy):
    INTERFACE_VERSION = 3

    timeframe = "1h"
    can_short = True
    # 本周期最长 = 成交量中位数 50 根 + range_period 上限 40 根; squeeze 连续计数从数据开头累计,
    # 和预热长度无关 (只要 >= 20 根就稳定)。日线 EMA50 来自合并的日线数据 (回测按 300 根日线额外加载)。
    startup_candle_count = 300
    process_only_new_candles = True

    minimal_roi = {"0": 100.0}
    trailing_stop = False
    use_custom_stoploss = True
    use_exit_signal = True
    exit_profit_only = False
    ignore_roi_if_entry_signal = False
    stoploss = -0.50  # 兜底 (2 倍杠杆 = 价格反向 25%), 成交后换成 ATR / 区间止损

    # ---------------- 可 hyperopt 的参数 (范围故意窄) ----------------
    range_period = IntParameter(12, 40, default=20, space="buy")
    vol_ratio_min = DecimalParameter(1.2, 3.0, default=1.5, decimals=1, space="buy")
    stop_atr_mult = DecimalParameter(1.5, 3.0, default=2.0, decimals=1, space="sell")
    trail_atr_mult = DecimalParameter(2.0, 4.5, default=3.0, decimals=1, space="sell")

    # ---------------- 固定参数 (不调) ----------------
    bb_period = 20
    bb_std = 2.0
    kc_period = 20
    kc_mult = 1.5
    min_squeeze_bars = 6
    squeeze_recent = 3
    vol_median_period = 50
    max_extension_atr = 1.5
    atr_period = 14
    atr_column = "atr"
    max_hold_candles = 72
    use_trend_filter = True
    trend_timeframe = "1d"
    trend_ema_period = 50
    enable_long = True
    enable_short = True
    et_name = "squeeze"

    plot_config = {
        "main_plot": {
            "bb_upper": {"color": "#2f80ed"},
            "bb_lower": {"color": "#2f80ed"},
            "kc_upper": {"color": "#f2994a"},
            "kc_lower": {"color": "#f2994a"},
            "box_high": {"color": "#27ae60"},
            "box_low": {"color": "#eb5757"},
        },
        "subplots": {
            "Squeeze": {"sq_run": {"color": "#9b51e0"}},
            "RelVol": {"rel_vol": {"color": "#828282"}},
        },
    }

    @property
    def protections(self):
        return [{"method": "CooldownPeriod", "stop_duration_candles": 2}]

    def informative_pairs(self):
        if self.dp is None or not self.use_trend_filter:
            return []
        if timeframe_to_seconds(self.timeframe) >= timeframe_to_seconds(self.trend_timeframe):
            return []
        return [(p, self.trend_timeframe) for p in self.dp.current_whitelist()]

    # ------------------------------------------------------------------
    # 指标
    # ------------------------------------------------------------------

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        c = dataframe["close"].astype("float64")
        dataframe["atr"] = atr(dataframe, self.atr_period)

        # 布林带 (总体标准差, 和 talib.BBANDS 一致) 和 肯特纳通道
        mid = c.rolling(self.bb_period, min_periods=self.bb_period).mean()
        sd = c.rolling(self.bb_period, min_periods=self.bb_period).std(ddof=0)
        dataframe["bb_mid"] = mid
        dataframe["bb_upper"] = mid + self.bb_std * sd
        dataframe["bb_lower"] = mid - self.bb_std * sd
        kc_mid = pd.Series(ema(c, self.kc_period), index=dataframe.index)
        kc_atr = pd.Series(atr(dataframe, self.kc_period), index=dataframe.index)
        dataframe["kc_upper"] = kc_mid + self.kc_mult * kc_atr
        dataframe["kc_lower"] = kc_mid - self.kc_mult * kc_atr
        squeeze = (dataframe["bb_upper"] < dataframe["kc_upper"]) & (dataframe["bb_lower"] > dataframe["kc_lower"])
        dataframe["sq_run"] = _run_length(squeeze)
        # 信号 K 线之前的 squeeze_recent 根里, 最长的连续 squeeze 根数
        dataframe["sq_recent_max"] = (
            dataframe["sq_run"].shift(1).rolling(self.squeeze_recent, min_periods=1).max()
        )

        # 相对成交量: 本根 / 前 50 根中位数 (不含本根)。中位数为 0 (停牌 / 补的空 K 线) -> NaN -> 不开仓
        vmed = dataframe["volume"].rolling(self.vol_median_period, min_periods=self.vol_median_period).median().shift(1)
        dataframe["rel_vol"] = dataframe["volume"] / vmed.where(vmed > 0)

        # 日线趋势: 上一根已收盘日线的 close 相对 EMA50。+1 = 上方, -1 = 下方, NaN = 不知道 (不开仓)
        dataframe["trend_dir"] = np.nan
        if not self.use_trend_filter:
            dataframe["trend_dir"] = 0.0
        elif timeframe_to_seconds(self.timeframe) >= timeframe_to_seconds(self.trend_timeframe):
            e = ema(c, self.trend_ema_period)
            dataframe["trend_dir"] = np.where(np.isnan(e), np.nan, np.sign(c.to_numpy() - e))
        else:
            inf = None
            if self.dp is not None:
                try:
                    inf = self.dp.get_pair_dataframe(pair=metadata["pair"], timeframe=self.trend_timeframe)
                except Exception as exc:
                    logger.warning("VolSqueezeBreakout %s 读取 %s 失败, 趋势过滤视为 '不知道' (不开仓): %s",
                                   metadata["pair"], self.trend_timeframe, exc)
            if inf is None or inf.empty:
                logger.warning("VolSqueezeBreakout %s: 没有 %s 数据, 不开仓", metadata["pair"], self.trend_timeframe)
            else:
                d = inf[["date", "close"]].copy()
                e = ema(d["close"], self.trend_ema_period)
                d["dir"] = np.where(np.isnan(e), np.nan, np.sign(d["close"].to_numpy(dtype="float64") - e))
                dataframe = dataframe.drop(columns=["trend_dir"])
                dataframe = self.et_merge_htf(dataframe, d[["date", "dir"]], self.trend_timeframe, "trend")
                dataframe = dataframe.rename(columns={"dir_trend": "trend_dir"})
        return dataframe

    # ------------------------------------------------------------------
    # 信号 (横盘区间长度是 hyperopt 参数, 所以在这里算)
    # ------------------------------------------------------------------

    def _box(self, dataframe: DataFrame) -> None:
        n = int(self.range_period.value)
        dataframe["box_high"] = dataframe["high"].rolling(n, min_periods=n).max().shift(1)
        dataframe["box_low"] = dataframe["low"].rolling(n, min_periods=n).min().shift(1)

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        self._box(dataframe)
        c = dataframe["close"]
        o = dataframe["open"]
        a = dataframe["atr"]
        ok = (
            (dataframe["volume"] > 0)
            & a.notna()
            & (dataframe["sq_recent_max"] >= self.min_squeeze_bars)
            & (dataframe["rel_vol"] >= float(self.vol_ratio_min.value))
        )
        if self.use_trend_filter:
            up = dataframe["trend_dir"] > 0
            down = dataframe["trend_dir"] < 0
        else:
            up = down = pd.Series(True, index=dataframe.index)

        long_sig = (
            ok & up & (c > dataframe["box_high"]) & (c > o)
            & ((c - dataframe["box_high"]) <= self.max_extension_atr * a)
            & bool(self.enable_long)
        )
        short_sig = (
            ok & down & (c < dataframe["box_low"]) & (c < o)
            & ((dataframe["box_low"] - c) <= self.max_extension_atr * a)
            & bool(self.enable_short)
        )
        dataframe["enter_long"] = long_sig.astype("int64")
        dataframe["enter_short"] = short_sig.astype("int64")
        tags = pd.Series(None, index=dataframe.index, dtype=object)
        tags[long_sig] = f"squeeze_long_{int(self.range_period.value)}"
        tags[short_sig] = f"squeeze_short_{int(self.range_period.value)}"
        dataframe["enter_tag"] = tags
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        # 突破失败: 收盘回到布林中轨另一侧 (中轨不依赖 hyperopt 参数)
        c = dataframe["close"]
        mid = dataframe["bb_mid"]
        fail_long = c < mid
        fail_short = c > mid
        dataframe["exit_long"] = fail_long.astype("int64")
        dataframe["exit_short"] = fail_short.astype("int64")
        tags = pd.Series(None, index=dataframe.index, dtype=object)
        tags[fail_long | fail_short] = "squeeze_failed"
        dataframe["exit_tag"] = tags
        return dataframe

    # ------------------------------------------------------------------
    # 止损 (被 EasyTradeRiskMixin 调用)
    # ------------------------------------------------------------------

    def et_structure_stop(self, row: pd.Series, is_short: bool) -> float | None:
        # 横盘区间的另一侧; 和 ATR 止损取更近的那个 (EasyTradeRiskMixin 负责比较)
        return row.get("box_high") if is_short else row.get("box_low")
