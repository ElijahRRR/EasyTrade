"""
NewListingShortHedged -- 新上线永续做空 + 等额 BTC 多单对冲, 日线 (第 5 轮预注册策略)。

预注册规格: docs/round5-preregistration.md 第 2.4 节 (运行前冻结, 改任何一条都要换新类名)。
机制: 新上线的永续通常在炒作高点上市、流通量小、之后不断解锁, 上线几个月内往往大幅下跌 (供给压力)。
      做空新币, 同时用名义金额相等的 BTC 多单对冲 (beta = 1), 赚 "新币相对 BTC 跑输" 的那部分。

参数 (3 个, 预注册, 不调):
  entry_delay_days = 14     上线当天算第 1 天, 第 15 天开盘做空 (= 上线日 + 14 天)
  exit_age_days    = 120    上线满 120 天后的第一个开盘平仓 (= 上线日 + 120 天 00:00 UTC), 每笔约持有 106 天
  stop_pct         = 0.40   固定止损: 价格涨到入场价的 1.4 倍就平仓; 开盘价跳空越过止损价时按开盘价平仓
                            (回测里由 easytrade_risk 的 et_stop_gap 处理, 不假装在止损价成交)
固定常量 (同样冻结):
  入场条件: 入场前 7 根日K线 (第 8 ~ 14 天) 的平均日成交额 >= 1,000,000 USDT。成交额 = 收盘价 x 成交量, 单位换算和
            仓库其他策略相同 (rust_port_common.ohlcv_quote_volume: OKX / 币安的 volume 是币的数量, 不乘合约面值;
            K线成交量是张数的交易所自动乘 contractSize, 见 docs/okx-volume-units.md)。
  仓位 (easytrade_risk 混入类): 每笔风险 0.5% (risk_per_trade = 0.005), 止损距离 40% + 成本缓冲 0.2% ->
            名义金额约 = 权益 x 0.005 / 0.402 ≈ 1.24%; 杠杆 1 倍 (逐仓 1 倍空单要涨约 100% 才会强平);
            组合热度上限 6% (max_portfolio_risk = 0.06, 同时约 12 笔空单), 混入类的回撤降仓保留 (权益比最高点低 8% 起新仓减半)。
  对冲 (enter_tag = "hedge"): BTC/USDT:USDT 多单, 名义金额 = 所有未平新币空单按现价计算的名义金额之和 (beta = 1)。
            BTC 的入场信号: 有任何一个新币处在持有窗口 (第 15 天开盘 ~ 第 121 天开盘) 里就为真;
            开多少由 custom_stake_amount 按当时真正开着的空单算 (一笔空单都没有 = 0 = 不开)。
            每天 00:00 UTC 检查一次 (adjust_trade_position): 对冲名义金额和空单名义金额之和差超过 10% 才调到相等。
            没有空单时 custom_exit 平掉对冲单。对冲单不设 ATR / 固定止损 (stoploss = -0.99 只是兜底), 不计入热度, 杠杆 1 倍。
  freqtrade 合约是单向持仓模式: 同一个币不能同时有多单和空单, 所以本策略永远不做空 BTC, 也永远不做多别的币
  (confirm_trade_entry 里再挡一次)。

上线日 (第 1 天):
  回测 / hyperopt / 分析工具: 只用预先算好的上线日文件 (scripts/build_listings.py 扫描完整的本地 1d 数据生成):
    默认 user_data/listings/<数据目录名>_first_candle.json (数据目录 user_data/data/okx -> okx_first_candle.json),
    配置里写 "nlsh_listings_file": "<路径>" 可以换文件 (相对路径按仓库根目录 = user_data 的上一级算)。
    不能用 "加载进来的第一行": 回测只加载 "回测区间 - 预热" 那一截, 老币的第一行不是它的上线日。
    文件里 pre_existing = true 的币 (第一根K线 = 数据起点, 2021 年以前就有的) 不算新币, 不做空。
    文件不存在 / 坏了 -> 一笔都不开, 日志里有中文错误 (先运行 build_listings.py)。文件里没有的币不交易 (日志提醒)。
    数据和文件对不上 (加载进来的数据比文件里的第一根K线还早 = 文件旧了) -> 这个币不交易, 日志报错。
  模拟盘 / 实盘: 同样先用这个文件 (文件不存在照样不开仓); 文件里没有的币 (文件生成之后才上线的) 用交易所给的上线时间:
    ccxt market 的 created (OKX = contTdSwTime / listTime, 币安 = onboardDate), 再没有就读 info 里的 listTime / onboardDate。
    这个兜底只在模拟盘 / 实盘里用, 回测里一律不用 (交易所接口里的上线时间是 "现在" 的数据, 已下架的币也查不到)。

实现说明 (freqtrade 2026.10, 按 .venv 里的源码核对):
  - startup_candle_count = 13 (规格写的是 "最多 14"): 回测时 freqtrade 先按根数裁掉每个币开头的 startup_candle_count 根
    (optimize/backtesting.py _get_ohlcv_as_lists -> converter.trim_dataframe), 把信号整体后移一根之后,
    还会再去掉剩下的第一根 (同一函数里的 df_analyzed.drop(df_analyzed.head(1).index))。
    所以新币最早能在第 startup + 2 天开盘入场: 设 14 时最早是第 16 天, 第 15 天的入场就没了; 13 是能保住第 15 天入场的最大值。
    指标只有 7 天平均成交额 (第 8 ~ 14 天的K线, 在裁掉之前算好) 和上线日 (来自文件), 不需要更长的预热。
  - 研究脚本按根数给每段加预热; 规格的运行命令带 RESEARCH_WARMUP_CANDLES=0 (每段只用策略自己的 13 根),
    RESEARCH_MIN_TRAIN_DAYS=0 (上线晚的币不去掉)。
  - 回测里每根K线按 "先处理有持仓的币 (按开仓先后), 再按白名单顺序处理其他币" 的顺序走 (_time_pair_generator_det)。
    BTC 放在白名单最后: 对冲单第一次开仓时, 同一根K线里先开的新币空单已经算进去了。
    对冲单开着以后它排在后开的空单前面: 某天 00:00 新开的空单, 当天 00:00 的对冲检查还看不到 (要等第二天 00:00,
    而且偏离超过 10% 才调)。实盘也一样 (freqtrade 每一轮先调整已有持仓, 再开新仓), 回测和实盘口径一致。
    同一天 00:00 到期 (满 120 天) 的空单在对冲检查里按 "已经平掉" 算 (规则上它就在这个开盘平仓, 不是未来数据)。
    最后一笔空单在某个 00:00 到期、同一个 00:00 又要开新币空单时 (nlsh_pending_shorts), 对冲单不平
    (这个开盘之后账上还有空单), 第二天 00:00 的检查再按新空单调大小; 新空单没开出来时对冲单下一次 custom_exit 平掉。
  - 中途下架的币 (RESEARCH_KEEP_DELISTED=1 保留下来的): freqtrade 回测里它没有K线以后就不再处理, 还开着的空单要到这一段
    结束才按最后一根K线的开盘价 force_exit。真实的交易所下架时会强制平仓, 所以从最后一根K线收盘起,
    这笔空单不再算进对冲目标和热度 (nlsh_data_ended; 浮动盈亏按那个 force_exit 价格算)。它占着的 freqtrade 名额
    (max_open_trades 40) 和保证金要到这一段结束才放出来, 策略改不了。
  - 对冲单 "每天 00:00 UTC 检查一次": 回测里 adjust_trade_position 每根K线 (带 --timeframe-detail 时每根细节K线) 都会调用,
    这里只在 UTC 0 点那一小时里、每天第一次调用时检查 (检查日期存进交易记录 nlsh_hedge_checked); 开仓当天不再检查 (开仓时已按目标定好)。
    实盘只在 0 点那一小时里、BTC 昨天的日线已经收盘到手之后检查一次。
  - 对冲单 "没有空单就平仓": custom_exit 每次被调用都检查 (回测不带细节K线时一天一次, 带 1h 细节K线时每小时一次, 实盘每一轮)。
  - 现价: 空单 = 这个币在决策时刻之前最后一根已收盘日线的收盘价 (无未来函数)。回测里按日期在 dp.historic_ohlcv 里查
    (不用 dp.get_analyzed_dataframe: 回测时它按币各自的进度切片, 同一根K线里还没轮到的币会晚一天); 实盘用最后一根分析过的K线。
    BTC = freqtrade 传进来的 current_rate (回测 = 这根K线开盘价)。
  - 当天K线内部止损的空单: 回测里它的平仓时间记成这根K线的开盘 (没有细节K线时), 但开盘那一刻其实还开着。
    所以平仓时间 >= 现在、而且不是 "开盘就平" (满 120 天 / 跳空止损 / 对冲平仓) 的空单, 对冲和热度计算里仍按 "还开着" 算,
    到下一根K线才算平掉 (否则对冲单会在同一个开盘 "提前知道" 当天要止损)。
  - lookahead-analysis / recursive-analysis 会把白名单缩成被检查的那一个币, 只留 BTC 时看不到任何空单。
    这两个工具运行时 (runmode = util_no_exchange): 币池 = 配置里的完整交易对列表 (同 XSMomentum; 正则写法按交易所合约列表展开,
    和回测时 StaticPairList 的 expand_pairlist(..., keep_invalid=True) 一样);
    对冲单用 freqtrade 给的建议仓位 (不按空单名义金额)、在 "没有币处在持有窗口" 的下一根开盘平仓 (exit_long 信号), 不做每日调整。
    这样检查的是信号本身 (上线日 + 成交额 + 持有窗口) 有没有用到未来数据; 对冲金额是组合层面的事, 单币工具本来就检查不了。
  - 已核实 freqtrade 回测: 加仓 = adjust_trade_position 返回正数 (保证金), 减仓 = 返回负数,
    减掉的数量 = |返回值| x 持仓数量 / 持仓保证金 (optimize/backtesting.py _check_adjust_trade_for_candle)。
    scripts/check_drawdown.py 按逐笔订单重建对冲单的加减仓 (含浮亏回撤口径不变)。

研究: OKX 全部 USDT 永续 (configs/config.okx-research-listings.json), 每个策略的额外设置 configs/research-overrides/NewListingShortHedged.json。
  OKX 回测没有资金费 (新币空头常常要付很高的负资金费), 所以第一阶段结果只算上限; 第二阶段用币安带真实资金费的数据。
  交易数 30 笔的门槛只数新币空单 (enter_tag != "hedge")。不对冲的结果不报告、不用来做任何选择。
"""

