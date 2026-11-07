# 策略说明

> 对应 freqtrade 2026.10，策略代码在 `user_data/strategies/`。最后更新：2026-11-05。
>
> 三个正式策略 **BigTrendShort / VolumeSpikeShort / DailyThreeSoldiersShort** 都是从 Rust 项目
> `crypto-signal-engine` 移植过来的。在 2026-06 的"90 天体检"里，这三个是仅有的、做空方向有统计优势（edge）的策略。
> 入场条件、阈值、评分、冷却、多周期过滤都按 Rust 源码逐条照搬，每一处对应关系都在代码注释里写了 Rust 文件名。
>
> **2026-11-07**：账户回撤上限（含浮亏）从 8% 放宽到 **15%**（杠杆上限 5 倍不变），见 README「风控硬规则」。
> 这三个移植策略自己的仓位（每笔 1%，`rust_port_common.py`）**没有改**；第 1 轮的结果是按 8% 判的。

---

## 0. 一页看懂

| 策略类名 | 一句话逻辑 | 周期 | 方向 | 默认止损 | 出场 | 6 月体检（Rust，只做空） | 建议 |
|---|---|---|---|---|---|---|---|
| `BigTrendShort` | 一根日线阴线同时跌穿至少 3 条均线，MACD 确认走弱 | 1d | 只做空 | 兜底止损：价格反向 20% | 止损 / 持满 13 天 | 409 笔，胜率 88.3%，每笔 +10.1 USDT（按 100U 名义），盈亏比 pf 5.97 | 主力候选 |
| `VolumeSpikeShort` | 某根 K 线成交额突然放大到平时的 50 倍以上，并且收阴 | 5m（可改 1m/3m） | 只做空 | 兜底止损：价格反向 10% | 止损 / 持满 48 小时 | 120 笔，胜率 82.5%，每笔 +11.1 USDT，pf 29.6 | 候选，样本少 |
| `DailyThreeSoldiersShort` | 日线连续 3 根阴线，并且一天比一天收得低 | 1d | 只做空 | 第 1 根阴线的最低价（Rust 原样） | 止损 / 持满 13 天 | 625 笔，合计 +283 USDT → 修正跳空偏差后 +2.4 USDT（约等于不赚钱） | 只观察，不建议单独上实盘 |
| `SampleFuturesSanity` | EMA20 / EMA50 金叉做多、死叉做空 | 1h | 多 + 空 | 固定 10%（账户口径） | 反向交叉 | 无 | 只用来测试整条流程 |
| `*RustBaseline`（3 个） | 上面三个策略的"Rust 口径对照版" | 同上 | 只做空 | 几乎没有 | 超时 | 无 | 只做对照，**不要实盘** |

6 月体检的总体结论（Rust `progress.md`）：Rust 全部策略 90 天共 11.7 万个信号，合计净盈亏约 −179 USDT，等于白忙；
只保留有 edge 的方向和周期后剩 1,602 个信号，合计 +5,715 USDT。这个组合里还有少量其他未被过滤的策略，不只是这三个。
第 3 阶段修正跳空成交后，基线为 +5,424 USDT；加任何移动止损或保本都更差。

**先读这三条：**

1. **做空赚钱，可能只是因为那 90 天在下跌。** 6 月体检窗口（约 2026-03 至 2026-06）整体是下跌行情，
   "只有做空有效"里可能有一大部分是跟着大盘跌的 beta，而不是策略本身的本事。行情转涨后，这几个策略可能连续亏损。
   Rust 项目自己的 `edge_filter.rs` 也写明了这条风险，要求"行情转向后重新体检"。
2. **Rust 的数字和真实账户不是一个口径。** Rust 回测里每个信号都单独开一笔固定 100 USDT，同时开多少笔都可以；
   真实账户资金有限、名额有限。判断"整体回撤 ≤ 15%"（2026-11-07 起；以前 8%）要看 freqtrade 回测结果（第 7 节），不要看 Rust 的数字。
3. **现在用的是 OKX 数据。** 币种、成交量、手续费都和币安不同。上实盘前要在币安数据上重新回测（见 README）。

---

## 1. 三个正式策略共用的规则

这部分写在 `user_data/strategies/rust_port_common.py`（`RustPortMixin`），三个策略完全一样。

### 1.1 杠杆

- 实际杠杆 = min(策略参数 `max_leverage_param`（默认 **3**），交易所对这个币允许的最大杠杆，**5**)，并且至少为 1。
- 5 倍上限写死在代码里（`HARD_MAX_LEVERAGE = 5`），任何参数都改不过它。逐仓模式。
- **杠杆不决定一笔亏多少钱。** 亏多少由下面的仓位公式决定；杠杆只影响占用多少保证金、离爆仓价多远。

### 1.2 每笔开多少（按止损距离定仓）

思路：先规定"这一笔如果打到止损，最多亏总资金的 1%"，再倒推应该开多大。

```
名义仓位 = 总资金 × 1% × 多周期降仓系数 ÷ (止损距离 + 0.2% 成本缓冲)
保证金   = 名义仓位 ÷ 杠杆
```

- **总资金** = freqtrade 口径的可交易总额：钱包 × 95%（配置 `tradable_balance_ratio`），包括已开仓占用的保证金，随盈亏变化。
- **0.2% 成本缓冲** 对应 Rust 的成本模型：手续费 5 个基点 + 滑点 2 个基点，每边一次，来回约 0.14%，再留一点余量。
- 安全限制：
  - 止损距离不足 1% 时按 1% 计算，防止仓位被放得过大；
  - 名义仓位不超过 1 倍总资金（`max_position_notional_pct`）；
  - 保证金不超过 freqtrade 给的建议仓位，约等于可用资金 × 95% ÷ `max_open_trades`（`cap_stake_by_proposed`）；
  - 止损价无效、或计算出错时，**一律不开仓**（不会回退到满仓）。

