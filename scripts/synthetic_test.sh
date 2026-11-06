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
#   1. 生成合成数据 → user_data/data/okx-synthetic/（scripts/make_synthetic_data.py）
#   2. 批量回测全部策略 + 8% 回撤检查（scripts/backtest_all.sh --config configs/config.synthetic-test.json）
#      结果在 user_data/backtest_results/synthetic/run_<时间>/
#   3. 实盘保护逻辑自检（scripts/check_live_guards.py：止损补救、只在信号刚收盘时入场）
#      —— 这些只在实盘/模拟盘出现，回测测不到，所以单独测
#   4. 对 3 个移植策略和 SampleFuturesSanity 跑 freqtrade 的 lookahead-analysis（未来函数检查）
#      和 recursive-analysis（指标预热长度是否够），日志和 CSV 也放在同一个结果目录
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
        -h|--help) sed -n '2,24p' "$0"; exit 0 ;;
        *) echo "未知参数：$1（可选 --regen / --quick）" >&2; exit 1 ;;
    esac
done

CONFIG="configs/config.synthetic-test.json"
DATADIR="user_data/data/okx-synthetic"
ERRORS=()

echo "================ 合成数据流程自检（结果数字没有意义）================"

# ---------- 1. 合成数据 ----------
if [[ "$REGEN" == "yes" || ! -f "$DATADIR/SYNTHETIC_DATA_README.txt" ]]; then
    echo "=== 1/4 生成合成数据"
    py_ft scripts/make_synthetic_data.py || { echo "生成合成数据失败" >&2; exit 1; }
else
    echo "=== 1/4 使用已有合成数据：$DATADIR（加 --regen 重新生成）"
fi

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
    # 时间段按"合成数据截止到今天、共 730 天"计算（make_synthetic_data.py 的默认值）
    days_ago() { python3 -c "import datetime as d, sys; print((d.date.today() - d.timedelta(days=int(sys.argv[1]))).strftime('%Y%m%d'))" "$1"; }

    # 策略名  lookahead 起始(天前)  recursive 起始(天前)  recursive 预热K线数
    # 日线策略需要 300 根日线预热，所以从 420 天前开始；5m 策略只取最近几个月，否则太慢
    PLAN="BigTrendShort:420:150:300,400,500
DailyThreeSoldiersShort:600:150:30,100,300,500
VolumeSpikeShort:150:35:300,600,1000
SampleFuturesSanity:300:150:100,199,399"

    for line in $PLAN; do
        IFS=':' read -r STRAT LA_DAYS RA_DAYS RA_STARTUP <<< "$line"
        LA_LOG="$OUT/lookahead_${STRAT}.log"
        LA_CSV="$OUT/lookahead_${STRAT}.csv"
        echo "--- lookahead-analysis $STRAT（$(days_ago "$LA_DAYS") 起）→ $LA_LOG"
        if ft lookahead-analysis --config "$CONFIG" --strategy "$STRAT" \
            --timerange "$(days_ago "$LA_DAYS")-" \
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
    print("    （1d 策略的 mtf_* 列被标记是已知的误报，原因见 README「用合成数据自检」；只要上面两个信号数为 0 即可）")
sys.exit(1 if bad else 0)
PY
        else
            echo "    !! 运行失败，最后 10 行："; tail -n 10 "$LA_LOG" | sed 's/^/    /'
            ERRORS+=("lookahead $STRAT")
        fi

        RA_LOG="$OUT/recursive_${STRAT}.log"
        echo "--- recursive-analysis $STRAT（预热 ${RA_STARTUP//,//} 根K线）→ $RA_LOG"
        # shellcheck disable=SC2086  # 故意按空格拆分
        if ft recursive-analysis --config "$CONFIG" --strategy "$STRAT" \
            --timerange "$(days_ago "$RA_DAYS")-" --startup-candle ${RA_STARTUP//,/ } > "$RA_LOG" 2>&1; then
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
