#!/usr/bin/env bash
# ------------------------------------------------------------------
# NFI X8 逐年回测（第 4 轮）：固定版本的 NostalgiaForInfinity X8，按 1 倍 / 2 倍 / 5 倍封顶三个版本，
# 2021 年起每个日历年单独回测一次（每次从 1000 USDT 重新开始），看它在每一年（包括 2022 年熊市）的表现。
# 说明（为什么做、NFI 的杠杆怎么定的、怎么看结果）：docs/nfi-yearly.md
#
# !! 这只是合理性检查：NFI X8 是 2026-09-06 才建的、之后改了约 1200 次，每一年都可能是作者看过的数据。
#    真正的样本外检验是之后在币安的模拟盘（前向测试）。
#
# 只在你自己的 VPS（能连 OKX）上跑真实数据，先下载固定版本的 NFI：bash scripts/fetch_benchmarks.sh
# 用法（建议在 tmux 里跑：一次回测 = 一个版本 × 一年，NFI 每次可能要 1 ~ 4 小时，默认 18 次）：
#   make research-nfi-yearly                                   # = bash scripts/research_nfi_yearly.sh
#   make research-nfi-yearly ARGS="--classes Lev1"             # 只跑 1 倍版本（先看它，省三分之二的时间）
#   make research-nfi-yearly ARGS="--years '2022 2023'"        # 只跑这几年（也可以写 2021-2023）
#   make research-nfi-yearly ARGS="--pairs 10"                 # 用研究列表的前 10 个币（BTC 一定在里面）
#   make research-nfi-yearly ARGS="--no-download"              # 不下载/更新数据，直接用已经下载的
#   make research-nfi-yearly ARGS="--resume user_data/research/nfi_yearly/run_<时间>"
#                                                              # 接着跑上次没跑完 / 出错的那几次（设置沿用上次的）
#   make research-nfi-yearly ARGS="--synthetic"                # 合成数据 + 替身策略离线走一遍流程（不联网，几分钟；数字没有意义）
#
# 参数（括号里是同名环境变量，命令行优先）：
#   --classes "Lev1 Lev2 Capped"  跑哪几个版本（NFI_YEARLY_CLASSES；可以写简称或完整类名）：
#                       Lev1 = NostalgiaForInfinityX8Lev1（杠杆封顶 1 倍）  Lev2 = …Lev2（2 倍）
#                       Capped = NostalgiaForInfinityX8Capped（NFI 默认 3 倍，封顶 5 倍，就是第 3 轮测的那个）
#   --years "..."       跑哪几年（NFI_YEARLY_YEARS；默认 2021 到今年，今年 = 到数据的最后一天；合成数据默认 2024 到今年）
#   --pairs N|all|auto  用研究交易对列表的前 N 个币（NFI_YEARLY_PAIRS，默认 auto = 按可用内存定，5 ~ 20 个；
#                       NFI 每个币每天的 5m 数据回测时约占 1 MB 内存（估算），一年 20 个币约 7 ~ 8 GB）。--nfi-pairs 同义
#   --no-download       不下载/更新数据（NFI_YEARLY_DOWNLOAD=0）
#   --fee F             每边手续费+滑点（NFI_YEARLY_FEE，默认 0.0007 = 0.07%，和 make research 一样；none = 交易所费率）
#   --max-dd PCT        账户回撤上限 %（含浮亏；默认 MAX_DD_PCT，没设 = 15 = 项目硬规则，定义在 scripts/check_drawdown.py）
#   --resume DIR        接着跑 DIR 里没跑完 / 出错的那几次；不能再写别的参数（设置、交易对、区间都用 DIR 里记下的）
#   --synthetic         用合成数据（user_data/data/okx-synthetic）+ 结果目录里临时写的替身策略离线自检（只检查流程和杠杆封顶）
#
# 做的事：
#   1. 核对 user_data/strategies/vendor/ 里的 NFI 是固定版本（scripts/fetch_benchmarks.sh --check，不联网、不运行它）；
#      把 BenchmarkCaps.py 和 NFI 文件复制到这次的结果目录（strategies/），之后每次回测都用这份副本（中途改了 vendor/ 也不影响）
#   2. 交易对：研究列表（configs/pairs.okx-research.json）的前 N 个，BTC 一定在里面（NFI 要看 BTC）；每一年都用同一批币
#   3. 下载 OKX 的 5m + NFI 的参考周期 15m 1h 4h 1d（scripts/download_okx_data.sh：已有的只补新的，本地开始得太晚的自动 --prepend 补前面）：
#      5m / 15m / 1h 从第一年的前一个月开始（默认 2020-12-01）；4h、1d 再往前（NFI 在 OKX 上每个周期预热 480 根，1d 要 480 天）
#   4. 检查每个币每一年有没有数据（data_check.txt），定下每一年的区间：1 月 1 日 ~ 次年 1 月 1 日；今年到数据的最后一天
#   5. 每个版本 × 每一年单独回测一次（一次只跑一个，内存有上限）：研究配置 + 交易对 + configs/research-overrides/<版本>.json，
#      手续费+滑点、保护机制和 make research 一样；某一次出错就记下来，接着跑下一次
#   6. 中文报告（scripts/nfi_yearly_report.py）：每个版本每一年一行（收益、PF、交易数、胜率、账户回撤含浮亏/只看平仓/差距、
#      最差单笔浮亏、最长持仓、杠杆、缺数据的币），每个版本一个结论（"可以上模拟盘" / "不建议上模拟盘" + 原因）
#
# 结果：user_data/research/nfi_yearly/run_<时间>/（合成数据：user_data/research/nfi_yearly/synthetic/run_<时间>/）
#   summary.txt / summary.csv / summary.json   报告（最后也会打印出来）      data_check.txt   每个币每一年的数据情况
#   bt/<版本>__<年份>/   每次回测的 freqtrade 原始结果       logs/   每一步的完整日志       strategies/   这次用的策略代码副本
#   重新出报告（不重跑回测）：.venv/bin/python scripts/nfi_yearly_report.py report --run-dir <结果目录>
#
# 退出码：0 = 全部跑完（不管结论是什么）；1 = 参数错误、NFI 文件缺少或被改过、数据检查失败；
#         2 = 有回测出错（报告照样生成，出错的看 logs/）；3 = 报告没生成；130 = 按了 Ctrl+C（已用跑完的部分生成报告）
# 兼容 macOS 自带的 bash 3.2。
# ------------------------------------------------------------------
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

