#!/usr/bin/env bash
# ------------------------------------------------------------------
# 第 2 步：策略研究 —— 一条命令跑完 训练段 / 样本外 / 走步验证（+ 可选自动调参），最后出一张中文排名表
#
# 用法：
#   make research                                         # = bash scripts/research.sh（VPS 上用 tmux 跑，可能要一两个小时）
#   make research RESEARCH_HYPEROPT=1                     # 另外再自动调参（第 3 轮默认不调参，见下面 --hyperopt）
#   make research ARGS="--strategies 'DonchianTrend TsmomTrend'"
#   make research ARGS="--no-download"                    # 不更新数据，直接用已下载的
#   make research-synthetic                               # 合成数据离线跑一遍流程（不联网，数字没有意义）
#
# 参数（括号里是同名环境变量，命令行优先）：
#   --config FILE        研究配置，默认 configs/config.okx-research.json（RESEARCH_CONFIG）
#   --strategies "A B"   要研究的策略（RESEARCH_STRATEGIES），默认（第 3 轮：只留日线策略）：
#                        BigTrendShort BigTrendShortLowRisk BigTrendBoth TsmomTrend DonchianTrend XSMomentum
#                        （没找到的策略跳过，表里不会有它，运行结束时提示，退出码 2）
#   --start YYYYMMDD     研究从哪天开始（RESEARCH_START，默认 20210101：包含 2021 年牛市和 2022 年熊市）。
#                        下载时会再往前多下"最长的指标预热"那么多天（日线策略约 600 天，交易所有多少给多少），
#                        这样训练段第一天指标就是热的；多下的那段只用来预热，不交易
#   --min-train-days N   上线晚的币：样本外开始前至少要有 N 天"能交易"的历史才保留（RESEARCH_MIN_TRAIN_DAYS，默认 365）。
#                        "能交易" = 第一根K线 + 训练段的指标预热（日线策略最多 300 天）之后：freqtrade 回测按根数裁掉
#                        每个币自己开头的预热K线，上线晚的币要等这么久才开始交易（样本外、走步各段预热最多 600 根，等得更久；
#                        每个币每段从哪天开始能交易写在 data_check.txt）。实盘只要等策略自己的预热
#   --no-download        不下载/更新数据（RESEARCH_DOWNLOAD=0）
#   --hyperopt 0|1       是否自动调参（RESEARCH_HYPEROPT，第 3 轮默认 0 = 只用策略代码里的默认参数；--no-hyperopt 等于 0）。
#                        第 2 轮的真实结果：调参后训练段很好看、样本外反而更差（例如 DonchianTrend 训练 PF 4.31 → 样本外 0.79），
#                        走步各折调出的参数跳来跳去 —— 是在拟合噪音。所以这一轮先只看默认参数
#   --epochs N           每次调参跑几轮（RESEARCH_EPOCHS，默认 60；前 30 轮是随机抽样，后面在好的参数附近找）
#   --timeout SEC        每次调参最多跑几秒，只是保险（RESEARCH_HYPEROPT_TIMEOUT，默认 3600 = 60 分钟；0 = 不限）
#                        2 核 VPS 上 60 个币、约 6 年数据、60 轮一般 20 ~ 60 分钟跑完；到时间没跑满时，跑了几轮取决于机器快慢
#   --loss NAME          调参目标（RESEARCH_LOSS，默认 SharpeHyperOptLossDaily；也可以 CalmarHyperOptLoss）
#   --folds K            走步验证折数（RESEARCH_WF_FOLDS，默认 6；0 = 不做走步验证；数据放不下时自动减少）
#   --wf-train-days N    走步每折训练窗口天数（RESEARCH_WF_TRAIN_DAYS，默认 365）
#   --wf-test-days N     走步每折测试窗口天数（RESEARCH_WF_TEST_DAYS，默认 180；0 = 第 2 轮的自动切法：
#                        最近一半时间平分成 K 折、训练窗口 = 测试窗口 × RESEARCH_WF_TRAIN_RATIO）
#   --jobs N             调参同时开几个进程（RESEARCH_JOBS，默认按 CPU 核数和可用内存自动定；-1 = 全部 CPU 核）
#                        内存：每个调参进程要把全部K线装一份，60 个币、约 6 年数据约 2 GB / 个，主进程再加 1 ~ 2 GB；
#                        2 个进程约需 6 GB。默认取 min(CPU 核数, (可用内存GB - 1) / 2.2)，至少 1
#   --fee F              每边手续费+滑点（RESEARCH_FEE，默认 0.0007 = 0.07%；none = 用交易所费率）
#   --max-dd PCT         账户最大回撤上限 %（含浮亏；RESEARCH_MAX_DD，没设时用 MAX_DD_PCT，都没设 = 15 = 项目硬规则。
#                        2026-11-07 起 15%，第 1、2 轮是按 8% 判的；用的值写进 plan.json，以后重新出报告仍按它判。
#                        按别的上限重新判一次已经跑完的结果，不重跑回测：
#                        .venv/bin/python scripts/research_report.py report --run-dir <结果目录> --max-dd 12）
#   --datadir DIR        K线数据目录（RESEARCH_DATADIR，默认 user_data/data/<交易所>）
#   --synthetic          用合成数据离线跑（只检查流程；配置文件名含 synthetic 时也自动进入这个模式）
# 其他环境变量：RESEARCH_SEED（随机种子，默认 42）  RESEARCH_WF_TRAIN_RATIO（--wf-test-days 0 时：走步训练/测试长度比，默认 2）
#   RESEARCH_WARMUP_CANDLES（默认 600：每段回测开始前最多多加载多少根K线预热指标，有多少历史就给多少）
#   RESEARCH_WF_MODE（rolling 滚动 / anchored 扩展，默认 rolling）  RESEARCH_TIMEFRAMES（默认 "1h 4h 1d"）
#   RESEARCH_DETAIL（auto = 日线/4h 策略用 1h 模拟K线内部走势；none；或指定周期）
#   RESEARCH_HYPEROPT_GATE（默认 1：默认参数在训练段不赚钱、或交易少于下面这个数就不调参）  RESEARCH_HYPEROPT_MIN_TRADES（默认 20）
#   RESEARCH_MIN_TRADES（30） RESEARCH_MIN_PF（1.2） RESEARCH_PF_RATIO（0.6）：通过标准，见 README「第 2 步：策略研究」
#   RESEARCH_PORTFOLIO="A,B,C"（子账户组合检查放哪几个策略；默认 = 默认参数在训练段赚钱、PF ≥ 1.0、回撤 ≤ 上限（默认 15%）的策略，
#                        训练段日收益相关系数 ≥ 0.7 的两个只留一个；不看样本外，免得"用样本外挑、再用样本外判"）
#   RESEARCH_SEEN_FROM（默认 20240101：以前的研究（第 2 轮）已经看过这天以后的数据，第 3 轮的选择是看着它定的。
#                        样本外落在这段里时报告会提醒"不是独立证据"；研究开始到这天之前的那段（训练段的前一部分）
#                        每个策略用默认参数单独回测一次 = "没看过的一段"，单独列表、也要赚钱才算通过。none = 不检查）
#   RESEARCH_PORTFOLIO_WEIGHTS（equal 平均分，默认 / riskparity 按训练段日收益波动率的倒数分）：见 README「组合检查」
# 每个策略自己的设置：configs/research-overrides/<策略类名>.json 存在时，回测/调参这个策略时自动叠加在研究配置后面
#   （例如 XSMomentum.json：max_open_trades 6，多空各 3 笔；它实盘时单独一个机器人，配置和其他策略不一样）
# 基准策略（别人公开的策略）：RESEARCH_BENCHMARKS=1 时，策略代码除了 user_data/strategies/*.py，再加上
#   user_data/strategies/benchmarks/*.py（本项目写的封顶杠杆子类）和 user_data/strategies/vendor/*.py
#   （scripts/fetch_benchmarks.sh 下载的固定版本第三方策略），合到结果目录的 strategies/src/ 里用；一律只用默认参数、不调参。
#   一般不直接设它，用 make research-benchmarks（scripts/research_benchmarks.sh，说明见 docs/benchmarks.md）
#
# 做的事：
#   1. 下载/更新数据（scripts/download_okx_data.sh，周期 1h 4h 1d，合约模式自动带 funding_rate 和 mark；
#      本地数据比开始日期晚的币自动用 --prepend 补前面那段）
#   2. 检查数据，去掉数据不够的币（中途断了的、样本外开始前能交易的历史不到 RESEARCH_MIN_TRAIN_DAYS 天的），
#      实际用的币写到结果目录的 pairs.effective.json；data_check.txt 里列出每个币每一段从哪天开始能交易。切分区间：
#      训练段 = 前约 2/3，样本外 = 后约 1/3；走步验证 = 从数据末尾往前排 K 折，每折先训练 365 天、再测后面没见过的 180 天；
#      没看过的一段 = 训练段里 RESEARCH_SEEN_FROM 之前的部分（以前的研究没用过的数据）
#   3. 每个策略用默认参数回测 训练段、没看过的一段（RESEARCH_SEEN_FROM 之前那段）、样本外、走步各折
#   4. （调参时）默认参数在训练段要有足够交易（≥ RESEARCH_HYPEROPT_MIN_TRADES）而且赚钱，才调参；
#      hyperopt 只用训练段 → 把调出的参数拿去测样本外；走步每折各自只用自己的训练窗口调参再测
#      （这一折训练窗口里默认参数的交易数不够调参要求时，这一折不调参、用默认参数）
#      调出的参数只存在结果目录里，绝不改 user_data/strategies/ 里的策略和参数文件
#   5. 用 scripts/check_drawdown.py 的口径（含浮亏、逐根K线）评估，生成中文排名表
#
# 结果：user_data/research/run_<时间>/（合成数据在 user_data/research/synthetic/run_<时间>/）
#   summary.txt / summary.csv / summary.json   排名表（最后也会打印出来，可以直接复制）
#   data_check.txt  pairs.effective.json        数据检查结果、实际用到的交易对
#   params/<策略>__main.json / __wf<k>.json      调出来的参数     hyperopt/*.fthypt  调参原始记录
#   bt/<策略>__<参数>__<区间>/                   每次回测的 freqtrade 原始结果     logs/  每一步的完整日志
#
# 退出码：0 = 跑完了（不管策略通没通过）；2 = 有回测/调参运行出错，或者有要求研究的策略没找到/代码有语法错误
#         （报告照样生成，出错的看 logs/）；
#         1 = 参数错误或数据检查失败；3 = 一个结果都没有；130 = 按了 Ctrl+C（已用跑完的部分生成报告）
# 兼容 macOS 自带的 bash 3.2。
# ------------------------------------------------------------------
source "$(dirname "$0")/_common.sh"

