"""
DonchianTrend -- 唐奇安通道突破 (海龟交易法 "系统 2" 的简化版), 多空双向。

逻辑 (逐根已收盘 K 线, 无未来函数):
  入场  做多: 收盘价 > 前 entry_period 根 K 线的最高价 (不含当根), 且处于多头趋势
        做空: 收盘价 < 前 entry_period 根 K 线的最低价, 且处于空头趋势
  趋势  日线收盘价在日线 EMA200 上方 = 多头, 下方 = 空头
        (策略跑 1d 时直接用本周期; 跑 4h 等更小周期时用 merge_informative_pair 合并日线 EMA200)
        可选 BTC 大盘过滤 (use_btc_filter): 只在 BTC 日线也在 EMA200 同一侧时开仓
  止损  初始止损 = 入场价 -/+ stop_atr_mult x ATR20 (海龟的 "2N"), 和离场通道取更近的那个
  离场  做多: 价格碰到最近 exit_period 根 K 线的最低价 (离场通道下轨, 作为移动止损挂着)
        做空: 价格碰到最近 exit_period 根 K 线的最高价
        通道只会往有利方向移动 (freqtrade 的止损只收紧不放宽), 所以盈利单会一路跟着趋势走
  仓位  easytrade_risk: 每笔打到止损亏总资金 0.5%, 组合热度 <= 3%, 杠杆 <= 2 (硬上限 5)

可 hyperopt 的参数只有 3 个 (只在训练段调参, 验证段不改): entry_period / exit_period / stop_atr_mult。
周期: 默认 1d; 4h 用 --timeframe 4h (参数单位是 "根 K 线", 4h 的 55 根 ≈ 9 天, 是另一套系统, 要单独验证)。
说明文档: docs/strategies-step2.md "趋势类 / DonchianTrend"。
"""

from __future__ import annotations

import logging

import numpy as np
import pandas as pd
from pandas import DataFrame

from freqtrade.exchange import timeframe_to_seconds
from freqtrade.persistence import Trade
from freqtrade.strategy import DecimalParameter, IntParameter, IStrategy

from easytrade_risk import EasyTradeRiskMixin, atr, ema


logger = logging.getLogger(__name__)


