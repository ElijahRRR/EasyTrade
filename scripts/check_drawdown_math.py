#!/usr/bin/env python3
"""
账户含浮亏回撤（scripts/check_drawdown.py）的算法自检：加仓 / 分批平仓的交易按逐笔订单重建持仓。

用法：
  .venv/bin/python scripts/check_drawdown_math.py            # 只跑手工例子（不需要任何数据，几秒钟）
  .venv/bin/python scripts/check_drawdown_math.py --result user_data/research/<目录>/bt/<…>/backtest-result-<时间>.zip
      # 另外拿一个真实回测结果核对：每笔加仓 / 分批平仓的交易，按订单重建出来的各次平仓盈亏加起来
      # = freqtrade 记的 profit_abs（扣掉资金费），最后剩下的仓位 = 0；有K线数据时再列出新旧两种算法的回撤
  .venv/bin/python scripts/check_drawdown_math.py --result <文件> --datadir user_data/data/okx

手工例子（1 小时K线，初始资金 1000 USDT；每个例子的权益曲线都是手算的）：
  1. 一次开仓、一次平仓的交易：和 2026-11-07 以前的算法逐位相同（本文件里冻结了一份旧算法）
  2. 多单加仓两次再一次平仓   3. 空单加仓（带手续费）   4. 多单分批平仓再全平
  5. 3 倍杠杆（杠杆只通过 数量 × 价格 进公式）   6. 还没平仓的交易（模拟盘的持仓）
  7. 分批平仓和整笔平仓在同一根K线   8. 没有订单记录 / 订单对不上 → 退回旧算法
  9. 走步拼接那样把 amount、profit 缩放   10. 没有 pandas 时的估算法
交易 dict 用 freqtrade 自己的 LocalTrade / Order 按顺序成交后 to_json() 生成（和回测结果、模拟盘数据库同一格式），
总盈亏也和 freqtrade 自己算的核对。

退出码：0 = 全部通过；1 = 有不对的。
"""

from __future__ import annotations

import argparse
import contextlib
import io
import math
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


SCRIPTS_DIR = Path(__file__).resolve().parent
ROOT_DIR = SCRIPTS_DIR.parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import numpy as np  # noqa: E402

import check_drawdown as cd  # noqa: E402


FAILS: list[str] = []
COUNT = 0
H = 3600
T0 = int(datetime(2025, 1, 1, tzinfo=timezone.utc).timestamp())
START = 1000.0
# trade_info 以前就有的字段（旧算法只用这些）
OLD_KEYS = ("pair", "open", "close", "dir", "amount", "open_rate", "close_rate", "fee_open", "fee_close", "profit",
            "min_rate", "max_rate", "stop_exit")


def _close(a, b, tol: float) -> bool:
    if a is None or b is None:
        return a is None and b is None
    if isinstance(a, (str, bool)) or isinstance(b, (str, bool)):
        return a == b
    a, b = float(a), float(b)
    if math.isinf(a) or math.isinf(b):
        return a == b
    return abs(a - b) <= tol


def _show(v):
    if isinstance(v, (list, tuple, np.ndarray)):
        return [_show(x) for x in v]
    if isinstance(v, (float, np.floating)):
        return round(float(v), 9)
    return v


def _flat(v):
    if isinstance(v, (list, tuple, np.ndarray)):
        return [y for x in v for y in _flat(x)]
    return [v]


def check(name: str, got, want, tol: float = 1e-9) -> None:
    global COUNT
    COUNT += 1
    if isinstance(want, (list, tuple)):
        got, want = (_flat(got) if got is not None else None), _flat(want)
        ok = got is not None and len(got) == len(want) and all(_close(a, b, tol) for a, b in zip(got, want))
    elif isinstance(want, (str, bool)):
        ok = got == want
    else:
        ok = _close(got, want, tol)
    print(f"  {'通过' if ok else '不对'}  {name}：得到 {_show(got)}，应为 {_show(want)}")
    if not ok:
        FAILS.append(name)


def h(k: float) -> float:
    return T0 + k * H


