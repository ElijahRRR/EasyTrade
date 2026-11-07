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
  9. 第 4 轮币安模拟盘（前向测试）配置 configs/config.dryrun-nfi.json（冻结版 NFI，1 倍杠杆，docs/forward-test.md）：
     合并后的配置（文件 + 环境变量，和 freqtrade 读到的一样）必须是模拟盘、币安合约逐仓、策略是 1 倍版本
     （BenchmarkCaps.py 里这个类的杠杆封顶 = 1、项目上限 5）、FreqUI 只监听 127.0.0.1、NFI 要求的设置没被改、
     配置文件里没有密钥 / 服务器 IP；环境变量把 dry_run 改成 false 时拒绝启动。
     scripts/run_dryrun.sh 启动前用的是同一个函数（--dryrun-config），这里再加回归检查（用自己写的替身基类测 1 倍封顶，
     不导入 NFI），并实际跑一次 run_dryrun.sh 的"只检查配置"模式。

用法：
  .venv/bin/python scripts/check_live_guards.py
  make guard-test
  .venv/bin/python scripts/check_live_guards.py --dryrun-config configs/config.dryrun-nfi.json
      # 只检查一份模拟盘配置（读当前的环境变量；scripts/run_dryrun.sh 启动前自动调用，不联网）
退出码：0 = 全部通过；1 = 有检查未通过（--dryrun-config：2 = 这份配置不能用来跑模拟盘）。
"""

from __future__ import annotations

import argparse
import ast
import copy
import importlib.util
import ipaddress
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
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


# ----------------------------------------------------------------------------
# 7) 币安模拟盘（前向测试）配置：第 4 轮冻结版 NFI 1 倍（configs/config.dryrun-nfi.json，docs/forward-test.md）
#    scripts/run_dryrun.sh 启动前调用 check_dryrun_config()（--dryrun-config），这里再做回归检查
# ----------------------------------------------------------------------------
# 模拟盘配置（相对项目根目录） -> 它必须用的策略类、这个类的杠杆封顶最多几倍
DRYRUN_TARGETS: dict[str, tuple[str, float]] = {
    "configs/config.dryrun-nfi.json": ("NostalgiaForInfinityX8Lev1", 1.0),
}
DRYRUN_DIR = "user_data/dryrun"
DRYRUN_DB_PREFIX = f"sqlite:///{DRYRUN_DIR}/"
CAPS_FILE_NAME = "BenchmarkCaps.py"
CAPS_FILE = ROOT_DIR / "user_data" / "strategies" / "benchmarks" / CAPS_FILE_NAME
LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1")
# NFI："不要在配置里覆盖策略的变量"。配置里出现这些键，就会改掉 NFI 自己的设置（freqtrade 的
# StrategyResolver 用配置覆盖策略属性；NFI __init__ 还会读配置顶层 / nfi_parameters 里的它自己的参数）。
# 前向测试要跑的就是固定版本的默认设置，所以一个都不许有。NFI 自己要求的几项（max_open_trades 等）另外查值。
NFI_NO_OVERRIDE_KEYS = (
    "minimal_roi", "stoploss", "trailing_stop", "trailing_stop_positive", "trailing_stop_positive_offset",
    "trailing_only_offset_is_reached", "use_custom_stoploss", "process_only_new_candles", "startup_candle_count",
    "exit_profit_offset", "disable_dataframe_checks", "ignore_buying_expired_candle_after",
    "position_adjustment_enable", "max_entry_position_adjustment",
    "nfi_parameters", "nfi_advanced_mode", "long_entry_signal_params", "short_entry_signal_params",
    "num_cores_indicators_calc", "max_slippage", "derisk_enable", "stops_enable", "grinding_enable",
)
# NFI 能从配置顶层读的参数（固定版本 __init__ 里的 NFI_SAFE_PARAMETERS，按名字前缀；杠杆 futures_mode_leverage* 也在这里）
NFI_NO_OVERRIDE_PREFIXES = (
    "futures_mode_leverage", "futures_max_open_trades_", "custom_fee_", "stop_threshold_", "regular_mode_derisk",
    "grind_mode_", "system_v4_", "legacy_system_", "inclusive_bad_trade_", "bad_trade_controller_",
)
# 配置文件里不能出现的密钥字段（值必须为空；FreqUI 的 jwt_secret_key 允许 config.base.json 的占位符）
SECRET_FIELDS = (
    ("exchange", "key"), ("exchange", "secret"), ("exchange", "password"), ("exchange", "uid"),
    ("exchange", "walletAddress"), ("exchange", "privateKey"),
    ("api_server", "username"), ("api_server", "password"), ("api_server", "ws_token"),
    ("telegram", "token"), ("telegram", "chat_id"), ("webhook", "url"), ("discord", "webhook_url"),
)
_TG_TOKEN_RE = re.compile(r"\b\d{6,12}:[A-Za-z0-9_-]{30,}\b")
_IPV4_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")


def _num_node(node: ast.AST, consts: dict[str, float]) -> float | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) and not isinstance(node.value, bool):
        return float(node.value)
    if isinstance(node, ast.Name):
        return consts.get(node.id)
    return None


def caps_leverage_info(caps_file: Path, class_name: str) -> dict:
    """静态读 BenchmarkCaps.py（只解析、不导入，所以没有 NFI 文件也能查）：
    class_name 在不在、它的 _easytrade_leverage_cap（自己没写就沿父类找）、文件的 MAX_LEVERAGE、它自己有没有改写 leverage()。
    实际的封顶 = min(max(cap, 1), MAX_LEVERAGE)，和 BenchmarkCaps._cap_value 相同。"""
    info: dict = {"file": str(caps_file), "found": False, "cap": None, "max_leverage": None,
                  "effective": None, "own_leverage_method": False, "error": ""}
    try:
        tree = ast.parse(caps_file.read_text(encoding="utf-8"))
    except (OSError, SyntaxError, ValueError) as exc:
        info["error"] = f"读不了 {caps_file}：{exc}"
        return info
    consts: dict[str, float] = {}
    classes: dict[str, ast.ClassDef] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            v = _num_node(node.value, consts)
            if v is not None:
                consts[node.targets[0].id] = v
        elif isinstance(node, ast.ClassDef):
            classes[node.name] = node
    info["max_leverage"] = consts.get("MAX_LEVERAGE")

    def cap_of(name: str, depth: int = 0) -> float | None:
        node = classes.get(name)
        if node is None or depth > 10:
            return None
        for st in node.body:
            targets = st.targets if isinstance(st, ast.Assign) else [st.target] if isinstance(st, ast.AnnAssign) else []
            if any(isinstance(t, ast.Name) and t.id == "_easytrade_leverage_cap" for t in targets) and st.value is not None:
                return _num_node(st.value, consts)
        for base in node.bases:
            if isinstance(base, ast.Name) and base.id in classes:
                found = cap_of(base.id, depth + 1)
                if found is not None:
                    return found
        return None

    node = classes.get(class_name)
    if node is None:
        return info
    info["found"] = True
    info["own_leverage_method"] = any(
        isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef)) and st.name == "leverage" for st in node.body)
    cap = cap_of(class_name)
    info["cap"] = cap
    mx = info["max_leverage"]
    if cap is not None and mx is not None:
        info["effective"] = min(max(cap, 1.0), mx)
    return info


def _get(cfg: dict, *keys: str):
    cur = cfg
    for k in keys:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(k)
    return cur


def inline_secret_problems(file_cfg: dict) -> list[str]:
    """配置文件本身（不含环境变量）里有没有密钥、Telegram Token、服务器 IP。"""
    out = []
    for section, key in SECRET_FIELDS:
        val = _get(file_cfg, section, key)
        if val not in (None, ""):
            out.append(f"配置文件里写了 {section}.{key}（密钥只能放在 .env.dryrun-nfi 里，用 FREQTRADE__{section.upper()}__{key.upper()}）")
    jwt = _get(file_cfg, "api_server", "jwt_secret_key")
    if jwt not in (None, "") and "PLACEHOLDER" not in str(jwt):
        out.append("配置文件里写了 api_server.jwt_secret_key（只能用环境变量 FREQTRADE__API_SERVER__JWT_SECRET_KEY）")

    def walk(obj, path: str):
        if isinstance(obj, dict):
            for k, v in obj.items():
                yield from walk(v, f"{path}.{k}" if path else str(k))
        elif isinstance(obj, list):
            for i, v in enumerate(obj):
                yield from walk(v, f"{path}[{i}]")
        elif isinstance(obj, str):
            yield path, obj

    for path, text in walk(file_cfg, ""):
        if path.startswith(("original_config", "config_files")):
            continue
        if _TG_TOKEN_RE.search(text) or "api.telegram.org/bot" in text:
            out.append(f"配置文件的 {path} 里像是有 Telegram Token")
        for ip in _IPV4_RE.findall(text):
            try:
                addr = ipaddress.ip_address(ip)
            except ValueError:
                continue
            if not (addr.is_loopback or addr.is_unspecified):
                out.append(f"配置文件的 {path} 里有 IP 地址 {ip}（不要把服务器 IP 写进仓库里的文件）")
    return out


def dryrun_config_problems(cfg: dict, expect_strategy: str, expect_max_lev: float, caps_file: Path) -> tuple[list[str], list[str], dict]:
    """合并后的配置（文件 + 环境变量）能不能用来跑这个模拟盘。返回 (不通过的原因, 提醒, 关键信息)。
    启动时 scripts/run_dryrun.sh 还会加 --dry-run --strategy --strategy-path --db-url（值和这里检查的相同）。"""
    from freqtrade.plugins.pairlist.pairlist_helpers import expand_pairlist

    bad: list[str] = []
    notes: list[str] = []

    # 1. 只能是模拟盘
    if cfg.get("dry_run") is not True:
        bad.append(f"dry_run 不是 true（现在是 {cfg.get('dry_run')!r}；多半是环境变量 FREQTRADE__DRY_RUN 改的）："
                   "这份配置只能跑模拟盘，拒绝启动")
    # 2. 币安 USDT 本位永续、逐仓
    exch = cfg.get("exchange") or {}
    for label, got, want in (("exchange.name", exch.get("name"), "binance"), ("trading_mode", cfg.get("trading_mode"), "futures"),
                             ("margin_mode", cfg.get("margin_mode"), "isolated"), ("stake_currency", cfg.get("stake_currency"), "USDT")):
        if got != want:
            bad.append(f"{label} 应该是 {want}，现在是 {got!r}")
    for key in ("key", "secret", "password", "uid", "privateKey"):
        if exch.get(key):
            bad.append(f"exchange.{key} 有值：模拟盘不需要币安 API Key，不要放进模拟盘的环境变量 / 配置")
    # 3. 策略 = 1 倍版本，杠杆封顶 <= 这个版本的上限 <= 项目上限 5 倍
    hard = float(easytrade_risk.HARD_MAX_LEVERAGE)
    strat = cfg.get("strategy")
    if strat != expect_strategy:
        bad.append(f"策略应该是 {expect_strategy}（{expect_max_lev:g} 倍版本），现在是 {strat!r}")
    lev = caps_leverage_info(caps_file, expect_strategy)
    if lev["error"]:
        bad.append(lev["error"])
    elif not lev["found"]:
        bad.append(f"{caps_file} 里没有 class {expect_strategy}")
    else:
        if lev["max_leverage"] is None or lev["max_leverage"] > hard:
            bad.append(f"{caps_file.name} 的 MAX_LEVERAGE 应该 <= {hard:g}，现在是 {lev['max_leverage']}")
        if lev["effective"] is None:
            bad.append(f"读不出 {expect_strategy} 的杠杆封顶（_easytrade_leverage_cap）")
        elif lev["effective"] > min(expect_max_lev, hard):
            bad.append(f"{expect_strategy} 的杠杆封顶是 {lev['effective']:g} 倍，这个模拟盘最多 {min(expect_max_lev, hard):g} 倍")
        if lev["own_leverage_method"]:
            bad.append(f"{expect_strategy} 自己改写了 leverage()，封顶可能不生效（应该只改 _easytrade_leverage_cap）")
    if cfg.get("force_entry_enable") is not False:
        bad.append("force_entry_enable 必须是 false（强制开仓可以自带杠杆参数，绕过策略的封顶）")
    # 4. NFI 的设置不许被配置改掉；NFI 自己要求的几项必须是这些值
    over = sorted(k for k in cfg if k in NFI_NO_OVERRIDE_KEYS or k.startswith(NFI_NO_OVERRIDE_PREFIXES))
    if over:
        bad.append("配置（或环境变量）改了 NFI 自己的设置：" + "、".join(over) + "（前向测试只跑固定版本的默认设置）")
    for key, want in (("timeframe", "5m"), ("max_open_trades", 6), ("stake_amount", "unlimited"), ("use_exit_signal", True),
                      ("exit_profit_only", False), ("ignore_roi_if_entry_signal", True)):
        if cfg.get(key) != want or type(cfg.get(key)) is not type(want):
            bad.append(f"{key} 应该是 {want!r}（NFI 要求 / 和回测相同），现在是 {cfg.get(key)!r}")
    # 5. 自己的数据库、自己的策略目录
    db = str(cfg.get("db_url") or "")
    if not (db.startswith(DRYRUN_DB_PREFIX) and db.endswith(".sqlite")) or "live" in db:
        bad.append(f"db_url 应该是 {DRYRUN_DB_PREFIX}<名字>.sqlite（模拟盘自己的数据库），现在是 {db!r}")
    sp = str(cfg.get("strategy_path") or "")
    if not sp.startswith(DRYRUN_DIR + "/"):
        bad.append(f"strategy_path 应该是 {DRYRUN_DIR}/ 下面的冻结副本目录，现在是 {sp!r}")
    # 6. FreqUI / REST API 只监听本机
    api = cfg.get("api_server") or {}
    if api.get("enabled"):
        if str(api.get("listen_ip_address")) not in LOOPBACK_HOSTS:
            bad.append(f"api_server.listen_ip_address 必须是 127.0.0.1（现在是 {api.get('listen_ip_address')!r}）："
                       "FreqUI 不能直接暴露到公网，远程用 SSH 隧道")
    else:
        # 15% 回撤规则（DEFAULT_MAX_DD_PCT）靠账户权益看门狗执行，它只能通过 REST API 读权益、调 /stopentry 暂停开仓：
        # API 没开 = 回撤超限也没人能让机器人停止开新仓，不能启动
        bad.append("api_server 没开（多半是环境变量 FREQTRADE__API_SERVER__ENABLED=false 改的）：账户权益看门狗"
                   "只能通过 REST API 读账户权益、在回撤超过上限时暂停开新仓，API 关掉就没人执行 15% 的规则")
    # 7. 交易对：固定列表，BTC 必须在（NFI 每个币都要看 BTC）
    pl = [p.get("method") for p in (cfg.get("pairlists") or []) if isinstance(p, dict)]
    if pl != ["StaticPairList"]:
        bad.append(f"pairlists 应该只有 StaticPairList（前向测试期间币不变），现在是 {pl}")
    wl = list(exch.get("pair_whitelist") or [])
    try:
        black = set(expand_pairlist(list(exch.get("pair_blacklist") or []), wl))
    except ValueError as exc:
        black = set()
        bad.append(f"pair_blacklist 写法不对：{exc}")
    eff = [p for p in wl if p not in black]
    stake = cfg.get("stake_currency") or "USDT"
    btc = f"BTC/{stake}:{stake}"
    if btc not in eff:
        bad.append(f"交易对里没有 {btc}（NFI 每个币都要参考 BTC）")
    if len(eff) < 20:
        bad.append(f"交易对只有 {len(eff)} 个（至少 20 个；NFI 推荐 40 ~ 80 个）")
    elif len(eff) < 40:
        notes.append(f"交易对 {len(eff)} 个，比 NFI 推荐的 40 ~ 80 个少，交易会少一些")
    wallet = cfg.get("dry_run_wallet")
    if not isinstance(wallet, (int, float)) or isinstance(wallet, bool) or wallet <= 0:
        bad.append(f"dry_run_wallet 应该是正数（模拟资金，USDT），现在是 {wallet!r}")
    tg = cfg.get("telegram") or {}
    if tg.get("enabled"):
        notes.append("开了 freqtrade 自带的 Telegram 命令机器人：它要用自己的 Token，不能和别的机器人共用（会互相抢消息）")
    facts = {
        "strategy": strat, "strategy_path": sp, "db_url": db, "bot_name": cfg.get("bot_name") or "",
        "exchange": exch.get("name"), "stake_currency": stake, "pairs": len(eff), "wallet": wallet,
        "api": f"{api.get('listen_ip_address')}:{api.get('listen_port')}" if api.get("enabled") else "",
        "lev_cap": lev.get("effective"), "caps_file": str(caps_file),
    }
    return bad, notes, facts


def load_dryrun_config(path: Path, with_env: bool) -> dict:
    """和 freqtrade 一样读配置：文件（含 add_config_files）+ 环境变量 FREQTRADE__*（环境变量优先）。"""
    from freqtrade.configuration.environment_vars import environment_vars_to_dict
    from freqtrade.configuration.load_config import load_from_files
    from freqtrade.misc import deep_merge_dicts

    cfg = load_from_files([str(path)])
    if with_env:
        cfg = deep_merge_dicts(environment_vars_to_dict(), cfg)
    return cfg


def freqtrade_validation_error(cfg: dict) -> str:
    """freqtrade 自己的配置校验（schema + 一致性，同 freqtrade trade 加载完策略以后的那一次）。
    stoploss / minimal_roi 平时由策略提供，这里填 NFI 的 -0.99 和一个占位值。没问题返回空字符串。"""
    from freqtrade.configuration.config_validation import validate_config_consistency
    from freqtrade.exceptions import ConfigurationError

    c = copy.deepcopy(cfg)
    c["runmode"] = RunMode.DRY_RUN
    c.setdefault("stoploss", -0.99)
    c.setdefault("minimal_roi", {"0": 100.0})
    try:
        validate_config_consistency(c)
    except (ConfigurationError, ValueError) as exc:
        return str(exc)
    return ""


def default_caps_file(cfg: dict) -> Path:
    """启动时真正用的 BenchmarkCaps.py：冻结副本（strategy_path 下面）有就用它，否则用仓库里的（第一次启动时会被复制过去）。"""
    sp = cfg.get("strategy_path")
    if sp:
        frozen = (ROOT_DIR / sp / CAPS_FILE_NAME) if not Path(sp).is_absolute() else Path(sp) / CAPS_FILE_NAME
        if frozen.is_file():
            return frozen
    return CAPS_FILE


def check_dryrun_config(config_path: str, caps_file: str | None = None, with_env: bool = True) -> int:
    """scripts/run_dryrun.sh 用：检查一份模拟盘配置。人看的结果打印出来；给脚本读的值打印成 "@@键=值" 行。"""
    path = (ROOT_DIR / config_path) if not Path(config_path).is_absolute() else Path(config_path)
    rel = os.path.relpath(path, ROOT_DIR)
    if rel not in DRYRUN_TARGETS:
        print(f"[模拟盘配置检查] 错误：{rel} 不是登记过的模拟盘配置（scripts/check_live_guards.py 的 DRYRUN_TARGETS）")
        return 2
    expect_strategy, expect_lev = DRYRUN_TARGETS[rel]
    try:
        file_cfg = load_dryrun_config(path, with_env=False)
        cfg = load_dryrun_config(path, with_env=with_env)
    except Exception as exc:  # 配置文件写错（JSON 语法等）
        print(f"[模拟盘配置检查] 错误：读不了配置 {rel}：{exc}")
        return 2
    caps = Path(caps_file) if caps_file else default_caps_file(cfg)
    bad = inline_secret_problems(file_cfg)
    more, notes, facts = dryrun_config_problems(cfg, expect_strategy, expect_lev, caps)
    bad += more
    err = freqtrade_validation_error(cfg)
    if err:
        bad.append(f"freqtrade 自己的配置校验不通过：{err}")
    for n in notes:
        print(f"[模拟盘配置检查] 注意：{n}")
    if bad:
        for b in bad:
            print(f"[模拟盘配置检查] 错误：{b}")
        print(f"[模拟盘配置检查] 不通过（{len(bad)} 项）：{rel} 现在不能用来跑模拟盘")
        return 2
    lev_txt = f"{facts['lev_cap']:g}" if isinstance(facts["lev_cap"], float) else "?"
    print(f"[模拟盘配置检查] 通过：{rel}（模拟盘、币安合约逐仓、策略 {facts['strategy']}、杠杆封顶 {lev_txt} 倍、"
          f"交易对 {facts['pairs']} 个、模拟资金 {facts['wallet']} {facts['stake_currency']}、FreqUI {facts['api'] or '未开'}）")
    for k, v in facts.items():
        print(f"@@{k}={v}")
    return 0


# 替身基类（本项目自己写的测试代码，不是 NFI）：故意要 10 倍杠杆、配置还能把杠杆设置改大，用来测 BenchmarkCaps.py 的封顶
_STUB_NFI = '''\
"""替身基类：本项目 scripts/check_live_guards.py 写的测试代码，不是 NostalgiaForInfinity。只用来测 BenchmarkCaps.py 的杠杆封顶。"""
from freqtrade.strategy import IStrategy


class NostalgiaForInfinityX8(IStrategy):
    INTERFACE_VERSION = 3
    timeframe = "5m"
    stoploss = -0.99
    minimal_roi = {"0": 100.0}
    can_short = True
    target_profit_cache = None
    futures_mode_leverage = 10.0
    futures_mode_leverage_rebuy_mode = 10.0
    futures_mode_leverage_grind_mode = 10.0
    futures_mode_leverage_long = None
    futures_mode_leverage_short = 8.0

    def __init__(self, config):
        super().__init__(config)
        for name in [n for n in dir(type(self)) if n.startswith("futures_mode_leverage")]:
            if name in config:
                setattr(self, name, config[name])

    def leverage(self, pair, current_time, current_rate, proposed_leverage, max_leverage, entry_tag, side, **kwargs):
        return 10.0

    def populate_indicators(self, dataframe, metadata):
        return dataframe

    def populate_entry_trend(self, dataframe, metadata):
        return dataframe

    def populate_exit_trend(self, dataframe, metadata):
        return dataframe
'''


def _clean_env(**extra: str) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith(("FREQTRADE__", "EASYTRADE_DRYRUN_"))}
    env.pop("MAX_DD_PCT", None)  # 回撤上限用默认（项目硬规则）；要测改过的值用 extra 传
    env["EASYTRADE_DRYRUN_ENV_FILE"] = "none"  # 不读服务器上真正的 .env.dryrun-nfi，结果只看这里给的环境变量
    if env.get("FT_RUNNER") not in ("venv", "path"):
        env.pop("FT_RUNNER", None)  # run_dryrun.sh 只支持 venv / path（make synthetic-test、Docker 里自动找）
    env.update(extra)
    return env


def test_dryrun_config() -> None:
    print("7) 币安模拟盘配置（第 4 轮：冻结版 NFI 1 倍，configs/config.dryrun-nfi.json）")
    rel = "configs/config.dryrun-nfi.json"
    path = ROOT_DIR / rel
    expect_strategy, expect_lev = DRYRUN_TARGETS[rel]
    file_cfg = load_dryrun_config(path, with_env=False)

    # 7a 配置文件本身（不看环境变量）：通过全部规则、freqtrade 自己的校验也通过、没有密钥
    secrets = inline_secret_problems(file_cfg)
    check("配置文件里没有密钥、Telegram Token、服务器 IP", not secrets, "；".join(secrets))
    bad, _, facts = dryrun_config_problems(file_cfg, expect_strategy, expect_lev, CAPS_FILE)
    check(f"配置本身：模拟盘、币安合约逐仓、策略 {expect_strategy}、NFI 要求的设置、自己的数据库、FreqUI 只听 127.0.0.1",
          not bad, "；".join(bad) if bad else f"{facts['pairs']} 个交易对，数据库 {facts['db_url']}")
    err = freqtrade_validation_error(file_cfg)
    check("freqtrade 自己的配置校验通过（schema + 一致性，同 make validate）", not err, err)
    check("dry_run = true、模拟资金 1000 USDT、FreqUI 端口 8081",
          file_cfg.get("dry_run") is True and file_cfg.get("dry_run_wallet") == 1000
          and _get(file_cfg, "api_server", "listen_port") == 8081,
          f"{file_cfg.get('dry_run')} / {file_cfg.get('dry_run_wallet')} / {_get(file_cfg, 'api_server', 'listen_port')}")

    # 7b 静态读 BenchmarkCaps.py：配置里的类是 1 倍版本，项目上限 5 倍
    lev = caps_leverage_info(CAPS_FILE, expect_strategy)
    check(f"BenchmarkCaps.py 里 {expect_strategy} 的杠杆封顶 = {expect_lev:g} 倍，MAX_LEVERAGE = 5（不导入 NFI，只解析源码）",
          lev["found"] and lev["effective"] == expect_lev and lev["max_leverage"] == easytrade_risk.HARD_MAX_LEVERAGE
          and not lev["own_leverage_method"], f"封顶 {lev['effective']}、上限 {lev['max_leverage']}")
    capped = caps_leverage_info(CAPS_FILE, "NostalgiaForInfinityX8Capped")
    bad5, _, _ = dryrun_config_problems({**file_cfg, "strategy": "NostalgiaForInfinityX8Capped"},
                                         "NostalgiaForInfinityX8Capped", expect_lev, CAPS_FILE)
    check("对照：换成 5 倍版本 NostalgiaForInfinityX8Capped 时拒绝（这个模拟盘最多 1 倍）",
          capped["effective"] == 5.0 and any("杠杆封顶是 5 倍" in b for b in bad5), f"封顶 {capped['effective']}")

    # 7c 环境变量能改配置（freqtrade 的机制），这些改法都必须被拒绝
    def problems_with(**over) -> list[str]:
        cfg = copy.deepcopy(file_cfg)
        for dotted, val in over.items():
            cur = cfg
            parts = dotted.split("__")
            for p in parts[:-1]:
                cur = cur.setdefault(p, {})
            cur[parts[-1]] = val
        return dryrun_config_problems(cfg, expect_strategy, expect_lev, CAPS_FILE)[0]

    cases = [
        ("FREQTRADE__DRY_RUN=false（实盘）", {"dry_run": False}, "dry_run"),
        ("FREQTRADE__API_SERVER__LISTEN_IP_ADDRESS=0.0.0.0（FreqUI 暴露到公网）", {"api_server__listen_ip_address": "0.0.0.0"}, "listen_ip_address"),
        ("FREQTRADE__API_SERVER__ENABLED=false（看门狗没法暂停开仓）", {"api_server__enabled": False}, "api_server 没开"),
        ("FREQTRADE__STRATEGY=NostalgiaForInfinityX8Capped（5 倍版本）", {"strategy": "NostalgiaForInfinityX8Capped"}, "策略应该是"),
        ("FREQTRADE__FUTURES_MODE_LEVERAGE=10（NFI 的杠杆设置）", {"futures_mode_leverage": 10}, "futures_mode_leverage"),
        ("FREQTRADE__FORCE_ENTRY_ENABLE=true（强制开仓可以自带杠杆）", {"force_entry_enable": True}, "force_entry_enable"),
        ("FREQTRADE__MAX_OPEN_TRADES=12（和回测不一样）", {"max_open_trades": 12}, "max_open_trades"),
        ("FREQTRADE__DB_URL=sqlite:///user_data/tradesv3.live.sqlite（实盘数据库）", {"db_url": "sqlite:///user_data/tradesv3.live.sqlite"}, "db_url"),
        ("FREQTRADE__EXCHANGE__KEY=…（模拟盘不需要 API Key）", {"exchange__key": "dummy-not-a-key"}, "exchange.key"),
        ("FREQTRADE__EXCHANGE__NAME=okx", {"exchange__name": "okx"}, "exchange.name"),
    ]
    for label, over, needle in cases:
        found = problems_with(**over)
        check(f"拒绝：{label}", any(needle in b for b in found), "；".join(found)[:120] or "（没拒绝）")
    # 环境变量真的会被读进来（和 freqtrade 一样的读法）
    old = os.environ.get("FREQTRADE__DRY_RUN")
    os.environ["FREQTRADE__DRY_RUN"] = "false"
    try:
        env_cfg = load_dryrun_config(path, with_env=True)
    finally:
        if old is None:
            os.environ.pop("FREQTRADE__DRY_RUN", None)
        else:
            os.environ["FREQTRADE__DRY_RUN"] = old
    check("环境变量 FREQTRADE__DRY_RUN=false 会被读成 dry_run = False（检查读到的就是 freqtrade 读到的）",
          env_cfg.get("dry_run") is False, repr(env_cfg.get("dry_run")))
    leaky = inline_secret_problems({**copy.deepcopy(file_cfg), "telegram": {"token": "123456789:" + "A" * 35},
                                    "api_server": {"listen_ip_address": "203.0.113.7"}})  # RFC 5737 documentation address, not a real server
    check("对照：配置文件里写了 Telegram Token / 服务器 IP 时能查出来", len(leaky) >= 2, f"{len(leaky)} 项")

    # 7d 用替身基类（不是 NFI）实际跑一遍 BenchmarkCaps.py：配置里的类在模拟盘里杠杆 <= 1，配置把杠杆改到 20 也没用
    tmp = Path(tempfile.mkdtemp(prefix="easytrade-guard-caps-"))
    try:
        shutil.copy2(CAPS_FILE, tmp / CAPS_FILE_NAME)
        (tmp / "NostalgiaForInfinityX8.py").write_text(_STUB_NFI, encoding="utf-8")
        # BenchmarkCaps 把"超过 5 倍、改成 1 倍"记成 WARNING（模拟盘里有用），这里是故意的，不显示
        logging.getLogger("easytrade_guard_benchmarkcaps").setLevel(logging.ERROR)
        spec = importlib.util.spec_from_file_location("easytrade_guard_benchmarkcaps", str(tmp / CAPS_FILE_NAME))
        mod = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = mod
        spec.loader.exec_module(mod)
        cfg = copy.deepcopy(file_cfg)
        cfg.update({"runmode": RunMode.DRY_RUN, "candle_type_def": "futures", "futures_mode_leverage": 20.0,
                    "futures_mode_leverage_short": 50.0, "user_data_dir": tmp})
        s = getattr(mod, cfg["strategy"])(cfg)
        s.bot_start()
        levs = [s.leverage(pair="BTC/USDT:USDT", current_time=T0, current_rate=100.0, proposed_leverage=3.0,
                           max_leverage=125.0, entry_tag=tag, side=side)
                for side in ("long", "short") for tag in (None, "1", "61")]
        attrs = {n: getattr(s, n) for n in dir(type(s)) if n.startswith("futures_mode_leverage")}
        nums = [v for v in attrs.values() if isinstance(v, (int, float)) and not isinstance(v, bool)]
        check(f"替身基类要 10 倍、配置改成 20 / 50 倍：{cfg['strategy']} 实际杠杆全部 = 1 倍，futures_mode_leverage* 全部 <= 1",
              all(v == 1.0 for v in levs) and nums and max(nums) <= 1.0,
              f"leverage() = {sorted(set(levs))}，属性最大 {max(nums) if nums else '?'}")
    except Exception as exc:  # 替身也跑不起来 = BenchmarkCaps.py 有问题
        check("用替身基类实际跑 BenchmarkCaps.py", False, f"{type(exc).__name__}: {exc}")
    finally:
        sys.modules.pop("easytrade_guard_benchmarkcaps", None)
        sys.modules.pop("easytrade_vendor_NostalgiaForInfinityX8", None)
        shutil.rmtree(tmp, ignore_errors=True)

    # 7e 实际跑一次 scripts/run_dryrun.sh 的"只检查配置"模式（不联网、不启动机器人）
    bash = shutil.which("bash")
    if bash is None:
        check("scripts/run_dryrun.sh（找不到 bash，跳过）", True)
        return
    script = str(ROOT_DIR / "scripts" / "run_dryrun.sh")
    ok = subprocess.run([bash, script, "nfi", "--config-check-only"], cwd=ROOT_DIR, env=_clean_env(),
                        capture_output=True, text=True, timeout=180)
    tail = (ok.stdout + ok.stderr).strip().splitlines()
    check("run_dryrun.sh nfi --config-check-only：干净的环境变量 → 通过（退出码 0）", ok.returncode == 0,
          f"退出码 {ok.returncode}" + ("" if ok.returncode == 0 or not tail else f"；{tail[-1][:160]}"))
    live = subprocess.run([bash, script, "nfi", "--config-check-only"], cwd=ROOT_DIR,
                          env=_clean_env(FREQTRADE__DRY_RUN="false", FREQTRADE__EXCHANGE__KEY="dummy-not-a-key",
                                         FREQTRADE__STRATEGY="NostalgiaForInfinityX8Capped"),
                          capture_output=True, text=True, timeout=180)
    out = live.stdout + live.stderr
    check("run_dryrun.sh：FREQTRADE__DRY_RUN=false → 拒绝（退出码 20 = 安全检查不通过），API Key、FREQTRADE__STRATEGY 被去掉",
          live.returncode == 20 and "dry_run 不是 true" in out and "exchange.key" not in out and "策略应该是" not in out,
          f"退出码 {live.returncode}")
    noapi = subprocess.run([bash, script, "nfi", "--config-check-only"], cwd=ROOT_DIR,
                           env=_clean_env(FREQTRADE__API_SERVER__ENABLED="false"),
                           capture_output=True, text=True, timeout=180)
    check("run_dryrun.sh：FREQTRADE__API_SERVER__ENABLED=false → 拒绝（退出码 20：看门狗没法暂停开仓）",
          noapi.returncode == 20 and "api_server 没开" in noapi.stdout + noapi.stderr, f"退出码 {noapi.returncode}")
    for label, value, want in (("MAX_DD_PCT=30（比 15% 宽）", "30", 20), ("MAX_DD_PCT=abc", "abc", 20),
                               ("MAX_DD_PCT=12（更严）", "12", 0)):
        r = subprocess.run([bash, script, "nfi", "--config-check-only"], cwd=ROOT_DIR, env=_clean_env(MAX_DD_PCT=value),
                           capture_output=True, text=True, timeout=180)
        check(f"run_dryrun.sh：{label} → 退出码 {want}", r.returncode == want, f"退出码 {r.returncode}")
    wd = subprocess.run([bash, script, "nfi-watchdog", "--status"], cwd=ROOT_DIR, env=_clean_env(MAX_DD_PCT="30"),
                        capture_output=True, text=True, timeout=60)
    check("run_dryrun.sh nfi-watchdog：MAX_DD_PCT=30 → 看门狗也不启动（退出码 20）", wd.returncode == 20,
          f"退出码 {wd.returncode}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="实盘 / 模拟盘保护逻辑自检（不联网）")
    ap.add_argument("--dryrun-config", metavar="配置文件",
                    help="只检查一份模拟盘配置（文件 + 当前环境变量），scripts/run_dryrun.sh 启动前用")
    ap.add_argument("--caps-file", metavar="BenchmarkCaps.py", help="（配合 --dryrun-config）检查哪个 BenchmarkCaps.py；默认冻结副本，没有就用仓库里的")
    args = ap.parse_args(argv)
    if args.dryrun_config:
        return check_dryrun_config(args.dryrun_config, args.caps_file)

    test_stop_rescue()
    test_stop_invalid()
    test_expired_entry()
    test_volume_units()
    test_step2_risk()
    test_xsmomentum_live()
    test_dryrun_config()
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