CLASSES="${NFI_YEARLY_CLASSES:-Lev1 Lev2 Capped}"
YEARS="${NFI_YEARLY_YEARS:-}"
PAIRS_ARG="${NFI_YEARLY_PAIRS:-auto}"
DOWNLOAD="${NFI_YEARLY_DOWNLOAD:-1}"
FEE="${NFI_YEARLY_FEE:-0.0007}"
MAX_DD_ARG=""
RESUME=""
SYNTH=0
N_OPTS=0
SRC_CONFIG="configs/config.okx-research.json"
SYNTH_DATADIR="user_data/data/okx-synthetic"
VENDOR_DIR="user_data/strategies/vendor"
NFI_FILE="NostalgiaForInfinityX8.py"
NFI_LICENSE="LICENSE-NostalgiaForInfinity.txt"

bad() { echo "参数错误：$1（看 bash scripts/research_nfi_yearly.sh --help）" >&2; exit 1; }
need_val() { [[ $# -ge 2 && -n "$2" ]] || bad "$1 后面要跟一个值"; }

while [[ $# -gt 0 ]]; do
    case "$1" in
        --classes)            need_val "$@"; CLASSES="$2"; N_OPTS=$((N_OPTS + 1)); shift 2 ;;
        --years)              need_val "$@"; YEARS="$2"; N_OPTS=$((N_OPTS + 1)); shift 2 ;;
        --pairs|--nfi-pairs)  need_val "$@"; PAIRS_ARG="$2"; N_OPTS=$((N_OPTS + 1)); shift 2 ;;
        --no-download)        DOWNLOAD=0; shift ;;
        --fee)                need_val "$@"; FEE="$2"; N_OPTS=$((N_OPTS + 1)); shift 2 ;;
        --max-dd)             need_val "$@"; MAX_DD_ARG="$2"; N_OPTS=$((N_OPTS + 1)); shift 2 ;;
        --resume)             need_val "$@"; RESUME="${2%/}"; shift 2 ;;
        --synthetic)          SYNTH=1; N_OPTS=$((N_OPTS + 1)); shift ;;
        -h|--help)            sed -n '2,/^# -----/p' "$0"; exit 0 ;;
        *)                    bad "不认识的参数 $1" ;;
    esac
done

# ---------- 参数检查 ----------
if [[ -n "$RESUME" ]]; then
    [[ $N_OPTS -eq 0 ]] || bad "--resume 时不能再写 --classes / --years / --pairs / --fee / --max-dd / --synthetic（沿用上次记下的设置）"
    [[ -f "$RESUME/plan.json" && -f "$RESUME/plan.sh" && -f "$RESUME/run.env" ]] \
        || bad "$RESUME 不是 NFI 逐年回测的结果目录（没有 plan.json / plan.sh / run.env）"
fi
[[ "$DOWNLOAD" == "0" || "$DOWNLOAD" == "1" ]] || bad "NFI_YEARLY_DOWNLOAD 只能是 0 或 1（收到：$DOWNLOAD）"
[[ "$FEE" == "none" || "$FEE" =~ ^0?\.[0-9]+$ || "$FEE" == "0" ]] || bad "--fee 要是小数，例如 0.0007（收到：$FEE）"
if [[ -n "$MAX_DD_ARG" ]]; then
    [[ "$MAX_DD_ARG" =~ ^[0-9]+([.][0-9]+)?$ ]] || bad "--max-dd 要是数字（单位 %，收到：$MAX_DD_ARG）"
    awk -v x="$MAX_DD_ARG" 'BEGIN { exit !(x > 0 && x <= 100) }' || bad "--max-dd 要在 0 ~ 100 之间（收到：$MAX_DD_ARG）"
fi
case "$PAIRS_ARG" in
    auto|all) ;;
    ''|*[!0-9]*) bad "--pairs 只能是正整数、all 或 auto（收到：$PAIRS_ARG）" ;;
    *) [[ "$PAIRS_ARG" -ge 1 ]] || bad "--pairs 至少 1 个（收到：$PAIRS_ARG）" ;;
