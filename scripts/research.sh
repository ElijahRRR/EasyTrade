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
#   --synthetic          用合成数据离线跑（只检查流程；配置文件名含 synthetic 时也自动进入这个模式）。
#                        数据目录默认按配置里的交易所：OKX = user_data/data/okx-synthetic，币安 = user_data/data/binance-synthetic
#                        （没有就自动生成；要研究第 5 轮的新策略、或打开下面第 5 轮的开关时，旧版合成数据会自动重新生成）
# 第 5 轮新增（docs/round5-preregistration.md 第 3 节；不打开时研究流程和以前完全一样）：
#   --prereg FILE        预注册登记表（RESEARCH_PREREG，默认 configs/preregistration.json；没有这个文件 = 所有策略都按"没有登记"照常研究）。
#                        任何回测之前先核对：登记过的策略，文件的 sha256 必须和登记的一样，不一样就拒绝运行（退出码 4）；
#                        没登记的策略照常研究。每个策略文件的 sha256、git 提交号写进 plan.json 的 "prereg"（scripts/check_prereg.py）
#   --allow-unregistered 和登记表不一致也照样跑（RESEARCH_ALLOW_UNREGISTERED=1）：会大声提醒，plan.json 里记为"不是正式的预注册运行"
#   --with-spot / --no-spot  第 1 步另外下载现货 BASE/USDT 1h（--trading-mode spot，从"研究开始 - 预热"开始），数据检查里报告
#                        每个币现货和永续 1h 时间戳能对上的比例（RESEARCH_WITH_SPOT=1 / 0；默认 auto = 策略里有 PremiumFade 时自动打开）
#   --keep-delisted      数据检查时不去掉中途停止交易（可能已下架）的币（RESEARCH_KEEP_DELISTED=1，默认 0 = 和以前一样去掉）。
#                        注意：freqtrade 回测里一个币没有K线以后，还开着的仓位要到这一段结束才平（按它最后一根K线的开盘价），
#                        交易所的交易对规格里已经没有的合约（例如 OKX 删掉的）没有杠杆档位，没法回测，自动去掉（data_check.txt 里列出）
#   --pre-holdout A-B    研究开始之前的留出段（RESEARCH_PRE_HOLDOUT，例如 20200101-20201231，必须在 --start 之前结束；默认不跑）：
#                        主运行之后每个策略用默认参数再回测一次这一段，更早的数据只用来预热指标（下载会自动多下这一段），
#                        回撤口径和 check_drawdown.py 相同，报告里单独一张表（jobs.tsv 里的段名 preholdout）
#   --pre-holdout-pairs "BTC ETH ..."  留出段用哪些币（RESEARCH_PRE_HOLDOUT_PAIRS，可以只写币名；默认 = 研究配置里留出段有数据的币
#                        （包括这一段里才上线的；它们要等策略自己的预热过了才交易））
#   --data-check-only    只做管道检查（RESEARCH_DATA_CHECK_ONLY=1；预注册 3.2 节"只看数据，不看收益"）：下载（除非 --no-download）、
#                        数据检查、现货 / 资金费覆盖、新币上线日文件和每年新币数，然后停止，一次回测都不跑；plan.json 记为 data_check_only，
#                        不是正式运行。正式运行之前先用它决定"测不了 / 无效"（README「第 5 步」第 2 节）
#   RESEARCH_LISTINGS（auto / 0 / 1；auto = 策略里有 NewListingShortHedged 时）：回测前用 scripts/build_listings.py 扫描本地 1d 数据，
#                        生成新币上线日文件 user_data/listings/<数据目录名>_first_candle.json（副本和 sha256 记在结果目录）
#   RESEARCH_FUNDING_CHECK（auto / 0 / 1；auto = 策略里有 XSFundingCarry 时）：数据检查里报告每个币的资金费条数、起止日期、
#                        结算间隔，以及资金费和标记价格同一时刻都有的比例
#   研究配置里的交易对写成正则（例如 configs/config.okx-research-listings.json 的"OKX 全部 USDT 永续"）时，按本地有K线的合约展开
#   （freqtrade 回测只按交易所现在还有的合约展开正则）。币安：--config configs/config.binance-research.json（只能在币安服务的地区运行），
#   合约模式下资金费率和标记价格自动按整个下载区间一起下载。
#   RESEARCH_MIN_TRAIN_DAYS=0 = 不要求样本外开始前有能交易的历史：样本外开始以后才上线的币也保留（新币策略要用）
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
#   0. 预注册检查（scripts/check_prereg.py，见上面 --prereg）：登记过的策略文件和登记表不一致就停止，一次回测都不跑
#   1. 下载/更新数据（scripts/download_okx_data.sh，周期 1h 4h 1d，合约模式自动带 funding_rate 和 mark；
#      本地数据比开始日期晚的币自动用 --prepend 补前面那段；--with-spot 时另外下载现货 1h）
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
#         1 = 参数错误或数据检查失败；3 = 一个结果都没有；130 = 按了 Ctrl+C（已用跑完的部分生成报告）；
#         4 = 预注册检查不通过（登记过的策略文件和 configs/preregistration.json 不一致，一次回测都没跑）
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
# 第 5 轮（见上面的说明；默认全部不打开 = 和以前一样，只多一步预注册检查）
PREREG="${RESEARCH_PREREG:-configs/preregistration.json}"
ALLOW_UNREG="${RESEARCH_ALLOW_UNREGISTERED:-0}"
WITH_SPOT="${RESEARCH_WITH_SPOT:-auto}"
KEEP_DELISTED="${RESEARCH_KEEP_DELISTED:-0}"
PRE_HOLDOUT="${RESEARCH_PRE_HOLDOUT:-}"
PRE_HOLDOUT_PAIRS="${RESEARCH_PRE_HOLDOUT_PAIRS:-}"
LISTINGS="${RESEARCH_LISTINGS:-auto}"
FUNDING_CHECK="${RESEARCH_FUNDING_CHECK:-auto}"
DATA_CHECK_ONLY="${RESEARCH_DATA_CHECK_ONLY:-0}"

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
        --prereg)       PREREG="$VAL"; shift 2 ;;
        --allow-unregistered) ALLOW_UNREG=1; shift ;;
        --with-spot)    WITH_SPOT=1; shift ;;
        --no-spot)      WITH_SPOT=0; shift ;;
        --keep-delisted) KEEP_DELISTED=1; shift ;;
        --pre-holdout)  PRE_HOLDOUT="$VAL"; shift 2 ;;
        --pre-holdout-pairs) PRE_HOLDOUT_PAIRS="$VAL"; shift 2 ;;
        --data-check-only) DATA_CHECK_ONLY=1; shift ;;
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
    # 数据目录（没写 --datadir 时按配置里的交易所定）在下面检查完参数后再定
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
# 第 5 轮的开关
[[ "$ALLOW_UNREG" == "0" || "$ALLOW_UNREG" == "1" ]] || bad_arg "RESEARCH_ALLOW_UNREGISTERED 只能是 0 或 1（收到：$ALLOW_UNREG）"
[[ "$KEEP_DELISTED" == "0" || "$KEEP_DELISTED" == "1" ]] || bad_arg "RESEARCH_KEEP_DELISTED 只能是 0 或 1（收到：$KEEP_DELISTED）"
[[ "$DATA_CHECK_ONLY" == "0" || "$DATA_CHECK_ONLY" == "1" ]] || bad_arg "RESEARCH_DATA_CHECK_ONLY 只能是 0 或 1（收到：$DATA_CHECK_ONLY）"
[[ "$WITH_SPOT" =~ ^(auto|0|1)$ ]] || bad_arg "RESEARCH_WITH_SPOT 只能是 auto、0 或 1（收到：$WITH_SPOT）"
[[ "$LISTINGS" =~ ^(auto|0|1)$ ]] || bad_arg "RESEARCH_LISTINGS 只能是 auto、0 或 1（收到：$LISTINGS）"
[[ "$FUNDING_CHECK" =~ ^(auto|0|1)$ ]] || bad_arg "RESEARCH_FUNDING_CHECK 只能是 auto、0 或 1（收到：$FUNDING_CHECK）"
[[ -n "$PREREG" ]] || bad_arg "--prereg 不能为空（默认 configs/preregistration.json）"
case "$PRE_HOLDOUT" in none|NONE|0) PRE_HOLDOUT="" ;; esac
if [[ -n "$PRE_HOLDOUT" ]]; then
    [[ "$PRE_HOLDOUT" =~ ^[0-9]{8}-[0-9]{8}$ ]] \
        || bad_arg "RESEARCH_PRE_HOLDOUT 要写成 YYYYMMDD-YYYYMMDD，例如 20200101-20201231（收到：$PRE_HOLDOUT）"
    [[ "${PRE_HOLDOUT%%-*}" < "${PRE_HOLDOUT##*-}" ]] || bad_arg "RESEARCH_PRE_HOLDOUT 的开始日期要早于结束日期（收到：$PRE_HOLDOUT）"
    [[ "${PRE_HOLDOUT##*-}" < "$START" ]] \
        || bad_arg "RESEARCH_PRE_HOLDOUT 要在研究开始（--start $START）之前结束，不能和研究区间重叠（收到：$PRE_HOLDOUT）"
