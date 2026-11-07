"""
easytrade_risk -- 第二步研究 (step 2) 新策略共用的风控混入类 EasyTradeRiskMixin。

本文件不是策略 (没有继承 IStrategy), freqtrade 不会把它列为策略。
用法: class MyStrategy(EasyTradeRiskMixin, IStrategy)   <- 混入类必须写在 IStrategy 前面
freqtrade 官方支持 "从同目录 import" (docs: strategy-advanced.md#derived-strategies),
hyperopt 也会把这个模块一起打包。

提供的回调 (全部是 freqtrade 官方回调, 说明见 docs/strategies-step2.md "趋势类 / 共用风控"):
  leverage()            杠杆 = min(max_leverage_param 默认 2, 交易所该币上限, 5), 永远 <= 5 倍
  custom_stake_amount() 按止损距离定仓: 打到止损约亏 总资金 x risk_per_trade (默认 1%)
                        + 组合热度上限: 所有持仓 "从现价打到各自止损还会亏多少" 合计 <= max_portfolio_risk (默认 6%)
                        + 回撤降仓: 账户权益 (含浮亏) 比最高点低 >= dd_throttle_start (默认 8%) 时, 新仓风险 x dd_throttle_mult (默认 0.5)
                        这三个默认值 2026-11-07 随账户回撤上限 8% -> 15% 一起放大到 2 倍 (第 1、2 轮是 0.5% / 3% / 4%);
                        都是类属性, 子类可以覆盖 (例如 risk_per_trade = 0.005 = 旧的一半仓位)
  custom_stoploss()     初始止损 = 入场价 -/+ stop_atr_mult x 信号K线的 ATR (ATR 和止损价存进交易记录, 重启不丢)
                        + 策略自己的移动止损 (唐奇安通道 / 吊灯止损), 只收紧不放宽
  custom_exit()         止损补救 (实盘: 止损价已被越过却挂不上 -> 立即平仓) / 持仓超时 (可选)
                        / 回测里开盘跳空越过止损 -> 按开盘价平仓 (不假装在止损价成交)
  custom_exit_price()   实盘止损补救用越过盘口 0.5% 的限价, 保证马上成交 (回测不改价)
  bot_loop_start()      每根 K 线记录账户权益最高点 (给回撤降仓用)

离场原因代码: et_stop_breached / et_stop_invalid (只会在实盘出现), et_trail_gap, et_stop_gap (只会在回测出现),
et_timeout; freqtrade 自己的 stop_loss = 初始止损, trailing_stop_loss = 止损被移动过 (通道 / 吊灯止损)。

子类提供 / 可覆盖:
  atr_column                        populate_indicators 里算好的 ATR 列名 (默认 "atr")
  stop_atr_mult                     初始止损的 ATR 倍数 (可以是 DecimalParameter)
  trail_atr_mult                    吊灯止损: 持仓以来最高价(做空=最低价) -/+ N x 入场 ATR; 0 = 关闭 (默认)
  max_hold_candles                  持仓超时 (根 K 线); 0 = 关闭 (默认)
  et_structure_stop(row, is_short)  可选: 信号 K 线上的 "结构止损" (如通道下轨), 和 ATR 止损取更近的那个
  et_trailing_stop(trade, last_row) 可选: 持仓期间的移动止损价 (绝对价格); 默认 = 吊灯止损
  列 et_size_mult_long / et_size_mult_short (可选): 0..1 的降仓系数 (例如多周期逆势时减半)

无未来函数: 所有止损价、热度、权益都只用 "当时已经收盘" 的 K 线计算
(回测里 dp.get_analyzed_dataframe 的最后一行就是上一根已收盘 K 线, 实盘同理)。
"""

from __future__ import annotations

import logging
import math
from datetime import datetime, timedelta
from typing import Any

import numpy as np
import pandas as pd
import talib
from pandas import DataFrame

from freqtrade.exchange import timeframe_to_prev_date, timeframe_to_seconds
from freqtrade.persistence import Trade
from freqtrade.strategy import merge_informative_pair, stoploss_from_absolute


logger = logging.getLogger(__name__)

