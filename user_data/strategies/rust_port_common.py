"""
rust_port_common -- 从 Rust 信号引擎 (crypto-signal-engine) 移植到 freqtrade 的公共部分。

本文件不是策略, freqtrade 不会把它列为策略 (它没有继承 IStrategy)。
BigTrendShort / VolumeSpikeShort / DailyThreeSoldiersShort 三个策略都会 import 它,
这样风控 (杠杆上限、按止损距离算仓位、超时出场、止损价) 只写一份, 三个策略保持一致。
freqtrade 官方支持这种 "从同目录 import" 的写法 (docs: strategy-advanced.md#derived-strategies,
hyperopt 也会把基类所在模块一起打包, 见 hyperopt_optimizer.hyperopt_pickle_magic)。

对照的 Rust 源码 (只读参考, 均在 crypto-signal-engine 仓库):
- 指标:       crates/indicators/src/ema.rs, macd.rs (EMA = SMA 种子 + 递推, 与 talib 一致)
- 多周期:     crates/engine/src/mtf.rs (D1/H4/H1 偏向打分 + 按策略族的拦截/降仓)
- 出场模型:   crates/llm_trader/src/sim.rs (scan / timeout_secs / gap_fill)
- 回测口径:   crates/backtest/src/bin/signal_backtest.rs (入场=信号后下一根 open)
- 准入闸:     crates/llm_trader/src/edge_filter.rs (只做有 edge 的方向/周期)
- 实盘降仓:   crates/llm_trader/src/paper.rs (mtf_risk_block / mtf_size_mult)
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
from freqtrade.strategy import stoploss_from_absolute


logger = logging.getLogger(__name__)

# 杠杆硬上限: 不管策略参数、交易所允许多少, 永远不超过 5 倍 (项目风控规则)。
HARD_MAX_LEVERAGE = 5.0

# 止损补救的离场原因 (只会在实盘/模拟盘出现, 见 RustPortMixin.custom_exit)
EXIT_STOP_BREACHED = "rust_stop_breached"  # 止损价没能挂上, 价格已经越过它
EXIT_STOP_INVALID = "rust_stop_invalid"  # 按实际成交价算, 策略止损在错误一侧
RUST_EXIT_NOW_KEY = "rust_exit_now"  # trade custom data: 有值 = 立即平仓 (值就是离场原因)

# Rust engine_core::Severity 的等级 (consumer.rs::severity_rank 同一顺序)
SEVERITY_RANK = {"LOW": 1, "MEDIUM": 2, "HIGH": 3, "CRITICAL": 4}
SEVERITY_NAME = {v: k for k, v in SEVERITY_RANK.items()}

# Rust crates/engine/src/mtf.rs: D1_WEIGHT / H4_WEIGHT / H1_WEIGHT
MTF_FRAMES: tuple[tuple[str, float], ...] = (("1d", 0.50), ("4h", 0.35), ("1h", 0.15))

# 策略族 (mtf.rs::policy_for_strategy)
POLICY_TREND_BREAKOUT = "trend_breakout"  # big_trend, daily_three_soldiers
POLICY_CONFIRMATION = "confirmation"  # volume_spike

_EPOCH = pd.Timestamp("1970-01-01", tz="UTC")


# ---------------------------------------------------------------------------
# 纯函数 (向量化, 只用当前及更早的 K 线, 无未来函数)
# ---------------------------------------------------------------------------


def ema(series: pd.Series, period: int) -> np.ndarray:
    """EMA, 与 Rust StreamingEma 完全同口径。

    Rust: crates/indicators/src/ema.rs -- 前 period 根取 SMA 作种子, 之后
    ema = a*price + (1-a)*ema_prev, a = 2/(period+1); 注释写明 "完全复刻 talib 行为"。
    talib 的 Python 封装会跳过开头的 NaN, 所以也能直接用于 MACD 的信号线。
    """
    return talib.EMA(series.to_numpy(dtype="float64"), timeperiod=period)


def macd_12_26_9(close: pd.Series) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """MACD(12, 26, 9), 按 Rust StreamingMacd 的算法手工组装。

    Rust: crates/indicators/src/macd.rs -- macd = EMA12 - EMA26 (两条 EMA 各自从第 1 根开始
    SMA 种子), signal = 对 macd 序列再做 EMA9 (从第一个有效 macd 开始种子)。
    不直接用 talib.MACD, 因为 talib.MACD 对快线的起点做了对齐, 早期数值和 Rust 有细微差别。
    """
    fast = ema(close, 12)
    slow = ema(close, 26)
    line = fast - slow
    signal = talib.EMA(line, timeperiod=9)
    hist = line - signal
    return line, signal, hist


def body_ratio(df: DataFrame) -> pd.Series:
    """Rust: crates/core/src/bar.rs::Bar::body_ratio = |close-open| / (high-low), 振幅为 0 时取 0。"""
    rng = df["high"] - df["low"]
    ratio = (df["close"] - df["open"]).abs() / rng.where(rng > 0)
    return ratio.fillna(0.0)


def cooldown_mask(events: np.ndarray, dates: pd.Series, cooldown_seconds: int) -> np.ndarray:
    """复刻 Rust TimeDedup.allow 的冷却: 距上次 "放行" 不足 cooldown 秒的事件被拦掉。

    Rust: crates/strategies/src/dedupe.rs -- diff < min_bars * tf_seconds 则拒绝, 否则放行并记录。
    只依赖过去的事件, 无未来函数。返回与 events 同长度的 bool 数组。
    """
    allowed = np.zeros(len(events), dtype=bool)
    idx = np.flatnonzero(events)
    if idx.size == 0:
        return allowed
    secs = to_epoch_seconds(dates)
    last_ts: int | None = None
    for i in idx:
        t = int(secs[i])
        if last_ts is None or (t - last_ts) >= cooldown_seconds:
            allowed[i] = True
            last_ts = t
    return allowed


def to_epoch_seconds(dates: pd.Series) -> np.ndarray:
    """把 freqtrade 的 date 列转成整数秒 (不依赖 pandas 的 ns/ms 精度设置)。"""
    return ((pd.to_datetime(dates, utc=True) - _EPOCH) // pd.Timedelta(seconds=1)).to_numpy(
        dtype="int64"
    )


def classify_bias(df: DataFrame) -> np.ndarray:
    """单个周期的趋势偏向 (-2 强空 .. +2 强多), 逐根计算。

    Rust: crates/engine/src/mtf.rs::classify_bias, 判断顺序完全一致:
      有 EMA200: 强多 close>EMA20>EMA60>EMA200 / 强空 反之 / 偏多 close>EMA200 且 close>EMA60 /
                 偏空 反之 / 否则中性
      无 EMA200 (新币): close>EMA20 偏多 / close<EMA20 偏空 / 否则中性
    """
    close = df["close"].astype("float64")  # 空表 (object 类型) 时也不报错
    e20 = pd.Series(ema(close, 20), index=df.index)
    e60 = pd.Series(ema(close, 60), index=df.index)
    e200 = pd.Series(ema(close, 200), index=df.index)

    valid_close = close.notna() & (close > 0) & np.isfinite(close)
    has200 = e200.notna()
    has20 = e20.notna()
    has60 = e60.notna()

    strong_bull = has200 & has20 & has60 & (close > e20) & (e20 > e60) & (e60 > e200)
    strong_bear = has200 & has20 & has60 & (close < e20) & (e20 < e60) & (e60 < e200)
    bull = has200 & (close > e200) & (~has60 | (close > e60))
    bear = has200 & (close < e200) & (~has60 | (close < e60))
    fb_bull = ~has200 & has20 & (close > e20)
    fb_bear = ~has200 & has20 & (close < e20)

    codes = np.select(
        [strong_bull, strong_bear, bull, bear, fb_bull, fb_bear],
        [2.0, -2.0, 1.0, -1.0, 1.0, -1.0],
        default=0.0,
    )
    codes = np.where(valid_close, codes, 0.0)
    return codes


def merge_by_close_time(
    base: DataFrame, base_tf: str, inf: DataFrame, inf_tf: str, values: np.ndarray
) -> np.ndarray:
    """把另一个周期 (更高或更低都行) 的逐根数值, 对齐到 base 的每一根 K 线上。

    规则: 取 "收盘时间 <= base 这根收盘时间" 的最后一根 inf K 线 (backward as-of)。
    对更高周期, 这和 freqtrade 的 merge_informative_pair(ffill=True) 等价;
    对更低周期 (例如 1d 策略要看 4h/1h), merge_informative_pair 不支持, 所以手写。
    这对应 Rust mtf.rs::MtfContext::from_cache —— 信号产生时各周期缓存里 "最新已收盘" 的那根。
    无未来函数: 只会用到已收盘的 K 线。没有匹配时为 NaN (= Rust 里该周期 frame 缺失)。
    """
    out = np.full(len(base), np.nan)
    if inf is None or inf.empty or len(base) == 0:
        return out
    base_close = to_epoch_seconds(base["date"]) + timeframe_to_seconds(base_tf)
    inf_close = to_epoch_seconds(inf["date"]) + timeframe_to_seconds(inf_tf)
    order = np.argsort(inf_close, kind="stable")
    inf_close = inf_close[order]
    vals = np.asarray(values, dtype="float64")[order]
    pos = np.searchsorted(inf_close, base_close, side="right") - 1
    ok = pos >= 0
    out[ok] = vals[pos[ok]]
    return out


def mtf_alignment(bias_columns: list[tuple[np.ndarray, float]], direction: int) -> np.ndarray:
    """Rust mtf.rs::MtfContext::alignment: dir * sum(bias*w) / sum(w), 只算有数据的周期。"""
    n = len(bias_columns[0][0]) if bias_columns else 0
    weighted = np.zeros(n)
    weights = np.zeros(n)
    for codes, w in bias_columns:
        present = ~np.isnan(codes)
        weighted += np.where(present, codes * w, 0.0)
        weights += np.where(present, w, 0.0)
    with np.errstate(invalid="ignore", divide="ignore"):
        align = np.where(weights > 0, direction * weighted / weights, np.nan)
    return align


def mtf_policy(
    alignment: np.ndarray, policy: str
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Rust mtf.rs::apply_strategy_policy -> (score_delta, size_mult, risk_block)。

    TrendBreakout (big_trend / daily_three_soldiers):
        a<=-1.0: -25 分, 仓位 0, 拦截 | a<=-0.35: -12 分, 仓位 x0.5 | a>=1.0: +8 | a>=0.35: +4
    Confirmation (volume_spike):
        a>=1.0: +3 | a<=-1.0: -5 分, 仓位 x0.5 (不拦截)
    alignment 为 NaN (没有任何高周期数据) 时不做调整 (Rust: mtf_available=0)。
    """
    a = np.nan_to_num(alignment, nan=0.0)
    has = ~np.isnan(alignment)
    if policy == POLICY_TREND_BREAKOUT:
        conds = [has & (a <= -1.0), has & (a <= -0.35), has & (a >= 1.0), has & (a >= 0.35)]
        delta = np.select(conds, [-25.0, -12.0, 8.0, 4.0], default=0.0)
        mult = np.select(conds, [0.0, 0.5, 1.0, 1.0], default=1.0)
        block = has & (a <= -1.0)
    elif policy == POLICY_CONFIRMATION:
        conds = [has & (a >= 1.0), has & (a <= -1.0)]
        delta = np.select(conds, [3.0, -5.0], default=0.0)
        mult = np.select(conds, [1.0, 0.5], default=1.0)
        block = np.zeros(len(a), dtype=bool)
    else:  # "Other" 族: 本项目三个策略用不到, 保留以防扩展
        conds = [has & (a >= 1.0), has & (a <= -1.0)]
        delta = np.select(conds, [3.0, -5.0], default=0.0)
        mult = np.select(conds, [1.0, 0.75], default=1.0)
        block = np.zeros(len(a), dtype=bool)
    return delta, mult, block


