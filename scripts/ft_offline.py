#!/usr/bin/env python3
"""
【仅限合成数据自检】不联网运行 freqtrade 的回测 / 分析命令。

背景：freqtrade 哪怕只是回测，启动时也要从交易所下载"交易对规格"(markets) 和"杠杆档位"，
连不上交易所（例如开发容器、或被地区限制的服务器）就会报 "Could not load markets"。
本脚本把这两样换成 scripts/make_synthetic_data.py 生成的假规格（synthetic_markets.json），
并且禁止一切交易所网络请求，然后照常调用 freqtrade。

安全限制（防止误用）：
  * 只允许回测 / 分析类命令（backtesting、lookahead-analysis、recursive-analysis 等），
    不允许 trade、download-data、webserver。
  * 数据目录必须有合成数据标记文件（SYNTHETIC_DATA_README.txt），不能拿真实数据目录"假装"联网。
    没写 --datadir 时自动用 user_data/data/okx-synthetic（freqtrade 只认命令行的 --datadir）。

用法（参数与 freqtrade 完全相同）：
  .venv/bin/python scripts/ft_offline.py backtesting --config configs/config.synthetic-test.json --strategy BigTrendShort
  FT_RUNNER=synthetic bash scripts/backtest_all.sh --config configs/config.synthetic-test.json
真实回测不需要它：在能访问 OKX 的服务器上直接用 freqtrade / scripts/backtest_all.sh。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


ALLOWED_COMMANDS = {
    "backtesting",
    "backtesting-show",
    "backtesting-analysis",
    "lookahead-analysis",
    "recursive-analysis",
    "hyperopt",
    "list-strategies",
    "list-data",
    "show-config",
    "plot-dataframe",
    "plot-profit",
    "test-pairlist",
}
MARKER_NAME = "SYNTHETIC_DATA_README.txt"
MARKETS_NAME = "synthetic_markets.json"
# freqtrade 只认命令行的 --datadir（配置文件里写 "datadir" 会被忽略），所以没写时由本脚本自动补上
DEFAULT_DATADIR = "user_data/data/okx-synthetic"
DATADIR_FLAGS = ("-d", "--datadir", "--data-dir")
# configs/config.synthetic-test.json 的 exportdirectory。目录必须事先存在，
# 否则 freqtrade 会把它当成文件名前缀，把结果写成 backtest_results/synthetic-<时间>.zip
RESULTS_DIR = Path(__file__).resolve().parent.parent / "user_data" / "backtest_results" / "synthetic"


def _fail(msg: str) -> None:
    from freqtrade.exceptions import OperationalException

    raise OperationalException(f"[ft_offline] {msg}")


def _fixture(config: dict) -> dict:
    datadir = Path(config["datadir"])
    if not (datadir / MARKER_NAME).is_file():
        _fail(
            f"数据目录 {datadir} 没有合成数据标记 {MARKER_NAME}。"
            "本脚本只能配合 configs/config.synthetic-test.json + make_synthetic_data.py 生成的数据使用。"
        )
    path = datadir / MARKETS_NAME
    if not path.is_file():
        _fail(f"找不到 {path}，请先运行 scripts/make_synthetic_data.py")
    return json.loads(path.read_text(encoding="utf-8"))


def install_patches() -> None:
    import ccxt
    import ccxt.async_support as ccxt_async

    from freqtrade.exchange import exchange as ex_mod
    from freqtrade.exchange.okx import Okx

    def no_network(self, *args, **kwargs):
        _fail("离线模式下禁止访问交易所接口（调用了 ccxt.fetch）")

    async def no_network_async(self, *args, **kwargs):
        _fail("离线模式下禁止访问交易所接口（调用了 ccxt.fetch）")

    ccxt.Exchange.fetch = no_network
    ccxt_async.Exchange.fetch = no_network_async

    def fake_load_async_markets(self, reload: bool = False) -> None:
        fx = _fixture(self._config)
        if fx.get("exchange") != self.id:
            _fail(f"合成规格是给 {fx.get('exchange')} 的，但配置用的是 {self.id}")
        self._api_async.set_markets(fx["markets"])

    def fake_fill_leverage_tiers(self) -> None:
        fx = _fixture(self._config)
        for pair, tiers in fx["leverage_tiers"].items():
            self._leverage_tiers[pair] = tiers

    ex_mod.Exchange._load_async_markets = fake_load_async_markets
    ex_mod.Exchange.fill_leverage_tiers = fake_fill_leverage_tiers
    Okx.fill_leverage_tiers = fake_fill_leverage_tiers


def main(argv: list[str]) -> int:
    cmd = next((a for a in argv if not a.startswith("-")), None)
    if cmd not in ALLOWED_COMMANDS:
        print(
            f"ft_offline.py 只支持这些命令：{', '.join(sorted(ALLOWED_COMMANDS))}（收到：{cmd}）",
            file=sys.stderr,
        )
        return 2
    has_datadir = any(a in DATADIR_FLAGS or a.startswith(("--datadir=", "--data-dir=")) for a in argv)
    if not has_datadir:
        pos = argv.index(cmd) + 1
        argv = [*argv[:pos], "--datadir", DEFAULT_DATADIR, *argv[pos:]]
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    install_patches()
    from freqtrade.main import main as ft_main

    try:
        ft_main(argv)
    except SystemExit as exc:
        return int(exc.code or 0) if not isinstance(exc.code, str) else 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
