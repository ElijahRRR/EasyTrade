# 第 5 轮预注册规格（冻结，每个策略只测一次）

> 对应 freqtrade 2026.10，冻结于 2026-11-07。
> 第 1 ~ 4 节是冻结的规格原文（原标题"EasyTrade 第 5 轮自研计划（预注册版）"，原文件 sha256 `ae0460ae7c740cabba8a126ef754b004baef89f0f07d0a42a7ee442807abd3d3`；
> 只去掉了给构建步骤用的英文附录），**一个字都不要改**。
> 实现时规格没写清楚、或者 freqtrade 逼着做选择的地方，逐条写在第 5 节"实现与规格的差异"。
> 机器可读的登记表是 [`configs/preregistration.json`](../configs/preregistration.json)，怎么用、怎么记改动见第 6 节"登记表"。
> 正式运行之前要你定下来的事集中在第 7 节。命令和运行安排见 README「第 5 步：新策略类型（预注册，只测一次）」。
>
> **写这份文档时：4 个策略只在合成数据上跑通了流程（未来函数检查、预热检查、整条研究流程），还没有任何真实行情结果。**
>
> **已定：XSFundingCarry 的盈亏因子门槛 min_pf = 1.1。** 2026-11-08 David 在决定卡上选了"多空对冲策略用 1.1"，当时还没有任何真实数据的回测结果。
> 规格 2.1 节的默认是 1.2；其他 3 个策略仍是 1.2。见第 6.3 节。

---

## 1. 结论

**一句话结论：** 这一轮做 4 个和趋势无关的新机制：资金费套息、溢价极端回归、爆仓后反弹、新币做空加 BTC 对冲。每个规格在运行前冻结，在 2021-01 至 2026-11 的完整历史上只测一次。其中 XSFundingCarry 只能在你那台位于币安服务地区的服务器上用币安数据测，因为 OKX 拿不到 2021 年以来的资金费历史。另外 3 个现在就可以在美国 VPS 上用 OKX 数据测。

### 现在就做的 4 个

| 策略类名 | 机制家族 | 机制（一句话） | 周期 | 方向 | 预计交易/年 | 主要风险 |
|---|---|---|---|---|---|---|
| XSFundingCarry | 资金费套息（风险溢价） | 做空资金费最高的永续，做多资金费最低的，收取"给加杠杆的人提供杠杆"的报酬 | 1d，每周一调仓 | 多空，市场中性 | 约 150-300 | 高资金费的热门币被轧空；2025 年后资金费溢价衰减；PF 可能只有 1.1 左右 |
| PremiumFade | 基差极端回归（拥挤反转，短线） | 永续相对现货的溢价极端偏高或偏低时，反向做，等溢价回到正常 | 1h，最长持有 48 小时 | 多空 | 约 250-800（跑之前不知道） | 证据是间接的；现货和永续的最后成交价有噪音，会产生假极端；2024 年后信号变少 |
| CapitulationRebound | 强制平仓后的流动性补偿（事件反转） | 全市场去杠杆的暴跌日（大于 3 倍标准差、放量、BTC 同跌）之后买入，赚被迫卖出造成的超跌回补 | 1d，最长持有 5 天 | 只做多 | 约 40-120，集中在少数暴跌日 | 幸存者偏差（OKX 没有 LUNA、FTT 这类归零币）；有效样本只有十几个暴跌日 |
| NewListingShortHedged | 新币供给压力（事件驱动） | 新上线永续在炒作高点上市、流通量小、解锁不断，做空它并用等额 BTC 多单对冲 | 1d，持有到上线第 120 天 | 做空新币，加 BTC 多单对冲 | 约 30-80 笔空单，另加少量对冲调整 | 低流通币暴力轧空、跳空越过止损；OKX 回测算不到空头要付的负资金费；2021 年山寨季新币上涨 |

预计这 4 个之间的相关性：PremiumFade 的做多一侧和 CapitulationRebound 都在暴跌后买入，可能正相关，组合检查会按训练段相关系数 0.7 去重。XSFundingCarry 的空头大多是近期涨得多的币，预计和 XSMomentum 负相关。NewListingShortHedged 实际上是"做多 BTC 占比、做空山寨"，预计和 BigTrendShort 这类做空山寨的策略正相关，和 BTC 涨跌方向关系不大。

### 等数据再做

- **SpotPerpCashCarry**（BTC/ETH 买现货、空永续，收资金费）：需要币安 2019-09 以来的资金费历史，在你那台服务器上下载。先写一个约 50 行的 pandas 逐年计算：收到的资金费，减去 4 腿成本，再减去基差变化。实盘不能用 freqtrade，因为它要同时持有现货和永续两腿，需要改写 Hummingbot（Apache-2.0）的脚本。现在 BTC 资金费年化只有约 4%-5%，1000 USDT 一年大约赚 20-50 USDT，建议等资金到 10000 USDT 再考虑。
- **CapitulationReboundOI**（把 DeleveragingFlush 并进来）：前提是 CapitulationRebound 先通过。做法是在它的入场条件上加"当天持仓量下降至少 10%，或资金费不高于 0"，换一个新名字单独预注册。需要持仓量历史，来源有两个：一是币安 data.binance.vision 的 metrics 存档，只能在你那台服务器上下载，起始日期待核实；二是 OKX rubik 的 1d 持仓量，据说只到 2022-11 左右，未核实，而且不包括 LUNA 和 FTX 两次暴跌。
- **FundingCrowdingTS**（30 日平均资金费转负时买入大币）：需要币安 2019-09 以来的资金费历史。独立行情只有 5-8 段，每段 30 笔的交易数达不到，而且和 CapitulationRebound 属于同一类风险。以后最多当行情过滤器用，不单独当策略。

### 不做（一句话理由）

- **FailedBreakoutFade**：规则建立在唐奇安通道上，收益大约等于"负的唐奇安再减成本"。我们已经知道唐奇安 2021-2023 年赚钱、2024-2026 年亏钱，所以结果的方向基本已知，测了也不是独立证据。
- **BTCShockAltCatchup**：每年 500-1500 笔，期望收益 0.2%-0.6%，对比 0.14% 的来回成本。Kim & Hansen 2026 在币安永续上测到每笔毛收益只有约 0.5 个基点，和已经被成本吃光的那两个 1h 策略是同一种情况。
- **CTRENDReplication**：它包含动量，和已经在两种行情里都失败的 XSMomentum 同族。它的换手最高，28 个特征加弹性网的实现自由度太大，还需要 sklearn。加上指标预热以后，2021 年几乎没有可用样本。
- **CrowdingFilteredTrend**：我们已经看过 TsmomTrend 在哪里亏，这时再加过滤器属于事后补救，也不增加新的收益来源。
- **UnlockShort**：没有免费的、按当时时点记录的解锁日历，后来修订过的日程会泄露未来信息。2023 年以前的事件太少，2021-2023 那段凑不够 30 笔。
- **FreqAI-LGBM-Relative**：没有经济机制，失败了也说不出原因。每笔期望优势和成本差不多。venv 里没有 sklearn 和 lightgbm。选特征本身就是过拟合的通道。如果你一定要一个机器学习候选，就冻结规格只跑一次，排在这 4 个之后。
- **RetailCrowdingContrarian**：没有学术证据，指标定义不公开。存档的时间戳在 2026-06 悄悄改过，容易用到未来数据。它在美国 VPS 上也跑不了。

## 2. 预注册规格

**4 个策略的共同约定：**

- 成本按每边 0.07% 计（harness 的 `--fee 0.0007`）。
- 账户回撤上限 15%，含浮亏。
- 杠杆不超过 2 倍（混入类默认值），只有另外写明的除外。
- 混入类的回撤降仓保留：权益比最高点低 8% 起，新仓减半。
- 不调参：所有数值都写成普通类属性。
- 下面的"参数"指决定策略行为的数值，每个策略最多 3 个。"固定常量"指数据质量和结构上的设定，同样冻结，列出来是为了透明。

### 2.1 XSFundingCarry

- **数据：** 币安 U 本位合约，`configs/pairs.binance-research.json` 里的 60 个币。用 1d K 线、1h 标记价格和资金费率，时间 2019-09 至 2026-11。在你那台位于币安服务地区的服务器上下载并直接回测，不和 OKX 数据混用。
- **周期：** 主周期 1d，资金费率用 freqtrade 的 `funding_rate` 类型读取（每次结算一行）。
- **币池（每个调仓日按当时的数据算）：** 先选至少有 60 根日K线、而且调仓前 7 天资金费记录完整的币，再按过去 30 天成交额（成交量乘收盘价求和）取前 30 个。合格的币不足 15 个时，这一周不开新仓。
- **信号：** 设调仓时刻 T 为周一 00:00 UTC。F7 等于 T 之前 7 天内所有已结算资金费率之和，只用结算时间严格早于 T 的记录。用求和而不用平均，是为了让 8h、4h、1h 不同结算周期的币可以直接比较。
- **入场：** 周一 00:00 开盘。把 N 个合格币按 F7 从低到高排，取 n = floor(N/3)。做多 F7 最低的 n 个，做空 F7 最高的 n 个。
- **离场：** 只在调仓日检查。做多的币已经不在最低的 n 个里就平仓，做空的币已经不在最高的 n 个里就平仓，仍在组内就继续持有。没有止盈。
- **止损：** 每条腿的初始止损是入场价加减 3 倍 ATR20(1d)，最远 25%，不移动。被止损的币，如果下一个调仓日仍在对应的组里，可以重新开仓。
- **仓位：**
  - 这个策略不用"每笔 1% 风险"。原因是它的收入按名义金额收资金费，而且同时持有约 20 条腿；按每笔 1% 风险算，总名义仓位会超过 1 倍，热度上限也会挡掉大部分腿。
  - 改用总名义仓位 G = 0.4 倍账户权益，多空各 0.2 倍。同一边内按 1/σ20 分配，σ20 是过去 20 天日对数收益的标准差。
  - G = 0.4 的依据：Vine 2026 在总仓位 1 倍时最大回撤约 -35%，按比例缩小后约 14%。
  - 杠杆 2 倍，逐仓。热度上限 8%（和 XSMomentum 相同），max_open_trades 设为 20。
