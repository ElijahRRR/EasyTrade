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
     信号收盘 10 分钟后不再入场、按止损距离定仓（打到止损约亏 0.5%）、止损挂不上 / 止损在错误一侧时立即平仓。

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

import easytrade_risk  # noqa: E402
from BigTrendBoth import BigTrendBoth  # noqa: E402
from BigTrendShort import BigTrendShort  # noqa: E402
from DailyThreeSoldiersShort import DailyThreeSoldiersShort  # noqa: E402
from DonchianTrend import DonchianTrend  # noqa: E402
from FundingContrarian import FundingContrarian  # noqa: E402
from RangeMeanReversion import RangeMeanReversion  # noqa: E402
from TsmomTrend import TsmomTrend  # noqa: E402
from VolSqueezeBreakout import VolSqueezeBreakout  # noqa: E402
from VolumeSpikeShort import VolumeSpikeShort  # noqa: E402


STEP2_STRATEGIES = (DonchianTrend, TsmomTrend, BigTrendBoth, RangeMeanReversion, VolSqueezeBreakout, FundingContrarian)

CustomDataWrapper.use_db = False  # custom data 只放内存，不碰数据库
# 策略在补救平仓时会写一条 WARNING 日志（实盘里有用），自检时不显示，免得和检查结果混在一起
logging.getLogger("rust_port_common").setLevel(logging.ERROR)
logging.getLogger("easytrade_risk").setLevel(logging.ERROR)
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

        # 5a 按止损距离定仓：止损 96（距离 4%）-> 名义仓位 = 1000 x 0.5% / (4% + 0.2% 成本缓冲) = 119.05，2 倍杠杆保证金 59.52
        s = _risk_strategy({})
        stake = s.custom_stake_amount("E/USDT:USDT", T0, 100.0, 237.5, 5.0, 950.0, 2.0, None, "long")
        loss_at_stop = stake * 2.0 * 0.04
        check("定仓：止损距离 4% 时保证金约 59.5 USDT（2 倍杠杆），打到止损约亏 4.8 USDT（总资金 0.5% 以内）",
              abs(stake - 1000 * 0.005 / 0.042 / 2.0) < 1e-6 and loss_at_stop <= 5.0, f"保证金 {stake:.2f}，亏 {loss_at_stop:.2f}")
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


def main() -> int:
    test_stop_rescue()
    test_stop_invalid()
    test_expired_entry()
    test_volume_units()
    test_step2_risk()
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