esac
# 版本：简称 → 完整类名（只认 BenchmarkCaps.py 里的三个）
CLASSES_FULL=""
for c in $CLASSES; do
    case "$c" in
        Lev1|lev1|NostalgiaForInfinityX8Lev1)       full="NostalgiaForInfinityX8Lev1" ;;
        Lev2|lev2|NostalgiaForInfinityX8Lev2)       full="NostalgiaForInfinityX8Lev2" ;;
        Capped|capped|NostalgiaForInfinityX8Capped) full="NostalgiaForInfinityX8Capped" ;;
        *) bad "--classes 只能写 Lev1 / Lev2 / Capped（或完整类名），收到：$c" ;;
    esac
    case " $CLASSES_FULL " in *" $full "*) ;; *) CLASSES_FULL="${CLASSES_FULL:+$CLASSES_FULL }$full" ;; esac
done
[[ -n "$CLASSES_FULL" ]] || bad "--classes 不能为空"
YEARS="$(printf '%s' "$YEARS" | tr ',' ' ')"
for y in $YEARS; do
    [[ "$y" =~ ^[0-9]{4}(-[0-9]{4})?$ ]] || bad "--years 要写成 \"2021 2022\" 或 2021-2023（收到：$YEARS）"
done

# ---------- 1. NFI 文件在不在、是不是固定版本（不联网、不运行它） ----------
if [[ -n "$RESUME" ]]; then
    # shellcheck disable=SC1090,SC1091
    source "$RESUME/run.env"
fi
if [[ "$SYNTH" == "1" ]]; then
    echo "（合成数据流程自检：不用真的 NFI，结果目录里临时写一个替身策略；不检查 $VENDOR_DIR/）"
elif [[ -z "$RESUME" ]]; then
    # 只要 NFI 这一个文件是固定版本就行（freqtrade-strategies 的文件这里用不到，缺了也没关系）
    CHECK_OUT="$(bash scripts/fetch_benchmarks.sh --check 2>&1 || true)"
    NFI_LINE="$(printf '%s\n' "$CHECK_OUT" | grep -F "$VENDOR_DIR/$NFI_FILE" | head -n 1 || true)"
    case "$NFI_LINE" in *正常*) NFI_OK=1 ;; *) NFI_OK=0 ;; esac
    if [[ $NFI_OK -ne 1 ]]; then
        if [[ ! -f "$VENDOR_DIR/$NFI_FILE" ]]; then
            cat >&2 <<'MSG'
================ NFI X8 逐年回测 ================
还没有下载固定版本的 NFI（user_data/strategies/vendor/NostalgiaForInfinityX8.py 不存在），这次什么都没有跑。
在你自己的 VPS 上：
  1. bash scripts/fetch_benchmarks.sh      # 下载固定版本，逐个核对 sha256（几秒钟）
  2. make research-nfi-yearly              # 再运行本命令（建议在 tmux 里跑）
说明：docs/nfi-yearly.md
MSG
        else
            echo "!! $VENDOR_DIR/$NFI_FILE 和固定版本对不上（被改过？），不能用（逐年回测没有开始）：" >&2
            printf '%s\n' "$CHECK_OUT" | sed 's/^/   /' >&2
            echo "   重新下载固定版本（被改过的文件会先备份）：bash scripts/fetch_benchmarks.sh --force" >&2
        fi
        exit 1
    fi
fi

# ---------- 2. 运行环境（freqtrade 在哪、回撤上限等，和 make research 一样） ----------
# shellcheck source=_common.sh
source scripts/_common.sh
if [[ "$SYNTH" == "1" ]]; then
    # shellcheck disable=SC2034  # 被 _common.sh 里的 ft / py_ft 使用
    FT_RUNNER_RESOLVED="synthetic"
    DOWNLOAD=0
fi

sha256_of() {
    if command -v sha256sum > /dev/null 2>&1; then sha256sum "$1" | awk '{print $1}'
    elif command -v shasum > /dev/null 2>&1; then shasum -a 256 "$1" | awk '{print $1}'
    else openssl dgst -sha256 "$1" | awk '{print $NF}'; fi
}
# Docker 里 freqtrade 以 uid 1000 运行，要能往结果目录写文件（同 research.sh）
mkd() {
    mkdir -p "$@"
    if [[ "$FT_RUNNER_RESOLVED" == "docker" ]]; then chmod a+rwx "$@"; fi
}

if [[ -n "$RESUME" ]]; then
    RUN_DIR="$RESUME"
    RUN_ID="$(basename "$RUN_DIR")"
else
    RUN_ID="run_$(date +%Y%m%d_%H%M%S)"
    if [[ "$SYNTH" == "1" ]]; then RUN_DIR="user_data/research/nfi_yearly/synthetic/$RUN_ID"; else RUN_DIR="user_data/research/nfi_yearly/$RUN_ID"; fi
    [[ ! -e "$RUN_DIR" ]] || bad "结果目录 $RUN_DIR 已经存在（同一秒运行了两次？）"
fi
SDIR="$RUN_DIR/strategies"
mkd "$RUN_DIR" "$RUN_DIR/logs" "$RUN_DIR/bt" "$SDIR"
RUN_LOG="$RUN_DIR/research.log"
JOBS_TSV="$RUN_DIR/jobs.tsv"
touch "$JOBS_TSV"
START_TS=$(date +%s)