fi
PH_PAIRS_RE='^[A-Za-z0-9/:_, -]*$'
[[ "$PRE_HOLDOUT_PAIRS" =~ $PH_PAIRS_RE ]] || bad_arg "RESEARCH_PRE_HOLDOUT_PAIRS 只能写交易对或币名，用空格或逗号分开（收到：$PRE_HOLDOUT_PAIRS）"
if [[ -n "$PRE_HOLDOUT_PAIRS" && -z "$PRE_HOLDOUT" ]]; then
    bad_arg "设了 RESEARCH_PRE_HOLDOUT_PAIRS 但没有设 RESEARCH_PRE_HOLDOUT（留出段的日期，例如 20200101-20201231）"
fi
# auto 的开关按要研究的策略定：PremiumFade 要现货、NewListingShortHedged 要上线日文件、XSFundingCarry 要看资金费覆盖
has_strategy() { case " $(echo $STRATEGIES) " in *" $1 "*) return 0 ;; esac; return 1; }
if [[ "$WITH_SPOT" == "auto" ]]; then if has_strategy PremiumFade; then WITH_SPOT=1; else WITH_SPOT=0; fi; fi
if [[ "$LISTINGS" == "auto" ]]; then if has_strategy NewListingShortHedged; then LISTINGS=1; else LISTINGS=0; fi; fi
if [[ "$FUNDING_CHECK" == "auto" ]]; then if has_strategy XSFundingCarry; then FUNDING_CHECK=1; else FUNDING_CHECK=0; fi; fi
# 合成数据：数据目录按配置里的交易所（币安的合成数据单独一个目录，带完整的资金费率和标记价格）
SYN_GEN_ARGS=()
if [[ "$SYNTHETIC" == "yes" && -z "$DATADIR_ARG" ]]; then
    CFG_EXCHANGE="$(py_ft -c "import sys
from freqtrade.configuration.load_config import load_from_files
print(str((load_from_files([sys.argv[1]]).get('exchange') or {}).get('name') or '').lower())" "$CONFIG" 2> /dev/null | tail -n 1 || true)"
    if [[ "$CFG_EXCHANGE" == binance* ]]; then
        DATADIR_ARG="user_data/data/binance-synthetic"
        SYN_GEN_ARGS=(--exchange binance)
    else
        DATADIR_ARG="user_data/data/okx-synthetic"
    fi
fi

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
        preholdout) echo "研究开始之前的留出段" ;;
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
    # 登记表换了文件（--prereg）时，报告也按那个文件判（默认的 configs/preregistration.json 报告自己会读）
    local extra=()
    if [[ "$PREREG" != "configs/preregistration.json" ]]; then extra=(--prereg "$PREREG"); fi
    py_ft scripts/research_report.py report --run-dir "$RUN_DIR" --stage "$1" ${extra[@]+"${extra[@]}"} 2>&1 | tee -a "$RUN_LOG"
}

# 第 5 轮的数据检查补充（py_ft -c "$RS_PY" <子命令> ...；只在打开对应开关、或白名单里有正则时运行）：
#   expand    白名单里的正则按本地有K线的合约展开（freqtrade 回测只按交易所现在还有的合约展开）
#   post      保留中途停止交易的币 / RESEARCH_MIN_TRAIN_DAYS=0 时保留样本外开始以后才上线的币 / 现货覆盖 / 资金费覆盖 /
#             研究开始之前的留出段用哪些币 / 新币上线日文件的数据起点；结果写进 plan.json、data_check.txt、plan.sh、preholdout/
#   preflight 按回测的方式加载交易所，去掉没有交易对规格、不活跃（没写 allow_inactive）、没有杠杆档位的币
IFS= read -r -d '' RS_PY <<'PYEOF' || true
# research.sh 第 5 轮的数据检查补充（由 research.sh 用 py_ft -c 运行，需要 freqtrade 环境）。
# 子命令：expand（白名单里的正则按本地数据展开）、post（保留下架币 / 现货覆盖 / 资金费覆盖 / 留出段的币）、
#         preflight（核对交易所规格和杠杆档位：freqtrade 回测时没有杠杆档位的币会让整次回测报错退出）。
# 只读K线数据；结果写进结果目录（plan.json、data_check.txt、pairs.effective.json、plan.sh、preholdout/）。
import json
import os
import re
import shlex
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

REGEX_CHARS = re.compile(r"[*?\[\]()^$|+\\]")
BTC = "BTC/USDT:USDT"
# 策略自己开仓要求的最少合格币数（XSFundingCarry.min_universe = 15）：留出段的币比这少时提醒
MIN_POOL = {"XSFundingCarry": 15}
EFFECTIVE_NOTE = ("本次研究实际使用的交易对（research_report.py prepare 自动生成，research.sh 第 5 轮的数据检查补充可能改过，不要手改）。"
                  "见同目录 data_check.txt。")


def out(lines, msg=""):
    print(msg)
    lines.append(msg)


def load_cfg(path):
    from freqtrade.configuration.load_config import load_from_files

    return load_from_files([path])


def cfg_datadir(cfg, datadir_arg):
    exch = str((cfg.get("exchange") or {}).get("name") or "")
    return Path(datadir_arg) if datadir_arg else Path("user_data") / "data" / exch


def handler(datadir, fmt):
    from freqtrade.data.history import get_datahandler

    return get_datahandler(Path(datadir), fmt)


def load_df(dh, pair, tf, ct):
    from freqtrade.enums import CandleType

    try:
        df = dh.ohlcv_load(pair, tf, candle_type=CandleType.from_string(ct), fill_missing=False, warn_no_data=False)
    except Exception:  # noqa: BLE001 — 文件坏了按没有数据算
        return None
    return None if df is None or df.empty else df


def tf_sec(tf):
    from freqtrade.exchange import timeframe_to_seconds

    return timeframe_to_seconds(tf)


def base(p):
    return p.split("/")[0]


def write_pairs_fragment(path, pairs, note):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "{\n"
        f"    // {note}\n"
        '    "exchange": {\n'
        '        "pair_whitelist": [\n'
        + ",\n".join(f'            "{p}"' for p in pairs)
        + "\n        ]\n    }\n}\n",
        encoding="utf-8",
    )


def set_sh(path, key, value):
    """plan.sh 里改一行 KEY=value（没有就加一行）。"""
    lines = path.read_text(encoding="utf-8").splitlines() if path.is_file() else []
    new = f"{key}={shlex.quote(str(value))}"
    for i, ln in enumerate(lines):
        if ln.startswith(f"{key}="):
            lines[i] = new
            break
    else:
        lines.append(new)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def save_json(path, obj):
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    os.replace(tmp, path)


def append_check(run_dir, lines):
    if lines:
        with (run_dir / "data_check.txt").open("a", encoding="utf-8") as fh:
            fh.write("\n" + "\n".join(lines) + "\n")


def scan(dh, pair, tfs, ct="futures"):
    """各周期取最晚的第一根、最早的结束时间（同 research_report.py prepare）。没有数据 = None。"""
    firsts, ends = [], []
    for tf in tfs:
        df = load_df(dh, pair, tf, ct)
        if df is None:
            return None
        firsts.append(df["date"].iloc[0].to_pydatetime())
        ends.append(df["date"].iloc[-1].to_pydatetime() + timedelta(seconds=tf_sec(tf)))
    return max(firsts), min(ends)


def ceil_day(dt):
    d = dt.date()
    return d if dt == datetime(d.year, d.month, d.day, tzinfo=dt.tzinfo) else d + timedelta(days=1)


def d_(text):
    return date.fromisoformat(str(text)[:10])


def utc0(d):
    return datetime(d.year, d.month, d.day, tzinfo=timezone.utc)


def order_like(pairs, whitelist, btc_last):
    """按白名单的顺序排；btc_last = BTC 放最后（对冲腿 / 白名单本来就把 BTC 放最后）。"""
    pos = {p: i for i, p in enumerate(whitelist)}
    out_ = sorted(dict.fromkeys(pairs), key=lambda p: (pos.get(p, len(pos)), p))
    if btc_last and BTC in out_:
        out_ = [p for p in out_ if p != BTC] + [BTC]
    return out_