# 第 3 轮：只研究日线策略（1h 策略第 2 轮被手续费吃光：一年约 1200 笔），加上新的 BigTrendShortLowRisk、XSMomentum
DEFAULT_STRATEGIES="BigTrendShort BigTrendShortLowRisk BigTrendBoth TsmomTrend DonchianTrend XSMomentum"

CONFIG="${RESEARCH_CONFIG:-configs/config.okx-research.json}"
STRATEGIES="${RESEARCH_STRATEGIES:-$DEFAULT_STRATEGIES}"
START="${RESEARCH_START:-20210101}"
# 第 2 轮用的是 2024-01-01 以后的数据：第 3 轮的样本外整段都在里面（见上面 RESEARCH_SEEN_FROM）
SEEN_FROM="${RESEARCH_SEEN_FROM:-20240101}"
MIN_TRAIN_DAYS="${RESEARCH_MIN_TRAIN_DAYS:-365}"
DOWNLOAD="${RESEARCH_DOWNLOAD:-1}"
# 第 3 轮默认不调参：第 2 轮调参后样本外普遍比默认参数还差（过拟合），见上面 --hyperopt
HYPEROPT="${RESEARCH_HYPEROPT:-0}"
EPOCHS="${RESEARCH_EPOCHS:-}"
HO_TIMEOUT="${RESEARCH_HYPEROPT_TIMEOUT:-}"
LOSS="${RESEARCH_LOSS:-SharpeHyperOptLossDaily}"
FOLDS="${RESEARCH_WF_FOLDS:-}"
JOBS_N="${RESEARCH_JOBS:-}"
FEE="${RESEARCH_FEE:-0.0007}"
WARMUP="${RESEARCH_WARMUP_CANDLES:-600}"
# 回撤上限：--max-dd > RESEARCH_MAX_DD > MAX_DD_PCT（scripts/_common.sh，默认 15 = 项目硬规则）
MAX_DD="${RESEARCH_MAX_DD:-$MAX_DD_PCT}"
DATADIR_ARG="${RESEARCH_DATADIR:-}"
SEED="${RESEARCH_SEED:-42}"
WF_RATIO="${RESEARCH_WF_TRAIN_RATIO:-2}"
WF_TRAIN_DAYS="${RESEARCH_WF_TRAIN_DAYS:-365}"
WF_TEST_DAYS="${RESEARCH_WF_TEST_DAYS:-180}"
WF_MODE="${RESEARCH_WF_MODE:-rolling}"
TIMEFRAMES="${RESEARCH_TIMEFRAMES:-1h 4h 1d}"
DETAIL="${RESEARCH_DETAIL:-auto}"
GATE="${RESEARCH_HYPEROPT_GATE:-1}"
HO_MIN_TRADES="${RESEARCH_HYPEROPT_MIN_TRADES:-}"
MIN_TRADES="${RESEARCH_MIN_TRADES:-30}"
MIN_PF="${RESEARCH_MIN_PF:-1.2}"
PF_RATIO="${RESEARCH_PF_RATIO:-0.6}"
SYNTHETIC="no"
HO_FLAG_SET=0
# 基准策略模式（见上面的说明）：0 = 平时，1 = 加上 benchmarks/ 和 vendor/ 里的策略、不调参
BENCHMARKS="${RESEARCH_BENCHMARKS:-0}"

while [[ $# -gt 0 ]]; do
    VAL="${2:-}"
    case "$1" in
        --config)       CONFIG="$VAL"; shift 2 ;;
        --strategies)   STRATEGIES="$VAL"; shift 2 ;;
        --start)        START="$VAL"; shift 2 ;;
        --min-train-days) MIN_TRAIN_DAYS="$VAL"; shift 2 ;;
        --wf-train-days) WF_TRAIN_DAYS="$VAL"; shift 2 ;;
        --wf-test-days) WF_TEST_DAYS="$VAL"; shift 2 ;;
        --no-download)  DOWNLOAD=0; shift ;;
        --hyperopt)     HYPEROPT="$VAL"; HO_FLAG_SET=1; shift 2 ;;
        --no-hyperopt)  HYPEROPT=0; HO_FLAG_SET=1; shift ;;
        --epochs)       EPOCHS="$VAL"; shift 2 ;;
        --timeout)      HO_TIMEOUT="$VAL"; shift 2 ;;
        --loss)         LOSS="$VAL"; shift 2 ;;
        --folds)        FOLDS="$VAL"; shift 2 ;;
        --jobs|-j)      JOBS_N="$VAL"; shift 2 ;;
        --fee)          FEE="$VAL"; shift 2 ;;
        --max-dd)       MAX_DD="$VAL"; shift 2 ;;
        --datadir)      DATADIR_ARG="$VAL"; shift 2 ;;
        --synthetic)    SYNTHETIC="yes"; shift ;;
        -h|--help)      sed -n '2,/^# -----/p' "$0"; exit 0 ;;
        *)              echo "未知参数：$1（看 bash scripts/research.sh --help）" >&2; exit 1 ;;
    esac