**1000 USDT 账户的例子**（3 倍杠杆，`max_open_trades` = 4，总资金按 950 计算）：

| 情况 | 止损距离 | 名义仓位 | 保证金 | 打到止损大约亏 |
|---|---|---|---|---|
| BigTrendShort（兜底止损） | 20% | 47 U | 15.7 U | 9.5 U（约 1%） |
| VolumeSpikeShort（兜底止损） | 10% | 93 U | 31 U | 9.5 U |
| DailyThreeSoldiersShort，止损在 3% 外 | 3% | 297 U | 99 U | 9.5 U |
| DailyThreeSoldiersShort，止损只有 0.5% | 按 1% 算 | 本应 792 U，受建议仓位 237.5 U 限制，实际 712 U | 237.5 U | 约 4.6 U |
| 多周期逆势、仓位减半时 | 同上 | 减半 | 减半 | 约 4.8 U |

账户变成 10000 USDT 时，上面所有数字自动乘 10，不用改代码。

> 可以看到 BigTrendShort 的仓位很小（名义只有总资金的 5% 左右），因为 Rust 原版没有止损，这里只能用很宽的 20% 兜底止损，
> 按"最多亏 1%"倒推就只能开这么大。想放大收益，可以调高 `risk_per_trade`，但要重新回测、确认整体回撤仍 ≤ 15%（见 1.5）。

### 1.3 止损

- freqtrade 期货的 `stoploss` 是**账户口径**，等于价格距离乘以杠杆。代码里 `stoploss = -0.60` 的意思是"3 倍杠杆下价格反向 20%"。
- 每笔成交后算出一个**固定的止损价**，之后不再移动：没有移动止损，没有保本。
  Rust 第 3 阶段实测，移动止损和保本的各种参数都是负优化（组合净盈亏从 +5,424 降到 +820 至 +1,334），所以默认关闭。
- 最终止损 = "策略自己的止损"和"兜底止损"里离入场价**更近**的那个。
  兜底止损 = 入场价 ± min(`disaster_stop_pct`, |`stoploss`| ÷ 杠杆)。
- 止损价存在交易记录里（trade custom data），机器人重启后会自动恢复；如果 freqtrade 重启后把止损放宽了，策略会把它收紧回来。
- 回测报告里有一部分止损显示为 `trailing_stop_loss`，**它和 `stop_loss` 是同一个固定止损**，统计时合在一起看。
  原因是 freqtrade 回测成交后会按重算的均价把止损挪动约 0.000000000001，于是被标成"移动止损"，策略这边改不掉这个标签。
- **交易所止损单：** 币安模拟盘/实盘用的 `configs/config.binance-futures.json` **已经**设了 `stoploss_on_exchange: true`，
  并且按标记价格（`stoploss_price_type: mark`）触发 —— 止损单直接挂在交易所，机器人掉线也能止损，不需要再改什么。
  只有 OKX 回测用的共享底稿 `configs/config.base.json` 里是 `false`，这对回测没有影响（回测本来就是模拟止损）。
  下单类型统一在配置里设，策略里不写，两边都写时 freqtrade 以配置为准。
- **止损补救（只在模拟盘/实盘出现）：** 成交后 freqtrade 用"当时的盘口价"来设止损。如果止损离入场价很近
  （三连阴的 b1.low 有时只差零点几个百分点），盘口价可能已经越过了止损价，这时 freqtrade 设不上止损，
  仓位只剩很宽的兜底止损（20%），而仓位是按近止损算的，打到兜底会亏掉远超 1% 的资金。
  策略发现这种情况会**立即平仓**，离场原因显示为 `rust_stop_breached`；按实际成交价算策略止损在错误一侧时同样立即平仓，
  离场原因 `rust_stop_invalid`。这两种平仓单越过盘口 0.5% 挂限价，保证马上成交。
  回测里不会出现这两种原因（回测按入场价设止损，总能设上）。自检：`make guard-test`。

每个策略可选的止损模式（`stop_mode`）：

| 策略 | 默认 `"rust"` | 其他可选 |
|---|---|---|
| BigTrendShort | 无结构止损，只有兜底 20% | `"atr"`：入场价 + 3 × ATR14；`"signal_high"`：信号 K 线最高价上方 0.2% |
| VolumeSpikeShort | 无结构止损，只有兜底 10% | `"spike_high"`：巨量 K 线最高价上方 0.2%；`"atr"`：入场价 + 6 × ATR14 |
| DailyThreeSoldiersShort | 第 1 根阴线的最低价 b1.low（兜底 20%） | `"b1_high"`：第 1 根阴线的最高价；`"atr"`：入场价 + 2 × ATR14 |

> 换成非 `"rust"` 模式就是一个**新策略**了：6 月体检只证明了默认模式。换模式后必须重新回测，最好还要做样本外验证。

### 1.4 出场

- **没有止盈。** `minimal_roi = {"0": 100.0}` 表示涨到 10000% 才止盈，等于关闭；Rust 信号本来就不带止盈。
- **超时出场：** 持有时间 = max(48 小时, 12 × K 线周期) + 1 根 K 线。最后这 1 根来自 Rust 回测的计数方式（`take_while` 多算一根），这里照搬。

  | 周期 | 实际持有 |
  |---|---|
  | 1d | 13 天 |
  | 4h | 52 小时 |
  | 5m | 48 小时 5 分钟 |
  | 1m | 48 小时 1 分钟 |

