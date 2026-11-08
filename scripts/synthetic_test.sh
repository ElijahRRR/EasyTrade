#!/usr/bin/env bash
# ------------------------------------------------------------------
# 用【合成测试数据】离线自检整条流程（不需要联网，不需要交易所 API）
#
# 用法：
#   bash scripts/synthetic_test.sh                 # 已有合成数据就直接用，没有就先生成
#   bash scripts/synthetic_test.sh --regen         # 重新生成合成数据
#   bash scripts/synthetic_test.sh --quick         # 跳过第 4 步 lookahead / recursive 分析
#   make synthetic-test                            # 同上
#
# 做的事：
#   1. 生成合成数据 → user_data/data/okx-synthetic/（scripts/make_synthetic_data.py）；
#      第 5 轮起还有币安的合成数据 → user_data/data/binance-synthetic/（--exchange binance，XSFundingCarry 用）。
#      已有的 okx-synthetic 是旧版（第 5 轮以前生成的，没有现货/新币/下架的币/暴跌日）时自动重新生成
#   2. 批量回测全部策略 + 账户回撤检查（上限默认 15%；scripts/backtest_all.sh --config configs/config.synthetic-test.json）
#      结果在 user_data/backtest_results/synthetic/run_<时间>/
#   3. 实盘保护逻辑自检（scripts/check_live_guards.py：止损补救、只在信号刚收盘时入场）
#      —— 这些只在实盘/模拟盘出现，回测测不到，所以单独测
#   4. 对 3 个移植策略、SampleFuturesSanity、第 2 步的 6 个新策略和第 3 轮的 XSMomentum、BigTrendShortLowRisk、
#      第 5 轮的 PremiumFade、CapitulationRebound、NewListingShortHedged、XSFundingCarry
#      跑 freqtrade 的 lookahead-analysis（未来函数检查）
#      和 recursive-analysis（指标预热长度是否够），日志和 CSV 也放在同一个结果目录。
#      第 5 轮的策略用各自的研究配置：NewListingShortHedged = config.okx-research-listings.json + 合成数据的交易对片段
#      （synthetic_pairs.json）+ 先用 scripts/build_listings.py 生成上线日文件；XSFundingCarry = config.binance-research.json
#      + 币安合成数据；都叠加 configs/research-overrides/<策略>.json
#
# !! 合成数据是随机生成的，回测收益/回撤数字没有任何意义；这里只看"每一步有没有报错"。
#    回撤检查"未通过"是正常的（随机行情里策略没有优势），不算自检失败。
#
# 退出码：0 = 流程全部跑通；1 = 有步骤报错，或 lookahead-analysis 发现入场/离场信号用了未来数据。
# ------------------------------------------------------------------
source "$(dirname "$0")/_common.sh"
# shellcheck disable=SC2034  # 被 _common.sh 里的 ft / py_ft 使用
FT_RUNNER_RESOLVED="synthetic"

REGEN="no"
QUICK="no"
while [[ $# -gt 0 ]]; do
    case "$1" in
        --regen) REGEN="yes"; shift ;;
        --quick) QUICK="yes"; shift ;;
        -h|--help) sed -n '2,/^# -----/p' "$0" | sed '$d'; exit 0 ;;
        *) echo "未知参数：$1（可选 --regen / --quick）" >&2; exit 1 ;;
    esac
done

CONFIG="configs/config.synthetic-test.json"
DATADIR="user_data/data/okx-synthetic"
BN_DATADIR="user_data/data/binance-synthetic"
ERRORS=()

echo "================ 合成数据流程自检（结果数字没有意义）================"

# ---------- 1. 合成数据 ----------
if [[ "$REGEN" == "yes" || ! -f "$DATADIR/SYNTHETIC_DATA_README.txt" ]]; then
    echo "=== 1/4 生成合成数据"
    py_ft scripts/make_synthetic_data.py || { echo "生成合成数据失败" >&2; exit 1; }
