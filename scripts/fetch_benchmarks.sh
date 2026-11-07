#!/usr/bin/env bash
# ------------------------------------------------------------------
# 下载"基准策略"：别人公开的 freqtrade 策略，用来和我们自己的策略在同一套研究流程里比较（make research-benchmarks）。
# 只在你自己的 VPS / 电脑上运行。说明见 docs/benchmarks.md。
#
# 用法：
#   bash scripts/fetch_benchmarks.sh            # 下载（已经下载过、校验通过的文件跳过，几秒钟）
#   bash scripts/fetch_benchmarks.sh --check    # 只检查 user_data/strategies/vendor/ 里的文件在不在、有没有被改过（不联网）
#   bash scripts/fetch_benchmarks.sh --force    # 全部重新下载（本地被改过的文件先备份成 .bak）
#   bash scripts/fetch_benchmarks.sh --list     # 列出要下载哪些文件、固定在哪个版本、许可证
# 下载到 user_data/strategies/vendor/（.gitignore 已忽略，不进 git）。然后运行：make research-benchmarks
#
# 下载什么（版本钉死，见下面的 FS_COMMIT / NFI_COMMIT）：
#   * freqtrade/freqtrade-strategies 的 user_data/strategies/futures/ 目录里的 7 个策略（这个 commit 时那里就这 7 个 .py，
#     没有"只负责下单"的辅助文件，全部是有自己买卖信号的策略；FSampleStrategy 是官方的示例模板）
#   * iterativv/NostalgiaForInfinity（NFI）当前的主策略 NostalgiaForInfinityX8.py（策略里写的版本号 v18.0.214）。
#     它只 import freqtrade / numpy / pandas / talib / rapidjson 和 Python 标准库，没有别的辅助文件要下载
#   * 两个仓库的许可证全文（都是 GPL-3.0）
#
# 安全规则：
#   * 每个文件都从 raw.githubusercontent.com 按固定 commit 下载（不跟着 main 分支变），下载后算 sha256，
#     和下面写死的值不一样就整批作废：vendor/ 里什么都不改，退出码 1。
#   * 绝不下载 NFI 的自动更新工具（docker-compose.yml 里的 nfi-updater 服务、tools/checkupdates.sh）或任何会自己更新的东西：
#     清单里只有策略 .py 文件和许可证文本；清单里出现这类文件名，脚本直接拒绝运行。
#   * 只下载、不运行：本脚本不执行、不导入下载来的任何代码。
#   * 下载来的 .py 只在文件开头加几行 "# [easytrade-vendor] ..." 注释（来源、commit、sha256、许可证），正文一个字不改；
#     --check 去掉这几行再核对 sha256，所以改过一个字都能发现。
#   * 换版本：改下面的 commit 和 sha256（docs/benchmarks.md「换版本」），不要手改 vendor/ 里的文件。
#
# 退出码：0 = 成功 / 检查全部通过；1 = 下载或校验失败（vendor/ 没有被改动）；2 = 参数错误；
#         3 = （--check）有文件缺少；4 = （--check）有文件被改过（或者既有缺少又有被改过）
# 兼容 macOS 自带的 bash 3.2。
# ------------------------------------------------------------------
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

VENDOR_DIR="user_data/strategies/vendor"
MARK="# [easytrade-vendor]"

# ---------- 固定版本（2026-11-07 用 git ls-remote 查到的 main 分支最新 commit） ----------
FS_REPO="freqtrade/freqtrade-strategies"
FS_COMMIT="f3340ce11f5bdf62f598522e64d1f5638eaa13f5"
FS_LICENSE_FILE="LICENSE-freqtrade-strategies.txt"
NFI_REPO="iterativv/NostalgiaForInfinity"
NFI_COMMIT="74c03087b8ced3899509ba8d36f5f1565bc5f49a"
NFI_LICENSE_FILE="LICENSE-NostalgiaForInfinity.txt"