done

case "$CONFIG" in *synthetic*) SYNTHETIC="yes" ;; esac
if [[ "$SYNTHETIC" == "yes" ]]; then
    # 合成数据：只能离线跑（scripts/ft_offline.py，同 backtest_all.sh），结果单独存放；
    # 调参时把轮数、折数调小（几分钟就能跑完）；不调参时折数和真实研究一样（6 折），流程完全相同
    # shellcheck disable=SC2034  # 被 _common.sh 里的 ft / py_ft 使用
    FT_RUNNER_RESOLVED="synthetic"
    DATADIR_ARG="${DATADIR_ARG:-user_data/data/okx-synthetic}"
    RESULTS_BASE="user_data/research/synthetic"
    DOWNLOAD=0
    EPOCHS="${EPOCHS:-16}"
    HO_TIMEOUT="${HO_TIMEOUT:-300}"
    if [[ "$HYPEROPT" == "1" ]]; then FOLDS="${FOLDS:-2}"; else FOLDS="${FOLDS:-6}"; fi
    HO_MIN_TRADES="${HO_MIN_TRADES:-5}"
else
    RESULTS_BASE="user_data/research"
    EPOCHS="${EPOCHS:-60}"
    HO_TIMEOUT="${HO_TIMEOUT:-3600}"
    FOLDS="${FOLDS:-6}"
    HO_MIN_TRADES="${HO_MIN_TRADES:-20}"
fi

# ---------- 参数检查 ----------
bad_arg() { echo "参数错误：$1" >&2; exit 1; }
is_uint() { [[ "$1" =~ ^[0-9]+$ ]]; }
is_num() { [[ "$1" =~ ^[0-9]+([.][0-9]+)?$ ]]; }
[[ -f "$CONFIG" ]] || bad_arg "找不到配置文件 $CONFIG"
[[ -n "$STRATEGIES" ]] || bad_arg "--strategies 不能为空"
[[ "$START" =~ ^[0-9]{8}$ ]] || bad_arg "--start 要写成 YYYYMMDD，例如 20210101（收到：$START）"
case "$SEEN_FROM" in none|NONE|0|"") SEEN_FROM="" ;; esac
[[ -z "$SEEN_FROM" || "$SEEN_FROM" =~ ^[0-9]{8}$ ]] || bad_arg "RESEARCH_SEEN_FROM 要写成 YYYYMMDD 或 none（收到：$SEEN_FROM）"
is_uint "$MIN_TRAIN_DAYS" || bad_arg "--min-train-days 要是天数（收到：$MIN_TRAIN_DAYS）"
is_uint "$WF_TRAIN_DAYS" || bad_arg "--wf-train-days 要是天数（收到：$WF_TRAIN_DAYS）"
is_uint "$WF_TEST_DAYS" || bad_arg "--wf-test-days 要是天数（收到：$WF_TEST_DAYS）"
[[ "$HYPEROPT" == "0" || "$HYPEROPT" == "1" ]] || bad_arg "--hyperopt 只能是 0 或 1（收到：$HYPEROPT）"
is_uint "$EPOCHS" && [[ "$EPOCHS" -gt 0 ]] || bad_arg "--epochs 要是正整数（收到：$EPOCHS）"
is_uint "$HO_TIMEOUT" || bad_arg "--timeout 要是秒数（收到：$HO_TIMEOUT）"
is_uint "$FOLDS" || bad_arg "--folds 要是整数（收到：$FOLDS）"
is_uint "$SEED" || bad_arg "RESEARCH_SEED 要是整数（收到：$SEED）"
is_uint "$HO_MIN_TRADES" || bad_arg "RESEARCH_HYPEROPT_MIN_TRADES 要是整数（收到：$HO_MIN_TRADES）"
is_uint "$MIN_TRADES" || bad_arg "RESEARCH_MIN_TRADES 要是整数（收到：$MIN_TRADES）"
is_uint "$WARMUP" || bad_arg "RESEARCH_WARMUP_CANDLES 要是整数（收到：$WARMUP）"
[[ -z "$JOBS_N" ]] || { is_uint "$JOBS_N" && [[ "$JOBS_N" -gt 0 ]]; } || [[ "$JOBS_N" == "-1" ]] \
    || bad_arg "--jobs 要是正整数或 -1（收到：$JOBS_N）"
[[ "$FEE" == "none" ]] || is_num "$FEE" || bad_arg "--fee 要是小数，例如 0.0007（收到：$FEE）"
is_num "$MAX_DD" || bad_arg "--max-dd 要是数字（收到：$MAX_DD）"
awk -v x="$MAX_DD" 'BEGIN { exit !(x > 0 && x <= 100) }' || bad_arg "--max-dd 要在 0 ~ 100 之间（单位 %，收到：$MAX_DD）"
is_num "$WF_RATIO" || bad_arg "RESEARCH_WF_TRAIN_RATIO 要是数字（收到：$WF_RATIO）"
is_num "$MIN_PF" || bad_arg "RESEARCH_MIN_PF 要是数字（收到：$MIN_PF）"
is_num "$PF_RATIO" || bad_arg "RESEARCH_PF_RATIO 要是数字（收到：$PF_RATIO）"
[[ "$WF_MODE" == "rolling" || "$WF_MODE" == "anchored" ]] || bad_arg "RESEARCH_WF_MODE 只能是 rolling 或 anchored"
[[ "$LOSS" =~ ^[A-Za-z0-9_]+$ ]] || bad_arg "--loss 名字不对（收到：$LOSS）"
# 子账户组合检查（scripts/research_portfolio.py）：放进组合的策略（空 = 默认挑选规则）、权重方式
PORTFOLIO="${RESEARCH_PORTFOLIO:-}"
PORTFOLIO_WEIGHTS="${RESEARCH_PORTFOLIO_WEIGHTS:-equal}"
PORTFOLIO_RE='^[A-Za-z0-9_, ]*$'
[[ "$PORTFOLIO" =~ $PORTFOLIO_RE ]] || bad_arg "RESEARCH_PORTFOLIO 只能写策略名，用逗号分开，例如 \"BigTrendShortLowRisk,TsmomTrend\"（收到：$PORTFOLIO）"
[[ "$PORTFOLIO_WEIGHTS" == "equal" || "$PORTFOLIO_WEIGHTS" == "riskparity" ]] \
    || bad_arg "RESEARCH_PORTFOLIO_WEIGHTS 只能是 equal 或 riskparity（收到：$PORTFOLIO_WEIGHTS）"
[[ "$BENCHMARKS" == "0" || "$BENCHMARKS" == "1" ]] || bad_arg "RESEARCH_BENCHMARKS 只能是 0 或 1（收到：$BENCHMARKS）"

# ---------- 结果目录 ----------
RUN_ID="run_$(date +%Y%m%d_%H%M%S)"
RUN_DIR="$RESULTS_BASE/$RUN_ID"
HO_DIR="user_data/hyperopt_results"

# Docker 里 freqtrade 以 uid 1000 运行，要能往结果目录写文件
mkd() {
    mkdir -p "$@"
    if [[ "$FT_RUNNER_RESOLVED" == "docker" ]]; then chmod a+rwx "$@"; fi
}
mkd "$RUN_DIR" "$RUN_DIR/logs" "$RUN_DIR/bt" "$RUN_DIR/params" "$RUN_DIR/hyperopt" "$RUN_DIR/strategies"
mkdir -p "$HO_DIR"
RUN_LOG="$RUN_DIR/research.log"
JOBS_TSV="$RUN_DIR/jobs.tsv"
NOTES_TSV="$RUN_DIR/notes.tsv"
: > "$JOBS_TSV"
: > "$NOTES_TSV"

START_TS=$(date +%s)
STEP=0
FAILED=0

