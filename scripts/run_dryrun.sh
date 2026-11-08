#!/usr/bin/env bash
# ------------------------------------------------------------------
# 币安 U 本位永续 模拟盘(dry-run，paper trading) 前向测试：冻结版 NFI X8，1 倍杠杆（第 4 轮）。
# 说明（为什么做、在哪跑、怎么看、什么时候停、跑完怎么判断）：docs/forward-test.md
#
# !! 只能在币安允许访问的地区的服务器上运行（例如你那台跑 Rust 引擎的 Vultr 服务器）。
# !! 美国 IP 会被币安拒绝（HTTP 451）；美国 VPS 继续只做 OKX 回测。
#
# 用法：
#   bash scripts/run_dryrun.sh nfi --check-only         # 只检查、不启动（第一次先跑这个，所有问题一次列出来）
#   bash scripts/run_dryrun.sh nfi                      # 检查全部通过后启动模拟盘机器人（前台运行；长期跑用 systemd，见 docs/forward-test.md）
#   bash scripts/run_dryrun.sh nfi --config-check-only  # 只检查配置（不联网，几秒钟；make guard-test 也用它）
#   bash scripts/run_dryrun.sh nfi-watchdog [参数...]   # 启动账户权益看门狗 scripts/equity_watchdog.py（参数原样交给它）
#
# 检查什么（--check-only 每一项都做、最后汇总；正式启动时任何一项不通过就不启动）：
#   1. 读密钥文件 .env.dryrun-nfi（权限必须是 600 / 400）；去掉模拟盘用不到、可能出错的环境变量
#      （币安 API Key、FREQTRADE__STRATEGY、FREQTRADE__STRATEGY_PATH、FREQTRADE__DB_URL），去掉了会说
#      回撤上限 MAX_DD_PCT 只能 <= 15（项目硬规则，只能改严；看门狗和报告都用它），更宽就不启动（看门狗也不启动）
#   2. 配置检查（scripts/check_live_guards.py --dryrun-config，和 make guard-test 同一套规则）：合并后的配置
#      （文件 + 环境变量，和 freqtrade 读到的一样）必须是模拟盘 dry_run = true、币安合约逐仓、策略是 1 倍版本、
#      杠杆封顶 <= 1（项目上限 5）、FreqUI 只监听 127.0.0.1、NFI 要求的设置没被改、配置文件里没有密钥
#   3. FreqUI 账号密码、Telegram（开了的话）都设置好了（scripts/preflight.sh，和 run_bot.sh 同一套）
#   4. NFI 是固定版本（scripts/fetch_benchmarks.sh --check，不联网、不运行它）；
#      策略代码冻结副本 user_data/dryrun/nfi-strategy/：第一次正式启动时从仓库复制（记下 sha256，FROZEN.txt），
#      之后每次启动都核对。git pull、重新下载 vendor/ 都不会改变这一轮模拟盘跑的代码
#   5. 不能有 NFI 的"手动持有"文件（user_data/nfi-hold-trades.json）；新开一轮时不能有上一轮留下的 NFI 止盈目标缓存；
#      看门狗已经因为回撤超限暂停开新仓、还没人工解除时（user_data/dryrun/nfi-watchdog.json），机器人以 paused 状态启动
#   6. 币安合约 / 现货公开接口连得上（/fapi/v1/ping、/api/v3/ping）；HTTP 451 / 403 = 这台服务器所在的地区不能用币安
#   7. 可用内存、硬盘空间（NFI 每 5 分钟要算几百列指标）
# 然后：freqtrade trade --dry-run（freqtrade 自己的"强制模拟盘"开关，还会去掉交易所密钥）
#       --strategy NostalgiaForInfinityX8Lev1 --strategy-path user_data/dryrun/nfi-strategy
#       --db-url sqlite:///user_data/dryrun/nfi.sqlite --logfile user_data/dryrun/nfi.log
#   每次启动记一行到 user_data/dryrun/nfi-starts.log（时间、仓库 commit、冻结副本）。
#
# 环境变量：
#   EASYTRADE_DRYRUN_ENV_FILE     密钥文件（默认 .env.dryrun-nfi；none = 不读文件，只用已有的环境变量）
#   EASYTRADE_DRYRUN_MIN_MEM_MB   最少可用内存（MB，默认 1536；低于 3072 会提醒）
#   EASYTRADE_DRYRUN_MIN_DISK_MB  user_data/ 所在硬盘最少剩余空间（MB，默认 1024）
#   FT_RUNNER                     只支持 venv（默认，用 .venv/bin/freqtrade）或 path（PATH 里的 freqtrade）
# 退出码（启动前的检查；启动以后就是 freqtrade 自己的退出码：0 / 130 = 正常停止，1 / 2 = 出错）：
#   0 = 检查通过（--check-only / --config-check-only）；1 = 参数错误、找不到 freqtrade；
#   20 = 安全检查不通过（配置、密钥文件、NFI 版本、冻结副本、手动持有文件）；21 = 这台服务器所在的地区不能用币安（HTTP 451 / 403）；
#   22 = 内存 / 硬盘不够；23 = 连不上币安（网络问题，可以稍后重试）
#   （几项同时不通过时取最靠前的那一类。docs/systemd/easytrade-dryrun@.service 对 20 21 22 不自动重启，对 23 和 freqtrade 出错会重启）
# 兼容 macOS 自带的 bash 3.2（服务器上一般是 Linux）。
# ------------------------------------------------------------------
set -euo pipefail