- **参数（3 个，都取自 Vine 2026，bryanvine.github.io/alpha-research/paper2.html，是唯一的扣成本后样本外结果）：**
  1. 回看 7 天。另外，资金费的 Hurst 指数为 0.82-0.94（MDPI Forecasting 2026），持续性强，支持按周使用。
  2. 按三分位分组。
  3. 每周调仓。Chi 等 2023 年（JFM）的周度 t 值为 3.4。
- **固定常量：** 币池取前 30 个（Vine 用的是 30 个币）、至少 60 根日K线、σ20 倒数加权、G = 0.4、止损 3 倍 ATR20 且最远 25%。
- **算失败的情况：**
  - harness 任意一段不通过。
  - 机制检查不通过：全期 funding_fees 合计必须是净收入。如果靠价格赚了钱，但资金费合计是净支出，说明赚的不是套息，也算失败。
  - 只在管道检查阶段才可能出现的"无效"：95% 以上的交易 funding_fees 为 0，说明资金费没有记进去。这种情况先修数据，不进入正式运行。
- **跑之前要你决定的一点：** 这种低波动的账户 PF 可能只有 1.1 左右，会被 PF 至少 1.2 的规则拒掉。默认不改规则。如果你想给市场中性账户用另一个门槛，必须在正式运行之前定下来，不能看完结果再改。

### 2.2 PremiumFade

- **数据：** OKX 研究币池（60 个）里有 OKX 现货 BASE/USDT 数据的币。永续 1h 数据已有，现货 1h 要另外下载，美国 VPS 可以下。
- **周期：** 主周期 1h，同一个币的 OKX 现货 1h 作为信息K线，按同一时间戳精确对齐，不向前填充。
- **指标：**
  - prem = 永续收盘价 / 现货收盘价 - 1，取同一根 1h K线。
  - prem8 = 最近 8 根 prem 的平均，大约是一个 8 小时结算周期。
  - z = (prem8 - 过去 720 根 prem8 的中位数) / (1.4826 × 过去 720 根 prem8 的 MAD)。
- **入场（信号K线的下一根开盘）：**
  - 做空：z 不低于 +3，而且信号K线永续收阴。
  - 做多：z 不高于 -3，而且信号K线永续收阳。
  - 数据条件：现货最近 24 根K线的成交额至少 100 万 USDT，最近 8 根现货K线都有数据。
  - 每个币 24 根K线冷却，用 CooldownPeriod 保护实现；harness 已经带 `--enable-protections`。
- **离场：** z 回到 0 之后的下一根开盘平仓（做空时 z 降到 0 或以下，做多时 z 升到 0 或以上）；或者持有满 48 根；或者打到止损。
- **止损：** 2 倍 ATR14(1h)，用混入类的 ATR 止损。
- **仓位：** 混入类默认值：每笔 1% 风险，热度 6%，带回撤降仓，杠杆 2 倍。
- **参数：**
  1. z 门槛 3。来源是 Leys 等 2013 年（J. Exp. Soc. Psych. 49(4)）的 MAD 异常值规则，3 是其中"非常保守"的一档。选保守是为了少交易。
  2. 窗口 720 根，即 30 天。分析资金费和溢价时常用 30 日均值，例如 K33 的 30 日平均资金费。
  3. 最长持有 48 小时。这是侦察员定的整数，大约是 6 次 OKX 结算，没有文献依据，这里照实写明。
- **固定常量：** 8 根平均、系数 1.4826、信号K线的阴阳条件、24 根冷却、现货成交额 100 万 USDT、止损 2 倍 ATR14。
- **算失败的情况：**
  - harness 任意一段不通过。
  - 平均每个币每年交易超过 30 笔。这说明信号并不"极端"，按失败记录，不再调门槛。
  - 能交易的币少于 20 个时记为"测不了"，不算通过。
- **回测偏保守的地方：** 持仓等待期间收到的资金费，只有最近约 3 个月被算进去，所以收入是少算的。

### 2.3 CapitulationRebound

- **数据：** 分两个阶段，两个阶段都通过才算通过。
  - 第一阶段：OKX 研究币池 60 个币，1d，美国 VPS 现在就能测。这一阶段不通过就是失败。
  - 第二阶段：只有第一阶段通过才跑。用币安 2021-2026 年的数据，包括已经下架的币（LUNA、FTT 等），在你那台服务器上跑。data.binance.vision 是否保留了下架币的数据，要先在那台服务器上核实。
- **周期：** 主周期 1d，信息K线是 BTC/USDT:USDT 的 1d。
- **信号（第 t 天收盘时，以下条件同时满足）：**
  - r_t = ln(C_t / C_{t-1}) 不高于 -3 × σ20。σ20 是第 t 天之前 20 天日对数收益的标准差，不包括第 t 天。
  - V_t 至少是前 20 天成交量中位数的 2 倍。
  - BTC 当天 r_t < 0。
- **同一天多个信号：** 按 r_t / σ20 从最负往上排，最多取前 5 个。之后还受热度上限 6% 和 max_open_trades 6 的限制。
- **入场：** 第 t+1 天开盘做多。
- **离场（哪个先到就按哪个）：** 某天收盘价回到 C_{t-1}（暴跌前一天的收盘价）或以上，下一根开盘平仓；持有满 5 根日K线；打到止损。
- **止损：** 在"L_t - 1 × ATR20"和"入场价 - 3 × ATR20"两者中，取离入场价更近的那个，而且止损价不低于 0.8 × C_t。L_t 是暴跌当天的最低价。
- **仓位：** 混入类默认值：每笔 1% 风险，热度 6%，杠杆 2 倍。
- **参数：**
  1. k = 3。这是"均值加 k 倍标准差"式的过度反应触发，Caporale 和 Plastun 2019 年研究加密币过度反应时用的就是这一类方法。3 倍是惯用的"极端日"门槛。
  2. 成交量倍数 2，惯例值。
  3. 最长持有 5 天，也就是一周，对应 Lehmann 1990 年（QJE）的周度短期反转。Nagel 2012 年（RFS）发现反转收益集中在高波动时期，Coval 和 Stafford 2007 年给出了被迫卖出后价格回补的证据。
- **固定常量：** BTC 当天下跌、每天最多 5 个、20 日窗口、上面的止损规则、回到前一天收盘价就离场。
- **算失败的情况：**
  - harness 任意一段不通过。样本外不足 30 笔也按规则算失败，不放宽。
  - 集中度检查不通过：去掉盈利最多的 3 个信号日后，没看过的一段或样本外变成亏损。这样规定是因为真正的有效样本是十几个暴跌日，而不是交易笔数。
  - 第二阶段（币安，包括下架币）不通过。

### 2.4 NewListingShortHedged

- **数据：** 分两个阶段，两个阶段都通过才算通过。
  - 第一阶段：OKX 全部 USDT 永续，不只是研究币池，1d，美国 VPS。OKX 回测里没有资金费，而新币空头常常要付很高的负资金费，所以第一阶段只算结果的上限：不通过就是失败，通过还不算数。
  - 第二阶段：币安 2021-2026 年全部 U 本位新上线币，已下架的能拿到多少算多少，带真实资金费和标记价格，在你那台服务器上跑。
- **上线日：**
  - 回测：用完整本地数据里这个币的第一根日K线，预先算好存成文件。第一根K线就是数据起点的币（2021 年以前就有的）不算新币。
  - 实盘：用交易所给的上线时间，OKX 是 listTime，ccxt 里是 market 的 created。
- **入场：** 上线当天算第 1 天，第 15 天开盘做空。条件是过去 7 天平均日成交额至少 100 万 USDT，OKX 的合约张数按仓库现有的口径换算成 USDT。
- **离场：** 上线满 120 天后的第一个开盘平仓，或者打到止损。没有止盈。
- **止损：** 固定止损，价格涨到入场价的 1.4 倍（+40%）就平仓。如果开盘价跳空越过止损价，按开盘价平仓，混入类的回测已经这样处理。
- **仓位：**
  - 每笔 0.5% 风险，而不是默认的 1%。原因有两个：低流通的新币经常一天涨 100%，跳空会让实际亏损达到计划的 2-3 倍；而且每笔持有约 106 天，同时持有的笔数很多。
  - 止损 40% 时，每笔名义金额约为权益的 1.25%。
  - 杠杆 1 倍：逐仓 1 倍的空单要涨约 100% 才会被强平。
  - 热度上限 6%，也就是同时最多约 12 笔。
- **对冲：**
  - 用一笔 BTC/USDT:USDT 多单对冲，名义金额等于所有未平新币空单按现价计算的名义金额之和（β = 1）。
  - 每天 00:00 UTC 检查一次，偏离超过 10% 才调整。没有空单时就平掉对冲单。
  - 对冲腿不设 ATR 止损，不计入热度，杠杆 1 倍。
- **参数：**
  1. 入场延迟 14 天。依据是 4Pillars 2024 年的统计：新币上线 30 天的收益中位数是 +2.9%，一半上涨，所以第一个月做空没有优势。
  2. 持有到上线第 120 天。4Pillars 统计的 6 个月收益中位数是 -60%，Animoca 统计 2024 年 OKX 新币平均 -27%。120 天介于 30 天和 6 个月之间，是侦察员选定的，不调。
  3. 止损 40%。这是文献侦察员的设定，给新币的高波动留出空间。
