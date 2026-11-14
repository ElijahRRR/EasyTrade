# 第 6 轮：100 个候选策略快速初筛

David 2026-11-13 的要求：“为我寻找并优化 100 个策略”。

这里的 100 个是**候选**。前五轮十几个策略几乎全部没通过样本外 / 走步检验，所以这一轮的目标是用同一把尺子，
一次把 100 个公开、有出处的策略筛一遍，**如实报告有几个真正通过**，通过的再改写成 freqtrade 策略，走 `make research` 正式复核。

## 怎么跑（美国 VPS，OKX 数据）

```bash
cd ~/EasyTrade && git pull
make screen100-selftest     # 合成数据自检，约 1 分钟：纯随机行情应当 0 个通过、植入截面动量应当抓到，最后一行"自检通过"
make screen100              # 真实数据：user_data/data/okx 里已经下载的 OKX 永续 1d + 4h（60 个研究币），几分钟
```

- 不联网、不下载：用 `make research` 以前下好的数据。数据不够新可以先 `make download ARGS="--timeframes '4h 1d'"`。
- 资金费：`user_data/data/okx-funding`（运行 C 导入的 2022-03 起 OKX 官方归档）存在就自动用上，没有的时段按 0。
- 结果在 `user_data/research/screen100/run_<时间>/`：`summary.txt`（中文排名表，最后也会打印）、`summary.csv`、
  `results.json`（每个策略每段的全部指标）、`wf_daily_returns.csv`（每个策略走步拼接的日收益，组合检查用）。
  请把整个目录打包发回来（`zip -r screen100.zip user_data/research/screen100/run_<时间>`）。
- 代码和门槛已经锁定（`configs/screen100.json` 的 `locked_sha256`），和锁定版本不一致会拒绝运行（退出码 4）。

## 做法

### 1. 回测方式：向量化初筛，不是 freqtrade 逐笔撮合

100 个策略、233 组参数、各 10 多段，用 freqtrade 逐个跑要几十个小时。初筛用 `scripts/screen100/` 里的向量化引擎
（numpy / pandas，几分钟），约定和 freqtrade 对齐：

- K 线时间 = 开盘时间；第 t 根收盘后算信号，仓位从 t+1 根开始持有。100 个策略都检查过没有未来函数
  （截断数据后重算，截断点之前的仓位一模一样）。
- 手续费 + 滑点每边 0.07%（同 `RESEARCH_FEE`），另外测一次手续费翻倍。
- 资金费：持多付、持空收。
- 回撤含浮亏：每根 K 线多头按最低价、空头按最高价算账户最差值。

**仓位大小（项目风控）**：单笔按波动率定（满仓一笔的日波动贡献 0.4%，约等于“打到止损亏 1%”），
所有仓位的波动贡献加起来 ≤ 2.4%（= 6 个满仓位，对应“总风险约 6%”），总杠杆 ≤ 1 倍，单币 ≤ 25%。
截面策略多空各一半、组内按波动率倒数分配；只做多的轮动策略全部做多。

### 2. “优化” = 只在训练数据里挑参数

每个策略 2 ~ 7 组参数（第一组是文献 / 原作者的默认值），网格在第一次真实运行之前定死（`catalog.py`），不会看了结果再加。

- 主结果：训练段（2021-01 起前 2/3）里挑夏普最高、交易 ≥ 30 笔的那组参数，拿去测样本外（后 1/3）。
- 走步验证：6 折，每折只用自己前 365 天挑参数，测后面没见过的 180 天，6 折拼起来 = 走步结果。
  **走步结果是这一轮最重要的数字**：参数选择完全没用到测试窗口。

### 3. 通过标准（全部满足）

和 `make research` 相同的部分：
- 训练段赚钱、回撤 ≤ 15%；
- 样本外赚钱、盈亏因子 ≥ 1.2（多空对冲的截面 / 因子 / 配对类 ≥ 1.1，沿用运行 C 的决定）、≥ 30 笔、回撤 ≤ 15%、盈亏因子不低于训练段的 0.6 倍；
- 走步拼接赚钱、盈亏因子达标、≥ 30 笔、回撤 ≤ 15%。

