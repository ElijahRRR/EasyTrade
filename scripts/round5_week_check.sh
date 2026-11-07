#!/usr/bin/env bash
# ------------------------------------------------------------------
# 第 5 轮管道检查（预注册 docs/round5-preregistration.md 第 3.2 节最后一条）：
#   "跑一个一周的小窗口（2026-11-01 至 2026-11-07），只打印交易笔数和 funding_fees 是否非零，不打印收益。"
#
# 用默认参数回测一个策略一周（freqtrade 的输出只写进日志文件，不显示），然后只打印：
#   交易笔数（按 enter_tag 分）、有交易的币几个、funding_fees 不为 0 的交易几笔。收益、盈亏一概不打印。
#
# 用法：
#   bash scripts/round5_week_check.sh --strategy XSFundingCarry --config configs/config.binance-research.json
#   bash scripts/round5_week_check.sh --strategy PremiumFade                      # 默认配置 configs/config.okx-research.json
#   bash scripts/round5_week_check.sh --strategy NewListingShortHedged --config configs/config.okx-research-listings.json
#   make pipeline-week ARGS="--strategy CapitulationRebound"
# 参数：
#   --strategy S        策略类名（必填）
#   --config CFG        研究配置（可以写多次，按顺序叠加；默认 configs/config.okx-research.json）。
#                       configs/research-overrides/<S>.json 存在时自动叠加在最后（和 make research 一样）
#   --datadir DIR       数据目录（默认按配置的交易所）
#   --timerange A-B     默认 20261101-20261108（= 2026-11-01 ~ 2026-11-07 这一周）
#   --synthetic         合成数据（不联网，用 scripts/ft_offline.py；数据目录默认 user_data/data/okx-synthetic）
# 结果和完整日志：user_data/backtest_results/round5_week/<策略>_<时间>/（不提交 git）。
# NewListingShortHedged 要先有上线日文件（默认 user_data/listings/okx_first_candle.json，不提交 git）：
#   先用 configs/config.okx-research-listings.json 下载全部永续的数据，再
#   make listings ARGS="--datadir user_data/data/okx --data-start <下载开始日期 YYYYMMDD>"
#   没有这个文件策略一笔都不开；这时本脚本报错退出（退出码 3），不会当成"这一周没有信号"。
# 退出码：0 = 跑完了（笔数是 0 也算跑完，屏幕上会写明）；1 = 回测出错；2 = 参数错误；
#         3 = 策略报了数据错误、一笔都不会开（例如找不到上线日文件），这次检查不算数。
# ------------------------------------------------------------------
source "$(dirname "$0")/_common.sh"

STRAT=""
CONFIGS=()
DATADIR=""
TIMERANGE="20261101-20261108"
SYNTH="no"
while [[ $# -gt 0 ]]; do
    case "$1" in
        --strategy)  STRAT="${2:-}"; shift 2 ;;
        --config)    CONFIGS+=("${2:-}"); shift 2 ;;
        --datadir)   DATADIR="${2:-}"; shift 2 ;;
        --timerange) TIMERANGE="${2:-}"; shift 2 ;;
        --synthetic) SYNTH="yes"; shift ;;
        -h|--help)   sed -n '2,29p' "$0"; exit 0 ;;
        *) echo "参数错误：不认识 $1（见 bash scripts/round5_week_check.sh --help）" >&2; exit 2 ;;
    esac
