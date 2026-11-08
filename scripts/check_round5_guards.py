#!/usr/bin/env python3
"""
第 5 轮复查时加的几条保护的自检（不联网，十几秒；make guard-test 也会跑）：

  1. XSFundingCarry 的"调仓前 7 天资金费记录完整"（funding_window_stats）：条数、窗口开始到第一条、相邻两条、最后一条到 T 的间隔；
  2. 回测里数据中途结束的币（下架币）：NewListingShortHedged.nlsh_data_ended、CapitulationRebound.cr_data_ended
     只在最后一根K线收盘以后才算"结束"，中间缺几根K线不算，实盘永远不算；
  3. scripts/check_prereg.py 的正式运行规则：在临时目录里复制一份仓库、git init 提交，然后检查
     读不到 git / 交易所不对 / 多出没登记的 research-override / 研究配置没提交 / 运行设置不对 / 同一个类再跑 / write --force；
     2026-11-08 起还有：运行设置 funding_archive（XSFundingCarry = okx，其他 = 0）、一起固定的工具脚本 pinned_tools
     （"PENDING" = 还没固定：正式运行拒绝、合成数据只提醒；write 直接填上；改过 = 拒绝；没提交 = 不是正式运行）；
     复查后加的：运行设置 datadir（XSFundingCarry 登记了 user_data/data/okx-funding，换目录 = 拒绝）、第 1 阶段（以后加币安的第 2 阶段，
     OKX 再跑照样是重复运行）。

用法：
  .venv/bin/python scripts/check_round5_guards.py
退出码：0 = 全部通过；1 = 有检查不对。
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR / "user_data" / "strategies"))
sys.path.insert(0, str(ROOT_DIR / "scripts"))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
from freqtrade.enums import RunMode  # noqa: E402

import check_prereg as cp  # noqa: E402
from CapitulationRebound import CapitulationRebound  # noqa: E402
from NewListingShortHedged import NewListingShortHedged  # noqa: E402
from XSFundingCarry import funding_window_stats  # noqa: E402

for _name in ("easytrade_risk", "rust_port_common", "NewListingShortHedged", "CapitulationRebound", "XSFundingCarry"):
    logging.getLogger(_name).setLevel(logging.ERROR)

FAILS: list[str] = []
H = 3600
DAY = 86400


def check(name: str, got: Any, want: Any) -> None:
    ok = got == want
    print(f"  {'通过' if ok else '不对'}  {name}" + ("" if ok else f"：得到 {got!r}，应为 {want!r}"))
    if not ok:
        FAILS.append(name)


# ----------------------------------------------------------------------------
# 1. funding_window_stats
# ----------------------------------------------------------------------------
def test_funding_window() -> None:
    print("1. XSFundingCarry：[T-7 天, T) 资金费记录完整 = ≥ 21 条，而且窗口开始到第一条、相邻两条、最后一条到 T 都 ≤ 8 小时")
    T = 1_700_000_000 // DAY * DAY  # 某个 00:00 UTC
    W, MIN_N, GAP = 7 * DAY, 21, 8 * H

    def run(times: list[int]) -> tuple[bool, int, float]:
        fd = np.array(sorted(times), dtype="int64")
        tot, comp, cnt = funding_window_stats(fd, np.full(len(fd), 0.0001), np.array([T]), W, MIN_N, GAP)
        return bool(comp[0]), int(cnt[0]), (float(tot[0]) if comp[0] else float("nan"))

    full8 = [T - W + k * 8 * H for k in range(21)]                       # T-7d, ..., T-8h
    c, n, s = run(full8 + [T])                                            # T 那一次不算（严格 < T）
    check("8 小时结算、整整 21 条 → 完整，和 = 21 × 0.0001（T 那一次不算）", (c, n, round(s, 12)), (True, 21, 0.0021))
    check("8 小时结算少一条 → 不完整", run(full8[:10] + full8[11:])[:2], (False, 20))
    check("8 小时结算、结算时间 04/12/20 点 → 完整", run([T - W + 4 * H + k * 8 * H for k in range(21)])[:2], (True, 21))
    check("4 小时结算、整整 7 天（42 条）→ 完整", run([T - W + k * 4 * H for k in range(42)])[:2], (True, 42))
    check("4 小时结算、只有最后 4 天（24 条，条数够）→ 不完整（窗口开头缺 3 天）",
          run([T - 4 * DAY + k * 4 * H for k in range(24)])[:2], (False, 24))
    check("1 小时结算、只有最后 21 小时（21 条）→ 不完整", run([T - 21 * H + k * H for k in range(21)])[:2], (False, 21))
    switch = [T - W + k * 8 * H for k in range(9)] + [T - 4 * DAY + k * 4 * H for k in range(24)]
    check("前 3 天 8 小时、后 4 天 4 小时结算 → 完整", run(switch)[:2], (True, 33))
    check("最后一条离 T 超过 8 小时 → 不完整", run([T - W + k * 4 * H for k in range(40)])[:2], (False, 40))
    hole = [x for x in (T - W + k * 4 * H for k in range(42)) if not (T - 3 * DAY <= x < T - 3 * DAY + 12 * H)]
    check("中间缺 12 小时（条数还够）→ 不完整", run(hole)[:2], (False, 39))


# ----------------------------------------------------------------------------
# 2. 数据中途结束的币
# ----------------------------------------------------------------------------
class FakeDP:
    def __init__(self, frames: dict[str, pd.DataFrame], runmode: RunMode) -> None:
        self.frames, self.runmode = frames, runmode

    def historic_ohlcv(self, pair: str, timeframe: str | None = None, candle_type: str = "") -> pd.DataFrame:
        return self.frames.get(pair, pd.DataFrame())


def _candles(start: str, days: int, skip: tuple[int, ...] = ()) -> pd.DataFrame:
    d0 = pd.Timestamp(start, tz="UTC")
    rows = [{"date": d0 + pd.Timedelta(days=i), "open": 10.0 + i, "high": 11.0 + i, "low": 9.0 + i,
             "close": 10.5 + i, "volume": 1.0} for i in range(days) if i not in skip]
    return pd.DataFrame(rows)


def _strategy(cls: type, runmode: RunMode, frames: dict[str, pd.DataFrame]) -> Any:
    cfg = {"runmode": runmode, "dry_run": True, "stake_currency": "USDT", "trading_mode": "futures",
           "margin_mode": "isolated", "candle_type_def": "futures", "exchange": {}}
    s = cls(cfg)
    s.dp = FakeDP(frames, runmode)
    return s


def test_data_ended() -> None:
    print("2. 回测里数据中途结束的币：最后一根K线收盘以后才算结束（价格 = 最后一根K线的开盘价 = freqtrade 的 force_exit 价）")
    frames = {"DEAD/USDT:USDT": _candles("2024-01-01", 10), "GAP/USDT:USDT": _candles("2024-01-01", 30, skip=(10, 11, 12))}
    last = datetime(2024, 1, 10, tzinfo=UTC)  # DEAD 的最后一根日线（开盘 01-10 00:00，收盘 01-11 00:00）
    for cls, fn in ((NewListingShortHedged, "nlsh_data_ended"), (CapitulationRebound, "cr_data_ended")):
        s = _strategy(cls, RunMode.BACKTEST, frames)
        f = getattr(s, fn)
        check(f"{cls.__name__}.{fn}：最后一根K线开盘时还没结束", f("DEAD/USDT:USDT", last), None)
        check(f"{cls.__name__}.{fn}：最后一根K线收盘前一秒还没结束", f("DEAD/USDT:USDT", last + timedelta(days=1, seconds=-1)), None)
        check(f"{cls.__name__}.{fn}：最后一根K线收盘 → 结束，价格 = 最后一根的开盘价 19",
              f("DEAD/USDT:USDT", last + timedelta(days=1)), 19.0)
        check(f"{cls.__name__}.{fn}：中间缺 3 根K线、后面还有数据 → 不算结束",
              f("GAP/USDT:USDT", datetime(2024, 1, 12, tzinfo=UTC)), None)
        check(f"{cls.__name__}.{fn}：没有数据的币 → None", f("NONE/USDT:USDT", last + timedelta(days=5)), None)
        live = _strategy(cls, RunMode.DRY_RUN, frames)
        check(f"{cls.__name__}.{fn}：模拟盘 / 实盘永远不算结束", getattr(live, fn)("DEAD/USDT:USDT", last + timedelta(days=5)), None)


# ----------------------------------------------------------------------------
# 3. check_prereg.py 的正式运行规则（临时 git 仓库）
# ----------------------------------------------------------------------------
COPY_DIRS = ("configs", "user_data/strategies")
COPY_FILES = ("docs/round5-preregistration.md", ".gitignore") + cp.HARNESS_FILES


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=selftest", "-c", "user.email=selftest@example.invalid",
                    "-c", "commit.gpgsign=false", *args], check=True, capture_output=True, text=True)


def _copy_repo(dst: Path) -> None:
    for d in COPY_DIRS:
        shutil.copytree(ROOT_DIR / d, dst / d, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    for f in COPY_FILES:
        if (ROOT_DIR / f).is_file():
            (dst / f).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(ROOT_DIR / f, dst / f)
    # 登记表 pinned_tools 列的工具脚本（例如 scripts/okx_funding_archive.py）：有就复制，还没有就放一个占位文件（只给这个自检用）
    reg = json.loads((ROOT_DIR / cp.DEFAULT_REGISTRY).read_text(encoding="utf-8"))
    for ent in (reg.get("strategies") or {}).values():
        for f in (ent.get("pinned_tools") or {}) if isinstance(ent, dict) else ():
            (dst / f).parent.mkdir(parents=True, exist_ok=True)
            if (ROOT_DIR / f).is_file():
                shutil.copy2(ROOT_DIR / f, dst / f)
            elif not (dst / f).is_file():
                (dst / f).write_text("# placeholder for check_round5_guards.py\n", encoding="utf-8")


def test_prereg_rules(tmp: Path) -> None:
    print("3. check_prereg.py：正式运行的条件（临时目录里的 git 仓库副本）")
    if shutil.which("git") is None:
        print("  跳过：这台机器没有 git")
        return
    repo = tmp / "repo"
    _copy_repo(repo)
    env = {**os.environ, "GIT_CEILING_DIRECTORIES": str(tmp)}

    def prereg(*args: str, cwd: Path = repo) -> tuple[int, str]:
        res = subprocess.run([sys.executable, str(cwd / "scripts" / "check_prereg.py"), *args], cwd=cwd, env=env,
                             capture_output=True, text=True, timeout=120, check=False)
        return res.returncode, res.stdout + res.stderr

    # 副本里先按副本的文件重新登记（和仓库现在的登记表是不是最新的无关），再提交
    rc, out = prereg("write", "--force")
    check("副本里 write --force 重新登记", rc, 0)
    _git(repo, "init", "-q")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "selftest")
    reg = json.loads((repo / "configs" / "preregistration.json").read_text(encoding="utf-8"))
    S, CFG = "PremiumFade", "configs/config.okx-research.json"
    want = cp.required_settings(reg, reg["strategies"][S], {})
    ok_settings = json.dumps({k: str(v) for k, v in want.items()})

    def run(name: str, *extra: str, settings: str = ok_settings, config: str = CFG, cwd: Path = repo) -> tuple[int, dict]:
        d = tmp / "runs" / name
        args = ["check", "--strategies", S, "--config", config, "--run-dir", str(d), "--settings", settings, *extra]
        rc, out = prereg(*args, cwd=cwd)
        p = d / "prereg.json"
        return rc, (json.loads(p.read_text(encoding="utf-8")) if p.is_file() else {"_out": out})

    rc, r = run("ok")
    check("都提交了、交易所 / 配置 / 运行设置都对 → 正式运行", (rc, r.get("formal_run")), (0, True))
    rc, r = run("bad_venue", config="configs/config.binance-research.json")
    check("交易所不对（PremiumFade 登记 OKX，用了币安配置）→ 拒绝", (rc, r.get("formal_run")), (3, False))
    rc, r = run("bad_venue_syn", "--synthetic", config="configs/config.binance-research.json")
    check("同上，合成数据 → 只提醒，不是正式运行", (rc, r.get("formal_run"), bool(r.get("warnings"))), (0, False, True))
    bad = json.loads(ok_settings)
    bad["fee"] = "0.0005"
    rc, r = run("bad_fee", settings=json.dumps(bad))
    check("运行设置不对（手续费 0.0005）→ 拒绝", (rc, r.get("formal_run")), (3, False))
    rc, r = run("no_settings", settings="")
    check("没传运行设置 → 不是正式运行", (rc, r.get("formal_run")), (0, False))

    ov = repo / "configs" / "research-overrides" / f"{S}.json"
    ov.write_text('{"max_open_trades": 9}\n', encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "extra override")
    rc, r = run("extra_override")
    check("多出没登记的 research-override（已经提交也一样）→ 拒绝", (rc, r.get("formal_run")), (3, False))
    _git(repo, "rm", "-q", str(ov.relative_to(repo)))
    _git(repo, "commit", "-q", "-m", "drop override")

    pairs = repo / "configs" / "pairs.okx-research.json"
    if pairs.is_file():
        orig = pairs.read_text(encoding="utf-8")
        pairs.write_text(orig + "\n", encoding="utf-8")
        rc, r = run("dirty_pairs")
        reasons = "；".join(r.get("not_formal_reasons") or [])
        check("币池文件有未提交的改动 → 不是正式运行（不拒绝）", (rc, r.get("formal_run"), "pairs.okx-research.json" in reasons),
              (0, False, True))
        pairs.write_text(orig, encoding="utf-8")
    else:
        print("  （没有 configs/pairs.okx-research.json，跳过币池那一条）")
    doc = repo / "docs" / "round5-preregistration.md"
    doc.write_text(doc.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    rc, r = run("dirty_doc")
    check("规格文档有未提交的改动 → 不是正式运行", (rc, r.get("formal_run")), (0, False))
    _git(repo, "checkout", "-q", "--", "docs/round5-preregistration.md")

    rc, r = run("first", "--record-formal")
    ledger = repo / "user_data" / "research" / "formal_runs.jsonl"
    rows = [json.loads(x) for x in ledger.read_text(encoding="utf-8").splitlines()] if ledger.is_file() else []
    check("正式运行 + --record-formal → 记进 formal_runs.jsonl", (rc, r.get("formal_run"), [x["class"] for x in rows]),
          (0, True, [S]))
    rc, r = run("second")
    check("同一个类、同一个阶段再跑 → 重复运行，不是正式运行", (rc, r.get("formal_run"),
                                                  bool((r.get("strategies") or {}).get(S, {}).get("formal_repeat"))), (0, False, True))
    sf = repo / "user_data" / "strategies" / f"{S}.py"
    src = sf.read_text(encoding="utf-8")
    reg_before = (repo / "configs" / "preregistration.json").read_bytes()
    sf.write_text(src + "\n# changed after the formal run\n", encoding="utf-8")
    rc, out = prereg("write", "--force", "--strategies", S)
    check("正式运行过的类改了文件再 write --force → 拒绝、登记表不变",
          (rc, (repo / "configs" / "preregistration.json").read_bytes() == reg_before), (3, True))
    sf.write_text(src, encoding="utf-8")

    bare = tmp / "nogit"
    shutil.copytree(repo, bare, ignore=shutil.ignore_patterns(".git", "research"))
    rc, r = run("nogit", cwd=bare)
    reasons = "；".join(r.get("not_formal_reasons") or [])
    check("读不到 git（没有 .git）→ 不是正式运行", (rc, r.get("formal_run"), "git" in reasons), (0, False, True))

    test_prereg_funding_archive(tmp, repo, prereg, run, ok_settings)


def test_prereg_funding_archive(tmp: Path, repo: Path, prereg: Any, run: Any, ok_settings: str) -> None:
    """2026-11-08：XSFundingCarry 改在 OKX 上测（OKX 资金费历史归档）。运行设置 funding_archive 和一起固定的工具脚本 pinned_tools。"""
    print("4. check_prereg.py：funding_archive 运行设置、pinned_tools（工具脚本）")
    reg_path = repo / "configs" / "preregistration.json"
    reg = json.loads(reg_path.read_text(encoding="utf-8"))
    X, XCFG = "XSFundingCarry", "configs/config.okx-research-funding.json"
    xent = reg["strategies"][X]
    tools = xent.get("pinned_tools") or {}
    check("登记表：XSFundingCarry 在 OKX、用 config.okx-research-funding.json、funding_archive = okx、没有 2020 留出段",
          (xent.get("venue"), xent.get("config"), (xent.get("run_settings") or {}).get("funding_archive"),
           (xent.get("run_settings") or {}).get("pre_holdout"), (xent.get("run_settings") or {}).get("start")),
          ("okx", XCFG, "okx", "", "20220401"))
    check("登记表：全局 funding_archive = 0（其他策略开着归档 = 不一样）", (reg.get("run_settings") or {}).get("funding_archive"), "0")
    check("登记表：XSFundingCarry 的 pinned_tools 里有 scripts/okx_funding_archive.py", "scripts/okx_funding_archive.py" in tools, True)
    if not tools:
        return
    tool = repo / next(iter(tools))
    x_want = cp.required_settings(reg, xent, {})
    x_ok = json.dumps({k: str(v) for k, v in x_want.items()})

    def xrun(name: str, *extra: str, settings: str = x_ok) -> tuple[int, dict]:
        d = tmp / "runs" / name
        rc, out = prereg("check", "--strategies", X, "--config", XCFG, "--run-dir", str(d), "--settings", settings, *extra)
        p = d / "prereg.json"
        return rc, (json.loads(p.read_text(encoding="utf-8")) if p.is_file() else {"_out": out})

    # 副本里 write --force 已经把工具脚本的 sha256 填上了
    check("副本里 write 以后 pinned_tools 是 64 位十六进制", all(cp.SHA_RE.match(str(v)) for v in (xent.get("pinned_tools") or {}).values()), True)
    rc, r = xrun("x_ok")
    check("XSFundingCarry：都提交了、funding_archive = okx → 正式运行", (rc, r.get("formal_run")), (0, True))
    bad = json.loads(x_ok)
    bad["funding_archive"] = "0"
    rc, r = xrun("x_fa0", settings=json.dumps(bad))
    check("XSFundingCarry：funding_archive = 0（没用归档）→ 拒绝", (rc, r.get("formal_run")), (3, False))
    bad.pop("funding_archive")
    rc, r = xrun("x_fa_missing", settings=json.dumps(bad))
    check("XSFundingCarry：没传 funding_archive（旧版研究脚本）→ 拒绝", (rc, r.get("formal_run")), (3, False))
    bad = json.loads(x_ok)
    bad["start"] = "20210101"
    rc, r = xrun("x_start", settings=json.dumps(bad))
    check("XSFundingCarry：start = 20210101（不是登记的 20220401）→ 拒绝", (rc, r.get("formal_run")), (3, False))
    d = tmp / "runs" / "x_bn"
    rc, _out = prereg("check", "--strategies", X, "--config", "configs/config.binance-research.json", "--run-dir", str(d),
                      "--settings", x_ok)
    check("XSFundingCarry：用币安配置（原来的运行 C）→ 拒绝", rc, 3)
    p_bad = json.loads(ok_settings)
    p_bad["funding_archive"] = "okx"
    rc, r = run("p_fa_okx", settings=json.dumps(p_bad))
    check("PremiumFade：funding_archive = okx（和 XSFundingCarry 一起开着归档跑）→ 拒绝", (rc, r.get("formal_run")), (3, False))

    # 工具脚本还没固定（"PENDING"）
    orig_reg = reg_path.read_text(encoding="utf-8")
    pend = json.loads(orig_reg)
    pend["strategies"][X]["pinned_tools"] = {k: "PENDING" for k in tools}
    reg_path.write_text(json.dumps(pend, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "tools pending")
    rc, r = xrun("x_pending")
    check("pinned_tools = PENDING → 正式运行拒绝", (rc, r.get("formal_run")), (3, False))
    rc, r = xrun("x_pending_syn", "--synthetic")
    check("pinned_tools = PENDING、合成数据 → 只提醒（退出码 0，不是正式运行）",
          (rc, r.get("formal_run"), any("工具脚本" in w for w in r.get("warnings") or [])), (0, False, True))
    rc, r = xrun("x_pending_dc", "--pipeline-only")
    check("pinned_tools = PENDING、只做数据检查 → 只提醒", (rc, r.get("formal_run")), (0, False))
    rc, out = prereg("write", "--strategies", X)
    now = json.loads(reg_path.read_text(encoding="utf-8"))["strategies"][X].get("pinned_tools") or {}
    check("write（不加 --force）把 PENDING 填成现在的 sha256",
          (rc, {k: cp.SHA_RE.match(str(v)) is not None for k, v in now.items()}), (0, {k: True for k in tools}))
    check("填上的 sha256 = 工具脚本现在的 sha256", now.get(next(iter(tools))), cp.sha256_file(tool))
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "tools pinned")
    rc, r = xrun("x_pinned")
    check("填好以后 → 正式运行", (rc, r.get("formal_run")), (0, True))

    # 工具脚本改过
    tool_src = tool.read_bytes()
    tool.write_bytes(tool_src + b"\n# changed\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "tool changed")
    rc, r = xrun("x_tool_changed")
    check("工具脚本改过（已经提交也一样）→ 拒绝", (rc, r.get("formal_run")), (3, False))
    rc, r = xrun("x_tool_changed_syn", "--synthetic")
    check("工具脚本改过、合成数据 → 照样拒绝（和 pinned_files 一样）", rc, 3)
    reg_before = reg_path.read_bytes()
    rc, out = prereg("write", "--strategies", X)
    check("工具脚本改过、write 不加 --force → 拒绝、登记表不变", (rc, reg_path.read_bytes() == reg_before), (3, True))
    rc, out = prereg("write", "--strategies", X, "--force")
    check("工具脚本改过、write --force → 重新固定", (rc, (json.loads(reg_path.read_text(encoding="utf-8"))["strategies"][X]
                                                    .get("pinned_tools") or {}).get(next(iter(tools)))), (0, cp.sha256_file(tool)))
    # 只提交登记表、工具脚本的改动不提交：sha256 一致，但不是正式运行
    tool.write_bytes(tool_src)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "tool restored")
    tool.write_bytes(tool_src + b"\n# uncommitted\n")
    rc, out = prereg("write", "--strategies", X, "--force")
    _git(repo, "add", "configs/preregistration.json")
    _git(repo, "commit", "-q", "-m", "registry only")
    rc, r = xrun("x_tool_dirty")
    reasons = "；".join(r.get("not_formal_reasons") or [])
    check("工具脚本有未提交的改动（登记表已经按它更新、提交）→ 不是正式运行（不拒绝）",
          (rc, r.get("formal_run"), "工具脚本" in reasons), (0, False, True))
    tool.write_bytes(tool_src)
    reg_path.write_text(orig_reg, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "restore")
    rc, out = prereg("show")
    check("show 能读新的登记表（venue_decision、deferred、pinned_tools 这些新字段不报错）", "XSFundingCarry" in out, True)

    # 2026-11-08 复查后加：数据目录（run_settings.datadir）和第 1 阶段（stage = "1"）
    check("登记表：XSFundingCarry 登记了数据目录 user_data/data/okx-funding、第 1 阶段",
          ((xent.get("run_settings") or {}).get("datadir"), xent.get("stage")), ("user_data/data/okx-funding", "1"))
    bad = json.loads(x_ok)
    bad["datadir"] = "user_data/data/okx-funding-try"
    rc, r = xrun("x_dd_other", settings=json.dumps(bad))
    check("XSFundingCarry：--datadir / RESEARCH_DATADIR 换成别的目录 → 拒绝", (rc, r.get("formal_run")), (3, False))
    rc, r = xrun("x_dd_other_dc", "--pipeline-only", settings=json.dumps(bad))
    check("同上、只做数据检查 → 只提醒", (rc, r.get("formal_run")), (0, False))
    bad.pop("datadir")
    rc, r = xrun("x_dd_missing", settings=json.dumps(bad))
    check("XSFundingCarry：没传 datadir（旧版研究脚本）→ 拒绝", rc, 3)
    p_dd = json.loads(ok_settings)
    p_dd["datadir"] = "user_data/data/okx"
    rc, r = run("p_dd", settings=json.dumps(p_dd))
    check("PremiumFade：没有登记 datadir，传了也不对照（不拒绝）", rc, 0)
    rc, r = xrun("x_first", "--record-formal")
    rows = [json.loads(x) for x in (repo / "user_data" / "research" / "formal_runs.jsonl").read_text(encoding="utf-8").splitlines()]
    check("XSFundingCarry 正式运行记进记录，stage = \"1\"", (rc, r.get("formal_run"), [x.get("stage") for x in rows if x["class"] == X]),
          (0, True, ["1"]))
    # 以后加币安复核：stages {"1": OKX, "2": 币安}（6.5 节）。OKX 再跑一次照样是重复运行
    st = json.loads(reg_path.read_text(encoding="utf-8"))
    ent = st["strategies"][X]
    ent.pop("stage", None)
    ent["stages"] = {"1": {"venue": "okx", "config": XCFG}, "2": {"venue": "binance", "config": "configs/config.binance-research.json"}}
    ent["venues"] = ["okx", "binance"]
    reg_path.write_text(json.dumps(st, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "add binance stage")
    rc, r = xrun("x_again")
    check("加了币安的第 2 阶段以后，OKX 再跑一次 → 第 1 阶段、重复运行，不是正式运行",
          (rc, r.get("formal_run"), (r.get("strategies") or {}).get(X, {}).get("stage"),
           bool((r.get("strategies") or {}).get(X, {}).get("formal_repeat"))), (0, False, "1", True))
    reg_path.write_text(orig_reg, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "restore 2")


def main() -> int:
    test_funding_window()
    test_data_ended()
    with tempfile.TemporaryDirectory(prefix="round5_guards_") as t:
        test_prereg_rules(Path(t))
    print()
    if FAILS:
        print(f"不通过：有 {len(FAILS)} 项不对：")
        for f in FAILS:
            print(f"  - {f}")
        return 1
    print("通过：全部。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