这一轮新加的：
- 手续费翻倍，走步仍然赚钱；
- **多重检验**：一次测 100 个策略，就算全都没用，最好的那个看起来也会不错。用 Deflated Sharpe Ratio
  （Bailey & López de Prado 2014）：假设 100 个策略真实夏普都是 0，算出“运气最好的那个”的夏普期望，
  走步日收益的夏普要显著高于它（概率 ≥ 95%）才算通过。约 3 年走步数据时，大约要求走步年化夏普 ≥ 2.4。
  - 宽松口径：策略之间高度相关（例如二十几个趋势策略），按相关矩阵算出的“有效独立次数”再算一次，
    只过宽松口径的标为“边缘”，可以进 freqtrade 复核，但要更怀疑。

结论分 5 种：**通过** / **边缘** / **像运气**（常规门槛都过了、多重检验没过）/ **未通过** / **出错**。

### 4. 合成数据自检（`make screen100-selftest`）

- 纯随机行情（共同市场因子 + 厚尾噪音，没有任何可预测性）：100 个策略应当 0 个通过。
- 植入“过去 28 天强的币继续强”的截面动量：X01 截面动量应当通过，反方向的 X03 短期反转不能通过。
- 开发时另外做过：每个策略截断数据重算对比（没有未来函数）；固定仓位对账（引擎收益 = 手算收益）；
  植入较弱的动量（年化夏普约 2）时严格口径能抓到一部分，更弱的（约 1.2 ~ 1.8）只到“像运气”，说明这道门槛很严。

## 已知局限（看结果时要记住）

- 交易对是现在 OKX 成交额靠前的 60 个币，有幸存者偏差（已经下架的币不在里面），对做多类策略偏乐观。
- 向量化回测和 freqtrade 不完全一样（没有逐笔止损、没有 max_open_trades 排队），所以只做初筛；通过的要正式复核。
- 2021 ~ 2026 的数据前五轮已经看过很多次，这一轮没有“完全没看过的一段”。2020 年留出段只有运行 A 用过，
  通过的策略可以在 freqtrade 复核时再用 `--pre-holdout 20200101-20201231` 看一次。
- 部分策略和前几轮测过的同一类（表里“前几轮测过的同类”一列），它们的结果不算新证据。

## 100 个候选策略

来源说明：101 Alphas = Kakushadze 2015《101 Formulaic Alphas》（arXiv:1601.00991），原文是美股日线，
这里 delta(close) 换成收益率、vwap 用 (高+低+收)/3 近似；加密因子主要来自 Liu, Tsyvinski, Wu 2022
《Common Risk Factors in Cryptocurrency》；经典技术策略参考了 freqtrade-strategies 和 je-suis-tm/quant-trading 里的同名策略，
用同一套规则重新实现（不直接跑第三方代码）。

