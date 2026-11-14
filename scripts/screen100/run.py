"""第 6 轮：100 个候选策略快速初筛（向量化，几分钟跑完）。

  python scripts/screen100/run.py                  # 真实数据（默认 user_data/data/okx + configs/pairs.okx-research.json）
  python scripts/screen100/run.py --self-test      # 合成数据自检：纯随机行情应当 0 个通过；植入截面动量时应当抓到
  python scripts/screen100/run.py --only "T01 X01"  # 只跑几个（试跑用，不算正式运行）

做法（参数网格和门槛在 configs/screen100.json，第一次真实运行之前锁定，见 docs/screen100.md）：
  1. 每个策略的每组参数，在整段数据上算一次逐根收益（信号只用过去的数据），再切成各段算指标。
  2. "优化" = 只在训练段里挑参数（训练段夏普最高、交易数够的那组），拿去测样本外；
     走步验证每一折只用自己的训练窗口挑参数，再测后面没见过的测试窗口，各折拼起来 = 走步结果。
  3. 门槛和 make research 一样（赚钱、回撤 ≤ 15%、盈亏因子、≥ 30 笔、样本外不明显退化），另外加两条：
     手续费翻倍走步仍赚钱；Deflated Sharpe ≥ 0.95（考虑到这次一共试了几百组参数，最好的那组靠运气也会好看）。
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
import subprocess
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
ROOT = HERE.parent.parent

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

import catalog as CAT  # noqa: E402
import engine as E  # noqa: E402
from data import load_panel, pairs_from_file, synthetic_panel  # noqa: E402

LOCK_FILES = ["scripts/screen100/catalog.py", "scripts/screen100/engine.py", "scripts/screen100/ind.py",
              "scripts/screen100/data.py", "scripts/screen100/run.py"]
NEUTRAL = {x["id"] for x in CAT.STRATEGIES if (x["family"] in ("截面", "因子"))} - {"X21", "X22"} | {"C04", "C05"}

G: dict = {}  # 子进程共享（fork）


# ---------------------------------------------------------------- 配置 / 锁定
def sha256(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def code_hashes() -> dict:
    return {f: sha256(ROOT / f) for f in LOCK_FILES}


def load_cfg(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def git_commit() -> str:
    try:
        c = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
        d = subprocess.run(["git", "-C", str(ROOT), "status", "--porcelain", "--", "scripts/screen100", "configs/screen100.json"],
                           capture_output=True, text=True).stdout.strip()
        return c + ("（有未提交的改动）" if d else "")
    except OSError:
        return "unknown"


# ---------------------------------------------------------------- 分段
def segments(start: pd.Timestamp, end: pd.Timestamp, folds: int, tr_days: int, te_days: int) -> dict:
    span = end - start
    split = start + pd.Timedelta(days=int(span.days * 2 / 3))
    seg = {"train": (start, split), "oos": (split, end), "full": (start, end)}
    wf = []
    for k in range(folds):
        te_end = end - pd.Timedelta(days=te_days * (folds - 1 - k))
        te_start = te_end - pd.Timedelta(days=te_days)
        tr_start = te_start - pd.Timedelta(days=tr_days)
        if tr_start < start:
            continue
        wf.append((tr_start, te_start, te_end))
    for i, (a, b, c) in enumerate(wf):
        seg[f"wf{i}_tr"] = (a, b)
        seg[f"wf{i}_te"] = (b, c)
    for y in range(start.year, end.year + 1):
        a = max(start, pd.Timestamp(f"{y}-01-01", tz="UTC"))
        b = min(end, pd.Timestamp(f"{y + 1}-01-01", tz="UTC"))
        if b > a:
            seg[f"y{y}"] = (a, b)
    return {"seg": seg, "nfolds": len(wf), "split": split}


# ---------------------------------------------------------------- 单组参数
def run_trial(task):
    sid, ci = task
    st = CAT_BY_ID[sid]
    P = G["panels"][st["tf"]]
    s: E.Settings = G["settings"]
    seg = G["seg"]
    params = st["grid"][ci]
    t0 = time.time()
    try:
        w = st["build"](P, s, **params)
        res = E.simulate(P, w, s)
        res2 = E.simulate(P, w, s, fee=s.fee * G["stress_mult"])
    except Exception as ex:  # 策略代码出错：记下来，不让整次运行停
        return {"sid": sid, "ci": ci, "error": f"{type(ex).__name__}: {ex}"}
    out = {"sid": sid, "ci": ci, "params": params, "seg": {}, "wf": {}, "secs": 0.0}
    for name, (a, b) in seg.items():
        out["seg"][name] = E.seg_metrics(res, a, b)
    for k in range(G["nfolds"]):
        a, b = seg[f"wf{k}_te"]
        m = E.window_mask(res.index, a, b)
        out["wf"][k] = {"ret": res.ret[m], "worst": res.worst[m], "tp": E.trade_pnls(res, m),
                        "ret2": res2.ret[m], "worst2": res2.worst[m]}
    d = E.to_daily(res)
    a, b = seg["wf0_te"][0], seg[f"wf{G['nfolds'] - 1}_te"][1]
    out["daily_wf"] = d[(d.index >= a) & (d.index < b)]
    out["secs"] = time.time() - t0
    return out


CAT_BY_ID = {x["id"]: x for x in CAT.STRATEGIES}


# ---------------------------------------------------------------- 选参 + 判定
def pick(trials: list[dict], seg_name: str, min_trades: int) -> dict | None:
    best, best_v = None, -math.inf
    for t in trials:
        m = t["seg"][seg_name]
        v = m["sharpe"] if m["trades"] >= min_trades else -math.inf
        if v > best_v:
            best, best_v = t, v
    return best if best is not None else (trials[0] if trials else None)


def stitched(trials_by_ci: dict, choice: list[int], nf: int, bpy: float, key="ret", wkey="worst") -> dict:
    rets, worsts, tps = [], [], []
    for k in range(nf):
        t = trials_by_ci[choice[k]]
        rets.append(t["wf"][k][key])
        worsts.append(t["wf"][k][wkey])
        tps.append(t["wf"][k]["tp"])
    return E.metrics(np.concatenate(rets), np.concatenate(worsts), np.concatenate(tps), bpy)


def judge(row: dict, cfg: dict) -> list[str]:
    g = cfg["gate"]
    max_dd = g["max_dd"]
    min_pf = g["min_pf_neutral"] if row["neutral"] else g["min_pf"]
    mt = g["min_trades"]
    tr, te, wf = row["train"], row["oos"], row["wf"]
    R = []

    def pf_ok(m):
        return m["pf"] is not None and m["pf"] >= min_pf

    if tr["ret"] <= 0:
        R.append(f"训练段亏损 {tr['ret'] * 100:+.1f}%")
    if tr["dd"] > max_dd:
        R.append(f"训练段回撤 {tr['dd'] * 100:.1f}%")
    if te["ret"] <= 0:
        R.append(f"样本外亏损 {te['ret'] * 100:+.1f}%")
    if not pf_ok(te):
        R.append(f"样本外PF {fmt(te['pf'])}<{min_pf:g}")
    if te["trades"] < mt:
        R.append(f"样本外只有 {te['trades']} 笔")
    if te["dd"] > max_dd:
        R.append(f"样本外回撤 {te['dd'] * 100:.1f}%")
    if tr["pf"] and te["pf"] is not None and math.isfinite(tr["pf"]) and te["pf"] < g["pf_ratio"] * tr["pf"]:
        R.append(f"样本外退化 PF {tr['pf']:.2f}→{te['pf']:.2f}")
    if wf["ret"] <= 0:
        R.append(f"走步亏损 {wf['ret'] * 100:+.1f}%")
    if not pf_ok(wf):
        R.append(f"走步PF {fmt(wf['pf'])}<{min_pf:g}")
    if wf["trades"] < mt:
        R.append(f"走步只有 {wf['trades']} 笔")
    if wf["dd"] > max_dd:
        R.append(f"走步回撤 {wf['dd'] * 100:.1f}%")
    if row["wf_stress"]["ret"] <= 0:
        R.append(f"手续费×{cfg['stress_fee_mult']:g} 走步亏损 {row['wf_stress']['ret'] * 100:+.1f}%")
    return R


def fmt(x, nd=2):
    if x is None:
        return "-"
    if isinstance(x, float) and math.isinf(x):
        return "inf"
    return f"{x:.{nd}f}"


def pct(x, sign=True):
    return "-" if x is None else (f"{x * 100:+.1f}%" if sign else f"{x * 100:.1f}%")


# ---------------------------------------------------------------- 主流程
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="第 6 轮：100 个候选策略快速初筛")
    ap.add_argument("--config", default="configs/screen100.json")
    ap.add_argument("--datadir", default=None)
    ap.add_argument("--pairs-file", default=None)
    ap.add_argument("--funding-dir", default=None, help="额外的资金费数据目录（默认：user_data/data/okx-funding 存在就用）")
    ap.add_argument("--only", default=None, help="只跑这些编号（空格分隔），不算正式运行")
    ap.add_argument("--jobs", type=int, default=0, help="并行进程数（默认 = CPU 核数）")
    ap.add_argument("--out", default=None)
    ap.add_argument("--synthetic", default=None, choices=["none", "xsmom", "tsrev"], help="用合成数据（自检用）")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--self-test", action="store_true", help="合成数据自检（纯随机 + 植入截面动量各跑一次）")
    ap.add_argument("--allow-unlocked", action="store_true", help="代码和锁定的 sha256 不一致也跑（不算正式运行）")
    ap.add_argument("--lock", action="store_true", help="（整合的人用）把当前代码的 sha256 写进配置的 locked_sha256")
    a = ap.parse_args(argv)
    os.chdir(ROOT)
    cfg_path = Path(a.config)
    cfg = load_cfg(cfg_path)
    if a.lock:
        cfg["locked_sha256"] = code_hashes()
        cfg_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print("已写入 locked_sha256：", json.dumps(cfg["locked_sha256"], indent=2))
        return 0
    CAT.check_catalog()
    if a.self_test:
        return self_test(a, cfg)
    return run(a, cfg)


def run(a, cfg, quiet=False) -> int:
    t_start = time.time()
    formal = True
    notes = []
    hashes = code_hashes()
    locked = cfg.get("locked_sha256") or {}
    diff = [f for f in LOCK_FILES if locked.get(f) != hashes[f]]
    if diff and not a.synthetic:
        msg = "代码和 configs/screen100.json 里锁定的 sha256 不一致：" + " ".join(diff)
        if not a.allow_unlocked:
            print("!! " + msg + "\n   正式运行必须用锁定的版本（git pull 到锁定的提交）。只是试跑：加 --allow-unlocked", file=sys.stderr)
            return 4
        formal = False
        notes.append("!! " + msg + "（--allow-unlocked，不算正式运行）")
    if a.only:
        formal = False
        notes.append(f"只跑了 {a.only}（不算正式运行；多重检验的试验次数也只按这些算）")
    if a.synthetic:
        formal = False

    s = E.Settings(**cfg["settings"])
    start = pd.Timestamp(cfg["start"], tz="UTC")
    warm = pd.Timedelta(days=cfg["warmup_days"])
    tfs = sorted({x["tf"] for x in CAT.STRATEGIES})
    panels = {}
    if a.synthetic:
        for tf in tfs:
            plant = None if a.synthetic == "none" else a.synthetic
            panels[tf] = synthetic_panel(tf=tf, plant=plant, seed=a.seed,
                                         start=str((start - warm).date()))
    else:
        datadir = Path(a.datadir or cfg["datadir"])
        pairs = pairs_from_file(Path(a.pairs_file or cfg["pairs_file"]))
        fdirs = []
        fd = Path(a.funding_dir) if a.funding_dir else Path(cfg.get("funding_dir") or "")
        if str(fd) and fd.is_dir():
            fdirs.append(fd)
        for tf in tfs:
            panels[tf] = load_panel(datadir, pairs, tf, start - warm, None, fdirs)
    # 结束 = 日线数据最后一根完整K线之后（所有周期统一）
    p1d = panels["1d"]
    end = p1d.c.dropna(how="all").index.max() + pd.Timedelta(days=1)
    for tf, P in panels.items():
        notes.extend(f"[{tf}] {n}" for n in P.notes)
    sg = segments(start, end, cfg["wf_folds"], cfg["wf_train_days"], cfg["wf_test_days"])
    if sg["nfolds"] < 2:
        print("数据太短，走步验证不到 2 折", file=sys.stderr)
        return 1
    G.update(panels=panels, settings=s, seg=sg["seg"], nfolds=sg["nfolds"], stress_mult=cfg["stress_fee_mult"])

    ids = a.only.split() if a.only else [x["id"] for x in CAT.STRATEGIES]
    tasks = [(sid, ci) for sid in ids for ci in range(len(CAT_BY_ID[sid]["grid"]))]
    jobs = a.jobs or (os.cpu_count() or 1)
    if not quiet:
        print(f"{len(ids)} 个策略、{len(tasks)} 组参数，{jobs} 个进程；数据 {start.date()} ~ {(end - pd.Timedelta(days=1)).date()}，"
              f"{len(p1d.pairs)} 个币；走步 {sg['nfolds']} 折", flush=True)
    results = []
    done = 0
    if jobs > 1:
        import multiprocessing as mp
        ctx = mp.get_context("fork")
        with ProcessPoolExecutor(max_workers=jobs, mp_context=ctx) as ex:
            for r in ex.map(run_trial, tasks, chunksize=1):
                results.append(r)
                done += 1
                if not quiet and done % 20 == 0:
                    print(f"  {done}/{len(tasks)}  {time.time() - t_start:.0f}s", flush=True)
    else:
        for t in tasks:
            results.append(run_trial(t))
            done += 1
            if not quiet and done % 20 == 0:
                print(f"  {done}/{len(tasks)}  {time.time() - t_start:.0f}s", flush=True)

    # ---- 汇总
    by_sid: dict[str, dict[int, dict]] = {}
    errors = []
    for r in results:
        if "error" in r:
            errors.append(r)
            continue
        by_sid.setdefault(r["sid"], {})[r["ci"]] = r
    nf = sg["nfolds"]
    mt = cfg["gate"]["min_trades"]
    n_trials = sum(len(v) for v in by_sid.values())
    rows = []
    daily_cols = {}
    for sid in ids:
        st = CAT_BY_ID[sid]
        trials = by_sid.get(sid)
        if not trials:
            rows.append({"id": sid, "name": st["name"], "family": st["family"], "verdict": "ERROR",
                         "reasons": [e["error"] for e in errors if e["sid"] == sid][:1], "tested": st["tested"]})
            continue
        bpy = panels[st["tf"]].bars_per_year
        tl = [trials[k] for k in sorted(trials)]
        main_t = pick(tl, "train", mt)
        choice = [pick(tl, f"wf{k}_tr", mt)["ci"] for k in range(nf)]
        wf = stitched(trials, choice, nf, bpy)
        wf2 = stitched(trials, choice, nf, bpy, key="ret2", wkey="worst2")
        daily = pd.concat([trials[choice[k]]["daily_wf"][
            (trials[choice[k]]["daily_wf"].index >= sg["seg"][f"wf{k}_te"][0]) &
            (trials[choice[k]]["daily_wf"].index < sg["seg"][f"wf{k}_te"][1])] for k in range(nf)])
        daily_cols[sid] = daily
        dft = tl[0]
        rows.append({
            "id": sid, "name": st["name"], "family": st["family"], "tf": st["tf"], "source": st["source"],
            "tested": st["tested"], "neutral": sid in NEUTRAL, "n_params": len(st["grid"]),
            "chosen": main_t["params"], "wf_choices": [trials[c]["params"] for c in choice],
            "train": main_t["seg"]["train"], "oos": main_t["seg"]["oos"], "full": main_t["seg"]["full"],
            "wf": wf, "wf_stress": wf2,
            "years": {k[1:]: v["ret"] for k, v in main_t["seg"].items() if k.startswith("y")},
            "default": {"params": dft["params"], "train": dft["seg"]["train"], "oos": dft["seg"]["oos"]},
            "param_stable": len({json.dumps(p, sort_keys=True) for p in [trials[c]["params"] for c in choice]}) == 1,
        })

    # 多重检验（Bailey & López de Prado 2014，False Strategy Theorem + Deflated Sharpe Ratio）：
    #   参数是每一折只在训练窗口里挑的，所以走步结果对"挑参数"来说已经是样本外；剩下的偏差是"从 N 个策略里挑最好的"。
    #   假设 N 个策略真实夏普都是 0：T 天日收益估出来的夏普标准差约 1/sqrt(T)，最好那个的期望 = sr0。
    #   严格口径（定结论用）：N = 这次跑的策略个数（100）。
    #   宽松口径（只用来标"边缘"）：很多策略高度相关（比如各种趋势策略），N_eff = 策略走步日收益相关矩阵
    #   特征值的参与率 (Σλ)² / Σλ²，可能低估独立次数，所以只作参考。
    M = pd.DataFrame(daily_cols).fillna(0.0)
    T_days = len(M)
    live = M.loc[:, M.std(ddof=1) > 0]
    if live.shape[1] >= 2:
        lam = np.clip(np.linalg.eigvalsh(np.corrcoef(live.values.T)), 0, None)
        n_eff = float(lam.sum() ** 2 / (lam ** 2).sum())
    else:
        n_eff = float(max(live.shape[1], 1))
    n_strict = max(len(daily_cols), 1)
    var_sr = 1.0 / max(T_days - 1, 1)
    sr0 = E.expected_max_sr(var_sr, n_strict)
    sr0_eff = E.expected_max_sr(var_sr, max(int(round(n_eff)), 1))
    md = cfg["gate"]["min_dsr"]
    for row in rows:
        if row.get("verdict") == "ERROR":
            continue
        d = daily_cols[row["id"]].values
        row["dsr"] = E.psr(d, sr0)
        row["dsr_eff"] = E.psr(d, sr0_eff)
        R = judge(row, cfg)
        if R:
            row["verdict"] = "FAIL"
        elif row["dsr"] >= md:
            row["verdict"] = "PASS"
        elif row["dsr_eff"] >= md:
            row["verdict"] = "EDGE"
            R = [f"严格口径 Deflated Sharpe {row['dsr']:.2f} < {md:g}，宽松口径 {row['dsr_eff']:.2f} 够了"]
        else:
            row["verdict"] = "LUCK"
            R = [f"Deflated Sharpe {row['dsr']:.2f}（宽松 {row['dsr_eff']:.2f}）< {md:g}：{n_strict} 个策略里挑出来的，可能是运气"]
        row["reasons"] = R

    order = {"PASS": 0, "EDGE": 1, "LUCK": 2, "FAIL": 3, "ERROR": 4}
    rows.sort(key=lambda r: (order[r["verdict"]], -(r.get("dsr") or 0), -((r.get("wf") or {}).get("sharpe") or -9)))
    meta = {
        "formal": formal, "git": git_commit(), "code_sha256": hashes, "config": cfg, "args": vars(a),
        "start": str(start.date()), "end": str((end - pd.Timedelta(days=1)).date()),
        "split": str(sg["split"].date()),
        "segments": {k: [str(v[0].date()), str(v[1].date())] for k, v in sg["seg"].items()},
        "pairs": p1d.pairs, "n_trials": n_trials, "n_strategies": n_strict, "n_eff": n_eff,
        "wf_days": T_days, "sr0_daily": sr0, "sr0_annual": sr0 * math.sqrt(365), "sr0_eff_annual": sr0_eff * math.sqrt(365), "notes": notes, "errors": [(e["sid"], e["ci"], e["error"]) for e in errors],
        "secs": time.time() - t_start, "utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    out = Path(a.out) if a.out else ROOT / "user_data/research/screen100" / (
        ("synthetic_" if a.synthetic else "") + "run_" + datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S"))
    out.mkdir(parents=True, exist_ok=True)
    write_outputs(out, rows, meta, daily_cols, cfg)
    if not quiet:
        print((out / "summary.txt").read_text(encoding="utf-8"))
        print(f"结果目录：{out}")
    G["last_rows"], G["last_meta"] = rows, meta
    return 0


def write_outputs(out: Path, rows: list[dict], meta: dict, daily: dict, cfg: dict) -> None:
    def clean(o):
        if isinstance(o, dict):
            return {str(k): clean(v) for k, v in o.items()}
        if isinstance(o, (list, tuple)):
            return [clean(v) for v in o]
        if isinstance(o, float) and (math.isinf(o) or math.isnan(o)):
            return None if math.isnan(o) else ("inf" if o > 0 else "-inf")
        if isinstance(o, (np.floating,)):
            return clean(float(o))
        if isinstance(o, (np.integer,)):
            return int(o)
        return o

    (out / "results.json").write_text(json.dumps(clean({"meta": meta, "rows": rows}), ensure_ascii=False, indent=1),
                                      encoding="utf-8")
    if daily:
        pd.DataFrame(daily).to_csv(out / "wf_daily_returns.csv", float_format="%.6g")
    with open(out / "summary.csv", "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["编号", "名称", "类别", "周期", "结论", "Deflated Sharpe（严格）", "Deflated Sharpe（宽松）", "走步收益", "走步夏普", "走步PF", "走步笔数",
                    "走步回撤", "样本外收益", "样本外PF", "样本外回撤", "训练段收益", "训练段PF", "训练段选的参数",
                    "走步各折选的参数一样", "手续费翻倍走步收益", "前几轮测过的同类", "没通过的原因"])
        for r in rows:
            if r["verdict"] == "ERROR":
                w.writerow([r["id"], r["name"], r["family"], "", "出错"] + [""] * 16 + ["；".join(r["reasons"])])
                continue
            wf, te, tr = r["wf"], r["oos"], r["train"]
            w.writerow([r["id"], r["name"], r["family"], r["tf"], VERDICT_ZH[r["verdict"]], f"{r['dsr']:.3f}", f"{r['dsr_eff']:.3f}",
                        pct(wf["ret"]), fmt(wf["sharpe"]), fmt(wf["pf"]), wf["trades"], pct(wf["dd"], False),
                        pct(te["ret"]), fmt(te["pf"]), pct(te["dd"], False), pct(tr["ret"]), fmt(tr["pf"]),
                        json.dumps(r["chosen"], ensure_ascii=False), "是" if r["param_stable"] else "否",
                        pct(r["wf_stress"]["ret"]), r["tested"] or "", "；".join(r["reasons"])])
    (out / "summary.txt").write_text(summary_text(rows, meta, daily, cfg), encoding="utf-8")


VERDICT_ZH = {"PASS": "通过", "EDGE": "边缘", "LUCK": "像运气", "FAIL": "未通过", "ERROR": "出错"}


def summary_text(rows, meta, daily, cfg) -> str:
    g = cfg["gate"]
    L = []
    L.append("第 6 轮：100 个候选策略快速初筛" + ("" if meta["formal"] else "（!! 不是正式运行）"))
    L.append(f"数据 {meta['start']} ~ {meta['end']}，{len(meta['pairs'])} 个币；训练段到 {meta['split']}，之后是样本外；"
             f"走步 {sum(1 for k in meta['segments'] if k.endswith('_te'))} 折（训练 {cfg['wf_train_days']} 天 / 测试 {cfg['wf_test_days']} 天）")
    L.append(f"手续费+滑点每边 {cfg['settings']['fee'] * 100:.2f}%，总杠杆 ≤ {cfg['settings']['gross_max']:g}x，"
             f"回撤上限 {g['max_dd'] * 100:g}%（含浮亏），PF 门槛 {g['min_pf']:g}（多空对冲类 {g['min_pf_neutral']:g}），"
             f"≥ {g['min_trades']} 笔")
    L.append(f"多重检验：{meta['n_strategies']} 个策略（{meta['n_trials']} 组参数，参数在各折训练窗口里挑）。走步 {meta['wf_days']} 天里，"
             f"{meta['n_strategies']} 个毫无优势的策略中最好那个的年化夏普期望约 {meta['sr0_annual']:.2f}；"
             f"Deflated Sharpe ≥ {g['min_dsr']:g} 才算通过。")
    L.append(f"  宽松口径（只用来标“边缘”）：策略之间互相相关，约等于 {meta['n_eff']:.0f} 个独立试验，运气门槛年化夏普约 {meta['sr0_eff_annual']:.2f}")
    L.append(f"代码 {meta['git']}；用时 {meta['secs']:.0f} 秒")
    for n in meta["notes"]:
        L.append("  " + n)
    cnt = {k: sum(1 for r in rows if r["verdict"] == k) for k in VERDICT_ZH}
    L.append("")
    L.append(f"结论：通过 {cnt['PASS']} 个；边缘（只过宽松口径的多重检验）{cnt['EDGE']} 个；过了常规门槛但多重检验不过（像运气）{cnt['LUCK']} 个；"
             f"未通过 {cnt['FAIL']} 个；出错 {cnt['ERROR']} 个")
    L.append("")
    hdr = f"{'编号':<5}{'结论':<5}{'DSR':>6}{'走步收益':>9}{'夏普':>6}{'PF':>6}{'笔数':>6}{'回撤':>7}{'样本外':>8}{'样外PF':>7}  名称 / 原因"
    L.append(hdr)
    for r in rows:
        if r["verdict"] == "ERROR":
            L.append(f"{r['id']:<5}出错   {'；'.join(r['reasons'])[:120]}  {r['name']}")
            continue
        wf, te = r["wf"], r["oos"]
        tag = f"（测过：{r['tested']}）" if r["tested"] else ""
        L.append(f"{r['id']:<5}{VERDICT_ZH[r['verdict']]:<5}{r['dsr']:>6.2f}{pct(wf['ret']):>9}{fmt(wf['sharpe']):>6}"
                 f"{fmt(wf['pf']):>6}{wf['trades']:>6}{pct(wf['dd'], False):>7}{pct(te['ret']):>8}{fmt(te['pf']):>7}  "
                 f"{r['name']}{tag}")
        if r["reasons"]:
            L.append(f"{'':<11}原因：{'；'.join(r['reasons'][:4])}" + (" ..." if len(r["reasons"]) > 4 else ""))
    # 组合
    passed = [r["id"] for r in rows if r["verdict"] == "PASS"]
    edge = [r["id"] for r in rows if r["verdict"] in ("PASS", "EDGE")]
    for label, ids in (("通过的", passed), ("通过 + 边缘的", edge if len(edge) > len(passed) else [])):
        if len(ids) >= 2:
            df = pd.DataFrame({k: daily[k] for k in ids}).fillna(0.0)
            port = df.mean(axis=1)
            eq = (1 + port).cumprod()
            dd = float((1 - eq / eq.cummax()).max())
            sr = float(port.mean() / port.std() * math.sqrt(365)) if port.std() > 0 else 0.0
            L.append("")
            L.append(f"{label} {len(ids)} 个等权组合（走步拼接的日收益）：收益 {pct(float(eq.iloc[-1] - 1))}，年化夏普 {sr:.2f}，"
                     f"回撤（按日收盘）{dd * 100:.1f}%")
            c = df.corr().values
            off = c[~np.eye(len(ids), dtype=bool)]
            L.append(f"  两两日收益相关系数：平均 {off.mean():.2f}，最高 {off.max():.2f}")
    L.append("")
    L.append("说明：初筛用的是向量化回测（和 freqtrade 的逐笔撮合不完全一样），通过的只是“值得用 freqtrade 正式复核”，不是可以上实盘。")
    return "\n".join(L) + "\n"


# ---------------------------------------------------------------- 自检
def self_test(a, cfg) -> int:
    ok = True
    print("自检 1/2：纯随机合成行情（没有任何规律）→ 应当 0 个通过", flush=True)
    a1 = argparse.Namespace(**{**vars(a), "synthetic": "none", "only": a.only, "out": None})
    run(a1, cfg, quiet=True)
    rows, meta = G["last_rows"], G["last_meta"]
    npass = [r["id"] for r in rows if r["verdict"] == "PASS"]
    nluck = [r["id"] for r in rows if r["verdict"] == "LUCK"]
    nerr = [r["id"] for r in rows if r["verdict"] == "ERROR"]
    print(f"  通过 {len(npass)} {npass}；像运气 {len(nluck)} {nluck}；出错 {len(nerr)} {nerr}；用时 {meta['secs']:.0f}s")
    if npass or nerr:
        ok = False
        if nerr:
            for e in meta["errors"][:10]:
                print("   ", e)
    print("自检 2/2：植入截面动量的合成行情 → X01 截面动量应当通过", flush=True)
    a2 = argparse.Namespace(**{**vars(a), "synthetic": "xsmom", "only": a.only or "X01 X02 X03 X14 T01 M02 A101",
                               "out": None})
    run(a2, cfg, quiet=True)
    rows = G["last_rows"]
    x01 = next(r for r in rows if r["id"] == "X01")
    x03 = next(r for r in rows if r["id"] == "X03")
    print(f"  X01 {VERDICT_ZH[x01['verdict']]}（走步夏普 {fmt(x01['wf'].get('sharpe'))}，DSR {x01['dsr']:.2f}）；"
          f"X03 短期反转 {VERDICT_ZH[x03['verdict']]}（走步夏普 {fmt(x03['wf'].get('sharpe'))}）")
    if x01["verdict"] != "PASS" or x03["verdict"] == "PASS":
        ok = False
    print("自检" + ("通过" if ok else "失败"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
