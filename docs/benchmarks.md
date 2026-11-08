# 基准策略：和别人公开的策略比一比（第 3 轮）

> 一句话：把两个最有名的公开策略库里的策略，放进**和我们自己的策略完全相同**的研究流程里跑一遍
> （同样的数据、手续费、训练段/样本外/走步切分、**账户含浮亏回撤 ≤ 15%**、杠杆 ≤ 5 倍），
> 看看它们在这把"尺子"下表现如何。它们只是**参照物**，不是要拿去实盘的策略。

David 问过"有没有参考公开的策略库"——前两轮没有。这一轮补上：

- **freqtrade-strategies**（freqtrade 官方的策略示例库）里 `futures/`（合约）目录的策略；
- **NostalgiaForInfinity**（NFI，社区里最有名、最复杂的 freqtrade 策略）当前的主策略 **X8**，固定在一个版本。

怎么用（在你自己的 VPS 上，建议在 tmux 里）：

```bash
git pull                              # 拿到本仓库的最新脚本
bash scripts/fetch_benchmarks.sh      # 下载固定版本的基准策略，逐个核对 sha256（几秒钟）
make research-benchmarks              # 跑基准策略研究（两组，加起来可能要大半天，见第 6 节）
```

还没下载就运行 `make research-benchmarks`，它只会打印上面这几步提示，什么都不跑。

---

## 1. 有哪些基准策略

| 策略（类名） | 来自 | 周期 | 方向 | 杠杆 | 在哪一组 | 简单说 |
|---|---|---|---|---|---|---|
| `FAdxSmaStrategy` | freqtrade-strategies | 1h | 多空 | 1 倍 | 1h 组 | ADX > 30 时 12/48 均线交叉开仓，ADX 回落平仓；止损 -5%，有止盈表 |
| `FSampleStrategy` | freqtrade-strategies | 1h | 多空 | 1 倍 | 1h 组 | 官方示例模板：RSI 上穿 30/70 + TEMA + 布林带；止损 -5% |
| `FSupertrendStrategy` | freqtrade-strategies | 1h | 只做多 | 1 倍 | 1h 组 | 三条 Supertrend 同向开仓；止损 -26.5%，追踪止盈 |
| `VolatilitySystem` | freqtrade-strategies | 1h | 多空 | 固定 2 倍 | 1h 组 | 3 小时 ATR 波动突破，反向信号平仓；**没有止损**（-100%）；先用一半资金开仓，新信号再**加仓**一次 |
| `FReinforcedStrategy` | freqtrade-strategies | 5m | 多空 | 1 倍 | 5m 组 | 8/21 EMA 交叉 + 1 小时 SMA50 过滤，ADX 回落平仓；止损 -5% |
| `TrendFollowingStrategy` | freqtrade-strategies | 5m | 只做多 | 1 倍 | 5m 组 | 收盘上穿 EMA20 且 OBV 上升；止损 -26.5%，追踪止盈 |
| `NostalgiaForInfinityX8Capped` | NFI X8（v18.0.214）+ 本项目的封顶子类 | 5m（另看 15m/1h/4h/1d 和 BTC） | 多空 | 默认 3 倍（封顶 5 倍） | 5m 组 | 几十种入场条件，加仓/分批平仓（rebuy、grinding、derisk）；名义止损 -99%，靠内部规则止损 |
| `FOttStrategy` | freqtrade-strategies | 1h | 只做多 | 1 倍 | **默认不跑** | 下载了，但在我们的 freqtrade 2026.10 下用不了，原因见下面 |

说明：

- "只做多"：`FOttStrategy`、`FSupertrendStrategy`、`TrendFollowingStrategy` 虽然放在 `futures/` 目录、代码里也算了做空信号，
  但没有写 `can_short = True`，freqtrade 不会替它们做空。我们按原样测，不替它们改。
- freqtrade-strategies 的 `futures/` 目录（固定的那个版本）一共就这 7 个 `.py`，**没有"只负责下单"的辅助文件**，
  全都是有自己买卖信号的策略；`FSampleStrategy` 是官方的示例模板，也照样测。
