#!/usr/bin/env python3
"""
OKX 资金费率历史归档导入（第 5 轮 XSFundingCarry 在 OKX 上跑用；2026-11-08 起）。

为什么要它：
  OKX 的公开接口 /api/v5/public/funding-rate-history 只给最近约 3 个月（约 92 天）的资金费，
  freqtrade 下载的 OKX 资金费也就只有这么多。OKX 官方"历史市场数据"页面另外提供
  2022-03 以来的资金费归档文件（https://www.okx.com/en-us/historical-data），
  本脚本把这些归档下载下来，合并进 freqtrade 的资金费文件，让 XSFundingCarry 能在 OKX 数据上回测 2022-04 以后的行情。

只写进单独的数据目录（默认按配置，configs/config.okx-research-funding.json = user_data/data/okx-funding）：
  !! 绝不往 user_data/data/okx 里写：那是运行 A / B 和 NFI 回测用的数据目录，加进去会悄悄改变它们的结果。
     数据目录指到 user_data/data/okx 时拒绝运行（确实要这样做才加 --allow-main-datadir）。

做了什么（update）：
  1. 资金费：按配置里的交易对（StaticPairList，BASE/USDT:USDT -> 归档里的 BASE-USDT-SWAP），
     列出 / 下载每个月的资金费归档（zip 里是 CSV），缓存在 <数据目录>/okx_funding_archive/raw/<YYYYMM>/<文件名>，
     合并进 <数据目录>/futures/<币>_USDT_USDT-1h-funding_rate.feather（用 freqtrade 自己的读写代码，格式完全一样）。
     - 资金费率用"实际结算的费率"（CSV 的 real_funding_rate 一类的列）：freqtrade 从接口下的 OKX 资金费
       （ccxt 把 realizedRate 当成 fundingRate）就是实际结算的费率。没有这一列时才退回 funding_rate，并大声警告、记进清单。
     - 时间用结算时间（funding_time 一类的列），和 freqtrade 一样取整到分钟（freqtrade.exchange.timeframe_to_floor_freq）。
     - 本地已有的行（freqtrade 从接口下载的）优先，归档只补本地没有的时间点；重叠的部分逐条比较，
       超过 1% 的重叠行差别大于 1e-9 时大声警告（"归档里的费率列可能不是实际结算的费率"），清单里 overlap_mismatch = true。
       重叠核对是累计的：某次一条都没比到（例如 --start / --end 很窄），沿用上次的结论。
     - 以前从归档导入的行，只要在这次的 --start ~ --end 里，就按这次的归档重建（脚本改过、换了费率列，旧的值不会留下来）。
     - 还没有接口资金费的币默认拒绝导入（退出码 1，数据目录不动）：先导入归档、后下载接口数据的话，
       freqtrade 只往文件最后一行后面接着下，两者永远不会重叠，没法核对费率列。确实下不到（例如已经下架）才加 --allow-no-api-funding。
     - 币在交易（有 1h 合约K线）、资金费却超过 24 小时没有一条的洞（例如归档少了一个月）记在清单 funding_gaps，大声提醒。
  2. 标记价格替身：OKX 的标记价格历史也只有最近约 3 个月，freqtrade 回测在没有标记价格的时间点资金费按 0 算。
     所以第一根真实标记价格之前的时间点（只在 --start ~ --end 之内），用同一个币的 1h 合约K线（最新成交价）补上（开高低收照抄，成交量 0）。
     最新价和标记价格一般只差约 0.1%，对资金费金额的影响可以忽略。只补第一根真实标记价格之前的时间点，
     从不覆盖真实的标记价格；每个币补了多少根记在清单里（--no-mark-substitute = 不补）。以前补的替身在这次区间里的按现在的K线重建。
     这个币完全没有真实标记价格、而且K线一直到最近时不补（多半是还没下载标记价格），提示先下载。
     第一根真实标记价格之后标记价格缺了一段（超过 6 小时，例如隔了 3 个月以上才更新）不补，记在清单 mark_gaps，大声提醒。
  3. 写清单 <数据目录>/okx_funding_archive/manifest.json（结构见下面），屏幕上每个币打印一行中文小结。
     另外写来源记录 <数据目录>/okx_funding_provenance.json（哪些资金费来自归档、哪些标记价格是替身、哪个版本的脚本做的），
     放在缓存文件夹外面：删掉 okx_funding_archive/ 或者清单坏了也认得出来。两份都没有、标记价格却比接口能给的长得多时，
     替身按数据认（文件开头连续的"成交量 0、开高低收和合约K线一样"的那一段）；资金费和接口数据重叠得不可能那么长时
     记成"来源不明"，不拿来核对。
  同样的输入再跑一次，数据文件一个字节都不变（没有新东西时根本不写文件）。

用法：
  .venv/bin/python scripts/okx_funding_archive.py probe [--family BTC-USDT] [--month 2023-01] [--lister official|priapi] [--save DIR]
      只发一次列表请求、下载一个文件，打印原始返回（前 2 KB）、找到的文件、zip 里的文件名、CSV 表头和前 3 行、认出来的列。
      第一次在 VPS 上运行前先用它看一眼 OKX 返回的格式（OKX 没有公开写清楚返回格式）。
  .venv/bin/python scripts/okx_funding_archive.py update --config configs/config.okx-research-funding.json
      [--datadir DIR] [--start YYYYMMDD] [--end YYYYMMDD] [--from-dir DIR] [--seed-from DIR]
      [--no-mark-substitute] [--allow-main-datadir] [--allow-no-api-funding] [--lister official|priapi]
      --start 默认 20220301（归档最早的月份），--end 默认今天（UTC）。
      --from-dir DIR：不联网，读 DIR 里（含子目录）的 .zip / .csv / .csv.gz（手工下载的文件、离线自检）。
      --seed-from DIR：导入前先把 DIR/futures 里本数据目录还没有的合约K线文件（*-futures.feather，只有这一种）复制过来，
                       省得重新下载（复制，不用符号链接；已有的文件不碰）。
      --lister official（默认）：GET https://www.okx.com/api/v5/public/market-data-history（module=3 资金费、instType=SWAP、
                       instFamilyList 每次最多 5 个（OKX 文档写 10 个，2026-11-08 VPS 实测超过 5 个返回 code=50025；
                       再遇到这个错误自动把这一批减半重试）、dateAggrType=monthly、每次 19 个月（接口上限 20，留一个月余量），
                       begin / end 按北京时间（UTC+8）的月份写，和 OKX 归档分月的方式一样）。
      --lister priapi：社区脚本用过的旧列表接口 https://www.okx.com/priapi/v5/broker/public/orderRecord?path=cdn/okex/traderecords/swaprate/monthly/YYYYMM
                       （有人说它已经 404，只在官方接口不行时试）。
      每两次请求之间至少隔 0.5 秒，网络出错自动重试（等 1、2、4、8 秒）。
  .venv/bin/python scripts/okx_funding_archive.py seed --config CFG [--datadir DIR] --from DIR
      只做 --seed-from 那一步（make okx-funding-data 在下载前先做）。
  .venv/bin/python scripts/okx_funding_archive.py status --config CFG [--datadir DIR]
      打印清单的小结。
  --config 可以写多次（按顺序叠加，同 freqtrade）。数据目录：--datadir > 配置里的 "datadir" > user_data/data/okx。
  !! freqtrade 自己不认配置里的 "datadir"（只认命令行 --datadir，见 freqtrade/configuration/configuration.py），
     所以调用 freqtrade（download-data / backtesting）时一定要写 --datadir。

退出码：0 = 成功；1 = 参数错误（包括数据目录是 user_data/data/okx、白名单是正则、交易所不是 OKX）；
        2 = 网络出错（数据文件和清单都不动，已经下载好的归档文件留在缓存里，重新运行接着下）；
        3 = 返回的列表 / 文件格式认不出来（屏幕上打印看到的内容）。

清单 manifest.json（schema 1）：
  tool, tool_sha256, data_tool_sha256[]（数据目录里的归档资金费 / 替身是哪些版本的脚本做的；正式运行要求只有现在登记的那一个）,
  updated_at, exchange, config[], datadir, start, end（YYYYMMDD，这次导入的区间）, lister（official|priapi|from-dir）,
  from_dir, funding_timeframe, mark_timeframe, mark_candle_type, mark_substitute（这次有没有补标记价格）,
  rate_column（real_funding_rate / funding_rate / ... / mixed）, rate_columns{列名: 文件数}, rate_is_realized,
  time_column, instrument_column, files[{url, local_path, size, sha256, month, members[], rows, rows_kept,
  rate_column, time_column, instrument_column}], files_skipped_other_family,
  pairs{交易对: {family, funding_rows, funding_first, funding_last, funding_median_interval_h, funding_max_gap_h,
         funding_gaps[[开始, 结束, 小时]], api_rows（不是归档的行）,
         archive_rows（这次归档里这个币在区间内的条数）, archive_rows_added（文件里来自归档的条数，累计）,
         archive_rows_added_this_run, archive_rows_replaced_this_run, archive_rows_removed_this_run（按这次的归档重建）,
         archive_rows_kept_outside_run, archive_added_ranges[[开始, 结束, 条数]], provenance_unknown_rows, provenance_unknown_before,
         overlap_rows, overlap_diff_rows, overlap_max_abs_diff, overlap_mismatch, overlap_carried_from（沿用的是哪次的核对）,
         mark_rows, mark_first, mark_last, first_real_mark, mark_substitute_rows（文件里的替身根数，累计）,
         mark_substitute_added_this_run, mark_substitute_replaced_this_run, mark_substitute_removed_this_run, mark_substitute_kept_rows,
         mark_substitute_ranges[[开始, 结束, 根数]], mark_gaps[[开始, 结束, 小时]], notes[]}},
  overlap_rows_total, overlap_diff_rows_total, overlap_checked, overlap_mismatch, overlap_mismatch_pairs[], no_overlap_pairs[],
  allow_no_api_funding, pairs_without_api_funding[], funding_gap_pairs[], mark_gap_pairs[], provenance_unknown_pairs[],
  warnings[], last_run{..., tool_sha256}, runs[最近 20 次的摘要，每次带 tool_sha256]。时间都是 UTC，写成 2022-03-01T00:00:00Z。
  "来自归档的行" / "替身行" 按来源记录 / 清单里记的时间段认（文件被删掉重下时自动重新算）。
"""

from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import http.client
import io
import json
import logging
import math
import os
import re
import shutil
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

ROOT_DIR = Path(__file__).resolve().parent.parent
MAIN_DATADIR_REL = Path("user_data") / "data" / "okx"
DEFAULT_DATADIR = MAIN_DATADIR_REL
DEFAULT_START = "20220301"  # OKX 官方：资金费归档从 2022 年 3 月开始
ARCHIVE_SUBDIR = "okx_funding_archive"
MANIFEST_NAME = "manifest.json"
# 来源记录（哪些资金费来自归档、哪些标记价格是替身、用哪个版本的脚本做的）另外存一份在数据目录最上层，
# 不放在缓存文件夹里：删掉 okx_funding_archive/（省空间）或者清单坏了，也认得出哪些行是归档 / 替身
PROVENANCE_NAME = "okx_funding_provenance.json"
SCHEMA_VERSION = 1

OFFICIAL_URL = "https://www.okx.com/api/v5/public/market-data-history"
PRIAPI_URL = "https://www.okx.com/priapi/v5/broker/public/orderRecord"
PRIAPI_PATH = "cdn/okex/traderecords/swaprate/monthly/{month}"
PRIAPI_FILE_BASE = "https://static.okx.com/cdn/okex/traderecords/swaprate/monthly/{month}/"
LISTERS = ("official", "priapi")
MODULE_FUNDING = "3"
# 官方接口：instFamilyList 文档写最多 10 个，可是 2026-11-08 在 VPS 上实测 10 个返回
# code=50025 "Number of instFamilyList values exceeds the maximum limit"，所以每次只放 5 个；
# 以后 OKX 再收紧，遇到 50025 自动减半重试（list_remote）
MAX_FAMILIES = 5
FAMILY_LIMIT_CODES = ("50025",)
# 官方接口：dateAggrType=monthly 时最多 20 个月。每次只要 19 个月，留一个月余量
# （OKX 的归档按北京时间 UTC+8 分月，请求的起止也按 UTC+8 的月份写，见 _month_bounds_ms_cn）
MAX_MONTHS = 19
CN_OFFSET_MS = 8 * 3_600_000  # 北京时间比 UTC 早 8 小时
MIN_INTERVAL = 0.5  # 两次请求之间至少隔这么多秒
RETRIES = 4  # 网络出错重试次数（等 1、2、4、8 秒）
TIMEOUT = 60
MAX_DOWNLOAD = 300 * 1024 * 1024  # 单个文件最大 300 MB（防止异常大的返回）
MAX_MEMBER = 1024 * 1024 * 1024  # zip 里单个 CSV 解压后最大 1 GB
USER_AGENT = "Mozilla/5.0 (compatible; EasyTrade-okx_funding_archive/1)"
RAW_PRINT = 2000  # probe / 格式错误时打印多少字符

