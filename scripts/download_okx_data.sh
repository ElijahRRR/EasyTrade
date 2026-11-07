#!/usr/bin/env bash
# ------------------------------------------------------------------
# 下载 OKX USDT 永续合约历史数据（回测用）
#
# 用法：
#   bash scripts/download_okx_data.sh                          # 默认：从 2021-01-01 到今天，全部周期
#   bash scripts/download_okx_data.sh --timerange 20230101-    # 指定开始日期
#   bash scripts/download_okx_data.sh --days 90                # 最近 90 天
#   bash scripts/download_okx_data.sh --pairs "BTC/USDT:USDT ETH/USDT:USDT"
#   bash scripts/download_okx_data.sh --timeframes "1h 4h 1d"  # 只下部分周期（1m 从 2021 年起要下很多个小时）
#   bash scripts/download_okx_data.sh --config configs/config.binance-futures.json
#        ↑ 以后在允许访问币安的地区下载币安数据，用同一个脚本换配置即可
#   bash scripts/download_okx_data.sh --no-backfill            # 不自动补"比本地数据更早"的那一段（见下面"补早期数据"）
#   bash scripts/download_okx_data.sh --funding-days 0         # 标记价格/资金费率也从开始日期下（OKX 上要多花 2~3 小时）
#   其他参数会原样传给 freqtrade download-data，例如 --erase、--prepend、--candle-types、--datadir ...
#   （自己写了 --erase / --prepend / --candle-types 时，脚本不再自动分几遍下载，按你写的只跑一次 freqtrade）
#
# 说明：
#   * 已经下载过的数据会自动增量更新（只补到今天），不会重复下载。
#   * 补早期数据（自动）：freqtrade 默认只往"后"补。本地数据如果是从 2024-01-01 开始的，
#     就算改成 --timerange 20210101-，它也不会去下 2021~2023 年（日志里只有一句 "Use `--prepend` ..."）。
#     所以脚本先看本地每个币的第一根K线：比开始日期晚、而且不是"这个币本来就上线晚"的，
#     再用 --prepend 只补前面那段K线（不碰后面已有的数据）。"上线晚"按交易所接口里的上线时间判断
#     （OKX 的 listTime）；读不到上线时间时也试着补一次（白跑几秒，不会出错）。
#   * 合约模式下 freqtrade 还会下载 mark（标记价格）和 funding_rate（资金费率）。
#     OKX 的资金费率历史只有最近约 3 个月，在那之前回测按资金费 0 算（配置里 futures_funding_rate = 0），
#     那段时间的标记价格也用不上；可是 freqtrade 照样会从开始日期起一段一段去要（每次只给 100 条，大部分是空的），
#     从 2021 年起要多花 2~3 小时。所以对 OKX：K线从开始日期下，标记价格和资金费率只下最近 --funding-days 天
#     （默认 120 天；本地已经有的照常往后补）。其他交易所（例如币安，资金费率历史很长）不受影响，全部从开始日期下。
#   * 速度（按 freqtrade 2026.10 + ccxt 4.5 源码）：OKX 历史K线（history-candles 接口）每次请求最多 300 根，
#     标记价格 / 资金费率每次 100 条；ccxt 按每次约 0.11 秒限速（资金费率 0.22 秒）。
#     开始下载前脚本会按本地已有的数据估算这次要发多少次请求、大约多久。
#     参考：57 个币 × 1h/4h/1d 从 2021-01-01 起第一次下载，每个币约 220 次请求，全部约 25~40 分钟；
#     1m 一个币从 2021 年起约 1 万次请求（约 20 分钟），20 个币就是好几个小时。
#   * --include-inactive-pairs：如果 --pairs 用了正则（例如 ".*/USDT:USDT"），也包含已下架的币，减少幸存者偏差。
# ------------------------------------------------------------------
source "$(dirname "$0")/_common.sh"
set -f  # 关闭通配符展开，避免 ".*/USDT:USDT" 这类正则被 shell 当成文件名

CONFIG="configs/config.okx-backtest.json"
TIMEFRAMES="1m 5m 15m 1h 4h 1d"
TIMERANGE=""
DAYS=""
PAIRS=""
BACKFILL=1
FUNDING_DAYS=120
EXTRA=()

while [[ $# -gt 0 ]]; do
    case "$1" in
        --config)        CONFIG="$2"; shift 2 ;;
        --timerange)     TIMERANGE="$2"; shift 2 ;;
        --days)          DAYS="$2"; shift 2 ;;
        --pairs)         PAIRS="$2"; shift 2 ;;
        --timeframes)    TIMEFRAMES="$2"; shift 2 ;;
        --no-backfill)   BACKFILL=0; shift ;;
        --funding-days)  FUNDING_DAYS="$2"; shift 2 ;;
        -h|--help)       sed -n '2,/^# -----/p' "$0"; exit 0 ;;
        *)               EXTRA+=("$1"); shift ;;
    esac