- 到时间后挂限价单平仓（配置里 exit 订单类型是 limit；回测里按那根 K 线的开盘价成交），离场原因显示为 `rust_timeout`。
- 没有离场信号（Rust 没有）。

### 1.5 整体最大回撤上限（15%）怎么控制

上限 2026-11-07 起是 **15%**（以前 8%，第 1 轮按 8% 判；临时改用 `MAX_DD_PCT`，例如 `MAX_DD_PCT=12 make backtest`）。
它指的是**整个账户资金曲线**从最高点到最低点的跌幅，不是单笔亏损。判定工具是 `scripts/check_drawdown.py`：
它同时看"只算已平仓"和"含浮亏"两种回撤，取较大的一个；`scripts/backtest_all.sh` 跑完会自动调用它。

粗略估算：

- **同一时间最多亏：** `max_open_trades`（默认 4）× 每笔风险 1% ≈ **4%**。
  三个策略都只做空，币和币之间高度相关，一次急涨（逼空）可能让所有空单同时打到止损，所以要按"全部同时亏"来估。
- **连续亏损：** 15% 大约相当于连续 15 笔满额止损（每笔 1%）。

回测回撤超过 15%（或你自己定的更严的上限，例如 12%）时，按这个顺序调整：

1. `risk_per_trade` 从 0.01 降到 0.005（每笔最多亏 0.5%）；
2. `max_open_trades` 从 4 降到 3 或 2（在配置里改，或回测时加 `--max-open-trades 3`）；
3. 只跑 1d 的 `BigTrendShort`。

不要做的事：

- 不要靠提高杠杆来"省保证金"。杠杆不改变亏损金额，只会让爆仓价更近。
- 不要随手把止损改紧。那会改变策略本质，6 月体检结论就不适用了。

另外两点：

- 1d 策略每笔持有 13 天，4 个名额很快就占满，之后的信号会被丢掉，所以交易数会远少于 Rust。这就是"资金有限"的真实情况。
- 这三个策略目前**没有定义 protections**（比如连亏暂停、回撤暂停）。`backtest_all.sh` 默认带 `--enable-protections`，但对它们没有效果。
  以后需要的话可以加（例如 MaxDrawdown 保护），加完要重新回测。

### 1.6 多周期过滤（MTF）

来自 Rust `crates/engine/src/mtf.rs`。2026-06-14 才加进 Rust，**6 月体检时还没有**。

大白话：开仓前看一眼同一个币在日线、4 小时、1 小时三个周期上的大方向，加权打分。

三个周期的权重：日线 50%，4 小时 35%，1 小时 15%。每个周期按 EMA20 / EMA60 / EMA200 判断方向（判断顺序和 Rust 一致）：

| 方向 | 条件 | 分值 |
|---|---|---|
| 强多 | 收盘 > EMA20 > EMA60 > EMA200 | +2 |
| 强空 | 收盘 < EMA20 < EMA60 < EMA200 | −2 |
| 偏多 | 收盘同时在 EMA200 和 EMA60 上方 | +1 |
| 偏空 | 收盘同时在 EMA200 和 EMA60 下方 | −1 |
| 新币（不够 200 根） | 收盘在 EMA20 上方 / 下方 | +1 / −1 |
| 其他 | 都不满足 | 0 |

把三个周期的分值加权平均，再乘以交易方向，得到"顺势程度"，范围是 −2 到 +2（做空时，各周期都强空 = +2）。

| 策略类型 | 强烈逆势（≤ −1） | 偏逆势（≤ −0.35） | 偏顺势（≥ 0.35） | 强烈顺势（≥ 1） |
|---|---|---|---|---|
| 趋势突破类（BigTrend、三连阴） | **不开仓** | 评分 −12，仓位 × 0.5 | 评分 +4 | 评分 +8 |
| 确认类（VolumeSpike） | 评分 −5，仓位 × 0.5（不拦截） | 不变 | 不变 | 评分 +3 |

- 某个周期缺数据就跳过它，权重按剩下的周期重新分配；三个都缺就不调整。
- 只使用信号 K 线收盘时**已经收盘**的高、低周期 K 线，不会用到未来数据。
- 默认开启，和现在的 Rust 实盘一致。想复现 6 月体检口径，就把 `use_mtf_policy` 设为 `False`（Rust 口径对照版已经关掉）。
- 需要同一个币的 1d、4h、1h 数据都下载好。

### 1.7 严重度和入场标签

- 每个信号都有 0 到 100 的评分和严重度：LOW、MEDIUM、HIGH、CRITICAL。多周期调分后会按 ≥90 / ≥80 / ≥65 重新定级。
- `min_severity` 默认是 `"LOW"`，即不过滤，和 6 月 signal-backtest 一致。Rust 实盘的 LLM 交易员门槛是 `"MEDIUM"`。
- 入场标签（回测报告和 FreqUI 里的 enter_tag）示例：
  - `big_trend_short_x4_high`：BigTrend 做空，跌穿 4 条均线，严重度 HIGH；
  - `volume_spike_short_r100_high`：成交额是平时的 100 到 300 倍（r50 / r100 / r300）；
  - `daily_three_soldiers_short_medium`。

### 1.8 做多开关

- `enable_long = False`：默认只做空。Rust 的做多逻辑已经一并移植。
- 6 月体检里三个策略的做多端全部亏损。例如 BigTrend 做多：12 根后命中率 32.7%，净 −2.82%。
- 行情转涨后可以改成 `True` 重新回测。`enable_short` 同理。

### 1.9 怎么改参数

这些参数是策略类的属性，freqtrade 配置文件改不了。有两种改法：

- 直接改 `.py` 文件里对应那一行；
- **推荐：** 新建一个小文件继承原策略，原文件保持不动（`RustBaselines.py` 就是这样写的）。例如新建 `user_data/strategies/MyVariants.py`：

