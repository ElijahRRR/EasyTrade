# 模拟盘监控：前向报告 + 账户权益看门狗（第 4 轮）

> 一句话：模拟盘跑着的时候，**看门狗**每分钟读一次账户权益（含持仓浮亏），回撤超过 **15%** 就让机器人**停止开新仓**、
> 发中文告警，等你人工复盘；**前向报告**每天（或每周）读一次数据库，逐根K线算出完整的权益曲线、回撤、胜率、盈亏比，
> 对照前向测试的成功标准。两个工具都只读、不改策略；看门狗唯一会做的"动作"是暂停开新仓（加 `--flatten` 才会平仓）。

前向测试本身（为什么做、在哪跑、怎么启动、什么时候停）：[`docs/forward-test.md`](forward-test.md)。
这里只讲这两个工具。命令都在**跑模拟盘的那台服务器**上、仓库目录里运行
（币安允许访问的地区的服务器，例如你那台跑 Rust 引擎的 Vultr 服务器；美国 VPS 继续只做 OKX 回测）。

```bash
# 看门狗：装成 systemd 服务长期跑（docs/forward-test.md 第 7 节），平时不用管
sudo systemctl enable --now easytrade-dryrun@nfi-watchdog
.venv/bin/python scripts/equity_watchdog.py --status        # 看门狗记下的峰值、现在的回撤、是否已暂停
.venv/bin/python scripts/equity_watchdog.py --test-alert    # 发一条测试告警，确认 Telegram 收得到

# 前向报告：先补K线，再出报告
.venv/bin/freqtrade download-data --config configs/config.dryrun-nfi.json --timerange 20261107- --timeframes 5m
.venv/bin/python scripts/forward_report.py                  # 或 make forward-report
```

---

## 1. 为什么要这两个工具

- **freqtrade 没有"账户级"的硬止损。** 它的 `MaxDrawdown` 保护只看**已平仓**交易的盈亏，持仓里的浮亏不算。
  NFI 会扛着亏损的单子、加仓摊平，浮亏可以比平仓口径大很多：第 3 轮回测里 NFI 的样本外回撤，平仓口径只有 1.92%，
  含浮亏是 9.28%；训练段含浮亏到了 30.26%。项目硬规则看的是**含浮亏**的账户权益（≤ 15%，唯一的定义在
  `scripts/check_drawdown.py` 的 `DEFAULT_MAX_DD_PCT`），所以要自己盯。
- **看门狗管"现在"**：每 60 秒读一次机器人的接口，超限马上暂停开新仓。它只能看到它运行期间每分钟的那个瞬间。
- **报告管"全程"**：从数据库里的每笔交易、每张订单，加上K线，重建整个测试期间的权益曲线（每根K线里对持仓最不利的价格都算进去）。
  最后判断这一轮通不通过，以报告为准。

两者用同一个上限（15%，环境变量 `MAX_DD_PCT` 或 `--max-dd` 可以临时改严）。口径的差别见第 5 节。

## 2. 前向报告 `scripts/forward_report.py`

### 2.1 先补K线

浮亏要用K线算。模拟盘机器人自己不会把K线存下来，所以出报告前要用 freqtrade 下载一次
（在模拟盘服务器上运行：它连得上币安；`--timerange` 写模拟盘开始的前一天，之后每次只会补新的部分，很快）：

```bash
.venv/bin/freqtrade download-data --config configs/config.dryrun-nfi.json --timerange 20261107- --timeframes 5m
```

- 用的是模拟盘同一份配置：交易所 = 币安、合约（futures）、交易对 = 配置里的固定列表，K线存到 `user_data/data/binance/futures/`。
- 和机器人同时运行没关系（它不碰机器人的数据库）。
- 报告发现缺K线时会列出缺了哪些币、哪些时段，并打印一条只补这几个币的下载命令。

### 2.2 运行