elif ! grep -q '^SYNTHETIC_FORMAT_VERSION=2' "$DATADIR/SYNTHETIC_DATA_README.txt" 2> /dev/null; then
    echo "=== 1/4 $DATADIR 是旧版合成数据（没有第 5 轮策略要的现货/新币/下架的币/暴跌日），重新生成"
    py_ft scripts/make_synthetic_data.py || { echo "生成合成数据失败" >&2; exit 1; }
else
    echo "=== 1/4 使用已有合成数据：$DATADIR（加 --regen 重新生成）"
fi
if [[ "$REGEN" == "yes" || ! -f "$BN_DATADIR/SYNTHETIC_DATA_README.txt" ]]; then
    echo "    生成币安的合成数据 → $BN_DATADIR（XSFundingCarry 用：带完整的资金费率和标记价格）"
    py_ft scripts/make_synthetic_data.py --exchange binance > /dev/null || { echo "生成币安合成数据失败" >&2; exit 1; }
fi
# NewListingShortHedged 回测要先有新币上线日文件（扫描合成数据的 1d K线 → user_data/listings/okx-synthetic_first_candle.json）
LISTINGS_LOG="$(mktemp "${TMPDIR:-/tmp}/build_listings.XXXXXX")"
if py_ft scripts/build_listings.py --datadir "$DATADIR" > "$LISTINGS_LOG" 2>&1; then
    echo "    新币上线日文件：user_data/listings/$(basename "$DATADIR")_first_candle.json（scripts/build_listings.py）"
else
    echo "    !! 生成新币上线日文件失败，最后 10 行："; tail -n 10 "$LISTINGS_LOG" | sed 's/^/    /'
    ERRORS+=("build_listings")
fi
rm -f "$LISTINGS_LOG"

# ---------- 2. 批量回测 + 回撤检查 ----------
echo
echo "=== 2/4 批量回测全部策略 + 回撤检查"
BT_RC=0
bash scripts/backtest_all.sh --config "$CONFIG" || BT_RC=$?
# backtest_all.sh：0 = 全部通过，1 = 有策略回撤/杠杆未通过（合成数据下正常），2 = 有回测运行报错，3 = 没有结果
if [[ "$BT_RC" -ne 0 && "$BT_RC" -ne 1 ]]; then
    ERRORS+=("批量回测（退出码 $BT_RC）")
fi
RUN_DIR="$(ls -1dt user_data/backtest_results/synthetic/run_* 2>/dev/null | head -n 1 || true)"

# ---------- 3. 实盘保护逻辑 ----------
echo
echo "=== 3/4 实盘保护逻辑自检（止损补救、入场时效）"
py_ft scripts/check_live_guards.py 2>&1 | grep -v '^[0-9]\{4\}-[0-9][0-9]-[0-9][0-9] ' | sed 's/^/    /' \
    || ERRORS+=("实盘保护逻辑自检")