```python
from BigTrendShort import BigTrendShort

class BigTrendShortHalfRisk(BigTrendShort):
    risk_per_trade = 0.005      # 每笔最多亏 0.5%
```

回测时用 `--strategy BigTrendShortHalfRisk`。

改杠杆时注意：兜底止损 = min(`disaster_stop_pct`, |`stoploss`| ÷ 杠杆)。比如把 `max_leverage_param` 改成 5 而 `stoploss` 还是 −0.60，
兜底止损会从 20% 收紧到 12%。所以改杠杆时要同时把 `stoploss` 改成 −(`disaster_stop_pct` × 杠杆)。
同样，**不要在配置文件里覆盖 `stoploss`**。

常用参数速查：

| 参数 | 默认 | 作用 |
|---|---|---|
| `risk_per_trade` | 0.01 | 每笔打到止损时亏总资金的比例 |
| `max_leverage_param` | 3.0 | 杠杆（最终不超过 5） |
| `disaster_stop_pct` | 0.20 / 0.10 / 0.20 | 兜底止损（价格距离），也用来算仓位 |
| `stoploss` | −0.60 / −0.30 / −0.60 | = 兜底 × 杠杆，和上面两个保持一致 |
| `stop_mode` | `"rust"` | 止损模式（见 1.3） |
| `use_mtf_policy` | True | 多周期过滤开关 |
| `min_severity` | `"LOW"` | 最低严重度 |
| `enable_short` / `enable_long` | True / False | 方向开关 |
| `use_risk_sizing` | True | False = 不按风险定仓，直接用配置的 `stake_amount`（只给对照版用） |
| `max_position_notional_pct` | 1.0 | 单笔名义仓位上限（× 总资金） |
| `cap_stake_by_proposed` | True | 保证金不超过 freqtrade 的建议仓位 |

---

## 2. BigTrendShort（日线多均线跌破，只做空）

**来源：**

- 信号：`crates/strategies/src/big_trend.rs`（`BigTrend::on_closed_bar`）
- MACD 确认：`crates/strategies/src/macd_confirm.rs`
- 准入：`crates/llm_trader/src/edge_filter.rs`，只放行 1d × 做空
- 出场：`crates/llm_trader/src/sim.rs` 和 `crates/backtest/src/bin/signal_backtest.rs`

**逻辑（大白话）：** 一根日线开盘时还在一堆均线上方，收盘时已经跌到它们下方，说明一天之内把多条均线全部跌穿；
同时 MACD 也在加速向下。这通常是一段大跌趋势的开始，于是开空，拿 13 天。

**条件**（只用已经收盘的 K 线，没有未来函数）：

1. 至少 220 根日线，EMA7、20、60、100、200 都已经算出来；
2. 同一根 K 线开盘价高于某条 EMA、收盘价低于它，这种情况对**至少 3 条** EMA 成立；
3. 冷却：同一个币、同一个方向，6 根 K 线内只算一次。Rust 是在 MACD 确认**之前**就占用冷却，这里照搬：
   一根"跌穿了但 MACD 没确认"的 K 线，也会挡住后面 6 天的信号；
4. MACD 确认：MACD 在信号线下方，柱子为负，并且柱子比上一根更负（继续扩张）。

**评分（只用于严重度标签，默认不过滤）：**

- 基础分：跌穿 3 / 4 / 5 条均线分别为 70 / 80 / 90；
- MACD 刚刚死叉 +10，否则 +6；
- 收盘在 EMA200 下方 +5；
- 成交量 ≥ 前 20 根均量的 2 倍 +5；
- 实体占振幅 ≥ 60% +5；
- 上限 100。≥80 为 HIGH，≥65 为 MEDIUM。

**周期：** 1d。Rust 也跑 4h，但体检显示 4h 约等于 0、1h 为负，所以 `enforce_edge_filter = True` 时非 1d 周期不产生信号。
想重测 4h，要把它改成 `False` 再加 `--timeframe 4h`。

**主要参数：**

| 参数 | 值 | Rust 常量 |
|---|---|---|
| `min_bars` | 220 | `ctx.window.len() < 220` |
| `min_emas_crossed` | 3 | `MIN_EMAS_CROSSED` |
| `dedup_bars` | 6 | `DEDUP_BARS` |
| `ema_periods` | 7, 20, 60, 100, 200 | `window.rs` |
| `startup_candle_count` | 300 | Rust 实盘预热拉 300 根（`warmup.rs`） |
| `disaster_stop_pct` / `stoploss` | 0.20 / −0.60 | Rust 没有止损（新增的兜底） |

**出场：** 兜底止损（价格反向 20%）或持满 13 天。

**6 月体检（来自 Rust `edge_filter.rs` 和 `progress.md`）：**

| 口径 | 结果 |
|---|---|
| SQL 全量历史，信号后持有 12 根，双向、全部周期 | 命中率 52.4%，平均净收益 +0.23% |
| 同上，只看做空 | 命中率 71.5%，+3.18%（做多：32.7%，−2.82%） |
| 同上，只看 1d | +2.27%（4h 约 0，1h 为负） |
| 90 天 signal-backtest，双向、全周期（过滤前） | 胜率 50.7%，每笔 −0.16 USDT，pf 0.96 |
| **90 天 signal-backtest，只留 1d × 做空，超时 12 天** | **n = 409，胜率 88.3%，每笔 +10.1 USDT（100U 名义），pf 5.97，Rust maxDD 1.2%** |
| 第 3 阶段：修正跳空成交后 | 每笔仍为 +10.1；加移动止损或保本后降到 +1.5 至 +2.0 |