# ----------------------------------------------------------------------------
def cmd_expand(argv):
    cfg_path, datadir_arg, run_dir = argv[:3]
    run_dir = Path(run_dir)
    try:
        cfg = load_cfg(cfg_path)
    except Exception as exc:  # noqa: BLE001 — 配置读不了：不展开，交给后面的数据检查报错
        print(f"（读配置 {cfg_path} 出错，不展开白名单：{exc.__class__.__name__}: {exc}）", file=sys.stderr)
        return 0
    wl = list((cfg.get("exchange") or {}).get("pair_whitelist") or [])
    if not any(REGEX_CHARS.search(p) for p in wl):
        return 0
    from freqtrade.enums import CandleType, TradingMode
    from freqtrade.plugins.pairlist.pairlist_helpers import expand_pairlist

    datadir = cfg_datadir(cfg, datadir_arg)
    fmt = str(cfg.get("dataformat_ohlcv") or "feather")
    mode = TradingMode(str(cfg.get("trading_mode") or "spot"))
    want = CandleType.FUTURES if mode == TradingMode.FUTURES else CandleType.SPOT
    dh = handler(datadir, fmt)
    avail = sorted({p for p, _tf, ct in dh.ohlcv_get_available_data(datadir, mode) if ct == want})
    try:
        expanded = list(dict.fromkeys(expand_pairlist(wl, avail, keep_invalid=True)))
    except ValueError as exc:
        print(f"错误：白名单里的正则写错了：{exc}", file=sys.stderr)
        return 1
    expanded = [p for p in expanded if not REGEX_CHARS.search(p)]
    if not expanded:
        print(f"错误：白名单里的正则在 {datadir} 里一个有K线的合约都没匹配上（先下载数据）", file=sys.stderr)
        return 1
    frag = run_dir / "pairs.expanded.json"
    write_pairs_fragment(frag, expanded, "白名单里的正则按本地数据目录里有K线的合约展开（research.sh 自动生成，只给数据检查用）")
    wrapper = run_dir / "config.expanded.json"
    rel_cfg = os.path.relpath(Path(cfg_path).resolve(), run_dir.resolve())
    wrapper.write_text(
        json.dumps({"add_config_files": [rel_cfg, frag.name]}, ensure_ascii=False, indent=4) + "\n", encoding="utf-8"
    )
    regs = [p for p in wl if REGEX_CHARS.search(p)]
    print(f"白名单里有正则（{'、'.join(regs)}）：按本地数据目录 {datadir} 里有K线的合约展开成 {len(expanded)} 个交易对"
          + ("（BTC/USDT:USDT 在最后）" if expanded[-1] == BTC else "")
          + "。freqtrade 回测时只按交易所现在还有的合约展开正则（已经删掉的合约展不出来），"
          "所以研究脚本按本地数据展开、写进 pairs.effective.json。")
    return 0


