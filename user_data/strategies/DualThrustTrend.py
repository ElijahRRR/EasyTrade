"""
DualThrustTrend -- Dual Thrust 区间突破（第 6 轮初筛 T21 的 freqtrade 版，多空双向，日线）。

来历: 第 6 轮 100 个候选策略初筛 (docs/screen100.md, 正式结果 docs/research-runs/2026-11-14-round6-screen100/)
里 0 个通过; T21 是只栽在"回撤超过 15%"上的 3 个之一 (样本外 / 走步 / 手续费翻倍都过了, 多重检验没过)。
2026-11-14 David 选了"放宽规则再看": 不再要求多重检验 (Deflated Sharpe), 回撤上限仍然 15%,
办法是把仓位降到初筛的约 0.4 倍。规则和门槛在任何 freqtrade 回测之前登记 (docs/round6-retest.md)。
!! 这 3 个是看完 100 个结果以后挑出来的, 2021 ~ 2026 的回测不算独立证据; 独立证据只有 2020 留出段和以后的模拟盘。

信号 (和初筛 scripts/screen100/catalog.py 的 T21 一样, 参数 = 初筛训练段选出来的那组, 固定不调):
  区间  rng = max(前 n 根最高价 - 前 n 根最低收盘价, 前 n 根最高收盘价 - 前 n 根最低价)   (都不含当根, n = 10)
  做多  当根收盘 > 当根开盘 + k x rng (k = 0.5);  做空: 当根收盘 < 当根开盘 - k x rng
  离场  出现反方向的突破 (多单遇到做空信号就平, 下一根如果信号还在再反手)

仓位和止损 (easytrade_risk 共用风控):
  初筛没有止损, 按波动率定仓。这里用很远的"灾难止损" = 入场价 -/+ 5 x ATR20 (最远 25%), 平时靠信号离场;
  每笔打到止损亏总资金 1%, 所以名义仓位 = 1% / (5 x ATR%), 大约是初筛满仓位 (日波动贡献 0.4%) 的 0.4 倍。
  组合热度 <= 6%, 回撤到 8% 以后新仓减半, 杠杆 <= 2 (硬上限 5), 同时最多 8 笔 (configs/research-overrides/DualThrustTrend.json)。

持仓状态用状态机逐根算 (和初筛的 latch 一样): 只在状态"刚变成"多 / 空的那根收盘后入场, 状态不再是多 / 空就离场。
状态机从数据开头算起, 预热 120 根以后和从更早开始算的结果基本一致。
"""

from __future__ import annotations

import numpy as np
import pandas as pd
from pandas import DataFrame

from freqtrade.strategy import IStrategy

from easytrade_risk import EasyTradeRiskMixin, atr


def _latch(enter_long, exit_long, enter_short, exit_short) -> np.ndarray:
    """进场 / 出场条件 -> 持仓状态 +1 / 0 / -1 (和 scripts/screen100/ind.py 的 latch 一样: 出场那一根先回到 0, 下一根再看进场)。"""
    el, xl, es, xs = (np.asarray(x, dtype=bool) for x in (enter_long, exit_long, enter_short, exit_short))
    out = np.zeros(len(el))
    s = 0
    for i in range(len(el)):
        if s == 1 and xl[i]:
            s = 0
        elif s == -1 and xs[i]:
            s = 0
        elif s == 0:
            if el[i]:
                s = 1
            elif es[i]:
                s = -1
        out[i] = s
    return out


class DualThrustTrend(EasyTradeRiskMixin, IStrategy):
    INTERFACE_VERSION = 3

    timeframe = "1d"
    can_short = True
    startup_candle_count = 120
    process_only_new_candles = True

    minimal_roi = {"0": 100.0}
    trailing_stop = False
    use_custom_stoploss = True
    use_exit_signal = True
    exit_profit_only = False
    ignore_roi_if_entry_signal = False
    stoploss = -0.50  # 兜底 (2 倍杠杆 = 价格反向 25%), 成交后换成 5 x ATR 的灾难止损

    # ---------------- 固定参数 (登记过, 不调参) ----------------
    range_period = 10
    k_mult = 0.5
    atr_period = 20
    atr_column = "atr"
    stop_atr_mult = 5.0  # 灾难止损, 不是日常离场
    risk_per_trade = 0.01
    max_portfolio_risk = 0.06
    et_name = "dualthrust"

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["atr"] = atr(dataframe, self.atr_period)
        n, k = int(self.range_period), float(self.k_mult)
        o, h, l, c = (dataframe[x].astype("float64") for x in ("open", "high", "low", "close"))
        hh = h.rolling(n, min_periods=n).max().shift(1)
        lc = c.rolling(n, min_periods=n).min().shift(1)
        hc = c.rolling(n, min_periods=n).max().shift(1)
        ll = l.rolling(n, min_periods=n).min().shift(1)
        rng = np.maximum(hh - lc, hc - ll)
        dataframe["dt_range"] = rng
        up = (c > o + k * rng).fillna(False)
        dn = (c < o - k * rng).fillna(False)
        dataframe["pos_state"] = _latch(up, dn, dn, up)
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        st = dataframe["pos_state"]
        prev = st.shift(1).fillna(0)
        ok = (dataframe["volume"] > 0) & dataframe["atr"].notna()
        long_sig = ok & (st == 1) & (prev != 1)
        short_sig = ok & (st == -1) & (prev != -1)
        dataframe["enter_long"] = long_sig.astype("int64")
        dataframe["enter_short"] = short_sig.astype("int64")
        tags = pd.Series(None, index=dataframe.index, dtype=object)
        tags[long_sig] = "dualthrust_long"
        tags[short_sig] = "dualthrust_short"
        dataframe["enter_tag"] = tags
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        st = dataframe["pos_state"]
        dataframe["exit_long"] = (st != 1).astype("int64")
        dataframe["exit_short"] = (st != -1).astype("int64")
        dataframe["exit_tag"] = "dualthrust_signal"
        return dataframe
