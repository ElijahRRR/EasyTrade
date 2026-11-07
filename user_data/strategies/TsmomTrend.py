"""
TsmomTrend -- 时间序列动量 (Time-Series Momentum, TSMOM), 多空双向, 日线。

经典 TSMOM (Moskowitz, Ooi & Pedersen 2012): 过去一段时间涨了就做多、跌了就做空, 每天重新判断。
这里为了少调参、更稳健, 同时看 3 个回看长度 (L, 2L, 4L 天) 并按波动率标准化后取平均:

  z_k   = ln(收盘 / k 天前收盘) / (日波动率 x sqrt(k))      k = L, 2L, 4L
          (日波动率 = 最近 vol_window 天对数收益率的标准差; z 相当于 "这段涨跌是几个标准差")
  score = (z_L + z_2L + z_4L) / 3

  入场  做多: score > entry_z;  做空: score < -entry_z
        波动率过滤: 最近 10 天波动率 / 最近 90 天波动率 > vol_ratio_max (默认 2) 时不开新仓
        (刚出现暴涨暴跌、波动突然放大时, 动量最容易反转 —— "动量崩溃")
  离场  做多: score 跌回 exit_z (默认 0) 以下 = 动量消失;  做空: score 回到 -exit_z 以上
        信号每天收盘重算, 效果接近 "每天调仓": 信号还在就继续拿, 信号没了就平
  止损  初始止损 = 入场价 -/+ stop_atr_mult x ATR20 (只防极端行情; 正常靠信号离场)
  仓位  easytrade_risk: 每笔打到止损亏总资金 1%, 组合热度 <= 6%, 杠杆 <= 2 (硬上限 5)
        仓位天然和波动率成反比 (止损 = 几倍 ATR), 相当于 TSMOM 的 "波动率目标" 定仓

可 hyperopt 的参数 3 个 (只在训练段调参): mom_lookback / entry_z / stop_atr_mult。
说明文档: docs/strategies-step2.md "趋势类 / TsmomTrend"。
"""

from __future__ import annotations

import numpy as np
import pandas as pd
from pandas import DataFrame

from freqtrade.strategy import DecimalParameter, IntParameter, IStrategy

from easytrade_risk import EasyTradeRiskMixin, atr


class TsmomTrend(EasyTradeRiskMixin, IStrategy):
    INTERFACE_VERSION = 3

    timeframe = "1d"
    can_short = True
    # 最长回看 = 4 x mom_lookback 上限 40 = 160 天, 慢波动率 90 天; 再留余量
    startup_candle_count = 200
    process_only_new_candles = True

    minimal_roi = {"0": 100.0}
    trailing_stop = False
    use_custom_stoploss = True
    use_exit_signal = True
    exit_profit_only = False
    ignore_roi_if_entry_signal = False
    stoploss = -0.50  # 兜底 (2 倍杠杆 = 价格反向 25%), 成交后换成 ATR 止损

    # ---------------- 可 hyperopt 的参数 (范围故意窄) ----------------
    mom_lookback = IntParameter(10, 40, default=20, space="buy")
    entry_z = DecimalParameter(0.3, 1.5, default=0.75, decimals=2, space="buy")
    stop_atr_mult = DecimalParameter(2.0, 4.0, default=3.0, decimals=1, space="sell")

    # ---------------- 固定参数 ----------------
    horizon_mults = (1, 2, 4)
    vol_window = 30
    vol_fast = 10
    vol_slow = 90
    vol_ratio_max = 2.0
    exit_z = 0.0
    atr_period = 20
    atr_column = "atr"
    enable_long = True
    enable_short = True
    et_name = "tsmom"

    plot_config = {
        "main_plot": {},
        "subplots": {
            "TSMOM": {"tsmom_score": {"color": "#2f80ed"}},
            "VolRatio": {"vol_ratio": {"color": "#eb5757"}},
        },
    }

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["atr"] = atr(dataframe, self.atr_period)
        logc = np.log(dataframe["close"].astype("float64"))
        dataframe["logc"] = logc
        r = logc.diff()
        dataframe["sigma"] = r.rolling(self.vol_window, min_periods=self.vol_window).std()
        fast = r.rolling(self.vol_fast, min_periods=self.vol_fast).std()
        slow = r.rolling(self.vol_slow, min_periods=self.vol_slow).std()
        dataframe["vol_ratio"] = fast / slow.where(slow > 0)
        return dataframe

    def _score(self, dataframe: DataFrame) -> pd.Series:
        """3 个回看长度的波动率标准化动量取平均 (回看长度是 hyperopt 参数, 所以每次重算)。"""
        L = int(self.mom_lookback.value)
        logc = dataframe["logc"]
        sigma = dataframe["sigma"].where(dataframe["sigma"] > 0)
        zs = []
        for m in self.horizon_mults:
            k = L * int(m)
            zs.append((logc - logc.shift(k)) / (sigma * np.sqrt(k)))
        score = sum(zs) / len(zs)
        dataframe["tsmom_score"] = score
        return score

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        score = self._score(dataframe)
        z = float(self.entry_z.value)
        ok = (dataframe["volume"] > 0) & dataframe["atr"].notna() & score.notna()
        calm = dataframe["vol_ratio"] <= self.vol_ratio_max  # NaN -> False -> 不开仓
        long_sig = ok & calm & (score > z) & bool(self.enable_long)
        short_sig = ok & calm & (score < -z) & bool(self.enable_short)
        dataframe["enter_long"] = long_sig.astype("int64")
        dataframe["enter_short"] = short_sig.astype("int64")
        tags = pd.Series(None, index=dataframe.index, dtype=object)
        tags[long_sig] = f"tsmom_long_L{int(self.mom_lookback.value)}"
        tags[short_sig] = f"tsmom_short_L{int(self.mom_lookback.value)}"
        dataframe["enter_tag"] = tags
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        score = dataframe["tsmom_score"] if "tsmom_score" in dataframe.columns else self._score(dataframe)
        dataframe["exit_long"] = (score < self.exit_z).astype("int64")
        dataframe["exit_short"] = (score > -self.exit_z).astype("int64")
        tags = pd.Series(None, index=dataframe.index, dtype=object)
        tags[score < self.exit_z] = "tsmom_fade"
        tags[score > -self.exit_z] = "tsmom_fade"
        dataframe["exit_tag"] = tags
        return dataframe
