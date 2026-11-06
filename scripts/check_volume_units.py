#!/usr/bin/env python3
"""
K 线成交量单位自检（回归测试）。不联网，几秒钟跑完。

为什么需要它：OKX 永续合约按"张"下单（BTC 1 张 = 0.01 BTC，ETH 1 张 = 0.1 ETH）。
如果 K 线里的 volume 是张数而不是币的数量，策略算出来的成交额（收盘价 x volume）就会差 10~100 倍，
VolumeSpikeShort 的 50,000 USDT、DailyThreeSoldiersShort 的 5,000,000 USDT 流动性门槛就全错了。

结论（详见 docs/okx-volume-units.md）：freqtrade + ccxt 拿到的 OKX 永续 volume 是【币的数量】，
不是张数，成交额 = 收盘价 x volume，不能再乘合约面值 contractSize。本脚本把这个结论变成可重复的检查，
升级 freqtrade / ccxt 之后跑一次即可：
  1. ccxt：OKX 永续K线取的是 volCcy（币的数量），不是 vol（张数）；现货取 vol（现货的 vol 就是币的数量）。
     下载历史数据（REST）、实盘行情（websocket）三条路径都查。
  2. freqtrade：OKX 没有标记"K线成交量是张数"（ohlcv_volume_in_contracts = False；Gate 才是 True），
     转成表格时 volume 原样保留，不做换算。
  3. 策略：假交易对 contractSize = 0.01（和 OKX 的 BTC 一样）——
     OKX 规则下成交额 = 收盘价 x volume，和 contractSize = 1 时逐位相同，信号不变；
     如果交易所的K线是张数（Gate 规则），自动乘 contractSize，信号和"币的数量"数据完全一样；
     拿不到 contractSize 时不出信号（不瞎猜单位）。
  4. 可选 --datadir：用真实下载的数据核对 BTC 每天成交额的量级（是张数的话会大 100 倍）。

用法：
  .venv/bin/python scripts/check_volume_units.py
  .venv/bin/python scripts/check_volume_units.py --datadir user_data/data/okx    # VPS 上下载数据之后
  （make guard-test 会自动跑第 1~3 项）
退出码：0 = 全部通过；1 = 有检查未通过。
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from collections.abc import Callable
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR / "user_data" / "strategies"))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
from freqtrade.enums import CandleType, RunMode  # noqa: E402


PAIR = "BTC/USDT:USDT"
CT = 0.01  # OKX BTC-USDT-SWAP 的合约面值: 1 张 = 0.01 BTC
# 一根 OKX BTC 永续日线的原始返回: [时间, 开, 高, 低, 收, vol(张), volCcy(币), volCcyQuote(USDT), 是否收盘]
# 1250 张 x 0.01 = 12.5 BTC; 12.5 BTC x 93500 ≈ 1,168,750 USDT
RAW_SWAP_ROW = ["1735689600000", "93000", "94000", "92500", "93500", "1250", "12.5", "1168750", "1"]
BASE_QTY, CONTRACTS = 12.5, 1250.0

CheckFn = Callable[..., None]
FAILS: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'通过' if ok else '失败'}  {name}" + (f"（{detail}）" if detail else ""))
    if not ok:
        FAILS.append(name)


def _swap_market(contract_size: float | None = CT) -> dict:
    """足够让 ccxt / 策略使用的最小 OKX 永续 market 结构。"""
    return {
        "id": "BTC-USDT-SWAP", "symbol": PAIR, "base": "BTC", "quote": "USDT", "settle": "USDT",
        "baseId": "BTC", "quoteId": "USDT", "settleId": "USDT",
        "type": "swap", "spot": False, "margin": False, "swap": True, "future": False,
        "option": False, "contract": True, "linear": True, "inverse": False,
        "contractSize": contract_size, "active": True,
        "precision": {"amount": 1.0, "price": 0.1},
        "limits": {"amount": {"min": 1.0, "max": None}, "price": {}, "cost": {}, "leverage": {}},
        "info": {"instId": "BTC-USDT-SWAP", "ctVal": str(contract_size)},
    }


# ---------------------------------------------------------------------------
# 1) ccxt
# ---------------------------------------------------------------------------


def check_ccxt(check: CheckFn, label: str = "1") -> None:
    import ccxt
    import ccxt.async_support
    import ccxt.pro

    print(f"{label}) ccxt {ccxt.__version__}：OKX 永续K线的 volume 是哪一列")
    market = _swap_market()
    ex = ccxt.okx()
    vol = ex.parse_ohlcvs([RAW_SWAP_ROW], market, "1d")[0][5]
    check(
        "永续：取 volCcy（币的数量 12.5 BTC），不是 vol（1250 张）",
        vol == BASE_QTY, f"得到 {vol}",
    )
    spot_vol = ex.parse_ohlcv(RAW_SWAP_ROW, dict(market, type="spot"))[5]
    check("对照：现货取第 6 列 vol（现货的 vol 本来就是币的数量）", spot_vol == CONTRACTS, f"得到 {spot_vol}")

    async def _async_paths() -> tuple[float, float]:
        # freqtrade 下载数据/回测用异步接口 (_api_async.fetch_ohlcv); 实盘行情用 websocket (ccxt.pro)
        a = ccxt.async_support.okx()
        p = ccxt.pro.okx()
        try:
            a_vol = a.parse_ohlcvs([RAW_SWAP_ROW], market, "1d")[0][5]
            p.set_markets([market])

            class _Client:
                def resolve(self, *args, **kwargs):
                    return None

            p.handle_ohlcv(
                _Client(), {"arg": {"channel": "candle1D", "instId": "BTC-USDT-SWAP"}, "data": [RAW_SWAP_ROW]}
            )
            ws_vol = p.ohlcvs[PAIR]["1d"][-1][5]
        finally:
            await a.close()
            await p.close()
        return a_vol, ws_vol

    try:
        a_vol, ws_vol = asyncio.run(_async_paths())
    except Exception as exc:  # ccxt 内部结构变了: 当作失败, 让人去看
        check("异步接口 / websocket 也是币的数量", False, f"运行出错：{exc!r}")
        return
    check("下载历史数据用的异步接口：同样是币的数量", a_vol == BASE_QTY, f"得到 {a_vol}")
    check("实盘 websocket 行情：同样是币的数量", ws_vol == BASE_QTY, f"得到 {ws_vol}")


# ---------------------------------------------------------------------------
# 2) freqtrade
# ---------------------------------------------------------------------------


def check_freqtrade(check: CheckFn, label: str = "2") -> None:
    import freqtrade
    from freqtrade.data.converter import ohlcv_to_dataframe
    from freqtrade.exchange.exchange import Exchange
    from freqtrade.exchange.gate import Gate
    from freqtrade.exchange.okx import Myokx, Okx

    print(f"{label}) freqtrade {freqtrade.__version__}：有没有把K线成交量当成张数再换算")
    default = Exchange._ft_has_default
    check(
        "默认：K线成交量是币的数量（ohlcv_volume_in_contracts = False, ohlcv_volume_currency = base）",
        default.get("ohlcv_volume_in_contracts") is False and default.get("ohlcv_volume_currency") == "base",
    )
    for cls in (Okx, Myokx):
        h = cls.combine_ft_has(include_futures=True)
        check(
            f"{cls.__name__} 合约：没有标记成张数",
            h.get("ohlcv_volume_in_contracts") is False and h.get("ohlcv_volume_currency") == "base",
            f"ohlcv_volume_in_contracts={h.get('ohlcv_volume_in_contracts')}",
        )
    g = Gate.combine_ft_has(include_futures=True)
    check(
        "对照：Gate 合约标记为张数（说明这项检查确实能看出区别）",
        g.get("ohlcv_volume_in_contracts") is True,
    )
    import ccxt

    row = ccxt.okx().parse_ohlcv(RAW_SWAP_ROW, _swap_market())
    df = ohlcv_to_dataframe(
        [row], "1d", PAIR, fill_missing=False, drop_incomplete=False, candle_type=CandleType.FUTURES
    )
    v = float(df["volume"].iloc[0])
    check("转成 DataFrame 时 volume 原样保留（不乘、不除合约面值）", v == BASE_QTY, f"得到 {v}")


# ---------------------------------------------------------------------------
# 3) 策略
# ---------------------------------------------------------------------------


class _FakeExchange:
    def __init__(self, ft_has: dict, contract_size: float | None):
        self._ft_has = ft_has
        self.markets = {PAIR: _swap_market(contract_size)}

    def get_option(self, param: str, default=None):
        return self._ft_has.get(param, default)


class _FakeDP:
    """只提供策略算指标时会用到的几个方法。"""

    def __init__(self, exchange: _FakeExchange | None):
        self._exchange = exchange

    def market(self, pair: str):
        return self._exchange.markets.get(pair) if self._exchange else None

    def get_pair_dataframe(self, pair: str, timeframe: str | None = None, candle_type: str = ""):
        return pd.DataFrame()

    def current_whitelist(self) -> list[str]:
        return [PAIR]


def _make_strategy(cls, exchange: _FakeExchange | None):
    cfg = {
        "runmode": RunMode.BACKTEST,
        "dry_run": True,
        "stake_currency": "USDT",
        "trading_mode": "futures",
        "margin_mode": "isolated",
        "candle_type_def": "futures",
        "exchange": {"name": "okx"},
    }
    s = cls(cfg)
    s.dp = _FakeDP(exchange)
    s.use_mtf_policy = False  # 只测成交量这一项, 多周期过滤关掉 (它不看成交量)
    return s


def _spike_frame(volume_scale: float = 1.0) -> pd.DataFrame:
    """60 根 5m: 平时每根 1 BTC (约 6 万 USDT), 第 45 根放量 100 倍收阴 -> VolumeSpikeShort 应做空。"""
    n = 60
    close = 60_000.0 + 50.0 * np.sin(np.arange(n))
    open_ = close + np.where(np.arange(n) % 2 == 0, 20.0, -20.0)
    vol = np.ones(n)
    open_[45], close[45], vol[45] = 60_300.0, 60_000.0, 100.0
    return pd.DataFrame(
        {
            "date": pd.date_range("2026-01-01", periods=n, freq="5min", tz="UTC"),
            "open": open_, "high": np.maximum(open_, close) + 30.0, "low": np.minimum(open_, close) - 30.0,
            "close": close, "volume": vol * volume_scale,
        }
    )


def _crows_frame(volume_scale: float = 1.0) -> pd.DataFrame:
    """40 根日线: 每天 1000 BTC (约 6000 万 USDT), 最后 3 根三连阴 -> DailyThreeSoldiersShort 应做空。"""
    n = 40
    even = np.arange(n) % 2 == 0
    close = 60_000.0 + np.where(even, 200.0, -200.0)
    open_ = close + np.where(even, -300.0, 300.0)  # 阳阴交替, 前面不会凑出三连阴
    for i, (o, c) in zip(range(n - 3, n), ((61_000.0, 60_500.0), (60_400.0, 59_800.0), (59_700.0, 59_000.0))):
        open_[i], close[i] = o, c
    return pd.DataFrame(
        {
            "date": pd.date_range("2025-11-01", periods=n, freq="1D", tz="UTC"),
            "open": open_, "high": np.maximum(open_, close) + 100.0, "low": np.minimum(open_, close) - 100.0,
            "close": close, "volume": np.full(n, 1000.0) * volume_scale,
        }
    )


def _run(cls, exchange: _FakeExchange | None, frame: pd.DataFrame) -> pd.DataFrame:
    s = _make_strategy(cls, exchange)
    df = s.populate_indicators(frame.copy(), {"pair": PAIR})
    return s.populate_entry_trend(df, {"pair": PAIR})


class _ListHandler(logging.Handler):
    def __init__(self):
        super().__init__(logging.ERROR)
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage())


def check_strategies(check: CheckFn, label: str = "3") -> None:
    from freqtrade.exchange.gate import Gate
    from freqtrade.exchange.okx import Okx

    from DailyThreeSoldiersShort import DailyThreeSoldiersShort
    from RustBaselines import DailyThreeSoldiersShortRustBaseline, VolumeSpikeShortRustBaseline
    from VolumeSpikeShort import VolumeSpikeShort

    print(f"{label}) 策略：假交易对 {PAIR}，合约面值 contractSize = {CT}")
    okx_rule = Okx.combine_ft_has(include_futures=True)
    gate_rule = Gate.combine_ft_has(include_futures=True)
    cases = (
        (VolumeSpikeShort, VolumeSpikeShortRustBaseline, _spike_frame, "quote_volume", 45,
         "成交额 6 万 USDT/根 ≥ 5 万门槛"),
        (DailyThreeSoldiersShort, DailyThreeSoldiersShortRustBaseline, _crows_frame, "notional_b3", 39,
         "成交额 6000 万 USDT ≥ 500 万门槛"),
    )
    for cls, baseline_cls, frame_fn, col, sig_row, why in cases:
        name = cls.__name__
        base = frame_fn()
        expected = base["close"] * base["volume"]
        okx = _run(cls, _FakeExchange(okx_rule, CT), base)
        okx_cs1 = _run(cls, _FakeExchange(okx_rule, 1.0), base)
        no_ex = _run(cls, None, base)
        check(
            f"{name}：OKX 规则下成交额 = 收盘价 x volume，不乘 0.01",
            np.array_equal(okx[col].to_numpy(), expected.to_numpy()),
            f"第 {sig_row} 根 {okx[col].iloc[sig_row]:,.0f} USDT",
        )
        check(
            f"{name}：OKX 规则下 contractSize = 0.01 和 = 1 的结果完全相同",
            okx.equals(okx_cs1) and okx.equals(no_ex),
        )
        check(
            f"{name}：信号照常出现（{why}）",
            int(okx["enter_short"].iloc[sig_row]) == 1 and int(okx["enter_short"].sum()) == 1,
            f"做空信号 {int(okx['enter_short'].sum())} 个",
        )
        # 假设换到 "K线成交量是张数" 的交易所: 同一段行情, volume 写成张数 (币的数量 / 0.01)
        contracts = frame_fn(volume_scale=1.0 / CT)
        gate = _run(cls, _FakeExchange(gate_rule, CT), contracts)
        check(
            f"{name}：K线是张数时（Gate 规则）自动乘 contractSize，成交额和信号与上面一致",
            np.allclose(gate[col].to_numpy(), expected.to_numpy(), rtol=1e-12, equal_nan=True)
            and gate["enter_short"].equals(okx["enter_short"]),
        )
        gate_cs1 = _run(cls, _FakeExchange(gate_rule, 1.0), base)
        check(
            f"{name}：contractSize = 1 时张数规则和币数规则完全相同",
            gate_cs1.equals(okx),
        )
        rpc_logger = logging.getLogger("rust_port_common")
        handler = _ListHandler()
        old_level, old_propagate = rpc_logger.level, rpc_logger.propagate
        rpc_logger.addHandler(handler)
        rpc_logger.setLevel(logging.ERROR)
        rpc_logger.propagate = False
        try:
            missing = _run(cls, _FakeExchange(gate_rule, None), contracts)
        finally:
            rpc_logger.removeHandler(handler)
            rpc_logger.setLevel(old_level)
            rpc_logger.propagate = old_propagate
        check(
            f"{name}：K线是张数却拿不到 contractSize → 不出信号并报错（不瞎猜单位）",
            missing[col].isna().all() and int(missing["enter_short"].sum()) == 0
            and any("contractSize" in m for m in handler.messages),
        )
        # RustBaselines 里的对照版继承同一个算法: 成交额列逐位相同 (仓位/止损参数不同, 但它们不影响这一列)
        bl_okx = _run(baseline_cls, _FakeExchange(okx_rule, CT), base)
        bl_gate = _run(baseline_cls, _FakeExchange(gate_rule, CT), contracts)
        check(
            f"{baseline_cls.__name__}（对照版）：两种规则下成交额和信号都与 {name} 相同",
            bl_okx[col].equals(okx[col]) and bl_okx["enter_short"].equals(okx["enter_short"])
            and bl_gate[col].equals(gate[col]) and bl_gate["enter_short"].equals(gate["enter_short"]),
        )


def run_checks(check_fn: CheckFn = check, prefix: str = "") -> None:
    """第 1~3 项（不需要数据）。scripts/check_live_guards.py 也会调用它（prefix="4." -> 4.1) 4.2) 4.3)）。"""
    check_ccxt(check_fn, f"{prefix}1")
    check_freqtrade(check_fn, f"{prefix}2")
    check_strategies(check_fn, f"{prefix}3")


# ---------------------------------------------------------------------------
# 4) 真实数据核对 (可选)
# ---------------------------------------------------------------------------


def check_datadir(check_fn: CheckFn, datadir: Path, days: int = 180, label: str = "4") -> None:
    from freqtrade.data.history import load_pair_history

    print(f"{label}) 真实数据：{datadir} 里 {PAIR} 最近 {days} 天每天的成交额")
    df = load_pair_history(
        pair=PAIR, timeframe="1d", datadir=datadir, candle_type=CandleType.FUTURES, fill_up_missing=False
    )
    if df.empty:
        h1 = load_pair_history(
            pair=PAIR, timeframe="1h", datadir=datadir, candle_type=CandleType.FUTURES, fill_up_missing=False
        )
        if not h1.empty:
            qv = (h1["close"] * h1["volume"]).groupby(h1["date"].dt.floor("1D")).sum()
            daily = qv[qv.index >= qv.index.max() - pd.Timedelta(days=days)]
        else:
            daily = pd.Series(dtype="float64")
    else:
        df = df[df["date"] >= df["date"].max() - pd.Timedelta(days=days)]
        daily = df["close"] * df["volume"]
    daily = daily[daily > 0]
    if daily.empty:
        check_fn(f"找到 {PAIR} 的 1d 或 1h 合约数据", False, f"{datadir} 里没有；先下载数据再跑")
        return
    med = float(daily.median())
    synthetic = (datadir / "SYNTHETIC_DATA_README.txt").is_file()
    note = "（合成数据，数字只用来演示）" if synthetic else ""
    # OKX BTC 永续正常每天几十亿到一两百亿 USDT; 如果 volume 是张数 (x100) 会变成几千亿到上万亿
    if med > 1e11:
        why = "超过 1000 亿：很可能是张数（大了 100 倍）"
    elif med < 2e8:
        why = "低于 2 亿：数据不全或单位不对"
    else:
        why = ""
    check_fn(
        f"BTC 每天成交额中位数约 {med / 1e8:,.1f} 亿 USDT，在 2 亿 ~ 1000 亿之间 = volume 是币的数量{note}",
        2e8 <= med <= 1e11,
        why,
    )


def main() -> int:
    ap = argparse.ArgumentParser(description="K线成交量单位自检（OKX 永续 volume = 币的数量）")
    ap.add_argument("--datadir", type=Path, help="可选：已下载的 OKX 数据目录，例如 user_data/data/okx")
    ap.add_argument("--days", type=int, default=180, help="第 4 项看最近多少天（默认 180）")
    args = ap.parse_args()

    run_checks(check)
    if args.datadir:
        datadir = args.datadir if args.datadir.is_absolute() else ROOT_DIR / args.datadir
        check_datadir(check, datadir, args.days)
    print()
    if FAILS:
        print(f"有 {len(FAILS)} 项未通过：")
        for f in FAILS:
            print(f"  - {f}")
        print("请先看 docs/okx-volume-units.md，确认成交量单位后再回测（流动性门槛依赖它）。")
        return 1
    print("全部通过。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