```bash
.venv/bin/python scripts/forward_report.py                       # 默认读 configs/config.dryrun-nfi.json 和它的数据库
.venv/bin/python scripts/forward_report.py --telegram            # 另外发一条中文摘要到 Telegram
.venv/bin/python scripts/forward_report.py --json user_data/dryrun/forward.json   # 另外把数字写成 JSON
make forward-report ARGS="--telegram"                            # 同上（make 方式）
```

常用参数：`--db`（数据库，默认取配置的 `db_url` = `user_data/dryrun/nfi.sqlite`）、`--datadir`（K线目录，默认 `user_data/data/binance`）、
`--timeframe`（默认 = 配置的 5m）、`--start-balance`（初始资金，默认 = 配置的 `dry_run_wallet` 1000）、`--max-dd`、
`--as-of 2026-12-01T00:00`（按某个时间点出报告，UTC）、`--watchdog-state`（看门狗的状态文件，默认和数据库放一起；`none` = 不读）。

数据库是**只读**打开的：机器人在跑时直接读最新内容，机器人停了也能读，数据库文件一个字节都不改。

### 2.3 报告里有什么

- 运行了多少天（从 freqtrade 记的第一次启动时间算）、初始资金、**现在的权益（含浮亏）**、收益；
- **回撤(含浮亏)**：现在的、整个期间最大的；**回撤(平仓)**：只看已平仓交易的余额。两者差得多 = 策略在扛着亏损的单子；
- 看门狗：现在是否暂停开新仓、触发过几次、人工解除过几次、它实时见过的最大回撤（读它的状态文件，只读）；
- 已平仓笔数、胜率、盈亏比（总盈利 ÷ 总亏损）、最差的一笔、拿得最久的一笔；
- 持仓表：每个持仓的方向、杠杆、数量、均价、现价、浮动盈亏、分批平仓已实现的盈亏、持有多久、加仓几次；
- 缺K线的币和时段（这些时段的浮亏是估算的）；
- **成功标准**（`docs/forward-test.md` 第 10 节第 1 ~ 4 条）逐项"达到 / 未达到"和进度：

| 标准 | 要求 |
|---|---|
| 运行天数 | ≥ 60 天（60 天时平仓不到 30 笔：延长到 90 天；90 天还不到 = 样本不够、没有结论） |
| 已平仓交易 | ≥ 30 笔 |
| 最大回撤（含浮亏） | ≤ 15%，并且看门狗一次都没触发过（一旦超过，这一轮直接判不通过） |
| 收益 | > 0（还开着的仓位按现价算） |
| 盈亏比 | ≥ 1.2 |

- **其他检查**（参考用，第 10 节第 5、6 条）：单笔最大亏损（含持仓浮亏）占初始资金多少（要求 ≤ 5%）、有没有不是 1 倍杠杆的交易。
  第 7 条（同一份冻结代码、没有手动干预）看 `user_data/dryrun/nfi-strategy/FROZEN.txt` 和 `nfi-starts.log` 自己确认。

### 2.4 怎么算的

1. 从数据库的 `trades`、`orders` 表读出每笔交易和它的每张订单，转成和 freqtrade 自己 `Trade.to_json()` 完全一样的格式
   （自检会逐个字段核对）。
2. 交给 `scripts/check_drawdown.py` 的 `account_equity_drawdown()`（和回测的回撤检查是同一套代码）：
   NFI 会加仓、分批平仓，数据库里每笔交易记的数量和均价是**最后的**值，所以按逐笔订单的成交时间重建每一段的持仓数量和均价；
   每根K线：权益 = 初始资金 + 已实现盈亏 + 持仓浮动盈亏（多单按这根K线的最低价、空单按最高价，扣预估平仓手续费）。