usage() { sed -n '2,/^# -----/p' "$0"; }
E_SAFETY=20
E_REGION=21
E_RESOURCE=22
E_NETWORK=23
bad_arg() { echo "参数错误：$1（看 bash scripts/run_dryrun.sh --help）" >&2; exit 1; }

TARGET="${1:-}"
case "$TARGET" in
    -h|--help|"") usage; [[ -n "$TARGET" ]] && exit 0; exit 1 ;;
esac
shift

MODE="start"
WATCHDOG_EXTRA=()
case "$TARGET" in
    nfi)
        while [[ $# -gt 0 ]]; do
            case "$1" in
                --check-only) MODE="check"; shift ;;
                --config-check-only) MODE="config"; shift ;;
                -h|--help) usage; exit 0 ;;
                *) bad_arg "不认识的参数 $1" ;;
            esac
        done
        ;;
    nfi-watchdog)
        MODE="watchdog"
        WATCHDOG_EXTRA=("$@")
        ;;
    *) bad_arg "不认识的模拟盘 $TARGET（现在只有 nfi、nfi-watchdog）" ;;
esac

# shellcheck source=_common.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/_common.sh"   # 切到项目根目录、找 freqtrade

# ---------- 这个模拟盘的固定设置（配置文件是唯一来源；其余的值从配置检查的结果里读） ----------
NAME="nfi"
CONFIG="configs/config.dryrun-nfi.json"
DRYRUN_DIR="user_data/dryrun"
LOGFILE="$DRYRUN_DIR/$NAME.log"
STARTS_LOG="$DRYRUN_DIR/$NAME-starts.log"
CAPS_SRC="user_data/strategies/benchmarks/BenchmarkCaps.py"
VENDOR_DIR="user_data/strategies/vendor"
NFI_FILE="NostalgiaForInfinityX8.py"
NFI_LICENSE="LICENSE-NostalgiaForInfinity.txt"
ENV_FILE="${EASYTRADE_DRYRUN_ENV_FILE:-.env.dryrun-$NAME}"
MIN_MEM_MB="${EASYTRADE_DRYRUN_MIN_MEM_MB:-1536}"
MIN_DISK_MB="${EASYTRADE_DRYRUN_MIN_DISK_MB:-1024}"
WARN_MEM_MB=3072
BINANCE_FAPI="https://fapi.binance.com"
BINANCE_API="https://api.binance.com"
# 账户权益看门狗（scripts/equity_watchdog.py）的默认参数：用哪份配置（FreqUI 地址 / 数据库都在里面）
WATCHDOG_ARGS=(--config "$CONFIG")

case "$MIN_MEM_MB$MIN_DISK_MB" in *[!0-9]*) bad_arg "EASYTRADE_DRYRUN_MIN_MEM_MB / _MIN_DISK_MB 要是整数（MB）" ;; esac

say()  { printf '%s\n' "[模拟盘检查] $*"; }
ok()   { printf '%s\n' "[模拟盘检查] 通过  $*"; }
note() { printf '%s\n' "[模拟盘检查] 注意  $*"; }