say() { printf '%s\n' "$*" | tee -a "$RUN_LOG"; }
elapsed() {
    local s=$(( $(date +%s) - START_TS ))
    printf '已用 %d:%02d:%02d' $((s / 3600)) $((s % 3600 / 60)) $((s % 60))
}
report() {
    py_ft scripts/nfi_yearly_report.py report --run-dir "$RUN_DIR" --stage "$1" 2>&1 | tee -a "$RUN_LOG"
    return "${PIPESTATUS[0]}"
}
# 中间报告：只写 summary.txt，屏幕上只显示刚跑完的那个版本那一段
report_section() {
    py_ft scripts/nfi_yearly_report.py report --run-dir "$RUN_DIR" --stage interim > "$RUN_DIR/logs/report_interim.log" 2>&1 \
        || { say "    （中间报告没生成，看 $RUN_DIR/logs/report_interim.log；不影响后面的回测）"; return 0; }
    awk -v head="---- $1（" '/^(---- |================)/ {show = 0} index($0, head) == 1 {show = 1} show' "$RUN_DIR/summary.txt" \
        | tee -a "$RUN_LOG"
}
# 中断（Ctrl+C）：先把输出换回屏幕（中断可能发生在带 > 日志 重定向的 ft 里面），用跑完的部分出报告
exec 3>&1 4>&2
on_interrupt() {
    trap - INT TERM
    exec 1>&3 2>&4
    echo
    say "!! 收到中断（Ctrl+C）。用已经跑完的部分生成报告 ..."
    if [[ -f "$RUN_DIR/plan.json" ]]; then report interrupted || true; fi
    say "结果目录：$RUN_DIR    接着跑：bash scripts/research_nfi_yearly.sh --resume $RUN_DIR"
    exit 130
}
trap on_interrupt INT TERM

# 可用内存（MB），读不到输出空（同 scripts/research_benchmarks.sh）
mem_available_mb() {
    if [[ -r /proc/meminfo ]]; then
        awk '/^MemAvailable:/ {print int($2 / 1024); exit}' /proc/meminfo 2> /dev/null || true
    elif command -v vm_stat > /dev/null 2>&1; then
        vm_stat 2> /dev/null | awk '/page size of/ {for (i = 1; i <= NF; i++) if ($i ~ /^[0-9]+$/) ps = $i}
            /^Pages (free|inactive|speculative|purgeable)/ {gsub(/\./, "", $NF); s += $NF}
            END {if (ps > 0 && s > 0) print int(s * ps / 1048576)}' || true
    fi
}

# ---------- 新的一次运行：策略副本、交易对、下载、数据检查 ----------
if [[ -z "$RESUME" ]]; then
    say "================ NFI X8 逐年回测（低杠杆版本，第 4 轮） ================"
    if [[ "$SYNTH" == "1" ]]; then
        say "!! 合成数据流程自检：随机生成的行情 + 替身策略（不是 NFI），结果只用来检查流程和杠杆封顶，数字没有意义 !!"
    fi
    say "!! 这只是合理性检查：NFI X8 是 2026-09-06 才建的、之后改了约 1200 次，每一年都可能是作者看过的数据；真正的检验是币安模拟盘。"
    say "运行方式：$FT_RUNNER_RESOLVED    结果目录：$RUN_DIR"

    # 策略代码副本：BenchmarkCaps.py（我们写的封顶子类）+ NFI 固定版本（或合成数据的替身）。
    # BenchmarkCaps.py 先在自己所在的目录找 NFI 文件，所以放在一起就能用；之后 vendor/ 改了也不影响这次运行
    cp user_data/strategies/benchmarks/BenchmarkCaps.py "$SDIR/"
    if [[ "$SYNTH" == "1" ]]; then
        cat > "$SDIR/$NFI_FILE" <<'PYEOF'
# ==========================================================================================
# !!! 合成数据流程自检用的【替身策略】—— 不是 NostalgiaForInfinity，也不是任何第三方代码 !!!
# 由 scripts/research_nfi_yearly.sh --synthetic 写在这次的结果目录里（只有 --synthetic 才会写），别的命令都不会用到它。
# 作用：开发机 / 没下载 NFI 的机器上，也能让 BenchmarkCaps.py 里的 NostalgiaForInfinityX8Lev1 / Lev2 / Capped
#       导入并回测，检查逐年回测的流程（数据 → 回测 → 报告）和杠杆封顶。交易逻辑是随便写的（均线交叉 + 亏了加仓），结果没有意义。
# 故意做成"会超过上限"：默认要 10 倍杠杆（futures_mode_leverage* = 10），做空另有一个 8 倍的覆盖值，
#       配置里的 nfi_parameters / 顶层同名键也能改（和 NFI 的 NFI_SAFE_PARAMETERS 一样）。封顶以后每笔交易的杠杆必须 ≤ 各版本的上限。
# 和 NFI 一样的地方（BenchmarkCaps.py 依赖的）：类名 NostalgiaForInfinityX8、futures_mode_leverage* 属性、leverage() 的选法、
#       target_profit_cache（有 path 属性的缓存对象，构造参数是文件路径）、合约模式在 __init__ 里打开 can_short、会加仓（DCA）。
# ==========================================================================================
from __future__ import annotations

import json
from pathlib import Path

from pandas import DataFrame

from freqtrade.strategy import IStrategy, merge_informative_pair


class Cache:
    """替身：和 NFI 的 Cache 一样，构造参数是文件路径、path 属性指向它（BenchmarkCaps.py 回测时会换成临时目录里的空文件）。"""

    def __init__(self, path):
        self.path = Path(path)
        self.data = {}
        try:
            self.data = json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError):
            pass

    def save(self):
        self.path.write_text(json.dumps(self.data), encoding="utf-8")


