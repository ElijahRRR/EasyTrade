#!/usr/bin/env python3
"""
【合成测试数据】生成器 —— 只用来检查流程能不能跑通，不是真实行情！

!!! 用它跑出来的回测收益、回撤、胜率全部没有意义，不能用来评价或挑选策略 !!!

用途：在连不上交易所的机器上（例如本项目的开发容器），也能把
  回测 -> 回撤检查 -> lookahead / recursive 分析
整条流程跑一遍，确认配置、脚本、策略代码没有报错。

生成的内容（freqtrade 磁盘格式，文件名、列名、K 线类型都按 freqtrade 2026.10 源码确定）：
  user_data/data/okx-synthetic/futures/<币>_USDT_USDT-<周期>-futures.feather   K 线（5m 15m 1h 4h 1d；
      5m / 15m 默认只有最近 730 天，见 --short-tf-days）
  user_data/data/okx-synthetic/futures/<币>_USDT_USDT-1h-mark.feather          标记价格（只有最近约 92 天）
  user_data/data/okx-synthetic/futures/<币>_USDT_USDT-1h-funding_rate.feather  资金费率（只有最近约 92 天，
      每次结算一行：大部分币每 8 小时一次，个别新币每 4 小时一次）
  user_data/data/okx-synthetic/<币>_USDT-1h.feather     现货 1h K 线（第 5 轮 PremiumFade 用；freqtrade 把现货放在数据目录根目录，
      合约放在 futures/ 下），偶尔有人为制造的"永续相对现货溢价"极端值；--no-spot 不生成
  user_data/data/okx-synthetic/synthetic_markets.json   假的交易对规格 + 杠杆档位（给 scripts/ft_offline.py 用）
  user_data/data/okx-synthetic/synthetic_pairs.json     交易对配置片段：数据一直到最后、交易对规格里"在交易"的永续，
      BTC 放最后（自检时用第二个 --config 叠加在研究配置后面，例如 XSFundingCarry 的未来函数检查）
  user_data/data/okx-synthetic/SYNTHETIC_DATA_README.txt  标记文件：说明这是合成数据（里面有 SYNTHETIC_FORMAT_VERSION=2）

  --exchange binance：生成"像币安"的一套（默认放在 user_data/data/binance-synthetic，给 configs/config.binance-research.json 用）：
      研究币池里的 25 个币（XSFundingCarry 至少要 15 个合格的币才开仓），周期 1h 4h 1d，
      标记价格和资金费率覆盖全部区间（币安能下到 2019-09 以来的资金费），大部分币 8 小时结算、
      几个币 4 小时结算、一个币中途从 8 小时改成 4 小时（币安 2023 年以后真的这样改过），交易对规格按币安的样子
      （每张 = 1 个币，最小下单额 BTC 100 USDT、ETH 20 USDT、其他 5 USDT），默认不生成现货。

为什么单独放在 okx-synthetic 目录、不放进 user_data/data/okx：
  freqtrade download-data 会在已有文件的末尾"增量追加"。如果合成数据放在真实数据目录里，
  以后下载真实数据时会接在假数据后面，真假混在一起而且很难发现。所以这里强制分开，
  并且拒绝往没有标记文件的目录里写。

行情模型（尽量像真的，但完全是随机数）：
  * 日级"行情状态"马尔可夫链：牛市 / 熊市 / 震荡 / 急跌 / 急涨，每段持续几天到几个月
  * 波动率聚集（小时级随机波动率）+ 肥尾收益（t 分布）+ 日内时段效应（美盘/亚盘更活跃，周末清淡）
  * 全市场共同因子 + 各币的 beta 和独立波动（山寨币波动更大）
  * 不定期的"连环爆仓"：单根 5 分钟 K 线放量几十到几百倍并大幅下跌（偶尔是轧空上涨），
    急跌/熊市里更频繁 —— 这样 VolumeSpikeShort / BigTrendShort / DailyThreeSoldiersShort 都会有信号
  * 每个币一段 1 小时的"交易所维护"缺口（测试 freqtrade 补齐缺失 K 线）
  * 价格按 OKX 的最小价格跳动取整，成交量按合约张数取整，成交量单位是"币的数量"（与 freqtrade 一致）
  * 有几个币"上线晚"（--listings，默认 SOL 2021-06-01、AVAX 2022-09-15、ADA 2024-06-01）：那天之前没有任何数据，
    用来测试研究脚本对上线晚的币的处理（scripts/research.sh：样本外开始前数据够 365 天的保留，不够的去掉）
  第 5 轮新加（都用单独的随机数生成；加 --legacy 时原来那 8 个币的数据和以前一模一样）：
  * 价格和数量按最小跳动取整后，再按跳动的小数位数取整一次，得到和交易所数据一样的"十进制"浮点数
    （以前会有 0.034820000000000004 这样的尾巴：freqtrade 回测按 0.03482 挂单，开盘价正好是最低价的那根K线上
    这张单就差一点点成交不了，例如回测结束时的强制平仓单，lookahead-analysis 会因此误报"入场偏差"）
  * 全市场暴跌日（--crash-days，默认 8 天，均匀分布在整个区间里，包括 2020 年）：所有币同一天下跌约 14% ~ 24%
    （BTC 也跌），成交量放大 3 ~ 7 倍，之后几天部分反弹 —— CapitulationRebound 有信号；暴跌时永续相对现货折价
  * 新上线的合成币 NEW1 ~ NEW10（名字故意和真实币不一样，研究币池 pairs.okx-research.json 里没有它们，
    所以原来的研究流程不受影响；configs/config.okx-research-listings.json 的"全部永续"会用到）：上线后先炒高、
    之后 4 个月里慢慢跌（NEW4 上线后第 3 周被轧空涨 80% 左右，会打到 NewListingShortHedged 的 40% 止损），
    其中 NEW2、NEW5、NEW8 中途"下架"（那天以后没有数据）：NEW2、NEW5 在交易对规格里标成"不活跃"，
    NEW8 交易对规格里没有它（真实的 OKX 删掉下架合约后就是这样，freqtrade 回测拿不到它的杠杆档位）；
    NEW9 2020-07 上线（研究开始之前的留出段里的新币），NEW10 2025-12 上线（样本外的新币）
  * 现货（--spot，OKX 默认生成）：现货价格 = 永续价格 / (1 + 溢价)，溢价平时在 +0.02% 附近小幅波动，
    每个币每年约 6 次"极端溢价"（±0.4% ~ ±1.5%，持续十几个小时），暴跌日永续折价；NEW3 没有现货，
    DOGE 现货 2023-04-10 ~ 2023-04-12 缺 3 天（测试"不向前填充"），现货成交量约为永续的 40%

默认区间（第 3 轮起）：2020-01-01 ~ 今天。研究从 2021-01-01 开始，前面那一年只用来给指标预热，
和 VPS 上真实数据的情况一样（下载时多下约 600 天给指标预热）。

用法（需要 freqtrade 环境，因为直接用 freqtrade 自己的数据写入代码保证格式正确）：
  .venv/bin/python scripts/make_synthetic_data.py                 # 默认：8 个币 + NEW1~NEW10、2020-01-01 至今、现货、暴跌日
  .venv/bin/python scripts/make_synthetic_data.py --exchange binance   # 币安的一套 → user_data/data/binance-synthetic
  .venv/bin/python scripts/make_synthetic_data.py --days 730 --listings ""   # 第 2 轮的样子：最近 2 年、所有币同一天开始
  .venv/bin/python scripts/make_synthetic_data.py --days 365 --seed 7
  .venv/bin/python scripts/make_synthetic_data.py --futures-history-days 0   # 标记价格/资金费率覆盖全部区间
  .venv/bin/python scripts/make_synthetic_data.py --legacy        # 第 4 轮以前的样子（8 个币、没有现货/暴跌日/新币，数据和以前完全相同）
  然后：make synthetic-test   （或 bash scripts/synthetic_test.sh），见 README「用合成数据自检」
"""

from __future__ import annotations

import argparse
import json
import math
import shutil
import sys
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUT = ROOT / "user_data" / "data" / "okx-synthetic"
MARKER_NAME = "SYNTHETIC_DATA_README.txt"
MARKETS_NAME = "synthetic_markets.json"
PAIRS_NAME = "synthetic_pairs.json"
# 标记文件里的格式版本（scripts/research.sh 看到旧版本的合成数据会自动重新生成）：
#   1 = 第 4 轮以前；2 = 第 5 轮（现货、暴跌日、新币/下架币、4 小时资金费、--exchange binance）
FORMAT_VERSION = 2
EXCHANGES = ("okx", "binance")
DEFAULT_TIMEFRAMES = ("5m", "15m", "1h", "4h", "1d")
BINANCE_TIMEFRAMES = ("1h", "4h", "1d")
BARS_PER_DAY = 288  # 5 分钟 K 线