# ----------------------------------------------------------------------------
def cmd_post(argv):
    import argparse

    ap = argparse.ArgumentParser(prog="post")
    ap.add_argument("--run-dir", required=True)
    ap.add_argument("--config", required=True)
    ap.add_argument("--keep-delisted", default="0")
    ap.add_argument("--spot", default="0")
    ap.add_argument("--funding", default="0")
    ap.add_argument("--pre-holdout", default="")
    ap.add_argument("--pre-holdout-pairs", default="")
    ap.add_argument("--listings", default="0")
    ap.add_argument("--listing-strategies", default="")
    ap.add_argument("--original-config", default="")
    a = ap.parse_args(argv)

    run_dir = Path(a.run_dir)
    plan_path = run_dir / "plan.json"
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    cfg = load_cfg(a.config)
    whitelist = [p for p in ((cfg.get("exchange") or {}).get("pair_whitelist") or []) if not REGEX_CHARS.search(p)]
    btc_last = bool(whitelist) and whitelist[-1] == BTC
    datadir = Path(plan["datadir"])
    fmt = str(plan.get("dataformat") or "feather")
    dh = handler(datadir, fmt)
    tfs = list(plan.get("timeframes") or [])
    S, E = d_(plan["data_range"][0]), d_(plan["data_range"][1])
    T0 = d_(plan["test"][0])
    strategies = plan.get("strategies") or {}
    w_train = max([int(v.get("warmup_days") or 0) for v in strategies.values()] or [0])
    min_train = int(plan.get("min_train_days") or 0)
    lines = []
    kept = list(plan.get("pairs") or [])
    dropped = dict(plan.get("pairs_dropped") or {})
    if a.original_config:
        # 数据检查用的是展开正则以后的配置（config.expanded.json）；plan.json 里记原来的研究配置
        plan["config_expanded"] = plan.get("config")
        plan["config"] = a.original_config
    readd_all = {}

    def tradable_window(p):
        sc = scan(dh, p, tfs)
        if sc is None:
            return None
        first, end = sc
        p_trade = max(S, ceil_day(first) + timedelta(days=w_train))
        last_day = (end - timedelta(seconds=1)).date()
        return first, last_day, p_trade

    # ---- 0. RESEARCH_MIN_TRAIN_DAYS=0：样本外开始以后才上线的币也保留（预注册 3.3 节"运行 B"）----
    if min_train == 0:
        late = [p for p, why in dropped.items() if str(why).startswith("上线太晚")]
        readd, never = {}, []
        for p in late:
            tw = tradable_window(p)
            if tw is None:
                continue
            first, last_day, p_trade = tw
            if p_trade <= min(last_day, E):
                readd[p] = {"first": first.date().isoformat(), "trade_from": p_trade.isoformat()}
            else:
                never.append(p)
        if readd:
            out(lines, f"RESEARCH_MIN_TRAIN_DAYS=0：不要求样本外开始前有能交易的历史，样本外开始（{T0}）以后才上线的 "
                f"{len(readd)} 个币也保留（它们在训练段里不交易）：" + " ".join(f"{base(p)}({v['first']} 起)" for p, v in readd.items()))
            for p in readd:
                dropped.pop(p, None)
            readd_all.update(readd)
        if never:
            out(lines, f"  研究区间结束前一天都不能交易的（第一根K线 + 预热 {w_train} 天之后数据已经结束）照样不用：{' '.join(base(p) for p in never)}")
        plan["min_train_zero"] = {"kept_late": readd, "never_tradable": never}

    # ---- 1. 保留下架的币（RESEARCH_KEEP_DELISTED=1）----
    if a.keep_delisted == "1":
        if lines:
            out(lines)
        cands = [p for p, why in dropped.items() if str(why).startswith("数据中途断了")]
        readd, still = {}, {}
        for p in cands:
            tw = tradable_window(p)
            if tw is None:
                still[p] = "数据读不出来"
                continue
            first, last_day, p_trade = tw
            if p_trade > min(last_day, E):
                still[p] = f"停止交易前一天都不能交易（第一根K线 {first.date()} + 预热 {w_train} 天之后，最后一天是 {last_day}）"
            elif min_train > 0 and (T0 - p_trade).days < min_train:
                still[p] = f"上线太晚：样本外开始（{T0}）前能交易的时间不到 {min_train} 天"
            else:
                readd[p] = {"first": first.date().isoformat(), "last": last_day.isoformat(), "trade_from": p_trade.isoformat()}
        out(lines, "保留中途停止交易的币（RESEARCH_KEEP_DELISTED=1）：")
        if not cands:
            out(lines, "  没有中途停止交易的币。")
        if readd:
            for p in readd:
                dropped.pop(p, None)
            readd_all.update(readd)
            out(lines, f"  {len(readd)} 个中途停止交易（可能已下架）的币照样参加回测（不再因为\"数据中途断了\"去掉）："
                + " ".join(f"{base(p)}({v['first']} ~ {v['last']})" for p, v in readd.items()))
            out(lines, "  注意（freqtrade 2026.10 optimize/backtesting.py 的做法）：一个币没有K线以后，freqtrade 不再检查它的离场条件和止损；")
            out(lines, "    还开着的仓位一直占着名额和保证金，到这一段回测结束时才按 force_exit 平掉：平仓价 = 这个币最后一根K线的开盘价，")
            out(lines, "    平仓时间记为那根K线的时间。真实的交易所下架时会按结算价强制平仓，所以这只是近似。")
        for p, why in still.items():
            dropped[p] = f"{dropped.get(p, '数据中途断了')}；保留下架币也用不上：{why}"
        if still:
            out(lines, f"  另外 {len(still)} 个停止交易的币用不上：" + "；".join(f"{base(p)}：{w}" for p, w in still.items()))
        plan["keep_delisted"] = {"enabled": True, "kept": readd, "not_usable": still}

    if readd_all:
        cfg_order = list(plan.get("pairs") or []) + list(readd_all)
        kept = order_like(cfg_order, whitelist or cfg_order, btc_last)
        plan["pairs"] = kept
        plan["pairs_dropped"] = dropped
        write_pairs_fragment(run_dir / "pairs.effective.json", kept, EFFECTIVE_NOTE)
        set_sh(run_dir / "plan.sh", "PAIR_COUNT", len(kept))
        out(lines, f"  现在实际用 {len(kept)} 个交易对（pairs.effective.json 已更新）")
    elif "keep_delisted" in plan or "min_train_zero" in plan:
        plan["pairs_dropped"] = dropped

    # ---- 2. 现货覆盖（PremiumFade：现货和永续 1h 时间戳能对上的比例，预注册 3.2 节）----
    if a.spot == "1":
        if lines:
            out(lines)
        t0, t1, tt = utc0(S), utc0(E) + timedelta(days=1), utc0(T0)
        cov = {}
        for p in kept:
            spot_pair = p.split(":", 1)[0]
            perp = load_df(dh, p, "1h", "futures")
            spot = load_df(dh, spot_pair, "1h", "spot")
            rec = {"spot_pair": spot_pair, "spot_first": None, "spot_last": None, "perp_first": None,
                   "perp_hours": 0, "matched_hours": 0, "share": None, "share_train": None, "share_test": None}
            if perp is not None:
                pdt = perp["date"]
                pdt = pdt[(pdt >= t0) & (pdt < t1)]
                rec["perp_first"] = perp["date"].iloc[0].date().isoformat()
                rec["perp_hours"] = int(len(pdt))
                if spot is not None:
                    rec["spot_first"] = spot["date"].iloc[0].date().isoformat()
                    rec["spot_last"] = spot["date"].iloc[-1].date().isoformat()
                    hit = pdt.isin(set(spot["date"]))
                    rec["matched_hours"] = int(hit.sum())
                    if len(pdt):
                        rec["share"] = float(hit.mean())
                        tr = (pdt < tt).to_numpy()
                        rec["share_train"] = float(hit[tr].mean()) if tr.any() else None
                        rec["share_test"] = float(hit[~tr].mean()) if (~tr).any() else None
                elif len(pdt):
                    rec["share"] = 0.0
            cov[p] = rec
        with_spot = [p for p, r in cov.items() if r["spot_first"]]
        out(lines, f"现货 1h 和永续 1h 的时间戳覆盖（PremiumFade 用；同一个时间戳才算对上，不向前填充；区间 {S} ~ {E}）：")
        out(lines, f"  有现货数据的币 {len(with_spot)}/{len(kept)} 个（预注册 2.2 节：PremiumFade 能交易的币少于 20 个记为\"测不了\"）")
        no_spot = [p for p in kept if p not in with_spot]
        if no_spot:
            out(lines, f"  没有现货数据（PremiumFade 不会交易这些币）：{' '.join(base(p) for p in no_spot)}")
            out(lines, "    下载现货：bash scripts/download_okx_data.sh --spot-only --config <研究配置> --timerange <开始日期>-"
                "（研究带 PremiumFade 时第 1 步自动下，或者 RESEARCH_WITH_SPOT=1）")
        rows = sorted((r["share"], p) for p, r in cov.items() if r["spot_first"] and r["share"] is not None)
        if rows:
            shares = [s for s, _ in rows]
            mid = shares[len(shares) // 2]
            out(lines, f"  对上的比例：中位数 {mid:.1%}，最低 {rows[0][0]:.1%}（{base(rows[0][1])}）")
            for s, p in rows:
                r = cov[p]
                if s < 0.999:
                    tr_txt = f"训练段 {r['share_train']:.1%}" if r["share_train"] is not None else "训练段 -"
                    te_txt = f"样本外 {r['share_test']:.1%}" if r["share_test"] is not None else "样本外 -"
                    out(lines, f"    {base(p):<8} {s:.1%}（{tr_txt}，{te_txt}；永续从 {r['perp_first']}、现货从 {r['spot_first']} 起）")
            if all(s >= 0.999 for s in shares):
                out(lines, "    有现货数据的币全部对上 99.9% 以上")
        plan["spot_coverage"] = {"timeframe": "1h", "range": [S.isoformat(), E.isoformat()],
                                 "pairs_with_spot": len(with_spot), "pairs_total": len(kept), "pairs": cov}

    # ---- 3. 资金费 / 标记价格覆盖（XSFundingCarry：每个币的资金费条数、起始日期、和标记价格对上的比例，预注册 3.2 节）----
    if a.funding == "1" and str(plan.get("trading_mode")) == "futures":
        import freqtrade.exchange as fe

        name = str(plan.get("exchange") or "").lower()
        cls = getattr(fe, name.title(), None) or fe.Exchange
        has = {**fe.Exchange._ft_has_default, **getattr(cls, "_ft_has", {}), **getattr(cls, "_ft_has_futures", {})}
        mark_tf = str(has.get("mark_ohlcv_timeframe") or "1h")
        fund_tf = str(has.get("funding_fee_timeframe") or "1h")
        mark_ct = str(has.get("mark_ohlcv_price") or "mark")
        if lines:
            out(lines)
        fc = {}
        t0 = utc0(S)
        for p in kept:
            fr = load_df(dh, p, fund_tf, "funding_rate")
            mk = load_df(dh, p, mark_tf, mark_ct)
            rec = {"rows": 0, "first": None, "last": None, "interval_hours": None, "rows_in_range": 0,
                   "mark_first": mk["date"].iloc[0].date().isoformat() if mk is not None else None, "mark_match_share": None}
            if fr is not None:
                dts = fr["date"]
                rec["rows"] = int(len(dts))
                rec["first"] = dts.iloc[0].date().isoformat()
                rec["last"] = dts.iloc[-1].date().isoformat()
                if len(dts) > 1:
                    rec["interval_hours"] = float(dts.diff().dt.total_seconds().median() / 3600)
                inr = dts[dts >= t0]
                rec["rows_in_range"] = int(len(inr))
                if len(inr):
                    rec["mark_match_share"] = float(inr.isin(set(mk["date"])).mean()) if mk is not None else 0.0
            fc[p] = rec
        out(lines, f"资金费率 / 标记价格（资金费每次结算一行，标记价格 {mark_tf}；freqtrade 按\"标记价格 x 资金费率\"算 funding_fees，"
            "两种数据缺一种的时间段资金费按 0 算）：")
        have = [p for p, r in fc.items() if r["rows"]]
        out(lines, f"  有资金费数据的币 {len(have)}/{len(kept)} 个")
        miss = [p for p in kept if p not in have]
        if miss:
            out(lines, f"  没有资金费数据：{' '.join(base(p) for p in miss)}")
        for p in have:
            r = fc[p]
            iv = f"中位间隔 {r['interval_hours']:g} 小时" if r["interval_hours"] else "-"
            mm = f"{r['mark_match_share']:.1%}" if r["mark_match_share"] is not None else "-"
            out(lines, f"    {base(p):<8} {r['rows']:>6} 条，{r['first']} ~ {r['last']}，{iv}；"
                f"研究区间内 {r['rows_in_range']} 条，其中同一时刻有标记价格的 {mm}")
        plan["funding_coverage"] = {"funding_timeframe": fund_tf, "mark_timeframe": mark_tf, "pairs": fc}

    # ---- 4. 研究开始之前的留出段（RESEARCH_PRE_HOLDOUT，预注册 3.3 节"2020 留出段"）----
    if a.pre_holdout:
        hs_txt, he_txt = a.pre_holdout.split("-", 1)
        try:
            hs = datetime.strptime(hs_txt, "%Y%m%d").date()
            he = datetime.strptime(he_txt, "%Y%m%d").date()
        except ValueError:
            print(f"错误：RESEARCH_PRE_HOLDOUT 的日期不对（{a.pre_holdout}）", file=sys.stderr)
            return 1
        h0, h1 = utc0(hs), utc0(he) + timedelta(days=1)
        if lines:
            out(lines)
        listing_strats = set(a.listing_strategies.split())
        env_pairs = []
        for x in re.split(r"[\s,]+", a.pre_holdout_pairs.strip()):
            if x:
                env_pairs.append(x if "/" in x else f"{x.upper()}/USDT:USDT")
        env_pairs = list(dict.fromkeys(env_pairs))
        cands = env_pairs or whitelist
        # 每个币各周期在留出段里的数据情况
        full, overlap, why_not, cat_of = {}, {}, {}, {}
        for p in cands:
            first_all, ok_full, ok_overlap, bad, cat = None, True, True, "", ""
            for tf in tfs:
                df = load_df(dh, p, tf, "futures")
                if df is None:
                    bad, cat, ok_full, ok_overlap = f"没有 {tf} 数据", "没有数据", False, False
                    break
                first = df["date"].iloc[0].to_pydatetime()
                end = df["date"].iloc[-1].to_pydatetime() + timedelta(seconds=tf_sec(tf))
                first_all = first if first_all is None else max(first_all, first)
                n_in = int(((df["date"] >= h0) & (df["date"] < h1)).sum())
                if first >= h1 or end <= h0:
                    ok_full = ok_overlap = False
                    bad = f"留出段里没有 {tf} 数据（{first.date()} ~ {(end - timedelta(seconds=1)).date()}）"
                    cat = "留出段之后才有数据" if first >= h1 else "留出段开始前就停了"
                    break
                if first > h0:
                    ok_full = False
                    bad, cat = bad or f"{tf} 数据 {first.date()} 才开始", cat or "留出段里才开始有数据"
                elif end < h1:
                    ok_full = False
                    bad, cat = bad or f"{tf} 数据 {(end - timedelta(seconds=1)).date()} 就停了", cat or "留出段里停止交易"
                elif n_in < 0.9 * (h1 - h0).total_seconds() / tf_sec(tf):
                    ok_full = False
                    bad, cat = bad or f"{tf} 数据在留出段里缺了 10% 以上的K线", cat or "缺口太多"
            if ok_full:
                full[p] = first_all
            if ok_overlap:
                overlap[p] = first_all
            if bad:
                why_not[p] = bad
                cat_of[p] = cat
        per = {}
        warm = {n: int(v.get("warmup_days") or 0) for n, v in strategies.items()}
        ph_dir = run_dir / "preholdout"
        pre_sh = ph_dir / "plan.sh"
        set_sh(pre_sh, "PRE_HOLDOUT_TR", a.pre_holdout)
        out(lines, f"研究开始之前的留出段（RESEARCH_PRE_HOLDOUT={a.pre_holdout}，预注册 3.3 节\"2020 留出段\"）：")
        out(lines, f"  主运行之后每个策略只用默认参数回测一次 {hs} ~ {he}；更早的数据只用来预热指标（预热 = 策略自己的 startup_candle_count），"
            "回撤口径和 scripts/check_drawdown.py 相同；交易至少 30 笔时必须赚钱、回撤不超过上限，不足 30 笔只报告。")
        for n in strategies:
            # 规格 3.3 节："2020 年就有数据的大币，以实际数据为准" / "用币安 2020 年的数据" / "币安 2020 年上线的币"：
            # 都按 "留出段里有数据" 选（研究配置里的币本来就是大币）。上线晚的币由策略自己的预热和合格条件挡住
            # （例如 XSFundingCarry 的 60 根日线 + 7 天资金费），不会用到留出段以外的数据。
            if env_pairs:
                rule, pool = "env", overlap
                how = "RESEARCH_PRE_HOLDOUT_PAIRS 指定的币里留出段有数据的"
            elif n in listing_strats:
                rule, pool = "listed", overlap
                how = "留出段里有数据的币（包括留出段里才上线、中途停止交易的；新币策略要的就是这些）"
            else:
                rule, pool = "overlap", overlap
                how = "留出段里有数据的币（以实际数据为准；留出段里才上线的币要等策略自己的预热过了才交易）"
            chosen = order_like(list(pool), cands, btc_last or n in listing_strats)
            frag = ph_dir / f"{n}.json"
            write_pairs_fragment(frag, chosen, f"{n} 在留出段 {hs} ~ {he} 用的交易对（research.sh 自动生成，不要手改）")
            wd = warm.get(n, 0)
            late = {p: max(hs, ceil_day(f) + timedelta(days=wd)).isoformat() for p, f in pool.items()
                    if ceil_day(f) + timedelta(days=wd) > hs}
            per[n] = {"rule": rule, "pairs": chosen, "count": len(chosen), "fragment": str(frag),
                      "warmup_days": wd, "trade_from_late": late}
            set_sh(pre_sh, f"PH_{n}_COUNT", len(chosen))
            out(lines, f"  {n}：{how} {len(chosen)} 个：{' '.join(base(p) for p in chosen) or '没有'}")
            if late:
                out(lines, f"    预热 {wd} 天：这些币留出段开头还在预热，从括号里的日期才开始交易："
                    + " ".join(f"{base(p)}({d})" for p, d in sorted(late.items(), key=lambda x: x[1])[:12])
                    + (" ..." if len(late) > 12 else ""))
            if chosen and BTC not in chosen and (n in listing_strats or BTC in whitelist):
                out(lines, f"    !! BTC 不在 {n} 的留出段交易对里（用 BTC 判断大盘 / 做对冲的策略会出问题）")
            if not chosen:
                out(lines, f"    !! {n} 在留出段没有能用的币，跳过这一次回测")
            elif len(chosen) < MIN_POOL.get(n, 0):
                out(lines, f"    !! {n} 在留出段只有 {len(chosen)} 个币，少于它自己开仓要求的 {MIN_POOL[n]} 个合格的币："
                    "这一段一笔都不会开，2020 留出段的结果只会是\"不足 30 笔只报告\"")
        excl = {p: w for p, w in why_not.items()}
        if excl:
            groups = {}
            for p in excl:
                groups.setdefault(cat_of.get(p) or "其他", []).append(base(p))
            out(lines, f"  数据没有覆盖整个留出段的 {len(excl)} 个币（只是列出来：留出段里有数据的照样参加；每个币的原因在 plan.json 的 pre_holdout_info）：")
            for c, names in groups.items():
                out(lines, f"    {c}（{len(names)} 个）：{' '.join(names[:15])}{' ...' if len(names) > 15 else ''}")
        plan["pre_holdout"] = [hs.isoformat(), he.isoformat()]
        plan["pre_holdout_info"] = {
            "range": [hs.isoformat(), he.isoformat()],
            "timerange": a.pre_holdout,
            "segment": "preholdout",
            "variant": "default",
            "pairs_source": "RESEARCH_PRE_HOLDOUT_PAIRS" if env_pairs else "auto",
            "pairs_requested": env_pairs,
            "pairs_not_covering": excl,
            "strategies": per,
            "judge": {"min_trades": int((plan.get("settings") or {}).get("min_trades") or 30),
                      "rule": "trades >= min_trades: profit > 0 and account DD (incl. unrealised) <= max_dd; else report only"},
        }

    # ---- 5. 新币上线日文件的数据起点（NewListingShortHedged：BTC 第一根日K线）----
    if a.listings == "1":
        btc = load_df(dh, BTC, "1d", "futures")
        start = btc["date"].iloc[0].date().strftime("%Y%m%d") if btc is not None else ""
        set_sh(run_dir / "plan.sh", "LISTINGS_DATA_START", start)

    save_json(plan_path, plan)
    append_check(run_dir, lines)
    return 0


# ----------------------------------------------------------------------------
def cmd_preflight(argv):
    """按回测的方式加载交易所（真实运行要联网；合成数据用 scripts/ft_offline.py 的离线规格），
    去掉 freqtrade 回测时会报错或自动去掉的币：交易对规格里没有、标成不活跃（StaticPairList 没写 allow_inactive）、没有杠杆档位。"""
    import argparse

    ap = argparse.ArgumentParser(prog="preflight")
    ap.add_argument("--run-dir", required=True)
    ap.add_argument("--config", required=True)
    ap.add_argument("--synthetic", default="0")
    a = ap.parse_args(argv)
    run_dir = Path(a.run_dir)
    plan_path = run_dir / "plan.json"
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    info = plan.get("pre_holdout_info") or {}
    per = info.get("strategies") or {}
    main_pairs = list(plan.get("pairs") or [])
    allp = list(dict.fromkeys(main_pairs + [p for v in per.values() for p in v.get("pairs") or []]))
    lines = []
    if not allp:
        return 0
    try:
        if a.synthetic == "1":
            sys.path.insert(0, str(Path("scripts").resolve()))
            import ft_offline  # noqa: PLC0415 — 只用它的离线补丁（假的交易对规格 + 杠杆档位）

            ft_offline.install_patches()
        from freqtrade.configuration import Configuration
        from freqtrade.enums import RunMode
        from freqtrade.exchange import market_is_active
        from freqtrade.resolvers import ExchangeResolver

        cfg = Configuration({"config": [a.config], "datadir": plan["datadir"]}, RunMode.BACKTEST).get_config()
        cfg["exchange"]["pair_whitelist"] = allp
        ex = ExchangeResolver.load_exchange(cfg, validate=False, load_leverage_tiers=True)
        markets = ex.markets
        allow_inactive = any(pl.get("method") == "StaticPairList" and pl.get("allow_inactive")
                             for pl in cfg.get("pairlists") or [])
        futures = str(cfg.get("trading_mode")) == "futures"
        bad = {}
        for p in allp:
            m = markets.get(p)
            if m is None:
                bad[p] = "交易所的交易对规格里没有这个合约（多半已经下架删掉了），freqtrade 回测拿不到它的规格和杠杆档位"
            elif not ex.market_is_tradable(m):
                bad[p] = "交易所说 freqtrade 不能交易这个合约"
            elif not allow_inactive and not market_is_active(m):
                bad[p] = "交易所标成不活跃，配置的 StaticPairList 没写 allow_inactive，freqtrade 回测时会自动去掉"
            elif futures and p not in ex._leverage_tiers:
                bad[p] = "没有杠杆档位，freqtrade 回测会整次报错退出（Pairs ... got no leverage tiers available）"
        try:
            ex.close()
        except Exception:  # noqa: BLE001
            pass
    except Exception as exc:  # noqa: BLE001 — 连不上交易所：不检查，照常继续
        out(lines, f"!! 核对交易所的交易对规格没做成（{exc.__class__.__name__}: {str(exc)[:200]}），这一步不检查。"
            "回测如果报 \"got no leverage tiers available\"，把报出来的币从配置 / RESEARCH_PRE_HOLDOUT_PAIRS 去掉再跑。")
        append_check(run_dir, lines)
        return 0
    out(lines, "核对交易所的交易对规格和杠杆档位（freqtrade 回测时没有杠杆档位的币会让整次回测报错退出）：")
    if not bad:
        out(lines, f"  {len(allp)} 个币都有规格和杠杆档位")
    else:
        for p, why in bad.items():
            out(lines, f"  - {base(p)}：{why}，这次不用")
        if any(p in bad for p in main_pairs):
            kept = [p for p in main_pairs if p not in bad]
            if not kept:
                print("错误：核对交易所规格以后一个能回测的交易对都没有了", file=sys.stderr)
                append_check(run_dir, lines)
                return 1
            plan["pairs"] = kept
            for p in main_pairs:
                if p in bad:
                    plan.setdefault("pairs_dropped", {})[p] = bad[p]
            write_pairs_fragment(run_dir / "pairs.effective.json", kept, EFFECTIVE_NOTE)
            set_sh(run_dir / "plan.sh", "PAIR_COUNT", len(kept))
            out(lines, f"  主运行现在实际用 {len(kept)} 个交易对（pairs.effective.json 已更新）")
            for key in ("keep_delisted", "min_train_zero"):
                kd = plan.get(key) or {}
                for p in list((kd.get("kept") or kd.get("kept_late") or {}).keys()):
                    if p in bad:
                        kd.setdefault("excluded_by_exchange", {})[p] = bad[p]
        for n, v in per.items():
            if any(p in bad for p in v.get("pairs") or []):
                v["excluded_by_exchange"] = {p: bad[p] for p in v["pairs"] if p in bad}
                v["pairs"] = [p for p in v["pairs"] if p not in bad]
                v["count"] = len(v["pairs"])
                write_pairs_fragment(Path(v["fragment"]), v["pairs"], f"{n} 在留出段用的交易对（research.sh 自动生成，不要手改）")
                set_sh(run_dir / "preholdout" / "plan.sh", f"PH_{n}_COUNT", v["count"])
    plan["exchange_preflight"] = {"checked": allp, "excluded": bad}
    save_json(plan_path, plan)
    append_check(run_dir, lines)
    return 0


def main():
    cmd, rest = sys.argv[1], sys.argv[2:]
    return {"expand": cmd_expand, "post": cmd_post, "preflight": cmd_preflight}[cmd](rest)


sys.exit(main())
PYEOF

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
    SYN_MARKER="$DATADIR_ARG/SYNTHETIC_DATA_README.txt"
    SYN_DEFAULT_DIR=0
    case "$DATADIR_ARG" in user_data/data/okx-synthetic|user_data/data/binance-synthetic) SYN_DEFAULT_DIR=1 ;; esac
    # 第 5 轮的新策略 / 开关要用新版合成数据（现货、新币、下架的币、暴跌日、币安完整资金费；SYNTHETIC_FORMAT_VERSION=2）
    SYN_NEED_V2=0
    for s in PremiumFade XSFundingCarry CapitulationRebound NewListingShortHedged; do
        if has_strategy "$s"; then SYN_NEED_V2=1; fi
    done
    if [[ "$WITH_SPOT" == "1" || "$KEEP_DELISTED" == "1" || -n "$PRE_HOLDOUT" || "$LISTINGS" == "1" ]]; then SYN_NEED_V2=1; fi
    if [[ ! -f "$SYN_MARKER" ]]; then
        if [[ "$SYN_DEFAULT_DIR" == "1" ]]; then
            say "还没有合成数据，先生成（scripts/make_synthetic_data.py ${SYN_GEN_ARGS[*]+${SYN_GEN_ARGS[*]}}）..."
            py_ft scripts/make_synthetic_data.py ${SYN_GEN_ARGS[@]+"${SYN_GEN_ARGS[@]}"} 2>&1 | tee -a "$RUN_LOG" || { say "生成合成数据失败"; exit 1; }
        else
            bad_arg "$DATADIR_ARG 不是合成数据目录（没有 SYNTHETIC_DATA_README.txt）"
        fi
    elif [[ "$SYN_NEED_V2" == "1" ]] && ! grep -q '^SYNTHETIC_FORMAT_VERSION=2' "$SYN_MARKER" 2> /dev/null; then
        if [[ "$SYN_DEFAULT_DIR" == "1" ]]; then
            say "合成数据是旧版的（第 5 轮以前生成的，没有现货/新币/下架的币/暴跌日），重新生成（scripts/make_synthetic_data.py）..."
            py_ft scripts/make_synthetic_data.py ${SYN_GEN_ARGS[@]+"${SYN_GEN_ARGS[@]}"} 2>&1 | tee -a "$RUN_LOG" || { say "生成合成数据失败"; exit 1; }
        else
            say "!! $DATADIR_ARG 是旧版合成数据（没有 SYNTHETIC_FORMAT_VERSION=2）：第 5 轮的新策略可能没有现货/新币数据。"
            say "   重新生成：.venv/bin/python scripts/make_synthetic_data.py --out $DATADIR_ARG"
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
R5_TXT=""
if [[ "$WITH_SPOT" == "1" ]]; then R5_TXT="${R5_TXT}现货下载+覆盖检查；"; fi
if [[ "$KEEP_DELISTED" == "1" ]]; then R5_TXT="${R5_TXT}保留中途停止交易的币；"; fi
if [[ "$LISTINGS" == "1" ]]; then R5_TXT="${R5_TXT}生成新币上线日文件；"; fi
if [[ "$FUNDING_CHECK" == "1" ]]; then R5_TXT="${R5_TXT}资金费覆盖检查；"; fi
if [[ -n "$PRE_HOLDOUT" ]]; then R5_TXT="${R5_TXT}研究开始之前的留出段 $PRE_HOLDOUT${PRE_HOLDOUT_PAIRS:+（币：$PRE_HOLDOUT_PAIRS）}；"; fi
if [[ "$ALLOW_UNREG" == "1" ]]; then R5_TXT="${R5_TXT}!!和登记表不一致也照样跑（不算正式运行）；"; fi
if [[ "$DATA_CHECK_ONLY" == "1" ]]; then R5_TXT="${R5_TXT}只做数据检查（管道检查），不回测、不是正式运行；"; fi
if [[ -n "$R5_TXT" ]]; then say "第 5 轮的开关：${R5_TXT%；}"; fi
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