class NostalgiaForInfinityX8(IStrategy):
    INTERFACE_VERSION = 3
    timeframe = "5m"
    can_short = False
    stoploss = -0.30
    minimal_roi = {"0": 0.02, "720": 0.0}
    use_exit_signal = True
    exit_profit_only = False
    ignore_roi_if_entry_signal = True
    process_only_new_candles = True
    startup_candle_count = 60
    position_adjustment_enable = True
    max_entry_position_adjustment = 2

    futures_mode_leverage = 10.0
    futures_mode_leverage_rebuy_mode = 10.0
    futures_mode_leverage_grind_mode = 10.0
    futures_mode_leverage_long = None
    futures_mode_leverage_long_rebuy_mode = None
    futures_mode_leverage_long_grind_mode = None
    futures_mode_leverage_short = 8.0
    futures_mode_leverage_short_rebuy_mode = None
    futures_mode_leverage_short_grind_mode = None
    target_profit_cache = None

    def __init__(self, config: dict) -> None:
        super().__init__(config)
        names = [n for n in dir(type(self)) if n.startswith("futures_mode_leverage")]
        params = self.config.get("nfi_parameters")
        for src in (params if isinstance(params, dict) else {}, self.config):
            for name in names:
                if name in src:
                    setattr(self, name, src[name])
        if self.target_profit_cache is None:
            mode = getattr(self.config.get("runmode"), "value", "")
            self.target_profit_cache = Cache(
                Path(self.config["user_data_dir"]) / f"nfix8-profit_max-synthetic-stub-({mode}).json"
            )
        if self.config.get("trading_mode") in ("futures", "margin"):
            self.can_short = True

    def informative_pairs(self):
        pairs = self.dp.current_whitelist() if self.dp else []
        stake = self.config.get("stake_currency", "USDT")
        return [(p, "1h") for p in pairs] + [(f"BTC/{stake}:{stake}", "4h")]

    def leverage(self, pair, current_time, current_rate, proposed_leverage, max_leverage, entry_tag, side, **kwargs):
        override = self.futures_mode_leverage_long if side == "long" else self.futures_mode_leverage_short
        if override is not None:
            return override
        return self.futures_mode_leverage

    def custom_stake_amount(self, pair, current_time, current_rate, proposed_stake, min_stake, max_stake,
                            leverage, entry_tag, side, **kwargs):
        return proposed_stake * 0.5  # 留一半给加仓

    def adjust_trade_position(self, trade, current_time, current_rate, current_profit, min_stake, max_stake, **kwargs):
        if trade.has_open_orders or trade.nr_of_successful_entries > 2:
            return None
        if current_profit < -0.04 * trade.nr_of_successful_entries:
            stake = trade.stake_amount * 0.5
            if min_stake is not None and stake < min_stake:
                return None
            return min(stake, max_stake), "stub_add"
        return None

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["ema_fast"] = dataframe["close"].ewm(span=12, adjust=False).mean()
        dataframe["ema_slow"] = dataframe["close"].ewm(span=48, adjust=False).mean()
        dataframe["trend_1h"] = 1.0
        if self.dp:
            inf = self.dp.get_pair_dataframe(metadata["pair"], "1h")
            if inf is not None and not inf.empty:
                inf = inf[["date", "close"]].copy()
                inf["ema24"] = inf["close"].ewm(span=24, adjust=False).mean()
                dataframe = merge_informative_pair(dataframe, inf, self.timeframe, "1h", ffill=True)
                dataframe["trend_1h"] = (dataframe["close"] > dataframe["ema24_1h"]).astype(float)
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        up = (dataframe["ema_fast"] > dataframe["ema_slow"]) & (dataframe["ema_fast"].shift(1) <= dataframe["ema_slow"].shift(1))
        down = (dataframe["ema_fast"] < dataframe["ema_slow"]) & (dataframe["ema_fast"].shift(1) >= dataframe["ema_slow"].shift(1))
        dataframe.loc[up & (dataframe["trend_1h"] > 0), ["enter_long", "enter_tag"]] = (1, "stub_long")
        dataframe.loc[down & (dataframe["trend_1h"] < 1), ["enter_short", "enter_tag"]] = (1, "stub_short")
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        return dataframe
PYEOF
        say "策略代码：$SDIR（BenchmarkCaps.py + 合成数据的替身策略 $NFI_FILE —— 不是 NFI）"
    else
        cp "$VENDOR_DIR/$NFI_FILE" "$SDIR/"
        if [[ -f "$VENDOR_DIR/$NFI_LICENSE" ]]; then cp "$VENDOR_DIR/$NFI_LICENSE" "$SDIR/"; fi
        say "策略代码：$SDIR（BenchmarkCaps.py + 固定版本的 NFI X8，sha256 校验通过；GPL-3.0，不要把结果目录发给别人）"
    fi
    {
        echo "$(sha256_of "$SDIR/BenchmarkCaps.py")  BenchmarkCaps.py"
        echo "$(sha256_of "$SDIR/$NFI_FILE")  $NFI_FILE"
    } > "$RUN_DIR/strategies.sha256"

    # 这次的设置（--resume 时读回来）
    {
        printf 'SYNTH=%q\n' "$SYNTH"
        printf 'FEE=%q\n' "$FEE"
        printf 'RUN_FT_RUNNER=%q\n' "$FT_RUNNER_RESOLVED"
    } > "$RUN_DIR/run.env"

    # 交易对：研究列表前 N 个（BTC 一定在里面）
    NFI_N="$PAIRS_ARG"
    PAIRS_TXT=""
    if [[ "$PAIRS_ARG" == "auto" ]]; then
        if [[ "$SYNTH" == "1" ]]; then
            NFI_N=4
            PAIRS_TXT="合成数据里有数据的前 4 个（自检用，少一点跑得快）"
        else
            # NFI 回测时把所有币的全部指标（约 200 多列）同时放在内存里：每个币每天约 1 MB（估算，同 research_benchmarks.sh），
            # 一次只回测一年（+ 预热），每个币约 380 MB
            PER_PAIR_MB=380
            MEM_MB="$(mem_available_mb)"
            if [[ "$MEM_MB" =~ ^[0-9]+$ && "$MEM_MB" -gt 0 ]]; then
                NFI_N=$(( (MEM_MB - 1536) / PER_PAIR_MB ))
                [[ $NFI_N -le 20 ]] || NFI_N=20
                [[ $NFI_N -ge 5 ]] || NFI_N=5
                PAIRS_TXT="前 $NFI_N 个（自动：可用内存约 $(awk -v m="$MEM_MB" 'BEGIN {printf "%.1f", m / 1024}') GB，每个币一年约 ${PER_PAIR_MB} MB，估算；最多 20 个 = 第 3 轮的那 20 个）"
                if [[ $(( NFI_N * PER_PAIR_MB + 1536 )) -gt $MEM_MB ]]; then
                    PAIRS_TXT="$PAIRS_TXT
          !! 可用内存可能连 $NFI_N 个币都不够，回测进程可能被系统杀掉（日志里 Killed / 退出码 137）"
                fi
            else
                NFI_N=10
                PAIRS_TXT="前 10 个（读不到可用内存，保守取 10 个）"
            fi
        fi
    elif [[ "$PAIRS_ARG" == "all" ]]; then
        PAIRS_TXT="研究列表里的全部币（按设置；NFI 每个币一年约要 380 MB 内存，估算）"
    else
        PAIRS_TXT="前 $PAIRS_ARG 个（按设置）"
    fi
    PAIRS_ARGS=(scripts/nfi_yearly_report.py pairs --config "$SRC_CONFIG" --out "$RUN_DIR/pairs.json" --n "$NFI_N" --years "$YEARS")
    if [[ "$SYNTH" == "1" ]]; then
        PAIRS_ARGS+=(--first-year 2024 --datadir "$SYNTH_DATADIR" --require-data)
        if [[ ! -f "$SYNTH_DATADIR/SYNTHETIC_DATA_README.txt" ]]; then
            say "还没有合成数据，先生成（scripts/make_synthetic_data.py）..."
            py_ft scripts/make_synthetic_data.py 2>&1 | tee -a "$RUN_LOG" || { say "生成合成数据失败"; exit 1; }
        fi
    fi
    if ! PAIRS_OUT="$(py_ft "${PAIRS_ARGS[@]}" 2> "$RUN_DIR/logs/pairs.err")"; then
        say "!! 选交易对失败："
        sed 's/^/   /' "$RUN_DIR/logs/pairs.err" | tail -n 20 | tee -a "$RUN_LOG"
        exit 1
    fi
    # 只接受 NY_xxx=... 这样的行（防止别的输出混进来被当成命令执行）
    eval "$(printf '%s\n' "$PAIRS_OUT" | grep -E '^NY_[A-Z0-9_]+=' || true)"
    [[ -n "${NY_PAIRS:-}" && -n "${NY_YEARS:-}" ]] || { say "!! 选交易对失败（没有输出）"; exit 1; }
    say "交易对（${NY_PAIRS_N} 个，每一年都用这一批）：${NY_PAIRS_COINS}"
    say "          $PAIRS_TXT"
    say "版本：    $CLASSES_FULL"
    say "年份：    $NY_YEARS（每个版本每一年单独回测一次，每次从 1000 USDT 开始）"
    N_RUNS=$(( $(echo $CLASSES_FULL | wc -w) * $(echo $NY_YEARS | wc -w) ))
    say "手续费+滑点：$FEE/边    回撤上限：${MAX_DD_ARG:-$MAX_DD_PCT}%（账户权益含浮亏；项目硬规则 ${EASYTRADE_DEFAULT_MAX_DD_PCT}%）"
    if [[ "$SYNTH" != "1" ]]; then
        say "预计：一共 $N_RUNS 次回测，NFI 每次（20 个币一年）大约 1 ~ 4 小时（估算，看 VPS），全部跑完可能要 1 ~ 3 天；"
        say "      先跑完的是 Lev1 的各年，跑完一个版本就出一次中间报告。断了可以 --resume 接着跑。"
    fi
    say ""

    # ---------- 下载 ----------
    if [[ "$DOWNLOAD" == "1" ]]; then
        say "=== 1/4 下载/更新 OKX 数据（$(elapsed)；完整输出：$RUN_DIR/logs/download.log）"
        say "    已有的只补新的；本地数据开始得比需要的晚的币自动 --prepend 补前面那段。第一次下载 20 个币的 5m（从 2020-12 起）要好几个小时。"
        DL_RC_ALL=0
        IFS='|' read -r -a DL_GROUPS <<< "$NY_DL_GROUPS"
        for g in "${DL_GROUPS[@]}"; do
            g_start="${g%%:*}"
            g_tfs="${g#*:}"
            say "    周期 $g_tfs：从 $g_start 开始"
            # 屏幕上每个币/周期只显示一行进度，加上下载脚本自己的提示行（同 research.sh）；完整输出在 logs/download.log
            set +e
            bash scripts/download_okx_data.sh --config "$SRC_CONFIG" --pairs "$NY_PAIRS" --timeframes "$g_tfs" \
                    --timerange "${g_start}-" 2>&1 | tee -a "$RUN_DIR/logs/download.log" \
                | awk -F'"' '/Download history data for/ {
                                 split($3, a, ","); sub(/ and store.*/, "", a[3])
                                 printf "      下载 %s%s%s\n", $2, a[2], a[3]; fflush() }
                             /^==> / { print "      " $0; fflush() }
                             /^  · / { print "    " $0; fflush() }
                             / ERROR / { print "      " $0; fflush() }'
            DL_RC=${PIPESTATUS[0]}
            set -e
            if [[ $DL_RC -ne 0 ]]; then
                DL_RC_ALL=$DL_RC
                say "    !! 这一组下载出错（退出码 $DL_RC，看 logs/download.log），继续用已经有的数据"
            fi
        done
        if [[ $DL_RC_ALL -eq 0 ]]; then say "    下载完成"; fi
    elif [[ "$SYNTH" == "1" ]]; then
        say "=== 1/4 合成数据模式，不下载（用 $SYNTH_DATADIR）"
    else
        say "=== 1/4 不下载数据（--no-download），直接用已经下载的"
    fi
    say ""

    # ---------- 数据检查 + 每一年的区间 ----------
    say "=== 2/4 检查数据、定下每一年的区间（$(elapsed)）"
    PLAN_ARGS=(scripts/nfi_yearly_report.py plan --run-dir "$RUN_DIR" --config "$SRC_CONFIG" --years "$NY_YEARS"
               --classes "$CLASSES_FULL" --fee "$FEE" --runner "$FT_RUNNER_RESOLVED"
               --strategy-files "$(tr '\n' ';' < "$RUN_DIR/strategies.sha256")")
    if [[ -n "$MAX_DD_ARG" ]]; then PLAN_ARGS+=(--max-dd "$MAX_DD_ARG"); fi
    if [[ "$SYNTH" == "1" ]]; then PLAN_ARGS+=(--datadir "$SYNTH_DATADIR" --synthetic); fi
    if ! py_ft "${PLAN_ARGS[@]}" 2>&1 | sed 's/^/    /' | tee -a "$RUN_LOG"; then
        say "数据检查失败，没法继续（上面有原因）。"
        exit 1
    fi
    [[ -f "$RUN_DIR/plan.sh" ]] || { say "数据检查没有生成 plan.sh，没法继续。"; exit 1; }
    say ""
