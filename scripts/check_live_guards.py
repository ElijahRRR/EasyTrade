#!/usr/bin/env python3
"""
实盘/模拟盘专用保护逻辑的自检（回归测试）。不联网、不需要K线数据，几秒钟跑完。

为什么需要它：下面这些情况只会在实盘/模拟盘出现，回测永远测不到，所以单独写成测试。
  1. 止损补救：成交回调时盘口价已经越过很近的策略止损价 → freqtrade 不会设止损，
     仓位只剩很宽的兜底止损（三连阴是 20%）。策略必须立即平仓（离场原因 rust_stop_breached）。
  2. 按实际成交价算，策略止损在错误一侧 → 立即平仓（离场原因 rust_stop_invalid），不能退回兜底止损。
  3. 止损补救用"能立即成交的限价"（越过盘口 0.5%），其他离场（rust_timeout）价格不变。
  4. 正常情况（止损已经设好）不受影响：止损价被碰到时仍然是 freqtrade 的普通止损（stop_loss）。
  5. 日线信号只在信号K线收盘后 10 分钟内入场（ignore_buying_expired_candle_after = 600），
     和回测"下一根开盘入场、没名额就放弃"一致。
  6. K线成交量单位：OKX 永续的 volume 是币的数量（不是张数），成交额 = 收盘价 x volume；
     具体检查在 scripts/check_volume_units.py（也可以单独运行，见 docs/okx-volume-units.md）。
  7. 第 2 步新策略共用的风控 easytrade_risk.py（EasyTradeRiskMixin）：杠杆永远 <= 5 倍、
     信号收盘 10 分钟后不再入场、按止损距离定仓（打到止损约亏 1%）、止损挂不上 / 止损在错误一侧时立即平仓。
     风险预算默认值跟着账户回撤上限（2026-11-07 起 15%，以前 8%）：每笔 1%、组合热度 6%、回撤 8% 起新仓减半
     （以前 0.5% / 3% / 4%）；BigTrendShort 自己的 1% 不变，BigTrendShortLowRisk 是它的一半（0.5%）。
  8. 第 3 轮截面动量 XSMomentum 的实盘排名：00:00 UTC 刚过时个别币的最新日线晚到一两轮（freqtrade 2026.10 会先丢掉
     收盘 15 秒内拿到的K线、拉取失败的币下一轮再拉）。不能拿着少一个币的排名开仓（别的币名次全错一位），
     等它到了以后所有币要用完整的排名重算；超过 300 秒还没到就按其余的币排名。
     另外：吊灯止损在初始止损被 25% 上限截短时按比例收近（组合热度不超过开仓风险的约 4/3 倍）、
     仓位低于交易所最小下单额时记日志（研究报告靠它数"开不出来"的次数）。

用法：
  .venv/bin/python scripts/check_live_guards.py
  make guard-test
退出码：0 = 全部通过；1 = 有检查未通过。
"""

from __future__ import annotations

import logging
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR / "user_data" / "strategies"))

import pandas as pd  # noqa: E402
from freqtrade.enums import ExitType, RunMode  # noqa: E402
from freqtrade.persistence import LocalTrade  # noqa: E402
from freqtrade.persistence.custom_data import CustomDataWrapper  # noqa: E402
from freqtrade.util import dt_now  # noqa: E402

import check_drawdown  # noqa: E402  （同目录：回撤上限 DEFAULT_MAX_DD_PCT）
import easytrade_risk  # noqa: E402
from BigTrendBoth import BigTrendBoth  # noqa: E402
from BigTrendShort import BigTrendShort  # noqa: E402
from BigTrendShortLowRisk import BigTrendShortLowRisk  # noqa: E402
from DailyThreeSoldiersShort import DailyThreeSoldiersShort  # noqa: E402
from DonchianTrend import DonchianTrend  # noqa: E402
from FundingContrarian import FundingContrarian  # noqa: E402
from RangeMeanReversion import RangeMeanReversion  # noqa: E402
from TsmomTrend import TsmomTrend  # noqa: E402
from VolSqueezeBreakout import VolSqueezeBreakout  # noqa: E402
from VolumeSpikeShort import VolumeSpikeShort  # noqa: E402
from XSMomentum import MIN_STAKE_BUMP_TAG, MIN_STAKE_SKIP_TAG, XSMomentum  # noqa: E402


STEP2_STRATEGIES = (DonchianTrend, TsmomTrend, BigTrendBoth, RangeMeanReversion, VolSqueezeBreakout, FundingContrarian)