# ---------- 清单 ----------
# 每行：本地文件名|仓库|commit|上游路径|上游原文的 sha256|许可证文件|说明
# sha256 是 2026-11-07 从 raw.githubusercontent.com 按上面的 commit 取下来的原文算的（两次下载结果相同）
MANIFEST="
FAdxSmaStrategy.py|$FS_REPO|$FS_COMMIT|user_data/strategies/futures/FAdxSmaStrategy.py|43ae02ecca4553b8a609d4533cb4d5b6517292d35b41fc4c77f54f8e959ced89|$FS_LICENSE_FILE|1h，ADX + 两条均线交叉，多空
FOttStrategy.py|$FS_REPO|$FS_COMMIT|user_data/strategies/futures/FOttStrategy.py|6e7dd4802915f6952c9f7be6b5fe9840d1c894a89476e1b274fb163b096d930e|$FS_LICENSE_FILE|1h，OTT 指标，只做多（没开 can_short）；在 freqtrade 2026.10（numpy 2 / pandas 3）下用不了，默认不跑（docs/benchmarks.md）
FReinforcedStrategy.py|$FS_REPO|$FS_COMMIT|user_data/strategies/futures/FReinforcedStrategy.py|4148d3d2d736a454a470649646a59b77a62f7d4ccb7f7bc6d2ecc5ad0ec01152|$FS_LICENSE_FILE|5m，EMA 交叉 + 1h 均线过滤，多空
FSampleStrategy.py|$FS_REPO|$FS_COMMIT|user_data/strategies/futures/FSampleStrategy.py|3fcbe2ed16dd097fd6e4548172acbde871bdfe64c5a7341f337f19caf3daa225|$FS_LICENSE_FILE|1h，官方示例模板（RSI + TEMA + 布林带），多空
FSupertrendStrategy.py|$FS_REPO|$FS_COMMIT|user_data/strategies/futures/FSupertrendStrategy.py|93f31258c4b6c05cf2380fc81f235f37355d97a3e4d2ebdf075e948e07978e19|$FS_LICENSE_FILE|1h，三条 Supertrend，只做多（没开 can_short）
TrendFollowingStrategy.py|$FS_REPO|$FS_COMMIT|user_data/strategies/futures/TrendFollowingStrategy.py|6b8f3e272acfd4923539120364b9ddceccc17a385117d12a249dc1460ea53c4f|$FS_LICENSE_FILE|5m，EMA20 + OBV，只做多（没开 can_short）
VolatilitySystem.py|$FS_REPO|$FS_COMMIT|user_data/strategies/futures/VolatilitySystem.py|10a32ea131fb8f4684741b48cd3eae70aa4d4990319b49a7acb7a2f9c7c8a672|$FS_LICENSE_FILE|1h，波动突破（3h ATR），多空，固定 2 倍杠杆，会加仓一次
$FS_LICENSE_FILE|$FS_REPO|$FS_COMMIT|LICENSE|8ceb4b9ee5adedde47b31e975c1d90c73ad27b6b165a1dcd80c7c545eb65b903|-|freqtrade-strategies 的许可证全文（GPL-3.0）
NostalgiaForInfinityX8.py|$NFI_REPO|$NFI_COMMIT|NostalgiaForInfinityX8.py|839b1aa28be14317291d51b1173a075317b2f8a6158d4d3419aa47927e4f1982|$NFI_LICENSE_FILE|5m 主策略 NFI X8（v18.0.214），多空，加仓/分批平仓（DCA），默认 3 倍杠杆
$NFI_LICENSE_FILE|$NFI_REPO|$NFI_COMMIT|LICENSE|3972dc9744f6499f0f9b2dbf76696f2ae7ad8af9b23dde66d6af86c9dfb36986|-|NostalgiaForInfinity 的许可证全文（GPL-3.0）
"

