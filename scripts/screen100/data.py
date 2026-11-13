"""读取 freqtrade 下载的 OKX 永续K线（feather），拼成"时间 × 币"的面板。

文件名约定（和 scripts/download_okx_data.sh、make_synthetic_data.py 相同）：
  <数据目录>/futures/<币>_USDT_USDT-<周期>-futures.feather        K线（date open high low close volume）
  <数据目录>/futures/<币>_USDT_USDT-1h-funding_rate.feather       资金费率（每次结算一行，open 列 = 费率）
OKX 合约的 volume 是"币的个数"（docs/okx-volume-units.md），所以成交额 = close × volume（USDT）。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd


def read_jsonc(path: Path) -> dict:
    """读带 // 注释的 JSON（项目里的配置文件都这么写）。只去掉不在字符串里的 // 注释。"""
    out_lines = []
    for line in path.read_text(encoding="utf-8").splitlines():
        in_str = False
        esc = False
        cut = None
        for i, ch in enumerate(line):
            if esc:
                esc = False
                continue
            if ch == "\\":
                esc = True
            elif ch == '"':
                in_str = not in_str
            elif not in_str and ch == "/" and i + 1 < len(line) and line[i + 1] == "/":
                cut = i
                break
        out_lines.append(line if cut is None else line[:cut])
    text = "\n".join(out_lines)
    text = re.sub(r",(\s*[}\]])", r"\1", text)  # 尾逗号
    return json.loads(text)


def pairs_from_file(path: Path) -> list[str]:
    d = read_jsonc(path)
    if isinstance(d, list):
        return list(d)
    if "pair_whitelist" in d:
        return list(d["pair_whitelist"])
    ex = d.get("exchange") or {}
    return list(ex.get("pair_whitelist") or [])


def pair_to_file(pair: str) -> str:
    # "BTC/USDT:USDT" -> "BTC_USDT_USDT"
    return pair.replace("/", "_").replace(":", "_")


@dataclass
class Panel:
    tf: str
    o: pd.DataFrame
    h: pd.DataFrame
    l: pd.DataFrame
    c: pd.DataFrame
    v: pd.DataFrame          # 币的个数
    dv: pd.DataFrame         # 成交额 USDT = close × volume
    funding: pd.DataFrame    # 每根K线内结算的资金费率之和（没有数据 = 0）
    funding_known: pd.DataFrame  # 这一根有没有资金费数据（布尔）
    bars_per_year: float
    notes: list[str] = field(default_factory=list)

    @property
    def pairs(self) -> list[str]:
        return list(self.c.columns)

    def ret(self) -> pd.DataFrame:
        return self.c.pct_change(fill_method=None)


TF_BARS = {"1d": 365.0, "4h": 365.0 * 6, "1h": 365.0 * 24}
TF_FREQ = {"1d": "1D", "4h": "4h", "1h": "1h"}


def _read_feather(p: Path) -> pd.DataFrame | None:
    if not p.is_file():
        return None
    df = pd.read_feather(p)
    if df.empty:
        return None
    df["date"] = pd.to_datetime(df["date"], utc=True)
    return df.drop_duplicates("date").set_index("date").sort_index()


def load_panel(datadir: Path, pairs: list[str], tf: str, start: pd.Timestamp | None,
               end: pd.Timestamp | None, funding_dirs: list[Path] | None = None) -> Panel:
    fut = datadir / "futures"
    cols: dict[str, dict[str, pd.Series]] = {k: {} for k in ("o", "h", "l", "c", "v")}
    fund: dict[str, pd.Series] = {}
    notes: list[str] = []
    missing = []
    for p in pairs:
        df = _read_feather(fut / f"{pair_to_file(p)}-{tf}-futures.feather")
        if df is None:
            missing.append(p)
            continue
        if start is not None:
            df = df[df.index >= start]
        if end is not None:
            df = df[df.index < end]
        if len(df) < 30:
            missing.append(p)
            continue
        for k, col in (("o", "open"), ("h", "high"), ("l", "low"), ("c", "close"), ("v", "volume")):
            cols[k][p] = df[col].astype(float)
        # 资金费：先看主数据目录，再看额外目录（例如 user_data/data/okx-funding 里 2022-03 起的官方归档），合并
        parts = []
        for d in [datadir] + list(funding_dirs or []):
            fdf = _read_feather(d / "futures" / f"{pair_to_file(p)}-1h-funding_rate.feather")
            if fdf is not None:
                parts.append(fdf["open"].astype(float))
        if parts:
            s = pd.concat(parts)
            s = s[~s.index.duplicated(keep="last")].sort_index()
            fund[p] = s
    if missing:
        notes.append(f"{len(missing)} 个币没有 {tf} 数据或太短，跳过：{' '.join(m.split('/')[0] for m in missing[:30])}"
                     + (" ..." if len(missing) > 30 else ""))
    if not cols["c"]:
        raise SystemExit(f"数据目录 {fut} 里一个 {tf} 文件都没找到（先 make download）")
    frames = {k: pd.DataFrame(v).sort_index() for k, v in cols.items()}
    idx = pd.date_range(frames["c"].index.min(), frames["c"].index.max(), freq=TF_FREQ[tf], tz="UTC")
    frames = {k: v.reindex(idx) for k, v in frames.items()}
    c = frames["c"]
    # 资金费：每根K线 [t, t+tf) 里结算的费率加起来
    fr = pd.DataFrame(0.0, index=idx, columns=c.columns)
    known = pd.DataFrame(False, index=idx, columns=c.columns)
    for p, s in fund.items():
        s = s[(s.index >= idx[0]) & (s.index < idx[-1] + pd.Timedelta(TF_FREQ[tf]))]
        if s.empty:
            continue
        b = s.groupby(s.index.floor(TF_FREQ[tf])).sum().reindex(idx)
        fr[p] = b.fillna(0.0)
        first, last = s.index.min().floor(TF_FREQ[tf]), s.index.max().floor(TF_FREQ[tf])
        known[p] = (idx >= first) & (idx <= last)
    cov = known.values.mean() if known.size else 0.0
    notes.append(f"资金费覆盖 {cov * 100:.0f}% 的 币×K线（没有数据的按 0 算；OKX 接口只给最近约 3 个月，"
                 f"2022-03 起的官方归档在 user_data/data/okx-funding）")
    return Panel(tf=tf, o=frames["o"], h=frames["h"], l=frames["l"], c=c, v=frames["v"],
                 dv=c * frames["v"], funding=fr, funding_known=known, bars_per_year=TF_BARS[tf], notes=notes)