say() { printf '%s\n' "$*" | tee -a "$RUN_LOG"; }
note() { printf '%s\t%s\t%s\n' "$1" "$2" "$3" >> "$NOTES_TSV"; }
elapsed() {
    local s=$(( $(date +%s) - START_TS ))
    printf '已用 %d:%02d:%02d' $((s / 3600)) $((s % 3600 / 60)) $((s % 60))
}
seg_cn() {
    case "$1" in
        train) echo "训练段" ;;
        test)  echo "样本外" ;;
        holdout) echo "没看过的一段" ;;
        main)  echo "训练段" ;;
        wf*train) local k="${1#wf}"; echo "走步第 ${k%train} 折训练窗口" ;;
        wf*)   echo "走步第 ${1#wf} 折" ;;
        *)     echo "$1" ;;
    esac
}
variant_cn() { if [[ "$1" == "opt" ]]; then echo "优化参数"; else echo "默认参数"; fi; }
timeout_txt() {
    if [[ "$HO_TIMEOUT" -le 0 ]]; then echo "不限时"
    elif [[ "$HO_TIMEOUT" -lt 120 ]]; then echo "${HO_TIMEOUT} 秒"
    else echo "$((HO_TIMEOUT / 60)) 分钟"; fi
}

# 从 freqtrade 回测日志里取"总收益、交易数"两行，打印成一行中文
bt_brief() {
    local profit trades
    profit="$(awk -F'│' '/Total profit %/ {gsub(/ /, "", $3); print $3; exit}' "$1" 2>/dev/null || true)"
    trades="$(awk -F'│' '/Total\/Daily Avg Trades/ {gsub(/ /, "", $3); split($3, a, "/"); print a[1]; exit}' "$1" 2>/dev/null || true)"
    if [[ -n "$profit" || -n "$trades" ]]; then
        echo "总收益 ${profit:-?}，交易 ${trades:-?} 笔"
    elif grep -q "No trades made" "$1" 2> /dev/null; then
        echo "一笔交易都没有"
    else
        echo "完成"
    fi
}

# 失败时显示日志里的报错行（没有报错行就显示最后几行）
show_error() {
    local lines
    lines="$(grep -E " ERROR | CRITICAL |Error:|Exception" "$1" 2> /dev/null | tail -n 5 || true)"
    if [[ -z "$lines" ]]; then lines="$(tail -n 8 "$1" 2> /dev/null || true)"; fi
    printf '%s\n' "$lines" | cut -c1-300 | sed 's/^/        /' | tee -a "$RUN_LOG"
}

report() {
    py_ft scripts/research_report.py report --run-dir "$RUN_DIR" --stage "$1" 2>&1 | tee -a "$RUN_LOG"
}

# 记住屏幕输出：中断发生在 ft 函数（带 > 日志 重定向）里面时，陷阱也会继承那个重定向，要先换回屏幕
exec 3>&1 4>&2
on_interrupt() {
    trap - INT TERM
    exec 1>&3 2>&4
    echo
    say "!! 收到中断（Ctrl+C）。用已经跑完的部分生成报告 ..."
    if [[ -f "$RUN_DIR/plan.json" ]]; then report interrupted || true; fi
    say "结果目录：$RUN_DIR"
    exit 130
}
trap on_interrupt INT TERM

if [[ "$SYNTHETIC" == "yes" ]]; then
    say "!! 使用合成测试数据（不是真实行情），结果只用来检查流程，收益/回撤数字没有意义 !!"
    if [[ ! -f "$DATADIR_ARG/SYNTHETIC_DATA_README.txt" ]]; then
        if [[ "$DATADIR_ARG" == "user_data/data/okx-synthetic" ]]; then
            say "还没有合成数据，先生成（scripts/make_synthetic_data.py）..."
            py_ft scripts/make_synthetic_data.py 2>&1 | tee -a "$RUN_LOG" || { say "生成合成数据失败"; exit 1; }
        else
            bad_arg "$DATADIR_ARG 不是合成数据目录（没有 SYNTHETIC_DATA_README.txt）"
        fi
    fi
fi

# ---------- 调参需要的东西 ----------
HO_REASON=""
TIMEOUT_BIN="$(command -v timeout || command -v gtimeout || true)"
if [[ "$HYPEROPT" == "1" ]]; then
    if ! py_ft -c "import optuna, filelock" > /dev/null 2>&1; then
        HYPEROPT=0
        HO_REASON="没装 hyperopt 依赖"
    fi
elif [[ -n "${RESEARCH_HYPEROPT:-}" || "$HO_FLAG_SET" == "1" ]]; then
    HO_REASON="按设置不调参：RESEARCH_HYPEROPT=0"
else
    HO_REASON="第 3 轮默认不调参：第 2 轮调参后样本外反而更差（过拟合）；要调参用 RESEARCH_HYPEROPT=1"
fi
if [[ "$BENCHMARKS" == "1" ]]; then
    HYPEROPT=0
    HO_REASON="基准策略模式只用默认参数，不调参（RESEARCH_BENCHMARKS=1）"
fi
# 调参用哪个 python 跑（要套 timeout，不能直接用 _common.sh 里的 ft 函数）
HO_PY=""
if [[ "$FT_RUNNER_RESOLVED" == "docker" ]]; then
    HO_PY="docker"
elif [[ "$FT_RUNNER_RESOLVED" != "path" && -x "$ROOT_DIR/.venv/bin/python" ]]; then
    HO_PY="$ROOT_DIR/.venv/bin/python"
elif [[ "$FT_RUNNER_RESOLVED" == "synthetic" ]] && command -v docker > /dev/null 2>&1; then
    HO_PY="docker"
else
    HO_PY="python3"
fi

# ---------- 调参并行进程数 ----------
# 每个 hyperopt 子进程都要装一份全部K线（60 个币的 1h/4h/1d：约 2 年 10 个月时约 1 GB，2021 年起约 6 年时约 2 GB），
# 主进程再要 1 ~ 2 GB。默认 = min(CPU 核数, (可用内存GB - 1) / 2.2)，至少 1；内存不够时子进程会被系统杀掉，那次调参就白跑了。
cpu_count() {
    local n
    n="$(nproc 2> /dev/null || getconf _NPROCESSORS_ONLN 2> /dev/null || sysctl -n hw.ncpu 2> /dev/null || echo 1)"
    [[ "$n" =~ ^[0-9]+$ && "$n" -gt 0 ]] || n=1
    echo "$n"
}
# 可用内存（MB）：Linux 读 /proc/meminfo 的 MemAvailable，Mac 用 vm_stat（空闲 + 不活跃 + 可回收的页）；读不到输出空
mem_available_mb() {
    if [[ -r /proc/meminfo ]]; then
        awk '/^MemAvailable:/ {print int($2 / 1024); exit}' /proc/meminfo 2> /dev/null || true
    elif command -v vm_stat > /dev/null 2>&1; then
        vm_stat 2> /dev/null | awk '/page size of/ {for (i = 1; i <= NF; i++) if ($i ~ /^[0-9]+$/) ps = $i}
            /^Pages (free|inactive|speculative|purgeable)/ {gsub(/\./, "", $NF); s += $NF}
            END {if (ps > 0 && s > 0) print int(s * ps / 1048576)}' || true
    fi
}
JOBS_INFO=""
if [[ "$HYPEROPT" == "1" ]]; then
    CPUS="$(cpu_count)"
    MEM_MB="$(mem_available_mb)"
    MEM_TXT="可用内存读不到"
    MEM_JOBS=""
    if [[ "$MEM_MB" =~ ^[0-9]+$ && "$MEM_MB" -gt 0 ]]; then
        MEM_TXT="可用内存约 $(awk -v m="$MEM_MB" 'BEGIN {printf "%.1f", m / 1024}') GB"
        MEM_JOBS=$(( (MEM_MB - 1024) / 2252 ))
        [[ "$MEM_JOBS" -ge 1 ]] || MEM_JOBS=1
    fi
    if [[ -z "$JOBS_N" ]]; then
        if [[ -n "$MEM_JOBS" ]]; then
            JOBS_N=$(( CPUS < MEM_JOBS ? CPUS : MEM_JOBS ))
        else
            JOBS_N=$(( CPUS < 2 ? CPUS : 2 ))  # 读不到内存：保守用 2 个
        fi
        JOBS_INFO="并行 $JOBS_N 个进程（自动：CPU $CPUS 核，${MEM_TXT}，每个进程约需 2.2 GB）"
    elif [[ "$JOBS_N" == "-1" ]]; then
        JOBS_INFO="并行 = 全部 $CPUS 个 CPU 核（按设置；${MEM_TXT}，每个进程约需 2.2 GB）"
    else
        JOBS_INFO="并行 $JOBS_N 个进程（按设置；CPU $CPUS 核，${MEM_TXT}）"
    fi
    if [[ -n "$MEM_JOBS" && "$SYNTHETIC" != "yes" ]]; then
        WANT_JOBS="$JOBS_N"
        [[ "$WANT_JOBS" != "-1" ]] || WANT_JOBS="$CPUS"
        if [[ "$WANT_JOBS" -gt "$MEM_JOBS" ]]; then
            JOBS_INFO="$JOBS_INFO
          !! 内存可能不够 $WANT_JOBS 个调参进程（大约只够 $MEM_JOBS 个），子进程可能被系统杀掉；建议 RESEARCH_JOBS=$MEM_JOBS"
        fi
    fi