CustomDataWrapper.use_db = False  # custom data 只放内存，不碰数据库
# 策略在补救平仓时会写一条 WARNING 日志（实盘里有用），自检时不显示，免得和检查结果混在一起
logging.getLogger("rust_port_common").setLevel(logging.ERROR)
logging.getLogger("easytrade_risk").setLevel(logging.ERROR)
logging.getLogger("XSMomentum").setLevel(logging.ERROR)
FAILS: list[str] = []
T0 = datetime(2026, 1, 2, tzinfo=UTC)
_TRADE_ID = [0]


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'通过' if ok else '失败'}  {name}" + (f"（{detail}）" if detail else ""))
    if not ok:
        FAILS.append(name)


def make_strategy(cls):
    cfg = {
        "runmode": RunMode.DRY_RUN,
        "dry_run": True,
        "stake_currency": "USDT",
        "trading_mode": "futures",
        "margin_mode": "isolated",
        "candle_type_def": "futures",
        "exchange": {},
    }
    s = cls(cfg)
    # 下面两项是 freqtrade 加载策略时 (StrategyResolver) 会做的处理
    s._ft_stop_uses_after_fill = True  # custom_stoploss 带 after_fill 参数
    s.minimal_roi = {int(k): v for k, v in s.minimal_roi.items()}  # ROI 表的键转成整数分钟
    s.dp = None
    return s


def make_short(stop: float | None, open_rate: float = 100.0) -> LocalTrade:
    _TRADE_ID[0] += 1
    t = LocalTrade(
        id=_TRADE_ID[0],
        pair=f"T{_TRADE_ID[0]}/USDT:USDT",
        is_short=True,
        leverage=3.0,
        open_rate=open_rate,
        amount=7.0,
        stake_amount=233.3,
        open_date=T0,
        fee_open=0.0005,
        fee_close=0.0005,
        exchange="okx",
        trading_mode="futures",
        is_open=True,
    )
    if stop is not None:
        t.set_custom_data("rust_stop_price", stop)
    return t


def after_fill(s, trade: LocalTrade, rate: float) -> None:
    s.ft_stoploss_adjust(
        rate, trade, T0 + timedelta(seconds=5), trade.calc_profit_ratio(rate), 0, after_fill=True
    )


def exits(s, trade: LocalTrade, rate: float, minutes: float = 1.0) -> list:
    return s.should_exit(trade, rate, T0 + timedelta(minutes=minutes), enter=False, exit_=False)


def first_reason(found: list) -> str:
    if not found:
        return "（无）"
    e = found[0]
    return e.exit_reason or e.exit_type.value


def test_stop_rescue() -> None:
    print("1) 止损补救（三连阴：做空 100 开仓，策略止损 100.5，3 倍杠杆）")
    s = make_strategy(DailyThreeSoldiersShort)

    # 1a 成交回调时买一已经到 100.8，越过了 100.5
    tr = make_short(100.5)
    after_fill(s, tr, 100.8)
    check(
        "越过止损时 freqtrade 不会把止损设到 100.5（这是要补救的情况）",
        tr.stop_loss > 100.5 * 1.001,
        f"当前止损 {tr.stop_loss:.4g}",
    )
    found = exits(s, tr, 100.8)
    check(
        "下一轮检查立即平仓，离场原因 rust_stop_breached",
        bool(found) and found[0].exit_type == ExitType.CUSTOM_EXIT
        and found[0].exit_reason == "rust_stop_breached",
        first_reason(found),
    )
    price = s.custom_exit_price(
        pair=tr.pair, trade=tr, current_time=T0, proposed_rate=100.8,
        current_profit=0.0, exit_tag="rust_stop_breached",
    )
    check("补救单挂在盘口上方 0.5%（做空平仓=买入，能立即成交）", abs(price - 100.8 * 1.005) < 1e-9,
          f"{price:.6g}")

    # 1b 对照组：成交回调时买一 99.98，没越过 → 止损正常设好，之后碰到 100.5 是普通止损
    tr2 = make_short(100.5)
    after_fill(s, tr2, 99.98)
    check("对照组：止损正常设到 100.5", abs(tr2.stop_loss - 100.5) < 1e-6, f"{tr2.stop_loss:.6g}")
    check("对照组：价格 100.2（没碰止损）不平仓", exits(s, tr2, 100.2) == [])
    found = exits(s, tr2, 100.6)
    check(
        "对照组：价格 100.6 碰到止损 → 仍是 freqtrade 普通止损 stop_loss（不是补救单）",
        bool(found) and found[0].exit_type == ExitType.STOP_LOSS,
        first_reason(found),
    )

    # 1c 越过之后价格又回到止损价下方：止损重新设上，不平仓
    tr3 = make_short(100.5)
    after_fill(s, tr3, 100.8)
    found = exits(s, tr3, 100.3)
    check("越过后价格回到 100.3：不平仓", found == [], first_reason(found))
    check("越过后价格回到 100.3：止损重新设到 100.5", abs(tr3.stop_loss - 100.5) < 1e-6,
          f"{tr3.stop_loss:.6g}")

    # 1d 其他离场原因不改价
    price = s.custom_exit_price(
        pair=tr2.pair, trade=tr2, current_time=T0, proposed_rate=101.0,
        current_profit=0.0, exit_tag="rust_timeout",
    )
    check("rust_timeout 的平仓价不变", price == 101.0, f"{price}")


