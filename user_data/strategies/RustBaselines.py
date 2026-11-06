"""
RustBaselines -- "Rust 口径对照" 版本, 只用于和 2026-06 的 90 天体检数字做同口径比较。

!!! 这不是要实盘的版本 (没有风险定仓、几乎没有止损) !!!

和 Rust crates/backtest/src/bin/signal_backtest.rs 尽量保持一致:
  - 每笔固定名义金额 (Rust --size 100 USDT), 1 倍杠杆 (Rust 的 PnL 按名义金额算, 杠杆只影响爆仓价)
  - 不用多周期拦截/降仓 (mtf.rs 是 2026-06-14 才加的, 6 月体检时还没有)
  - big_trend / volume_spike 没有止损 (这里放一个 90% 的兜底, 基本等于没有)
  - daily_three_soldiers 止损 = b1.low (Rust 原样)
  - 出场: 超时 max(48h, 12x周期) + 1 根; 无止盈; 无移动止损

推荐回测参数 (见 docs/strategies.md "Rust 口径对照"):
  --stake-amount 100 --dry-run-wallet 10000 --max-open-trades 1000
  --enable-position-stacking --fee 0.0007
  (fee 0.0007 = Rust 的 taker 5bps + 滑点 2bps, 每边)
"""

from __future__ import annotations

from BigTrendShort import BigTrendShort
from DailyThreeSoldiersShort import DailyThreeSoldiersShort
from VolumeSpikeShort import VolumeSpikeShort


class BigTrendShortRustBaseline(BigTrendShort):
    use_risk_sizing = False
    max_leverage_param = 1.0
    use_mtf_policy = False
    stop_mode = "rust"
    disaster_stop_pct = 0.90
    stoploss = -0.95


class VolumeSpikeShortRustBaseline(VolumeSpikeShort):
    use_risk_sizing = False
    max_leverage_param = 1.0
    use_mtf_policy = False
    stop_mode = "rust"
    disaster_stop_pct = 0.90
    stoploss = -0.95


class DailyThreeSoldiersShortRustBaseline(DailyThreeSoldiersShort):
    use_risk_sizing = False
    max_leverage_param = 1.0
    use_mtf_policy = False
    stop_mode = "rust"  # b1.low
    disaster_stop_pct = 0.90
    stoploss = -0.95
