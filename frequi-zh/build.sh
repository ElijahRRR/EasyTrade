#!/usr/bin/env bash
# ------------------------------------------------------------------
# 构建 FreqUI 中文版（汉化补丁 + 官方 FreqUI 固定版本）
#
# 做的事：
#   1. 从 GitHub 克隆官方 FreqUI 的固定版本（默认 tag 3.2.1）到 frequi-zh/.build/，
#      并核对上游提交号（FREQUI_COMMIT）——tag 被改动或镜像源内容不同就直接失败
#   2. 打上 frequi-zh/patches/frequi-<版本>-zh-CN.patch（git apply）
#   3. pnpm install --frozen-lockfile（严格按锁文件安装；失败就停止，不会悄悄换依赖版本）
#   4. pnpm build，并用 scripts/check-i18n.mjs 检查中英文 key 是否一致
#   5. 把构建结果放到 frequi-zh/dist/（已在 .gitignore 里），并写入 .uiversion
#
# 用法（在项目根目录或任意目录都行）：
#   bash frequi-zh/build.sh
#   FREQUI_TAG=3.3.0 FREQUI_COMMIT=<3.3.0 的完整提交号> bash frequi-zh/build.sh   # 补丁默认 patches/frequi-3.3.0-zh-CN.patch
#   KEEP_BUILD=1 bash frequi-zh/build.sh      # 保留 frequi-zh/.build/ 里打好补丁的源码（默认构建完删除）
#   ALLOW_LOCKFILE_UPDATE=1 bash frequi-zh/build.sh   # 仅升级调试用：锁文件不匹配时允许普通 install
#
# 需要：git、Node.js >= 22、pnpm（没有 pnpm 时自动用 corepack）、能访问 github.com 和 npm 源。
# 构建完成后用 install-into-venv.sh 安装到 freqtrade（venv 方式），
# Docker 方式见 docker/Dockerfile.freqtrade-zh（镜像里会自动执行本脚本）。
# ------------------------------------------------------------------
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FREQUI_TAG="${FREQUI_TAG:-3.2.1}"
# 每个 FreqUI 版本对应的上游完整提交号（补丁就是基于它生成的）。换版本时在这里加一行
# （见 README「升级 FreqUI」），或者用环境变量 FREQUI_COMMIT 指定；FREQUI_COMMIT= （空）跳过核对，不推荐。
case "$FREQUI_TAG" in
    3.2.1) DEFAULT_FREQUI_COMMIT=4e1717ad1eaaf98447ebe2bdf4c00c8d755d2191 ;;
    *) DEFAULT_FREQUI_COMMIT="" ;;
esac
FREQUI_COMMIT="${FREQUI_COMMIT-$DEFAULT_FREQUI_COMMIT}"
FREQUI_REPO="${FREQUI_REPO:-https://github.com/freqtrade/frequi.git}"
PATCH="${PATCH:-$HERE/patches/frequi-${FREQUI_TAG}-zh-CN.patch}"
WORK_DIR="${WORK_DIR:-$HERE/.build/frequi-${FREQUI_TAG}}"
OUT_DIR="${OUT_DIR:-$HERE/dist}"
UI_VERSION="${UI_VERSION:-${FREQUI_TAG}-zh-CN}"

log() { echo "[frequi-zh] $*"; }
fail() { echo "[frequi-zh] 错误：$*" >&2; exit 1; }

# ---------- 检查工具 ----------
command -v git >/dev/null || fail "找不到 git"
command -v node >/dev/null || fail "找不到 node（需要 Node.js >= 22）"
NODE_MAJOR="$(node -p 'process.versions.node.split(".")[0]')"
[ "$NODE_MAJOR" -ge 22 ] || fail "Node.js 版本太低：$(node --version)，需要 >= 22"
if command -v pnpm >/dev/null; then
    PNPM=(pnpm)
elif command -v corepack >/dev/null; then
    PNPM=(corepack pnpm)
else
    fail "找不到 pnpm，也没有 corepack（Node.js 自带）。可以执行：npm install -g corepack && corepack enable"
fi
[ -f "$PATCH" ] || fail "找不到补丁文件：$PATCH"
PATCH="$(cd "$(dirname "$PATCH")" && pwd)/$(basename "$PATCH")"