OVERLAP_TOL = 1e-9
OVERLAP_MAX_SHARE = 0.01
# 清单丢了（或读不了）时：资金费文件从头到尾超过这么多天 = 里面多半有以前从归档导入的行（接口只给最近约 92 天），
# 分不清哪些是归档的，比这更早的行"来源不明"，不拿来做重叠核对（见 merge_funding）
PROVENANCE_SPAN_DAYS = 120
# 缺口：币在交易（有 1h 合约K线）、资金费却超过这么多小时没有一条 = 数据有洞（例如归档少了一个月）
FUNDING_GAP_H = 24
# 缺口：第一根真实标记价格之后，币在交易、标记价格却连续缺这么多小时以上（例如过了 3 个月才更新，中间那段接口已经不给了）
MARK_GAP_H = 6
# 标记价格文件从头到尾超过这么多天 = 比 OKX 接口能给的（约 3 个月）长得多，里面有以前补的替身（记录丢了时按数据认，见 mark_substitute）
MARK_SPAN_DAYS = 100
# 完全没有真实标记价格的币：K线最后一根比 --end 早这么多天以上（多半是已下架，真实标记价格永远下不到）才补替身
NO_MARK_STALE_DAYS = 100
RUNS_KEPT = 20

EXIT_OK, EXIT_ARGS, EXIT_NET, EXIT_FORMAT = 0, 1, 2, 3
# 清单里每个币的字段（顺序固定）
PAIR_KEYS = (
    "family", "funding_rows", "funding_first", "funding_last", "funding_median_interval_h", "funding_max_gap_h", "funding_gaps",
    "api_rows", "archive_rows", "archive_rows_added", "archive_rows_added_this_run", "archive_rows_replaced_this_run",
    "archive_rows_removed_this_run", "archive_rows_kept_outside_run", "archive_added_ranges",
    "provenance_unknown_rows", "provenance_unknown_before",
    "overlap_rows", "overlap_diff_rows", "overlap_max_abs_diff", "overlap_mismatch", "overlap_carried_from",
    "mark_rows", "mark_first", "mark_last", "first_real_mark", "mark_substitute_rows", "mark_substitute_added_this_run",
    "mark_substitute_replaced_this_run", "mark_substitute_removed_this_run", "mark_substitute_kept_rows",
    "mark_substitute_ranges", "mark_gaps", "notes",
)

# CSV 列名（不分大小写，去掉空格 / 下划线 / 横线后比较）。按顺序优先。
INST_ALIASES = ("instrumentname", "instid", "instrumentid", "symbol", "instrument", "instname", "合约名称", "合约", "产品名称")
TIME_ALIASES = ("fundingtime", "fundingts", "fundingtimestamp", "settletime", "settlementtime", "结算时间", "资金费时间")
TIME_FALLBACK = ("timestamp", "ts", "time", "createdtime")
RATE_REALIZED = ("realfundingrate", "realizedrate", "realizedfundingrate", "realrate", "实际资金费率")
RATE_FALLBACK = ("fundingrate", "资金费率")
SIZE_KEYS = ("filesize", "size", "contentlength", "bytes", "filesizebytes")
NAME_KEYS = ("filename", "name", "file", "filekey")
DATE_KEYS = ("datets", "date", "ts", "month", "begin", "datestart", "daterangestart", "start")
FILE_SUFFIXES = (".zip", ".csv", ".csv.gz", ".gz")

logger = logging.getLogger("okx_funding_archive")


# ----------------------------------------------------------------------------
# 错误类型（对应退出码）
# ----------------------------------------------------------------------------
class ArgError(Exception):
    """参数错误 -> 退出码 1"""


class NetError(Exception):
    """网络出错 -> 退出码 2"""


class FormatError(Exception):
    """格式认不出来 -> 退出码 3。seen = 看到的内容（打印前 2 KB）"""

    def __init__(self, msg: str, seen: str | bytes | None = None) -> None:
        super().__init__(msg)
        self.seen = seen


class ApiError(FormatError):
    """OKX 返回了 code != 0（例如 50025 = instFamilyList 太多）。仍然算"格式 / 请求不对" -> 退出码 3"""

    def __init__(self, msg: str, code: str, api_msg: str, seen: str | bytes | None = None) -> None:
        super().__init__(msg, seen=seen)
        self.code, self.api_msg = code, api_msg


class HttpStatus(Exception):
    def __init__(self, status: int, body: bytes, url: str) -> None:
        super().__init__(f"HTTP {status}: {url}")
        self.status, self.body, self.url = status, body, url


# ----------------------------------------------------------------------------
# 小工具
# ----------------------------------------------------------------------------
def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def tool_sha256() -> str:
    return _sha256_file(Path(__file__).resolve())


def _iso(ms: int | None) -> str | None:
    if ms is None:
        return None
    return datetime.fromtimestamp(ms / 1000, tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _iso_to_ms(s: str) -> int:
    return int(datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp() * 1000)


def _ymd(s: str, what: str) -> datetime:
    if not re.fullmatch(r"\d{8}", s or ""):
        raise ArgError(f"{what} 要写成 YYYYMMDD（收到：{s!r}）")
    try:
        return datetime.strptime(s, "%Y%m%d").replace(tzinfo=UTC)
    except ValueError:
        raise ArgError(f"{what} 不是有效日期：{s}") from None


def _rel(path: Path) -> str:
    """能写成相对项目根目录的路径就写相对的（清单里好看，换机器也对得上）。"""
    p = Path(os.path.abspath(path))
    try:
        return str(p.relative_to(ROOT_DIR))
    except ValueError:
        return str(p)


def _show(raw: str | bytes | None, limit: int = RAW_PRINT) -> str:
    if raw is None:
        return "（没有内容）"
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="replace")
    return raw if len(raw) <= limit else raw[:limit] + f"\n……（共 {len(raw)} 个字符，只显示前 {limit} 个）"


def _compact(name: str) -> str:
    """列名比较用：去掉 BOM、空格、下划线、横线、点，转小写（中文原样保留）。"""
    return re.sub(r"[\s_\-\.﻿]+", "", str(name)).lower()


def _months(start: datetime, end: datetime) -> list[str]:
    out, y, m = [], start.year, start.month
    while (y, m) <= (end.year, end.month):
        out.append(f"{y:04d}{m:02d}")
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


def _month_bounds_ms(month: str) -> tuple[int, int]:
    """YYYYMM -> (这个月第一毫秒, 这个月最后一毫秒)，UTC。"""
    y, m = int(month[:4]), int(month[4:])
    a = datetime(y, m, 1, tzinfo=UTC)
    b = datetime(y + (m == 12), 1 if m == 12 else m + 1, 1, tzinfo=UTC)
    return int(a.timestamp() * 1000), int(b.timestamp() * 1000) - 1


def _month_bounds_ms_cn(month: str) -> tuple[int, int]:
    """YYYYMM -> 这个月按北京时间（UTC+8）的第一毫秒和最后一毫秒（写成 UTC 毫秒）。
    OKX 的归档按北京时间分日 / 分月（每天从 UTC 前一天 16:00 开始），列表请求的 begin / end 也按这个写，
    免得按 dateTs 过滤时漏掉第一个月（多出来的几个小时由 --start / --end 的过滤去掉）。"""
    a, b = _month_bounds_ms(month)
    return a - CN_OFFSET_MS, b - CN_OFFSET_MS


def _chunks(seq: list, n: int) -> list[list]:
    return [seq[i : i + n] for i in range(0, len(seq), n)]


def _safe_name(name: str) -> str:
    """缓存文件名：只留最后一段，去掉奇怪的字符（防止 ../ 之类）。"""
    base = name.replace("\\", "/").rsplit("/", 1)[-1]
    base = re.sub(r"[^A-Za-z0-9._-]+", "_", base).strip("._") or "file"
    return base[:200]


def month_from_text(*texts: str | None) -> str | None:
    """从文件名 / URL 里找 YYYY-MM / YYYYMM / YYYYMMDD（找到第一个就算）。"""
    for t in texts:
        if not t:
            continue
        m = re.search(r"(?<!\d)(20\d{2})[-_]?(0[1-9]|1[0-2])(?:[-_]?(?:0[1-9]|[12]\d|3[01]))?(?!\d)", t)
        if m:
            return m.group(1) + m.group(2)
    return None


def family_from_text(text: str) -> str | None:
    """文件名里写明的交易对家族（BTC-USDT-SWAP... -> BTC-USDT）；没写明返回 None。"""
    m = re.search(r"(?<![A-Z0-9])([A-Z0-9]{1,30}-USDT)-SWAP(?![A-Z0-9])", text.upper())
    return m.group(1) if m else None