3. 现在的权益算到**最近一根已经收盘的K线**；持仓的现价 = 那根K线的收盘价。
4. 缺K线（没有这个币的文件，或者某段时间没有）：用这笔交易持仓期间到过的最高 / 最低价（freqtrade 记在数据库里）代替，
   偏保守（回撤只会算大），报告里标"估"。持仓的现价没有K线时用持仓期间最差价，标"(估)"。补上K线再跑一次就准了。
5. 还没结算到订单上的资金费（持仓中途的 funding）不算进浮亏，报告里单独列出合计。

### 2.5 退出码和定时运行

退出码：**0** = 正常；**3** = 含浮亏回撤超过上限，或看门狗现在处于暂停状态（要人工复盘）；**1** = 出错（找不到数据库、读不了配置……）。

用 cron 每天 UTC 0:15 补K线、出报告、发 Telegram 摘要（`crontab -e`，用平时登录的普通用户；把路径和日期换成你的）：

```cron
CRON_TZ=UTC
15 0 * * * cd /home/你的用户名/easytrade && .venv/bin/freqtrade download-data --config configs/config.dryrun-nfi.json --timerange 20261107- --timeframes 5m >> user_data/dryrun/forward-cron.log 2>&1; mkdir -p user_data/dryrun/reports && .venv/bin/python scripts/forward_report.py --telegram > user_data/dryrun/reports/forward-$(date -u +\%Y\%m\%d).txt 2>> user_data/dryrun/forward-cron.log
```

- crontab 里 `%` 要写成 `\%`（上面已经写好）。
- 每天一份报告留在 `user_data/dryrun/reports/`；Telegram 收到的是几行摘要，回撤超限时第一行是 `!! 含浮亏回撤 … 超过上限`。
- 想让 cron 在退出码 3 时另外做点什么（例如发邮件），在命令后面接 `|| 你的命令`（退出码 3 和 1 都会触发）。

### 2.6 Telegram

`--telegram` 用环境变量 `FREQTRADE__TELEGRAM__TOKEN`、`FREQTRADE__TELEGRAM__CHAT_ID`；没设置时从 `.env.dryrun-nfi` 里读这两个
（别的变量不读；`--env-file` 可以换文件）。和中文交易通知用同一个 Telegram 机器人就行。token 不会出现在输出和日志里。

## 3. 账户权益看门狗 `scripts/equity_watchdog.py`

### 3.1 怎么算

每 60 秒通过机器人的 REST API（就是 FreqUI 用的接口，只在本机 `127.0.0.1:8081`，账号密码和 FreqUI 一样）读一次：

- **账户权益** = 钱包余额 + 每个持仓的浮动盈亏
  - 钱包余额：`/api/v1/balance` 里 USDT 的 `balance`（模拟盘 = 初始资金 + 已平仓盈亏 + 持仓里分批平仓已实现的盈亏，不含浮亏）；
  - 浮动盈亏：`/api/v1/status` 里每个持仓的 `profit_abs`（按现价、扣开仓和预估平仓手续费，和 FreqUI 显示的一样）；
  - 某个持仓拿不到现价时，按它持仓期间到过的最差价估（偏保守），日志里会说。
- **峰值** = 看门狗读到过的最高权益，**不低于初始资金**（模拟盘取配置的 `dry_run_wallet`）。存在状态文件
  `user_data/dryrun/nfi-watchdog.json` 里，看门狗或服务器重启都不会丢。
- **回撤** = (峰值 − 现在的权益) ÷ 峰值。

### 3.2 告警等级