# 不通过的项：记下原因和退出码（取最靠前的那一类）；正式启动 / 只查配置时第一项不通过就停
FAILS=()
RC=0
fail_item() {
    local code="$1"
    shift
    printf '%s\n' "[模拟盘检查] 不通过  $*" >&2
    FAILS+=("$*")
    if [[ $RC -eq 0 || $code -lt $RC ]]; then RC=$code; fi
    if [[ "$MODE" != "check" ]]; then
        echo "[模拟盘检查] 模拟盘没有启动。说明：docs/forward-test.md" >&2
        exit "$code"
    fi
}

sha256_of() {
    if command -v sha256sum > /dev/null 2>&1; then
        sha256sum "$1" | awk '{print $1}'
    elif command -v shasum > /dev/null 2>&1; then
        shasum -a 256 "$1" | awk '{print $1}'
    else
        openssl dgst -sha256 "$1" | awk '{print $NF}'
    fi
}

file_mode() {  # 文件权限（八进制，例如 600）
    stat -c '%a' "$1" 2> /dev/null || stat -f '%Lp' "$1" 2> /dev/null || echo "?"
}

case "$FT_RUNNER_RESOLVED" in
    venv) FT_BIN="$ROOT_DIR/.venv/bin/freqtrade" ;;
    path) FT_BIN="$(command -v freqtrade)" ;;
    *)
        echo "模拟盘只支持 venv 方式运行（现在是 FT_RUNNER=$FT_RUNNER_RESOLVED）。请在服务器上按 docs/forward-test.md 建 .venv。" >&2
        exit 1 ;;
esac

# ---------- 1. 密钥文件、清理环境变量 ----------
if [[ "$MODE" != "watchdog" ]]; then
    say "配置：$CONFIG    运行方式：$FT_RUNNER_RESOLVED    密钥文件：$ENV_FILE"