def test_stop_invalid() -> None:
    print("2) 按实际成交价算，策略止损在错误一侧")
    s = make_strategy(DailyThreeSoldiersShort)
    s._signal_row_for_trade = lambda trade: pd.Series({"b1_low": 99.5, "b1_high": 101.5})
    # stop_mode="rust": 止损 = 第 1 根阴线（b1）的最低价 99.5；做空实际成交在 100.0（下单时价格还在 99.5 下方）
    # → 做空的止损应该在入场价上方，99.5 却在下方 = 错误一侧
    tr = make_short(None, open_rate=100.0)
    after_fill(s, tr, 100.0)
    found = exits(s, tr, 100.0)
    check(
        "立即平仓，离场原因 rust_stop_invalid（不退回 20% 兜底止损继续持有）",
        bool(found) and found[0].exit_reason == "rust_stop_invalid",
        first_reason(found),
    )
    check("平掉之前先挂着兜底止损当保险（约 120）", 119.0 < tr.stop_loss < 120.01, f"{tr.stop_loss:.6g}")


def _signal_frame(timeframe_minutes: int, close_age: timedelta) -> pd.DataFrame:
    """最后一根K线在 close_age 之前收盘，并且带做空信号。"""
    now = dt_now()
    last_open = now - close_age - timedelta(minutes=timeframe_minutes)
    dates = [last_open - timedelta(minutes=timeframe_minutes * i) for i in (2, 1, 0)]
    return pd.DataFrame(
        {
            "date": pd.to_datetime(dates, utc=True),
            "open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0, "volume": 1.0,
            "enter_long": 0, "exit_long": 0, "enter_short": [0, 0, 1], "exit_short": 0,
            "enter_tag": [None, None, "test"],
        }
    )


def test_expired_entry() -> None:
    print("3) 只在信号K线收盘后 10 分钟内入场")
    for cls in (BigTrendShort, DailyThreeSoldiersShort, VolumeSpikeShort):
        s = make_strategy(cls)
        check(f"{cls.__name__}.ignore_buying_expired_candle_after = 600",
              s.ignore_buying_expired_candle_after == 600, str(s.ignore_buying_expired_candle_after))
    s = make_strategy(BigTrendShort)
    sig, _ = s.get_entry_signal("BTC/USDT:USDT", "1d", _signal_frame(1440, timedelta(minutes=5)))
    check("日线信号收盘 5 分钟后：仍然入场", sig is not None, str(sig))
    sig, _ = s.get_entry_signal("BTC/USDT:USDT", "1d", _signal_frame(1440, timedelta(minutes=11)))
    check("日线信号收盘 11 分钟后（例如限价单超时被撤、或中途有名额空出）：不再入场", sig is None, str(sig))
    sig, _ = s.get_entry_signal("BTC/USDT:USDT", "1d", _signal_frame(1440, timedelta(hours=8)))
    check("日线信号收盘 8 小时后（例如机器人中途重启）：不再入场", sig is None, str(sig))


def test_volume_units() -> None:
    from check_volume_units import run_checks  # 同目录的 scripts/check_volume_units.py

    print("4) K线成交量单位（OKX 永续 volume = 币的数量，不是张数）")
    run_checks(check, prefix="4.")


class _FakeDP:
    """只提供 EasyTradeRiskMixin 用到的两样：运行模式、分析好的 K 线（最后一行 = 最近一根已收盘 K 线）。"""

    runmode = RunMode.DRY_RUN

    def __init__(self, df: pd.DataFrame) -> None:
        self.df = df

    def get_analyzed_dataframe(self, pair: str, timeframe: str):
        return self.df, self.df["date"].iloc[-1]


