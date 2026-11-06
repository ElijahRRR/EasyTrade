"""
RangeMeanReversion -- 震荡行情里的均值回归 (布林带 + RSI 极值), 多空双向, 默认 1h。

思路: 没有趋势的时候, 价格冲出布林带外沿、RSI 到极值, 大概率会回到中轨。
      最大的风险是 "以为是震荡, 其实是趋势刚开始" -> 用两层 "没有趋势" 过滤 + 很近的止损 + 时间止损。

逻辑 (逐根已收盘 K 线, 无未来函数):
  入场  做多: 收盘价 < 布林下轨 (20 根, bb_std 倍标准差), 且 RSI14 < rsi_extreme
        做空: 收盘价 > 布林上轨, 且 RSI14 > 100 - rsi_extreme
  过滤  (1) 本周期 ADX14 < adx_max                         = 本周期没有趋势
        (2) 高一级周期 (1h 策略用 4h; 4h 策略用 1d) "走平":
            ADX14 < htf_adx_max (25), 且 EMA50 最近 6 根的斜率 < htf_slope_max (1.0) 个 ATR14
            高周期 K 线收盘后才可见 (merge_informative_pair), 无未来函数
        (3) 波动没有突然放大: ATR14 / 最近 100 根平均真实波幅 <= vol_ratio_max (1.5)
            (刚放量突破、急涨急跌时不接飞刀)
  止盈  收盘价回到布林中轨 (做多: 收盘 >= 中轨; 做空: 收盘 <= 中轨) -> 下一根开盘平仓
  止损  入场价 -/+ stop_atr_mult x ATR14 (默认 1.5 倍, 故意很近: 错了就认)
  超时  持有满 max_hold_candles 根 (默认 24 根 = 1h 策略 1 天) 还没回到中轨就平仓
  保护  同一个币: 平仓后冷却 2 根 K 线; 48 根内被止损 2 次 -> 这个币暂停 24 根
        (连续止损通常说明这个币已经从震荡变成趋势)
  仓位  easytrade_risk: 每笔打到止损亏总资金 0.5%, 组合热度 <= 3%, 杠杆 <= 2 (硬上限 5),
        账户回撤 >= 4% 时新仓风险减半

可 hyperopt 的参数 4 个 (只在训练段调参, 验证段不改): bb_std / rsi_extreme / adx_max / stop_atr_mult。
其余阈值都是常用的 "常识值", 固定不调。
说明文档: docs/strategies-step2.md "非趋势类 / RangeMeanReversion"。
"""

from __future__ import annotations

import logging

import numpy as np
import pandas as pd
import talib
from pandas import DataFrame

from freqtrade.exchange import timeframe_to_seconds
from freqtrade.strategy import DecimalParameter, IntParameter, IStrategy

from easytrade_risk import EasyTradeRiskMixin, atr, ema


logger = logging.getLogger(__name__)


def _htf_for(timeframe: str) -> str | None:
    """高一级周期: 4h 以下 -> 4h; 4h 到 1d 以下 -> 1d; 1d 及以上 -> None (用本周期自己算)。"""
    s = timeframe_to_seconds(timeframe)
    if s < timeframe_to_seconds("4h"):
        return "4h"
    if s < timeframe_to_seconds("1d"):
        return "1d"
    return None