fi
if [[ "$ENV_FILE" == "none" ]]; then ENV_DESC="模拟盘的环境变量"; else ENV_DESC="$ENV_FILE"; fi
load_env_file() {
    if [[ "$ENV_FILE" == "none" ]]; then
        return 0
    fi
    if [[ ! -f "$ENV_FILE" ]]; then
        if [[ "$MODE" == "config" ]]; then
            note "没有密钥文件 $ENV_FILE（只查配置，不影响）"
            return 0
        fi
        fail_item "$E_SAFETY" "没有密钥文件 $ENV_FILE。按 docs/forward-test.md 第 5 节建一个（权限 600；只放 FreqUI 账号密码和可选的 Telegram，不要放币安 API Key）"
        return 0
    fi
    local mode
    mode="$(file_mode "$ENV_FILE")"
    case "$mode" in
        600|400) ;;
        *) fail_item "$E_SAFETY" "密钥文件 $ENV_FILE 的权限是 $mode，别的用户也能读。请改成只有自己能读：chmod 600 $ENV_FILE"; return 0 ;;
    esac
    set -a
    # shellcheck disable=SC1090
    case "$ENV_FILE" in /*) . "$ENV_FILE" ;; *) . "./$ENV_FILE" ;; esac
    set +a
}
load_env_file

# 模拟盘不需要币安 API Key：环境变量里有就去掉（freqtrade 的 --dry-run 也会去掉，这里先去掉，检查和启动看到的一样）
for v in FREQTRADE__EXCHANGE__KEY FREQTRADE__EXCHANGE__SECRET FREQTRADE__EXCHANGE__PASSWORD FREQTRADE__EXCHANGE__UID; do
    if [[ -n "${!v+x}" ]]; then
        if [[ -n "${!v}" ]]; then note "环境变量里有 $v：模拟盘用不到 API Key，已去掉（不要把 Key 放进 $ENV_DESC）"; fi
        unset "$v"
    fi
done
# 策略、策略目录、数据库由这份配置决定（启动时也会在命令行明确传），别的机器人的设置不能带进来
for v in FREQTRADE__STRATEGY FREQTRADE__STRATEGY_PATH FREQTRADE__DB_URL; do
    if [[ -n "${!v+x}" ]]; then
        if [[ -n "${!v}" ]]; then note "环境变量 $v 不用：这个模拟盘的值写在 $CONFIG 里，已去掉"; fi
        unset "$v"
    fi
done
# 回撤上限（MAX_DD_PCT，看门狗和报告都用它）只能比项目硬规则（scripts/_common.sh 的 EASYTRADE_DEFAULT_MAX_DD_PCT
# = check_drawdown.py 的 DEFAULT_MAX_DD_PCT）更严：密钥文件里写了更宽的值，机器人和看门狗都不启动
DD_VALUE="$(printf '%s' "${MAX_DD_PCT:-$EASYTRADE_DEFAULT_MAX_DD_PCT}" | tr -d ' %')"
if awk -v v="$DD_VALUE" -v r="$EASYTRADE_DEFAULT_MAX_DD_PCT" \
        'BEGIN { exit !(v ~ /^[0-9]+([.][0-9]+)?$/ && v + 0 > 0 && v + 0 <= r + 1e-9) }'; then
    if [[ "$MODE" != "watchdog" ]]; then ok "回撤上限 ${DD_VALUE}%（项目硬规则 ${EASYTRADE_DEFAULT_MAX_DD_PCT}%，只能改严）"; fi
else
    fail_item "$E_SAFETY" "回撤上限 MAX_DD_PCT=${MAX_DD_PCT:-} 不行：模拟盘的回撤上限要大于 0、不超过 ${EASYTRADE_DEFAULT_MAX_DD_PCT}（%；项目硬规则只能改严、不能改宽）。多半是 $ENV_DESC 里写的，删掉那一行"
fi

# ---------- 看门狗：读好密钥文件后直接交给 scripts/equity_watchdog.py ----------
if [[ "$MODE" == "watchdog" ]]; then
    [[ $RC -eq 0 ]] || exit "$RC"
    if [[ ! -f scripts/equity_watchdog.py ]]; then
        echo "找不到 scripts/equity_watchdog.py（账户权益看门狗）。先 git pull 拿到最新的仓库。" >&2
        exit "$E_SAFETY"
    fi
    if [[ "$FT_RUNNER_RESOLVED" == "venv" ]]; then PY="$ROOT_DIR/.venv/bin/python"; else PY="python3"; fi
    exec "$PY" scripts/equity_watchdog.py "${WATCHDOG_ARGS[@]}" ${WATCHDOG_EXTRA[@]+"${WATCHDOG_EXTRA[@]}"}
fi

# ---------- 2. 配置检查（文件 + 环境变量；规则在 scripts/check_live_guards.py） ----------
GUARD_ARGS=(scripts/check_live_guards.py --dryrun-config "$CONFIG")
set +e
GUARD_OUT="$(py_ft "${GUARD_ARGS[@]}" 2>&1)"
GUARD_RC=$?
set -e
printf '%s\n' "$GUARD_OUT" | grep -v '^@@' || true
fact() { printf '%s\n' "$GUARD_OUT" | sed -n "s/^@@$1=//p" | head -n 1; }
if [[ $GUARD_RC -ne 0 ]]; then
    fail_item "$E_SAFETY" "配置检查不通过（原因见上面「错误」那几行）"
fi
STRATEGY="$(fact strategy)"
STRATEGY_PATH="$(fact strategy_path)"
DB_URL="$(fact db_url)"
BOT_NAME="$(fact bot_name)"
API_ADDR="$(fact api)"
if [[ "$MODE" == "config" ]]; then
    exit "$RC"
fi
if [[ $GUARD_RC -eq 0 && ( -z "$STRATEGY" || -z "$STRATEGY_PATH" || -z "$DB_URL" ) ]]; then
    fail_item "$E_SAFETY" "配置检查没有给出策略 / 策略目录 / 数据库（scripts/check_live_guards.py 和本脚本版本不一致？）"
fi
SNAP_DIR="${STRATEGY_PATH:-$DRYRUN_DIR/$NAME-strategy}"
DB_FILE="${DB_URL#sqlite:///}"

# ---------- 3. FreqUI 账号密码、Telegram（和 run_bot.sh 同一套检查；这里用 true 代替 freqtrade，只检查不启动） ----------
set +e
PRE_OUT="$(sh scripts/preflight.sh true trade --strategy "${STRATEGY:-x}" --db-url "${DB_URL:-sqlite:///x}" 2>&1)"
PRE_RC=$?
set -e
if [[ $PRE_RC -ne 0 ]]; then
    fail_item "$E_SAFETY" "$(printf '%s\n' "$PRE_OUT" | grep '错误' | head -n 3 | sed -e 's/^\[启动检查\] 错误：//' -e 's/请在 \.env 里设置/请设置/')（填在 $ENV_DESC 里）"
else
    ok "FreqUI 账号密码已设置$(case "${FREQTRADE__WEBHOOK__ENABLED:-}" in true|True|TRUE|1|yes|YES) echo "，中文通知已打开" ;; *) echo "（中文通知没开：FREQTRADE__WEBHOOK__ENABLED=true 打开）" ;; esac)"
fi

# ---------- 4. NFI 固定版本、冻结副本 ----------
set +e
VENDOR_OUT="$(bash scripts/fetch_benchmarks.sh --check 2>&1)"
VENDOR_RC=$?
set -e
NFI_OK=0
if [[ $VENDOR_RC -eq 0 ]]; then
    NFI_OK=1
    ok "user_data/strategies/vendor/ 里的基准策略都是固定版本（sha256 核对通过）"
elif printf '%s\n' "$VENDOR_OUT" | grep -qF "正常    $VENDOR_DIR/$NFI_FILE"; then
    NFI_OK=1
    ok "$VENDOR_DIR/$NFI_FILE 是固定版本"
    note "vendor/ 里别的基准策略文件不全或被改过（不影响模拟盘）：bash scripts/fetch_benchmarks.sh --check 看详情"
fi

FROZEN="$SNAP_DIR/FROZEN.txt"
NEED_FREEZE=0
if [[ -f "$FROZEN" ]]; then
    # 已经冻结过：逐个核对 sha256（这一轮模拟盘一直用这份）
    SNAP_BAD=""
    while read -r tag fname want; do
        [[ "$tag" == "sha256" ]] || continue
        if [[ ! -f "$SNAP_DIR/$fname" ]]; then
            SNAP_BAD="${SNAP_BAD:+$SNAP_BAD、}$fname（不见了）"
        elif [[ "$(sha256_of "$SNAP_DIR/$fname")" != "$want" ]]; then
            SNAP_BAD="${SNAP_BAD:+$SNAP_BAD、}$fname（被改过）"
        fi
    done < "$FROZEN"
    for need in BenchmarkCaps.py "$NFI_FILE"; do
        grep -q "^sha256 $need " "$FROZEN" || SNAP_BAD="${SNAP_BAD:+$SNAP_BAD、}FROZEN.txt 里没有 $need"
    done
    if [[ -n "$SNAP_BAD" ]]; then
        fail_item "$E_SAFETY" "冻结的策略代码副本 $SNAP_DIR/ 和冻结时不一样：$SNAP_BAD。
             这一轮模拟盘跑的代码已经不确定了，只能新开一轮（docs/forward-test.md「重新开始一轮」）"
    else
        ok "冻结副本 $SNAP_DIR/ 和冻结时相同（$(sed -n 's/^created_utc=//p' "$FROZEN") 冻结）"
        if [[ $NFI_OK -eq 0 ]]; then
            note "$VENDOR_DIR/$NFI_FILE 不在或对不上固定版本（不影响这一轮：用的是冻结副本）"
        fi
        if [[ -f "$CAPS_SRC" && "$(sha256_of "$CAPS_SRC")" != "$(sha256_of "$SNAP_DIR/BenchmarkCaps.py")" ]]; then
            note "仓库里的 $CAPS_SRC 已经更新过，这一轮模拟盘继续用冻结的旧版本（要换成新版本 = 新开一轮）"
        fi
        if [[ -f "$VENDOR_DIR/$NFI_FILE" && "$(sha256_of "$VENDOR_DIR/$NFI_FILE")" != "$(sha256_of "$SNAP_DIR/$NFI_FILE")" ]]; then
            note "vendor/ 里的 $NFI_FILE 和冻结副本不一样，这一轮模拟盘继续用冻结副本"
        fi
    fi
elif [[ $NFI_OK -eq 1 && -f "$CAPS_SRC" ]]; then
    NEED_FREEZE=1
    if [[ "$MODE" == "check" ]]; then
        note "还没有冻结副本：正式启动时会把 $CAPS_SRC 和 $VENDOR_DIR/$NFI_FILE 复制到 $SNAP_DIR/（记下 sha256）"
    fi
elif printf '%s\n' "$VENDOR_OUT" | grep -q "缺少    $VENDOR_DIR/$NFI_FILE"; then
    fail_item "$E_SAFETY" "还没有下载固定版本的 NFI（$VENDOR_DIR/$NFI_FILE）。先运行：bash scripts/fetch_benchmarks.sh（只下载固定 commit、核对 sha256，不下载任何自动更新工具）"
else
    fail_item "$E_SAFETY" "$VENDOR_DIR/$NFI_FILE 和固定版本对不上（被改过？），不能用。重新下载固定版本：bash scripts/fetch_benchmarks.sh --force
             $(printf '%s\n' "$VENDOR_OUT" | tail -n 1)"
fi

# ---------- 5. 手动持有文件、上一轮的止盈目标缓存 ----------
for f in user_data/nfi-hold-trades.json "$SNAP_DIR/hold-trades.json"; do
    if [[ -e "$f" ]]; then
        fail_item "$E_SAFETY" "有 NFI 的\"手动持有\"文件 $f：它会让 NFI 不按自己的规则平仓，前向测试就不是在测 NFI 了。移走它：mv $f $f.disabled"
    fi
done
# 看门狗（scripts/equity_watchdog.py）已经因为回撤超限暂停了开新仓、还没人工解除：机器人重启后也保持暂停
# （freqtrade 不保存暂停状态，重启后按配置的 initial_state = running 开始；看门狗的状态文件和数据库放在一起）
WD_STATE="${DB_FILE%.sqlite}-watchdog.json"
WD_HALTED=0
if [[ -f "$WD_STATE" ]] && grep -Eq '"halted"[[:space:]]*:[[:space:]]*true' "$WD_STATE"; then
    WD_HALTED=1
    note "看门狗记录：回撤超过上限、已暂停开新仓，还没人工解除（$WD_STATE）。机器人会以 paused 状态启动：不开新仓、不加仓，已有持仓照常减仓 / 平仓"
fi
CACHE_FILE="user_data/nfix8-profit_max-${BOT_NAME:+$BOT_NAME-}binance-USDT.json"
if [[ ! -f "$DB_FILE" && -f "$CACHE_FILE" ]]; then
    fail_item "$E_SAFETY" "要新开一轮模拟盘（还没有数据库 $DB_FILE），但有上一轮留下的 NFI 止盈目标缓存 $CACHE_FILE。
             先移走它（新的一轮要从空的开始）：mv $CACHE_FILE $CACHE_FILE.old"
fi

# ---------- 6. 币安公开接口（只读、不需要 Key） ----------
probe() {  # 用法：probe <URL>；设置 PROBE_CODE（HTTP 状态码，连不上 = 000）和 PROBE_ERR（curl 的报错）
    local err_f
    err_f="$(mktemp "${TMPDIR:-/tmp}/easytrade-probe.XXXXXX")"
    PROBE_CODE="$(curl -sS -o /dev/null -w '%{http_code}' --proto '=https' --connect-timeout 10 --max-time 20 "$1" 2> "$err_f" < /dev/null || true)"
    PROBE_ERR="$(head -n 1 "$err_f" 2> /dev/null || true)"
    rm -f "$err_f"
    [[ "$PROBE_CODE" =~ ^[0-9]{3}$ ]] || PROBE_CODE="000"
}
if ! command -v curl > /dev/null 2>&1; then
    fail_item "$E_NETWORK" "没有 curl 命令，没法检查币安连不连得上（Ubuntu：sudo apt install curl）"
else
    BLOCKED=""
    UNREACH=""
    for pair in "合约|$BINANCE_FAPI/fapi/v1/ping" "现货|$BINANCE_API/api/v3/ping"; do
        label="${pair%%|*}"
        url="${pair#*|}"
        probe "$url"
        case "$PROBE_CODE" in
            200) ok "币安${label}公开接口连得上（$url）" ;;
            451|403) BLOCKED="${BLOCKED:+$BLOCKED、}${label} $url 返回 HTTP $PROBE_CODE" ;;
            000) UNREACH="${UNREACH:+$UNREACH、}${label} $url（${PROBE_ERR:-没有响应}）" ;;
            *) UNREACH="${UNREACH:+$UNREACH、}${label} $url 返回 HTTP $PROBE_CODE" ;;
        esac
    done
    if [[ -n "$BLOCKED" ]]; then
        fail_item "$E_REGION" "币安拒绝了这台服务器的访问（$BLOCKED）。
             这台服务器所在的地区不能使用币安（美国 IP 就是这样，币安对它返回 HTTP 451）。
             请在币安允许访问的地区的服务器上运行模拟盘（例如你那台跑 Rust 引擎的 Vultr 服务器）；美国 VPS 只用来做 OKX 回测。"
    elif [[ -n "$UNREACH" ]]; then
        fail_item "$E_NETWORK" "连不上币安（$UNREACH）。
             可能是这台服务器的网络、DNS 或防火墙有问题，过一会儿再试；这台服务器如果在币安不服务的地区，也会连不上。
             模拟盘要在币安允许访问的地区的服务器上运行（例如你那台 Vultr 服务器）。"
    else
        # 顺便看一下本机时钟（K线按时间对齐，差太多会晚一根K线才看到信号）
        server_ms="$(curl -s --max-time 10 "$BINANCE_FAPI/fapi/v1/time" < /dev/null | sed -n 's/.*"serverTime":\([0-9]*\).*/\1/p' || true)"
        if [[ "$server_ms" =~ ^[0-9]{13}$ ]]; then
            skew=$(( $(date +%s) - server_ms / 1000 ))
            [[ $skew -ge 0 ]] || skew=$(( -skew ))
            if [[ $skew -gt 5 ]]; then
                note "本机时钟和币安差了约 $skew 秒，建议打开自动对时（Ubuntu：sudo timedatectl set-ntp true）"
            fi
        fi
    fi