done

if [[ -z "$TIMERANGE" && -z "$DAYS" ]]; then
    TIMERANGE="20210101-"
    echo "未指定 --timerange / --days，默认使用 --timerange $TIMERANGE"
fi
[[ "$FUNDING_DAYS" =~ ^[0-9]+$ ]] || { echo "参数错误：--funding-days 要是天数（收到：$FUNDING_DAYS）" >&2; exit 1; }

# 自己写了 --erase / --prepend / --candle-types：完全按你写的跑一次，不自动分批、不自动补早期数据
AUTO=1
DATADIR=""
i=0
while [[ $i -lt ${#EXTRA[@]} ]]; do
    a="${EXTRA[$i]}"
    case "$a" in
        --erase|--prepend|--candle-types|--candle-types=*) AUTO=0 ;;
        --datadir|--data-dir|-d) DATADIR="${EXTRA[$((i + 1))]:-}" ;;
        --datadir=*|--data-dir=*) DATADIR="${a#*=}" ;;
    esac
    i=$((i + 1))
done

# 开始日期（YYYYMMDD），只有 --timerange 写了开始日期时才有；--days 时由下面的 python 自己算
START=""
if [[ "$TIMERANGE" =~ ^([0-9]{8}) ]]; then START="${BASH_REMATCH[1]}"; fi

# ---------- 下载前检查（读配置 + 本地已有数据，不下载） ----------
# 输出 PLAN_* 变量：交易所、标记价格周期、资金费率从哪天下、要补早期数据的币、请求次数估算
# （用 read 读 heredoc，不用 "$(cat <<EOF)"：macOS 自带的 bash 3.2 解析命令替换里的 heredoc 时会被引号/括号弄糊涂）
PLAN_PY=""
IFS= read -r -d '' PLAN_PY <<'PYEOF' || true
import math, shlex, sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

cfg_path, datadir_arg, start_s, days_s, tfs_s, pairs_s, fund_days_s, auto_s, backfill_s = sys.argv[1:10]
now = datetime.now(timezone.utc)

def emit(k, v):
    print(f"{k}={shlex.quote(str(v))}")

def say(msg):
    print(f"  · {msg}", file=sys.stderr)

from freqtrade.configuration.load_config import load_from_files
cfg = load_from_files([cfg_path])
ex = cfg.get("exchange") or {}
name = str(ex.get("name") or "").lower()
is_okx = name in ("okx", "myokx", "okxus")
fmt = str(cfg.get("dataformat_ohlcv") or "feather")
datadir = Path(datadir_arg) if datadir_arg else Path("user_data") / "data" / name
pairs = pairs_s.split() if pairs_s else [str(p) for p in (ex.get("pair_whitelist") or [])]
literal = bool(pairs) and not any(c in p for p in pairs for c in "*?[]()^$|+\\")
tfs = tfs_s.split()
fund_days = int(fund_days_s or 0)
auto = auto_s == "1"

if start_s:
    start = datetime.strptime(start_s, "%Y%m%d").replace(tzinfo=timezone.utc)
elif days_s:
    start = (now - timedelta(days=int(days_s))).replace(hour=0, minute=0, second=0, microsecond=0)
else:
    start = None

import freqtrade.exchange as fe
from freqtrade.data.history import get_datahandler
from freqtrade.enums import CandleType
from freqtrade.exchange import timeframe_to_seconds

cls = getattr(fe, name.title(), None) or fe.Exchange
has = {**fe.Exchange._ft_has_default, **getattr(cls, "_ft_has", {}), **getattr(cls, "_ft_has_futures", {})}
mark_tf = str(has.get("mark_ohlcv_timeframe") or "1h")
fund_tf = str(has.get("funding_fee_timeframe") or "1h")
mark_type = CandleType.from_string(str(has.get("mark_ohlcv_price") or "mark"))

split = auto and is_okx and fund_days > 0
fund_start = None
if split:
    fund_start = (now - timedelta(days=fund_days)).replace(hour=0, minute=0, second=0, microsecond=0)
    if start is not None and start > fund_start:
        fund_start = start
emit("PLAN_OK", 1)
emit("PLAN_EXCHANGE", name)
emit("PLAN_SPLIT", 1 if split else 0)
emit("PLAN_MARK_TF", mark_tf)
emit("PLAN_FUND_START", fund_start.strftime("%Y%m%d") if fund_start else "")

dh = get_datahandler(datadir, fmt)