class DonchianTrend(EasyTradeRiskMixin, IStrategy):
    INTERFACE_VERSION = 3

    timeframe = "1d"
    can_short = True
    # EMA200 (1d 本周期) 需要预热, 与 BigTrendShort 一致取 300。300 根时起点的影响还剩约 37%
    # (talib 的 EMA 从前 200 根的简单平均起步, 之后只递推 100 次: (1-2/201)^100; recursive-analysis 实测差约 6%),
    # 600 根时约 2%。freqtrade 每次回测只在 --timerange 开始前加载这 300 根, 所以从中间某天开始的回测,
    # 开头一段贴近 EMA200 的个别信号会和连续回测不同; scripts/research.sh 给样本外 / 走步各段最多 600 根预热。
    # 跑 4h 时日线 EMA200 来自合并的日线数据, freqtrade 会按同样的根数 (按 "日线" 算) 额外加载日线历史。
    startup_candle_count = 300
    process_only_new_candles = True

    # 不用 ROI 止盈, 只靠 止损 / 离场通道
    minimal_roi = {"0": 100.0}
    trailing_stop = False
    use_custom_stoploss = True
    use_exit_signal = True  # custom_exit (止损补救 / 跳空) 需要它
    exit_profit_only = False
    ignore_roi_if_entry_signal = False
    # freqtrade 的兜底止损 (账户口径 = 价格距离 x 杠杆): 2 倍杠杆时 = 价格反向 25%。
    # 正常情况下成交后马上被 custom_stoploss 换成 ATR 止损, 这里只是保险。
    stoploss = -0.50

    # ---------------- 可 hyperopt 的参数 (范围故意窄) ----------------
    entry_period = IntParameter(20, 80, default=55, space="buy")
    exit_period = IntParameter(10, 30, default=20, space="sell")
    stop_atr_mult = DecimalParameter(1.5, 3.5, default=2.0, decimals=1, space="sell")

    # ---------------- 固定参数 ----------------
    atr_period = 20  # 海龟的 N = 20 日 ATR
    atr_column = "atr"
    trend_ema_period = 200
    trend_timeframe = "1d"
    use_btc_filter = False
    btc_ema_period = 200
    enable_long = True
    enable_short = True
    et_name = "donchian"

    plot_config = {
        "main_plot": {
            "dc_upper": {"color": "#27ae60"},
            "dc_lower": {"color": "#eb5757"},
            "dc_exit_low": {"color": "#6fcf97"},
            "dc_exit_high": {"color": "#f2994a"},
            "trend_ema": {"color": "#9b51e0"},
        },
    }

    def informative_pairs(self):
        pairs = []
        if self.dp is None:
            return pairs
        if timeframe_to_seconds(self.timeframe) < timeframe_to_seconds(self.trend_timeframe):
            pairs += [(p, self.trend_timeframe) for p in self.dp.current_whitelist()]
        if self.use_btc_filter:
            pairs.append((self.btc_pair, self.btc_timeframe))
        return pairs

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["atr"] = atr(dataframe, self.atr_period)

        # 趋势: 日线 EMA200。本周期 >= 1d 时直接算; 更小周期时合并日线 (收盘后才可见)。
        if timeframe_to_seconds(self.timeframe) >= timeframe_to_seconds(self.trend_timeframe):
            dataframe["trend_ema"] = ema(dataframe["close"], self.trend_ema_period)
            dataframe["trend_close"] = dataframe["close"]
        else:
            dataframe["trend_ema"] = np.nan
            dataframe["trend_close"] = np.nan
            inf = None
            if self.dp is not None:
                try:
                    inf = self.dp.get_pair_dataframe(pair=metadata["pair"], timeframe=self.trend_timeframe)
                except Exception as exc:
                    logger.warning("DonchianTrend %s 读取 %s 失败: %s", metadata["pair"], self.trend_timeframe, exc)
            if inf is not None and not inf.empty:
                inf = inf[["date", "close"]].copy()
                inf["ema"] = ema(inf["close"], self.trend_ema_period)
                dataframe = dataframe.drop(columns=["trend_ema", "trend_close"])
                dataframe = self.et_merge_htf(dataframe, inf, self.trend_timeframe, "trend")
                dataframe = dataframe.rename(columns={"ema_trend": "trend_ema", "close_trend": "trend_close"})

        if self.use_btc_filter:
            dataframe = self.et_add_btc_regime(dataframe, self.btc_ema_period)
        return dataframe

    # ------------------------------------------------------------------
    # 信号 (通道长度是 hyperopt 参数, 所以在这里算, 不放 populate_indicators)
    # ------------------------------------------------------------------

    def _channels(self, dataframe: DataFrame) -> None:
        n_in = int(self.entry_period.value)
        n_out = int(self.exit_period.value)
        # 入场通道: 前 N 根 (不含当根) 的最高 / 最低价
        dataframe["dc_upper"] = dataframe["high"].rolling(n_in, min_periods=n_in).max().shift(1)
        dataframe["dc_lower"] = dataframe["low"].rolling(n_in, min_periods=n_in).min().shift(1)
        # 离场通道: 最近 M 根 (含当根已收盘 K 线) 的最低 / 最高价 -> 下一根 K 线的移动止损价
        dataframe["dc_exit_low"] = dataframe["low"].rolling(n_out, min_periods=n_out).min()
        dataframe["dc_exit_high"] = dataframe["high"].rolling(n_out, min_periods=n_out).max()

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        self._channels(dataframe)
        c = dataframe["close"]
        ok = (dataframe["volume"] > 0) & dataframe["atr"].notna() & dataframe["trend_ema"].notna()
        up = dataframe["trend_close"] > dataframe["trend_ema"]
        down = dataframe["trend_close"] < dataframe["trend_ema"]
        if self.use_btc_filter:
            up &= dataframe["btc_regime"] > 0
            down &= dataframe["btc_regime"] < 0

        long_sig = ok & up & (c > dataframe["dc_upper"]) & bool(self.enable_long)
        short_sig = ok & down & (c < dataframe["dc_lower"]) & bool(self.enable_short)
        dataframe["enter_long"] = long_sig.astype("int64")
        dataframe["enter_short"] = short_sig.astype("int64")
        tags = pd.Series(None, index=dataframe.index, dtype=object)
        tags[long_sig] = f"donchian_long_{int(self.entry_period.value)}"
        tags[short_sig] = f"donchian_short_{int(self.entry_period.value)}"
        dataframe["enter_tag"] = tags
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        # 离场靠离场通道 (挂成移动止损, 价格一碰到就走, 和海龟原版一样), 不用收盘信号
        if "dc_exit_low" not in dataframe.columns:
            self._channels(dataframe)
        dataframe["exit_long"] = 0
        dataframe["exit_short"] = 0
        return dataframe

    # ------------------------------------------------------------------
    # 止损 (被 EasyTradeRiskMixin 调用)
    # ------------------------------------------------------------------

    def et_structure_stop(self, row: pd.Series, is_short: bool) -> float | None:
        # 入场时离场通道比 2N 止损更近, 就用离场通道
        return row.get("dc_exit_high") if is_short else row.get("dc_exit_low")

    def et_trailing_stop(self, trade: Trade, last_row: pd.Series) -> float | None:
        return last_row.get("dc_exit_high") if trade.is_short else last_row.get("dc_exit_low")
