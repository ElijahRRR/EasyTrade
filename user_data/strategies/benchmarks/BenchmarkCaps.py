"""
基准策略的"封顶"子类 —— 本项目自己写的代码（不是第三方代码），用在 make research-benchmarks。

为什么要有它
  项目硬规则：杠杆 ≤ 5 倍。第三方基准策略 NostalgiaForInfinityX8（NFI）的 leverage() 默认返回 3 倍，
  但它允许用配置里的 futures_mode_leverage、nfi_parameters 等把杠杆改大，理论上可以超过 5 倍。
  这里用一个子类 NostalgiaForInfinityX8Capped 把它封顶：
    * leverage() 算出来 > 5 倍时一律改成 5 倍（其他情况原样返回，默认就是上游的 3 倍）；
    * 配置里改过的 futures_mode_leverage* 设置 > 5 时也改成 5（日志里有警告）。
  其余逻辑和上游完全一样（继承，不复制、不修改上游代码）。
  freqtrade-strategies 的基准策略杠杆最多 2 倍（VolatilitySystem 固定 2 倍，其余没写 leverage()，用 freqtrade 默认的 1 倍），
  不可能超过 5 倍，不需要封顶。研究报告还会再查每笔交易的实际杠杆（> 5 倍直接判不通过）。

  只在回测里的一个修正：NFI 把"每个币的止盈目标"存在 user_data/ 下的一个 json 文件里（nfix8-profit_max-…-(backtest).json），
  下一次回测启动时会读回来 —— 上一段（例如训练段）最后留下的目标会影响下一段（样本外）的离场，
  各段结果就不独立，也没法复现。回测 / 调参时这里改成每次用一个新的空临时文件；模拟盘 / 实盘不受影响。

怎么找到上游文件
  第三方文件由 scripts/fetch_benchmarks.sh 下载到 user_data/strategies/vendor/（不进 git）。
  本文件导入时按路径找它：先找同一个目录（scripts/research.sh 的基准策略模式会把它们复制到一起），再找 ../vendor/。
  找不到就抛出 ImportError（中文提示），freqtrade 报"找不到这个策略"。
  不会影响其他策略：freqtrade 按名字找策略时，只导入文件内容里有 "class <策略名>(" 的文件；
  本文件又放在 user_data/strategies/benchmarks/ 子目录（不在 user_data/strategies/ 本层），
  平时的 make backtest / make research 根本扫不到它，只有 make research-benchmarks（RESEARCH_BENCHMARKS=1）才用。

手动单独回测（需要 5m 15m 1h 4h 1d 的K线，交易对里要有 BTC）：
  .venv/bin/freqtrade backtesting --config configs/config.okx-research.json \\
      --config configs/research-overrides/NostalgiaForInfinityX8Capped.json \\
      --strategy NostalgiaForInfinityX8Capped --strategy-path user_data/strategies/benchmarks --timerange 20260101-
说明：docs/benchmarks.md
"""

from __future__ import annotations

import atexit
import importlib.util
import logging
import math
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any


logger = logging.getLogger(__name__)

# 项目硬规则：杠杆上限（和 scripts/check_drawdown.py 的 --max-leverage 默认值、研究报告的 max_leverage 相同）
MAX_LEVERAGE = 5.0

_HERE = Path(__file__).resolve().parent
_FETCH_HINT = "先在你自己的 VPS 上运行：bash scripts/fetch_benchmarks.sh（说明见 docs/benchmarks.md）"


def _load_vendor_class(file_name: str, class_name: str) -> type:
    """按文件路径导入 vendor/ 里的第三方策略，返回它的类。找不到时抛 ImportError（freqtrade 会显示这条中文提示）。"""
    candidates = [_HERE / file_name, _HERE.parent / "vendor" / file_name, _HERE / "vendor" / file_name]
    path = next((p for p in candidates if p.is_file()), None)
    if path is None:
        tried = "、".join(str(p) for p in candidates)
        raise ImportError(f"没找到基准策略文件 {file_name}（找过：{tried}）。{_FETCH_HINT}")
    # 用自己的模块名，不和 freqtrade 自己按文件名导入的模块混在一起；放进 sys.modules，inspect.getsource 等才找得到
    mod_name = f"easytrade_vendor_{path.stem}"
    mod = sys.modules.get(mod_name)
    if mod is None or Path(getattr(mod, "__file__", "") or "").resolve() != path.resolve():
        spec = importlib.util.spec_from_file_location(mod_name, str(path))
        if spec is None or spec.loader is None:
            raise ImportError(f"没法导入 {path}。{_FETCH_HINT}")
        mod = importlib.util.module_from_spec(spec)
        sys.modules[mod_name] = mod
        try:
            spec.loader.exec_module(mod)
        except BaseException:
            sys.modules.pop(mod_name, None)
            raise
    cls = getattr(mod, class_name, None)
    if not isinstance(cls, type):
        raise ImportError(f"{path} 里没有 class {class_name}（文件版本不对？{_FETCH_HINT}）")
    return cls