# 杠杆硬上限: 不管参数、交易所允许多少, 永远不超过 5 倍 (项目风控规则)。
HARD_MAX_LEVERAGE = 5.0
# 止损距离 (价格) 最多 = 0.8 / 杠杆: 逐仓 5 倍时强平大约在 -19.5%, 止损最远放到 -16%, 留出余量。
LIQ_SAFE_FRACTION = 0.8

# 离场原因 (回测报告 / FreqUI / 中文通知里显示的英文代码)
EXIT_STOP_BREACHED = "et_stop_breached"  # 实盘: 止损价没能挂上, 价格已经越过它 -> 立即平仓
EXIT_STOP_INVALID = "et_stop_invalid"  # 实盘: 按实际成交价算不出有效止损 -> 立即平仓
EXIT_TRAIL_GAP = "et_trail_gap"  # 移动止损价已经在上一根收盘价的另一侧 -> 开盘立即平仓
EXIT_STOP_GAP = "et_stop_gap"  # 只在回测: K 线开盘价已经跳过止损价 -> 按开盘价平仓 (不假装在止损价成交)
EXIT_TIMEOUT = "et_timeout"  # 持仓满 max_hold_candles 根 K 线
RESCUE_EXITS = (EXIT_STOP_BREACHED, EXIT_STOP_INVALID, EXIT_TRAIL_GAP)

# trade custom data 的键 (存在交易记录里, 机器人重启后还在)
KEY_INIT_STOP = "et_init_stop"  # 初始止损价
KEY_STOP = "et_stop"  # 当前目标止损价 (初始止损 + 移动止损, 只收紧)
KEY_ATR = "et_entry_atr"  # 信号 K 线的 ATR
KEY_EXIT_NOW = "et_exit_now"  # 有值 = 下一次检查立即平仓 (值就是离场原因)


# ---------------------------------------------------------------------------
# 小工具 (纯函数)
# ---------------------------------------------------------------------------


def param_value(value: Any) -> Any:
    """hyperopt 参数 (IntParameter / DecimalParameter ...) 取 .value; 普通数值原样返回。"""
    return getattr(value, "value", value)


def positive_or_none(value: Any) -> float | None:
    """转成有限正数; NaN / None / <=0 返回 None。"""
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) and f > 0 else None


def atr(df: DataFrame, period: int) -> np.ndarray:
    """ATR (talib, Wilder 平滑)。只用当前及更早的 K 线。"""
    return talib.ATR(
        df["high"].to_numpy(dtype="float64"),
        df["low"].to_numpy(dtype="float64"),
        df["close"].to_numpy(dtype="float64"),
        timeperiod=int(period),
    )


def ema(series: pd.Series, period: int) -> np.ndarray:
    """EMA (talib, SMA 种子), 与 rust_port_common.ema 同口径。"""
    return talib.EMA(series.to_numpy(dtype="float64"), timeperiod=int(period))


# ---------------------------------------------------------------------------
# 混入类
# ---------------------------------------------------------------------------