class _FakeWallets:
    def get_total_stake_amount(self) -> float:
        return 1000.0


def _risk_strategy(signal_row: dict) -> DonchianTrend:
    """DonchianTrend（日线）+ 一根信号 K 线（开仓前一天）。ATR 2、离场通道 90/110 -> 做多初始止损 = 100 - 2 x 2 = 96。"""
    s = make_strategy(DonchianTrend)
    row = {"date": pd.Timestamp(T0 - timedelta(days=1)), "open": 99.0, "high": 101.0, "low": 98.0, "close": 100.0,
           "volume": 1.0, "atr": 2.0, "dc_exit_low": 90.0, "dc_exit_high": 110.0}
    row.update(signal_row)
    s.dp = _FakeDP(pd.DataFrame([row]))
    s.wallets = _FakeWallets()
    return s


def make_long(open_rate: float = 100.0, leverage: float = 2.0) -> LocalTrade:
    _TRADE_ID[0] += 1
    return LocalTrade(
        id=_TRADE_ID[0], pair=f"E{_TRADE_ID[0]}/USDT:USDT", is_short=False, leverage=leverage,
        open_rate=open_rate, amount=1.0, stake_amount=open_rate / leverage, open_date=T0,
        fee_open=0.0005, fee_close=0.0005, exchange="okx", trading_mode="futures", is_open=True,
    )