# ----------------------------------------------------------------------------
# K线和交易
# ----------------------------------------------------------------------------
CANDLES: dict[str, list[tuple[float, float, float, float]]] = {
    # 多单加仓两次：100 开 1 张，90 加 1 张，80 加 2 张，108 全平
    "AAA/USDT:USDT": [(100, 100, 100, 100), (100, 100, 90, 92), (92, 92, 80, 82), (82, 95, 82, 94),
                      (94, 110, 94, 108), (108, 108, 108, 108), (108, 108, 108, 108), (108, 108, 108, 108)],
    # 空单加仓：50 空 2 张，55 加 2 张，60 加 1 张，45 全平
    "BBB/USDT:USDT": [(50, 50, 50, 50), (50, 56, 50, 55), (55, 61, 55, 60), (60, 60, 45, 46), (46, 46, 44, 45),
                      (45, 45, 45, 45), (45, 45, 45, 45)],
    # 多单分批平仓：100 开 4 张，110 平 2 张，104 平剩下 2 张
    "CCC/USDT:USDT": [(100, 100, 100, 100), (100, 112, 100, 110), (110, 110, 96, 98), (98, 99, 90, 92),
                      (92, 105, 92, 104), (104, 104, 104, 104), (104, 104, 104, 104)],
    # 3 倍杠杆加仓：100 开 3 张（保证金 100），80 加 3.75 张（保证金 100），95 全平
    "DDD/USDT:USDT": [(100, 100, 100, 100), (100, 100, 78, 80), (80, 85, 75, 84), (84, 96, 84, 95),
                      (95, 95, 95, 95), (95, 95, 95, 95)],
    # 还没平仓：50 开 2 张，45 加 2 张，48 平 1 张，现在还拿着 3 张
    "EEE/USDT:USDT": [(50, 50, 50, 50), (50, 50, 46, 47), (47, 47, 44, 45), (45, 49, 45, 48), (48, 49, 47, 47.5)],
    # 同一根K线里先分批平 2 张、再平剩下的 2 张
    "KKK/USDT:USDT": [(100, 100, 100, 100), (100, 110, 100, 108), (108, 112, 108, 110), (110, 110, 110, 110),
                      (110, 110, 110, 110)],
    # 一次开平的老例子（和 check_portfolio_math.py 一样），再加一个中间缺K线的
    "OLD/USDT:USDT": [(100, 101, 100, 100), (100, 100, 95, 96), (96, 97, 90, 92), (92, 106, 98, 105),
                      (105, 106, 104, 105), (105, 106, 104, 105), (105, 106, 104, 105), (105, 106, 104, 105)],
    "SHO/USDT:USDT": [(50, 51, 49, 50), (50, 53, 50, 52), (52, 56, 51, 55), (55, 55, 48, 49),
                      (49, 50, 47, 48), (48, 49, 46, 47), (47, 48, 46, 47), (47, 48, 46, 47)],
}
GAP_PAIR = "GAP/USDT:USDT"  # 第 2、3 根K线缺失


def write_candles(d: Path) -> None:
    import pandas as pd

    (d / "futures").mkdir(parents=True, exist_ok=True)

    def write(pair: str, rows, hours) -> None:
        df = pd.DataFrame({
            "date": pd.to_datetime([h(k) for k in hours], unit="s", utc=True),
            "open": [r[0] for r in rows], "high": [r[1] for r in rows],
            "low": [r[2] for r in rows], "close": [r[3] for r in rows], "volume": 1.0,
        })
        df.to_feather(d / "futures" / f"{cd._pair_file(pair)}-1h-futures.feather")

    for pair, rows in CANDLES.items():
        write(pair, rows, range(len(rows)))
    gap_rows = [(10, 11, 9, 10), (10, 10, 8, 9), (9, 12, 9, 11), (11, 11, 10, 10.5), (10.5, 11, 10, 10.5)]
    write(GAP_PAIR, gap_rows, [0, 1, 4, 5, 6])


def ft_trade(pair: str, short: bool, lev: float, fee: float, fills: list[tuple[float, float, float, bool]], *,
             close: bool = True, exit_reason: str = "exit_signal", min_rate: float | None = None,
             max_rate: float | None = None, minified: bool = True) -> dict[str, Any]:
    """用 freqtrade 自己的 LocalTrade / Order 按顺序成交（和回测一样，每张单成交后重算一次），返回 to_json()。

    fills = [(第几小时, 数量, 成交价, 是不是开仓单)]。minified=True = 回测结果的格式，False = 模拟盘数据库的完整格式。
    """
    from freqtrade.enums import TradingMode
    from freqtrade.persistence import LocalTrade, Order

    def dt(k: float) -> datetime:
        return datetime.fromtimestamp(h(k), tz=timezone.utc)

    k0, a0, p0, _ = fills[0]
    t = LocalTrade(pair=pair, is_short=short, leverage=lev, fee_open=fee, fee_close=fee, open_rate=p0, amount=a0,
                   stake_amount=a0 * p0 / lev, open_date=dt(k0), exchange="binance",
                   trading_mode=TradingMode.FUTURES, is_open=True, contract_size=1.0)
    entry_side, exit_side = ("sell", "buy") if short else ("buy", "sell")
    for n, (k, amount, price, is_entry) in enumerate(fills):
        side = entry_side if is_entry else exit_side
        t.orders.append(Order(
            ft_order_side=side, ft_pair=pair, ft_is_open=False, ft_amount=amount, ft_price=price,
            order_id=str(n + 1), status="closed", symbol=pair, order_type="limit", side=side, price=price,
            average=price, amount=amount, filled=amount, remaining=0.0, cost=amount * price,
            order_date=dt(k), order_filled_date=dt(k), ft_order_tag=f"o{n + 1}",
        ))
        if close and not is_entry and n == len(fills) - 1:
            t.close_date = dt(k)
            t.exit_reason = exit_reason
            t.close(price, show_msg=False)
        else:
            t.recalc_trade_from_orders()
    prices = [p for _k, _a, p, _e in fills]
    t.min_rate = min_rate if min_rate is not None else min(prices)
    t.max_rate = max_rate if max_rate is not None else max(prices)
    return t.to_json(minified)