# ---------- 0. 预注册检查（任何下载、回测之前）----------
# scripts/check_prereg.py：登记过的策略文件 sha256 必须和 configs/preregistration.json 一样，否则停止（退出码 4）；
# 没登记的照常研究。只用 Python 标准库，在本机跑（要读本机的 git 提交号）。
# 用法：prereg_check <策略目录> <策略列表>；结果写进 结果目录/plan.json 的 "prereg"（plan.json 还没生成时先写 prereg.json）
# 第 3 个参数 quiet = 通过时只写进 research.log，不打印到屏幕
# 这次的运行设置（和登记表的 run_settings 对照：手续费、回撤上限、通过标准、切分、预热、保留下架币、留出段；不一样 = 不是正式运行）
PREREG_SETTINGS="$(printf '{"fee": "%s", "max_dd": "%s", "min_trades": "%s", "pf_ratio": "%s", "min_pf": "%s", "start": "%s", "seen_from": "%s", "wf_folds": "%s", "wf_train_days": "%s", "wf_test_days": "%s", "wf_mode": "%s", "hyperopt": "%s", "detail": "%s", "min_train_days": "%s", "warmup_candles": "%s", "keep_delisted": "%s", "pre_holdout": "%s", "pre_holdout_pairs": "%s"}' \
    "$FEE" "$MAX_DD" "$MIN_TRADES" "$PF_RATIO" "$MIN_PF" "$START" "$SEEN_FROM" "$FOLDS" "$WF_TRAIN_DAYS" "$WF_TEST_DAYS" \
    "$WF_MODE" "$HYPEROPT" "$DETAIL" "$MIN_TRAIN_DAYS" "$WARMUP" "$KEEP_DELISTED" "$PRE_HOLDOUT" "$PRE_HOLDOUT_PAIRS")"