def severity_for_score_mtf(score: np.ndarray) -> np.ndarray:
    """Rust mtf.rs::severity_for_score (MTF 调分后重新定级, 多了 CRITICAL 档)。"""
    return np.select([score >= 90, score >= 80, score >= 65], [4, 3, 2], default=1)


# ---------------------------------------------------------------------------
# 混入类: 三个策略共用的回调 (杠杆 / 仓位 / 止损 / 超时出场 / 多周期)
# ---------------------------------------------------------------------------


class RustPortMixin:
    """放在 IStrategy 前面继承: class XxxShort(RustPortMixin, IStrategy)。

    子类需要提供:
      - rust_name: Rust 策略名 (用于 entry tag)
      - mtf_policy_name: POLICY_TREND_BREAKOUT / POLICY_CONFIRMATION
      - disaster_stop_pct: 兜底止损 (价格距离), 也用来算仓位
      - _raw_stop_price(row, entry_rate, is_short) -> float | None: 策略自己的止损价
    """

    # ---------------- 方向开关 ----------------
    # edge_filter.rs: 这三个策略只有 SHORT 端有 edge, 所以默认只做空。
    # 以后行情转向想重测多头, 把 enable_long 改成 True 即可 (Rust 的多头逻辑都已一并移植)。
    enable_short: bool = True
    enable_long: bool = False

    # ---------------- 杠杆 ----------------
    # 实际杠杆 = min(max_leverage_param, 交易所该币允许的最大杠杆, 5)。永远 <= 5。
    max_leverage_param: float = 3.0

    # ---------------- 仓位 (按止损距离定仓) ----------------
    # 每笔交易: 若打到止损, 亏损约等于 "总资金 x risk_per_trade"。
    # 名义仓位 = 总资金 x risk_per_trade x MTF降仓系数 / (止损距离 + 成本缓冲); 保证金 = 名义 / 杠杆。
    risk_per_trade: float = 0.01
    # 手续费+滑点缓冲 (Rust sim: taker 5bps + 滑点 2bps, 每边; 往返约 0.14%)
    sizing_cost_buffer: float = 0.002
    # 止损特别近时, 按这个最小距离算仓位 (防止仓位被放得过大)
    min_stop_distance_for_sizing: float = 0.01
    # 单笔名义仓位上限 = 总资金 x 这个比例 (1.0 = 不超过 1 倍总资金)
    max_position_notional_pct: float = 1.0
    # 同时不超过 freqtrade 按 stake_amount / max_open_trades 给出的建议仓位 (防止一笔吃光保证金)
    cap_stake_by_proposed: bool = True
    # False = 不按风险定仓, 直接用 config 的 stake_amount (只给 RustBaselines.py 的 "Rust 口径对照" 用)
    use_risk_sizing: bool = True
    # 止损补救出场 (rust_stop_breached / rust_stop_invalid) 越过盘口多少挂单, 保证立即成交
    stop_rescue_exit_slippage: float = 0.005

    # ---------------- 只在信号 K 线刚收盘时入场 ----------------
    # 回测和 Rust signal-backtest 都只在 "信号 K 线收盘后的下一根开盘" 入场, 那一刻没有空名额就放弃这个信号。
    # freqtrade 实盘默认 (0 = 不限) 会在整根 K 线期间一直按这个信号入场 —— 1d 策略就是一整天:
    # 中途有名额空出来、机器人中途重启、或者限价单 10 分钟没成交被撤后再挂, 都会在当天任意时刻、任意价格入场,
    # 这些交易回测里根本不存在。设成 600 秒 (= unfilledtimeout.entry 10 分钟): 信号 K 线收盘 10 分钟后
    # 就不再入场, 和回测口径一致。对 5m 策略几乎没有影响 (5 分钟后已经是下一根 K 线的信号了)。
    # 用 --timeframe-detail 回测时 freqtrade 也会按这个值丢掉 "当天晚些时候" 的入场, 两边口径一致。
    ignore_buying_expired_candle_after: int = 600

    # ---------------- 信号过滤 ----------------
    # 是否启用 Rust mtf.rs 的多周期拦截/降仓 (2026-06-14 才加入 Rust, 6 月 90 天体检时还没有)。
    # 想复现 6 月体检口径就设为 False。
    use_mtf_policy: bool = True
    # 最低严重度 (LOW / MEDIUM / HIGH / CRITICAL)。6 月 signal-backtest 没有严重度过滤 = "LOW";
    # Rust 实盘 LLM 交易员的门槛是 MEDIUM (config.rs MIN_SEVERITY_LABEL)。
    min_severity: str = "LOW"

    # ---------------- 子类覆盖 ----------------
    rust_name: str = "rust"
    mtf_policy_name: str = POLICY_TREND_BREAKOUT
    disaster_stop_pct: float = 0.20

    # ======================================================================
    # 时间相关
    # ======================================================================

    def rust_timeout_seconds(self) -> int:
        """Rust sim.rs::timeout_secs: max(48 小时, 12 x 周期)。

        注意: 不是单纯的 12 x 周期。1d = 12 天; 4h = 48 小时; 5m/1m 也是 48 小时 (下限)。
        """
        return max(48 * 3600, 12 * timeframe_to_seconds(self.timeframe))

    def rust_hold_seconds(self) -> int:
        """实际持仓时长 = timeout + 1 根 K 线。

        Rust signal_backtest.rs 用 take_while(open_time <= opened_at + cap) 截取前向 K 线,
        会把 "恰好在截止时刻开盘" 的那根也包含进来, 并在它收盘时按 timeout 出场
        (progress.md: 1d 交易出现 bars_held=13)。这里照搬: 1d 实际持有 13 天。
        """
        return self.rust_timeout_seconds() + timeframe_to_seconds(self.timeframe)

    # ======================================================================
    # 多周期 (MTF) —— Rust crates/engine/src/mtf.rs
    # ======================================================================

    def mtf_informative_pairs(self) -> list[tuple[str, str]]:
        if not self.use_mtf_policy or self.dp is None:
            return []
        pairs = self.dp.current_whitelist()
        return [(p, tf) for p in pairs for tf, _ in MTF_FRAMES if tf != self.timeframe]

    def add_mtf_columns(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        """给每根 K 线写上 D1/H4/H1 偏向 (mtf_bias_1d / 4h / 1h, 缺失为 NaN)。"""
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
                except Exception as exc:  # 数据缺失时降级为 "该周期无数据"
                    logger.warning("MTF %s %s 数据读取失败: %s", metadata["pair"], tf, exc)
                    inf = None
            if inf is None or inf.empty:
                dataframe[col] = np.nan
                continue
            codes = classify_bias(inf)
            dataframe[col] = merge_by_close_time(dataframe, self.timeframe, inf, tf, codes)
        return dataframe

    def apply_mtf_and_severity(
        self,
        dataframe: DataFrame,
        signal: np.ndarray,
        score: np.ndarray,
        severity: np.ndarray,
        direction: int,
    ) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
        """对一个方向的信号套 MTF 调分/拦截 + 严重度门槛。

        返回 (最终可入场 mask, 最终分数, 最终严重度, 仓位系数, 对齐度)。
        """
        cols = [(dataframe[f"mtf_bias_{tf}"].to_numpy(dtype="float64"), w) for tf, w in MTF_FRAMES]
        align = mtf_alignment(cols, direction) if self.use_mtf_policy else np.full(len(score), np.nan)
        delta, mult, block = mtf_policy(align, self.mtf_policy_name)
        changed = np.abs(delta) > 1e-6
        final_score = np.where(changed, np.clip(score + delta, 0.0, 100.0), score)
        final_sev = np.where(changed, severity_for_score_mtf(final_score), severity)
        min_rank = SEVERITY_RANK.get(str(self.min_severity).upper(), 1)
        ok = signal & ~block & (final_sev >= min_rank)
        return ok, final_score, final_sev, mult, align

    def write_entry_columns(
        self,
        dataframe: DataFrame,
        side: str,
        ok: np.ndarray,
        score: np.ndarray,
        severity: np.ndarray,
        size_mult: np.ndarray,
        align: np.ndarray,
        tag_extra: np.ndarray | None = None,
    ) -> None:
        """写 enter_short/enter_long + enter_tag + 供回调读取的辅助列。"""
        enter_col = "enter_short" if side == "short" else "enter_long"
        dataframe[enter_col] = ok.astype("int64")
        dataframe[f"rust_score_{side}"] = score
        dataframe[f"rust_severity_{side}"] = severity
        dataframe[f"mtf_size_mult_{side}"] = size_mult
        dataframe[f"mtf_alignment_{side}"] = align
        if "enter_tag" in dataframe.columns:
            # copy=True: pandas 3 (copy-on-write) 返回的是只读视图
            tags_arr = dataframe["enter_tag"].to_numpy(dtype=object, copy=True)
        else:
            tags_arr = np.full(len(dataframe), None, dtype=object)
        if ok.any():
            # 例: big_trend_short_x4_high = big_trend 做空, 穿 4 条 EMA, 严重度 HIGH
            idx = np.flatnonzero(ok)
            base = f"{self.rust_name}_{side}"
            for i in idx:
                sev = SEVERITY_NAME.get(int(severity[i]), "LOW").lower()
                tags_arr[i] = f"{base}_{tag_extra[i]}_{sev}" if tag_extra is not None else f"{base}_{sev}"
        dataframe["enter_tag"] = pd.Series(tags_arr, index=dataframe.index, dtype=object)

    # ======================================================================
    # 止损价 (绝对价格)
    # ======================================================================

    def _raw_stop_price(self, row: pd.Series, entry_rate: float, is_short: bool) -> float | None:
        """子类实现: 返回策略自己的止损价; None = 该模式下没有结构性止损 (只用兜底止损)。"""
        return None

    def resolve_stop_price(
        self, row: pd.Series | None, entry_rate: float, is_short: bool, leverage: float
    ) -> float | None:
        """最终止损价 = 策略止损 与 兜底止损 中更近 (更紧) 的那个。

        - 兜底止损 = 入场价 +/- min(disaster_stop_pct, |stoploss| / 杠杆)。
          freqtrade 期货的 stoploss 是 "账户口径" (已乘杠杆), 换成价格距离要除以杠杆。
        - 策略止损在错误一侧 (做空却低于入场价) 时返回 None -> 不开仓。
          Rust sim 遇到这种情况会在入场那根立即按开盘价止损出场 (毛利 0, 只亏手续费),
          这里直接跳过, 差异只是省掉一次往返手续费。
        """
        if not entry_rate or entry_rate <= 0 or not math.isfinite(entry_rate):
            return None
        lev = max(1.0, float(leverage or 1.0))
        cap = min(float(self.disaster_stop_pct), abs(float(self.stoploss)) / lev * 0.999)
        disaster = entry_rate * (1 + cap) if is_short else entry_rate * (1 - cap)

        raw = None
        if row is not None:
            try:
                raw = self._raw_stop_price(row, entry_rate, is_short)
            except Exception as exc:
                logger.warning("%s 计算策略止损失败, 只用兜底止损: %s", self.rust_name, exc)
                raw = None
        if raw is None or not math.isfinite(raw) or raw <= 0:
            return disaster
        if is_short and raw <= entry_rate:
            return None
        if (not is_short) and raw >= entry_rate:
            return None
        return min(raw, disaster) if is_short else max(raw, disaster)

    # ======================================================================
    # 读取信号 K 线
    # ======================================================================

    def _last_signal_row(self, pair: str) -> pd.Series | None:
        """入场时: get_analyzed_dataframe 的最后一行就是触发信号的那根已收盘 K 线。

        (回测中 freqtrade 会把 dataframe 截到当前时刻之前, 见 backtesting.py
        _set_dataframe_max_index; 实盘中最后一行是刚收盘的那根。)
        """
        if self.dp is None:
            return None
        df, _ = self.dp.get_analyzed_dataframe(pair, self.timeframe)
        if df is None or df.empty:
            return None
        return df.iloc[-1]

    def _signal_row_for_trade(self, trade: Trade) -> pd.Series | None:
        """持仓中: 找到开仓所依据的那根信号 K 线 (= 开仓所在 K 线的前一根)。"""
        if self.dp is None:
            return None
        df, _ = self.dp.get_analyzed_dataframe(trade.pair, self.timeframe)
        if df is None or df.empty:
            return None
        entry_candle = timeframe_to_prev_date(self.timeframe, trade.open_date_utc)
        signal_date = entry_candle - timedelta(seconds=timeframe_to_seconds(self.timeframe))
        dates = pd.to_datetime(df["date"], utc=True)
        hit = df.loc[dates == pd.Timestamp(signal_date)]
        if hit.empty:
            return None
        return hit.iloc[-1]

    # ======================================================================
    # freqtrade 回调
    # ======================================================================

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
        """杠杆 = min(策略参数 默认3, 交易所允许的最大值, 5), 且 >= 1。永远不会超过 5 倍。"""
        lev = min(float(self.max_leverage_param), float(max_leverage), HARD_MAX_LEVERAGE)
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
        """按止损距离定仓: 打到止损时亏损 ≈ 总资金 x risk_per_trade (x MTF 降仓系数)。

        返回 0 = 不开这笔 (freqtrade 会跳过)。任何异常也返回 0 —— 绝不在出错时用默认满仓。
        """
        try:
            is_short = side == "short"
            row = self._last_signal_row(pair)
            stop = self.resolve_stop_price(row, current_rate, is_short, leverage)
            if stop is None:
                logger.info(
                    "%s %s: 止损价在错误一侧/无效 (Rust 会立即按开盘价止损), 跳过本次入场",
                    self.rust_name,
                    pair,
                )
                return 0.0
            dist = abs(stop - current_rate) / current_rate
            dist_for_sizing = max(dist, float(self.min_stop_distance_for_sizing))
            dist_for_sizing += float(self.sizing_cost_buffer)

            size_mult = 1.0
            if row is not None:
                v = row.get(f"mtf_size_mult_{side}", 1.0)
                if v is not None and math.isfinite(float(v)):
                    size_mult = min(max(float(v), 0.0), 1.0)
            if size_mult <= 0:
                return 0.0
            if not self.use_risk_sizing:
                # Rust signal-backtest 口径: 每笔固定金额 (--stake-amount), 只乘 MTF 降仓系数
                return max(min(float(proposed_stake) * size_mult, float(max_stake)), 0.0)

            equity = (
                float(self.wallets.get_total_stake_amount()) if self.wallets else proposed_stake
            )
            risk_amount = equity * float(self.risk_per_trade) * size_mult
            notional = risk_amount / dist_for_sizing
            notional = min(notional, equity * float(self.max_position_notional_pct))
            lev = max(1.0, float(leverage or 1.0))
            stake = notional / lev
            if self.cap_stake_by_proposed and proposed_stake:
                stake = min(stake, float(proposed_stake))
            stake = min(stake, float(max_stake))
            return max(stake, 0.0)
        except Exception as exc:
            logger.error("%s custom_stake_amount 出错, 跳过入场: %s", self.rust_name, exc)
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
        """成交后把止损设到策略止损价 (绝对价格), 之后固定不动 (Rust: trailing/保本 默认关闭)。

        - 止损价只算一次, 存在 trade 的 custom data 里 (实盘重启后也还在)。
        - after_fill=True (成交回调) 时设置; 之后每次检查: 如果当前止损比它宽 (例如机器人重启后
          freqtrade 把止损重置成了 stoploss 兜底值), 就重新收紧回来; 否则返回 None = 不改动。
        """
        stop = trade.get_custom_data("rust_stop_price")
        if stop is None:
            row = self._signal_row_for_trade(trade)
            stop = self.resolve_stop_price(row, trade.open_rate, trade.is_short, trade.leverage)
            if stop is None:
                # 按实际成交价算, 策略止损已经在错误一侧 (只有实盘/模拟盘里成交价和下单时的价格不同才会
                # 出现; 回测里 custom_stake_amount 已经拦掉, 走不到这里)。Rust sim 的做法是入场那根立即止损,
                # 这里同样【立即平仓】: 打上标记, custom_exit 马上按离场原因 rust_stop_invalid 平掉。
                # 平掉之前先挂兜底止损当保险 (不能拿它当正式止损: 仓位是按更近的策略止损算的)。
                trade.set_custom_data(RUST_EXIT_NOW_KEY, EXIT_STOP_INVALID)
                cap = min(
                    float(self.disaster_stop_pct),
                    abs(float(self.stoploss)) / max(1.0, trade.leverage or 1.0) * 0.999,
                )
                stop = trade.open_rate * (1 + cap) if trade.is_short else trade.open_rate * (1 - cap)
            trade.set_custom_data("rust_stop_price", float(stop))
        stop = float(stop)

        current_stop = trade.stop_loss
        if current_stop and abs(current_stop - stop) <= stop * 1e-6:
            # 已经在目标止损价上 -> 不改动。
            # 说明: freqtrade 回测在成交后会按重算的开仓均价把止损挪动约 1e-12 的距离
            # (backtesting._enter_trade -> recalc_trade_from_orders), 于是大约一半的止损单在报告里
            # 显示为 "trailing_stop_loss"。在这几个策略里它和 "stop_loss" 是同一个固定止损,
            # 统计时合并看即可。(回调拿到的 trade 是副本, 策略无法改掉这个标记。)
            if not after_fill:
                return None
        looser = (
            not current_stop
            or (trade.is_short and current_stop > stop)
            or ((not trade.is_short) and current_stop < stop)
        )
        if not (after_fill or looser):
            return None
        sl = stoploss_from_absolute(
            stop, current_rate, is_short=trade.is_short, leverage=trade.leverage
        )
        if not sl:
            # 当前价已经越过止损价 (实盘里成交回调拿到的是盘口价, 可能已经跳过了很近的止损)。
            # stoploss_from_absolute 这时返回 0, 而 freqtrade 会把 0 当成 "没返回" 丢掉, 止损就一直
            # 停在很宽的兜底价上。这种情况由 custom_exit 的 rust_stop_breached 立即平仓, 这里不改止损。
            return None
        return sl

    def _stop_breach_reason(self, trade: Trade, current_rate: float) -> str | None:
        """止损价没能生效、价格却已经越过它时, 返回要立即平仓的离场原因; 否则 None。

        只在实盘/模拟盘会出现 (见 custom_stoploss 的说明)。回测里成交回调用的是入场价, 止损总能设上,
        之后由 freqtrade 正常止损处理, 这里永远返回 None (回测结果不受影响)。
        """
        flag = trade.get_custom_data(RUST_EXIT_NOW_KEY)
        if flag:
            return str(flag)
        stop = trade.get_custom_data("rust_stop_price")
        if stop is None or not current_rate:
            return None
        stop = float(stop)
        active = trade.stop_loss
        if active:
            # 当前止损已经等于 (或比) 策略止损价 (更紧): 交给 freqtrade 正常止损 / 交易所止损单, 这里不管
            if trade.is_short and active <= stop * (1 + 1e-6):
                return None
            if (not trade.is_short) and active >= stop * (1 - 1e-6):
                return None
        crossed = current_rate >= stop if trade.is_short else current_rate <= stop
        return EXIT_STOP_BREACHED if crossed else None

    def custom_exit(
        self,
        pair: str,
        trade: Trade,
        current_time: datetime,
        current_rate: float,
        current_profit: float,
        **kwargs: Any,
    ) -> str | None:
        """两种出场 (需要 use_exit_signal = True 才会被 freqtrade 调用):

        1. 止损补救 (rust_stop_breached / rust_stop_invalid): 见 _stop_breach_reason。
           仓位是按策略止损算的, 止损没生效就不能继续拿着, 立即平仓。
        2. 超时出场 (rust_timeout): 持有满 rust_hold_seconds 后平仓 (Rust sim.rs 的 timeout)。
           回测中会在那根 K 线的开盘价成交 (= 上一根的收盘价), 与 Rust "第 13 根收盘出场" 一致。
        """
        reason = self._stop_breach_reason(trade, current_rate)
        if reason:
            if reason == EXIT_STOP_INVALID:
                why = f"按实际成交价 {trade.open_rate} 算, 策略止损在错误一侧"
            else:
                why = f"止损价 {trade.get_custom_data('rust_stop_price')} 没能生效且价格已越过"
            logger.warning(
                "%s %s: %s (当前价 %s), 立即平仓, 离场原因 %s",
                self.rust_name, pair, why, current_rate, reason,
            )
            return reason
        held = (current_time - trade.open_date_utc).total_seconds()
        if held >= self.rust_hold_seconds():
            return "rust_timeout"
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
        """止损补救出场用 "能立即成交的限价": 越过盘口 0.5% 挂单 (相当于带保护价的市价单)。

        配置里平仓是限价单、挂在盘口同侧 (做空平仓 = 挂在买一), 价格继续往不利方向走时可能一直不成交;
        止损补救必须马上成交, 所以只对这两个离场原因改价。freqtrade 会再把价格限制在建议价 ±2% 以内
        (custom_price_max_distance_ratio)。其他离场 (rust_timeout 等) 价格不变, 回测结果不受影响。
        """
        if exit_tag in (EXIT_STOP_BREACHED, EXIT_STOP_INVALID) and proposed_rate:
            slip = float(self.stop_rescue_exit_slippage)
            return proposed_rate * (1 + slip) if trade.is_short else proposed_rate * (1 - slip)
        return proposed_rate