# 交易对规格（数值参考 OKX USDT 永续的公开规格，只用来让 freqtrade 能算精度/最小下单量）
#   p0 = 起始价, tick = 最小价格跳动, ct = 每张合约对应的币数, lot = 最小下单张数,
#   beta = 对全市场因子的弹性, idio = 独立日波动, qv = 5 分钟平均成交额 (USDT), lev = 最大杠杆
PAIR_SPECS: dict[str, dict[str, float]] = {
    "BTC": {"p0": 70000.0, "tick": 0.1, "ct": 0.01, "lot": 0.01, "beta": 1.00, "idio": 0.004, "qv": 25e6, "lev": 100},
    "ETH": {"p0": 2500.0, "tick": 0.01, "ct": 0.1, "lot": 0.01, "beta": 1.15, "idio": 0.012, "qv": 12e6, "lev": 100},
    "SOL": {"p0": 170.0, "tick": 0.01, "ct": 1.0, "lot": 0.01, "beta": 1.40, "idio": 0.020, "qv": 4e6, "lev": 50},
    "XRP": {"p0": 0.55, "tick": 0.0001, "ct": 100.0, "lot": 1.0, "beta": 1.10, "idio": 0.022, "qv": 2.5e6, "lev": 75},
    "DOGE": {"p0": 0.17, "tick": 0.00001, "ct": 1000.0, "lot": 1.0, "beta": 1.50, "idio": 0.025, "qv": 2e6, "lev": 75},
    "LINK": {"p0": 12.0, "tick": 0.001, "ct": 1.0, "lot": 1.0, "beta": 1.30, "idio": 0.020, "qv": 0.8e6, "lev": 50},
    "ADA": {"p0": 0.45, "tick": 0.0001, "ct": 100.0, "lot": 1.0, "beta": 1.25, "idio": 0.022, "qv": 1.5e6, "lev": 75},
    "AVAX": {"p0": 30.0, "tick": 0.001, "ct": 1.0, "lot": 1.0, "beta": 1.45, "idio": 0.024, "qv": 1.2e6, "lev": 50},
    # ---- 第 5 轮：币安那一套（研究币池里的币）再加 17 个 ----
    "LTC": {"p0": 85.0, "tick": 0.01, "ct": 1.0, "lot": 1.0, "beta": 1.05, "idio": 0.018, "qv": 0.9e6, "lev": 75},
    "BCH": {"p0": 350.0, "tick": 0.01, "ct": 0.1, "lot": 1.0, "beta": 1.10, "idio": 0.020, "qv": 0.7e6, "lev": 75},
    "DOT": {"p0": 6.5, "tick": 0.001, "ct": 1.0, "lot": 1.0, "beta": 1.30, "idio": 0.022, "qv": 0.8e6, "lev": 50},
    "TRX": {"p0": 0.12, "tick": 0.00001, "ct": 1000.0, "lot": 1.0, "beta": 0.70, "idio": 0.015, "qv": 0.6e6, "lev": 75},
    "NEAR": {"p0": 5.0, "tick": 0.001, "ct": 10.0, "lot": 1.0, "beta": 1.45, "idio": 0.026, "qv": 0.7e6, "lev": 50},
    "FIL": {"p0": 5.0, "tick": 0.001, "ct": 0.1, "lot": 1.0, "beta": 1.35, "idio": 0.025, "qv": 0.6e6, "lev": 50},
    "ATOM": {"p0": 8.0, "tick": 0.001, "ct": 1.0, "lot": 1.0, "beta": 1.25, "idio": 0.022, "qv": 0.6e6, "lev": 50},
    "ETC": {"p0": 25.0, "tick": 0.001, "ct": 10.0, "lot": 1.0, "beta": 1.20, "idio": 0.022, "qv": 0.6e6, "lev": 50},
    "UNI": {"p0": 8.0, "tick": 0.001, "ct": 1.0, "lot": 1.0, "beta": 1.35, "idio": 0.024, "qv": 0.6e6, "lev": 50},
    "AAVE": {"p0": 150.0, "tick": 0.01, "ct": 0.1, "lot": 1.0, "beta": 1.40, "idio": 0.026, "qv": 0.5e6, "lev": 50},
    "XLM": {"p0": 0.11, "tick": 0.00001, "ct": 100.0, "lot": 1.0, "beta": 1.05, "idio": 0.020, "qv": 0.5e6, "lev": 50},
    "INJ": {"p0": 20.0, "tick": 0.001, "ct": 0.1, "lot": 1.0, "beta": 1.60, "idio": 0.030, "qv": 0.5e6, "lev": 50},
    "OP": {"p0": 1.8, "tick": 0.0001, "ct": 1.0, "lot": 1.0, "beta": 1.55, "idio": 0.030, "qv": 0.6e6, "lev": 50},
    "APT": {"p0": 8.0, "tick": 0.001, "ct": 1.0, "lot": 1.0, "beta": 1.50, "idio": 0.030, "qv": 0.6e6, "lev": 50},
    "ARB": {"p0": 1.1, "tick": 0.0001, "ct": 10.0, "lot": 1.0, "beta": 1.50, "idio": 0.030, "qv": 0.7e6, "lev": 50},
    "SUI": {"p0": 1.2, "tick": 0.0001, "ct": 1.0, "lot": 1.0, "beta": 1.60, "idio": 0.032, "qv": 0.8e6, "lev": 50},
    "TIA": {"p0": 6.0, "tick": 0.001, "ct": 1.0, "lot": 1.0, "beta": 1.60, "idio": 0.034, "qv": 0.5e6, "lev": 50},
    # ---- 第 5 轮：合成的"新上线币"（名字故意和真实币不同；研究币池里没有，只有"全部永续"的配置会用到）----
    "NEW1": {"p0": 2.0, "tick": 0.0001, "ct": 10.0, "lot": 1.0, "beta": 1.60, "idio": 0.035, "qv": 0.4e6, "lev": 50},
    "NEW2": {"p0": 0.8, "tick": 0.0001, "ct": 10.0, "lot": 1.0, "beta": 1.70, "idio": 0.040, "qv": 0.3e6, "lev": 50},
    "NEW3": {"p0": 15.0, "tick": 0.001, "ct": 1.0, "lot": 1.0, "beta": 1.50, "idio": 0.035, "qv": 0.5e6, "lev": 50},
    "NEW4": {"p0": 0.35, "tick": 0.00001, "ct": 100.0, "lot": 1.0, "beta": 1.70, "idio": 0.040, "qv": 0.4e6, "lev": 50},
    "NEW5": {"p0": 4.0, "tick": 0.001, "ct": 1.0, "lot": 1.0, "beta": 1.60, "idio": 0.038, "qv": 0.3e6, "lev": 50},
    "NEW6": {"p0": 1.5, "tick": 0.0001, "ct": 10.0, "lot": 1.0, "beta": 1.60, "idio": 0.035, "qv": 0.5e6, "lev": 50},
    "NEW7": {"p0": 0.05, "tick": 0.000001, "ct": 1000.0, "lot": 1.0, "beta": 1.80, "idio": 0.040, "qv": 0.4e6, "lev": 50},
    "NEW8": {"p0": 3.0, "tick": 0.001, "ct": 1.0, "lot": 1.0, "beta": 1.60, "idio": 0.038, "qv": 0.3e6, "lev": 50},
    "NEW9": {"p0": 1.2, "tick": 0.0001, "ct": 10.0, "lot": 1.0, "beta": 1.60, "idio": 0.035, "qv": 0.4e6, "lev": 50},
    "NEW10": {"p0": 0.6, "tick": 0.0001, "ct": 10.0, "lot": 1.0, "beta": 1.70, "idio": 0.038, "qv": 0.4e6, "lev": 50},
}
# 第 4 轮以前的 8 个币（顺序也不能变：随机数按这个顺序抽，换了顺序数据就和以前不一样）
LEGACY_PAIRS = ("BTC", "ETH", "SOL", "XRP", "DOGE", "LINK", "ADA", "AVAX")
# 加新的币只能加在最后（随机数按顺序抽，加在中间会让后面的币的数据变掉）
NEW_COINS = ("NEW1", "NEW2", "NEW3", "NEW4", "NEW5", "NEW6", "NEW7", "NEW8", "NEW9", "NEW10")
DEFAULT_PAIRS: dict[str, tuple[str, ...]] = {
    "okx": LEGACY_PAIRS + NEW_COINS,
    "binance": LEGACY_PAIRS
    + ("LTC", "BCH", "DOT", "TRX", "NEAR", "FIL", "ATOM", "ETC", "UNI", "AAVE", "XLM", "INJ", "OP", "APT", "ARB", "SUI", "TIA"),
}
DEFAULT_START = "20200101"
# 上线晚的币（那天之前没有数据）：一个在研究开始后不久、一个在训练段中间（都保留），一个离样本外太近（会被去掉）
LEGACY_LISTINGS = "SOL:20210601 AVAX:20220915 ADA:20240601"
# 第 5 轮：写成 币:上线日[:下架日]（下架日那天起没有数据）。NEW5 上线后第 112 天下架（新币空单还没平就没有数据了），
# NEW8 在样本外才上线（之后下架、交易对规格里没有）；NEW9 在 2020 年上线（研究开始之前的留出段里有新币），
# NEW10 在样本外上线、一直在交易（样本外和走步里有新币空单）
DEFAULT_LISTINGS: dict[str, str] = {
    "okx": LEGACY_LISTINGS
    + " NEW1:20210310 NEW2:20211105:20230315 NEW3:20220620 NEW4:20230208 NEW5:20230920:20240110"
    " NEW6:20240305 NEW7:20240801 NEW8:20250415:20260120 NEW9:20200701 NEW10:20251201",
    "binance": "AVAX:20200922 OP:20220601 APT:20221019 ARB:20230323 SUI:20230503 TIA:20231031",
}
# 下架的币在交易对规格里怎么写：inactive = 还在，标成"不活跃"（ccxt 对币安的样子）；
# omit = 交易对规格里没有（OKX 删掉下架合约以后的样子：freqtrade 回测拿不到它的杠杆档位，会报错退出）
DELISTED_MARKETS: dict[str, str] = {"NEW2": "inactive", "NEW5": "inactive", "NEW8": "omit"}
# 资金费结算间隔（小时）。没写的 = 8；"8>4@YYYYMMDD" = 那天起从 8 小时改成 4 小时
FUNDING_SCHEDULE: dict[str, dict[str, str]] = {
    "okx": {"NEW2": "4", "NEW6": "4"},
    "binance": {"DOT": "4", "INJ": "4", "SUI": "4", "TIA": "4", "FIL": "8>4@20231101"},
}
# 现货：没有现货的币；现货缺数据的日期段（含头不含尾）
NO_SPOT = ("NEW3",)
SPOT_GAPS: dict[str, tuple[str, str]] = {"DOGE": ("20230410", "20230413")}
# 币安的最小下单额（USDT），预注册 4 节：BTC 100、ETH 20，其他 5
BINANCE_MIN_COST = {"BTC": 100.0, "ETH": 20.0}
DEFAULT_CRASH_DAYS = 8
# 5m / 15m 只有 5m 策略和 --detail 用到，6 年全生成又大又慢（每个币 70 多万根）
DEFAULT_SHORT_TF_DAYS = 730
SHORT_TFS = ("5m", "15m")

