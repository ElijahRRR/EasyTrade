#!/usr/bin/env bash
# ------------------------------------------------------------------
# 公共函数（被其他 scripts/*.sh 引用，不要直接运行）
# - 切换到项目根目录（所有路径都相对项目根目录，Docker 里同样适用）
# - 决定用哪种方式运行 freqtrade：
#     FT_RUNNER=venv    使用 .venv/bin/freqtrade（推荐 VPS / Linux）
#     FT_RUNNER=docker  使用 docker compose run --rm cli（推荐 Mac）
#     FT_RUNNER=path    使用 PATH 里的 freqtrade
#     FT_RUNNER=synthetic  不联网，用 scripts/ft_offline.py 跑合成测试数据（只用于流程自检）
#     不设置            自动检测：.venv > PATH > docker
# 兼容 macOS 自带的 bash 3.2（不用关联数组、mapfile 等新语法）。
# ------------------------------------------------------------------
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

COMPOSE_FILE="docker/docker-compose.yml"

# 硬规则：账户权益（含浮亏）最大回撤上限，单位 %。2026-11-07 起 15（以前是 8，第 1、2 轮按 8 判）。
# 必须和 scripts/check_drawdown.py 的 DEFAULT_MAX_DD_PCT 相同（scripts/check_portfolio_math.py 会核对）。
# 临时改：MAX_DD_PCT=12 make research / make backtest / make check（各脚本的 --max-dd 优先）。
# export：venv / PATH 方式运行的 Python 脚本直接读到同一个值；Docker 方式由 py_ft 传进容器。
EASYTRADE_DEFAULT_MAX_DD_PCT=15
MAX_DD_PCT="${MAX_DD_PCT:-$EASYTRADE_DEFAULT_MAX_DD_PCT}"
export MAX_DD_PCT

# docker compose 命令（.env 由 compose 文件里的 env_file 读取，这里不用管）
compose_cmd() {
    docker compose -f "$ROOT_DIR/$COMPOSE_FILE" "$@"
}

detect_runner() {
    local runner="${FT_RUNNER:-auto}"
    if [[ "$runner" == "auto" ]]; then
        if [[ -x "$ROOT_DIR/.venv/bin/freqtrade" ]]; then
            runner="venv"
        elif command -v freqtrade >/dev/null 2>&1; then
            runner="path"
        elif command -v docker >/dev/null 2>&1; then
            runner="docker"
        else
            echo "错误：找不到 freqtrade。请先按 README 安装（Docker 或 .venv）。" >&2
            exit 1
        fi
    fi
    FT_RUNNER_RESOLVED="$runner"
}

# 合成数据自检用的 python：.venv > Docker 镜像 > PATH 里的 python3（需要已装 freqtrade）
_offline_py() {
    if [[ -x "$ROOT_DIR/.venv/bin/python" ]]; then
        "$ROOT_DIR/.venv/bin/python" "$@"
    elif command -v docker >/dev/null 2>&1; then
        compose_cmd run --rm -T -e "MAX_DD_PCT=$MAX_DD_PCT" --entrypoint python cli "$@"
    else
        python3 "$@"
    fi
}

# 用法：ft <freqtrade 子命令> [参数...]
ft() {
    case "$FT_RUNNER_RESOLVED" in
        venv)   "$ROOT_DIR/.venv/bin/freqtrade" "$@" ;;
        path)   freqtrade "$@" ;;
        synthetic) _offline_py scripts/ft_offline.py "$@" ;;
        docker)
            # 输出被重定向（写日志/管道）时不分配 TTY，否则 docker 会报错或混入控制字符
            if [[ -t 0 && -t 1 ]]; then
                compose_cmd run --rm cli "$@"
            else
                compose_cmd run --rm -T cli "$@"
            fi ;;
        *)      echo "错误：未知的 FT_RUNNER=$FT_RUNNER_RESOLVED（可选 venv / docker / path）" >&2; exit 1 ;;
    esac
}

# 用法：py_ft <python 脚本> [参数...]  —— 需要 freqtrade 库的 Python 脚本用它运行
py_ft() {
    case "$FT_RUNNER_RESOLVED" in
        venv)   "$ROOT_DIR/.venv/bin/python" "$@" ;;
        path)   python3 "$@" ;;
        synthetic) _offline_py "$@" ;;
        docker)
            # -e MAX_DD_PCT：把回撤上限（见上面）传进容器，check_drawdown.py 等脚本的默认值才和这里一致
            if [[ -t 0 && -t 1 ]]; then
                compose_cmd run --rm -e "MAX_DD_PCT=$MAX_DD_PCT" --entrypoint python cli "$@"
            else
                compose_cmd run --rm -T -e "MAX_DD_PCT=$MAX_DD_PCT" --entrypoint python cli "$@"
            fi ;;
    esac
}

# 用法：plain_py <python 脚本> [参数...]  —— 只用 Python 标准库、又要在本机运行的脚本（例如 scripts/check_prereg.py
# 要读本机的 git 提交号，Docker 容器里没有 git）：.venv > PATH 里的 python3 > py_ft
plain_py() {
    if [[ -x "$ROOT_DIR/.venv/bin/python" ]]; then
        "$ROOT_DIR/.venv/bin/python" "$@"
    elif command -v python3 >/dev/null 2>&1; then
        python3 "$@"
    else
        py_ft "$@"
    fi
}

detect_runner
