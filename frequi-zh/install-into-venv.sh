#!/usr/bin/env bash
# ------------------------------------------------------------------
# 把 FreqUI 中文版（frequi-zh/dist）安装到 freqtrade 里
#
# 效果和 `freqtrade install-ui` 一样，只是文件来自本地构建，而不是从 GitHub 下载英文版：
#   目标目录 = <site-packages>/freqtrade/rpc/api_server/ui/installed/
#   先清空目录（保留 .gitkeep / fallback_file.html，用的就是 freqtrade 自己的 clean_ui_subdir），
#   再复制 dist 里的全部文件，最后写入 .uiversion（例如 3.2.1-zh-CN）。
# 目标目录由 freqtrade 自己的代码算出（与 install-ui 相同），所以 venv、pip --user、
# 源码安装（pip install -e）和官方 Docker 镜像都适用。
#
# 用法：
#   bash frequi-zh/install-into-venv.sh                     # 安装到项目根目录的 .venv
#   bash frequi-zh/install-into-venv.sh /path/to/.venv      # 安装到指定 venv
#   bash frequi-zh/install-into-venv.sh --python python3    # 用指定的 Python（Docker 镜像里用）
#   可选：--dist <目录>（默认 frequi-zh/dist），--dry-run（只显示目标目录，不改文件）
#
# 注意：之后再执行 `freqtrade install-ui` 会换回官方英文版（版本号不同），重新执行本脚本即可。
# ------------------------------------------------------------------
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$HERE/.." && pwd)"
DIST="$HERE/dist"
PY=""
VENV=""
DRY_RUN=0

fail() { echo "[frequi-zh] 错误：$*" >&2; exit 1; }

while [ $# -gt 0 ]; do
    case "$1" in
        --python) PY="${2:?--python 后面要写 Python 路径}"; shift 2 ;;
        --dist) DIST="${2:?--dist 后面要写目录}"; shift 2 ;;
        --dry-run) DRY_RUN=1; shift ;;
        -h|--help) sed -n '2,22p' "$0"; exit 0 ;;
        -*) fail "未知参数：$1" ;;
        *) VENV="$1"; shift ;;
    esac
done

if [ -z "$PY" ]; then
    VENV="${VENV:-$REPO_ROOT/.venv}"
    [ -x "$VENV/bin/python" ] || fail "找不到 $VENV/bin/python（先 make venv，或者把 venv 目录作为参数传进来）"
    PY="$VENV/bin/python"
fi
[ -f "$DIST/index.html" ] || fail "找不到 $DIST/index.html，先执行：bash frequi-zh/build.sh"
[ -f "$DIST/.uiversion" ] || fail "找不到 $DIST/.uiversion（dist 不是 build.sh 生成的？）"
DIST="$(cd "$DIST" && pwd)"

FRQ_DIST="$DIST" FRQ_DRY_RUN="$DRY_RUN" "$PY" - <<'PYEOF'
import os
import shutil
import sys
from pathlib import Path

try:
    from freqtrade.commands import deploy_commands
    from freqtrade.commands.deploy_ui import clean_ui_subdir, read_ui_version
except ImportError as exc:  # pragma: no cover - environment problem
    sys.exit(f"[frequi-zh] 错误：这个 Python 里没有安装 freqtrade（{exc}）")

dist = Path(os.environ["FRQ_DIST"])
# Same destination as `freqtrade install-ui` (freqtrade/commands/deploy_commands.py)
dest = (Path(deploy_commands.__file__).parents[1] / "rpc/api_server/ui/installed/").resolve()
new_version = (dist / ".uiversion").read_text().strip()
old_version = read_ui_version(dest)

print(f"[frequi-zh] freqtrade 界面目录：{dest}")
print(f"[frequi-zh] 当前版本：{old_version or '未安装'} → 新版本：{new_version}")
if os.environ.get("FRQ_DRY_RUN") == "1":
    print("[frequi-zh] --dry-run：没有修改任何文件")
    sys.exit(0)

clean_ui_subdir(dest)  # keeps .gitkeep / fallback_file.html, like install-ui
dest.mkdir(parents=True, exist_ok=True)
for item in dist.iterdir():
    target = dest / item.name
    if item.is_dir():
        shutil.copytree(item, target, dirs_exist_ok=True)
    else:
        shutil.copy2(item, target)
# install-ui writes the version without a trailing newline
(dest / ".uiversion").write_text(new_version)
count = sum(1 for p in dest.rglob("*") if p.is_file())
print(f"[frequi-zh] 已安装 {count} 个文件。freqtrade 每次请求都从这个目录读文件，浏览器强制刷新（Ctrl+Shift+R）即可看到中文界面。")
PYEOF
