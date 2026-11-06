"""
DailyThreeSoldiersShort -- 日线三连阴 (Three Black Crows), 只做空

移植自 Rust: crypto-signal-engine/crates/strategies/src/daily_three_soldiers.rs
准入闸:     crates/llm_trader/src/edge_filter.rs -> daily_three_soldiers 只放行 SHORT
多周期:     crates/engine/src/mtf.rs (TrendBreakout 族: 强逆高周期拦截, 逆高周期降仓)
出场:       crates/llm_trader/src/sim.rs + crates/backtest/src/bin/signal_backtest.rs
            止损 = 信号自带的 invalidation_price = 第 1 根 (b1) 的最低价; 无止盈;
            超时 = max(48h, 12 x 1d) = 12 天 (+1 根, 实际 13 天)。
            progress.md: 此策略 "需 ~12 根 1d 持有期", 正是 sim.rs 把超时从 5x 改为 12x 的原因。

信号 (最近 3 根已收盘日线 b1, b2, b3=最新, 无未来函数):
  1. 三根全部 close < open (三连阴)
  2. 阶梯式: b2.close < b1.close 且 b3.close < b2.close
  3. 流动性: b3 成交额 (close x volume) >= 5,000,000 USDT
  (2026-05 已取消实体占比下限 —— 带长影线也算; Rust 的 1 根冷却在日线上等于没有冷却)
评分: 70 + 3 天累计跌幅 >=10% 加 20 (>=5% 加 10) + 成交量逐日递增 10; >=80 HIGH 否则 MEDIUM

!!! Rust 原版的一个怪处 (照搬, 并提供替代):
  Rust 对三连阴 (做空) 也把止损设在 b1.low (第一根阴线的最低价)。做空的止损应在入场价上方,
  b1.low 通常高于入场价 (价格连跌后), 此时它是一个比较紧的止损 (大约在第一根阴线收盘附近);
  但如果 b1 下影线很长, b1.low 会低于入场价 —— Rust sim 会在入场那根立即按开盘价止损 (只亏手续费)。
  stop_mode="rust" 照搬这一行为 (无效时直接不开仓); stop_mode="b1_high" 用镜像逻辑 b1.high。
"""

from __future__ import annotations

import logging

import numpy as np
import pandas as pd
import talib
from pandas import DataFrame

from freqtrade.strategy import IStrategy

from rust_port_common import POLICY_TREND_BREAKOUT, RustPortMixin


logger = logging.getLogger(__name__)


