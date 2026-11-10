#!/usr/bin/env python3
"""
从 OKX 合约信息的快照生成 NewListingShortHedged 的排除清单：不是加密币的 USDT 永续（股票、ETF、指数、商品、外汇……）。
只读快照文件，不联网（快照在能连 OKX 的机器上导出，见下面"快照怎么来"）。

为什么要排除（预注册文档 6.7 节，2026-11-10，运行 B 的数据检查之后、正式运行之前，没看过任何回测收益）：
  规格 2.4 节的假设和依据都是加密新币（上线时炒作高、流通量小、不断解锁）。OKX 2026 年起成批上线股票、ETF、指数、商品永续，
  按"第一根K线 = 上线日"它们都会被当成新币做空，而且占 2026 年新合约的大多数，会主导样本外和走步后几折。
  所以按交易所自己给的分类（instCategory）把它们排除；策略代码不变，只是上线日文件里把它们记成"不算新币"。

规则：instId 以 -USDT-SWAP 结尾、instCategory 不是 "1"（1 = 加密货币）的合约全部排除，原因写成分类的中文名。
  instCategory：1 加密货币、3 股票、4 商品、5 外汇、6 债券（OKX API 文档）；不认识的代码照样排除，原因写"非加密（instCategory X）"。
  快照里没有 instCategory 字段 = 出错退出（不猜）。

快照怎么来（在 VPS 上，只读公开接口，不要 API Key）：
  GET https://www.okx.com/api/v5/public/instruments?instType=SWAP，把 data 里 instId 以 -USDT-SWAP 结尾的项原样存成 JSON 列表。

用法：
  python3 scripts/okx_noncrypto_pairs.py --instruments <快照.json> --out configs/nlsh-exclude.okx.json
  python3 scripts/okx_noncrypto_pairs.py --instruments <快照.json> --check configs/nlsh-exclude.okx.json   # 只核对，不写

退出码：0 = 写好了 / 核对一致；1 = 快照不对（没有 instCategory 等）或核对不一致；2 = 参数错误。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any

FORMAT_VERSION = 1  # 和 scripts/build_listings.py 的 EXCLUDE_FORMAT_VERSION 一致
SUFFIX = "-USDT-SWAP"
CRYPTO = "1"
CATEGORY_CN = {"1": "加密货币", "3": "股票", "4": "商品", "5": "外汇", "6": "债券"}
BTC_PAIR = "BTC/USDT:USDT"


def load_snapshot(path: Path) -> list[dict[str, Any]]:
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"读不了快照 {path}：{exc}") from None
    if isinstance(doc, dict) and isinstance(doc.get("data"), list):  # 接口的原始返回也认
        doc = doc["data"]
    if not isinstance(doc, list) or not doc:
        raise ValueError(f"快照 {path} 不是合约信息的列表（或者是空的）")
    rows = [x for x in doc if isinstance(x, dict) and str(x.get("instId", "")).endswith(SUFFIX)]
    if not rows:
        raise ValueError(f"快照 {path} 里没有 *{SUFFIX} 合约")
    missing = [x["instId"] for x in rows if str(x.get("instCategory", "")).strip() == ""]
    if missing:
        raise ValueError(f"快照里有 {len(missing)} 个合约没有 instCategory（例如 {' '.join(missing[:5])}）：没法按交易所的分类排除，不猜")
    return rows


def build(rows: list[dict[str, Any]], snapshot: Path, snapshot_sha: str) -> dict[str, Any]:
    pairs: dict[str, str] = {}
    for x in rows:
        cat = str(x["instCategory"]).strip()
        if cat == CRYPTO:
            continue
        base = str(x["instId"])[: -len(SUFFIX)]
        pairs[f"{base}/USDT:USDT"] = CATEGORY_CN.get(cat, f"非加密（instCategory {cat}）")
    if BTC_PAIR in pairs:
        raise ValueError(f"快照里 BTC-USDT-SWAP 的 instCategory 不是 {CRYPTO}：快照不对")
    counts = Counter(pairs.values())
    return {
        "_note": (
            "由 scripts/okx_noncrypto_pairs.py 从 OKX 合约信息快照生成，不要手改。NewListingShortHedged 的排除清单："
            "这些 USDT 永续不是加密币（instCategory 不是 1），scripts/build_listings.py 把它们记成不算新币（不做空、不算对冲窗口）。"
            "预注册文档 6.7 节。"
        ),
        "version": FORMAT_VERSION,
        "exchange": "okx",
        "rule": "instId 以 -USDT-SWAP 结尾、instCategory 不是 \"1\"（加密货币）的合约",
        "source": {
            "endpoint": "GET /api/v5/public/instruments?instType=SWAP",
            "snapshot": snapshot.name,
            "snapshot_sha256": snapshot_sha,
            "usdt_swaps": len(rows),
            "categories": dict(sorted(Counter(str(x["instCategory"]).strip() for x in rows).items())),
        },
        "counts": dict(sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))),
        "pairs": dict(sorted(pairs.items())),
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="从 OKX 合约信息快照生成 NewListingShortHedged 的排除清单（不联网）")
    ap.add_argument("--instruments", type=Path, required=True, help="OKX 合约信息快照（JSON 列表，或接口原始返回）")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--out", type=Path, help="写排除清单到这个文件")
    g.add_argument("--check", type=Path, help="只核对：这个排除清单是不是由这个快照生成的（pairs 一样）")
    a = ap.parse_args(argv)
    try:
        rows = load_snapshot(a.instruments)
        doc = build(rows, a.instruments, hashlib.sha256(a.instruments.read_bytes()).hexdigest())
    except ValueError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1
    if a.check:
        try:
            have = json.loads(a.check.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            print(f"错误：读不了 {a.check}：{exc}", file=sys.stderr)
            return 1
        if have.get("pairs") != doc["pairs"]:
            extra = sorted(set(have.get("pairs") or {}) - set(doc["pairs"]))
            gone = sorted(set(doc["pairs"]) - set(have.get("pairs") or {}))
            print(f"不一致：清单多了 {len(extra)} 个（{' '.join(extra[:10])}），少了 {len(gone)} 个（{' '.join(gone[:10])}）")
            return 1
        print(f"一致：{a.check} 的 {len(doc['pairs'])} 个合约和快照 {a.instruments} 生成的一样")
        return 0
    a.out.parent.mkdir(parents=True, exist_ok=True)
    a.out.write_text(json.dumps(doc, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(f"USDT 永续 {len(rows)} 个，分类 {doc['source']['categories']}")
    print(f"排除 {len(doc['pairs'])} 个：" + "、".join(f"{k} {v} 个" for k, v in doc["counts"].items()))
    print(f"已写入 {a.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