# ----------------------------------------------------------------------------
# 网络（测试里替换 Http._raw_get / sleep / clock，不联网）
# ----------------------------------------------------------------------------
class Http:
    def __init__(
        self,
        min_interval: float = MIN_INTERVAL,
        retries: int = RETRIES,
        timeout: float = TIMEOUT,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.min_interval = max(MIN_INTERVAL, float(min_interval))
        self.retries = int(retries)
        self.timeout = timeout
        self._sleep = sleep
        self._clock = clock
        self._last: float | None = None
        self.requests = 0
        self.urls: list[str] = []

    def _raw_get(self, url: str) -> tuple[int, bytes]:
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "*/*"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:  # 只访问写死的 https 地址（get 里检查）
                body = r.read(MAX_DOWNLOAD + 1)
                return int(r.status), body
        except urllib.error.HTTPError as e:
            try:
                body = e.read(64 * 1024)
            except Exception:  # noqa: BLE001
                body = b""
            return int(e.code), body

    def _throttle(self) -> None:
        if self._last is not None:
            wait = self.min_interval - (self._clock() - self._last)
            if wait > 0:
                self._sleep(wait)
        self._last = self._clock()

    def get(self, url: str, params: dict[str, str] | None = None) -> bytes:
        if not url.startswith("https://"):
            raise ArgError(f"只访问 https 地址（收到：{url}）")
        # 逗号不转义（OKX 文档的例子就是 instFamilyList=BTC-USDT,ETH-USDT）
        full = url + ("?" + urllib.parse.urlencode(params, safe=",") if params else "")
        err = ""
        for attempt in range(self.retries + 1):
            self._throttle()
            self.requests += 1
            self.urls.append(full)
            try:
                status, body = self._raw_get(full)
            except (OSError, http.client.HTTPException) as e:  # URLError / 超时 / 连接断开 / SSL 都是 OSError
                err = f"{type(e).__name__}: {e}"
            else:
                if 200 <= status < 300:
                    if len(body) > MAX_DOWNLOAD:
                        raise FormatError(f"返回的内容超过 {MAX_DOWNLOAD // (1024 * 1024)} MB：{full}")
                    return body
                if status in (401, 403, 407):
                    raise NetError(
                        f"HTTP {status}（被拒绝访问：可能是地区限制、代理或防火墙）：{full}\n"
                        f"   返回内容：{_show(body, 300)}"
                    )
                if status == 429 or status >= 500:
                    err = f"HTTP {status}"
                else:
                    raise HttpStatus(status, body, full)
            if attempt < self.retries:
                wait = min(30.0, float(2**attempt) * (3.0 if err.startswith("HTTP 429") else 1.0))
                print(f"  · 网络出错（{err}），{wait:.0f} 秒后重试（第 {attempt + 1}/{self.retries} 次）", flush=True)
                self._sleep(wait)
        raise NetError(f"{full}：{err}（重试 {self.retries} 次仍然失败）")


# ----------------------------------------------------------------------------
# 列表（返回格式 OKX 没有公开写清楚：遍历整个 JSON，凡是带 https://...zip/csv 的字典都算一个文件）
# ----------------------------------------------------------------------------
@dataclass
class FileEntry:
    url: str
    file_name: str
    size: int | None = None
    month: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


def _looks_like_file(s: str) -> bool:
    path = urllib.parse.urlparse(s).path if s.startswith(("http://", "https://")) else s
    return path.lower().endswith(FILE_SUFFIXES)


def _int_size(v: Any) -> int | None:
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v if v >= 0 else None
    if isinstance(v, str) and re.fullmatch(r"\d+", v.strip()):
        return int(v.strip())
    return None


def _month_from_value(v: Any) -> str | None:
    """日期类字段 -> YYYYMM。毫秒时间戳按北京时间（OKX 归档按 UTC+8 分日 / 分月）算。"""
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        v = str(int(v))
    if not isinstance(v, str):
        return None
    s = v.strip()
    if re.fullmatch(r"\d{12,14}", s):  # 毫秒
        d = datetime.fromtimestamp(int(s) / 1000, tz=UTC) + timedelta(hours=8)
        return f"{d.year:04d}{d.month:02d}"
    if re.fullmatch(r"\d{9,11}", s):  # 秒
        d = datetime.fromtimestamp(int(s), tz=UTC) + timedelta(hours=8)
        return f"{d.year:04d}{d.month:02d}"
    return month_from_text(s)


def _entry_from_dict(url: str, d: dict[str, Any]) -> FileEntry:
    low = {_compact(k): v for k, v in d.items()}
    name = None
    for k in NAME_KEYS:
        v = low.get(k)
        if isinstance(v, str) and v.strip() and not v.startswith("http"):
            name = v.strip()
            break
    if name is None:
        name = urllib.parse.unquote(urllib.parse.urlparse(url).path.rsplit("/", 1)[-1]) if url.startswith("http") else url
    size = None
    for k in SIZE_KEYS:
        if k in low:
            size = _int_size(low[k])
            if size is not None:
                break
    month = month_from_text(name, urllib.parse.urlparse(url).path)
    if month is None:
        for k in DATE_KEYS:
            if k in low:
                month = _month_from_value(low[k])
                if month:
                    break
    extra = {k: v for k, v in d.items() if isinstance(v, (str, int, float, bool)) or v is None}
    extra = {k: (v[:200] if isinstance(v, str) else v) for k, v in list(extra.items())[:20]}
    return FileEntry(url=url, file_name=name, size=size, month=month, extra=extra)


def _check_api_code(doc: Any, raw: bytes) -> None:
    if isinstance(doc, dict) and "code" in doc:
        code = str(doc.get("code"))
        if code not in ("0", "", "None", "200"):
            api_msg = str(doc.get("msg") or "")
            raise ApiError(f"OKX 返回错误：code={code} msg={api_msg!r}", code=code, api_msg=api_msg, seen=raw)


def walk_json(doc: Any) -> Iterable[tuple[str, dict[str, Any] | None]]:
    """遍历 JSON：产出 (字符串值, 它所在的字典或 None)，只要看起来像文件（.zip / .csv / .csv.gz）的字符串。"""
    stack: list[Any] = [doc]
    while stack:
        cur = stack.pop(0)
        if isinstance(cur, dict):
            for v in cur.values():
                if isinstance(v, str) and _looks_like_file(v):
                    yield v, cur
                elif isinstance(v, (dict, list)):
                    stack.append(v)
        elif isinstance(cur, list):
            for v in cur:
                if isinstance(v, str) and _looks_like_file(v):
                    yield v, None
                elif isinstance(v, (dict, list)):
                    stack.append(v)


def parse_official_listing(raw: bytes) -> list[FileEntry]:
    """官方 market-data-history 的返回 -> 文件列表。认不出来时 FormatError。"""
    try:
        doc = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise FormatError("列表返回的不是 JSON", seen=raw) from None
    _check_api_code(doc, raw)
    out: dict[str, FileEntry] = {}
    for s, parent in walk_json(doc):
        if not s.startswith("https://"):
            continue
        if s in out:
            continue
        out[s] = _entry_from_dict(s, parent) if parent is not None else _entry_from_dict(s, {})
    return list(out.values())


def parse_priapi_listing(raw: bytes, month: str) -> list[FileEntry]:
    """旧的 priapi 列表 -> 文件列表（只给文件名，地址按 static.okx.com/.../swaprate/monthly/YYYYMM/<文件名> 拼）。"""
    try:
        doc = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise FormatError("列表返回的不是 JSON", seen=raw) from None
    _check_api_code(doc, raw)
    out: dict[str, FileEntry] = {}
    for s, parent in walk_json(doc):
        if s.startswith("https://"):
            url = s
        elif s.startswith("http://"):
            continue
        else:
            url = PRIAPI_FILE_BASE.format(month=month) + urllib.parse.quote(s.rsplit("/", 1)[-1])
        if url in out:
            continue
        e = _entry_from_dict(url, parent or {})
        if e.month is None:
            e.month = month
        out[url] = e
    return list(out.values())


def _official_params(fam: list[str], mon: list[str], now_ms: int) -> dict[str, str] | None:
    begin = _month_bounds_ms_cn(mon[0])[0]
    end = min(_month_bounds_ms_cn(mon[-1])[1], now_ms)
    if end < begin:
        return None
    return {
        "module": MODULE_FUNDING,
        "instType": "SWAP",
        "instFamilyList": ",".join(fam),
        "dateAggrType": "monthly",
        "begin": str(begin),
        "end": str(end),
    }


def _official_batches(families: list[str], months: list[str], max_families: int) -> list[tuple[list[str], list[str]]]:
    return [(fam, mon) for fam in _chunks(sorted(families), max_families) for mon in _chunks(months, MAX_MONTHS)]


def official_requests(families: list[str], months: list[str], now_ms: int,
                      max_families: int = MAX_FAMILIES) -> list[dict[str, str]]:
    reqs = []
    for fam, mon in _official_batches(families, months, max_families):
        params = _official_params(fam, mon, now_ms)
        if params is not None:
            reqs.append(params)
    return reqs


def _is_family_limit(e: "ApiError") -> bool:
    return e.code in FAMILY_LIMIT_CODES or "instfamilylist" in e.api_msg.lower()


def list_remote(http: Http, lister: str, families: list[str], months: list[str], now_ms: int) -> list[FileEntry]:
    entries: dict[str, FileEntry] = {}
    if lister == "official":
        cap = MAX_FAMILIES
        queue = _official_batches(families, months, cap)
        print(f"列出归档文件：官方接口，约 {len(queue)} 次请求（每次最多 {cap} 个币、{MAX_MONTHS} 个月）")
        i = 0
        while queue:
            fam, mon = queue.pop(0)
            params = _official_params(fam, mon, now_ms)
            if params is None:
                continue
            i += 1
            try:
                raw = http.get(OFFICIAL_URL, params)
            except HttpStatus as e:
                raise FormatError(f"列表接口返回 HTTP {e.status}：{e.url}", seen=e.body) from None
            try:
                got = parse_official_listing(raw)
            except ApiError as e:
                if not (_is_family_limit(e) and len(fam) > 1):
                    raise
                # OKX 嫌一次的币太多（code=50025）：这一批和后面还没发的都按一半重新分批
                cap = max(1, len(fam) // 2)
                print(f"  · 第 {i} 次：OKX 说 instFamilyList 太多（code={e.code}，这一批 {len(fam)} 个币），"
                      f"改成每次最多 {cap} 个币重试")
                rest = [(fam, mon)] + queue
                queue = [(f2, m2) for f1, m2 in rest for f2 in _chunks(f1, cap)]
                continue
            if not got:
                print(f"  · 第 {i} 次：没有文件（{params['instFamilyList'][:60]}…，"
                      f"{_iso(int(params['begin']))[:7]} ~ {_iso(int(params['end']))[:7]}）")
            for e in got:
                entries.setdefault(e.url, e)
        print(f"  列表请求共 {i} 次（最后每次最多 {cap} 个币）")
    else:
        print(f"列出归档文件：旧的 priapi 接口，{len(months)} 次请求（每个月一次）")
        for mon in months:
            try:
                raw = http.get(PRIAPI_URL, {"path": PRIAPI_PATH.format(month=mon)})
            except HttpStatus as e:
                raise FormatError(f"priapi 列表接口返回 HTTP {e.status}：{e.url}", seen=e.body) from None
            for e in parse_priapi_listing(raw, mon):
                entries.setdefault(e.url, e)
    return sorted(entries.values(), key=lambda e: (e.month or "999999", e.file_name, e.url))


def download_entries(http: Http, entries: list[FileEntry], raw_dir: Path, families: set[str]) -> tuple[list[dict], int]:
    """下载（或复用缓存）。返回 (文件记录, 因为文件名写明是别的币而跳过的个数)。网络出错 NetError。"""
    todo, skipped = [], 0
    for e in entries:
        fam = family_from_text(e.file_name) or family_from_text(urllib.parse.urlparse(e.url).path)
        if fam is not None and fam not in families:
            skipped += 1
            continue
        todo.append(e)
    have = 0
    plan = []
    for e in todo:
        name = _safe_name(e.file_name)
        if e.month is None:  # 认不出月份：放在 unknown/，文件名前面加地址的短哈希，免得重名
            name = hashlib.sha1(e.url.encode()).hexdigest()[:8] + "_" + name
        local = raw_dir / (e.month or "unknown") / name
        if any(local == x for _, x in plan):  # 同一个文件给了两个地址（例如带不同的签名参数）：只要一份
            continue
        plan.append((e, local))
        if _cache_ok(local, e.size):
            have += 1
    need = len(plan) - have
    print(f"归档文件 {len(plan)} 个：本地缓存已有 {have} 个，要下载 {need} 个"
          + (f"（每个至少 {MIN_INTERVAL} 秒，预计 {need * (MIN_INTERVAL + 0.3) / 60:.0f} 分钟以上）" if need else "")
          + (f"；文件名写明是别的币、跳过 {skipped} 个" if skipped else ""), flush=True)
    records, done = [], 0
    for e, local in plan:
        if not _cache_ok(local, e.size):
            try:
                body = http.get(e.url)
            except HttpStatus as ex:
                raise FormatError(f"下载归档文件返回 HTTP {ex.status}：{e.url}", seen=ex.body) from None
            if e.size is not None and len(body) != e.size:
                raise NetError(f"下载不完整：{e.url}（应为 {e.size} 字节，收到 {len(body)} 字节）")
            local.parent.mkdir(parents=True, exist_ok=True)
            tmp = local.with_name(local.name + ".part")
            tmp.write_bytes(body)
            os.replace(tmp, local)
            done += 1
            if done % 50 == 0:
                print(f"  · 已下载 {done}/{need}", flush=True)
        records.append({"url": e.url, "path": local, "month": e.month, "listed_size": e.size, "extra": e.extra})
    return records, skipped


def _cache_ok(local: Path, size: int | None) -> bool:
    if not local.is_file() or local.is_symlink():
        return False
    n = local.stat().st_size
    if size is not None:
        return n == size
    if n == 0:
        return False
    if local.name.lower().endswith(".zip"):
        return zipfile.is_zipfile(local)
    return True


def list_local(src: Path) -> list[dict]:
    if not src.is_dir():
        raise ArgError(f"--from-dir {src} 不是目录")
    files = []
    for p in sorted(src.rglob("*")):
        if not p.is_file() or p.name.startswith(".") or "__MACOSX" in p.parts:
            continue
        if not p.name.lower().endswith(FILE_SUFFIXES):
            continue
        files.append({"url": None, "path": p, "month": month_from_text(p.name, str(p.parent)), "listed_size": None, "extra": {}})
    return files


# ----------------------------------------------------------------------------
# 解析 CSV
# ----------------------------------------------------------------------------
@dataclass
class Columns:
    inst: int | None
    time: int
    rate: int
    inst_name: str | None
    time_name: str
    rate_name: str
    realized: bool
    time_fallback: bool


def detect_columns(header: list[str]) -> Columns:
    """按列名认出 合约 / 结算时间 / 资金费率 三列。认不出来 FormatError。"""
    comp = [_compact(h) for h in header]

    def find(aliases: tuple[str, ...]) -> int | None:
        for a in aliases:
            if a in comp:
                return comp.index(a)
        return None

    inst = find(INST_ALIASES)
    t = find(TIME_ALIASES)
    t_fb = False
    if t is None:
        t = find(TIME_FALLBACK)
        t_fb = t is not None
    r = find(RATE_REALIZED)
    realized = r is not None
    if r is None:
        r = find(RATE_FALLBACK)
    if t is None or r is None:
        missing = ("结算时间" if t is None else "") + ("、" if t is None and r is None else "") + ("资金费率" if r is None else "")
        raise FormatError(f"CSV 表头认不出{missing}列：{header}", seen=",".join(header))
    return Columns(
        inst=inst, time=t, rate=r,
        inst_name=header[inst] if inst is not None else None,
        time_name=header[t], rate_name=header[r], realized=realized, time_fallback=t_fb,
    )


def parse_time_ms(value: str) -> tuple[int, bool]:
    """时间 -> (UTC 毫秒, 是否"没写时区、按 UTC 算")。支持 秒 / 毫秒 / 微秒 / 纳秒整数和 ISO 字符串。认不出 ValueError。"""
    s = str(value).strip()
    if not s:
        raise ValueError("空的时间")
    if re.fullmatch(r"-?\d+(\.\d+)?", s):
        x = float(s) if "." in s else int(s)
        ax = abs(x)
        if ax < 1e11:
            ms = int(round(x * 1000))
        elif ax < 1e14:
            ms = int(x)
        elif ax < 1e17:
            ms = int(x // 1000)
        else:
            ms = int(x // 1_000_000)
        return ms, False
    s2 = s.replace("Z", "+00:00").replace("z", "+00:00")
    if re.match(r"^\d{4}/\d{2}/\d{2}", s2):
        s2 = s2.replace("/", "-", 2)
    # 去掉 " UTC" / "UTC+8" 这种写法
    m = re.fullmatch(r"(.*?)[ ]?UTC([+-]\d{1,2})?", s2)
    if m:
        off = int(m.group(2) or 0)
        s2 = m.group(1) + f"{'+' if off >= 0 else '-'}{abs(off):02d}:00"
    d = datetime.fromisoformat(s2)
    naive = d.tzinfo is None
    if naive:
        d = d.replace(tzinfo=UTC)
    return int(d.timestamp() * 1000), naive


@dataclass
class Table:
    name: str
    header: list[str]
    columns: Columns
    rows: list[tuple[str, int, float]]  # (family, ms, rate)，已经按交易对家族和区间过滤
    n_rows: int
    n_bad: int
    n_naive_time: int
    inferred_family: str | None


def _decode(raw: bytes) -> str:
    for enc in ("utf-8-sig", "gb18030"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("latin-1")


def parse_csv_text(text: str, name: str, families: set[str] | None, start_ms: int | None, end_ms: int | None,
                   floor_ms: int = 60_000) -> Table:
    lines = text.splitlines()
    first = next((ln for ln in lines if ln.strip()), "")
    if not first:
        raise FormatError(f"{name}：CSV 是空的", seen=text[:RAW_PRINT])
    delim = max((",", ";", "\t", "|"), key=lambda c: first.count(c))
    if first.count(delim) == 0:
        delim = ","
    reader = csv.reader(io.StringIO(text), delimiter=delim)
    header: list[str] | None = None
    for row in reader:
        if any(c.strip() for c in row):
            header = [c.strip() for c in row]
            break
    if header is None:
        raise FormatError(f"{name}：CSV 是空的", seen=text[:RAW_PRINT])
    if not any(re.search(r"[A-Za-z一-鿿]", c) for c in header):
        raise FormatError(f"{name}：第一行不像表头（没有列名）：{header}", seen=text[:RAW_PRINT])
    try:
        cols = detect_columns(header)
    except FormatError as e:
        e.seen = "\n".join(lines[:4])
        raise
    inferred = None
    if cols.inst is None:
        inferred = family_from_text(name)
        if inferred is None:
            raise FormatError(f"{name}：CSV 里没有合约列（{'/'.join(INST_ALIASES[:5])} ...），文件名里也没写是哪个币：{header}",
                              seen="\n".join(lines[:4]))
    rows: list[tuple[str, int, float]] = []
    n = bad = naive = 0
    need = max(cols.time, cols.rate, cols.inst if cols.inst is not None else 0)
    for row in reader:
        if not any(c.strip() for c in row):
            continue
        n += 1
        if len(row) <= need:
            bad += 1
            continue
        if cols.inst is not None:
            inst = row[cols.inst].strip().upper()
            m = re.fullmatch(r"([A-Z0-9]+-[A-Z0-9]+)-SWAP", inst)
            if not m:
                continue  # 交割合约、期权等
            fam = m.group(1)
        else:
            fam = inferred  # type: ignore[assignment]
        if families is not None and fam not in families:
            continue
        try:
            ms, is_naive = parse_time_ms(row[cols.time])
            rate = float(row[cols.rate].strip())
        except (ValueError, OverflowError):
            bad += 1
            continue
        if math.isnan(rate) or math.isinf(rate):
            bad += 1
            continue
        naive += int(is_naive)
        ms -= ms % floor_ms
        if start_ms is not None and ms < start_ms:
            continue
        if end_ms is not None and ms >= end_ms:
            continue
        rows.append((fam, ms, rate))
    if n > 0 and bad == n:
        raise FormatError(f"{name}：{n} 行全部读不出时间或费率", seen="\n".join(lines[:4]))
    return Table(name=name, header=header, columns=cols, rows=rows, n_rows=n, n_bad=bad, n_naive_time=naive,
                 inferred_family=inferred)


def read_archive_file(path: Path) -> list[tuple[str, str]]:
    """归档文件 -> [(zip 里的文件名, CSV 文本)]。zip 只在内存里读，不解压到磁盘。"""
    low = path.name.lower()
    raw = path.read_bytes()
    if low.endswith(".zip") or raw[:4] == b"PK\x03\x04":
        try:
            zf = zipfile.ZipFile(io.BytesIO(raw))
        except zipfile.BadZipFile:
            raise FormatError(f"{path.name}：不是有效的 zip 文件", seen=raw[:RAW_PRINT]) from None
        infos = [i for i in zf.infolist() if not i.is_dir() and "__MACOSX" not in i.filename]
        csvs = [i for i in infos if i.filename.lower().endswith((".csv", ".csv.gz"))] or infos
        if not csvs:
            raise FormatError(f"{path.name}：zip 里没有文件", seen=str([i.filename for i in zf.infolist()]))
        out = []
        for i in csvs:
            if i.file_size > MAX_MEMBER:
                raise FormatError(f"{path.name}：{i.filename} 解压后 {i.file_size} 字节，太大")
            data = zf.read(i)
            if i.filename.lower().endswith(".gz"):
                data = gzip.decompress(data)
            out.append((i.filename, _decode(data)))
        return out
    if low.endswith(".gz") or raw[:2] == b"\x1f\x8b":
        return [(path.name, _decode(gzip.decompress(raw)))]
    return [(path.name, _decode(raw))]


# ----------------------------------------------------------------------------
# 配置 / 数据目录
# ----------------------------------------------------------------------------
@dataclass
class Context:
    configs: list[str]
    pairs: list[str]
    families: dict[str, str]  # family -> pair
    datadir: Path
    datadir_txt: str
    fmt: str
    funding_tf: str
    mark_tf: str
    mark_type: Any
    floor_ms: int


def _load_config(paths: list[str]) -> dict:
    for p in paths:
        if not Path(p).is_file():
            raise ArgError(f"找不到配置文件 {p}")
    from freqtrade.configuration.load_config import load_from_files

    try:
        return load_from_files(list(paths))
    except Exception as e:  # noqa: BLE001
        raise ArgError(f"读不了配置 {paths}：{e}") from None


def is_main_datadir(path: Path) -> bool:
    p = Path(os.path.abspath(path)).resolve()
    for base in (ROOT_DIR, Path.cwd()):
        if p == (base / MAIN_DATADIR_REL).resolve():
            return True
    return False


def resolve_context(configs: list[str], datadir_arg: str | None, allow_main: bool) -> Context:
    if not configs:
        raise ArgError("要写 --config（例如 configs/config.okx-research-funding.json）")
    cfg = _load_config(configs)
    ex = cfg.get("exchange") or {}
    name = str(ex.get("name") or "").lower()
    if name != "okx":
        raise ArgError(f"配置里的交易所是 {name or '（没写）'}，本脚本只用于 OKX")
    if str(cfg.get("trading_mode") or "") != "futures":
        raise ArgError("配置的 trading_mode 不是 futures（资金费只有合约有）")
    methods = [str((p or {}).get("method")) for p in (cfg.get("pairlists") or [{"method": "StaticPairList"}])]
    if not methods or methods[0] != "StaticPairList":
        raise ArgError(f"只支持 StaticPairList 的配置（收到：{methods}），交易对要写死在 pair_whitelist 里")
    pairs = [str(p) for p in (ex.get("pair_whitelist") or [])]
    if not pairs:
        raise ArgError("配置里的 pair_whitelist 是空的")
    regex = [p for p in pairs if any(c in p for c in "*?[]()^$|+\\")]
    if regex:
        raise ArgError(f"pair_whitelist 里有正则（{regex[:3]}），本脚本只认写死的交易对（例如 BTC/USDT:USDT）")
    families: dict[str, str] = {}
    for p in pairs:
        m = re.fullmatch(r"([A-Z0-9]+)/USDT:USDT", p)
        if not m:
            raise ArgError(f"交易对 {p} 不是 BASE/USDT:USDT 这种 USDT 本位永续")
        families[f"{m.group(1)}-USDT"] = p
    cfg_dd = cfg.get("datadir")
    if datadir_arg:
        dd_txt = datadir_arg
        if cfg_dd and Path(os.path.abspath(str(cfg_dd))) != Path(os.path.abspath(datadir_arg)):
            print(f"  · 提示：--datadir {datadir_arg} 和配置里的 \"datadir\": {cfg_dd} 不一样，按 --datadir")
    elif cfg_dd:
        dd_txt = str(cfg_dd)
    else:
        dd_txt = str(DEFAULT_DATADIR)
    datadir = Path(dd_txt)
    if not datadir.is_absolute():
        datadir = Path.cwd() / datadir
    if is_main_datadir(datadir) and not allow_main:
        raise ArgError(
            f"数据目录 {dd_txt} 是 user_data/data/okx：那是运行 A / B 和 NFI 回测用的数据目录，"
            "往里面加归档资金费和替身标记价格会悄悄改变它们的结果。\n"
            "   XSFundingCarry 用单独的数据目录：--config configs/config.okx-research-funding.json（user_data/data/okx-funding）。\n"
            "   确实要写进 user_data/data/okx 才加 --allow-main-datadir。"
        )
    import freqtrade.exchange as fe
    from freqtrade.enums import CandleType
    from freqtrade.exchange import timeframe_to_floor_freq

    cls = fe.Okx
    has = {**fe.Exchange._ft_has_default, **getattr(cls, "_ft_has", {}), **getattr(cls, "_ft_has_futures", {})}
    ftf = str(has.get("funding_fee_timeframe") or "1h")
    mtf = str(has.get("mark_ohlcv_timeframe") or "1h")
    mtype = CandleType.from_string(str(has.get("mark_ohlcv_price") or "mark"))
    import pandas as pd

    floor_ms = int(pd.Timedelta(timeframe_to_floor_freq(ftf)).total_seconds() * 1000)
    return Context(
        configs=list(configs), pairs=pairs, families=families, datadir=datadir, datadir_txt=dd_txt,
        fmt=str(cfg.get("dataformat_ohlcv") or "feather"), funding_tf=ftf, mark_tf=mtf, mark_type=mtype,
        floor_ms=max(1, floor_ms),
    )


def manifest_path(datadir: Path) -> Path:
    return datadir / ARCHIVE_SUBDIR / MANIFEST_NAME


def load_manifest(datadir: Path) -> dict | None:
    p = manifest_path(datadir)
    if not p.is_file():
        return None
    try:
        doc = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        print(f"!! 清单 {p} 读不了（格式坏了），这次当成没有旧清单")
        return None
    return doc if isinstance(doc, dict) else None


def provenance_path(datadir: Path) -> Path:
    return datadir / PROVENANCE_NAME


def load_provenance(datadir: Path) -> dict | None:
    p = provenance_path(datadir)
    if not p.is_file():
        return None
    try:
        doc = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        print(f"!! 来源记录 {p} 读不了（格式坏了），这次当成没有")
        return None
    return doc if isinstance(doc, dict) else None


# ----------------------------------------------------------------------------
# 复制合约K线（--seed-from / seed）
# ----------------------------------------------------------------------------
def seed_candles(src: Path, dst: Path, pairs: list[str], fmt: str) -> tuple[int, int]:
    from freqtrade.misc import pair_to_filename

    if not (src / "futures").is_dir():
        raise ArgError(f"{src}/futures 不存在，没有可以复制的合约K线")
    if Path(os.path.abspath(src)).resolve() == Path(os.path.abspath(dst)).resolve():
        raise ArgError("--seed-from 和数据目录是同一个")
    (dst / "futures").mkdir(parents=True, exist_ok=True)
    copied = existed = 0
    for pair in pairs:
        pf = pair_to_filename(pair)
        pat = re.compile(rf"{re.escape(pf)}-\d+[a-zA-Z]+-futures\.{re.escape(fmt)}")
        for f in sorted((src / "futures").glob(f"{pf}-*-futures.{fmt}")):
            if not pat.fullmatch(f.name) or not f.is_file():
                continue
            target = dst / "futures" / f.name
            if target.exists() or target.is_symlink():
                existed += 1
                continue
            tmp = target.with_name(target.name + ".part")
            shutil.copyfile(f, tmp)  # 复制内容（源文件是符号链接时也复制内容），从不建符号链接
            shutil.copystat(f, tmp)
            os.replace(tmp, target)
            copied += 1
    return copied, existed


# ----------------------------------------------------------------------------
# 合并
# ----------------------------------------------------------------------------
def _ranges_from_mask(dates_ms: list[int], mask: list[bool]) -> list[list]:
    """按排好序的时间，把 mask 为 True 的连续几行写成 [开始, 结束, 行数]。"""
    out: list[list] = []
    cur: list | None = None
    for d, m in zip(dates_ms, mask):
        if m:
            if cur is None:
                cur = [d, d, 0]
            cur[1] = d
            cur[2] += 1
        elif cur is not None:
            out.append(cur)
            cur = None
    if cur is not None:
        out.append(cur)
    return [[_iso(a), _iso(b), n] for a, b, n in out]


def _in_ranges(dates_ms: Any, ranges: list[list]) -> Any:
    import numpy as np

    arr = np.asarray(dates_ms, dtype="int64")
    m = np.zeros(len(arr), dtype=bool)
    for a, b, _n in ranges or []:
        m |= (arr >= _iso_to_ms(a)) & (arr <= _iso_to_ms(b))
    return m


def _valid_ranges(dates_ms: Any, ranges: list[list]) -> list[list]:
    """旧清单里记的时间段（来自归档的资金费 / 替身标记价格）还对得上文件吗？
    一段的第一行或最后一行还在文件里就算对得上（freqtrade 增量下载时会先丢掉文件的最后一行再接着下，
    所以只看一头）；两头都不在了 = 文件被删掉重下过，这一段作废。freqtrade 只往文件前后加行，不会插进这些时间段中间。"""
    s = {int(x) for x in dates_ms}
    good = []
    for r in ranges or []:
        try:
            if _iso_to_ms(r[0]) in s or _iso_to_ms(r[1]) in s:
                good.append(r)
        except (TypeError, ValueError, IndexError):
            continue
    return good


def _date_ms(series: Any) -> Any:
    import pandas as pd

    return pd.to_datetime(series, utc=True).astype("datetime64[ms, UTC]").astype("int64").to_numpy()


def _store_atomic(ctx: Context, pair: str, tf: str, df: Any, ct: Any) -> None:
    """用 freqtrade 自己的写入代码写到临时目录，再整个文件换过去（写到一半出错时不会留下坏文件）。"""
    from freqtrade.data.history import get_datahandler

    tmp_root = ctx.datadir / ARCHIVE_SUBDIR / "tmp"
    (tmp_root / "futures").mkdir(parents=True, exist_ok=True)
    tdh = get_datahandler(tmp_root, ctx.fmt)
    tdh.ohlcv_store(pair, tf, data=df, candle_type=ct)
    src = tdh._pair_data_filename(tmp_root, pair, tf, ct)
    dst = get_datahandler(ctx.datadir, ctx.fmt)._pair_data_filename(ctx.datadir, pair, tf, ct)
    dst.parent.mkdir(parents=True, exist_ok=True)
    os.replace(src, dst)


def _load(ctx: Context, pair: str, tf: str, ct: Any) -> tuple[Any, bool]:
    """读本地文件（不补缺、不丢最后一根）。返回 (DataFrame, 文件存在但读不了)。"""
    from freqtrade.data.history import get_datahandler

    dh = get_datahandler(ctx.datadir, ctx.fmt)
    fn = dh._pair_data_filename(ctx.datadir, pair, tf, ct)
    df = dh.ohlcv_load(pair, tf, ct, fill_missing=False, drop_incomplete=False, warn_no_data=False)
    broken = bool(df.empty and fn.is_file() and fn.stat().st_size > 0)
    if broken:
        import pandas as pd

        try:
            broken = len(pd.read_feather(fn) if ctx.fmt == "feather" else pd.read_parquet(fn)) > 0
        except Exception:  # noqa: BLE001
            broken = True
    return df, broken


def merge_funding(ctx: Context, pair: str, rows: dict[int, float], prev: dict, start_ms: int, end_ms: int) -> dict:
    """把这次归档里的资金费（rows：{结算时间毫秒: 费率}，已经按 --start / --end 过滤）合并进资金费文件。

    - 本地已有的、不是以前从归档导入的行（freqtrade 从接口下载的）优先，归档只补没有的时间点；重叠的逐条比较。
    - 以前从归档导入的行（清单的 archive_added_ranges）只要在这次的 --start ~ --end 里，就按这次的归档重建：
      先拿掉，再按这次解析的结果放回去（脚本改过、归档换了列，旧的值不会留下来）；区间外的不动。
      同样的输入再跑一次，结果和文件完全一样（不写文件）。
    - 清单和来源记录里都没有这个币（prev 是空的），重叠的那段却超过 PROVENANCE_SPAN_DAYS 天（接口只给最近约 92 天，
      不可能重叠这么长 = 以前从归档导入过、来源记录丢了）：比"最后一行 - 这么多天"更早的行记成"来源不明"
      （provenance_unknown_before），不拿来做重叠核对；以后一直记着，直到这些行没了（删掉资金费文件重下）。"""
    import numpy as np
    import pandas as pd
    from freqtrade.enums import CandleType

    ct = CandleType.FUNDING_RATE
    st: dict[str, Any] = {"notes": []}
    df, broken = _load(ctx, pair, ctx.funding_tf, ct)
    if broken:
        st["notes"].append("资金费文件读不了（坏了？），这个币这次不动")
        st["broken"] = True
        return st
    cur = pd.DataFrame({"date": pd.to_datetime(df["date"], utc=True).astype("datetime64[ms, UTC]") if len(df) else
                        pd.Series([], dtype="datetime64[ms, UTC]"),
                        "funding_rate": df["funding_rate"].astype("float64") if len(df) else pd.Series([], dtype="float64")})
    cur_ms = cur["date"].astype("int64").to_numpy() if len(cur) else np.array([], dtype="int64")
    cur_rate = cur["funding_rate"].to_numpy()
    prev_ranges = _valid_ranges(cur_ms, prev.get("archive_added_ranges") or [])
    arch_old = _in_ranges(cur_ms, prev_ranges)
    # 来源不明的行（来源记录丢过）
    cutoff: int | None = None
    if prev.get("provenance_unknown_before"):
        try:
            cutoff = _iso_to_ms(str(prev["provenance_unknown_before"]))
        except ValueError:
            cutoff = None

    def split(cut: int | None) -> tuple[Any, Any, dict[int, float]]:
        unk = (cur_ms < cut) & ~arch_old if cut is not None else np.zeros(len(cur_ms), dtype=bool)
        ap = ~arch_old & ~unk
        return unk, ap, dict(zip(cur_ms[ap].tolist(), cur_rate[ap].tolist()))

    unknown, api, api_vals = split(cutoff)
    if not prev and cutoff is None and len(cur_ms):
        both = [d for d in rows if d in api_vals]
        if both and max(both) - min(both) > PROVENANCE_SPAN_DAYS * 86_400_000:
            cutoff = int(cur_ms[-1]) - PROVENANCE_SPAN_DAYS * 86_400_000
            unknown, api, api_vals = split(cutoff)
    st["provenance_unknown_rows"] = int(unknown.sum())
    st["provenance_unknown_before"] = _iso(cutoff) if unknown.any() else None
    if unknown.any():
        st["notes"].append(f"!! 有 {int(unknown.sum())} 条 {_iso(cutoff)[:10]} 以前的资金费分不清是接口下载的还是以前从归档导入的"
                           f"（清单和来源记录 {PROVENANCE_NAME} 都没有这个币），没有拿来做重叠核对")
    st["api_rows"] = int(api.sum())
    ov = [(abs(api_vals[d] - r)) for d, r in rows.items() if d in api_vals]
    st["archive_rows"] = len(rows)
    st["overlap_rows"] = len(ov)
    st["overlap_diff_rows"] = int(sum(1 for x in ov if x > OVERLAP_TOL))
    st["overlap_max_abs_diff"] = float(max(ov)) if ov else None
    # 以前从归档导入、又在这次区间里的行：按这次的归档重建
    in_run = (cur_ms >= start_ms) & (cur_ms < end_ms)
    drop = arch_old & in_run
    old_arch = dict(zip(cur_ms[drop].tolist(), cur_rate[drop].tolist()))
    keep = ~drop
    kept_dates = set(cur_ms[keep].tolist())
    new = sorted((d, r) for d, r in rows.items() if d not in kept_dates)
    new_set = {d for d, _ in new}
    all_old = set(cur_ms.tolist())
    st["archive_rows_added_this_run"] = sum(1 for d, _ in new if d not in all_old)
    st["archive_rows_replaced_this_run"] = sum(1 for d, r in new if d in old_arch and old_arch[d] != r)
    st["archive_rows_removed_this_run"] = sum(1 for d in old_arch if d not in new_set)
    st["archive_rows_kept_outside_run"] = int((arch_old & ~in_run).sum())
    add = pd.DataFrame({
        "date": pd.to_datetime([d for d, _ in new], unit="ms", utc=True).astype("datetime64[ms, UTC]"),
        "funding_rate": np.array([r for _, r in new], dtype="float64"),
    })
    base = cur.loc[keep].reset_index(drop=True)
    merged = pd.concat([base, add], ignore_index=True) if len(base) and len(add) else (add if len(add) else base)
    merged = merged.sort_values("date", kind="mergesort").drop_duplicates("date", keep="first").reset_index(drop=True)
    m_ms = merged["date"].astype("int64").to_numpy() if len(merged) else np.array([], dtype="int64")
    same = len(m_ms) == len(cur_ms) and bool(np.array_equal(m_ms, cur_ms)) and bool(
        np.array_equal(merged["funding_rate"].to_numpy(), cur_rate, equal_nan=True))
    if not same:
        _store_atomic(ctx, pair, ctx.funding_tf, merged, ct)
    else:
        merged, m_ms = cur, cur_ms
    added_mask = (_in_ranges(m_ms, prev_ranges) & ~((m_ms >= start_ms) & (m_ms < end_ms))
                  | np.isin(m_ms, np.array(sorted(new_set), dtype="int64"))).tolist()
    st["archive_added_ranges"] = _ranges_from_mask(m_ms.tolist(), added_mask)
    st["archive_rows_added"] = int(sum(added_mask))
    st["funding_rows"] = len(merged)
    st["funding_first"] = _iso(int(m_ms[0])) if len(m_ms) else None
    st["funding_last"] = _iso(int(m_ms[-1])) if len(m_ms) else None
    if len(m_ms) >= 2:
        gaps = np.diff(m_ms) / 3_600_000.0
        st["funding_median_interval_h"] = round(float(np.median(gaps)), 4)
        st["funding_max_gap_h"] = round(float(gaps.max()), 4)
    else:
        st["funding_median_interval_h"] = None
        st["funding_max_gap_h"] = None
    st["_dates"] = m_ms
    return st


def _futures_ms(ctx: Context, pair: str) -> Any:
    """这个币 1h 合约K线的时间（毫秒，排好序）；没有 = None。"""
    import numpy as np
    from freqtrade.enums import CandleType

    fut, fbroken = _load(ctx, pair, ctx.mark_tf, CandleType.FUTURES)
    if fbroken or not len(fut):
        return None
    return np.sort(_date_ms(fut["date"]))


def funding_gaps(f_ms: Any, fund_ms: Any, start_ms: int, end_ms: int) -> list[list]:
    """资金费的洞：[max(--start, 第一根合约K线), min(最后一条资金费, --end)] 里，相邻两条资金费（第一段从起点算）
    隔了 FUNDING_GAP_H 小时以上、而且这段时间里币一直在交易（合约K线至少有 FUNDING_GAP_H 根；没有合约K线时只看资金费）。
    返回 [[开始, 结束, 小时], ...]（开始、结束 = 洞两边那两条资金费的时间）。"""
    import numpy as np

    if fund_ms is None or not len(fund_ms):
        return []
    lo = max(start_ms, int(f_ms[0])) if f_ms is not None and len(f_ms) else start_ms
    seq = fund_ms[(fund_ms >= lo) & (fund_ms < end_ms)]
    if not len(seq):
        return []
    pts = np.concatenate([np.array([lo], dtype="int64"), seq]) if lo < int(seq[0]) else seq
    out = []
    lim = FUNDING_GAP_H * 3_600_000
    for a, b in zip(pts[:-1].tolist(), pts[1:].tolist()):
        if b - a <= lim:
            continue
        if f_ms is not None and len(f_ms):
            n = int(((f_ms > a) & (f_ms < b)).sum())
            if n < FUNDING_GAP_H:  # 这段时间币没在交易（暂停 / 还没上线）
                continue
        out.append([_iso(a), _iso(b), round((b - a) / 3_600_000, 2)])
    return out


def mark_substitute(ctx: Context, pair: str, prev: dict, start_ms: int, end_ms: int, enabled: bool) -> dict:
    """第一根真实标记价格之前（只在 --start ~ --end 之内）用 1h 合约K线补标记价格替身（开高低收照抄，成交量 0）。

    - 以前补的替身（清单的 mark_substitute_ranges）在这次区间里的，按现在的合约K线重建（先拿掉再补），区间外的不动；
      真实的标记价格从不覆盖。同样的输入再跑一次，文件不变（不写文件）。
    - 清单和来源记录里都没有这个币、标记价格文件却超过 MARK_SPAN_DAYS 天（接口只给约 3 个月 = 以前补过替身、记录丢了）：
      按数据认替身（文件开头连续的一段"成交量 0、开高低收和同一时刻的 1h 合约K线完全一样"），不会把以前补的替身当成真实的标记价格。
    - 第一根真实标记价格之后，币在交易、标记价格却连续缺 MARK_GAP_H 小时以上的段记在 mark_gaps（不补，只报告）。"""
    import numpy as np
    import pandas as pd
    from freqtrade.enums import CandleType

    st: dict[str, Any] = {"notes": [], "mark_substitute_added_this_run": 0}
    mark, broken = _load(ctx, pair, ctx.mark_tf, ctx.mark_type)
    if broken:
        st["notes"].append("标记价格文件读不了（坏了？），这个币这次不补替身")
        enabled = False
    cols = ["date", "open", "high", "low", "close", "volume"]
    if len(mark):
        mark = mark.loc[:, cols].copy()
        mark["date"] = pd.to_datetime(mark["date"], utc=True).astype("datetime64[ms, UTC]")
    mk_ms = mark["date"].astype("int64").to_numpy() if len(mark) else np.array([], dtype="int64")
    fut, fbroken = _load(ctx, pair, ctx.mark_tf, CandleType.FUTURES)
    if fbroken or not len(fut):
        fut = None
    else:
        fut = fut.loc[:, cols].copy()
        fut["date"] = pd.to_datetime(fut["date"], utc=True).astype("datetime64[ms, UTC]")
    f_ms = fut["date"].astype("int64").to_numpy() if fut is not None else np.array([], dtype="int64")
    prev_ranges = _valid_ranges(mk_ms, prev.get("mark_substitute_ranges") or [])
    sub_old = _in_ranges(mk_ms, prev_ranges)
    if not prev and len(mark) and fut is not None and int(mk_ms[-1]) - int(mk_ms[0]) > MARK_SPAN_DAYS * 86_400_000:
        # 清单和来源记录里都没有这个币、标记价格却比接口能给的长得多（OKX 只给最近约 3 个月）= 以前补过替身、记录丢了：
        # 按数据认替身 = 文件开头连续的一段"成交量 0、开高低收和同一时刻的 1h 合约K线完全一样"的K线（替身只补在第一根真实的之前；
        # 真实的标记价格有时也会碰巧和成交价一样，所以只认开头连着的那一段）
        j = pd.merge(mark, fut, on="date", how="left", suffixes=("", "_f"))
        same = np.ones(len(j), dtype=bool)
        for c in ("open", "high", "low", "close"):
            same &= (j[c].to_numpy() == j[c + "_f"].to_numpy())
        vol = j["volume"].to_numpy(dtype="float64")
        match = same & (np.isnan(vol) | (vol == 0))
        n_lead = int(np.argmin(match)) if not match.all() else len(match)
        detected = np.zeros(len(match), dtype=bool)
        detected[:n_lead] = True
        if n_lead:
            st["notes"].append(f"清单和来源记录里都没有这个币：按数据认出开头 {n_lead} 根是以前补的替身标记价格（成交量 0、开高低收和合约K线一样）")
        sub_old = sub_old | detected
    real_ms = mk_ms[~sub_old]
    first_real = int(real_ms.min()) if len(real_ms) else None
    in_run = (mk_ms >= start_ms) & (mk_ms < end_ms)
    old_sub_kept = set(mk_ms[sub_old & ~in_run].tolist())
    new_sub: set[int] = set()
    rebuilt = False
    if enabled and not broken:
        limit: int | None = None
        if fut is None:
            st["notes"].append(f"没有 {ctx.mark_tf} 合约K线，补不了标记价格替身")
        elif first_real is not None:
            limit = first_real
        elif int(f_ms.max()) < end_ms - NO_MARK_STALE_DAYS * 86_400_000:
            limit = int(f_ms.max()) + 1  # 已经停止更新的币：真实标记价格永远下不到，全部用替身
            st["notes"].append("没有真实标记价格、K线也早就停了（可能已下架），整段用合约K线做替身")
        else:
            st["notes"].append("还没有真实标记价格（K线一直到最近）：先下载标记价格（make okx-funding-data）再导入，这次不补替身")
        if limit is not None:
            rebuilt = True
            drop = sub_old & in_run
            old_vals = mark.loc[drop].set_index("date")
            base = mark.loc[~drop]
            base_ms = mk_ms[~drop]
            pick = (f_ms >= start_ms) & (f_ms < min(limit, end_ms)) & ~np.isin(f_ms, base_ms)
            sub = fut.loc[pick, cols].copy()
            sub["volume"] = 0.0
            new_sub = set(f_ms[pick].tolist())
            merged = pd.concat([base, sub], ignore_index=True) if len(base) and len(sub) else (sub if len(sub) else base)
            merged = merged.sort_values("date", kind="mergesort").drop_duplicates("date", keep="first").reset_index(drop=True)
            m_ms = merged["date"].astype("int64").to_numpy() if len(merged) else np.array([], dtype="int64")
            same = len(m_ms) == len(mk_ms) and bool(np.array_equal(m_ms, mk_ms)) and bool(np.array_equal(
                merged[cols[1:]].to_numpy(dtype="float64"), mark[cols[1:]].to_numpy(dtype="float64"), equal_nan=True))
            old_set = set(mk_ms.tolist())
            st["mark_substitute_added_this_run"] = sum(1 for d in new_sub if d not in old_set)
            if len(old_vals):
                sv = sub.set_index("date")
                both = old_vals.index.intersection(sv.index)
                diff = (old_vals.loc[both, ["open", "high", "low", "close"]].to_numpy()
                        != sv.loc[both, ["open", "high", "low", "close"]].to_numpy()).any(axis=1)
                st["mark_substitute_replaced_this_run"] = int(diff.sum())
                st["mark_substitute_removed_this_run"] = int(len(old_vals.index.difference(sv.index)))
            if not same:
                _store_atomic(ctx, pair, ctx.mark_tf, merged, ctx.mark_type)
                mark, mk_ms = merged, m_ms
    if rebuilt:
        sub_mask = np.isin(mk_ms, np.array(sorted(old_sub_kept | new_sub), dtype="int64"))
        st["mark_substitute_kept_rows"] = len(old_sub_kept)  # 以前补的、在这次区间外（没有重建）
    else:
        sub_mask = np.isin(mk_ms, np.array(sorted(set(mk_ms[sub_old].tolist())), dtype="int64"))
        st["mark_substitute_kept_rows"] = int(sub_mask.sum())  # 这次没有重建：以前补的全部照旧
    st["mark_substitute_ranges"] = _ranges_from_mask(mk_ms.tolist(), sub_mask.tolist())
    st["mark_substitute_rows"] = int(sub_mask.sum())
    st["mark_rows"] = len(mk_ms)
    st["mark_first"] = _iso(int(mk_ms[0])) if len(mk_ms) else None
    st["mark_last"] = _iso(int(mk_ms[-1])) if len(mk_ms) else None
    st["first_real_mark"] = _iso(first_real)
    # 第一根真实标记价格之后的缺口（币在交易 = 有 1h 合约K线、却没有标记价格；只看 --start ~ --end）
    gaps: list[list] = []
    if first_real is not None and len(f_ms):
        miss = f_ms[(f_ms >= max(first_real, start_ms)) & (f_ms < end_ms) & ~np.isin(f_ms, mk_ms)]
        if len(miss):
            step = 3_600_000
            br = np.where(np.diff(miss) != step)[0]
            starts = np.concatenate([[0], br + 1])
            ends = np.concatenate([br, [len(miss) - 1]])
            for a, b in zip(starts.tolist(), ends.tolist()):
                hours = (int(miss[b]) - int(miss[a])) / step + 1
                if hours > MARK_GAP_H:
                    gaps.append([_iso(int(miss[a])), _iso(int(miss[b])), round(hours, 2)])
    st["mark_gaps"] = gaps
    return st


# ----------------------------------------------------------------------------
# 子命令
# ----------------------------------------------------------------------------
def _parse_files(files: list[dict], ctx: Context, start_ms: int, end_ms: int) -> tuple[dict[str, dict[int, float]], list[dict], list[str]]:
    fams = set(ctx.families)
    by_fam: dict[str, dict[int, float]] = {f: {} for f in fams}
    records, warnings = [], []
    conflicts = 0
    naive_total = 0
    for f in sorted(files, key=lambda x: (x.get("month") or "999999", str(x["path"]))):
        path: Path = f["path"]
        members = read_archive_file(path)
        rec = {
            "url": f.get("url"), "local_path": _rel(path), "size": path.stat().st_size, "sha256": _sha256_file(path),
            "month": f.get("month"), "members": [m for m, _ in members], "rows": 0, "rows_kept": 0,
            "rate_column": None, "time_column": None, "instrument_column": None,
        }
        for mname, text in members:
            t = parse_csv_text(text, f"{path.name}:{mname}" if mname != path.name else path.name, fams, start_ms, end_ms,
                               ctx.floor_ms)
            rec["rows"] += t.n_rows
            rec["rows_kept"] += len(t.rows)
            rec["rate_column"] = t.columns.rate_name
            rec["time_column"] = t.columns.time_name
            rec["instrument_column"] = t.columns.inst_name or f"（文件名：{t.inferred_family}）"
            rec["realized"] = t.columns.realized
            rec["time_fallback"] = t.columns.time_fallback
            if t.n_bad:
                warnings.append(f"{t.name}：{t.n_bad}/{t.n_rows} 行读不出时间或费率，已跳过")
            naive_total += t.n_naive_time
            for fam, ms, rate in t.rows:
                d = by_fam[fam]
                if ms in d:
                    if abs(d[ms] - rate) > OVERLAP_TOL:
                        conflicts += 1
                    continue
                d[ms] = rate
        records.append(rec)
    if conflicts:
        warnings.append(f"不同归档文件里同一个币、同一个结算时间的费率不一样：{conflicts} 处（用排在前面的文件）")
    if naive_total:
        warnings.append(f"有 {naive_total} 行结算时间没写时区，按 UTC 算了（如果其实是北京时间，和接口数据的重叠比较会对不上）")
    return by_fam, records, warnings


def _summarize_columns(records: list[dict]) -> dict[str, Any]:
    def count(key: str) -> dict[str, int]:
        c: dict[str, int] = {}
        for r in records:
            v = r.get(key)
            if v is not None:
                c[str(v)] = c.get(str(v), 0) + 1
        return dict(sorted(c.items()))

    rc, tc, ic = count("rate_column"), count("time_column"), count("instrument_column")
    with_rows = [r for r in records if r.get("rate_column") is not None]
    return {
        "rate_column": (next(iter(rc)) if len(rc) == 1 else ("mixed" if rc else None)),
        "rate_columns": rc,
        "rate_is_realized": bool(with_rows) and all(r.get("realized") for r in with_rows),
        "time_column": (next(iter(tc)) if len(tc) == 1 else ("mixed" if tc else None)),
        "instrument_column": (next(iter(ic)) if len(ic) == 1 else ("mixed" if ic else None)),
        "time_is_fallback": any(r.get("time_fallback") for r in with_rows),
    }


def _write_json_atomic(path: Path, doc: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".part")
    tmp.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _non_archive_rows(ctx: Context, pair: str, pv: dict) -> int | None:
    """资金费文件里不是从归档导入的行（freqtrade 从接口下载的）有几条；文件读不了 = None。"""
    from freqtrade.enums import CandleType

    df, broken = _load(ctx, pair, ctx.funding_tf, CandleType.FUNDING_RATE)
    if broken:
        return None
    if not len(df):
        return 0
    d = _date_ms(df["date"])
    return int((~_in_ranges(d, _valid_ranges(d, pv.get("archive_added_ranges") or []))).sum())


def _write_provenance(ctx: Context, pairs_out: dict[str, dict], data_tools: list[str]) -> None:
    prov_pairs = {}
    for pair, st in pairs_out.items():
        prov_pairs[pair] = {k: st.get(k) for k in ("archive_added_ranges", "mark_substitute_ranges", "provenance_unknown_before")}
    doc = {
        "schema": 1,
        "note": "scripts/okx_funding_archive.py 写的来源记录：哪些资金费来自 OKX 归档、哪些标记价格是用合约K线补的替身、"
                "这些行是哪个版本的导入脚本做的（data_tool_sha256）。清单 okx_funding_archive/manifest.json 丢了时按这个认。不要手改。",
        "tool": "scripts/okx_funding_archive.py",
        "data_tool_sha256": data_tools,
        "pairs": prov_pairs,
    }
    p = provenance_path(ctx.datadir)
    text = json.dumps(doc, ensure_ascii=False, indent=2) + "\n"
    if p.is_file() and p.read_text(encoding="utf-8") == text:
        return  # 没变就不写
    _write_json_atomic(p, doc)


def cmd_update(a: argparse.Namespace, http: Http | None = None) -> int:
    ctx = resolve_context(a.config, a.datadir, a.allow_main_datadir)
    start = _ymd(a.start or DEFAULT_START, "--start")
    end = _ymd(a.end, "--end") if a.end else datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    if end < start:
        raise ArgError(f"--end {end:%Y%m%d} 早于 --start {start:%Y%m%d}")
    if start < datetime(2022, 3, 1, tzinfo=UTC):
        print(f"  · 提示：OKX 的资金费归档从 2022-03 开始，{start:%Y-%m-%d} ~ 2022-02 这段不会有数据")
    start_ms = int(start.timestamp() * 1000)
    end_ms = int((end + timedelta(days=1)).timestamp() * 1000)  # 包含 --end 那一天
    now_ms = int(time.time() * 1000)
    lister = "from-dir" if a.from_dir else a.lister
    print(f"OKX 资金费归档导入：{len(ctx.pairs)} 个币，{start:%Y-%m-%d} ~ {end:%Y-%m-%d}，数据目录 {ctx.datadir_txt}，"
          f"来源 {lister}{'（' + str(a.from_dir) + '）' if a.from_dir else ''}")
    if a.seed_from and not (Path(a.seed_from) / "futures").is_dir():
        raise ArgError(f"--seed-from {a.seed_from}/futures 不存在，没有可以复制的合约K线")
    ctx.datadir.mkdir(parents=True, exist_ok=True)
    prev_doc = load_manifest(ctx.datadir)
    prov_doc = load_provenance(ctx.datadir)
    prev = prev_doc or {}
    prev_pairs: dict[str, dict] = prev.get("pairs") if isinstance(prev.get("pairs"), dict) else {}
    prov_pairs: dict[str, dict] = (prov_doc or {}).get("pairs") if isinstance((prov_doc or {}).get("pairs"), dict) else {}
    early_warn: list[str] = []
    if prev_doc is None and prov_pairs:
        early_warn.append(f"清单 {_rel(manifest_path(ctx.datadir))} 没有了（或者读不了）：哪些资金费来自归档、哪些标记价格是替身，"
                          f"按来源记录 {_rel(provenance_path(ctx.datadir))} 认")

    def prev_of(pair: str) -> dict:
        """这个币以前的记录：来源记录（来自归档的时间段、替身的时间段）+ 清单（重叠核对等），清单优先。"""
        pv = dict(prov_pairs.get(pair) or {}) if isinstance(prov_pairs.get(pair), dict) else {}
        if isinstance(prev_pairs.get(pair), dict):
            pv.update(prev_pairs[pair])
        return pv

    # 1. 收集文件（联网的部分全部在这里；出错时数据文件和清单都还没动）
    skipped_other = 0
    if a.from_dir:
        files = list_local(Path(a.from_dir))
        print(f"本地归档文件：{len(files)} 个（{a.from_dir}）")
    else:
        http = http or Http()
        months = _months(start, end)
        entries = list_remote(http, a.lister, sorted(ctx.families), months, now_ms)
        if not entries:
            raise FormatError("列表接口一个文件都没给（参数不对，或者返回格式变了）；先用 probe 子命令看看返回的内容")
        files, skipped_other = download_entries(http, entries, ctx.datadir / ARCHIVE_SUBDIR / "raw", set(ctx.families))
    if not files:
        raise FormatError("没有可以读的归档文件（.zip / .csv / .csv.gz）")

    # 2. 解析（格式认不出来时 FormatError，数据文件还没动）
    by_fam, records, warnings = _parse_files(files, ctx, start_ms, end_ms)
    warnings = early_warn + warnings
    cols = _summarize_columns(records)
    # 2b. 还没有接口资金费的币：先导入归档、后下载接口数据的话，freqtrade 只往文件最后一行后面接着下，
    #     归档和接口数据永远不会重叠，没法核对费率列。默认拒绝（数据目录一点都没动），确实下不到接口数据才加 --allow-no-api-funding
    no_api = []
    for fam, pair in ctx.families.items():
        if by_fam.get(fam) and _non_archive_rows(ctx, pair, prev_of(pair)) == 0:
            no_api.append(pair)
    no_api.sort(key=ctx.pairs.index)
    if no_api and not a.allow_no_api_funding:
        raise ArgError(
            f"这些币还没有从接口下载的资金费（freqtrade 下的最近约 3 个月）：{' '.join(no_api[:10])}"
            + (f" 等 {len(no_api)} 个" if len(no_api) > 10 else "") + "。\n"
            "   先导入归档、后下载接口数据的话，freqtrade 只往文件最后一行后面接着下，归档和接口数据就永远没有重叠，"
            "没法核对归档的费率列是不是实际结算的费率。\n"
            "   先下载再导入：make okx-funding-data（先下载最近的标记价格 / 资金费，再导入归档）。\n"
            "   确实下不到接口数据（例如已经下架的币）才加 --allow-no-api-funding（这些币记在清单的 pairs_without_api_funding）。"
        )
    if a.seed_from:  # 放在联网和解析之后：前面出错时数据目录一点都没动
        c, e = seed_candles(Path(a.seed_from), ctx.datadir, ctx.pairs, ctx.fmt)
        print(f"复制合约K线（--seed-from {a.seed_from}）：复制了 {c} 个文件，本数据目录已有、没动 {e} 个")
    if not cols["rate_is_realized"]:
        warnings.append(
            f"!! 归档里没有\"实际结算的费率\"列（real_funding_rate / realized_rate），用的是 {cols['rate_columns']}："
            "freqtrade 从接口下载的 OKX 资金费是实际结算的费率（realizedRate），两者可能不一样。看下面的重叠比较。"
        )
    if cols["time_is_fallback"]:
        warnings.append("!! 归档里没有结算时间列（funding_time），用的是 timestamp 一类的列：它可能是记录时间，不是结算时间。")
    if no_api:
        warnings.append(f"!! 这些币还没有接口下载的资金费，用 --allow-no-api-funding 照样导入了归档：{' '.join(no_api[:10])}"
                        + (f" 等 {len(no_api)} 个" if len(no_api) > 10 else "") + "（它们的费率没法和接口数据核对）")

    # 3. 合并资金费 + 4. 标记价格替身
    pairs_out: dict[str, dict] = {}
    tot_ov = tot_diff = 0
    leftover = 0  # 以前做的、这次没有重建的行（区间外的归档资金费 / 替身、来源不明的资金费）
    for fam, pair in sorted(ctx.families.items(), key=lambda kv: ctx.pairs.index(kv[1])):
        pv = prev_of(pair)
        fs = merge_funding(ctx, pair, by_fam.get(fam, {}), pv, start_ms, end_ms)
        ms = mark_substitute(ctx, pair, pv, start_ms, end_ms, not a.no_mark_substitute)
        notes = fs.pop("notes") + ms.pop("notes")
        broken = fs.pop("broken", None)
        dates = fs.pop("_dates", None)
        if not by_fam.get(fam):
            notes.insert(0, "归档里没有这个币在这段时间的资金费")
        st = {"family": fam, **fs, **ms}
        if broken and isinstance(prev_pairs.get(pair), dict):
            # 资金费文件读不了：这个币这次没动，来源记录照旧（不然下次会把归档的行当成接口的）
            for k in ("archive_added_ranges", "provenance_unknown_before", "overlap_rows", "overlap_diff_rows", "overlap_max_abs_diff"):
                st.setdefault(k, prev_pairs[pair].get(k))
        st["funding_gaps"] = funding_gaps(_futures_ms(ctx, pair), dates, start_ms, end_ms) if dates is not None else []
        # 重叠核对是累计的：这次一条都没比到（例如区间很窄），沿用上次的结论，不清掉
        pm = prev_pairs.get(pair) if isinstance(prev_pairs.get(pair), dict) else {}
        if not st.get("overlap_rows") and int(pm.get("overlap_rows") or 0) > 0:
            for k in ("overlap_rows", "overlap_diff_rows", "overlap_max_abs_diff"):
                st[k] = pm.get(k)
            st["overlap_carried_from"] = pm.get("overlap_carried_from") or prev.get("updated_at") or "上次"
        else:
            st["overlap_carried_from"] = None
        st["overlap_mismatch"] = bool(st.get("overlap_rows") and int(st.get("overlap_diff_rows") or 0) > OVERLAP_MAX_SHARE * st["overlap_rows"])
        st["notes"] = notes
        st = {k: st.get(k) for k in PAIR_KEYS}  # 固定顺序（清单好读）
        tot_ov += int(st.get("overlap_rows") or 0)
        tot_diff += int(st.get("overlap_diff_rows") or 0)
        leftover += int(st.get("archive_rows_kept_outside_run") or 0) + int(st.get("mark_substitute_kept_rows") or 0) \
            + int(st.get("provenance_unknown_rows") or 0)
        mi = st.get("funding_median_interval_h")
        if mi is not None and mi not in (1.0, 2.0, 4.0, 8.0):
            notes.append(f"结算间隔中位数 {mi} 小时，不是 1 / 2 / 4 / 8 小时")
        if st["funding_gaps"]:
            g = max(st["funding_gaps"], key=lambda x: x[2])
            notes.append(f"!! 资金费有 {len(st['funding_gaps'])} 个洞（币在交易、超过 {FUNDING_GAP_H} 小时没有一条），最长 {g[2]:g} 小时（{g[0][:10]} ~ {g[1][:10]}）")
        if st["mark_gaps"]:
            g = max(st["mark_gaps"], key=lambda x: x[2])
            notes.append(f"!! 第一根真实标记价格之后标记价格缺了 {len(st['mark_gaps'])} 段（超过 {MARK_GAP_H} 小时），最长 {g[2]:g} 小时（{g[0][:10]} ~ {g[1][:10]}）")
        if int(st.get("archive_rows_replaced_this_run") or 0) or int(st.get("archive_rows_removed_this_run") or 0):
            notes.append(f"按这次的归档重建：改了 {st['archive_rows_replaced_this_run']} 条、去掉 {st['archive_rows_removed_this_run']} 条以前导入的资金费")
        if int(st.get("mark_substitute_replaced_this_run") or 0) or int(st.get("mark_substitute_removed_this_run") or 0):
            notes.append(f"按现在的合约K线重建替身：改了 {st.get('mark_substitute_replaced_this_run') or 0} 根、"
                         f"去掉 {st.get('mark_substitute_removed_this_run') or 0} 根")
        pairs_out[pair] = st
    mismatch = bool(tot_ov and tot_diff > OVERLAP_MAX_SHARE * tot_ov)
    if mismatch:
        warnings.append(
            f"!! 归档和本地已有的资金费重叠 {tot_ov} 条，其中 {tot_diff} 条差别大于 {OVERLAP_TOL:g}（超过 1%）："
            "归档里的费率列可能不是实际结算的费率（archive rate column may not be the realized rate）。"
            "本地已有的行没有被改动；用这份数据做正式运行之前先查清楚。"
        )
    no_ov = [p for p, s in pairs_out.items() if int(s.get("archive_rows_added") or 0) and not s.get("overlap_rows")]
    remedy = ("一般是先导入了归档、后下载的接口数据：freqtrade 只往文件最后一行后面接着下，重叠的那段永远下不回来，"
              "所以\"先下载再导入一次\"没有用。办法：删掉这些币的资金费文件（futures/<币>_USDT_USDT-1h-funding_rate.feather），"
              "再运行 make okx-funding-data（先下载最近的资金费、再导入归档）；或者等下个月的归档出来以后再导入（新的一个月和接口数据有重叠）")
    if not tot_ov:
        warnings.append("!! 归档和本地已有的资金费（freqtrade 从接口下载的最近约 3 个月）一条都没有重叠，没法核对费率列对不对"
                        f"（overlap_checked = false，不能正式运行）。{remedy}")
    elif no_ov:
        warnings.append(f"!! {len(no_ov)} 个币有归档导入的资金费、却和接口数据一条都没有重叠，没有逐个核对：{' '.join(no_ov[:10])}"
                        + (" ..." if len(no_ov) > 10 else "") + f"。{remedy}")
    mm_pairs = [p for p, s in pairs_out.items() if s["overlap_mismatch"]]
    fg_pairs = [p for p, s in pairs_out.items() if s.get("funding_gaps")]
    mg_pairs = [p for p, s in pairs_out.items() if s.get("mark_gaps")]
    pu_pairs = [p for p, s in pairs_out.items() if int(s.get("provenance_unknown_rows") or 0)]
    if fg_pairs:
        warnings.append(f"!! {len(fg_pairs)} 个币的资金费中间有洞（币在交易、超过 {FUNDING_GAP_H} 小时没有资金费，例如归档少了一个月）："
                        f"{' '.join(fg_pairs[:10])}{' ...' if len(fg_pairs) > 10 else ''}。洞里持仓的资金费按 0 算，"
                        "XSFundingCarry 也会因为\"前 7 天资金费不完整\"不交易它们。先重新运行一次（会接着下）；还缺就把那个月的归档文件"
                        "手工下载到一个目录，用 --from-dir 导入。时间段见清单 pairs.<币>.funding_gaps")
    if mg_pairs:
        warnings.append(f"!! {len(mg_pairs)} 个币第一根真实标记价格之后标记价格缺了一段（超过 {MARK_GAP_H} 小时）：{' '.join(mg_pairs[:10])}"
                        f"{' ...' if len(mg_pairs) > 10 else ''}。多半是隔了 3 个月以上才更新（OKX 接口只给最近约 3 个月的标记价格，"
                        "中间那段再也下不到）；回测会用很久以前的标记价格去算那段的资金费。办法：删掉这些币的标记价格文件"
                        "（futures/<币>_USDT_USDT-1h-mark.feather），再运行 make okx-funding-data（真实的只剩最近 3 个月，更早的都用替身）。"
                        "时间段见清单 pairs.<币>.mark_gaps")
    if pu_pairs:
        warnings.append(f"!! {len(pu_pairs)} 个币有来源不明的资金费（清单和来源记录 {PROVENANCE_NAME} 都丢过）：{' '.join(pu_pairs[:10])}"
                        "。分不清哪些是归档导入的，没法做可靠的重叠核对。办法：删掉这些币的资金费文件和标记价格文件，"
                        "再运行 make okx-funding-data")

    # 5. 来源记录 + 清单
    cur_tool = tool_sha256()
    prev_tools = [str(x) for x in ((prev.get("data_tool_sha256") if isinstance(prev.get("data_tool_sha256"), list) else None)
                                    or ((prov_doc or {}).get("data_tool_sha256") if isinstance((prov_doc or {}).get("data_tool_sha256"), list) else None)
                                    or ([prev["tool_sha256"]] if prev.get("tool_sha256") else []))]
    if leftover:
        # 还有以前做的、这次没有重建的行（--start / --end 比以前窄、--no-mark-substitute、来源不明）：那些行可能是旧版脚本做的
        data_tools = sorted(set(prev_tools or ["unknown"]) | {cur_tool})
    else:
        data_tools = [cur_tool]
    if data_tools != [cur_tool]:
        warnings.append("!! 数据目录里还有以前的导入（别的版本的脚本，或者这次的 --start / --end 没有覆盖到）做的归档资金费 / 替身标记价格"
                        f"（data_tool_sha256 有 {len(data_tools)} 个）。正式运行之前用默认区间重新导入一次"
                        "（make okx-funding-data，会按现在的脚本重建 2022-03 起的全部归档资金费和替身）")
    added = sum(int(s.get("archive_rows_added_this_run") or 0) for s in pairs_out.values())
    subs = sum(int(s.get("mark_substitute_added_this_run") or 0) for s in pairs_out.values())
    replaced = sum(int(s.get("archive_rows_replaced_this_run") or 0) + int(s.get("archive_rows_removed_this_run") or 0)
                   for s in pairs_out.values())
    now_iso = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    last_run = {
        "at": now_iso, "tool_sha256": cur_tool, "start": f"{start:%Y%m%d}", "end": f"{end:%Y%m%d}", "lister": lister,
        "files": len(records), "archive_rows_added": added, "archive_rows_rebuilt": replaced, "mark_substitute_added": subs,
        "mark_substitute": not a.no_mark_substitute, "http_requests": (http.requests if http is not None else 0),
    }
    runs = list(prev.get("runs") or [])[-(RUNS_KEPT - 1):] + [last_run]
    doc = {
        "schema": SCHEMA_VERSION,
        "tool": "scripts/okx_funding_archive.py",
        "tool_sha256": cur_tool,
        "data_tool_sha256": data_tools,
        "updated_at": now_iso,
        "exchange": "okx",
        "config": [_rel(Path(c)) for c in ctx.configs],
        "datadir": _rel(ctx.datadir),
        "start": f"{start:%Y%m%d}",
        "end": f"{end:%Y%m%d}",
        "lister": lister,
        "from_dir": _rel(Path(a.from_dir)) if a.from_dir else None,
        "funding_timeframe": ctx.funding_tf,
        "mark_timeframe": ctx.mark_tf,
        "mark_candle_type": str(ctx.mark_type),
        "mark_substitute": not a.no_mark_substitute,
        **{k: v for k, v in cols.items() if k != "time_is_fallback"},
        "time_is_fallback": cols["time_is_fallback"],
        "files": [{k: v for k, v in r.items() if k not in ("realized", "time_fallback")} for r in records],
        "files_skipped_other_family": skipped_other,
        "pairs": pairs_out,
        "overlap_rows_total": tot_ov,
        "overlap_diff_rows_total": tot_diff,
        "overlap_checked": bool(tot_ov),
        "overlap_mismatch": mismatch,
        "overlap_mismatch_pairs": mm_pairs,
        "no_overlap_pairs": no_ov,
        "allow_no_api_funding": bool(a.allow_no_api_funding),
        "pairs_without_api_funding": no_api,
        "funding_gap_pairs": fg_pairs,
        "mark_gap_pairs": mg_pairs,
        "provenance_unknown_pairs": pu_pairs,
        "warnings": warnings,
        "last_run": last_run,
        "runs": runs,
    }
    _write_provenance(ctx, pairs_out, data_tools)
    _write_json_atomic(manifest_path(ctx.datadir), doc)
    shutil.rmtree(ctx.datadir / ARCHIVE_SUBDIR / "tmp", ignore_errors=True)

    # 6. 屏幕小结
    print()
    print("每个币（资金费条数、起止、间隔；这次从归档新加几条；和已有数据的重叠；标记价格替身）：")
    for pair, s in pairs_out.items():
        print("  " + _pair_line(pair, s))
    print()
    print(f"费率列：{cols['rate_column']}（{'实际结算的费率' if cols['rate_is_realized'] else '!! 不是实际结算的费率'}），"
          f"时间列：{cols['time_column']}，合约列：{cols['instrument_column']}")
    print(f"这次：归档文件 {len(records)} 个，资金费新加 {added} 条（按这次的归档重建 {replaced} 条），标记价格替身新加 {subs} 根；"
          f"重叠 {tot_ov} 条、差别大于 {OVERLAP_TOL:g} 的 {tot_diff} 条")
    for w in warnings:
        print(("" if w.startswith("!!") else "  · ") + w)
    print(f"清单：{_rel(manifest_path(ctx.datadir))}（来源记录：{_rel(provenance_path(ctx.datadir))}）")
    return EXIT_OK


def _pair_line(pair: str, s: dict) -> str:
    def d(x: str | None) -> str:
        return x[:10] if x else "—"

    mi = s.get("funding_median_interval_h")
    parts = [
        f"{pair:<16} 资金费 {s.get('funding_rows', 0)} 条 {d(s.get('funding_first'))} ~ {d(s.get('funding_last'))}"
        + (f"（间隔中位数 {mi:g} 小时，最长 {s.get('funding_max_gap_h'):g} 小时）" if mi is not None else ""),
        f"新加 {s.get('archive_rows_added_this_run', 0)} 条（来自归档累计 {s.get('archive_rows_added', 0)}）",
        f"重叠 {s.get('overlap_rows', 0)} 条"
        + (f" 最大差 {s['overlap_max_abs_diff']:.2g}" if s.get("overlap_max_abs_diff") is not None else "")
        + (" !!对不上" if s.get("overlap_mismatch") else ""),
        f"替身标记价格 {s.get('mark_substitute_rows', 0)} 根（真实的从 {d(s.get('first_real_mark'))} 开始）",
    ]
    line = "；".join(parts)
    if s.get("notes"):
        line += "  · " + "；".join(s["notes"])
    return line


def cmd_seed(a: argparse.Namespace) -> int:
    ctx = resolve_context(a.config, a.datadir, a.allow_main_datadir)
    ctx.datadir.mkdir(parents=True, exist_ok=True)
    c, e = seed_candles(Path(a.src), ctx.datadir, ctx.pairs, ctx.fmt)
    print(f"复制合约K线 {a.src} -> {ctx.datadir_txt}：复制了 {c} 个文件，已有、没动 {e} 个（只复制 *-futures.{ctx.fmt}，"
          "标记价格和资金费不复制）")
    return EXIT_OK


def cmd_status(a: argparse.Namespace) -> int:
    ctx = resolve_context(a.config, a.datadir, True)
    m = load_manifest(ctx.datadir)
    if m is None:
        print(f"还没有导入过资金费归档：没有 {_rel(manifest_path(ctx.datadir))}（先运行 make okx-funding-data）", file=sys.stderr)
        return EXIT_ARGS
    print(f"清单 {_rel(manifest_path(ctx.datadir))}（更新于 {m.get('updated_at')}）")
    print(f"  区间 {m.get('start')} ~ {m.get('end')}，来源 {m.get('lister')}，归档文件 {len(m.get('files') or [])} 个")
    sha = tool_sha256()
    dt = m.get("data_tool_sha256") if isinstance(m.get("data_tool_sha256"), list) else [m.get("tool_sha256")]
    print(f"  导入脚本 sha256 {str(m.get('tool_sha256'))[:12]}…" + ("" if m.get("tool_sha256") == sha else
          f"  !! 和现在的 scripts/okx_funding_archive.py（{sha[:12]}…）不一样：脚本改过，用默认区间重新导入一次"
          "（make okx-funding-data；会按现在的脚本重建 2022-03 起的归档资金费和替身标记价格）"))
    if dt != [sha]:
        print(f"  !! 数据目录里的归档资金费 / 替身标记价格不全是现在的脚本做的（data_tool_sha256：{', '.join(str(x)[:12] for x in dt)}）："
              "正式运行之前用默认区间重新导入一次（make okx-funding-data）")
    print(f"  费率列 {m.get('rate_column')}（{'实际结算的费率' if m.get('rate_is_realized') else '!! 不是实际结算的费率'}），"
          f"时间列 {m.get('time_column')}，合约列 {m.get('instrument_column')}")
    if m.get("overlap_mismatch"):
        ov_txt = "  !! overlap_mismatch：归档费率和接口费率对不上"
    elif not m.get("overlap_checked") or not m.get("overlap_rows_total"):
        ov_txt = "  !! 一条都没有比到（overlap_checked = false）：费率列没有核对，不能正式运行"
    else:
        ov_txt = "（对得上）"
    print(f"  重叠核对：{m.get('overlap_rows_total')} 条，差别大的 {m.get('overlap_diff_rows_total')} 条" + ov_txt)
    for key, what in (("no_overlap_pairs", "有归档资金费、却没有和接口数据重叠（没有逐个核对）的币"),
                      ("funding_gap_pairs", "资金费中间有洞的币"), ("mark_gap_pairs", "真实标记价格中间缺了一段的币"),
                      ("provenance_unknown_pairs", "有来源不明的资金费的币"),
                      ("pairs_without_api_funding", "没有接口资金费、用 --allow-no-api-funding 导入的币")):
        lst = m.get(key) or []
        if lst:
            print(f"  !! {what} {len(lst)} 个：{' '.join(lst[:10])}{' ...' if len(lst) > 10 else ''}")
    pairs = m.get("pairs") or {}
    missing = [p for p in ctx.pairs if p not in pairs]
    for pair, s in pairs.items():
        print("  " + _pair_line(pair, s))
    if missing:
        print(f"  !! 配置里有、清单里没有的币 {len(missing)} 个：{' '.join(missing[:10])}（重新运行 update）")
    for w in m.get("warnings") or []:
        print(("  " if w.startswith("!!") else "  · ") + w)
    return EXIT_OK


def cmd_probe(a: argparse.Namespace, http: Http | None = None) -> int:
    http = http or Http()
    fam = a.family.upper()
    if not re.fullmatch(r"[A-Z0-9]+-USDT", fam):
        raise ArgError(f"--family 要写成 BTC-USDT 这样（收到：{a.family}）")
    mm = re.fullmatch(r"(20\d{2})-?(0[1-9]|1[0-2])", a.month or "")
    if not mm:
        raise ArgError(f"--month 要写成 2023-01 这样（收到：{a.month}）")
    month = mm.group(1) + mm.group(2)
    if a.lister == "official":
        reqs = official_requests([fam], [month], int(time.time() * 1000))
        if not reqs:
            raise ArgError(f"--month {a.month} 还没到")
        params, url = reqs[0], OFFICIAL_URL
    else:
        params = {"path": PRIAPI_PATH.format(month=month)}
        url = PRIAPI_URL
    print(f"列表请求：GET {url}?{urllib.parse.urlencode(params)}")
    try:
        raw = http.get(url, params)
    except HttpStatus as e:
        print(f"返回 HTTP {e.status}：\n{_show(e.body)}")
        return EXIT_FORMAT
    print("原始返回（前 2 KB）：")
    print(_show(raw))
    try:
        entries = parse_official_listing(raw) if a.lister == "official" else parse_priapi_listing(raw, month)
    except FormatError as e:
        print(f"!! {e}")
        return EXIT_FORMAT
    print(f"\n找到 {len(entries)} 个文件：")
    for e in entries[:10]:
        print(f"  {e.file_name}  月份 {e.month}  大小 {e.size}  {e.url}")
        if e.extra:
            print(f"     其他字段：{json.dumps(e.extra, ensure_ascii=False)[:300]}")
    if not entries:
        print("!! 一个文件都没有：返回格式认不出来，或者这个月 / 这个币没有归档")
        return EXIT_FORMAT
    pick = next((e for e in entries if family_from_text(e.file_name) == fam), entries[0])
    print(f"\n下载第一个：{pick.url}")
    try:
        body = http.get(pick.url)
    except HttpStatus as e:
        print(f"!! 下载返回 HTTP {e.status}：\n{_show(e.body, 500)}")
        return EXIT_FORMAT
    save_dir = Path(a.save) if a.save else Path(tempfile.mkdtemp(prefix="okx_funding_probe_"))
    save_dir.mkdir(parents=True, exist_ok=True)
    local = save_dir / _safe_name(pick.file_name)
    local.write_bytes(body)
    print(f"  {len(body)} 字节，sha256 {hashlib.sha256(body).hexdigest()}，存在 {_rel(local)}")
    try:
        members = read_archive_file(local)
        print(f"  文件里的内容：{[m for m, _ in members]}")
        mname, text = members[0]
        lines = [ln for ln in text.splitlines() if ln.strip()]
        print("  CSV 表头和前 3 行：")
        for ln in lines[:4]:
            print(f"    {ln[:300]}")
        t = parse_csv_text(text, f"{local.name}:{mname}", None, None, None)
        c = t.columns
        print("  认出来的列：")
        print(f"    合约：{c.inst_name or '（没有这一列，按文件名：' + str(t.inferred_family) + '）'}")
        print(f"    结算时间：{c.time_name}" + ("  !! 没有 funding_time，用的是这一列" if c.time_fallback else ""))
        print(f"    资金费率：{c.rate_name}" + ("（实际结算的费率）" if c.realized else "  !! 不是实际结算的费率（没有 real_funding_rate 一类的列）"))
        fams = sorted({f for f, _, _ in t.rows})
        print(f"  共 {t.n_rows} 行，USDT 永续家族 {len(fams)} 个（例如 {' '.join(fams[:5])}），读不出的 {t.n_bad} 行")
        if t.rows:
            ex = [r for r in t.rows if r[0] == fam][:3] or t.rows[:3]
            for f, ms, r in ex:
                print(f"    {f}  {_iso(ms)}  {r}")
    except FormatError as e:
        print(f"!! 文件格式认不出来：{e}")
        if e.seen:
            print(_show(e.seen))
        return EXIT_FORMAT
    print("\n格式能读。正式导入：make okx-funding-data（或 okx_funding_archive.py update --config configs/config.okx-research-funding.json）")
    return EXIT_OK


# ----------------------------------------------------------------------------
# 入口
# ----------------------------------------------------------------------------
class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:  # 参数错误 -> 退出码 1（argparse 默认是 2，和"网络出错"冲突）
        self.print_usage(sys.stderr)
        print(f"参数错误：{message}", file=sys.stderr)
        raise SystemExit(EXIT_ARGS)


def build_parser() -> argparse.ArgumentParser:
    ap = _Parser(description="OKX 资金费率历史归档：下载 / 合并进单独的数据目录（XSFundingCarry 用）")
    sub = ap.add_subparsers(dest="cmd", required=True, parser_class=_Parser)

    p = sub.add_parser("probe", help="发一次列表请求、下载一个文件，看 OKX 返回的格式")
    p.add_argument("--family", default="BTC-USDT")
    p.add_argument("--month", default="2023-01")
    p.add_argument("--lister", choices=LISTERS, default="official")
    p.add_argument("--save", default=None, help="下载的文件存在哪个目录（默认新建一个临时目录）")

    def common(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--config", action="append", default=[], help="freqtrade 配置（可以写多次）")
        sp.add_argument("--datadir", default=None, help="数据目录（默认取配置里的 \"datadir\"，再没有就是 user_data/data/okx）")
        sp.add_argument("--allow-main-datadir", action="store_true", help="允许写进 user_data/data/okx（一般不要用）")

    u = sub.add_parser("update", help="下载 / 读取归档，合并进数据目录")
    common(u)
    u.add_argument("--start", default=None, help=f"开始日期 YYYYMMDD（默认 {DEFAULT_START}）")
    u.add_argument("--end", default=None, help="结束日期 YYYYMMDD（默认今天，包含这一天）")
    u.add_argument("--from-dir", default=None, help="不联网，读这个目录里的 .zip / .csv")
    u.add_argument("--seed-from", default=None, help="先从这个数据目录复制本目录还没有的合约K线（*-futures）")
    u.add_argument("--no-mark-substitute", action="store_true", help="不补标记价格替身")
    u.add_argument("--allow-no-api-funding", action="store_true",
                   help="还没有接口下载的资金费的币也导入归档（默认拒绝：先导入归档再下载接口数据，两者永远不会重叠，没法核对费率列）")
    u.add_argument("--lister", choices=LISTERS, default="official")

    s = sub.add_parser("seed", help="只复制合约K线（*-futures）")
    common(s)
    s.add_argument("--from", dest="src", required=True, help="从哪个数据目录复制（例如 user_data/data/okx）")

    st = sub.add_parser("status", help="打印清单的小结")
    common(st)
    return ap


def main(argv: list[str] | None = None, http: Http | None = None) -> int:
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    for name in ("freqtrade", "ccxt", "urllib3"):
        logging.getLogger(name).setLevel(logging.ERROR)
    a = build_parser().parse_args(argv)
    try:
        if a.cmd == "probe":
            return cmd_probe(a, http)
        if a.cmd == "update":
            return cmd_update(a, http)
        if a.cmd == "seed":
            return cmd_seed(a)
        return cmd_status(a)
    except ArgError as e:
        print(f"参数错误：{e}", file=sys.stderr)
        return EXIT_ARGS
    except NetError as e:
        print(f"!! 网络出错：{e}", file=sys.stderr)
        print("   数据文件和清单都没有改；已经下载好的归档文件留在缓存里，网络好了以后重新运行会接着下。", file=sys.stderr)
        return EXIT_NET
    except FormatError as e:
        print(f"!! 格式认不出来：{e}", file=sys.stderr)
        if e.seen is not None:
            print("   看到的内容：", file=sys.stderr)
            print(_show(e.seen), file=sys.stderr)
        print("   数据文件和清单都没有改。先运行 make okx-funding-probe 看看 OKX 现在返回什么，再改脚本或手工下载后用 --from-dir。",
              file=sys.stderr)
        return EXIT_FORMAT


if __name__ == "__main__":
    sys.exit(main())
