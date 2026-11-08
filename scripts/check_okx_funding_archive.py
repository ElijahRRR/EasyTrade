#!/usr/bin/env python3
"""
scripts/okx_funding_archive.py（OKX 资金费历史归档导入）的离线自检（不联网，make guard-test 也会跑）。

  1. CSV 列名的各种写法（不分大小写、驼峰、BOM）、"实际结算的费率"优先、只有 funding_rate 时的警告、认不出的表头 -> 退出码 3
  2. 时间格式：秒 / 毫秒 / 微秒整数、ISO 字符串（带时区 / 不带时区）；结算时间取整到分钟，和 freqtrade 从接口下载时完全一样
  3. 只留配置里的币的 USDT 永续（BTC-USDT-SWAP），币本位、交割合约、别的币都不要；没有合约列时按文件名认
  4. 列表接口的返回（OKX 没有公开写清楚格式）：几种可能的样子、错误码、旧的 priapi 列表；请求按 5 个币 / 6 个月分批；OKX 返回 code=50025（币太多）/ 50077（时间跨度太长，HTTP 400）时自动减半重试
  5. 联网模式（假的 HTTP 层，不联网）：缓存、重下时不再下载、两次请求至少隔 0.5 秒、重试、网络出错 -> 退出码 2 而且数据不动
  6. 合并：本地已有的行优先；重叠的行对不上（> 1%）-> overlap_mismatch
  7. 标记价格替身：只补第一根真实标记价格之前的时间点，从不覆盖真实的；再跑一次不会多补；--no-mark-substitute
  8. 同样的输入跑两次，数据文件一个字节都不变；两份新目录各跑一次，结果也一样
  9. 拒绝 user_data/data/okx（--datadir、配置里的 datadir、默认值、符号链接），除非 --allow-main-datadir
 10. --from-dir：zip / csv / csv.gz、一个 zip 里几个 CSV；--seed-from / seed 只复制合约K线、不覆盖、不用符号链接
 11. 配置检查（正则白名单、不是 OKX、不是 StaticPairList）和 status 子命令
 12. 端到端（--no-e2e 跳过；约 20~30 秒）：把合成数据 user_data/data/okx-synthetic 复制到临时目录，
     去掉某天以前的资金费、更晚某天以前的标记价格，把去掉的资金费写成"猜的归档格式"的 zip，用 update --from-dir 导回来：
     资金费文件和原来完全一样；不补标记价格替身时回测的 funding_fees 全是 0；补了以后不是 0，
     而且和原始数据回测的笔数相同、资金费合计只差一点点（标记价格和最新价的差别）。
     没有 okx-synthetic 时在临时目录里生成一份小的（scripts/make_synthetic_data.py，约 2 秒）。

用法：
  .venv/bin/python scripts/check_okx_funding_archive.py            # 全部
  .venv/bin/python scripts/check_okx_funding_archive.py --no-e2e   # 不跑端到端（几秒钟）
  .venv/bin/python scripts/check_okx_funding_archive.py --e2e-only
退出码：0 = 全部通过；1 = 有检查不对。
"""

from __future__ import annotations

import argparse
import contextlib
import gzip
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import zipfile
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR / "scripts"))

import numpy as np  # noqa: E402
import okx_funding_archive as ofa  # noqa: E402
import pandas as pd  # noqa: E402
from freqtrade.data.converter import ohlcv_to_dataframe  # noqa: E402
from freqtrade.data.history import get_datahandler  # noqa: E402
from freqtrade.enums import CandleType  # noqa: E402

FAILS: list[str] = []
H = 3_600_000  # 1 小时（毫秒）
DAY = 24 * H
FR, MARK, FUT = CandleType.FUNDING_RATE, CandleType.MARK, CandleType.FUTURES
HEADER = "instrument_name,contract_type,funding_rate,real_funding_rate,funding_time,timestamp"
# 这些自检里很多币只有归档、没有"接口下载的"资金费：默认会被拒绝（见 test_no_api），所以加这个参数
NA = "--allow-no-api-funding"


def check(name: str, got: Any, want: Any) -> None:
    ok = got == want
    print(f"  {'通过' if ok else '不对'}  {name}" + ("" if ok else f"：得到 {got!r}，应为 {want!r}"))
    if not ok:
        FAILS.append(name)


def check_true(name: str, cond: bool, detail: str = "") -> None:
    print(f"  {'通过' if cond else '不对'}  {name}" + ("" if cond else (f"：{detail}" if detail else "")))
    if not cond:
        FAILS.append(name)


