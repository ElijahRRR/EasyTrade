#!/usr/bin/env bash
# ------------------------------------------------------------------
# 按 OKX 当前 24 小时成交额，重新生成回测用的固定交易对列表
#   输出：configs/pairs.okx-usdt-perp.json（会覆盖，旧文件备份为 .bak）
#
# 用法：
#   bash scripts/update_okx_pairs.sh          # 取前 20 个
#   bash scripts/update_okx_pairs.sh 30       # 取前 30 个
#
# 需要能访问 OKX API（美国 VPS 可以；本地开发机看网络情况）。
# 提醒：用"今天"的热门币去回测过去，会有幸存者偏差（已经跌没/下架的币不在列表里），
#       回测结果会偏乐观。可以把列表固定下来长期使用，而不是频繁更新。
# ------------------------------------------------------------------
source "$(dirname "$0")/_common.sh"

TOP_N="${1:-20}"
OUT="configs/pairs.okx-usdt-perp.json"
TMP="$(mktemp)"
ERR_LOG="$(mktemp)"
trap 'rm -f "$TMP" "$ERR_LOG"' EXIT

echo "从 OKX 读取成交量排名（运行方式：$FT_RUNNER_RESOLVED；要拉几百个币的 1 小时K线，可能要等几分钟）..."
# freqtrade 的日志/报错写在 stderr，先存到临时文件，失败时打印出来（不要直接丢掉，否则看不到真正原因）
ft test-pairlist --config configs/config.okx-pairlist-volume.json -1 2>"$ERR_LOG" \
    | tr -d '\r' | grep -E '^[A-Z0-9]+/USDT:USDT$' | head -n "$TOP_N" > "$TMP" || true

COUNT="$(wc -l < "$TMP" | tr -d ' ')"
if [[ "$COUNT" -eq 0 ]]; then
    echo "错误：没有拿到任何交易对。原文件未改动。freqtrade 输出的最后 30 行：" >&2
    tail -n 30 "$ERR_LOG" | sed 's/^/    /' >&2
    echo "常见原因：网络连不上 OKX；或 configs/config.okx-pairlist-volume.json 的设置被 freqtrade 拒绝（看上面的 ERROR 行）。" >&2
    exit 1
fi

[[ -f "$OUT" ]] && cp "$OUT" "$OUT.bak"
python3 - "$TMP" "$OUT" <<'PY'
import json, sys
from datetime import datetime, timezone
pairs = [l.strip() for l in open(sys.argv[1], encoding="utf-8") if l.strip()]
stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
body = {
    "exchange": {
        "pair_whitelist": pairs,
        "pair_blacklist": ["(USDC|FDUSD|TUSD|DAI|USDE)/.*"],
    }
}
text = json.dumps(body, ensure_ascii=False, indent=4)
header = (
    "// 配置片段，不单独使用（被入口配置的 add_config_files 引入）。\n"
    "// OKX USDT 永续交易对（StaticPairList 用）\n"
    f"// 由 scripts/update_okx_pairs.sh 于 {stamp} 按 24h 成交额自动生成，共 {len(pairs)} 个。\n"
    "// 注意：按当前成交量选币回测历史有幸存者偏差。\n"
)
with open(sys.argv[2], "w", encoding="utf-8") as fh:
    fh.write(header + text + "\n")
PY

echo "已写入 $OUT（$COUNT 个交易对）："
cat "$TMP"
echo
echo "提示：如果要和币安对照，记得同步修改 configs/pairs.binance-usdt-perp.json"