class DailyThreeSoldiersShort(RustPortMixin, IStrategy):
    INTERFACE_VERSION = 3

    # daily_three_soldiers.rs: TF = [D1]
    timeframe = "1d"
    can_short = True

    # 形态本身只要 3 根。这里取 30 (ATR14 + EMA20 回退判定够用), 尽量不排除新上线的币
    # (Rust 对新币只要有 3 根日线就会发信号)。
    # 代价: 回测开头约 200 天内, 日线 EMA200 还没算出来, MTF 的 D1 偏向退化成 EMA20 判定
    # (与 Rust 对新币的处理一致)。如果更在意 MTF 精度, 在 config 里加 "startup_candle_count": 300。
    startup_candle_count = 30
    process_only_new_candles = True

    minimal_roi = {"0": 100.0}  # Rust: take_profit_hints = [] -> 无止盈
    trailing_stop = False
    use_custom_stoploss = True
    use_exit_signal = True  # custom_exit (超时) 需要
    exit_profit_only = False
    ignore_roi_if_entry_signal = False

    # ---------- 风控 ----------
    disaster_stop_pct = 0.20  # 兜底: b1 止损失效或过远时, 最多反向 20%
    max_leverage_param = 3.0
    stoploss = -0.60  # 账户口径 = 0.20 x 3

    # 止损模式:
    #   "rust"    -> b1.low (与 Rust 完全一致, 包括做空时的怪处, 默认)
    #   "b1_high" -> 做空用 b1.high (形态最高点, 逻辑上的镜像失效位); 做多仍用 b1.low
    #   "atr"     -> 入场价 + atr_stop_mult x ATR14 (日线)
    stop_mode = "rust"
    atr_stop_mult = 2.0

    # ---------- 信号参数 (与 Rust 常量一一对应) ----------
    rust_name = "daily_three_soldiers"
    mtf_policy_name = POLICY_TREND_BREAKOUT
    min_notional = 5_000_000.0  # MIN_NOTIONAL (USDT)

    # 下单类型 (limit/market、是否交易所挂止损) 统一由 configs/config.base.json 的 order_types 决定,
    # 策略里不重复写 (两边都写时 freqtrade 以 config 为准)。

    plot_config = {
        "main_plot": {},
        "subplots": {"Score": {"rust_score_short": {"color": "#27ae60"}}},
    }

    def informative_pairs(self):
        return self.mtf_informative_pairs()

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        # b1 = 倒数第 3 根, b2 = 倒数第 2 根, b3 = 当前 (最新已收盘) 根
        for k, lag in (("b1", 2), ("b2", 1)):
            for col in ("open", "high", "low", "close", "volume"):
                dataframe[f"{k}_{col}"] = dataframe[col].shift(lag)
        dataframe["cum_pct"] = (dataframe["close"] - dataframe["b1_open"]) / dataframe["b1_open"] * 100.0
        # 成交额 (USDT) = close x volume; OKX 永续的 volume 是币的数量 (不是张数), 见 docs/okx-volume-units.md
        dataframe["notional_b3"] = self.quote_volume(dataframe, metadata["pair"])
        dataframe["atr14"] = talib.ATR(
            dataframe["high"].to_numpy(dtype="float64"),
            dataframe["low"].to_numpy(dtype="float64"),
            dataframe["close"].to_numpy(dtype="float64"),
            timeperiod=14,
        )
        dataframe = self.add_mtf_columns(dataframe, metadata)
        return dataframe

    def _direction_signal(self, df: DataFrame, direction: int):
        o1, c1, v1 = df["b1_open"], df["b1_close"], df["b1_volume"]
        o2, c2, v2 = df["b2_open"], df["b2_close"], df["b2_volume"]
        o3, c3, v3 = df["open"], df["close"], df["volume"]

        # daily_three_soldiers.rs: win.len() >= 3; 三根 open > 0
        # (Rust 的 "相邻性" 检查在这里自动满足: freqtrade 会把缺失的 K 线补成 open=close 的平盘,
        #  平盘不是阴线, 形态自然不成立)
        valid = (np.arange(len(df)) >= 2) & (o1 > 0) & (o2 > 0) & (o3 > 0)
        if direction < 0:
            pattern = (c1 < o1) & (c2 < o2) & (c3 < o3)  # three_black
            stepwise = (c2 < c1) & (c3 < c2)
        else:
            pattern = (c1 > o1) & (c2 > o2) & (c3 > o3)  # three_white
            stepwise = (c2 > c1) & (c3 > c2)
        liquid = df["notional_b3"] >= self.min_notional
        # DEDUP_BARS = 1: 日线每根都是新的一天, 冷却不起作用 (连续 4 根阴线会在第 3、4 根各发一次)
        signal = (valid & pattern & stepwise & liquid).to_numpy()

        abs_cum = df["cum_pct"].abs().to_numpy(dtype="float64")
        score = 70.0 + np.select([abs_cum >= 10.0, abs_cum >= 5.0], [20.0, 10.0], default=0.0)
        vol_inc = ((v3 > v2) & (v2 > v1)).to_numpy()
        score = np.minimum(score + np.where(vol_inc, 10.0, 0.0), 100.0)
        severity = np.where(score >= 80, 3, 2)
        return signal, score, severity

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["enter_short"] = 0
        dataframe["enter_long"] = 0
        sides = []
        if self.enable_short:
            sides.append(("short", -1))
        if self.enable_long:
            sides.append(("long", 1))
        for side, direction in sides:
            signal, score, severity = self._direction_signal(dataframe, direction)
            ok, fscore, fsev, mult, align = self.apply_mtf_and_severity(
                dataframe, signal, score, severity, direction
            )
            self.write_entry_columns(dataframe, side, ok, fscore, fsev, mult, align)
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["exit_short"] = 0
        dataframe["exit_long"] = 0
        return dataframe

    def _raw_stop_price(self, row: pd.Series, entry_rate: float, is_short: bool) -> float | None:
        mode = str(self.stop_mode).lower()
        b1_low = float(row.get("b1_low", np.nan))
        b1_high = float(row.get("b1_high", np.nan))
        if mode == "rust":
            # daily_three_soldiers.rs: invalidation_price: Some(b1.low) —— 多空都是 b1.low
            return b1_low if np.isfinite(b1_low) else None
        if mode == "b1_high":
            v = b1_high if is_short else b1_low
            return v if np.isfinite(v) else None
        if mode == "atr":
            atr = float(row.get("atr14", np.nan))
            if not np.isfinite(atr) or atr <= 0:
                return None
            off = self.atr_stop_mult * atr
            return entry_rate + off if is_short else entry_rate - off
        logger.warning("DailyThreeSoldiersShort: 未知 stop_mode=%s, 只用兜底止损", self.stop_mode)
        return None
