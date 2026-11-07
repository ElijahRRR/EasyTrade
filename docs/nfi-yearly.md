# NFI X8 逐年回测（第 4 轮）：低杠杆版本，2021 年起每年单独测一次

> 一句话：把固定版本的 NostalgiaForInfinity X8 按 **1 倍 / 2 倍 / 5 倍封顶**三个版本，2021 年起**每个日历年单独回测一次**
> （每次从 1000 USDT 重新开始），看它在每一年——特别是 2022 年熊市——账户回撤（含浮亏）能不能守住 15%。
> 这只是**合理性检查**；真正的检验是之后在币安的**模拟盘**（前向测试，2 ~ 3 个月）。

在你自己的 VPS（美国那台，能连 OKX）上，建议在 tmux 里：

```bash
git pull
bash scripts/fetch_benchmarks.sh          # 第一次：下载固定版本的 NFI，核对 sha256（已经下过就几秒钟）
make research-nfi-yearly                  # 默认：3 个版本 × 2021 ~ 今年，每次一个（很慢，见第 5 节）
make research-nfi-yearly ARGS="--classes Lev1"   # 想先快一点看结论：只跑 1 倍版本
```

---

## 1. 为什么做这个

第 3 轮（2026-11-07，你的 VPS）13 个策略一个都没过。其中 `NostalgiaForInfinityX8Capped`（5m，20 个 OKX 币，只测了 2024-11-07 ~ 2026-11-07）：

| | 收益 | 交易 | 胜率 | PF | 账户回撤（含浮亏） | 只看平仓的回撤 |
|---|---|---|---|---|---|---|
| 训练段（2024-11 ~ 2026-03） | +470.75% | 171 | - | 15.44 | **30.26%（超过 15%）** | - |
| 样本外（2026-03 ~ 2026-11） | +54.27% | 35 | 97.14% | 23.45 | 9.28% | 1.92% |

几个要心里有数的地方：

- **样本外其实不"外"**：NFI X8 这个文件是 2026-09-06 才建的，到 2026-11-07 已经提交了约 1223 次（其中 488 次的标题是 "add protection"）。
  作者一直在用最近几年的行情调它——上游源码的注释里就写着"在七年的基线上测过"、"六年里逐笔重建亏损"这类话
  （固定版本第 753、763 行附近的注释）。所以 2021 ~ 2026 每一年对作者来说都可能是**看过的数据**。
- **它拿着亏损的仓位不平**：只看平仓的回撤 1.92%、含浮亏却是 9.28%——胜率 97% 是因为亏的仓位一直拿着、等反弹或者加仓摊平。
- **训练段 3 倍杠杆回撤 30%**，超过我们的 15% 规则。

所以这一轮的计划（已经跟你说过、你同意的）：

1. **冻结**固定版本的 NFI（不跟着上游更新），只把杠杆降到 1 倍 / 2 倍；
2. **逐年回测** 2021 ~ 2026（含 2022 熊市），看低杠杆时每一年的账户回撤——**这份文档讲的就是这一步**；
3. 再在币安合约**模拟盘**（paper trading）跑 2 ~ 3 个月，配前向报告和账户权益看门狗（回撤超过 15% 就停止开新仓、等人工检查）。
   模拟盘要在**币安允许访问的地区**的服务器上跑（你那台跑 Rust 引擎的 Vultr 服务器）；美国 VPS 继续做 OKX 回测。

## 2. 先说清楚：这里"通过"只是合理性检查

报告抬头每次都会写这段话，这里再说一遍：

- 每一年都可能是 NFI 作者调参时看过的（样本内）。**这里"可以上模拟盘"只说明"在它自己可能调过的年份里没出大问题"**，不能证明以后能赚钱。
- **"不建议上模拟盘"是有用的信息**：连它自己调过的年份都扛不住 15%，上模拟盘也没意义。
- **真正的样本外检验是模拟盘**：2026-11 以后的行情，作者也没见过。

## 3. NFI X8 的杠杆是怎么定的（读固定版本源码的记录）