def test_step2_risk() -> None:
    print("5) 第 2 步新策略共用风控（easytrade_risk.py）")
    from freqtrade.persistence import Trade

    use_db = Trade.use_db
    Trade.use_db = False  # 组合热度只看内存里的持仓（这里一笔都没有），不碰数据库
    try:
        for cls in STEP2_STRATEGIES:
            s = make_strategy(cls)
            lev = s.leverage("BTC/USDT:USDT", T0, 100.0, 1.0, 125.0, None, "long")
            s.max_leverage_param = 10.0  # 就算有人把参数改成 10 倍
            lev_hi = s.leverage("BTC/USDT:USDT", T0, 100.0, 1.0, 125.0, None, "short")
            lev_ex = s.leverage("BTC/USDT:USDT", T0, 100.0, 1.0, 3.0, None, "short")
            check(f"{cls.__name__}：默认杠杆 2 倍；参数改成 10 倍也只有 5 倍；交易所只允许 3 倍时用 3 倍",
                  lev == 2.0 and lev_hi == easytrade_risk.HARD_MAX_LEVERAGE == 5.0 and lev_ex == 3.0,
                  f"{lev:g} / {lev_hi:g} / {lev_ex:g}")
            check(f"{cls.__name__}.ignore_buying_expired_candle_after = 600",
                  s.ignore_buying_expired_candle_after == 600, str(s.ignore_buying_expired_candle_after))

        # 5 风险预算默认值：2026-11-07 账户回撤上限 8% -> 15%，三个预算一起放大到 2 倍（以前 0.5% / 3% / 4%）
        m = easytrade_risk.EasyTradeRiskMixin
        budgets = (m.risk_per_trade, m.max_portfolio_risk, m.dd_throttle_start, m.dd_throttle_mult)
        check("共用风控默认值：每笔风险 1%、组合热度上限 6%、账户回撤 8% 起新仓风险减半",
              budgets == (0.01, 0.06, 0.08, 0.5), " / ".join(f"{v:g}" for v in budgets))
        cap = check_drawdown.DEFAULT_MAX_DD_PCT / 100
        check(f"组合热度上限、回撤降仓起点都小于账户回撤上限 {cap:.0%}（踩刹车要在碰到上限之前）",
              m.max_portfolio_risk < cap and 0 < m.dd_throttle_start < cap,
              f"{m.max_portfolio_risk:.0%} / {m.dd_throttle_start:.0%} < {cap:.0%}")
        check("BigTrendShort 自己的仓位不变（每笔 1%）；BigTrendShortLowRisk 是它的一半（0.5%），也是共用默认值的一半",
              BigTrendShort.risk_per_trade == 0.01 and BigTrendShortLowRisk.risk_per_trade == BigTrendShort.risk_per_trade / 2
              and BigTrendShortLowRisk.risk_per_trade == m.risk_per_trade / 2,
              f"{BigTrendShort.risk_per_trade:g} / {BigTrendShortLowRisk.risk_per_trade:g}")

        # 5a 按止损距离定仓：止损 96（距离 4%）-> 名义仓位 = 1000 x 1% / (4% + 0.2% 成本缓冲) = 238.10，2 倍杠杆保证金 119.05
        s = _risk_strategy({})
        stake = s.custom_stake_amount("E/USDT:USDT", T0, 100.0, 237.5, 5.0, 950.0, 2.0, None, "long")
        loss_at_stop = stake * 2.0 * 0.04
        check("定仓：止损距离 4% 时保证金约 119 USDT（2 倍杠杆），打到止损约亏 9.5 USDT（总资金 1% 以内）",
              abs(stake - 1000 * s.risk_per_trade / 0.042 / 2.0) < 1e-6 and loss_at_stop <= 1000 * s.risk_per_trade,
              f"保证金 {stake:.2f}，亏 {loss_at_stop:.2f}")
        s_bad = _risk_strategy({"atr": float("nan")})
        stake_bad = s_bad.custom_stake_amount("E/USDT:USDT", T0, 100.0, 237.5, 5.0, 950.0, 2.0, None, "long")
        check("定仓：算不出止损（没有 ATR）时不开仓（返回 0，不会满仓兜底）", stake_bad == 0.0, f"{stake_bad}")

        # 5b 正常：成交在 100.2，止损设到 96；之后碰到 96 是 freqtrade 普通止损
        tr = make_long()
        after_fill(s, tr, 100.2)
        check("正常成交：止损设到 入场价 - 2 x ATR = 96", abs(tr.stop_loss - 96.0) < 1e-6, f"{tr.stop_loss:.6g}")
        check("正常成交：价格 99（没碰止损）不平仓", exits(s, tr, 99.0) == [])
        found = exits(s, tr, 95.9)
        check("正常成交：价格 95.9 碰到止损 → freqtrade 普通止损 stop_loss（不是补救单）",
              bool(found) and found[0].exit_type == ExitType.STOP_LOSS, first_reason(found))

        # 5c 止损补救：成交回调时价格已经跌到 95.5（越过 96），freqtrade 设不上这个止损 -> 立即平仓
        s = _risk_strategy({})
        tr = make_long()
        after_fill(s, tr, 95.5)
        found = exits(s, tr, 95.5)
        check("成交时价格已越过止损：下一轮立即平仓，离场原因 et_stop_breached",
              bool(found) and found[0].exit_reason == easytrade_risk.EXIT_STOP_BREACHED, first_reason(found))
        price = s.custom_exit_price(pair=tr.pair, trade=tr, current_time=T0, proposed_rate=95.5,
                                    current_profit=0.0, exit_tag=easytrade_risk.EXIT_STOP_BREACHED)
        check("补救单挂在盘口下方 0.5%（做多平仓=卖出，能立即成交）", abs(price - 95.5 * 0.995) < 1e-9, f"{price:.6g}")
        price = s.custom_exit_price(pair=tr.pair, trade=tr, current_time=T0, proposed_rate=101.0,
                                    current_profit=0.0, exit_tag=easytrade_risk.EXIT_TIMEOUT)
        check("et_timeout 的平仓价不变", price == 101.0, f"{price}")

        # 5d 止损在错误一侧：信号 K 线的离场通道下轨 101 已经在入场价 100 上方 -> 立即平仓, 先挂最远止损当保险
        s = _risk_strategy({"dc_exit_low": 101.0})
        tr = make_long()
        after_fill(s, tr, 100.0)
        found = exits(s, tr, 100.0)
        check("按实际成交价算不出有效止损：立即平仓，离场原因 et_stop_invalid",
              bool(found) and found[0].exit_reason == easytrade_risk.EXIT_STOP_INVALID, first_reason(found))
        check("平掉之前先挂着最远止损当保险（入场价 -25% = 75）", 74.9 < (tr.stop_loss or 0) < 75.1,
              f"{tr.stop_loss}")
    finally:
        Trade.use_db = use_db


# ----------------------------------------------------------------------------
# 6) XSMomentum 实盘排名（K线晚到）
# ----------------------------------------------------------------------------
XS_PAIRS = [f"{c}/USDT:USDT" for c in ("BTC", "ETH", "SOL", "XRP", "DOGE", "LINK", "ADA", "AVAX")]
XS_SIGNAL_DAY = pd.Timestamp("2026-10-25", tz="UTC")  # 周日：这根日线收盘后（周一 00:00 UTC）是调仓日