- **事后之见的风险：** 这三个数是看着 2023-2025 年新币的走势定的，所以 2021 年山寨季那批新币（没看过的一段）才是真正的检验。
- **算失败的情况：**
  - harness 任意一段不通过，特别要看 2021-2023 那段。
  - 第二阶段（币安，带资金费）不通过。
  - 只有对冲后的版本算数，不对冲的结果不报告，也不用来做任何选择。
  - 交易数 30 笔的门槛只数新币空单，不数对冲腿。

## 3. 检验流程

### 3.1 冻结（任何回测之前）

1. 把第 2 节原文写进 `docs/round5-preregistration.md`，和 4 个策略文件、各自的 research-overrides、新配置一起提交 git，记下 commit。研究脚本把每个策略文件的 sha256 写进 plan.json。正式运行时，如果文件和登记的不一致，就拒绝运行。
2. 登记表规则：正式运行之后，任何改动（参数、条件、周期、币池、成本）都算一个新策略，要换新的类名（例如 XSFundingCarry2），并在登记表里写明日期、改了什么、为什么改。旧结果原样保留，不覆盖。新名字的策略在同一段历史上再测，结果只能当参考；要成为独立证据，必须用新数据，例如之后的模拟盘。

### 3.2 管道检查（只看数据，不看收益）

- 每个策略跑 `freqtrade lookahead-analysis` 和 `recursive-analysis`。
- 用合成数据走一遍流程：`make research-synthetic ARGS="--strategies '...'"`。
- 只数数量，不看收益：
  - XSFundingCarry：每个币的资金费记录条数、起始日期，以及资金费和标记价格能对上的比例。
  - PremiumFade：现货和永续时间戳能对上的比例。
  - NewListingShortHedged：上线日文件里每年有多少个新币。如果数量明显凑不够每段 30 笔，这一步就直接记为"测不了"。
- 跑一个一周的小窗口（2026-11-01 至 2026-11-07），只打印交易笔数和 funding_fees 是否非零，不打印收益。

### 3.3 正式检验（每个规格只跑一次）

**切分方式（用 harness 现有的默认值）：**

- 研究从 2021-01-01 开始。
- 训练段约 2021-01 至 2024-10（前 2/3），样本外约 2024-11 至 2026-11（后 1/3）。
- 走步验证 6 折，每折 365 天训练加 180 天测试，只用默认参数，按拼接后的结果判。
- 没看过的一段是 2021-01 至 2023-12，`RESEARCH_SEEN_FROM=20240101` 不改。
- 对这 4 个新机制来说，整段历史都没有看过。保留这个切分，是为了让 2021 年牛市加 2022 年熊市这一段也必须单独赚钱。

**通过标准（沿用现有规则，不改）：** 训练段、没看过的一段、样本外、走步拼接，每一段都要同时满足：赚钱、含浮亏的账户回撤不超过 15%、PF 至少 1.2、至少 30 笔。另外，样本外 PF 不低于训练段 PF 的 0.6 倍，杠杆不超过 5 倍。最后再加上第 2 节里每个策略自己的失败条件。

**运行安排：**

- **运行 A（美国 VPS，OKX）：** PremiumFade 和 CapitulationRebound。先下好现货K线，再运行 `make research ARGS="--strategies 'PremiumFade CapitulationRebound' --no-download"`。
- **运行 B（美国 VPS，OKX 全部永续）：** NewListingShortHedged 单独跑：`RESEARCH_MIN_TRAIN_DAYS=0 RESEARCH_WARMUP_CANDLES=0 make research ARGS="--config configs/config.okx-research-listings.json --strategies NewListingShortHedged"`。还需要第 4 节里"保留下架币"的开关。
- **运行 C（你那台位于币安服务地区的服务器，币安数据）：** XSFundingCarry 用 `make research ARGS="--config configs/config.binance-research.json --strategies XSFundingCarry"`。另外，在运行 A 或 B 里通过第一阶段的策略，在这里跑第二阶段确认。

**2020 留出段（从来没用过）：** 主运行之后，每个策略只跑一次 2020-01-01 至 2020-12-31，2019 年的数据只用来预热指标，回撤口径和 `scripts/check_drawdown.py` 相同。

- CapitulationRebound 和 PremiumFade：用 OKX 上 2020 年就有数据的大币，以实际数据为准。这一段包括 2020-03-12 那次暴跌。
- XSFundingCarry：用币安 2020 年的数据。币安资金费从 2019-09 开始，所以 2020 年可以测。
- NewListingShortHedged：只在第二阶段，用币安 2020 年上线的币。
- 判法：交易至少 30 笔时，必须赚钱而且回撤不超过 15%，否则算失败。不足 30 笔时只报告，既不算通过，也不算失败。

### 3.4 组合检查

- 通过的策略用 `scripts/research_portfolio.py` 做组合：只按训练段挑选，训练段日收益相关系数达到 0.7 的两个只留一个，权重用 riskparity。组合总权益（含浮亏）的回撤不超过 15%，其他标准和单个策略相同。
- XSFundingCarry 的资金曲线来自币安那次运行，其他策略来自 OKX。需要 harness 支持把多个运行目录合在一起（见第 4 节），结果标注为"跨交易所近似"。
- 同时报告每个新策略和 TsmomTrend、DonchianTrend、XSMomentum、BigTrendShort 的训练段日收益相关系数。这些数只报告，不用来挑选。
- NFI：你要的"冻结 NFI、1 倍杠杆、2021-2024 逐年回测"由另一个任务做（`scripts/research_nfi_yearly.sh`），结果作为参照。即使它在 2022 年没有崩，它仍然是作者反复调过的版本，属于样本内。只有在同样的规则下通过，才放进组合检查当候选。
- 通过组合检查的策略，先在你那台服务器上跑币安模拟盘，每个策略一个子账户、一个机器人，至少 3 个月。这是唯一真正没见过的新数据，之后才考虑投入真钱。

### 3.5 如果 0 个通过

1. 不补救：不改参数、不换周期，也不放宽规则后重跑同一个策略。失败结果原样记进登记表。
2. 只做诊断，写进报告，留给下一轮设计用：亏在哪一段、成本占毛利多少、资金费占多少、亏损是否集中在少数几天。
3. 下一轮从"等数据再做"的清单里选。币安资金费和持仓量下载好之后，SpotPerpCashCarry 的逐年计算和 CapitulationReboundOI 都用新名字、重新预注册。
4. 规则由你决定，而且只能在下一轮开始之前定。例如，低波动的市场中性账户是否继续用 PF 至少 1.2、每段至少 30 笔。不能看完这一轮的结果再改这一轮的判定。
5. 前 4 轮已经全部失败。如果这一轮也是 0 个通过，诚实的结论是：在每边 0.07% 的成本、15% 的回撤上限和我们能拿到的数据下，暂时没有找到可靠、能自动交易的优势。这时把钱留作现金（或者只跑模拟盘），比放进一个没通过检验的策略更安全。

## 4. freqtrade 2026.10 实现要点

**通用：**

- 4 个新文件放在 `user_data/strategies/`，类定义写成 `class X(EasyTradeRiskMixin, IStrategy)`，并设置 `INTERFACE_VERSION = 3`、`minimal_roi = {"0": 100}`、`use_custom_stoploss = True`。所有数值写成普通类属性，不用 IntParameter 或 DecimalParameter。
- 另一个任务正在改这个仓库。等它结束后再加文件，只新增文件，不改已有的策略。
- 这一轮不用 FreqAI，不需要安装 `freqtrade[freqai]`（venv 里本来也没有 scikit-learn 和 lightgbm）。
- 没有许可证合适、可以直接复用的外部实现：Vine 的仓库没有 LICENSE 文件；Robot Wealth 的 R 代码是 GPL-3.0，只能当参考。所以这一轮复用仓库里已有的部分：XSMomentum 的截面排名框架，以及 FundingContrarian 的资金费读取方法。
- 已核实的一个陷阱：回测里 `dp.historic_ohlcv` 和 `dp.get_pair_dataframe` 给其他周期加载的预热是同样的**根数**，不是同样的天数（`get_required_startup` 直接返回 `startup_candle_count`）。也就是说，1d 策略设 `startup_candle_count = 130` 时，1h 的资金费只往前多加载 130 小时。

**XSFundingCarry：**

- `timeframe = "1d"`，`can_short = True`，`startup_candle_count = 130`（和 XSMomentum 一样，让 ATR20 收敛）。
- 资金费用 `self.dp.get_pair_dataframe(p, self.dp.get_funding_rate_timeframe(), candle_type="funding_rate")` 读取，照搬 `FundingContrarian._load_funding`。不要用 `@informative`，它遇到空数据会报错。
- `informative_pairs` 返回每个币的 `(pair, "1h", "funding_rate")`，这样实盘会刷新资金费。
- 因为上面那个预热陷阱，合格条件要用"至少 60 根日K线，加上前 7 天资金费窗口完整"，不能用"60 天资金费历史"。
- 截面计算照搬 XSMomentum 的 `_xs_frame`、`_xs_table`、调仓日掩码和 `confirm_trade_entry` 名额逻辑。
- 重写 `custom_stake_amount`：先按倒数波动率算名义金额，除以杠杆得到保证金，再乘以混入类的回撤降仓系数。
- `configs/research-overrides/XSFundingCarry.json` 里设 `max_open_trades` 为 20，类属性里 `max_portfolio_risk` 设为 0.08。
- 数据在你那台服务器上下载：`bash scripts/download_okx_data.sh --config configs/config.binance-research.json --timerange 20190901- --timeframes "1h 1d"`。对 OKX 以外的交易所，这个脚本会从开始日期下载标记价格和资金费。
- 币安最小下单额 BTC 是 100 USDT、ETH 是 20 USDT，在 1000 USDT 资金下这两条腿会被跳过。现有的 MIN_STAKE 日志标记会统计这些跳过。
- 实盘要在模拟盘核实一件事：结算周期为 1h 的币，内存里是否有完整 7 天（168 条）的资金费记录。

