#!/usr/bin/env bash
# ------------------------------------------------------------------
# 批量回测：策略 × 周期，结果统一存到一个批次目录，最后自动做账户回撤检查（上限默认 15%，见 --max-dd）
#
# 用法：
#   bash scripts/backtest_all.sh                                   # 回测 user_data/strategies 里全部策略
#   bash scripts/backtest_all.sh --strategies "StratA StratB"      # 只测指定策略
#   bash scripts/backtest_all.sh --timeframes "15m 1h 4h"          # 强制用这些周期各测一遍
#   bash scripts/backtest_all.sh --timerange 20240101-20250101     # 指定回测区间
#   bash scripts/backtest_all.sh --detail 1m                       # 用 1m 数据模拟K线内部走势（更准、更慢）
#   bash scripts/backtest_all.sh --max-dd 12                       # 回撤上限（%），默认 15 = 项目硬规则
#                                                                  # （也可以用环境变量：MAX_DD_PCT=12 make backtest）
#   bash scripts/backtest_all.sh --config configs/config.binance-futures.json   # 以后用币安数据复核
#   bash scripts/backtest_all.sh --config configs/config.synthetic-test.json    # 合成数据流程自检（不联网）
#   其他参数原样传给 freqtrade backtesting，例如 --fee 0.0005、--max-open-trades 3
#
# 说明：
#   * 不加 --timeframes 时，每个策略用它自己代码里写的 timeframe（推荐）。
#     加了 --timeframes 会覆盖策略的周期 —— 只对"设计上支持多周期"的策略有意义。
#   * 默认带 --enable-protections（让策略里写的保护机制，比如连亏暂停、回撤暂停，在回测里也生效）。
#     不想要就加 --no-protections。
#   * 默认 --cache none，保证每次都是重新计算。
#   * 结果目录：user_data/backtest_results/run_<时间>/
#       backtest-result-*.zip   freqtrade 原始结果（回撤检查读这里）
#       logs/<策略>__<周期>.log 每次回测的完整输出
#       summary.txt / summary.csv  回撤检查汇总表
#   * FreqUI 的 Backtest 历史列表只认 user_data/backtest_results/ 这一层的文件（不看子目录），
#     所以每个 backtest-result-*.zip（和同名 .meta.json）会再复制一份到 user_data/backtest_results/，
#     FreqUI 里才能看到。
#   * 用合成数据配置（文件名含 synthetic）时：自动改成离线运行（scripts/ft_offline.py），
#     结果放到 user_data/backtest_results/synthetic/run_<时间>/，不和真实回测混在一起，
#     也不复制给 FreqUI（合成数据的数字没有意义）。
# ------------------------------------------------------------------
source "$(dirname "$0")/_common.sh"

CONFIG="configs/config.okx-backtest.json"
STRATEGIES=""
TIMEFRAMES=""
TIMERANGE=""
DETAIL=""
# 默认 = 环境变量 MAX_DD_PCT，没设时 15（项目硬规则，定义在 scripts/_common.sh / scripts/check_drawdown.py）
MAX_DD="$MAX_DD_PCT"
PROTECTIONS="yes"
EXTRA=()

while [[ $# -gt 0 ]]; do
    case "$1" in
        --config)          CONFIG="$2"; shift 2 ;;
        --strategies)      STRATEGIES="$2"; shift 2 ;;
        --timeframes)      TIMEFRAMES="$2"; shift 2 ;;
        --timerange)       TIMERANGE="$2"; shift 2 ;;
        --detail)          DETAIL="$2"; shift 2 ;;
        --max-dd)          MAX_DD="$2"; shift 2 ;;
        --no-protections)  PROTECTIONS="no"; shift ;;
        -h|--help)         sed -n '2,/^# -----/p' "$0"; exit 0 ;;
        *)                 EXTRA+=("$1"); shift ;;
    esac
done
[[ "$MAX_DD" =~ ^[0-9]+([.][0-9]+)?$ ]] || { echo "参数错误：--max-dd / MAX_DD_PCT 要是数字（单位 %，例如 15；收到：$MAX_DD）" >&2; exit 1; }

RESULTS_BASE="user_data/backtest_results"
# FreqUI 只列这个目录本层的回测结果（freqtrade api_backtest_history 不看子目录）
UI_RESULTS_DIR="user_data/backtest_results"
SYNTHETIC="no"
CHECK_DATADIR=""
case "$CONFIG" in
    *synthetic*)
        # 合成测试数据：只能离线跑，结果单独存放，回撤检查读合成数据目录的K线
        FT_RUNNER_RESOLVED="synthetic"
        SYNTHETIC="yes"
        RESULTS_BASE="user_data/backtest_results/synthetic"
        CHECK_DATADIR="user_data/data/okx-synthetic"
        echo "!! 使用合成测试数据（不是真实行情），结果只用来检查流程，收益/回撤数字没有意义 !!"
        ;;
esac

# 1) 找策略
if [[ -z "$STRATEGIES" ]]; then
    echo "未指定 --strategies，自动列出 user_data/strategies 里的全部策略 ..."
    STRATEGIES="$(ft list-strategies --config "$CONFIG" -1 2>/dev/null \
        | tr -d '\r' | grep -E '^[A-Za-z_][A-Za-z0-9_]*$' || true)"