def plain_trade(pair: str, o: float, c: float, rate: float, close_rate: float, *, short: bool = False,
                amount: float = 1.0, fee: float = 0.0, reason: str = "exit_signal", with_orders: bool = False):
    """一次开平的交易（回测结果格式；with_orders=True 时带 2 张订单，和真实回测结果一样）。"""
    d = -1.0 if short else 1.0
    profit = d * amount * (close_rate - rate) - fee * amount * (rate + close_rate)
    t = {
        "pair": pair, "open_date": datetime.fromtimestamp(h(o), tz=timezone.utc).isoformat(),
        "close_date": datetime.fromtimestamp(h(c), tz=timezone.utc).isoformat(),
        "is_short": short, "amount": amount, "open_rate": rate, "close_rate": close_rate,
        "fee_open": fee, "fee_close": fee, "profit_abs": profit, "min_rate": rate, "max_rate": rate,
        "exit_reason": reason, "leverage": 2.0,
    }
    if with_orders:
        es, xs = ("sell", "buy") if short else ("buy", "sell")
        t["orders"] = [
            {"amount": amount, "safe_price": rate, "ft_order_side": es, "order_filled_timestamp": int(h(o) * 1000),
             "ft_is_entry": True, "ft_order_tag": "in", "cost": amount * rate},
            {"amount": amount, "safe_price": close_rate, "ft_order_side": xs,
             "order_filled_timestamp": int(h(c) * 1000), "ft_is_entry": False, "ft_order_tag": reason,
             "cost": amount * close_rate},
        ]
    return t