**PremiumFade：**

- `timeframe = "1h"`，`can_short = True`，`startup_candle_count = 760`。
- `informative_pairs` 返回每个币的 `(BASE/USDT, "1h", "spot")`。用 `self.dp.get_pair_dataframe(spot_pair, "1h", candle_type="spot")` 读取，按 date 左连接，不向前填充。
- 已核实：idatahandler 把现货文件放在 datadir 根目录，非现货的放在 `futures/` 下，所以期货回测里可以读到现货。
- 现货下载在美国 VPS 上：`freqtrade download-data --config configs/config.okx-research.json --trading-mode spot --pairs <BASE/USDT 列表> --timeframes 1h --timerange 20191201-`。
- OKX 现货的成交量单位是币，成交额等于成交量乘收盘价。
- 混入类设置：`atr_period = 14`，`stop_atr_mult = 2.0`，`max_hold_candles = 48`。冷却用 `protections()` 里的 CooldownPeriod，`stop_duration_candles` 为 24。
- 实盘前要在模拟盘确认：币安期货机器人里的现货信息K线能正常刷新。

**CapitulationRebound：**

- `timeframe = "1d"`，`startup_candle_count = 60`，BTC/USDT:USDT 1d 作为信息K线（同周期合并）。
- 同一天的排序（最多取 5 个）用 XSMomentum 那种缓存的截面表实现。
- 开仓时把 C_{t-1} 存进交易的 custom data，在 `custom_exit` 里和上一根已收盘的日K线比较，实现"回到前一天收盘价就离场"。
- `et_structure_stop` 返回 max(L_t - ATR20, 0.8 × C_t)，`stop_atr_mult` 设为 3.0，由混入类取两者中离入场价更近的那个。
- `max_hold_candles = 5`，`max_open_trades = 6`。

**NewListingShortHedged：**

- `timeframe = "1d"`，`startup_candle_count` 最多 14。已核实：回测时 freqtrade 会裁掉每个币自己开头的 `startup_candle_count` 根K线（`converter.trim_dataframe`）；预热设大了，正好会把要交易的那几天删掉。
- 上线日不能用加载进来的第一行数据，因为回测只加载"回测区间减去预热"那一段。正确做法是在运行前用一个只读脚本扫描本地完整的 1d 文件，生成 `user_data/listings/okx_first_candle.json`（币安另生成一份）。
- 币池：用 `--pairs ".*/USDT:USDT"` 下载 OKX 全部永续。脚本已经带 `--include-inactive-pairs`，但 OKX 是否还提供已下架合约的数据没有核实，要记录实际拿回了哪些已下架的币。
- 对冲：
  - BTC/USDT:USDT 放在白名单最后，`position_adjustment_enable = True`。
  - BTC 的入场信号由上线日文件算出：有任何一个币处在第 15 到第 120 天的持有窗口内就为真。
  - `custom_stake_amount` 和 `adjust_trade_position` 按未平空单的名义金额之和来定对冲大小，每天只在 00:00 UTC 调整一次，偏离超过 10% 才调。
  - 没有空单时，`custom_exit` 平掉 BTC 对冲单。
  - 对冲单的 enter_tag 设为 "hedge"，不计入热度，不用 ATR 止损。
- `check_drawdown.py` 已经能按逐笔订单重建加减仓（和 NFI 一样的处理）。
- 报告里的"缺失资金费估算"按中性的 0.01%/8h 计，空头算收入。对新币来说方向是反的，所以 OKX 那次的结果只能当上限。
- 实盘要注意：币安 BTC 最小下单额 100 USDT，1000 USDT 资金时对冲单经常开不出来，大约要 5000-10000 USDT 才行。

**harness 需要新增的功能：**

1. 预注册凭证：在 plan.json 里记录每个策略文件的 sha256 和 git commit；和登记表不一致时停止运行。
2. 现货下载：增加一遍 `--trading-mode spot` 下载，数据检查里报告现货和永续时间戳能对上的比例。
3. `RESEARCH_KEEP_DELISTED=1` 开关：数据检查时不去掉中途停止交易的币。
4. 资金费列：每段的 funding_fees 合计，以及 funding_fees 为 0 的交易占比。XSFundingCarry 的这个占比达到 95% 时，整行标为"无效"。
5. 2020 留出段：`RESEARCH_PRE_HOLDOUT="20200101-20201231"` 加 `RESEARCH_PRE_HOLDOUT_PAIRS`，用同一回撤口径单独列表。
6. 事件集中度：按入场日期分组，去掉盈利最多的 3 天后，重算没看过的一段和样本外。
7. 交易数口径：增加"平均每币每年交易数"一列；按 enter_tag 计数，NewListingShortHedged 只数空单。
8. 逐年表：每个策略每个日历年的收益和回撤，口径参照 `scripts/nfi_yearly_report.py`。只报告，判定仍按段。
9. 组合：`research_portfolio.py` 支持 `--run-dirs` 合并多个运行目录，另外加一张和参考趋势策略的相关系数表。
10. 新配置：`configs/config.binance-research.json`（币安 U 本位、逐仓、1000 USDT、StaticPairList 加 pairs.binance-research.json）和 `configs/config.okx-research-listings.json`（OKX 全部 USDT 永续）。

---

## 5. 实现与规格的差异

下面是 4 个策略和研究脚本（harness）实现时，规格没写清楚、或者 freqtrade 2026.10 逼着做选择的地方。**规则和参数都没有改**；
每一条都是"最贴近原文的实现"，写在这里是为了让结果可以对照。各策略文件开头的说明里也有同样的内容。

### 5.1 XSFundingCarry（`user_data/strategies/XSFundingCarry.py`）

1. **回测 / 分析时资金费从哪里读。** 规格说只用 `dp.get_pair_dataframe` 读资金费。回测和分析模式下，策略另外读同一个数据目录里完整的资金费文件（`_funding_disk`），
   只保留结算时间早于"最后一根日线收盘"的记录；每个调仓时刻 T 仍然只用结算时间 < T 的记录。
   - 原因：第 4 节的预热陷阱让 dp 只加载 130 小时的 1h 资金费，不够 7 天窗口；dp 还在回测区间结束时截断，而最后一根日线要一天以后才收盘。
   - 不这样做，lookahead-analysis 和 recursive-analysis 会误报未来函数，从周日开始的回测会丢掉第一周。
   - 实盘只用 dp（交易所缓存里就是最新的）。读不到磁盘文件时，策略不报错，只按 dp 给的数据算。
2. **"调仓前 7 天资金费记录完整"的定义**（规格没写）：
   - [T-7 天, T) 里至少 ceil(7 × 24 / 8) = 21 次结算；
   - 从窗口开始（T − 7 天）到第一次结算、相邻两次结算之间、以及最后一次结算到 T 的间隔都不超过 8 小时（`funding_max_interval_hours = 8.0`，币安最长的结算周期）。
   - 8 小时结算的币少一次就不完整；从 8 小时改成 4 小时或 1 小时结算的币仍然完整。
   - 只有窗口后半段才有记录的币（例如 4 小时结算、只有最后 4 天的记录，条数也有 24 条）不完整：复查时补上了"窗口开始到第一次结算"这一条，原来只查条数和后面的间隔。
3. **回测里币的历史。** 沿用 XSMomentum 的做法：freqtrade 按根数裁掉的每个币开头 `startup_candle_count` 根日线不参加排名（这几根不能交易）。
   所以回测里上线晚的币要等 max(60, 预热) 根日线才进币池：训练段和没看过的一段是 130 根，样本外和走步各段最多 600 根，而不是 60 根。实盘用 60 根。
4. **兜底止损是 -0.51，不是 -0.50。** 混入类的止损上限是 min(0.25, |stoploss| / 杠杆 × 0.999, 0.8 / 杠杆)，这样 2 倍杠杆时正好是规格的 25%。
5. **G 和热度上限用的账户权益** = `wallets.get_total(USDT)` + `et_portfolio_state` 里的持仓浮动盈亏，不乘 `tradable_balance_ratio` 0.95。
6. **热度上限按混入类的口径：**
   - 每条腿的风险 = 名义金额 ×（max(止损距离, min_stop_distance) + sizing_cost_buffer）；
   - 剩余额度不够时，把这条腿缩小到刚好放得下；
   - 剩余额度不到这条腿风险的 25% 时，这条腿跳过。
7. **仓位不受 proposed_stake（余额 ÷ max_open_trades）限制**，只受 freqtrade 的 max_stake 限制。
8. **名额逻辑**（规格：照搬 XSMomentum）：同方向、其他币上已有的持仓算进这一边的 n 个名额，除非这一次调仓的排名表显示它已经出组；组别不知道的也算。
   XSMomentum 有缓冲区，这个策略没有；不这样排除，结果会取决于 freqtrade 处理交易对的顺序（多空互换的币会被挡掉）。
9. **币池里的币数 N 的边界情况：**
   - N = 0（没有合格的币，例如资金费数据中断）：不做调仓平仓，止损照旧；
   - 1 ≤ N < 15：不开新仓，但调仓平仓照常按 n = floor(N / 3) 做，所以 N < 3 时全部平仓。
10. **入场另外要求**信号K线成交量 > 0、ATR 有值（XSMomentum 的可交易条件；止损要用 ATR）。
11. **σ20 ≤ 0 或缺失的币没有权重**，这条腿跳过；权重只在 σ20 有效的腿之间归一。
12. **窗口定义：**
    - "过去 30 天成交额" = 截至信号K线（周日那根日线，在 T 收盘）的 30 根日线的 成交量 × 收盘价 之和；
    - σ20 = 截至信号K线的 20 个日对数收益的样本标准差（ddof = 1）；
    - 排名并列时按交易对名字排。