def ms(s: str) -> int:
    return int(pd.Timestamp(s, tz="UTC").value // 1_000_000)


def run(argv: list[str], http: ofa.Http | None = None) -> tuple[int, str]:
    """进程内运行 okx_funding_archive.main，返回 (退出码, 屏幕输出)。"""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            rc = ofa.main(argv, http=http)
        except SystemExit as e:
            rc = int(e.code or 0)
    return rc, out.getvalue() + err.getvalue()


# ----------------------------------------------------------------------------
# 造数据
# ----------------------------------------------------------------------------
def make_config(path: Path, pairs: list[str], datadir: Path | str | None = None, **extra: Any) -> Path:
    cfg: dict[str, Any] = {
        "exchange": {"name": "okx", "pair_whitelist": pairs},
        "trading_mode": "futures", "margin_mode": "isolated", "stake_currency": "USDT",
        "pairlists": [{"method": "StaticPairList"}],
    }
    if datadir is not None:
        cfg["datadir"] = str(datadir)
    for k, v in extra.items():
        if k == "exchange":
            cfg["exchange"].update(v)
        else:
            cfg[k] = v
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    return path


def store(datadir: Path, pair: str, df: pd.DataFrame, ct: CandleType, tf: str = "1h") -> None:
    (datadir / "futures").mkdir(parents=True, exist_ok=True)
    get_datahandler(datadir, "feather").ohlcv_store(pair, tf, data=df, candle_type=ct)


def load(datadir: Path, pair: str, ct: CandleType, tf: str = "1h") -> pd.DataFrame:
    return get_datahandler(datadir, "feather").ohlcv_load(pair, tf, ct, fill_missing=False, warn_no_data=False)


def funding_df(start: str, n: int, step_h: int = 8, base: float = 1e-4, skip: tuple[int, ...] = ()) -> pd.DataFrame:
    t0 = ms(start)
    idx = [i for i in range(n) if i not in skip]
    return pd.DataFrame({
        "date": pd.to_datetime([t0 + i * step_h * H for i in idx], unit="ms", utc=True),
        "funding_rate": [base * (1 + (i % 7)) * (-1 if i % 5 == 0 else 1) for i in idx],
    })


def candles_df(start: str, hours: int, p0: float = 100.0, vol: float = 10.0) -> pd.DataFrame:
    t0 = ms(start)
    o = p0 + np.sin(np.arange(hours) / 5.0) * 3
    return pd.DataFrame({
        "date": pd.to_datetime([t0 + i * H for i in range(hours)], unit="ms", utc=True),
        "open": o, "high": o + 1.0, "low": o - 1.0, "close": o + 0.25, "volume": np.full(hours, vol),
    })


def csv_text(header: str, rows: list[str]) -> str:
    return header + "\n" + "\n".join(rows) + "\n"


def archive_rows(fam: str, df: pd.DataFrame, fake_offset: float = 5e-5) -> list[str]:
    out = []
    for d, r in zip(df["date"], df["funding_rate"]):
        t = int(pd.Timestamp(d).value // 1_000_000)
        out.append(f"{fam}-SWAP,SWAP,{float(r) + fake_offset!r},{float(r)!r},{t},{t + 137}")
    return out


def write_zip(path: Path, members: dict[str, str]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as z:
        for name, text in members.items():
            z.writestr(name, text)
    return path


def sha_tree(d: Path) -> dict[str, str]:
    return {str(p.relative_to(d)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(d.rglob("*.feather")) if p.is_file()}


def manifest(dd: Path) -> dict:
    return json.loads((dd / "okx_funding_archive" / "manifest.json").read_text(encoding="utf-8"))


# ----------------------------------------------------------------------------
# 1. 列名
# ----------------------------------------------------------------------------
def test_columns() -> None:
    print("1. CSV 列名：各种写法都认得出；实际结算的费率优先；只有 funding_rate 时记下来；认不出时报格式错误")

    def cols(h: list[str]) -> tuple:
        c = ofa.detect_columns(h)
        return c.inst_name, c.time_name, c.rate_name, c.realized, c.time_fallback

    check("okx-dump 的写法（instrument_name、real_funding_rate、funding_time）",
          cols(HEADER.split(",")), ("instrument_name", "funding_time", "real_funding_rate", True, False))
    check("接口的驼峰写法（instId、fundingTime、realizedRate）",
          cols(["instId", "fundingRate", "realizedRate", "fundingTime"]), ("instId", "fundingTime", "realizedRate", True, False))
    check("大小写、空格、BOM 都不影响（\\ufeff INST_ID、Funding_Time、Real_Funding_Rate）",
          cols(["﻿ INST_ID ", "Funding_Time", "Real_Funding_Rate"])[1:], ("Funding_Time", "Real_Funding_Rate", True, False))
    check("symbol、funding_ts、realized_rate", cols(["symbol", "funding_ts", "realized_rate"]),
          ("symbol", "funding_ts", "realized_rate", True, False))
    check("只有 funding_rate：用它，但标成不是实际结算的费率", cols(["instrument_id", "funding_rate", "funding_time"])[2:4],
          ("funding_rate", False))
    check("没有 funding_time、只有 timestamp：用 timestamp，标成退而求其次",
          cols(["instrument_name", "real_funding_rate", "timestamp"])[1:], ("timestamp", "real_funding_rate", True, True))
    try:
        ofa.detect_columns(["foo", "bar", "baz"])
        got = "没报错"
    except ofa.FormatError:
        got = "FormatError"
    check("认不出的表头 -> FormatError", got, "FormatError")


# ----------------------------------------------------------------------------
# 2. 时间格式
# ----------------------------------------------------------------------------
def test_times() -> None:
    print("2. 时间格式：秒 / 毫秒 / 微秒、ISO（带时区 / 不带时区）；结算时间按 freqtrade 的口径取整到分钟")
    t = ms("2023-01-01")
    check("毫秒整数", ofa.parse_time_ms(str(t)), (t, False))
    check("秒整数", ofa.parse_time_ms(str(t // 1000)), (t, False))
    check("微秒整数", ofa.parse_time_ms(str(t * 1000)), (t, False))
    check("ISO 带 Z", ofa.parse_time_ms("2023-01-01T00:00:00.000Z"), (t, False))
    check("ISO 带 +08:00（北京时间 08:00 = UTC 00:00）", ofa.parse_time_ms("2023-01-01T08:00:00+08:00"), (t, False))
    check("ISO 不带时区：按 UTC，标出来", ofa.parse_time_ms("2023-01-01 00:00:00"), (t, True))
    check("2023/01/01 00:00:00", ofa.parse_time_ms("2023/01/01 00:00:00")[0], t)
    # freqtrade 从接口下载时：ohlcv_to_dataframe([[fundingTime, rate]], "1h", candle_type=funding_rate) -> 取整到分钟
    raw = t + 8 * H + 12_345  # 不是整分钟
    ft = ohlcv_to_dataframe([[raw, 0.0001]], "1h", "BTC/USDT:USDT", fill_missing=False, drop_incomplete=False,
                            candle_type=FR)
    tab = ofa.parse_csv_text(csv_text("inst_id,funding_time,real_funding_rate", [f"BTC-USDT-SWAP,{raw},0.0001"]),
                             "x.csv", {"BTC-USDT"}, None, None, floor_ms=60_000)
    check("结算时间不是整分钟时取整的结果和 freqtrade 下载时一样",
          tab.rows[0][1], int(pd.Timestamp(ft["date"].iloc[0]).value // 1_000_000))
    ctx_floor = ofa.resolve_context([str(make_config(TMP / "cfg_floor.json", ["BTC/USDT:USDT"], TMP / "dd_floor"))],
                                    None, False).floor_ms
    check("OKX 资金费周期 1h -> 取整到 60 秒（freqtrade.exchange.timeframe_to_floor_freq）", ctx_floor, 60_000)


# ----------------------------------------------------------------------------
# 3. 解析 CSV：只留要的币
# ----------------------------------------------------------------------------
def test_parse_filter() -> None:
    print("3. 只留配置里的币的 USDT 永续；费率取实际结算的那一列；没有合约列时按文件名认")
    t = ms("2023-01-01")
    rows = [
        f"BTC-USDT-SWAP,SWAP,0.0002,0.0001,{t},{t + 5}",
        f"btc-usdt-swap,SWAP,0.0003,-0.0001,{t + 8 * H},{t + 8 * H}",
        f"BTC-USD-SWAP,SWAP,0.0009,0.0009,{t},{t}",           # 币本位
        f"BTC-USDT-230331,FUTURES,0.0009,0.0009,{t},{t}",      # 交割合约
        f"ETH-USDT-SWAP,SWAP,0.0005,0.0004,{t},{t}",           # 别的币
        f"BTC-USDT-SWAP,SWAP,0.0002,0.0001,{t - 8 * H},{t}",   # 区间以前
        f"BTC-USDT-SWAP,SWAP,0.0002,,{t + 16 * H},{t}",        # 费率是空的
    ]
    tab = ofa.parse_csv_text(csv_text(HEADER, rows), "a.csv", {"BTC-USDT"}, t, t + DAY)
    check("只剩 BTC-USDT-SWAP 在区间里、读得出的两行，费率 = real_funding_rate",
          tab.rows, [("BTC-USDT", t, 0.0001), ("BTC-USDT", t + 8 * H, -0.0001)])
    check("读不出的行记下来（1 行）", tab.n_bad, 1)
    tab2 = ofa.parse_csv_text(csv_text(HEADER, rows), "a.csv", {"BTC-USDT", "ETH-USDT"}, t, t + DAY)
    check("要 BTC 和 ETH 时 ETH 也留下", sorted({f for f, _, _ in tab2.rows}), ["BTC-USDT", "ETH-USDT"])
    tab3 = ofa.parse_csv_text(csv_text("funding_time,funding_rate,real_funding_rate", [f"{t},0.0002,0.0001"]),
                              "BTC-USDT-SWAP-fundingrates-2023-01.csv", {"BTC-USDT"}, None, None)
    check("没有合约列、文件名写着 BTC-USDT-SWAP：按文件名认", (tab3.inferred_family, tab3.rows), ("BTC-USDT", [("BTC-USDT", t, 0.0001)]))
    try:
        ofa.parse_csv_text(csv_text("funding_time,real_funding_rate", [f"{t},0.0001"]), "allswaprate-2023-01.csv",
                           {"BTC-USDT"}, None, None)
        got = "没报错"
    except ofa.FormatError:
        got = "FormatError"
    check("没有合约列、文件名里也没写是哪个币 -> FormatError", got, "FormatError")
    tab4 = ofa.parse_csv_text(csv_text("instId;fundingTime;realizedRate", ["BTC-USDT-SWAP;2023-01-01T00:00:00Z;0.0001"]),
                              "b.csv", {"BTC-USDT"}, None, None)
    check("分号分隔、ISO 时间", tab4.rows, [("BTC-USDT", t, 0.0001)])


# ----------------------------------------------------------------------------
# 4. 列表接口的返回
# ----------------------------------------------------------------------------
def test_listing() -> None:
    print("4. 列表接口的返回：几种可能的样子都找得到文件；错误码 / 不是 JSON -> 格式错误；请求分批")
    base = "https://static.okx.com/cdn/okex/traderecords/swaprate/monthly/202301/"
    a = {"code": "0", "msg": "", "data": [{"dateAggrType": "monthly", "details": [{"dateRangeStart": "1672502400000",
         "groupDetails": [{"filename": "BTC-USDT-SWAP-fundingrates-2023-01.zip", "sizeMB": "0.01",
                           "url": base + "BTC-USDT-SWAP-fundingrates-2023-01.zip"}]}]}]}
    e = ofa.parse_official_listing(json.dumps(a).encode())
    check("嵌套很深的 data/details/groupDetails", [(x.file_name, x.month, x.size) for x in e],
          [("BTC-USDT-SWAP-fundingrates-2023-01.zip", "202301", None)])
    b = {"code": "0", "data": [{"fileName": "f1.zip", "fileSize": "1234", "dateTs": "1675180800000",
                                "url": "https://static.okx.com/x/f1.zip?sign=abc"},
                               {"fileName": "f1.zip", "fileSize": "1234", "url": "https://static.okx.com/x/f1.zip?sign=abc"}]}
    e = ofa.parse_official_listing(json.dumps(b).encode())
    check("平铺的列表、带 ?参数 的地址、大小是字符串、月份从 dateTs（北京时间）、重复的只算一个",
          [(x.file_name, x.size, x.month) for x in e], [("f1.zip", 1234, "202302")])
    c = {"code": "0", "data": {"list": [["https://static.okx.com/a/BTC-USDT-SWAP-2023-03.csv", "https://x.okx.com/readme.txt"]]}}
    e = ofa.parse_official_listing(json.dumps(c).encode())
    check("只有一串地址（不在字典里）；不是 zip/csv 的不要", [(x.file_name, x.month) for x in e], [("BTC-USDT-SWAP-2023-03.csv", "202303")])
    check("data 是空的 -> 空列表（不算格式错误）", ofa.parse_official_listing(b'{"code":"0","msg":"","data":[]}'), [])
    for name, raw in (("错误码 51000", b'{"code":"51000","msg":"Parameter instFamilyList error","data":[]}'),
                      ("不是 JSON", b"<html>blocked</html>")):
        try:
            ofa.parse_official_listing(raw)
            got = "没报错"
        except ofa.FormatError:
            got = "FormatError"
        check(f"{name} -> FormatError", got, "FormatError")
    p = {"code": 0, "data": {"recordFileList": [{"fileName": "allswaprate-swaprate-2023-01.zip", "fileSize": 4321}]}}
    e = ofa.parse_priapi_listing(json.dumps(p).encode(), "202301")
    check("旧的 priapi 列表：只给文件名，地址自己拼",
          [(x.url, x.size, x.month) for x in e], [(base + "allswaprate-swaprate-2023-01.zip", 4321, "202301")])
    fams = [f"C{i}-USDT" for i in range(60)]
    months = ofa._months(datetime(2022, 3, 1, tzinfo=UTC), datetime(2026, 11, 8, tzinfo=UTC))
    reqs = ofa.official_requests(fams, months, ms("2026-11-08"))
    nf = [len(r["instFamilyList"].split(",")) for r in reqs]
    spans = [(int(r["end"]) - int(r["begin"])) for r in reqs]
    check("60 个币 × 57 个月 -> 120 次请求，每次最多 5 个币（OKX 实测上限，文档写 10）", (len(reqs), max(nf), len(months)), (120, 5, 57))
    cn = timedelta(hours=8)  # OKX 归档按北京时间分月：请求的起止也按北京时间的月份写
    check("每次最多 6 个月（文档写 20 个月，实测 19 个月返回 50077；按北京时间的月份数算）",
          max(len(ofa._months(datetime.fromtimestamp(int(r["begin"]) / 1000, UTC) + cn,
                              datetime.fromtimestamp(int(r["end"]) / 1000, UTC) + cn)) for r in reqs), 6)
    check("参数：module=3、instType=SWAP、dateAggrType=monthly", {(r["module"], r["instType"], r["dateAggrType"]) for r in reqs},
          {("3", "SWAP", "monthly")})
    check("第一次从北京时间 2022-03-01 00:00（= UTC 2022-02-28 16:00）开始，最后一次到现在为止",
          (int(reqs[0]["begin"]), max(int(r["end"]) for r in reqs)), (ms("2022-02-28 16:00"), ms("2026-11-08")))
    b2 = sorted({int(r["begin"]) for r in reqs})
    check("后面几次也从北京时间的月初开始（2022-09、2023-03 …… 2026-09 = UTC 前一天 16:00）", b2[1:],
          [ms(f"{d} 16:00") for d in ("2022-08-31", "2023-02-28", "2023-08-31", "2024-02-29", "2024-08-31",
                                      "2025-02-28", "2025-08-31", "2026-02-28", "2026-08-31")])
    e1 = sorted({int(r["end"]) for r in reqs})[0]
    check("每次结束在北京时间的月底（UTC 那天 15:59:59.999），和下一次接上", (e1, e1 + 1 == b2[1]), (ms("2022-08-31 16:00") - 1, True))
    check_true("开始 <= 结束", all(s > 0 for s in spans))


# ----------------------------------------------------------------------------
# 5. 联网模式（假的 HTTP）
# ----------------------------------------------------------------------------
class FakeHttp(ofa.Http):
    def __init__(self, route: Callable[[str], tuple[int, bytes]]) -> None:
        self.t = 0.0
        self.sleeps: list[float] = []
        self.starts: list[float] = []
        super().__init__(sleep=self._sleep_fake, clock=lambda: self.t)
        self.route = route

    def _sleep_fake(self, s: float) -> None:
        self.sleeps.append(s)
        self.t += s

    def _raw_get(self, url: str) -> tuple[int, bytes]:
        self.starts.append(self.t)
        self.t += 0.05
        return self.route(url)


def _net_fixture(root: Path) -> tuple[Path, Path, dict[str, bytes], pd.DataFrame, pd.DataFrame]:
    dd = root / "dd"
    cfg = make_config(root / "cfg.json", ["BTC/USDT:USDT", "ETH/USDT:USDT"], dd)
    btc = funding_df("2023-01-01", 3 * 59)  # 2023-01 ~ 2023-02，8 小时一次
    eth = funding_df("2023-01-01", 3 * 59, base=2e-4)
    files: dict[str, bytes] = {}
    for fam, df in (("BTC-USDT", btc), ("ETH-USDT", eth), ("XRP-USDT", eth)):
        for mon in ("2023-01", "2023-02"):
            part = df[df["date"].dt.strftime("%Y-%m") == mon]
            name = f"{fam}-SWAP-fundingrates-{mon}.zip"
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w") as z:
                z.writestr(name.replace(".zip", ".csv"), csv_text(HEADER, archive_rows(fam, part)))
            files[f"https://static.okx.com/cdn/okex/traderecords/fundingrate/monthly/{mon.replace('-', '')}/{name}"] = buf.getvalue()
    return dd, cfg, files, btc, eth


def _listing_route(files: dict[str, bytes], extra: Callable[[str], tuple[int, bytes] | None] | None = None):
    def route(url: str) -> tuple[int, bytes]:
        if extra is not None:
            r = extra(url)
            if r is not None:
                return r
        if url.startswith(ofa.OFFICIAL_URL):
            q = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(url).query))
            fams = q["instFamilyList"].split(",")
            groups = [{"filename": u.rsplit("/", 1)[-1], "url": u, "fileSize": str(len(b))}
                      for u, b in files.items() if any(f"/{f}-SWAP" in u for f in fams + ["XRP-USDT"])]
            return 200, json.dumps({"code": "0", "msg": "", "data": [{"details": [{"groupDetails": groups}]}]}).encode()
        if url in files:
            return 200, files[url]
        return 404, b"not found"
    return route


def test_network() -> None:
    print("5. 联网模式（假的 HTTP 层，不联网）：缓存、两次请求至少隔 0.5 秒、重试、网络出错 -> 退出码 2 而且数据不动")
    root = TMP / "net"
    dd, cfg, files, btc, _eth = _net_fixture(root)
    store(dd, "BTC/USDT:USDT", btc.iloc[-20:].reset_index(drop=True), FR)  # 接口下的最近一段
    http = FakeHttp(_listing_route(files))
    rc, out = run(["update", NA, "--config", str(cfg), "--start", "20230101", "--end", "20230228", "--no-mark-substitute"], http)
    check("退出码 0", rc, 0)
    if rc != 0:
        print(out)
        return
    check("1 次列表请求 + 4 个文件（XRP 的文件名写明不是要的币，不下载）", http.requests, 5)
    gaps = np.diff(http.starts)
    check_true("每两次请求之间至少隔 0.5 秒", bool(len(gaps)) and float(gaps.min()) >= 0.5 - 1e-9, f"最小间隔 {gaps.min() if len(gaps) else None}")
    raw = sorted(str(p.relative_to(dd)) for p in (dd / "okx_funding_archive" / "raw").rglob("*.zip"))
    check("缓存在 okx_funding_archive/raw/<YYYYMM>/<文件名>", raw[0], "okx_funding_archive/raw/202301/BTC-USDT-SWAP-fundingrates-2023-01.zip")
    got = load(dd, "BTC/USDT:USDT", FR)
    check("BTC 资金费 = 归档里的全部（和原来一样）", (len(got), np.array_equal(got["funding_rate"].to_numpy(), btc["funding_rate"].to_numpy())),
          (len(btc), True))
    m = manifest(dd)
    check("清单：来源 official、5 次请求、4 个文件、跳过 XRP 2 个", (m["lister"], m["last_run"]["http_requests"], len(m["files"]),
                                                       m["files_skipped_other_family"]), ("official", 5, 4, 2))
    check("清单：文件记录有地址、大小、sha256、月份、行数",
          sorted(k for k in m["files"][0] if k in ("url", "local_path", "size", "sha256", "month", "rows", "rows_kept")),
          ["local_path", "month", "rows", "rows_kept", "sha256", "size", "url"])
    before = sha_tree(dd)
    http2 = FakeHttp(_listing_route(files))
    rc, _ = run(["update", NA, "--config", str(cfg), "--start", "20230101", "--end", "20230228", "--no-mark-substitute"], http2)
    check("再跑一次：只发列表请求，文件用缓存（大小一样）", (rc, http2.requests), (0, 1))
    check("再跑一次：数据文件一个字节都不变", sha_tree(dd), before)

    # 网络出错：ETH 2 月那个文件一直连不上
    root2 = TMP / "net_fail"
    dd2, cfg2, files2, btc2, _ = _net_fixture(root2)
    store(dd2, "BTC/USDT:USDT", btc2.iloc[-20:].reset_index(drop=True), FR)
    bad = next(u for u in files2 if "ETH-USDT-SWAP-fundingrates-2023-02" in u)

    def fail(url: str) -> tuple[int, bytes] | None:
        if url == bad:
            raise urllib.error.URLError("connection reset")
        return None

    h3 = FakeHttp(_listing_route(files2, fail))
    sha_before = sha_tree(dd2)
    rc, out = run(["update", NA, "--config", str(cfg2), "--start", "20230101", "--end", "20230228"], h3)
    check("网络出错 -> 退出码 2", rc, 2)
    check("出错的地址重试 4 次（一共 5 次）", h3.urls.count(bad), 5)
    check("重试前等 1、2、4、8 秒", [s for s in h3.sleeps if s >= 1.0], [1.0, 2.0, 4.0, 8.0])
    check("数据文件没动、没有写清单", (sha_tree(dd2), (dd2 / "okx_funding_archive" / "manifest.json").exists()), (sha_before, False))
    cached = sorted(p.name for p in (dd2 / "okx_funding_archive" / "raw").rglob("*.zip"))
    check_true("已经下好的文件留在缓存里（重跑接着下）", "BTC-USDT-SWAP-fundingrates-2023-01.zip" in cached and len(cached) == 3, str(cached))
    h4 = FakeHttp(_listing_route(files2))
    rc, _ = run(["update", NA, "--config", str(cfg2), "--start", "20230101", "--end", "20230228"], h4)
    check("网络好了重跑：只下剩下的 1 个文件", (rc, h4.requests), (0, 2))

    def code_err(url: str) -> tuple[int, bytes] | None:
        return (200, b'{"code":"51000","msg":"Parameter error","data":[]}') if url.startswith(ofa.OFFICIAL_URL) else None

    for name, extra, want in (
        ("列表返回错误码 -> 退出码 3", code_err, 3),
        ("列表返回空的 -> 退出码 3", lambda u: (200, b'{"code":"0","data":[]}') if u.startswith(ofa.OFFICIAL_URL) else None, 3),
        ("下载返回 404 -> 退出码 3", lambda u: (404, b"no") if u.startswith("https://static") else None, 3),
        ("下载返回 403（地区限制 / 代理）-> 退出码 2", lambda u: (403, b"blocked") if u.startswith("https://static") else None, 2),
        ("列表一直 HTTP 500 -> 退出码 2", lambda u: (500, b"err") if u.startswith(ofa.OFFICIAL_URL) else None, 2),
    ):
        r3 = TMP / f"net_{abs(hash(name)) % 10_000}"
        _d3, c3, f3, _b, _e = _net_fixture(r3)
        rc, _ = run(["update", NA, "--config", str(c3), "--start", "20230101", "--end", "20230228"], FakeHttp(_listing_route(f3, extra)))
        check(name, rc, want)
    # probe：1 次列表请求 + 下载 1 个文件，打印原始返回、表头、认出来的列
    h5 = FakeHttp(_listing_route(files))
    rc, out = run(["probe", "--family", "ETH-USDT", "--month", "2023-02", "--save", str(TMP / "probe")], h5)
    check_true("probe：退出码 0、2 次请求、打印原始返回 / 表头 / 认出来的列（real_funding_rate）",
               rc == 0 and h5.requests == 2 and '"groupDetails"' in out and HEADER in out and "资金费率：real_funding_rate" in out
               and "ETH-USDT-SWAP-fundingrates-2023-02.zip" in out, out[-800:])
    rc, out = run(["probe", "--save", str(TMP / "probe")], FakeHttp(lambda u: (200, b'{"code":"0","data":[]}')))
    check("probe：列表是空的 -> 退出码 3", rc, 3)

    def weird(url: str) -> tuple[int, bytes] | None:
        if url.startswith("https://static"):
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w") as z:
                z.writestr("x.csv", "a,b,c\n1,2,3\n")
            return 200, buf.getvalue()
        return None

    rc, out = run(["probe", "--family", "BTC-USDT", "--month", "2023-01", "--save", str(TMP / "probe")],
                  FakeHttp(_listing_route(files, weird)))
    check("probe：文件表头认不出 -> 退出码 3，打印表头", (rc, "a,b,c" in out), (3, True))
    rc, _ = run(["probe", "--month", "2023-13"])
    check("probe：--month 不对 -> 退出码 1", rc, 1)
    rp = TMP / "net_priapi"
    _dp, cp, _fp, _b, _e = _net_fixture(rp)
    rc, out = run(["update", "--config", str(cp), "--start", "20230101", "--end", "20230131", "--lister", "priapi"],
                  FakeHttp(lambda u: (404, b"<html>404</html>")))
    check("priapi 列表 404 -> 退出码 3（会打印看到的内容）", (rc, "404" in out), (3, True))


# ----------------------------------------------------------------------------
# 6. 合并：已有的行优先 + 重叠对不上
# ----------------------------------------------------------------------------
def test_family_limit() -> None:
    print("5b. OKX 嫌一次的币太多（code=50025）：自动减半重试，后面的批次也按新的上限分；只剩 1 个币还报错 -> 退出码 3")
    root = TMP / "famlimit"
    dd, cfg, files, btc, eth = _net_fixture(root)
    store(dd, "BTC/USDT:USDT", btc.iloc[-20:].reset_index(drop=True), FR)
    store(dd, "ETH/USDT:USDT", eth.iloc[-20:].reset_index(drop=True), FR)
    seen: list[int] = []
    err = json.dumps({"code": "50025", "msg": "Number of instFamilyList values exceeds the maximum limit 1", "data": []}).encode()

    def limit1(url: str) -> tuple[int, bytes] | None:
        if url.startswith(ofa.OFFICIAL_URL):
            q = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(url).query))
            n = len(q["instFamilyList"].split(","))
            seen.append(n)
            if n > 1:
                return 200, err
        return None

    http = FakeHttp(_listing_route(files, limit1))
    rc, out = run(["update", NA, "--config", str(cfg), "--start", "20230101", "--end", "20230228", "--no-mark-substitute"], http)
    check("退出码 0（2 个币一批被拒，减成 1 个币一批）", rc, 0)
    check("列表请求：先 2 个币一次（被拒），再 1 个币各一次", seen, [2, 1, 1])
    check_true("屏幕上说明了为什么重试", "code=50025" in out and "每次最多 1 个币" in out, out[-600:])
    got = load(dd, "ETH/USDT:USDT", FR)
    check("ETH 资金费 = 归档里的全部", (len(got), np.array_equal(got["funding_rate"].to_numpy(), eth["funding_rate"].to_numpy())),
          (len(eth), True))

    # 只剩 1 个币还报 50025：不再重试，退出码 3，数据不动
    root2 = TMP / "famlimit_one"
    dd2, cfg2, files2, btc2, eth2 = _net_fixture(root2)
    store(dd2, "BTC/USDT:USDT", btc2.iloc[-20:].reset_index(drop=True), FR)
    store(dd2, "ETH/USDT:USDT", eth2.iloc[-20:].reset_index(drop=True), FR)
    before = sha_tree(dd2)
    http2 = FakeHttp(_listing_route(files2, lambda u: (200, err) if u.startswith(ofa.OFFICIAL_URL) else None))
    rc, out = run(["update", NA, "--config", str(cfg2), "--start", "20230101", "--end", "20230228", "--no-mark-substitute"], http2)
    check("1 个币也被拒 -> 退出码 3", rc, 3)
    check("数据目录一个字节都不变", sha_tree(dd2), before)

    # 别的错误码不重试
    other = json.dumps({"code": "51000", "msg": "Parameter instType error", "data": []}).encode()
    h3 = FakeHttp(_listing_route(files2, lambda u: (200, other) if u.startswith(ofa.OFFICIAL_URL) else None))
    rc, _ = run(["update", NA, "--config", str(cfg2), "--start", "20230101", "--end", "20230228", "--no-mark-substitute"], h3)
    check("别的错误码（51000 参数错误）不重试：1 次请求、退出码 3", (rc, h3.requests), (3, 1))


def test_time_limit() -> None:
    print("5c. OKX 嫌起止时间隔得太长（HTTP 400 + code=50077）：自动把月数减半重试；币太多的 50025 用 HTTP 400 返回也认得")
    root = TMP / "timelimit"
    dd, cfg, files, btc, eth = _net_fixture(root)
    store(dd, "BTC/USDT:USDT", btc.iloc[-20:].reset_index(drop=True), FR)
    store(dd, "ETH/USDT:USDT", eth.iloc[-20:].reset_index(drop=True), FR)
    seen: list[tuple[int, int]] = []
    t_err = json.dumps({"code": "50077", "msg": "The time interval between the start date and end date cannot exceed 1 months",
                        "data": []}).encode()
    f_err = json.dumps({"code": "50025", "msg": "Number of instFamilyList values exceeds the maximum limit 1", "data": []}).encode()
    cn = timedelta(hours=8)

    def limit(url: str) -> tuple[int, bytes] | None:
        if url.startswith(ofa.OFFICIAL_URL):
            q = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(url).query))
            n = len(q["instFamilyList"].split(","))
            nm = len(ofa._months(datetime.fromtimestamp(int(q["begin"]) / 1000, UTC) + cn,
                                 datetime.fromtimestamp(int(q["end"]) / 1000, UTC) + cn))
            seen.append((n, nm))
            if nm > 1:
                return 400, t_err
            if n > 1:
                return 400, f_err
        return None

    http = FakeHttp(_listing_route(files, limit))
    rc, out = run(["update", NA, "--config", str(cfg), "--start", "20230101", "--end", "20230228", "--no-mark-substitute"], http)
    check("退出码 0（2 个月一批被拒，减成 1 个月；2 个币一批被拒，减成 1 个币）", rc, 0)
    check("列表请求：(2 币, 2 月) 被拒 → (2 币, 1 月) 被拒 → 1 币 1 月 × 4", seen, [(2, 2), (2, 1), (1, 1), (1, 1), (1, 1), (1, 1)])
    check_true("屏幕上说明了为什么重试", "code=50077" in out and "每次最多 1 个月" in out and "code=50025" in out, out[-800:])
    got = load(dd, "ETH/USDT:USDT", FR)
    check("ETH 资金费 = 归档里的全部", (len(got), np.array_equal(got["funding_rate"].to_numpy(), eth["funding_rate"].to_numpy())),
          (len(eth), True))

    # 1 个月也被拒 -> 退出码 3、数据不动；HTTP 400 不是 OKX 的 JSON -> 退出码 3
    root2 = TMP / "timelimit_one"
    dd2, cfg2, files2, btc2, eth2 = _net_fixture(root2)
    store(dd2, "BTC/USDT:USDT", btc2.iloc[-20:].reset_index(drop=True), FR)
    store(dd2, "ETH/USDT:USDT", eth2.iloc[-20:].reset_index(drop=True), FR)
    before = sha_tree(dd2)
    h2 = FakeHttp(_listing_route(files2, lambda u: (400, t_err) if u.startswith(ofa.OFFICIAL_URL) else None))
    rc, _ = run(["update", NA, "--config", str(cfg2), "--start", "20230101", "--end", "20230131", "--no-mark-substitute"], h2)
    check("1 个月也被拒 -> 退出码 3，只请求 1 次", (rc, h2.requests), (3, 1))
    check("数据目录一个字节都不变", sha_tree(dd2), before)
    h3 = FakeHttp(_listing_route(files2, lambda u: (400, b"<html>bad request</html>") if u.startswith(ofa.OFFICIAL_URL) else None))
    rc, _ = run(["update", NA, "--config", str(cfg2), "--start", "20230101", "--end", "20230228", "--no-mark-substitute"], h3)
    check("HTTP 400 但不是 OKX 的 JSON -> 退出码 3，只请求 1 次", (rc, h3.requests), (3, 1))

    # 限速（code=50011，HTTP 200 带回来）：等一会儿再试同一个请求，最多 4 次；一直限速 -> 退出码 3
    rl = json.dumps({"code": "50011", "msg": "Too Many Requests", "data": []}).encode()
    left = [2]

    def ratelimited(url: str) -> tuple[int, bytes] | None:
        if url.startswith(ofa.OFFICIAL_URL) and left[0] > 0:
            left[0] -= 1
            return 200, rl
        return None

    root3 = TMP / "ratelimit"
    dd3, cfg3, files3, btc3, eth3 = _net_fixture(root3)
    store(dd3, "BTC/USDT:USDT", btc3.iloc[-20:].reset_index(drop=True), FR)
    store(dd3, "ETH/USDT:USDT", eth3.iloc[-20:].reset_index(drop=True), FR)
    h4 = FakeHttp(_listing_route(files3, ratelimited))
    rc, out = run(["update", NA, "--config", str(cfg3), "--start", "20230101", "--end", "20230228", "--no-mark-substitute"], h4)
    check("限速 2 次以后成功：退出码 0，等了 2、4 秒", (rc, [x for x in h4.sleeps if x >= 2]), (0, [2.0, 4.0]))
    check_true("屏幕上说明了限速重试", "code=50011" in out, out[-400:])
    h5 = FakeHttp(_listing_route(files3, lambda u: (200, rl) if u.startswith(ofa.OFFICIAL_URL) else None))
    rc, _ = run(["update", NA, "--config", str(cfg3), "--start", "20230101", "--end", "20230228", "--no-mark-substitute"], h5)
    check("一直限速：重试 4 次后退出码 3（列表请求 5 次）", (rc, h5.requests), (3, 5))


def test_merge() -> None:
    print("6. 合并：本地已有的行（接口下载的）优先，归档只补没有的；重叠的行对不上超过 1% -> overlap_mismatch")
    for case, bump, want_mm in (("一样", 0.0, False), ("对不上", 1e-6, True)):
        root = TMP / f"merge_{case}"
        dd = root / "dd"
        cfg = make_config(root / "cfg.json", ["BTC/USDT:USDT"], dd)
        full = funding_df("2023-01-01", 90)  # 30 天
        existing = full.iloc[60:].reset_index(drop=True).copy()
        store(dd, "BTC/USDT:USDT", existing, FR)
        arch = full.copy()
        if bump:
            arch.loc[arch.index[-3:], "funding_rate"] += bump  # 重叠 30 行里 3 行对不上 = 10%
        src = root / "src"
        write_zip(src / "allswaprate-swaprate-2023-01.zip", {"a.csv": csv_text(HEADER, archive_rows("BTC-USDT", arch))})
        rc, out = run(["update", "--config", str(cfg), "--from-dir", str(src), "--start", "20230101", "--end", "20230131"])
        check(f"[{case}] 退出码 0", rc, 0)
        got = load(dd, "BTC/USDT:USDT", FR)
        check(f"[{case}] 合并后 90 行、本地已有的 30 行一点没变",
              (len(got), np.array_equal(got["funding_rate"].to_numpy()[60:], existing["funding_rate"].to_numpy())), (90, True))
        s = manifest(dd)["pairs"]["BTC/USDT:USDT"]
        check(f"[{case}] 新加 60 行、重叠 30 行、对不上 {3 if bump else 0} 行",
              (s["archive_rows_added_this_run"], s["overlap_rows"], s["overlap_diff_rows"]), (60, 30, 3 if bump else 0))
        check(f"[{case}] overlap_mismatch = {want_mm}（全局和这个币）", (manifest(dd)["overlap_mismatch"], s["overlap_mismatch"]),
              (want_mm, want_mm))
        if bump:
            check_true("[对不上] 屏幕上大声警告（!! ... 可能不是实际结算的费率）", "!!" in out and "不是实际结算的费率" in out)
        check(f"[{case}] 来自归档的行记成一段（开始、结束、条数）", s["archive_added_ranges"],
              [[ofa._iso(ms("2023-01-01")), ofa._iso(ms("2023-01-01") + 59 * 8 * H), 60]])
        rc, _ = run(["update", "--config", str(cfg), "--from-dir", str(src), "--start", "20230101", "--end", "20230131"])
        s2 = manifest(dd)["pairs"]["BTC/USDT:USDT"]
        check(f"[{case}] 再跑一次：重叠核对还是和接口那 30 行比（不和自己导入的比）",
              (s2["overlap_rows"], s2["overlap_diff_rows"], s2["archive_rows_added"], s2["archive_rows_added_this_run"]),
              (30, 3 if bump else 0, 60, 0))
        if bump:
            continue
        # freqtrade 之后增量下载：后面多了 6 条接口数据 -> 清单里"来自归档"的那一段照样认得出
        later = funding_df("2023-01-31", 6)
        store(dd, "BTC/USDT:USDT", pd.concat([load(dd, "BTC/USDT:USDT", FR)[["date", "funding_rate"]], later], ignore_index=True), FR)
        rc, _ = run(["update", "--config", str(cfg), "--from-dir", str(src), "--start", "20230101", "--end", "20230131"])
        s3 = manifest(dd)["pairs"]["BTC/USDT:USDT"]
        check("[一样] 之后 freqtrade 又往后追加了接口数据：来自归档的还是那 60 条，重叠还是 30 条",
              (rc, s3["funding_rows"], s3["archive_rows_added"], s3["overlap_rows"]), (0, 96, 60, 30))
        # 资金费文件被删掉、重新从接口下（只剩最近 20 条）-> 旧的"来自归档"记录作废，重新导入
        store(dd, "BTC/USDT:USDT", full.iloc[70:].reset_index(drop=True), FR)
        rc, _ = run(["update", "--config", str(cfg), "--from-dir", str(src), "--start", "20230101", "--end", "20230131"])
        s4 = manifest(dd)["pairs"]["BTC/USDT:USDT"]
        check("[一样] 文件被删掉重下（只有最近 20 条）：重新补 70 条，重叠 20 条，来自归档 70 条",
              (rc, s4["archive_rows_added_this_run"], s4["overlap_rows"], s4["archive_rows_added"]), (0, 70, 20, 70))
    # 只有 funding_rate 列：警告进清单
    root = TMP / "merge_fallback"
    dd = root / "dd"
    cfg = make_config(root / "cfg.json", ["BTC/USDT:USDT"], dd)
    full = funding_df("2023-01-01", 30)
    src = root / "src"
    rows = [f"BTC-USDT-SWAP,{float(r)!r},{int(pd.Timestamp(d).value // 1_000_000)}" for d, r in zip(full["date"], full["funding_rate"])]
    write_zip(src / "x-2023-01.zip", {"x.csv": csv_text("instrument_name,funding_rate,funding_time", rows)})
    rc, out = run(["update", NA, "--config", str(cfg), "--from-dir", str(src), "--start", "20230101", "--end", "20230131"])
    m = manifest(dd)
    check("只有 funding_rate：照样导入，清单 rate_column = funding_rate、rate_is_realized = false",
          (rc, m["rate_column"], m["rate_is_realized"]), (0, "funding_rate", False))
    check_true("只有 funding_rate：警告进清单、屏幕上有 !!", any("实际结算的费率" in w for w in m["warnings"]) and "!!" in out)
    check_true("没有和接口数据重叠：清单里说没法核对", (not m["overlap_checked"]) and any("没法核对" in w for w in m["warnings"]))


# ----------------------------------------------------------------------------
# 7. 标记价格替身
# ----------------------------------------------------------------------------
def test_mark() -> None:
    print("7. 标记价格替身：只补第一根真实标记价格之前的时间点（用 1h 合约K线、成交量 0），从不覆盖真实的")
    root = TMP / "mark"
    dd = root / "dd"
    pairs = ["BTC/USDT:USDT", "ETH/USDT:USDT", "OLD/USDT:USDT", "NEWX/USDT:USDT"]
    cfg = make_config(root / "cfg.json", pairs, dd)
    fut = candles_df("2023-01-01", 40 * 24)
    real = candles_df("2023-01-21", 20 * 24, p0=100.3, vol=0.0)
    real = real.drop(index=[30, 31]).reset_index(drop=True)  # 真实标记价格中间缺 2 根
    store(dd, "BTC/USDT:USDT", fut, FUT)
    store(dd, "BTC/USDT:USDT", real, MARK)
    store(dd, "ETH/USDT:USDT", fut, FUT)
    store(dd, "ETH/USDT:USDT", real, MARK)
    store(dd, "OLD/USDT:USDT", candles_df("2022-03-01", 30 * 24), FUT)  # 2022-03 就停了（下架）、没有真实标记价格
    store(dd, "NEWX/USDT:USDT", fut, FUT)  # K线到最近、还没下标记价格
    src = root / "src"
    write_zip(src / "all-2023-01.zip", {"a.csv": csv_text(HEADER, archive_rows("BTC-USDT", funding_df("2023-01-01", 60)))})
    rc, _ = run(["update", NA, "--config", str(cfg), "--from-dir", str(src), "--start", "20230105", "--end", "20230209"])
    check("退出码 0", rc, 0)
    got = load(dd, "BTC/USDT:USDT", MARK)
    first_real = ms("2023-01-21")
    gd = got["date"].astype("datetime64[ms, UTC]").astype("int64").to_numpy()
    before = got[gd < first_real]
    after = got[gd >= first_real].reset_index(drop=True)
    f_part = fut[(fut["date"] >= pd.Timestamp("2023-01-05", tz="UTC")) & (fut["date"] < pd.Timestamp("2023-01-21", tz="UTC"))]
    check("替身从 --start（2023-01-05）补到第一根真实标记价格之前：16 天 = 384 根", len(before), 16 * 24)
    check("替身 = 合约K线的开高低收、成交量 0",
          (np.allclose(before[["open", "high", "low", "close"]].to_numpy(), f_part[["open", "high", "low", "close"]].to_numpy()),
           float(before["volume"].abs().max())), (True, 0.0))
    check("真实的标记价格一根没变（中间缺的 2 根也没有补）",
          (len(after), np.array_equal(after[["open", "high", "low", "close", "volume"]].to_numpy(),
                                      real[["open", "high", "low", "close", "volume"]].to_numpy())), (len(real), True))
    s = manifest(dd)["pairs"]
    check("清单：BTC 替身 384 根、第一根真实标记价格 2023-01-21", (s["BTC/USDT:USDT"]["mark_substitute_rows"],
          s["BTC/USDT:USDT"]["first_real_mark"]), (384, "2023-01-21T00:00:00Z"))
    check("K线全在 --start 以前的币：一根都不补", s["OLD/USDT:USDT"]["mark_substitute_rows"], 0)
    check("K线到最近、还没下标记价格的币：不补，提示先下载",
          (s["NEWX/USDT:USDT"]["mark_substitute_rows"], any("先下载标记价格" in n for n in s["NEWX/USDT:USDT"]["notes"])), (0, True))
    sha1 = sha_tree(dd)
    rc, _ = run(["update", NA, "--config", str(cfg), "--from-dir", str(src), "--start", "20230105", "--end", "20230209"])
    s2 = manifest(dd)["pairs"]["BTC/USDT:USDT"]
    check("再跑一次：一根都不多补，文件不变，第一根真实标记价格还是 2023-01-21",
          (rc, s2["mark_substitute_added_this_run"], s2["mark_substitute_rows"], s2["first_real_mark"], sha_tree(dd) == sha1),
          (0, 0, 384, "2023-01-21T00:00:00Z", True))
    # 之后 freqtrade 下载又往后追加了真实标记价格；更早的 --start 再导一次
    more = candles_df("2023-02-10", 48, p0=101.0, vol=0.0)
    store(dd, "BTC/USDT:USDT", pd.concat([load(dd, "BTC/USDT:USDT", MARK), more], ignore_index=True), MARK)
    rc, _ = run(["update", NA, "--config", str(cfg), "--from-dir", str(src), "--start", "20230101", "--end", "20230212"])
    s3 = manifest(dd)["pairs"]["BTC/USDT:USDT"]
    check("后来追加了真实标记价格、--start 提前到 01-01：只多补 01-01 ~ 01-04 的 96 根，第一根真实的不变",
          (rc, s3["mark_substitute_added_this_run"], s3["mark_substitute_rows"], s3["first_real_mark"],
           len(s3["mark_substitute_ranges"])), (0, 96, 480, "2023-01-21T00:00:00Z", 1))
    tail = load(dd, "BTC/USDT:USDT", MARK).tail(48).reset_index(drop=True)
    check("追加的真实标记价格没被动", np.array_equal(tail[["open", "close"]].to_numpy(), more[["open", "close"]].to_numpy()), True)
    # --no-mark-substitute：新目录里不补
    root2 = TMP / "mark_off"
    dd2 = root2 / "dd"
    cfg2 = make_config(root2 / "cfg.json", ["BTC/USDT:USDT"], dd2)
    store(dd2, "BTC/USDT:USDT", fut, FUT)
    store(dd2, "BTC/USDT:USDT", real, MARK)
    sha_m = sha_tree(dd2)["futures/BTC_USDT_USDT-1h-mark.feather"]
    rc, _ = run(["update", NA, "--config", str(cfg2), "--from-dir", str(src), "--start", "20230101", "--end", "20230209",
                 "--no-mark-substitute"])
    check("--no-mark-substitute：标记价格文件不动，清单 mark_substitute = false",
          (rc, sha_tree(dd2)["futures/BTC_USDT_USDT-1h-mark.feather"] == sha_m, manifest(dd2)["mark_substitute"]), (0, True, False))
    # 下架的币：K线停在 --start 之后、--end 100 天以前 -> 整段替身
    root3 = TMP / "mark_dead"
    dd3 = root3 / "dd"
    cfg3 = make_config(root3 / "cfg.json", ["OLD/USDT:USDT"], dd3)
    store(dd3, "OLD/USDT:USDT", candles_df("2023-01-01", 10 * 24), FUT)
    rc, _ = run(["update", NA, "--config", str(cfg3), "--from-dir", str(src), "--start", "20230101", "--end", "20230601"])
    s4 = manifest(dd3)["pairs"]["OLD/USDT:USDT"]
    check("下架的币（K线 2023-01-10 就停了，--end 2023-06-01）：整段 240 根都用K线做替身，没有真实标记价格",
          (rc, s4["mark_substitute_rows"], s4["first_real_mark"]), (0, 240, None))


# ----------------------------------------------------------------------------
# 8. 幂等
# ----------------------------------------------------------------------------
def test_idempotent() -> None:
    print("8. 同样的输入跑两次数据文件一个字节都不变；两份新目录各跑一次结果也一样")
    shas = []
    for k in (1, 2):
        root = TMP / f"idem{k}"
        dd = root / "dd"
        cfg = make_config(root / "cfg.json", ["BTC/USDT:USDT", "ETH/USDT:USDT"], dd)
        store(dd, "BTC/USDT:USDT", candles_df("2023-01-01", 30 * 24), FUT)
        store(dd, "BTC/USDT:USDT", candles_df("2023-01-25", 6 * 24, vol=0.0), MARK)
        store(dd, "BTC/USDT:USDT", funding_df("2023-01-25", 18), FR)
        src = root / "src"
        rows = archive_rows("BTC-USDT", funding_df("2023-01-01", 90)) + archive_rows("ETH-USDT", funding_df("2023-01-01", 90, base=3e-4))
        write_zip(src / "all-2023-01.zip", {"a.csv": csv_text(HEADER, rows)})
        rc1, _ = run(["update", NA, "--config", str(cfg), "--from-dir", str(src), "--start", "20230101", "--end", "20230131"])
        s1 = sha_tree(dd)
        rc2, _ = run(["update", NA, "--config", str(cfg), "--from-dir", str(src), "--start", "20230101", "--end", "20230131"])
        check(f"第 {k} 份：两次都成功、第二次数据文件 sha256 一样", (rc1, rc2, sha_tree(dd) == s1), (0, 0, True))
        shas.append(sha_tree(dd))
    check("两份新目录各跑一次，数据文件 sha256 完全一样", shas[0], shas[1])


# ----------------------------------------------------------------------------
# 9. 拒绝主数据目录
# ----------------------------------------------------------------------------
def test_main_datadir() -> None:
    print("9. 拒绝往 user_data/data/okx 里写（运行 A / B 和 NFI 回测的数据目录），除非 --allow-main-datadir")
    main_dd = ROOT_DIR / "user_data" / "data" / "okx"
    before = sorted(str(p) for p in main_dd.rglob("*")) if main_dd.exists() else None
    existed = main_dd.exists()
    src = TMP / "main_src"
    write_zip(src / "all-2023-01.zip", {"a.csv": csv_text(HEADER, archive_rows("BTC-USDT", funding_df("2023-01-01", 9)))})
    cases = [
        ("--datadir 写绝对路径", make_config(TMP / "main1.json", ["BTC/USDT:USDT"]), ["--datadir", str(main_dd)]),
        ("--datadir 写 ./user_data/data/okx/（相对项目根目录）", make_config(TMP / "main2.json", ["BTC/USDT:USDT"]),
         ["--datadir", "./user_data/data/okx/"]),
        ("配置里的 \"datadir\"", make_config(TMP / "main3.json", ["BTC/USDT:USDT"], main_dd), []),
        ("都没写（默认 user_data/data/okx）", make_config(TMP / "main4.json", ["BTC/USDT:USDT"]), []),
    ]
    cwd = os.getcwd()
    os.chdir(ROOT_DIR)
    try:
        for name, cfg, extra in cases:
            rc, out = run(["update", "--config", str(cfg), "--from-dir", str(src), *extra])
            check(f"{name} -> 退出码 1", (rc, "user_data/data/okx" in out), (1, True))
        rc, _ = run(["seed", "--config", str(cases[0][1]), "--datadir", str(main_dd), "--from", str(TMP)])
        check("seed 往 user_data/data/okx 里复制 -> 退出码 1", rc, 1)
        link = TMP / "okx_link"
        if main_dd.exists():
            link.symlink_to(main_dd, target_is_directory=True)
            check("指向 user_data/data/okx 的符号链接也算", ofa.is_main_datadir(link), True)
        check("user_data/data/okx-funding 不算", ofa.is_main_datadir(ROOT_DIR / "user_data" / "data" / "okx-funding"), False)
        ctx = ofa.resolve_context([str(cases[0][1])], str(main_dd), True)
        check("--allow-main-datadir 时放行（只检查，不真的写）", ctx.datadir.resolve() == main_dd.resolve(), True)
    finally:
        os.chdir(cwd)
    after = sorted(str(p) for p in main_dd.rglob("*")) if main_dd.exists() else None
    check("user_data/data/okx 里一个文件都没多", (main_dd.exists(), after), (existed, before))


# ----------------------------------------------------------------------------
# 10. --from-dir 的文件种类、seed
# ----------------------------------------------------------------------------
def test_from_dir_and_seed() -> None:
    print("10. --from-dir：zip / csv / csv.gz、一个 zip 里几个 CSV、认不出的文件 -> 退出码 3；seed 只复制合约K线")
    root = TMP / "fromdir"
    dd = root / "dd"
    cfg = make_config(root / "cfg.json", ["BTC/USDT:USDT", "ETH/USDT:USDT", "SOL/USDT:USDT"], dd)
    src = root / "src"
    write_zip(src / "2023-01" / "two-2023-01.zip", {
        "btc.csv": csv_text(HEADER, archive_rows("BTC-USDT", funding_df("2023-01-01", 9))),
        "eth.csv": csv_text(HEADER, archive_rows("ETH-USDT", funding_df("2023-01-01", 9))),
        "__MACOSX/._btc.csv": "junk",
    })
    (src / "sol-2023-01.csv").write_text(csv_text(HEADER, archive_rows("SOL-USDT", funding_df("2023-01-01", 3))), encoding="utf-8")
    (src / "sol-2023-02.csv.gz").write_bytes(gzip.compress(csv_text(HEADER, archive_rows("SOL-USDT", funding_df("2023-02-01", 3))).encode()))
    (src / "readme.txt").write_text("不是归档", encoding="utf-8")
    rc, out = run(["update", NA, "--config", str(cfg), "--from-dir", str(src), "--start", "20230101", "--end", "20230228"])
    m = manifest(dd) if rc == 0 else {"pairs": {}, "files": []}
    check("zip（2 个 CSV）+ csv + csv.gz 都读到：BTC 9、ETH 9、SOL 6 条",
          (rc, [m["pairs"].get(p, {}).get("funding_rows") for p in ("BTC/USDT:USDT", "ETH/USDT:USDT", "SOL/USDT:USDT")]), (0, [9, 9, 6]))
    check("清单：3 个文件、来源 from-dir、zip 里的文件名", (len(m["files"]), m.get("lister"), m["files"][0]["members"] if m["files"] else None),
          (3, "from-dir", ["btc.csv", "eth.csv"]))
    sha = sha_tree(dd)
    bad = root / "bad"
    write_zip(bad / "weird-2023-03.zip", {"w.csv": "合约,时间,价格\nBTC-USDT-SWAP,1,2\n"})
    rc, out = run(["update", "--config", str(cfg), "--from-dir", str(bad), "--start", "20230101", "--end", "20230331"])
    check("表头认不出 -> 退出码 3，打印看到的表头，数据文件不动", (rc, "合约,时间,价格" in out, sha_tree(dd) == sha), (3, True, True))
    write_zip(bad / "weird-2023-03.zip", {"w.csv": "1,2,3\n4,5,6\n"})
    rc, _ = run(["update", "--config", str(cfg), "--from-dir", str(bad)])
    check("没有表头的 CSV -> 退出码 3", rc, 3)
    (bad / "weird-2023-03.zip").write_bytes(b"PK\x03\x04 not really a zip")
    rc, _ = run(["update", "--config", str(cfg), "--from-dir", str(bad)])
    check("坏的 zip -> 退出码 3", rc, 3)
    rc, _ = run(["update", "--config", str(cfg), "--from-dir", str(root / "empty_dir_not_there")])
    check("--from-dir 不存在 -> 退出码 1", rc, 1)

    # seed
    s_src = root / "seed_src"
    s_dst = root / "seed_dst"
    cfg_s = make_config(root / "cfg_seed.json", ["BTC/USDT:USDT", "ETH/USDT:USDT"], s_dst)
    for p in ("BTC/USDT:USDT", "ETH/USDT:USDT", "XRP/USDT:USDT"):
        for tf in ("1h", "1d"):
            store(s_src, p, candles_df("2023-01-01", 48), FUT, tf)
        store(s_src, p, candles_df("2023-01-01", 48, vol=0.0), MARK)
        store(s_src, p, funding_df("2023-01-01", 6), FR)
    real_file = s_src / "futures" / "ETH_USDT_USDT-1d-futures.feather"
    link_target = root / "elsewhere.feather"
    shutil.move(real_file, link_target)
    real_file.symlink_to(link_target)  # 源文件是符号链接
    store(s_dst, "BTC/USDT:USDT", candles_df("2022-01-01", 5), FUT, "1h")  # 目标已有
    dst_sha = sha_tree(s_dst)["futures/BTC_USDT_USDT-1h-futures.feather"]
    rc, out = run(["seed", "--config", str(cfg_s), "--from", str(s_src)])
    names = sorted(p.name for p in (s_dst / "futures").iterdir())
    check("seed：只复制配置里的币的合约K线（BTC 1d、ETH 1h/1d），不复制 mark / funding_rate / 别的币", (rc, names),
          (0, ["BTC_USDT_USDT-1d-futures.feather", "BTC_USDT_USDT-1h-futures.feather",
               "ETH_USDT_USDT-1d-futures.feather", "ETH_USDT_USDT-1h-futures.feather"]))
    check("seed：目标里已有的文件不覆盖", sha_tree(s_dst)["futures/BTC_USDT_USDT-1h-futures.feather"], dst_sha)
    check("seed：复制的是普通文件，不是符号链接（源文件是符号链接时也复制内容）",
          [p.is_symlink() for p in sorted((s_dst / "futures").iterdir())], [False, False, False, False])
    rc, out = run(["update", NA, "--config", str(cfg_s), "--from-dir", str(src), "--seed-from", str(s_src), "--no-mark-substitute",
                   "--start", "20230101", "--end", "20230131"])
    check("update --seed-from：已经都有了，一个也不复制", (rc, "复制了 0 个文件" in out), (0, True))


# ----------------------------------------------------------------------------
# 11. 配置检查、status
# ----------------------------------------------------------------------------
def test_config_status() -> None:
    print("11. 配置检查（正则白名单 / 不是 OKX / 不是 StaticPairList / 不是 USDT 永续）和 status")
    src = TMP / "main_src"
    for name, cfg in (
        ("白名单是正则", make_config(TMP / "c1.json", [".*/USDT:USDT"], TMP / "c1")),
        ("交易所不是 OKX", make_config(TMP / "c2.json", ["BTC/USDT:USDT"], TMP / "c2", exchange={"name": "binance"})),
        ("VolumePairList", make_config(TMP / "c3.json", ["BTC/USDT:USDT"], TMP / "c3", pairlists=[{"method": "VolumePairList"}])),
        ("币本位交易对", make_config(TMP / "c4.json", ["BTC/USD:BTC"], TMP / "c4")),
    ):
        rc, out = run(["update", "--config", str(cfg), "--from-dir", str(src)])
        check(f"{name} -> 退出码 1", rc, 1)
    rc, _ = run(["update", "--config", str(TMP / "nope.json")])
    check("配置文件不存在 -> 退出码 1", rc, 1)
    rc, _ = run(["update", "--config", str(make_config(TMP / "c6.json", ["BTC/USDT:USDT"], TMP / "c6")), "--start", "2023-01-01"])
    check("--start 格式不对 -> 退出码 1", rc, 1)
    rc, _ = run(["update", "--bogus"])
    check("不认识的参数 -> 退出码 1（不是 argparse 默认的 2，2 留给网络出错）", rc, 1)
    cfg = TMP / "merge_一样" / "cfg.json"
    rc, out = run(["status", "--config", str(cfg)])
    check_true("status：打印清单小结（区间、费率列、每个币一行）", rc == 0 and "real_funding_rate" in out and "BTC/USDT:USDT" in out, out[-500:])
    rc, out = run(["status", "--config", str(make_config(TMP / "c5.json", ["BTC/USDT:USDT"], TMP / "c5_empty"))])
    check("status：没有清单 -> 退出码 1", rc, 1)


# ----------------------------------------------------------------------------
# 13 ~ 18. 2026-11-08 复查后加的：重建、拒绝没有接口数据的币、累计的重叠核对、来源记录、资金费 / 标记价格的洞
# ----------------------------------------------------------------------------
BTC = "BTC/USDT:USDT"


def dms(df: pd.DataFrame) -> list[int]:
    return [int(pd.Timestamp(d).value // 1_000_000) for d in df["date"]]


def _set_manifest(dd: Path, **kv: Any) -> None:
    p = dd / "okx_funding_archive" / "manifest.json"
    m = json.loads(p.read_text(encoding="utf-8"))
    m.update(kv)
    p.write_text(json.dumps(m, ensure_ascii=False, indent=2), encoding="utf-8")


def test_rebuild() -> None:
    print("13. 重新导入时按这次的归档 / 现在的合约K线重建（脚本改过、换了费率列，旧的值不留下）；区间外没重建的行记成旧版脚本做的")
    root = TMP / "rebuild"
    dd = root / "dd"
    cfg = make_config(root / "cfg.json", [BTC], dd)
    full = funding_df("2023-01-01", 90)  # 2023-01-01 ~ 01-30
    store(dd, BTC, full.iloc[80:].reset_index(drop=True), FR)  # 接口下的最后 10 条
    fut = candles_df("2023-01-01", 31 * 24)
    store(dd, BTC, fut, FUT)
    real = candles_df("2023-01-25", 7 * 24, p0=100.3, vol=0.0)
    store(dd, BTC, real, MARK)
    v1 = root / "v1"  # 旧版：只有 funding_rate 列，值也不对
    write_zip(v1 / "x-2023-01.zip", {"x.csv": csv_text("instrument_name,funding_rate,funding_time",
                                                      [f"BTC-USDT-SWAP,0.00123,{t}" for t in dms(full)])})
    v2 = root / "v2"  # 新版：real_funding_rate，值对
    write_zip(v2 / "x-2023-01.zip", {"x.csv": csv_text(HEADER, archive_rows("BTC-USDT", full))})
    args = ["update", "--config", str(cfg), "--start", "20230101", "--end", "20230131"]
    rc, _ = run([*args, "--from-dir", str(v1)])
    m1 = manifest(dd)
    s1 = m1["pairs"][BTC]
    check("v1：导入 80 条，rate_is_realized = false，重叠 10 条全对不上（overlap_mismatch）",
          (rc, s1["archive_rows_added"], m1["rate_is_realized"], s1["overlap_rows"], s1["overlap_diff_rows"], m1["overlap_mismatch"]),
          (0, 80, False, 10, 10, True))
    rc, out = run([*args, "--from-dir", str(v2)])
    m2 = manifest(dd)
    s2 = m2["pairs"][BTC]
    got = load(dd, BTC, FR)
    check("v2：以前导入的 80 条按新的归档重建（改了 80 条），文件里全是对的值，接口的 10 条没动",
          (rc, s2["archive_rows_replaced_this_run"], s2["archive_rows_added_this_run"],
           np.array_equal(got["funding_rate"].to_numpy(), full["funding_rate"].to_numpy())), (0, 80, 0, True))
    check("v2：清单 rate_column = real_funding_rate、rate_is_realized = true、overlap_mismatch = false",
          (m2["rate_column"], m2["rate_is_realized"], m2["overlap_mismatch"], m2["overlap_checked"]), ("real_funding_rate", True, False, True))
    cur = ofa.tool_sha256()
    check("清单：data_tool_sha256 只有现在的脚本；runs[] 每次都记了 tool_sha256",
          (m2["data_tool_sha256"], [r.get("tool_sha256") for r in m2["runs"]]), ([cur], [cur, cur]))
    check_true("来源记录 okx_funding_provenance.json 写在数据目录最上层（不在缓存文件夹里）",
               (dd / ofa.PROVENANCE_NAME).is_file() and json.loads((dd / ofa.PROVENANCE_NAME).read_text(encoding="utf-8"))
               ["pairs"][BTC]["archive_added_ranges"] == s2["archive_added_ranges"])
    sha = sha_tree(dd)
    prov = (dd / ofa.PROVENANCE_NAME).read_bytes()
    rc, _ = run([*args, "--from-dir", str(v2)])
    s3 = manifest(dd)["pairs"][BTC]
    check("再跑一次：数据文件和来源记录一个字节都不变、重建 0 条",
          (rc, sha_tree(dd) == sha, (dd / ofa.PROVENANCE_NAME).read_bytes() == prov, s3["archive_rows_replaced_this_run"]),
          (0, True, True, 0))
    # 替身标记价格也按现在的合约K线重建（模拟旧版脚本补错了值）
    mk = load(dd, BTC, MARK)
    first_real = pd.Timestamp("2023-01-25", tz="UTC")
    n_sub = int((mk["date"] < first_real).sum())
    mk.loc[mk["date"] < first_real, ["open", "close"]] += 7.0
    store(dd, BTC, mk, MARK)
    rc, _ = run([*args, "--from-dir", str(v2)])
    s4 = manifest(dd)["pairs"][BTC]
    mk2 = load(dd, BTC, MARK)
    sub = mk2[mk2["date"] < first_real].reset_index(drop=True)
    fpart = fut[fut["date"] < first_real].reset_index(drop=True)
    check("改坏的替身按现在的合约K线重建（改了 576 根），真实的标记价格没动",
          (rc, n_sub, s4["mark_substitute_replaced_this_run"], np.allclose(sub[["open", "close"]].to_numpy(), fpart[["open", "close"]].to_numpy()),
           np.array_equal(mk2[mk2["date"] >= first_real][["open", "close"]].to_numpy(), real[["open", "close"]].to_numpy())),
          (0, 576, 576, True, True))
    # 区间外没有重建的行：上次是"旧版脚本"做的（改清单和来源记录里的 sha256 模拟），这次 --start 20230115
    old = "0" * 64
    _set_manifest(dd, data_tool_sha256=[old], tool_sha256=old)
    pv = json.loads((dd / ofa.PROVENANCE_NAME).read_text(encoding="utf-8"))
    pv["data_tool_sha256"] = [old]
    (dd / ofa.PROVENANCE_NAME).write_text(json.dumps(pv), encoding="utf-8")
    rc, out = run(["update", "--config", str(cfg), "--start", "20230115", "--end", "20230131", "--from-dir", str(v2)])
    m5 = manifest(dd)
    check("--start 20230115（2023-01-01 ~ 14 的归档资金费、替身没有重建）：data_tool_sha256 = 旧的 + 现在的，大声提醒",
          (rc, m5["data_tool_sha256"], "data_tool_sha256" in out), (0, sorted([old, cur]), True))
    rc, _ = run([*args, "--from-dir", str(v2)])
    check("用整段区间再导一次：全部按现在的脚本重建，data_tool_sha256 只剩现在的", (rc, manifest(dd)["data_tool_sha256"]), (0, [cur]))
    rc, out = run(["status", "--config", str(cfg)])
    check_true("status：data_tool_sha256 一致时不提醒", rc == 0 and "data_tool_sha256" not in out, out[-400:])


def test_no_api() -> None:
    print("14. 还没有接口下载的资金费的币：默认拒绝导入（退出码 1，数据目录不动），--allow-no-api-funding 才照样导入")
    root = TMP / "noapi"
    dd = root / "dd"
    cfg = make_config(root / "cfg.json", [BTC, "ETH/USDT:USDT"], dd)
    full = funding_df("2023-01-01", 90)
    store(dd, BTC, full.iloc[80:].reset_index(drop=True), FR)
    src = root / "src"
    write_zip(src / "x-2023-01.zip", {"x.csv": csv_text(HEADER, archive_rows("BTC-USDT", full) + archive_rows("ETH-USDT", full))})
    args = ["update", "--config", str(cfg), "--from-dir", str(src), "--start", "20230101", "--end", "20230131", "--no-mark-substitute"]
    sha = sha_tree(dd)
    rc, out = run(args)
    check("ETH 还没有接口下载的资金费 -> 退出码 1、点名 ETH、数据目录不动、没有写清单",
          (rc, "ETH/USDT:USDT" in out and "allow-no-api-funding" in out, sha_tree(dd) == sha,
           (dd / "okx_funding_archive" / "manifest.json").exists()), (1, True, True, False))
    rc, out = run([*args, NA])
    m = manifest(dd)
    check("--allow-no-api-funding：照样导入，清单记下 ETH（pairs_without_api_funding、no_overlap_pairs），BTC 照常核对",
          (rc, m["pairs_without_api_funding"], m["allow_no_api_funding"], m["no_overlap_pairs"], m["overlap_checked"],
           m["pairs"][BTC]["overlap_rows"]), (0, ["ETH/USDT:USDT"], True, ["ETH/USDT:USDT"], True, 10))
    check_true("--allow-no-api-funding：警告里说 ETH 没有逐个核对，给的办法是删掉资金费文件再下载",
               any("没有逐个核对" in w and "删掉" in w for w in m["warnings"]), str(m["warnings"]))
    rc, _ = run(args)
    check("ETH 的文件里只有归档的行（还是没有接口数据）-> 不加参数照样拒绝", rc, 1)
    # 只有一个币、一条都不重叠：overlap_checked = false，提醒里的办法不是"再导入一次"
    root2 = TMP / "noapi_only"
    dd2 = root2 / "dd"
    cfg2 = make_config(root2 / "cfg.json", [BTC], dd2)
    rc, out = run(["update", NA, "--config", str(cfg2), "--from-dir", str(src), "--start", "20230101", "--end", "20230131",
                   "--no-mark-substitute"])
    m2 = manifest(dd2)
    check_true("一条都不重叠：overlap_checked = false、!! 提醒（不能正式运行；办法：删掉资金费文件、先下载再导入）",
               rc == 0 and not m2["overlap_checked"] and any(w.startswith("!!") and "不能正式运行" in w and "删掉" in w for w in m2["warnings"]),
               str(m2["warnings"]))


def test_overlap_cumulative() -> None:
    print("15. 重叠核对是累计的：一次一条都没比到（区间很窄），沿用上次的结论（不会把 overlap_mismatch 清掉）")
    root = TMP / "ov_cum"
    dd = root / "dd"
    cfg = make_config(root / "cfg.json", [BTC], dd)
    full = funding_df("2023-01-01", 90)
    store(dd, BTC, full.iloc[60:].reset_index(drop=True), FR)
    arch = full.copy()
    arch.loc[arch.index[-10:], "funding_rate"] += 1e-6
    src = root / "src"
    write_zip(src / "x-2023-01.zip", {"x.csv": csv_text(HEADER, archive_rows("BTC-USDT", arch))})
    rc, _ = run(["update", "--config", str(cfg), "--from-dir", str(src), "--start", "20230101", "--end", "20230131", "--no-mark-substitute"])
    m1 = manifest(dd)
    check("第一次：重叠 30 条、对不上 10 条 -> overlap_mismatch", (rc, m1["overlap_rows_total"], m1["overlap_diff_rows_total"],
                                                         m1["overlap_mismatch"]), (0, 30, 10, True))
    rc, _ = run(["update", "--config", str(cfg), "--from-dir", str(src), "--start", "20230101", "--end", "20230105", "--no-mark-substitute"])
    m2 = manifest(dd)
    s2 = m2["pairs"][BTC]
    check("第二次 --start 20230101 --end 20230105（和接口数据不重叠）：沿用上次的核对，overlap_mismatch 还是 true",
          (rc, m2["overlap_rows_total"], m2["overlap_mismatch"], m2["overlap_checked"], bool(s2["overlap_carried_from"])),
          (0, 30, True, True, True))
    check("第二次：区间 1 ~ 5 号以外（6 ~ 20 号）的归档行没有重建、照旧", (s2["archive_rows_added"], s2["archive_rows_kept_outside_run"]),
          (60, 45))


def test_provenance() -> None:
    print("16. 清单丢了 / 坏了：按来源记录认（不在缓存文件夹里）；来源记录也丢了：替身按数据认，资金费重叠得不可能那么长 = 来源不明")
    root = TMP / "prov"
    dd = root / "dd"
    cfg = make_config(root / "cfg.json", [BTC], dd)
    full = funding_df("2023-01-01", 3 * 200)  # 200 天
    api = full.iloc[-30:].reset_index(drop=True)
    store(dd, BTC, api, FR)
    store(dd, BTC, candles_df("2023-01-01", 200 * 24), FUT)
    store(dd, BTC, candles_df("2023-07-01", 19 * 24, p0=100.3, vol=0.0), MARK)
    src = root / "src"
    write_zip(src / "all-2023.zip", {"a.csv": csv_text(HEADER, archive_rows("BTC-USDT", full))})
    args = ["update", "--config", str(cfg), "--from-dir", str(src), "--start", "20230101", "--end", "20230719"]
    keys = ("first_real_mark", "mark_substitute_rows", "archive_rows_added", "overlap_rows", "provenance_unknown_rows")
    rc, _ = run(args)
    s1 = {k: manifest(dd)["pairs"][BTC][k] for k in keys}
    check("第一次：替身 = 2023-01-01 ~ 06-30（181 天），归档补 570 条，重叠 30 条", (rc, s1),
          (0, {"first_real_mark": "2023-07-01T00:00:00Z", "mark_substitute_rows": 181 * 24, "archive_rows_added": 570,
               "overlap_rows": 30, "provenance_unknown_rows": 0}))
    sha = sha_tree(dd)
    shutil.rmtree(dd / "okx_funding_archive")
    rc, out = run(args)
    s2 = {k: manifest(dd)["pairs"][BTC][k] for k in keys}
    check("删掉 okx_funding_archive/（清单和缓存都没了）：按来源记录认，结果和原来一样、数据文件不变",
          (rc, s2, sha_tree(dd) == sha, "来源记录" in out), (0, s1, True, True))
    (dd / "okx_funding_archive" / "manifest.json").write_text("{坏了", encoding="utf-8")
    rc, _ = run(args)
    check("清单坏了：一样", (rc, {k: manifest(dd)["pairs"][BTC][k] for k in keys}), (0, s1))
    # 来源记录也没了
    shutil.rmtree(dd / "okx_funding_archive")
    (dd / ofa.PROVENANCE_NAME).unlink()
    rc, out = run(args)
    m3 = manifest(dd)
    s3 = m3["pairs"][BTC]
    check("清单和来源记录都没了：替身按数据认出来（第一根真实标记价格、替身根数和原来一样）",
          (rc, s3["first_real_mark"], s3["mark_substitute_rows"]), (0, s1["first_real_mark"], s1["mark_substitute_rows"]))
    check_true("清单和来源记录都没了：资金费和\"接口数据\"重叠了 200 天（不可能）-> 来源不明，列在 provenance_unknown_pairs、!! 提醒",
               m3["provenance_unknown_pairs"] == [BTC] and int(s3["provenance_unknown_rows"]) > 0
               and any("来源不明" in w for w in m3["warnings"]), str((s3["provenance_unknown_rows"], m3["warnings"])))
    rc, _ = run(args)
    check("下一次（有清单了）：来源不明照样记着", (rc, manifest(dd)["provenance_unknown_pairs"]), (0, [BTC]))
    # 办法：删掉资金费文件、重新下载接口数据、再导入
    store(dd, BTC, api, FR)
    rc, _ = run(args)
    m4 = manifest(dd)
    check("删掉资金费文件重下以后再导入：来源不明没了，重叠又是 30 条", (rc, m4["provenance_unknown_pairs"], m4["pairs"][BTC]["overlap_rows"]),
          (0, [], 30))


def test_gaps() -> None:
    print("17. 资金费中间的洞（归档少了一个月）、真实标记价格中间缺的一段（隔了 3 个月以上才更新）：记进清单、大声提醒")
    root = TMP / "gaps"
    dd = root / "dd"
    cfg = make_config(root / "cfg.json", [BTC, "SUSP/USDT:USDT"], dd)
    full = funding_df("2024-01-01", 3 * 182)  # 2024-01-01 ~ 06-30
    no_mar = full[full["date"].dt.strftime("%Y-%m") != "2024-03"].reset_index(drop=True)
    store(dd, BTC, no_mar.iloc[-30:].reset_index(drop=True), FR)
    store(dd, BTC, candles_df("2024-01-01", 182 * 24), FUT)
    # SUSP：3 月整月暂停交易（没有K线），资金费也没有 = 不是数据问题
    susp_fut = pd.concat([candles_df("2024-01-01", 60 * 24), candles_df("2024-04-01", 91 * 24)], ignore_index=True)
    store(dd, "SUSP/USDT:USDT", susp_fut, FUT)
    store(dd, "SUSP/USDT:USDT", no_mar.iloc[-30:].reset_index(drop=True), FR)
    src = root / "src"
    for mon, part in no_mar.groupby(no_mar["date"].dt.strftime("%Y-%m")):
        write_zip(src / f"all-{mon}.zip", {"a.csv": csv_text(HEADER, archive_rows("BTC-USDT", part) + archive_rows("SUSP-USDT", part))})
    rc, out = run(["update", "--config", str(cfg), "--from-dir", str(src), "--start", "20240101", "--end", "20240630", "--no-mark-substitute"])
    m = manifest(dd)
    g = m["pairs"][BTC]["funding_gaps"]
    check("BTC 少了 2024-03：记一个洞（2024-02-29 16:00 ~ 2024-04-01 00:00，752 小时），funding_gap_pairs 只有 BTC",
          (rc, g, m["funding_gap_pairs"]), (0, [["2024-02-29T16:00:00Z", "2024-04-01T00:00:00Z", 752.0]], [BTC]))
    check("SUSP 3 月暂停交易（没有K线）：不算洞", m["pairs"]["SUSP/USDT:USDT"]["funding_gaps"], [])
    check_true("资金费有洞：!! 提醒（洞里的资金费按 0 算；办法：重新运行 / --from-dir）", any(w.startswith("!!") and "有洞" in w for w in m["warnings"])
               and "资金费有 1 个洞" in out, str(m["warnings"]))
    # 标记价格：真实的从 2024-03-01 起，4 ~ 7 月那段没有（隔了很久才更新），8 月以后又有
    root2 = TMP / "markgap"
    dd2 = root2 / "dd"
    cfg2 = make_config(root2 / "cfg.json", [BTC], dd2)
    store(dd2, BTC, candles_df("2024-01-01", 274 * 24), FUT)  # 2024-01-01 ~ 09-30
    mk = pd.concat([candles_df("2024-03-01", 31 * 24, p0=100.3, vol=0.0), candles_df("2024-08-01", 61 * 24, p0=100.3, vol=0.0)],
                   ignore_index=True)
    store(dd2, BTC, mk, MARK)
    f2 = funding_df("2024-01-01", 3 * 274)
    store(dd2, BTC, f2.iloc[-30:].reset_index(drop=True), FR)
    src2 = root2 / "src"
    write_zip(src2 / "all-2024.zip", {"a.csv": csv_text(HEADER, archive_rows("BTC-USDT", f2))})
    rc, out = run(["update", "--config", str(cfg2), "--from-dir", str(src2), "--start", "20240101", "--end", "20240930"])
    m2 = manifest(dd2)
    s2 = m2["pairs"][BTC]
    check("真实标记价格 4 ~ 7 月没有：记一段（2024-04-01 00:00 ~ 07-31 23:00，2928 小时），不补；替身只补 3 月以前（60 天）",
          (rc, s2["mark_gaps"], m2["mark_gap_pairs"], s2["mark_substitute_rows"]),
          (0, [["2024-04-01T00:00:00Z", "2024-07-31T23:00:00Z", 2928.0]], [BTC], 60 * 24))
    check_true("标记价格缺一段：!! 提醒（办法：删掉标记价格文件，再 make okx-funding-data）",
               any(w.startswith("!!") and "标记价格缺了一段" in w and "删掉" in w for w in m2["warnings"]), str(m2["warnings"]))
    rc, out = run(["status", "--config", str(cfg2)])
    check_true("status 也列出来", rc == 0 and "真实标记价格中间缺了一段的币 1 个" in out, out[-600:])


def _fa_py() -> Path:
    """research.sh 里的 FA_PY（manifest 的覆盖 / 摘要 / 正式运行前的检查）抽出来，写成临时文件。"""
    text = (ROOT_DIR / "scripts" / "research.sh").read_text(encoding="utf-8")
    head = "IFS= read -r -d '' FA_PY <<'PYEOF' || true\n"
    i = text.index(head) + len(head)
    j = text.index("\nPYEOF\n", i)
    p = TMP / "fa_py.py"
    p.write_text(text[i:j] + "\n", encoding="utf-8")
    return p


def test_gate() -> None:
    print("18. research.sh 正式运行之前的检查（FA_PY gate）：清单里任何一项不对 = 不能正式运行")
    fa = _fa_py()
    tool = ROOT_DIR / "scripts" / "okx_funding_archive.py"
    cur = ofa.tool_sha256()
    reg = TMP / "gate_reg.json"
    reg.write_text(json.dumps({"strategies": {"XSFundingCarry": {"pinned_tools": {"scripts/okx_funding_archive.py": cur}}}}), encoding="utf-8")
    today = datetime.now(UTC).strftime("%Y%m%d")
    good = {
        "tool_sha256": cur, "data_tool_sha256": [cur], "start": "20220301", "end": today, "rate_column": "real_funding_rate",
        "rate_is_realized": True, "time_is_fallback": False, "mark_substitute": True, "overlap_rows_total": 500,
        "overlap_checked": True, "overlap_mismatch": False, "provenance_unknown_pairs": [],
        "pairs": {BTC: {"funding_gaps": [["2022-03-01T00:00:00Z", "2022-03-10T00:00:00Z", 216.0]], "mark_gaps": [],
                        "overlap_rows": 50, "archive_rows_added": 100}},
    }

    def gate(man: dict, reg_path: Path = reg) -> tuple[int, str]:
        mp = TMP / "gate_manifest.json"
        mp.write_text(json.dumps(man), encoding="utf-8")
        out_json = TMP / "gate_out.json"
        p = subprocess.run([sys.executable, "-I", str(fa), "gate", str(mp), "20220401", str(tool), str(reg_path), "XSFundingCarry",
                            str(out_json)], capture_output=True, text=True, timeout=60, check=False)
        return p.returncode, p.stdout + p.stderr

    rc, out = gate(good)
    check_true("都对（2022-03 上旬的洞在研究需要的 2022-03-25 以前，不管）-> 退出码 0", rc == 0, out)
    check_true("结果写进 JSON（ok、problems）", json.loads((TMP / "gate_out.json").read_text(encoding="utf-8")).get("ok") is True)
    cases = [
        ("overlap_mismatch = true", {"overlap_mismatch": True}),
        ("overlap_checked = false（一条都没比到）", {"overlap_checked": False, "overlap_rows_total": 0}),
        ("费率列不是实际结算的费率", {"rate_is_realized": False, "rate_column": "funding_rate"}),
        ("时间列是 timestamp", {"time_is_fallback": True}),
        ("没有补标记价格替身", {"mark_substitute": False}),
        ("清单是旧版脚本写的", {"tool_sha256": "0" * 64, "data_tool_sha256": ["0" * 64]}),
        ("数据里还有旧版脚本做的行", {"data_tool_sha256": sorted(["0" * 64, cur])}),
        ("导入从 2023 年才开始", {"start": "20230101"}),
        ("导入只到一年以前", {"end": "20251101"}),
        ("有来源不明的资金费", {"provenance_unknown_pairs": [BTC]}),
        ("研究区间里资金费有洞", {"pairs": {BTC: {"funding_gaps": [["2023-02-28T16:00:00Z", "2023-04-01T00:00:00Z", 752.0]],
                                                "mark_gaps": []}}}),
        ("研究区间里真实标记价格缺了一天以上", {"pairs": {BTC: {"funding_gaps": [], "mark_gaps": [["2026-05-01T00:00:00Z",
                                                                                     "2026-07-31T23:00:00Z", 2208.0]]}}}),
    ]
    for name, patch in cases:
        rc, out = gate({**good, **patch})
        check_true(f"{name} -> 退出码 1，打印原因", rc == 1 and "!!" in out, out)
    rc, out = gate({**good, "pairs": {BTC: {"funding_gaps": [], "mark_gaps": [["2026-05-01T00:00:00Z", "2026-05-01T09:00:00Z", 10.0]]}}})
    check_true("标记价格只缺 10 小时（不到一天）-> 只提醒，退出码 0", rc == 0, out)
    reg2 = TMP / "gate_reg2.json"
    reg2.write_text(json.dumps({"strategies": {"XSFundingCarry": {"pinned_tools": {"scripts/okx_funding_archive.py": "1" * 64}}}}),
                    encoding="utf-8")
    rc, out = gate(good, reg2)
    check_true("登记表固定的工具脚本 sha256 和清单的不一样 -> 退出码 1", rc == 1 and "pinned_tools" in out, out)
    mp = TMP / "gate_bad.json"
    mp.write_text("{坏了", encoding="utf-8")
    p = subprocess.run([sys.executable, "-I", str(fa), "gate", str(mp), "20220401", str(tool), str(reg), "XSFundingCarry", ""],
                       capture_output=True, text=True, timeout=60, check=False)
    check("清单读不了 -> 退出码 1", p.returncode, 1)


# ----------------------------------------------------------------------------
# 12. 端到端
# ----------------------------------------------------------------------------
def _bt_trades(out_dir: Path, strat: str) -> list[dict]:
    zips = sorted(out_dir.glob("backtest-result-*.zip"))
    if not zips:
        return []
    with zipfile.ZipFile(zips[-1]) as z:
        name = zips[-1].stem + ".json"
        if name not in z.namelist():
            name = next(n for n in z.namelist() if n.endswith(".json") and "_config" not in n)
        doc = json.loads(z.read(name))
    return (doc.get("strategy", {}).get(strat) or {}).get("trades") or []


def _backtest(datadir: Path, configs: list[Path], timerange: str, out: Path) -> tuple[int, list[dict], str]:
    out.mkdir(parents=True, exist_ok=True)
    args = [sys.executable, str(ROOT_DIR / "scripts" / "ft_offline.py"), "backtesting"]
    for c in configs:
        args += ["--config", str(c)]
    args += ["--datadir", str(datadir), "--strategy", "XSFundingCarry", "--timerange", timerange, "--fee", "0.0007",
             "--enable-protections", "--export", "trades", "--backtest-directory", str(out), "--cache", "none"]
    p = subprocess.run(args, cwd=ROOT_DIR, capture_output=True, text=True, timeout=600, check=False)
    (out / "backtest.log").write_text(p.stdout + p.stderr, encoding="utf-8")
    return p.returncode, _bt_trades(out, "XSFundingCarry"), p.stdout + p.stderr


def _cli(args: list[str]) -> tuple[int, str]:
    p = subprocess.run([sys.executable, str(ROOT_DIR / "scripts" / "okx_funding_archive.py"), *args], cwd=ROOT_DIR,
                       capture_output=True, text=True, timeout=600, check=False)
    return p.returncode, p.stdout + p.stderr


def test_e2e(generate: bool = False) -> None:
    print("12. 端到端：去掉合成数据的老资金费和老标记价格，用 update --from-dir 导回来，回测 XSFundingCarry 的 funding_fees 不是 0")
    t0 = time.monotonic()
    syn = ROOT_DIR / "user_data" / "data" / "okx-synthetic"
    root = TMP / "e2e"
    orig = root / "orig"
    if not generate and (syn / "SYNTHETIC_DATA_README.txt").is_file() and (syn / "synthetic_pairs.json").is_file():
        src = syn
        print(f"  （数据：复制 {ofa._rel(syn)}）")
    else:
        src = root / "generated"
        p = subprocess.run([sys.executable, str(ROOT_DIR / "scripts" / "make_synthetic_data.py"), "--out", str(src), "--end", "20261101",
                            "--days", "300", "--timeframes", "1h 1d", "--no-spot", "--listings", "", "--crash-days", "0", "--pairs",
                            "BTC ETH SOL XRP DOGE LINK ADA AVAX NEW1 NEW2 NEW3 NEW4 NEW5 NEW6 NEW7 NEW8"],
                           cwd=ROOT_DIR, capture_output=True, text=True, timeout=600, check=False)
        if p.returncode != 0:
            check_true("生成合成数据", False, (p.stdout + p.stderr)[-800:])
            return
        print("  （数据：没有 user_data/data/okx-synthetic，在临时目录里生成了一份小的）")
    pairs = json.loads(_strip_comments((src / "synthetic_pairs.json").read_text(encoding="utf-8")))["exchange"]["pair_whitelist"]
    (orig / "futures").mkdir(parents=True, exist_ok=True)
    for f in ("SYNTHETIC_DATA_README.txt", "synthetic_markets.json", "synthetic_pairs.json"):
        shutil.copyfile(src / f, orig / f)
    from freqtrade.misc import pair_to_filename

    for p in pairs:
        for kind in ("1d-futures", "1h-futures", "1h-mark", "1h-funding_rate"):
            fn = f"{pair_to_filename(p)}-{kind}.feather"
            shutil.copyfile(src / "futures" / fn, orig / "futures" / fn)
    f_first = max(int(load(orig, p, FR)["date"].iloc[0].value // 1_000_000) for p in pairs)
    m_last = min(int(load(orig, p, MARK)["date"].iloc[-1].value // 1_000_000) for p in pairs)
    d = datetime.fromtimestamp(f_first / 1000, UTC).replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=8)
    w_start = d + timedelta(days=(7 - d.weekday()) % 7)  # 第一个周一（XSFundingCarry 周一调仓，要前 7 天资金费）
    w_end = w_start + timedelta(days=26)
    d_fund = w_end + timedelta(days=2)  # 这天以前的资金费去掉（"接口"数据从这天开始）
    d_arch = d_fund + timedelta(days=7)  # 归档写到这天以前：和"接口"数据重叠 7 天（真实情况：月度归档和最近 120 天的接口数据重叠）
    d_mark = d_fund + timedelta(days=28)  # 这天以前的标记价格去掉
    if int(d_mark.timestamp() * 1000) + 7 * DAY > m_last:
        check_true("合成数据的标记价格够长", False, f"标记价格只到 {ofa._iso(m_last)}")
        return
    tr = f"{w_start:%Y%m%d}-{w_end:%Y%m%d}"
    print(f"  （{len(pairs)} 个币；回测 {tr}；{d_fund:%Y-%m-%d} 以前的资金费、{d_mark:%Y-%m-%d} 以前的标记价格去掉）")

    # okx = "user_data/data/okx 的样子"：K线全、资金费 / 标记价格只有最近一段（freqtrade 从接口下的）
    okx = root / "okx"
    shutil.copytree(orig, okx)
    fixtures = root / "archive"
    by_month: dict[str, list[str]] = {}
    fd_ms, md_ms, ad_ms = int(d_fund.timestamp() * 1000), int(d_mark.timestamp() * 1000), int(d_arch.timestamp() * 1000)
    for p in pairs:
        fam = p.split("/")[0] + "-USDT"
        fr = load(orig, p, FR)
        frm = fr["date"].astype("datetime64[ms, UTC]").astype("int64")
        old = fr[frm < ad_ms]
        store(okx, p, fr[frm >= fd_ms].reset_index(drop=True), FR)
        for mon, part in old.groupby(old["date"].dt.strftime("%Y-%m")):
            by_month.setdefault(mon, []).extend(archive_rows(fam, part))
            by_month[mon].extend(archive_rows(fam.replace("-USDT", "-USD"), part.iloc[:3], 1e-3))  # 币本位，不要
        mk = load(orig, p, MARK)
        store(okx, p, mk[mk["date"].astype("datetime64[ms, UTC]").astype("int64") >= md_ms].reset_index(drop=True), MARK)
    by_month[min(by_month)].append(f"ZZZ-USDT-SWAP,SWAP,0.1,0.1,{f_first},{f_first}")  # 别的币，不要
    for mon, rows in by_month.items():
        write_zip(fixtures / f"allswaprate-swaprate-{mon}.zip", {f"allswaprate-swaprate-{mon}.csv": csv_text(HEADER, rows)})

    funding_cfg = ROOT_DIR / "configs" / "config.okx-research-funding.json"
    base_cfg = funding_cfg if funding_cfg.is_file() else ROOT_DIR / "configs" / "config.okx-research.json"
    configs = [base_cfg, okx / "synthetic_pairs.json", ROOT_DIR / "configs" / "research-overrides" / "XSFundingCarry.json"]
    cargs = [x for c in configs for x in ("--config", str(c))]

    # 目标数据目录：seed（只复制K线）+ "下载"最近的标记价格 / 资金费
    tgt = root / "okx-funding"
    rc, out = _cli(["seed", *cargs, "--datadir", str(tgt), "--from", str(okx)])
    kinds = sorted({p.name.split("-", 1)[1] for p in (tgt / "futures").iterdir()}) if rc == 0 else []
    check("seed（命令行）：只复制了合约K线", (rc, kinds), (0, ["1d-futures.feather", "1h-futures.feather"]))
    for p in pairs:
        for kind in ("1h-mark", "1h-funding_rate"):
            fn = f"{pair_to_filename(p)}-{kind}.feather"
            shutil.copyfile(okx / "futures" / fn, tgt / "futures" / fn)
    for f in ("SYNTHETIC_DATA_README.txt", "synthetic_markets.json"):
        shutil.copyfile(okx / f, tgt / f)

    # a. 只导资金费、不补标记价格：资金费和原来一样；回测有交易但 funding_fees 全是 0
    start_d = datetime.fromtimestamp(f_first / 1000, UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    uargs = ["update", *cargs, "--datadir", str(tgt), "--from-dir", str(fixtures), "--start", f"{start_d:%Y%m%d}",
             "--end", f"{d_arch:%Y%m%d}"]
    rc, out = _cli([*uargs, "--no-mark-substitute"])
    check("update --from-dir --no-mark-substitute（命令行）退出码 0", rc, 0)
    if rc != 0:
        print(out[-2000:])
        return
    same = all(
        np.array_equal(load(tgt, p, FR)["date"].astype("int64").to_numpy(), load(orig, p, FR)["date"].astype("int64").to_numpy())
        and np.array_equal(load(tgt, p, FR)["funding_rate"].to_numpy(), load(orig, p, FR)["funding_rate"].to_numpy())
        for p in pairs
    )
    check("每个币的资金费文件和原来完全一样（时间、费率逐条相等）", same, True)
    m = manifest(tgt)
    n_ov = 0
    for p in pairs:
        fm = load(orig, p, FR)["date"].astype("datetime64[ms, UTC]").astype("int64")
        n_ov += int(((fm >= fd_ms) & (fm < ad_ms)).sum())
    check("清单：费率列 real_funding_rate、币本位和别的币没进来、和\"接口\"数据重叠的 7 天逐条一致",
          (m["rate_column"], m["rate_is_realized"], sorted(m["pairs"]) == sorted(pairs), m["overlap_rows_total"],
           m["overlap_diff_rows_total"], m["overlap_checked"], m["overlap_mismatch"]),
          ("real_funding_rate", True, True, n_ov, 0, n_ov > 0, False))
    rc_a, tr_a, log_a = _backtest(tgt, configs, tr, root / "bt_no_mark")
    nz_a = sum(1 for t in tr_a if float(t.get("funding_fees") or 0) != 0)
    check_true("不补标记价格：回测有交易，但 funding_fees 全是 0（freqtrade 没有标记价格就不算资金费）",
               rc_a == 0 and len(tr_a) > 0 and nz_a == 0, f"退出码 {rc_a}，{len(tr_a)} 笔，{nz_a} 笔非 0\n{log_a[-1500:]}")

    # b. 补标记价格替身：funding_fees 不是 0；和原始数据回测比较
    rc, out = _cli(uargs)
    check("update --from-dir（补标记价格替身）退出码 0", rc, 0)
    m = manifest(tgt)
    subs = {p: s["mark_substitute_rows"] for p, s in m["pairs"].items()}
    s_ms = int(start_d.timestamp() * 1000)
    e_ms = ad_ms + DAY  # --end 那一天也算
    want_sub = {}
    for p in pairs:
        fm = load(orig, p, FUT)["date"].astype("datetime64[ms, UTC]").astype("int64")
        want_sub[p] = int(((fm >= s_ms) & (fm < min(md_ms, e_ms))).sum())
    check("每个币补的替身 = --start 到 \"第一根真实标记价格和 --end 里早的那个\"之间的 1h 合约K线根数", subs, want_sub)
    real_ok = all(
        np.array_equal(
            load(tgt, p, MARK).loc[lambda x: x["date"].astype("datetime64[ms, UTC]").astype("int64") >= md_ms, ["open", "close"]].to_numpy(),
            load(okx, p, MARK)[["open", "close"]].to_numpy())
        for p in pairs
    )
    check("真实标记价格（去掉那天以后的）一根都没变", real_ok, True)
    sha1 = sha_tree(tgt)
    rc, _ = _cli(uargs)
    check("再跑一次：数据文件一个字节都不变", (rc, sha_tree(tgt) == sha1), (0, True))
    rc_b, tr_b, log_b = _backtest(tgt, configs, tr, root / "bt_archive")
    rc_o, tr_o, _log_o = _backtest(orig, configs, tr, root / "bt_orig")
    nz_b = sum(1 for t in tr_b if float(t.get("funding_fees") or 0) != 0)
    check_true("补了替身：回测的 funding_fees 不是 0", rc_b == 0 and len(tr_b) > 0 and nz_b > 0,
               f"退出码 {rc_b}，{len(tr_b)} 笔，{nz_b} 笔非 0\n{log_b[-1500:]}")
    def key(t: dict) -> tuple:
        return (t.get("pair"), t.get("open_date"), t.get("is_short"))

    check("和原始数据回测：交易一模一样（币、开仓时间、方向）", sorted(map(key, tr_b)), sorted(map(key, tr_o)))
    ff_b = sum(float(t.get("funding_fees") or 0) for t in tr_b)
    ff_o = sum(float(t.get("funding_fees") or 0) for t in tr_o)
    rel = abs(ff_b - ff_o) / max(abs(ff_o), 1e-12)
    print(f"  （资金费合计：替身 {ff_b:.6f}，原始标记价格 {ff_o:.6f}，相差 {rel:.3%}；用时 {time.monotonic() - t0:.0f} 秒）")
    check_true("资金费合计和原始数据只差一点点（< 2%，标记价格和最新价的差别）", rc_o == 0 and nz_b > 0 and rel < 0.02, f"相差 {rel:.3%}")


def _strip_comments(text: str) -> str:
    out = []
    for line in text.splitlines():
        s = line.lstrip()
        out.append("" if s.startswith("//") else line)
    return "\n".join(out)


def main() -> int:
    ap = argparse.ArgumentParser(description="okx_funding_archive.py 的离线自检")
    ap.add_argument("--no-e2e", action="store_true", help="不跑端到端（回测）那一项")
    ap.add_argument("--e2e-only", action="store_true", help="只跑端到端")
    ap.add_argument("--e2e-generate", action="store_true", help="端到端不用 user_data/data/okx-synthetic，在临时目录里生成一份")
    a = ap.parse_args()
    global TMP
    t0 = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="okx_funding_check_") as td:
        TMP = Path(td)
        if not a.e2e_only:
            test_columns()
            test_times()
            test_parse_filter()
            test_listing()
            test_network()
            test_family_limit()
            test_time_limit()
            test_merge()
            test_mark()
            test_idempotent()
            test_main_datadir()
            test_from_dir_and_seed()
            test_config_status()
            test_rebuild()
            test_no_api()
            test_overlap_cumulative()
            test_provenance()
            test_gaps()
            test_gate()
        if not a.no_e2e:
            test_e2e(a.e2e_generate)
    print()
    if FAILS:
        print(f"有 {len(FAILS)} 项不对：")
        for f in FAILS:
            print(f"  - {f}")
        return 1
    print(f"OKX 资金费归档导入：全部通过（{time.monotonic() - t0:.0f} 秒）")
    return 0


TMP = Path(tempfile.gettempdir())

if __name__ == "__main__":
    sys.exit(main())