说明：命中率是"12 根 K 线后顺方向为正的比例"，净收益已扣来回成本 0.14%。
"每笔 USDT"按每笔 100 USDT 名义计算，+10.1 约等于每笔赚 10%。Rust 的 maxDD 口径和 freqtrade 不同，不能拿来和回撤上限（15%）比。
**注意 beta 风险**（见第 0 节）：409 笔几乎都发生在同一段下跌行情里，彼此高度相关，实际有效样本远少于 409。

**和 Rust 的差别：**

- 加了 20% 兜底止损（Rust 只靠超时出场）。下跌行情中这几乎不触发，反弹行情中会截断亏损；
- 有仓位和名额限制（Rust 不限）；
- 多周期过滤默认开启（6 月没有）；
- 其他共同差异见第 8 节。

**回测：**

```bash
bash scripts/backtest_all.sh --strategies "BigTrendShort" --timerange 20250101- --detail 1h --fee 0.0007
```

`--detail 1h` 用 1 小时数据模拟日线内部的走势，止损判断更准（已验证超时仍是正好 13 天）。

---

## 3. VolumeSpikeShort（单根巨量卖盘，只做空）

**来源：**

- 信号：`crates/strategies/src/volume_spike.rs`（`VolumeSpike::on_closed_bar`）
- 准入：`edge_filter.rs`，只放行做空
- 多周期：`mtf.rs` 的确认类规则

**逻辑（大白话）：** 平时每根 5 分钟 K 线成交几万美元的币，突然有一根成交了平时的 50 倍以上，而且是阴线，
说明有大资金在集中砸盘。顺着它做空，拿 48 小时。

**条件：**

1. 至少 21 根 K 线；开盘价、收盘价、成交量都大于 0；
2. 本根成交额（收盘价 × 成交量）≥ 前 20 根平均成交额的 **50 倍**。均值不含本根，只算成交额大于 0 的根，至少要有 10 根；
3. 前 20 根平均成交额 ≥ **50,000 USDT**（过滤冷门币）；
4. 收阴（收盘 < 开盘）就做空；收阳是做多信号，默认关闭；平盘忽略；
5. 冷却：同一个币、同一个方向，5 根 K 线内只算一次。

**评分：**

- 成交额是平时的 50 到 100 倍：65；100 到 300 倍：80；≥300 倍：95；
- 实体占振幅 ≥ 70% 再加 5 分；
- ≥80 为 HIGH，否则 MEDIUM。

**周期：** 默认 5m。Rust 同时跑 1m、3m、5m，6 月体检把三个周期合在一起统计。
想换周期，可以改代码里的 `timeframe`，也可以回测时加 `--timeframe 1m`。冷却和超时会跟着周期自动换算，但超时始终有 48 小时的下限。
freqtrade 一个机器人只能跑一个周期，所以要分别回测各个周期。分开测的结果加起来不等于 Rust 的合并统计，
因为同一次巨量可能在 1m 和 5m 上各触发一次，Rust 会算两笔。

**主要参数：**

| 参数 | 值 | Rust 常量 |
|---|---|---|
| `lookback` | 20 | `LOOKBACK` |
| `min_vol_ratio` | 50 | `MIN_VOL_RATIO` |
| `min_avg_notional` | 50,000 USDT | `MIN_AVG_NOTIONAL` |
| `dedup_bars` | 5 | `DEDUP_BARS` |
| `startup_candle_count` | 300 | 信号只需 21 根；取 300 是为了让日线、4h、1h 的 EMA200 有足够数据 |
| `disaster_stop_pct` / `stoploss` | 0.10 / −0.30 | Rust 没有止损（新增的兜底） |

**出场：** 兜底止损（价格反向 10%）或持满 48 小时 5 分钟（5m 周期）。

**6 月体检：**

| 口径 | 结果 |
|---|---|
| SQL 全量历史，持有 12 根，双向 | 命中率 62.1%，平均净收益 +3.48% |
| 同上，只看做空 | 命中率 73.0%，+6.14%（做多：19.4%，−6.99%） |
| 90 天 signal-backtest，双向（过滤前） | 胜率 67.6%，每笔 +4.74 USDT，pf 3.59 |
| **90 天 signal-backtest，只做空** | **n = 120，胜率 82.5%，每笔 +11.1 USDT，pf 29.6，Rust maxDD 0.1%** |

**注意：** 90 天只有 120 笔（还是三个周期合计），pf 29.6 这种数字在小样本加单边行情下很常见，不能当成常态。

**和 Rust 的差别：**

- 加了 10% 兜底止损；
- 只能单周期运行；
- OKX 和币安的成交量分布不同，巨量出现的时间和次数不会完全一样；
- 其他共同差异见第 8 节。

**回测：**

```bash
bash scripts/backtest_all.sh --strategies "VolumeSpikeShort" --timerange 20250101- --fee 0.0007
# 换 1 分钟（需要先下载 1m 数据）：
bash scripts/backtest_all.sh --strategies "VolumeSpikeShort" --timeframes "1m" --timerange 20250101- --fee 0.0007
```

---

## 4. DailyThreeSoldiersShort（日线三连阴，只做空）

**来源：**

- 信号：`crates/strategies/src/daily_three_soldiers.rs`
- 准入：`edge_filter.rs`，只放行做空
- 多周期：`mtf.rs` 的趋势突破类规则
- 出场：`sim.rs`。Rust 第 2 阶段把超时从 5 × 周期改成 12 × 周期，就是因为这个策略"需要约 12 根日线的持有期"

**逻辑（大白话）：** 连续三天都是阴线，而且一天比一天收得低（经典的"三只乌鸦"形态），说明空头连续占优，顺势做空。

**条件**（最近 3 根已收盘日线：b1 最早，b3 最新）：

