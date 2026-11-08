#!/usr/bin/env python3
"""
预注册凭证检查（第 5 轮起，预注册 docs/round5-preregistration.md 第 3.1 节）：
"研究脚本把每个策略文件的 sha256 写进 plan.json。正式运行时，如果文件和登记的不一致，就拒绝运行。"

登记表：configs/preregistration.json（纯 JSON，不要写注释；由整合的人在所有策略文件定稿后用 write 子命令生成 sha256）
  {
    "round": 5,
    "spec_doc": "docs/round5-preregistration.md",
    "strategies": {
      "<类名>": {"file": "user_data/strategies/<类名>.py", "sha256": "<64 位十六进制>", "venue": "okx" | "binance",
                 "stage": "1", "params": {...}, "min_pf": 1.2, "extra_checks": [...], ...}
    },
    "changes": []      // 正式运行之后的改动记录：日期、旧类名、新类名、改了什么、为什么
  }
  本脚本看 "file"、"sha256"、"pinned_files"，以及 "venue(s)" / "stages" / "config"（这次的研究配置和交易所必须是登记的那个阶段的）
  和 "run_settings"（全局一份，策略和阶段可以各自覆盖：手续费、回撤上限、切分、预热、保留下架币、留出段 ...，
  research.sh 用 --settings 传这次的实际值）；其他字段原样抄进 plan.json 给报告用。
  "formal_runs"（可选）：已经做过的正式运行（从本机的 user_data/research/formal_runs.jsonl 抄过来、提交 git），同一个类 + 阶段再跑 = 重复运行。
  "pinned_files" {路径: sha256}：write 时和策略文件一起固定的文件 = 策略 import 的本地模块（easytrade_risk.py 混入类等）
  和它自己的 configs/research-overrides/<类名>.json；改了它们和改策略文件一样，check 时不一致 = 拒绝。
  "min_pf_pending_decision"（或 min_pf 写成 {"value", "pending_decision": true}）= 还没定的决定：合成数据照常跑（只提醒），
  正式运行拒绝，直到在登记表里定下来（预注册 2.1 节："必须在正式运行之前定下来"）。

子命令：
  check   研究脚本（scripts/research.sh）在任何回测之前调用：
            python3 scripts/check_prereg.py check --strategies "A B" [--strategy-dir DIR] [--config CFG ...]
                [--registry configs/preregistration.json] [--run-dir RUN] [--allow-unregistered] [--synthetic]
          对这次要研究的每个策略：
            * 在登记表里的：找到定义这个类的 .py 文件（策略目录里只能有一个文件定义它），算 sha256，必须和登记的完全一样；
              文件名也必须和登记的 "file" 一样。不一样 = 拒绝（退出码 3），除非 --allow-unregistered
              （RESEARCH_ALLOW_UNREGISTERED=1）：照样跑，但大声提醒，plan.json 里记为"不是正式的预注册运行"。
            * 不在登记表里的：照常研究，记为"没有登记"（以前几轮的策略都是这样）。
            * 登记的策略另外要和登记表一样（不一样 = 拒绝，合成数据只提醒）：交易所（多阶段的策略按交易所对应阶段）、
              这个阶段登记的研究配置（"config" 或 "stages.<阶段>.config"，还没登记 = 拒绝）、运行设置（--settings 对照 run_settings）、
              一起固定的文件集合（现在多出来的 research-override 也算不一致）。
          正式运行（formal_run = true）还要求：读得到 git 提交号；策略文件、一起固定的文件、登记表、研究配置（连同 add_config_files
          引入的币池文件）、规格文档和研究脚本（HARNESS_FILES）都已提交、没有未提交的改动；同一个类 + 阶段以前没有正式运行过
          （--ledger 记录文件和登记表的 formal_runs）。不满足时照常研究，但记为"不是正式运行"并写明原因。
          另外记下：git 提交号、策略文件 import 的本地模块（例如 easytrade_risk.py）、研究配置、research-override 的 sha256。
          --run-dir：结果写进 <RUN>/plan.json 的 "prereg"（plan.json 还没生成时写 <RUN>/prereg.json，下一次调用时并进去）。
          --record-formal：这次是正式运行时，把每个策略记进 --ledger（研究脚本在第一次回测之前加）。
  write   整合的人在策略文件全部定稿后（重新）计算登记表里每个文件的 sha256 并写回登记表：
            python3 scripts/check_prereg.py write [--registry configs/preregistration.json] [--strategies "A B"] [--force]
          已经有 sha256、而且和现在的文件不一样时默认拒绝改（正式运行之后的任何改动都要换新类名，见预注册 3.1 节），
          确认是在正式运行之前修改的才加 --force。正式运行记录（--ledger、登记表的 formal_runs）里有的类，加 --force 也拒绝。
  show    打印登记表（每个策略的文件、sha256、现在的文件是否一致）。

只用 Python 标准库（不需要 freqtrade），git 读不到时照样运行（提交号记为空，结果不算正式运行）。
退出码：0 = 通过（或用 --allow-unregistered 放行）；3 = 不通过（文件和登记的不一致、登记表读不了）；2 = 参数错误。
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent.parent
DEFAULT_REGISTRY = "configs/preregistration.json"
OVERRIDES_DIR = "configs/research-overrides"
# 正式运行记录（每个登记的类 + 阶段只能有一次正式运行；user_data/research/ 在 .gitignore 里，只在这台机器上）
DEFAULT_LEDGER = "user_data/research/formal_runs.jsonl"
# 研究流程和判定用的脚本：正式运行时也要已经提交、没有改动（预注册 3.1 节：规格、代码、配置一起提交）
HARNESS_FILES = (
    "Makefile",
    "scripts/research.sh",
    "scripts/_common.sh",
    "scripts/research_report.py",
    "scripts/check_drawdown.py",
    "scripts/check_prereg.py",
    "scripts/build_listings.py",
    "scripts/download_okx_data.sh",
)
SHA_RE = re.compile(r"^[0-9a-f]{64}$")
EXIT_OK, EXIT_USAGE, EXIT_REFUSED = 0, 2, 3


# ----------------------------------------------------------------------------
# 小工具
# ----------------------------------------------------------------------------
def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def rel(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(ROOT))
    except ValueError:
        return str(path)


def repo_path(text: str) -> Path:
    p = Path(text)
    return p if p.is_absolute() else (ROOT / p)


def now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _strip_json_comments(text: str) -> str:
    """去掉 // 和 /* */ 注释（不碰字符串里的内容），给读 freqtrade 配置用。"""
    out, i, n, in_str = [], 0, len(text), False
    while i < n:
        c = text[i]
        if in_str:
            out.append(c)
            if c == "\\" and i + 1 < n:
                out.append(text[i + 1])
                i += 2
                continue
            if c == '"':
                in_str = False
            i += 1
            continue
        if c == '"':
            in_str = True
            out.append(c)
            i += 1
        elif text.startswith("//", i):
            j = text.find("\n", i)
            i = n if j < 0 else j
        elif text.startswith("/*", i):
            j = text.find("*/", i + 2)
            i = n if j < 0 else j + 2
        else:
            out.append(c)
            i += 1
    return re.sub(r",(\s*[}\]])", r"\1", "".join(out))


