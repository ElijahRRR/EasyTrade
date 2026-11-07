#!/usr/bin/env python3
"""
模拟盘报告（scripts/forward_report.py）和账户权益看门狗（scripts/equity_watchdog.py）的自检。不联网，几秒钟。

用法：
  .venv/bin/python scripts/check_forward_tools.py        # 或 make forward-check

做什么：
  1. 用 freqtrade 自己的数据库模型（Trade / Order，init_db）在临时目录建一个模拟盘数据库，表结构和真的一样：
       A  BTC 多单，加仓一次再一次平掉（中间还有一张撤掉的单）
       B  ETH 空单 2 倍，分批平仓再全平
       C  SOL 多单 3 倍，带手续费，分批平掉一部分，还没平仓（持仓中）
       D  XRP 多单，亏损平仓，没有K线文件（按交易记录的最高 / 最低价估）
       E  ADA 开仓单还挂着没成交（不算持仓）
     再写一份 1h K线（BTC 故意缺一根）。核对：
       * 读数据库得到的交易 = freqtrade 自己的 Trade.to_json()（每个字段、每张订单）
       * 机器人在跑（有 -wal 文件）和停了（没有）两种情况都能只读打开，数据库文件一个字节都不变
       * 报告里的权益、回撤(含浮亏 / 平仓)、胜率、盈亏比、持仓浮亏、持有时间、成功标准 = 手算的值
       * 缺K线时标"估"，持仓现价退回持仓期间最差价；回撤超过 15% 时退出码 3
     另一个小数据库：最后一根收盘K线里刚平掉一笔（同时有持仓浮亏）、或报告时间所在的还没收盘的K线里才平掉，
     峰值和回撤只看已经收盘的K线（不会多出一根只有已实现盈亏、没有持仓浮亏的K线，误报超限）
  2. 在本机起一个假的 freqtrade REST API（http.server，线程里跑），让看门狗去连：
       * 回撤 14% 只预警、不暂停；正好 15% 不暂停；15.5% 调 stopentry（只调一次）
       * 峰值存在状态文件里，换一个看门狗进程（新对象）接着用
       * 回撤恢复后仍然保持暂停、从不调 /start、/reload_config；机器人重启（又变成 running）就再暂停
       * --reset 要写复盘结论；回撤仍超限时不让解除（除非 --rebase-peak）；--resume 才调 /start
       * 连不上 / 密码不对不会崩，等待时间逐步加长，超过 10 分钟告警一次，恢复后再说一声
       * --flatten 时调 forceexit（tradeid = all、ordertype = market）；Telegram 消息发到假的 Bot API
       * 回撤上限比 15% 宽（--max-dd / MAX_DD_PCT，含密钥文件里写的）：拒绝启动、拒绝 --reset
       * 看门狗自己出错（状态文件坏了）：10 分钟后告警一次，连续 10 次退出，修好后说一声恢复
退出码：0 = 全部通过；1 = 有不通过的。
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import math
import os
import shutil
import socket
import sys
import tempfile
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

SCRIPTS_DIR = Path(__file__).resolve().parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import check_drawdown as cd  # noqa: E402
import equity_watchdog as ew  # noqa: E402
import forward_report as fr  # noqa: E402

H = 3600
T0 = int(datetime(2026, 9, 1, tzinfo=timezone.utc).timestamp())
NOW = T0 + 10 * H + 30 * 60  # 报告时间：第 10 根K线中间 → 权益算到 T0 + 10h
START = 1000.0
RESULTS: list[tuple[bool, str, str]] = []


def _close(a: Any, b: Any, tol: float) -> bool:
    if isinstance(a, (int, float)) and isinstance(b, (int, float)) and not isinstance(a, bool):
        if isinstance(b, bool):
            return False
        if math.isinf(a) or math.isinf(b):
            return a == b
        return abs(float(a) - float(b)) <= tol * max(1.0, abs(float(b)))
    return a == b


def check(name: str, got: Any, want: Any, tol: float = 1e-9) -> None:
    ok = _close(got, want, tol)
    RESULTS.append((ok, name, "" if ok else f"得到 {got!r}，应为 {want!r}"))


def check_true(name: str, cond: bool, detail: str = "") -> None:
    RESULTS.append((bool(cond), name, "" if cond else detail))


def dt(hours: float) -> datetime:
    return datetime.fromtimestamp(T0 + hours * H, tz=timezone.utc)


# ----------------------------------------------------------------------------
# 1. 用 freqtrade 的模型建数据库
# ----------------------------------------------------------------------------
def build_db(path: Path) -> None:
    from freqtrade.enums import TradingMode
    from freqtrade.persistence import Order, Trade, init_db
    from freqtrade.persistence.key_value_store import KeyValueStore

    init_db(f"sqlite:///{path}")

    def make(pair: str, *, short: bool, lev: float, fee: float, fills: list[tuple[float, float, float, bool]],
             min_rate: float, max_rate: float, exit_reason: str | None = None,
             cancelled_at: float | None = None) -> Trade:
        h0, a0, p0, _ = fills[0]
        trade = Trade(
            pair=pair, exchange="binance", base_currency=pair.split("/")[0], stake_currency="USDT", is_open=True,
            fee_open=fee, fee_close=fee, open_rate=p0, open_rate_requested=p0, amount=a0, amount_requested=a0,
            stake_amount=a0 * p0 / lev, open_date=dt(h0), leverage=lev, is_short=short,
            trading_mode=TradingMode.FUTURES, strategy="StubStrategy", timeframe=60, enter_tag="stub",
        )
        Trade.session.add(trade)
        Trade.commit()
        entry, exit_ = ("sell", "buy") if short else ("buy", "sell")
        for n, (h, amount, price, is_entry) in enumerate(fills):
            if cancelled_at is not None and n == 1:
                trade.orders.append(Order(
                    ft_order_side=entry, ft_pair=pair, ft_is_open=False, ft_amount=1.0, ft_price=price + 1,
                    order_id=f"{pair}-cancel", status="canceled", symbol=pair, order_type="limit", side=entry,
                    price=price + 1, average=None, amount=1.0, filled=0.0, remaining=1.0, cost=0.0,
                    order_date=dt(cancelled_at), order_update_date=dt(cancelled_at)))
            side = entry if is_entry else exit_
            o = Order(
                ft_order_side=side, ft_pair=pair, ft_is_open=False, ft_amount=amount, ft_price=price,
                order_id=f"{pair}-{n}", status="closed", symbol=pair, order_type="limit", side=side,
                price=price, average=price, amount=amount, filled=amount, remaining=0.0, cost=amount * price,
                order_date=dt(h), order_filled_date=dt(h), order_update_date=dt(h))
            trade.orders.append(o)
            if not is_entry and n == len(fills) - 1 and exit_reason:
                trade.exit_reason = exit_reason
            trade.update_trade(o)
        trade.min_rate, trade.max_rate = min_rate, max_rate
        Trade.commit()
        return trade

    # A：BTC 多单 1@100 → 加仓 1@80（均价 90）→ 2@95 全平：盈利 10
    make("BTC/USDT:USDT", short=False, lev=1, fee=0.0, min_rate=78, max_rate=101, exit_reason="exit_signal",
         fills=[(0, 1, 100, True), (2, 1, 80, True), (4, 2, 95, False)], cancelled_at=1.5)
    # D：XRP 多单 100@1.0 → 100@0.9：亏 10（没有K线文件）
    make("XRP/USDT:USDT", short=False, lev=1, fee=0.0, min_rate=0.85, max_rate=1.02, exit_reason="exit_signal",
         fills=[(1, 100, 1.0, True), (3, 100, 0.9, False)])
    # B：ETH 空单 2 倍 4@50 → 平 2@45（+10）→ 平 2@52（-4）：盈利 6
    make("ETH/USDT:USDT", short=True, lev=2, fee=0.0, min_rate=44, max_rate=51, exit_reason="roi",
         fills=[(5, 4, 50, True), (6, 2, 45, False), (7, 2, 52, False)])
    # C：SOL 多单 3 倍，手续费 0.1%，10@20 → 平 4@22（已实现 7.832），剩 6 个还拿着
    c = make("SOL/USDT:USDT", short=False, lev=3, fee=0.001, min_rate=18, max_rate=22.5,
             fills=[(6, 10, 20, True), (8, 4, 22, False)])
    c.funding_fee_running = -0.05
    # E：ADA 开仓单还挂着
    e = Trade(pair="ADA/USDT:USDT", exchange="binance", base_currency="ADA", stake_currency="USDT", is_open=True,
              fee_open=0.0005, fee_close=0.0005, open_rate=0.5, open_rate_requested=0.5, amount=50,
              amount_requested=50, stake_amount=25, open_date=dt(10.2), leverage=1, is_short=False,
              trading_mode=TradingMode.FUTURES, strategy="StubStrategy", timeframe=60)
    Trade.session.add(e)
    Trade.commit()
    e.orders.append(Order(ft_order_side="buy", ft_pair="ADA/USDT:USDT", ft_is_open=True, ft_amount=50, ft_price=0.5,
                          order_id="ADA-0", status="open", symbol="ADA/USDT:USDT", order_type="limit", side="buy",
                          price=0.5, amount=50, filled=0.0, remaining=50, cost=0.0, order_date=dt(10.2)))
    Trade.commit()
    KeyValueStore.store_value("bot_start_time", dt(-24))
    KeyValueStore.store_value("startup_time", dt(5))


def freqtrade_json() -> dict[int, dict[str, Any]]:
    """freqtrade 自己从数据库读出来、再 to_json() 的结果（重新从数据库加载，时间不带时区，和机器人一样）。"""
    from freqtrade.persistence import Trade
    from sqlalchemy import select

    Trade.session.expire_all()
    return {t.id: t.to_json() for t in Trade.session.scalars(select(Trade).order_by(Trade.id)).all()}


def close_db() -> None:
    from freqtrade.persistence import Trade

    bind = Trade.session.get_bind()
    Trade.session.remove()
    bind.dispose()


def write_candles(d: Path, *, drop: dict[str, list[int]] | None = None) -> None:
    import pandas as pd

    rows = {
        "BTC/USDT:USDT": {0: (100, 101, 95, 96), 1: (96, 97, 85, 88), 2: (88, 90, 78, 82), 3: (82, 92, 84, 91),
                          4: (95, 96, 93, 95), **{k: (95, 96, 94, 95) for k in range(5, 10)}},
        "ETH/USDT:USDT": {4: (50, 50.5, 49.5, 50), 5: (50, 51, 48, 49), 6: (49, 50, 44, 46), 7: (51.5, 53, 51, 52),
                          8: (52, 52.5, 51.5, 52), 9: (52, 52.5, 51.5, 52)},
        "SOL/USDT:USDT": {5: (20, 20.2, 19.8, 20), 6: (20, 20.5, 19, 19.5), 7: (19.5, 21, 19.5, 20.8),
                          8: (21.6, 22.5, 21.5, 22), 9: (22, 22, 18, 18.5)},
    }
    drop = drop or {}
    (d / "futures").mkdir(parents=True, exist_ok=True)
    for pair, by_h in rows.items():
        hs = [h for h in sorted(by_h) if h not in drop.get(pair, [])]
        df = pd.DataFrame({
            "date": pd.to_datetime([T0 + h * H for h in hs], unit="s", utc=True),
            "open": [by_h[h][0] for h in hs], "high": [by_h[h][1] for h in hs],
            "low": [by_h[h][2] for h in hs], "close": [by_h[h][3] for h in hs], "volume": [1000.0] * len(hs),
        })
        df.to_feather(d / "futures" / f"{pair.replace('/', '_').replace(':', '_')}-1h-futures.feather")


def _norm(v: Any) -> Any:
    if hasattr(v, "value") and type(v).__module__.startswith("freqtrade"):
        return str(v)
    return v


def test_db_and_report(tmp: Path) -> None:
    from freqtrade import constants

    check("NON_OPEN_EXCHANGE_STATES 和 freqtrade 相同", tuple(fr.NON_OPEN_EXCHANGE_STATES),
          tuple(constants.NON_OPEN_EXCHANGE_STATES))
    check("DATETIME_PRINT_FORMAT 和 freqtrade 相同", fr.DATETIME_PRINT_FORMAT, constants.DATETIME_PRINT_FORMAT)

    db = tmp / "nfi.sqlite"
    build_db(db)
    want = freqtrade_json()

    # 机器人还开着数据库（有 -wal 文件）时只读
    check_true("建库后有 -wal 文件（freqtrade 用 WAL 模式）", Path(str(db) + "-wal").exists())
    con = fr.open_db_readonly(db)
    got_live = fr.load_trades(con)
    con.close()
    close_db()
    check_true("机器人停了以后 -wal 文件没了", not Path(str(db) + "-wal").exists())
    digest = hashlib.sha256(db.read_bytes()).hexdigest()
    con = fr.open_db_readonly(db)
    got = fr.load_trades(con)
    kv = fr.load_kv(con)
    con.close()
    check("两种情况读到的一样", got_live == got, True)
    check("读数据库不改文件（sha256 不变）", hashlib.sha256(db.read_bytes()).hexdigest(), digest)
    check_true("读完没有留下 -wal / -shm 文件",
               not Path(str(db) + "-wal").exists() and not Path(str(db) + "-shm").exists())

    check("交易笔数", len(got), len(want))
    for t in got:
        w = want[t["trade_id"]]
        extra = set(t) - set(w)
        check(f"#{t['trade_id']} 字段和 to_json 一样", (sorted(set(w) - set(t)), sorted(extra)), ([], ["funding_fee_running"]))
        bad = [k for k in w if k != "orders" and not _close(t.get(k), _norm(w[k]), 1e-12)]
        check(f"#{t['trade_id']} {t['pair']} 每个字段的值 = freqtrade to_json()", bad, [])
        check(f"#{t['trade_id']} 订单（只留已成交 / 挂着的，和 to_json 一样）", t["orders"], w["orders"])
    by_pair = {t["pair"]: t for t in got}
    check("撤掉的单不算（A 的订单 3 张）", len(by_pair["BTC/USDT:USDT"]["orders"]), 3)
    check("A 加仓后 profit_abs = 10", by_pair["BTC/USDT:USDT"]["profit_abs"], 10.0)
    check("C 分批平仓已实现 = 7.832", by_pair["SOL/USDT:USDT"]["realized_profit"], 7.832)
    check("KeyValueStore bot_start_time", kv.get("bot_start_time"), float(T0 - 24 * H))

    # ---- 报告数字（手算，1h K线；过程见下面注释）----
    # 每根K线 [最低权益, 收盘权益]（初始 1000）：
    #   h0 995/996  h1 970/973  h2 961/974  h3 966/1000（BTC 缺这根：按 A 的最低价 78 估）  h4 1000/1000
    #   h5 996/1004  h6 999.61/1000.605  h7 1000.605/1013.592  h8 1022.583/1025.58  h9 1001.604/1004.601
    #   最大回撤 = h2：(1000 - 961) / 1000 = 3.9%；现在 = (1025.58 - 1004.601) / 1025.58
    dd = tmp / "data"
    write_candles(dd, drop={"BTC/USDT:USDT": [3]})
    rep = fr.compute(got, start_balance=START, timeframe="1h", datadir=dd, now=NOW, max_dd=15.0, kv=kv)
    check("权益算到 T0+10h（最近一根收盘K线）", rep["as_of"], float(T0 + 10 * H))
    check("现在的权益", rep["equity_now"], 1004.601)
    check("已平仓盈亏 10 + 6 - 10", rep["realized_closed"], 6.0)
    check("持仓里分批平仓已实现", rep["realized_open"], 7.832)
    check("持仓浮动盈亏（SOL 6@20，现价 18.5，手续费 0.1%）", rep["upnl"], -9.231)
    check("权益 = 资金曲线最后一根收盘", rep["equity_now"], 1004.601)
    check("峰值", rep["peak"], 1025.58)
    check("最大回撤(含浮亏) 3.9%", rep["dd_max"], 0.039)
    check("现在的回撤", rep["dd_now"], (1025.58 - 1004.601) / 1025.58)
    check("回撤(平仓) 1%（XRP 先亏 10）", rep["dd_closed"], 0.01)
    check("收益", rep["return"], 0.004601)
    check("已平仓笔数", rep["closed"], 3)
    check("胜率", rep["winrate"], 2 / 3)
    check("盈亏比 16 / 10", rep["profit_factor"], 1.6)
    check("最差的一笔 = XRP -10", (rep["worst"]["pair"], rep["worst"]["profit_abs"]), ("XRP/USDT:USDT", -10.0))
    check("拿得最久 = SOL（还没平，4.5 小时）",
          (rep["longest"]["pair"], rep["longest"]["seconds"], rep["longest"]["open"]), ("SOL/USDT:USDT", 4.5 * H, True))
    check("运行天数（bot_start_time 起）", rep["days"], (NOW - (T0 - 24 * H)) / 86400)
    check("持仓 1 个", len(rep["positions"]), 1)
    p = rep["positions"][0]
    check("持仓：数量 / 均价 / 现价 / 来源", (p["amount"], p["avg"], p["price"], p["price_src"]), (6.0, 20.0, 18.5, "K线"))
    check("持仓：保证金 6 × 20 / 3", p["stake"], 40.0)
    check("持仓：持有 4.5 小时", p["age_s"], 4.5 * H)
    check("持仓：未结算资金费", rep["funding_running"], -0.05)
    check("挂着没成交的开仓单（ADA）", [x["pair"] for x in rep["pending"]], ["ADA/USDT:USDT"])
    cov = rep["coverage"]
    check("XRP 没有K线文件，2 根估算", (cov["XRP/USDT:USDT"]["file"], cov["XRP/USDT:USDT"]["missing"]), (False, 2))
    check("BTC 缺 1 / 4 根", (cov["BTC/USDT:USDT"]["missing"], cov["BTC/USDT:USDT"]["needed"]), (1, 4))
    check("ETH、SOL 不缺", (cov["ETH/USDT:USDT"]["missing"], cov["SOL/USDT:USDT"]["missing"]), (0, 0))
    check("成功标准达到 3 / 5（天数、笔数还不够）", [c["ok"] for c in rep["criteria"]], [False, False, True, True, True])
    check("没有超限", rep["dd_breach"], False)
    ws = rep["worst_single"]
    check("单笔最大亏损 = XRP -10 = 1%（SOL 已实现 7.832 + 浮亏 -9.231 = -1.399 没它大）",
          (ws["pair"], ws["profit"], ws["pct"], ws["open"]), ("XRP/USDT:USDT", -10.0, 0.01, False))
    check("杠杆：最高 3 倍，不是 1 倍的 2 笔（ETH 2 倍、SOL 3 倍）", (rep["leverage_max"], rep["leverage_not1"]), (3.0, 2))
    r60 = dict(rep, days=61.0)
    r90 = dict(rep, days=91.0)
    check_true("满 60 天不到 30 笔：结论是延长到 90 天", "延长到 90 天" in fr.verdict(r60), fr.verdict(r60))
    check_true("满 90 天不到 30 笔：没有结论", "没有结论" in fr.verdict(r90), fr.verdict(r90))
    text = fr.render(rep, title="stub")
    for frag in ("现在的权益（含浮亏）：1004.60 USDT", "回撤(含浮亏)：现在 2.05%，最大 3.90%", "XRP/USDT:USDT：没有K线文件",
                 "BTC/USDT:USDT：缺 1 / 4 根", "--pairs BTC/USDT:USDT XRP/USDT:USDT", "另有 1 笔开仓单还挂着",
                 "胜率 66.7%", "盈亏比 1.60", "结论：进行中", "单笔最大亏损（含持仓浮亏）：#2 XRP/USDT:USDT -10.00 USDT",
                 "不是 1 倍的 2 笔"):
        check_true(f"报告里有：{frag}", frag in text, text)
    tg = fr.telegram_text(rep, "stub")
    check_true("Telegram 摘要是中文、不长", "【模拟盘日报】" in tg and len(tg) < 600, tg)

    # 持仓那个币缺最新K线 → 现价退回持仓期间最差价（估）
    dd2 = tmp / "data-stale"
    write_candles(dd2, drop={"BTC/USDT:USDT": [3], "SOL/USDT:USDT": [9]})
    rep2 = fr.compute(got, start_balance=START, timeframe="1h", datadir=dd2, now=NOW, max_dd=15.0, kv=kv)
    p2 = rep2["positions"][0]
    check("SOL 缺最新K线：现价 = 最低价 18（估）", (p2["price"], p2["price_src"]), (18.0, "估"))
    check("SOL 缺最新K线：权益", rep2["equity_now"], 1000 + 6 + 7.832 + (6 * (18 - 20) - 0.12 - 0.108))
    check("一共 4 根估算", rep2["synthetic_candles"], 4)
    check_true("报告里标了 (估)", "(估)" in fr.render(rep2), "")

    # 命令行：正常 → 退出码 0，JSON 数字一致；初始资金改成 200 → 回撤 19.5% > 15% → 退出码 3
    out = tmp / "rep.json"
    base = ["--config", "none", "--db", str(db), "--datadir", str(dd), "--timeframe", "1h",
            "--as-of", datetime.fromtimestamp(NOW, tz=timezone.utc).isoformat()]
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = fr.main(base + ["--start-balance", "1000", "--json", str(out)])
    check("命令行退出码 0", code, 0)
    data = json.loads(out.read_text(encoding="utf-8"))
    check("JSON 里的最大回撤", data["dd_max"], 0.039)
    with contextlib.redirect_stdout(io.StringIO()) as buf3:
        code3 = fr.main(base + ["--start-balance", "200"])
    check("初始资金 200：回撤 (200 - 161) / 200 = 19.5% → 退出码 3", code3, 3)
    check_true("超限时结论是不通过", "结论：不通过" in buf3.getvalue(), buf3.getvalue()[-300:])
    # 看门狗的状态文件在数据库旁边：没触发过 → 照常；触发过（现在暂停中）→ 回撤这一项不通过、退出码 3
    wd_file = tmp / "nfi-watchdog.json"
    st = ew.new_state()
    st.update(max_dd_seen=0.031, last={"at": float(NOW - 60)})
    ew.save_state(wd_file, st)
    with contextlib.redirect_stdout(io.StringIO()) as buf4:
        code4 = fr.main(base + ["--start-balance", "1000"])
    check("看门狗没触发过：退出码 0", code4, 0)
    check_true("报告里写了看门狗没触发过", "看门狗没触发过" in buf4.getvalue() and "看门狗：没有暂停" in buf4.getvalue(),
               buf4.getvalue())
    st.update(halted=True, halted_at=float(NOW - 3600), max_dd_seen=0.152)
    ew.save_state(wd_file, st)
    with contextlib.redirect_stdout(io.StringIO()) as buf5:
        code5 = fr.main(base + ["--start-balance", "1000"])
    check("看门狗暂停中（报告按K线只有 3.9%）：退出码 3", code5, 3)
    check_true("看门狗触发过：结论不通过", "结论：不通过：看门狗触发过 1 次" in buf5.getvalue(), buf5.getvalue()[-400:])
    with contextlib.redirect_stdout(io.StringIO()):
        code6 = fr.main(base + ["--start-balance", "1000", "--watchdog-state", "none"])
    check("--watchdog-state none：不读看门狗", code6, 0)
    wd_file.unlink()
    with contextlib.redirect_stderr(io.StringIO()):
        code_err = fr.main(["--config", "none", "--db", str(tmp / "nope.sqlite"), "--start-balance", "1000"])
    check("找不到数据库 → 退出码 1", code_err, 1)
    with contextlib.redirect_stderr(io.StringIO()):
        code_nobal = fr.main(["--config", "none", "--db", str(db)])
    check("不知道初始资金 → 退出码 1", code_nobal, 1)

    # 空数据库（刚启动，还没有交易）
    from freqtrade.persistence import init_db

    empty = tmp / "empty.sqlite"
    init_db(f"sqlite:///{empty}")
    close_db()
    con = fr.open_db_readonly(empty)
    rep0 = fr.compute(fr.load_trades(con), start_balance=START, timeframe="5m", datadir=dd, now=NOW, max_dd=15.0,
                      kv=fr.load_kv(con))
    con.close()
    check("空数据库：权益 = 初始资金、回撤 0", (rep0["equity_now"], rep0["dd_max"], rep0["closed"]), (START, 0.0, 0))
    check_true("空数据库也能出报告", "已平仓 0 笔" in fr.render(rep0), "")


def build_late_close_db(path: Path, variant: str) -> None:
    """回归用的小数据库（1h K线，不收手续费，1 倍）：XRP 4 笔各赚 75，第 4 笔在最后一根收盘K线 [9h, 10h) 里平掉。
    variant = "open"：另有一笔 SOL 多单 10@100 一直拿着（现价 80，浮亏 -200）；
    variant = "after"：另有一笔 DOGE 多单 1000@1.0，在报告时间所在、还没收盘的K线里（10.2h）以 1.1 平掉，
                       持仓期间最低价 0.8 只出现在还没收盘的那根K线里。"""
    from freqtrade.enums import TradingMode
    from freqtrade.persistence import Order, Trade, init_db

    init_db(f"sqlite:///{path}")

    def make(pair: str, fills: list[tuple[float, float, float, bool]], min_rate: float, max_rate: float) -> None:
        h0, a0, p0, _ = fills[0]
        trade = Trade(pair=pair, exchange="binance", base_currency=pair.split("/")[0], stake_currency="USDT",
                      is_open=True, fee_open=0.0, fee_close=0.0, open_rate=p0, open_rate_requested=p0, amount=a0,
                      amount_requested=a0, stake_amount=a0 * p0, open_date=dt(h0), leverage=1.0, is_short=False,
                      trading_mode=TradingMode.FUTURES, strategy="StubStrategy", timeframe=60)
        Trade.session.add(trade)
        Trade.commit()
        for n, (h, amount, price, is_entry) in enumerate(fills):
            side = "buy" if is_entry else "sell"
            o = Order(ft_order_side=side, ft_pair=pair, ft_is_open=False, ft_amount=amount, ft_price=price,
                      order_id=f"{pair}-{h}-{n}", status="closed", symbol=pair, order_type="limit", side=side,
                      price=price, average=price, amount=amount, filled=amount, remaining=0.0, cost=amount * price,
                      order_date=dt(h), order_filled_date=dt(h), order_update_date=dt(h))
            trade.orders.append(o)
            if not is_entry:
                trade.exit_reason = "exit_signal"
            trade.update_trade(o)
        trade.min_rate, trade.max_rate = min_rate, max_rate
        Trade.commit()

    if variant == "open":
        make("SOL/USDT:USDT", [(0.5, 10, 100.0, True)], 79.5, 100.0)
    for o, c in ((1.5, 2.5), (3.5, 4.5), (5.5, 6.5), (7.5, 9.5)):
        make("XRP/USDT:USDT", [(o, 100, 1.0, True), (c, 100, 1.75, False)], 1.0, 1.75)
    if variant == "after":
        make("DOGE/USDT:USDT", [(8.5, 1000, 1.0, True), (10.2, 1000, 1.1, False)], 0.8, 1.1)


def test_late_close(tmp: Path) -> None:
    """最后一根收盘K线里（或之后）刚平掉一笔时，曲线不能多出一根只有已实现盈亏、没有持仓浮亏的K线。"""
    import pandas as pd

    dd = tmp / "data"
    (dd / "futures").mkdir(parents=True)
    sol = [100.0, 98, 96, 94, 92, 90, 88, 86, 84, 80]
    frames = {
        "SOL_USDT_USDT": (list(range(10)), sol, [c + 0.5 for c in sol], [99.5] + [c - 0.5 for c in sol[1:]]),
        "XRP_USDT_USDT": (list(range(10)), [1.0] * 10, [1.75] * 10, [1.0] * 10),
        "DOGE_USDT_USDT": ([8, 9], [1.0, 1.0], [1.0, 1.0], [1.0, 1.0]),
    }
    for name, (hs, close, high, low) in frames.items():
        pd.DataFrame({"date": pd.to_datetime([T0 + h * H for h in hs], unit="s", utc=True), "open": close,
                      "high": high, "low": low, "close": close, "volume": [1.0] * len(hs)}).to_feather(
            dd / "futures" / f"{name}-1h-futures.feather")
    now = T0 + 10 * H + 20 * 60  # 报告时间 10:20 → 最后一根收盘K线 [9h, 10h)

    db = tmp / "late-open.sqlite"
    build_late_close_db(db, "open")
    close_db()
    con = fr.open_db_readonly(db)
    trades = fr.load_trades(con)
    con.close()
    # 收盘权益：h6 = 1000 - 120 + 225 = 1105 是峰值；h9 最低 = 1000 + 225（第 4 笔还没平）- 205 = 1020；
    # 现在 = 1000 + 300 - 200 = 1100。旧算法多加一根 10h（只有 +300、没有 SOL 的 -200）：峰值 1300，回撤 15.38% → 误报超限
    rep = fr.compute(trades, start_balance=START, timeframe="1h", datadir=dd, now=now, max_dd=15.0)
    check("刚平掉一笔 + 持仓浮亏：现在的权益 1100", rep["equity_now"], 1100.0)
    check("刚平掉一笔 + 持仓浮亏：峰值 1105（不是 1300）", rep["peak"], 1105.0)
    check("刚平掉一笔 + 持仓浮亏：现在的回撤 5 / 1105", rep["dd_now"], 5 / 1105)
    check("刚平掉一笔 + 持仓浮亏：最大回撤 85 / 1105", rep["dd_max"], 85 / 1105)
    check("刚平掉一笔 + 持仓浮亏：没有超限", rep["dd_breach"], False)
    eq = cd.account_equity_drawdown(trades, START, "1h", dd, as_of=float(T0 + 10 * H))
    check("account_equity_drawdown：曲线到 as_of 前一根（9h）为止", int(eq["curve"]["grid"][-1]), T0 + 9 * H)
    check("account_equity_drawdown：现在的权益 / 现在的回撤", (eq["equity_now"], eq["dd_now"]), (1100.0, 85 / 1105))
    with contextlib.redirect_stdout(io.StringIO()):
        code = fr.main(["--config", "none", "--db", str(db), "--datadir", str(dd), "--timeframe", "1h",
                        "--start-balance", "1000", "--watchdog-state", "none", "--env-file", "none",
                        "--as-of", datetime.fromtimestamp(now, tz=timezone.utc).isoformat()])
    check("刚平掉一笔 + 持仓浮亏：命令行退出码 0", code, 0)

    db2 = tmp / "late-after.sqlite"
    build_late_close_db(db2, "after")
    close_db()
    con = fr.open_db_readonly(db2)
    trades2 = fr.load_trades(con)
    con.close()
    # DOGE 在 10.2h 平掉（+100）：现在 = 1400；还没收盘的 10h 那根不进峰值 / 回撤，也不用交易记录的最低价 0.8 补
    rep2 = fr.compute(trades2, start_balance=START, timeframe="1h", datadir=dd, now=now, max_dd=15.0)
    check("还没收盘的K线里平仓：现在的权益 1400、回撤 0",
          (rep2["equity_now"], rep2["peak"], rep2["dd_now"], rep2["dd_max"], rep2["dd_breach"]),
          (1400.0, 1400.0, 0.0, 0.0, False))
    check("还没收盘的K线不补、不计入估算根数",
          (rep2["synthetic_candles"], rep2["coverage"]["DOGE/USDT:USDT"]["needed"]), (0, 2))


# ----------------------------------------------------------------------------
# 2. 假的 freqtrade REST API
# ----------------------------------------------------------------------------
class MockBot:
    def __init__(self, user: str = "wd-user", password: str = "wd-pass-123") -> None:
        self.user, self.password = user, password
        self.state = "running"
        self.wallet = 1000.0
        self.trades: list[dict[str, Any]] = []
        self.calls: list[tuple[str, str, Any]] = []
        self.telegram: list[tuple[str, Any]] = []
        self.flip_status = 0  # > 0：前几次 /status 返回不一样的持仓（模拟读的时候正好有交易平仓）
        self.stuck_running = False  # True：stopentry 返回成功但状态不变（模拟暂停没生效）
        self.lock = threading.Lock()

    def set_upnl(self, upnl: float | None, *, pair: str = "SOL/USDT:USDT", amount: float = 10.0) -> None:
        self.trades = [] if upnl is None and amount == 0 else [{
            "trade_id": 7, "pair": pair, "is_open": True, "is_short": False, "amount": amount, "open_rate": 20.0,
            "min_rate": 15.0, "max_rate": 21.0, "fee_open": 0.0, "fee_close": 0.0,
            "profit_abs": upnl, "current_rate": None if upnl is None else 20 + upnl / amount,
        }]

    def handle(self, method: str, path: str, auth: str | None, body: Any) -> tuple[int, Any]:
        with self.lock:
            if path.startswith("/bot") and path.endswith("/sendMessage"):
                self.telegram.append((path, body))
                return 200, {"ok": True}
            self.calls.append((method, path, body))
            import base64

            want = "Basic " + base64.b64encode(f"{self.user}:{self.password}".encode()).decode()
            if auth != want:
                return 401, {"detail": "Unauthorized"}
            if method == "GET" and path == "/api/v1/show_config":
                return 200, {"state": self.state, "dry_run": True, "stake_currency": "USDT", "bot_name": "mock-bot",
                             "max_open_trades": 6}
            if method == "GET" and path == "/api/v1/balance":
                upnl = sum(t["profit_abs"] or 0 for t in self.trades)
                return 200, {"currencies": [{"currency": "USDT", "free": self.wallet, "balance": self.wallet,
                                             "used": 0.0, "est_stake": self.wallet, "stake": "USDT", "side": "long",
                                             "is_position": False, "position": 0, "is_bot_managed": True}],
                             "total": self.wallet + upnl, "total_bot": self.wallet + upnl, "stake": "USDT"}
            if method == "GET" and path == "/api/v1/status":
                if self.flip_status > 0:
                    self.flip_status -= 1
                    return 200, [dict(t, trade_id=99) for t in self.trades] + [{"trade_id": 98, "amount": 1}]
                return 200, self.trades
            if method == "POST" and path in ("/api/v1/stopentry", "/api/v1/stopbuy", "/api/v1/pause"):
                if self.state in ("running", "stopped") and not self.stuck_running:
                    self.state = "paused"
                return 200, {"status": "paused, no more entries will occur from now. Run /start to enable entries."}
            if method == "POST" and path == "/api/v1/forceexit":
                return 200, {"result": "Created exit orders for all open trades."}
            if method == "POST" and path == "/api/v1/start":
                self.state = "running"
                return 200, {"status": "starting trader ..."}
            if method == "POST" and path == "/api/v1/reload_config":
                return 200, {"status": "Reloading config ..."}
            return 404, {"detail": "Not Found"}

    def paths(self, method: str | None = None) -> list[str]:
        return [p for m, p, _b in self.calls if method is None or m == method]


def start_mock(bot: MockBot) -> tuple[ThreadingHTTPServer, str]:
    class Handler(BaseHTTPRequestHandler):
        def _do(self, method: str) -> None:
            n = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(n) if n else b""
            body = json.loads(raw) if raw else None
            code, resp = bot.handle(method, self.path, self.headers.get("Authorization"), body)
            data = json.dumps(resp).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self) -> None:  # noqa: N802
            self._do("GET")

        def do_POST(self) -> None:  # noqa: N802
            self._do("POST")

        def log_message(self, *args: Any) -> None:
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


class Clock:
    def __init__(self, t: float) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_watchdog(tmp: Path) -> None:
    bot = MockBot()
    srv, url = start_mock(bot)
    logs: list[str] = []
    clock = Clock(float(T0))
    state = tmp / "wd.json"

    def make(*, api: ew.FtApi | None = None, state_path: Path | None = None, **kw: Any) -> ew.Watchdog:
        args: dict[str, Any] = {"max_dd": 15.0, "warn_dd": 12.0, "notifier": ew.Notifier(None, log=logs.append),
                                "start_balance": 1000.0, "clock": clock, "log": logs.append}
        args.update(kw)
        return ew.Watchdog(api or ew.FtApi(url, bot.user, bot.password, timeout=5), state_path or state, **args)

    try:
        check("上限默认值来自 check_drawdown.DEFAULT_MAX_DD_PCT", cd.DEFAULT_MAX_DD_PCT, 15.0)
        wd = make()
        sent = wd.notifier.sent
        r = wd.poll_once()
        check("第一次：权益 1000、回撤 0", (r["ok"], r["equity"], r["dd"], r["halted"]), (True, 1000.0, 0.0, False))
        bot.wallet = 1100.0
        clock.t += 60
        r = wd.poll_once()
        check("权益涨到 1100：峰值 1100", r["peak"], 1100.0)
        bot.set_upnl(-154.0)
        clock.t += 60
        r = wd.poll_once()
        check("回撤 14%：不暂停", (round(r["dd"], 6), r["halted"]), (0.14, False))
        check("回撤 14%：没调 stopentry", bot.paths("POST"), [])
        check_true("回撤 14%：发了预警（超过 12%）", any("回撤预警" in s for s in sent), str(sent))
        n_warn = len(sent)
        clock.t += 60
        wd.poll_once()
        check("预警只发一次", len(sent), n_warn)
        bot.set_upnl(-165.0)
        clock.t += 60
        r = wd.poll_once()
        check("回撤正好 15%：不暂停（规则是超过 15%）", (r["halted"], bot.paths("POST")), (False, []))
        bot.set_upnl(-170.5)
        clock.t += 60
        r = wd.poll_once()
        check("回撤 15.5%：暂停", (round(r["dd"], 6), r["halted"]), (0.155, True))
        check("回撤 15.5%：调了一次 stopentry", bot.paths("POST"), ["/api/v1/stopentry"])
        check_true("暂停告警是中文、说了怎么解除",
                   any("已停止开新仓" in s and "--reset" in s for s in sent), str(sent[-1:]))
        st = ew.load_state(state)
        check("状态文件：已暂停、峰值 1100", (st["halted"], st["peak"]), (True, 1100.0))
        clock.t += 60
        wd.poll_once()
        check("机器人已经 paused：不再重复调 stopentry", bot.paths("POST").count("/api/v1/stopentry"), 1)
        bot.set_upnl(50.0)
        clock.t += 60
        r = wd.poll_once()
        check("权益回到 1150：仍然暂停", (r["halted"], r["dd"], r["peak"]), (True, 0.0, 1150.0))
        check("从不调 /start、/reload_config", [p for p in bot.paths() if p in ("/api/v1/start", "/api/v1/reload_config")], [])
        # 机器人重启了（暂停状态不保存，又是 running）→ 再暂停
        bot.state = "running"
        clock.t += 60
        n = len(sent)
        wd.poll_once()
        check("机器人重启后又是 running：再调 stopentry", bot.paths("POST").count("/api/v1/stopentry"), 2)
        check_true("重启后再暂停有告警", len(sent) > n and "再次停止开新仓" in sent[-1], str(sent[-1:]))
        # 换一个看门狗对象（= 看门狗进程重启）：峰值和暂停状态从文件接着用
        wd2 = make()
        clock.t += 60
        r = wd2.poll_once()
        check("看门狗重启：峰值 1150、仍然暂停", (r["peak"], r["halted"]), (1150.0, True))
        # 暂停 12 小时以后提醒
        clock.t += 13 * H
        n = len(wd2.notifier.sent)
        wd2.poll_once()
        check_true("暂停 12 小时后提醒一次", len(wd2.notifier.sent) == n + 1 and "还在等人工复盘" in wd2.notifier.sent[-1],
                   str(wd2.notifier.sent[-1:]))
        # 解除：要写结论；回撤仍超限时不让保留峰值解除
        bot.set_upnl(-250.0)  # 权益 850，峰值 1150 → 26%
        clock.t += 60
        wd2.poll_once()
        code, _msg = ew.reset_state(state, note="", rebase_peak=False, max_dd=15.0, clock=clock)
        check("--reset 不写 --note：拒绝", code, 1)
        code, msg = ew.reset_state(state, note="复盘：测试", rebase_peak=False, max_dd=15.0, clock=clock)
        check("回撤仍超限、不加 --rebase-peak：拒绝解除", (code, ew.load_state(state)["halted"]), (1, True))
        code, msg = ew.reset_state(state, note="复盘：测试，从现在重新算", rebase_peak=True, max_dd=15.0, clock=clock)
        st = ew.load_state(state)
        check("--reset --rebase-peak：解除、峰值清空、记下结论",
              (code, st["halted"], st["peak"], st["resets"][-1]["note"]), (0, False, None, "复盘：测试，从现在重新算"))
        clock.t += 60
        r = wd2.poll_once()
        check("解除后：峰值从现在的权益 850 算（不用初始资金 1000 垫底），不再暂停",
              (r["peak"], r["dd"], r["halted"]), (850.0, 0.0, False))
        check("解除后看门狗也不会自己恢复开仓", [p for p in bot.paths() if p == "/api/v1/start"], [])
        check("机器人仍然是 paused（等人手动点开始）", bot.state, "paused")

        # 命令行：--once / --status / --resume（账号密码从环境变量）
        env = {ew.USER_ENV: bot.user, ew.PASS_ENV: bot.password}
        old_env = {k: os.environ.get(k) for k in (*env, ew.TG_TOKEN_ENV, ew.TG_CHAT_ENV, cd.MAX_DD_ENV)}
        os.environ.update(env)
        for k in (ew.TG_TOKEN_ENV, ew.TG_CHAT_ENV, cd.MAX_DD_ENV):
            os.environ.pop(k, None)
        try:
            common = ["--config", "none", "--env-file", "none", "--url", url]
            state3 = tmp / "wd-cli.json"
            bot.wallet = 1000.0
            bot.set_upnl(-50.0)
            with contextlib.redirect_stdout(io.StringIO()):
                c_once = ew.main(common + ["--state", str(state3), "--once", "--start-balance", "1000"])
            check("--once：回撤没超限 → 退出码 0", c_once, 0)
            bot.set_upnl(-200.0)
            bot.state = "running"
            with contextlib.redirect_stdout(io.StringIO()):
                c_once = ew.main(common + ["--state", str(state3), "--once", "--start-balance", "1000"])
            check("--once：回撤 20% → 暂停、退出码 3", c_once, 3)
            with contextlib.redirect_stdout(io.StringIO()) as out, contextlib.redirect_stderr(io.StringIO()):
                c_status = ew.main(common + ["--state", str(state3), "--status"])
            check("--status：暂停中 → 退出码 3", c_status, 3)
            check_true("--status 是中文", "已暂停开新仓" in out.getvalue(), out.getvalue())
            n_start = bot.paths().count("/api/v1/start")
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                c_res = ew.main(common + ["--state", str(state3), "--resume"])
            check("还没 --reset 就 --resume：拒绝（退出码 3），没调 /start",
                  (c_res, bot.paths().count("/api/v1/start")), (3, n_start))
            with contextlib.redirect_stdout(io.StringIO()):
                ew.main(common + ["--state", str(state3), "--reset", "--rebase-peak", "--note", "复盘：测试"])
                c_res = ew.main(common + ["--state", str(state3), "--resume"])
            check("--reset 以后 --resume：调了一次 /start", (c_res, bot.paths().count("/api/v1/start")), (0, n_start + 1))
            os.environ.pop(ew.PASS_ENV)
            with contextlib.redirect_stderr(io.StringIO()) as err:
                c_nopw = ew.main(common + ["--state", str(state3), "--once"])
            check("没有 API 密码 → 退出码 1", c_nopw, 1)
            check_true("没有密码时的提示是中文", "没有 API 账号密码" in err.getvalue(), err.getvalue())
            # 回撤上限比硬规则 15% 宽：看门狗拒绝启动、--reset 也拒绝（只能改严）
            state_loose = tmp / "wd-loose.json"
            st_loose = ew.new_state()
            st_loose.update(peak=1000.0, halted=True, last={"at": float(T0), "equity": 780.0, "dd": 0.22})
            ew.save_state(state_loose, st_loose)
            os.environ[cd.MAX_DD_ENV] = "30"
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()) as err:
                c_loose = ew.main(common + ["--state", str(state_loose), "--once", "--start-balance", "1000"])
                c_loose_reset = ew.main(common + ["--state", str(state_loose), "--reset", "--note", "测试"])
            check("MAX_DD_PCT=30（比 15% 宽）：--once 和 --reset 都拒绝（退出码 1）、状态文件不变",
                  (c_loose, c_loose_reset, ew.load_state(state_loose)["halted"]), (1, 1, True))
            check_true("拒绝的说明是中文、说了怎么回事", "比项目硬规则 15% 宽" in err.getvalue(), err.getvalue())
            os.environ.pop(cd.MAX_DD_ENV)
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                c_loose_arg = ew.main(common + ["--state", str(state_loose), "--reset", "--note", "测试", "--max-dd", "25"])
            check("--max-dd 25：一样拒绝", c_loose_arg, 1)
            check("reset_state 直接调用：上限 25% 拒绝", ew.reset_state(state_loose, note="x", rebase_peak=False,
                                                                 max_dd=25.0, clock=clock)[0], 1)
            envf_loose = tmp / ".env.loose"
            envf_loose.write_text("MAX_DD_PCT=30\n", encoding="utf-8")
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                c_loose_file = ew.main(["--config", "none", "--env-file", str(envf_loose), "--url", url,
                                        "--state", str(state_loose), "--once", "--start-balance", "1000"])
            os.environ.pop(cd.MAX_DD_ENV, None)
            check("密钥文件里写 MAX_DD_PCT=30：直接运行也拒绝（先读文件再定上限）", c_loose_file, 1)
            os.environ[cd.MAX_DD_ENV] = "12"
            os.environ[ew.PASS_ENV] = bot.password
            bot.set_upnl(-130.0)  # 13%
            bot.state = "running"
            state4 = tmp / "wd-env.json"
            with contextlib.redirect_stdout(io.StringIO()):
                c_env = ew.main(common + ["--state", str(state4), "--once", "--start-balance", "1000"])
            check("环境变量 MAX_DD_PCT=12：回撤 13% 就暂停（和别的脚本同一个开关）", c_env, 3)
        finally:
            for k, v in old_env.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v

        # stopentry 返回成功、机器人却一直是 running：重试，第 3 次还不行告警一次
        bot.stuck_running = True
        bot.state = "running"
        bot.wallet = 1000.0
        bot.set_upnl(-200.0)
        wd_stuck = make(state_path=tmp / "wd-stuck.json")
        for _ in range(5):
            clock.t += 60
            wd_stuck.poll_once()
        bot.stuck_running = False
        check("暂停一直不生效：每次都重试 stopentry", sum(1 for p in bot.paths() if p == "/api/v1/stopentry") >= 5, True)
        check("暂停一直不生效：只告警一次", len([s for s in wd_stuck.notifier.sent if "暂停开新仓没有生效" in s]), 1)

        # 读的时候正好有交易成交：前后两次 /status 不一样 → 重读
        bot.wallet = 1000.0
        bot.set_upnl(-10.0)
        bot.flip_status = 1
        wd5 = make(state_path=tmp / "wd-flip.json")
        r = wd5.poll_once()
        check("前后两次持仓不一样时重读，用稳定的那次", (r["ok"], r["equity"]), (True, 990.0))
        # 拿不到现价（profit_abs 为空）：按最差价估
        bot.set_upnl(None, amount=10.0)
        snap = wd5.read_bot()
        check("没有现价：按 min_rate 15 估 10 × (15 - 20) = -50", (snap["upnl"], snap["estimated"]),
              (-50.0, ["SOL/USDT:USDT"]))

        # 看门狗自己出错（状态文件坏了，每次检查都抛异常）：10 分钟后告警一次；连续 10 次退出（systemd 会重启）
        bad_state = tmp / "wd-corrupt.json"
        bad_state.write_text('{"halted": false, "peak": 1000', encoding="utf-8")
        wd_bad = make(state_path=bad_state)
        sleeps2: list[float] = []

        def fake_sleep(sec: float) -> None:
            sleeps2.append(sec)
            clock.t += sec

        wd_bad.run(60, max_backoff=300, max_polls=3, sleep=fake_sleep)
        check("内部出错 3 次（3 分钟）：还不告警", [s for s in wd_bad.notifier.sent if "内部出错" in s], [])
        exited = None
        try:
            wd_bad.run(60, max_backoff=300, max_polls=20, sleep=fake_sleep)
        except SystemExit as e:
            exited = str(e)
        alerts = [s for s in wd_bad.notifier.sent if "现在没在保护账户" in s]
        check("内部出错超过 10 分钟：告警一次（说了没在保护账户）", len(alerts), 1)
        check_true("连续 10 次内部出错：退出（SystemExit），并告警", exited is not None and "连续 10 次" in exited
                   and any("现在退出" in s for s in wd_bad.notifier.sent), str(exited))
        bad_state.write_text("{}", encoding="utf-8")
        wd_bad.internal_errors = 9
        wd_bad.internal_fail_since = clock.t - 3600
        wd_bad.internal_alerted = True
        bot.state = "running"
        bot.set_upnl(0.0)
        wd_bad.run(60, max_polls=1, sleep=fake_sleep)
        check_true("状态文件修好以后：说一声恢复、计数清零",
                   any("恢复正常" in s for s in wd_bad.notifier.sent) and wd_bad.internal_errors == 0, "")

        # --flatten：暂停的同时平掉全部持仓
        check("默认不平仓：前面的看门狗从没调过 forceexit", "/api/v1/forceexit" in bot.paths(), False)
        bot.calls.clear()
        bot.state = "running"
        bot.set_upnl(-200.0)
        wd6 = make(state_path=tmp / "wd-flat.json", flatten=True)
        r = wd6.poll_once()
        check("--flatten：调 stopentry 和 forceexit", bot.paths("POST"), ["/api/v1/stopentry", "/api/v1/forceexit"])
        check("--flatten：forceexit 的参数 tradeid = all、ordertype = market（不写时用配置里的 limit，可能不成交）",
              [b for m, p, b in bot.calls if p == "/api/v1/forceexit"], [{"tradeid": "all", "ordertype": "market"}])
        check("--flatten：记下的动作", r["actions"], ["stopentry", "forceexit"])

        # 连不上：不崩、逐步加长等待、10 分钟后告警一次、恢复后说一声
        dead = ew.FtApi(f"http://127.0.0.1:{free_port()}", "u", "p", timeout=2)
        wd7 = make(state_path=tmp / "wd-dead.json", api=dead)
        clock.t += 60
        r = wd7.poll_once()
        check("连不上：返回出错、不抛异常", (r["ok"], r["kind"]), (False, "unreachable"))
        clock.t += 5 * 60
        wd7.poll_once()
        check("连不上 5 分钟：还不告警", [s for s in wd7.notifier.sent if "连不上" in s], [])
        clock.t += 6 * 60
        wd7.poll_once()
        clock.t += 60
        wd7.poll_once()
        check("连不上超过 10 分钟：告警一次", len([s for s in wd7.notifier.sent if "连不上机器人" in s]), 1)
        sleeps: list[float] = []
        wd7.fail_count = 0
        wd7.run(60, max_backoff=300, max_polls=6, sleep=sleeps.append)
        check("连不上时等待逐步加长、最长 300 秒", sleeps, [60, 120, 240, 300, 300])
        wd7.api = ew.FtApi(url, bot.user, bot.password, timeout=5)
        bot.set_upnl(0.0)
        clock.t += 60
        r = wd7.poll_once()
        check_true("恢复连接：说一声", r["ok"] and any("重新连上机器人" in x for x in wd7.notifier.sent),
                   str(wd7.notifier.sent[-2:]))
        check("恢复后清掉出错记录", ew.load_state(tmp / "wd-dead.json")["fail_since"], None)

        # 密码不对：告警一次，不崩
        wd8 = make(state_path=tmp / "wd-auth.json", api=ew.FtApi(url, bot.user, "wrong-password", timeout=5))
        r1 = wd8.poll_once()
        wd8.poll_once()
        check("密码不对：出错类型 auth", (r1["ok"], r1["kind"]), (False, "auth"))
        check("密码不对：告警只发一次", len([s for s in wd8.notifier.sent if "登录失败" in s]), 1)

        # 只连本机
        for bad_url in ("http://10.1.2.3:8081", "http://example.com:8081", "http://u:p@127.0.0.1:8081"):
            try:
                ew.check_api_url(bad_url, False)
                ok = False
            except ValueError:
                ok = True
            check_true(f"拒绝 {bad_url}", ok, "")
        check("允许 127.0.0.1", ew.check_api_url("http://127.0.0.1:8081/", False), "http://127.0.0.1:8081")

        # Telegram：发到假的 Bot API
        ok, msg = ew.send_telegram("【测试】中文告警", "123:abc", "42", api_base=url)
        check("Telegram 发送成功", (ok, msg), (True, "已发送"))
        path, body = bot.telegram[-1]
        check("Telegram 请求：路径和内容", (path, body["chat_id"], body["text"]), ("/bot123:abc/sendMessage", "42", "【测试】中文告警"))
        n = len(bot.telegram)
        notifier = ew.Notifier(("123:abc", "42"), api_base=url, log=logs.append)
        wd9 = make(state_path=tmp / "wd-tg.json", notifier=notifier)
        bot.state = "running"
        bot.set_upnl(-200.0)
        wd9.poll_once()
        check_true("暂停告警发到了 Telegram", len(bot.telegram) == n + 1 and "已停止开新仓" in bot.telegram[-1][1]["text"],
                   str(bot.telegram[-1:]))
        ok, msg = ew.send_telegram("x", "123:abc", "42", api_base=f"http://127.0.0.1:{free_port()}")
        check_true("Telegram 连不上：返回失败、说明里没有 token", not ok and "123:abc" not in msg, msg)
        check_true("日志里没有 API 密码", not any(bot.password in s for s in logs), "")

        # .env 文件：只读需要的几个变量，别的（例如交易所 Key）不碰；已有的环境变量不覆盖
        envf = tmp / ".env.test"
        envf.write_text("export FREQTRADE__API_SERVER__USERNAME='file-user'\nFREQTRADE__EXCHANGE__KEY=should-not-load\n"
                        "# 注释\nFREQTRADE__TELEGRAM__CHAT_ID=\"77\"\n", encoding="utf-8")
        saved = {k: os.environ.pop(k, None) for k in (ew.USER_ENV, "FREQTRADE__EXCHANGE__KEY", ew.TG_CHAT_ENV)}
        try:
            loaded = ew.load_env_keys(envf)
            check(".env：只读需要的变量", (sorted(loaded), os.environ.get(ew.USER_ENV), os.environ.get(ew.TG_CHAT_ENV),
                                       os.environ.get("FREQTRADE__EXCHANGE__KEY")),
                  (sorted([ew.USER_ENV, ew.TG_CHAT_ENV]), "file-user", "77", None))
        finally:
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
        # 配置读取（没有 freqtrade 时的简单读法）和真配置的端口 / 数据库 / 初始资金
        cfgf = tmp / "cfg.json"
        (tmp / "sub.json").write_text('{"dry_run_wallet": 500, "api_server": {"listen_port": 9999}}', encoding="utf-8")
        cfgf.write_text('{\n // 注释\n "add_config_files": ["sub.json"], "url": "http://a//b", /* 块注释 */\n'
                        ' "db_url": "sqlite:///user_data/dryrun/x.sqlite", "dry_run_wallet": 1000,}\n', encoding="utf-8")
        import builtins

        real_import = builtins.__import__

        def no_freqtrade(name: str, *a: Any, **k: Any) -> Any:
            if name.startswith("freqtrade"):
                raise ImportError(name)
            return real_import(name, *a, **k)

        builtins.__import__ = no_freqtrade
        try:
            cfg = ew.load_config(cfgf)
        finally:
            builtins.__import__ = real_import
        check("没有 freqtrade 时也能读带注释的配置（含 add_config_files）",
              (cfg["dry_run_wallet"], cfg["api_server"]["listen_port"], cfg["url"]), (1000, 9999, "http://a//b"))
        real_cfg = ew.load_config(fr.DEFAULT_CONFIG) if fr.DEFAULT_CONFIG.is_file() else None
        if real_cfg is not None:
            check("configs/config.dryrun-nfi.json：数据库位置", ew.config_db_path(real_cfg),
                  fr.ROOT_DIR / "user_data" / "dryrun" / "nfi.sqlite")
            check("configs/config.dryrun-nfi.json：API 只在本机 8081",
                  ((real_cfg.get("api_server") or {}).get("listen_ip_address"),
                   (real_cfg.get("api_server") or {}).get("listen_port")), ("127.0.0.1", 8081))
            check("configs/config.dryrun-nfi.json：初始资金", ew.config_start_balance(real_cfg), 1000.0)
    finally:
        srv.shutdown()
        srv.server_close()


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="easytrade-forward-check-"))
    try:
        for name, fn in (("模拟盘报告", test_db_and_report), ("模拟盘报告（最后一根K线里刚平仓）", test_late_close),
                         ("账户权益看门狗", test_watchdog)):
            sub = tmp / name
            sub.mkdir()
            try:
                fn(sub)
            except Exception:  # noqa: BLE001
                import traceback

                RESULTS.append((False, f"{name}：运行出错", traceback.format_exc()))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    n_fail = 0
    for ok, name, detail in RESULTS:
        if ok:
            print(f"通过  {name}")
        else:
            n_fail += 1
            print(f"失败  {name}" + (f"\n      {detail}" if detail else ""))
    print()
    if n_fail:
        print(f"自检结果：{len(RESULTS) - n_fail} 项通过，{n_fail} 项失败（共 {len(RESULTS)} 项）")
        return 1
    print(f"自检结果：全部通过（{len(RESULTS)} 项）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