1. 三根都是收盘 < 开盘；
2. 阶梯下跌：b2 收盘 < b1 收盘，b3 收盘 < b2 收盘；
3. 流动性：b3 的成交额 ≥ **5,000,000 USDT**。

Rust 在 2026-05 取消了"实体占比下限"，带长影线的也算。Rust 的 1 根 K 线冷却在日线上等于没有冷却，
所以连续 4 根阴线会在第 3、4 根各发一次信号，这里照搬。

**评分：**

- 基础分 70；
- 3 天累计跌幅 ≥ 10% 加 20，≥ 5% 加 10；
- 成交量逐日递增再加 10；
- ≥80 为 HIGH，否则 MEDIUM。

**周期：** 1d。

**止损（重要）：** Rust 把这个做空策略的止损设在 **b1.low**（第一根阴线的最低价），这里默认照搬。

- 价格连跌之后，b1.low 通常在入场价上方不远处，所以这是一个**很紧的止损**，经常被正常反弹扫掉；
- 如果 b1 下影线很长，b1.low 会低于入场价，止损就落在了错误的一侧。Rust 的模拟器会在入场那根立刻按开盘价止损（只亏手续费），
  这里直接**不开这一笔**；
- 可选 `stop_mode = "b1_high"`（形态最高点，逻辑上更合理）或 `"atr"`（2 × ATR14）。但那是新策略，需要重新验证。

**主要参数：**

| 参数 | 值 | 说明 |
|---|---|---|
| `min_notional` | 5,000,000 USDT | Rust `MIN_NOTIONAL` |
| `startup_candle_count` | 30 | 形态只要 3 根。取 30 是为了不把新上线的币排除掉 |
| `stop_mode` | `"rust"`（b1.low） | 见上 |
| `disaster_stop_pct` / `stoploss` | 0.20 / −0.60 | 兜底 |

`startup_candle_count = 30` 的代价：回测开头约 200 天内，日线 EMA200 还没算出来，多周期过滤的日线部分退化成只看 EMA20
（Rust 对新币也是这样处理）。更在意多周期精度的话，在配置里加 `"startup_candle_count": 300`。

**出场：** b1.low 止损（不超过 20% 兜底）或持满 13 天。

**6 月体检：**

| 口径 | 结果 |
|---|---|
| SQL 全量历史，持有 12 根（不止损），双向 | 命中率 64.9%，平均净收益 +2.96% |
| 同上，只看做空 | 命中率 77.9%，+7.01%（做多：25.7%，−9.29%） |
| 90 天 signal-backtest，双向（过滤前，当时 1d 超时还是 5 天） | 胜率 38.7%，每笔 −0.31 USDT，pf 0.95 |
| 90 天 signal-backtest，只做空，超时 12 天 | n = 625，合计 +283 USDT，pf 1.10 |
| **第 3 阶段：修正跳空成交后** | **合计 +2.4 USDT（约等于打平）** |

**结论：** 只看"持有 12 天、不止损"的方向统计，这个形态很强；一旦加上 Rust 的 b1.low 紧止损，就基本不赚钱了。
所以它目前**只适合观察**，不建议单独上实盘。如果想研究，可以用 `stop_mode = "b1_high"` 或 `"atr"` 回测对比，
但要做样本外验证，并且注意 beta 风险。

**回测：**

```bash
bash scripts/backtest_all.sh --strategies "DailyThreeSoldiersShort" --timerange 20250101- --detail 1h --fee 0.0007
```

---

## 5. SampleFuturesSanity（流程冒烟测试）

- **用途：** 只用来验证"下载数据 → 回测 → 模拟盘 → 中文通知"整条流程是通的。**不是赚钱策略，不要实盘。**
- **逻辑：** 1 小时线 EMA20 上穿 EMA50 开多，下穿开空，反向交叉平仓；止损 10%（账户口径，2 倍杠杆下等于价格反向 5%）；
  杠杆 = min(2, 交易所上限, 5)。
- 多空都会交易，所以能同时检验做多、做空、杠杆、止损、离场信号和资金费计算。

```bash
bash scripts/backtest_all.sh --strategies "SampleFuturesSanity" --timerange 20250101-
```

它不需要通过回撤检查。

---

## 6. Rust 口径对照（`RustBaselines.py`）

这个文件里有三个对照版：`BigTrendShortRustBaseline`、`VolumeSpikeShortRustBaseline`、`DailyThreeSoldiersShortRustBaseline`。
它们**只用于**和 6 月体检做同口径比较，用来确认移植没有走样。**不要实盘**：没有风险定仓，几乎没有止损。

和 Rust `signal_backtest.rs` 对齐的地方：

- 每笔固定 100 USDT 名义、1 倍杠杆（Rust 按名义金额算盈亏，杠杆只影响爆仓价），不按风险定仓（`use_risk_sizing = False`）；
- 关闭多周期过滤（6 月还没有）；
- BigTrend 和 VolumeSpike 只有 90% 的兜底止损（约等于没有）；三连阴仍是 b1.low；
- 超时 = max(48 小时, 12 × 周期) + 1 根；无止盈；无移动止损。

**推荐命令**（复现 6 月体检窗口 2026-03-13 至 2026-06-11）：

```bash
bash scripts/backtest_all.sh \
  --strategies "BigTrendShortRustBaseline VolumeSpikeShortRustBaseline DailyThreeSoldiersShortRustBaseline" \
  --timerange 20260313-20260611 --no-protections \
  --stake-amount 100 --dry-run-wallet 10000 --max-open-trades 1000 \
  --enable-position-stacking --fee 0.0007
```

参数说明：

