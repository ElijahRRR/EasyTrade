"""
BigTrendShort -- 大周期多 EMA 跌破 (只做空, 日线)

移植自 Rust: crypto-signal-engine/crates/strategies/src/big_trend.rs (BigTrend::on_closed_bar)
准入闸:     crates/llm_trader/src/edge_filter.rs -> big_trend 只放行 "1d x SHORT"
多周期:     crates/engine/src/mtf.rs (TrendBreakout 族: 强逆高周期拦截, 逆高周期降仓)
出场:       crates/llm_trader/src/sim.rs + crates/backtest/src/bin/signal_backtest.rs
            Rust 信号不带止盈/止损 (invalidation_price=None, take_profit_hints=[]),
            所以 Rust 回测里是 "持有到超时": max(48h, 12x1d) = 12 天 (+1 根, 实际 13 天)。

信号 (逐根已收盘 K 线, 无未来函数):
  1. 至少 220 根 K 线 (EMA200 预热完成), EMA7/20/60/100/200 都有值
  2. 单根 K 线 open > EMA 且 close < EMA, 对至少 3 条 EMA 成立 (= 一根阴线跌穿 >=3 条均线)
  3. 冷却: 同币同方向 6 根 K 线内只算一次 (注意: Rust 在 MACD 确认之前就占用了冷却)
  4. MACD 确认: MACD < 信号线 且 柱体 < 0 且 柱体继续向下扩张 (macd_confirm.rs)
评分 (只用于严重度标签, 默认不过滤):
  基础 70/80/90 (穿 3/4/5 条) + MACD 6 (新死叉 10) + 收盘在 EMA200 下方 5
  + 放量 >=2x 前 20 根均量 5 + 实体占振幅 >=60% 5, 上限 100; >=80 HIGH, >=65 MEDIUM
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
    POLICY_TREND_BREAKOUT,
    RustPortMixin,
    body_ratio,
    cooldown_mask,
    ema,
    macd_12_26_9,
)


logger = logging.getLogger(__name__)


class BigTrendShort(RustPortMixin, IStrategy):
    INTERFACE_VERSION = 3

    # big_trend.rs: TF = [H4, D1]; edge_filter.rs 只保留 1d -> 默认 1d。
    # 回测时可以用 --timeframe 4h 重测 4h (需同时把 enforce_edge_filter 改成 False)。
    timeframe = "1d"
    can_short = True

    # Rust 需要 >= 220 根 (EMA200 预热); 这里取 300, 与 Rust 实盘 warmup 拉取的 300 根一致
    # (crates/ingestor/src/warmup.rs target_bars 默认 300), 也让 EMA200 更稳定。
    # 300 根时 EMA200 的起点误差还剩约 37% ((1-2/201)^100), 从中间某天开始的回测开头一段信号可能和连续回测不同;
    # scripts/research.sh 给样本外 / 走步各段最多 600 根预热 (README "用合成数据自检" 有解释)。
    startup_candle_count = 300
    process_only_new_candles = True

    # Rust 信号无止盈 -> 关闭 ROI; 无移动止损 (progress.md: trailing/保本 全部负优化, 默认关闭)
    minimal_roi = {"0": 100.0}
    trailing_stop = False
    use_custom_stoploss = True
    # custom_exit (超时出场) 只有在 use_exit_signal=True 时才会被 freqtrade 调用
    use_exit_signal = True
    exit_profit_only = False
    ignore_roi_if_entry_signal = False

    # ---------- 风控 ----------
    # Rust 没有止损; freqtrade 必须有一个, 这里是 "兜底止损": 价格反向 20% 离场, 也用它算仓位。
    disaster_stop_pct = 0.20
    max_leverage_param = 3.0
    # freqtrade 期货的 stoploss 是账户口径 (= 价格距离 x 杠杆): 0.20 x 3 = 0.60
    stoploss = -0.60

    # 止损模式:
    #   "rust"         -> 与 Rust 回测一致: 无结构止损, 只有兜底止损 (默认)
    #   "atr"          -> 入场价 + atr_stop_mult x ATR14 (日线)
    #   "signal_high"  -> 信号 K 线最高价上方 signal_stop_buffer
    stop_mode = "rust"
    atr_stop_mult = 3.0
    signal_stop_buffer = 0.002

    # ---------- 信号参数 (与 Rust 常量一一对应) ----------
    rust_name = "big_trend"
    mtf_policy_name = POLICY_TREND_BREAKOUT
    min_bars = 220  # big_trend.rs: ctx.window.len() < 220 -> None
    min_emas_crossed = 3  # MIN_EMAS_CROSSED
    dedup_bars = 6  # DEDUP_BARS
    ema_periods = (7, 20, 60, 100, 200)
    # edge_filter.rs: big_trend 只在 1d 有 edge。True 时非 1d 周期不产生入场。
    enforce_edge_filter = True
    edge_timeframes = ("1d",)

    # 下单类型 (limit/market、是否交易所挂止损) 统一由 configs/config.base.json 的 order_types 决定,
    # 策略里不重复写 (两边都写时 freqtrade 以 config 为准)。

    plot_config = {
        "main_plot": {
            "ema7": {"color": "#f2c94c"},
            "ema20": {"color": "#f2994a"},
            "ema60": {"color": "#56ccf2"},
            "ema100": {"color": "#2f80ed"},
            "ema200": {"color": "#9b51e0"},
        },
        "subplots": {
            "MACD": {"macd": {"color": "#2f80ed"}, "macd_signal": {"color": "#eb5757"}},
            "Score": {"rust_score_short": {"color": "#27ae60"}},
        },
    }

    def informative_pairs(self):
        # 多周期需要 4h / 1h 数据 (1d 就是本周期)
        return self.mtf_informative_pairs()

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        close = dataframe["close"]
        for p in self.ema_periods:
            # Rust: crates/engine/src/window.rs 对每根已收盘 K 线流式更新 EMA7/20/60/100/200
            dataframe[f"ema{p}"] = ema(close, p)

        macd, signal, hist = macd_12_26_9(close)
        dataframe["macd"] = macd
        dataframe["macd_signal"] = signal
        dataframe["macd_hist"] = hist
        # Rust: window.rs push_closed 把上一根的 MACD 存为 prev_macd
        dataframe["macd_prev"] = dataframe["macd"].shift(1)
        dataframe["macd_signal_prev"] = dataframe["macd_signal"].shift(1)
        dataframe["macd_hist_prev"] = dataframe["macd_hist"].shift(1)

        # Rust: context.rs::volume_sma_excluding_current(20) -- 前 20 根 (不含当前) 的均量
        dataframe["vol_sma20_prev"] = dataframe["volume"].shift(1).rolling(20, min_periods=20).mean()
        dataframe["body_ratio"] = body_ratio(dataframe)

        # 只给可选止损模式 "atr" 用 (Rust 没有)
        dataframe["atr14"] = talib.ATR(
            dataframe["high"].to_numpy(dtype="float64"),
            dataframe["low"].to_numpy(dtype="float64"),
            close.to_numpy(dtype="float64"),
            timeperiod=14,
        )

        dataframe = self.add_mtf_columns(dataframe, metadata)
        return dataframe

    # ------------------------------------------------------------------
    # 信号: 按方向分别计算 (direction=-1 做空, +1 做多)
    # ------------------------------------------------------------------

    def _direction_signal(self, df: DataFrame, direction: int):
        o = df["open"]
        c = df["close"]
        emas = [df[f"ema{p}"] for p in self.ema_periods]

        # big_trend.rs: len >= 220, open > 0, 所有 EMA ready
        ready = (np.arange(len(df)) >= self.min_bars - 1) & (o > 0)
        for e in emas:
            ready &= e.notna()

        crossed_long = sum(((o < e) & (c > e)).astype(int) for e in emas)
        crossed_short = sum(((o > e) & (c < e)).astype(int) for e in emas)
        # Rust 先判断多头穿越, 再判断空头穿越 (同一根 K 线不会同时成立)
        long_event = ready & (crossed_long >= self.min_emas_crossed)
        short_event = ready & ~long_event & (crossed_short >= self.min_emas_crossed)
        event = short_event if direction < 0 else long_event
        crossed = crossed_short if direction < 0 else crossed_long

        # big_trend.rs: dedup.allow(...) 在 macd_confirm::confirm(...)? 之前调用,
        # 所以 "有穿越但 MACD 不确认" 的 K 线也会占用 6 根 K 线的冷却。这里照搬这一顺序。
        cooldown_secs = self.dedup_bars * timeframe_to_seconds(self.timeframe)
        allowed = cooldown_mask(event.to_numpy(), df["date"], cooldown_secs)

        # macd_confirm.rs::confirm
        m, s, h = df["macd"], df["macd_signal"], df["macd_hist"]
        mp, sp, hp = df["macd_prev"], df["macd_signal_prev"], df["macd_hist_prev"]
        has_macd = m.notna() & s.notna() & mp.notna() & sp.notna()
        if direction < 0:
            macd_ok = has_macd & (m < s) & (h < 0) & ((h - hp) < -1e-12)
            fresh = (mp >= sp) & (m < s)
        else:
            macd_ok = has_macd & (m > s) & (h > 0) & ((h - hp) > 1e-12)
            fresh = (mp <= sp) & (m > s)
        signal = allowed & macd_ok.to_numpy()

        # ---- 评分 (big_trend.rs "组装 facts + score") ----
        n_crossed = crossed.to_numpy()
        base = np.select([n_crossed == 3, n_crossed == 4], [70.0, 80.0], default=90.0)
        score = base + np.where(fresh.to_numpy(), 10.0, 6.0)
        e200 = df["ema200"]
        # Rust: if close > ema200 {多头+5} else {空头+5}
        if direction < 0:
            score = score + np.where((c <= e200).to_numpy(), 5.0, 0.0)
        else:
            score = score + np.where((c > e200).to_numpy(), 5.0, 0.0)
        vs = df["vol_sma20_prev"]
        vol_ok = (vs > 0) & (df["volume"] > 0) & ((df["volume"] / vs) >= 2.0)
        score = score + np.where(vol_ok.to_numpy(), 5.0, 0.0)
        score = score + np.where((df["body_ratio"] >= 0.6).to_numpy(), 5.0, 0.0)
        score = np.minimum(score, 100.0)
        severity = np.select([score >= 80, score >= 65], [3, 2], default=1)

        return signal, score, severity, n_crossed

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["enter_short"] = 0
        dataframe["enter_long"] = 0
        edge_ok = (not self.enforce_edge_filter) or (self.timeframe in self.edge_timeframes)
        if not edge_ok:
            logger.warning(
                "BigTrendShort: %s 不在 edge_filter 允许的周期 %s 内, 不产生入场 "
                "(要重测请把 enforce_edge_filter 设为 False)",
                self.timeframe,
                self.edge_timeframes,
            )
            return dataframe

        sides = []
        if self.enable_short:
            sides.append(("short", -1))
        if self.enable_long:
            sides.append(("long", 1))
        for side, direction in sides:
            signal, score, severity, n_crossed = self._direction_signal(dataframe, direction)
            ok, fscore, fsev, mult, align = self.apply_mtf_and_severity(
                dataframe, signal, score, severity, direction
            )
            tag_extra = np.array([f"x{int(n)}" for n in n_crossed])
            self.write_entry_columns(dataframe, side, ok, fscore, fsev, mult, align, tag_extra)
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        # Rust 没有离场信号; 出场只靠 止损 / 超时 (custom_exit)
        dataframe["exit_short"] = 0
        dataframe["exit_long"] = 0
        return dataframe

    # ------------------------------------------------------------------
    # 止损价 (被 RustPortMixin.resolve_stop_price 调用)
    # ------------------------------------------------------------------

    def _raw_stop_price(self, row: pd.Series, entry_rate: float, is_short: bool) -> float | None:
        mode = str(self.stop_mode).lower()
        if mode == "rust":
            return None  # big_trend.rs: invalidation_price = None
        if mode == "atr":
            atr = float(row.get("atr14", np.nan))
            if not np.isfinite(atr) or atr <= 0:
                return None
            off = self.atr_stop_mult * atr
            return entry_rate + off if is_short else entry_rate - off
        if mode == "signal_high":
            if is_short:
                return float(row["high"]) * (1 + self.signal_stop_buffer)
            return float(row["low"]) * (1 - self.signal_stop_buffer)
        logger.warning("BigTrendShort: 未知 stop_mode=%s, 只用兜底止损", self.stop_mode)
        return None