| 情况 | 看门狗做什么 |
|---|---|
| 回撤 ≥ **12%**（预警线，默认 = 上限 × 0.8，`--warn-dd` 可改） | 发一次"回撤预警"；回落到 11% 以下以后，再到 12% 才会再发 |
| 回撤 **> 15%**（上限，正好 15% 不算） | 调 `POST /api/v1/stopentry`：机器人进入 paused 状态，**不开新仓、不加仓**，已有持仓照常由 NFI 减仓 / 平仓；发告警；状态文件记"已暂停" |
| 已暂停，机器人又变成 running | freqtrade 的暂停状态重启后不保留（会按配置 `initial_state = running` 开始）。看门狗再调一次 stopentry 并告警。另外 `scripts/run_dryrun.sh` 启动机器人时看到状态文件里的"已暂停"，也会让它直接以 paused 状态启动 |
| 已暂停，调了 stopentry 机器人还是 running（连续 3 次）或调用失败 | 告警一次，请你马上在 FreqUI 里点"暂停开仓"或停掉机器人 |
| 已暂停 | 每 12 小时提醒一次还在等人工复盘（`--remind-hours` 可改） |
| 连不上机器人 | 不退出，等待时间逐步加长（60、120、240 秒……最长 5 分钟）；连续超过 **10 分钟**告警一次，恢复后再说一声 |
| API 账号密码不对 | 告警一次（检查 `.env.dryrun-nfi`） |
| 看门狗自己出错（例如状态文件坏了） | 同样逐步加长等待；连续超过 **10 分钟**告警一次（"现在没在保护账户"），恢复后再说一声；连续出错 10 次就退出（退出码 1），systemd 60 秒后重启它 |

看门狗**永远不会自己恢复开仓**：它不调 `/api/v1/start`、也不调 `/api/v1/reload_config`（重载配置也会让机器人恢复开仓）。
默认**不平仓**；要超限时同时把所有持仓按市价平掉，启动时加 `--flatten`（调 `POST /api/v1/forceexit`，`tradeid = all`、`ordertype = market`：
一定是市价单。不写 `ordertype` 的话 freqtrade 用配置里的 `order_types.force_exit`，这份配置是 limit，限价单 2 分钟没成交会被撤掉、没人重试）。
NFI 的设计是扛单等反弹，强制平仓等于替它做了决定，所以模拟盘阶段默认只停开新仓、留给你判断。

告警总是打印在日志里（systemd 的 journal）；配了 `FREQTRADE__TELEGRAM__TOKEN` / `CHAT_ID` 就同时发 Telegram。启动时会发一条"已启动"。

### 3.3 运行

长期运行用 systemd（模板 [`docs/systemd/easytrade-dryrun@.service`](systemd/easytrade-dryrun@.service)，安装步骤见
`docs/forward-test.md` 第 7 节）。看门狗是同一个模板的另一个实例，启动命令是 `bash scripts/run_dryrun.sh nfi-watchdog`
（读好 `.env.dryrun-nfi` 后交给 `scripts/equity_watchdog.py --config configs/config.dryrun-nfi.json`）：

```bash
sudo systemctl enable --now easytrade-dryrun@nfi-watchdog      # 装好以后：开机自动启动
systemctl status easytrade-dryrun@nfi-watchdog                 # 在不在跑
journalctl -u easytrade-dryrun@nfi-watchdog -n 30              # 每分钟一行：权益、峰值、回撤、机器人状态
journalctl -u easytrade-dryrun@nfi-watchdog -g 告警            # 只看告警
sudo systemctl restart easytrade-dryrun@nfi-watchdog           # 重启（峰值、暂停状态都从状态文件接着用）
```

前台试跑 / 临时看一眼（Ctrl+C 停止）：

```bash
bash scripts/run_dryrun.sh nfi-watchdog                        # 和 systemd 一样的启动方式
.venv/bin/python scripts/equity_watchdog.py --once             # 只查一次（退出码 3 = 已暂停）
.venv/bin/python scripts/equity_watchdog.py --status           # 不连机器人，只看状态文件
```

不加 `--config` 时默认用 `configs/config.dryrun-nfi.json`（拿 API 端口、数据库位置、机器人名字、初始资金）；
账号密码和 Telegram 从环境变量读，没有就从 `.env.dryrun-nfi` 里读这几个（`--env-file` 可以换文件）。
看门狗只用 Python 标准库，venv 的 python 和系统自带的 `python3` 都能跑。