fi

say "================ 第 2 步：策略研究 ================"
say "运行方式：$FT_RUNNER_RESOLVED    配置：$CONFIG"
say "策略：    $STRATEGIES"
if [[ "$HYPEROPT" == "1" ]]; then
    say "调参：    开（每次 $EPOCHS 轮，最多 $(timeout_txt)，目标 $LOSS，随机种子 $SEED，只用训练窗口）"
    say "          $JOBS_INFO"
else
    say "调参：    关（$HO_REASON），只跑默认参数"
fi
if [[ "$HO_REASON" == "没装 hyperopt 依赖" ]]; then
    say "          要调参请先安装：.venv/bin/pip install -r requirements.txt（会装上 freqtrade[hyperopt]==2026.10；Docker 镜像已自带）"
fi
if [[ "$HYPEROPT" == "1" && "$HO_PY" != "docker" && -z "$TIMEOUT_BIN" && "$HO_TIMEOUT" -gt 0 ]]; then
    say "          提示：系统里没有 timeout 命令（Mac 可以 brew install coreutils），调参只按轮数停，不按时间停"
fi
if [[ "$FOLDS" -eq 0 ]]; then
    WF_TXT="不做（--folds 0）"
elif [[ "$WF_TEST_DAYS" -gt 0 ]]; then
    WF_TXT="${FOLDS} 折（每折训练 ${WF_TRAIN_DAYS} 天 → 测试 ${WF_TEST_DAYS} 天）"
else
    WF_TXT="${FOLDS} 折（自动切分）"
fi
say "研究区间：从 ${START} 开始（上线晚的币要在样本外开始前有 ${MIN_TRAIN_DAYS} 天以上能交易的历史）    走步验证：$WF_TXT"
if [[ -n "$SEEN_FROM" ]]; then
    say "以前的研究看过的数据：${SEEN_FROM} 以后（第 2 轮）；在那之前的一段单独回测（没看过的一段，RESEARCH_SEEN_FROM）"
fi
say "手续费+滑点：$FEE/边    回撤上限：${MAX_DD}%（账户权益含浮亏；项目硬规则 ${EASYTRADE_DEFAULT_MAX_DD_PCT}%）    指标预热：每段最多 $WARMUP 根K线"
say "结果目录：$RUN_DIR"

# ---------- 策略代码从哪里读 ----------
# 平时：user_data/strategies/*.py。基准策略模式（RESEARCH_BENCHMARKS=1）：再加上 benchmarks/ 和 vendor/ 里的 .py，
# 先合到结果目录的 strategies/src/（分析策略信息、复制各参数版本都从这里拿），不改 user_data/strategies/ 本身
STRAT_SRC_DIR="user_data/strategies"
if [[ "$BENCHMARKS" == "1" ]]; then
    if ! ls user_data/strategies/vendor/*.py > /dev/null 2>&1; then
        say "!! 没有基准策略文件（user_data/strategies/vendor/ 里没有 .py）。先运行：bash scripts/fetch_benchmarks.sh（说明见 docs/benchmarks.md）"
        exit 1
    fi
    STRAT_SRC_DIR="$RUN_DIR/strategies/src"
    mkd "$STRAT_SRC_DIR"
    cp user_data/strategies/*.py "$STRAT_SRC_DIR/"
    for f in user_data/strategies/benchmarks/*.py user_data/strategies/vendor/*.py; do
        [[ -f "$f" ]] || continue
        if [[ -e "$STRAT_SRC_DIR/$(basename "$f")" ]]; then
            say "!! $f 和别的策略文件同名（$(basename "$f")），没法放在一起，研究停止。把其中一个改名后再运行。"
            exit 1
        fi
        cp "$f" "$STRAT_SRC_DIR/"
    done
    say "基准策略模式：策略代码 = user_data/strategies/ + benchmarks/ + vendor/（合在 $STRAT_SRC_DIR），只用默认参数"
fi
say ""

# ---------- 1. 数据 ----------
DL_TFS="$(py_ft scripts/research_report.py timeframes --strategies "$STRATEGIES" --strategy-dir "$STRAT_SRC_DIR" \
    --timeframes "$TIMEFRAMES" --detail "$DETAIL" 2> /dev/null | tail -n 1 || true)"
[[ -n "$DL_TFS" ]] || DL_TFS="$TIMEFRAMES"
if [[ "$DOWNLOAD" == "1" ]]; then
    # 下载从"研究开始日期 - 最长的指标预热"开始（日线策略：max(300, RESEARCH_WARMUP_CANDLES) 根 = 约 600 天），
    # 训练段第一天指标就是热的；交易所没有那么早的数据时有多少给多少（不报错）
    DL_START="$(py_ft scripts/research_report.py timeframes --strategies "$STRATEGIES" --strategy-dir "$STRAT_SRC_DIR" \
        --timeframes "$TIMEFRAMES" --detail "$DETAIL" --print download-start --start "$START" --warmup-candles "$WARMUP" 2> /dev/null | tail -n 1 || true)"
    [[ "$DL_START" =~ ^[0-9]{8}$ ]] || DL_START="$START"
    if [[ "$DL_START" != "$START" ]]; then
        say "=== 1/5 下载/更新数据：周期 $DL_TFS，从 $DL_START 开始（$(elapsed)）"
        say "    研究从 $START 开始；前面多下的这段只用来给指标预热（交易所没有那么早的数据就从有数据的那天开始）"
    else
        say "=== 1/5 下载/更新数据：周期 $DL_TFS，从 $START 开始（$(elapsed)）"
    fi
    say "    已有的数据只补新的；本地数据开始得比这个日期晚、又不是因为上线晚的币，会自动补前面那段（--prepend）。"
    say "    下载前会先估算要多久（参考：60 个币第一次从 2021 年起下载约 30~45 分钟，以后更新几分钟）。完整输出：$RUN_DIR/logs/download.log"
    DL_ARGS=(--config "$CONFIG" --timeframes "$DL_TFS" --timerange "${DL_START}-")
    if [[ -n "$DATADIR_ARG" ]]; then DL_ARGS+=(--datadir "$DATADIR_ARG"); fi
    # 屏幕上每个币/周期只显示一行进度（带下载的日期段），加上下载脚本自己的提示行（"==> "、"  · " 开头）；
    # 完整输出在 logs/download.log
    set +e
    bash scripts/download_okx_data.sh "${DL_ARGS[@]}" 2>&1 | tee "$RUN_DIR/logs/download.log" \
        | awk -F'"' '/Download history data for/ {
                         split($3, a, ","); sub(/ and store.*/, "", a[3]); r = ""
                         if (match($0, /From [^ ]+ to [^ ]+/)) {
                             r = substr($0, RSTART + 5, RLENGTH - 5); sub(/ to /, " → ", r); gsub(/T00:00:00/, "", r)
                             sub(/now$/, "现在", r); r = "（" r "）"
                         }
                         printf "    下载 %s%s%s%s\n", $2, a[2], a[3], r; fflush() }
                     /^==> / { print "    " $0; fflush() }
                     /^  · / { print "  " $0; fflush() }
                     / ERROR / { print "    " $0; fflush() }'
    DL_RC=${PIPESTATUS[0]}
    set -e
    if [[ "$DL_RC" -ne 0 ]]; then
        say "    !! 下载出错（看 $RUN_DIR/logs/download.log 最后几行），继续用已经有的数据："
        tail -n 5 "$RUN_DIR/logs/download.log" | sed 's/^/       /' | tee -a "$RUN_LOG"
    else
        say "    下载完成"
    fi