class RangeMeanReversion(EasyTradeRiskMixin, IStrategy):
    INTERFACE_VERSION = 3

    timeframe = "1h"
    can_short = True
    # 最长的指标: 100 根平均真实波幅 (本周期)、ADX14 (Wilder 递推, 要 100+ 根才稳定)、高周期 EMA50 / ADX14。
    # 回测时高周期数据也按 "300 根高周期 K 线" 额外加载, 足够 EMA50 / ADX 预热。
    startup_candle_count = 300
    process_only_new_candles = True

    # 不用 ROI, 止盈靠 "回到中轨" 的离场信号
    minimal_roi = {"0": 100.0}
    trailing_stop = False
    use_custom_stoploss = True
    use_exit_signal = True  # 中轨止盈 + custom_exit (超时 / 止损补救) 都需要它
    exit_profit_only = False
    ignore_roi_if_entry_signal = False
    # 兜底止损 (账户口径): 2 倍杠杆 = 价格反向 25%。成交后马上被 custom_stoploss 换成 ATR 止损。
    stoploss = -0.50

    # ---------------- 可 hyperopt 的参数 (范围故意窄) ----------------
    bb_std = DecimalParameter(1.8, 2.6, default=2.0, decimals=1, space="buy")
    rsi_extreme = IntParameter(20, 35, default=30, space="buy")
    adx_max = IntParameter(15, 30, default=22, space="buy")
    stop_atr_mult = DecimalParameter(1.0, 2.5, default=1.5, decimals=1, space="sell")

    # ---------------- 固定参数 (不调) ----------------
    bb_period = 20
    rsi_period = 14
    adx_period = 14
    atr_period = 14
    atr_column = "atr"
    vol_slow_period = 100
    vol_ratio_max = 1.5
    htf_ema_period = 50
    htf_slope_bars = 6
    htf_adx_max = 25.0
    htf_slope_max = 1.0
    max_hold_candles = 24
    enable_long = True
    enable_short = True
    et_name = "range_mr"

    plot_config = {
        "main_plot": {
            "bb_mid": {"color": "#9b51e0"},
            "bb_upper": {"color": "#eb5757"},
            "bb_lower": {"color": "#27ae60"},
        },
        "subplots": {
            "RSI": {"rsi": {"color": "#2f80ed"}},
            "ADX": {"adx": {"color": "#f2994a"}, "adx_htf": {"color": "#828282"}},
        },
    }

    @property
    def protections(self):
        return [
            {"method": "CooldownPeriod", "stop_duration_candles": 2},
            {
                "method": "StoplossGuard",
                "lookback_period_candles": 48,
                "trade_limit": 2,
                "stop_duration_candles": 24,
                "only_per_pair": True,
            },
        ]

    def informative_pairs(self):
        htf = _htf_for(self.timeframe)
        if self.dp is None or htf is None:
            return []
        return [(p, htf) for p in self.dp.current_whitelist()]

    # ------------------------------------------------------------------
    # 指标
    # ------------------------------------------------------------------

    def _htf_features(self, df: DataFrame) -> DataFrame:
        """高周期 "走平" 指标: ADX14, EMA50 最近 N 根的斜率 (以 ATR14 为单位)。"""
        out = df[["date"]].copy()
        h = df["high"].to_numpy(dtype="float64")
        lo = df["low"].to_numpy(dtype="float64")
        c = df["close"].to_numpy(dtype="float64")
        out["adx"] = talib.ADX(h, lo, c, timeperiod=self.adx_period)
        e = pd.Series(ema(df["close"], self.htf_ema_period), index=df.index)
        a = pd.Series(atr(df, self.atr_period), index=df.index)
        out["slope"] = (e - e.shift(self.htf_slope_bars)) / a.where(a > 0)
        return out

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        c = dataframe["close"].astype("float64")
        dataframe["atr"] = atr(dataframe, self.atr_period)
        # 慢速波动 = 最近 100 根真实波幅 (TR) 的简单平均。不用 Wilder ATR100: 它是递推的,
        # 300 根预热时和 "无限长历史" 还差约 10% (recursive-analysis 实测), 结果会随回测起点变。
        tr = pd.Series(
            talib.TRANGE(
                dataframe["high"].to_numpy(dtype="float64"),
                dataframe["low"].to_numpy(dtype="float64"),
                c.to_numpy(),
            ),
            index=dataframe.index,
        )
        dataframe["atr_slow"] = tr.rolling(self.vol_slow_period, min_periods=self.vol_slow_period).mean()
        dataframe["vol_ratio"] = dataframe["atr"] / dataframe["atr_slow"].where(dataframe["atr_slow"] > 0)
        # 布林带: 中轨 = SMA20, 标准差用总体标准差 (ddof=0, 和 talib.BBANDS 一致)
        dataframe["bb_mid"] = c.rolling(self.bb_period, min_periods=self.bb_period).mean()
        dataframe["bb_sd"] = c.rolling(self.bb_period, min_periods=self.bb_period).std(ddof=0)
        dataframe["rsi"] = talib.RSI(c.to_numpy(), timeperiod=self.rsi_period)
        dataframe["adx"] = talib.ADX(
            dataframe["high"].to_numpy(dtype="float64"),
            dataframe["low"].to_numpy(dtype="float64"),
            c.to_numpy(),
            timeperiod=self.adx_period,
        )

        htf = _htf_for(self.timeframe)
        if htf is None:
            feats = self._htf_features(dataframe)
            dataframe["adx_htf"] = feats["adx"]
            dataframe["slope_htf"] = feats["slope"]
            return dataframe

        dataframe["adx_htf"] = np.nan
        dataframe["slope_htf"] = np.nan
        inf = None
        if self.dp is not None:
            try:
                inf = self.dp.get_pair_dataframe(pair=metadata["pair"], timeframe=htf)
            except Exception as exc:
                logger.warning("RangeMeanReversion %s 读取 %s 失败, 高周期过滤视为 '不知道' (不开仓): %s",
                               metadata["pair"], htf, exc)
        if inf is None or inf.empty:
            logger.warning("RangeMeanReversion %s: 没有 %s 数据, 不开仓 (请下载 %s 周期数据)",
                           metadata["pair"], htf, htf)
            return dataframe
        feats = self._htf_features(inf)
        dataframe = dataframe.drop(columns=["adx_htf", "slope_htf"])
        dataframe = self.et_merge_htf(dataframe, feats, htf, "htf")
        return dataframe

    # ------------------------------------------------------------------
    # 信号 (布林带宽度倍数是 hyperopt 参数, 所以上下轨在这里算)
    # ------------------------------------------------------------------

    def _bands(self, dataframe: DataFrame) -> None:
        k = float(self.bb_std.value)
        dataframe["bb_upper"] = dataframe["bb_mid"] + k * dataframe["bb_sd"]
        dataframe["bb_lower"] = dataframe["bb_mid"] - k * dataframe["bb_sd"]

    def _regime_ok(self, dataframe: DataFrame) -> pd.Series:
        """三层 "没有趋势 / 波动正常" 过滤。任何一项是 NaN (数据不够) -> False -> 不开仓。"""
        flat_ltf = dataframe["adx"] < int(self.adx_max.value)
        flat_htf = (dataframe["adx_htf"] < self.htf_adx_max) & (dataframe["slope_htf"].abs() < self.htf_slope_max)
        calm = dataframe["vol_ratio"] <= self.vol_ratio_max
        return flat_ltf & flat_htf & calm

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        self._bands(dataframe)
        c = dataframe["close"]
        lo_rsi = int(self.rsi_extreme.value)
        ok = (dataframe["volume"] > 0) & dataframe["atr"].notna() & self._regime_ok(dataframe)

        long_sig = ok & (c < dataframe["bb_lower"]) & (dataframe["rsi"] < lo_rsi) & bool(self.enable_long)
        short_sig = ok & (c > dataframe["bb_upper"]) & (dataframe["rsi"] > 100 - lo_rsi) & bool(self.enable_short)
        dataframe["enter_long"] = long_sig.astype("int64")
        dataframe["enter_short"] = short_sig.astype("int64")
        tags = pd.Series(None, index=dataframe.index, dtype=object)
        tags[long_sig] = "range_mr_long"
        tags[short_sig] = "range_mr_short"
        dataframe["enter_tag"] = tags
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        # 止盈: 收盘回到中轨 (中轨不依赖 hyperopt 参数)
        c = dataframe["close"]
        mid = dataframe["bb_mid"]
        to_mid_long = c >= mid
        to_mid_short = c <= mid
        dataframe["exit_long"] = to_mid_long.astype("int64")
        dataframe["exit_short"] = to_mid_short.astype("int64")
        tags = pd.Series(None, index=dataframe.index, dtype=object)
        tags[to_mid_long | to_mid_short] = "range_mr_mid"
        dataframe["exit_tag"] = tags
        return dataframe