fi

# ---------- 7. 内存、硬盘 ----------
mem_available_mb() {  # 可用内存（MB），读不到输出空（同 scripts/research_benchmarks.sh）
    if [[ -r /proc/meminfo ]]; then
        awk '/^MemAvailable:/ {print int($2 / 1024); exit}' /proc/meminfo 2> /dev/null || true
    elif command -v vm_stat > /dev/null 2>&1; then
        vm_stat 2> /dev/null | awk '/page size of/ {for (i = 1; i <= NF; i++) if ($i ~ /^[0-9]+$/) ps = $i}
            /^Pages (free|inactive|speculative|purgeable)/ {gsub(/\./, "", $NF); s += $NF}
            END {if (ps > 0 && s > 0) print int(s * ps / 1048576)}' || true
    fi
}
MEM_MB="$(mem_available_mb)"
if [[ "$MEM_MB" =~ ^[0-9]+$ ]]; then
    if [[ $MEM_MB -lt $MIN_MEM_MB ]]; then
        fail_item "$E_RESOURCE" "可用内存只有 ${MEM_MB} MB，至少要 ${MIN_MEM_MB} MB（NFI 每 5 分钟要给每个币算几百列指标；同一台服务器上还有别的程序的话，先看看谁占着）"
    elif [[ $MEM_MB -lt $WARN_MEM_MB ]]; then
        note "可用内存 ${MEM_MB} MB，够启动，但建议 3 GB 以上（docs/forward-test.md 第 3 节）"
    else
        ok "可用内存 ${MEM_MB} MB"
    fi