13. **rebalance_days 的实现：** "距 1970-01-05（周一）的天数能被 rebalance_days 整除"，所以 7 = 每周一。
14. **调仓时不调整已有腿的大小**（规格没写）；新开的腿按当时的权益算。
15. **只改实现、行为不变的地方：**
    - 初始止损设好以后 `custom_stoploss` 返回 None（止损本来就不移动，省时间）；
    - F7、成交额、σ20、权重都按窗口内的值用 `math.fsum` 直接求和，不用累加和或 pandas rolling，所以结果和数据从哪天开始加载无关
      （rolling 版本有 1 ulp 的舍入差，lookahead-analysis 按"完全相等"比较，会报成未来函数）。
16. **保留 XSMomentum 的实盘晚到K线等待**（最多 300 秒）。
17. **下载命令：** 规格写 `--timeframes "1h 1d"`；研究脚本的数据检查要求 `RESEARCH_TIMEFRAMES`（默认 "1h 4h 1d"）里每个周期都有，
    所以配置注释和 README 写的是 "1h 4h 1d"（也可以设 `RESEARCH_TIMEFRAMES="1h 1d"`）。多下的 4h 不参加计算。
18. **`configs/config.binance-research.json` 故意不写 `candle_types` 和 `futures_funding_rate`：**
    - 不写 `candle_types`：freqtrade 下载合约数据时自动加 1h 标记价格和资金费率；写了反而会让标记价格跟着 `--timeframes` 走。
    - 不写 `futures_funding_rate`：用币安真实的资金费历史；两种数据缺一种的时间段资金费按 0 算（报告会数"资金费为 0 的交易占比"）。

已知现象（不是差异）：约 7% 的止损离场被 freqtrade 记成 `trailing_stop_loss`，其实止损没有移动过
（`recalc_trade_from_orders` 在入场成交时重新设一次止损，差一个价格单位，就标成了"跟踪"）。数离场原因时两种都按止损算。
1000 USDT 时 BTC 这条腿（最小下单额 100 USDT）多数周开不出来、ETH 有时被放大或跳过，所以总名义仓位比 0.4 低几个百分点（日志标记 `[最小下单额:开不出来]` / `[最小下单额:放大]`，报告会数）。

### 5.2 PremiumFade（`user_data/strategies/PremiumFade.py`）

1. **现货K线成交量 > 0 而且收盘价 > 0 才算"有数据"。** freqtrade 加载数据时（`load_pair_history` 的 `fill_up_missing=True`，实盘也一样）
   会把缺的K线用前一根的收盘价、成交量 0 补上，这等于向前填充；把成交量为 0 的K线当成缺失，"不向前填充"才成立。交易所真实的 0 成交量K线也当成缺失。
2. **z 里的"过去 720 根 prem8"** = 第 t 根之前（不含 t）最近 720 个有值的 prem8，跳过空值；不足 720 个、或者 MAD = 0 时 z 为空，没有信号。
3. **prem8 要 8 个 prem 都有值**（否则为空），等价于"最近 8 根现货K线都有数据"。24 根现货成交额 = 包括信号K线在内最近 24 根 1h 的 成交量 × 收盘价 之和，缺失的K线按 0 算。
4. **冷却**照原文用 CooldownPeriod，`stop_duration_candles = 24`。freqtrade 把锁定的结束时间向上取整到下一根K线（`PairLocks.lock_pair`），
   所以最早在平仓那根K线开盘之后第 25 根K线重新入场。
5. **现货交易对**照字面从 `BASE/USDT:USDT` 换成 `BASE/USDT`（币安 1000PEPE 这类合约会对不上现货，两个研究币池里都没有）。
6. **max_open_trades** 规格没写，用研究配置默认的 5（没有 PremiumFade 的 research-override）。

另外（只加日志，不改行为）：
- 没有现货数据的币记一行 `[现货数据:没有]`；每个币的现货覆盖情况记一行 `[现货数据:覆盖]`（"数据条件都满足且 z 有值的K线 K 根"，K = 0 也算测不了）。
  研究报告按这两个标记数"能交易的币"，标记字符串和 `scripts/research_report.py` 里的一样，要改两边一起改。
- 和 XSMomentum 一样的最小下单额标记。

### 5.3 CapitulationRebound（`user_data/strategies/CapitulationRebound.py`）

1. **σ20 用 pandas 样本标准差**（ddof = 1，规格没写）；σ20 = 0 时没有信号。
2. **同一天并列**按交易对名字排（`method="first"`）。
3. **回测里每个币开头 `startup_candle_count` 根不参加排名**（freqtrade `converter.trim_dataframe` 裁掉、不能交易），照搬 XSMomentum；实盘用全部行。
4. **热度 6% 或 max_open_trades 6 起作用时**，当天前 5 名里谁能开仓按 freqtrade 处理交易对的顺序（白名单顺序），不按名次：前 5 名之内先到先得，和 XSMomentum 相同。
5. **第二天开盘价已经低于或等于 max(L_t − ATR20, 0.8 × C_t) 时**，混入类找不到有效的止损，这一笔不开。
6. **混入类原有的止损上限**（2 倍杠杆时约 25%）也起作用，不过 0.8 × C_t 几乎总是更近。
7. **持有满 5 天和"回到 C_{t-1}"在同一个开盘价触发时**，离场原因记为 `et_timeout`，价格一样。
8. **只在实盘：** 关掉 `process_only_new_candles`，晚到的日线最多等 300 秒（XSMomentum 框架），不影响回测。
9. **max_open_trades 6** 放在 `configs/research-overrides/CapitulationRebound.json`（研究配置默认是 5）。
10. **样本外和走步各段的预热是 600 根，不是 60 根**（和 5.1 节第 3 条 XSFundingCarry 一样）：研究脚本给从中间开始的段多加载预热，最多 `RESEARCH_WARMUP_CANDLES` = 600 根（登记表 `run_settings` 固定），
    freqtrade 按根数裁掉每个币开头这么多根日线，排名（`_xs_backtest_trim`）也跳过这几根。所以这些段开始之前历史在 60 ~ 600 天之间的币，
    要等到第一根K线之后 600 天才能排名、交易。训练段和没看过的一段用冻结的 60 根；实盘用全部行。
    （PremiumFade 的预热 760 根 1h 本来就比 600 大，不受影响；NewListingShortHedged 的运行 B 用 `RESEARCH_WARMUP_CANDLES=0`，也不受影响。）
11. **数据中途结束的币**（第二阶段保留的下架币，`RESEARCH_KEEP_DELISTED=1`）：freqtrade 在这个币最后一根K线之后不再处理它，到这一段结束才按最后一根K线的开盘价强制平仓
    （force_exit，close_date 记为最后一根K线）。从最后一根K线之后的下一根开始，这笔交易不再计入热度 6%，浮动盈亏按那个强制平仓价算（`cr_data_ended`，复查时加的）；
    原来会按止损距离一直占着热度到这一段结束，挡住别的币开仓。**freqtrade 的名额（6 个之一）和保证金仍然被它占到这一段结束**，这是 freqtrade 回测的限制，策略改不了。
    实盘没有这个问题（交易所会通知下架，`cr_data_ended` 在实盘永远不起作用）。

另外：同 PremiumFade 的最小下单额标记（只是日志）。ATR20 在冻结的预热 60 根时和完全收敛的值差约 0.4% ~ 1%（Wilder 平滑，recursive-analysis 在合成数据上看到的），只记录，不改规格值。
第二阶段（币安，包括下架币）用同一个类、同一个文件。

### 5.4 NewListingShortHedged（`user_data/strategies/NewListingShortHedged.py`）

1. **startup_candle_count = 13，不是 14**（规格允许 ≤ 14）。已核实 freqtrade 2026.10 `optimize/backtesting.py` 的 `_get_ohlcv_as_lists`：
   `trim_dataframe(startup)` 和 `shift(1)` 之后还会删掉剩下的第一行，所以新币最早在第 startup + 2 天开盘入场，设 14 会丢掉第 15 天那次入场。
   指标只有 7 天平均成交额和上线日文件。
2. **max_open_trades 40**（配置和 research-override）只是上限，让 freqtrade 的名额不会先于热度 6% 起作用（约 12 笔空单 + 1 笔对冲），不是新规则。
3. **对冲的每日检查时机**（freqtrade 的处理顺序，实盘相同）：对冲单开着以后，它在同一根 00:00 K线上比后来新开的空单先处理，
   所以 00:00 新开的空单要到第二天检查时才算进去，而且偏离超过 10% 才调。BTC 放在白名单最后只解决对冲单第一次开仓。
   合成数据上核实过：除了一天（就是这种滞后）以外，每天偏离都不超过 10%，第二天就纠正了。
4. **在一根K线内部被止损的空单**，close_date 记为那根K线的开盘时间；到下一根K线之前，对冲目标和热度都仍然把它算作未平
   （否则对冲在开盘时就"知道"它会被止损，就是未来函数）。在开盘价平仓的空单（满 120 天离场、跳空止损）按在那个开盘价平掉算。
   所以最后一笔空单没了以后，对冲单在下一次 `custom_exit` 调用时平掉：不带细节数据时是第二天，带 1h 细节数据时是下一个小时。
5. **分析模式下的替代做法**（runmode `util_no_exchange`，即 lookahead / recursive-analysis 把白名单缩成一个币时）：
   - 币池按配置重建，正则按 StaticPairList 的方式展开（`expand_pairlist(..., keep_invalid=True)`）；
   - 对冲单金额 = freqtrade 建议的金额；没有新币处在 [第 15 天, 第 121 天) 窗口时，对冲单用 exit_long 信号离场；不做每日调整。
   - 这些工具只检查信号。正常回测和实盘用真实的"按空单名义金额对冲"的逻辑。
