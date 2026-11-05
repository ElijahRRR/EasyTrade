"""
SampleFuturesSanity -- 只用来 "冒烟测试" 整条流程 (数据下载 -> 回测 -> 模拟盘 -> 通知) 的最简策略。

!!! 不是赚钱策略, 不要拿它实盘 !!!

逻辑: 1 小时线 EMA20 上穿 EMA50 开多 / 下穿开空, 反向交叉平仓; 固定止损; 杠杆最多 2 倍。
多空都会交易, 所以能同时验证: 做多、做空、杠杆回调、止损、离场信号、资金费计算。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import talib.abstract as ta
from pandas import DataFrame

from technical import qtpylib
from freqtrade.strategy import IStrategy


class SampleFuturesSanity(IStrategy):
    INTERFACE_VERSION = 3

    timeframe = "1h"
    can_short = True
    startup_candle_count = 100
    process_only_new_candles = True

    minimal_roi = {"0": 100.0}  # 不用 ROI, 只靠反向交叉 / 止损离场
    stoploss = -0.10  # 账户口径: 2 倍杠杆时 = 价格反向 5%
    trailing_stop = False
    use_exit_signal = True

    def leverage(
        self,
        pair: str,
        current_time: datetime,
        current_rate: float,
        proposed_leverage: float,
        max_leverage: float,
        entry_tag: str | None,
        side: str,
        **kwargs: Any,
    ) -> float:
        # 冒烟测试用低杠杆; 无论如何不超过 5 倍
        return max(1.0, min(2.0, max_leverage, 5.0))

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["ema_fast"] = ta.EMA(dataframe, timeperiod=20)
        dataframe["ema_slow"] = ta.EMA(dataframe, timeperiod=50)
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        up = qtpylib.crossed_above(dataframe["ema_fast"], dataframe["ema_slow"])
        down = qtpylib.crossed_below(dataframe["ema_fast"], dataframe["ema_slow"])
        has_volume = dataframe["volume"] > 0
        dataframe.loc[up & has_volume, ["enter_long", "enter_tag"]] = (1, "ema_cross_up")
        dataframe.loc[down & has_volume, ["enter_short", "enter_tag"]] = (1, "ema_cross_down")
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        up = qtpylib.crossed_above(dataframe["ema_fast"], dataframe["ema_slow"])
        down = qtpylib.crossed_below(dataframe["ema_fast"], dataframe["ema_slow"])
        dataframe.loc[down, "exit_long"] = 1
        dataframe.loc[up, "exit_short"] = 1
        return dataframe