要给 systemd 里的看门狗加参数（例如 `--flatten`）：`sudo systemctl edit easytrade-dryrun@nfi-watchdog`，写入

```ini
[Service]
ExecStart=
ExecStart=/bin/bash /home/你的用户名/easytrade/scripts/run_dryrun.sh nfi-watchdog --flatten
```

保存后 `sudo systemctl restart easytrade-dryrun@nfi-watchdog`。

常用参数：

| 参数 | 默认 | 说明 |
|---|---|---|
| `--interval` | 60 | 多少秒查一次 |
| `--max-dd` | 15 | 回撤上限（%）。默认 = 项目硬规则；环境变量 `MAX_DD_PCT`（也可以写在 `.env.dryrun-nfi` 里）也能改。**只能改严**（例如 12）：比 15 宽时看门狗不启动、`--reset` 也不让用（除非加 `--allow-looser-than-rule`，不建议），`scripts/run_dryrun.sh` 连机器人也不启动 |
| `--warn-dd` | 上限 × 0.8 | 预警线（%） |
| `--flatten` | 关 | 超限时同时平掉全部持仓 |
| `--start-balance` | 模拟盘取 `dry_run_wallet` | 峰值不低于它。实盘要填 |
| `--unreachable-alert-min` | 10 | 连不上多少分钟后告警 |
| `--remind-hours` | 12 | 暂停以后多久提醒一次 |
| `--state` | `user_data/dryrun/nfi-watchdog.json` | 状态文件（和数据库放在一起） |
| `--url` | `http://127.0.0.1:8081` | API 地址（也可以用环境变量 `EASYTRADE_API_URL`）。只允许本机地址：明文 HTTP 发密码到别的机器不安全 |

### 3.4 状态文件

`user_data/dryrun/nfi-watchdog.json`：峰值和时间、最近一次读到的权益 / 回撤、看门狗见过的最大回撤、是否已暂停（`"halted": true`）、
暂停时的回撤和权益、每次人工解除的记录（时间、复盘结论）、最近 50 条事件。看门狗每次检查都重新读、写完整个替换（有文件锁，
和 `--reset` 不会互相覆盖）。

**不要手动删它或改它来"解除"**：会丢掉峰值和解除记录。新开一轮时它和数据库一起被移走留档（`docs/forward-test.md` 第 12 节）。

## 4. 触发以后：人工复盘、解除

1. **收到"回撤超过上限，已停止开新仓"**：机器人已经不开新仓、不加仓，持仓还在由 NFI 管。不用慌着做操作。
2. **看现状**：`--status`、出一份前向报告（`forward_report.py`，看持仓表、最差的一笔、回撤(平仓) 和 回撤(含浮亏) 的差距），
   FreqUI 里看每个持仓。复盘看什么、怎么二选一（这一轮结束 / 确认可以接受后继续），见 `docs/forward-test.md` 第 8 节。
3. **决定这一轮结束**：什么都不用解除，按"没通过"记录，把报告发回来一起看。
4. **决定继续**（要写下原因，这一轮的结论要带上这条记录）：

   ```bash
   .venv/bin/python scripts/equity_watchdog.py --reset --note "2026-12-03 复盘：XX 币单边下跌导致，NFI 在减仓，接受，继续"
   ```

   - 只改状态文件，**不会**让机器人恢复开仓。看门狗在跑也没关系（不用停）。
   - 现在的回撤还超过 15% 时，保留原峰值解除会马上再暂停，所以会拒绝，提示你选 `--rebase-peak`：
     把峰值清空，从下一次读到的权益重新算。**注意**：这样账户还能在现在的基础上再亏 15% 才会再次暂停，
     从初始资金算的总亏损可能超过 15%。这正是要你写下复盘结论的原因。
   - 回撤已经回到 15% 以内（例如超限是一下子的插针），不加 `--rebase-peak`，峰值保留。