文件：`NostalgiaForInfinityX8.py`，commit `74c03087b8ced3899509ba8d36f5f1565bc5f49a`（v18.0.214，65283 行，sha256 `839b1aa2…4f1982`，和
`scripts/fetch_benchmarks.sh` 清单里的一样）。**只当文本读的**（`curl … | grep`），开发机上没有下载成文件、没有导入、没有运行。行号是这个版本的。

| 行号 | 内容 | 对杠杆的影响 |
|---|---|---|
| 264 ~ 267 | `futures_mode_leverage = 3.0`，`futures_mode_leverage_rebuy_mode = 3.0`，`futures_mode_leverage_grind_mode = 3.0` | 默认 3 倍（普通 / rebuy / grind 三种入场） |
| 269 ~ 275 | `futures_mode_leverage_long / _long_rebuy_mode / _long_grind_mode / _short / _short_rebuy_mode / _short_grind_mode = None` | 按方向的覆盖值，默认 None = 不覆盖 |
| 1179 ~ 1191 | `NFI_SAFE_PARAMETERS`（`__init__` 里，1177 行起）有上面全部 9 个 `futures_mode_leverage*` | **配置可以改杠杆**：配置顶层同名键（1275 ~ 1278 行，"老写法"）或者 `nfi_parameters` 块（1259 ~ 1273 行） |
| 1255 ~ 1256、1265 | `nfi_advanced_mode = true` 时 `nfi_parameters` 能改**任何**属性 | 同上，范围更大 |
| 1314 ~ 1316 | 合约模式时 `is_futures_mode = True`、`can_short = True` | 合约才用杠杆、才做空 |
| 5623 ~ 5658 | `leverage()`：按入场标签和方向先找覆盖值（不是 None 就直接返回它），否则 rebuy 标签 → `…_rebuy_mode`，grind 标签 → `…_grind_mode`，其余 → `futures_mode_leverage` | **完全不看** freqtrade 传进来的 `proposed_leverage` 和 `max_leverage`；做空在没有覆盖值时检查的是做多的 rebuy/grind 标签（上游的小毛病），实际就是 `futures_mode_leverage` |
| 5662 ~ 5667 | `correct_min_stake()`：币安 / bybit 上最小下单额 = 5 USDT ÷ 杠杆（没有交易时用 `futures_mode_leverage`） | 只影响最小下单额 |
| 2527 ~ 2640 | `custom_stake_amount()`：首笔仓位 = freqtrade 给的保证金 × 倍数（v4 普通 1.0、rebuy 0.25、grind 0.20 ~ 0.50 等，398 ~ 915 行） | 倍数作用在**保证金**上，和杠杆无关；名义仓位 = 保证金 × 杠杆 |
| 38999、39015、39847 | 加仓（grind）：`slice_amount` = 第一笔的名义价值；每次加仓的保证金 = `slice_amount × grind 倍数 ÷ trade.leverage` | 加仓都按"第一笔的保证金的几分之几"算，**杠杆降低 → 每次加仓的名义价值同比例变小** |
| 469 ~ 470 | `grinding_v2_max_stake = 1.0  # ratio of first entry`，`grinding_v2_max_grinds_and_buybacks = 20` | grind 的上限按第一笔算；上游注释（762 ~ 766 行）提到过某个配置下加仓失控的例子（"INJ 58 adds, 28x"） |
| 37049 ~ 37100、37596 ~ 37640 | 止损：`profit_stake < -(entry_cost × threshold ÷ leverage)`，`entry_cost` = 第一笔的名义价值 | 止损线 = **第一笔保证金 × threshold**（USDT），和杠杆无关 |
| 730、770、772 | `system_name_use = system_v4_name`；`system_v4_stops_enable = False`；`system_v4_stop_threshold_doom_futures = 0.35` | 默认用 v4 系统，**v4 的止损默认是关的**，靠 derisk（亏到一定程度分批减仓，39540 行起，同样按第一笔保证金算）兜底 |
| 1280 ~ 1297 | 止盈目标缓存 `user_data/nfix8-profit_max-…-(backtest).json` | 和杠杆无关；我们在回测里把它换成临时空文件（见下） |
| 1303 ~ 1304 | OKX 上 `startup_candle_count = 480` | freqtrade 回测时**每个参考周期**都按 480 根往前加载：1d 要 480 天、4h 要 80 天（见第 5 节的下载） |