# ---------- 1. 克隆官方 FreqUI 固定版本 ----------
log "克隆 FreqUI ${FREQUI_TAG}（${FREQUI_REPO}）→ ${WORK_DIR}"
rm -rf "$WORK_DIR"
mkdir -p "$(dirname "$WORK_DIR")"
git -c advice.detachedHead=false clone --quiet --depth 1 --branch "$FREQUI_TAG" "$FREQUI_REPO" "$WORK_DIR"
UPSTREAM_COMMIT="$(git -C "$WORK_DIR" rev-parse HEAD)"
log "上游提交：${UPSTREAM_COMMIT}"
if [ -n "$FREQUI_COMMIT" ]; then
    [ "$UPSTREAM_COMMIT" = "$FREQUI_COMMIT" ] || fail "上游提交不对：${FREQUI_TAG} 克隆到的是 ${UPSTREAM_COMMIT}，补丁基于 ${FREQUI_COMMIT}。tag 可能被改动过，或者 FREQUI_REPO（${FREQUI_REPO}）的内容和官方不同"
    log "上游提交核对通过（FREQUI_COMMIT）"
else
    log "注意：没有设置 FREQUI_COMMIT，跳过上游提交核对"
fi

# ---------- 2. 打汉化补丁 ----------
log "应用补丁 $(basename "$PATCH")"
if ! APPLY_OUT="$(git -C "$WORK_DIR" apply --check --whitespace=nowarn "$PATCH" 2>&1)"; then
    echo "$APPLY_OUT" >&2
    fail "补丁无法应用到 FreqUI ${FREQUI_TAG}。换新版本时需要先重新生成补丁，见 frequi-zh/README.md 的「升级 FreqUI」"
fi
git -C "$WORK_DIR" apply --whitespace=nowarn "$PATCH"

# ---------- 3. 安装依赖 ----------
cd "$WORK_DIR"
log "安装依赖（${PNPM[*]} install --frozen-lockfile）"
if ! "${PNPM[@]}" install --frozen-lockfile; then
    if [ "${ALLOW_LOCKFILE_UPDATE:-0}" = "1" ]; then
        log "注意：ALLOW_LOCKFILE_UPDATE=1，改用普通 install（依赖版本可能和测试时不同，只用于升级调试）"
        "${PNPM[@]}" install --no-frozen-lockfile
    else
        fail "pnpm install --frozen-lockfile 失败（网络问题，或锁文件和 package.json 不一致）。网络问题请重试或换 npm 源；锁文件问题见 frequi-zh/README.md 的「升级 FreqUI」（调试时可设 ALLOW_LOCKFILE_UPDATE=1）"
    fi
fi

# ---------- 4. 构建 + 检查翻译 ----------
log "构建（pnpm build）"
"${PNPM[@]}" run build
if [ -f scripts/check-i18n.mjs ]; then
    log "检查翻译文件（scripts/check-i18n.mjs）"
    node scripts/check-i18n.mjs --quiet
fi
[ -f dist/index.html ] || fail "构建结果里没有 dist/index.html"

# ---------- 5. 输出 ----------
rm -rf "$OUT_DIR"
mkdir -p "$OUT_DIR"
cp -R dist/. "$OUT_DIR/"
# 与 freqtrade install-ui 写入的 .uiversion 相同的格式（FreqUI 设置页显示这个版本号）
printf '%s' "$UI_VERSION" > "$OUT_DIR/.uiversion"
cd "$HERE"
if [ "${KEEP_BUILD:-0}" = "1" ]; then
    log "保留构建目录 ${WORK_DIR}（KEEP_BUILD=1）"
else
    rm -rf "$WORK_DIR"   # 约 500 MB（含 node_modules）；要保留请设 KEEP_BUILD=1
    rmdir "$(dirname "$WORK_DIR")" 2>/dev/null || true
fi
log "完成：${OUT_DIR}（版本 ${UI_VERSION}，$(find "$OUT_DIR" -type f | wc -l | tr -d ' ') 个文件）"
log "下一步：bash $(basename "$HERE")/install-into-venv.sh [venv 目录]   # 安装到 freqtrade"