def load_registry(path: Path) -> tuple[dict[str, Any] | None, str]:
    """返回 (登记表, 出错原因)。登记表不存在 = (None, "")。"""
    if not path.is_file():
        return None, ""
    text = path.read_text(encoding="utf-8")
    try:
        doc = json.loads(text)
    except json.JSONDecodeError as exc:
        return None, f"登记表 {rel(path)} 不是合法的 JSON（{exc}；登记表里不要写注释）"
    if not isinstance(doc, dict) or not isinstance(doc.get("strategies"), dict):
        return None, f"登记表 {rel(path)} 里没有 \"strategies\"（应该是 {{类名: {{file, sha256, ...}}}}）"
    for name, ent in doc["strategies"].items():
        if not isinstance(ent, dict) or not isinstance(ent.get("file"), str) or not ent["file"]:
            return None, f"登记表 {rel(path)} 里 {name} 没有写 \"file\"（策略文件路径）"
    return doc, ""


def classes_in_file(path: Path) -> set[str]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (SyntaxError, UnicodeDecodeError, OSError):
        return set()
    return {n.name for n in tree.body if isinstance(n, ast.ClassDef)}


def local_imports(path: Path, sdir: Path) -> list[Path]:
    """策略文件 import 的、策略目录里的本地模块（例如 easytrade_risk.py）。只看第一层。"""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (SyntaxError, UnicodeDecodeError, OSError):
        return []
    mods: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            mods += [a.name.split(".")[0] for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            mods.append(node.module.split(".")[0])
    out = []
    for m in dict.fromkeys(mods):
        f = sdir / f"{m}.py"
        if f.is_file() and f.resolve() != path.resolve():
            out.append(f)
    return out


def config_chain(paths: list[str]) -> list[Path]:
    """研究配置和它的 add_config_files（按 freqtrade 的规则：相对路径按所在文件的目录算），去重保序。"""
    seen: list[Path] = []

    def visit(p: Path, depth: int) -> None:
        if depth > 5 or not p.is_file() or p.resolve() in [s.resolve() for s in seen]:
            return
        seen.append(p)
        try:
            doc = json.loads(_strip_json_comments(p.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, UnicodeDecodeError, OSError):
            return
        subs = doc.get("add_config_files") if isinstance(doc, dict) else None
        for sub in subs if isinstance(subs, list) else []:
            sp = Path(str(sub))
            visit(sp if sp.is_absolute() else (p.resolve().parent / sp), depth + 1)

    for c in paths:
        visit(repo_path(c), 0)
    return seen


def config_exchange(chain: list[Path]) -> str:
    name = ""
    for p in chain:  # 后面的文件覆盖前面的（只取 exchange.name）
        try:
            doc = json.loads(_strip_json_comments(p.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, UnicodeDecodeError, OSError):
            continue
        ex = doc.get("exchange") if isinstance(doc, dict) else None
        if isinstance(ex, dict) and ex.get("name"):
            name = str(ex["name"]).lower()
    return name


def _git(*args: str) -> str | None:
    try:
        res = subprocess.run(["git", "-C", str(ROOT), *args], capture_output=True, text=True, timeout=20, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    return res.stdout.strip() if res.returncode == 0 else None


def git_info(files: list[Path]) -> dict[str, Any]:
    head = _git("rev-parse", "HEAD")
    info: dict[str, Any] = {
        "available": head is not None,
        "commit": head,
        "branch": _git("rev-parse", "--abbrev-ref", "HEAD") if head else None,
        "files": {},
    }
    if head is None:
        return info
    status = _git("status", "--porcelain", "--untracked-files=all")
    info["dirty"] = bool(status)
    for f in files:
        r = rel(f)
        tracked = _git("ls-files", "--error-unmatch", r) is not None
        diff = _git("status", "--porcelain", "--", r) if tracked else None
        info["files"][r] = {"tracked": tracked, "clean": bool(tracked and diff == "")}
    return info


def _short(sha: str | None) -> str:
    return (sha or "")[:12] or "（没有）"


# ----------------------------------------------------------------------------
# check
# ----------------------------------------------------------------------------
def find_class_files(sdir: Path, name: str) -> list[Path]:
    return [p for p in sorted(sdir.glob("*.py")) if name in classes_in_file(p)]


def pinned_now(name: str, strategy_file: Path) -> dict[str, str]:
    """登记时一起固定的文件：策略 import 的本地模块（例如 easytrade_risk.py 混入类）和它自己的 research-override。
    这些文件也决定策略的行为（止损、仓位、max_open_trades），改了它们和改策略文件一样。"""
    files = local_imports(strategy_file, strategy_file.parent)
    ov = ROOT / OVERRIDES_DIR / f"{name}.json"
    if ov.is_file():
        files.append(ov)
    return {rel(f): sha256_file(f) for f in files}


def check_pinned(ent: dict[str, Any], sdir: Path) -> str:
    """登记表里的 "pinned_files" {路径: sha256}：现在的文件必须一样。策略目录里的模块按文件名在 sdir 里找
    （研究脚本核对的是结果目录里的副本），其他文件（research-override）按仓库路径找。返回不一致的原因，一致 = ""。"""
    pins = ent.get("pinned_files")
    if not pins:
        return ""
    if not isinstance(pins, dict):
        return "登记表里 pinned_files 不是 {路径: sha256}"
    bad = []
    for path_txt, want in pins.items():
        p = Path(str(path_txt))
        f = sdir / p.name if p.parent.as_posix() == "user_data/strategies" else repo_path(str(path_txt))
        if not f.is_file():
            bad.append(f"{path_txt} 不存在")
        elif sha256_file(f) != str(want).lower():
            bad.append(f"{path_txt} 的 sha256 是 {_short(sha256_file(f))}…，登记的是 {_short(str(want))}…")
    return ("登记时一起固定的文件改过：" + "；".join(bad)) if bad else ""


def pending_decisions(ent: dict[str, Any]) -> list[str]:
    """登记表里标成"待定"的决定（和 scripts/research_report.py prereg_rules 认的写法一样）。"""
    out = []
    mp = ent.get("min_pf")
    flag = ent.get("min_pf_pending_decision") or (mp.get("pending_decision") if isinstance(mp, dict) else None)
    pd_any = ent.get("pending_decision")
    if not flag and (pd_any is True or (isinstance(pd_any, (list, tuple, dict)) and "min_pf" in pd_any)):
        flag = True
    if flag:
        val = mp.get("value") if isinstance(mp, dict) else mp
        out.append(f"min_pf 现在写的是 {val}，还没定" + (f"：{flag}" if isinstance(flag, str) else ""))
    return out


def pinned_set_now(name: str, strategy_file: Path, sdir: Path) -> set[str]:
    """现在 "应该一起固定" 的文件集合（路径写法和登记表 pinned_files 一样：策略目录里的模块写成 user_data/strategies/<文件名>）。
    研究脚本核对的是结果目录里的副本，副本目录里的模块按文件名换回 user_data/strategies/ 下的路径。"""
    out = {f"user_data/strategies/{f.name}" for f in local_imports(strategy_file, sdir)}
    ov = ROOT / OVERRIDES_DIR / f"{name}.json"
    if ov.is_file():
        out.add(rel(ov))
    return out


def check_pinned_set(ent: dict[str, Any], name: str, strategy_file: Path, sdir: Path) -> str:
    """登记的 pinned_files 和现在应该固定的文件集合必须一样：现在多出来的（例如后来新加的 research-override，
    研究脚本会把它叠加在配置后面、改掉 stoploss / max_open_trades 等）和登记了却不见了的都算不一致。"""
    pins = ent.get("pinned_files") if isinstance(ent.get("pinned_files"), dict) else {}
    now = pinned_set_now(name, strategy_file, sdir)
    extra = sorted(now - set(pins))
    gone = sorted(set(pins) - now)
    bad = [f"{p} 现在存在、但登记时没有固定" for p in extra] + [f"{p} 登记时固定了、现在不再被用到" for p in gone]
    return ("一起固定的文件和登记的不一样：" + "；".join(bad)) if bad else ""


def _norm_setting(v: Any) -> Any:
    """运行设置比较用：数字（包括写成字符串的数字）按数值比，其他按去掉空白的字符串比。"""
    if v is None:
        return ""
    if isinstance(v, bool):
        return float(v)
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip()
    try:
        return float(s)
    except ValueError:
        return s


def stage_of(ent: dict[str, Any], venue_actual: str) -> tuple[str | None, dict[str, Any]]:
    """这次跑的是登记表里的哪个阶段：有 "stages" 时按交易所对应（例如 okx = 第一阶段、binance = 第二阶段）。
    返回 (阶段名, 阶段的登记内容)；对不上 = (None, {})。没有 "stages" 时用 "stage"（可以没有）。"""
    stages = ent.get("stages") if isinstance(ent.get("stages"), dict) else {}
    if stages:
        for k, st in stages.items():
            v = str((st or {}).get("venue") or "").lower()
            if v and venue_actual and (venue_actual.startswith(v) or v.startswith(venue_actual)):
                return str(k), (st if isinstance(st, dict) else {})
        return None, {}
    return (str(ent["stage"]) if ent.get("stage") is not None else None), {}


def required_settings(registry: dict[str, Any], ent: dict[str, Any], stage_ent: dict[str, Any]) -> dict[str, Any]:
    """登记表冻结的运行设置：全局 run_settings，再叠加这个策略的 run_settings、这个阶段的 run_settings（后面的覆盖前面的）。"""
    out: dict[str, Any] = {}
    for src in (registry.get("run_settings"), ent.get("run_settings"), stage_ent.get("run_settings")):
        if isinstance(src, dict):
            out.update({k: v for k, v in src.items() if not str(k).startswith("_")})
    return out


def read_ledger(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(rec, dict) and rec.get("class"):
            rows.append(rec)
    return rows


def formal_runs_of(name: str, ledger: list[dict[str, Any]], registry: dict[str, Any] | None) -> list[dict[str, Any]]:
    """这个类已经有过的正式运行：本机的记录文件 + 登记表里的 "formal_runs"（正式运行之后抄进登记表、提交 git，别的机器也看得到）。"""
    reg_rows = (registry or {}).get("formal_runs")
    rows = list(ledger) + ([r for r in reg_rows if isinstance(r, dict)] if isinstance(reg_rows, list) else [])
    return [r for r in rows if r.get("class") == name]


def cmd_check(a: argparse.Namespace) -> int:
    names = list(dict.fromkeys(a.strategies.split()))
    if not names:
        print("参数错误：--strategies 不能为空", file=sys.stderr)
        return EXIT_USAGE
    sdir = Path(a.strategy_dir)
    if not sdir.is_dir():
        print(f"参数错误：策略目录 {a.strategy_dir} 不存在", file=sys.stderr)
        return EXIT_USAGE
    settings: dict[str, Any] | None = None
    if a.settings:
        try:
            settings = json.loads(a.settings)
        except json.JSONDecodeError as exc:
            print(f"参数错误：--settings 不是合法的 JSON（{exc}）", file=sys.stderr)
            return EXIT_USAGE
        if not isinstance(settings, dict):
            print("参数错误：--settings 要是 JSON 对象 {名字: 值}", file=sys.stderr)
            return EXIT_USAGE
    reg_path = repo_path(a.registry)
    registry, reg_err = load_registry(reg_path)
    reg_entries: dict[str, Any] = (registry or {}).get("strategies") or {}
    chain = config_chain(a.config or [])
    venue_actual = config_exchange(chain)
    allow = bool(a.allow_unregistered)
    ledger_path = repo_path(a.ledger)
    ledger = read_ledger(ledger_path)
    run_dir_rel = rel(Path(a.run_dir)) if a.run_dir else ""
    # 合成数据 / 只做数据检查：永远不是正式运行；交易所、配置、运行设置、待定的决定只提醒（文件不一致照样拒绝）
    lenient = bool(a.synthetic or a.pipeline_only)
    spec_doc = (registry or {}).get("spec_doc")

    strategies: dict[str, Any] = {}
    problems: list[str] = []
    warnings: list[str] = []
    files_for_git: list[Path] = []
    for n in names:
        found = find_class_files(sdir, n)
        rec: dict[str, Any] = {"registered": n in reg_entries}
        cur = found[0] if len(found) == 1 else None
        if cur is not None:
            rec["file"] = rel(cur)
            rec["sha256"] = sha256_file(cur)
            files_for_git.append(cur)
            deps = local_imports(cur, sdir)
            rec["imports"] = {rel(d): sha256_file(d) for d in deps}
        elif len(found) > 1:
            rec["file"] = None
            rec["ambiguous_files"] = [rel(p) for p in found]
        else:
            rec["file"] = None
        ov = ROOT / OVERRIDES_DIR / f"{n}.json"
        if ov.is_file():
            rec["research_override"] = {"file": rel(ov), "sha256": sha256_file(ov)}
        if n in reg_entries:
            ent = reg_entries[n]
            reg_file = str(ent.get("file"))
            reg_sha = str(ent.get("sha256") or "").lower()
            rec["registry_entry"] = ent
            rec["registered_file"] = reg_file
            rec["registered_sha256"] = reg_sha or None
            orig = repo_path(reg_file)
            if orig.is_file():
                rec["registered_file_sha256_now"] = sha256_file(orig)
                if orig not in files_for_git:
                    files_for_git.append(orig)
            pins = ent.get("pinned_files")
            for ptxt in pins if isinstance(pins, dict) else ():
                pp = repo_path(str(ptxt))
                if pp.is_file() and pp not in files_for_git:
                    files_for_git.append(pp)
            # 交易所：可以写一个（"venue"），也可以写列表（"venues"，两个阶段在不同交易所跑的策略）
            raw_v = ent.get("venues", ent.get("venue"))
            venues = [str(v).lower() for v in (raw_v if isinstance(raw_v, list) else [raw_v]) if v]
            if venues:
                rec["venue_expected"] = venues[0] if len(venues) == 1 else venues
            rec["venue_actual"] = venue_actual or None
            if not chain:
                rec["venue_match"] = None  # 没有传研究配置：没法核对（不算正式运行）
            elif venues:
                rec["venue_match"] = bool(venue_actual) and any(
                    venue_actual.startswith(v) or v.startswith(venue_actual) for v in venues
                )
            else:
                rec["venue_match"] = True
            # 阶段（有 "stages" 时按交易所对应）和这个阶段登记的研究配置
            stage, stage_ent = stage_of(ent, venue_actual)
            rec["stage"] = stage
            reg_cfg = stage_ent.get("config") if isinstance(ent.get("stages"), dict) and ent.get("stages") else ent.get("config")
            rec["config_expected"] = str(reg_cfg) if reg_cfg else None
            rec["config_actual"] = rel(chain[0]) if chain else None
            cfg_why = ""
            if not chain:
                rec["config_match"] = None
            elif not reg_cfg:
                rec["config_match"] = False
                what = ("第 %s 阶段" % stage, 'stages."%s".config' % stage) if stage else ("这个策略", "config")
                cfg_why = f"登记表里还没有{what[0]}的研究配置（{what[1]}）：先定好、写进登记表、提交 git 再跑"
            else:
                rec["config_match"] = rel(repo_path(str(reg_cfg))) == rel(chain[0])
                if not rec["config_match"]:
                    cfg_why = f"登记的研究配置是 {reg_cfg}，这次用的是 {rel(chain[0])}"
            # 运行设置（手续费、回撤上限、切分、预热、保留下架币、留出段 ...）
            want = required_settings(registry or {}, ent, stage_ent)
            rec["settings_expected"] = want
            set_bad: list[str] = []
            if settings is None:
                rec["settings_match"] = None
            else:
                for k, v in want.items():
                    got = settings.get(k)
                    if k not in settings or _norm_setting(got) != _norm_setting(v):
                        set_bad.append(f"{k} = {got if k in settings else '（没有传）'}，登记的是 {v}")
                rec["settings_match"] = not set_bad
                if set_bad:
                    rec["settings_mismatch"] = set_bad
            # 已经有过的正式运行（同一个类、同一个阶段只能有一次）
            earlier = [r for r in formal_runs_of(n, ledger, registry)
                       if str(r.get("stage")) == str(stage) and r.get("run_dir") != run_dir_rel]
            rec["formal_repeat"] = bool(earlier)
            if earlier:
                rec["earlier_formal_runs"] = earlier
            why = ""
            if len(found) > 1:
                why = f"策略目录 {rel(sdir)} 里有 {len(found)} 个文件都定义了 {n}（{'、'.join(rel(p) for p in found)}），没法确定 freqtrade 会用哪一个"
            elif cur is None:
                why = f"策略目录 {rel(sdir)} 里找不到定义 {n} 的文件"
            elif not SHA_RE.match(reg_sha):
                why = f"登记表里还没有 {n} 的 sha256（整合的人先运行：python3 scripts/check_prereg.py write）"
            elif Path(reg_file).name != cur.name:
                why = f"登记的文件是 {reg_file}，可是 {n} 现在定义在 {rel(cur)}"
            elif rec["sha256"] != reg_sha:
                why = f"文件 {rel(cur)} 的 sha256 是 {_short(rec['sha256'])}…，登记的是 {_short(reg_sha)}…（登记之后文件改过）"
            elif pin_err := check_pinned(ent, sdir):
                why = pin_err
            elif set_err := check_pinned_set(ent, n, cur, sdir):
                why = set_err
            rec["match"] = not why
            if why:
                rec["mismatch"] = why
                problems.append(f"{n}：{why}")
            rec["status"] = "registered_ok" if not why else "mismatch"
            # 交易所 / 研究配置 / 运行设置和登记的不一样：正式运行拒绝（合成数据只提醒）
            run_bad = []
            if rec["venue_match"] is False:
                run_bad.append(f"登记的交易所是 {rec.get('venue_expected')}，这次的配置是 {venue_actual or '（读不出来）'}")
            elif venues and len(venues) > 1 and stage is None and chain:
                run_bad.append(f"登记表的 stages 里没有交易所 {venue_actual} 的阶段")
            if cfg_why:
                run_bad.append(cfg_why)
            if set_bad:
                run_bad.append("运行设置和登记的不一样：" + "；".join(set_bad))
            if run_bad:
                rec["run_mismatch"] = run_bad
                for msg in run_bad:
                    (warnings if lenient else problems).append(f"{n}：{msg}")
            pend = pending_decisions(ent)
            if pend:
                rec["pending_decisions"] = pend
                if not lenient:
                    problems.append(f"{n}：登记表里还有待定的决定（{'；'.join(pend)}），预注册要求在正式运行之前定下来")
            if earlier and not a.synthetic:
                warnings.append(
                    f"{n}：{'第 ' + stage + ' 阶段' if stage else ''}已经正式运行过（"
                    + "、".join(f"{r.get('run_id') or r.get('run_dir')}，{str(r.get('date_utc') or '')[:10]}" for r in earlier)
                    + "）。预注册每个规格只跑一次：这次记为重复运行，不算正式运行")
        else:
            rec["status"] = "unregistered"
            rec["match"] = None
        strategies[n] = rec

    if reg_err:
        problems.insert(0, reg_err)
    registered = [n for n in names if strategies[n]["registered"]]
    refused = bool(problems) and not allow
    if reg_path.is_file():
        files_for_git.append(reg_path)
    # 正式运行时也要已经提交的：研究配置（连同 add_config_files 引入的币池文件）、规格文档、研究流程和判定的脚本
    frozen_extra: list[Path] = list(chain)
    if spec_doc:
        frozen_extra.append(repo_path(str(spec_doc)))
    frozen_extra += [ROOT / f for f in HARNESS_FILES]
    for p in frozen_extra:
        if p not in files_for_git:
            files_for_git.append(p)
    git = git_info(files_for_git)
    git_ok = bool(git.get("available") and git.get("commit"))

    def committed(path_txt: str | None) -> bool | None:
        """这个文件有没有提交到 git、而且没有未提交的改动。读不到 git = None（不算正式运行）。"""
        if not git_ok or not path_txt:
            return None
        return bool(git["files"].get(path_txt, {}).get("clean"))

    reg_committed = committed(rel(reg_path)) if reg_path.is_file() else None
    frozen_state = {rel(p): committed(rel(p)) for p in frozen_extra}
    frozen_dirty = [p for p, ok in frozen_state.items() if ok is not True]
    for n in names:
        r = strategies[n]
        # 看登记的原文件（研究脚本核对的是结果目录里的副本，副本不在 git 里）
        r["git_committed"] = committed(rel(repo_path(r["registered_file"]))) if r["registered"] else None
        # 一起固定的文件（easytrade_risk.py、research-override 等）也要先提交
        pins = (r.get("registry_entry") or {}).get("pinned_files") if r["registered"] else None
        pin_state = [committed(rel(repo_path(str(p)))) for p in pins] if isinstance(pins, dict) else []
        r["pinned_committed"] = (None if not git_ok else True) if not pin_state else (
            None if None in pin_state else all(pin_state))
        not_formal: list[str] = []
        if r["registered"]:
            if not git_ok:
                not_formal.append("读不到 git（没装 git、不是 git 仓库或者读不到提交号），没法确认已经提交")
            else:
                if r["git_committed"] is not True:
                    not_formal.append(f"策略文件 {r['registered_file']} 没有提交或者有未提交的改动")
                if r["pinned_committed"] is not True:
                    not_formal.append("一起固定的文件没有提交或者有未提交的改动")
                if reg_committed is not True:
                    not_formal.append(f"登记表 {rel(reg_path)} 没有提交或者有未提交的改动")
                if frozen_dirty:
                    not_formal.append("这些文件没有提交或者有未提交的改动：" + "、".join(frozen_dirty[:8])
                                      + (" ..." if len(frozen_dirty) > 8 else ""))
            if r.get("registered_file_sha256_now") not in (None, r.get("registered_sha256")):
                not_formal.append(f"仓库里的 {r['registered_file']} 和登记的 sha256 不一样")
            if r.get("venue_match") is None or r.get("config_match") is None:
                not_formal.append("没有传研究配置（--config），没法核对交易所和研究配置")
            if r.get("settings_match") is None:
                not_formal.append("没有传运行设置（--settings），没法核对手续费、回撤上限、切分等")
            if r.get("formal_repeat"):
                not_formal.append("同一个类、同一个阶段已经正式运行过")
        r["not_formal_reasons"] = not_formal
        r["formal"] = bool(
            r["registered"] and r.get("match") and not allow and not lenient and not reg_err
            and not r.get("run_mismatch") and not r.get("pending_decisions") and not not_formal
        )
    formal_run = bool(registry) and bool(registered) and all(strategies[n]["formal"] for n in registered)
    nf_reasons = list(dict.fromkeys(x for n in registered for x in strategies[n]["not_formal_reasons"]))
    if not registered:
        note = "这次研究的策略都没有登记（或者没有登记表），不是预注册的正式运行"
    elif a.synthetic:
        note = "合成数据：只检查流程，不是正式运行（登记的文件照样核对）"
    elif a.pipeline_only:
        note = "只做数据检查（管道检查，--data-check-only）：不回测，不是正式运行（登记的文件照样核对）"
    elif allow and problems:
        note = "用了 RESEARCH_ALLOW_UNREGISTERED=1：不是正式的预注册运行"
    elif problems:
        note = "预注册检查不通过"
    elif allow:
        note = "用了 RESEARCH_ALLOW_UNREGISTERED=1：不是正式的预注册运行"
    elif nf_reasons:
        note = "文件和登记表一致，但不算正式运行：" + "；".join(nf_reasons)
    else:
        note = ("登记过的策略文件、一起固定的文件、研究配置、规格文档和研究脚本都和登记的一致、都已提交 git，"
                "交易所、阶段和运行设置也和登记的一样：这是正式的预注册运行")
    result = {
        "checked_utc": now_utc(),
        "registry": rel(reg_path),
        "registry_found": registry is not None,
        "registry_error": reg_err or None,
        "registry_sha256": sha256_file(reg_path) if reg_path.is_file() else None,
        "registry_round": (registry or {}).get("round"),
        "spec_doc": spec_doc,
        "strategy_dir": rel(sdir),
        "allow_unregistered": allow,
        "synthetic": bool(a.synthetic),
        "pipeline_only": bool(a.pipeline_only),
        "refused": refused,
        "formal_run": formal_run,
        "formal_note": note,
        "not_formal_reasons": nf_reasons,
        "problems": problems,
        "warnings": warnings,
        "git": git,
        "registry_git_committed": reg_committed,
        "frozen_files_committed": frozen_state,
        "configs": {rel(p): sha256_file(p) for p in chain},
        "settings": settings,
        "ledger": rel(ledger_path),
        "strategies": strategies,
    }

    # ---- 打印 ----
    g = result["git"]
    commit_txt = f"git 提交 {g['commit'][:10]}{'（工作区有未提交的改动）' if g.get('dirty') else ''}" if g.get("commit") else "读不到 git 提交号"
    if registry is None and not reg_err:
        print(f"预注册检查：没有登记表 {rel(reg_path)}，所有策略都按\"没有登记\"照常研究（{commit_txt}）")
    else:
        rnd = f"第 {registry.get('round')} 轮，" if registry and registry.get("round") is not None else ""
        print(f"预注册检查（登记表 {rel(reg_path)}，{rnd}{commit_txt}）：")
    for n in names:
        r = strategies[n]
        if r["status"] == "registered_ok":
            extra = ""
            if r.get("git_committed") is False:
                extra = "；!! 这个文件还没有提交到 git（或者有未提交的改动），按预注册要先提交再正式运行"
            if r.get("stage"):
                extra += f"；第 {r['stage']} 阶段"
            for msg in r.get("run_mismatch") or []:
                extra += f"；!! {msg}" + ("（合成数据只提醒）" if a.synthetic else ("（只做数据检查，只提醒）" if a.pipeline_only else ""))
            if r.get("pending_decisions"):
                extra += f"；!! 待定：{'；'.join(r['pending_decisions'])}" + ("（合成数据不影响）" if a.synthetic else ("（只做数据检查不影响）" if a.pipeline_only else ""))
            print(f"  - {n}：已登记，sha256 一致（{_short(r['sha256'])}…，{r['file']}）{extra}")
        elif r["status"] == "mismatch":
            print(f"  - {n}：!! 和登记的不一致：{r['mismatch']}")
        else:
            print(f"  - {n}：没有登记（照常研究，结果不算预注册的正式运行）")
    if reg_err:
        print(f"  !! {reg_err}")
    for w in warnings:
        if not a.synthetic:
            print(f"  !! {w}")
    if problems and allow:
        print("!! ============================================================")
        print("!! RESEARCH_ALLOW_UNREGISTERED=1：预注册检查没通过也照样跑。")
        print("!! 这次的结果【不是正式的预注册运行】，不能当成预注册检验的结论（plan.json 里记为 formal_run = false）。")
        print("!! ============================================================")
    elif problems:
        print("!! 预注册检查不通过，研究停止（正式运行必须用登记时的文件、配置和运行设置，见 docs/round5-preregistration.md 第 3.1 节和第 6 节）。")
        if any(strategies[n].get("pending_decisions") for n in names):
            print("   登记表里标成\"待定\"的决定（例如 min_pf）要在正式运行之前定下来：改好 configs/preregistration.json、"
                  "删掉 min_pf_pending_decision，提交 git 以后再运行（见 docs/round5-preregistration.md「登记表」一节）。")
        if any(strategies[n].get("run_mismatch") for n in names):
            print("   交易所、研究配置、运行设置要和登记表一样（configs/preregistration.json 的 config / stages / run_settings）："
                  "按 README「第 5 步」里的命令运行，不要另外改 RESEARCH_* 环境变量。")
        print("   正式运行之后的任何改动都算新策略：换新类名（例如 XSFundingCarry2），在登记表 changes 里写明日期、改了什么、为什么。")
        print("   正式运行之前改的：整合的人重新登记（python3 scripts/check_prereg.py write --force），连同文件一起提交 git。")
        print("   只是想试跑（结果不算正式运行）：RESEARCH_ALLOW_UNREGISTERED=1 make research ...")
    elif registered and not lenient:
        print(f"  {'!! ' if not formal_run else ''}{note}")

    # ---- 写结果 ----
    if a.run_dir:
        run_dir = Path(a.run_dir)
        run_dir.mkdir(parents=True, exist_ok=True)
        plan_path = run_dir / "plan.json"
        if plan_path.is_file():
            plan = json.loads(plan_path.read_text(encoding="utf-8"))
            early = run_dir / "prereg.json"
            if early.is_file() and "prereg_early" not in plan:
                try:
                    plan["prereg_early"] = json.loads(early.read_text(encoding="utf-8"))
                except json.JSONDecodeError:
                    pass
            plan["prereg"] = result
            write_json_atomic(plan_path, plan)
        else:
            write_json_atomic(run_dir / "prereg.json", result)
    # 正式运行记录：研究脚本在第一次回测之前（第二次检查）加 --record-formal，同一个类 + 阶段以后再跑就记为重复运行
    if a.record_formal and formal_run and not refused:
        have = {(r.get("class"), str(r.get("stage")), r.get("run_dir")) for r in ledger}
        rows = []
        for n in registered:
            r = strategies[n]
            key = (n, str(r.get("stage")), run_dir_rel)
            if key in have:
                continue
            rows.append({"class": n, "stage": r.get("stage"), "venue": r.get("venue_actual"),
                         "run_id": Path(run_dir_rel).name if run_dir_rel else None, "run_dir": run_dir_rel,
                         "commit": git.get("commit"), "sha256": r.get("sha256"),
                         "registry_sha256": result["registry_sha256"], "date_utc": result["checked_utc"]})
        if rows:
            ledger_path.parent.mkdir(parents=True, exist_ok=True)
            with ledger_path.open("a", encoding="utf-8") as fh:
                for row in rows:
                    fh.write(json.dumps(row, ensure_ascii=False) + "\n")
            print(f"  正式运行已记进 {rel(ledger_path)}（{'、'.join(r['class'] for r in rows)}）：同一个类、同一个阶段以后再跑记为重复运行。"
                  "跑完以后把这几行抄进登记表的 formal_runs、提交 git（别的机器也就知道它跑过了）。")
    return EXIT_REFUSED if refused else EXIT_OK


# ----------------------------------------------------------------------------
# write / show
# ----------------------------------------------------------------------------
def write_json_atomic(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(obj, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def cmd_write(a: argparse.Namespace) -> int:
    reg_path = repo_path(a.registry)
    if not reg_path.is_file():
        print(f"错误：找不到登记表 {rel(reg_path)}（先按 docs/round5-preregistration.md 写好，再运行本命令填 sha256）", file=sys.stderr)
        return EXIT_USAGE
    registry, err = load_registry(reg_path)
    if registry is None:
        print(f"错误：{err}", file=sys.stderr)
        return EXIT_USAGE
    entries = registry["strategies"]
    wanted = a.strategies.split() if a.strategies else list(entries)
    unknown = [n for n in wanted if n not in entries]
    if unknown:
        print(f"错误：登记表里没有这些策略：{' '.join(unknown)}", file=sys.stderr)
        return EXIT_USAGE
    changed, refused, errors = [], [], []
    ledger = read_ledger(repo_path(a.ledger))
    for n in wanted:
        ent = entries[n]
        f = repo_path(ent["file"])
        done = formal_runs_of(n, ledger, registry)
        if not f.is_file():
            errors.append(f"{n}：找不到文件 {ent['file']}")
            continue
        if n not in classes_in_file(f):
            errors.append(f"{n}：文件 {ent['file']} 里没有 class {n}")
            continue
        new = sha256_file(f)
        old = str(ent.get("sha256") or "").lower()
        pins_new = pinned_now(n, f)
        pins_old = ent.get("pinned_files") if isinstance(ent.get("pinned_files"), dict) else {}
        if old == new and pins_old == pins_new:
            print(f"  - {n}：sha256 没变（{_short(new)}…；一起固定的 {len(pins_new)} 个文件也没变）")
            continue
        if done:
            refused.append(f"{n}：已经正式运行过（{'、'.join(str(r.get('run_id') or r.get('run_dir')) for r in done)}），"
                           "登记不能再改（--force 也不行）；改动要换新类名")
            continue
        if SHA_RE.match(old) and not a.force:
            if old != new:
                refused.append(f"{n}：登记的是 {_short(old)}…，现在的文件是 {_short(new)}…")
            else:
                diff = sorted(k for k in set(pins_old) | set(pins_new) if pins_old.get(k) != pins_new.get(k))
                refused.append(f"{n}：一起固定的文件改过：{'、'.join(diff)}")
            continue
        ent["sha256"] = new
        ent["pinned_files"] = pins_new
        changed.append(n)
        print(f"  - {n}：sha256 {'改成' if SHA_RE.match(old) and old != new else ('写入' if not SHA_RE.match(old) else '没变')} {new}（{ent['file']}）；"
              f"一起固定：{'、'.join(pins_new) or '（没有）'}")
    if errors:
        for e in errors:
            print(f"错误：{e}", file=sys.stderr)
        return EXIT_USAGE
    if refused:
        print("!! 这些策略已经登记过、文件又改了，没有改登记表：", file=sys.stderr)
        for r in refused:
            print(f"   {r}", file=sys.stderr)
        print("   正式运行之后的改动必须换新类名（预注册 3.1 节）；确认是正式运行之前改的，加 --force 重新登记。", file=sys.stderr)
        return EXIT_REFUSED
    if changed:
        write_json_atomic(reg_path, registry)
        print(f"已更新登记表 {rel(reg_path)}（{len(changed)} 个）。记得把登记表和策略文件一起提交 git，再做正式运行。")
    else:
        print(f"登记表 {rel(reg_path)} 不用改。")
    return EXIT_OK


def cmd_show(a: argparse.Namespace) -> int:
    reg_path = repo_path(a.registry)
    registry, err = load_registry(reg_path)
    if registry is None:
        print(err or f"没有登记表 {rel(reg_path)}")
        return EXIT_REFUSED if err else EXIT_OK
    print(f"登记表 {rel(reg_path)}（第 {registry.get('round', '?')} 轮，规格 {registry.get('spec_doc') or '?'}）：")
    bad = 0
    for n, ent in registry["strategies"].items():
        f = repo_path(ent["file"])
        reg_sha = str(ent.get("sha256") or "").lower()
        if not f.is_file():
            state = "!! 文件不存在"
            bad += 1
        elif not SHA_RE.match(reg_sha):
            state = "还没有 sha256"
            bad += 1
        elif sha256_file(f) != reg_sha:
            state = "!! 文件和登记的不一致"
            bad += 1
        elif pin_err := check_pinned(ent, f.parent):
            state = f"!! {pin_err}"
            bad += 1
        else:
            state = "一致" + (f"（连同 {len(ent['pinned_files'])} 个一起固定的文件）" if ent.get("pinned_files") else "")
        pend = pending_decisions(ent)
        if pend:
            state += f"；!! 待定：{'；'.join(pend)}（正式运行之前要定下来）"
        extra = []
        for k in ("venue", "stage", "min_pf"):
            if ent.get(k) is not None:
                extra.append(f"{k}={ent[k]}")
        print(f"  - {n}：{ent['file']}，sha256 {_short(reg_sha)}…，{state}" + (f"（{'，'.join(extra)}）" if extra else ""))
    changes = registry.get("changes") or []
    print(f"  正式运行之后的改动记录：{len(changes)} 条")
    ledger = read_ledger(repo_path(a.ledger))
    done = [r for n in registry["strategies"] for r in formal_runs_of(n, ledger, registry)]
    if done:
        print(f"  已经做过的正式运行（{rel(repo_path(a.ledger))} 和登记表的 formal_runs）：")
        for r in done:
            print(f"    - {r.get('class')} 第 {r.get('stage') or '-'} 阶段：{r.get('run_id') or r.get('run_dir')}，"
                  f"{str(r.get('date_utc') or '')[:10]}，提交 {str(r.get('commit') or '')[:10]}")
    else:
        print("  还没有正式运行记录")
    rs = registry.get("run_settings")
    if isinstance(rs, dict) and rs:
        print("  冻结的运行设置（run_settings；策略 / 阶段可以各自覆盖）：" + "，".join(
            f"{k}={v}" for k, v in rs.items() if not str(k).startswith("_")))
    return EXIT_REFUSED if bad else EXIT_OK


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="预注册凭证：核对策略文件的 sha256，记进 plan.json；或者重新计算登记表里的 sha256")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sp = sub.add_parser("check", help="核对这次要研究的策略（scripts/research.sh 在回测前调用）")
    sp.add_argument("--strategies", required=True)
    sp.add_argument("--strategy-dir", default="user_data/strategies")
    sp.add_argument("--registry", default=DEFAULT_REGISTRY)
    sp.add_argument("--config", action="append", default=[], help="研究配置（可以写多次；只用来记 sha256、对照交易所）")
    sp.add_argument("--run-dir", default="", help="结果目录：写进 plan.json 的 prereg（还没有 plan.json 时写 prereg.json）")
    sp.add_argument("--allow-unregistered", action="store_true", help="不一致也放行（结果不算正式运行）")
    sp.add_argument("--synthetic", action="store_true", help="合成数据运行（永远不算正式运行）")
    sp.add_argument("--pipeline-only", action="store_true", help="只做数据检查、不回测（research.sh --data-check-only；永远不算正式运行）")
    sp.add_argument("--settings", default="", help="这次研究的运行设置（JSON：fee、max_dd、切分、预热、保留下架币、留出段 ...），和登记表的 run_settings 对照")
    sp.add_argument("--ledger", default=DEFAULT_LEDGER, help="正式运行记录文件（默认 user_data/research/formal_runs.jsonl）")
    sp.add_argument("--record-formal", action="store_true", help="这次是正式运行时记进正式运行记录（研究脚本在第一次回测之前加）")
    sp = sub.add_parser("write", help="（重新）计算登记表里的 sha256")
    sp.add_argument("--registry", default=DEFAULT_REGISTRY)
    sp.add_argument("--strategies", default="", help="只更新这些策略（默认全部）")
    sp.add_argument("--force", action="store_true", help="已经登记过的 sha256 也覆盖（只在正式运行之前用；正式运行过的类加了也拒绝）")
    sp.add_argument("--ledger", default=DEFAULT_LEDGER, help="正式运行记录文件（默认 user_data/research/formal_runs.jsonl）")
    sp = sub.add_parser("show", help="打印登记表")
    sp.add_argument("--registry", default=DEFAULT_REGISTRY)
    sp.add_argument("--ledger", default=DEFAULT_LEDGER)
    a = ap.parse_args(argv)
    if a.cmd == "check":
        return cmd_check(a)
    if a.cmd == "write":
        return cmd_write(a)
    return cmd_show(a)


if __name__ == "__main__":
    sys.exit(main())