**freqtrade 这一侧**（按 `.venv` 里 freqtrade 2026.10 的源码核对）：

- `optimize/backtesting.py` 1064 ~ 1088 行、`freqtradebot.py` 1331 ~ 1345 行：只在**开新仓**时调用一次策略的 `leverage()`，
  结果再夹在 `1 ~ 交易所这个币的最大杠杆` 之间；**加仓沿用这笔交易的杠杆**（"Changing leverage currently not possible"）。
  所以封住 `leverage()` 的返回值，就封住了这笔交易所有订单的杠杆。
- 唯一绕过 `leverage()` 的路：FreqUI / Telegram 的**强制开仓**可以自带 `leverage` 参数（`freqtradebot.py` 的 `leverage_`）。
  所以配置里 `force_entry_enable` 必须保持 `false`（`configs/config.binance-futures.json` 就是 `false`）。

**降杠杆意味着什么**（从上面推出来的，逐年回测会验证）：

- 同样的保证金，名义仓位变成 1/3（1 倍）或 2/3（2 倍）：同样的价格波动，账户的浮亏也是 1/3 或 2/3；加仓的名义价值同比例变小。
- 但 NFI 的止损 / derisk 线是按**保证金**算的（亏到第一笔保证金的百分之多少）：1 倍时价格要走 3 倍远才会触发。
  所以**每一笔被止损 / 减仓时亏的 USDT 差不多一样**，只是触发得少、拿得更久；"拿着亏损不平"的毛病不会因为降杠杆消失。
  报告里的"差距"（含浮亏回撤 - 只看平仓的回撤）和"年底未平"两列就是看这个的。

## 4. 三个版本（`user_data/strategies/benchmarks/BenchmarkCaps.py`）

| 类名 | 简称 | 杠杆封顶 | 说明 |
|---|---|---|---|
| `NostalgiaForInfinityX8Lev1` | Lev1 | 1 倍 | 不加杠杆。模拟盘的首选候选 |
| `NostalgiaForInfinityX8Lev2` | Lev2 | 2 倍 | |
| `NostalgiaForInfinityX8Capped` | Capped | 5 倍 | 就是第 3 轮测的那个（NFI 默认 3 倍，封顶 5 倍），用来和第 3 轮的数字对照 |

- 都是我们自己写的小子类，继承 NFI 原类（不复制、不修改它的代码）。Lev1 / Lev2 是 `NostalgiaForInfinityX8Capped` 的子类，
  只把类属性 `_easytrade_leverage_cap` 改小；5 倍版本的行为和以前完全一样。
- 封住的路径：`leverage()` 的返回值；9 个 `futures_mode_leverage*` 属性（在上游 `__init__` 读完配置之后、`bot_start` 时再查一遍）
  ——配置里写得再大也没用（超过 5 倍会打警告）。
- 回测时 NFI 的止盈目标缓存每次换成临时目录里的空文件（各年 / 各段回测互不影响）；模拟盘 / 实盘不动它。
- 每个版本有自己的覆盖文件 `configs/research-overrides/NostalgiaForInfinityX8Lev1.json` / `…Lev2.json`，内容和
  `NostalgiaForInfinityX8Capped.json` **完全一样**（同时 6 个仓位、NFI 要求的 exit 设置等，见 `docs/benchmarks.md` 第 4 节）——
  三个版本只差杠杆，结果才能直接比。改的话三个文件一起改。
- 没下载 NFI（`user_data/strategies/vendor/` 里没有文件）时导入就报中文错误，提示先 `bash scripts/fetch_benchmarks.sh`。
- 开发机上用我们自己写的替身基类（不是 NFI，故意要 10 倍、配置里再改成 20 / 50 倍）测过：三个版本每一条路径都 ≤ 各自的封顶。

## 5. 怎么运行、要多久、要多少内存