class EasyTradeRiskMixin:
    """放在 IStrategy 前面继承: class XxxTrend(EasyTradeRiskMixin, IStrategy)。"""

    # ---------------- 杠杆 ----------------
    # 实际杠杆 = min(max_leverage_param, 交易所该币允许的最大杠杆, 5), 且 >= 1。
    # 杠杆不决定一笔亏多少 (那是 risk_per_trade 的事), 只决定占用多少保证金、离强平多远。
    max_leverage_param: float = 2.0

    # ---------------- 仓位 (按止损距离定仓) ----------------
    # 三个风险预算 (risk_per_trade / max_portfolio_risk / dd_throttle_start) 跟着项目的账户回撤上限定:
    # 2026-11-07 回撤上限从 8% 放宽到 15% (scripts/check_drawdown.py DEFAULT_MAX_DD_PCT), 这三个一起放大到 2 倍
    # (第 1、2 轮研究用的是 0.5% / 3% / 4%)。子类可以直接覆盖 (类属性), 例如 risk_per_trade = 0.005。
    # 每笔: 打到止损亏 ≈ 总资金 x risk_per_trade (默认 1%; 以前 0.5%)。
    # 名义仓位 = 风险金额 / (止损距离 + 成本缓冲); 保证金 = 名义仓位 / 杠杆。
    risk_per_trade: float = 0.01
    # 组合热度上限: 所有持仓 "从现价打到各自当前止损还会亏多少" 合计不超过 总资金 x 6% (以前 3%)。
    # 币之间高度相关, 急涨急跌时可能所有止损一起被打到, 这个上限就是 "最坏一起亏多少" (远小于 15% 的回撤上限)。
    max_portfolio_risk: float = 0.06
    # 剩余热度不到一笔正常风险的 25% 时, 这一笔就不开了 (避免开一堆很小的仓)。
    min_risk_fraction: float = 0.25
    # 手续费 + 滑点缓冲 (OKX/币安 taker 约 0.05% x 2 边 + 滑点, 再留余量)
    sizing_cost_buffer: float = 0.002
    # 止损特别近时按这个最小距离算仓位 (防止仓位被放得过大)
    min_stop_distance: float = 0.01
    # 止损最远放到入场价的 25% (再远就截到 25%, 仓位按截断后的距离算)
    max_stop_distance: float = 0.25
    # 单笔名义仓位上限 = 总资金 x 这个比例
    max_position_notional_pct: float = 1.0
    # 保证金同时不超过 freqtrade 按 stake_amount / max_open_trades 给的建议仓位
    cap_stake_by_proposed: bool = True

    # ---------------- 回撤降仓 ----------------
    # 账户权益 (已平仓余额 + 持仓浮动盈亏) 比运行以来最高点低 >= dd_throttle_start 时,
    # 新开仓的风险乘 dd_throttle_mult。dd_throttle_start = 0 关闭。
    # 默认 8% (以前 4%): 大约是 15% 回撤上限的一半, 离上限还有一半距离时开始踩刹车。
    # 注意: 实盘重启后 "最高点" 从重启那一刻重新算。
    dd_throttle_start: float = 0.08
    dd_throttle_mult: float = 0.5

    # ---------------- 止损 ----------------
    atr_column: str = "atr"
    stop_atr_mult: Any = 2.0
    # 吊灯止损 (chandelier): 持仓以来已收盘 K 线的最高价 - N x 入场 ATR (做空反之); 0 = 关闭
    trail_atr_mult: Any = 0.0
    # 止损补救出场 (et_stop_breached 等) 越过盘口多少挂单, 保证立即成交
    stop_rescue_exit_slippage: float = 0.005

    # ---------------- 出场 ----------------
    # 持仓满多少根 K 线后平仓; 0 = 不限
    max_hold_candles: Any = 0

    # ---------------- 只在信号 K 线刚收盘时入场 ----------------
    # 回测只会在 "信号 K 线收盘后的下一根开盘" 入场。实盘默认会在整根 K 线期间一直按这个信号入场
    # (日线 = 一整天), 回测里没有这些交易。600 秒 = unfilledtimeout.entry 10 分钟, 和回测口径一致。
    ignore_buying_expired_candle_after: int = 600

    # ---------------- BTC 大盘过滤 (策略可选用) ----------------
    btc_pair: str = "BTC/USDT:USDT"
    btc_timeframe: str = "1d"

    # 日志 / 入场标签前缀
    et_name: str = "et"

    # ======================================================================
    # 参数 / 数据小工具
    # ======================================================================

    def _pv(self, name: str, default: Any = None) -> Any:
        return param_value(getattr(self, name, default))

    def _tf_seconds(self) -> int:
        return timeframe_to_seconds(self.timeframe)

    def _et_is_live(self) -> bool:
        """模拟盘 / 实盘 = True; 回测 / hyperopt / 其他 = False。"""
        dp = getattr(self, "dp", None)
        mode = dp.runmode if dp is not None else self.config.get("runmode")
        return str(getattr(mode, "value", mode)) in ("live", "dry_run")

    def et_analyzed(self, pair: str) -> DataFrame | None:
        if getattr(self, "dp", None) is None:
            return None
        df, _ = self.dp.get_analyzed_dataframe(pair, self.timeframe)
        if df is None or df.empty:
            return None
        return df

    def et_last_row(self, pair: str) -> pd.Series | None:
        """最后一根已收盘 K 线 (入场时就是触发信号的那根)。"""
        df = self.et_analyzed(pair)
        return None if df is None else df.iloc[-1]

    def et_last_close(self, pair: str) -> float | None:
        row = self.et_last_row(pair)
        return None if row is None else positive_or_none(row.get("close"))

    def et_signal_row_for_trade(self, trade: Trade) -> pd.Series | None:
        """开仓所依据的信号 K 线 (= 开仓所在 K 线的前一根)。"""
        df = self.et_analyzed(trade.pair)
        if df is None:
            return None
        entry_candle = timeframe_to_prev_date(self.timeframe, trade.open_date_utc)
        signal_date = pd.Timestamp(entry_candle - timedelta(seconds=self._tf_seconds()))
        dates = pd.to_datetime(df["date"], utc=True)
        hit = df.loc[dates == signal_date]
        return None if hit.empty else hit.iloc[-1]

    def et_merge_htf(
        self, dataframe: DataFrame, informative: DataFrame, inf_tf: str, suffix: str
    ) -> DataFrame:
        """把更高 (或相同) 周期的列并到本周期, 列名 <列>_<suffix>。

        用 freqtrade 官方 merge_informative_pair: 高周期 K 线收盘后才会出现在本周期上, 无未来函数。
        informative 只需要 date + 要合并的列。
        """
        if timeframe_to_seconds(inf_tf) < self._tf_seconds():
            raise ValueError(f"{inf_tf} 比策略周期 {self.timeframe} 小, 不能用 merge_informative_pair")
        merged = merge_informative_pair(
            dataframe, informative, self.timeframe, inf_tf, ffill=True,
            append_timeframe=False, suffix=suffix,
        )
        return merged.drop(columns=[f"date_{suffix}"], errors="ignore")

    def et_add_btc_regime(self, dataframe: DataFrame, ema_period: int) -> DataFrame:
        """写 btc_regime 列: BTC 日线收盘在 EMA 上方 = +1 (多头市场), 下方 = -1, 数据不够 = NaN。"""
        dataframe["btc_regime"] = np.nan
        if getattr(self, "dp", None) is None:
            return dataframe
        try:
            btc = self.dp.get_pair_dataframe(pair=self.btc_pair, timeframe=self.btc_timeframe)
        except Exception as exc:
            logger.warning("%s: 读取 %s %s 失败, BTC 过滤视为 '不知道' (不开仓): %s",
                           self.et_name, self.btc_pair, self.btc_timeframe, exc)
            return dataframe
        if btc is None or btc.empty:
            logger.warning("%s: 没有 %s %s 数据, BTC 过滤视为 '不知道' (不开仓)",
                           self.et_name, self.btc_pair, self.btc_timeframe)
            return dataframe
        inf = btc[["date", "close"]].copy()
        e = ema(inf["close"], ema_period)
        c = inf["close"].to_numpy(dtype="float64")
        inf["regime"] = np.where(np.isnan(e), np.nan, np.where(c > e, 1.0, np.where(c < e, -1.0, 0.0)))
        inf = inf[["date", "regime"]]
        dataframe = dataframe.drop(columns=["btc_regime"])
        dataframe = self.et_merge_htf(dataframe, inf, self.btc_timeframe, "btc")
        return dataframe.rename(columns={"regime_btc": "btc_regime"})

    # ======================================================================
    # 止损价 (绝对价格)
    # ======================================================================

    def et_stop_cap(self, leverage: float | None) -> float:
        """止损最远距离 (价格比例) = min(max_stop_distance, |stoploss|/杠杆, 0.8/杠杆)。"""
        lev = max(1.0, float(leverage or 1.0))
        return min(
            float(self.max_stop_distance),
            abs(float(self.stoploss)) / lev * 0.999,
            LIQ_SAFE_FRACTION / lev,
        )

    def et_structure_stop(self, row: pd.Series, is_short: bool) -> float | None:
        """子类可覆盖: 信号 K 线上的结构止损价 (例如通道下轨)。None = 没有。"""
        return None

    def et_initial_stop(
        self, row: pd.Series | None, entry_rate: float, is_short: bool, leverage: float | None
    ) -> tuple[float | None, float | None]:
        """初始止损 = 入场价 -/+ stop_atr_mult x ATR, 和结构止损取更近的, 再按 et_stop_cap 截断。

        返回 (止损价, ATR); 不能开仓时止损价为 None (没有 ATR / 结构止损在错误一侧)。
        """
        entry = positive_or_none(entry_rate)
        if entry is None or row is None:
            return None, None
        a = positive_or_none(row.get(self.atr_column))
        mult = positive_or_none(self._pv("stop_atr_mult"))
        if a is None or mult is None:
            return None, None
        stop = entry + mult * a if is_short else entry - mult * a
        try:
            struct = self.et_structure_stop(row, is_short)
        except Exception as exc:
            logger.warning("%s 计算结构止损失败, 只用 ATR 止损: %s", self.et_name, exc)
            struct = None
        struct = positive_or_none(struct)
        if struct is not None:
            if (is_short and struct <= entry) or ((not is_short) and struct >= entry):
                return None, a  # 价格已经在离场线另一侧, 不开仓
            stop = min(stop, struct) if is_short else max(stop, struct)
        cap = self.et_stop_cap(leverage)
        cap_stop = entry * (1 + cap) if is_short else entry * (1 - cap)
        stop = min(stop, cap_stop) if is_short else max(stop, cap_stop)
        if stop <= 0:
            return None, a
        return float(stop), a

    def et_best_price_since_entry(self, trade: Trade) -> float | None:
        """持仓以来已收盘 K 线的最高价 (做空 = 最低价), 含开仓那根 (收盘后)。"""
        df = self.et_analyzed(trade.pair)
        if df is None:
            return None
        entry_candle = pd.Timestamp(timeframe_to_prev_date(self.timeframe, trade.open_date_utc))
        mask = pd.to_datetime(df["date"], utc=True) >= entry_candle
        if not mask.any():
            return None
        return float(df.loc[mask, "low"].min() if trade.is_short else df.loc[mask, "high"].max())

    def et_trailing_stop(self, trade: Trade, last_row: pd.Series) -> float | None:
        """子类可覆盖: 持仓期间的移动止损价。默认 = 吊灯止损 (trail_atr_mult > 0 时)。"""
        mult = positive_or_none(self._pv("trail_atr_mult", 0.0))
        a = positive_or_none(trade.get_custom_data(KEY_ATR))
        if mult is None or a is None:
            return None
        best = self.et_best_price_since_entry(trade)
        if best is None:
            return None
        return best + mult * a if trade.is_short else best - mult * a

    def et_trade_stop(self, trade: Trade) -> float | None:
        """一笔持仓当前的有效止损价 (存下来的目标止损 和 freqtrade 当前止损 里更紧的那个)。"""
        cands = []
        stored = positive_or_none(trade.get_custom_data(KEY_STOP))
        if stored is None:
            # 还没成交 / 还没跑过 custom_stoploss: 按开仓时的算法估算
            row = self.et_signal_row_for_trade(trade)
            stored, _ = self.et_initial_stop(row, trade.open_rate, trade.is_short, trade.leverage)
        if stored is not None:
            cands.append(stored)
        active = positive_or_none(trade.stop_loss)
        if active is not None:
            cands.append(active)
        if not cands:
            return None
        return min(cands) if trade.is_short else max(cands)

    # ======================================================================
    # 组合: 热度 (open risk) 和 权益
    # ======================================================================

    def et_wallet_equity(self) -> float | None:
        """freqtrade 口径的总资金 (已平仓余额, 含已占用保证金, x tradable_balance_ratio)。"""
        wallets = getattr(self, "wallets", None)
        if wallets is None:
            return None
        try:
            return float(wallets.get_total_stake_amount())
        except Exception:
            return None

    def et_portfolio_state(self) -> tuple[float, float]:
        """返回 (组合热度 USDT, 持仓浮动盈亏 USDT)。现价 = 各币最后一根已收盘 K 线的收盘价。

        热度 = sum( 数量 x (max(现价到止损的不利距离, 0) + 现价 x 成本缓冲) )。
        止损不知道时 (理论上不会) 按最远止损 et_stop_cap 估算, 宁可高估。
        """
        heat = 0.0
        upnl = 0.0
        buf = float(self.sizing_cost_buffer)
        for t in Trade.get_trades_proxy(is_open=True):
            amount = float(t.amount or 0.0)
            # 实盘里还没成交的开仓单: amount 还是 0, 按挂单数量算 (宁可高估)
            for o in getattr(t, "open_orders", []) or []:
                if getattr(o, "ft_order_side", None) == t.entry_side:
                    amount += float(o.safe_remaining or 0.0)
            if amount <= 0:
                continue
            price = self.et_last_close(t.pair) or float(t.open_rate)
            stop = self.et_trade_stop(t)
            if stop is None:
                cap = self.et_stop_cap(t.leverage)
                stop = price * (1 + cap) if t.is_short else price * (1 - cap)
            dist = (stop - price) if t.is_short else (price - stop)
            heat += amount * (max(dist, 0.0) + price * buf)
            upnl += amount * ((t.open_rate - price) if t.is_short else (price - t.open_rate))
        return heat, upnl

    def et_drawdown(self, current_time: datetime) -> float:
        """账户权益 (余额 + 浮动盈亏) 相对运行以来最高点的回撤 (0..1), 顺便更新最高点。

        回测/hyperopt 里同一个策略对象会跑很多遍: 发现时间倒退就清零重新记。
        """
        st = self.__dict__.setdefault("_et_dd_state", {"peak": None, "t": None})
        if st["t"] is not None and current_time < st["t"]:
            st["peak"] = None
        st["t"] = current_time
        wallet = self.et_wallet_equity()
        if wallet is None:
            return 0.0
        _, upnl = self.et_portfolio_state()
        eq = wallet + upnl
        if st["peak"] is None or eq > st["peak"]:
            st["peak"] = eq
        peak = st["peak"]
        return max(0.0, (peak - eq) / peak) if peak and peak > 0 else 0.0

    def et_size_mult(self, row: pd.Series | None, side: str) -> float:
        """信号 K 线上的降仓系数列 et_size_mult_<side> (没有这一列 = 1)。"""
        if row is None:
            return 1.0
        v = positive_or_none(row.get(f"et_size_mult_{side}", 1.0))
        return 0.0 if v is None else min(v, 1.0)

    # ======================================================================
    # freqtrade 回调
    # ======================================================================

    def bot_loop_start(self, current_time: datetime, **kwargs: Any) -> None:
        # 每根 K 线 (实盘每轮循环) 记一次权益最高点, 回撤降仓才不会漏掉中间的高点
        if float(self.dd_throttle_start or 0) > 0:
            try:
                self.et_drawdown(current_time)
            except Exception as exc:
                logger.debug("%s 记录权益最高点失败: %s", self.et_name, exc)

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
        """杠杆 = min(max_leverage_param 默认 2, 交易所允许的最大值, 5), 且 >= 1。永远不超过 5 倍。"""
        lev = min(float(self._pv("max_leverage_param")), float(max_leverage), HARD_MAX_LEVERAGE)
        return max(1.0, lev)

    def custom_stake_amount(
        self,
        pair: str,
        current_time: datetime,
        current_rate: float,
        proposed_stake: float,
        min_stake: float | None,
        max_stake: float,
        leverage: float,
        entry_tag: str | None,
        side: str,
        **kwargs: Any,
    ) -> float:
        """按止损距离定仓 + 组合热度上限 + 回撤降仓。返回 0 = 不开这笔; 出错也返回 0 (绝不满仓兜底)。"""
        try:
            is_short = side == "short"
            row = self.et_last_row(pair)
            stop, _a = self.et_initial_stop(row, current_rate, is_short, leverage)
            if stop is None:
                logger.info("%s %s: 算不出有效止损 (没有 ATR 或价格已在离场线另一侧), 跳过", self.et_name, pair)
                return 0.0
            dist = abs(stop - current_rate) / current_rate
            dist_for_sizing = max(dist, float(self.min_stop_distance)) + float(self.sizing_cost_buffer)

            mult = self.et_size_mult(row, side)
            if mult <= 0:
                return 0.0
            equity = self.et_wallet_equity() or float(proposed_stake)
            if equity <= 0:
                return 0.0

            risk_frac = float(self._pv("risk_per_trade")) * mult
            dd_start = float(self.dd_throttle_start or 0)
            if dd_start > 0:
                dd = self.et_drawdown(current_time)
                if dd >= dd_start:
                    risk_frac *= float(self.dd_throttle_mult)
                    logger.info("%s %s: 账户回撤 %.2f%% >= %.2f%%, 本笔风险减为 %.3f%%",
                                self.et_name, pair, dd * 100, dd_start * 100, risk_frac * 100)
            base_risk = equity * risk_frac

            heat, _ = self.et_portfolio_state()
            room = equity * float(self.max_portfolio_risk) - heat
            risk_amount = min(base_risk, room)
            if risk_amount <= 0 or risk_amount < base_risk * float(self.min_risk_fraction):
                logger.info("%s %s: 组合热度 %.2f%% 已接近上限 %.2f%%, 跳过",
                            self.et_name, pair, heat / equity * 100, float(self.max_portfolio_risk) * 100)
                return 0.0

            notional = risk_amount / dist_for_sizing
            notional = min(notional, equity * float(self.max_position_notional_pct))
            lev = max(1.0, float(leverage or 1.0))
            stake = notional / lev
            if self.cap_stake_by_proposed and proposed_stake:
                stake = min(stake, float(proposed_stake))
            stake = min(stake, float(max_stake))
            return max(stake, 0.0)
        except Exception as exc:
            logger.error("%s custom_stake_amount 出错, 跳过入场: %s", self.et_name, exc)
            return 0.0

    def custom_stoploss(
        self,
        pair: str,
        trade: Trade,
        current_time: datetime,
        current_rate: float,
        current_profit: float,
        after_fill: bool,
        **kwargs: Any,
    ) -> float | None:
        """止损 = 初始 ATR 止损 (成交时算一次, 存进交易记录), 之后按策略的移动止损收紧, 永不放宽。"""
        init = positive_or_none(trade.get_custom_data(KEY_INIT_STOP))
        if init is None:
            row = self.et_signal_row_for_trade(trade)
            if row is None:
                row = self.et_last_row(pair)
            init, a = self.et_initial_stop(row, trade.open_rate, trade.is_short, trade.leverage)
            if init is None:
                # 实盘里成交价和下单时不同, 按实际成交价算不出有效止损: 先挂最远止损当保险, 再立即平仓。
                trade.set_custom_data(KEY_EXIT_NOW, EXIT_STOP_INVALID)
                cap = self.et_stop_cap(trade.leverage)
                init = trade.open_rate * (1 + cap) if trade.is_short else trade.open_rate * (1 - cap)
            trade.set_custom_data(KEY_INIT_STOP, float(init))
            if a is not None:
                trade.set_custom_data(KEY_ATR, float(a))

        target = positive_or_none(trade.get_custom_data(KEY_STOP)) or init
        target = min(target, init) if trade.is_short else max(target, init)
        last = self.et_last_row(pair)
        if last is not None:
            try:
                dyn = positive_or_none(self.et_trailing_stop(trade, last))
            except Exception as exc:
                logger.warning("%s %s 计算移动止损失败: %s", self.et_name, pair, exc)
                dyn = None
            last_close = positive_or_none(last.get("close"))
            tighter = dyn is not None and ((dyn < target) if trade.is_short else (dyn > target))
            if tighter and last_close is not None:
                crossed = dyn <= last_close if trade.is_short else dyn >= last_close
                if crossed:
                    # 新的移动止损价已经在上一根收盘价的另一侧 (= 规则上已经该走了):
                    # 不把止损挪过去 (那样回测会假装在更好的价格成交), 而是开盘立即平仓。
                    if not trade.get_custom_data(KEY_EXIT_NOW):
                        trade.set_custom_data(KEY_EXIT_NOW, EXIT_TRAIL_GAP)
                else:
                    target = dyn
        if positive_or_none(trade.get_custom_data(KEY_STOP)) != target:
            trade.set_custom_data(KEY_STOP, float(target))

        current_stop = positive_or_none(trade.stop_loss)
        if current_stop is not None and abs(current_stop - target) <= target * 1e-6 and not after_fill:
            return None
        looser_now = current_stop is None or (
            current_stop > target if trade.is_short else current_stop < target
        )
        if not (after_fill or looser_now):
            return None  # freqtrade 当前止损已经和目标一样紧或更紧
        sl = stoploss_from_absolute(target, current_rate, is_short=trade.is_short, leverage=trade.leverage)
        if not sl:
            # 现价已经越过止损价 (实盘: 成交回调拿到的盘口价跳过了止损 / 机器人离线期间穿过了离场线)。
            # freqtrade 会把 0 当成 "没返回" 丢掉, 所以由 custom_exit 立即平仓。
            if not trade.get_custom_data(KEY_EXIT_NOW):
                trade.set_custom_data(KEY_EXIT_NOW, EXIT_STOP_BREACHED)
            return None
        return sl

    def custom_exit(
        self,
        pair: str,
        trade: Trade,
        current_time: datetime,
        current_rate: float,
        current_profit: float,
        **kwargs: Any,
    ) -> str | None:
        """止损补救 (et_stop_breached / et_stop_invalid / et_trail_gap) 和 持仓超时 (et_timeout)。

        需要 use_exit_signal = True 才会被调用 (而且当根 K 线没有离场信号)。
        """
        flag = trade.get_custom_data(KEY_EXIT_NOW)
        if flag:
            if flag != EXIT_TRAIL_GAP:
                logger.warning("%s %s: 止损没能生效 (%s), 当前价 %s, 立即平仓",
                               self.et_name, pair, flag, current_rate)
            return str(flag)
        stop = positive_or_none(trade.get_custom_data(KEY_STOP))
        active = positive_or_none(trade.stop_loss)
        if stop is not None and current_rate:
            effective = active is not None and (
                active <= stop * (1 + 1e-6) if trade.is_short else active >= stop * (1 - 1e-6)
            )
            crossed = current_rate >= stop if trade.is_short else current_rate <= stop
            if crossed and not effective:
                logger.warning("%s %s: 止损价 %s 没能生效且价格 %s 已越过, 立即平仓",
                               self.et_name, pair, stop, current_rate)
                return EXIT_STOP_BREACHED
            if crossed and not self._et_is_live():
                # 只在回测: 这根 K 线一开盘就已经跳过了止损价。freqtrade 回测默认会假装在止损价成交
                # (比开盘价好); 实盘里止损单会在跳空后的价格 (约等于开盘价) 成交。这里按开盘价平仓, 更真实。
                return EXIT_STOP_GAP
        hold = int(self._pv("max_hold_candles", 0) or 0)
        if hold > 0:
            held = (current_time - trade.open_date_utc).total_seconds()
            if held >= hold * self._tf_seconds():
                return EXIT_TIMEOUT
        return None

    def custom_exit_price(
        self,
        pair: str,
        trade: Trade,
        current_time: datetime,
        proposed_rate: float,
        current_profit: float,
        exit_tag: str | None,
        **kwargs: Any,
    ) -> float:
        """实盘/模拟盘: 止损补救出场越过盘口 0.5% 挂限价 (相当于带保护价的市价单)。

        回测里不改价 (回测按 K 线开盘价成交), 其他离场原因也不改价。
        """
        if exit_tag in RESCUE_EXITS and proposed_rate and self._et_is_live():
            slip = float(self.stop_rescue_exit_slippage)
            return proposed_rate * (1 + slip) if trade.is_short else proposed_rate * (1 - slip)
        return proposed_rate