# ----------------------------------------------------------------------------
# 2026-11-07 以前 check_drawdown.mtm_drawdown_candles 的算法，原样冻结在这里（只用来核对"一次开平的交易逐位不变"；
# 不要改它，改了就失去意义）
# ----------------------------------------------------------------------------
def old_curve(trades, start_balance, grid_tf, candles):
    g_sec = cd._tf_seconds(grid_tf)
    t0 = int(min(t["open"] for t in trades) // g_sec * g_sec)
    t1 = int(max(t["close"] for t in trades) // g_sec * g_sec + g_sec)
    grid = np.arange(t0, t1 + 1, g_sec, dtype="int64")
    n = len(grid)
    unreal_low = np.zeros(n)
    unreal_end = np.zeros(n)
    closed_before = np.zeros(n)
    closed_end = np.zeros(n)
    for tr in trades:
        secs, high, low, close = candles[tr["pair"]]
        k0 = int(np.searchsorted(grid, tr["open"] - g_sec, side="right"))
        k1 = int(np.searchsorted(grid, tr["close"], side="left"))
        if k1 > k0:
            ts = grid[k0:k1]
            pos = np.searchsorted(secs, ts)
            pos = np.clip(pos, 0, len(secs) - 1)
            have = secs[pos] == ts
            bad = np.where(have, high[pos] if tr["dir"] < 0 else low[pos], tr["open_rate"])
            last = ts + g_sec > tr["close"]
            if tr["stop_exit"] and last.any():
                cap = tr["close_rate"]
                bad = np.where(last, np.minimum(bad, cap) if tr["dir"] < 0 else np.maximum(bad, cap), bad)
            unreal_low[k0:k1] += cd._upnl(tr, bad)
            hold = ts + g_sec < tr["close"]
            if hold.any():
                end_px = np.where(have, close[pos], tr["open_rate"])
                unreal_end[k0:k1] += np.where(hold, cd._upnl(tr, end_px), 0.0)
        kb = int(np.searchsorted(grid, tr["close"], side="left"))
        closed_before[kb:] += tr["profit"]
        ke = int(np.searchsorted(grid, tr["close"] - g_sec, side="left"))
        closed_end[ke:] += tr["profit"]
    e_low = start_balance + closed_before + unreal_low
    e_end = start_balance + closed_end + unreal_end
    peak = np.maximum.accumulate(np.concatenate([[start_balance], e_end]))[:-1]
    peak = np.maximum(peak, start_balance)
    dd = np.where(peak > 0, (peak - np.minimum(e_low, e_end)) / peak, 0.0)
    return grid, e_low, e_end, float(max(dd.max(), 0.0))


def curve(infos, d: Path, start: float = START) -> dict[str, Any]:
    c = cd.equity_curve_candles(infos, start, "1h", d, "feather", "futures")
    assert c is not None, "找不到K线"
    return c


def info(t: dict[str, Any], **kw) -> dict[str, Any]:
    i = cd.trade_info(t, **kw)
    assert i is not None, f"trade_info 读不出来：{t.get('pair')}"
    return i


# ----------------------------------------------------------------------------
def test_single_unchanged(d: Path) -> None:
    print("1. 一次开仓、一次平仓的交易：和以前的算法逐位相同")
    loaded = cd.load_candles(set(CANDLES) | {GAP_PAIR}, "1h", d, "feather", "futures")
    candles = loaded[1]
    # 每笔 = plain_trade 的参数
    sets: dict[str, list[dict[str, Any]]] = {
        "一笔多单": [dict(pair="OLD/USDT:USDT", o=0, c=4, rate=100, close_rate=105)],
        "多单 + 两笔空单（止损、手续费）": [
            dict(pair="OLD/USDT:USDT", o=0, c=4, rate=100, close_rate=105),
            dict(pair="SHO/USDT:USDT", o=1, c=3, rate=52, close_rate=55, short=True, amount=2.0, fee=0.0005,
                 reason="stop_loss"),
            dict(pair="SHO/USDT:USDT", o=3, c=5, rate=49, close_rate=47, short=True, amount=3.0, fee=0.0005)],
        "开平都在K线中间、止损在K线中间成交": [
            dict(pair="OLD/USDT:USDT", o=0.5, c=2.25, rate=100, close_rate=91, fee=0.0007, reason="stop_loss"),
            dict(pair="SHO/USDT:USDT", o=1.75, c=6.5, rate=52, close_rate=47.5, short=True, amount=1.5, fee=0.0007)],
        "持仓期间缺K线": [dict(pair=GAP_PAIR, o=0, c=6, rate=10, close_rate=10.5, amount=5.0, fee=0.001)],
    }
    for name, specs in sets.items():
        for with_orders in (False, True):
            ts = [plain_trade(**kw, with_orders=with_orders) for kw in specs]
            infos = [info(t) for t in ts]
            tag = f"{name}（{'带' if with_orders else '不带'}订单记录）"
            grid, lo, en, dd = old_curve(infos, START, "1h", candles)
            c = curve(infos, d)
            same = (c["grid"].tobytes() == grid.tobytes() and c["low"].tobytes() == lo.tobytes()
                    and c["end"].tobytes() == en.tobytes())
            check(f"{tag}：每根K线的最低 / 收盘权益逐位相同", same, True)
            ref = cd.mtm_drawdown_candles(infos, START, "1h", d, "feather", "futures")
            check(f"{tag}：回撤逐位相同", ref[0] == dd, True)
            check(f"{tag}：没有 pandas 时的估算也不变", cd.mtm_drawdown_bound(infos, START) == cd.mtm_drawdown_bound(
                [{k: v for k, v in i.items() if k in OLD_KEYS} for i in infos], START), True)
            if with_orders:
                check(f"{tag}：标成 single（一次开平）", ",".join(i["orders_mode"] for i in infos),
                      ",".join("single" for _ in infos))
                # 强制走"按订单重建"的路径：结果应该一样（浮点误差以内）
                c2 = curve([info(t, rebuild="always") for t in ts], d)
                check(f"{tag}：按订单重建的路径结果相同（最低）", c2["low"], list(lo), 1e-9)
                check(f"{tag}：按订单重建的路径结果相同（收盘）", c2["end"], list(en), 1e-9)


def test_long_dca(d: Path) -> None:
    print("2. 多单加仓两次再一次平仓（100 开 1 张、90 加 1 张、80 加 2 张，均价 87.5，108 全平 4 张）")
    t = ft_trade("AAA/USDT:USDT", False, 1.0, 0.0,
                 [(0, 1, 100, True), (1, 1, 90, True), (2, 2, 80, True), (5, 4, 108, False)], min_rate=80)
    check("freqtrade 自己算的总盈亏 = 4 × (108 - 87.5) = 82", t["profit_abs"], 82.0)
    check("freqtrade 记的最后数量 / 均价 = 4 张 / 87.5", [t["amount"], t["open_rate"]], [4, 87.5])
    i = info(t)
    check("标成 multi（按订单重建）", i["orders_mode"], "multi")
    check("重建的持仓：1 张@100 → 2 张@95 → 4 张@87.5",
          [(lg[2], lg[3]) for lg in i["legs"]], [(1, 100), (2, 95), (4, 87.5)])
    c = curve([i], d)
    # 第 0 根收盘：90 的加仓是下一根K线才成交的，不能按 100 的收盘价算成 +10 的浮盈
    check("每根K线最低权益", c["low"], [1000, 990, 970, 978, 1026, 1082, 1082])
    check("每根K线收盘权益（第 0 根没有把下一根才成交的加仓算进去）", c["end"], [1000, 994, 978, 1026, 1082, 1082, 1082])
    check("回撤 = (1000 - 970) / 1000 = 3%", cd.curve_max_dd(c), 0.03)
    old = curve([info(t, rebuild="never")], d)
    check("对照：旧算法（4 张@87.5 套整个持仓期间，第 0 根就有 +50 的假浮盈）回撤 = 80 / 1050",
          cd.curve_max_dd(old), 80 / 1050)


def test_short_dca(d: Path) -> None:
    print("3. 空单加仓（50 空 2 张、55 加 2 张、60 加 1 张，均价 54，45 全平；手续费每边 0.1%）")
    fills = [(0, 2, 50, True), (1, 2, 55, True), (2, 1, 60, True), (4, 5, 45, False)]
    t = ft_trade("BBB/USDT:USDT", True, 2.0, 0.001, fills, max_rate=61)
    want = 5 * (54 - 45) - 0.001 * 5 * 54 - 0.001 * 5 * 45
    check("freqtrade 自己算的总盈亏 = 5 × (54 - 45) - 手续费 0.27 - 0.225 = 44.505", t["profit_abs"], want)
    i = info(t)
    check("重建的持仓：2 张@50 → 4 张@52.5 → 5 张@54", [(lg[2], lg[3]) for lg in i["legs"]],
          [(2, 50), (4, 52.5), (5, 54)])
    c = curve([i], d)
    # 例：第 1 根最低 = 4 张空单在最高价 56：-4 × 3.5 - 0.001 × 4 × 52.5 - 0.001 × 4 × 56 = -14.434
    check("每根K线最低权益（空单按最高价）", c["low"], [999.8, 985.566, 964.425, 969.43, 1044.505, 1044.505])
    check("每根K线收盘权益", c["end"], [999.8, 989.57, 969.43, 1044.505, 1044.505, 1044.505])
    check("回撤 = 35.575 / 1000", cd.curve_max_dd(c), 0.035575)
    check("对照：旧算法回撤 = (1019.48 - 964.425) / 1019.48", cd.curve_max_dd(curve([info(t, rebuild="never")], d)),
          (1019.48 - 964.425) / 1019.48)


def test_partial_exit(d: Path) -> None:
    print("4. 多单分批平仓（100 开 4 张，110 平 2 张 +20，104 平剩下 2 张 +8）")
    t = ft_trade("CCC/USDT:USDT", False, 1.0, 0.0, [(0, 4, 100, True), (2, 2, 110, False), (4, 2, 104, False)],
                 min_rate=90)
    check("freqtrade 自己算的总盈亏 = 20 + 8 = 28", t["profit_abs"], 28.0)
    check("freqtrade 记的最后数量 = 2 张（最后一次平仓前剩下的）", t["amount"], 2)
    i = info(t)
    check("分批平仓的盈亏记在成交那一刻（第 2 小时 +20）", [(p[0] - T0) / H for p in i["partials"]] + [i["partials"][0][1]],
          [2, 20])
    c = curve([i], d)
    # 第 1 根收盘：4 张都还在，收盘价 110 → +40；第 2 根：已实现 +20，剩 2 张在最低 96 → -8
    check("每根K线最低权益", c["low"], [1000, 1000, 1012, 1000, 1028, 1028])
    check("每根K线收盘权益", c["end"], [1000, 1040, 1016, 1028, 1028, 1028])
    check("回撤 = (1040 - 1000) / 1040", cd.curve_max_dd(c), 40 / 1040)
    check("对照：旧算法（2 张套整个持仓期间）回撤 = 40 / 1020", cd.curve_max_dd(curve([info(t, rebuild="never")], d)),
          40 / 1020)
    # 走步拼接 / 子账户：调用方把 amount 和 profit 按同一比例缩放，重建的持仓和分批盈亏跟着缩放
    s = info(t)
    s["amount"] *= 0.5
    s["profit"] *= 0.5
    cs = curve([s], d)
    check("amount、profit 都乘 0.5：权益相对初始资金的变化正好减半（最低）", cs["low"] - START, list((c["low"] - START) * 0.5))
    check("amount、profit 都乘 0.5：（收盘）", cs["end"] - START, list((c["end"] - START) * 0.5))
    check("没有 pandas 时的估算：4 张在最差价 90 → 浮亏 40 → 4%", cd.mtm_drawdown_bound([i], START), 0.04)


def test_leverage(d: Path) -> None:
    print("5. 3 倍杠杆加仓（保证金 100 开 3 张@100，再 100 加 3.75 张@80，均价 88.89，95 全平；手续费每边 0.05%）")
    fills = [(0, 3, 100, True), (1, 3.75, 80, True), (3, 6.75, 95, False)]
    t = ft_trade("DDD/USDT:USDT", False, 3.0, 0.0005, fills, min_rate=75)
    want = 6.75 * 95 - 600 - 0.0005 * 600 - 0.0005 * 6.75 * 95
    check("freqtrade 自己算的总盈亏 = 641.25 - 600 - 0.3 - 0.320625 = 40.629375", t["profit_abs"], want)
    i = info(t)
    c = curve([i], d)
    # 第 2 根最低：6.75 张在 75：6.75 × 75 - 600 - 0.3 - 0.0005 × 6.75 × 75 = -94.303125
    check("每根K线最低权益", c["low"], [999.7, 925.93675, 905.696875, 1040.629375, 1040.629375])
    check("每根K线收盘权益", c["end"], [999.7, 939.43, 1040.629375, 1040.629375, 1040.629375])
    check("回撤 = 94.303125 / 1000", cd.curve_max_dd(c), 0.094303125)
    t1 = ft_trade("DDD/USDT:USDT", False, 1.0, 0.0005, fills, min_rate=75)
    c1 = curve([info(t1)], d)
    check("同样的数量和价格、杠杆写成 1 倍：权益曲线完全一样（杠杆只影响保证金，不影响盈亏）",
          c1["low"].tobytes() == c["low"].tobytes() and c1["end"].tobytes() == c["end"].tobytes(), True)
    old = cd.curve_max_dd(curve([info(t, rebuild="never")], d))
    check("对照：旧算法回撤 = (1074.3625 - 905.696875) / 1074.3625（超过 15%，新算法 9.43%）", old,
          (1074.3625 - 905.696875) / 1074.3625)
    # 同一组订单用模拟盘数据库的完整格式（filled、is_open、funding_fee 等字段）也一样
    full = ft_trade("DDD/USDT:USDT", False, 3.0, 0.0005, fills, min_rate=75, minified=False)
    cf = curve([info(full)], d)
    check("模拟盘数据库的完整格式（to_json 不精简）：结果相同", cf["low"].tobytes() == c["low"].tobytes()
          and cf["end"].tobytes() == c["end"].tobytes(), True)


def test_open_trade(d: Path) -> None:
    print("6. 还没平仓的交易（50 开 2 张，45 加 2 张，48 平 1 张；现在第 4 小时半，还拿着 3 张@47.5）")
    fills = [(0, 2, 50, True), (2, 2, 45, True), (3, 1, 48, False)]
    t = ft_trade("EEE/USDT:USDT", False, 2.0, 0.0, fills, close=False, min_rate=44)
    check("freqtrade：还没平仓、剩 3 张、均价 47.5", [t["is_open"], t["amount"], t["open_rate"]], [True, 3, 47.5])
    check("不给 open_until 时跳过（和以前一样）", cd.trade_info(t) is None, True)
    as_of = h(4.5)
    i = info(t, open_until=as_of)
    check("标成 open，已实现的分批盈亏 = 1 × (48 - 47.5) = 0.5", [i["orders_mode"], i["profit"]], ["open", 0.5])
    r = cd.account_equity_drawdown([t], START, "1h", d, as_of=as_of)
    c = r["curve"]
    check("K线只到 as_of 之前开始的那根（第 0 ~ 4 小时）", [int(x) for x in (c["grid"] - T0) // H], [0, 1, 2, 3, 4])
    check("每根K线最低权益", c["low"], [1000, 992, 986, 993, 999])
    check("每根K线收盘权益（第 4 根 = 现在：已实现 0.5 + 3 张在 47.5 → 1000.5）", c["end"], [1000, 994, 990, 1002, 1000.5])
    check("历史最大回撤 = 14 / 1000", r["dd"], 0.014)
    check("现在的权益 / 峰值 / 现在的回撤 = 1000.5 / 1002 / 3 ÷ 1002",
          [r["equity_now"], r["peak"], r["dd_now"]], [1000.5, 1002, 3 / 1002])
    check("只按已平仓余额的回撤 = 0（还没有平仓的交易）", r["dd_closed"], 0.0)
    check("orders 统计：1 笔 open", r["orders"]["open"], 1)
    check("没有K线数据时退回估算（最差价 44：4 张@47.5 → -14 → 1.4%）",
          [cd.account_equity_drawdown([t], START, "1h", None, as_of=as_of)[k] for k in ("method", "dd")], ["估", 0.014])
    # 回测结束时被强制平掉（force_exit）的同一笔：就是一笔普通的已平仓交易
    tf = ft_trade("EEE/USDT:USDT", False, 2.0, 0.0, fills + [(4, 3, 47.5, False)], exit_reason="force_exit", min_rate=44)
    cf = curve([info(tf)], d)
    check("回测结束时强制平仓的版本：总盈亏 0.5，最后的权益 1000.5", [tf["profit_abs"], cf["end"][-1]], [0.5, 1000.5])
    check("强制平仓版本：平仓前的权益和没平仓的一样", cf["low"][:4], list(c["low"][:4]))


def test_same_candle(d: Path) -> None:
    print("7. 分批平仓和整笔平仓在同一根K线（100 开 4 张，第 2 小时 110 先平 2 张、再平 2 张）")
    t = ft_trade("KKK/USDT:USDT", False, 1.0, 0.0, [(0, 4, 100, True), (2, 2, 110, False), (2, 2, 110, False)])
    check("freqtrade 自己算的总盈亏 = 40", t["profit_abs"], 40.0)
    c = curve([info(t)], d)
    check("每根K线最低权益", c["low"], [1000, 1000, 1040, 1040])
    check("每根K线收盘权益（第 1 根收盘时两次平仓都算已平仓：+40，不是只有 +20）", c["end"], [1000, 1040, 1040, 1040])


def test_fallback(d: Path) -> None:
    print("8. 没有订单记录 / 订单对不上：退回旧算法，并且只提醒一次")
    t = ft_trade("AAA/USDT:USDT", False, 1.0, 0.0,
                 [(0, 1, 100, True), (1, 1, 90, True), (2, 2, 80, True), (5, 4, 108, False)], min_rate=80)
    no_orders = {k: v for k, v in t.items() if k != "orders"}
    bad = dict(t, orders=[dict(o) for o in t["orders"]])
    bad["orders"][-1]["amount"] = 3  # 平完还剩 1 张：对不上
    cd._ORDERS_NOTE_TO_STDERR, cd._ORDERS_NOTE_DONE = True, False
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        a = info(no_orders)
        info(no_orders)
    check("没有 orders：标成 missing", a["orders_mode"], "missing")
    check("没有 orders：stderr 只提醒一次", err.getvalue().count("没有逐笔订单记录"), 1)
    check("没有 orders：回撤 = 旧算法 80 / 1050", cd.curve_max_dd(curve([a], d)), 80 / 1050)
    b = info(bad)
    check("订单对不上：标成 bad，按旧算法", [b["orders_mode"], "legs" in b], ["bad", False])
    st = {"trades": [t], "starting_balance": START, "timeframe": "1h", "total_trades": 1, "profit_total": 0.082,
          "max_drawdown_account": 0.0}
    cfg = {"exchange": {"name": "test"}, "trading_mode": "futures", "dataformat_ohlcv": "feather"}
    r_ok = cd.evaluate("AAA", st, Path("x.zip"), 15.0, 5.0, 1, cfg, d)
    r_no = cd.evaluate("AAA", dict(st, trades=[no_orders]), Path("y.zip"), 15.0, 5.0, 1, cfg, d)
    r_bad = cd.evaluate("AAA", dict(st, trades=[bad]), Path("z.zip"), 15.0, 5.0, 1, cfg, d)
    check("check_drawdown 的表格：有订单 3%、没有订单 7.62%、对不上 7.62%",
          [r_ok["dd_mtm"], r_no["dd_mtm"], r_bad["dd_mtm"]], [0.03, 80 / 1050, 80 / 1050])
    check("表格里的计数：orders_multi / orders_missing / orders_bad",
          [r_ok["orders_multi"], r_no["orders_missing"], r_bad["orders_bad"]], [1, 1, 1])
    text = cd.render_table([r_ok, r_no, r_bad], 15.0, 5.0)
    check("表格末尾说一次\"没有逐笔订单记录\"", text.count("没有逐笔订单记录"), 1)
    check("订单对不上的那一行有提醒", any("对不上" in n for n in r_bad["notes"]), True)
    cd._ORDERS_NOTE_TO_STDERR, cd._ORDERS_NOTE_DONE = False, False


def test_mixed(d: Path) -> None:
    print("9. 一次开平和加仓的交易放在同一个账户：一次开平的那几笔贡献不变")
    singles = [plain_trade("OLD/USDT:USDT", 0, 4, 100, 105, with_orders=True),
               plain_trade("SHO/USDT:USDT", 1, 3, 52, 55, short=True, amount=2.0, fee=0.0005, reason="stop_loss",
                           with_orders=True)]
    multi = ft_trade("AAA/USDT:USDT", False, 1.0, 0.0,
                     [(0, 1, 100, True), (1, 1, 90, True), (2, 2, 80, True), (5, 4, 108, False)])
    both = curve([info(x) for x in singles + [multi]], d)
    only_m = curve([info(multi)], d)
    loaded = cd.load_candles(set(CANDLES), "1h", d, "feather", "futures")[1]
    _g, lo, en, _dd = old_curve([info(x) for x in singles], START, "1h", loaded)
    n = min(len(lo), len(both["low"]))
    check("合在一起的曲线 - 只有加仓那笔的曲线 = 一次开平那几笔的旧算法曲线（最低）",
          (both["low"][:n] - only_m["low"][:n]), list(lo[:n] - START), 1e-9)
    check("（收盘）", (both["end"][:n] - only_m["end"][:n]), list(en[:n] - START), 1e-9)
    # 开仓、加仓、平仓全在同一根K线里成交：没有持仓时段，只在平仓时记盈亏（和旧算法对同一时刻开平的交易一样）
    same = ft_trade("AAA/USDT:USDT", False, 1.0, 0.0, [(5, 1, 108, True), (5, 1, 108, True), (5, 2, 108, False)])
    i = info(same)
    cs = curve([i], d)
    check("同一根K线里开、加、平：没有持仓时段，权益一直是 1000", [i["orders_mode"], len(i["legs"]), float(cs["low"].min()),
          float(cs["end"].max())], ["multi", 0, 1000, 1000])


def test_rebuild_vs_freqtrade() -> None:
    print("10. 按订单重建的各次平仓盈亏合计 = freqtrade 自己算的 profit_abs，最后的均价 = freqtrade 的 open_rate")
    cases = {
        "多单加仓": (False, 1.0, 0.0007, [(0, 1, 100, True), (1, 1.3, 91, True), (2, 2.2, 83, True), (5, 4.5, 99, False)]),
        "空单加仓 + 分批平仓": (True, 3.0, 0.0005, [(0, 2, 50, True), (1, 1.5, 54, True), (2, 1, 47, False),
                                               (3, 1.25, 58, True), (4, 3.75, 49, False)]),
        "多单分批平三次": (False, 5.0, 0.001, [(0, 10, 20, True), (1, 3, 21, False), (2, 3, 19.5, False),
                                         (3, 2, 22, True), (4, 6, 23, False)]),
    }
    for name, (short, lev, fee, fills) in cases.items():
        t = ft_trade("AAA/USDT:USDT", short, lev, fee, fills)
        rb = cd.rebuild_position(t)
        check(f"{name}：各次平仓盈亏加起来 = freqtrade 的 profit_abs", sum(p for _ts, p in rb["exits"]), t["profit_abs"], 1e-7)
        check(f"{name}：最后剩下 0 张", rb["remaining"], 0.0, 1e-9)
        check(f"{name}：最后一段的均价 = freqtrade 记的 open_rate", rb["legs"][-1][3], t["open_rate"], 1e-9)


# ----------------------------------------------------------------------------
# 拿真实回测结果核对（可选）
# ----------------------------------------------------------------------------
def check_result(path: Path, datadir: Path | None) -> None:
    print(f"11. 回测结果 {path}")
    stats = cd.load_result_file(path)
    if not stats:
        check("读得出回测结果", False, True)
        return
    cfg = cd.load_result_config(path)
    if datadir is None:
        exch = str((cfg.get("exchange") or {}).get("name") or "").lower()
        datadir = ROOT_DIR / "user_data" / "data" / exch if exch else None
    for name, st in stats["strategy"].items():
        trades = st.get("trades") or []
        modes: dict[str, int] = {}
        worst_sum = worst_flat = 0.0
        amount_mismatch = 0
        for t in trades:
            rb = cd.rebuild_position(t)
            modes[rb["status"]] = modes.get(rb["status"], 0) + 1
            if rb["status"] != "multi":
                continue
            total = sum(p for _ts, p in rb["exits"]) + float(t.get("funding_fees") or 0.0)
            worst_sum = max(worst_sum, abs(total - float(t.get("profit_abs") or 0.0)) / max(1.0, rb["n_exits"]))
            worst_flat = max(worst_flat, abs(rb["remaining"]) / rb["max_amount"])
            exits = [o for o in t["orders"] if not o.get("ft_is_entry")]
            if exits and not math.isclose(float(exits[-1]["amount"]), float(t["amount"]), rel_tol=1e-9):
                amount_mismatch += 1
        print(f"    {name}：{len(trades)} 笔交易，" + "，".join(f"{k} {v} 笔" for k, v in sorted(modes.items())))
        check(f"{name}：没有订单对不上的交易（bad / 平仓后还有仓位）", modes.get("bad", 0) + modes.get("open", 0)
              + modes.get("empty", 0), 0)
        if modes.get("multi"):
            check(f"{name}：每笔加仓 / 分批平仓交易，重建的平仓盈亏合计 + 资金费 = profit_abs（每张平仓单误差 < 1e-6）",
                  worst_sum, 0.0, 1e-6)
            check(f"{name}：重建后最后剩下的仓位 = 0", worst_flat, 0.0, cd.ORDERS_FLAT_TOL)
            check(f"{name}：最后一张平仓单的数量 = freqtrade 记的 amount", amount_mismatch, 0)
        if datadir is None:
            continue
        start = cd._num(st.get("starting_balance")) or START
        tf = str(st.get("timeframe_detail") or st.get("timeframe") or "")
        fmt = str(cfg.get("dataformat_ohlcv") or "feather")
        mode = str(cfg.get("trading_mode") or "futures")
        new_i = [i for i in (cd.trade_info(t) for t in trades) if i is not None]
        old_i = [i for i in (cd.trade_info(t, rebuild="never") for t in trades) if i is not None]
        c = cd.equity_curve_candles(new_i, start, tf, datadir, fmt, mode)
        if c is None:
            print(f"    （{datadir} 里没有这个结果用到的K线，不比较回撤）")
            continue
        o = cd.equity_curve_candles(old_i, start, tf, datadir, fmt, mode)
        final_ok = abs(float(c["end"][-1]) - (start + sum(float(t.get("profit_abs") or 0.0) for t in trades)))
        check(f"{name}：曲线最后的权益 = 初始资金 + 全部 profit_abs", final_ok, 0.0, 1e-6)
        print(f"    {name}：含浮亏回撤 旧算法（最后的数量和均价）{cd.curve_max_dd(o):.2%} → "
              f"按订单重建 {cd.curve_max_dd(c):.2%}（{c['method']}）；只按平仓 "
              f"{max(cd._num(st.get('max_drawdown_account')) or 0.0, cd._num(st.get('max_relative_drawdown')) or 0.0):.2%}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="账户含浮亏回撤（加仓 / 分批平仓按订单重建）的算法自检")
    ap.add_argument("--result", type=Path, action="append", help="另外拿这个回测结果（.zip / .json）核对，可以写多次")
    ap.add_argument("--datadir", type=Path, help="--result 用的K线目录（默认 user_data/data/<交易所>）")
    a = ap.parse_args(argv)
    try:
        import freqtrade  # noqa: F401
        import pandas  # noqa: F401
    except ImportError as exc:
        print(f"需要 freqtrade 和 pandas（用 .venv/bin/python 运行）：{exc}")
        return 1
    cd._ORDERS_NOTE_TO_STDERR = False  # 例子里故意有没有订单记录的交易；第 8 节单独测这个提醒
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        write_candles(d)
        test_single_unchanged(d)
        test_long_dca(d)
        test_short_dca(d)
        test_partial_exit(d)
        test_leverage(d)
        test_open_trade(d)
        test_same_candle(d)
        test_fallback(d)
        test_mixed(d)
    test_rebuild_vs_freqtrade()
    for p in a.result or []:
        check_result(p, a.datadir)
    print()
    if FAILS:
        print(f"有 {len(FAILS)} / {COUNT} 项不对：")
        for f in FAILS:
            print(f"  - {f}")
        return 1
    print(f"全部 {COUNT} 项通过。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
