"""
VolumeSpikeShort -- 单根 K 线巨量卖盘 (只做空, 默认 5 分钟)

移植自 Rust: crypto-signal-engine/crates/strategies/src/volume_spike.rs (VolumeSpike::on_closed_bar)
准入闸:     crates/llm_trader/src/edge_filter.rs -> volume_spike 只放行 SHORT
多周期:     crates/engine/src/mtf.rs (Confirmation 族: 只调分, 强逆高周期时仓位减半, 不拦截)
出场:       crates/llm_trader/src/sim.rs -- Rust 信号不带止盈/止损, 回测里 "持有到超时":
            max(48h, 12 x 5m) = 48 小时 (+1 根)。注意是 48 小时, 不是 12 x 5 分钟 = 1 小时。

信号 (逐根已收盘 K 线, 无未来函数):
  1. 至少 21 根 K 线; open/close/volume > 0
  2. 本根成交额 (close x volume) >= 50 x 前 20 根成交额均值 (不含本根, 只算 >0 的根, 至少 10 根)
  3. 前 20 根平均成交额 >= 50,000 USDT (过滤冷门币)
  4. 方向: close < open -> 做空 (卖盘巨量); close > open -> 做多 (默认关闭); 平盘 -> 忽略
  5. 冷却: 同币同方向 5 根 K 线内只算一次
评分: 50x-100x 65 / 100x-300x 80 / >=300x 95, 实体 >=70% 再 +5; >=80 HIGH 否则 MEDIUM

周期: Rust 同时跑 1m / 3m / 5m (6 月体检把三个周期合在一起统计)。这里默认 5m,
      想换周期: 改下面的 timeframe, 或回测时加 --timeframe 1m (所有冷却/超时都跟着周期自动换算)。
"""

from __future__ import annotations

import logging

import numpy as np
import pandas as pd
import talib
from pandas import DataFrame

from freqtrade.exchange import timeframe_to_seconds
from freqtrade.strategy import IStrategy

from rust_port_common import (
    POLICY_CONFIRMATION,
    RustPortMixin,
    body_ratio,
    cooldown_mask,
)


logger = logging.getLogger(__name__)


