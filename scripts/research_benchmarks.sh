#!/usr/bin/env bash
# ------------------------------------------------------------------
# 基准策略研究：把别人公开的 freqtrade 策略放进和我们自己的策略完全相同的研究流程（scripts/research.sh）里跑，比一比。
# 说明（每个基准策略是什么、要多久、怎么和我们的策略比）：docs/benchmarks.md
#
# 先下载基准策略（只在你自己的 VPS 上；固定版本、核对 sha256）：bash scripts/fetch_benchmarks.sh
# 用法：
#   make research-benchmarks                         # = bash scripts/research_benchmarks.sh（在 tmux 里跑，要很久）
#   make research-benchmarks ARGS="--groups 1h"      # 只跑 1h 组（快一些）；--groups 5m 只跑 5 分钟组（含 NFI，很慢）
#   make research-benchmarks ARGS="--no-download"    # 不下载/更新数据，直接用已经下载的
#   make research-benchmarks ARGS="--nfi-pairs 30"   # 5 分钟组用研究交易对列表的前 30 个币
#   make research-benchmarks ARGS="--with-ours"      # 5 分钟组里也放上我们自己第 3 轮的默认策略，同一区间、同一批币对比
#
# 两组（各跑一次 scripts/research.sh，各出一张排名表）：
#   1h 组：freqtrade-strategies futures/ 目录里的 1h 策略
#            FAdxSmaStrategy FSampleStrategy FSupertrendStrategy VolatilitySystem
#          （FOttStrategy 也下载了，但在 freqtrade 2026.10 下用不了，默认不跑，原因见 docs/benchmarks.md）
#          区间、交易对（约 60 个）、手续费、通过标准都和 make research 一样（2021-01-01 起），可以直接和我们的研究表比
#   5m 组：FReinforcedStrategy TrendFollowingStrategy（freqtrade-strategies 的 5m 策略）
#          + NostalgiaForInfinityX8Capped（NFI X8，杠杆封顶 5 倍，user_data/strategies/benchmarks/BenchmarkCaps.py）
#          只用最近 --days 天（默认 730 ≈ 2 年：5 分钟K线数据量大），交易对 = 研究列表的前 N 个（BTC 一定在里面：NFI 要看 BTC）
#
# 参数（括号里是同名环境变量，命令行优先）：
#   --groups "1h 5m"    跑哪几组（RESEARCH_BENCH_GROUPS，默认 "1h 5m"）
#   --days N            5 分钟组用最近多少天（RESEARCH_BENCH_DAYS，默认 730，最少 200）
#   --nfi-pairs N|all|auto  5 分钟组用前 N 个币（RESEARCH_BENCH_NFI_PAIRS，默认 auto = 按可用内存定，5 ~ 20 个；
#                       NFI 每个币每天的 5 分钟K线回测时约占 1 MB 内存（估算），60 个币 2 年要 20 GB 以上）
#   --with-ours         5 分钟组也放上我们第 3 轮的默认策略（RESEARCH_BENCH_WITH_OURS=1）
#   其他参数原样交给 scripts/research.sh（例如 --no-download、--fee 0.0007、--max-dd 15、--folds 2、--jobs 1）。
#   --strategies / --start / --config / --hyperopt 由本脚本决定，不能自己写。
#   想换某一组的策略：RESEARCH_BENCH_1H_STRATEGIES="..." / RESEARCH_BENCH_5M_STRATEGIES="..."（类名，空格分开）
#
# 规则：只用策略代码里的默认参数（不调参）；回撤上限（账户含浮亏，默认 15%）、杠杆 ≤ 5x、手续费+滑点、
#       通过标准都和 make research 相同（MAX_DD_PCT / RESEARCH_MAX_DD / --max-dd 照样可以改）。
# 结果：每组一个 user_data/research/run_<时间>/（summary.txt 是排名表）；5 分钟组的交易对配置在 user_data/research/benchmarks/。
# 退出码：0 = 跑完（不管策略通没通过），或者还没下载基准策略（只打印提示）；
#         1 = 参数错误、基准策略文件不全或被改过；130 = 按了 Ctrl+C；其他 = scripts/research.sh 的退出码（2 = 有回测出错等）
# 兼容 macOS 自带的 bash 3.2。
# ------------------------------------------------------------------
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