done
[[ -n "$STRAT" ]] || { echo "参数错误：要写 --strategy <策略类名>" >&2; exit 2; }
[[ "$TIMERANGE" =~ ^[0-9]{8}-[0-9]{8}$ ]] || { echo "参数错误：--timerange 要写成 YYYYMMDD-YYYYMMDD（收到：$TIMERANGE）" >&2; exit 2; }
if [[ ${#CONFIGS[@]} -eq 0 ]]; then CONFIGS=("configs/config.okx-research.json"); fi
if [[ "$SYNTH" == "yes" ]]; then
    FT_RUNNER_RESOLVED=synthetic
    [[ -n "$DATADIR" ]] || DATADIR="user_data/data/okx-synthetic"
fi

ARGS=()
for c in "${CONFIGS[@]}"; do ARGS+=(--config "$c"); done
OV="configs/research-overrides/${STRAT}.json"
if [[ -f "$OV" ]]; then ARGS+=(--config "$OV"); fi
if [[ -n "$DATADIR" ]]; then ARGS+=(--datadir "$DATADIR"); fi
OUT="user_data/backtest_results/round5_week/${STRAT}_$(date -u +%Y%m%d_%H%M%S)"
mkdir -p "$OUT"
LOG="$OUT/backtest.log"

echo "第 5 轮管道检查（一周小窗口，只数笔数和资金费是否非零，不看收益）：$STRAT，$TIMERANGE"
echo "  配置：${CONFIGS[*]}$([[ -f "$OV" ]] && echo " + $OV")$([[ -n "$DATADIR" ]] && echo "；数据目录 $DATADIR")"
# --enable-protections：和 make research 一样（PremiumFade 的 CooldownPeriod 等保护要打开，笔数才和研究流程一致）
if ! ft backtesting "${ARGS[@]}" --strategy "$STRAT" --timerange "$TIMERANGE" --fee 0.0007 --enable-protections \
        --export trades --backtest-directory "$OUT" --cache none > "$LOG" 2>&1; then
    echo "!! 回测出错，最后 15 行（完整日志：$LOG）："
    tail -n 15 "$LOG" | sed 's/^/   /'
    exit 1
fi
# 策略自己报的"一笔都不开"的数据错误（freqtrade 不会因此失败，笔数会是 0，看起来像"这一周没有信号"）
if grep -qE "找不到上线日文件|上线日文件 .* 读不了" "$LOG"; then
    echo "!! 策略报了数据错误，一笔都不会开，这次检查不算数（完整日志：$LOG）："
    grep -E "找不到上线日文件|上线日文件 .* 读不了" "$LOG" | head -n 3 | sed 's/^/   /'
    if [[ "$STRAT" == "NewListingShortHedged" ]]; then
        echo "   先用 configs/config.okx-research-listings.json 下载全部永续的数据，再生成上线日文件："
        echo "   make listings ARGS=\"--datadir user_data/data/okx --data-start <下载开始日期 YYYYMMDD>\""
    fi
    exit 3
fi

py_ft - "$OUT" "$STRAT" <<'PY'
import json, sys, zipfile
from collections import Counter
from pathlib import Path

out, strat = Path(sys.argv[1]), sys.argv[2]
zips = sorted(out.glob("backtest-result-*.zip"))
if not zips:
    print(f"!! 没有找到回测结果（{out}）")
    sys.exit(1)
with zipfile.ZipFile(zips[-1]) as z:
    name = zips[-1].stem + ".json"  # 统计结果；同一个 zip 里还有 _config.json、策略参数等
    if name not in z.namelist():
        name = next(n for n in z.namelist() if n.endswith(".json") and "_config" not in n)
    doc = json.loads(z.read(name))
trades = (doc.get("strategy", {}).get(strat) or {}).get("trades") or []
tags = Counter(str(t.get("enter_tag") or "（没有标签）") for t in trades)
pairs = {t.get("pair") for t in trades}
nonzero = sum(1 for t in trades if float(t.get("funding_fees") or 0.0) != 0.0)
print(f"  交易 {len(trades)} 笔（按 enter_tag：{'、'.join(f'{k} {v}' for k, v in sorted(tags.items())) or '无'}），有交易的币 {len(pairs)} 个")
if trades:
    print(f"  funding_fees 不为 0 的交易：{nonzero}/{len(trades)}（{nonzero / len(trades):.0%}）"
          + ("  !! 全部为 0：资金费没有记进去（OKX 只有最近约 3 个月的资金费；币安要有 funding_rate 和 mark 数据）" if nonzero == 0 else ""))
else:
    print("  这一周一笔交易都没有（事件型策略一周没有信号是正常的；资金费是否非零这一周看不出来）")
print(f"  （收益没有打印；完整日志和结果在 {out}）")
PY