def span(pair, tf, ct):
    try:
        mn, mx, n = dh.ohlcv_data_min_max(pair, tf, ct)
    except Exception:  # noqa: BLE001 — 文件坏了按没有数据算
        return None
    return (mn, mx) if n else None

# ---- 要补早期数据的币：本地K线第一根比开始日期晚（任何一个周期）----
need = []
fresh = 0
if auto and backfill_s == "1" and start is not None and literal:
    for pair in pairs:
        spans = [span(pair, tf, CandleType.FUTURES) for tf in tfs]
        have = [s for s in spans if s]
        if not have:
            fresh += 1
            continue
        first = max(s[0] for s in have)
        if first > start + timedelta(days=1):
            need.append((pair, first))

backfill, at_listing = [], []
if need:
    listed = {}
    try:
        import ccxt

        opts = dict(ex.get("ccxt_config") or {})
        opts.update({"timeout": 20000, "enableRateLimit": True})
        if is_okx:
            opts["options"] = {"fetchMarkets": {"types": ["swap"]}}
        markets = getattr(ccxt, name)(opts).load_markets()
        for pair, _ in need:
            c = (markets.get(pair) or {}).get("created")
            if c:
                listed[pair] = datetime.fromtimestamp(int(c) / 1000, tz=timezone.utc)
    except Exception as exc:  # noqa: BLE001 — 连不上就全部试着补
        say(f"读不到交易所的上线时间（{exc.__class__.__name__}），本地数据比开始日期晚的币都试着补一次")
    for pair, first in need:
        lt = listed.get(pair)
        if lt is not None and first <= lt + timedelta(days=3):
            at_listing.append((pair, first, lt))
        else:
            backfill.append((pair, first))
emit("PLAN_BACKFILL_PAIRS", " ".join(p for p, _ in backfill))
if at_listing:
    names = " ".join(f"{p.split('/')[0]}({f.date()})" for p, f, _ in at_listing)
    say(f"这 {len(at_listing)} 个币本地数据已经从上线第一天开始（比 {start.date()} 晚是因为上线晚），不用补：{names}")
if backfill:
    names = " ".join(f"{p.split('/')[0]}({f.date()})" for p, f in backfill)
    say(f"这 {len(backfill)} 个币本地数据比 {start.date()} 晚开始，会用 --prepend 补前面那段K线：{names}")

# ---- 请求次数估算（只对 OKX：K线每次 300 根，标记价格/资金费率每次 100 条；ccxt 限速约 0.11 秒/次，资金费率 0.22 秒）----
if is_okx and start is not None:
    def calls(since, until, tf, per):
        sec = timeframe_to_seconds(tf) * per
        return max(0, math.ceil((until - since).total_seconds() / sec)) if until > since else 0

    n_k = n_mark = n_fund = 0
    for pair in pairs if literal else []:
        for tf in tfs:
            s = span(pair, tf, CandleType.FUTURES)
            n_k += calls(s[1] if s else start, now, tf, 300)
            if s and s[0] > start + timedelta(days=1) and pair in dict(backfill):
                n_k += calls(start, s[0], tf, 300)
        for ct, tf, est in ((mark_type, mark_tf, "m"), (CandleType.FUNDING_RATE, fund_tf, "f")):
            s = span(pair, tf, ct)
            since = s[1] if s else (fund_start or start)
            n = calls(since, now, tf, 100)
            if est == "m":
                n_mark += n
            else:
                n_fund += n
    secs = (n_k + n_mark) * 0.11 + n_fund * 0.22
    if literal and secs > 0:
        mins = secs / 60
        say(
            f"预计这次要向 OKX 发约 {n_k + n_mark + n_fund} 次请求（K线 {n_k}，标记价格 {n_mark}，资金费率 {n_fund}），"
            + (f"按 ccxt 的限速至少要 {mins:.0f} 分钟" if mins >= 1 else "不到 1 分钟")
            + ("（很久，建议放在 tmux 里跑）" if mins > 60 else "")
        )
    if "1m" in tfs and literal and (now - start).days > 365:
        m1 = (now - start).days * 1440 / 300 * 0.11 / 60
        say(f"提示：1m 每个币每年约 1750 次请求（约 3 分钟），从 {start.date()} 起每个币约 {m1:.0f} 分钟。"
            "只做研究的话 1h 4h 1d 就够：--timeframes \"1h 4h 1d\"")
PYEOF