# 两组的默认策略（类名）。文件和固定版本见 scripts/fetch_benchmarks.sh
# FOttStrategy 也下载了，但默认不跑：它 `from numpy.lib import math`（numpy 2 没有了，freqtrade 2026.10 下导入就报错），
# 而且在 pandas 3 下它的 Var 指标算不出来（链式赋值不再生效）、每根K线都整列重算（平方级，几年数据要跑一天以上）
G1H="${RESEARCH_BENCH_1H_STRATEGIES:-FAdxSmaStrategy FSampleStrategy FSupertrendStrategy VolatilitySystem}"
G5M="${RESEARCH_BENCH_5M_STRATEGIES:-FReinforcedStrategy TrendFollowingStrategy NostalgiaForInfinityX8Capped}"
# NFI 需要的周期：5m 主周期 + 15m 1h 4h 1d 参考周期（BTC 用 5m 和 4h，BTC 本来就在交易对里）
TF_5M="5m 15m 1h 4h 1d"

BENCH_GROUPS="${RESEARCH_BENCH_GROUPS:-1h 5m}"
DAYS="${RESEARCH_BENCH_DAYS:-730}"
NFI_PAIRS="${RESEARCH_BENCH_NFI_PAIRS:-auto}"
WITH_OURS="${RESEARCH_BENCH_WITH_OURS:-0}"
# 只给合成数据流程自检用（--synthetic 时才认）：0 = 不校验 vendor/ 里的文件（自检时那里放的是假的替身策略）
VERIFY="${RESEARCH_BENCH_VERIFY:-1}"
SYNTH=0
PASS=()

bad() { echo "参数错误：$1（看 bash scripts/research_benchmarks.sh --help）" >&2; exit 1; }
need_val() { [[ $# -ge 2 && -n "$2" ]] || bad "$1 后面要跟一个值"; }

while [[ $# -gt 0 ]]; do
    case "$1" in
        --groups)     need_val "$@"; BENCH_GROUPS="$2"; shift 2 ;;
        --days)       need_val "$@"; DAYS="$2"; shift 2 ;;
        --nfi-pairs)  need_val "$@"; NFI_PAIRS="$2"; shift 2 ;;
        --with-ours)  WITH_OURS=1; shift ;;
        --synthetic)  SYNTH=1; PASS+=("$1"); shift ;;
        --no-hyperopt) shift ;;
        --strategies|--start|--config|--hyperopt)
            bad "$1 由本脚本决定（每组的策略、区间、配置是固定的，只用默认参数），不能自己写" ;;
        -h|--help)    sed -n '2,/^# -----/p' "$0"; exit 0 ;;
        *)            PASS+=("$1"); shift ;;
    esac
done
case "$DAYS" in ''|*[!0-9]*) bad "--days 要是天数（收到：$DAYS）" ;; esac
[[ "$DAYS" -ge 200 ]] || bad "--days 至少 200 天（要切成训练段 + 样本外，收到：$DAYS）"
case "$NFI_PAIRS" in
    auto|all) ;;
    ''|*[!0-9]*) bad "--nfi-pairs 只能是正整数、all 或 auto（收到：$NFI_PAIRS）" ;;
    *) [[ "$NFI_PAIRS" -ge 1 ]] || bad "--nfi-pairs 至少 1 个（收到：$NFI_PAIRS）" ;;