def _capped(value: Any) -> float:
    """杠杆值封顶到 MAX_LEVERAGE；不是正常数字时按 1 倍（freqtrade 默认值）。"""
    try:
        lev = float(value)
    except (TypeError, ValueError):
        return 1.0
    if not math.isfinite(lev) or lev <= 0:
        return 1.0
    return min(lev, MAX_LEVERAGE)


# 上游类（导入失败时整个文件导入失败，freqtrade 只会说找不到 NostalgiaForInfinityX8Capped，不影响别的策略）
NostalgiaForInfinityX8 = _load_vendor_class("NostalgiaForInfinityX8.py", "NostalgiaForInfinityX8")


class NostalgiaForInfinityX8Capped(NostalgiaForInfinityX8):
    """NostalgiaForInfinityX8（固定版本，见 scripts/fetch_benchmarks.sh），杠杆封顶 5 倍；回测时止盈目标缓存每次从空开始。"""

    def __init__(self, config: dict) -> None:
        super().__init__(config)
        self._easytrade_cap_leverage_settings()
        self._easytrade_fresh_backtest_cache(config)

    def bot_start(self, **kwargs: Any) -> None:
        # 上游如果在 bot_start 里才建（或重建）止盈目标缓存 / 读配置里的杠杆设置，这里再处理一次（已经处理过的不会重复）
        super().bot_start(**kwargs)
        self._easytrade_cap_leverage_settings()
        self._easytrade_fresh_backtest_cache(self.config, final=True)

    def _easytrade_cap_leverage_settings(self) -> None:
        # 上游允许从配置改的杠杆设置：futures_mode_leverage、futures_mode_leverage_rebuy_mode、…_long、…_short_grind_mode 等
        for name in [n for n in dir(type(self)) if n.startswith("futures_mode_leverage")]:
            value = getattr(self, name, None)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                continue  # None = 上游"不单独设置"，保持原样
            if value > MAX_LEVERAGE:
                logger.warning(
                    "%s = %s 超过项目杠杆上限 %s 倍，改成 %s 倍（user_data/strategies/benchmarks/BenchmarkCaps.py）",
                    name, value, MAX_LEVERAGE, MAX_LEVERAGE,
                )
                setattr(self, name, MAX_LEVERAGE)

    def _easytrade_fresh_backtest_cache(self, config: dict, final: bool = False) -> None:
        """回测 / 调参时把 NFI 的止盈目标缓存换成临时目录里的空文件。可以调用多次（已经换过就不动）。
        final = True（bot_start 里最后一次）时还没换成功就打警告：各段回测之间可能互相影响，结果要打问号。"""
        runmode = config.get("runmode")
        runmode = getattr(runmode, "value", runmode)
        if str(runmode) not in ("backtest", "hyperopt"):
            return  # 模拟盘 / 实盘：保持上游行为（重启后接着用自己的止盈目标）
        cache = getattr(self, "target_profit_cache", None)
        old_path = getattr(cache, "path", None)
        done_dir = self.__dict__.get("_easytrade_cache_dir")
        if done_dir is not None and old_path is not None and Path(old_path).parent == done_dir:
            return  # 已经换过了
        if cache is None or old_path is None:
            if final:
                logger.warning(
                    "NFI 的止盈目标缓存（target_profit_cache）不是预期的写法，没能换成空的临时文件："
                    "user_data/ 下的 nfix8-profit_max-*-(backtest).json 会被各段回测共用，结果之间可能互相影响"
                    "（每段回测前手动删掉这个文件可以避免；见 docs/benchmarks.md 第 4 节）"
                )
            return
        try:
            tmp_dir = Path(tempfile.mkdtemp(prefix="easytrade-nfi-bt-"))
            atexit.register(shutil.rmtree, str(tmp_dir), True)
            self.target_profit_cache = type(cache)(tmp_dir / Path(old_path).name)
            self.__dict__["_easytrade_cache_dir"] = tmp_dir
        except Exception as exc:  # 上游改了缓存的写法时只提醒，不影响回测本身
            logger.warning("没能给 NFI 的止盈目标缓存换成空的临时文件（%s）：各段回测之间可能互相影响", exc)

    def leverage(
        self,
        pair: str,
        current_time: Any,
        current_rate: float,
        proposed_leverage: float,
        max_leverage: float,
        entry_tag: str | None,
        side: str,
        **kwargs: Any,
    ) -> float:
        lev = super().leverage(
            pair=pair,
            current_time=current_time,
            current_rate=current_rate,
            proposed_leverage=proposed_leverage,
            max_leverage=max_leverage,
            entry_tag=entry_tag,
            side=side,
            **kwargs,
        )
        return _capped(lev)