elif [[ "$SYNTHETIC" == "yes" ]]; then
    say "=== 1/5 合成数据模式，不下载数据"
else
    say "=== 1/5 不下载数据（--no-download），直接用已经下载的"
fi
say ""

# ---------- 2. 数据检查 + 区间切分 ----------
say "=== 2/5 检查数据、去掉数据不够的币、切分区间（$(elapsed)）"
SETTINGS="$(printf '{"max_dd": %s, "max_leverage": 5, "min_pf": %s, "min_trades": %s, "pf_ratio": %s, "fee": "%s", "hyperopt": %s, "hyperopt_reason": "%s", "epochs": %s, "loss": "%s", "seed": %s, "timeout": %s, "jobs": "%s", "detail": "%s", "hyperopt_gate": %s, "hyperopt_min_trades": %s, "runner": "%s"}' \
    "$MAX_DD" "$MIN_PF" "$MIN_TRADES" "$PF_RATIO" "${FEE/none/}" "$HYPEROPT" "$HO_REASON" "$EPOCHS" "$LOSS" "$SEED" \
    "$HO_TIMEOUT" "$JOBS_N" "$DETAIL" "$GATE" "$HO_MIN_TRADES" "$FT_RUNNER_RESOLVED")"
# 子账户组合检查的设置也写进 plan.json（报告从这里读；Docker 里跑报告时环境变量传不进去）
SETTINGS="${SETTINGS%\}}, \"portfolio\": \"$PORTFOLIO\", \"portfolio_weights\": \"$PORTFOLIO_WEIGHTS\"}"
PREP_ARGS=(prepare --config "$CONFIG" --run-dir "$RUN_DIR" --strategies "$STRATEGIES" --strategy-dir "$STRAT_SRC_DIR"
           --timeframes "$TIMEFRAMES"
           --detail "$DETAIL" --start "$START" --min-train-days "$MIN_TRAIN_DAYS"
           --folds "$FOLDS" --wf-train-days "$WF_TRAIN_DAYS" --wf-test-days "$WF_TEST_DAYS" --wf-train-ratio "$WF_RATIO" --wf-mode "$WF_MODE"
           --warmup-candles "$WARMUP" --settings "$SETTINGS")
if [[ -n "$DATADIR_ARG" ]]; then PREP_ARGS+=(--datadir "$DATADIR_ARG"); fi
if [[ -n "$SEEN_FROM" ]]; then PREP_ARGS+=(--seen-from "$SEEN_FROM"); fi
if [[ "$SYNTHETIC" == "yes" ]]; then PREP_ARGS+=(--synthetic); fi
if ! py_ft scripts/research_report.py "${PREP_ARGS[@]}" 2>&1 | sed 's/^/    /' | tee -a "$RUN_LOG"; then
    say "数据检查失败，研究没法继续（上面有原因）。"
    exit 1
fi
# shellcheck disable=SC1090,SC1091
source "$RUN_DIR/plan.sh"
for S in $PLAN_STRATEGIES; do
    fsvar="S_${S}_FILESTEM"
    if [[ -f "user_data/strategies/${!fsvar:-$S}.json" ]]; then
        say "提示：发现 user_data/strategies/${!fsvar:-$S}.json（以前 hyperopt 写的 $S 的参数）。研究一律用策略代码里的默认参数，不读、也不改这个文件。"
    fi
done
say ""

# freqtrade 的公共参数：研究配置 + 本次实际用的交易对（第二个 --config 覆盖交易对列表）
CFG_ARGS=(--config "$CONFIG" --config "$RUN_DIR/pairs.effective.json")
if [[ -n "$DATADIR_ARG" ]]; then CFG_ARGS+=(--datadir "$DATADIR_ARG"); fi

