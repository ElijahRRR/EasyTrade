"""
FundingContrarian -- 资金费率极端时的反向交易 ("拥挤方向" + "动能停滞"), 多空双向, 默认 1h。

思路: 永续合约的资金费率 = 多空哪边更拥挤。费率特别高 = 大家都在借钱做多, 一旦涨不动,
      多头止损 / 爆仓会把价格往下砸; 费率特别低 (负) 反之。这时反向开仓, 还能 "收" 资金费。

数据: freqtrade 期货模式的资金费率历史 (K 线类型 funding_rate, 2026.10 只有 date + funding_rate 两列,
      每次结算一行)。策略里用 dp.get_pair_dataframe(pair, 资金费率周期, candle_type="funding_rate") 读取。
      !! OKX 的资金费率接口只给最近约 3 个月, 回测区间更早的部分没有数据 -> 那段时间这个策略一笔都不开。
      这是故意的 "失败即安全": 没有数据、数据太旧 (>12 小时没更新)、数据不够 = 不开仓。
      配置里的 futures_funding_rate: 0 只用于回测算资金费成本, 不会变成这里的 "假数据"。

逻辑 (逐根已收盘 K 线, 无未来函数):
  费率  每次结算的费率先换算成 "每 8 小时" 口径 (OKX 有的币是 4h / 2h / 1h 结算一次, 按间隔换算),
        再取最近 24 小时内各次结算的平均 = fund_avg (至少 2 次结算才算数)。
        结算时间 <= 这根 K 线开盘时间的费率才可见 (结算后才知道, 比实际更保守)。
        "中性" 费率 = 0.01% / 8h (OKX、币安在多空平衡时的默认费率), 偏离 = fund_avg - 0.01%。
  做空  偏离 >= fund_extreme (默认 0.04%, 即 fund_avg >= 0.05%/8h, 多头拥挤),
        且最近一次结算的偏离 >= fund_extreme / 2 (现在还拥挤)
        + 过去 72 根 K 线是涨的 (多头确实追进去了)
        + 动能停滞: 收盘价跌到 EMA20 下方, 且 EMA20 比 3 根前低 (涨不动了)
  做多  偏离 <= -fund_extreme (fund_avg <= -0.03%/8h, 空头拥挤), 且最近一次结算偏离 <= -fund_extreme / 2
        + 过去 72 根 K 线是跌的 + 收盘价回到 EMA20 上方且 EMA20 比 3 根前高
  止损  入场价 -/+ stop_atr_mult x ATR14 (默认 1.5 倍, 严格)
  离场  (1) 拥挤消失: 最近一次结算的费率回到中性 (做空: <= 0.01%; 做多: >= 0.01%) -> 下一根开盘平仓
        (2) 吊灯止损: 持仓以来最低价 (做多 = 最高价) +/- trail_atr_mult x 入场 ATR, 只收紧
        (3) 超时: 持有满 max_hold_candles 根 (默认 48 根 = 1h 策略 2 天)
  保护  同一个币: 平仓后冷却 4 根; 48 根内被止损 2 次 -> 这个币暂停 24 根
  仓位  easytrade_risk: 每笔打到止损亏总资金 1%, 组合热度 <= 6%, 杠杆 <= 2 (硬上限 5)

可 hyperopt 的参数 3 个: fund_extreme / stop_atr_mult / trail_atr_mult。
!! OKX 只有约 3 个月资金费率, 不够分 "训练段 / 验证段", 现在不要调参, 先用默认值观察 (见文档)。
说明文档: docs/strategies-step2.md "非趋势类 / FundingContrarian"。
"""

from __future__ import annotations

import logging

import numpy as np
import pandas as pd
from pandas import DataFrame

from freqtrade.strategy import DecimalParameter, IStrategy

from easytrade_risk import EasyTradeRiskMixin, atr, ema


logger = logging.getLogger(__name__)

FUNDING_CANDLE_TYPE = "funding_rate"