PLAN_OK=0
PLAN_SPLIT=0
PLAN_MARK_TF="1h"
PLAN_FUND_START=""
PLAN_BACKFILL_PAIRS=""
echo "运行方式：$FT_RUNNER_RESOLVED"
echo "下载前检查本地已有的数据 ..."
if PLAN_OUT="$(py_ft -c "$PLAN_PY" "$CONFIG" "$DATADIR" "$START" "$DAYS" "$TIMEFRAMES" "$PAIRS" "$FUNDING_DAYS" "$AUTO" "$BACKFILL")"; then
    # 只接受 PLAN_xxx=... 这样的行（防止其他输出混进来被当成命令执行）
    PLAN_OUT="$(printf '%s\n' "$PLAN_OUT" | grep -E '^PLAN_[A-Z_]+=' || true)"
    eval "$PLAN_OUT"
fi
if [[ "$PLAN_OK" != "1" ]]; then
    echo "  · 检查没做成（看上面的报错），按 freqtrade 的默认方式下载一次，不自动补早期数据"
    AUTO=0
fi
[[ "$BACKFILL" == "1" ]] || PLAN_BACKFILL_PAIRS=""

# ---------- 公共参数 ----------
BASE=(download-data --config "$CONFIG" --trading-mode futures --include-inactive-pairs)
PAIR_ARGS=()
if [[ -n "$PAIRS" ]]; then
    # shellcheck disable=SC2206  # 故意按空格拆分
    PAIR_ARGS=(--pairs $PAIRS)
fi
RANGE_ARGS=()
if [[ -n "$TIMERANGE" ]]; then RANGE_ARGS+=(--timerange "$TIMERANGE"); fi
if [[ -n "$DAYS" ]]; then RANGE_ARGS+=(--days "$DAYS"); fi

RC=0
run_pass() {
    local title="$1"
    shift
    echo
    echo "==> $title"
    echo "执行：freqtrade $*"
    local rc=0
    ft "$@" || rc=$?
    if [[ $rc -ne 0 ]]; then
        echo "==> !! 这一步出错（退出码 $rc），继续下一步"
        [[ $RC -ne 0 ]] || RC=$rc
    fi
}

# 1) K线（OKX 分批时只下K线；不分批时 freqtrade 自动连标记价格、资金费率一起下）
MAIN=("${BASE[@]}")
# shellcheck disable=SC2206  # 故意按空格拆分
MAIN+=(--timeframes $TIMEFRAMES)
if [[ "$PLAN_SPLIT" == "1" ]]; then MAIN+=(--candle-types futures); fi
MAIN+=(${RANGE_ARGS[@]+"${RANGE_ARGS[@]}"} ${PAIR_ARGS[@]+"${PAIR_ARGS[@]}"} ${EXTRA[@]+"${EXTRA[@]}"})
if [[ "$PLAN_SPLIT" == "1" ]]; then
    run_pass "第 1 步：K线（$TIMEFRAMES），${TIMERANGE:-最近 $DAYS 天}" "${MAIN[@]}"
else
    run_pass "下载：${TIMEFRAMES}（合约模式自动带标记价格和资金费率），${TIMERANGE:-最近 $DAYS 天}" "${MAIN[@]}"
fi

# 2) OKX：标记价格 + 资金费率只下最近一段（更早的 OKX 没有资金费率，回测按 0 算，标记价格也用不上）
if [[ "$PLAN_SPLIT" == "1" && -n "$PLAN_FUND_START" ]]; then
    run_pass "第 2 步：标记价格 + 资金费率，从 $PLAN_FUND_START 开始（本地已有的照常往后补；--funding-days 0 = 也从开始日期下）" \
        "${BASE[@]}" --candle-types mark funding_rate --timeframes "$PLAN_MARK_TF" --timerange "${PLAN_FUND_START}-" \
        ${PAIR_ARGS[@]+"${PAIR_ARGS[@]}"} ${EXTRA[@]+"${EXTRA[@]}"}
fi

# 3) 补早期数据：本地K线比开始日期晚开始的币，用 --prepend 补前面那段
if [[ "$AUTO" == "1" && -n "$PLAN_BACKFILL_PAIRS" && -n "$TIMERANGE" ]]; then
    # shellcheck disable=SC2206  # 故意按空格拆分
    run_pass "第 3 步：补早期K线（--prepend，只补本地第一根K线之前的部分）" \
        "${BASE[@]}" --candle-types futures --timeframes $TIMEFRAMES --timerange "$TIMERANGE" --prepend \
        --pairs $PLAN_BACKFILL_PAIRS ${EXTRA[@]+"${EXTRA[@]}"}
fi

echo
if [[ $RC -eq 0 ]]; then
    echo "下载完成。查看已有数据的时间范围："
else
    echo "下载有出错的步骤（退出码 $RC，看上面的日志），已经下好的部分照常保存。查看已有数据的时间范围："
fi
echo "  freqtrade list-data --config $CONFIG --trading-mode futures --show-timerange"
exit $RC
