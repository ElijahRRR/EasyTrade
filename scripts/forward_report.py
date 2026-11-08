#!/usr/bin/env python3
"""
模拟盘（前向测试）报告：读机器人的数据库（只读），算账户权益曲线（含持仓浮亏）、回撤、胜率、盈亏比，
对照前向测试的成功标准。说明：docs/forward-monitoring.md

用法（在跑模拟盘的服务器上；要 pandas，用 .venv/bin/python 或 make forward-report）：
  .venv/bin/python scripts/forward_report.py                       # 默认 configs/config.dryrun-nfi.json 和它的数据库
  .venv/bin/python scripts/forward_report.py --db user_data/dryrun/nfi.sqlite --config configs/config.dryrun-nfi.json
  .venv/bin/python scripts/forward_report.py --telegram            # 另外发一条中文摘要到 Telegram
  .venv/bin/python scripts/forward_report.py --json out.json       # 另外把数字写成 JSON
  先补K线（浮亏要用K线算；在模拟盘服务器上运行，它连得上币安）：
  .venv/bin/freqtrade download-data --config configs/config.dryrun-nfi.json --timerange 20261107- --timeframes 5m

怎么算：
  * 交易和订单从 freqtrade 2026.10 的数据库读（trades、orders 表，只读打开，不会改数据库），
    转成和 freqtrade 自己 Trade.to_json() 一样的格式（回测结果的交易也是这个格式，多了完整的订单字段），
    再交给 scripts/check_drawdown.py 的 account_equity_drawdown()：加仓 / 分批平仓按逐笔订单重建持仓，
    逐根K线算账户权益 = 初始资金 + 已实现盈亏 + 持仓浮动盈亏（按K线里对持仓最不利的价格，扣预估平仓手续费）。
  * K线用 --datadir 里的文件（默认 user_data/data/binance，freqtrade download-data 下载的）。
    缺K线（没有这个币的文件、或者某段时间没有）的时段，用这笔交易持仓期间到过的最高 / 最低价
    （freqtrade 记在数据库里的 max_rate / min_rate）代替，偏保守，报告里标"估"并列出缺了哪些。
  * 现在的权益算到最近一根已经收盘的K线；持仓的现价 = 那根K线的收盘价，没有就用持仓期间最差价（标"估"）。
  * 回撤(含浮亏) = 权益从之前的最高点（不低于初始资金）跌下去的最大比例；回撤(平仓) = 只看已平仓交易的余额。

成功标准（前向测试跑完时全部满足才算过；回撤超限 = 立刻判不通过）：
  运行 >= 60 天、已平仓 >= 30 笔、含浮亏最大回撤 <= 15%（项目硬规则，scripts/check_drawdown.py 的 DEFAULT_MAX_DD_PCT）、
  收益 > 0、盈亏比 >= 1.2。

看门狗：数据库旁边有看门狗的状态文件（user_data/dryrun/nfi-watchdog.json）就一起读（只读）：
  它触发过（实时回撤超过上限）也算这一轮回撤超限（docs/forward-test.md 第 10 节第 2 条）。

退出码：0 = 正常；3 = 含浮亏回撤超过上限，或看门狗现在处于暂停状态（cron 可以据此告警）；
        1 = 出错（找不到数据库、读不了配置……）。
Telegram（--telegram）：环境变量 FREQTRADE__TELEGRAM__TOKEN / FREQTRADE__TELEGRAM__CHAT_ID，
  没设置时从 .env.dryrun-nfi 读（只读这两个；--env-file 可改）。
"""

from __future__ import annotations

import argparse
import json
import math
import sqlite3
import sys
import time
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCRIPTS_DIR = Path(__file__).resolve().parent
ROOT_DIR = SCRIPTS_DIR.parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import check_drawdown as cd  # noqa: E402
import equity_watchdog as ew  # noqa: E402  (配置读取、Telegram、时间格式)

DEFAULT_CONFIG = ROOT_DIR / "configs" / "config.dryrun-nfi.json"
DEFAULT_DB = ROOT_DIR / "user_data" / "dryrun" / "nfi.sqlite"

# 前向测试的成功标准（回撤上限不在这里定义：用 check_drawdown.DEFAULT_MAX_DD_PCT）
MIN_DAYS = 60
MIN_CLOSED = 30
MIN_PF = 1.2
# docs/forward-test.md 第 10 节：60 天不到 30 笔就延长到 90 天；单笔亏损（含持仓浮亏）不超过初始资金的 5%
EXTEND_DAYS = 90
MAX_SINGLE_LOSS = 0.05

EXIT_OK, EXIT_ERROR, EXIT_DD = 0, 1, 3

# 和 freqtrade 2026.10 相同（freqtrade.constants；scripts/check_forward_tools.py 会核对）
NON_OPEN_EXCHANGE_STATES = ("cancelled", "canceled", "expired", "rejected", "closed")
DATETIME_PRINT_FORMAT = "%Y-%m-%d %H:%M:%S"


class ReportError(Exception):
    pass


# ----------------------------------------------------------------------------
# 读数据库（只读）→ freqtrade Trade.to_json() 格式
# ----------------------------------------------------------------------------
def open_db_readonly(path: Path) -> sqlite3.Connection:
    """只读打开 freqtrade 的 sqlite 数据库。

    freqtrade 把数据库设成 WAL 模式：机器人在跑时旁边有 -wal / -shm 文件，用 mode=ro 只读，能读到最新内容；
    机器人停了以后这两个文件会被删掉，这时用 immutable=1（没人在写，安全；mode=ro 会在旁边新建 -wal / -shm 文件）。
    """
    if not path.is_file():
        raise ReportError(f"找不到数据库 {path}（模拟盘还没启动过？数据库位置看配置里的 db_url）")
    base = "file:" + urllib.parse.quote(str(path.resolve()))
    running = Path(str(path) + "-wal").exists()
    modes = ("?mode=ro", "?mode=ro&immutable=1") if running else ("?mode=ro&immutable=1", "?mode=ro")
    last_err: Exception | None = None
    for mode in modes:
        try:
            con = sqlite3.connect(base + mode, uri=True, timeout=15)
            con.row_factory = sqlite3.Row
            con.execute("select count(*) from sqlite_master").fetchone()
            return con
        except sqlite3.Error as e:
            last_err = e
    raise ReportError(f"打不开数据库 {path}：{last_err}")


def _columns(con: sqlite3.Connection, table: str) -> set[str]:
    return {r[1] for r in con.execute(f'PRAGMA table_info("{table}")')}