else
    say ""
    say "================ NFI X8 逐年回测：接着跑 $RUN_DIR（$(date '+%Y-%m-%d %H:%M')） ================"
    # 策略副本不能被改过（这次运行的所有回测必须是同一份代码）
    while read -r want name; do
        [[ -n "$name" ]] || continue
        got="$(sha256_of "$SDIR/$name" 2> /dev/null || echo missing)"
        if [[ "$got" != "$want" ]]; then
            say "!! $SDIR/$name 和这次运行开始时不一样（sha256 对不上），不能接着跑。重新开一次：bash scripts/research_nfi_yearly.sh"
            exit 1
        fi
    done < "$RUN_DIR/strategies.sha256"
    if [[ "$SYNTH" == "1" ]]; then FT_RUNNER_RESOLVED="synthetic"; fi
    say "设置沿用上次的（plan.json）；已经跑成功的跳过，没跑的和出错的重跑。运行方式：$FT_RUNNER_RESOLVED"
    if [[ -n "${RUN_FT_RUNNER:-}" && "$RUN_FT_RUNNER" != "$FT_RUNNER_RESOLVED" ]]; then
        say "提示：上次的运行方式是 $RUN_FT_RUNNER，这次是 $FT_RUNNER_RESOLVED（freqtrade 版本不同的话，前后几年的结果可能不完全可比）"
    fi