prereg_check() {
    local args=(scripts/check_prereg.py check --strategies "$2" --strategy-dir "$1" --registry "$PREREG"
                --config "$CONFIG" --run-dir "$RUN_DIR" --settings "$PREREG_SETTINGS")
    local rc=0 out=""
    if [[ "$ALLOW_UNREG" == "1" ]]; then args+=(--allow-unregistered); fi
    if [[ "$SYNTHETIC" == "yes" ]]; then args+=(--synthetic); fi
    if [[ "$DATA_CHECK_ONLY" == "1" ]]; then args+=(--pipeline-only); fi
    # 第二次检查（第一次回测之前）：正式运行记进 user_data/research/formal_runs.jsonl（同一个类 + 阶段以后再跑 = 重复运行）
    if [[ "${3:-}" == "quiet" ]]; then args+=(--record-formal); fi
    set +e
    out="$(plain_py "${args[@]}" 2>&1)"
    rc=$?
    set -e
    if [[ "${3:-}" == "quiet" && $rc -eq 0 ]]; then
        printf '%s\n' "$out" | sed 's/^/    /' >> "$RUN_LOG"
    else
        printf '%s\n' "$out" | sed 's/^/    /' | tee -a "$RUN_LOG"
    fi
    if [[ $rc -eq 3 ]]; then
        say "!! 预注册检查不通过，研究停止，一次回测都没有跑（详情：$RUN_DIR/prereg.json 或 plan.json 的 prereg）。"
        exit 4
    elif [[ $rc -ne 0 ]]; then
        say "!! 预注册检查出错（退出码 $rc，上面有原因），研究停止。"
        exit 4
    fi
}
say "=== 0/5 预注册检查（登记表 $PREREG）"
prereg_check "$STRAT_SRC_DIR" "$STRATEGIES"
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
    if [[ -n "$PRE_HOLDOUT" ]]; then
        # 研究开始之前的留出段：从"留出段开始 - 策略自己的预热"开始下（比上面更早时）
        PH_DL_START="$(py_ft scripts/research_report.py timeframes --strategies "$STRATEGIES" --strategy-dir "$STRAT_SRC_DIR" \
            --timeframes "$TIMEFRAMES" --detail "$DETAIL" --print download-start --start "${PRE_HOLDOUT%%-*}" --warmup-candles 0 2> /dev/null | tail -n 1 || true)"
        [[ "$PH_DL_START" =~ ^[0-9]{8}$ ]] || PH_DL_START="${PRE_HOLDOUT%%-*}"
        if [[ "$PH_DL_START" < "$DL_START" ]]; then DL_START="$PH_DL_START"; fi
    fi
    if [[ "$DL_START" != "$START" ]]; then
        say "=== 1/5 下载/更新数据：周期 $DL_TFS，从 $DL_START 开始（$(elapsed)）"
        say "    研究从 $START 开始；前面多下的这段只用来给指标预热（交易所没有那么早的数据就从有数据的那天开始）"
        if [[ -n "$PRE_HOLDOUT" ]]; then
            say "    （还包括研究开始之前的留出段 $PRE_HOLDOUT 和它的预热；留出段只在主运行之后单独回测一次）"
        fi
    else
        say "=== 1/5 下载/更新数据：周期 $DL_TFS，从 $START 开始（$(elapsed)）"
    fi
    if [[ "$WITH_SPOT" == "1" ]]; then
        say "    另外下载现货 BASE/USDT 1h（--trading-mode spot，PremiumFade 要用；同一个开始日期）"
    fi
    say "    已有的数据只补新的；本地数据开始得比这个日期晚、又不是因为上线晚的币，会自动补前面那段（--prepend）。"
    say "    下载前会先估算要多久（参考：60 个币第一次从 2021 年起下载约 30~45 分钟，以后更新几分钟）。完整输出：$RUN_DIR/logs/download.log"
    DL_ARGS=(--config "$CONFIG" --timeframes "$DL_TFS" --timerange "${DL_START}-")
    if [[ -n "$DATADIR_ARG" ]]; then DL_ARGS+=(--datadir "$DATADIR_ARG"); fi
    if [[ "$WITH_SPOT" == "1" ]]; then DL_ARGS+=(--spot); fi
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
# 第 5 轮的开关也记进 plan.json
SETTINGS="${SETTINGS%\}}, \"keep_delisted\": $KEEP_DELISTED, \"with_spot\": $WITH_SPOT, \"listings\": $LISTINGS, \"funding_check\": $FUNDING_CHECK, \"pre_holdout\": \"$PRE_HOLDOUT\", \"pre_holdout_pairs\": \"$PRE_HOLDOUT_PAIRS\", \"allow_unregistered\": $ALLOW_UNREG, \"prereg_registry\": \"$PREREG\", \"data_check_only\": $DATA_CHECK_ONLY}"
# 白名单里有正则（例如 config.okx-research-listings.json 的"OKX 全部 USDT 永续"）：按本地有K线的合约展开，
# 数据检查用展开后的列表（结果目录的 config.expanded.json = 研究配置 + pairs.expanded.json）；没有正则时什么都不做
R5_LOG="$RUN_DIR/logs/round5_data_check.log"
PREP_CONFIG="$CONFIG"
EXPANDED=0
set +e
EXP_OUT="$(py_ft -c "$RS_PY" expand "$CONFIG" "$DATADIR_ARG" "$RUN_DIR" 2>> "$R5_LOG")"
EXP_RC=$?
set -e
if [[ $EXP_RC -ne 0 ]]; then
    say "    $EXP_OUT"
    tail -n 3 "$R5_LOG" | sed 's/^/    /' | tee -a "$RUN_LOG"
    say "数据检查失败：白名单里的正则没法按本地数据展开（完整日志：$R5_LOG）。"
    exit 1