6. **对冲单不受混入类回撤降仓的影响**（它按空单名义金额 1:1 跟）；空单照常降仓。
7. **对冲单的 freqtrade stoploss = -0.99**，只是框架的兜底，不是 ATR 止损或固定止损。
8. **freqtrade 把空单的止损价按价格精度向下取整**（`persistence/trade_model.py`，空单 ROUND_DOWN），实际止损最多比 1.40 倍低一个价格单位
   （例如入场 0.1132、价格单位 0.0001 时是 1.3993 倍）。
9. **老币** = 第一根日K线 ≤ 数据起点的币。数据起点默认是数据目录里最早的第一根K线；要准确就写 `--data-start`（下载开始日期）。
   研究脚本传的是 BTC 第一根本地日线和这次下载开始日期里晚的那个（见 5.5 节第 8 条）。
10. **模拟盘 / 实盘也要有上线日文件**（没有 = 不开仓，和回测一样）。交易所给的上线时间（OKX `contTdSwTime` / `listTime`、币安 `onboardDate`、ccxt market 的 `created`）
    只用来补文件里没有的币，回测里不用。
11. **文件过期保护**（规格里没有）：加载的数据比文件里这个币的第一根K线还早时，这个币不交易（日志里有中文错误）。
12. **对冲调整小于交易所最小下单额时**，freqtrade 跳过；如果仍然需要，下一次每日检查再试（每天只检查一次）。
13. **数据中途结束的币**（下架币，运行 B 和第二阶段用 `RESEARCH_KEEP_DELISTED=1` 保留）：freqtrade 在最后一根K线之后不再处理这个币，到这一段结束才按最后一根K线的开盘价强制平仓。
    复查时加的（`nlsh_data_ended`）：从最后一根K线之后的下一根开始，对冲目标和热度都不再算这笔空单，浮动盈亏按那个强制平仓价算，对冲单在当天的每日检查里按剩下的空单调整
    （没有空单了就平掉）。原来对冲单会一直按这笔"已经没有数据"的空单对冲到这一段结束（合成数据上多对冲了 8 天），热度也一直被占着。
    freqtrade 的名额（40 个之一）和保证金仍被占到这一段结束（freqtrade 回测的限制；40 远多于热度允许的约 12 笔，名额不会先起作用）。实盘不起作用。
14. **同一根 00:00 K线上"最后一笔空单到期、另一个新币当天入场"**（复查时加的）：原来对冲单先按"没有空单"平掉、第二天再开回来，新空单一天没有对冲、多付两次手续费。
    现在没有空单时，对冲单先看这一根K线上有没有今天该入场的新币（上线日 + 14 天 = 今天、7 天平均成交额 ≥ 100 万，和入场信号同一个算法、只用信号K线及以前的数据），
    有就先不平，等这一根K线的空单开出来以后照常调整。只在K线开盘那一刻看（回测 = 开盘时刻；实盘 = 开盘后 `ignore_buying_expired_candle_after` 之内）。
    这个新币的空单最后没开出来（最小下单额、热度不够）时，对冲单多留一次检查再平（不带细节数据是一天，带 1h 细节数据是一小时）。

另外：白名单里除了 BTC 没有别的币时，策略记一条中文错误（研究脚本已经会按本地数据展开正则，见 5.5 节第 7 条）。
1000 USDT 时每笔空单约 12 USDT 名义金额，OKX 很多合约的最小下单量比这大，会开不出来；对冲的小额调整也常被跳过。第一阶段的交易数会因此偏少。

### 5.5 研究脚本：数据和流程（`scripts/research.sh`、`scripts/check_prereg.py`、`scripts/download_okx_data.sh`、`scripts/make_synthetic_data.py`）

1. **预注册检查不通过时 `research.sh` 退出码 4**（规格只说"拒绝"）；`check_prereg.py` 自己 3 = 拒绝、2 = 参数错误。
2. **规格没点名的开关：** `RESEARCH_PREREG` / `--prereg`（登记表路径）、`RESEARCH_LISTINGS`、`RESEARCH_FUNDING_CHECK`（auto / 0 / 1），
   以及每个环境变量对应的命令行写法（`--allow-unregistered`、`--with-spot`、`--no-spot`、`--keep-delisted`、`--pre-holdout`、`--pre-holdout-pairs`）。
3. **合成数据也做预注册检查**（不一致照样拒绝），但合成数据的运行永远不算正式运行。
4. **2020 留出段用哪些币**（规格："用币安 2020 年的数据"、"2020 年就有数据的大币，以实际数据为准"）：
   - 所有策略：本地数据和留出段有重叠的币（留出段里有数据就算，包括这一段里才上线、或者中途停止交易的）；上线晚的币由策略自己的预热和合格条件挡住，不会用到留出段以外的数据。
     复查时改的：原来除 NewListingShortHedged 以外默认是"数据覆盖整个留出段"，币安研究币池里 2020 年年中才上线的合约会被去掉，XSFundingCarry 可能凑不够它开仓要的 15 个合格的币；
     "有数据"按字面就是有重叠。选出来的币少于策略开仓要求（XSFundingCarry 15 个）时，研究脚本大声提醒（这一段一笔都不会开，结果只会是"不足 30 笔只报告"）。
   - NewListingShortHedged：同上（"覆盖整段"会正好去掉它要交易的币）；规格只在第二阶段（币安 2020 年上线的币）跑它的留出段；
   - 写了 `RESEARCH_PRE_HOLDOUT_PAIRS` 时：这些币里和留出段有重叠的；
   - 预热用策略自己的 `startup_candle_count`（freqtrade 从 2020-01-01 之前加载），不是 `RESEARCH_WARMUP_CANDLES`；只用默认参数，叠加这个策略的 research-override。
5. **`RESEARCH_MIN_TRAIN_DAYS=0` 时也保留样本外开始以后才上线的币**（运行 B 要用）。`research_report.py prepare` 即使是 0 也会去掉它们，
   `research.sh` 在后面一步加回来，记在 `plan.json` 的 `min_train_zero`（所以 data_check.txt 里先列成"去掉"、再列成"加回"）。
6. **交易所预检**（规格没有）：打开"保留下架币"、正则展开或留出段时，去掉交易对规格里没有、不活跃（除非允许）、或者没有杠杆档位的币，
   列在 `plan.json` 的 `exchange_preflight`。freqtrade 2026.10 遇到没有杠杆档位的币会让整次回测报错退出。
   所以 OKX 上 `RESEARCH_KEEP_DELISTED=1` 只能保留交易所规格里还有（可能不活跃）的合约；OKX 已经删掉的合约回测不了。
7. **正则白名单**（`config.okx-research-listings.json`）按本地有合约数据的币展开，写进 `结果目录/config.expanded.json`，BTC 仍在最后。
   freqtrade 只按交易所当前的合约展开正则，而数据检查需要具体的交易对。
8. **上线日文件的数据起点** = BTC 第一根本地日线和这次下载的开始日期里晚的那个（传给 `build_listings.py --data-start`；整合时改的，见 5.7 节第 6 条）。
   各个币从不同日期开始下载时，老币可能被当成新币，`build_listings.py` 会提醒。
9. **现货下载：** 用 `--include-inactive-pairs`，开始日期和合约下载相同（研究开始 − 预热，或者留出段开始 − 预热，取早的）；
   `--no-download`（运行 A）时什么都不下载，现货要事先 `make download-spot`；正则白名单没法换算成现货交易对，要用 `--spot-pairs`。
10. **合成数据（第 2 版）：** 新增 NEW9（2020-07 上线，在留出段里）和 NEW10（2025-12 上线，在样本外里）；非 legacy 的价格取整到十进制小数
    （否则已有的 BigTrend 三个策略的 lookahead 检查会因为 force_exit 的价格精度误报一次入场偏差）；
    `research.sh` 和 `synthetic_test.sh` 需要时自动把默认的合成数据目录重新生成成第 2 版（约 130 MB），`make synthetic-data ARGS="--legacy"` 可以还原第 4 轮以前那份。

### 5.6 研究报告（`scripts/research_report.py`、`scripts/research_portfolio.py`）

1. **3.3 节"每一段都要 … PF 至少 1.2、至少 30 笔"和"沿用现有规则，不改"互相矛盾。** 现有规则只在样本外和走步拼接上查 PF 和笔数，训练段和没看过的一段只查赚钱和回撤。
   - 做法：**登记过的策略按 3.3 节原文**，训练段、没看过的一段、样本外、走步拼接每一段都要 PF ≥ 这个策略的 min_pf、计数的交易 ≥ 30 笔；
     **没登记的策略**（第 1 ~ 4 轮）完全按旧规则，旧报告的结论一字不差。
   - 影响：合成数据上 XSFundingCarry 会因为"训练段盈亏因子 1.08 < 1.1"未通过。要不要这样判，见第 7 节第 2 条。
2. **funding_net_positive / zero_funding_share_invalid 的"全期"** = 训练段 + 样本外两次回测（没看过的一段和走步各折和它们重叠，不重复算；每段的合计照样打印）。
   - funding_fees 为空的交易按 0 算；"达到 95%" = 占比 ≥ max_share，记为"无效"。
   - 登记了这项检查就判，没有单独区分"只在管道检查阶段"：正式运行出现"无效"同样表示资金费没记进去，要先修数据再重跑（这一次不算正式结果）。
3. **drop_top_days（事件集中度）：** 按入场日期（UTC）分组，只去掉盈利为正的日子；剩下的交易按每次回测的 profit_abs 求和重算收益（不复利、不重新算仓位），
   PF 同样重算；"亏损" = 剩下的盈利 ≤ 0。CapitulationRebound 每个信号日 t 都在 t+1 开盘入场，所以按入场日分组等价于按信号日分组。
   列出的某一段没有结果时判未通过并写明原因，不会悄悄通过。