# ---------- 4. lookahead / recursive 分析 ----------
if [[ "$QUICK" == "no" ]]; then
    echo
    echo "=== 4/4 未来函数检查（lookahead-analysis）和预热检查（recursive-analysis）"
    OUT="${RUN_DIR:-user_data/backtest_results/synthetic}/analysis"
    mkdir -p "$OUT"
    # 时间段从"合成数据的最后一天"往前倒推，并且写明结束日期（不用"到今天为止"的开放区间）：
    # recursive-analysis 遇到开放区间会把"现在"当成结束时间，只在最后几根K线上比较指标；
    # 合成数据生成一两天后，5m 策略最后 300 根K线（25 小时）里就已经没有数据了，会报 "No data found"。
    data_end_of() {
        py_ft -c "import pandas as pd, sys; d = pd.read_feather(sys.argv[1], columns=['date'])['date']; print((d.iloc[-1] + pd.Timedelta(days=1)).strftime('%Y%m%d'))" \
            "$1/futures/BTC_USDT_USDT-1d-futures.feather" 2> /dev/null || true
    }
    DATA_END="$(data_end_of "$DATADIR")"
    [[ "$DATA_END" =~ ^[0-9]{8}$ ]] || DATA_END="$(date -u +%Y%m%d)"
    # 币安的合成数据可能是另一天生成的，结束日期单独算
    BN_DATA_END="$(data_end_of "$BN_DATADIR")"
    [[ "$BN_DATA_END" =~ ^[0-9]{8}$ ]] || BN_DATA_END="$DATA_END"
    echo "    合成数据截止 $DATA_END（下面的区间都从这一天往前倒推；币安的合成数据截止 $BN_DATA_END）"
    END="$DATA_END"
    days_ago() { python3 -c "import datetime as d, sys; print((d.datetime.strptime(sys.argv[2], '%Y%m%d') - d.timedelta(days=int(sys.argv[1]))).strftime('%Y%m%d'))" "$1" "$END"; }

    # 策略名  lookahead 起始(天前)  recursive 起始(天前)  recursive 预热K线数
    # 日线策略需要 300 根日线预热，所以从 420 天前开始；5m 策略只取最近几个月，否则太慢
    # 第 2 步新策略：1h 策略取最近 150 天；FundingContrarian 只看最近 80 天（合成资金费率只有最近约 92 天，前面不开仓）
    # TsmomTrend 从 300 天前开始：420 天起的窗口里有一笔交易碰上 freqtrade 回测结尾强制平仓单没成交、
    #   被 lookahead-analysis 误报成"入场偏差"（没有任何指标列被标记；逐根截断数据重算信号也完全一致）
    # 第 3 轮：XSMomentum 预热 130 根（截面排名要用整个币池，分析工具运行时自动用配置里的全部交易对）；
    #   BigTrendShortLowRisk 和 BigTrendShort 信号完全相同（只是仓位减半）
    # 第 5 轮（第 5 列 = 用哪套配置，见下面 profile_args）：PremiumFade 1h、预热 760 根，取最近 300 天；
    #   CapitulationRebound 和 NewListingShortHedged 信号少（全市场暴跌日 / 新币上线），取几乎全部数据；
    #   XSFundingCarry 用币安合成数据（每周一换仓，交易多），取最近 600 天
    PLAN="BigTrendShort:420:150:300,400,500