from __future__ import annotations

import json
import logging
import math
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from pandas import DataFrame

from freqtrade.enums import RunMode
from freqtrade.exchange import timeframe_to_prev_date
from freqtrade.persistence import LocalTrade, Trade
from freqtrade.strategy import IStrategy

from easytrade_risk import EXIT_STOP_GAP, EasyTradeRiskMixin, positive_or_none
from rust_port_common import ohlcv_quote_volume, to_epoch_seconds, valid_contract_size


logger = logging.getLogger(__name__)

# 配置里的交易对可能写成正则 (例如 ".*/USDT:USDT"), 这种不是具体的币 (同 XSMomentum)
_PAIR_REGEX_CHARS = re.compile(r"[*?\[\]()|+\\^$]")
# 日志里的固定标记: 研究报告 (scripts/research_report.py) 按它数 "因为最小下单额开不出来" 的次数 (和 XSMomentum 相同, 改的话一起改)
MIN_STAKE_SKIP_TAG = "[最小下单额:开不出来]"
MIN_STAKE_BUMP_TAG = "[最小下单额:放大]"
FT_MIN_STAKE_ADJUST = 1.3  # freqtrade 最多把仓位放大 30% 去凑最小下单额 (freqtrade/wallets.py validate_stake_amount)
LISTINGS_FILE_VERSION = 1  # scripts/build_listings.py FORMAT_VERSION
DAY = timedelta(days=1)

# trade custom data 的键 (存在交易记录里, 机器人重启后还在)
KEY_LISTING_DAY = "nlsh_listing_day"  # 空单: 这个币的上线日 (第 1 天, YYYY-MM-DD)
KEY_HEDGE_CHECKED = "nlsh_hedge_checked"  # 对冲单: 最近一次 00:00 检查的日期 (YYYY-MM-DD)

# 离场原因代码
EXIT_AGE = "nlsh_age_exit"  # 空单: 上线满 exit_age_days 天后的第一个开盘
EXIT_HEDGE_FLAT = "nlsh_hedge_flat"  # 对冲单: 没有空单了
# 在K线开盘这一刻就按规则平掉的离场原因 (开盘时就知道); 其他离场 (止损 / 强平) 是盘中发生的
OPEN_EXITS = frozenset({EXIT_AGE, EXIT_STOP_GAP, EXIT_HEDGE_FLAT})


def _to_day(value: Any) -> date | None:
    """YYYY-MM-DD / datetime / 毫秒时间戳 -> UTC 日期; 不认识返回 None。"""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).date() if value.tzinfo else value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, (int, float)) and math.isfinite(float(value)) and value > 0:
        return datetime.fromtimestamp(float(value) / 1000.0, tz=timezone.utc).date()
    text = str(value).strip()
    if text.isdigit():
        return _to_day(int(text))
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def _day_ts(d: date) -> pd.Timestamp:
    return pd.Timestamp(d.isoformat(), tz="UTC")


