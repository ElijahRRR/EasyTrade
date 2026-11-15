"""第 7 轮附带：资金费率套利（现货多 + 永续空）每年能收多少（清单第 10 条，docs/round7-promoted.md 第 4 节）。

  python scripts/screen100/carry7.py                 # 真实数据（和初筛同一份配置：configs/screen7.json）
  python scripts/screen100/carry7.py --synthetic     # 合成数据试跑

这不是回测，是一笔账：对冲以后价格涨跌两边抵消，剩下的就是"收到的资金费 - 进出场成本"。所以不套回撤 / 盈亏因子门槛
（价格盈亏按 0 算，夏普会高得离谱，没有意义），只回答一个问题：扣掉成本以后，每年的净收益比把钱放着吃利息高多少。

规则（事先定好，不调）：
  * 每天看每个币过去 7 天的平均资金费（年化，只用昨天为止的数据）。高于进场线就做（现货买入 + 永续做空同样金额），
    跌到 0 以下就平。进场线给三档：5%、10%、20%，三档都报，不挑。
  * 每个币最多用总资金的 10%，同时最多 10 个币；位置不够时先做资金费高的。
  * 每份资金里 2/3 买现货、1/3 做永续保证金（永续 2 倍杠杆），所以"收到的资金费"要乘 2/3 才是对总资金的收益。
  * 成本两档：全部吃单（现货 0.10% + 永续 0.05% + 两边滑点各 0.02% = 每次进或出 0.19%）；
    全部挂单（现货 0.08% + 永续 0.02% = 0.10%）。
没算进去的风险：急涨时永续那条腿被强平或被交易所自动减仓（2025-10-11 那种行情）、现货和永续的价差短期拉大造成的浮亏、
交易所本身出事、有些币在 OKX 没有现货。这些都只会让实际结果比这里更差。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
ROOT = HERE.parent.parent

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from data import load_panel, pairs_from_file, synthetic_panel  # noqa: E402

ENTER = (0.05, 0.10, 0.20)
EXIT = 0.0
CAP = 0.10
SLOTS = 10
NOTIONAL_PER_CAPITAL = 2.0 / 3.0
COSTS = (("全部吃单", 0.0019), ("全部挂单", 0.0010))


def carry(f: pd.DataFrame, enter: float, cost_side: float) -> pd.DataFrame:
    """f：每天每币的资金费（当天结算的费率之和；没有数据 = NaN）。返回每天的：净收益、在做的币数、进出场次数。"""
    m7 = (f.rolling(7, min_periods=5).mean().shift(1) * 365.0).values
    fv = f.values
    T, N = fv.shape
    held = np.zeros(N, dtype=bool)
    ret = np.zeros(T)
    nact = np.zeros(T)
    turns = np.zeros(T)
    for i in range(T):
        m = m7[i]
        # 今天收昨天收盘时持有的币的资金费
        ret[i] += CAP * NOTIONAL_PER_CAPITAL * np.nansum(np.where(held, fv[i], 0.0))
        # 收盘时调整：不够格的平掉，有空位就补资金费最高的
        out = held & ~(m >= EXIT)  # NaN 也平
        held = held & ~out
        free = SLOTS - int(held.sum())
        cand = np.where(~held & (m > enter))[0]
        add = cand[np.argsort(-m[cand], kind="mergesort")][:max(free, 0)]
        held[add] = True
        n_change = int(out.sum()) + len(add)
        ret[i] -= n_change * CAP * NOTIONAL_PER_CAPITAL * cost_side
        nact[i] = held.sum()
        turns[i] = n_change
    return pd.DataFrame({"ret": ret, "n": nact, "turns": turns}, index=f.index)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="资金费率套利（现货多 + 永续空）逐年净收益估算")
    ap.add_argument("--config", default="configs/screen7.json")
    ap.add_argument("--datadir", default=None)
    ap.add_argument("--pairs-file", default=None)
    ap.add_argument("--funding-dir", default=None)
    ap.add_argument("--synthetic", action="store_true")
    ap.add_argument("--out", default=None)
    a = ap.parse_args(argv)
    os.chdir(ROOT)
    cfg = json.loads(Path(a.config).read_text(encoding="utf-8"))
    if a.synthetic:
        P = synthetic_panel(tf="1d", start="2022-03-01")
    else:
        fdirs = []
        fd = Path(a.funding_dir) if a.funding_dir else Path(cfg.get("funding_dir") or "")
        if str(fd) and fd.is_dir():
            fdirs.append(fd)
        P = load_panel(Path(a.datadir or cfg["datadir"]), pairs_from_file(Path(a.pairs_file or cfg["pairs_file"])),
                       "1d", None, None, fdirs)
    f = P.funding.where(P.funding_known)
    f = f.loc[f.notna().any(axis=1)]
    if f.empty:
        print("没有资金费数据（真实数据要先有 user_data/data/okx-funding，见 docs/round5-research.md 运行 C）", file=sys.stderr)
        return 1
    f = f.loc[f.index.min():]
    L = []
    L.append("资金费率套利（现货多 + 永续空）逐年净收益估算" + ("（合成数据试跑）" if a.synthetic else ""))
    L.append(f"数据 {f.index.min().date()} ~ {f.index.max().date()}，{f.notna().any().sum()} 个币有资金费数据；"
             f"每币最多 {CAP * 100:g}% 资金、最多 {SLOTS} 个币；资金的 2/3 是对冲仓位")
    L.append("")
    years = sorted(set(f.index.year))
    L.append("一、所有币一直做（不挑）时的资金费水平（年化，未扣成本，按对冲仓位算）")
    L.append(f"{'年份':<6}{'各币中位数':>10}{'各币平均':>10}{'为正的天数占比':>14}")
    for y in years:
        fy = f[f.index.year == y]
        per = fy.mean() * 365.0
        L.append(f"{y:<8}{per.median() * 100:>12.1f}%{per.mean() * 100:>12.1f}%{(fy > 0).values[fy.notna().values].mean() * 100:>16.0f}%")
    L.append("")
    L.append("二、按规则挑着做，扣掉进出场成本以后，对总资金的净收益（不是年化：当年有几个月数据就是几个月的收益）")
    for label, cost in COSTS:
        L.append(f"  成本：{label}（每次进或出 {cost * 100:.2f}%）")
        L.append(f"  {'年份':<6}" + "".join(f"{'进场线 ' + format(e * 100, 'g') + '%':>24}" for e in ENTER))
        res = {e: carry(f, e, cost) for e in ENTER}
        for y in years:
            cells = []
            for e in ENTER:
                d = res[e][res[e].index.year == y]
                cells.append(f"{d['ret'].sum() * 100:+6.2f}%（平均 {d['n'].mean():4.1f} 个币）")
            L.append(f"  {y:<8}" + "".join(f"{c:>24}" for c in cells))
        tot = []
        for e in ENTER:
            d = res[e]
            yrs = len(d) / 365.0
            tot.append(f"年均 {d['ret'].sum() / yrs * 100:+6.2f}%，进出 {int(d['turns'].sum())} 次")
        L.append(f"  {'全部':<6}" + "".join(f"{c:>24}" for c in tot))
        L.append("")
    L.append("怎么看：把上面的净收益和同一年稳定币理财 / 美元存款能拿到的利率比。高不出几个点就不值得承担强平、自动减仓和交易所风险。")
    L.append("没算进去的风险都只会让实际结果更差（见 scripts/screen100/carry7.py 开头）。")
    text = "\n".join(L) + "\n"
    print(text)
    out = Path(a.out) if a.out else ROOT / cfg.get("out_dir", "user_data/research/screen7") / (
        ("synthetic_" if a.synthetic else "") + "carry_" + datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S") + ".txt")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")
    print(f"结果文件：{out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