DailyThreeSoldiersShort:600:150:30,100,300,500
VolumeSpikeShort:150:35:300,600,1000
SampleFuturesSanity:300:150:100,199,399
DonchianTrend:420:150:300,400,500
TsmomTrend:300:150:200,300,500
BigTrendBoth:420:150:300,400,500
RangeMeanReversion:150:60:150,300,500
VolSqueezeBreakout:150:60:150,300,500
FundingContrarian:80:70:100,200,400
XSMomentum:420:150:130,200,300,500
BigTrendShortLowRisk:420:150:300,400,500
PremiumFade:300:150:760,1000,1400:okx
CapitulationRebound:2300:420:60,100,200:override
NewListingShortHedged:2300:420:13,30,60:listings
XSFundingCarry:600:420:130,200,300:binance"

    # 每套配置的 freqtrade 参数（第 5 轮）：okx = 合成数据自检配置；override = 再叠加这个策略自己的研究设置；
    # listings = OKX 全部 USDT 永续的研究配置 + 合成数据的交易对片段 + 研究设置；binance = 币安研究配置 + 币安合成数据
    PROFILE_ARGS=()
    profile_args() {
        local ov="configs/research-overrides/$2.json"
        case "$1" in
            override) PROFILE_ARGS=(--config "$CONFIG") ;;
            listings) PROFILE_ARGS=(--config configs/config.okx-research-listings.json --config "$DATADIR/synthetic_pairs.json"
                                    --datadir "$DATADIR") ;;
            binance)  PROFILE_ARGS=(--config configs/config.binance-research.json --config "$BN_DATADIR/synthetic_pairs.json"
                                    --datadir "$BN_DATADIR") ;;
            *)        PROFILE_ARGS=(--config "$CONFIG"); return 0 ;;
        esac
        if [[ -f "$ov" ]]; then PROFILE_ARGS+=(--config "$ov"); fi
    }

    for line in $PLAN; do
        IFS=':' read -r STRAT LA_DAYS RA_DAYS RA_STARTUP PROFILE <<< "$line"
        profile_args "${PROFILE:-okx}" "$STRAT"
        END="$DATA_END"
        if [[ "${PROFILE:-}" == "binance" ]]; then END="$BN_DATA_END"; fi
        LA_LOG="$OUT/lookahead_${STRAT}.log"
        LA_CSV="$OUT/lookahead_${STRAT}.csv"
        echo "--- lookahead-analysis $STRAT（$(days_ago "$LA_DAYS") ~ $END）→ $LA_LOG"
        if ft lookahead-analysis "${PROFILE_ARGS[@]}" --strategy "$STRAT" \
            --timerange "$(days_ago "$LA_DAYS")-$END" \
            --minimum-trade-amount 10 --targeted-trade-amount 20 \
            --lookahead-analysis-exportfilename "$LA_CSV" > "$LA_LOG" 2>&1; then
            # CSV 列：filename,strategy,has_bias,total_signals,biased_entry_signals,biased_exit_signals,biased_indicators
            python3 - "$LA_CSV" "$STRAT" <<'PY' || ERRORS+=("lookahead $STRAT")
import csv, sys
rows = [r for r in csv.DictReader(open(sys.argv[1], encoding="utf-8")) if r["strategy"] == sys.argv[2]]
if not rows:
    print("    !! 没有结果"); sys.exit(1)
r = rows[-1]
bad = int(r["biased_entry_signals"] or 0) + int(r["biased_exit_signals"] or 0)
print(f"    信号数 {r['total_signals']}，用了未来数据的入场信号 {r['biased_entry_signals']}，离场信号 {r['biased_exit_signals']}")
if r["biased_indicators"]:
    print(f"    被标记的指标列：{r['biased_indicators']}")
    print("    （1d 策略的 mtf_* 列及由它算出的列被标记是已知的误报，原因见 README「用合成数据自检」；只要上面两个信号数为 0 即可）")
sys.exit(1 if bad else 0)
PY
        else
            echo "    !! 运行失败，最后 10 行："; tail -n 10 "$LA_LOG" | sed 's/^/    /'
            ERRORS+=("lookahead $STRAT")
        fi

        RA_LOG="$OUT/recursive_${STRAT}.log"
        echo "--- recursive-analysis $STRAT（预热 ${RA_STARTUP//,//} 根K线）→ $RA_LOG"
        # shellcheck disable=SC2086  # 故意按空格拆分
        if ft recursive-analysis "${PROFILE_ARGS[@]}" --strategy "$STRAT" \
            --timerange "$(days_ago "$RA_DAYS")-$END" --startup-candle ${RA_STARTUP//,/ } > "$RA_LOG" 2>&1; then
            sed -n '/┏/,/┘/p' "$RA_LOG" | sed 's/^/    /'
        else
            echo "    !! 运行失败，最后 10 行："; tail -n 10 "$RA_LOG" | sed 's/^/    /'
            ERRORS+=("recursive $STRAT")
        fi
    done
else
    echo
    echo "=== 4/4 已跳过（--quick）"
fi

echo
echo "================ 自检结果 ================"
echo "结果目录：${RUN_DIR:-（没有生成）}"
if [[ ${#ERRORS[@]} -gt 0 ]]; then
    echo "有 ${#ERRORS[@]} 个步骤出错：${ERRORS[*]}"
    exit 1
fi
echo "流程全部跑通。（再次提醒：合成数据的收益/回撤数字没有意义）"
exit 0