- `--fee 0.0007` = Rust 的手续费 5 个基点 + 滑点 2 个基点，每边计一次；
- `--max-open-trades 1000` 加 `--enable-position-stacking` 用来模拟 Rust"不限同时持仓、同一币可叠加"；
- 对照版**不需要**通过回撤检查，这里看的是胜率、每笔平均收益、pf 和 Rust 是否接近。

仍然对不齐的地方：

- 币池不同（OKX 20 个主流币 vs 币安），所以交易笔数不会一样；
- 窗口结束时还没到期的交易，freqtrade 会在最后一根强制平仓（`force_exit`），Rust 则会用之后的数据持有到期；
- 三连阴"止损在错误一侧"的信号，Rust 记一笔只亏手续费的交易，这里不开仓。

---

## 7. 怎么回测

### 7.1 下载数据

1d 策略需要 1d、4h、1h 数据（多周期过滤用）；VolumeSpike 还需要 5m（以及想测的 1m、3m）。
数据开始日期要比回测开始日期**早约 300 天**（EMA200 预热和多周期数据要用），否则 freqtrade 会自动把回测开始日往后挪。

```bash
# 例：回测从 2025-01-01 开始，数据从 2024-03-01 下载
bash scripts/download_okx_data.sh --timerange 20240301- --timeframes "5m 1h 4h 1d"
# 复现 6 月体检窗口：数据从 2025-05-01 开始即可
bash scripts/download_okx_data.sh --timerange 20250501- --timeframes "5m 1h 4h 1d"
# VolumeSpike 测 1m / 3m 时再加（1m 下载很慢）
bash scripts/download_okx_data.sh --timerange 20250501- --timeframes "1m 3m"
```

### 7.2 跑回测和回撤检查（上限 15%）

```bash
# 三个正式策略（建议都加 --fee 0.0007，和 Rust 的成本假设一致，也比 OKX 默认费率保守）
bash scripts/backtest_all.sh \
  --strategies "BigTrendShort VolumeSpikeShort DailyThreeSoldiersShort" \
  --timerange 20250101- --fee 0.0007

# 1d 策略用 1h 数据模拟日内走势，更准
bash scripts/backtest_all.sh --strategies "BigTrendShort DailyThreeSoldiersShort" \
  --timerange 20250101- --detail 1h --fee 0.0007

# 也可以直接用 freqtrade
.venv/bin/freqtrade backtesting --config configs/config.okx-backtest.json \
  --strategy BigTrendShort --timerange 20250101- --fee 0.0007 \
  --timeframe-detail 1h --export trades --breakdown month
```

注意：`backtest_all.sh` 不加 `--strategies` 时会把目录里**全部 7 个**策略都跑一遍，包括对照版和冒烟测试，
它们不通过回撤检查是正常的。

### 7.3 看结果

- **整体回撤：** 看 `summary.txt` 里的"回撤(平仓)"和"回撤(含浮亏)"，两者都要 ≤ 15%（2026-11-07 起；以前 8%）。
  freqtrade 输出里的 `Max % of account underwater` 只算已平仓交易；合约模式下它的 "(balance)" 那一行也不含浮亏。
- **离场原因：** `rust_timeout` = 持满到期；`stop_loss` 和 `trailing_stop_loss` 都是固定止损，合起来看；`force_exit` = 回测结束时强制平仓。
  模拟盘/实盘里还可能看到 `rust_stop_breached` / `rust_stop_invalid` = 止损补救立即平仓（见 1.3）；回测里不会有。
- **入场标签：** 按 enter_tag 分组，可以看出不同严重度和跌穿条数的表现。
- **交易数：** 太少（例如少于 30 笔）的结果不要下结论。

### 7.4 未来函数检查

```bash
.venv/bin/freqtrade lookahead-analysis --config configs/config.okx-backtest.json \
  --strategy VolumeSpikeShort --timeframe 5m --timerange 20250101-20250401
```

- 必须写 `--timerange`；`--timeframe` 可以不写（默认用策略自己的周期）。
- 两个 1d 策略在这个工具里会报 `mtf_bias_4h`、`mtf_bias_1h` 等**指标列**"有偏差"，这是误报：
  工具截断数据时保留了"刚开盘、还没收盘"的那根日线（freqtrade 按 `date <= 截断时间` 取数据），
  而那一天的 4h、1h 数据只截到了开头。策略只在日线收盘后才用这一行，那时当天的高、低周期 K 线都已收盘。
  关键指标 biased_entry_signals（入场信号偏差）和 biased_exit_signals（离场信号偏差）为 0。
- 另外用"只保留当时已经收盘的 K 线重新计算"的方法单独验证过：截断前后每一行的信号都完全相同。
- 不联网也能跑一遍（合成数据）：`make synthetic-test`，见 README「用合成数据自检」。

---

## 8. 和 Rust 版本的全部差异（逐条）

