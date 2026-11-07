"""
BigTrendBoth -- BigTrendShort 的多空双向版 + 大盘状态过滤 (日线)。

信号和 BigTrendShort 完全对称 (Rust crates/strategies/src/big_trend.rs 本来就多空对称,
BigTrendShort 里的做多逻辑也已移植, 只是默认关闭):
  做空: 一根 K 线 open > EMA 且 close < EMA, 对 EMA7/20/60/100/200 里至少 3 条成立 (阴线跌穿 >=3 条均线)
        + MACD 确认 (MACD < 信号线, 柱体 < 0 且继续向下扩张)
  做多: 镜像 —— 阳线涨穿 >=3 条均线 + MACD 确认向上
  冷却: 同币同方向 6 根 K 线内只算一次 (和 Rust 一样, 冷却在 MACD 确认之前占用)
  多周期: Rust mtf.rs 的 "趋势突破族" 规则 (日线/4h/1h 强烈逆势不开, 偏逆势仓位减半), 同 BigTrendShort

新增 —— 大盘状态过滤 (regime):
  只在 "多头市场" 做多: BTC 日线收盘 > BTC EMA200, 且这个币自己的收盘 > 它的 EMA200
  只在 "空头市场" 做空: BTC 日线收盘 < BTC EMA200, 且这个币自己的收盘 < 它的 EMA200
  原因: 2026-06 体检和 2026-11 OKX 回测都说明, BigTrendShort 的 "做空优势" 很大一部分是熊市 beta。

出场 / 风控 (easytrade_risk, 和 BigTrendShort 不同):
  止损  入场价 -/+ stop_atr_mult x ATR14 (= BigTrendShort 的可选 stop_mode "atr", 默认 3 倍)
  超时  持有满 max_hold_candles 根 K 线 (默认 13 = Rust 的 12 天 timeout + 1 根) 平仓
  仓位  每笔打到止损亏总资金 1%, 组合热度 <= 6%, 杠杆 <= 2 (硬上限 5)

可 hyperopt 的参数 2 个 (只在训练段调参): stop_atr_mult / max_hold_candles。
说明文档: docs/strategies-step2.md "趋势类 / BigTrendBoth"。
"""

from __future__ import annotations

import logging

import numpy as np
import pandas as pd
from pandas import DataFrame

from freqtrade.exchange import timeframe_to_seconds
from freqtrade.strategy import DecimalParameter, IntParameter, IStrategy

from easytrade_risk import EasyTradeRiskMixin, atr
from rust_port_common import (
    MTF_FRAMES,
    POLICY_TREND_BREAKOUT,
    classify_bias,
    cooldown_mask,
    ema,
    macd_12_26_9,
    merge_by_close_time,
    mtf_alignment,
    mtf_policy,
)


logger = logging.getLogger(__name__)


