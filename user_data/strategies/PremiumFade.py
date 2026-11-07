"""
PremiumFade -- 永续相对现货的溢价极端时反向做 (基差极端回归), 多空双向, 1h。第 5 轮预注册策略 (规格冻结, 不调参)。

规格原文: docs/round5-preregistration.md 第 2.2 节 (和第 4 节 "PremiumFade")。下面每一条都照规格写, 数值都是普通类属性。

数据:
  主周期 = OKX USDT 永续 1h; 信息K线 = 同一个币的 OKX 现货 BASE/USDT 1h (candle_type "spot")。
  用 dp.get_pair_dataframe(现货对, "1h", candle_type="spot") 读取, 按 date 精确对齐 (同一个时间戳), 不向前填充。
  freqtrade 读K线时会把缺的K线补成 "成交量 0、价格 = 上一根收盘价" 的假K线 (load_pair_history fill_up_missing),
  这等于向前填充, 所以这里把成交量为 0 的现货K线当成 "这一根没有数据" (价格算空值)。
  这个币完全没有现货数据 = 永远不开仓, 日志里记一行带 "[现货数据:没有]" 的警告 (研究报告可以按它数 "测不了的币")。

指标 (逐根已收盘 1h K线, 无未来函数):
  prem  = 永续收盘价 / 现货收盘价 - 1                (同一根 1h K线; 现货这一根没有数据 = 空值)
  prem8 = 最近 8 根 prem 的平均 (含当前这根)          (8 根里有一根是空值 = 空值, 也就是 "最近 8 根现货K线都有数据")
  z     = (prem8 - 中位数) / (1.4826 x MAD)
          中位数、MAD 都按 "这根K线之前" 的 720 个 prem8 值算 (不含当前这根, 跳过空值; 不够 720 个 = 空值)。
          MAD = 720 个值和它们中位数之差的绝对值的中位数; MAD 为 0 时 z 算不出来 (空值, 不出信号)。

入场 (信号K线收盘后, 下一根开盘):
  做空  z >= +3 (z_threshold), 且信号K线永续收阴 (收盘 < 开盘)
  做多  z <= -3, 且信号K线永续收阳 (收盘 > 开盘)
  数据条件 (两边都要): 现货最近 24 根K线 (含信号K线) 成交额合计 >= 100 万 USDT; 最近 8 根现货K线都有数据。
                      OKX 现货的成交量单位是币, 成交额 = 成交量 x 收盘价。
  冷却: 同一个币平仓后 24 根K线内不再开仓 (protections 里的 CooldownPeriod; 回测要加 --enable-protections, 研究脚本已经带了)。

离场 (哪个先到按哪个):
  z 回到 0: 做空时 z <= 0, 做多时 z >= 0 (按已收盘K线判断), 下一根开盘平仓 (离场信号, 标签 pf_z_zero)
  持有满 48 根 (max_hold_bars; easytrade_risk 的 et_timeout)
  止损: 入场价 -/+ 2 x ATR14(1h) (easytrade_risk 的 ATR 止损, 不移动; 最远 25%, 混入类的上限)

仓位 (easytrade_risk 默认值): 每笔打到止损亏总资金 1%, 组合热度 <= 6%, 账户回撤 >= 8% 后新仓减半, 杠杆 2 倍 (硬上限 5 倍)。

参数 (3 个, 冻结): z_threshold = 3.0, window_bars = 720, max_hold_bars = 48。
固定常量 (同样冻结): 8 根平均、系数 1.4826、信号K线阴阳条件、24 根冷却、现货成交额 100 万 USDT、止损 2 x ATR14。

日志标记 (研究报告 / 管道检查按固定标记数, 改的话两边一起改):
  "[现货数据:没有]"  这个币没有现货 1h 数据, 一笔都不会开 (= "测不了" 的币)
  "[现货数据:覆盖]"  这个币的现货覆盖情况: 现货有几根、和永续时间戳对上几根、数据条件都满足且 z 有值的K线有几根
                     (最后这个数是 0 也算 "测不了")
实盘前要在模拟盘确认: 期货机器人里的现货信息K线能正常刷新 (informative_pairs 已经返回每个币的 (BASE/USDT, "1h", "spot"))。
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

import numpy as np
import pandas as pd
from numpy.lib.stride_tricks import sliding_window_view
from pandas import DataFrame

from freqtrade.exchange import timeframe_to_prev_date
from freqtrade.strategy import IStrategy

from easytrade_risk import EasyTradeRiskMixin, atr, positive_or_none


logger = logging.getLogger(__name__)

# 日志里的固定标记 (见文件开头)
SPOT_MISSING_TAG = "[现货数据:没有]"
SPOT_COVERAGE_TAG = "[现货数据:覆盖]"
# 和 XSMomentum / scripts/research_report.py 相同的 "最小下单额" 标记 (研究报告按它数次数)
MIN_STAKE_SKIP_TAG = "[最小下单额:开不出来]"
MIN_STAKE_BUMP_TAG = "[最小下单额:放大]"
FT_MIN_STAKE_ADJUST = 1.3  # freqtrade 最多把仓位放大 30% 去凑最小下单额 (freqtrade/wallets.py)

_EPOCH = pd.Timestamp("1970-01-01", tz="UTC")


# ---------------------------------------------------------------------------
# 纯函数 (向量化, 第 t 行只用到第 t 行及更早的数据)
# ---------------------------------------------------------------------------


def epoch_seconds(dates: pd.Series) -> np.ndarray:
    """freqtrade 的 date 列 -> 整数 UTC 秒 (不依赖 ns / ms 精度)。"""
    return ((pd.to_datetime(dates, utc=True) - _EPOCH) // pd.Timedelta(seconds=1)).to_numpy(dtype="int64")


def robust_z_past(x: np.ndarray, window: int, scale: float, chunk: int = 4096) -> np.ndarray:
    """z_t = (x_t - median(W_t)) / (scale x MAD(W_t)), W_t = x 在第 t 行之前的 window 个有效值 (不含 t, 跳过 NaN)。

    x_t 本身是 NaN、之前不够 window 个有效值、或 MAD = 0 时结果是 NaN。
    MAD = median(|W - median(W)|)。按块计算 (每块 chunk 个窗口), 内存只占 chunk x window 个数。
    """
    x = np.asarray(x, dtype="float64")
    out = np.full(x.shape, np.nan)
    w = int(window)
    pos = np.flatnonzero(np.isfinite(x))
    v = x[pos]
    m = len(v)
    if w < 1 or m <= w:
        return out
    win = sliding_window_view(v, w)  # 第 k 行 = v[k : k + w], 是目标 v[k + w] 之前的 w 个值
    z = np.empty(m - w)
    for s in range(0, m - w, int(chunk)):
        e = min(s + int(chunk), m - w)
        blk = win[s:e]
        med = np.median(blk, axis=1)
        mad = np.median(np.abs(blk - med[:, None]), axis=1)
        with np.errstate(divide="ignore", invalid="ignore"):
            zz = (v[s + w : e + w] - med) / (float(scale) * mad)
        zz[~np.isfinite(zz)] = np.nan  # MAD = 0: 算不出来
        z[s:e] = zz
    out[pos[w:]] = z
    return out


def spot_pair_for(pair: str) -> str:
    """永续 BASE/USDT:USDT -> 现货 BASE/USDT (照字面去掉结算币)。"""
    return pair.split(":", 1)[0]


# ---------------------------------------------------------------------------
# 策略
# ---------------------------------------------------------------------------


class PremiumFade(EasyTradeRiskMixin, IStrategy):
    INTERFACE_VERSION = 3

    timeframe = "1h"
    can_short = True
    # 规格: 760 根 (720 根窗口 + 8 根平均 + 余量)。回测时同样按 760 根 1h 额外加载现货。
    startup_candle_count = 760
    process_only_new_candles = True

    minimal_roi = {"0": 100}
    trailing_stop = False
    use_custom_stoploss = True
    use_exit_signal = True  # z 回到 0 的离场信号 + custom_exit (超时 / 止损补救) 需要它
    exit_profit_only = False
    ignore_roi_if_entry_signal = False
    stoploss = -0.50  # 兜底 (2 倍杠杆 = 价格反向 25%), 成交后换成 ATR 止损

    # ---------------- 参数 (3 个, 冻结, 不调) ----------------
    z_threshold = 3.0
    window_bars = 720
    max_hold_bars = 48

    # ---------------- 固定常量 (冻结) ----------------
    prem_mean_bars = 8
    mad_scale = 1.4826
    spot_volume_bars = 24
    spot_min_quote_volume = 1_000_000.0  # USDT
    spot_present_bars = 8
    cooldown_candles = 24
    spot_timeframe = "1h"
    spot_candle_type = "spot"

    # ---------------- 风控 (easytrade_risk) ----------------
    atr_period = 14
    atr_column = "atr"
    stop_atr_mult = 2.0
    max_hold_candles = max_hold_bars
    # risk_per_trade 1% / max_portfolio_risk 6% / dd_throttle 8% / max_leverage_param 2: 用混入类默认值
    et_name = "premfade"

    plot_config = {
        "main_plot": {},
        "subplots": {
            "Premium": {"prem": {"color": "#bdbdbd"}, "prem8": {"color": "#2f80ed"}},
            "Z": {"pf_z": {"color": "#eb5757"}},
        },
    }

    @property
    def protections(self):
        return [{"method": "CooldownPeriod", "stop_duration_candles": int(self.cooldown_candles)}]

    # ======================================================================
    # 现货数据
    # ======================================================================

    def informative_pairs(self):
        # 实盘 / 模拟盘: 让 freqtrade 刷新每个币的现货 1h (回测不需要, dp 直接读磁盘: 现货文件在 datadir 根目录)
        if getattr(self, "dp", None) is None:
            return []
        return [(spot_pair_for(p), self.spot_timeframe, self.spot_candle_type) for p in self.dp.current_whitelist()]

    def _log_once(self, key: tuple, level: int, msg: str, *args: Any) -> None:
        seen = self.__dict__.setdefault("_pf_logged", set())
        if key in seen:
            return
        if len(seen) > 5000:
            seen.clear()
        seen.add(key)
        logger.log(level, msg, *args)

    def _load_spot(self, pair: str) -> DataFrame | None:
        """这个币的现货 1h K线 (date / close / volume); 没有数据或出错 = None (这个币不开仓)。"""
        spot = spot_pair_for(pair)
        if getattr(self, "dp", None) is None:
            return None
        try:
            df = self.dp.get_pair_dataframe(pair=spot, timeframe=self.spot_timeframe, candle_type=self.spot_candle_type)
        except Exception as exc:  # noqa: BLE001 — 读不到就当没有数据, 不开仓
            self._log_once((pair, "spot_err"), logging.WARNING,
                           "%s %s %s: 读取现货 %s %s 失败, 这个币不开仓: %s",
                           SPOT_MISSING_TAG, self.et_name, pair, spot, self.spot_timeframe, exc)
            return None
        if df is None or df.empty or not {"date", "close", "volume"}.issubset(df.columns):
            self._log_once((pair, "spot_none"), logging.WARNING,
                           "%s %s %s: 没有现货 %s %s 数据, 这个币一笔都不会开 (要先下载现货: "
                           "freqtrade download-data --trading-mode spot --pairs %s --timeframes 1h)",
                           SPOT_MISSING_TAG, self.et_name, pair, spot, self.spot_timeframe, spot)
            return None
        return df

    # ======================================================================
    # 指标
    # ======================================================================

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        pair = metadata["pair"]
        n = len(dataframe)
        dataframe["atr"] = atr(dataframe, self.atr_period)
        cols = {c: np.full(n, np.nan) for c in ("spot_close", "spot_qv24", "spot_present8", "prem", "prem8", "pf_z")}
        spot = self._load_spot(pair)
        if spot is not None and n > 0:
            idx = epoch_seconds(dataframe["date"])
            s_idx = epoch_seconds(spot["date"])
            sc = pd.Series(pd.to_numeric(spot["close"], errors="coerce").to_numpy(dtype="float64"), index=s_idx)
            sv = pd.Series(pd.to_numeric(spot["volume"], errors="coerce").to_numpy(dtype="float64"), index=s_idx)
            keep = ~sc.index.duplicated(keep="last")
            sc, sv = sc[keep], sv[keep]
            # 精确对齐: 只取和永续同一个时间戳的现货K线, 没有就是空值 (不向前填充)
            s_close = sc.reindex(idx).to_numpy(dtype="float64")
            s_vol = sv.reindex(idx).to_numpy(dtype="float64")
            # 成交量 0 = freqtrade 补出来的假K线 (价格是上一根的收盘价) 或这一小时没有成交: 当成没有数据
            present = np.isfinite(s_close) & (s_close > 0) & np.isfinite(s_vol) & (s_vol > 0)
            s_close = np.where(present, s_close, np.nan)
            qv = pd.Series(np.where(present, s_vol * s_close, 0.0))
            pres = pd.Series(present.astype("float64"))
            perp_close = dataframe["close"].to_numpy(dtype="float64")
            with np.errstate(divide="ignore", invalid="ignore"):
                prem = pd.Series(perp_close / s_close - 1.0)
            prem8 = prem.rolling(int(self.prem_mean_bars), min_periods=int(self.prem_mean_bars)).mean()
            cols["spot_close"] = s_close
            cols["spot_qv24"] = qv.rolling(int(self.spot_volume_bars), min_periods=int(self.spot_volume_bars)).sum().to_numpy()
            cols["spot_present8"] = pres.rolling(int(self.spot_present_bars), min_periods=int(self.spot_present_bars)).sum().to_numpy()
            cols["prem"] = prem.to_numpy(dtype="float64")
            cols["prem8"] = prem8.to_numpy(dtype="float64")
            cols["pf_z"] = robust_z_past(cols["prem8"], int(self.window_bars), float(self.mad_scale))

            usable = self._data_ok(cols["spot_qv24"], cols["spot_present8"]) & np.isfinite(cols["pf_z"])
            last = int(idx[-1])
            self._log_once(
                (pair, "coverage", int(idx[0]), last if not self._et_is_live() else 0), logging.INFO,
                "%s %s %s: 现货 %s 1h 读到 %d 根; 永续 %d 根里和现货时间戳对上 (现货有成交) %d 根 (%.1f%%); "
                "数据条件都满足且 z 有值的K线 %d 根 (%s ~ %s)",
                SPOT_COVERAGE_TAG, self.et_name, pair, spot_pair_for(pair), len(spot), n, int(present.sum()),
                100.0 * float(present.sum()) / n, int(usable.sum()),
                pd.Timestamp(int(idx[0]), unit="s", tz="UTC"), pd.Timestamp(last, unit="s", tz="UTC"),
            )
        for c, v in cols.items():
            dataframe[c] = v
        return dataframe

    def _data_ok(self, qv24: np.ndarray, present8: np.ndarray) -> np.ndarray:
        """数据条件: 现货最近 24 根成交额 >= 100 万 USDT, 最近 8 根现货都有数据。NaN = 不满足。"""
        qv24 = np.asarray(qv24, dtype="float64")
        present8 = np.asarray(present8, dtype="float64")
        with np.errstate(invalid="ignore"):
            return (qv24 >= float(self.spot_min_quote_volume)) & (present8 >= int(self.spot_present_bars))

    # ======================================================================
    # 信号
    # ======================================================================

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        z = dataframe["pf_z"].to_numpy(dtype="float64")
        o = dataframe["open"].to_numpy(dtype="float64")
        c = dataframe["close"].to_numpy(dtype="float64")
        ok = self._data_ok(dataframe["spot_qv24"].to_numpy(), dataframe["spot_present8"].to_numpy())
        thr = float(self.z_threshold)
        with np.errstate(invalid="ignore"):  # NaN 比较 = False = 不开仓
            short_sig = ok & (z >= thr) & (c < o)
            long_sig = ok & (z <= -thr) & (c > o)
        dataframe["enter_long"] = long_sig.astype("int64")
        dataframe["enter_short"] = short_sig.astype("int64")
        tags = np.full(len(dataframe), None, dtype=object)
        tags[long_sig] = "pf_long"
        tags[short_sig] = "pf_short"
        dataframe["enter_tag"] = pd.Series(tags, index=dataframe.index, dtype=object)
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        # z 回到 0 (z 是空值时不出离场信号, 交给超时 / 止损)
        z = dataframe["pf_z"].to_numpy(dtype="float64")
        with np.errstate(invalid="ignore"):
            exit_short = z <= 0.0
            exit_long = z >= 0.0
        dataframe["exit_long"] = exit_long.astype("int64")
        dataframe["exit_short"] = exit_short.astype("int64")
        tags = np.full(len(dataframe), None, dtype=object)
        tags[exit_long | exit_short] = "pf_z_zero"
        dataframe["exit_tag"] = pd.Series(tags, index=dataframe.index, dtype=object)
        return dataframe

    # ======================================================================
    # 仓位: easytrade_risk 的按止损距离定仓; 另外记 "最小下单额" 日志 (和 XSMomentum 同一个标记)
    # ======================================================================

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
        stake = super().custom_stake_amount(
            pair=pair, current_time=current_time, current_rate=current_rate, proposed_stake=proposed_stake,
            min_stake=min_stake, max_stake=max_stake, leverage=leverage, entry_tag=entry_tag, side=side, **kwargs,
        )
        try:
            ms = positive_or_none(min_stake)
            if stake > 0 and ms is not None and stake < ms:
                seen = self.__dict__.setdefault("_pf_min_stake_seen", set())
                key = (pair, timeframe_to_prev_date(self.timeframe, current_time))
                if key not in seen:
                    if len(seen) > 2000:
                        seen.clear()
                    seen.add(key)
                    log = logger.warning if self._et_is_live() else logger.info
                    if stake * FT_MIN_STAKE_ADJUST < ms:
                        log("%s %s %s: 按风险算的保证金 %.2f USDT 比交易所最小下单额 %.2f USDT 小太多 (freqtrade 最多放大 30%%), "
                            "这一笔开不出来", MIN_STAKE_SKIP_TAG, self.et_name, pair, stake, ms)
                    else:
                        log("%s %s %s: 按风险算的保证金 %.2f USDT 比交易所最小下单额 %.2f USDT 小, freqtrade 会放大到 %.2f "
                            "(这一笔实际风险比设计的大, 最多 +30%%)", MIN_STAKE_BUMP_TAG, self.et_name, pair, stake, ms, ms)
        except Exception as exc:  # noqa: BLE001 — 只是记日志, 不能影响下单
            logger.debug("%s %s 检查最小下单额失败: %s", self.et_name, pair, exc)
        return stake