fi
if [[ -f "$RUN_DIR/config.expanded.json" ]]; then
    PREP_CONFIG="$RUN_DIR/config.expanded.json"
    EXPANDED=1
    say "    $EXP_OUT"
fi
PREP_ARGS=(prepare --config "$PREP_CONFIG" --run-dir "$RUN_DIR" --strategies "$STRATEGIES" --strategy-dir "$STRAT_SRC_DIR"
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

# 第 5 轮的数据检查补充（只在打开对应开关时运行；都没打开时这一段什么都不做）：
#   保留中途停止交易的币、RESEARCH_MIN_TRAIN_DAYS=0 时保留样本外开始以后才上线的币、现货覆盖、资金费覆盖、留出段的币；
#   然后按回测的方式核对交易所的交易对规格和杠杆档位（没有杠杆档位的币会让整次回测报错退出），最后生成新币上线日文件
R5_POST=0
if [[ "$KEEP_DELISTED" == "1" || "$WITH_SPOT" == "1" || "$FUNDING_CHECK" == "1" || -n "$PRE_HOLDOUT" \
      || "$LISTINGS" == "1" || "$EXPANDED" == "1" || "$MIN_TRAIN_DAYS" =~ ^0+$ ]]; then R5_POST=1; fi
if [[ "$R5_POST" == "1" ]]; then
    POST_ARGS=(post --run-dir "$RUN_DIR" --config "$PREP_CONFIG" --keep-delisted "$KEEP_DELISTED" --spot "$WITH_SPOT"
               --funding "$FUNDING_CHECK" --listings "$LISTINGS" --pre-holdout "$PRE_HOLDOUT" --pre-holdout-pairs "$PRE_HOLDOUT_PAIRS")
    if [[ "$LISTINGS" == "1" ]]; then POST_ARGS+=(--listing-strategies "NewListingShortHedged"); fi
    if [[ "$EXPANDED" == "1" ]]; then POST_ARGS+=(--original-config "$CONFIG"); fi
    say ""
    if ! py_ft -c "$RS_PY" "${POST_ARGS[@]}" 2>> "$R5_LOG" | sed 's/^/    /' | tee -a "$RUN_LOG"; then
        tail -n 3 "$R5_LOG" | sed 's/^/    /' | tee -a "$RUN_LOG"
        say "数据检查失败（第 5 轮的补充检查，完整日志：$R5_LOG），研究没法继续。"
        exit 1
    fi
    if [[ "$KEEP_DELISTED" == "1" || "$EXPANDED" == "1" || -n "$PRE_HOLDOUT" ]]; then
        PF_ARGS=(preflight --run-dir "$RUN_DIR" --config "$PREP_CONFIG" --synthetic "$([[ "$SYNTHETIC" == "yes" ]] && echo 1 || echo 0)")
        say ""
        if ! py_ft -c "$RS_PY" "${PF_ARGS[@]}" 2>> "$R5_LOG" | sed 's/^/    /' | tee -a "$RUN_LOG"; then
            say "数据检查失败（核对交易所规格以后没有能回测的交易对，完整日志：$R5_LOG），研究没法继续。"
            exit 1
        fi
    fi
    # shellcheck disable=SC1090,SC1091
    source "$RUN_DIR/plan.sh"
fi
PRE_HOLDOUT_TR=""
if [[ -n "$PRE_HOLDOUT" && -f "$RUN_DIR/preholdout/plan.sh" ]]; then
    # shellcheck disable=SC1090,SC1091
    source "$RUN_DIR/preholdout/plan.sh"
fi

# 新币上线日文件（NewListingShortHedged）：扫描完整的本地 1d 数据，第一根日K线 = 上线日。
# 写到 user_data/listings/<数据目录名>_first_candle.json（策略默认读这个文件），副本和 sha256 记在结果目录
if [[ "$LISTINGS" == "1" ]]; then
    LISTINGS_FILE="user_data/listings/$(basename "$PLAN_DATADIR")_first_candle.json"
    BL_ARGS=(scripts/build_listings.py --datadir "$PLAN_DATADIR" --config "$CONFIG")
    LISTINGS_START_WHY="BTC 第一根日K线"
    # 这次下载的开始日期比 BTC 的第一根日K线晚时，用下载开始日期当数据起点：数据目录里的 BTC 可能是以前从更早开始下的
    # （例如运行 A 带 2020 留出段从 2019-11 起），而研究币池以外的老币这次才从 DL_START 开始下，它们的第一根K线 = DL_START，
    # 按 BTC 的日期算会被当成"DL_START 那天上线的新币"
    if [[ -n "${DL_START:-}" && -n "${LISTINGS_DATA_START:-}" && "$DL_START" > "$LISTINGS_DATA_START" ]]; then
        LISTINGS_DATA_START="$DL_START"
        LISTINGS_START_WHY="这次下载的开始日期（比 BTC 第一根日K线晚）"
    fi
    if [[ -n "${LISTINGS_DATA_START:-}" ]]; then BL_ARGS+=(--data-start "$LISTINGS_DATA_START"); fi
    say ""
    say "    新币上线日文件（scripts/build_listings.py，数据起点 ${LISTINGS_DATA_START:-自动} = ${LISTINGS_START_WHY}；完整输出：$RUN_DIR/logs/build_listings.log）："
    if ! py_ft "${BL_ARGS[@]}" > "$RUN_DIR/logs/build_listings.log" 2>&1 || [[ ! -f "$LISTINGS_FILE" ]]; then
        tail -n 8 "$RUN_DIR/logs/build_listings.log" | sed 's/^/      /' | tee -a "$RUN_LOG"
        say "!! 生成新币上线日文件失败（$LISTINGS_FILE），NewListingShortHedged 没法回测，研究停止。"
        exit 1
    fi
    { grep -v -E ' - (INFO|DEBUG) - ' "$RUN_DIR/logs/build_listings.log" || true; } | sed 's/^/      /' | tee -a "$RUN_LOG"
    mkd "$RUN_DIR/listings"
    cp "$LISTINGS_FILE" "$RUN_DIR/listings/"
    plain_py -c 'import hashlib, json, os, sys
plan_path, src, copy, start = sys.argv[1:5]
plan = json.load(open(plan_path, encoding="utf-8"))
plan["listings_file"] = {"file": src, "copy": copy, "data_start": start or None,
                         "sha256": hashlib.sha256(open(src, "rb").read()).hexdigest()}
tmp = plan_path + ".tmp"
with open(tmp, "w", encoding="utf-8") as fh:
    json.dump(plan, fh, ensure_ascii=False, indent=1)
os.replace(tmp, plan_path)' "$RUN_DIR/plan.json" "$LISTINGS_FILE" "$RUN_DIR/listings/$(basename "$LISTINGS_FILE")" "${LISTINGS_DATA_START:-}"
fi
for S in $PLAN_STRATEGIES; do
    fsvar="S_${S}_FILESTEM"
    if [[ -f "user_data/strategies/${!fsvar:-$S}.json" ]]; then
        say "提示：发现 user_data/strategies/${!fsvar:-$S}.json（以前 hyperopt 写的 $S 的参数）。研究一律用策略代码里的默认参数，不读、也不改这个文件。"
    fi
done
say ""

# 只做数据检查（--data-check-only，预注册 3.2 节"只看数据，不看收益"）：到这里为止，一次回测都不跑
if [[ "$DATA_CHECK_ONLY" == "1" ]]; then
    plain_py -c 'import json, os, sys
plan_path = sys.argv[1]
plan = json.load(open(plan_path, encoding="utf-8"))
plan["data_check_only"] = True
early = os.path.join(os.path.dirname(plan_path), "prereg.json")
if "prereg" not in plan and os.path.isfile(early):
    plan["prereg"] = json.load(open(early, encoding="utf-8"))  # 第 0 步预注册检查的完整结果
plan.setdefault("prereg", {})["formal_run"] = False
plan["prereg"]["formal_note"] = "只做数据检查（--data-check-only），没有回测，不是正式运行"
tmp = plan_path + ".tmp"
with open(tmp, "w", encoding="utf-8") as fh:
    json.dump(plan, fh, ensure_ascii=False, indent=1)
os.replace(tmp, plan_path)' "$RUN_DIR/plan.json"
    say "=== 只做数据检查（--data-check-only）：到此为止，一次回测都没有跑，也不出收益报告（$(elapsed)）"
    say "    数据检查的完整结果：$RUN_DIR/data_check.txt（上面也打印了）；plan.json 记为 data_check_only，不是正式运行。"
    say "    正式运行之前按预注册 3.2 节先看这些数（只看数量，不看收益）："
    if has_strategy NewListingShortHedged; then
        say "    - NewListingShortHedged：上面\"每年新上线的币\"（入场在这一年的）。明显凑不够每段 30 笔空单（例如 2021 ~ 2023 加起来远少于 30）时，直接记为\"测不了\"，不做正式运行。"
    fi
    if has_strategy XSFundingCarry; then
        say "    - XSFundingCarry：data_check.txt 里每个币的资金费条数、起止日期和\"同一时刻有标记价格\"的比例。大部分币研究区间里没有资金费、或者对不上标记价格时，"
        say "      回测里的 funding_fees 会是 0（\"无效\"），先修数据，不进入正式运行；再用 make pipeline-week 看一周里 funding_fees 不为 0 的笔数。"
    fi
    if has_strategy PremiumFade; then
        say "    - PremiumFade：data_check.txt 里\"有现货数据的币\"和现货 / 永续时间戳对上的比例。有现货数据的币少于 20 个时记为\"测不了\"。"
    fi
    say "    数据没问题以后，去掉 --data-check-only，用同样的命令做正式运行（每个规格只跑一次）。"
    exit 0
fi

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
# 预注册凭证写进 plan.json（核对的是回测真正用的副本 strategies/base/；第 0 步的结果另存为 plan.json 的 prereg_early）
prereg_check "$RUN_DIR/strategies/base" "$PLAN_STRATEGIES" quiet
say "预注册凭证（每个策略文件的 sha256、git 提交号）已写进 $RUN_DIR/plan.json 的 prereg（核对的是回测用的副本 strategies/base/）"
FORMAL_TXT="$(plain_py -c 'import json, sys
p = json.load(open(sys.argv[1], encoding="utf-8")).get("prereg") or {}
if any((v or {}).get("registered") for v in (p.get("strategies") or {}).values()):
    print("    这次是正式的预注册运行" if p.get("formal_run") else "    !! 不是正式的预注册运行：" + str(p.get("formal_note") or ""))' "$RUN_DIR/plan.json" 2> /dev/null || true)"
if [[ -n "$FORMAL_TXT" ]]; then say "$FORMAL_TXT"; fi
say ""

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

# ---------- 研究开始之前的留出段（RESEARCH_PRE_HOLDOUT，预注册 3.3 节）----------
# 主运行之后，每个策略只用默认参数回测一次这一段；交易对 = preholdout/<策略>.json（数据检查时定的），
# 预热 = 策略自己的 startup_candle_count（freqtrade 自动往前多加载，更早的数据只用来预热，不交易）
if [[ -n "$PRE_HOLDOUT" ]]; then
    say "=== 研究开始之前的留出段 ${PRE_HOLDOUT}：每个策略默认参数回测一次（$(elapsed)）"
    CFG_ARGS_MAIN=("${CFG_ARGS[@]}")
    for STRAT in $PLAN_STRATEGIES; do
        cvar="PH_${STRAT}_COUNT"
        if [[ "${!cvar:-0}" -eq 0 ]]; then
            say "--- $STRAT：留出段没有能用的币（原因见 data_check.txt），跳过"
            note "$STRAT" default "研究开始之前的留出段 $PRE_HOLDOUT 没有能用的币，没有回测"
            continue
        fi
        say "--- $STRAT（留出段 ${!cvar} 个币）"
        strat_args "$STRAT"
        CFG_ARGS=(--config "$CONFIG" --config "$RUN_DIR/preholdout/$STRAT.json")
        if [[ -n "$DATADIR_ARG" ]]; then CFG_ARGS+=(--datadir "$DATADIR_ARG"); fi
        run_bt "$STRAT" default preholdout "${PRE_HOLDOUT_TR:-$PRE_HOLDOUT}" "$RUN_DIR/strategies/base"
    done
    CFG_ARGS=("${CFG_ARGS_MAIN[@]}")
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
