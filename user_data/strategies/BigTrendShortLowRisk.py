"""
BigTrendShortLowRisk -- BigTrendShort 的低风险版 (只做空, 日线)。

和 BigTrendShort 完全相同: 入场信号 (阴线跌穿 >= 3 条 EMA + MACD 确认 + 6 根冷却 + 多周期过滤)、
兜底止损 20%、持满 13 天超时离场、杠杆 3 倍, 一行都没改 (直接继承)。
唯一的区别: 每笔风险 risk_per_trade 从 1% 降到 0.5% (打到止损约亏总资金 0.5%), 仓位大约减半。
  0.5% = BigTrendShort 原版 1% 的一半 (原版的仓位从第 1 轮起就是 1%, 没改过);
  也是 easytrade_risk 共用风控新默认值 1% (2026-11-07 起, 以前 0.5%) 的一半。仍然是 "低风险版"。

为什么: 第二轮真实研究 (OKX 57 个币, 2024-01 ~ 2026-11) 里 BigTrendShort 样本外回撤 7.89%、训练段回撤 11.1%,
按当时 8% 的回撤上限, 离红线太近 (训练段已经超了)。2026-11-07 回撤上限放宽到 15% 以后, 原版的 11.1% 也在上限以内,
低风险版保留下来作为同一个信号的 "半仓" 版本: 仓位减半后回撤大约减半, 收益也大约减半, 盈亏因子基本不变 ——
它不会让策略 "变好", 只是少占回撤预算 (组合里给别的策略留出空间), 可以作为子账户组合里的一份 (docs/strategies-round3.md)。

!! "大约减半" 的前提是每一笔都真的减半了, 不一定成立:
   - 合约张数要取整: 有的交易两个版本都只能开最少的一档 (例如都是 1 张), 这一笔的风险根本没减;
   - 仓位低于交易所最小下单量 (freqtrade 最多放大 30% 去凑) 时这一笔开不出来, 两个版本做的就不是同一批交易了
     (合成数据、1000 USDT: 原版有 10 笔这里没开出来, 其中 8 笔是赚钱的; 单独比较时谁好谁差都可能反过来)。
   研究报告 (scripts/research_report.py) 在这个策略那一行逐笔对比两个版本, 用真实数据核对 "减半" 成不成立;
   上线前用真实的交易所规则跑 --strategy-list BigTrendShort BigTrendShortLowRisk (1000 USDT 和子账户实际资金各一次)。

关于 "组合热度上限": BigTrendShort 用的是 rust_port_common.RustPortMixin, 它没有热度上限
(那是 easytrade_risk 才有的), 同时最多亏多少由 max_open_trades 决定: 研究配置 5 笔 x 0.5% = 2.5%。
"""

from __future__ import annotations

from BigTrendShort import BigTrendShort


class BigTrendShortLowRisk(BigTrendShort):
    # RustPortMixin.custom_stake_amount: 名义仓位 = 总资金 x risk_per_trade x 多周期降仓系数 / (止损距离 + 0.2%)
    risk_per_trade = 0.005  # 0.5% = 原版 BigTrendShort (1%) 的一半, 也是 easytrade_risk 新默认值 (1%) 的一半