- **`FOttStrategy` 为什么默认不跑**（读它的源码发现的，三个问题都在它自己的代码里）：
  1. 它写了 `from numpy.lib import math`，numpy 2 已经没有这个东西了（freqtrade 2026.10 用 numpy 2.5），**一导入就报错**；
  2. 它用 `df["Var"].iat[i] = ...` 这种"链式赋值"算指标，pandas 3（freqtrade 2026.10 用 pandas 3.1）里这样写**不再改动原表**，
     指标会一直是 0，就算能导入，测出来的也不是它本来的策略；
  3. 它每根K线都把整列重算一遍（平方级），几年的 1h 数据要跑一天以上。
  所以下载了（留个固定版本的记录），但不放进默认的 1h 组。想硬跑：`RESEARCH_BENCH_1H_STRATEGIES="FOttStrategy" make research-benchmarks`（会报错）。
- NFI 要的数据最多：5m 主周期，另外每个币还要 15m、1h、4h、1d，再加 BTC 的 5m 和 4h（BTC 一定放在交易对里）。

## 2. 固定版本、来源、许可证

所有文件都从 GitHub 按**固定的 commit** 下载（不跟着 main 分支变），下载后逐个核对 sha256，对不上就整批作废。

| 仓库 | 固定 commit（2026-11-07 的 main） | 许可证 |
|---|---|---|
| https://github.com/freqtrade/freqtrade-strategies | `f3340ce11f5bdf62f598522e64d1f5638eaa13f5` | GPL-3.0 |
| https://github.com/iterativv/NostalgiaForInfinity | `74c03087b8ced3899509ba8d36f5f1565bc5f49a`（策略里写的版本 v18.0.214） | GPL-3.0 |