```bash
tmux new -s nfi                                   # 断线也不会中断；回来用 tmux attach -t nfi
make research-nfi-yearly                          # 默认：Lev1 Lev2 Capped × 2021 ~ 今年
```

常用变化（`ARGS="..."` 里写，或者直接 `bash scripts/research_nfi_yearly.sh ...`）：

```bash
make research-nfi-yearly ARGS="--classes Lev1"            # 只跑 1 倍（先看它，省三分之二的时间）
make research-nfi-yearly ARGS="--classes 'Lev1 Lev2'"     # 只跑低杠杆的两个
make research-nfi-yearly ARGS="--years 2022"              # 只跑 2022 年（也可以 --years 2021-2023）
make research-nfi-yearly ARGS="--pairs 10"                # 用研究列表的前 10 个币（BTC 一定在里面）
make research-nfi-yearly ARGS="--no-download"             # 不更新数据
make research-nfi-yearly ARGS="--resume user_data/research/nfi_yearly/run_<时间>"   # 接着跑没跑完 / 出错的
MAX_DD_PCT=12 make research-nfi-yearly                    # 换回撤上限（默认 15%）
make research-nfi-yearly ARGS="--synthetic"               # 不联网的流程自检（合成数据 + 替身策略，几分钟，数字没有意义）
```

它做的事（`scripts/research_nfi_yearly.sh`）：

1. **核对 NFI 是固定版本**（`scripts/fetch_benchmarks.sh --check`，不联网、不运行它）；没下载就提示先下载，被改过就拒绝运行。
   然后把 `BenchmarkCaps.py` 和 NFI 文件**复制到这次的结果目录**（`strategies/`，记下 sha256），之后每次回测都用这份副本。
2. **交易对**：研究列表（`configs/pairs.okx-research.json`）的前 N 个，BTC 一定在里面；**每一年都是同一批币**。
   N 默认按可用内存自动定（5 ~ 20 个；内存够时就是第 3 轮的那 20 个）。
3. **下载**（`scripts/download_okx_data.sh`，已有的只补新的；本地开始得太晚的币自动用 `--prepend` 补前面那段）：
   5m / 15m / 1h 从第一年的前一个月开始（默认 **2020-12-01**）；NFI 在 OKX 上每个周期预热 480 根，所以 4h 从约 2020-10-08、
   1d 从约 2019-09-04 开始（交易所没有那么早的就有多少给多少）。资金费率 / 标记价格照旧只下最近 120 天（OKX 更早的没有）。
4. **数据检查**（`data_check.txt`）：每个币每一年有没有整年的数据、参考周期缺不缺、年初的预热够不够；定下每一年的区间：
   1 月 1 日 ~ 12 月 31 日，**今年到数据的最后一个完整的日子**。一个币都没有数据的年份跳过。
5. **回测**：每个版本 × 每一年**单独回测一次**，每次从 1000 USDT 开始、一次只跑一个（内存有上限）。
   配置 = 研究配置（`configs/config.okx-research.json`）+ 交易对 + 这个版本的覆盖文件；手续费+滑点每边 0.07%、保护机制
   都和 `make research` 一样。**某一次出错就记下来接着跑下一次**；跑完一个版本就打印一次它的中间结果。
6. **报告**（`scripts/nfi_yearly_report.py`，见第 6 节）。

| | 大概（估算） |
|---|---|
| 第一次下载 | 20 个币的 5m 从 2020-12 起约 6 年，OKX 每次请求 300 根：每个币约 2 千次请求，全部**好几个小时**；以后更新几分钟 |
| 硬盘 | 20 个币 6 年的 5m/15m/1h/4h/1d 约 1 ~ 2 GB |
| 一次回测（一个版本 × 一年，20 个币） | NFI 很慢，大约 **1 ~ 4 小时**（第 3 轮 4 段约 3.3 年的数据用了 4 ~ 12 小时） |
| 默认全部（3 个版本 × 6 年 = 18 次） | 大约 **1 ~ 3 天**。Lev1 的各年最先跑完，可以先看它 |
| 内存 | 一次只回测一年：每个币约 380 MB（估算：每天约 1 MB），20 个币约 8 GB + freqtrade 本身 1 ~ 2 GB |