# 行情状态：日漂移 mu、日波动 sigma、持续天数范围、爆仓事件概率（每天）与下跌占比、成交量倍数
REGIMES: dict[str, dict] = {
    "bull": {"mu": 0.0035, "sigma": 0.028, "days": (20, 80), "ev": 0.04, "down": 0.45, "vol": 1.0},
    "bear": {"mu": -0.0040, "sigma": 0.034, "days": (20, 70), "ev": 0.08, "down": 0.85, "vol": 1.2},
    "chop": {"mu": 0.0, "sigma": 0.020, "days": (10, 40), "ev": 0.03, "down": 0.60, "vol": 0.8},
    "crash": {"mu": -0.0250, "sigma": 0.060, "days": (3, 9), "ev": 0.40, "down": 0.92, "vol": 2.2},
    "rally": {"mu": 0.0180, "sigma": 0.050, "days": (3, 9), "ev": 0.15, "down": 0.30, "vol": 1.8},
}
TRANSITIONS: dict[str, tuple[tuple[str, ...], tuple[float, ...]]] = {
    "bull": (("chop", "bear", "crash", "rally"), (0.45, 0.20, 0.20, 0.15)),
    "bear": (("chop", "bull", "crash", "rally"), (0.40, 0.15, 0.30, 0.15)),
    "chop": (("bull", "bear", "crash", "rally"), (0.35, 0.35, 0.18, 0.12)),
    "crash": (("bear", "chop", "rally"), (0.50, 0.35, 0.15)),
    "rally": (("bull", "chop", "crash"), (0.50, 0.35, 0.15)),
}


# ---------------------------------------------------------------------------
# 生成
# ---------------------------------------------------------------------------


def regime_path(n_days: int, rng: np.random.Generator) -> list[str]:
    """逐日的行情状态。"""
    out: list[str] = []
    cur = str(rng.choice(["bull", "bear", "chop"]))
    while len(out) < n_days:
        lo, hi = REGIMES[cur]["days"]
        out.extend([cur] * int(rng.integers(lo, hi + 1)))
        nxt, probs = TRANSITIONS[cur]
        cur = str(rng.choice(nxt, p=probs))
    return out[:n_days]


def intraday_profile(index: pd.DatetimeIndex) -> tuple[np.ndarray, np.ndarray]:
    """日内时段效应：返回 (波动倍数, 成交量倍数)。美盘开盘、亚盘开盘最活跃，周末清淡。"""
    hour = index.hour.to_numpy() + index.minute.to_numpy() / 60.0
    us = np.exp(-0.5 * ((hour - 14.5) / 1.5) ** 2)  # 13:30 UTC 美股开盘前后
    asia = np.exp(-0.5 * ((hour - 1.0) / 1.2) ** 2)  # 01:00 UTC 亚盘
    eu = np.exp(-0.5 * ((hour - 8.0) / 1.5) ** 2)  # 08:00 UTC 欧盘
    act = 0.75 + 0.55 * us + 0.30 * asia + 0.20 * eu
    weekend = index.day_of_week.to_numpy() >= 5
    vol_mult = act * np.where(weekend, 0.80, 1.0)
    volu_mult = act**1.5 * np.where(weekend, 0.60, 1.0)
    # 归一化，使平均值约为 1
    return vol_mult / vol_mult.mean(), volu_mult / volu_mult.mean()


def stochastic_vol(n_bars: int, rng: np.random.Generator) -> np.ndarray:
    """小时级 AR(1) 对数波动率 -> 每根 5 分钟 K 线的波动倍数（波动率聚集）。"""
    n_hours = n_bars // 12 + 2
    phi, eta = 0.985, 0.12
    shocks = rng.normal(0.0, eta, size=n_hours)
    h = np.empty(n_hours)
    h[0] = 0.0
    for i in range(1, n_hours):
        h[i] = phi * h[i - 1] + shocks[i]
    var = eta**2 / (1 - phi**2)
    mult = np.exp(h - var / 2.0)
    return np.repeat(mult, 12)[:n_bars]


def t_noise(rng: np.random.Generator, size: int, df: float = 4.0) -> np.ndarray:
    """单位方差的 t 分布噪声（肥尾）。"""
    return rng.standard_t(df, size=size) / math.sqrt(df / (df - 2.0))


def pick_event_bars(day_idx: int, n: int, rng: np.random.Generator) -> np.ndarray:
    """某一天里选 n 根 5 分钟 K 线作为事件（偏向美盘时段）。"""
    w = np.ones(BARS_PER_DAY)
    w[156:204] *= 3.0  # 13:00-17:00 UTC
    w /= w.sum()
    return day_idx * BARS_PER_DAY + rng.choice(BARS_PER_DAY, size=n, replace=False, p=w)


def build_market(n_bars: int, n_days: int, regimes: list[str], rng: np.random.Generator):
    """全市场因子（5 分钟对数收益）+ 全市场爆仓事件。"""
    mu_d = np.array([REGIMES[r]["mu"] for r in regimes])
    sig_d = np.array([REGIMES[r]["sigma"] for r in regimes])
    mu = np.repeat(mu_d, BARS_PER_DAY)[:n_bars] / BARS_PER_DAY
    sig = np.repeat(sig_d, BARS_PER_DAY)[:n_bars] / math.sqrt(BARS_PER_DAY)

    events: list[tuple[int, float, float]] = []  # (bar, 对数收益冲击, 成交量倍数)
    for d, r in enumerate(regimes):
        cfg = REGIMES[r]
        if rng.random() < cfg["ev"]:
            for bar in pick_event_bars(d, int(rng.integers(1, 3)), rng):
                down = rng.random() < cfg["down"]
                size = rng.uniform(0.008, 0.035)
                vmult = math.exp(rng.uniform(math.log(8), math.log(150)))
                events.append((int(bar), -size if down else size * 0.8, vmult))
    return mu, sig, events


