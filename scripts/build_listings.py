#!/usr/bin/env python3
"""
新币上线日文件：扫描本地完整的 1d 永续K线数据，记下每个币第一根日K线的日期（UTC）。只读数据，不联网。

给第 5 轮的 NewListingShortHedged 用（预注册 docs/round5-preregistration.md 第 2.4 节）：
  "回测：用完整本地数据里这个币的第一根日K线，预先算好存成文件。第一根K线就是数据起点的币（2021 年以前就有的）不算新币。"
回测里策略不能用"加载进来的第一行"当上线日：freqtrade 每段回测只加载"回测区间 - 预热"那一截，
老币的第一行只是这一段的开始，不是它的上线日。所以运行前用本脚本扫一遍完整的本地文件，结果存成 JSON，策略直接读。

输出（默认）：user_data/listings/<数据目录名>_first_candle.json
  数据目录 user_data/data/okx     -> user_data/listings/okx_first_candle.json
  数据目录 user_data/data/binance -> user_data/listings/binance_first_candle.json
  合成数据 user_data/data/okx-synthetic -> user_data/listings/okx-synthetic_first_candle.json（不会盖掉真实数据的文件）
  策略按同样的规则找文件（配置里的 datadir），也可以在配置里写 "nlsh_listings_file" 指定别的路径。

"数据起点"和"老币"：
  第一根日K线 <= 数据起点的币记为 pre_existing（老币，不是新上线的，策略不做空它）。
  数据起点 = --data-start（建议写这次下载用的开始日期，研究脚本知道这个日期）；
  不写时 = 所有币里最早的第一根K线。
  !! 本地数据是分几次、从不同日期开始下载的（例如研究币池从 2019 年起下、其他币从 2020-12 才开始下）时，
     一定要写 --data-start：否则"2020-12 才开始下载的老币"第一根K线晚于最早的那个币，会被当成 2020-12 新上线的币。
     脚本发现"很多币的第一根K线是同一天、而且晚于数据起点"时会提醒（那多半是某次下载的开始日期，不是真的上线日）。

用法：
  .venv/bin/python scripts/build_listings.py --datadir user_data/data/okx --data-start 20201219
  .venv/bin/python scripts/build_listings.py --config configs/config.okx-research-listings.json   # 数据目录按配置的交易所
  .venv/bin/python scripts/build_listings.py --datadir user_data/data/okx-synthetic               # 合成数据自检
  .venv/bin/python scripts/build_listings.py --datadir user_data/data/okx --write-pairs user_data/listings/okx_pairs.json
      # 另外写一个配置片段：本地有 1d 数据的全部 USDT 永续（BTC/USDT:USDT 放最后），
      # 可以用第二个 --config 叠加在 configs/config.okx-research-listings.json 后面直接回测
屏幕上打印中文汇总：每年有多少个新币（按上线年份、按"第 15 天入场"落在哪一年）、已经停止更新（可能已下架）的币。
预注册 3.2 节的管道检查就看这个数：每段明显凑不够 30 笔新币空单时，这一段直接记为"测不了"。

退出码：0 = 写好了；1 = 出错（没有数据等）；2 = 参数错误。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from collections import Counter
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any


ROOT_DIR = Path(__file__).resolve().parent.parent
LISTINGS_DIR = ROOT_DIR / "user_data" / "listings"
FILE_SUFFIX = "_first_candle.json"
FORMAT_VERSION = 1
TIMEFRAME = "1d"
QUOTE = "USDT"
BTC_PAIR = "BTC/USDT:USDT"
# 和策略一致（NewListingShortHedged.entry_delay_days / exit_age_days）：只用来在汇总里说明"入场落在哪一年"
ENTRY_DELAY_DAYS = 14
# 最后一根K线比全部数据的最后一天早这么多天以上 = 停止更新（可能已下架）；和 research_report.py prepare 的口径相同
STALE_DAYS = 3
# 同一天"上线"的币达到这么多个、而且晚于数据起点时提醒：多半是某次下载的开始日期，不是真的上线日
CLUSTER_WARN = 5


def default_output(datadir: Path) -> Path:
    """数据目录 user_data/data/<名字> -> user_data/listings/<名字>_first_candle.json（策略用同样的规则找文件）。"""
    return LISTINGS_DIR / f"{datadir.resolve().name}{FILE_SUFFIX}"


def _ymd(text: str, what: str) -> date:
    try:
        return datetime.strptime(text, "%Y%m%d").replace(tzinfo=UTC).date()
    except ValueError:
        raise SystemExit(f"参数错误：{what} 要写成 YYYYMMDD（收到：{text}）") from None


def _load_config(path: str) -> dict[str, Any]:
    from freqtrade.configuration.load_config import load_from_files

    return load_from_files([path])


def scan_datadir(datadir: Path, fmt: str, quote: str = QUOTE) -> dict[str, dict[str, Any]]:
    """扫描 datadir/futures 下所有 <币>_<quote>_<quote>-1d-futures 文件，返回 {pair: {first, last, candles}}。

    只读：用 freqtrade 自己的数据读取代码（文件名规则、列格式和回测完全相同），不补缺失K线、不改文件。
    """
    from freqtrade.data.history import get_datahandler
    from freqtrade.enums import CandleType, TradingMode

    dh = get_datahandler(datadir, fmt)
    found = sorted(
        {
            p
            for p, tf, ct in dh.ohlcv_get_available_data(datadir, TradingMode.FUTURES)
            if tf == TIMEFRAME and ct == CandleType.FUTURES and p.endswith(f"/{quote}:{quote}")
        }
    )
    out: dict[str, dict[str, Any]] = {}
    for pair in found:
        df = dh._ohlcv_load(pair, TIMEFRAME, timerange=None, candle_type=CandleType.FUTURES)
        if df is None or df.empty:
            continue
        dates = df["date"]
        out[pair] = {
            "first": dates.min().to_pydatetime().astimezone(UTC).date(),
            "last": dates.max().to_pydatetime().astimezone(UTC).date(),
            "candles": len(df),
        }
    return out


def build_document(
    scans: dict[str, dict[str, Any]],
    *,
    exchange: str,
    datadir: Path,
    data_start: date | None,
) -> dict[str, Any]:
    first_min = min(v["first"] for v in scans.values())
    start = data_start or first_min
    pairs: dict[str, dict[str, Any]] = {}
    for pair in sorted(scans):
        v = scans[pair]
        pairs[pair] = {
            "first_candle": v["first"].isoformat(),
            "last_candle": v["last"].isoformat(),
            "candles": v["candles"],
            "pre_existing": bool(v["first"] <= start),
        }
    new = {p: scans[p]["first"] for p, rec in pairs.items() if not rec["pre_existing"]}
    by_year = Counter(str(d.year) for d in new.values())
    by_entry_year = Counter(str((d + timedelta(days=ENTRY_DELAY_DAYS)).year) for d in new.values())
    try:
        datadir_txt = str(datadir.resolve().relative_to(ROOT_DIR))
    except ValueError:
        datadir_txt = str(datadir.resolve())
    return {
        "_note": (
            "由 scripts/build_listings.py 生成，不要手改。每个币第一根 1d 永续K线的日期（UTC）= 上线日（第 1 天）；"
            "pre_existing = 第一根K线 <= 数据起点（老币，不算新上线）。给 NewListingShortHedged 用。"
        ),
        "version": FORMAT_VERSION,
        "exchange": exchange,
        "datadir": datadir_txt,
        "timeframe": TIMEFRAME,
        "candle_type": "futures",
        "data_start": start.isoformat(),
        "data_start_source": "--data-start" if data_start else "最早的第一根K线",
        "generated_utc": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "pairs": pairs,
        "new_listings_per_year": dict(sorted(by_year.items())),
        "new_listings_per_entry_year": dict(sorted(by_entry_year.items())),
    }


def write_json_atomic(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(obj, fh, ensure_ascii=False, indent=1)
            fh.write("\n")
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def pairs_fragment(pairs: list[str], btc: str = BTC_PAIR) -> str:
    """配置片段：全部交易对，BTC 放最后（对冲腿；回测里同一根K线先处理新币空单，再处理 BTC）。"""
    ordered = [p for p in sorted(pairs) if p != btc] + ([btc] if btc in pairs else [])
    body = ",\n".join(f'            "{p}"' for p in ordered)
    return (
        "{\n"
        "    // 由 scripts/build_listings.py --write-pairs 生成，不要手改：本地有 1d 永续数据的全部 USDT 永续，\n"
        "    // BTC/USDT:USDT 放在最后（NewListingShortHedged 的对冲腿）。用第二个 --config 叠加在研究配置后面。\n"
        '    "exchange": {\n'
        '        "pair_whitelist": [\n' + body + "\n        ]\n    }\n}\n"
    )


def print_summary(doc: dict[str, Any], out: Path) -> None:
    pairs = doc["pairs"]
    n_total = len(pairs)
    n_old = sum(1 for r in pairs.values() if r["pre_existing"])
    last_all = max(date.fromisoformat(r["last_candle"]) for r in pairs.values())
    stale = sorted(
        (p, r["last_candle"])
        for p, r in pairs.items()
        if date.fromisoformat(r["last_candle"]) < last_all - timedelta(days=STALE_DAYS)
    )
    print(f"新币上线日（数据目录 {doc['datadir']}，{doc['timeframe']} 永续K线，交易所 {doc['exchange'] or '未知'}）")
    print(f"  数据起点：{doc['data_start']}（{doc['data_start_source']}）；数据最后一天：{last_all}")
    print(f"  一共 {n_total} 个 USDT 永续：老币（第一根K线 <= 数据起点，不算新币）{n_old} 个，新上线 {n_total - n_old} 个")
    if BTC_PAIR not in pairs:
        print(f"  !! 没有 {BTC_PAIR} 的 1d 数据：NewListingShortHedged 没法对冲，不会开新币空单。先下载 BTC 的数据。")
    elif not pairs[BTC_PAIR]["pre_existing"]:
        print(f"  !! {BTC_PAIR} 的第一根K线 {pairs[BTC_PAIR]['first_candle']} 晚于数据起点：数据起点可能写错了（BTC 不可能是新币）")
    by_year = doc["new_listings_per_year"]
    by_entry = doc["new_listings_per_entry_year"]
    years = sorted(set(by_year) | set(by_entry))
    if years:
        print("  每年新上线的币（左：按上线日期；右：第 15 天入场落在这一年的。还要过 7 天平均成交额 >= 100 万 USDT 才会真的做空）：")
        for y in years:
            print(f"    {y} 年：{by_year.get(y, 0):>4} 个    入场在这一年：{by_entry.get(y, 0):>4} 个")
    else:
        print("  没有新上线的币（所有币的第一根K线都 <= 数据起点）")
    if stale:
        shown = " ".join(f"{p.split('/')[0]}({d})" for p, d in stale[:40])
        more = f" 等 {len(stale)} 个" if len(stale) > 40 else ""
        print(f"  数据停止更新（最后一根K线比 {last_all} 早 {STALE_DAYS} 天以上，可能已下架）{len(stale)} 个：{shown}{more}")
        print("    研究脚本默认会去掉这些币；要保留（减少幸存者偏差）用 RESEARCH_KEEP_DELISTED=1（见预注册第 4 节）。")
    start = date.fromisoformat(doc["data_start"])
    clusters = Counter(
        r["first_candle"] for r in pairs.values() if not r["pre_existing"] and date.fromisoformat(r["first_candle"]) > start
    )
    for day, cnt in sorted(clusters.items()):
        if cnt >= CLUSTER_WARN:
            print(
                f"  !! 有 {cnt} 个币的第一根K线都是 {day}（晚于数据起点 {start}）：这多半是某次下载的开始日期，不是真的上线日。"
                f"如果是这样，用 --data-start {day.replace('-', '')}（或更晚）重新生成，否则这些老币会被当成新币做空。"
            )
    print(f"已写入 {out}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="扫描本地 1d 永续K线，生成新币上线日文件（只读数据，不联网）")
    ap.add_argument("--datadir", type=Path, default=None, help="K线数据目录（默认 user_data/data/<配置里的交易所>）")
    ap.add_argument("--config", default=None, help="freqtrade 配置（只用来取交易所名字和数据格式）")
    ap.add_argument("--exchange", default=None, help="交易所名字（只写进文件里备查；默认取配置或数据目录名）")
    ap.add_argument("--data-start", default=None, help="数据起点 YYYYMMDD：第一根K线 <= 这一天的币算老币（建议写下载的开始日期）")
    ap.add_argument("--out", type=Path, default=None, help="输出文件（默认 user_data/listings/<数据目录名>_first_candle.json）")
    ap.add_argument("--format", default=None, choices=("feather", "parquet"), help="K线文件格式（默认取配置，没有配置时 feather）")
    ap.add_argument("--write-pairs", type=Path, default=None, help="另外写一个交易对配置片段（全部 USDT 永续，BTC 放最后）")
    args = ap.parse_args(argv)

    try:
        import freqtrade  # noqa: F401
    except ImportError:
        print("错误：需要 freqtrade 环境。请用 .venv/bin/python 运行本脚本。", file=sys.stderr)
        return 2

    cfg: dict[str, Any] = {}
    if args.config:
        try:
            cfg = _load_config(args.config)
        except Exception as exc:  # noqa: BLE001 — 给出中文原因
            print(f"参数错误：读不了配置 {args.config}：{exc}", file=sys.stderr)
            return 2
    exchange = args.exchange or str((cfg.get("exchange") or {}).get("name") or "")
    if args.datadir is None:
        if not exchange:
            print("参数错误：要写 --datadir，或者用 --config / --exchange 给出交易所（默认数据目录 user_data/data/<交易所>）", file=sys.stderr)
            return 2
        datadir = ROOT_DIR / "user_data" / "data" / exchange
    else:
        datadir = args.datadir if args.datadir.is_absolute() else (Path.cwd() / args.datadir)
    if not exchange:
        exchange = datadir.resolve().name.split("-")[0]
    fmt = args.format or str(cfg.get("dataformat_ohlcv") or "feather")
    data_start = _ymd(args.data_start, "--data-start") if args.data_start else None

    if not (datadir / "futures").is_dir():
        print(f"错误：{datadir}/futures 不存在（还没有下载永续合约数据？）", file=sys.stderr)
        return 1
    out = args.out or default_output(datadir)
    out = out if out.is_absolute() else (Path.cwd() / out)
    for target in (out, args.write_pairs):
        if target is None:
            continue
        t = target if target.is_absolute() else (Path.cwd() / target)
        if datadir.resolve() in t.resolve().parents:
            print(f"参数错误：不能把结果写进数据目录 {datadir}（本脚本对数据只读）", file=sys.stderr)
            return 2

    scans = scan_datadir(datadir, fmt)
    if not scans:
        print(f"错误：{datadir}/futures 里没有 {QUOTE} 永续的 {TIMEFRAME} K线文件（*.{fmt}）", file=sys.stderr)
        return 1
    if data_start is not None and data_start < min(v["first"] for v in scans.values()) - timedelta(days=366):
        print(f"提示：--data-start {data_start} 比所有数据都早一年以上，所有币都会按第一根K线算上线日（老币也可能被当成新币）。")
    doc = build_document(scans, exchange=exchange, datadir=datadir, data_start=data_start)
    write_json_atomic(out, doc)
    print_summary(doc, out)
    if args.write_pairs is not None:
        wp = args.write_pairs if args.write_pairs.is_absolute() else (Path.cwd() / args.write_pairs)
        wp.parent.mkdir(parents=True, exist_ok=True)
        wp.write_text(pairs_fragment(list(scans)), encoding="utf-8")
        print(f"交易对配置片段（{len(scans)} 个，{BTC_PAIR} 在最后）已写入 {wp}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
