#!/bin/sh
# ------------------------------------------------------------------
# 启动前安全检查（启动机器人 / FreqUI 之前自动执行，不需要手动运行）
#
# 被这两处调用：
#   * docker/docker-compose.yml 里 freqtrade、ui 两个服务的 entrypoint
#   * scripts/run_bot.sh（venv 方式）
# 用法：sh scripts/preflight.sh <freqtrade 可执行文件> <子命令> [参数...]
#
# 做的事：
#   1. 检查 FreqUI/REST API 的账号、密码、JWT 密钥已在 .env 里设置（否则拒绝启动）
#   2. 开了中文通知时，检查 Telegram token / chat id，并拼出 FREQTRADE__WEBHOOK__URL
#   3. trade 模式：检查策略名；按模拟盘/实盘选择不同的数据库文件；实盘时检查 API Key
# 只用 POSIX sh 语法（Docker 镜像和 macOS 都能跑）。
# ------------------------------------------------------------------
set -eu

fail() { echo "[启动检查] 错误：$*" >&2; exit 1; }
warn() { echo "[启动检查] 注意：$*" >&2; }
is_true() { case "${1:-}" in true|True|TRUE|1|yes|YES) return 0 ;; *) return 1 ;; esac; }

[ $# -ge 2 ] || fail "用法：sh scripts/preflight.sh <freqtrade> <trade|webserver> [参数...]"
SUBCMD="$2"

# ---------- 1. FreqUI / REST API 凭据 ----------
case "$SUBCMD" in
    trade|webserver)
        [ -n "${FREQTRADE__API_SERVER__USERNAME:-}" ] || fail "请在 .env 里设置 FREQTRADE__API_SERVER__USERNAME（FreqUI 登录用户名）"
        PW="${FREQTRADE__API_SERVER__PASSWORD:-}"
        [ ${#PW} -ge 10 ] || fail "请在 .env 里设置 FREQTRADE__API_SERVER__PASSWORD（至少 10 个字符）"
        JWT="${FREQTRADE__API_SERVER__JWT_SECRET_KEY:-}"
        [ ${#JWT} -ge 32 ] || fail "请在 .env 里设置 FREQTRADE__API_SERVER__JWT_SECRET_KEY（至少 32 个随机字符，生成方法见 .env.example）"
        case "$JWT" in *PLACEHOLDER*|*CHANGE_ME*) fail "FREQTRADE__API_SERVER__JWT_SECRET_KEY 还是占位符，请换成随机字符串" ;; esac
        case "$PW" in *CHANGE_ME*) fail "FREQTRADE__API_SERVER__PASSWORD 还是占位符，请修改" ;; esac
        ;;
esac

# ---------- 2. Telegram（中文 webhook 通知 + 英文命令机器人） ----------
TG_TOKEN="${FREQTRADE__TELEGRAM__TOKEN:-}"
TG_CHAT="${FREQTRADE__TELEGRAM__CHAT_ID:-}"
if is_true "${FREQTRADE__WEBHOOK__ENABLED:-}" || is_true "${FREQTRADE__TELEGRAM__ENABLED:-}"; then
    [ -n "$TG_TOKEN" ] || fail "开启了 Telegram 通知，但没有设置 FREQTRADE__TELEGRAM__TOKEN"
    [ -n "$TG_CHAT" ] || fail "开启了 Telegram 通知，但没有设置 FREQTRADE__TELEGRAM__CHAT_ID"
fi
if is_true "${FREQTRADE__WEBHOOK__ENABLED:-}" && [ -z "${FREQTRADE__WEBHOOK__URL:-}" ]; then
    FREQTRADE__WEBHOOK__URL="https://api.telegram.org/bot${TG_TOKEN}/sendMessage?chat_id=${TG_CHAT}"
    export FREQTRADE__WEBHOOK__URL
fi

# ---------- 3. trade 模式 ----------
if [ "$SUBCMD" = "trade" ]; then
    HAS_STRATEGY=""
    HAS_DB=""
    for arg in "$@"; do
        case "$arg" in
            -s|--strategy|--strategy=*) HAS_STRATEGY=1 ;;
            --db-url|--db-url=*) HAS_DB=1 ;;
        esac
    done
    if [ -z "$HAS_STRATEGY" ] && [ -z "${FREQTRADE__STRATEGY:-}" ]; then
        fail "没有指定策略：请在 .env 里设置 FREQTRADE__STRATEGY=你的策略类名"
    fi

    # 判断模拟盘/实盘必须和 freqtrade 自己的读法一致：它不分大小写，true/t 都算模拟盘，false/f 都算实盘
    # （例如 False、FALSE 也是实盘）。其他写法（0、1、no、空字符串…）一律拒绝启动，免得这里和 freqtrade 理解不一样。
    # 没设置这个变量时，用配置文件里的 "dry_run": true（模拟盘）。
    LIVE=""
    if [ -n "${FREQTRADE__DRY_RUN+x}" ]; then
        DR="$(printf '%s' "$FREQTRADE__DRY_RUN" | tr '[:upper:]' '[:lower:]')"
        case "$DR" in
            true|t) LIVE="" ;;
            false|f) LIVE=1 ;;
            *) fail "FREQTRADE__DRY_RUN 只能写 true（模拟盘）或 false（实盘），当前是：'$FREQTRADE__DRY_RUN'" ;;
        esac
    fi
    if [ -n "$LIVE" ]; then
        [ -n "${FREQTRADE__EXCHANGE__KEY:-}" ] || fail "实盘模式需要 FREQTRADE__EXCHANGE__KEY"
        [ -n "${FREQTRADE__EXCHANGE__SECRET:-}" ] || fail "实盘模式需要 FREQTRADE__EXCHANGE__SECRET"
        DB_FILE="user_data/tradesv3.live.sqlite"
        warn "================ 实盘模式（会用真钱下单）================"
    else
        DB_FILE="user_data/tradesv3.dryrun.sqlite"
        warn "模拟盘模式（dry-run，不会真下单）"
    fi
    if [ -z "$HAS_DB" ] && [ -z "${FREQTRADE__DB_URL:-}" ]; then
        FREQTRADE__DB_URL="sqlite:///${DB_FILE}"
        export FREQTRADE__DB_URL
    fi
fi

exec "$@"