内存不够时系统会杀掉回测进程（日志里 `Killed`、退出码 137）：报告里那一年记为"回测出错"，脚本接着跑下一次。
可以换少一点的币单独重跑那一年（`--pairs 8 --years 2022 --classes Lev1`，会是新的结果目录）。

结果在 `user_data/research/nfi_yearly/run_<时间>/`：

| 文件 | 内容 |
|---|---|
| `summary.txt` / `summary.csv` / `summary.json` | 报告（最后也会打印在屏幕上；CSV 可以直接用 Excel 打开） |
| `data_check.txt` | 每个币每一年的数据情况 |
| `plan.json` | 这次的设置（交易对、每一年的区间、回撤上限、判定规则、策略文件的 sha256） |
| `bt/<版本>__<年份>/` | 每次回测的 freqtrade 原始结果（可以在 FreqUI 里看） |
| `logs/` | 每一步的完整日志 |
| `strategies/` | 这次用的策略代码副本（含 NFI，GPL-3.0：**不要把整个结果目录发给别人**；发报告用 summary.txt） |

不重跑回测、换个规则重新出报告：

```bash
.venv/bin/python scripts/nfi_yearly_report.py report --run-dir user_data/research/nfi_yearly/run_<时间>
.venv/bin/python scripts/nfi_yearly_report.py report --run-dir <结果目录> --max-dd 12               # 更严的回撤上限
.venv/bin/python scripts/nfi_yearly_report.py report --run-dir <结果目录> --max-losing-years 0      # 不允许任何亏损年
```

## 6. 怎么看报告

每个版本一张表，每一年一行；最后一张汇总表每个版本一行。

| 列 | 意思 |
|---|---|
| 年份 | 后面带 `*` 的不是完整的一年（今年，或者那一年 BTC / 一半以上的币数据不全）：只查回撤、杠杆、亏损，不算"完整年份" |
| 收益 | 这一年从 1000 USDT 开始的总收益 |
| PF / 交易 / 胜率 | 盈亏因子（总盈利 ÷ 总亏损）、交易笔数、胜率 |
| 回撤(含浮亏) | **账户权益**（已平仓余额 + 持仓浮动盈亏）从最高点到最低点的跌幅，口径同 `make check`（`scripts/check_drawdown.py`，"K线(5m)" = 逐根 5 分钟K线） |
| 回撤(平仓) | 只看已平仓交易的回撤（freqtrade 自己报告的那种） |
| 差距 | 含浮亏 - 平仓（百分点）。**差距大 = 拿着亏损的仓位不平**，胜率和 PF 看起来比账户实际好 |
| 最差单笔(估) | 某一笔交易持仓期间最多亏了初始资金的百分之多少（含浮亏、手续费和中途减仓已实现的亏损）。只用回测记录估算：加过仓 / 减过仓的按订单重建每一段持仓，每段都按整个持仓期间最差的价格算、取最差的一段，所以只会估多、不会估少 |
| 最长持仓 | 这一年持仓最久的一笔（天） |
| 杠杆 | 这一年所有交易里最大的杠杆，**必须 ≤ 这个版本的封顶**，否则这个版本的结果作废 |
| 加仓笔数 | 开仓后又加过仓的交易数（NFI 的 rebuy / grind） |
| 年底未平 | 回测结束时还开着、被按最后价格**强制平掉**的笔数，括号里是它们合计占初始资金的盈亏——能看出"年底账面上压着多少亏损" |
| 缺数据的币 | 这一年没有整年数据的币 / 全部币（多半是那时还没上线；这一年等于在更少的币上跑） |

表下面每一年还有细节：最差单笔是哪个币、哪天、价格逆向走了多少；最长持仓是哪一笔；年底被强平的仓位合计多少；哪些币没数据。

**结论规则**（每个版本一个，全部满足才是"**可以上模拟盘**"，否则"**不建议上模拟盘**"并列出原因）：