| 本地文件（`user_data/strategies/vendor/`） | 上游文件 | 上游原文 sha256 |
|---|---|---|
| `FAdxSmaStrategy.py` | [user_data/strategies/futures/FAdxSmaStrategy.py](https://github.com/freqtrade/freqtrade-strategies/blob/f3340ce11f5bdf62f598522e64d1f5638eaa13f5/user_data/strategies/futures/FAdxSmaStrategy.py) | `43ae02ecca4553b8a609d4533cb4d5b6517292d35b41fc4c77f54f8e959ced89` |
| `FOttStrategy.py` | [user_data/strategies/futures/FOttStrategy.py](https://github.com/freqtrade/freqtrade-strategies/blob/f3340ce11f5bdf62f598522e64d1f5638eaa13f5/user_data/strategies/futures/FOttStrategy.py) | `6e7dd4802915f6952c9f7be6b5fe9840d1c894a89476e1b274fb163b096d930e` |
| `FReinforcedStrategy.py` | [user_data/strategies/futures/FReinforcedStrategy.py](https://github.com/freqtrade/freqtrade-strategies/blob/f3340ce11f5bdf62f598522e64d1f5638eaa13f5/user_data/strategies/futures/FReinforcedStrategy.py) | `4148d3d2d736a454a470649646a59b77a62f7d4ccb7f7bc6d2ecc5ad0ec01152` |
| `FSampleStrategy.py` | [user_data/strategies/futures/FSampleStrategy.py](https://github.com/freqtrade/freqtrade-strategies/blob/f3340ce11f5bdf62f598522e64d1f5638eaa13f5/user_data/strategies/futures/FSampleStrategy.py) | `3fcbe2ed16dd097fd6e4548172acbde871bdfe64c5a7341f337f19caf3daa225` |
| `FSupertrendStrategy.py` | [user_data/strategies/futures/FSupertrendStrategy.py](https://github.com/freqtrade/freqtrade-strategies/blob/f3340ce11f5bdf62f598522e64d1f5638eaa13f5/user_data/strategies/futures/FSupertrendStrategy.py) | `93f31258c4b6c05cf2380fc81f235f37355d97a3e4d2ebdf075e948e07978e19` |
| `TrendFollowingStrategy.py` | [user_data/strategies/futures/TrendFollowingStrategy.py](https://github.com/freqtrade/freqtrade-strategies/blob/f3340ce11f5bdf62f598522e64d1f5638eaa13f5/user_data/strategies/futures/TrendFollowingStrategy.py) | `6b8f3e272acfd4923539120364b9ddceccc17a385117d12a249dc1460ea53c4f` |
| `VolatilitySystem.py` | [user_data/strategies/futures/VolatilitySystem.py](https://github.com/freqtrade/freqtrade-strategies/blob/f3340ce11f5bdf62f598522e64d1f5638eaa13f5/user_data/strategies/futures/VolatilitySystem.py) | `10a32ea131fb8f4684741b48cd3eae70aa4d4990319b49a7acb7a2f9c7c8a672` |
| `LICENSE-freqtrade-strategies.txt` | [LICENSE](https://github.com/freqtrade/freqtrade-strategies/blob/f3340ce11f5bdf62f598522e64d1f5638eaa13f5/LICENSE) | `8ceb4b9ee5adedde47b31e975c1d90c73ad27b6b165a1dcd80c7c545eb65b903` |
| `NostalgiaForInfinityX8.py` | [NostalgiaForInfinityX8.py](https://github.com/iterativv/NostalgiaForInfinity/blob/74c03087b8ced3899509ba8d36f5f1565bc5f49a/NostalgiaForInfinityX8.py)（约 3.3 MB、6.5 万行） | `839b1aa28be14317291d51b1173a075317b2f8a6158d4d3419aa47927e4f1982` |
| `LICENSE-NostalgiaForInfinity.txt` | [LICENSE](https://github.com/iterativv/NostalgiaForInfinity/blob/74c03087b8ced3899509ba8d36f5f1565bc5f49a/LICENSE) | `3972dc9744f6499f0f9b2dbf76696f2ae7ad8af9b23dde66d6af86c9dfb36986` |

- 同样的信息也写在每个下载下来的 `.py` 文件开头（几行 `# [easytrade-vendor] ...` 注释：上游仓库、文件、commit、下载地址、sha256、许可证），
  正文一个字不改。`bash scripts/fetch_benchmarks.sh --list` 也能列出来。
- NFI 只 import freqtrade、numpy、pandas、talib、rapidjson 和 Python 标准库，**没有别的辅助文件要下载**。
- **许可证（GPL-3.0）**：两个仓库都是 GPL-3.0。你自己下载、回测、自己用，没有问题。
  但如果把这些文件（或者改过的版本、或者把它们拷进去的程序）**发给别人**，就必须按 GPL-3.0 同时提供完整源代码、保留许可证。
  所以它们放在 `user_data/strategies/vendor/`，**`.gitignore` 已忽略，不进我们的 git 仓库**；我们仓库里只有下载脚本、
  一个封顶杠杆的小子类（我们自己写的）和这份说明。
- 不要手改 `vendor/` 里的文件：改了以后 `make research-benchmarks` 校验不通过，会拒绝运行。
  检查：`bash scripts/fetch_benchmarks.sh --check`；恢复成固定版本：`bash scripts/fetch_benchmarks.sh --force`（被改过的先备份成 `.bak`）。

## 3. NFI 为什么不适合直接拿去实盘

NFI 名气很大，但它的设计和我们的风控规则差得很远。这里只把它当"参照物"测一测：

1. **加仓摊平（DCA）**：它会在亏损时继续加仓（rebuy、grinding 等模式），再分批平仓。仓位越亏越大，
   单个币的风险可能远超开仓时的那一份；行情一路单边时，账户回撤会突然变得很大。
2. **止损很宽**：名义止损是 -99%（等于不设），靠代码里一套自己的止损规则兜底（`stop_threshold_*`、"doom" 止损等，
   阈值大多在 0.10 ~ 0.20、按第一笔开仓算，加仓的部分另有规则）；默认 3 倍杠杆，逐仓时离强平也不远。
   我们的规则是每笔开仓前就按止损距离算好风险、账户回撤 ≤ 15%。
3. **上游改得非常频繁**：作者几乎每天都提交新版本（我们固定的是 v18.0.214，这时最新的正式发布标签是 v18.0.116，中间就隔了约一百个小版本）。
   它自带两个**自动更新**工具：Docker 里的 `nfi-updater` 服务（每天自动换成最新策略、黑名单，然后重启机器人）
   和 `tools/checkupdates.sh`（配 cron 定时更新）。这意味着**实盘机器人的逻辑会在没人看过的情况下变掉**。
   我们只下载一个固定版本的 `.py` 文件，**绝不下载这两个更新工具**（下载脚本里也有防呆检查）。
4. **代码太大，没法审查**：一个文件 6.5 万行。我们自己的策略每一行都能讲清楚；它出了问题，没人说得清是哪条规则造成的。
5. **结果偏乐观**：作者一直用最近的行情调它（每个提交下面都贴回测结果），所以"最近 2 年"对 NFI 来说差不多就是它的训练数据，
   我们测出来的样本外不是真正"没见过"的。
6. **很吃资源**：它的回测要几百列指标、多个周期。NFI 自己的文档说完整回测要 96 GB 内存、跑好几天，所以我们只测最近约 2 年、少一些币（第 6 节）。

## 4. NFI 要求的设置（我们怎么照办的）

来源：NFI 的 README「General Recommendations」、策略文件开头的说明、`configs/exampleconfig.json`（都在上面那个固定 commit，当作文本读的）。
我们的写法在 `configs/research-overrides/NostalgiaForInfinityX8Capped.json`（研究脚本回测它时自动叠加，只对它生效）。

| NFI 的要求 / 推荐 | 我们怎么做 |
|---|---|
| timeframe 必须 5m，不要在配置里改 | 研究配置和覆盖文件都不写 timeframe（用策略自己的 5m） |
| `use_exit_signal` true、`exit_profit_only` false、`ignore_roi_if_entry_signal` true（或不写） | 研究配置本来就没写；覆盖文件里再明确写成这三个值 |
| 同时 6 ~ 12 个仓位，stake 用 unlimited | `max_open_trades` 6（它的 exampleconfig 也是 6）；stake 研究配置里本来就是 unlimited |
| 交易对 40 ~ 80 个，按成交量的动态列表 | **没照办**：用研究列表的前 N 个固定币（默认按内存自动定，最多 20 个；BTC 一定在里面）。原因：内存（第 6 节） |
| 黑名单：杠杆代币、稳定币等 | OKX 永续本来就没有杠杆代币，研究列表也不含稳定币；它的 OKX 黑名单（`configs/blacklist-okx.json`）不包含前 20 个币。用 `--nfi-pairs all` 时会包含 BLUR、STX（它黑名单里有），要注意 |
| 合约 + 逐仓 | 研究配置就是 futures + isolated |
| 杠杆（它默认 3 倍，可以用配置改） | 用默认 3 倍；我们的子类 `NostalgiaForInfinityX8Capped` 封顶 5 倍（见下） |
| 限价单超时 3 / 2 分钟 | 覆盖文件里照写（回测里基本用不到） |
| 例子里初始资金 10000、tradable_balance_ratio 0.99 | **没照搬**：和我们的策略一样用 1000 USDT、0.95，方便对比 |

**杠杆封顶**（`user_data/strategies/benchmarks/BenchmarkCaps.py`，我们自己写的小文件）：
NFI 的 `leverage()` 默认 3 倍，但允许用配置（`futures_mode_leverage` 等）改大，理论上可以超过 5 倍。
`NostalgiaForInfinityX8Capped` 继承 NFI 原类（不复制、不修改它的代码），只做两件事：
`leverage()` 结果超过 5 倍就改成 5 倍；配置里改大的杠杆设置超过 5 也改成 5。
freqtrade-strategies 的策略杠杆最多 2 倍（只有 `VolatilitySystem` 写了固定 2 倍，其余用 freqtrade 默认的 1 倍），不需要封顶。
研究报告还会再查每笔交易的实际杠杆，超过 5 倍直接判不通过。

**回测时的一个修正**：NFI 把"每个币的止盈目标"存在 `user_data/` 下面一个文件里，下次启动会读回来——
上一段（例如训练段）最后留下的目标会影响下一段（样本外）的离场，各段就不独立了、结果也没法复现。
所以子类在**回测时**让它每次从一个新的空文件开始（模拟盘/实盘时不动它的行为）。
这个修正是照着固定版本的源码写的（当文本读的，开发机上没有运行过 NFI）：如果回测日志里出现"没能换成空的临时文件"的警告（上游改了写法），每段回测之间手动删掉 `user_data/nfix8-profit_max-*-(backtest).json`（正常时没有这条警告）。

**这个子类只在 `make research-benchmarks` 时用**：它放在 `user_data/strategies/benchmarks/` 子目录，
平时的 `make backtest`、`make research` 根本扫不到它；`vendor/` 里没有 NFI 文件时它导入就报错（中文提示先下载），不影响别的策略。

## 5. 怎么运行

```bash
tmux new -s bench                      # 断线也不会中断；回来用 tmux attach -t bench
bash scripts/fetch_benchmarks.sh       # 第一次，或者换版本以后
make research-benchmarks               # 两组都跑
```

常用变化：

```bash
make research-benchmarks ARGS="--groups 1h"        # 只跑 1h 组（快，先看这个）
make research-benchmarks ARGS="--groups 5m"        # 只跑 5 分钟组（含 NFI，很慢）
make research-benchmarks ARGS="--no-download"      # 不更新数据
make research-benchmarks ARGS="--nfi-pairs 30"     # 5 分钟组用前 30 个币（内存够才行）
make research-benchmarks ARGS="--days 365"         # 5 分钟组只用最近 1 年（更快、更省内存）
make research-benchmarks ARGS="--with-ours"        # 5 分钟组里也放上我们自己的策略，同一张表对比
MAX_DD_PCT=12 make research-benchmarks             # 换回撤上限（默认 15%，和 make research 相同）
```

它做的事（`scripts/research_benchmarks.sh`）：

1. 先核对 `vendor/` 里的文件是不是固定版本（不联网、不运行它们）；没下载就打印提示退出，被改过就拒绝运行。
2. **1h 组**：`FAdxSmaStrategy FSampleStrategy FSupertrendStrategy VolatilitySystem`，交给 `scripts/research.sh`，
   区间（默认 2021-01-01 起）、交易对（约 60 个）、手续费、切分、通过标准**和 `make research` 完全一样**。
3. **5m 组**：`FReinforcedStrategy TrendFollowingStrategy NostalgiaForInfinityX8Capped`，只用**最近约 2 年**（`--days`，默认 730 天，
   开始日期自动算 = 今天往前 730 天，结束 = 数据的最后一天），交易对 = 研究列表的前 N 个（BTC 一定在里面），
   周期 5m 15m 1h 4h 1d。交易对配置自动写在 `user_data/research/benchmarks/config.bench-5m.json`。
4. 两组都**只用默认参数**（不调参，`RESEARCH_HYPEROPT=1` 也不管用）。
5. 数据：两组各自的下载步骤会自动补齐需要的数据——1h 组用的就是 `make research` 的 1h/4h/1d；
   5m 组会多下载这几个币最近约 2 年的 **5m 和 15m** K线（BTC 在交易对里，它的 5m/4h 也就有了）。
   平时的 `make research` 不会下载 5m 数据。

底层开关：`RESEARCH_BENCHMARKS=1 bash scripts/research.sh ...` 让研究脚本把 `benchmarks/`、`vendor/` 里的策略也放进来、并且不调参。
一般不用直接用，`make research-benchmarks` 会替你设好。

## 6. 要多久、要多少内存、多少硬盘（估算）

| 步骤 | 大概 |
|---|---|
| `fetch_benchmarks.sh` | 几秒钟（一共约 3.5 MB） |
| 1h 组 | 1 ~ 3 小时（4 个策略 × 9 段回测，60 个币 × 约 6 年 1h K线；数据已经下载过的话不用再下） |
| 5m 组：第一次下载 | 每个币约 2 分钟（5m + 15m，约 2 年），20 个币约 40 分钟；以后更新几分钟 |
| 5m 组：两个 freqtrade-strategies 策略 | 每个 4 段回测，一共约 0.5 ~ 2 小时 |
| 5m 组：NFI | **很慢**，4 段回测，可能要 4 ~ 12 小时（看 VPS） |
| 硬盘 | 20 个币 2 年的 5m/15m 数据约几百 MB |

**内存**：NFI 回测时把所有币的全部指标（约 200 多列）同时放在内存里。估算每个币每天的 5m 数据约 1 MB，
最长的一段（训练段，约 2 年的 2/3 ≈ 490 天）每个币约 0.5 GB，20 个币约 10 GB。
所以默认按可用内存自动决定用几个币（5 ~ 20 个），开始时会打印"用了几个、为什么"。
内存不够时系统会杀掉回测进程（日志里有 `Killed`、退出码 137），那就用 `--nfi-pairs 8` 或 `--days 365` 再跑。

## 7. 怎么和我们的策略比（15% 规则）

基准策略和我们的策略**用的是同一把尺子**：同一个 `scripts/research.sh`、同样的手续费+滑点（每边 0.07%）、同样的通过标准。
每组跑完会在 `user_data/research/run_<时间>/summary.txt` 出一张排名表（屏幕上也会打印），看法和我们的研究表完全一样
（见 README「第 2 步：策略研究」→「怎么看排名表」）：

- **"结论"列 = 通过** 才算过关：样本外和走步拼接结果都要满足 **账户权益回撤（含浮亏）≤ 15%**、总收益 > 0、盈亏因子 ≥ 1.2、
  交易 ≥ 30 笔、杠杆 ≤ 5 倍；训练段也要赚钱、回撤 ≤ 15%；样本外盈亏因子不能比训练段差太多；1h 组还有"没看过的一段"（2021 ~ 2023）也要赚钱。
- **1h 组**：区间、交易对、手续费都和 `make research` 一样，可以直接和我们的研究表逐行比
  （比"样本外收益、样本外 PF、样本外回撤、走步收益、走步回撤"这几列）。
- **5m 组**：只有最近约 2 年、币也少，**不能直接和我们 6 年的研究表比**。要比就加 `--with-ours`，
  让我们第 3 轮的默认策略在同一区间、同一批币里一起跑，出在同一张表里
  （注意：`XSMomentum` 是在约 60 个币里排名的策略，只给它 20 个币时它的表现不代表它本来的样子）。
- 表下面的"子账户组合检查"会把训练段赚钱的策略合在一起算——对基准策略这一节只是顺带算的，可以不看。

几点要心里有数：

1. **加仓（DCA）策略的回撤按逐笔订单算**（第 4 轮起）：freqtrade 结果里每笔交易记的数量和平均开仓价是**最后的**值。
   对会加仓/分批平仓的 NFI、`VolatilitySystem`，回撤检查（`scripts/check_drawdown.py`，研究报告也用它）按结果里的逐笔订单（orders）
   重建每个时段的持仓数量和平均开仓价（和 freqtrade 自己的算法相同），分批平仓的盈亏记在成交那一刻，整笔平仓时再记剩下的部分。
   一次开满的策略（我们自己的策略都是这样）结果和以前逐位相同。
   - **第 3 轮的报告还是旧算法**（按最后的数量和均价套整个持仓期间，持仓中间的浮亏可能被算多或算少），NFI 训练段 30.26%、
     样本外 9.28% 都是这样算的。不用重跑回测，在 VPS 上对原来的结果目录重新生成报告就是新算法的数：
     `.venv/bin/python scripts/research_report.py report --run-dir <第 3 轮 5m 组的结果目录>`
     （会重写这个目录里的 summary.txt 等报告文件，想留着旧版就先复制一份）。
   - 自检：`.venv/bin/python scripts/check_drawdown_math.py`（手算的例子）；
     加 `--result <某个 backtest-result-….zip>` 还会核对这个结果里每笔加仓交易的重建（各次平仓盈亏加起来 = freqtrade 的
     profit_abs），并列出新旧两种算法的回撤。
   - 还剩的近似：K线内部的价格顺序不知道，加仓 / 分批平仓那根K线按整根K线的最低价（空单最高价）算，偏保守；
     资金费按 freqtrade 记的总额在平仓时计入。很旧的 freqtrade 结果没有 orders，会退回旧算法，并提醒一次（`make check` 在表格末尾，研究脚本在屏幕输出里）。
   - 回测日志里 freqtrade 自己的 "Max % of account underwater" 只按平仓算（偏乐观），可以作对照。
2. **频繁交易的策略被手续费吃得多**：5m 和 1h 的策略交易次数多，第 2 轮我们自己的 1h 策略就是这样亏掉的。每边 0.07% 是按实盘估的，不要为了基准策略调低。
3. **幸存者偏差**：交易对是今天挑的"还活着的大币"，所有策略都偏乐观；做多的策略尤其占便宜。
4. **资金费**：OKX 只给最近约 3 个月的资金费率，更早的按 0 算；拿得久的策略会显得偏好（表里的"样本外扣资金费"列有估算）。
5. **NFI 的最近 2 年接近它的训练数据**（第 3 节第 5 点），它在这段表现好也不能说明以后好。
6. **5m 组报告开头"样本外这段第 2 轮已经看过……这次的全部数据第 2 轮都看过"的 `!!` 提醒**：5m 组只用最近约 2 年（2024-11 以后），整段都在第 2 轮用过的数据里（2024-01-01 以后），
   所以没有"没看过的一段"。这条提醒说的是用 `--with-ours` 放进来的我们自己的策略；对基准策略来说，这段也接近 NFI 的训练数据（上面第 5 点），一样只能当参考。

怎么理解结果：

- 基准策略也过不了 15% 规则：说明"在这些成本和风控下，能稳定赚钱本来就很难"，不只是我们的策略不行。
- 某个基准策略过了：不要直接拿去实盘。先看它是靠什么赚的（交易数、每年的收益、是不是只靠一段大行情），再考虑借鉴它的思路写进我们自己的、能讲清楚的策略里。

## 8. 安全说明

- 我们的开发环境里**没有下载、导入或运行过**这些第三方代码（只把它们当文本读，确定文件名、类名、周期、需要的设置和依赖）。
  它们只在你自己的 VPS 上、由你运行 `fetch_benchmarks.sh` 下载以后才会被 freqtrade 运行。
- 下载脚本只下载、不运行；文件按固定 commit 下载并核对 sha256；不下载任何自动更新工具。
- 回测**不需要**交易所 API Key：研究配置里没有密钥，不要用带密钥的配置去跑基准策略。
  freqtrade 运行策略时它就是普通的 Python 代码，权限和我们自己的策略一样——所以只运行这里固定、校验过的版本。
- 不想要了：`rm -rf user_data/strategies/vendor` 就删干净了（我们的仓库不受影响）。

## 9. 换版本（以后要测新版本时）

1. 用 `git ls-remote https://github.com/<仓库> refs/heads/main` 查新的 commit；
2. 按 `https://raw.githubusercontent.com/<仓库>/<commit>/<文件路径>` 下载新文件（当文本），**先读一遍**它 import 了什么、有没有新的辅助文件、有没有联网/自动更新的代码；
3. 算 sha256，把新的 commit 和 sha256 写进 `scripts/fetch_benchmarks.sh` 的清单，并更新本文第 2 节的表；
4. NFI 换成新的主策略（例如 X9）时，还要改 `BenchmarkCaps.py` 里的文件名/类名、`configs/research-overrides/` 的文件名和 `scripts/research_benchmarks.sh` 的策略名；
5. 在 VPS 上 `bash scripts/fetch_benchmarks.sh --force`。

## 10. 第 4 轮：NFI 低杠杆版本逐年回测（接着这里往下做的）

第 3 轮 `NostalgiaForInfinityX8Capped` 训练段回撤 30%、没过 15% 规则，而且它拿着亏损的仓位不平（样本外只看平仓的回撤 1.92%、含浮亏 9.28%）。
第 4 轮把固定版本的 NFI 冻结，另外加了两个低杠杆版本 `NostalgiaForInfinityX8Lev1`（封顶 1 倍）、`NostalgiaForInfinityX8Lev2`（封顶 2 倍），
也在 `BenchmarkCaps.py` 里，覆盖文件和 5 倍版本完全相同；然后 2021 年起**每个日历年单独回测一次**（包含 2022 年熊市），
每个版本给一个"可以上模拟盘 / 不建议上模拟盘"的结论：`bash scripts/fetch_benchmarks.sh` → `make research-nfi-yearly`。
NFI 的杠杆在源码里是怎么定的（带行号）、三个版本封住了哪些路径、要多久、怎么看结果：[`docs/nfi-yearly.md`](nfi-yearly.md)。
那里的"通过"也只是合理性检查（NFI X8 在 2026-09 以后改了约 1200 次，每一年都可能是作者看过的），真正的检验是之后的币安模拟盘。
（冻结版 NFI 1 倍杠杆的币安模拟盘、看门狗和前向报告：[`docs/forward-test.md`](forward-test.md)、[`docs/forward-monitoring.md`](forward-monitoring.md)。）