| 编号 | 名称 | 类别 | 周期 | 来源 | 参数网格（第一组 = 默认） | 前几轮测过的同类 |
|---|---|---|---|---|---|---|
| T01 | 均线交叉（SMA 快慢线） | 趋势 | 1d | 经典；freqtrade-strategies 多个示例策略 | f=20, sl=100；f=10, sl=50；f=50, sl=200 |  |
| T02 | 指数均线交叉（EMA） | 趋势 | 1d | 经典 EMA crossover | f=12, sl=26；f=20, sl=60；f=30, sl=120 |  |
| T03 | 价格在均线上做多、下做空 | 趋势 | 1d | Faber 2007 时间序列择时（单均线） | n=100；n=50；n=200 |  |
| T04 | 时间序列动量（过去 N 天涨跌） | 趋势 | 1d | Moskowitz, Ooi, Pedersen 2012 | n=90；n=30；n=180 | TsmomTrend（第 2、3 轮） |
| T05 | 多周期动量合成 | 趋势 | 1d | Hurst, Ooi, Pedersen 2017《A Century of Evidence on Trend-Following》 | ns=(20, 60, 120, 250)；ns=(10, 30, 90) |  |
| T06 | 唐奇安通道突破（海龟） | 趋势 | 1d | Dennis/Faith《Way of the Turtle》 | e=55, x=20；e=20, x=10；e=100, x=50 | DonchianTrend（第 2、3 轮） |
| T07 | 肯特纳通道突破 | 趋势 | 1d | Chester Keltner / Linda Raschke 版（EMA ± ATR 倍数） | n=20, k=2.0；n=50, k=2.0；n=20, k=3.0 |  |
| T08 | 布林带突破 | 趋势 | 1d | John Bollinger；突破上轨做多、回到中轨平仓 | n=20, k=2.0；n=50, k=2.0 |  |
| T09 | MACD 信号线交叉 | 趋势 | 1d | Gerald Appel | f=12, sl=26, g=9；f=24, sl=52, g=18 |  |
| T10 | MACD 零轴 | 趋势 | 1d | Gerald Appel（MACD 线在零轴上方做多） | f=12, sl=26；f=24, sl=52 |  |
| T11 | Supertrend | 趋势 | 1d | Olivier Seban；freqtrade-strategies Supertrend | n=10, k=3.0；n=20, k=3.0；n=10, k=4.0 |  |
| T12 | 抛物线 SAR | 趋势 | 1d | Welles Wilder 1978；je-suis-tm/quant-trading Parabolic SAR | af=0.02, mx=0.2；af=0.01, mx=0.1 |  |
| T13 | ADX 过滤的 DI 交叉 | 趋势 | 1d | Welles Wilder 1978（DMI/ADX） | n=14, th=20；n=14, th=25；n=28, th=20 |  |
| T14 | Aroon 振荡器 | 趋势 | 1d | Tushar Chande 1995 | n=25；n=50 |  |
| T15 | 一目均衡表（价格在云上 + 转换线>基准线） | 趋势 | 1d | 细田悟一；freqtrade-strategies Ichimoku 类 | a=9, b=26, c=52；a=20, b=60, c=120 |  |
| T16 | 平均K线（Heikin-Ashi）连续同色 | 趋势 | 1d | je-suis-tm/quant-trading Heikin-Ashi | k=2；k=3 |  |
| T17 | KAMA 自适应均线方向 | 趋势 | 1d | Perry Kaufman《Trading Systems and Methods》 | n=10；n=20 |  |
| T18 | Hull 均线斜率 | 趋势 | 1d | Alan Hull 2005 | n=55；n=20；n=100 |  |
| T19 | TRIX | 趋势 | 1d | Jack Hutson | n=15；n=30 |  |
| T20 | 回归斜率 t 值（趋势强度） | 趋势 | 1d | 统计趋势检验；类似 Clenow《Stocks on the Move》的回归动量 | n=60, th=2.0；n=30, th=2.0；n=90, th=2.0 |  |
| T21 | Dual Thrust 区间突破（日线版） | 趋势 | 1d | Michael Chalek；je-suis-tm/quant-trading Dual Thrust | n=4, k=0.5；n=4, k=0.7；n=10, k=0.5 |  |
| T22 | Vortex 指标 | 趋势 | 1d | Botes & Siepman 2010 | n=14；n=28 |  |
| T23 | Coppock 曲线（只做多） | 趋势 | 1d | Edwin Coppock 1962（月线指标，按日线等比例换算） | a=14, b=11, w=10；a=28, b=22, w=20 |  |
| T24 | Elder 动力系统 | 趋势 | 1d | Alexander Elder《Come Into My Trading Room》 | n=13；n=26 |  |
| T25 | 低波动时才跟趋势 | 趋势 | 1d | 波动率择时 + 动量（Moreira & Muir 2017 的思路） | n=90, vn=30；n=60, vn=30 |  |
| T26 | 只做多：价格在长均线之上 | 趋势 | 1d | Faber 2007（只做多版本） | n=200；n=100 |  |
| T27 | 只做多：金叉 | 趋势 | 1d | 经典 50/200 金叉 | f=50, sl=200；f=20, sl=100 |  |
| T28 | Larry Williams 波动突破（持有 1 天） | 趋势 | 1d | Larry Williams《Long-Term Secrets to Short-Term Trading》 | k=0.5；k=0.7 |  |
| T29 | N 日新高突破 + ATR 追踪止损 | 趋势 | 1d | Chuck LeBeau 吊灯止损（Chandelier Exit） | n=50, k=3.0；n=20, k=3.0 |  |
| T30 | 区间位置趋势（随机指标高位做多） | 趋势 | 1d | George Lane 随机指标的趋势用法 | n=20；n=50 |  |
| T31 | 4 小时 EMA 交叉 | 趋势 | 4h | 经典 EMA crossover（4h） | f=20, sl=60；f=50, sl=200 |  |
| T32 | 4 小时唐奇安突破 | 趋势 | 4h | 海龟规则（4h） | e=120, x=60；e=60, x=30 | DonchianTrend（日线，第 2、3 轮） |
| M01 | Connors RSI(2)（200 日均线过滤） | 回归 | 1d | Larry Connors《Short Term Trading Strategies That Work》 | th=10；th=5；th=15 |  |
| M02 | RSI(14) 超买超卖 | 回归 | 1d | Welles Wilder 1978 | lo=30, hi=70；lo=25, hi=75；lo=20, hi=80 |  |
| M03 | 布林带回归 | 回归 | 1d | John Bollinger；je-suis-tm/quant-trading Bollinger Bands Pattern Recognition 的简化版 | n=20, k=2.0；n=20, k=2.5；n=50, k=2.0 |  |
| M04 | Z 分数回归 | 回归 | 1d | 统计均值回归（Chan《Algorithmic Trading》） | n=20；n=10；n=40 |  |
| M05 | 威廉 %R | 回归 | 1d | Larry Williams | n=14, lo=-90, hi=-10；n=14, lo=-95, hi=-5 |  |
| M06 | 随机指标 KD 超买超卖 | 回归 | 1d | George Lane | n=14, lo=20, hi=80；n=14, lo=10, hi=90 |  |
| M07 | CCI 回归 | 回归 | 1d | Donald Lambert 1980 | n=20, th=100；n=20, th=200 |  |
| M08 | IBS 收盘位置（持有 1 天） | 回归 | 1d | Pagonidis《The IBS Effect》 | lo=0.2, hi=0.8；lo=0.1, hi=0.9 |  |
| M09 | 连续 N 天下跌后买（持有 3 天） | 回归 | 1d | Connors 连跌规则 | k=3；k=4；k=5 |  |
| M10 | 累积 RSI(2) | 回归 | 1d | Connors & Alvarez《High Probability ETF Trading》 | th=35；th=25 |  |
| M11 | MFI 资金流量 | 回归 | 1d | Quong & Soudack | lo=20, hi=80；lo=10, hi=90 |  |
| M12 | 偏离均线太远就反向 | 回归 | 1d | 均线乖离率 | n=20, x=0.15；n=20, x=0.1；n=50, x=0.2 |  |
| M13 | 肯特纳通道回归 | 回归 | 1d | Keltner 通道的反向用法 | n=20, k=2.0；n=20, k=3.0 |  |
| M14 | ConnorsRSI 复合指标 | 回归 | 1d | Connors Research 2012（RSI3 + 连涨跌 RSI + 涨幅百分位） | th=10；th=5 |  |
| M15 | 单日暴跌后反弹（N 倍波动） | 回归 | 1d | 极端收益反转（短线） | k=2.0, h=3；k=3.0, h=3；k=2.0, h=1 |  |
| M16 | 上升趋势里买回调（只做多） | 回归 | 1d | 趋势过滤 + 布林 %B 回调 | t=100, n=20；t=50, n=20 |  |
| M17 | 4 小时 RSI(2) 回调买（日线上升趋势） | 回归 | 4h | Connors RSI(2) 的 4h 版 | th=10；th=5 |  |
| M18 | 4 小时布林带回归 | 回归 | 4h | 布林带回归（4h） | n=20, k=2.0；n=40, k=2.5 |  |
| X01 | 截面动量（每周调仓） | 截面 | 1d | Liu, Tsyvinski, Wu 2022《Common Risk Factors in Cryptocurrency》 | n=28；n=14；n=56 | XSMomentum（第 3 轮） |
| X02 | 截面动量跳过最近一段 | 截面 | 1d | Jegadeesh & Titman 1993（12-1 月动量的加密版） | n=28, k=7；n=90, k=7；n=180, k=30 |  |
| X03 | 截面短期反转（买最近跌得多的） | 截面 | 1d | Lehmann 1990；加密短期反转 | n=7, reb=7；n=3, reb=3；n=1, reb=1 |  |
| X04 | 低波动（多低波、空高波） | 截面 | 1d | Ang et al. 2006；低波动异象 | n=30；n=90 |  |
| X05 | 彩票效应 MAX（空最近暴涨过的） | 截面 | 1d | Bali, Cakici, Whitelaw 2011；Li et al. 2021 加密 MAX | n=30；n=60 |  |
| X06 | 成交额增长 | 截面 | 1d | Liu & Tsyvinski 2021《Risks and Returns of Cryptocurrency》成交量因子 | sign=1；sign=-1 |  |
| X07 | 规模（成交额大小） | 截面 | 1d | Liu, Tsyvinski, Wu 2022 规模因子（用成交额代替市值） | sign=-1；sign=1 |  |
| X08 | Amihud 非流动性 | 截面 | 1d | Amihud 2002 | n=30；n=90 |  |
| X09 | 低贝塔（BAB，相对 BTC） | 截面 | 1d | Frazzini & Pedersen 2014《Betting Against Beta》 | n=60；n=120 |  |
| X10 | 低特质波动（剔除 BTC 后） | 截面 | 1d | Ang et al. 2006 特质波动率 | n=30；n=90 |  |
| X11 | 偏度（多负偏、空正偏） | 截面 | 1d | Boyer, Mitton, Vorkink 2010 预期偏度 | n=30；n=90 |  |
| X12 | 接近 N 日新高 | 截面 | 1d | George & Hwang 2004（52 周新高效应） | n=180；n=90；n=365 |  |
| X13 | 残差动量（剔除 BTC 贝塔） | 截面 | 1d | Blitz, Huij, Martens 2011 | n=28；n=56 |  |
| X14 | 夏普动量（涨幅 / 波动） | 截面 | 1d | 风险调整动量（Rachev et al. 2007） | n=28；n=90 |  |
| X15 | 量价相关（多负相关的） | 截面 | 1d | 量价背离（101 Alphas 里多个因子的共同成分） | n=30；n=14 |  |
| X16 | 低下行贝塔 | 截面 | 1d | Ang, Chen, Xing 2006 下行风险 | n=90；n=180 |  |
| X17 | 截面 RSI 排名 | 截面 | 1d | RSI 用作截面强弱（动量 / 反转两种方向） | sign=1；sign=-1 |  |
| X18 | 放量涨的继续涨（量价动量） | 截面 | 1d | Gervais, Kaniel, Mingelgrin 2001 高成交量溢价 | n=7；n=14 |  |
| X19 | 与 BTC 协偏度 | 截面 | 1d | Harvey & Siddique 2000 协偏度 | n=90；n=180 |  |
| X20 | 截面动量 + BTC 趋势过滤 | 截面 | 1d | 动量崩溃保护（Daniel & Moskowitz 2016 的思路） | n=28, t=100；n=56, t=100 | XSMomentum（第 3 轮，不带过滤） |
| X21 | 只做多：最强币轮动 + BTC 趋势过滤 | 截面 | 1d | Antonacci《Dual Momentum》的加密版 | n=28, q=0.1；n=56, q=0.1；n=28, q=0.2 |  |
| X22 | 只做多：低波动篮子 | 截面 | 1d | 低波动异象（只做多） | n=30, filt=1；n=90, filt=1；n=30, filt=0 |  |
| X23 | 相对 BTC 强弱（币/BTC 比值在均线上方） | 截面 | 1d | 相对强弱轮动 | n=20；n=50 |  |
| X24 | 截面趋势质量（回归 t 值） | 截面 | 1d | 趋势平滑度动量（Clenow 回归动量的截面版） | n=90；n=30 |  |
| X25 | 4 小时截面短期反转 | 截面 | 4h | 加密日内反转 | n=6, reb=6；n=18, reb=6 |  |
| A001 | Alpha#1 | 因子 | 1d | Kakushadze 2015《101 Formulaic Alphas》 | reb=1；reb=5 |  |
| A002 | Alpha#2 | 因子 | 1d | Kakushadze 2015《101 Formulaic Alphas》 | reb=1；reb=5 |  |
| A003 | Alpha#3 | 因子 | 1d | Kakushadze 2015《101 Formulaic Alphas》 | reb=1；reb=5 |  |
| A004 | Alpha#4 | 因子 | 1d | Kakushadze 2015《101 Formulaic Alphas》 | reb=1；reb=5 |  |
| A006 | Alpha#6 | 因子 | 1d | Kakushadze 2015《101 Formulaic Alphas》 | reb=1；reb=5 |  |
| A009 | Alpha#9 | 因子 | 1d | Kakushadze 2015《101 Formulaic Alphas》 | reb=1；reb=5 |  |
| A012 | Alpha#12 | 因子 | 1d | Kakushadze 2015《101 Formulaic Alphas》 | reb=1；reb=5 |  |
| A013 | Alpha#13 | 因子 | 1d | Kakushadze 2015《101 Formulaic Alphas》 | reb=1；reb=5 |  |
| A016 | Alpha#16 | 因子 | 1d | Kakushadze 2015《101 Formulaic Alphas》 | reb=1；reb=5 |  |
| A033 | Alpha#33 | 因子 | 1d | Kakushadze 2015《101 Formulaic Alphas》 | reb=1；reb=5 |  |
| A034 | Alpha#34 | 因子 | 1d | Kakushadze 2015《101 Formulaic Alphas》 | reb=1；reb=5 |  |
| A038 | Alpha#38 | 因子 | 1d | Kakushadze 2015《101 Formulaic Alphas》 | reb=1；reb=5 |  |
| A041 | Alpha#41 | 因子 | 1d | Kakushadze 2015《101 Formulaic Alphas》 | reb=1；reb=5 |  |
| A053 | Alpha#53 | 因子 | 1d | Kakushadze 2015《101 Formulaic Alphas》 | reb=1；reb=5 |  |
| A101 | Alpha#101 | 因子 | 1d | Kakushadze 2015《101 Formulaic Alphas》 | reb=1；reb=5 |  |
| C01 | 星期效应（训练段里挑一天做多 BTC） | 市场 | 1d | 加密星期效应（Caporale & Plastun 2019） | d=0；d=1；d=2；d=3；d=4；d=5；d=6 |  |
| C02 | 月末月初做多 BTC | 市场 | 1d | 月初效应（Ariel 1987；加密版） | a=2, b=3；a=1, b=1 |  |
| C03 | BTC 低波动时持有 | 市场 | 1d | 波动率择时（Moreira & Muir 2017） | n=30；n=14 |  |
| C04 | ETH/BTC 比值回归 | 市场 | 1d | 配对交易（Gatev, Goetzmann, Rouwenhorst 2006） | n=20；n=60 |  |
| C05 | 各币相对 BTC 的价差回归 | 市场 | 1d | 统计套利（Avellaneda & Lee 2010 的简化版） | n=20；n=60 |  |
| C06 | 山寨季轮动（山寨强就拿山寨篮子，否则拿 BTC） | 市场 | 1d | BTC 主导率轮动 | n=30；n=60 |  |
| C07 | 市场宽度（多数币在均线上方就做多篮子） | 市场 | 1d | 市场宽度指标（Zweig breadth） | n=50, up=0.6, dn=0.4；n=20, up=0.7, dn=0.3 |  |
| C08 | BTC 上涨趋势时做多高贝塔币 | 市场 | 1d | 贝塔择时 | t=100；t=50 |  |
| C09 | 等权指数的时间序列动量（整个篮子一起多空） | 市场 | 1d | 指数级 TSMOM | n=30；n=90 |  |
| C10 | 4 小时时段效应（训练段里挑一个时段做多 BTC） | 市场 | 4h | 加密日内季节性（Eross et al. 2019） | slot=0；slot=1；slot=2；slot=3；slot=4；slot=5 |  |