4. **max_trades_per_pair_year（平均每币每年交易数）的分母：** 每一段的"币-年" = 对每个能交易的币，从 max(这一段开始, 第一根K线 + 预热) 到这一段结束的天数（含两端）÷ 365.25，再求和。
   - PremiumFade（日志里有现货标记的策略）还要有现货：开始取 max(这一段开始, 合约第一根K线 + 预热, 现货第一根K线 + 预热)，结束取 min(这一段结束, 现货最后一根K线)
     （复查时加的：原来只按合约算，现货晚开始或者早结束的币分母偏大，"每币每年 > 30 笔"会被稀释）。
   - 交易数不数 count_tags_exclude 里的 enter_tag；按全部能交易的币平均（不只是有交易的币；没有现货的币不计入分母，免得把"不够极端"稀释掉）；
   - 用全期（训练段 + 样本外）判。
5. **min_tradable_pairs 的"能交易的币"：** 日志里有 PremiumFade 标记的策略，任何一段默认参数日志里"有值的K线 N 根"N > 0 的币（各段取并集）；
   没有这种标记的策略 = 数据检查后有数据的币。
6. **逐年表：** 训练段和样本外两次回测按复利拼成一条资金曲线（样本外的交易按训练段结束时的权益 ÷ 初始资金放大，和走步拼接的做法相同）。
   训练段结束、样本外开始的那一年不是一次连续回测（freqtrade 在训练段结束时把仓位平掉），表下有注释。
   年份来自主运行的区间，不是像 `nfi_yearly_report.py` 那样每年单独回测；回撤算法相同（含浮亏、逐根K线、峰值从年初起算）。
7. **2020 留出段的判定**对 jobs.tsv 里有默认参数留出段回测的所有策略生效（登记没登记都一样，规格对每个策略都这样规定），只判默认参数；回测失败或没有结果算失败。
8. **登记表里不认识的检查类型、或者参数写错**，这一行判未通过（"登记表写错：…"），不忽略；检查的参数没有默认值，必须写全（例如 max_share 0.95、n 3）。
9. **正式运行按运行时的登记表快照判**（复查时改的）：`plan.json` 的 `prereg.formal_run` 为真时，报告用 `prereg.strategies.<类名>.registry_entry`（运行时登记表里这个策略的原文）判，
   不读现在的登记表；现在的登记表和运行时不一样（或者读不出来）时，报告开头提醒，结论不变。不是正式运行时照旧按生成报告时读到的登记表判，改过也提醒。
   报告开头还写这次是不是正式运行（`正式运行：是 / 否` 和原因；用了 `RESEARCH_ALLOW_UNREGISTERED=1` 或有不一致时写"!! 不是正式的预注册运行"），`summary.csv` 最后加一列 `formal_run`。
10. **组合检查只用一个 min_pf：** `research_portfolio.py` 判组合时仍用全局设置的 1.2，不用某个策略登记的 min_pf。
    组合的默认挑选只看训练段（和以前一样），可能挑到"无效 / 测不了"的策略，所以第 5 轮做组合时用 `--strategies` 明确写出通过的策略（见 README）。
    指定的名单里有登记过的策略时，**照样按训练段日收益相关系数 ≥ 0.7 去重**（3.4 节；留训练段 收益/回撤 比更好的，输出里写明去掉了哪个）。复查时改的：原来指定名单一律不去重。
    没登记的策略指定时和以前一样不去重，只在训练段相关系数 ≥ 0.7 时大字提醒。来源目录不是正式的预注册运行时，组合输出开头写"!! 不是正式的预注册运行"。
11. **合并的组合（`--run-dirs`）按第一个目录：** 训练段 / 样本外 / 留出段 / 走步的区间、手续费、回撤上限都取第一个目录；几个目录的区间不同时只列出来提醒，不重新对齐，结论标"跨交易所近似"。
12. **资金费说明按交易所写：** 原来币安的运行也会写"OKX 只能下载最近约 3 个月"，现在按配置的交易所写（这是旧报告里唯一换掉的一行）。

### 5.7 整合时的改动

1. **登记时一起固定混入类和 research-override**（`check_prereg.py`）：规格只说"策略文件的 sha256"，可是 `easytrade_risk.py`（止损、仓位、超时、跳空离场）、
   `rust_port_common.py` 和各自的 `configs/research-overrides/<类名>.json`（max_open_trades）同样决定策略行为，改了它们而不换类名，就等于悄悄改了规格。
   所以登记表每个策略多一项 `pinned_files`（`check_prereg.py write` 自动计算），研究前核对，不一致和策略文件不一致一样拒绝；
   这些文件也要和策略文件一样先提交 git，否则结果记为不是正式运行。
2. **登记表里还标着"待定"的决定，正式运行拒绝**（`check_prereg.py`；合成数据只提醒）：规格 2.1 节"必须在正式运行之前定下来，不能看完结果再改"。
3. **min_pf 待定的写法**用报告认得的 `min_pf_pending_decision`（XSFundingCarry 构建时写的 `min_pf_status` 报告不认）。
4. **两个阶段在不同交易所的策略**（CapitulationRebound、NewListingShortHedged）：登记表按类名只能有一项，写成 `"venues": ["okx", "binance"]` 加 `"stages"`（每个阶段的说明），不写单个 `"stage"`。
5. **最小下单额提醒的说法**：报告原来对所有策略都写"排名进了前 K 名 … 名额空到下一个调仓日"，那是 XSMomentum 的说法。现在只对按调仓日排名的截面策略
   （XSMomentum、XSFundingCarry）这样写，PremiumFade、CapitulationRebound、NewListingShortHedged 写"有信号但开不出来，少开了这一笔"。XSMomentum 的旧报告不变。
6. **新币上线日文件的数据起点**（`research.sh`）：原来固定用 BTC 第一根本地日线。可是数据目录里的 BTC 可能是以前从更早开始下的
   （例如运行 A 带 2020 留出段，从 2019-11 起下研究币池），运行 B 才第一次从约 2020-12-19 开始下"研究币池以外"的老币，
   这些老币的第一根K线 = 下载开始日期，按 BTC 的日期算会被当成"2020-12-19 上线的新币"，在 2021-01 被做空——正好落在最关键的"没看过的一段"。
   现在数据起点取"BTC 第一根日K线"和"这次下载的开始日期"里晚的那个；用 `--no-download` 或合成数据时和原来一样。
7. **一周小窗口管道检查**（3.2 节最后一条）原来没有人做：新加 `scripts/round5_week_check.sh`（`make pipeline-week`），
   回测一周、freqtrade 的输出只写进日志文件，屏幕上只打印交易笔数（按 enter_tag）和 funding_fees 不为 0 的笔数，不打印收益。
8. **整合时核对过的 lookahead-analysis 结果**（合成数据，没有改策略）：4 个策略按 `make synthetic-test` 的参数（20 笔）都是 0 偏差；
   加到 100 笔时 PremiumFade（68 笔）、CapitulationRebound（89 笔）、NewListingShortHedged（15 笔）仍然是 0，
   XSFundingCarry 报入场 7 笔、离场 2 笔（没有指标列被标记）。原因是工具截断重跑时白名单只剩一个币，组合热度上限和回撤降仓不起作用；
   只在临时副本里关掉这两项再跑，100 笔全部一致，所以是误报，不是未来函数。
9. **正式运行（`formal_run = true`）的条件**（复查时加严的，规格 3.1 节第 1 条"把规格和代码提交 git、记下 sha256 之后再跑"、3.3 节"每个规格只跑一次"）：
   - 读得到 git 提交号（没装 git、不是 git 仓库 = 不是正式运行）；
   - 这些文件都已提交、工作区没有改动：登记的策略文件、`pinned_files`、登记表、这次的研究配置（连同 `add_config_files` 引入的币池文件）、本文档、
     研究和判定的脚本（`Makefile`、`scripts/research.sh`、`scripts/_common.sh`、`scripts/research_report.py`、`scripts/check_drawdown.py`、`scripts/check_prereg.py`、
     `scripts/build_listings.py`、`scripts/download_okx_data.sh`）；仓库里的策略文件和登记的 sha256 一样；
   - **和登记的不一样就拒绝（退出码 4）**：交易所（`venue` / `venues`）、阶段（按交易所找 `stages`）、研究配置（`config` / `stages."N".config`；第二阶段的配置还没登记 = 拒绝）、
     运行设置（登记表 `run_settings`，见 6.1 节）、research-override（多出没登记的 override 文件也算不一致）；合成数据和 `--data-check-only` 只提醒；
   - 同一个类、同一个阶段没有正式运行过（见下面第 11 条）。
10. **`--data-check-only`（`RESEARCH_DATA_CHECK_ONLY=1`）**：3.2 节"管道检查只看数据"要在正式运行之前单独做，原来只能在正式运行里顺带出现（看到收益之前没法先定"测不了 / 无效"）。
    现在研究脚本可以只下载、做数据检查、生成上线日文件和资金费覆盖的汇总，然后停下（一次回测都不跑，`plan.json` 记为 `data_check_only`、不是正式运行）。
11. **正式运行记录**：正式运行在第一次回测之前记进本机的 `user_data/research/formal_runs.jsonl`（类名、阶段、交易所、结果目录、git 提交号、sha256、时间；不提交 git），
    屏幕上提示把这几行复制进登记表的 `formal_runs` 再提交。之后同一个类、同一个阶段再跑（本机记录或登记表里有）= "重复运行"，不是正式运行；
    `check_prereg.py write --force` 也拒绝改已经正式运行过的类。
12. **一周小窗口管道检查**加了 `--enable-protections`（和研究流程一样，PremiumFade 的冷却才起作用）；NewListingShortHedged 找不到上线日文件时报错退出（退出码 3），不再显示成"这一周没有信号"。