5. **恢复开新仓**：FreqUI 里点"启动交易"（Start），或者

   ```bash
   .venv/bin/python scripts/equity_watchdog.py --resume       # = POST /api/v1/start；还没 --reset 时会拒绝
   ```

6. 之后的前向报告里，"最大回撤(含浮亏)"仍然是整个期间的最大值（不会因为解除而变小），报告也会读看门狗的状态文件，
   写"看门狗触发过 1 次"——这一轮按标准第 2 条已经算"没通过"。继续跑只是为了看后面的表现，结论里要写清楚。

## 5. 两个回撤为什么会不一样

| | 看门狗 | 前向报告 |
|---|---|---|
| 什么时候算 | 实时，每 60 秒一个瞬间 | 事后，整个期间 |
| 价格 | freqtrade 当时的现价（订单簿） | 每根K线里对持仓最不利的价格（多单最低价、空单最高价） |
| 峰值 | 看门狗运行期间读到的最高权益 | 每根K线收盘时的最高权益 |
| 缺数据 | 连不上的那段时间看不到 | 缺K线的时段按持仓期间最高 / 最低价估（偏保守） |

一般报告的回撤会比看门狗的**稍大**（K线里的最低点看门狗不一定正好碰上）；偶尔也会稍小
（看门狗读到的峰值可能出现在两根K线收盘之间）。两者都不含还没结算的资金费。

所以判定是"**任何一个超了就算超了**"：报告算出来 > 15%，或者看门狗触发过（`docs/forward-test.md` 第 10 节第 2 条），
这一轮都不通过。报告会自动读看门狗的状态文件（和数据库放在一起），把两者合在一起判断：
看门狗现在处于暂停状态时，报告的退出码也是 3。

## 6. 以后上实盘

两个工具对实盘也能用（实盘的配置、密钥、子账户另有文档，这里不展开）：

- 看门狗：`--config` 换成实盘配置、`--env-file` 换成实盘的密钥文件、**必须填 `--start-balance`**（实盘没有 `dry_run_wallet`），
  `--state` 用单独的文件。实盘的钱包余额来自交易所（freqtrade 已经把交易所算在余额里的浮亏去掉，不会重复算）。
- 报告：`--config` 实盘配置、`--db` 实盘数据库、**必须填 `--start-balance`**。
- 账户是单币种保证金、逐仓、单向持仓，和这里的算法一致。

## 7. 自检

```bash
.venv/bin/python scripts/check_forward_tools.py      # 或 make forward-check；不联网，几秒钟
```

用 freqtrade 自己的数据库模型建一个临时的模拟盘数据库（加仓、分批平仓、还没平仓、挂着没成交的单、缺K线），
核对报告的每个数字和手算的一样；再在本机起一个假的 freqtrade 接口，核对看门狗 14% 只预警、15.5% 暂停、峰值存得住、
从不自己恢复、重启后再暂停、连不上不崩、`--reset` / `--resume` 的规则。最后一行"全部通过"才算没问题。

## 8. 文件一览

| 文件 | 是什么 |
|---|---|
| `scripts/forward_report.py` | 前向报告（只读数据库） |
| `scripts/equity_watchdog.py` | 账户权益看门狗 |
| `scripts/check_forward_tools.py` | 上面两个的自检 |
| `scripts/check_drawdown.py` | 回撤上限的唯一定义（`DEFAULT_MAX_DD_PCT`），和权益曲线的算法 |
| `user_data/dryrun/nfi.sqlite` | 模拟盘数据库（报告读它） |
| `user_data/dryrun/nfi-watchdog.json` | 看门狗的状态 |
| `user_data/dryrun/reports/` | cron 每天的报告（第 2.5 节，自己建） |
| `user_data/data/binance/futures/` | `download-data` 下载的K线（报告算浮亏用） |