def _dt(value: Any) -> datetime | None:
    """数据库里的时间（SQLAlchemy 存成不带时区的 UTC 文本，例如 2026-11-07 14:34:41.123456）→ 不带时区的 datetime。"""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        dt = datetime.fromisoformat(str(value))
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def _utc(dt: datetime | None) -> datetime | None:
    return dt.replace(tzinfo=timezone.utc) if dt is not None else None


def _ms(dt: datetime | None) -> int | None:
    """= freqtrade.util.dt_ts_none（UTC 毫秒）。"""
    return int(_utc(dt).timestamp() * 1000) if dt is not None else None


def _fmt(dt: datetime | None) -> str | None:
    return dt.strftime(DATETIME_PRINT_FORMAT) if dt is not None else None


def _bool(v: Any) -> bool:
    return bool(v) if v is not None else False


def _get(row: dict[str, Any], key: str, default: Any = None) -> Any:
    v = row.get(key, default)
    return default if v is None and default is not None else v


def _filled_or_open(o: dict[str, Any]) -> bool:
    """= freqtrade LocalTrade.select_filled_or_open_orders 的条件。"""
    is_open = _bool(o.get("ft_is_open"))
    if not is_open:
        return (o.get("filled") or 0) > 0 and o.get("status") in NON_OPEN_EXCHANGE_STATES
    return o.get("status") is not None


def order_to_json(o: dict[str, Any], entry_side: str) -> dict[str, Any]:
    """一行 orders 表 → freqtrade Order.to_json(entry_side)（完整格式）。"""
    amount = o.get("amount") or o.get("ft_amount")
    price = o.get("average") or o.get("price") or o.get("stop_price") or o.get("ft_price")
    order_date = _dt(o.get("order_date"))
    filled_date = _dt(o.get("order_filled_date"))
    average = o.get("average")
    return {
        "amount": amount,
        "safe_price": price,
        "ft_order_side": o.get("ft_order_side"),
        "order_filled_timestamp": _ms(filled_date),
        "ft_is_entry": o.get("ft_order_side") == entry_side,
        "ft_order_tag": o.get("ft_order_tag"),
        "cost": o.get("cost") if o.get("cost") else 0,
        "pair": o.get("ft_pair"),
        "order_id": o.get("order_id"),
        "status": o.get("status"),
        "average": round(average, 8) if average else 0,
        "filled": o.get("filled"),
        "is_open": _bool(o.get("ft_is_open")),
        "order_date": _fmt(order_date),
        "order_timestamp": _ms(order_date),
        "order_filled_date": _fmt(filled_date),
        "order_type": o.get("order_type"),
        "price": o.get("price"),
        "remaining": o.get("remaining"),
        "ft_fee_base": o.get("ft_fee_base"),
        "funding_fee": o.get("funding_fee"),
    }