# 每个"参数版本"一份策略代码副本：freqtrade 只认和策略文件同名的 .json 参数文件，
# 放在副本目录里，就不会碰 user_data/strategies/ 里的策略和参数
VARIANT_DIRS="base main"
k=1
while [[ $k -le $WF_COUNT ]]; do VARIANT_DIRS="$VARIANT_DIRS wf$k"; k=$((k + 1)); done
for v in $VARIANT_DIRS; do
    mkd "$RUN_DIR/strategies/$v"
    cp "$STRAT_SRC_DIR"/*.py "$RUN_DIR/strategies/$v/"
done

# 每个策略自己的研究设置（可选）：configs/research-overrides/<策略>.json，叠加在研究配置后面、只对这个策略生效
STRAT_CFG_DIR="configs/research-overrides"
strat_cfg() { if [[ -f "$STRAT_CFG_DIR/$1.json" ]]; then echo "$STRAT_CFG_DIR/$1.json"; fi; }

# 每个策略的附加参数：保护机制、手续费、细节周期、这个策略自己的设置
STRAT_ARGS=()
strat_args() {
    local dvar="S_${1}_DETAIL" scfg
    scfg="$(strat_cfg "$1")"
    STRAT_ARGS=(--enable-protections)
    if [[ -n "$scfg" ]]; then STRAT_ARGS+=(--config "$scfg"); fi
    if [[ "$FEE" != "none" ]]; then STRAT_ARGS+=(--fee "$FEE"); fi
    if [[ -n "${!dvar:-}" ]]; then STRAT_ARGS+=(--timeframe-detail "${!dvar}"); fi
}

# 这一段的指标预热：research_report.py prepare 写的 warmup/<策略>__<段>.json（只有 startup_candle_count），
# 让这一段开始前多加载K线（有多少历史给多少，最多 RESEARCH_WARMUP_CANDLES 根）。没有这个文件 = 用策略自己的预热长度
WARM_ARGS=()
warm_args() {
    local frag="$RUN_DIR/warmup/${1}__${2}.json"
    WARM_ARGS=()
    if [[ -f "$frag" ]]; then WARM_ARGS=(--config "$frag"); fi
}

# 用法：run_bt <策略> <default|opt> <train|test|holdout|wfN|wfNtrain> <timerange> <策略副本目录>
run_bt() {
    local strat="$1" variant="$2" seg="$3" tr="$4" sdir="$5"
    local label="${strat}__${variant}__${seg}"
    local out="$RUN_DIR/bt/$label" log="$RUN_DIR/logs/$label.log" status="ok"
    mkd "$out"
    warm_args "$strat" "$seg"
    local args=(backtesting "${CFG_ARGS[@]}" ${WARM_ARGS[@]+"${WARM_ARGS[@]}"} --strategy "$strat" --strategy-path "$sdir"
                --timerange "$tr" --export trades --backtest-directory "$out" --cache none
                --notes "research=$RUN_ID $variant $seg" ${STRAT_ARGS[@]+"${STRAT_ARGS[@]}"})
    STEP=$((STEP + 1))
    say "  [$STEP] 回测 $strat · $(variant_cn "$variant") · $(seg_cn "$seg")（${tr}，$(elapsed)）"
    if ft "${args[@]}" > "$log" 2>&1; then
        if [[ "$variant" == "opt" ]] && ! grep -q "Loading parameters from file" "$log"; then
            # 防呆：freqtrade 没读到调出来的参数文件，这一行其实是默认参数，不能算"优化参数"的结果
            status="fail"
            FAILED=$((FAILED + 1))
            say "        !! freqtrade 没有读到调出来的参数文件（日志里没有 \"Loading parameters from file\"），这次结果作废：$log"
        else
            say "        $(bt_brief "$log")"
        fi
    else
        status="fail"
        FAILED=$((FAILED + 1))
        say "        !! 回测失败（完整日志：$log）："
        show_error "$log"
    fi
    printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\n' "$strat" "$variant" "$seg" "$tr" "$status" "$out" "$log" >> "$JOBS_TSV"
    return 0
}

# 调参命令：套上 timeout（到时间发 Ctrl+C，freqtrade 会正常收尾、保存已跑完的轮数）
hyperopt_cmd() {
    local py=(scripts/research_report.py hyperopt)
    if [[ "$FT_RUNNER_RESOLVED" == "synthetic" ]]; then py+=(--offline); fi
    py+=(-- "$@")
    if [[ "$HO_PY" == "docker" ]]; then
        # 容器里自带 timeout（Debian 镜像），在容器内部计时，Ctrl+C 也能传进去
        if [[ "$HO_TIMEOUT" -gt 0 ]]; then
            compose_cmd run --rm -T --entrypoint timeout cli --foreground -s INT -k 120 "$HO_TIMEOUT" python "${py[@]}"
        else
            compose_cmd run --rm -T --entrypoint python cli "${py[@]}"
        fi
    elif [[ "$HO_TIMEOUT" -gt 0 && -n "$TIMEOUT_BIN" ]]; then
        "$TIMEOUT_BIN" --foreground -s INT -k 120 "$HO_TIMEOUT" "$HO_PY" "${py[@]}"
    else
        "$HO_PY" "${py[@]}"
    fi
}

# 用法：run_hyperopt <策略> <main|wfN> <训练 timerange> <策略副本目录>
# 成功（拿到参数，写到 <策略副本目录>/<策略文件名>.json）返回 0，否则返回 1。
# freqtrade 读的参数文件是"策略所在 .py 文件同名的 .json"（不是类名），文件名来自 plan.sh 的 S_<策略>_FILESTEM
run_hyperopt() {
    local strat="$1" label="$2" tr="$3" sdir="$4"
    local log="$RUN_DIR/logs/${strat}__hyperopt__${label}.log"
    local svar="S_${strat}_SPACES"
    local spaces="${!svar:-}"
    local fsvar="S_${strat}_FILESTEM"
    local filestem="${!fsvar:-$strat}"
    local wseg="${label}train"
    if [[ "$label" == "main" ]]; then wseg="train"; fi
    local t0 retry_why="" ho_secs=0
    local marker="$RUN_DIR/.hyperopt_started"
    local dest="$RUN_DIR/hyperopt/${strat}__${label}.fthypt"
    local status="fail" rc=0 found="" msg=""
    # 随机种子固定：轮数跑满（没到时间就停）、并行进程数相同时可以复现；走步第 k 折用 种子+k，
    # 免得每折抽到完全相同的候选参数、看起来"很稳定"
    local seed="$SEED"
    case "$label" in wf*) seed=$((SEED + ${label#wf})) ;; esac
    STEP=$((STEP + 1))
    say "  [$STEP] 调参 $strat · $(seg_cn "$label")：训练窗口 ${tr}，调 ${spaces}，$EPOCHS 轮（$(elapsed)）"
    warm_args "$strat" "$wseg"
    # shellcheck disable=SC2206  # spaces 故意按空格拆分
    local args=(${CFG_ARGS[@]+"${CFG_ARGS[@]}"} ${WARM_ARGS[@]+"${WARM_ARGS[@]}"} --strategy "$strat" --strategy-path "$sdir" --timerange "$tr"
                --spaces $spaces --hyperopt-loss "$LOSS" --epochs "$EPOCHS" --random-state "$seed"
                --min-trades "$HO_MIN_TRADES" --disable-param-export --no-color
                ${STRAT_ARGS[@]+"${STRAT_ARGS[@]}"})
    if [[ -n "$JOBS_N" ]]; then args+=(-j "$JOBS_N"); fi
    touch "$marker"
    t0=$(date +%s)
    hyperopt_cmd "${args[@]}" > "$log" 2>&1 || rc=$?
    ho_secs=$(( $(date +%s) - t0 ))
    if [[ $rc -ne 0 && $rc -ne 124 && "$JOBS_N" != "1" ]]; then
        if grep -q "PicklingError" "$log"; then
            retry_why="多进程调参出错（PicklingError）"
        elif grep -qE "TerminatedWorkerError|BrokenProcessPool" "$log"; then
            retry_why="调参的子进程被系统杀掉了（多半是内存不够）"
        elif [[ $rc -eq 137 ]] && [[ "$HO_TIMEOUT" -le 0 || "$ho_secs" -lt "$HO_TIMEOUT" ]]; then
            # 137 = 被 kill -9；没到时间就被杀，多半是系统内存不够（到时间后 timeout 补发的 kill 不算）
            retry_why="调参进程被系统杀掉了（退出码 137，多半是内存不够）"
        fi
    fi
    if [[ -n "$retry_why" ]]; then
        say "        ${retry_why}，改成单进程重跑（会慢一些；以后可以设 RESEARCH_JOBS=1）"
        touch "$marker"
        rc=0
        hyperopt_cmd "${args[@]}" -j 1 >> "$log" 2>&1 || rc=$?
    fi
    if [[ $rc -eq 124 ]]; then
        say "        到时间了（$(timeout_txt)），用已经跑完的轮数里最好的一组"
    elif [[ $rc -ne 0 ]]; then
        say "        !! hyperopt 出错（退出码 $rc，完整日志：$log）："
        show_error "$log"
    fi
    found="$(find "$HO_DIR" -maxdepth 1 -name "strategy_${strat}_*.fthypt" -newer "$marker" 2> /dev/null | sort | tail -n 1 || true)"
    if [[ -n "$found" ]]; then
        # 合成数据的调参记录不留在 user_data/hyperopt_results/（免得和真实结果混在一起）
        if [[ "$SYNTHETIC" == "yes" ]]; then mv "$found" "$dest" 2> /dev/null || cp "$found" "$dest"; else cp "$found" "$dest"; fi
    fi
    if [[ -f "$dest" ]]; then
        if msg="$(py_ft scripts/research_report.py best-params --fthypt "$dest" --strategy "$strat" \
                --out "$sdir/$filestem.json" --copy "$RUN_DIR/params/${strat}__${label}.json" --label "$label" 2>&1)"; then
            status="ok"
        fi
        say "        $(printf '%s\n' "$msg" | tail -n 1)"
    else
        say "        !! 没有生成调参结果文件（完整日志：$log）"
    fi
    if [[ "$status" != "ok" && $rc -ne 0 && $rc -ne 124 ]]; then FAILED=$((FAILED + 1)); fi
    printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\n' "$strat" "hyperopt" "$label" "$tr" "$status" "$dest" "$log" >> "$JOBS_TSV"
    [[ "$status" == "ok" ]]
}

# ---------- 3. 默认参数 ----------
N_STRATS=$(echo $PLAN_STRATEGIES | wc -w | tr -d ' ')
N_SEG=$((2 + WF_COUNT))
SEG_TXT="训练段 + 样本外 + 走步 $WF_COUNT 折"
if [[ -n "${HOLDOUT_RANGE:-}" ]]; then N_SEG=$((N_SEG + 1)); SEG_TXT="训练段 + 没看过的一段 + 样本外 + 走步 $WF_COUNT 折"; fi
say "=== 3/5 默认参数回测：$N_STRATS 个策略 × （${SEG_TXT}）= $((N_STRATS * N_SEG)) 次（$(elapsed)）"
for STRAT in $PLAN_STRATEGIES; do
    strat_args "$STRAT"
    tfvar="S_${STRAT}_TF"
    say "--- $STRAT（周期 ${!tfvar:-?}）"
    SCFG="$(strat_cfg "$STRAT")"
    if [[ -n "$SCFG" ]]; then
        SCFG_TXT="$(py_ft -c "import json, sys, rapidjson; d = rapidjson.loads(open(sys.argv[1], encoding='utf-8').read(), parse_mode=rapidjson.PM_COMMENTS | rapidjson.PM_TRAILING_COMMAS); print('，'.join(f'{k} = {json.dumps(v, ensure_ascii=False)}' for k, v in d.items()))" "$SCFG" 2> /dev/null || true)"
        say "    这个策略自己的设置：$SCFG（${SCFG_TXT:-读不出内容}），叠加在研究配置后面"
        note "$STRAT" default "这个策略用了自己的研究设置 $SCFG（${SCFG_TXT:-见文件}），其他策略不受影响"
    fi
    run_bt "$STRAT" default train "$TRAIN_RANGE" "$RUN_DIR/strategies/base"
    if [[ -n "${HOLDOUT_RANGE:-}" ]]; then
        run_bt "$STRAT" default holdout "$HOLDOUT_RANGE" "$RUN_DIR/strategies/base"
    fi
    run_bt "$STRAT" default test "$TEST_RANGE" "$RUN_DIR/strategies/base"
    k=1
    while [[ $k -le $WF_COUNT ]]; do
        tv="WF_TEST_$k"
        run_bt "$STRAT" default "wf$k" "${!tv}" "$RUN_DIR/strategies/base"
        k=$((k + 1))
    done
done
say ""

# ---------- 4. 调参 ----------
if [[ "$HYPEROPT" == "1" ]]; then
    N_HO=0
    for STRAT in $PLAN_STRATEGIES; do
        svar="S_${STRAT}_SPACES"
        bvar="S_${STRAT}_HO_BLOCK"
        if [[ -n "${!svar:-}" && -z "${!bvar:-}" ]]; then N_HO=$((N_HO + 1 + WF_COUNT)); fi
    done
    say "默认参数阶段的结果（调参还没开始；最终结果看最后那张表）："
    report interim || true
    say ""
    say "=== 4/5 调参：最多 $N_HO 次 hyperopt，每次 $EPOCHS 轮、最多 $(timeout_txt)（$(elapsed)）"
    say "    默认参数在训练段就不赚钱、或者交易少于 $HO_MIN_TRADES 笔的策略不调参（RESEARCH_HYPEROPT_GATE=0 可以关掉这条规则）。"
    say "    中途想停：按 Ctrl+C，会用已经跑完的部分生成报告。"
    for STRAT in $PLAN_STRATEGIES; do
        strat_args "$STRAT"
        svar="S_${STRAT}_SPACES"
        say "--- $STRAT"
        if [[ -z "${!svar:-}" ]]; then
            say "    没有可调参数，不调参"
            note "$STRAT" default "策略没有可调参数（buy/sell），只有默认参数这一行"
            continue
        fi
        bvar="S_${STRAT}_HO_BLOCK"
        if [[ -n "${!bvar:-}" ]]; then
            say "    不调参：${!bvar}"
            note "$STRAT" default "没有调参：${!bvar}"
            continue
        fi
        if [[ "$GATE" == "1" ]]; then
            if ! gate_msg="$(py_ft scripts/research_report.py gate --run-dir "$RUN_DIR" --strategy "$STRAT" \
                    --min-trades "$HO_MIN_TRADES" 2>&1 | tail -n 1)"; then
                say "    $gate_msg"
                note "$STRAT" default "没有调参：$gate_msg"
                continue
            fi
            say "    $gate_msg"
        fi
        if ! run_hyperopt "$STRAT" main "$TRAIN_RANGE" "$RUN_DIR/strategies/main"; then
            note "$STRAT" default "训练段调参没有得到可用参数（看 logs/${STRAT}__hyperopt__main.log），只有默认参数这一行"
            continue
        fi
        run_bt "$STRAT" opt train "$TRAIN_RANGE" "$RUN_DIR/strategies/main"
        run_bt "$STRAT" opt test "$TEST_RANGE" "$RUN_DIR/strategies/main"
        okvar="S_${STRAT}_WF_OK"
        # shellcheck disable=SC2206
        WF_OK_LIST=(${!okvar:-})
        k=1
        while [[ $k -le $WF_COUNT ]]; do
            trv="WF_TRAIN_$k"
            tv="WF_TEST_$k"
            if [[ "${WF_OK_LIST[$((k - 1))]:-1}" != "1" ]]; then
                say "    走步第 $k 折：训练窗口扣掉指标预热后太短，不调参，这一折用默认参数"
                note "$STRAT" opt "走步第 $k 折训练窗口扣掉指标预热后太短，没有调参"
                k=$((k + 1))
                continue
            fi
            # 先用默认参数回测一次这一折的训练窗口：交易数不到 hyperopt 的 --min-trades，
            # 每一轮都会被判"交易太少"，调参只会白跑满时间（或者挑出"交易最多"的参数）
            if [[ "$GATE" == "1" ]]; then
                run_bt "$STRAT" default "wf${k}train" "${!trv}" "$RUN_DIR/strategies/base"
                if ! gate_msg="$(py_ft scripts/research_report.py gate --run-dir "$RUN_DIR" --strategy "$STRAT" \
                        --segment "wf${k}train" --min-trades "$HO_MIN_TRADES" --trades-only 2>&1 | tail -n 1)"; then
                    say "    走步第 $k 折：$gate_msg；这一折用默认参数"
                    note "$STRAT" opt "走步第 $k 折没有调参：$gate_msg，这一折用默认参数"
                    k=$((k + 1))
                    continue
                fi
            fi
            if run_hyperopt "$STRAT" "wf$k" "${!trv}" "$RUN_DIR/strategies/wf$k"; then
                run_bt "$STRAT" opt "wf$k" "${!tv}" "$RUN_DIR/strategies/wf$k"
            else
                note "$STRAT" opt "走步第 $k 折调参没有得到可用参数（看 logs/${STRAT}__hyperopt__wf$k.log）"
            fi
            k=$((k + 1))
        done
    done
    say ""
else
    say "=== 4/5 跳过调参（$HO_REASON）"
    say ""
fi

# ---------- 5. 汇总 ----------
say "=== 5/5 汇总（$(elapsed)）"
say ""
REPORT_RC=0
report final || REPORT_RC=$?
say ""
if [[ $FAILED -gt 0 ]]; then
    say "有 $FAILED 次回测/调参运行出错（看 $RUN_DIR/logs/ 里的日志），其他策略照常完成。"
fi
if [[ -n "${PLAN_DROPPED:-}" ]]; then
    say "!! 这些策略没有研究（没找到，或者代码有语法错误，原因见 $RUN_DIR/data_check.txt）：$PLAN_DROPPED"
fi
say "全部结果：$RUN_DIR    （$(elapsed)）"
if [[ "$SYNTHETIC" == "yes" ]]; then
    say "（合成数据：只看流程有没有报错，表里的数字没有意义）"
fi
if [[ $REPORT_RC -ne 0 ]]; then exit 3; fi
if [[ $FAILED -gt 0 || -n "${PLAN_DROPPED:-}" ]]; then exit 2; fi
exit 0