fi

# shellcheck disable=SC1090,SC1091
source "$RUN_DIR/plan.sh"
if [[ -n "${NY_SKIP_YEARS:-}" ]]; then
    say "跳过的年份（没有数据，见 data_check.txt）：$NY_SKIP_YEARS"
fi
[[ -n "${NY_RUN_YEARS:-}" ]] || { say "!! 没有一年有数据可以回测（见 $RUN_DIR/data_check.txt）"; exit 1; }

# ---------- 3. 回测：每个版本 × 每一年，一次一个 ----------
FT_COMMON=(--config "$SRC_CONFIG" --config "$RUN_DIR/pairs.json")
if [[ "$SYNTH" == "1" ]]; then FT_COMMON+=(--datadir "$NY_PLAN_DATADIR"); fi
already_ok() {
    # jobs.tsv 里这个（版本, 年份）最后一行是 ok
    awk -F'\t' -v c="$1" -v y="$2" '$1 == c && $2 == y { s = $4 } END { exit !(s == "ok") }' "$JOBS_TSV"
}
bt_brief() {
    local profit trades
    profit="$(awk -F'│' '/Total profit %/ {gsub(/ /, "", $3); print $3; exit}' "$1" 2> /dev/null || true)"
    trades="$(awk -F'│' '/Total\/Daily Avg Trades/ {gsub(/ /, "", $3); split($3, a, "/"); print a[1]; exit}' "$1" 2> /dev/null || true)"
    if [[ -n "$profit" || -n "$trades" ]]; then echo "总收益 ${profit:-?}，交易 ${trades:-?} 笔"
    elif grep -q "No trades made" "$1" 2> /dev/null; then echo "一笔交易都没有"
    else echo "完成"; fi
}
show_error() {
    local lines
    lines="$(grep -E " ERROR | CRITICAL |Error:|Exception|Killed" "$1" 2> /dev/null | tail -n 5 || true)"
    if [[ -z "$lines" ]]; then lines="$(tail -n 8 "$1" 2> /dev/null || true)"; fi
    printf '%s\n' "$lines" | cut -c1-300 | sed 's/^/        /' | tee -a "$RUN_LOG"
}