class BigTrendBoth(EasyTradeRiskMixin, IStrategy):
    INTERFACE_VERSION = 3

    timeframe = "1d"
    can_short = True
    # 与 BigTrendShort 相同: Rust 需要 >= 220 根 (EMA200 预热), 取 300。
    # 注意 300 根还不够 EMA200 "忘掉" 起点: talib 的 EMA 从前 200 根的简单平均起步, 之后只递推 100 次,
    # 起点误差还剩约 37% ((1-2/201)^100); 交叉计数、本币/BTC 的 EMA200 过滤、1h/4h 多周期偏向 (各自周期也只有 300 根)
    # 都会受影响, 从中间某天开始的回测开头一段信号会和连续回测不同 (600 根时约 2%)。
    # 不把这里改成 600: 数据从 2024-01 开始时, 日线训练段会再少 300 天。scripts/research.sh 给样本外 / 走步各段
    # 最多 600 根预热 (RESEARCH_WARMUP_CANDLES); OKX 实盘 freqtrade 也会拉取并保留约 600 根。
    startup_candle_count = 300
    process_only_new_candles = True

    minimal_roi = {"0": 100.0}
    trailing_stop = False
    use_custom_stoploss = True
    use_exit_signal = True  # 超时出场 (custom_exit) 需要它
    exit_profit_only = False
    ignore_roi_if_entry_signal = False
    stoploss = -0.50  # 兜底 (2 倍杠杆 = 价格反向 25%), 成交后换成 ATR 止损

    # ---------------- 可 hyperopt 的参数 (都只影响出场, 不碰 Rust 的入场规则) ----------------
    stop_atr_mult = DecimalParameter(2.0, 5.0, default=3.0, decimals=1, space="sell")
    max_hold_candles = IntParameter(8, 20, default=13, space="sell")

    # ---------------- 信号参数 (与 Rust / BigTrendShort 相同, 不调) ----------------
    min_bars = 220
    min_emas_crossed = 3
    dedup_bars = 6
    ema_periods = (7, 20, 60, 100, 200)
    use_mtf_policy = True

    # ---------------- 大盘状态过滤 ----------------
    regime_ema_period = 200  # BTC 日线 EMA
    require_pair_regime = True  # 同时要求这个币自己在 EMA200 同一侧
    enable_long = True
    enable_short = True

    atr_column = "atr14"
    et_name = "bigtrend_both"

    plot_config = {
        "main_plot": {
            "ema20": {"color": "#f2994a"},
            "ema60": {"color": "#56ccf2"},
            "ema200": {"color": "#9b51e0"},
        },
        "subplots": {
            "MACD": {"macd": {"color": "#2f80ed"}, "macd_signal": {"color": "#eb5757"}},
            "Regime": {"btc_regime": {"color": "#27ae60"}},
        },
    }

    def informative_pairs(self):
        if self.dp is None:
            return []
        pairs = [(self.btc_pair, self.btc_timeframe)]
        if self.use_mtf_policy:
            wl = self.dp.current_whitelist()
            pairs += [(p, tf) for p in wl for tf, _ in MTF_FRAMES if tf != self.timeframe]
        return pairs

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        close = dataframe["close"]
        for p in self.ema_periods:
            dataframe[f"ema{p}"] = ema(close, p)
        macd, signal, hist = macd_12_26_9(close)
        dataframe["macd"] = macd
        dataframe["macd_signal"] = signal
        dataframe["macd_hist"] = hist
        dataframe["macd_prev"] = dataframe["macd"].shift(1)
        dataframe["macd_signal_prev"] = dataframe["macd_signal"].shift(1)
        dataframe["macd_hist_prev"] = dataframe["macd_hist"].shift(1)
        dataframe["atr14"] = atr(dataframe, 14)
        dataframe = self._add_mtf_columns(dataframe, metadata)
        dataframe = self.et_add_btc_regime(dataframe, self.regime_ema_period)
        return dataframe

    def _add_mtf_columns(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        """同 rust_port_common.RustPortMixin.add_mtf_columns: 每根 K 线写上 1d/4h/1h 偏向 (-2..+2)。"""
        for tf, _w in MTF_FRAMES:
            col = f"mtf_bias_{tf}"
            if not self.use_mtf_policy:
                dataframe[col] = np.nan
                continue
            if tf == self.timeframe:
                dataframe[col] = classify_bias(dataframe)
                continue
            inf = None
            if self.dp is not None:
                try:
                    inf = self.dp.get_pair_dataframe(pair=metadata["pair"], timeframe=tf)
                except Exception as exc:
                    logger.warning("BigTrendBoth MTF %s %s 数据读取失败: %s", metadata["pair"], tf, exc)
            if inf is None or inf.empty:
                dataframe[col] = np.nan
                continue
            dataframe[col] = merge_by_close_time(dataframe, self.timeframe, inf, tf, classify_bias(inf))
        return dataframe

    def _direction_signal(self, df: DataFrame, direction: int) -> tuple[np.ndarray, np.ndarray]:
        """与 BigTrendShort._direction_signal 相同的入场判断 (不含只用于标签的评分)。"""
        o = df["open"]
        c = df["close"]
        emas = [df[f"ema{p}"] for p in self.ema_periods]
        ready = (np.arange(len(df)) >= self.min_bars - 1) & (o > 0)
        for e in emas:
            ready &= e.notna()
        crossed_long = sum(((o < e) & (c > e)).astype(int) for e in emas)
        crossed_short = sum(((o > e) & (c < e)).astype(int) for e in emas)
        long_event = ready & (crossed_long >= self.min_emas_crossed)
        short_event = ready & ~long_event & (crossed_short >= self.min_emas_crossed)
        event = short_event if direction < 0 else long_event
        crossed = crossed_short if direction < 0 else crossed_long

        cooldown_secs = self.dedup_bars * timeframe_to_seconds(self.timeframe)
        allowed = cooldown_mask(event.to_numpy(), df["date"], cooldown_secs)

        m, s, h = df["macd"], df["macd_signal"], df["macd_hist"]
        mp, sp, hp = df["macd_prev"], df["macd_signal_prev"], df["macd_hist_prev"]
        has_macd = m.notna() & s.notna() & mp.notna() & sp.notna()
        if direction < 0:
            macd_ok = has_macd & (m < s) & (h < 0) & ((h - hp) < -1e-12)
        else:
            macd_ok = has_macd & (m > s) & (h > 0) & ((h - hp) > 1e-12)
        return allowed & macd_ok.to_numpy(), crossed.to_numpy()

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["enter_long"] = 0
        dataframe["enter_short"] = 0
        tags = np.full(len(dataframe), None, dtype=object)
        btc = dataframe["btc_regime"].to_numpy(dtype="float64")
        above = (dataframe["close"] > dataframe["ema200"]).to_numpy()
        below = (dataframe["close"] < dataframe["ema200"]).to_numpy()
        cols = [(dataframe[f"mtf_bias_{tf}"].to_numpy(dtype="float64"), w) for tf, w in MTF_FRAMES]

        sides = []
        if self.enable_long:
            sides.append(("long", 1))
        if self.enable_short:
            sides.append(("short", -1))
        for side, direction in sides:
            signal, n_crossed = self._direction_signal(dataframe, direction)
            if self.use_mtf_policy:
                align = mtf_alignment(cols, direction)
            else:
                align = np.full(len(dataframe), np.nan)
            _delta, mult, block = mtf_policy(align, POLICY_TREND_BREAKOUT)
            if direction > 0:
                regime_ok = btc > 0
                if self.require_pair_regime:
                    regime_ok &= above
            else:
                regime_ok = btc < 0
                if self.require_pair_regime:
                    regime_ok &= below
            ok = signal & ~block & regime_ok & (dataframe["volume"] > 0).to_numpy()
            dataframe[f"enter_{side}"] = ok.astype("int64")
            dataframe[f"et_size_mult_{side}"] = mult
            dataframe[f"mtf_alignment_{side}"] = align
            for i in np.flatnonzero(ok):
                tags[i] = f"bigtrend_{side}_x{int(n_crossed[i])}"
        dataframe["enter_tag"] = pd.Series(tags, index=dataframe.index, dtype=object)
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        # 与 Rust 一样没有离场信号; 出场只靠 止损 / 超时 (custom_exit)
        dataframe["exit_long"] = 0
        dataframe["exit_short"] = 0
        return dataframe