def synthetic_panel(n_pairs: int = 40, start: str = "2019-06-01", end: str = "2026-11-01", seed: int = 0,
                    tf: str = "1d", plant: str | None = None, strength: float = 0.003) -> Panel:
    """自检用的合成行情。plant=None：纯随机（共同市场因子 + 个币噪音，厚尾，没有任何可预测性）。
    plant="xsmom"：植入"过去 28 天强的币接下来继续强"的截面动量；plant="tsrev"：植入短期反转。"""
    rng = np.random.default_rng(seed)
    freq = TF_FREQ[tf]
    idx = pd.date_range(start, end, freq=freq, tz="UTC", inclusive="left")
    n = len(idx)
    scale = 1.0 if tf == "1d" else (1 / 6) ** 0.5
    mkt = rng.standard_t(4, n) * 0.025 * scale
    beta = rng.uniform(0.7, 1.5, n_pairs)
    idio_vol = rng.uniform(0.02, 0.05, n_pairs) * scale
    eps = rng.standard_t(4, (n, n_pairs)) * idio_vol / np.sqrt(2)
    r = mkt[:, None] * beta[None, :] + eps
    if plant == "xsmom":
        lr = np.zeros_like(r)
        for i in range(28, n):
            past = r[i - 28:i].sum(axis=0)
            z = (past - past.mean()) / (past.std() + 1e-12)
            lr[i] = strength * scale * z
        r = r + lr
    elif plant == "tsrev":
        r[1:] += -0.25 * eps[:-1]
    logp = np.cumsum(r, axis=0) + np.log(rng.uniform(1, 100, n_pairs))[None, :]
    c = np.exp(logp)
    o = np.vstack([c[0:1], c[:-1]]) * np.exp(rng.normal(0, 0.001, (n, n_pairs)))
    rng_hl = np.abs(rng.normal(0, 0.02 * scale, (n, n_pairs)))
    h = np.maximum(o, c) * np.exp(rng_hl)
    l = np.minimum(o, c) * np.exp(-np.abs(rng.normal(0, 0.02 * scale, (n, n_pairs))))
    vol = np.exp(rng.normal(12, 1, n_pairs))[None, :] * np.exp(rng.normal(0, 0.4, (n, n_pairs)) + 3 * np.abs(r))
    cols = [f"S{i:02d}/USDT:USDT" for i in range(n_pairs)]
    cols[0] = "BTC/USDT:USDT"
    cols[1] = "ETH/USDT:USDT"
    # 上线晚的币：前面一段设成 NaN
    late = rng.choice(np.arange(2, n_pairs), size=n_pairs // 5, replace=False)
    mk = lambda a: pd.DataFrame(a.copy(), index=idx, columns=cols)
    frames = [mk(x) for x in (o, h, l, c, vol)]
    for j in late:
        cut = int(rng.uniform(0.1, 0.6) * n)
        for f in frames:
            f.iloc[:cut, j] = np.nan
    O, H, L, C, V = frames
    fr = pd.DataFrame(rng.normal(0.0001, 0.0002, (n, n_pairs)) * (3 if tf == "1d" else 0.5), index=idx, columns=cols)
    known = pd.DataFrame(True, index=idx, columns=cols)
    return Panel(tf=tf, o=O, h=H, l=L, c=C, v=V, dv=C * V, funding=fr, funding_known=known,
                 bars_per_year=TF_BARS[tf], notes=[f"合成数据（plant={plant}，seed={seed}）"])