---

## 6. 登记表（`configs/preregistration.json`）

### 6.1 里面有什么

纯 JSON（不要写注释，`check_prereg.py` 只认纯 JSON）。每个策略一项，键是类名：

| 字段 | 意思 | 谁用 |
|---|---|---|
| `file`、`class` | 策略文件、类名 | `check_prereg.py`（找文件、核对类名） |
| `sha256` | 策略文件的 sha256 | `check_prereg.py`（研究前核对，不一致 = 拒绝） |
| `pinned_files` | 一起固定的文件 {路径: sha256}：策略 import 的本地模块（`easytrade_risk.py`、`rust_port_common.py`）和它的 research-override | `check_prereg.py`（同上） |
| `venue` / `venues`、`stages` | 在哪个交易所测；两个阶段的策略写列表和每个阶段的说明 | `check_prereg.py`（和这次的配置对照，不一样 = 拒绝；合成数据只提醒）、报告开头 |
| `config`、`override`、`timeframe` | 用哪个研究配置、research-override、主周期 | `config`：`check_prereg.py`（和这次的 `--config` 对照，不一样 = 拒绝）；其他人看 |
| `params` | 规格第 2 节的 3 个参数（说明用；真正的值在策略文件里，被 sha256 固定） | 人看 |
| `min_pf` | 盈亏因子门槛（默认 1.2） | 报告 |
| `min_pf_pending_decision` | 还没定的决定（说明文字）；有这一项 = 待定 | 报告标"!! 待定"；`check_prereg.py` 正式运行时拒绝 |
| `extra_checks` | 第 2 节每个策略自己"算失败的情况"（见下表） | 报告 |
| `run_settings`（最上层、每个策略、每个阶段） | 冻结的运行设置：手续费 0.0007、回撤上限 15、min_trades 30、pf_ratio 0.6、min_pf 1.2、起始日期、seen_from、走步（6 折、365 / 180 天、rolling）、hyperopt 0、detail、min_train_days、warmup_candles、keep_delisted、pre_holdout、pre_holdout_pairs。策略和阶段里写的覆盖最上层的同名项 | `check_prereg.py`（和这次 `research.sh` 的实际值对照，不一样 = 拒绝） |
| `stages."N".config` | 这个阶段的研究配置（第二阶段在币安服务地区的服务器上定好以后填） | `check_prereg.py`（没有 = 这个阶段拒绝） |

`extra_checks` 的写法（参数都要写全）：

| type | 参数 | 规格出处 | 判法 |
|---|---|---|---|
| `funding_net_positive` | 无 | 2.1 节（XSFundingCarry） | 全期 funding_fees 合计 ≤ 0 = 未通过 |
| `zero_funding_share_invalid` | `max_share`: 0.95 | 2.1 节（XSFundingCarry） | funding_fees 为 0 的交易占比 ≥ 95% = 无效（先修数据） |
| `max_trades_per_pair_year` | `value`: 30 | 2.2 节（PremiumFade） | 平均每币每年交易 > 30 = 未通过 |
| `min_tradable_pairs` | `value`: 20 | 2.2 节（PremiumFade） | 能交易的币 < 20 = 测不了（不算通过） |
| `drop_top_days` | `n`: 3，`segments`: ["unseen", "oos"] | 2.3 节（CapitulationRebound） | 去掉盈利最多的 3 个入场日后，没看过的一段或样本外不赚钱 = 未通过 |
| `count_tags_exclude` | `tags`: ["hedge"] | 2.4 节（NewListingShortHedged） | 30 笔门槛、每币每年交易数不数对冲腿（PF、收益仍用全部交易） |

最上层还有 `formal_runs`（正式运行的记录，从本机 `user_data/research/formal_runs.jsonl` 复制过来，见 6.2 节第 5 条）和 `changes`（6.4 节）。

第 2 节里"第二阶段不通过 = 失败"和"2020 留出段"不在 `extra_checks` 里：第二阶段是另一次运行（另一个结果目录），2020 留出段由研究脚本的 `RESEARCH_PRE_HOLDOUT` 跑、报告单独判。

### 6.2 定稿和提交的顺序（正式运行之前）

1. 4 个策略文件、research-override、新配置都定稿以后，计算 sha256：`make prereg-write`（= `python3 scripts/check_prereg.py write`）。
2. **之后策略文件、`easytrade_risk.py`、`rust_port_common.py`、research-override 只要改了一个字，都要重新运行 `make prereg-write ARGS="--force"`**
   （已经登记过的 sha256 不一致时，不加 `--force` 会拒绝改），否则研究会被预注册检查拒绝（退出码 4）。
   `--force` 只能在正式运行之前用；正式运行之后的改动见 6.4。
3. 把本文档、4 个策略文件、research-override、新配置、登记表一起提交 git，记下 commit。`make prereg-check` 应该显示 4 个"一致"。
4. 正式运行时，研究脚本把每个策略文件的 sha256、一起固定的文件、研究配置的 sha256、运行设置、git 提交号写进结果目录 `plan.json` 的 `prereg`。
   5.7 节第 9 条里的文件有一个没有提交、或者有未提交的改动时，结果记为"不是正式的预注册运行"（`formal_run = false`）。
5. 正式运行在第一次回测之前记进本机的 `user_data/research/formal_runs.jsonl`。跑完以后把屏幕上提示的那几行复制进登记表的 `formal_runs`、提交 git
   （两台服务器各自只有自己的本机记录，登记表里的才是两边都看得到的）。之后同一个类、同一个阶段不能再算正式运行，`--force` 也会拒绝改这个类。

### 6.3 XSFundingCarry 的 min_pf（已定：1.1）

- **2026-11-08 已定**：David 在决定卡上选了"多空对冲策略用 1.1"，当时还没有任何真实数据的回测结果。登记表里 `"min_pf": 1.1`，`min_pf_pending_decision` 已删除，改记 `"min_pf_decision"`（日期和选择）；`"min_pf_options": [1.1, 1.2]`、`"min_pf_spec_default": 1.2` 保留作记录。
- 下面是当时的说明，留作记录：
- 推荐 1.1：这是市场中性、低波动的账户，PF 可能只有 1.1 左右（规格 2.1 节"跑之前要你决定的一点"）。规格的默认是不改规则（1.2）。
- 定下来以后（当时的做法，已照做）：把 `min_pf` 改成你选的值（1.1 或 1.2），**删掉 `min_pf_pending_decision` 这一行**，提交 git，再做正式运行。
  （改 min_pf 不改策略文件，不用重新 `prereg-write`；登记表本身的 sha256 和有没有提交也会记进 plan.json。）
- 还没删掉 `min_pf_pending_decision` 时，正式运行会被拒绝（合成数据照常跑，只提醒）。
- 定的时候只能看规格，不能看任何真实数据的回测结果（规格 3.5 节第 4 条）。

### 6.4 改动记录（`changes`，正式运行之后）

规格 3.1 节第 2 条：正式运行之后，任何改动（参数、条件、周期、币池、成本）都算一个新策略，要换新的类名（例如 XSFundingCarry2），旧结果原样保留，不覆盖。
做法：

1. 新建一个文件 `user_data/strategies/<新类名>.py`（旧文件不动），在登记表 `strategies` 里加新类名的一项；
2. 在 `changes` 里加一条：

   ```json
   {"date": "2026-12-01", "old_class": "XSFundingCarry", "new_class": "XSFundingCarry2",
    "what": "改了什么（参数 / 条件 / 周期 / 币池 / 成本）", "why": "为什么改", "commit": "提交号"}
   ```

3. `make prereg-write ARGS="--strategies XSFundingCarry2"` 填新文件的 sha256，提交 git（旧类名已经正式运行过，`--force` 改不了它的登记，这是故意的）。
4. 新名字的策略在同一段历史上再测，结果只能当参考；要成为独立证据，必须用新数据（例如之后的模拟盘）。

第二阶段（币安）用的配置和币池（例如包括 LUNA、FTT 的交易对列表）不是策略改动，但也要在第二阶段运行之前写进登记表对应策略的 `stages."2".config`、提交 git。

---

## 7. 正式运行之前要你确认的事

1. ~~XSFundingCarry 的 min_pf：1.1 还是 1.2。~~ **已定 1.1**（2026-11-08，在任何真实数据回测之前）。见 6.3 节。
2. **3.3 节的"每一段"是不是真的包括训练段和没看过的一段。** 现在按原文实现：登记的策略每一段都要 PF ≥ 门槛、≥ 30 笔（见 5.6 节第 1 条）。
   如果你要的是"沿用旧规则"（只在样本外和走步查 PF 和笔数），要在正式运行之前说，改 `scripts/research_report.py` 里 `build_rows` 的 `all_segments=rules["registered"]`；
   结果出来以后不能再改。
3. **PremiumFade"平均每个币每年交易超过 30 笔"的分母**：现在按能交易的币（有现货数据、数据条件满足过）算，见 5.6 节第 4 条。
4. **第二阶段**（CapitulationRebound、NewListingShortHedged 的币安确认）的配置和币池还没有做：要等第一阶段通过、在币安服务地区的服务器上核实 data.binance.vision 有哪些下架币的数据之后再定，定了先写进登记表、提交 git，再跑。
5. **实盘之前要在模拟盘核实的事**（第 4 节）：XSFundingCarry 1h 结算的币内存里有完整 7 天（168 条）资金费；PremiumFade 的现货信息K线在期货机器人里能正常刷新；
   各策略单独一个机器人、一个子账户，机器人配置里的 max_open_trades 和 research-override 一样（XSFundingCarry 20、CapitulationRebound 6、NewListingShortHedged 40 加 `position_adjustment_enable`），白名单用研究结果目录的 `pairs.effective.json`。