def trade_to_json(t: dict[str, Any], orders: list[dict[str, Any]]) -> dict[str, Any]:
    """一行 trades 表 + 它的 orders（按 id 排好）→ freqtrade LocalTrade.to_json()（完整格式，含 orders 列表）。

    另外多一个字段 funding_fee_running（持仓中还没记到订单上的资金费，报告里提一句）。
    """
    is_short = _bool(t.get("is_short"))
    entry_side, exit_side = ("sell", "buy") if is_short else ("buy", "sell")
    sel = [o for o in orders if _filled_or_open(o)]
    filled_entry, filled_exit, open_wo_sl, open_sl_ts = [], [], [], []
    entry_fill_dates: list[datetime] = []
    for o in sel:
        if _bool(o.get("ft_is_open")):
            if o.get("ft_order_side") != "stoploss":
                open_wo_sl.append(o)
            else:
                d = _dt(o.get("order_date"))
                if d is not None:
                    open_sl_ts.append(d)
        elif o.get("ft_order_side") == entry_side:
            filled_entry.append(o)
            d = _dt(o.get("order_filled_date"))
            if d is not None:
                entry_fill_dates.append(d)
        elif o.get("ft_order_side") == exit_side:
            filled_exit.append(o)
    pair = str(t.get("pair") or "")
    open_date = _dt(t.get("open_date"))
    close_date = _dt(t.get("close_date"))
    entry_fill = min(entry_fill_dates) if entry_fill_dates else None
    sl_update = max(open_sl_ts) if open_sl_ts else None
    duration_s = int((close_date - open_date).total_seconds()) if close_date and open_date else None
    cp = t.get("close_profit")
    sl_pct = t.get("stop_loss_pct")
    isl_pct = t.get("initial_stop_loss_pct")
    try:
        base = t.get("base_currency") or pair.split("/")[0]
    except IndexError:
        base = ""
    try:
        quote = t.get("stake_currency") or pair.split("/")[1].split(":")[0]
    except IndexError:
        quote = ""
    tm = t.get("trading_mode")
    trading_mode = str(tm).lower() if tm else None  # 数据库存的是枚举名 FUTURES；to_json 序列化后是 "futures"

    def r8(v: Any) -> Any:
        return round(v, 8) if v is not None else None

    return {
        "trade_id": t.get("id"),
        "pair": pair,
        "base_currency": base,
        "quote_currency": quote,
        "is_open": _bool(t.get("is_open")),
        "exchange": t.get("exchange"),
        "amount": r8(t.get("amount")),
        "amount_requested": r8(t.get("amount_requested")) if t.get("amount_requested") else None,
        "stake_amount": r8(t.get("stake_amount")),
        "max_stake_amount": r8(t.get("max_stake_amount")) if t.get("max_stake_amount") else None,
        "strategy": t.get("strategy"),
        "enter_tag": t.get("enter_tag"),
        "timeframe": t.get("timeframe"),
        "fee_open": t.get("fee_open"),
        "fee_open_cost": t.get("fee_open_cost"),
        "fee_open_currency": t.get("fee_open_currency"),
        "fee_close": t.get("fee_close"),
        "fee_close_cost": t.get("fee_close_cost"),
        "fee_close_currency": t.get("fee_close_currency"),
        "open_date": _fmt(open_date),
        "open_timestamp": _ms(open_date),
        "open_fill_date": _fmt(entry_fill),
        "open_fill_timestamp": _ms(entry_fill),
        "open_rate": t.get("open_rate"),
        "open_rate_requested": t.get("open_rate_requested"),
        "open_trade_value": r8(t.get("open_trade_value")),
        "close_date": _fmt(close_date),
        "close_timestamp": _ms(close_date),
        "realized_profit": t.get("realized_profit") or 0.0,
        "realized_profit_ratio": cp or None,
        "close_rate": t.get("close_rate"),
        "close_rate_requested": t.get("close_rate_requested"),
        "close_profit": cp,
        "close_profit_pct": round(cp * 100, 2) if cp else None,
        "close_profit_abs": t.get("close_profit_abs"),
        "trade_duration_s": duration_s,
        "trade_duration": int(duration_s // 60) if duration_s is not None else None,
        "profit_ratio": cp,
        "profit_pct": round(cp * 100, 2) if cp else None,
        "profit_abs": t.get("close_profit_abs"),
        "exit_reason": t.get("exit_reason"),
        "exit_order_status": t.get("exit_order_status"),
        "stop_loss_abs": t.get("stop_loss"),
        "stop_loss_ratio": sl_pct if sl_pct else None,
        "stop_loss_pct": (sl_pct * 100) if sl_pct else None,
        "stoploss_last_update": _fmt(sl_update),
        "stoploss_last_update_timestamp": _ms(sl_update),
        "initial_stop_loss_abs": t.get("initial_stop_loss"),
        "initial_stop_loss_ratio": isl_pct if isl_pct else None,
        "initial_stop_loss_pct": isl_pct * 100 if isl_pct else None,
        "min_rate": t.get("min_rate"),
        "max_rate": t.get("max_rate"),
        "leverage": t.get("leverage"),
        "interest_rate": t.get("interest_rate"),
        "liquidation_price": t.get("liquidation_price"),
        "is_short": is_short,
        "trading_mode": trading_mode,
        "funding_fees": t.get("funding_fees"),
        "amount_precision": t.get("amount_precision"),
        "price_precision": t.get("price_precision"),
        "precision_mode": t.get("precision_mode"),
        "precision_mode_price": t.get("precision_mode_price"),
        "contract_size": t.get("contract_size"),
        "nr_of_successful_entries": len(filled_entry),
        "nr_of_successful_exits": len(filled_exit),
        "has_open_orders": len(open_wo_sl) > 0,
        "orders": [order_to_json(o, entry_side) for o in sel],
        # 不是 to_json 的字段：
        "funding_fee_running": t.get("funding_fee_running"),
    }


def load_trades(con: sqlite3.Connection) -> list[dict[str, Any]]:
    """数据库里全部交易（按 id），每笔带 orders。"""
    for table in ("trades", "orders"):
        if not _columns(con, table):
            raise ReportError(f"数据库里没有 {table} 表：不是 freqtrade 的数据库？")
    orders: dict[int, list[dict[str, Any]]] = {}
    for r in con.execute('SELECT * FROM "orders" ORDER BY id'):
        o = dict(r)
        orders.setdefault(int(o["ft_trade_id"]), []).append(o)
    out = []
    for r in con.execute('SELECT * FROM "trades" ORDER BY id'):
        t = dict(r)
        out.append(trade_to_json(t, orders.get(int(t["id"]), [])))
    return out


def load_kv(con: sqlite3.Connection) -> dict[str, Any]:
    """KeyValueStore 表：bot_start_time（第一次启动 / 第一笔交易的时间）、startup_time（最近一次启动）。"""
    if not _columns(con, "KeyValueStore"):
        return {}
    out: dict[str, Any] = {}
    for r in con.execute('SELECT * FROM "KeyValueStore"'):
        r = dict(r)
        vt = r.get("value_type")
        key = r.get("key")
        if vt == "datetime":
            d = _dt(r.get("datetime_value"))
            out[key] = _utc(d).timestamp() if d else None
        elif vt == "float":
            out[key] = r.get("float_value")
        elif vt == "int":
            out[key] = r.get("int_value")
        else:
            out[key] = r.get("string_value")
    return out


# ----------------------------------------------------------------------------
# K线：读文件，缺的用交易记录的最高 / 最低价补（标"估"）
# ----------------------------------------------------------------------------
def tf_seconds(tf: str) -> int:
    """K线周期 → 秒（5m → 300，1h → 3600）；不认识返回 0。"""
    units = {"m": 60, "h": 3600, "d": 86400, "w": 604800}
    try:
        return int(tf[:-1]) * units[tf[-1]]
    except (KeyError, ValueError, IndexError):
        return 0


def _needed_candles(info: dict[str, Any], g: int, until: float | None = None) -> list[int]:
    """这笔交易持仓期间和哪些K线有重叠（K线开始时间 T：T < 结束 且 T + g > 开始；和 check_drawdown 的口径相同）。

    until：只要在这个时间以前已经收盘的K线（T + g <= until）。"""
    a, b = float(info["open"]), float(info["close"])
    if b <= a:
        return []
    first = int(math.floor(a / g) * g)
    last = int(math.ceil(b / g) * g)
    if until is not None:
        last = min(last, int(math.floor(until / g) * g))  # T + g <= until
    return list(range(first, last, g))


def build_candles(
    infos: list[dict[str, Any]], timeframe: str, datadir: Path | None, data_format: str, trading_mode: str,
    until: float | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """每个交易对的K线 (时间秒, high, low, close)，缺的时段用交易记录补。返回 (K线, 覆盖情况)。
    until：只补在这个时间以前已经收盘的K线（报告时间所在的、还没收盘的那根不补，也不计入"补的根数"）。

    覆盖情况：{交易对: {"file": 有没有文件, "needed": 要用的根数, "missing": 补的根数, "gaps": [(开始, 结束)],
                       "last_real": 最后一根真实K线的开始时间}}
    """
    import numpy as np

    g = tf_seconds(timeframe)
    by_pair: dict[str, list[dict[str, Any]]] = {}
    for i in infos:
        by_pair.setdefault(i["pair"], []).append(i)
    candles: dict[str, Any] = {}
    cover: dict[str, Any] = {}
    for pair, its in sorted(by_pair.items()):
        real = None
        if datadir is not None:
            loaded = cd.load_candles({pair}, timeframe, datadir, data_format, trading_mode)
            if loaded is not None and loaded[0] == timeframe:
                real = loaded[1][pair]
        if real is None:
            secs = np.zeros(0, dtype="int64")
            high = low = close = np.zeros(0)
        else:
            secs, high, low, close = real
            ok = np.isfinite(high) & np.isfinite(low) & np.isfinite(close)
            secs, high, low, close = secs[ok], high[ok], low[ok], close[ok]
        have = {int(s) for s in secs}
        synth: dict[int, list[float]] = {}  # T → [high, low, close]
        needed = 0
        for i in its:
            ts = _needed_candles(i, g, until)
            needed += len(ts)
            hi = float(i["max_rate"])
            lo = float(i["min_rate"])
            worst = hi if i["dir"] < 0 else lo
            for T in ts:
                if T in have:
                    continue
                if T in synth:  # 同一根K线里一笔平仓、另一笔开仓：取两笔的极值
                    s = synth[T]
                    synth[T] = [max(s[0], hi), min(s[1], lo), min(s[2], worst) if i["dir"] > 0 else max(s[2], worst)]
                else:
                    synth[T] = [hi, lo, worst]
        if synth:
            ts_s = np.array(sorted(synth), dtype="int64")
            vals = np.array([synth[int(T)] for T in ts_s], dtype="float64")
            secs = np.concatenate([secs, ts_s])
            high = np.concatenate([high, vals[:, 0]])
            low = np.concatenate([low, vals[:, 1]])
            close = np.concatenate([close, vals[:, 2]])
            order = np.argsort(secs, kind="stable")
            secs, high, low, close = secs[order], high[order], low[order], close[order]
        if len(secs) == 0:  # 一根都不需要、也没有文件：给一根占位（check_drawdown 要求数组非空），不会被用到
            secs, high, low, close = (np.zeros(1, dtype="int64"), np.ones(1), np.ones(1), np.ones(1))
        gaps: list[tuple[int, int]] = []
        for T in sorted(synth):
            if gaps and gaps[-1][1] == T:
                gaps[-1] = (gaps[-1][0], T + g)
            else:
                gaps.append((T, T + g))
        candles[pair] = (secs, high, low, close)
        cover[pair] = {"file": real is not None, "needed": needed, "missing": len(synth), "gaps": gaps,
                       "last_real": int(max(have)) if have else None, "real": have}
    return candles, cover


# ----------------------------------------------------------------------------
# 计算
# ----------------------------------------------------------------------------
def _upnl(d: float, amount: float, avg: float, fee_open: float, fee_close: float, price: float) -> float:
    """持仓按 price 平掉的盈亏（含开仓手续费和预估平仓手续费，不含资金费）= freqtrade calculate_profit 的公式。"""
    return d * amount * (price - avg) - fee_open * amount * avg - fee_close * amount * price


def compute(
    trades: list[dict[str, Any]],
    *,
    start_balance: float,
    timeframe: str,
    datadir: Path | None,
    data_format: str = "feather",
    trading_mode: str = "futures",
    now: float,
    max_dd: float,
    kv: dict[str, Any] | None = None,
    watchdog: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """算报告里的全部数字（不打印）。now = 报告时间（unix 秒）；权益算到 now 之前最后一根收盘的K线。

    watchdog：看门狗的状态文件内容（equity_watchdog.load_state），有的话一起判断：看门狗触发过 = 回撤超限。
    """
    kv = kv or {}
    g = tf_seconds(timeframe)
    if g <= 0:
        raise ReportError(f"不认识的K线周期 {timeframe!r}")
    as_of = float(math.floor(now / g) * g)  # 最后一根已经收盘的K线的收盘时间

    infos_all = [(t, cd.trade_info(t, open_until=as_of)) for t in trades]
    pending = [t for t, i in infos_all if i is None and t.get("is_open")]  # 开仓单还没成交
    skipped = [t for t, i in infos_all if i is None and not t.get("is_open")]
    used = [(t, i) for t, i in infos_all if i is not None]
    infos = [i for _t, i in used]

    closed = [t for t, i in used if not t.get("is_open")]
    open_ = [(t, i) for t, i in used if t.get("is_open")]
    profits = [float(t.get("profit_abs") or 0.0) for t in closed]
    wins = [p for p in profits if p > 0]
    losses = [p for p in profits if p < 0]
    gross_win, gross_loss = sum(wins), -sum(losses)
    pf = (gross_win / gross_loss) if gross_loss > 0 else (math.inf if gross_win > 0 else None)
    realized_closed = sum(profits)

    # 资金曲线（含浮亏）：只用 as_of 之前开仓的交易；之后开的（还在当前这根没收盘的K线里）只算进"现在的权益"
    in_curve = [t for t, i in used if i["open"] < as_of]
    curve_infos = [i for t, i in used if i["open"] < as_of]
    candles, cover = build_candles(curve_infos, timeframe, datadir, data_format, trading_mode, until=as_of)
    eq = None
    if in_curve:
        eq = cd.account_equity_drawdown(in_curve, start_balance, timeframe, candles=candles, as_of=as_of)
    synth_total = sum(c["missing"] for c in cover.values())

    # 持仓：按最近一根收盘K线的收盘价；没有真实K线就用持仓期间最差价（估）
    positions = []
    last_T = int(as_of - g)
    for t, i in open_:
        c = cover.get(t["pair"])
        legs = i.get("legs") or []
        if legs:
            _a, _b, amount, avg, _r = legs[-1]
        else:
            amount, avg = i["amount"], i["open_rate"]
        price, src = None, ""
        if c is not None and i["open"] < as_of and last_T in c["real"]:
            secs, _h, _l, close = candles[t["pair"]]
            price, src = float(close[_index_of(secs, last_T)]), "K线"
        if price is None:
            price = float(i["max_rate"] if i["dir"] < 0 else i["min_rate"])
            src = "估"
        upnl = _upnl(i["dir"], amount, avg, i["fee_open"], i["fee_close"], price)
        lev = float(t.get("leverage") or 1.0)
        positions.append({
            "trade_id": t.get("trade_id"), "pair": t["pair"], "side": "空" if i["dir"] < 0 else "多",
            "leverage": lev, "amount": amount, "avg": avg, "price": price, "price_src": src,
            "stake": amount * avg / lev if lev else None, "upnl": upnl, "realized": float(i["profit"]),
            "age_s": now - i["open"], "open": i["open"], "entries": int(t.get("nr_of_successful_entries") or 0),
            "exits": int(t.get("nr_of_successful_exits") or 0),
            "funding_running": float(t.get("funding_fee_running") or 0.0),
        })
    realized_open = sum(p["realized"] for p in positions)
    upnl_total = sum(p["upnl"] for p in positions)
    equity_now = start_balance + realized_closed + realized_open + upnl_total

    if eq is not None and eq.get("curve") is not None and len(eq["curve"]["end"]):
        import numpy as np

        # 峰值和历史回撤只看 as_of 以前已经收盘的K线。在 as_of 以后才平掉的交易会让曲线多出 as_of 那根
        # （还没收盘、没有真实K线，只有这笔交易的已实现盈亏和它一生的最高 / 最低价），不能拿来算峰值和回撤；
        # 这些交易的盈亏已经算进 equity_now。
        curve = eq["curve"]
        keep = np.asarray(curve["grid"]) + int(curve["g"]) <= as_of
        closed_curve = dict(curve, grid=curve["grid"][keep], low=curve["low"][keep], end=curve["end"][keep])
        ends = closed_curve["end"]
        peak = max(float(start_balance), float(np.max(ends)) if len(ends) else float(start_balance), equity_now)
        dd_max_curve = cd.curve_max_dd(closed_curve)
        method = eq["method"]
    else:
        peak = max(float(start_balance), equity_now)
        dd_max_curve = float(eq["dd"]) if eq is not None else 0.0
        method = eq["method"] if eq is not None else ""
    dd_now = max(0.0, (peak - equity_now) / peak) if peak > 0 else 0.0
    dd_max = max(dd_max_curve, dd_now)
    dd_closed = cd.closed_drawdown(infos, start_balance)

    # 最差的一笔、拿得最久的一笔
    worst = min(closed, key=lambda t: float(t.get("profit_abs") or 0.0)) if closed else None
    holds = [(float(i["close"]) - float(i["open"]), t, False) for t, i in used if not t.get("is_open")]
    holds += [(now - float(i["open"]), t, True) for t, i in open_]
    longest = max(holds, key=lambda x: x[0]) if holds else None

    first_open = min((float(i["open"]) for i in infos), default=None)
    start_ts = kv.get("bot_start_time") or first_open
    days = (now - float(start_ts)) / 86400 if start_ts else 0.0
    ret = equity_now / start_balance - 1 if start_balance > 0 else 0.0

    # 单笔最大亏损（已平仓的 profit_abs；持仓 = 分批平仓已实现 + 浮动盈亏）、杠杆
    singles = [(float(t.get("profit_abs") or 0.0), t.get("trade_id"), t["pair"], False) for t in closed]
    singles += [(p["realized"] + p["upnl"], p["trade_id"], p["pair"], True) for p in positions]
    worst_single = min(singles, key=lambda x: x[0]) if singles else None
    levs = [float(t.get("leverage") or 1.0) for t, _i in used]

    cap = max_dd / 100.0
    wd = None
    if watchdog is not None:
        resets = watchdog.get("resets") or []
        wd = {"halted": bool(watchdog.get("halted")), "halted_at": watchdog.get("halted_at"),
              "triggers": sum(1 for r in resets if r.get("was_halted")) + (1 if watchdog.get("halted") else 0),
              "resets": len(resets), "max_dd_seen": float(watchdog.get("max_dd_seen") or 0.0),
              "last_at": (watchdog.get("last") or {}).get("at")}
    wd_triggered = bool(wd and wd["triggers"])
    dd_value = f"{dd_max:.2%}" + ("" if wd is None else (f"，看门狗触发过 {wd['triggers']} 次" if wd_triggered
                                                       else "，看门狗没触发过"))
    criteria = [
        {"name": "运行天数", "need": f">= {MIN_DAYS} 天", "value": f"{days:.1f} 天", "ok": days >= MIN_DAYS,
         "progress": f"{min(days / MIN_DAYS, 1):.0%}"},
        {"name": "已平仓交易", "need": f">= {MIN_CLOSED} 笔", "value": f"{len(closed)} 笔", "ok": len(closed) >= MIN_CLOSED,
         "progress": f"{min(len(closed) / MIN_CLOSED, 1):.0%}"},
        {"name": "最大回撤(含浮亏)", "need": f"<= {max_dd:g}%，看门狗没触发过", "value": dd_value,
         "ok": dd_max <= cap + 1e-12 and not wd_triggered, "progress": ""},
        {"name": "收益", "need": "> 0", "value": f"{ret:+.2%}", "ok": ret > 0, "progress": ""},
        {"name": "盈亏比", "need": f">= {MIN_PF:g}", "value": _pf_text(pf), "ok": pf is not None and pf >= MIN_PF,
         "progress": ""},
    ]
    dd_breach = dd_max > cap + 1e-12
    return {
        "watchdog": wd, "wd_triggered": wd_triggered, "alert": dd_breach or bool(wd and wd["halted"]),
        "now": now, "as_of": as_of, "timeframe": timeframe, "start_balance": float(start_balance),
        "bot_start_time": kv.get("bot_start_time"), "startup_time": kv.get("startup_time"), "first_open": first_open,
        "days": days, "equity_now": equity_now, "peak": peak, "return": ret,
        "realized_closed": realized_closed, "realized_open": realized_open, "upnl": upnl_total,
        "closed": len(closed), "wins": len(wins), "losses": len(losses),
        "winrate": (len(wins) / len(closed)) if closed else None, "profit_factor": pf,
        "gross_win": gross_win, "gross_loss": gross_loss,
        "dd_now": dd_now, "dd_max": dd_max, "dd_max_curve": dd_max_curve, "dd_closed": dd_closed,
        "method": method, "max_dd": max_dd, "dd_breach": dd_breach,
        "positions": positions, "pending": [{"trade_id": t.get("trade_id"), "pair": t.get("pair")} for t in pending],
        "skipped": len(skipped), "trades_total": len(trades),
        "worst": None if worst is None else {
            "trade_id": worst.get("trade_id"), "pair": worst["pair"], "profit_abs": float(worst.get("profit_abs") or 0),
            "profit_ratio": worst.get("profit_ratio"), "exit_reason": worst.get("exit_reason"),
            "close_date": worst.get("close_date")},
        "longest": None if longest is None else {
            "trade_id": longest[1].get("trade_id"), "pair": longest[1]["pair"], "seconds": longest[0],
            "open": longest[2]},
        "coverage": {p: {k: v for k, v in c.items() if k != "real"} for p, c in cover.items()},
        "synthetic_candles": synth_total,
        "orders": cd.orders_summary(infos),
        "funding_running": sum(p["funding_running"] for p in positions),
        "criteria": criteria, "criteria_ok": sum(1 for c in criteria if c["ok"]),
        "worst_single": None if worst_single is None else {
            "profit": worst_single[0], "trade_id": worst_single[1], "pair": worst_single[2], "open": worst_single[3],
            "pct": max(0.0, -worst_single[0]) / start_balance if start_balance > 0 else 0.0},
        "leverage_max": max(levs) if levs else None, "leverage_not1": sum(1 for v in levs if abs(v - 1.0) > 1e-9),
    }


def _index_of(secs: Any, T: int) -> int:
    """排好序的K线时间数组里 T 的位置（调用前已确认 T 在里面）。"""
    import numpy as np

    return int(np.searchsorted(secs, T))


def _pf_text(pf: float | None) -> str:
    if pf is None:
        return "-"
    return "无亏损" if math.isinf(pf) else f"{pf:.2f}"


# ----------------------------------------------------------------------------
# 输出
# ----------------------------------------------------------------------------
def verdict(rep: dict[str, Any]) -> str:
    if rep["dd_breach"]:
        return (f"不通过：含浮亏回撤 {rep['dd_max']:.2%} 超过上限 {rep['max_dd']:g}%。"
                "看门狗应该已经让机器人停止开新仓；人工复盘前不要恢复（docs/forward-monitoring.md）")
    if rep.get("wd_triggered"):
        wd = rep["watchdog"]
        now = "，现在还处于暂停开新仓状态，等人工复盘" if wd["halted"] else "（之后人工解除过）"
        return (f"不通过：看门狗触发过 {wd['triggers']} 次（它实时读到的回撤超过 {rep['max_dd']:g}%）{now}。"
                "报告按K线收盘算的峰值可能比看门狗读到的低一点，所以这里的回撤数字可能没到上限")
    n = rep["criteria_ok"]
    if rep["closed"] < MIN_CLOSED and rep["days"] >= EXTEND_DAYS:
        return f"没有结论：跑满 {EXTEND_DAYS} 天平仓还不到 {MIN_CLOSED} 笔，样本不够（不算通过；docs/forward-test.md 第 10 节）"
    if rep["closed"] < MIN_CLOSED and rep["days"] >= MIN_DAYS:
        return (f"已满 {MIN_DAYS} 天但平仓只有 {rep['closed']} 笔（< {MIN_CLOSED}）：按 docs/forward-test.md 第 10 节"
                f"延长到 {EXTEND_DAYS} 天再看")
    if n == len(rep["criteria"]):
        return ("5 项标准都达到了：再人工确认下面的\u201c其他检查\u201d和 docs/forward-test.md 第 10 节第 5 ~ 7 条，"
                "然后拿这份报告做评审（达标只说明值得继续往下走，不等于可以直接上实盘）")
    return f"进行中：5 项标准达到 {n} 项（运行天数和交易笔数要等时间，回撤一旦超限就直接判不通过）"


def render(rep: dict[str, Any], *, title: str = "") -> str:
    stake = "USDT"
    L: list[str] = []
    L.append(f"===== 模拟盘前向测试报告{('：' + title) if title else ''} =====")
    L.append(f"报告时间：{ew.utc_text(rep['now'])}（权益算到 {ew.utc_text(rep['as_of'])}，最近一根收盘的 {rep['timeframe']} K线）")
    start_txt = ew.utc_text(rep["bot_start_time"]) if rep.get("bot_start_time") else (
        ew.utc_text(rep["first_open"]) + "（第一笔交易）" if rep.get("first_open") else "-")
    L.append(f"开始运行：{start_txt}，已运行 {rep['days']:.1f} 天"
             + (f"；最近一次启动 {ew.utc_text(rep['startup_time'])}" if rep.get("startup_time") else ""))
    L.append("")
    L.append(f"初始资金：{rep['start_balance']:.2f} {stake}")
    L.append(f"现在的权益（含浮亏）：{rep['equity_now']:.2f} {stake}，收益 {rep['return']:+.2%}"
             f"（已平仓 {rep['realized_closed']:+.2f}，持仓里分批平仓已实现 {rep['realized_open']:+.2f}，"
             f"浮动盈亏 {rep['upnl']:+.2f}）")
    L.append(f"权益最高点：{rep['peak']:.2f} {stake}")
    method = rep["method"] or "-"
    est = f"；其中 {rep['synthetic_candles']} 根K线缺数据，按持仓期间最高 / 最低价估（偏保守）" if rep["synthetic_candles"] else ""
    rule = "项目硬规则" if abs(rep["max_dd"] - cd.DEFAULT_MAX_DD_PCT) < 1e-9 else cd.max_dd_rule_note(rep["max_dd"])
    L.append(f"回撤(含浮亏)：现在 {rep['dd_now']:.2%}，最大 {rep['dd_max']:.2%}（上限 {rep['max_dd']:g}%，{rule}；"
             f"算法 {method}{est}）")
    L.append(f"回撤(平仓)：{rep['dd_closed']:.2%}（只看已平仓交易的余额；和上面差得多 = 策略在扛着亏损的单子）")
    wd = rep.get("watchdog")
    if wd is None:
        L.append("看门狗：没找到它的状态文件（没在跑？见 docs/forward-monitoring.md 第 3 节）")
    else:
        state = (f"!! 现在暂停开新仓中（{ew.utc_text(wd['halted_at'])} 起），等人工复盘" if wd["halted"]
                 else "没有暂停")
        L.append(f"看门狗：{state}；触发过 {wd['triggers']} 次、人工解除过 {wd['resets']} 次；它实时见过的最大回撤 "
                 f"{wd['max_dd_seen']:.2%}；最近一次检查 {ew.utc_text(wd['last_at'])}")
    if rep["closed"]:
        L.append(f"已平仓 {rep['closed']} 笔：赚 {rep['wins']} 笔、亏 {rep['losses']} 笔，胜率 {rep['winrate']:.1%}，"
                 f"盈亏比 {_pf_text(rep['profit_factor'])}（总盈利 {rep['gross_win']:.2f} / 总亏损 {rep['gross_loss']:.2f}）")
    else:
        L.append("已平仓 0 笔")
    w = rep.get("worst")
    if w:
        ratio = f"（{float(w['profit_ratio']):+.2%}）" if w.get("profit_ratio") is not None else ""
        L.append(f"最差的一笔：#{w['trade_id']} {w['pair']} {w['profit_abs']:+.2f} {stake}{ratio}，"
                 f"离场原因 {w.get('exit_reason') or '-'}，{w.get('close_date')} UTC 平仓")
    lg = rep.get("longest")
    if lg:
        L.append(f"拿得最久的一笔：#{lg['trade_id']} {lg['pair']} {ew.duration_text(lg['seconds'])}"
                 + ("（还没平仓）" if lg["open"] else ""))
    om = rep.get("orders") or {}
    if om.get("multi") or om.get("open"):
        L.append(f"按逐笔订单重建持仓的交易：已平仓 {om.get('multi', 0)} 笔（加仓 / 分批平仓），持仓中 {om.get('open', 0)} 笔")
    if rep.get("skipped"):
        L.append(f"!! 有 {rep['skipped']} 笔已平仓的交易读不出来（缺开仓价 / 数量等），没有算进回撤和统计")
    if om.get("bad") or om.get("missing"):
        L.append(f"!! 有 {om.get('bad', 0) + om.get('missing', 0)} 笔交易的订单记录对不上，按最后的数量和均价近似计算")
    L.append("")

    pos = rep["positions"]
    L.append(f"--- 持仓 {len(pos)} 个 ---")
    if pos:
        L.append("  编号  交易对               方向 杠杆  数量            均价          现价              浮动盈亏      已实现    持有")
        for p in sorted(pos, key=lambda x: x["upnl"]):
            src = "" if p["price_src"] == "K线" else "(估)"
            L.append(
                f"  #{p['trade_id']:<4} {p['pair']:<20} {p['side']}  {p['leverage']:>3g}x  {p['amount']:<14.6g} "
                f"{p['avg']:<13.6g} {p['price']:<12.6g}{src:<4} {p['upnl']:>+10.2f}  {p['realized']:>+8.2f}  "
                f"{ew.duration_text(p['age_s'])}"
                + (f"（加仓 {p['entries'] - 1} 次）" if p["entries"] > 1 else "")
            )
        if any(p["price_src"] != "K线" for p in pos):
            L.append("  (估) = 没有最近一根收盘K线，现价用持仓期间最差价（多单最低价、空单最高价）代替；先补K线再看")
        if abs(rep["funding_running"]) > 1e-9:
            L.append(f"  还没结算到订单上的资金费合计 {rep['funding_running']:+.4f} {stake}，没算进浮动盈亏")
    if rep["pending"]:
        L.append(f"  另有 {len(rep['pending'])} 笔开仓单还挂着没成交（不算持仓）："
                 + "、".join(f"#{x['trade_id']} {x['pair']}" for x in rep["pending"]))
    L.append("")

    miss = {p: c for p, c in rep["coverage"].items() if c["missing"]}
    if miss:
        L.append("--- K线缺数据（这些时段的浮亏是估算的）---")
        for p, c in sorted(miss.items()):
            if not c["file"]:
                L.append(f"  {p}：没有K线文件，{c['missing']} 根全部按估算")
            else:
                spans = "、".join(f"{ew.utc_text(a)} ~ {ew.utc_text(b)}" for a, b in c["gaps"][:3])
                more = f" 等 {len(c['gaps'])} 段" if len(c["gaps"]) > 3 else ""
                L.append(f"  {p}：缺 {c['missing']} / {c['needed']} 根（{spans}{more}）")
        L.append("  补K线（在模拟盘服务器上运行）：" + download_hint(rep, sorted(miss)))
        L.append("")

    L.append("--- 成功标准（跑满以后全部达到才算通过）---")
    for c in rep["criteria"]:
        prog = f"，进度 {c['progress']}" if c["progress"] and not c["ok"] else ""
        L.append(f"  {'达到  ' if c['ok'] else '未达到'} {c['name']}：{c['value']}（要求 {c['need']}{prog}）")
    L.append("")
    L.append("--- 其他检查（docs/forward-test.md 第 10 节第 5、6 条，参考用，不算在上面 5 项里）---")
    ws = rep.get("worst_single")
    if ws:
        ok = ws["pct"] <= MAX_SINGLE_LOSS + 1e-12
        L.append(f"  {'达到  ' if ok else '未达到'} 单笔最大亏损（含持仓浮亏）：#{ws['trade_id']} {ws['pair']} "
                 f"{ws['profit']:+.2f} {stake} = 初始资金的 {ws['pct']:.2%}{'（还没平仓）' if ws['open'] else ''}"
                 f"（要求 <= {MAX_SINGLE_LOSS:.0%}）")
    if rep.get("leverage_max") is not None:
        n1 = rep["leverage_not1"]
        L.append(f"  杠杆：最高 {rep['leverage_max']:g} 倍；不是 1 倍的 {n1} 笔"
                 + ("（NFI 1 倍这一轮应该是 0 笔，不是 0 就要停下来查封顶）" if n1 else "")
                 + ("；!! 超过项目上限 5 倍" if rep["leverage_max"] > 5 + 1e-9 else ""))
    L.append("结论：" + verdict(rep))
    return "\n".join(L)


def download_hint(rep: dict[str, Any], pairs: list[str]) -> str:
    start = rep.get("bot_start_time") or rep.get("first_open") or rep["now"]
    day = datetime.fromtimestamp(float(start) - 86400, tz=timezone.utc).strftime("%Y%m%d")
    return (f".venv/bin/freqtrade download-data --config configs/config.dryrun-nfi.json --timerange {day}- "
            f"--timeframes {rep['timeframe']} --pairs {' '.join(pairs)}")


def telegram_text(rep: dict[str, Any], title: str = "") -> str:
    lines = []
    if rep["dd_breach"]:
        lines.append(f"!! 含浮亏回撤 {rep['dd_max']:.2%} 超过上限 {rep['max_dd']:g}%，需要人工复盘")
    elif rep.get("watchdog") and rep["watchdog"]["halted"]:
        lines.append("!! 看门狗已暂停开新仓（实时回撤超过上限），需要人工复盘")
    lines.append(f"【模拟盘日报】{title}".rstrip())
    lines.append(f"运行 {rep['days']:.1f} 天｜权益 {rep['equity_now']:.2f} USDT（{rep['return']:+.2%}）")
    lines.append(f"回撤(含浮亏) 现在 {rep['dd_now']:.2%}，最大 {rep['dd_max']:.2%}（上限 {rep['max_dd']:g}%）"
                 + ("，部分K线是估算" if rep["synthetic_candles"] else ""))
    wr = f"，胜率 {rep['winrate']:.1%}，盈亏比 {_pf_text(rep['profit_factor'])}" if rep["closed"] else ""
    lines.append(f"已平仓 {rep['closed']} 笔{wr}")
    lines.append(f"持仓 {len(rep['positions'])} 个，浮动盈亏 {rep['upnl']:+.2f} USDT")
    lines.append(f"进度：天数 {rep['days']:.0f}/{MIN_DAYS}，笔数 {rep['closed']}/{MIN_CLOSED}；标准达到 {rep['criteria_ok']}/5")
    return "\n".join(lines)


def _json_safe(v: Any) -> Any:
    if isinstance(v, float) and not math.isfinite(v):
        return None if math.isnan(v) else ("inf" if v > 0 else "-inf")
    if isinstance(v, dict):
        return {str(k): _json_safe(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_json_safe(x) for x in v]
    return v


# ----------------------------------------------------------------------------
# 命令行
# ----------------------------------------------------------------------------
def _parse_time(text: str) -> float:
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        raise argparse.ArgumentTypeError(f"时间格式不对：{text!r}（例如 2026-11-07T12:00）") from None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="模拟盘前向测试报告（docs/forward-monitoring.md）",
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--config", default=None,
                   help="机器人的配置（拿初始资金、K线周期、数据库位置），默认 configs/config.dryrun-nfi.json；none = 不读")
    p.add_argument("--db", default=None, help="数据库文件（默认取配置的 db_url，再没有用 user_data/dryrun/nfi.sqlite）")
    p.add_argument("--datadir", default=None, help="K线目录（默认 user_data/data/<交易所>，例如 user_data/data/binance）")
    p.add_argument("--timeframe", default=None, help="用多少分钟的K线算浮亏（默认 = 配置的 timeframe，例如 5m）")
    p.add_argument("--start-balance", type=float, default=None, help="初始资金（默认 = 配置的 dry_run_wallet；实盘必须填）")
    p.add_argument("--max-dd", type=cd.max_dd_arg, default=None,
                   help=f"回撤上限 %%（默认 {cd.DEFAULT_MAX_DD_PCT:g} = 项目硬规则；环境变量 {cd.MAX_DD_ENV} 也可以改）")
    p.add_argument("--as-of", type=_parse_time, default=None, help="报告时间（UTC，默认现在；例如 2026-11-07T12:00）")
    p.add_argument("--watchdog-state", default=None,
                   help="看门狗的状态文件（默认和数据库放一起：user_data/dryrun/nfi-watchdog.json）；none = 不读")
    p.add_argument("--json", default=None, help="另外把数字写成 JSON 文件")
    p.add_argument("--telegram", action="store_true", help="另外发一条中文摘要到 Telegram")
    p.add_argument("--env-file", default=None, help="Telegram 设置从这个文件读（默认 .env.dryrun-nfi；none = 不读）")
    return p


def run(args: argparse.Namespace) -> int:
    try:
        max_dd = args.max_dd if args.max_dd is not None else cd.default_max_dd_pct()
    except ValueError as e:
        raise ReportError(str(e)) from None
    cfg: dict[str, Any] = {}
    if (args.config or "").lower() != "none":
        cfg_path = Path(args.config) if args.config else DEFAULT_CONFIG
        if not cfg_path.is_absolute() and not cfg_path.is_file():
            cfg_path = ROOT_DIR / cfg_path
        if cfg_path.is_file():
            try:
                cfg = ew.load_config(cfg_path)
            except Exception as e:  # noqa: BLE001
                raise ReportError(f"读不了配置 {cfg_path}：{e}") from None
        elif args.config:
            raise ReportError(f"找不到配置 {cfg_path}")

    db = Path(args.db) if args.db else (ew.config_db_path(cfg) or DEFAULT_DB)
    if not db.is_absolute() and not db.is_file():
        db = ROOT_DIR / db
    start_balance = args.start_balance
    if start_balance is None:
        if cfg and cfg.get("dry_run") is False:
            raise ReportError("这是实盘配置（dry_run = false）：请用 --start-balance 写明起始资金")
        start_balance = ew.config_start_balance(cfg)
    if not start_balance or start_balance <= 0:
        raise ReportError("不知道初始资金：用 --start-balance 指定，或者给 --config（读 dry_run_wallet）")

    con = open_db_readonly(db)
    try:
        trades = load_trades(con)
        kv = load_kv(con)
    except sqlite3.Error as e:
        raise ReportError(f"读数据库 {db} 出错：{e}") from None
    finally:
        con.close()

    tf = args.timeframe or cfg.get("timeframe")
    if not tf:
        mins = {t.get("timeframe") for t in trades if t.get("timeframe")}
        tf = f"{mins.pop()}m" if len(mins) == 1 else "5m"
    if args.datadir:
        datadir = Path(args.datadir)
    elif cfg.get("datadir"):
        datadir = Path(cfg["datadir"])
    else:
        exch = str((cfg.get("exchange") or {}).get("name") or (trades[0].get("exchange") if trades else "") or "binance")
        datadir = Path("user_data") / "data" / exch.lower()
    if not datadir.is_absolute():
        datadir = ROOT_DIR / datadir

    wd_state = None
    if (args.watchdog_state or "").lower() != "none":
        wd_path = Path(args.watchdog_state) if args.watchdog_state else db.with_name(db.stem + "-watchdog.json")
        if not wd_path.is_absolute() and not wd_path.is_file():
            wd_path = ROOT_DIR / wd_path
        if wd_path.is_file():
            try:
                wd_state = ew.load_state(wd_path)
            except ValueError as e:
                raise ReportError(str(e)) from None
        elif args.watchdog_state:
            raise ReportError(f"找不到看门狗的状态文件 {wd_path}")

    now = args.as_of if args.as_of is not None else time.time()
    rep = compute(trades, start_balance=start_balance, timeframe=str(tf), datadir=datadir,
                  data_format=str(cfg.get("dataformat_ohlcv") or "feather"),
                  trading_mode=str(cfg.get("trading_mode") or "futures"), now=now, max_dd=max_dd, kv=kv,
                  watchdog=wd_state)
    rep["db"] = str(db)
    rep["datadir"] = str(datadir)
    title = str(cfg.get("bot_name") or "")
    print(render(rep, title=title))
    if args.json:
        Path(args.json).write_text(json.dumps(_json_safe(rep), ensure_ascii=False, indent=1), encoding="utf-8")
    if args.telegram:
        ew.load_env_keys(ew.resolve_env_file(args.env_file), (ew.TG_TOKEN_ENV, ew.TG_CHAT_ENV))
        tg = ew.telegram_settings()
        if not tg:
            print(f"Telegram：没有设置 {ew.TG_TOKEN_ENV} / {ew.TG_CHAT_ENV}，没有发送", file=sys.stderr)
        else:
            ok, msg = ew.send_telegram(telegram_text(rep, title), *tg)
            print(f"Telegram：{msg}", file=sys.stderr if not ok else sys.stdout)
    return EXIT_DD if rep["alert"] else EXIT_OK


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return run(args)
    except ReportError as e:
        print(f"错误：{e}", file=sys.stderr)
        return EXIT_ERROR


if __name__ == "__main__":
    sys.exit(main())
