#!/usr/bin/env bash
# ------------------------------------------------------------------
# 不用 Docker、直接用 .venv 启动（推荐 Linux VPS；Mac 推荐用 Docker：make up / make ui）
#
# 用法：
#   bash scripts/run_bot.sh            # 启动机器人（币安合约，默认模拟盘）—— 只能在币安允许的地区运行
#   bash scripts/run_bot.sh ui         # 只启动 FreqUI，查看 OKX 回测结果（美国 VPS 也可以）
#   其他参数原样传给 freqtrade，例如：bash scripts/run_bot.sh trade --strategy MyStrategy
#
# 会自动读取项目根目录的 .env（格式：KEY=VALUE，等号两边不要有空格）。
# 启动前会执行 scripts/preflight.sh 做安全检查。
# ------------------------------------------------------------------
set -euo pipefail
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

FT="$ROOT_DIR/.venv/bin/freqtrade"
if [[ ! -x "$FT" ]]; then
    echo "找不到 $FT。请先创建 venv（见 README），或用 Docker：make up / make ui" >&2
    exit 1
fi

if [[ -f .env ]]; then
    set -a
    # shellcheck disable=SC1091
    . ./.env
    set +a
else
    echo "提示：没有找到 .env（可以 cp .env.example .env 后修改）" >&2
fi

MODE="${1:-trade}"
if [[ $# -gt 0 ]]; then shift; fi
mkdir -p user_data/logs

case "$MODE" in
    trade)
        exec sh scripts/preflight.sh "$FT" trade \
            --config configs/config.binance-futures.json \
            --logfile user_data/logs/freqtrade.log "$@"
        ;;
    ui|webserver)
        export FREQTRADE__API_SERVER__ENABLED=true
        exec sh scripts/preflight.sh "$FT" webserver \
            --config configs/config.okx-backtest.json \
            --logfile user_data/logs/webserver.log "$@"
        ;;
    *)
        echo "未知模式：$MODE（可选 trade / ui）" >&2
        exit 1
        ;;
esac