MODE="fetch"
QUIET=0
FORCE=0
while [[ $# -gt 0 ]]; do
    case "$1" in
        --check)  MODE="check"; shift ;;
        --list)   MODE="list"; shift ;;
        --force)  FORCE=1; shift ;;
        --quiet|-q) QUIET=1; shift ;;
        -h|--help) sed -n '2,/^# -----/p' "$0"; exit 0 ;;
        *) echo "未知参数：$1（看 bash scripts/fetch_benchmarks.sh --help）" >&2; exit 2 ;;
    esac
done

say() { if [[ "$QUIET" != "1" ]]; then printf '%s\n' "$*"; fi; }

# ---------- 防呆：清单里绝不能有自动更新工具 ----------
# （NFI 的 nfi-updater、tools/checkupdates.sh 会定时把策略换成上游最新版：实盘逻辑会在没人看过的情况下变掉）
while IFS='|' read -r name repo commit path sum lic desc; do
    [[ -n "$name" ]] || continue
    case "$name $path" in
        *checkupdates*|*updater*|*update*.sh*|*docker-compose*|*.sh\ *|*.sh)
            echo "拒绝运行：清单里有自动更新/脚本类文件（$name ← $path）。基准策略只能下载固定版本的策略文件和许可证。" >&2
            exit 1 ;;
    esac
    case "$name" in
        */*|.*|"") echo "拒绝运行：清单里的本地文件名不对（$name）" >&2; exit 1 ;;
    esac
    [[ "$commit" =~ ^[0-9a-f]{40}$ ]] || { echo "拒绝运行：$name 的 commit 不是 40 位 SHA（$commit）" >&2; exit 1; }
    [[ "$sum" =~ ^[0-9a-f]{64}$ ]] || { echo "拒绝运行：$name 的 sha256 格式不对（$sum）" >&2; exit 1; }
done <<EOF
$MANIFEST
EOF

# ---------- 工具 ----------
sha256_of() {
    if command -v sha256sum > /dev/null 2>&1; then
        sha256sum "$1" | awk '{print $1}'
    elif command -v shasum > /dev/null 2>&1; then
        shasum -a 256 "$1" | awk '{print $1}'
    elif command -v openssl > /dev/null 2>&1; then
        openssl dgst -sha256 "$1" | awk '{print $NF}'
    else
        echo "错误：找不到 sha256sum / shasum / openssl，没法校验文件" >&2
        return 1
    fi
}

# 文件开头有几行是我们加的 "# [easytrade-vendor]" 注释
header_lines() {
    awk -v m="$MARK" 'index($0, m) == 1 { n++; next } { exit } END { print n + 0 }' "$1"
}

# 去掉我们加的注释后，正文（= 上游原文）的 sha256。tail -n +K 原样输出字节（Linux / macOS 都一样）
body_sha256() {
    local f="$1" h tmp sum
    h="$(header_lines "$f")"
    if [[ "$h" -eq 0 ]]; then
        sha256_of "$f"
        return
    fi
    tmp="$(mktemp "${TMPDIR:-/tmp}/easytrade-vendor-check.XXXXXX")"
    tail -n +"$((h + 1))" "$f" > "$tmp"
    sum="$(sha256_of "$tmp")"
    rm -f "$tmp"
    echo "$sum"
}

raw_url() { echo "https://raw.githubusercontent.com/$1/$2/$3"; }

# 状态：ok / missing / modified
file_status() {
    local f="$VENDOR_DIR/$1" want="$2" got
    if [[ ! -f "$f" ]]; then echo "missing"; return; fi
    got="$(body_sha256 "$f")" || { echo "modified"; return; }
    if [[ "$got" == "$want" ]]; then echo "ok"; else echo "modified"; fi
}

# ---------- --list ----------
if [[ "$MODE" == "list" ]]; then
    echo "基准策略清单（下载到 $VENDOR_DIR/，许可证都是 GPL-3.0）："
    while IFS='|' read -r name repo commit path sum lic desc; do
        [[ -n "$name" ]] || continue
        echo "  $name"
        echo "      $desc"
        echo "      来源：https://github.com/$repo/blob/$commit/$path"
        echo "      sha256：$sum"
    done <<EOF
$MANIFEST
EOF
    exit 0
fi

# ---------- --check ----------
if [[ "$MODE" == "check" ]]; then
    n_ok=0; n_missing=0; n_mod=0
    while IFS='|' read -r name repo commit path sum lic desc; do
        [[ -n "$name" ]] || continue
        st="$(file_status "$name" "$sum")"
        case "$st" in
            ok)       n_ok=$((n_ok + 1)); say "  正常    $VENDOR_DIR/$name" ;;
            missing)  n_missing=$((n_missing + 1)); say "  缺少    $VENDOR_DIR/$name" ;;
            *)        n_mod=$((n_mod + 1)); say "  被改过  $VENDOR_DIR/$name（和固定版本的 sha256 对不上）" ;;
        esac
    done <<EOF
$MANIFEST
EOF
    say "检查结果：正常 $n_ok 个，缺少 $n_missing 个，被改过 $n_mod 个"
    if [[ $n_mod -gt 0 ]]; then exit 4; fi
    if [[ $n_missing -gt 0 ]]; then exit 3; fi
    exit 0
fi

# ---------- 下载 ----------
command -v curl > /dev/null 2>&1 || { echo "错误：没有 curl 命令（Ubuntu：sudo apt install curl）" >&2; exit 1; }
sha256_of /dev/null > /dev/null

echo "==> 下载基准策略到 $VENDOR_DIR/（固定版本，下载后逐个核对 sha256）"
echo "    freqtrade-strategies @ ${FS_COMMIT:0:12}    NostalgiaForInfinity @ ${NFI_COMMIT:0:12}"
echo "    不下载 NFI 的自动更新工具（nfi-updater / checkupdates.sh），本脚本也不运行下载来的代码。"

mkdir -p "$(dirname "$VENDOR_DIR")"
TMP_DIR="$(mktemp -d "$(dirname "$VENDOR_DIR")/.vendor-download.XXXXXX")"
cleanup() { rm -rf "$TMP_DIR"; }
trap cleanup EXIT

FAILED=0
TODO=""   # 下载成功、待放进 vendor/ 的文件名（空格分开）
while IFS='|' read -r name repo commit path sum lic desc; do
    [[ -n "$name" ]] || continue
    if [[ "$FORCE" != "1" && "$(file_status "$name" "$sum")" == "ok" ]]; then
        echo "  已有，校验通过，跳过：$name"
        continue
    fi
    url="$(raw_url "$repo" "$commit" "$path")"
    raw="$TMP_DIR/$name.raw"
    if ! curl -fsSL --proto '=https' --tlsv1.2 --retry 3 --retry-delay 2 --connect-timeout 20 --max-time 600 \
            -o "$raw" "$url" < /dev/null; then
        echo "  !! 下载失败：$name（$url）" >&2
        FAILED=$((FAILED + 1))
        continue
    fi
    got="$(sha256_of "$raw")"
    if [[ "$got" != "$sum" ]]; then
        echo "  !! 校验不通过：$name" >&2
        echo "       应该是 $sum" >&2
        echo "       下载到 $got" >&2
        echo "       （上游这个 commit 的文件不会变；对不上多半是网络/代理改了内容，或者有人篡改。不要手动绕过）" >&2
        FAILED=$((FAILED + 1))
        continue
    fi
    case "$name" in
        *.py)
            {
                echo "$MARK 第三方基准策略（不是本项目写的），由 scripts/fetch_benchmarks.sh 下载；下面是上游原文，一个字没改，只在开头加了这几行注释"
                echo "$MARK 上游仓库：https://github.com/$repo"
                echo "$MARK 上游文件：$path"
                echo "$MARK 固定版本（commit）：$commit"
                echo "$MARK 下载地址：$url"
                echo "$MARK 上游原文 sha256：$sum"
                echo "$MARK 许可证：GPL-3.0（全文见同目录 $lic）。自己用没问题；把这个文件或改过的版本发给别人，必须按 GPL-3.0 同时提供源代码"
                echo "$MARK 不要手改：改了以后 make research-benchmarks 会因为校验不通过而拒绝运行。换版本请改 scripts/fetch_benchmarks.sh"
                cat "$raw"
            } > "$TMP_DIR/$name"
            ;;
        *)
            cp "$raw" "$TMP_DIR/$name"
            ;;
    esac
    # 自查：加了注释以后，按 --check 的方法算出来的还是上游原文的 sha256
    if [[ "$(body_sha256 "$TMP_DIR/$name")" != "$sum" ]]; then
        echo "  !! 内部错误：$name 加注释后校验不通过" >&2
        FAILED=$((FAILED + 1))
        continue
    fi
    echo "  下载并校验通过：$name"
    TODO="$TODO $name"
done <<EOF
$MANIFEST
EOF

if [[ $FAILED -gt 0 ]]; then
    echo "!! 有 $FAILED 个文件下载失败或校验不通过，这一批全部作废，$VENDOR_DIR/ 没有任何改动。" >&2
    echo "   检查网络后重新运行 bash scripts/fetch_benchmarks.sh；一直校验不通过请不要使用这些文件。" >&2
    exit 1
fi

mkdir -p "$VENDOR_DIR"
STAMP="$(date +%Y%m%d_%H%M%S)"
for name in $TODO; do
    if [[ -f "$VENDOR_DIR/$name" ]]; then
        bak="$VENDOR_DIR/$name.local-$STAMP.bak"
        mv "$VENDOR_DIR/$name" "$bak"
        echo "  本地的 $name 和固定版本不一样（或用了 --force），旧文件备份成 $bak"
    fi
    mv "$TMP_DIR/$name" "$VENDOR_DIR/$name"
done

# 目录说明（给以后打开这个目录的人看；不参与校验）
{
    echo "这个目录放的是第三方\"基准策略\"，由 scripts/fetch_benchmarks.sh 下载，不是本项目写的代码，也不进 git（.gitignore 已忽略）。"
    echo "用途：make research-benchmarks 把它们放进和我们自己的策略相同的研究流程里比较（docs/benchmarks.md）。"
    echo ""
    echo "固定版本："
    echo "  https://github.com/$FS_REPO  commit $FS_COMMIT"
    echo "  https://github.com/$NFI_REPO  commit $NFI_COMMIT"
    echo ""
    echo "许可证：两个仓库都是 GPL-3.0（全文：$FS_LICENSE_FILE、$NFI_LICENSE_FILE）。"
    echo "自己用没问题；把这些文件或改过的版本发给别人，必须按 GPL-3.0 同时提供源代码。"
    echo ""
    echo "不要手改这些文件（改了校验就不通过）。检查：bash scripts/fetch_benchmarks.sh --check"
    echo "不需要了可以整个删掉这个目录：rm -rf $VENDOR_DIR"
} > "$VENDOR_DIR/README.txt"

echo ""
echo "完成：$VENDOR_DIR/ 里的基准策略全部是固定版本、校验通过。"
echo ""
echo "许可证提醒（GPL-3.0）："
echo "  * freqtrade-strategies 和 NostalgiaForInfinity 都是 GPL-3.0。自己下载、回测、自己用，没有问题。"
echo "  * 如果把这些文件（或者改过的版本、或者把它们拷进去的程序）发给别人，必须按 GPL-3.0 同时提供完整源代码、保留许可证。"
echo "  * 所以它们放在 $VENDOR_DIR/（不进 git），我们自己的仓库里只有下载脚本和封顶杠杆的小子类。"
echo ""
echo "下一步：make research-benchmarks（在 tmux 里跑，要很久；说明见 docs/benchmarks.md）"