esac
[[ "$WITH_OURS" == "0" || "$WITH_OURS" == "1" ]] || bad "RESEARCH_BENCH_WITH_OURS 只能是 0 或 1（收到：$WITH_OURS）"
RUN_1H=0
RUN_5M=0
for g in $BENCH_GROUPS; do
    case "$g" in
        1h) RUN_1H=1 ;;
        5m) RUN_5M=1 ;;
        *) bad "--groups 只能写 1h 和/或 5m（收到：$BENCH_GROUPS）" ;;
    esac
done
[[ $RUN_1H -eq 1 || $RUN_5M -eq 1 ]] || bad "--groups 不能为空"
for s in $G1H $G5M; do
    [[ "$s" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] || bad "策略类名不对：$s"
done

# ---------- 1. 基准策略文件在不在、是不是固定版本（不联网、不运行它们） ----------
if [[ "$SYNTH" == "1" && "$VERIFY" == "0" ]]; then
    echo "（合成数据流程自检：按 RESEARCH_BENCH_VERIFY=0 不校验 user_data/strategies/vendor/ 里的文件）"
else
    set +e
    CHECK_OUT="$(bash scripts/fetch_benchmarks.sh --check 2>&1)"
    CHECK_RC=$?
    set -e
    if [[ $CHECK_RC -eq 3 ]] && printf '%s\n' "$CHECK_OUT" | grep -q "检查结果：正常 0 个，缺少 [0-9]* 个，被改过 0 个"; then
        cat <<'MSG'
================ 基准策略研究 ================
还没有下载基准策略（user_data/strategies/vendor/ 里没有需要的文件），这次什么都没有跑。

基准策略是别人公开的 freqtrade 策略（freqtrade-strategies 的 futures/ 目录 + NostalgiaForInfinity X8），
用来和我们自己的策略在同一套研究流程里比较。它们只在你自己的 VPS 上下载、回测，步骤：
  1. bash scripts/fetch_benchmarks.sh      # 下载固定版本，逐个核对 sha256（几秒钟）
  2. make research-benchmarks              # 跑基准策略研究（建议在 tmux 里跑，要好几个小时）
说明（每个基准策略是什么、要多久、怎么和我们的策略比）：docs/benchmarks.md
MSG
        exit 0
    fi
    if [[ $CHECK_RC -ne 0 ]]; then
        echo "!! 基准策略文件不全或和固定版本对不上，不能用来比较（研究没有开始）：" >&2
        printf '%s\n' "$CHECK_OUT" | sed 's/^/   /' >&2
        echo "   重新下载固定版本（被改过的文件会先备份）：bash scripts/fetch_benchmarks.sh --force" >&2
        exit 1
    fi
fi

# ---------- 2. 运行环境（freqtrade 在哪、回撤上限等，和 make research 一样） ----------
# shellcheck source=_common.sh
source scripts/_common.sh

RESULTS_BASE="user_data/research"
if [[ "$SYNTH" == "1" ]]; then RESULTS_BASE="user_data/research/synthetic"; fi
SRC_CONFIG="${RESEARCH_CONFIG:-configs/config.okx-research.json}"
[[ -f "$SRC_CONFIG" ]] || bad "找不到研究配置 $SRC_CONFIG"

OURS=""
if [[ "$WITH_OURS" == "1" ]]; then
    OURS="$(sed -n 's/^DEFAULT_STRATEGIES="\(.*\)"$/\1/p' scripts/research.sh | head -n 1)"
    [[ -n "$OURS" ]] || bad "从 scripts/research.sh 读不到我们的默认策略列表（DEFAULT_STRATEGIES）"
fi

# 5 分钟组从哪天开始：今天（UTC）往前 DAYS 天；结束 = 数据的最后一天（research.sh 自动定）
days_ago() { date -u -d "-$1 days" +%Y%m%d 2> /dev/null || date -u -v-"$1"d +%Y%m%d; }
START_5M="$(days_ago "$DAYS")"
[[ "$START_5M" =~ ^[0-9]{8}$ ]] || bad "算不出开始日期（date 命令不认识）"

# 可用内存（MB），读不到输出空（和 scripts/research.sh 同样的读法）
mem_available_mb() {
    if [[ -r /proc/meminfo ]]; then
        awk '/^MemAvailable:/ {print int($2 / 1024); exit}' /proc/meminfo 2> /dev/null || true
    elif command -v vm_stat > /dev/null 2>&1; then
        vm_stat 2> /dev/null | awk '/page size of/ {for (i = 1; i <= NF; i++) if ($i ~ /^[0-9]+$/) ps = $i}
            /^Pages (free|inactive|speculative|purgeable)/ {gsub(/\./, "", $NF); s += $NF}
            END {if (ps > 0 && s > 0) print int(s * ps / 1048576)}' || true
    fi
}

# 5 分钟组用几个币：NFI 回测时把所有币的全部指标（约 200 多列）同时放在内存里，最长的一段（训练段，约 DAYS 的 2/3）
# 每个币每天约 1 MB（估算：288 根 × 约 230 列 × 8 字节，freqtrade 还要再存一份）
NFI_N=""
NFI_TXT=""
if [[ $RUN_5M -eq 1 ]]; then
    PER_PAIR_MB=$(( DAYS * 2 / 3 ))
    if [[ "$NFI_PAIRS" == "auto" ]]; then
        MEM_MB="$(mem_available_mb)"
        if [[ "$MEM_MB" =~ ^[0-9]+$ && "$MEM_MB" -gt 0 ]]; then
            NFI_N=$(( (MEM_MB - 1536) / PER_PAIR_MB ))
            [[ $NFI_N -le 20 ]] || NFI_N=20
            [[ $NFI_N -ge 5 ]] || NFI_N=5
            NFI_TXT="前 $NFI_N 个（自动：可用内存约 $(awk -v m="$MEM_MB" 'BEGIN {printf "%.1f", m / 1024}') GB，NFI 每个币约要 ${PER_PAIR_MB} MB，估算；最多 20 个）"
            if [[ $(( NFI_N * PER_PAIR_MB + 1536 )) -gt $MEM_MB ]]; then
                NFI_TXT="$NFI_TXT
          !! 可用内存可能连 $NFI_N 个币都不够，NFI 回测进程可能被系统杀掉（日志里 Killed / 退出码 137）；可以试 --days 365"
            fi
        else
            NFI_N=10
            NFI_TXT="前 10 个（读不到可用内存，保守取 10 个）"
        fi
    elif [[ "$NFI_PAIRS" == "all" ]]; then
        NFI_N="all"
        NFI_TXT="研究列表里的全部币（按设置；NFI 每个币约要 ${PER_PAIR_MB} MB 内存，估算）"
    else
        NFI_N="$NFI_PAIRS"
        NFI_TXT="前 $NFI_N 个（按设置；NFI 每个币约要 ${PER_PAIR_MB} MB 内存，估算）"
    fi
fi

# ---------- 3. 5 分钟组的交易对配置（研究配置 + 只留前 N 个币，BTC 一定在里面） ----------
BENCH_CFG_DIR="$RESULTS_BASE/benchmarks"
BENCH_CFG="$BENCH_CFG_DIR/config.bench-5m.json"
GEN_PY=""
IFS= read -r -d '' GEN_PY <<'PYEOF' || true
import json, os, sys
from freqtrade.configuration.load_config import load_from_files

src, out, n, days, start = sys.argv[1:6]
cfg = load_from_files([src])
stake = str(cfg.get("stake_currency") or "USDT")
mode = str(cfg.get("trading_mode") or "spot")
btc = f"BTC/{stake}:{stake}" if mode in ("futures", "margin") else f"BTC/{stake}"
wl = list((cfg.get("exchange") or {}).get("pair_whitelist") or [])
if not wl:
    sys.exit(f"错误：研究配置 {src} 里没有交易对")
if n == "all":
    picked = list(wl)
else:
    picked = wl[: int(n)]
if btc not in picked:
    # NFI 每个币都要看 BTC 的 5m / 4h；BTC 不在前 N 个里时换掉最后一个
    picked = [btc] + (picked[:-1] if n != "all" else picked)
out_dir = os.path.dirname(os.path.abspath(out))
os.makedirs(out_dir, exist_ok=True)
rel = os.path.relpath(os.path.abspath(src), out_dir)
lines = [
    "{",
    "    // 自动生成（scripts/research_benchmarks.sh），每次运行都会覆盖，不要手改。",
    f"    // 基准策略研究的 5 分钟组：{src} 的全部设置，交易对只留前 {len(picked)} 个（{n}；BTC 一定在里面，NFI 要看 BTC）。",
    f"    // 区间：最近 {days} 天（从 {start} 开始）。为什么只用这几个币：NFI 很吃内存，见 docs/benchmarks.md",
    f'    "add_config_files": [{json.dumps(rel)}],',
    '    "exchange": {',
    '        "pair_whitelist": [',
    ",\n".join(f"            {json.dumps(p)}" for p in picked),
    "        ]",
    "    }",
    "}",
]
with open(out, "w", encoding="utf-8") as fh:
    fh.write("\n".join(lines) + "\n")
print(f"{len(picked)}|{' '.join(p.split('/')[0] for p in picked)}")
PYEOF

# ---------- 运行一组 ----------
# 用法：run_group <组名> <research.sh 的参数...>；结果目录记在 LAST_DIR，退出码记在 LAST_RC
LAST_DIR=""
LAST_RC=0
run_group() {
    local label="$1" before_f
    shift
    mkdir -p "$RESULTS_BASE"
    # 跑之前已有哪些结果目录（跑完后多出来的那个就是这一组的）
    before_f="$(mktemp "${TMPDIR:-/tmp}/easytrade-bench.XXXXXX")"
    (ls -1d "$RESULTS_BASE"/run_* 2> /dev/null || true) > "$before_f"
    set +e
    "$@"
    LAST_RC=$?
    set -e
    LAST_DIR="$( (ls -1d "$RESULTS_BASE"/run_* 2> /dev/null || true) | grep -vxF -f "$before_f" | sort | tail -n 1 || true)"
    rm -f "$before_f"
    echo ""
    echo ">>> 基准策略研究 $label 组结束（退出码 $LAST_RC）：${LAST_DIR:-没有生成结果目录}"
    echo ""
}

echo "================ 基准策略研究（别人公开的策略，和我们的策略用同一套流程比较） ================"
if [[ "$SYNTH" == "1" ]]; then
    echo "!! 合成数据流程自检：数字没有意义 !!"
fi
echo "基准策略：user_data/strategies/vendor/（固定版本、sha256 校验通过；来源和许可证见 docs/benchmarks.md）"
echo "规则：    只用默认参数，不调参；回撤上限、杠杆 ≤ 5x、手续费+滑点、通过标准和 make research 相同"
echo "运行方式：$FT_RUNNER_RESOLVED"
if [[ $RUN_1H -eq 1 ]]; then
    echo "1h 组：   $G1H"
    echo "          区间和交易对同 make research（$SRC_CONFIG，默认 2021-01-01 起）；预计 1 ~ 3 小时（数据已经下载过的话）"
fi
if [[ $RUN_5M -eq 1 ]]; then
    echo "5m 组：   $G5M${OURS:+ + 我们的 $OURS}"
    echo "          最近 $DAYS 天（$START_5M 起），周期 $TF_5M；交易对：$NFI_TXT"
    echo "          第一次要下载这些币的 5m / 15m K线（每个币约 2 分钟）；NFI 回测很慢，这一组可能要 4 ~ 12 小时"
fi
echo ""

FINAL_RC=0
DIR_1H=""
DIR_5M=""
note_rc() {
    if [[ $LAST_RC -eq 130 ]]; then
        echo "按了 Ctrl+C，后面的组不跑了。已经跑完的部分在 ${LAST_DIR:-（没有结果目录）}"
        exit 130
    fi
    if [[ $LAST_RC -gt $FINAL_RC ]]; then FINAL_RC=$LAST_RC; fi
}

if [[ $RUN_1H -eq 1 ]]; then
    echo "################ 1h 组：freqtrade-strategies 的 1h 策略（$(date '+%Y-%m-%d %H:%M')） ################"
    run_group 1h env RESEARCH_BENCHMARKS=1 bash scripts/research.sh ${PASS[@]+"${PASS[@]}"} \
        --config "$SRC_CONFIG" --strategies "$G1H" --no-hyperopt
    DIR_1H="$LAST_DIR"
    note_rc
fi

if [[ $RUN_5M -eq 1 ]]; then
    echo "################ 5m 组：5 分钟策略 + NFI，最近 $DAYS 天（$(date '+%Y-%m-%d %H:%M')） ################"
    mkdir -p "$BENCH_CFG_DIR"
    # Docker 里 freqtrade 以 uid 1000 运行，要能写这个目录（同 research.sh 的结果目录）
    if [[ "$FT_RUNNER_RESOLVED" == "docker" ]]; then chmod a+rwx "$BENCH_CFG_DIR"; fi
    if ! GEN_OUT="$(py_ft -c "$GEN_PY" "$SRC_CONFIG" "$BENCH_CFG" "$NFI_N" "$DAYS" "$START_5M" 2>&1 | tail -n 1)" \
            || [[ ! "$GEN_OUT" =~ ^[0-9]+\| ]]; then
        echo "!! 生成 5 分钟组的交易对配置失败：$GEN_OUT" >&2
        exit 1
    fi
    echo "交易对（${GEN_OUT%%|*} 个）：${GEN_OUT#*|}    配置：$BENCH_CFG"
    run_group 5m env RESEARCH_BENCHMARKS=1 RESEARCH_TIMEFRAMES="$TF_5M" bash scripts/research.sh ${PASS[@]+"${PASS[@]}"} \
        --config "$BENCH_CFG" --strategies "$G5M${OURS:+ $OURS}" --start "$START_5M" --no-hyperopt
    DIR_5M="$LAST_DIR"
    note_rc
fi

echo "================ 基准策略研究完成 ================"
if [[ $RUN_1H -eq 1 ]]; then if [[ -n "$DIR_1H" ]]; then echo "1h 组排名表：$DIR_1H/summary.txt"; else echo "1h 组排名表：（没有结果）"; fi; fi
if [[ $RUN_5M -eq 1 ]]; then if [[ -n "$DIR_5M" ]]; then echo "5m 组排名表：$DIR_5M/summary.txt"; else echo "5m 组排名表：（没有结果）"; fi; fi
cat <<'MSG'
怎么看（详细见 docs/benchmarks.md「怎么和我们的策略比」）：
  * 判定和我们的策略完全一样：表里"结论"列 = 通过 才算过关（账户含浮亏回撤 ≤ 上限、样本外和走步都要赚钱、盈亏因子、交易数、杠杆 ≤ 5x）。
  * 1h 组的区间、交易对、手续费和 make research 一样，可以直接和我们的研究表逐行比。
  * 5m 组只有最近约 2 年、币也少：要和我们的策略比，用 --with-ours 让我们的策略在同一张表里一起跑。
  * NFI、VolatilitySystem 会加仓（DCA）/ 分批平仓：回撤检查按回测结果里的逐笔订单重建持仓（第 4 轮起，见 docs/benchmarks.md 第 7 节）；第 3 轮的报告还是旧算法，在原目录重新生成报告即可（不用重跑回测）。
  * 基准策略只用来对照，不代表可以实盘：NFI 为什么不适合直接实盘，见 docs/benchmarks.md。
MSG
exit "$FINAL_RC"