class VolumeSpikeShort(RustPortMixin, IStrategy):
    INTERFACE_VERSION = 3

    # volume_spike.rs: TF = [M1, M3, M5]
    timeframe = "5m"
    can_short = True

    # 只需要 21 根; 但多周期偏向要在 1h/4h/1d 上算 EMA200, freqtrade 回测会按
    # "startup_candle_count 根 x 各自周期" 预加载信息周期数据, 所以取 300 (= 300 根日线/4h/1h)。
    startup_candle_count = 300
    process_only_new_candles = True

    minimal_roi = {"0": 100.0}  # Rust 无止盈
    trailing_stop = False  # Rust 默认无移动止损
    use_custom_stoploss = True
    use_exit_signal = True  # custom_exit (超时) 需要
    exit_profit_only = False
    ignore_roi_if_entry_signal = False

    # ---------- 风控 ----------
    # Rust 无止损 -> 兜底止损: 价格反向 10% 离场, 也用它算仓位。
    disaster_stop_pct = 0.10
    max_leverage_param = 3.0
    stoploss = -0.30  # 账户口径 = 0.10 x 3

    # 止损模式:
    #   "rust"          -> 与 Rust 回测一致: 只有兜底止损 (默认)
    #   "spike_high"    -> 巨量 K 线最高价上方 spike_stop_buffer (做多时为最低价下方)
    #   "atr"           -> 入场价 + atr_stop_mult x ATR14 (本周期)
    stop_mode = "rust"
    spike_stop_buffer = 0.002
    atr_stop_mult = 6.0

    # ---------- 信号参数 (与 Rust 常量一一对应) ----------
    rust_name = "volume_spike"
    mtf_policy_name = POLICY_CONFIRMATION
    lookback = 20  # LOOKBACK
    min_vol_ratio = 50.0  # MIN_VOL_RATIO
    min_avg_notional = 50_000.0  # MIN_AVG_NOTIONAL (USDT)
    dedup_bars = 5  # DEDUP_BARS

    # 下单类型 (limit/market、是否交易所挂止损) 统一由 configs/config.base.json 的 order_types 决定,
    # 策略里不重复写 (两边都写时 freqtrade 以 config 为准)。

    plot_config = {
        "main_plot": {},
        "subplots": {
            "VolRatio": {"vol_ratio": {"color": "#eb5757"}},
            "Score": {"rust_score_short": {"color": "#27ae60"}},
        },
    }

    def informative_pairs(self):
        return self.mtf_informative_pairs()

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        # 成交额 = close x volume (volume_spike.rs: let qv = b.close * b.volume)。
        # 说明: freqtrade 的 volume 列是 "基础币数量"。Binance U 本位是币的数量;
        # OKX 永续在 ccxt 4.5.x 里取的也是币的数量 (parse_ohlcv volumeIndex=6), 不是张数,
        # 所以两边都直接 close x volume 即可。
        qv = dataframe["close"] * dataframe["volume"]
        dataframe["quote_volume"] = qv
        prev = qv.shift(1)
        # Rust: for i in 1..=20 { if qv > 0 { sum += qv; count += 1 } }; count < 10 -> None
        prev_pos = prev.where(prev > 0)
        dataframe["avg_quote_vol_prev"] = prev_pos.rolling(self.lookback, min_periods=self.lookback // 2).mean()
        dataframe["vol_ratio"] = qv / dataframe["avg_quote_vol_prev"]
        dataframe["body_ratio"] = body_ratio(dataframe)
        dataframe["atr14"] = talib.ATR(
            dataframe["high"].to_numpy(dtype="float64"),
            dataframe["low"].to_numpy(dtype="float64"),
            dataframe["close"].to_numpy(dtype="float64"),
            timeperiod=14,
        )
        dataframe = self.add_mtf_columns(dataframe, metadata)
        return dataframe

    def _direction_signal(self, df: DataFrame, direction: int):
        o, c, v = df["open"], df["close"], df["volume"]
        avg = df["avg_quote_vol_prev"]
        ratio = df["vol_ratio"]
        base = (
            (np.arange(len(df)) >= self.lookback)  # win.len() >= LOOKBACK + 1
            & (o > 0)
            & (c > 0)
            & (v > 0)
            & avg.notna()
            & (avg >= self.min_avg_notional)
            & (ratio >= self.min_vol_ratio)
        )
        if direction < 0:
            event = base & (c < o)
        else:
            event = base & (c > o)
        # volume_spike.rs: dedup 在全部条件通过后才检查 (key 含方向)
        cooldown_secs = self.dedup_bars * timeframe_to_seconds(self.timeframe)
        signal = cooldown_mask(event.to_numpy(), df["date"], cooldown_secs)

        r = ratio.to_numpy(dtype="float64")
        score = np.select([r >= 300.0, r >= 100.0], [95.0, 80.0], default=65.0)
        score = score + np.where((df["body_ratio"] >= 0.7).to_numpy(), 5.0, 0.0)
        score = np.minimum(score, 100.0)
        severity = np.where(score >= 80, 3, 2)  # HIGH / MEDIUM
        return signal, score, severity, r

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["enter_short"] = 0
        dataframe["enter_long"] = 0
        sides = []
        if self.enable_short:
            sides.append(("short", -1))
        if self.enable_long:
            sides.append(("long", 1))
        for side, direction in sides:
            signal, score, severity, ratio = self._direction_signal(dataframe, direction)
            ok, fscore, fsev, mult, align = self.apply_mtf_and_severity(
                dataframe, signal, score, severity, direction
            )
            bucket = np.select([ratio >= 300.0, ratio >= 100.0], ["r300", "r100"], default="r50")
            self.write_entry_columns(dataframe, side, ok, fscore, fsev, mult, align, bucket)
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["exit_short"] = 0
        dataframe["exit_long"] = 0
        return dataframe

    def _raw_stop_price(self, row: pd.Series, entry_rate: float, is_short: bool) -> float | None:
        mode = str(self.stop_mode).lower()
        if mode == "rust":
            return None  # volume_spike.rs: invalidation_price = None
        if mode == "spike_high":
            if is_short:
                return float(row["high"]) * (1 + self.spike_stop_buffer)
            return float(row["low"]) * (1 - self.spike_stop_buffer)
        if mode == "atr":
            atr = float(row.get("atr14", np.nan))
            if not np.isfinite(atr) or atr <= 0:
                return None
            off = self.atr_stop_mult * atr
            return entry_rate + off if is_short else entry_rate - off
        logger.warning("VolumeSpikeShort: 未知 stop_mode=%s, 只用兜底止损", self.stop_mode)
        return None