def apply_events(rets, vol_boost, volu_mult, events, beta, rng):
    """把事件写进某个币的收益 / 成交量：事件 K 线大幅波动 + 巨量，之后十几根 K 线余波递减。"""
    n = len(rets)
    for bar, shock, vmult in events:
        if bar >= n - 1:
            continue
        rets[bar] += shock * beta * rng.uniform(0.7, 1.3)
        volu_mult[bar] *= vmult * rng.uniform(0.6, 1.4)
        tail = np.arange(1, 25)
        idx = bar + tail
        idx = idx[idx < n]
        decay = np.exp(-tail[: len(idx)] / 6.0)
        vol_boost[idx] *= 1.0 + 1.5 * decay
        volu_mult[idx] *= 1.0 + 0.08 * vmult * decay


def integrate_with_reversion(rets: np.ndarray, p0: float, band: float = math.log(3.0)) -> np.ndarray:
    """把收益累加成对数价格。价格离起始价超过 3 倍 (上下) 时, 每天往回拉一点,
    避免两年后跑到离谱的价位 (在区间内完全不干预)。"""
    out = np.empty(len(rets))
    x = 0.0  # log(价格 / 起始价)
    for d0 in range(0, len(rets), BARS_PER_DAY):
        seg = rets[d0 : d0 + BARS_PER_DAY]
        excess = abs(x) - band
        adj = -math.copysign(0.03 * excess, x) / len(seg) if excess > 0 else 0.0
        path = x + np.cumsum(seg + adj)
        out[d0 : d0 + len(seg)] = path
        x = float(path[-1])
    return math.log(p0) + out


# Round 5: snap rounded prices/amounts to the float a decimal string parses to (what exchange data
# looks like). np.round(x / step) * step leaves binary tails (3482 * 1e-5 = 0.034820000000000004);
# freqtrade's backtest prices orders with price_to_precision (-> 0.03482), so an order at a candle's
# open can sit one ulp outside [low, high] and never fill (seen: an end-of-backtest force_exit
# short-close on a candle that opened at its low stayed unfilled, the trade vanished from the cut run
# and lookahead-analysis counted a "biased entry"). --legacy keeps the old arithmetic (byte-identical).
CANONICAL_DECIMALS = True


def _step_decimals(step: float) -> int:
    exp = Decimal(repr(float(step))).normalize().as_tuple().exponent
    return min(12, max(0, -int(exp)))


def round_to(x: np.ndarray, step: float) -> np.ndarray:
    out = np.round(x / step) * step
    if CANONICAL_DECIMALS:
        out = np.round(out, _step_decimals(step))
    return out


def build_pair(
    base: str,
    spec: dict[str, float],
    index: pd.DatetimeIndex,
    regimes: list[str],
    market: tuple,
    sv_market: np.ndarray,
    act_vol: np.ndarray,
    act_volu: np.ndarray,
    gap: tuple[int, int],
    rng: np.random.Generator,
    extra: tuple[np.ndarray, np.ndarray] | None = None,
) -> pd.DataFrame:
    """extra = (额外的 5 分钟对数收益, 成交量倍数)：暴跌日、新币上线后的走势（第 5 轮）。
    用单独的随机数生成，不消耗 rng，所以 extra=None 时和以前生成的数据完全相同。"""
    n = len(index)
    mu, sig, m_events = market
    beta = spec["beta"]
    m_noise = t_noise(rng, n)
    idio_sig = spec["idio"] / math.sqrt(BARS_PER_DAY)
    vol_boost = np.ones(n)
    volu_mult = np.ones(n)

    # 自己独有的事件（例如单币利空），频率较低
    own_events: list[tuple[int, float, float]] = []
    for d, r in enumerate(regimes):
        if rng.random() < 0.015 + 0.02 * (r in ("bear", "crash")):
            for bar in pick_event_bars(d, 1, rng):
                down = rng.random() < 0.7
                own_events.append(
                    (int(bar), (-1 if down else 0.8) * rng.uniform(0.01, 0.05), math.exp(rng.uniform(math.log(8), math.log(120))))
                )
    sv_own = stochastic_vol(n, rng) ** 0.5

    # 收益 = beta * 市场 + 独立部分；波动 = 状态波动 x 波动聚集 x 日内时段
    shock = np.zeros(n)
    apply_events(shock, vol_boost, volu_mult, m_events + own_events, beta, rng)
    rets = (
        beta * (mu + sig * sv_market * act_vol * vol_boost * m_noise)
        + idio_sig * sv_own * act_vol * vol_boost * t_noise(rng, n)
        + shock
    )
    if extra is not None:
        rets = rets + extra[0]
        volu_mult = volu_mult * extra[1]
    close = np.exp(integrate_with_reversion(rets, spec["p0"]))

    tick = spec["tick"]
    close = np.maximum(round_to(close, tick), tick)
    open_ = np.concatenate([[round_to(np.array([spec["p0"]]), tick)[0]], close[:-1]])
    bar_sig = (beta * sig + idio_sig) * sv_market * act_vol * vol_boost
    up_wick = np.abs(rng.normal(0.0, 0.6, n)) * bar_sig * np.maximum(open_, close)
    dn_wick = np.abs(rng.normal(0.0, 0.6, n)) * bar_sig * np.minimum(open_, close)
    high = round_to(np.maximum(open_, close) + up_wick, tick)
    low = np.maximum(round_to(np.minimum(open_, close) - dn_wick, tick), tick)
    high = np.maximum(high, np.maximum(open_, close))
    low = np.minimum(low, np.minimum(open_, close))

    # 成交额 (USDT) = 平均 x 时段 x 行情状态 x |收益| 放大 x 对数正态噪声；成交量 = 成交额 / 价格（币的数量）
    reg_volu = np.repeat(np.array([REGIMES[r]["vol"] for r in regimes]), BARS_PER_DAY)[:n]
    abs_z = np.abs(rets) / np.maximum(bar_sig, 1e-12)
    notional = (
        spec["qv"]
        * act_volu
        * reg_volu
        * volu_mult
        * (0.6 + 0.4 * np.minimum(abs_z, 6.0))
        * rng.lognormal(-0.08, 0.4, n)
    )
    lot_coins = spec["lot"] * spec["ct"]
    volume = np.maximum(round_to(notional / close, lot_coins), lot_coins)

    df = pd.DataFrame(
        {"date": index, "open": open_, "high": high, "low": low, "close": close, "volume": volume}
    )
    # 交易所维护：这段时间没有 K 线（freqtrade 读取时会自动补成平盘 K 线）
    g0, g1 = gap
    return df.drop(index=range(g0, g1)).reset_index(drop=True)