class NewListingShortHedged(EasyTradeRiskMixin, IStrategy):
    INTERFACE_VERSION = 3

    timeframe = "1d"
    can_short = True
    # 最多 14 (预注册); 13 才保得住第 15 天开盘的入场 (见文件开头 "实现说明")
    startup_candle_count = 13
    process_only_new_candles = True

    minimal_roi = {"0": 100.0}
    trailing_stop = False
    use_custom_stoploss = True
    use_exit_signal = True  # custom_exit (满 120 天离场 / 对冲平仓 / 止损补救) 需要它
    exit_profit_only = False
    ignore_roi_if_entry_signal = False
    # 兜底: 空单成交后马上换成 1.4 倍入场价的固定止损 (custom_stoploss); 对冲单就停在这里 (= 不设止损)
    stoploss = -0.99
    # 对冲单按空单名义金额加减仓 (配置里也写了 position_adjustment_enable: true)
    position_adjustment_enable = True
    max_entry_position_adjustment = -1

    # ---------------- 预注册参数 (3 个, 普通类属性, 不调) ----------------
    entry_delay_days = 14
    exit_age_days = 120
    stop_pct = 0.40

    # ---------------- 固定常量 (预注册, 不调) ----------------
    min_avg_quote_volume = 1_000_000.0  # 入场前 7 天平均日成交额 (USDT)
    volume_window_days = 7
    hedge_drift = 0.10  # 对冲名义金额偏离超过 10% 才调整
    hedge_check_hour_utc = 0  # 每天 00:00 UTC 检查
    hedge_tag = "hedge"
    short_tag = "nlsh_short"

    # ---------------- 风控 (easytrade_risk) ----------------
    risk_per_trade = 0.005
    max_portfolio_risk = 0.06
    max_leverage_param = 1.0
    et_name = "nlsh"

    # 上线日文件: 配置里的这个键可以指定路径; 不写 = user_data/listings/<数据目录名>_first_candle.json
    listings_config_key = "nlsh_listings_file"

    plot_config = {
        "main_plot": {},
        "subplots": {
            "Volume7d": {"nlsh_qv7": {"color": "#2f80ed"}},
            "Listing": {"nlsh_age_entry": {"color": "#828282"}, "nlsh_window_n": {"color": "#eb5757"}},
        },
    }

    # ======================================================================
    # 运行模式 / 币池
    # ======================================================================

    def _analysis_mode(self) -> bool:
        """lookahead-analysis / recursive-analysis (它们会把白名单缩成一个币)。"""
        dp = getattr(self, "dp", None)
        mode = dp.runmode if dp is not None else self.config.get("runmode")
        return str(getattr(mode, "value", mode)) == RunMode.UTIL_NO_EXCHANGE.value

    def _log_once(self, key: Any, level: int, msg: str, *args: Any) -> None:
        seen = self.__dict__.setdefault("_nlsh_logged", set())
        if key in seen:
            return
        if len(seen) > 5000:
            seen.clear()
        seen.add(key)
        logger.log(level, msg, *args)

    def nlsh_universe(self) -> list[str]:
        """新币空单的币池 = 当前白名单 (分析工具运行时再加上配置里的完整交易对列表; 同 XSMomentum.xs_universe)。"""
        if getattr(self, "dp", None) is None:
            return []
        pairs = list(self.dp.current_whitelist())
        if self._analysis_mode():
            extra: list[Any] = []
            cfg_pairs = self.config.get("pairs")
            if isinstance(cfg_pairs, list):
                extra += cfg_pairs
            extra += (self.config.get("exchange") or {}).get("pair_whitelist") or []
            orig = self.config.get("original_config") or {}
            extra += (orig.get("exchange") or {}).get("pair_whitelist") or []
            pairs += self._nlsh_expand([p for p in extra if isinstance(p, str)])
        return list(dict.fromkeys(pairs))

    def _nlsh_expand(self, patterns: list[str]) -> list[str]:
        """Expand regex whitelist entries exactly like StaticPairList(allow_inactive) does in backtesting
        (pairlistmanager.verify_whitelist -> expand_pairlist(..., exchange.get_markets().keys(), keep_invalid=True)).
        Falls back to the literal (non-regex) entries if the markets are unavailable."""
        literal = [p for p in patterns if not _PAIR_REGEX_CHARS.search(p)]
        if not any(_PAIR_REGEX_CHARS.search(p) for p in patterns):
            return literal
        cache = self.__dict__.setdefault("_nlsh_expand_cache", {})
        key = tuple(patterns)
        if key in cache:
            return cache[key]
        try:
            from freqtrade.plugins.pairlist.pairlist_helpers import expand_pairlist

            markets = list(self.dp._exchange.get_markets().keys())
            out = expand_pairlist(patterns, markets, keep_invalid=True)
        except Exception as exc:  # noqa: BLE001
            self._log_once(("expand_fail",), logging.WARNING,
                           "%s: 展开白名单里的正则失败 (%s), 只用写死的交易对", self.et_name, exc)
            out = literal
        cache[key] = out
        return out

    def _btc_ready(self) -> bool:
        """BTC 对冲腿能不能用: 在白名单里 (回测时还要有数据)。不能用时不开任何新币空单 (只有对冲后的版本算数)。"""
        if self._analysis_mode():
            return True
        universe = self.nlsh_universe()
        if self.btc_pair not in universe:
            self._log_once(("no_btc",), logging.ERROR,
                           "%s: 白名单里没有 %s, 没法对冲。预注册规定只有对冲后的版本算数, 所以一笔新币空单都不开",
                           self.et_name, self.btc_pair)
            return False
        if not any(p != self.btc_pair for p in universe):
            self._log_once(("only_btc",), logging.ERROR,
                           "%s: 白名单里除了 %s 没有别的币, 一笔新币空单都不会开。白名单里的正则 (例如 \".*/USDT:USDT\") "
                           "是不是没有展开? 用 scripts/build_listings.py --write-pairs 生成具体的交易对列表再叠加进来",
                           self.et_name, self.btc_pair)
        if universe[-1] != self.btc_pair:
            self._log_once(("btc_not_last",), logging.WARNING,
                           "%s: %s 不在白名单最后。回测里对冲单第一次开仓会晚一天 (同一根K线先处理的 BTC 还看不到新开的空单); "
                           "研究配置 configs/config.okx-research-listings.json 把它放在最后", self.et_name, self.btc_pair)
        if not self._et_is_live():
            ok = self.__dict__.get("_nlsh_btc_data")
            if ok is None:
                try:
                    df = self.dp.historic_ohlcv(pair=self.btc_pair, timeframe=self.timeframe)
                    ok = df is not None and not df.empty
                except Exception as exc:  # noqa: BLE001
                    logger.warning("%s: 读取 %s 数据失败: %s", self.et_name, self.btc_pair, exc)
                    ok = False
                self.__dict__["_nlsh_btc_data"] = ok
            if not ok:
                self._log_once(("no_btc_data",), logging.ERROR,
                               "%s: 没有 %s 的 %s 数据, 没法对冲, 一笔新币空单都不开", self.et_name, self.btc_pair, self.timeframe)
                return False
        return True

    # ======================================================================
    # 上线日
    # ======================================================================

    def nlsh_listings_path(self) -> Path:
        cfg = self.config or {}
        raw = cfg.get(self.listings_config_key)
        ud = Path(cfg.get("user_data_dir") or "user_data")
        if raw:
            p = Path(str(raw))
            return p if p.is_absolute() else (ud.resolve().parent / p)
        datadir = cfg.get("datadir")
        name = Path(str(datadir)).resolve().name if datadir else str((cfg.get("exchange") or {}).get("name") or "")
        return ud / "listings" / f"{name}_first_candle.json"

    def _listings(self) -> dict[str, dict[str, Any]] | None:
        """上线日文件 {pair: {"first": date, "pre_existing": bool}}; 文件不存在 / 坏了返回 None (= 不开仓)。按修改时间缓存。"""
        path = self.nlsh_listings_path()
        try:
            mtime = path.stat().st_mtime
        except OSError:
            self._log_once(("listings_missing", str(path)), logging.ERROR,
                           "%s: 找不到上线日文件 %s, 一笔都不开。先运行: .venv/bin/python scripts/build_listings.py --datadir <K线数据目录> "
                           "--data-start <下载开始日期 YYYYMMDD> (或者在配置里用 \"%s\" 指定文件)",
                           self.et_name, path, self.listings_config_key)
            return None
        cache = self.__dict__.get("_nlsh_listings_cache")
        if cache is not None and cache[0] == (str(path), mtime):
            return cache[1]
        out: dict[str, dict[str, Any]] | None = None
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
            if int(doc.get("version", 0)) != LISTINGS_FILE_VERSION or not isinstance(doc.get("pairs"), dict):
                raise ValueError(f"格式不对 (version = {doc.get('version')}, 要 {LISTINGS_FILE_VERSION})")
            out = {}
            for pair, rec in doc["pairs"].items():
                first = _to_day((rec or {}).get("first_candle"))
                if first is None:
                    raise ValueError(f"{pair} 没有有效的 first_candle")
                out[str(pair)] = {"first": first, "pre_existing": bool(rec.get("pre_existing"))}
            n_new = sum(1 for r in out.values() if not r["pre_existing"])
            logger.info("%s: 上线日文件 %s: %d 个币, 其中新上线 %d 个 (数据起点 %s)",
                        self.et_name, path, len(out), n_new, doc.get("data_start"))
        except Exception as exc:  # noqa: BLE001
            self._log_once(("listings_bad", str(path), mtime), logging.ERROR,
                           "%s: 上线日文件 %s 读不了 (%s), 一笔都不开。重新运行 scripts/build_listings.py",
                           self.et_name, path, exc)
            out = None
        self.__dict__["_nlsh_listings_cache"] = ((str(path), mtime), out)
        return out

    def _market_listing_day(self, pair: str) -> date | None:
        """模拟盘 / 实盘兜底: 交易所给的上线时间 (ccxt market 的 created, 或 info 里的 listTime / onboardDate)。"""
        try:
            market = self.dp.market(pair) or {}
        except Exception:  # noqa: BLE001
            return None
        info = market.get("info") or {}
        for v in (market.get("created"), info.get("listTime"), info.get("onboardDate")):
            d = _to_day(v)
            if d is not None:
                return d
        return None

    def nlsh_listing_day(self, pair: str) -> date | None:
        """这个币的上线日 (第 1 天); 老币 / 不知道 / BTC 返回 None (= 不做空)。"""
        if pair == self.btc_pair:
            return None
        listings = self._listings()
        if listings is None:
            return None
        rec = listings.get(pair)
        if rec is not None:
            return None if rec["pre_existing"] else rec["first"]
        if self._et_is_live():
            d = self._market_listing_day(pair)
            if d is not None:
                self._log_once(("market_listing", pair), logging.INFO,
                               "%s %s: 上线日文件里没有这个币, 用交易所给的上线时间 %s", self.et_name, pair, d)
                return d
            self._log_once(("no_listing_live", pair), logging.WARNING,
                           "%s %s: 上线日文件里没有、交易所也没给上线时间, 这个币不交易", self.et_name, pair)
            return None
        self._log_once(("no_listing", pair), logging.WARNING,
                       "%s %s: 上线日文件里没有这个币 (文件比数据旧? 重新运行 scripts/build_listings.py), 这个币不交易",
                       self.et_name, pair)
        return None

    def _windows(self, pairs: list[str]) -> np.ndarray:
        """币池里每个新币的持有窗口 [上线日 + entry_delay_days, 上线日 + exit_age_days), UTC 秒数, 形状 (m, 2)。"""
        rows = []
        for p in pairs:
            d = self.nlsh_listing_day(p)
            if d is None:
                continue
            t0 = int(_day_ts(d).timestamp())
            rows.append((t0 + int(self.entry_delay_days) * 86400, t0 + int(self.exit_age_days) * 86400))
        return np.array(rows, dtype="int64").reshape(-1, 2)

    # ======================================================================
    # 成交额 (USDT): 和 rust_port_common.RustPortMixin.ohlcv_volume_units 同一规则
    # ======================================================================

    def _volume_units(self, pair: str) -> tuple[bool, float | None, str]:
        exchange = getattr(self.dp, "_exchange", None) if getattr(self, "dp", None) is not None else None
        if exchange is None:
            return False, None, "base"
        try:
            in_contracts = bool(exchange.get_option("ohlcv_volume_in_contracts", False))
            currency = str(exchange.get_option("ohlcv_volume_currency", "base") or "base")
        except Exception as exc:  # noqa: BLE001
            logger.warning("%s 读取交易所成交量单位失败, 按默认 (币的数量) 处理: %s", self.et_name, exc)
            return False, None, "base"
        contract_size = None
        if in_contracts:
            try:
                contract_size = valid_contract_size((self.dp.market(pair) or {}).get("contractSize"))
            except Exception:  # noqa: BLE001
                contract_size = None
            if contract_size is None and currency.lower() != "quote":
                self._log_once(("ct_missing", pair), logging.ERROR,
                               "%s %s: 交易所的K线成交量是张数, 但拿不到合约面值 contractSize, 成交额无法换算, 这个币不会出信号",
                               self.et_name, pair)
        return in_contracts, contract_size, currency

    # ======================================================================
    # freqtrade 接口: 指标 / 信号
    # ======================================================================

    def informative_pairs(self):
        return []  # BTC 本身就在白名单里 (对冲腿), 不需要额外数据

    def bot_start(self, **kwargs: Any) -> None:
        super().bot_start(**kwargs)
        if not self._et_is_live():
            return
        self._listings()  # 文件不存在时启动日志里就报错
        if getattr(self, "dp", None) is not None:
            wl = list(self.dp.current_whitelist())
            if self.btc_pair not in wl:
                logger.error("%s: 白名单里没有 %s, 没法对冲, 不会开新币空单", self.et_name, self.btc_pair)

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        pair = metadata["pair"]
        n = len(dataframe)
        in_contracts, cs, currency = self._volume_units(pair)
        qv = ohlcv_quote_volume(dataframe, volume_in_contracts=in_contracts, contract_size=cs, volume_currency=currency)
        dataframe["nlsh_qv"] = qv.astype("float64")
        w = int(self.volume_window_days)
        # Explicit fixed-order sum of the last w closed candles (NaN until w candles exist). Unlike pandas'
        # online rolling sum this is bit-identical whatever the startup length (clean recursive-analysis).
        acc = dataframe["nlsh_qv"].copy()
        for i in range(1, w):
            acc = acc + dataframe["nlsh_qv"].shift(i)
        dataframe["nlsh_qv7"] = acc / w
        cols = {c: np.full(n, np.nan) for c in ("nlsh_listing_ts", "nlsh_age_entry", "nlsh_window_n")}
        if n and getattr(self, "dp", None) is not None:
            secs = to_epoch_seconds(dataframe["date"])
            entry_secs = secs + 86400  # 信号K线收盘 = 下一根开盘 = 入场时刻
            if pair == self.btc_pair:
                win = self._windows([p for p in self.nlsh_universe() if p != self.btc_pair])
                if len(win):
                    inside = (entry_secs[:, None] >= win[None, :, 0]) & (entry_secs[:, None] < win[None, :, 1])
                    cols["nlsh_window_n"] = inside.sum(axis=1).astype("float64")
                else:
                    cols["nlsh_window_n"] = np.zeros(n)
            else:
                d = self.nlsh_listing_day(pair)
                if d is not None:
                    t0 = int(_day_ts(d).timestamp())
                    if not self._et_is_live() and int(secs[0]) < t0:
                        # 加载进来的数据比文件里的第一根K线还早: 文件是旧的 (数据后来又往前补过)
                        self._log_once(("listing_stale", pair), logging.ERROR,
                                       "%s %s: 数据从 %s 开始, 比上线日文件里的第一根K线 %s 还早 (文件旧了), 这个币不交易。"
                                       "重新运行 scripts/build_listings.py", self.et_name, pair,
                                       pd.Timestamp(int(secs[0]), unit="s", tz="UTC").date(), d)
                    else:
                        cols["nlsh_listing_ts"] = np.full(n, float(t0))
                        # 入场时刻是上线后第几天 (0 = 上线当天 = 第 1 天; 14 = 第 15 天)
                        cols["nlsh_age_entry"] = ((entry_secs - t0) // 86400).astype("float64")
        for c, v in cols.items():
            dataframe[c] = v
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        pair = metadata["pair"]
        n = len(dataframe)
        short_sig = np.zeros(n, dtype=bool)
        long_sig = np.zeros(n, dtype=bool)
        if n and self._listings() is not None and self._btc_ready():
            if pair == self.btc_pair:
                wn = dataframe["nlsh_window_n"].to_numpy(dtype="float64")
                long_sig = np.nan_to_num(wn, nan=0.0) > 0
            else:
                age = dataframe["nlsh_age_entry"].to_numpy(dtype="float64")
                qv7 = dataframe["nlsh_qv7"].to_numpy(dtype="float64")
                short_sig = (age == float(self.entry_delay_days)) & (qv7 >= float(self.min_avg_quote_volume))  # NaN -> False
        dataframe["enter_long"] = long_sig.astype("int64")
        dataframe["enter_short"] = short_sig.astype("int64")
        tags = np.full(n, None, dtype=object)
        tags[long_sig] = self.hedge_tag
        tags[short_sig] = self.short_tag
        dataframe["enter_tag"] = pd.Series(tags, index=dataframe.index, dtype=object)
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        # 正常运行: 不用离场信号 (满 120 天 / 对冲平仓都在 custom_exit 里按时间和持仓判断)。
        # 分析工具运行时: 对冲单在 "没有币处在持有窗口" 的下一根开盘平仓 (代替 "没有空单", 见文件开头)。
        n = len(dataframe)
        exit_long = np.zeros(n, dtype=bool)
        if metadata["pair"] == self.btc_pair and self._analysis_mode() and n:
            wn = dataframe["nlsh_window_n"].to_numpy(dtype="float64")
            exit_long = np.nan_to_num(wn, nan=0.0) <= 0
        dataframe["exit_long"] = exit_long.astype("int64")
        dataframe["exit_short"] = np.zeros(n, dtype="int64")
        tags = np.full(n, None, dtype=object)
        tags[exit_long] = EXIT_HEDGE_FLAT
        dataframe["exit_tag"] = pd.Series(tags, index=dataframe.index, dtype=object)
        return dataframe

    # ======================================================================
    # 持仓小工具
    # ======================================================================

    def _is_hedge(self, trade: Trade) -> bool:
        return trade.pair == self.btc_pair and not trade.is_short

    def _trade_listing_day(self, trade: Trade) -> date | None:
        d = _to_day(trade.get_custom_data(KEY_LISTING_DAY))
        return d if d is not None else self.nlsh_listing_day(trade.pair)

    def _age_exit_time(self, trade: Trade) -> datetime | None:
        d = self._trade_listing_day(trade)
        if d is None:
            return None
        return _day_ts(d).to_pydatetime() + timedelta(days=int(self.exit_age_days))

    def nlsh_close_at(self, pair: str, current_time: datetime) -> float | None:
        """这个币在 current_time 时最后一根已收盘日线的收盘价 (无未来函数)。

        回测里不用 dp.get_analyzed_dataframe 的最后一行: 它只在 freqtrade 处理到这个币时才往前推一根,
        同一根K线里排在后面处理的币还停在前天 (见文件开头 "实现说明" 的处理顺序)。
        这里按日期查 dp.historic_ohlcv (这一段回测加载的数据), 取开盘时间 <= current_time - 1 天的最后一根。
        模拟盘 / 实盘: 分析好的数据最后一行就是最后一根已收盘K线。
        """
        hist = self._nlsh_hist(pair)
        if hist is None:
            return self.et_last_close(pair)
        secs, _open, close = hist
        i = int(np.searchsorted(secs, current_time.timestamp() - 86400, side="right")) - 1
        return positive_or_none(close[i]) if i >= 0 else None

    def _nlsh_hist(self, pair: str) -> tuple[np.ndarray, np.ndarray, np.ndarray] | None:
        """回测 / 分析: 这一段加载的日线 (dp.historic_ohlcv) 的 (开盘时间 UTC 秒, 开盘价, 收盘价), 按数据指纹缓存。
        模拟盘 / 实盘 / 读不到 = None。"""
        if self._et_is_live() or getattr(self, "dp", None) is None:
            return None
        try:
            df = self.dp.historic_ohlcv(pair=pair, timeframe=self.timeframe)
        except Exception:  # noqa: BLE001
            df = None
        if df is None or df.empty:
            return None
        cache = self.__dict__.setdefault("_nlsh_close_cache", {})
        key = (len(df), int(pd.Timestamp(df["date"].iat[-1]).timestamp()))
        hit = cache.get(pair)
        if hit is None or hit[0] != key:
            hit = (key, to_epoch_seconds(df["date"]), df["open"].to_numpy(dtype="float64", copy=True),
                   df["close"].to_numpy(dtype="float64", copy=True))
            cache[pair] = hit
        return hit[1], hit[2], hit[3]

    def nlsh_data_ended(self, pair: str, current_time: datetime | None) -> float | None:
        """回测 / 分析: 这个币的数据在 current_time 之前已经结束 (中途下架, RESEARCH_KEEP_DELISTED=1 保留下来的币)
        时返回 freqtrade 最后给它的平仓价 (最后一根K线的开盘价), 否则 None。

        freqtrade 回测里一个币没有K线以后就不再处理它 (optimize/backtesting.py validate_row), 还开着的仓位要到这一段结束
        才按最后一根K线的开盘价 force_exit, 平仓时间记成那根K线。真实的交易所下架时按结算价强制平仓,
        所以这种仓位从 "最后一根K线收盘" 起不再算进对冲目标和热度 (浮动盈亏按 freqtrade 将来记的平仓价算)。
        "最后一根K线收盘" 以后才判断, 所以不会提前知道它要下架; 中间缺几根K线 (数据有缺口但后面还有) 不算结束。
        实盘: 交易所自己会平掉下架合约的仓位, 这里永远返回 None。
        """
        if current_time is None:
            return None
        hist = self._nlsh_hist(pair)
        if hist is None:
            return None
        secs, open_, _close = hist
        if current_time.timestamp() < float(secs[-1]) + self._tf_seconds():
            return None
        return positive_or_none(open_[-1]) or 0.0

    def _book_trades(self, current_time: datetime) -> list[Trade]:
        """这一刻 "账上" 的交易 = 未平的, 加上 (只在回测里) 在当前这根K线里盘中才平掉的。

        回测不带细节K线时, 盘中打到止损的交易 close_date 记成这根K线的开盘时间 (带 1h 细节K线时是那一小时的开盘)。
        同一根K线里排在后面处理的币 (例如 BTC 对冲单) 在 "开盘" 这一刻还不应该知道它会在盘中被止损,
        否则对冲单会在开盘价平仓 / 减仓, 用到了开盘之后才发生的事 (未来数据)。所以这种交易在这根K线里仍按 "未平" 算,
        下一根K线才算平掉。在开盘就按规则平掉的 (满 120 天 nlsh_age_exit、开盘跳空越过止损 et_stop_gap) 开盘时就知道, 不算。
        实盘里止损是实时发生的 (close_date 早于现在), 不需要这一步。
        """
        trades = list(Trade.get_trades_proxy(is_open=True))
        if not self._et_is_live():
            for t in reversed(LocalTrade.bt_trades):  # 按平仓先后追加, 从后往前找到这根K线以前就停
                cd = t.close_date_utc if t.close_date else None
                if cd is None or cd < current_time:
                    break
                if t.exit_reason not in OPEN_EXITS:
                    trades.append(t)
        return trades

    def _open_shorts(self, current_time: datetime) -> list[tuple[Trade, float]]:
        """账上的新币空单和数量 (实盘里还没成交的开仓单按挂单数量算; 盘中才平掉的见 _book_trades)。

        这一刻已经到期 (满 exit_age_days 天, 规则上就在这个开盘平仓) 的空单不算。
        回测里数据已经结束的币 (中途下架) 的空单也不算 (见 nlsh_data_ended)。
        """
        out = []
        for t in self._book_trades(current_time):
            if not t.is_short or t.pair == self.btc_pair:
                continue
            due = self._age_exit_time(t)
            if due is not None and current_time >= due:
                continue
            if t.is_open and self.nlsh_data_ended(t.pair, current_time) is not None:
                continue
            amount = float(t.amount or 0.0)
            for o in (getattr(t, "open_orders", []) or []) if t.is_open else []:
                if getattr(o, "ft_order_side", None) == t.entry_side:
                    amount += float(o.safe_remaining or 0.0)
            if amount > 0:
                out.append((t, amount))
        return out

    def nlsh_pending_shorts(self, current_time: datetime) -> list[str]:
        """这根K线开盘时就要开新币空单的币 (入场信号在上一根已收盘日线上: 上线第 15 天 + 7 天平均成交额够)。

        用途: 最后一笔空单在某个 00:00 到期、同一个 00:00 又有新币空单要开时, freqtrade 先处理有持仓的币
        (对冲单排在新空单前面), 对冲单这时看不到任何空单。规格是 "没有空单时就平掉对冲单" ——
        这个开盘之后账上仍有空单, 所以对冲单不平, 第二天 00:00 的检查再按新空单调大小 (同 5.4 节第 3 条的滞后)。
        只在开盘那一刻算 (回测 = 这根K线的开盘时间; 实盘 = 开盘后 ignore_buying_expired_candle_after 秒之内, 之后 freqtrade
        不会再按这根信号开仓)。只用上线日文件和已收盘K线, 没有未来数据。新空单因为热度 / 最小下单额 / 名额没开出来时,
        对冲单在下一次 custom_exit (第二天, 带 1h 细节K线时下一个小时) 平掉。
        """
        if getattr(self, "dp", None) is None or self._listings() is None:
            return []
        candle = timeframe_to_prev_date(self.timeframe, current_time)
        elapsed = (current_time - candle).total_seconds()
        if self._et_is_live():
            limit = float(self.ignore_buying_expired_candle_after or 0)
            if limit > 0 and elapsed >= limit:
                return []
        elif elapsed > 0:
            return []
        busy = {t.pair for t in Trade.get_trades_proxy(is_open=True)}
        entry_day = (pd.Timestamp(candle) - pd.Timedelta(days=int(self.entry_delay_days))).date()
        signal_ts = pd.Timestamp(candle) - pd.Timedelta(seconds=self._tf_seconds())
        out = []
        for p in self.nlsh_universe():
            if p == self.btc_pair or p in busy or self.nlsh_listing_day(p) != entry_day:
                continue
            if self._et_is_live():
                row = self.et_last_row(p)
                if row is not None and pd.Timestamp(row["date"]) == signal_ts and int(row.get("enter_short") or 0) == 1:
                    out.append(p)
                continue
            # 回测: 这个币这根K线还没轮到处理, 分析好的数据还停在前一天, 所以按 populate_indicators 的算法直接从K线算
            try:
                df = self.dp.historic_ohlcv(pair=p, timeframe=self.timeframe)
            except Exception:  # noqa: BLE001
                continue
            if df is None or df.empty:
                continue
            secs = to_epoch_seconds(df["date"])
            if int(secs[0]) < int(_day_ts(entry_day).timestamp()):
                continue  # 上线日文件旧了: populate_indicators 不让这个币交易
            j = int(np.searchsorted(secs, int(signal_ts.timestamp()), side="left"))
            w = int(self.volume_window_days)
            if j >= len(secs) or int(secs[j]) != int(signal_ts.timestamp()) or j + 1 < w:
                continue
            in_contracts, cs, currency = self._volume_units(p)
            qv = ohlcv_quote_volume(df.iloc[j + 1 - w : j + 1], volume_in_contracts=in_contracts, contract_size=cs,
                                    volume_currency=currency).to_numpy(dtype="float64")
            acc = qv[-1]
            for i in range(1, w):  # same summation order as populate_indicators
                acc = acc + qv[-1 - i]
            if acc / w >= float(self.min_avg_quote_volume):
                out.append(p)
        return out

    def nlsh_short_notional(self, current_time: datetime) -> tuple[float, int]:
        """所有未平新币空单按现价 (最后一根已收盘日线的收盘价) 计算的名义金额之和, 和笔数。"""
        total = 0.0
        shorts = self._open_shorts(current_time)
        for t, amount in shorts:
            price = self.nlsh_close_at(t.pair, current_time) or float(t.open_rate)
            total += amount * price
        return total, len(shorts)

    # ======================================================================
    # 风控 (在 easytrade_risk 的基础上)
    # ======================================================================

    def et_initial_stop(
        self, row: pd.Series | None, entry_rate: float, is_short: bool, leverage: float | None
    ) -> tuple[float | None, float | None]:
        """固定止损: 空单 = 入场价 x (1 + stop_pct) (1.4 倍), 不用 ATR、不截到 25%。多单 (只有对冲单) 没有止损。"""
        entry = positive_or_none(entry_rate)
        if entry is None or not is_short:
            return None, None
        return entry * (1.0 + float(self.stop_pct)), None

    def et_drawdown(self, current_time: datetime) -> float:
        # 记下 "现在" 给 et_portfolio_state 按日期取现价 (混入类的 et_portfolio_state 没有时间参数)
        self.__dict__["_nlsh_now"] = current_time
        return super().et_drawdown(current_time)

    def et_portfolio_state(self) -> tuple[float, float]:
        """(组合热度, 浮动盈亏): 热度不算对冲单 (预注册), 浮动盈亏算所有未平持仓 (回撤降仓看的是整个账户)。

        现价 = 最后一根已收盘日线的收盘价 (nlsh_close_at, 按混入类调用时的时间取; 混入类自己的版本在回测里
        对同一根K线排在后面处理的币会晚一天, 见 nlsh_close_at)。
        回测里在当前这根K线盘中才平掉的空单, 热度照算 (开盘这一刻它还在, 见 _book_trades), 浮动盈亏不算 (已经进了余额)。
        回测里数据已经结束的币 (中途下架) 的仓位不算热度, 浮动盈亏按 freqtrade 将来 force_exit 的价格算 (见 nlsh_data_ended)。
        """
        heat = 0.0
        upnl = 0.0
        buf = float(self.sizing_cost_buffer)
        now = self.__dict__.get("_nlsh_now")
        trades = self._book_trades(now) if now is not None else list(Trade.get_trades_proxy(is_open=True))
        for t in trades:
            amount = float(t.amount or 0.0)
            if t.is_open:
                for o in getattr(t, "open_orders", []) or []:
                    if getattr(o, "ft_order_side", None) == t.entry_side:
                        amount += float(o.safe_remaining or 0.0)
            if amount <= 0:
                continue
            ended = self.nlsh_data_ended(t.pair, now) if t.is_open else None
            if ended is not None:
                price = ended or float(t.open_rate)
                upnl += amount * ((t.open_rate - price) if t.is_short else (price - t.open_rate))
                continue
            price = (self.nlsh_close_at(t.pair, now) if now is not None else self.et_last_close(t.pair)) or float(t.open_rate)
            if t.is_open:
                upnl += amount * ((t.open_rate - price) if t.is_short else (price - t.open_rate))
            if self._is_hedge(t):
                continue
            stop = self.et_trade_stop(t)
            if stop is None:
                stop = float(t.open_rate) * (1.0 + float(self.stop_pct)) if t.is_short else price * (1.0 - self.et_stop_cap(t.leverage))
            dist = (stop - price) if t.is_short else (price - stop)
            heat += amount * (max(dist, 0.0) + price * buf)
        return heat, upnl

    def custom_stoploss(
        self,
        pair: str,
        trade: Trade,
        current_time: datetime,
        current_rate: float,
        current_profit: float,
        after_fill: bool,
        **kwargs: Any,
    ) -> float | None:
        if self._is_hedge(trade):
            return None  # 对冲单不设止损
        return super().custom_stoploss(
            pair=pair, trade=trade, current_time=current_time, current_rate=current_rate,
            current_profit=current_profit, after_fill=after_fill, **kwargs,
        )

    def _log_min_stake(self, pair: str, stake: float, min_stake: float | None, current_time: datetime) -> None:
        """仓位比交易所最小下单额小时记一行日志 (同 XSMomentum; 同一个币同一根K线只记一次)。"""
        try:
            ms = positive_or_none(min_stake)
            if not (stake > 0 and ms is not None and stake < ms):
                return
            seen = self.__dict__.setdefault("_nlsh_min_stake_seen", set())
            key = (pair, timeframe_to_prev_date(self.timeframe, current_time))
            if key in seen:
                return
            if len(seen) > 2000:
                seen.clear()
            seen.add(key)
            log = logger.warning if self._et_is_live() else logger.info
            if stake * FT_MIN_STAKE_ADJUST < ms:
                log("%s %s %s: 按规则算的保证金 %.2f USDT 比交易所最小下单额 %.2f USDT 小太多 (freqtrade 最多放大 30%%), "
                    "这一笔开不出来", MIN_STAKE_SKIP_TAG, self.et_name, pair, stake, ms)
            else:
                log("%s %s %s: 按规则算的保证金 %.2f USDT 比交易所最小下单额 %.2f USDT 小, freqtrade 会放大到 %.2f "
                    "(这一笔实际金额比设计的大, 最多 +30%%)", MIN_STAKE_BUMP_TAG, self.et_name, pair, stake, ms, ms)
        except Exception as exc:  # noqa: BLE001 — 只是记日志, 不能影响下单
            logger.debug("%s %s 检查最小下单额失败: %s", self.et_name, pair, exc)

    def custom_stake_amount(
        self,
        pair: str,
        current_time: datetime,
        current_rate: float,
        proposed_stake: float,
        min_stake: float | None,
        max_stake: float,
        leverage: float,
        entry_tag: str | None,
        side: str,
        **kwargs: Any,
    ) -> float:
        """新币空单: easytrade_risk 的按止损距离定仓 (0.5% 风险, 热度 6% 不含对冲, 回撤降仓)。
        对冲单: 保证金 = 未平空单名义金额之和 / 杠杆 (不降仓、不受热度和建议仓位限制, 只受可用资金限制)。"""
        if pair == self.btc_pair:
            if side != "long":
                return 0.0
            try:
                if self._analysis_mode():
                    return float(proposed_stake)  # 分析工具: 看不到其他币的持仓, 用建议仓位代替 (见文件开头)
                target, count = self.nlsh_short_notional(current_time)
                if target <= 0 or count == 0:
                    return 0.0
                lev = max(1.0, float(leverage or 1.0))
                stake = target / lev
                if stake > float(max_stake):
                    logger.warning("%s %s: 对冲需要保证金 %.2f USDT, 可用只有 %.2f USDT, 只能对冲一部分",
                                   self.et_name, pair, stake, float(max_stake))
                    stake = float(max_stake)
                self._log_min_stake(pair, stake, min_stake, current_time)
                (logger.info if self._et_is_live() else logger.debug)(
                    "%s %s: 开对冲单, %d 笔空单名义金额 %.2f USDT", self.et_name, pair, count, target)
                return max(stake, 0.0)
            except Exception as exc:  # noqa: BLE001
                logger.error("%s custom_stake_amount (对冲) 出错, 不开: %s", self.et_name, exc)
                return 0.0
        if side != "short":
            return 0.0
        self.__dict__["_nlsh_now"] = current_time
        stake = super().custom_stake_amount(
            pair=pair, current_time=current_time, current_rate=current_rate, proposed_stake=proposed_stake,
            min_stake=min_stake, max_stake=max_stake, leverage=leverage, entry_tag=entry_tag, side=side, **kwargs,
        )
        self._log_min_stake(pair, stake, min_stake, current_time)
        return stake

    def confirm_trade_entry(
        self,
        pair: str,
        order_type: str,
        amount: float,
        rate: float,
        time_in_force: str,
        current_time: datetime,
        entry_tag: str | None,
        side: str,
        **kwargs: Any,
    ) -> bool:
        """单向持仓: 永远不做空 BTC, 永远不做多别的币 (多单只能是 BTC 对冲单)。"""
        if pair == self.btc_pair:
            if side != "long":
                logger.error("%s: 拒绝做空 %s (它是对冲腿; 单向持仓模式下做空 BTC 会和对冲单冲突)", self.et_name, pair)
                return False
            return True
        if side != "short":
            logger.error("%s %s: 拒绝做多 (这个策略只做空新币, 多单只有 BTC 对冲单)", self.et_name, pair)
            return False
        return True

    def order_filled(self, pair: str, trade: Trade, order: Any, current_time: datetime, **kwargs: Any) -> None:
        try:
            if getattr(order, "ft_order_side", None) != trade.entry_side:
                return
            if self._is_hedge(trade):
                # 开仓 / 调整成交的这一天不再做 00:00 检查 (刚按目标定好)
                trade.set_custom_data(KEY_HEDGE_CHECKED, current_time.astimezone(timezone.utc).date().isoformat())
            elif trade.is_short and not trade.get_custom_data(KEY_LISTING_DAY):
                d = self.nlsh_listing_day(pair)
                if d is not None:
                    trade.set_custom_data(KEY_LISTING_DAY, d.isoformat())
        except Exception as exc:  # noqa: BLE001
            logger.debug("%s %s order_filled 记录失败: %s", self.et_name, pair, exc)

    # ======================================================================
    # 对冲: 每天 00:00 UTC 检查一次
    # ======================================================================

    def _hedge_check_due(self, trade: Trade, current_time: datetime) -> bool:
        now = current_time.astimezone(timezone.utc)
        if now.hour != int(self.hedge_check_hour_utc):
            return False
        today = now.date().isoformat()
        if trade.get_custom_data(KEY_HEDGE_CHECKED) == today:
            return False
        if trade.open_date_utc.astimezone(timezone.utc).date() == now.date():
            return False  # 今天刚开仓, 开仓时已按目标定好
        if self._et_is_live():
            # 实盘: 等 BTC 昨天的日线收盘到手 (空单的现价也是昨天的收盘价)
            row = self.et_last_row(self.btc_pair)
            last = None if row is None else pd.Timestamp(row["date"])
            if last is None or last.tz_convert("UTC").date() != (now - DAY).date():
                return False
        return True

    def adjust_trade_position(
        self,
        trade: Trade,
        current_time: datetime,
        current_rate: float,
        current_profit: float,
        min_stake: float | None,
        max_stake: float,
        current_entry_rate: float,
        current_exit_rate: float,
        current_entry_profit: float,
        current_exit_profit: float,
        **kwargs: Any,
    ) -> float | tuple[float | None, str | None] | None:
        """对冲单每天 00:00 UTC 检查一次: 名义金额和空单名义金额之和差超过 10% 才调到相等。空单不调整。"""
        if not self._is_hedge(trade) or self._analysis_mode():
            return None
        try:
            if not self._hedge_check_due(trade, current_time):
                return None
            trade.set_custom_data(KEY_HEDGE_CHECKED, current_time.astimezone(timezone.utc).date().isoformat())
            rate = positive_or_none(current_rate)
            amount = float(trade.amount or 0.0)
            if rate is None or amount <= 0:
                return None
            target, count = self.nlsh_short_notional(current_time)
            if target <= 0 or count == 0:
                return None  # 没有空单: custom_exit 平仓
            hedge = amount * rate
            drift = abs(hedge - target) / target
            if drift <= float(self.hedge_drift):
                return None
            lev = max(1.0, float(trade.leverage or 1.0))
            delta = target - hedge
            (logger.info if self._et_is_live() else logger.debug)(
                "%s %s: 对冲 %.2f USDT, %d 笔空单 %.2f USDT, 偏离 %.1f%% > %.0f%%, 调到相等",
                self.et_name, trade.pair, hedge, count, target, drift * 100, float(self.hedge_drift) * 100)
            if delta > 0:
                stake = delta / lev
                if stake > float(max_stake):
                    logger.warning("%s %s: 对冲加仓要 %.2f USDT 保证金, 可用只有 %.2f USDT, 只加一部分",
                                   self.et_name, trade.pair, stake, float(max_stake))
                    stake = float(max_stake)
                if stake <= 0:
                    return None
                self._log_min_stake(trade.pair, stake, min_stake, current_time)
                return stake, "hedge_up"
            # 减仓: freqtrade 减掉的数量 = |返回值| x 持仓数量 / 持仓保证金
            stake_now = float(trade.stake_amount or 0.0)
            if stake_now <= 0:
                return None
            reduce_amount = min(-delta / rate, amount)
            return -(reduce_amount * stake_now / amount), "hedge_down"
        except Exception as exc:  # noqa: BLE001
            logger.error("%s %s 对冲调整出错, 这次不调: %s", self.et_name, trade.pair, exc)
            return None

    # ======================================================================
    # 离场
    # ======================================================================

    def custom_exit(
        self,
        pair: str,
        trade: Trade,
        current_time: datetime,
        current_rate: float,
        current_profit: float,
        **kwargs: Any,
    ) -> str | None:
        """空单: 止损补救 / 开盘跳空越过止损 (easytrade_risk), 满 exit_age_days 天后的第一个开盘平仓。
        对冲单: 没有空单就平仓。"""
        if self._is_hedge(trade):
            if self._analysis_mode():
                return None  # 分析工具里按 exit_long 信号平仓
            if not self._open_shorts(current_time):
                pending = self.nlsh_pending_shorts(current_time)
                if pending:
                    (logger.info if self._et_is_live() else logger.debug)(
                        "%s %s: 现在没有空单, 但这个开盘要开 %s 的空单, 对冲单不平 (下一次 00:00 检查再调大小)",
                        self.et_name, pair, " ".join(pending))
                    return None
                return EXIT_HEDGE_FLAT
            return None
        reason = super().custom_exit(
            pair=pair, trade=trade, current_time=current_time, current_rate=current_rate,
            current_profit=current_profit, **kwargs,
        )
        if reason:
            return reason
        due = self._age_exit_time(trade)
        if due is not None and current_time >= due:
            return EXIT_AGE
        return None