class FundingContrarian(EasyTradeRiskMixin, IStrategy):
    INTERFACE_VERSION = 3

    timeframe = "1h"
    can_short = True
    # 本周期: 72 根涨跌幅、EMA20、ATR14 -> 100 根足够; 取 200 留余量。
    # 资金费率是 "每次结算一行", 回测时按 200 根 1h (约 8 天) 额外加载, 24 小时平均窗口足够。
    startup_candle_count = 200
    process_only_new_candles = True

    minimal_roi = {"0": 100.0}
    trailing_stop = False
    use_custom_stoploss = True
    use_exit_signal = True
    exit_profit_only = False
    ignore_roi_if_entry_signal = False
    stoploss = -0.50  # 兜底 (2 倍杠杆 = 价格反向 25%), 成交后换成 ATR 止损

    # ---------------- 可 hyperopt 的参数 (数据够长之前不要调) ----------------
    # 偏离中性费率多少算 "极端" (每 8 小时口径, 0.0004 = 0.04%)
    fund_extreme = DecimalParameter(0.0002, 0.0010, default=0.0004, decimals=4, space="buy")
    stop_atr_mult = DecimalParameter(1.0, 2.5, default=1.5, decimals=1, space="sell")
    trail_atr_mult = DecimalParameter(1.5, 4.0, default=2.5, decimals=1, space="sell")

    # ---------------- 固定参数 (不调) ----------------
    fund_neutral = 0.0001  # 0.01% / 8h
    fund_avg_window = "24h"
    fund_min_settlements = 2
    fund_max_age_hours = 12.0  # 最近一次结算超过 12 小时 = 数据断了 -> 不开仓
    fund_max_interval_hours = 8.0  # 两次结算间隔 > 8h = 中间缺数据, 间隔沿用上一次的
    run_bars = 72
    stall_ema_period = 20
    stall_slope_bars = 3
    atr_period = 14
    atr_column = "atr"
    max_hold_candles = 48
    enable_long = True
    enable_short = True
    et_name = "funding_ctr"

    plot_config = {
        "main_plot": {"stall_ema": {"color": "#9b51e0"}},
        "subplots": {
            "Funding(8h)": {
                "fund_8h": {"color": "#2f80ed"},
                "fund_avg": {"color": "#eb5757"},
            },
        },
    }

    @property
    def protections(self):
        return [
            {"method": "CooldownPeriod", "stop_duration_candles": 4},
            {
                "method": "StoplossGuard",
                "lookback_period_candles": 48,
                "trade_limit": 2,
                "stop_duration_candles": 24,
                "only_per_pair": True,
            },
        ]

    # ------------------------------------------------------------------
    # 资金费率数据
    # ------------------------------------------------------------------

    def _funding_timeframe(self) -> str:
        try:
            return str(self.dp.get_funding_rate_timeframe())
        except Exception:
            return "1h"  # freqtrade 2026.10 所有交易所的默认值

    def informative_pairs(self):
        # 实盘 / 模拟盘: 让 freqtrade 每轮刷新资金费率历史 (回测不需要, dp 会直接读磁盘)
        if self.dp is None:
            return []
        ftf = self._funding_timeframe()
        return [(p, ftf, FUNDING_CANDLE_TYPE) for p in self.dp.current_whitelist()]

    def _log_once(self, pair: str, level: int, msg: str, *args) -> None:
        seen = self.__dict__.setdefault("_fc_logged", set())
        key = (pair, msg)
        if key in seen:
            return
        seen.add(key)
        logger.log(level, msg, *args)

    def _load_funding(self, pair: str) -> DataFrame | None:
        """读取这个币的资金费率历史, 返回 [fdate, rate] (按时间排序); 没有数据 / 出错返回 None。"""
        if getattr(self, "dp", None) is None:
            return None
        try:
            fr = self.dp.get_pair_dataframe(
                pair=pair, timeframe=self._funding_timeframe(), candle_type=FUNDING_CANDLE_TYPE
            )
        except Exception as exc:
            self._log_once(pair, logging.WARNING,
                           "FundingContrarian %s: 读取资金费率失败, 不开仓: %s", pair, exc)
            return None
        if fr is None or fr.empty:
            self._log_once(pair, logging.WARNING,
                           "FundingContrarian %s: 没有资金费率数据, 不开仓 "
                           "(期货模式下载数据时会自动下载 funding_rate; OKX 只有最近约 3 个月)", pair)
            return None
        col = "funding_rate" if "funding_rate" in fr.columns else ("open" if "open" in fr.columns else None)
        if col is None:
            self._log_once(pair, logging.ERROR,
                           "FundingContrarian %s: 资金费率数据没有 funding_rate 列 (列: %s), 不开仓",
                           pair, list(fr.columns))
            return None
        out = pd.DataFrame({
            "fdate": pd.to_datetime(fr["date"], utc=True).astype("datetime64[ns, UTC]"),
            "rate": pd.to_numeric(fr[col], errors="coerce"),
        })
        out = out.dropna().sort_values("fdate").drop_duplicates("fdate", keep="last").reset_index(drop=True)
        if out.empty:
            return None
        return out

    def _funding_features(self, fr: DataFrame) -> DataFrame:
        """每次结算: 换算成 8h 口径 (fund_8h), 最近 24h 平均 (fund_avg) 和结算次数 (fund_n)。"""
        gap_h = fr["fdate"].diff().dt.total_seconds() / 3600.0
        interval = gap_h.where((gap_h > 0.5) & (gap_h <= self.fund_max_interval_hours + 0.01))
        interval = interval.ffill().fillna(self.fund_max_interval_hours).clip(lower=1.0)
        f8 = fr["rate"] * (8.0 / interval)
        s = pd.Series(f8.to_numpy(), index=pd.DatetimeIndex(fr["fdate"]))
        roll = s.rolling(self.fund_avg_window)  # 窗口 (t-24h, t], 只用 t 及以前
        return pd.DataFrame({
            "fdate": fr["fdate"].reset_index(drop=True),  # 保留 UTC 时区, merge_asof 要求两边类型一致
            "fund_8h": f8.to_numpy(),
            "fund_avg": roll.mean().to_numpy(),
            "fund_n": roll.count().to_numpy(),
        })

    # ------------------------------------------------------------------
    # 指标
    # ------------------------------------------------------------------

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        pair = metadata["pair"]
        c = dataframe["close"].astype("float64")
        dataframe["atr"] = atr(dataframe, self.atr_period)
        e = pd.Series(ema(c, self.stall_ema_period), index=dataframe.index)
        dataframe["stall_ema"] = e
        dataframe["stall_slope"] = e - e.shift(self.stall_slope_bars)
        dataframe["run_ret"] = c / c.shift(self.run_bars) - 1.0

        for col in ("fund_8h", "fund_avg", "fund_n", "fund_age_h"):
            dataframe[col] = np.nan

        fr = self._load_funding(pair)
        if fr is None:
            return dataframe
        feats = self._funding_features(fr)

        # 每根 K 线取 "结算时间 <= 这根 K 线开盘时间" 的最近一次结算 (backward asof, 无未来函数)
        left = pd.DataFrame({
            "date": pd.to_datetime(dataframe["date"], utc=True).astype("datetime64[ns, UTC]"),
            "_row": np.arange(len(dataframe)),
        })
        merged = pd.merge_asof(left, feats, left_on="date", right_on="fdate", direction="backward")
        merged = merged.sort_values("_row")
        age_h = (merged["date"] - merged["fdate"]).dt.total_seconds() / 3600.0
        fresh = (age_h <= self.fund_max_age_hours) & (merged["fund_n"] >= self.fund_min_settlements)
        dataframe["fund_8h"] = merged["fund_8h"].where(fresh).to_numpy()
        dataframe["fund_avg"] = merged["fund_avg"].where(fresh).to_numpy()
        dataframe["fund_n"] = merged["fund_n"].to_numpy()
        dataframe["fund_age_h"] = age_h.to_numpy()

        covered = int(np.isfinite(dataframe["fund_avg"].to_numpy(dtype="float64")).sum())
        self._log_once(
            pair, logging.INFO,
            "FundingContrarian %s: 资金费率 %d 条 (%s ~ %s), 覆盖 %d / %d 根 K 线; 没覆盖的时间不开仓",
            pair, len(fr), fr["fdate"].iloc[0], fr["fdate"].iloc[-1], covered, len(dataframe),
        )
        return dataframe

    # ------------------------------------------------------------------
    # 信号
    # ------------------------------------------------------------------

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        x = float(self.fund_extreme.value)
        dev = dataframe["fund_avg"] - self.fund_neutral  # NaN (没有数据) -> 比较结果 False -> 不开仓
        c = dataframe["close"]
        e = dataframe["stall_ema"]
        slope = dataframe["stall_slope"]
        ok = (dataframe["volume"] > 0) & dataframe["atr"].notna()

        # 24h 平均够极端, 并且最近一次结算仍然偏向同一边 (至少一半), 否则一进场就会被 "拥挤消失" 离场
        last = dataframe["fund_8h"] - self.fund_neutral
        crowded_long = (dev >= x) & (last >= x / 2)
        crowded_short = (dev <= -x) & (last <= -x / 2)
        stall_up = (dataframe["run_ret"] > 0) & (c < e) & (slope <= 0)  # 涨过一段, 现在涨不动了
        stall_down = (dataframe["run_ret"] < 0) & (c > e) & (slope >= 0)  # 跌过一段, 现在跌不动了

        short_sig = ok & crowded_long & stall_up & bool(self.enable_short)
        long_sig = ok & crowded_short & stall_down & bool(self.enable_long)
        dataframe["enter_long"] = long_sig.astype("int64")
        dataframe["enter_short"] = short_sig.astype("int64")
        tags = pd.Series(None, index=dataframe.index, dtype=object)
        tags[long_sig] = "funding_ctr_long"
        tags[short_sig] = "funding_ctr_short"
        dataframe["enter_tag"] = tags
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        # 拥挤消失: 最近一次结算的费率回到中性。没有数据 (NaN) 时不发离场信号, 交给止损 / 吊灯 / 超时
        f = dataframe["fund_8h"]
        calm_for_short = f <= self.fund_neutral
        calm_for_long = f >= self.fund_neutral
        dataframe["exit_short"] = calm_for_short.astype("int64")
        dataframe["exit_long"] = calm_for_long.astype("int64")
        tags = pd.Series(None, index=dataframe.index, dtype=object)
        tags[calm_for_short | calm_for_long] = "funding_normal"
        dataframe["exit_tag"] = tags
        return dataframe
