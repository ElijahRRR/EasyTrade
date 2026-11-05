#!/usr/bin/env bash
# ------------------------------------------------------------------
# 下载 OKX USDT 永续合约历史数据（回测用）
#
# 用法：
#   bash scripts/download_okx_data.sh                          # 默认：从 2024-01-01 到今天，全部周期
#   bash scripts/download_okx_data.sh --timerange 20230101-    # 指定开始日期
#   bash scripts/download_okx_data.sh --days 90                # 最近 90 天
#   bash scripts/download_okx_data.sh --pairs "BTC/USDT:USDT ETH/USDT:USDT"
#   bash scripts/download_okx_data.sh --timeframes "5m 1h"     # 只下部分周期（1m 很慢）
#   bash scripts/download_okx_data.sh --config configs/config.binance-futures.json
#        ↑ 以后在允许访问币安的地区下载币安数据，用同一个脚本换配置即可
#   其他参数会原样传给 freqtrade download-data，例如 --erase、--prepend、--candle-types ...
#
# 说明：
#   * 合约模式下 freqtrade 会自动额外下载 mark（标记价格）和 funding_rate（资金费率）数据，
#     不需要手动指定 --candle-types。
#   * OKX 的 mark / funding_rate 只有最近约 3 个月，更早的部分下载不到是正常的。
#   * OKX 每次请求只返回 100 根 K 线，1m 数据量大，2 年 x 20 个币可能要下好几个小时；
#     可以先用 --timeframes "5m 15m 1h 4h 1d" 跳过 1m。
#   * 已经下载过的数据会自动增量更新，不会重复下载。
#   * --include-inactive-pairs：如果 --pairs 用了正则（例如 ".*/USDT:USDT"），也包含已下架的币，
#     减少幸存者偏差。
# ------------------------------------------------------------------
source "$(dirname "$0")/_common.sh"
set -f  # 关闭通配符展开，避免 ".*/USDT:USDT" 这类正则被 shell 当成文件名

CONFIG="configs/config.okx-backtest.json"
TIMEFRAMES="1m 5m 15m 1h 4h 1d"
TIMERANGE=""
DAYS=""
PAIRS=""
EXTRA=()

while [[ $# -gt 0 ]]; do
    case "$1" in
        --config)      CONFIG="$2"; shift 2 ;;
        --timerange)   TIMERANGE="$2"; shift 2 ;;
        --days)        DAYS="$2"; shift 2 ;;
        --pairs)       PAIRS="$2"; shift 2 ;;
        --timeframes)  TIMEFRAMES="$2"; shift 2 ;;
        -h|--help)     sed -n '2,24p' "$0"; exit 0 ;;
        *)             EXTRA+=("$1"); shift ;;
    esac
done

if [[ -z "$TIMERANGE" && -z "$DAYS" ]]; then
    TIMERANGE="20240101-"
    echo "未指定 --timerange / --days，默认使用 --timerange $TIMERANGE"
fi

ARGS=(download-data --config "$CONFIG" --trading-mode futures --include-inactive-pairs)
# shellcheck disable=SC2206  # 故意按空格拆分
ARGS+=(--timeframes $TIMEFRAMES)
if [[ -n "$TIMERANGE" ]]; then ARGS+=(--timerange "$TIMERANGE"); fi
if [[ -n "$DAYS" ]]; then ARGS+=(--days "$DAYS"); fi
if [[ -n "$PAIRS" ]]; then
    # shellcheck disable=SC2206
    ARGS+=(--pairs $PAIRS)
fi
if [[ ${#EXTRA[@]} -gt 0 ]]; then ARGS+=("${EXTRA[@]}"); fi

echo "运行方式：$FT_RUNNER_RESOLVED"
echo "执行：freqtrade ${ARGS[*]}"
ft "${ARGS[@]}"

echo
echo "下载完成。查看已有数据的时间范围："
echo "  freqtrade list-data --config $CONFIG --trading-mode futures --show-timerange"