else
    note "读不到可用内存，跳过内存检查（至少要 ${MIN_MEM_MB} MB）"
fi
DISK_MB="$(df -Pk user_data 2> /dev/null | awk 'NR == 2 {print int($4 / 1024)}' || true)"
if [[ "$DISK_MB" =~ ^[0-9]+$ ]]; then
    if [[ $DISK_MB -lt $MIN_DISK_MB ]]; then
        fail_item "$E_RESOURCE" "user_data/ 所在的硬盘只剩 ${DISK_MB} MB，至少要 ${MIN_DISK_MB} MB（数据库、日志会一直变大）"
    else
        ok "硬盘剩余 ${DISK_MB} MB"
    fi
else
    note "读不到硬盘剩余空间，跳过硬盘检查"
fi
CPUS="$( (getconf _NPROCESSORS_ONLN || nproc || sysctl -n hw.ncpu) 2> /dev/null | head -n 1 || true)"
if [[ "$CPUS" =~ ^[0-9]+$ && $CPUS -lt 2 ]]; then
    note "只有 $CPUS 个 CPU 核：NFI 每根 5 分钟K线收盘后要算一遍所有币，可能要一两分钟才开仓，建议 2 核以上"
fi

# ---------- 汇总 ----------
if [[ ${#FAILS[@]} -gt 0 ]]; then
    echo "" >&2
    echo "[模拟盘检查] 有 ${#FAILS[@]} 项不通过，模拟盘不能启动（退出码 $RC）：" >&2
    for f in "${FAILS[@]}"; do printf '  - %s\n' "$f" >&2; done
    echo "说明：docs/forward-test.md" >&2
    exit "$RC"
fi
if [[ "$MODE" == "check" ]]; then
    echo ""
    say "全部通过。启动：bash scripts/run_dryrun.sh $NAME（长期跑用 systemd：docs/forward-test.md 第 7 节）"
    exit 0
fi

# ---------- 启动 ----------
mkdir -p "$DRYRUN_DIR"
if [[ $NEED_FREEZE -eq 1 ]]; then
    mkdir -p "$SNAP_DIR"
    cp "$CAPS_SRC" "$SNAP_DIR/BenchmarkCaps.py"
    cp "$VENDOR_DIR/$NFI_FILE" "$SNAP_DIR/$NFI_FILE"
    SNAP_FILES="BenchmarkCaps.py $NFI_FILE"
    if [[ -f "$VENDOR_DIR/$NFI_LICENSE" ]]; then
        cp "$VENDOR_DIR/$NFI_LICENSE" "$SNAP_DIR/$NFI_LICENSE"
        SNAP_FILES="$SNAP_FILES $NFI_LICENSE"
    fi
    COMMIT="$(git rev-parse HEAD 2> /dev/null || echo unknown)"
    if [[ -n "$(git status --porcelain -- "$CAPS_SRC" 2> /dev/null || true)" ]]; then COMMIT="$COMMIT（BenchmarkCaps.py 有未提交的改动）"; fi
    {
        echo "# 这一轮模拟盘用的策略代码（scripts/run_dryrun.sh 第一次启动时冻结）。不要手改：每次启动都会核对下面的 sha256。"
        echo "# NFI 是第三方代码（GPL-3.0），不要把这个目录发给别人。新开一轮：docs/forward-test.md「重新开始一轮」"
        echo "created_utc=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
        echo "repo_commit=$COMMIT"
        echo "nfi_commit=$(sed -n 's/^NFI_COMMIT="\([0-9a-f]*\)"$/\1/p' scripts/fetch_benchmarks.sh | head -n 1)"
        echo "strategy=$STRATEGY"
        for f in $SNAP_FILES; do echo "sha256 $f $(sha256_of "$SNAP_DIR/$f")"; done
    } > "$FROZEN"
    say "已冻结策略代码：$SNAP_DIR/（FROZEN.txt）"
fi

{
    printf '%s start strategy=%s repo=%s frozen=%s db=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$STRATEGY" \
        "$(git rev-parse --short HEAD 2> /dev/null || echo unknown)" "$(sed -n 's/^created_utc=//p' "$FROZEN")" "$DB_URL"
} >> "$STARTS_LOG"

if [[ $WD_HALTED -eq 1 ]]; then
    FREQTRADE__INITIAL_STATE=paused
    export FREQTRADE__INITIAL_STATE
fi
say "启动模拟盘：$STRATEGY（1 倍杠杆，币安合约，dry-run）。FreqUI：${API_ADDR:-未开}（SSH 隧道看，docs/forward-test.md 第 6 节）"
say "日志：$LOGFILE    数据库：$DB_FILE    停止：Ctrl+C（systemd：sudo systemctl stop easytrade-dryrun@$NAME）"
exec sh scripts/preflight.sh "$FT_BIN" trade \
    --config "$CONFIG" \
    --dry-run \
    --strategy "$STRATEGY" \
    --strategy-path "$SNAP_DIR" \
    --db-url "$DB_URL" \
    --logfile "$LOGFILE"