1. 计划里的每一年都跑完了，没有回测出错；
2. **每一年**（包括今年这种不完整的年份）账户回撤 ≤ 15%（含浮亏；取"平仓"和"含浮亏"里大的那个，同 `make check`）；
3. 每一年的最大杠杆 ≤ 这个版本的封顶；
4. 完整年份至少 3 个，每个完整年份都有交易；
5. 亏损的年份（包括今年）**最多 1 个**，而且**亏不到 5%**；
6. 各年收益连乘 > 0。

这比 `make research` 的通过标准更看重"每一年都不能出事"：模拟盘之后要上实盘的话，任何一年回撤超过 15% 都不能接受。
规则的数字写在 `plan.json` 里，重新出报告时可以用 `--max-dd`、`--max-losing-years`、`--max-year-loss`、`--min-full-years` 改（只会更严才有意义）。

## 7. 已知的局限

1. **样本内**（第 2 节）：通过 ≠ 以后能赚钱。
2. **加仓（DCA）交易的浮亏**：NFI 几乎每笔都可能加仓 / 分批平仓。账户回撤由 `scripts/check_drawdown.py` 计算，加仓交易的算法以它开头的说明为准；
   "最差单笔"一列是只用回测记录做的保守估算。
3. **年底强制平仓**：每一年单独回测，12 月 31 日还开着的仓位按最后价格平掉、算进这一年。真实账户里这些仓位会继续拿到下一年，
   所以每一年的收益 / 回撤和"连着跑 6 年"不完全一样（"年底未平"一列就是提醒这个）。
4. **每年从 1000 USDT 重新开始**：不会因为前一年亏了就仓位变小，"各年连乘"只是一个假设的复利数字。
5. **幸存者偏差**：交易对是 2026 年还活着的大币，越早的年份越偏乐观（那时后来消失的币不在里面）；2021 年有些币还没上线。
6. **资金费**：OKX 只给最近约 3 个月的资金费率，更早的按 0 算；NFI 拿得久，资金费的影响不小。
7. **OKX ≠ 币安**：模拟盘在币安，价格、深度、资金费都和 OKX 不完全一样（README「OKX 数据 vs 币安」）。
8. **只有 6 个仓位、固定币**：NFI 推荐 40 ~ 80 个按成交量的动态币，我们为了内存和对比用固定的前 N 个。

## 8. 安全说明

- 开发机上**没有下载、导入或运行过** NFI：读源码只用 `curl … | grep` 当文本看；所有测试用的是我们自己写的替身基类。
  `--synthetic` 时脚本在结果目录里写的 `NostalgiaForInfinityX8.py` 是一个开头写明"替身策略，不是 NFI"的小文件，只有 `--synthetic` 才会写。
- NFI 只在你的 VPS 上、由 `fetch_benchmarks.sh` 按固定 commit 下载并核对 sha256 以后才运行；不下载它的自动更新工具。
- 回测不需要交易所 API Key，研究配置里也没有。

## 9. 下一步

- 某个低杠杆版本"可以上模拟盘"：在**币安允许访问的地区**的服务器（你的 Vultr 服务器）上用它跑币安合约模拟盘 2 ~ 3 个月，
  配前向报告和账户权益看门狗（回撤超过 15% 就停止开新仓、等人工检查）。美国 VPS 继续做 OKX 回测（币安对美国 IP 返回 HTTP 451）。
  已经准备好的是 **1 倍版本**（`NostalgiaForInfinityX8Lev1`）的模拟盘：`make dryrun-nfi-check` → `make dryrun-nfi`，
  说明见 [`docs/forward-test.md`](forward-test.md)、[`docs/forward-monitoring.md`](forward-monitoring.md)。
  只有 2 倍版本"可以"、1 倍"不建议"时先别上：要换成 2 倍得改配置和启动检查（`scripts/check_live_guards.py` 的 `DRYRUN_TARGETS`），先一起看报告再定。
- 全都"不建议"：说明降杠杆也救不了它的回撤，就不上模拟盘了；把报告（`summary.txt`）发回来一起看是哪几年、为什么。