fi
if [[ -z "$STRATEGIES" ]]; then
    echo "错误：没有找到任何策略。请把策略 .py 文件放到 user_data/strategies/，或用 --strategies 指定。" >&2
    exit 1
fi

RUN_ID="run_$(date +%Y%m%d_%H%M%S)"
RUN_DIR="$RESULTS_BASE/$RUN_ID"
mkdir -p "$RUN_DIR/logs"
# Docker 里 freqtrade 以 uid 1000 运行，要能往这个目录写结果
if [[ "$FT_RUNNER_RESOLVED" == "docker" ]]; then chmod a+rwx "$RUN_DIR"; fi

echo "运行方式：$FT_RUNNER_RESOLVED"
echo "配置：    $CONFIG"
echo "策略：    $(echo $STRATEGIES | tr '\n' ' ')"
echo "周期：    ${TIMEFRAMES:-（各策略自己的周期）}"
echo "区间：    ${TIMERANGE:-（全部已下载数据）}"
echo "结果目录：$RUN_DIR"
echo

FAILED=()
TOTAL=0
TF_LIST="${TIMEFRAMES:-default}"

for STRAT in $STRATEGIES; do
    for TF in $TF_LIST; do
        TOTAL=$((TOTAL + 1))
        LABEL="${STRAT}__${TF}"
        LOG="$RUN_DIR/logs/${LABEL}.log"

        ARGS=(backtesting --config "$CONFIG" --strategy "$STRAT"
              --export trades --backtest-directory "$RUN_DIR"
              --cache none --breakdown month
              --notes "batch=$RUN_ID tf=$TF")
        if [[ "$TF" != "default" ]]; then ARGS+=(--timeframe "$TF"); fi
        if [[ -n "$TIMERANGE" ]]; then ARGS+=(--timerange "$TIMERANGE"); fi
        if [[ -n "$DETAIL" ]]; then ARGS+=(--timeframe-detail "$DETAIL"); fi
        if [[ "$PROTECTIONS" == "yes" ]]; then ARGS+=(--enable-protections); fi
        if [[ ${#EXTRA[@]} -gt 0 ]]; then ARGS+=("${EXTRA[@]}"); fi

        echo "=== [$TOTAL] 回测 $STRAT（周期：$TF）→ $LOG"
        if ft "${ARGS[@]}" > "$LOG" 2>&1; then
            # 打印汇总那几行，完整输出在日志里
            grep -E "Total profit %|Max % of account underwater|Absolute drawdown|Total/Daily Avg Trades" "$LOG" | sed 's/^/    /' || true
        else
            echo "    !! 回测失败，最后 15 行日志："
            tail -n 15 "$LOG" | sed 's/^/    /'
            FAILED+=("$LABEL")
        fi
    done
done

# 复制一份给 FreqUI（只复制本批次的结果；合成数据不复制）
UI_COPIED=0
if [[ "$SYNTHETIC" == "no" ]]; then
    for f in "$RUN_DIR"/backtest-result-*.zip; do
        if [[ ! -f "$f" ]]; then continue; fi
        cp -p "$f" "$UI_RESULTS_DIR/"
        meta="${f%.zip}.meta.json"
        if [[ -f "$meta" ]]; then cp -p "$meta" "$UI_RESULTS_DIR/"; fi
        UI_COPIED=$((UI_COPIED + 1))
    done
fi

echo
echo "=== 回撤检查（上限 ${MAX_DD}%）"
CHECK_RC=0
# 用 freqtrade 环境的 python 运行（有 pandas，才能逐根K线计算含浮亏回撤）
CHECK_ARGS=("$RUN_DIR" --max-dd "$MAX_DD" --csv "$RUN_DIR/summary.csv")
if [[ -n "$CHECK_DATADIR" ]]; then CHECK_ARGS+=(--datadir "$CHECK_DATADIR"); fi
py_ft scripts/check_drawdown.py "${CHECK_ARGS[@]}" | tee "$RUN_DIR/summary.txt" || CHECK_RC=$?

echo
if [[ ${#FAILED[@]} -gt 0 ]]; then
    echo "有 ${#FAILED[@]}/$TOTAL 个回测运行失败：${FAILED[*]}（看 $RUN_DIR/logs/ 里的日志）"
fi
echo "全部结果：$RUN_DIR"
if [[ "$SYNTHETIC" == "yes" ]]; then
    echo "（合成数据的结果不复制给 FreqUI，FreqUI 的 Backtest 历史里看不到它们，这是故意的）"
elif [[ "$UI_COPIED" -gt 0 ]]; then
    echo "已复制 $UI_COPIED 个结果到 $UI_RESULTS_DIR/，用 FreqUI 查看：make ui（Docker）或 make ui-venv（venv），"
    echo "在 Backtest 页面的历史列表里打开，见 README「用 FreqUI 网页看回测结果」"
fi

if [[ ${#FAILED[@]} -gt 0 ]]; then exit 2; fi
exit "$CHECK_RC"