def _xs_frames(n_days: int = 300) -> dict[str, pd.DataFrame]:
    """8 个币的日线（随机游走，每个币的漂移不同，排名拉得开），最后一根 = XS_SIGNAL_DAY。"""
    import numpy as np

    rng = np.random.default_rng(7)
    dates = pd.date_range(end=XS_SIGNAL_DAY, periods=n_days, freq="1D")
    out = {}
    for i, pair in enumerate(XS_PAIRS):
        r = rng.normal((i - 3.5) * 0.002, 0.03, n_days)
        close = 100.0 * np.exp(np.cumsum(r))
        open_ = np.concatenate([[100.0], close[:-1]])
        out[pair] = pd.DataFrame({
            "date": dates, "open": open_, "high": np.maximum(open_, close) * 1.01,
            "low": np.minimum(open_, close) * 0.99, "close": close, "volume": 1000.0,
        })
    return out


class _XsFakeDP:
    """模拟盘的数据接口：交易所K线缓存（dp.ohlcv）、白名单、freqtrade 分析完缓存的K线（入场/离场信号从这里读）。"""

    runmode = RunMode.DRY_RUN

    def __init__(self, frames: dict[str, pd.DataFrame], whitelist: list[str] | None = None) -> None:
        self.frames = frames
        self.whitelist = list(whitelist or XS_PAIRS)
        self.analyzed: dict[str, pd.DataFrame] = {}

    def current_whitelist(self) -> list[str]:
        return list(self.whitelist)

    def ohlcv(self, pair: str, timeframe: str | None = None, copy: bool = True, candle_type: str = "") -> pd.DataFrame:
        df = self.frames.get(pair, pd.DataFrame())
        return df.copy() if copy else df

    def get_pair_dataframe(self, pair: str, timeframe: str | None = None, candle_type: str = "") -> pd.DataFrame:
        return self.ohlcv(pair, timeframe)

    def _set_cached_df(self, pair, timeframe, dataframe, candle_type=None) -> None:
        self.analyzed[pair] = dataframe

    def _emit_df(self, *args, **kwargs) -> None:
        pass

    def get_analyzed_dataframe(self, pair: str, timeframe: str):
        df = self.analyzed[pair]
        return df, df["date"].iloc[-1]


def _xs_strategy(frames, now: datetime, whitelist: list[str] | None = None, runmode=RunMode.DRY_RUN) -> XSMomentum:
    cfg = {"runmode": runmode, "dry_run": True, "max_open_trades": 6, "stake_currency": "USDT", "timeframe": "1d",
           "trading_mode": "futures", "margin_mode": "isolated", "candle_type_def": "futures", "exchange": {}}
    s = XSMomentum(cfg)
    s.dp = _XsFakeDP(frames, whitelist)
    s.dp.runmode = runmode
    s._xs_now = lambda: now  # 假的"现在"
    s.bot_start()
    return s


def _xs_loop(s: XSMomentum) -> None:
    """freqtrade 每一轮（约 5 秒）对白名单里的每个币做一次的事：拿交易所K线（复制一份）交给策略分析。"""
    for pair in s.dp.current_whitelist():
        df = s.dp.ohlcv(pair, "1d")
        if not df.empty:
            s._analyze_ticker_internal(df, {"pair": pair})


def _xs_last(s: XSMomentum, pair: str) -> pd.Series:
    return s.dp.analyzed[pair].iloc[-1]