N_TOTAL=$(( $(echo $NY_CLASSES | wc -w) * $(echo $NY_RUN_YEARS | wc -w) ))
STEP=0
FAILED=0
say "=== 3/4 回测：$(echo $NY_CLASSES | wc -w | tr -d ' ') 个版本 × $(echo $NY_RUN_YEARS | wc -w | tr -d ' ') 年 = $N_TOTAL 次（$(elapsed)）"
for CLS in $NY_CLASSES; do
    say "--- $CLS"
    SCFG="configs/research-overrides/$CLS.json"
    [[ -f "$SCFG" ]] || { say "!! 找不到 $SCFG，没法回测 $CLS"; exit 1; }
    for Y in $NY_RUN_YEARS; do
        STEP=$((STEP + 1))
        trv="NY_TR_$Y"
        TR="${!trv}"
        LABEL="${CLS}__${Y}"
        OUT="$RUN_DIR/bt/$LABEL"
        LOG="$RUN_DIR/logs/$LABEL.log"
        if already_ok "$CLS" "$Y"; then
            say "  [$STEP/$N_TOTAL] $CLS · $Y 年：上次已经跑完，跳过"
            continue
        fi
        rm -rf "$OUT"
        mkd "$OUT"
        ARGS=(backtesting "${FT_COMMON[@]}" --strategy "$CLS" --strategy-path "$SDIR" --timerange "$TR"
              --export trades --backtest-directory "$OUT" --cache none --notes "nfi-yearly=$RUN_ID $Y"
              --enable-protections --config "$SCFG")
        if [[ "$NY_PLAN_FEE" != "none" ]]; then ARGS+=(--fee "$NY_PLAN_FEE"); fi
        say "  [$STEP/$N_TOTAL] 回测 $CLS · $Y 年（$TR，$(date '+%m-%d %H:%M')，$(elapsed)）"
        RC=0
        ft "${ARGS[@]}" > "$LOG" 2>&1 || RC=$?
        if [[ $RC -eq 0 ]] && ! ls "$OUT"/backtest-result-*.zip > /dev/null 2>&1; then
            # freqtrade 遇到配置错误（ConfigurationError）时退出码也是 0，只能看有没有生成结果文件
            STATUS="fail"
            FAILED=$((FAILED + 1))
            say "        !! freqtrade 没有生成回测结果（退出码 0，多半是配置错误；完整日志：$LOG），接着跑下一次："
            show_error "$LOG"
            RC=-1
        elif [[ $RC -eq 0 ]]; then
            STATUS="ok"
            say "        $(bt_brief "$LOG")"
        else
            STATUS="fail"
            FAILED=$((FAILED + 1))
            say "        !! 回测失败（退出码 $RC，完整日志：$LOG），接着跑下一次："
            show_error "$LOG"
            if [[ $RC -eq 137 ]] || grep -q "Killed" "$LOG" 2> /dev/null; then
                say "        （退出码 137 / Killed 多半是内存不够：换少一点的币重新跑这一年，例如 --pairs 8 --years $Y --classes ${CLS#NostalgiaForInfinityX8}）"
            fi
        fi
        printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\n' "$CLS" "$Y" "$TR" "$STATUS" "$OUT" "$LOG" "$RC" >> "$JOBS_TSV"
    done
    if [[ "$CLS" != "$(echo $NY_CLASSES | awk '{print $NF}')" ]]; then
        say ""
        say "$CLS 的各年跑完了，中间报告（最终结论看最后那张表）："
        report_section "$CLS"
        say ""
    fi
done
say ""

# ---------- 4. 报告 ----------
say "=== 4/4 报告（$(elapsed)）"
say ""
REPORT_RC=0
report final || REPORT_RC=$?
say ""
if [[ $FAILED -gt 0 ]]; then
    say "有 $FAILED 次回测出错（看 $RUN_DIR/logs/），其他的照常完成。修好以后可以只重跑出错的：bash scripts/research_nfi_yearly.sh --resume $RUN_DIR"
fi
say "全部结果：$RUN_DIR    （$(elapsed)）"
if [[ "$SYNTH" == "1" ]]; then
    say "（合成数据 + 替身策略：只看流程有没有报错、杠杆有没有超过封顶，表里的数字没有意义）"
fi
if [[ $REPORT_RC -ne 0 ]]; then exit 3; fi
if [[ $FAILED -gt 0 ]]; then exit 2; fi
exit 0