| # | 差异 | 影响 | 怎么处理 |
|---|---|---|---|
| 1 | **记账口径**：Rust 每个信号独立开 100U，不限同时持仓，同币可叠加；freqtrade 有名额限制，同一币同时只持一笔 | freqtrade 交易数远少于 Rust；结果更接近真实账户 | 要和 Rust 对比时用第 6 节的对照版和参数 |
| 2 | **交易所和币池**：Rust 用币安，这里用 OKX 20 个主流币 | 信号次数、时间都不同；流动性门槛（5M、50k USDT）在 OKX 上筛掉的币不同 | 上实盘前用币安数据复核 |
| 3 | **成本**：Rust 每边 7 个基点、不算资金费；freqtrade 默认用 OKX 手续费、没有滑点模型 | 不加 `--fee` 时偏乐观 | 回测加 `--fee 0.0007` |
| 4 | **资金费**：OKX 只能下载最近约 3 个月的资金费率，更早的按 0 算；Rust 完全不算 | 1d 策略持仓 13 天，约 39 次资金费；下跌行情里资金费常为负，空头要付钱 | 长持仓结果要考虑这部分成本；6 月窗口两边都按 0，口径一致 |
| 5 | **止损**：Rust 的 BigTrend 和 VolumeSpike 没有止损；这里加了 20% / 10% 兜底 | 下跌行情影响很小；反弹行情会截断亏损（也可能截断后来会回本的单） | 对照版用 90% 兜底来近似"无止损" |
| 6 | **三连阴 b1.low 在错误一侧**：Rust 立即止损（只亏手续费）；这里不开仓 | 少了一些只亏手续费的交易 | 已在代码和本文档说明 |
| 7 | **多周期过滤**：2026-06-14 才加入 Rust，6 月体检没有 | 默认版的信号比 6 月少，部分仓位减半 | `use_mtf_policy = False` 复现 6 月 |
| 8 | **EMA 起算点**：Rust 实盘从预热窗口（300 根）开始算 EMA；freqtrade 从全部历史开始算。两边都用 talib 的 SMA 起点算法 | EMA200 在开头几百根会有微小差异，之后收敛 | `startup_candle_count = 300` |
| 9 | **冷却**：Rust 的冷却记在内存里，服务重启会清空；这里按历史 K 线确定性计算 | Rust 重启后偶尔会多出信号 | 无需处理 |
| 10 | **严重度门槛**：Rust 只在 LLM 交易员那条路径要求 ≥ MEDIUM；signal-backtest 不过滤 | 无 | 默认 `"LOW"`，和 signal-backtest 一致；`min_severity` 可调 |
| 11 | **MACD 确认是否在 6 月就存在**：Rust 的 git 历史被压缩过，无法确认体检时 big_trend 是否已有 MACD 确认 | 如果当时没有，6 月的 big_trend 信号会比现在多 | 按 Rust 当前代码移植 |
| 12 | **LLM 交易员**：Rust 实盘由 LLM 决定止损、止盈和仓位，无法复现 | 这里复现的是 signal-backtest 的机械规则，不是 LLM 交易员 | 无 |
| 13 | **入场价**：没有差异 —— 两边都在信号之后下一根 K 线的开盘价入场（Rust `signal_backtest.rs`：`entry_price = entry_bar.open`） | 无 | 无 |
| 14 | **超时出场价**：Rust 在第 13 根收盘出场；freqtrade 在下一根开盘出场 | 永续合约 24 小时连续交易，两者几乎相同 | 无 |
| 15 | **K 线时区**：OKX 日线按 UTC 0 点切分（北京时间早上 8 点收盘），和币安一致 | 无 | 无 |
| 16 | **报告标签** `trailing_stop_loss` | 只是标签，实际是固定止损 | 和 `stop_loss` 合起来看 |
| 17 | **lookahead-analysis 误报**（1d 策略的多周期指标列） | 无，入场信号无偏差 | 见 7.4 |
| 18 | **三连阴 `startup_candle_count = 30`** | 回测开头约 200 天的日线多周期判定退化成只看 EMA20 | 可在配置里设 300 |
| 19 | **实盘下单**：配置里入场是限价单（挂在盘口同侧），10 分钟不成交就撤单；Rust 模拟假设吃单立即成交。策略设了 `ignore_buying_expired_candle_after = 600`：信号 K 线收盘 10 分钟后就不再入场，所以撤单后**不会**再按新价格重挂；中途有名额空出、机器人中途重启时也不会补开当天的信号 —— 和回测"下一根开盘入场、没名额就放弃"一致 | 10 分钟内没成交的信号就错过了（回测里这些信号是成交的），实盘交易数可能比回测少 | 模拟盘阶段观察成交率，必要时把入场改为市价单或对手价。不要把 600 改成 0（freqtrade 默认 0 = 整根 K 线期间都会入场，1d 策略就是一整天，会开出回测里不存在的交易） |

### 关于持仓量（OI）和资金费率

这三个策略**都不使用** OI 或资金费率作为信号，所以移植时没有数据缺口。
Rust 里用 OI、资金费率的策略（`oi_price_divergence`、`funding_extreme` 等）在 6 月体检中都被判定没有 edge，已经砍掉，这里也没有移植。
如果以后需要：freqtrade 2026.10 支持下载 `open_interest` 类型的数据，但 OKX 的历史 OI 很短，回测价值有限。
资金费率只作为**成本**计入回测，见上表第 4 条。

---

## 9. 文件和 Rust 源码对照

| freqtrade 文件 | 内容 | 对应 Rust |
|---|---|---|
| `rust_port_common.py` | 共用：EMA/MACD、冷却、多周期、仓位、止损、超时 | `engine/src/window.rs`、`engine/src/mtf.rs`、`strategies/src/macd_confirm.rs`、`llm_trader/src/sim.rs`、`backtest/src/bin/signal_backtest.rs` |
| `BigTrendShort.py` | 日线多均线跌破 | `strategies/src/big_trend.rs` |
| `VolumeSpikeShort.py` | 巨量卖盘 | `strategies/src/volume_spike.rs` |
| `DailyThreeSoldiersShort.py` | 日线三连阴 | `strategies/src/daily_three_soldiers.rs` |
| `RustBaselines.py` | 三个 Rust 口径对照版 | `backtest/src/bin/signal_backtest.rs` |
| `SampleFuturesSanity.py` | 流程冒烟测试 | 无 |

准入规则（哪个策略只做空、只在哪个周期）全部来自 `crates/llm_trader/src/edge_filter.rs`；
6 月体检数据来自该文件的注释、`analysis/signal_edge.sql` 和 `progress.md`。