def test_xsmomentum_live() -> None:
    print("6) 截面动量 XSMomentum：实盘里个别币的最新日线晚到")
    full = _xs_frames()
    close_ts = (XS_SIGNAL_DAY + pd.Timedelta(days=1)).to_pydatetime()  # 信号K线收盘 = 周一 00:00 UTC

    # 对照：所有币的K线都到齐时（一个新的策略对象）
    ref = _xs_strategy(full, close_ts + timedelta(seconds=30))
    _xs_loop(ref)
    want = {p: _xs_last(ref, p) for p in XS_PAIRS}
    late = next(p for p in XS_PAIRS if want[p]["xs_rank_long"] == 1)  # 让分数最高（周一要做多）的那个币晚到
    check("对照：周日这根K线是调仓信号K线，8 个币都参加排名",
          all(want[p]["xs_n"] == 8 for p in XS_PAIRS) and bool(ref._rebalance_mask(full[late].tail(1))[0]),
          f"晚到的币选 {late}（做多第 1 名）")
    check("实盘里每一轮都重新分析所有币（bot_start 把 process_only_new_candles 关掉）",
          ref.process_only_new_candles is False, str(ref.process_only_new_candles))
    bt = _xs_strategy(full, close_ts, runmode=RunMode.BACKTEST)
    check("回测不变：仍然是每根K线只分析一次", bt.process_only_new_candles is True, str(bt.process_only_new_candles))

    stale = dict(full)
    stale[late] = full[late].iloc[:-1].reset_index(drop=True)  # 这个币的周日K线还没到

    # 第 1 轮（收盘后 5 秒）：late 的K线还没到
    s = _xs_strategy(stale, close_ts + timedelta(seconds=5))
    _xs_loop(s)
    sun_rows = {p: _xs_last(s, p) for p in XS_PAIRS if p != late}
    signals = sum(int(r["enter_long"]) + int(r["enter_short"]) + int(r["exit_long"]) + int(r["exit_short"])
                  for r in sun_rows.values())
    check("第 1 轮（收盘后 5 秒，有一个币的K线还没到）：其他 7 个币周日这一行都不出信号（不开仓、不按排名平仓）",
          all(pd.isna(r["xs_n"]) for r in sun_rows.values()) and signals == 0,
          f"信号数 {signals}")
    old_rank = {p: r["xs_rank_long"] for p, r in sun_rows.items()}
    check("（对照：少了这个币时其他币的名次确实会错位，所以必须等）",
          any(old_rank[p] != want[p]["xs_rank_long"] for p in sun_rows), "")

    # 第 2 轮（收盘后 10 秒）：late 的K线到了 -> 所有币重新分析，名次和对照完全相同
    s.dp.frames = full
    s._xs_now = lambda: close_ts + timedelta(seconds=10)
    _xs_loop(s)
    got = {p: _xs_last(s, p) for p in XS_PAIRS}
    cols = ["xs_rank_long", "xs_rank_short", "xs_n", "enter_long", "enter_short", "exit_long", "exit_short"]
    diff = [p for p in XS_PAIRS if any(not (got[p][c] == want[p][c]) for c in cols)]
    check("第 2 轮（K线到了）：8 个币的名次、币数、入场/离场信号和 \"一开始就到齐\" 完全相同",
          not diff, "不同：" + " ".join(diff) if diff else f"{late} 做多信号 {int(got[late]['enter_long'])}")

    # 等了 300 秒还没到：不再等，按其余 7 个币排名（和白名单里本来就没有它一样）
    s2 = _xs_strategy(stale, close_ts + timedelta(seconds=301))
    _xs_loop(s2)
    ref7 = _xs_strategy(stale, close_ts + timedelta(seconds=301), whitelist=[p for p in XS_PAIRS if p != late])
    _xs_loop(ref7)
    ok = all(_xs_last(s2, p)["xs_n"] == 7 and _xs_last(s2, p)["xs_rank_long"] == _xs_last(ref7, p)["xs_rank_long"]
             for p in XS_PAIRS if p != late)
    check("收盘 301 秒后这个币还没到：不再等，按其余 7 个币排名", ok, "")
    check("实盘读交易所K线只读不改（策略没有往交易所的K线缓存里写列）",
          all(list(full[p].columns) == ["date", "open", "high", "low", "close", "volume"] for p in XS_PAIRS), "")

    # 吊灯止损：初始止损被 25% 上限截短时按比例收近
    s3 = _xs_strategy(full, close_ts)
    tr = make_long(open_rate=100.0, leverage=2.0)
    tr.set_custom_data(easytrade_risk.KEY_ATR, 10.0)  # 3 x ATR = 30% > 上限 -> 初始止损截到 100 x (1 - 0.24975)
    init = 100.0 * (1 - s3.et_stop_cap(2.0))
    tr.set_custom_data(easytrade_risk.KEY_INIT_STOP, init)
    s3.et_best_price_since_entry = lambda trade: 115.0
    trail = s3.et_trailing_stop(tr, pd.Series({"close": 115.0}))
    heat = 115.0 - max(trail, init) + 115.0 * s3.sizing_cost_buffer
    risk0 = (100.0 - init) + 100.0 * s3.sizing_cost_buffer
    check("吊灯止损：ATR 10%（初始止损被截到约 25%）、最高价 115 → 止损 = 115 - 4/3 x 24.975 ≈ 81.70，"
          "从现价打到止损最多亏开仓风险的约 4/3 倍",
          abs(trail - (115.0 - 4.0 / 3.0 * (100.0 - init))) < 1e-9 and heat / risk0 <= 4.0 / 3.0 + 0.01,
          f"止损 {trail:.2f}，热度 / 开仓风险 = {heat / risk0:.3f}（改之前是 1.60）")
    tr2 = make_long(open_rate=100.0, leverage=2.0)
    tr2.set_custom_data(easytrade_risk.KEY_ATR, 4.0)
    tr2.set_custom_data(easytrade_risk.KEY_INIT_STOP, 88.0)  # 没有截短：3 x ATR = 12
    trail2 = s3.et_trailing_stop(tr2, pd.Series({"close": 115.0}))
    check("吊灯止损：没截短时不变（= 最高价 - 4 x ATR = 99）", abs(trail2 - 99.0) < 1e-9, f"{trail2:.4g}")
    # 组合热度上限要放得下多空各 K 笔：已有 2K-1 笔、每笔热度最多 4/3 x 开仓风险，第 2K 笔还要有一整笔的空间
    # （risk_per_trade 改了，max_portfolio_risk 要按比例跟着改：2026-11-07 起 1% / 8%，以前 0.5% / 4%）
    k = int(easytrade_risk.param_value(s3.top_k))
    r = float(s3.risk_per_trade)
    need = (2 * k - 1) * r * 4.0 / 3.0 + r
    check(f"XSMomentum 组合热度上限放得下多空各 {k} 笔（{2 * k - 1} x {r:.1%} x 4/3 + {r:.1%} = {need:.2%}）",
          float(s3.max_portfolio_risk) >= need, f"上限 {float(s3.max_portfolio_risk):.2%}")

    # 最小下单额：开不出来 / 会被放大 时记一行带固定标记的日志（研究报告按它数次数），同一根K线只记一次
    from freqtrade.persistence import Trade

    use_db = Trade.use_db
    Trade.use_db = False
    records: list[str] = []

    class _Grab(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record.getMessage())

    xs_log = logging.getLogger("XSMomentum")
    handler = _Grab()
    xs_log.addHandler(handler)
    level = xs_log.level
    xs_log.setLevel(logging.INFO)
    try:
        s4 = _xs_strategy(full, close_ts)
        s4.wallets = _FakeWallets()
        row = {"date": XS_SIGNAL_DAY, "open": 99.0, "high": 101.0, "low": 98.0, "close": 100.0, "volume": 1.0, "atr": 5.0}
        s4.dp.analyzed["E/USDT:USDT"] = pd.DataFrame([row])
        args = dict(pair="E/USDT:USDT", current_time=close_ts, current_rate=100.0, proposed_stake=237.5,
                    max_stake=950.0, leverage=2.0, entry_tag=None, side="long")
        # 按风险算的保证金 = 1000 x 1% / (15% + 0.2%) / 2 倍 ≈ 32.9 USDT（止损 3 x ATR 5 = 15%）
        base = 1000 * float(s4.risk_per_trade) / 0.152 / 2.0
        st = s4.custom_stake_amount(min_stake=base * 1.5, **args)  # x 1.3 也不够
        s4.custom_stake_amount(min_stake=base * 1.5, **args)  # 同一根K线第二次：不再记
        skip = [m for m in records if MIN_STAKE_SKIP_TAG in m]
        check("仓位放大 30% 也不够最小下单额：记一行 \"开不出来\"（同一根K线只记一次），仓位本身不改",
              len(skip) == 1 and abs(st - base) < 1e-6, f"保证金 {st:.2f}，日志 {len(skip)} 行")
        s4.dp.analyzed["F/USDT:USDT"] = pd.DataFrame([row])
        s4.custom_stake_amount(min_stake=base * 1.15, **{**args, "pair": "F/USDT:USDT"})
        check("差得不多（x 1.3 够）：记一行 \"会被放大\"", sum(MIN_STAKE_BUMP_TAG in m for m in records) == 1, "")
    finally:
        xs_log.removeHandler(handler)
        xs_log.setLevel(level)
        Trade.use_db = use_db


def main() -> int:
    test_stop_rescue()
    test_stop_invalid()
    test_expired_entry()
    test_volume_units()
    test_step2_risk()
    test_xsmomentum_live()
    print()
    if FAILS:
        print(f"有 {len(FAILS)} 项未通过：")
        for f in FAILS:
            print(f"  - {f}")
        return 1
    print("全部通过。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