def resample(df5: pd.DataFrame, tf: str) -> pd.DataFrame:
    rule = {"15m": "15min", "1h": "1h", "4h": "4h", "1d": "1D", "5m": None}[tf]
    if rule is None:
        return df5
    agg = {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
    out = (
        df5.set_index("date")
        .resample(rule, label="left", closed="left")
        .agg(agg)
        .dropna(subset=["open"])
        .reset_index()
    )
    return out


def build_mark(df1h: pd.DataFrame, rng: np.random.Generator, tick: float) -> pd.DataFrame:
    """标记价格：成交价 + 很小的基差噪声（标记价格 K 线没有成交量）。"""
    basis = 1 + rng.normal(0.0, 0.0003, len(df1h))
    m = df1h.copy()
    for c in ("open", "high", "low", "close"):
        m[c] = np.maximum(round_to(m[c].to_numpy() * basis, tick), tick)
    m["high"] = m[["open", "high", "low", "close"]].max(axis=1)
    m["low"] = m[["open", "high", "low", "close"]].min(axis=1)
    m["volume"] = 0.0
    return m


def build_funding(df1h: pd.DataFrame, rng: np.random.Generator) -> pd.DataFrame:
    """资金费率：每 8 小时一次 (00/08/16 UTC)。上涨行情偏正、下跌行情偏负，上下限 +/-0.75%。"""
    s = df1h.set_index("date")["close"]
    trend = np.log(s / s.shift(72)).fillna(0.0)  # 过去 3 天涨跌
    at = s.index[s.index.hour % 8 == 0]
    rate = 0.0001 + 0.0004 * np.tanh(trend.loc[at].to_numpy() / 0.05) + rng.normal(0.0, 0.00005, len(at))
    rate = np.clip(np.round(rate, 8), -0.0075, 0.0075)
    return pd.DataFrame({"date": at, "funding_rate": rate})


# ---------------------------------------------------------------------------
# 第 5 轮新加的数据（都用单独的随机数：np.random.default_rng([种子, 用途, 币的编号])，不动原来的随机数序列）
# ---------------------------------------------------------------------------


def side_rng(seed: int, purpose: int, key: int = 0) -> np.random.Generator:
    return np.random.default_rng([int(seed), int(purpose), int(key)])


def coin_key(base: str) -> int:
    return list(PAIR_SPECS).index(base)


def pick_crash_days(start: pd.Timestamp, end: pd.Timestamp, n: int, seed: int) -> list[tuple[pd.Timestamp, float, float]]:
    """全市场暴跌日：把 [开始+90 天, 结束-15 天] 平分成 n 段，每段里随机挑一天。
    返回 [(日期, 全市场跌幅（对数）, 之后 3 天反弹掉的比例)]。"""
    lo = start + pd.Timedelta(days=90)
    hi = end - pd.Timedelta(days=15)
    if n <= 0 or hi <= lo:
        return []
    rng = side_rng(seed, 5)
    width = (hi - lo).days / n
    out = []
    for k in range(n):
        a = lo + pd.Timedelta(days=int(k * width))
        d = (a + pd.Timedelta(days=int(rng.integers(0, max(1, int(width) - 5))))).normalize()
        out.append((d, float(rng.uniform(0.14, 0.24)), float(rng.uniform(0.25, 0.60))))
    return out


def crash_extra(
    index: pd.DatetimeIndex, crashes: list[tuple[pd.Timestamp, float, float]], spec: dict[str, float], rng: np.random.Generator
) -> tuple[np.ndarray, np.ndarray]:
    """暴跌日写进某个币的 5 分钟收益 / 成交量：12:00-16:00 UTC 跌掉 70%，16:00-22:00 再跌 30%，
    当天成交量约是平时的 3 倍（下跌那 4 小时 6 ~ 9 倍），之后 3 天反弹一部分。山寨币（beta 大）跌得更多，BTC 也跌。"""
    n = len(index)
    rets = np.zeros(n)
    volu = np.ones(n)
    t0 = index[0].normalize()
    for day, depth, rebound in crashes:
        b0 = int((day - t0).days) * BARS_PER_DAY
        if b0 < 0 or b0 + 4 * BARS_PER_DAY > n:
            continue
        drop = depth * (0.6 + 0.4 * spec["beta"]) * rng.uniform(0.85, 1.15)
        rets[b0 + 144 : b0 + 192] -= 0.7 * drop / 48
        rets[b0 + 192 : b0 + 264] -= 0.3 * drop / 72
        volu[b0 : b0 + 144] *= 1.5
        volu[b0 + 144 : b0 + 192] *= rng.uniform(6.0, 9.0)
        volu[b0 + 192 : b0 + BARS_PER_DAY] *= 2.5
        rb = drop * rebound * rng.uniform(0.8, 1.2)
        rets[b0 + BARS_PER_DAY : b0 + 4 * BARS_PER_DAY] += rb / (3 * BARS_PER_DAY)
        volu[b0 + BARS_PER_DAY : b0 + 2 * BARS_PER_DAY] *= 1.6
    return rets, volu


def listing_extra(index: pd.DatetimeIndex, listed: pd.Timestamp, base: str, rng: np.random.Generator) -> tuple[np.ndarray, np.ndarray]:
    """新上线合成币（NEW*）上线后的走势：头 7 天炒高 5% ~ 35%，第 15 ~ 140 天慢慢跌掉 30% ~ 60%，
    上线头两周成交量更大。NEW4 在第 17 ~ 24 天被轧空（约 +80%，会打到 40% 的止损）。"""
    n = len(index)
    rets = np.zeros(n)
    volu = np.ones(n)
    b0 = int((listed.normalize() - index[0].normalize()).days) * BARS_PER_DAY
    d = BARS_PER_DAY

    def add(a: int, b: int, total: float) -> None:
        lo, hi = max(0, b0 + a * d), min(n, b0 + b * d)
        if hi > lo:
            rets[lo:hi] += total / ((b - a) * d)

    add(0, 7, rng.uniform(0.05, 0.35))
    add(14, 140, -rng.uniform(0.35, 0.90))
    if base == "NEW4":
        add(17, 24, 0.60)
    lo, hi = max(0, b0), min(n, b0 + 14 * d)
    if hi > lo:
        volu[lo:hi] *= np.linspace(3.0, 1.2, hi - lo)
    return rets, volu


def _ar1(n: int, phi: np.ndarray | float, innov: np.ndarray) -> np.ndarray:
    out = np.empty(n)
    x = 0.0
    ph = np.broadcast_to(np.asarray(phi, dtype=float), (n,))
    for i in range(n):
        x = ph[i] * x + innov[i]
        out[i] = x
    return out


def build_spot(
    df1h: pd.DataFrame,
    spec: dict[str, float],
    rng: np.random.Generator,
    crashes: list[tuple[pd.Timestamp, float, float]],
    gap: tuple[pd.Timestamp, pd.Timestamp] | None,
) -> tuple[pd.DataFrame, int]:
    """现货 1h = 永续 1h / (1 + 溢价)。溢价 = +0.02% + 小时级 AR(1) 小波动 + 每年约 6 次极端溢价 + 暴跌时永续折价。
    返回 (现货K线, 极端溢价次数)。现货成交量（币的数量）约为永续的 40%。"""
    n = len(df1h)
    prem = 0.0002 + _ar1(n, 0.92, rng.normal(0.0, 0.0002, n))
    n_spikes = int(rng.poisson(6.0 * n / 8760.0))
    for _ in range(n_spikes):
        t = int(rng.integers(0, max(1, n - 1)))
        sign = 1.0 if rng.random() < 0.6 else -1.0
        amp = rng.uniform(0.004, 0.015)
        shape = np.concatenate(
            [
                np.linspace(0.0, 1.0, int(rng.integers(2, 7)), endpoint=False),
                np.ones(int(rng.integers(4, 17))),
                np.linspace(1.0, 0.0, int(rng.integers(8, 31))),
            ]
        )
        m = min(len(shape), n - t)
        prem[t : t + m] += sign * amp * shape[:m]
    dates = df1h["date"]
    for day, _depth, _rb in crashes:
        mask = ((dates >= day + pd.Timedelta(hours=12)) & (dates < day + pd.Timedelta(hours=22))).to_numpy()
        if mask.any():
            prem[mask] -= rng.uniform(0.004, 0.010)
    tick = spec["tick"]
    factor = 1.0 / (1.0 + prem)
    spot = df1h.copy()
    for c in ("open", "high", "low", "close"):
        spot[c] = np.maximum(round_to(df1h[c].to_numpy() * factor, tick), tick)
    spot["high"] = spot[["open", "high", "low", "close"]].max(axis=1)
    spot["low"] = spot[["open", "high", "low", "close"]].min(axis=1)
    step = spec["lot"] * spec["ct"] / 100.0
    spot["volume"] = np.maximum(round_to(df1h["volume"].to_numpy() * 0.4 * rng.lognormal(0.0, 0.3, n), step), step)
    if gap is not None:
        spot = spot[~((spot["date"] >= gap[0]) & (spot["date"] < gap[1]))]
    return spot.reset_index(drop=True), n_spikes


def funding_hours(schedule: str, dates: pd.DatetimeIndex) -> np.ndarray:
    """结算间隔（小时），每根 1h K线一个值。"8" / "4" / "8>4@YYYYMMDD"（那天起改成 4 小时）。"""
    if ">" in schedule:
        a, rest = schedule.split(">", 1)
        b, day = rest.split("@", 1)
        switch = pd.Timestamp(datetime.strptime(day, "%Y%m%d"), tz="UTC")
        return np.where(dates < switch, int(a), int(b))
    return np.full(len(dates), int(schedule))


def build_funding_v2(df1h: pd.DataFrame, rng: np.random.Generator, schedule: str) -> pd.DataFrame:
    """资金费率（第 5 轮）：每次结算一行，结算时刻 = UTC 整点、按间隔对齐（8 小时：00/08/16；4 小时：00/04/...）。
    费率 = 间隔/8 x (0.01% 基础利率 + 每个币自己的持续偏离（半衰期约 25 天，各币之间差得开） + 过去 3 天涨跌带来的偏离)
    + 小噪声，上下限 +/-0.75%。"""
    s = df1h.set_index("date")["close"]
    trend = np.log(s / s.shift(72)).fillna(0.0)
    ih = funding_hours(schedule, s.index)
    mask = (s.index.hour % ih) == 0
    at = s.index[mask]
    hrs = ih[mask].astype(float)
    n = len(at)
    phi = 0.5 ** (hrs / 24.0 / 25.0)
    sd = rng.uniform(0.00015, 0.00035)
    pers = rng.normal(0.0, 0.0002) + _ar1(n, phi, rng.normal(0.0, 1.0, n) * sd * np.sqrt(1.0 - phi**2))
    scale = hrs / 8.0
    rate = scale * (0.0001 + pers + 0.0003 * np.tanh(trend.loc[at].to_numpy() / 0.05)) + rng.normal(0.0, 0.00004, n) * np.sqrt(scale)
    rate = np.clip(np.round(rate, 8), -0.0075, 0.0075)
    return pd.DataFrame({"date": at, "funding_rate": rate})


# ---------------------------------------------------------------------------
# 交易对规格 / 杠杆档位（给 scripts/ft_offline.py，代替连不上的交易所接口）
# ---------------------------------------------------------------------------


def market_entry(base: str, spec: dict[str, float], created: int | None = None, active: bool = True) -> dict:
    sym = f"{base}/USDT:USDT"
    tick = spec["tick"]
    return {
        "id": f"{base}-USDT-SWAP",
        "symbol": sym,
        "base": base,
        "quote": "USDT",
        "settle": "USDT",
        "baseId": base,
        "quoteId": "USDT",
        "settleId": "USDT",
        "type": "swap",
        "spot": False,
        "margin": False,
        "swap": True,
        "future": False,
        "option": False,
        "index": None,
        "active": active,
        "contract": True,
        "linear": True,
        "inverse": False,
        "subType": "linear",
        "contractSize": spec["ct"],
        "expiry": None,
        "expiryDatetime": None,
        "strike": None,
        "optionType": None,
        "taker": 0.0005,
        "maker": 0.0002,
        "percentage": True,
        "tierBased": True,
        "feeSide": "get",
        "precision": {"amount": spec["lot"], "price": tick, "cost": None, "base": None, "quote": None},
        "limits": {
            "leverage": {"min": 1.0, "max": float(spec["lev"])},
            "amount": {"min": spec["lot"], "max": None},
            "price": {"min": None, "max": None},
            "cost": {"min": None, "max": None},
            "market": {"min": None, "max": None},
        },
        "marginModes": {"cross": True, "isolated": True},
        "created": created,
        "info": {"instId": f"{base}-USDT-SWAP", "note": "SYNTHETIC - not real exchange data"},
    }


def market_entry_binance(base: str, spec: dict[str, float], created: int | None = None, active: bool = True) -> dict:
    """币安 U 本位永续的样子：每张合约 = 1 个币，数量精度 = 币的数量，最小下单额 BTC 100、ETH 20、其他 5 USDT。"""
    sym = f"{base}/USDT:USDT"
    tick = spec["tick"]
    lot = spec["lot"] * spec["ct"]
    return {
        "id": f"{base}USDT",
        "symbol": sym,
        "base": base,
        "quote": "USDT",
        "settle": "USDT",
        "baseId": base,
        "quoteId": "USDT",
        "settleId": "USDT",
        "type": "swap",
        "spot": False,
        "margin": False,
        "swap": True,
        "future": False,
        "option": False,
        "index": None,
        "active": active,
        "contract": True,
        "linear": True,
        "inverse": False,
        "subType": "linear",
        "contractSize": 1.0,
        "expiry": None,
        "expiryDatetime": None,
        "strike": None,
        "optionType": None,
        "taker": 0.0005,
        "maker": 0.0002,
        "percentage": True,
        "tierBased": True,
        "feeSide": "get",
        "precision": {"amount": lot, "price": tick, "cost": None, "base": None, "quote": None},
        "limits": {
            "leverage": {"min": 1.0, "max": float(spec["lev"])},
            "amount": {"min": lot, "max": None},
            "price": {"min": None, "max": None},
            "cost": {"min": BINANCE_MIN_COST.get(base, 5.0), "max": None},
            "market": {"min": None, "max": None},
        },
        "marginModes": {"cross": True, "isolated": True},
        "created": created,
        "info": {"symbol": f"{base}USDT", "note": "SYNTHETIC - not real exchange data"},
    }


def leverage_tiers(spec: dict[str, float], with_maint_amt: bool = False) -> list[dict]:
    """杠杆档位（名义价值单位 USDT），格式与 freqtrade 解析后的 LeverageTier 相同。
    with_maint_amt：币安的强平价公式要用"维持保证金速算额"（cum），按币安的算法逐档累加；OKX 用 None（和以前一样）。"""
    first = 50_000.0 * max(spec["qv"] / 1e6, 1.0)
    tiers = []
    lo = 0.0
    lev = float(spec["lev"])
    mmr = 0.004
    cum = 0.0
    prev_mmr = mmr
    for _ in range(6):
        hi = first if lo == 0 else lo * 4
        if with_maint_amt:
            cum += (mmr - prev_mmr) * lo
            prev_mmr = mmr
        tiers.append(
            {
                "minNotional": lo,
                "maxNotional": hi,
                "maintenanceMarginRate": mmr,
                "maxLeverage": lev,
                "maintAmt": round(cum, 8) if with_maint_amt else None,
            }
        )
        lo, lev, mmr = hi, max(lev / 2.0, 1.0), mmr * 2
    return tiers


# ---------------------------------------------------------------------------


MARKER_TEXT = """\
!!! 这是【合成测试数据】，不是真实行情 !!!
THIS DIRECTORY CONTAINS SYNTHETIC (RANDOMLY GENERATED) MARKET DATA - NOT REAL.
SYNTHETIC_FORMAT_VERSION={version}
SYNTHETIC_EXCHANGE={exchange}

由 scripts/make_synthetic_data.py 生成，生成时间 {now} UTC，随机种子 {seed}。
交易所的样子：{exchange}（交易对规格见 synthetic_markets.json）
交易对：{pairs}
区间：{start} ~ {end}（{days} 天），周期：{tfs}
{short_tf}
上线晚的币（那天之前没有数据）：{listings}
下架的币（那天起没有数据）：{delisted}
标记价格/资金费率：{fut}
资金费结算间隔：{fund}
现货 1h：{spot}
全市场暴跌日：{crash}

只用来检查回测流程（配置、脚本、策略代码）能否跑通。
用这些数据得到的收益、回撤、胜率没有任何意义，不能用来评价策略。
真实回测请用 scripts/download_okx_data.sh 下载的 user_data/data/<交易所> 数据。
可以随时删除整个目录：rm -rf {outdir}
"""


def _parse_listings(text: str, explicit: bool, bases: list[str], start: pd.Timestamp, end: pd.Timestamp, ymd) -> tuple[
    dict[str, pd.Timestamp], dict[str, pd.Timestamp], list[str]
]:
    """币:上线日[:下架日]。explicit = 用户自己写的（写错就报错）；默认值里超出区间的条目跳过（例如 --days 365 时）。"""
    listings: dict[str, pd.Timestamp] = {}
    delist: dict[str, pd.Timestamp] = {}
    notes: list[str] = []
    for item in text.split():
        parts = item.split(":")
        coin = parts[0].upper()
        if coin not in PAIR_SPECS or len(parts) not in (2, 3) or not parts[1]:
            raise SystemExit(f"错误：--listings 写成 币:YYYYMMDD 或 币:上线日:下架日，例如 SOL:20210601（收到：{item}）")
        t = ymd(parts[1], f"--listings {coin}")
        d = ymd(parts[2], f"--listings {coin} 下架日") if len(parts) == 3 and parts[2] else None
        if coin not in bases:
            continue
        bad = ""
        if not (start < t < end - pd.Timedelta(days=30)):
            bad = f"{coin} 的上线日期 {t.date()} 要在 {start.date()} 和结束前 30 天之间"
        elif d is not None and not (t + pd.Timedelta(days=30) < d < end - pd.Timedelta(days=5)):
            bad = f"{coin} 的下架日期 {d.date()} 要在上线 30 天以后、结束前 5 天以前"
        if bad:
            if explicit:
                raise SystemExit(f"错误：{bad}")
            notes.append(f"{bad}，这一条跳过")
            continue
        listings[coin] = t
        if d is not None:
            delist[coin] = d
    return listings, delist, notes


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="生成合成测试数据（仅用于流程自检，不是真实行情）")
    ap.add_argument("--exchange", choices=EXCHANGES, default="okx",
                    help="生成哪个交易所的样子（默认 okx；binance = 给 configs/config.binance-research.json 用的一套）")
    ap.add_argument("--out", type=Path, default=None, help="输出目录（默认 user_data/data/<交易所>-synthetic）")
    ap.add_argument("--start", default=None, help=f"开始日期 YYYYMMDD（默认 {DEFAULT_START}；写了 --days 时不用）")
    ap.add_argument("--days", type=int, default=None, help="天数（从结束日期往前倒推；写了就不看 --start）")
    ap.add_argument("--end", default=None, help="结束日期 YYYYMMDD（默认今天 UTC 0 点）")
    ap.add_argument("--seed", type=int, default=42, help="随机种子（同一种子生成的数据完全相同）")
    ap.add_argument("--pairs", default=None,
                    help=f"币种，空格分隔（OKX 默认 {' '.join(DEFAULT_PAIRS['okx'])}；币安默认 25 个；可选：{' '.join(PAIR_SPECS)}）")
    ap.add_argument("--timeframes", default=None, help="周期（OKX 默认 5m 15m 1h 4h 1d；币安默认 1h 4h 1d）")
    ap.add_argument(
        "--futures-history-days",
        type=int,
        default=None,
        help="标记价格/资金费率只生成最近 N 天（OKX 默认 92，模仿 OKX 只有约 3 个月历史；币安默认 0）；0 = 覆盖全部区间",
    )
    ap.add_argument(
        "--listings",
        default=None,
        help='上线晚的币，空格分隔的 币:YYYYMMDD 或 币:上线日:下架日（那天之前/之后没有数据）；"" = 所有币同一天开始、都不下架。'
        f' OKX 默认 "{DEFAULT_LISTINGS["okx"]}"',
    )
    ap.add_argument(
        "--short-tf-days",
        type=int,
        default=DEFAULT_SHORT_TF_DAYS,
        help=f"5m / 15m 只生成最近 N 天（默认 {DEFAULT_SHORT_TF_DAYS}）；0 = 覆盖全部区间",
    )
    ap.add_argument("--spot", action=argparse.BooleanOptionalAction, default=None,
                    help="生成现货 1h K线（OKX 默认生成，币安默认不生成；--no-spot 不生成）")
    ap.add_argument("--crash-days", type=int, default=None,
                    help=f"全市场暴跌日的天数（默认 {DEFAULT_CRASH_DAYS}；0 = 没有）")
    ap.add_argument("--legacy", action="store_true",
                    help="第 4 轮以前的样子：8 个币、没有现货/暴跌日/新币（同一个种子生成的K线、标记价格、资金费率和以前完全相同）")
    args = ap.parse_args(argv)

    try:
        from freqtrade.data.history import get_datahandler
        from freqtrade.enums import CandleType
        from freqtrade.exchange import Exchange
        from freqtrade.exchange.binance import Binance
        from freqtrade.exchange.okx import Okx
    except ImportError:
        print("错误：需要 freqtrade 环境。请用 .venv/bin/python 运行本脚本（见 README）。", file=sys.stderr)
        return 2

    exch = args.exchange
    global CANONICAL_DECIMALS
    CANONICAL_DECIMALS = not args.legacy
    if args.legacy:
        if exch != "okx":
            print("错误：--legacy 只能和 --exchange okx 一起用", file=sys.stderr)
            return 2
        args.pairs = args.pairs if args.pairs is not None else " ".join(LEGACY_PAIRS)
        args.listings = args.listings if args.listings is not None else LEGACY_LISTINGS
        args.spot = False if args.spot is None else args.spot
        args.crash_days = 0 if args.crash_days is None else args.crash_days
    explicit_listings = args.listings is not None
    pairs_txt = args.pairs if args.pairs is not None else " ".join(DEFAULT_PAIRS[exch])
    tfs_txt = args.timeframes if args.timeframes is not None else " ".join(DEFAULT_TIMEFRAMES if exch == "okx" else BINANCE_TIMEFRAMES)
    fut_days = args.futures_history_days if args.futures_history_days is not None else (92 if exch == "okx" else 0)
    want_spot = args.spot if args.spot is not None else (exch == "okx")
    n_crash = DEFAULT_CRASH_DAYS if args.crash_days is None else max(0, int(args.crash_days))

    out: Path = (args.out or (ROOT / "user_data" / "data" / f"{exch}-synthetic")).resolve()
    for real in ("okx", "binance", "binanceusdm", "myokx"):
        real_dir = (ROOT / "user_data" / "data" / real).resolve()
        if out == real_dir or real_dir in out.parents:
            print(f"错误：不能把合成数据写进真实数据目录 {real_dir}。", file=sys.stderr)
            return 2
    if out.exists() and any(out.rglob("*.feather")) and not (out / MARKER_NAME).is_file():
        print(f"错误：{out} 里已有数据文件且没有合成数据标记，可能是真实数据，拒绝覆盖。", file=sys.stderr)
        return 2
    bases = [b.upper() for b in pairs_txt.split()]
    unknown = [b for b in bases if b not in PAIR_SPECS]
    if unknown:
        print(f"错误：不支持的币种 {unknown}，可选：{list(PAIR_SPECS)}", file=sys.stderr)
        return 2
    tfs = tfs_txt.split()
    bad_tf = [t for t in tfs if t not in ("5m", "15m", "1h", "4h", "1d")]
    if bad_tf:
        print(f"错误：不支持的周期 {bad_tf}（可选 5m 15m 1h 4h 1d）", file=sys.stderr)
        return 2

    # 标记价格 / 资金费率的周期和 K 线类型以 freqtrade 本版本的交易所设置为准（2026.10: OKX、币安都是 1h）
    ex_cls = Okx if exch == "okx" else Binance
    ft_has = {**Exchange._ft_has_default, **ex_cls._ft_has, **ex_cls._ft_has_futures}
    mark_tf = ft_has["mark_ohlcv_timeframe"]
    fund_tf = ft_has["funding_fee_timeframe"]
    mark_type = CandleType.from_string(ft_has["mark_ohlcv_price"])
    for need in {mark_tf, fund_tf} - {"1h"}:
        print(f"错误：本脚本只会生成 1h 的标记价格/资金费率，但 freqtrade 需要 {need}", file=sys.stderr)
        return 2

    def _ymd(txt: str, what: str) -> pd.Timestamp:
        try:
            return pd.Timestamp(datetime.strptime(txt, "%Y%m%d"), tz="UTC")
        except ValueError:
            raise SystemExit(f"错误：{what} 要写成 YYYYMMDD（收到：{txt}）") from None

    end = _ymd(args.end, "--end") if args.end else pd.Timestamp.now(tz="UTC").floor("D")
    if args.days is not None:
        start = end - pd.Timedelta(days=args.days)
    else:
        start = _ymd(args.start or DEFAULT_START, "--start")
    n_days = int((end - start).days)
    if n_days < 30:
        print(f"错误：区间只有 {n_days} 天（{start.date()} ~ {end.date()}），太短", file=sys.stderr)
        return 2
    index = pd.date_range(start, end, freq="5min", inclusive="left")

    listings, delist, listing_notes = _parse_listings(
        args.listings if explicit_listings else DEFAULT_LISTINGS[exch], explicit_listings, bases, start, end, _ymd
    )
    short_start = start if args.short_tf_days <= 0 else max(start, end - pd.Timedelta(days=args.short_tf_days))

    # 参数都检查完了再删旧数据
    if out.exists():
        shutil.rmtree(out)
    (out / "futures").mkdir(parents=True)
    rng = np.random.default_rng(args.seed)
    dh = get_datahandler(out, "feather")

    regimes = regime_path(n_days, rng)
    market = build_market(len(index), n_days, regimes, rng)
    sv_market = stochastic_vol(len(index), rng)
    act_vol, act_volu = intraday_profile(index)
    # 维护缺口：所有币同一个小时（选在区间中段的某个周二 02:00 UTC）
    mid = index[len(index) // 2].normalize()
    while mid.day_of_week != 1:
        mid += pd.Timedelta(days=1)
    g0 = int(index.get_indexer([mid + pd.Timedelta(hours=2)])[0])
    gap = (g0, g0 + 12)
    crashes = pick_crash_days(start, end, n_crash, args.seed)

    fut_start = start if fut_days <= 0 else end - pd.Timedelta(days=fut_days)
    markets: dict[str, dict] = {}
    tiers: dict[str, list[dict]] = {}
    counts = {r: regimes.count(r) for r in REGIMES}
    fund_sched = FUNDING_SCHEDULE.get(exch, {})
    print(f"合成测试数据（不是真实行情；交易所的样子：{exch}）")
    print(f"  区间 {start.date()} ~ {end.date()}（{n_days} 天），种子 {args.seed}")
    print("  行情状态天数：" + "，".join(f"{k} {v}" for k, v in counts.items()))
    print(f"  全市场爆仓事件：{len(market[2])} 次")
    if crashes:
        print("  全市场暴跌日：" + " ".join(f"{d.date()}(-{dep:.0%})" for d, dep, _ in crashes))
    for note in listing_notes:
        print(f"  提示：{note}")

    spot_info: list[str] = []
    alive_pairs: list[str] = []
    for base in bases:
        spec = PAIR_SPECS[base]
        key = coin_key(base)
        pair = f"{base}/USDT:USDT"
        extra = None
        if crashes or (base in NEW_COINS and base in listings):
            er = np.zeros(len(index))
            ev = np.ones(len(index))
            if crashes:
                cr, cv = crash_extra(index, crashes, spec, side_rng(args.seed, 7, key))
                er, ev = er + cr, ev * cv
            if base in NEW_COINS and base in listings:
                lr, lv = listing_extra(index, listings[base], base, side_rng(args.seed, 13, key))
                er, ev = er + lr, ev * lv
            extra = (er, ev)
        df5 = build_pair(base, spec, index, regimes, market, sv_market, act_vol, act_volu, gap, rng, extra)
        if base in listings:
            # 上线晚：那天之前没有任何K线（随机数照常生成，这样其他币的数据不受影响）
            df5 = df5[df5["date"] >= listings[base]].reset_index(drop=True)
        if base in delist:
            df5 = df5[df5["date"] < delist[base]].reset_index(drop=True)
        for tf in tfs:
            out_df = resample(df5, tf)
            if tf in SHORT_TFS:
                out_df = out_df[out_df["date"] >= short_start].reset_index(drop=True)
            dh.ohlcv_store(pair, tf, out_df, CandleType.FUTURES)
        df1h = resample(df5, "1h")
        recent = df1h[df1h["date"] >= fut_start].reset_index(drop=True)
        dh.ohlcv_store(pair, mark_tf, build_mark(recent, rng, spec["tick"]), mark_type)
        legacy_funding = exch == "okx" and base in LEGACY_PAIRS and base not in fund_sched
        if legacy_funding:
            fund = build_funding(df1h, rng)  # 第 4 轮以前的算法和随机数顺序（这 8 个币的数据和以前相同）
        else:
            fund = build_funding_v2(df1h, side_rng(args.seed, 17, key), fund_sched.get(base, "8"))
        dh.ohlcv_store(pair, fund_tf, fund.query("date >= @fut_start"), CandleType.FUNDING_RATE)
        if want_spot and base not in NO_SPOT:
            sgap = None
            if base in SPOT_GAPS:
                sgap = (_ymd(SPOT_GAPS[base][0], "spot gap"), _ymd(SPOT_GAPS[base][1], "spot gap"))
            spot_df, n_sp = build_spot(df1h, spec, side_rng(args.seed, 11, key), crashes, sgap)
            dh.ohlcv_store(f"{base}/USDT", "1h", spot_df, CandleType.SPOT)
            spot_info.append(f"{base}/USDT({n_sp} 次极端溢价{'，缺 ' + str(sgap[0].date()) + ' 起 ' + str((sgap[1] - sgap[0]).days) + ' 天' if sgap else ''})")
        created = int(listings[base].timestamp() * 1000) if base in listings else None
        mstate = DELISTED_MARKETS.get(base, "inactive") if base in delist else "active"
        if mstate != "omit":
            if exch == "okx":
                markets[pair] = market_entry(base, spec, created=created, active=(mstate == "active"))
                tiers[pair] = leverage_tiers(spec)
            else:
                markets[pair] = market_entry_binance(base, spec, created=created, active=(mstate == "active"))
                tiers[pair] = leverage_tiers(spec, with_maint_amt=True)
        if base not in delist and mstate == "active":
            alive_pairs.append(pair)
        d1 = resample(df5, "1d")
        when = ""
        if base in listings:
            when = f"从 {listings[base].date()} 起"
        if base in delist:
            when += f"到 {delist[base].date()} 下架"
        print(
            f"  {pair:<15} {when:<32}"
            f"起始 {df5['open'].iloc[0]:>10g}  最终 {df5['close'].iloc[-1]:>12.6g}  "
            f"最高 {df5['high'].max():>12.6g}  最低 {df5['low'].min():>12.6g}  "
            f"日线阴线占比 {(d1['close'] < d1['open']).mean():.0%}"
        )

    (out / MARKETS_NAME).write_text(
        json.dumps(
            {
                "_note": "SYNTHETIC markets for scripts/ft_offline.py only - not real exchange specs",
                "exchange": exch,
                "markets": markets,
                "leverage_tiers": tiers,
            },
            ensure_ascii=False,
            indent=1,
        ),
        encoding="utf-8",
    )
    btc = "BTC/USDT:USDT"
    ordered = [p for p in sorted(alive_pairs) if p != btc] + ([btc] if btc in alive_pairs else [])
    (out / PAIRS_NAME).write_text(
        "{\n"
        "    // 合成数据的交易对配置片段（scripts/make_synthetic_data.py 生成，不要手改）：数据一直到最后、\n"
        "    // 交易对规格里\"在交易\"的永续，BTC/USDT:USDT 放最后。用第二个 --config 叠加在研究配置后面（只用于合成数据自检）。\n"
        '    "exchange": {\n'
        '        "pair_whitelist": [\n'
        + ",\n".join(f'            "{p}"' for p in ordered)
        + "\n        ]\n    }\n}\n",
        encoding="utf-8",
    )
    fund_txt = "全部每 8 小时一次（00/08/16 UTC）"
    special = [f"{c} {v.replace('>', ' 小时改成 ').replace('@', ' 小时，从 ')}" if ">" in v else f"{c} 每 {v} 小时"
               for c, v in fund_sched.items() if c in bases]
    if special:
        fund_txt = "大部分每 8 小时一次（00/08/16 UTC）；" + "、".join(special)
    try:
        outdir_txt = str(out.relative_to(ROOT))
    except ValueError:
        outdir_txt = str(out)
    (out / MARKER_NAME).write_text(
        MARKER_TEXT.format(
            version=FORMAT_VERSION,
            exchange=exch,
            now=datetime.now(UTC).strftime("%Y-%m-%d %H:%M"),
            seed=args.seed,
            pairs=" ".join(f"{b}/USDT:USDT" for b in bases),
            start=start.date(),
            end=end.date(),
            days=n_days,
            tfs=" ".join(tfs),
            short_tf=(
                f"5m / 15m：只有 {short_start.date()} 之后"
                if short_start > start and any(t in SHORT_TFS for t in tfs)
                else "所有周期都覆盖全部区间"
            ),
            listings="、".join(f"{c} {t.date()}" for c, t in listings.items()) or "没有",
            delisted="、".join(
                f"{c} {t.date()}（交易对规格里{'没有' if DELISTED_MARKETS.get(c) == 'omit' else '标成不活跃'}）"
                for c, t in delist.items()
            ) or "没有",
            fut=f"只有 {fut_start.date()} 之后（{mark_tf}）" if fut_days > 0 else f"覆盖全部区间（{mark_tf}）",
            fund=fund_txt,
            spot=("、".join(spot_info) + "（文件在数据目录根目录）") if spot_info else "没有",
            crash=" ".join(f"{d.date()}" for d, _, _ in crashes) or "没有",
            outdir=outdir_txt,
        ),
        encoding="utf-8",
    )
    size_mb = sum(f.stat().st_size for f in out.rglob("*") if f.is_file()) / 1e6
    print(f"已写入 {out}（{size_mb:.1f} MB）")
    print("下一步：bash scripts/synthetic_test.sh   （或 make synthetic-test）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
