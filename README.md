# binance-quant

基于 [freqtrade](https://www.freqtrade.io)（固定版本 **2026.10**）的币安 USDT 本位永续合约量化项目。

目标分三步走：**第一阶段 回测研究 → 第二阶段 模拟盘 + 中文 Telegram 提醒 → 第三阶段 全自动实盘**。
现在处于第一阶段：因为研究服务器在美国（美国 IP 访问币安会被拒），回测先用 **OKX 的 USDT 永续合约数据**。

> 策略说明见 [`docs/strategies.md`](docs/strategies.md)，策略代码在 `user_data/strategies/`。

---

## 风控硬规则

| 规则 | 说明 | 在哪里保证 |
|---|---|---|
| 最大杠杆 **5 倍** | 任何一笔都不能超过 5x | 策略的 `leverage()` 回调统一封顶 5x；`check_drawdown.py` 会检查回测里每笔交易的杠杆 |
| 整体最大回撤 **≤ 8%** | 指**整个账户资金曲线**从最高点到最低点的跌幅，**不是单笔亏损** | `scripts/check_drawdown.py` 对每个回测结果判定 通过 / 未通过 |
| 每笔开多少、止损放哪 | 由各个策略自己决定（仓位管理、止损、保护机制） | 策略代码 |
| 资金 | 现在 1000 USDT，以后可能 10000 USDT | 配置里 `dry_run_wallet`；仓位按可用资金比例分配，会随资金自动放大 |

freqtrade 的配置文件里没有"最大杠杆"这个开关，杠杆只能在策略的 `leverage()` 回调里限制，所以这条规则写在策略里，再由回测检查兜底。

---

## 目录结构

```
binance-quant/
├── README.md                      ← 本文件
├── Makefile                       ← 常用命令快捷方式（make help 查看）
├── requirements.txt               ← freqtrade==2026.10
├── .env.example                   ← 密钥模板，复制成 .env 再填（.env 不会提交到 git）
├── configs/
│   ├── config.base.json           ← 共享基础设置（合约、逐仓、USDT、下单方式…）不单独使用
│   ├── config.okx-backtest.json   ← 【入口】第一阶段：OKX 回测
│   ├── config.binance-futures.json← 【入口】第二/三阶段：币安合约 模拟盘 / 实盘
│   ├── config.okx-pairlist-volume.json ← 【入口】只用来按成交量生成交易对列表
│   ├── config.notify-zh.json      ← 中文 Telegram 通知模板
│   ├── config.synthetic-test.json ← 【入口】合成数据流程自检专用（结果没有意义）
│   ├── pairs.okx-usdt-perp.json   ← OKX 回测用的交易对列表
│   └── pairs.binance-usdt-perp.json ← 币安用的交易对列表（与 OKX 同一批币）
├── scripts/
│   ├── download_okx_data.sh       ← 下载历史数据
│   ├── backtest_all.sh            ← 批量回测（策略 × 周期）+ 自动回撤检查
│   ├── check_drawdown.py          ← 8% 回撤 / 5x 杠杆检查，输出中文表格
│   ├── synthetic_test.sh          ← 用合成数据离线自检整条流程（不联网）
│   ├── make_synthetic_data.py     ← 生成合成测试数据（不是真实行情）
│   ├── ft_offline.py              ← 不联网运行 freqtrade 回测/分析（只配合合成数据）
│   ├── update_okx_pairs.sh        ← 按 OKX 成交额重新生成交易对列表
│   ├── test_notify.py             ← 检查/预览/测试发送中文通知
│   ├── run_bot.sh                 ← 不用 Docker 时启动机器人 / FreqUI
│   ├── preflight.sh               ← 启动前安全检查（自动调用）
│   └── _common.sh                 ← 脚本公用函数
├── docker/
│   ├── docker-compose.yml         ← VPS 和 Mac 通用
│   └── README.md                  ← Docker 补充说明（Mac / VPS）
├── docs/
│   └── strategies.md              ← 策略说明
└── user_data/
    ├── strategies/                ← 策略代码
    ├── data/                      ← 下载的行情数据（不提交）；okx-synthetic/ 是合成测试数据
    ├── backtest_results/          ← 回测结果（不提交）
    └── logs/                      ← 日志（不提交）
```

配置是"拼起来"的：入口配置用 freqtrade 的 `add_config_files` 引入基础配置和交易对列表，**入口文件里写的值优先**。
`.env` 里以 `FREQTRADE__` 开头的环境变量再覆盖配置文件（用来放密钥）。随时可以看最终合并结果：

```bash
make validate                                                    # 校验全部入口配置
.venv/bin/freqtrade show-config --config configs/config.okx-backtest.json   # 查看合并后的完整配置
```

---

## 快速开始

### A. 在 Mac（Apple Silicon，M4 Max）上 —— 推荐 Docker

1. 安装 [Docker Desktop for Mac（Apple 芯片版）](https://docs.docker.com/desktop/setup/install/mac-install/)，打开它。
   建议在 Docker Desktop → Settings → Resources 里给 **至少 8 GB 内存**（1m 数据回测比较吃内存）。
2. 在终端里：

```bash
cd binance-quant
cp .env.example .env            # 回测阶段可以先不填
make pull                       # 拉取 freqtradeorg/freqtrade:2026.10（官方镜像自带 arm64 版本）
make validate FT_RUNNER=docker  # 校验配置
```

3. 下载数据、回测（见下面「下载数据」「回测」两节），命令里加 `FT_RUNNER=docker`，或者不加也行（Mac 上没有 .venv 时会自动用 Docker）。

> 不想用 Docker？Mac 自带的 python3 版本太低（freqtrade 需要 Python ≥ 3.12）。可以 `brew install python@3.12`，然后
> `python3.12 -m venv .venv && .venv/bin/pip install -r requirements.txt`。

更多 Mac 细节见 [`docker/README.md`](docker/README.md)。

### B. 在美国 VPS（Linux）上 —— Docker 或 venv 二选一

**方式 1：venv（简单直接，推荐）**，需要 Python ≥ 3.12（Ubuntu 24.04 自带 3.12）：

```bash
sudo apt update && sudo apt install -y python3-venv git make
cd binance-quant
make venv                       # 创建 .venv，安装 freqtrade==2026.10 和 FreqUI 网页
cp .env.example .env
make validate
```

**方式 2：Docker**：

```bash
curl -fsSL https://get.docker.com | sudo sh     # 安装 Docker（官方脚本）
sudo usermod -aG docker $USER                    # 重新登录后生效
cd binance-quant
cp .env.example .env
sudo chown -R 1000:1000 user_data                # 容器里的用户 uid 是 1000，需要写权限
make pull && make validate FT_RUNNER=docker
```

---

## 下载 OKX 历史数据

```bash
make download                                              # 默认：2024-01-01 至今，1m 5m 15m 1h 4h 1d 全部周期
make download ARGS="--timerange 20230101-"                 # 指定开始日期
make download ARGS="--timeframes '5m 15m 1h 4h 1d'"        # 先跳过 1m（快很多）
make download ARGS="--pairs 'BTC/USDT:USDT ETH/USDT:USDT'" # 只下几个币
# 等价于：bash scripts/download_okx_data.sh --timerange 20230101- ...
```

- 合约模式会**自动**同时下载 `mark`（标记价格）和 `funding_rate`（资金费率），用于计算资金费。
- 已下载的数据再次运行只会**增量补齐**到今天。
- **1m 很慢**：OKX 每次请求只给 100 根 K 线，20 个币 × 2 年的 1m 数据可能要几个小时，建议放在 VPS 上用 `tmux` / `screen` 后台跑。
- 查看已有数据：`.venv/bin/freqtrade list-data --config configs/config.okx-backtest.json --trading-mode futures --show-timerange`
- 交易对列表在 `configs/pairs.okx-usdt-perp.json`（20 个两边都有的主流币）。想按 OKX 当前成交额重选：`make pairs`。

---

## 回测

```bash
make backtest                                                    # 回测全部策略（各用自己的周期）
make backtest ARGS="--strategies 'StratA StratB'"                # 只测指定策略
make backtest ARGS="--timerange 20240101-20250101"               # 指定区间
make backtest ARGS="--timeframes '15m 1h 4h'"                    # 覆盖策略周期，每个周期测一遍（仅适用于支持多周期的策略）
make backtest ARGS="--detail 5m"                                 # 用更小周期模拟 K 线内部走势，止损判断更准（需要已下载该周期数据）
# 等价于：bash scripts/backtest_all.sh ...
```

每次批量回测的结果放在 `user_data/backtest_results/run_<时间>/`：

| 文件 | 内容 |
|---|---|
| `backtest-result-*.zip` | freqtrade 原始结果（含当时的策略代码和配置，可复现）。8% 回撤检查读这里的文件 |
| `logs/<策略>__<周期>.log` | 每次回测的完整输出（含月度收益表） |
| `summary.txt` / `summary.csv` | 回撤检查汇总（csv 可用 Excel 打开） |

FreqUI 的回测历史列表只认 `user_data/backtest_results/` 这一层的文件（不看子目录），所以脚本会把每个
`backtest-result-*.zip`（和同名 `.meta.json`）再复制一份到 `user_data/backtest_results/`，FreqUI 里才能看到。
合成数据（`synthetic/` 目录）的结果不复制。

回测默认开启 `--enable-protections`（策略里写的保护机制在回测中也生效）和 `--cache none`（每次重新计算）。

**建议的研究纪律**：把数据分成"训练段"和"验证段"（例如 2024 全年调参，2025 年只验证不改参数），只有两段都通过才算数。

---

## 看懂 8% 回撤检查

```bash
make check                                                       # 检查最近一次回测
make check ARGS="user_data/backtest_results/run_20251101_120000" # 检查一个批次
.venv/bin/python scripts/check_drawdown.py <目录或zip> --max-dd 8 --max-leverage 5 --min-trades 30
```

请用 `make check` 或 `.venv/bin/python`（Docker 用户 `make check` 会自动用容器里的 python）运行：
这样才有 pandas，能读回测用的K线逐根计算"含浮亏"回撤。用 Mac 自带的 `python3` 也能跑，但"含浮亏"那一列只是估算（标"估"）。

输出示例：

```
账户整体最大回撤上限：8%    杠杆上限：5x
+--------+---------+------+-----------------------+--------+--------+--------+----------+--------+--------+------------+--------------+----------+
| 结论   | 策略    | 周期 | 回测区间              | 交易数 | 胜率   | 总收益 | 盈亏因子 | Sharpe | Calmar | 回撤(平仓) | 回撤(含浮亏) | 最大杠杆 |
+--------+---------+------+-----------------------+--------+--------+--------+----------+--------+--------+------------+--------------+----------+
| 通过   | TrendA  | 1h   | 2024-01-01~2025-01-01 |    120 | 55.00% | 35.00% |     1.40 |   1.20 |   3.40 |      6.00% |  7.50% K线(1h) |       3x |
| 未通过 | MeanRev | 15m  | 2024-01-01~2025-01-01 |     12 | 41.67% | -2.00% |     0.90 |  -0.30 |  -0.20 |      6.00% | 9.10% K线(15m) |       5x |
+--------+---------+------+-----------------------+--------+--------+--------+----------+--------+--------+------------+--------------+----------+

说明：
- MeanRev（15m）
    未通过：最大回撤 9.10% 超过上限 8%
    提醒：交易数只有 12 笔（< 30），统计上不可靠
    提醒：回测期内总收益 <= 0，回撤达标也没有意义
```

| 列 | 含义 |
|---|---|
| 回撤(平仓) | 只按**已平仓**交易累计的账户余额曲线算的最大回撤（取 freqtrade 的 `max_drawdown_account` 与 `max_relative_drawdown` 较大者） |
| 回撤(含浮亏) | 按**账户权益 = 已平仓余额 + 所有持仓的浮动盈亏**算的最大回撤，更接近你在交易所账户里看到的数字。标 `K线(1h)` 表示逐根 K 线计算：每根 K 线按对持仓最不利的价格（多单最低价、空单最高价）算浮亏，日线/4h 策略用 1h K 线算得更细；标 `估` 表示没有 pandas 或找不到 K 线数据时的估算（用每笔交易到过的最差价格，假设同时到达，一般偏大） |
| 结论 | 两种回撤**都 ≤ 8%**、杠杆 **≤ 5x**、且有交易，才算"通过" |
| 盈亏因子 | 总盈利 ÷ 总亏损，> 1 才赚钱，一般希望 > 1.3 |
| Sharpe / Calmar | 风险调整后收益；Calmar = 年化收益 ÷ 最大回撤，越大越好 |

表格下方还会列出"提醒"：交易数太少（< 30 笔统计上不可靠）、回测期总收益 ≤ 0 等。

> 注意：freqtrade 自己报告里的 `Max % of account underwater (balance)`（"wallet based"）在**合约模式下只统计已平仓余额，不含浮亏**
> （已对照 freqtrade 2026.10 源码 `wallets.py` 确认），所以不能拿它当"含浮亏回撤"。以本脚本的"回撤(含浮亏)"为准。
> K 线数据目录默认是 `user_data/data/<交易所>`（例如 `user_data/data/okx`），不在默认位置时加 `--datadir <目录>`。
退出码：0 = 全部通过，1 = 有未通过，3 = 没找到结果（方便以后写自动化）。

> 回测回撤 ≤ 8% **不等于**实盘一定 ≤ 8%（滑点、极端行情、数据差异）。建议回测目标留余量，例如 ≤ 6%。

---

## 用合成数据自检（不联网）

在连不上交易所的机器上（或者改完配置/策略想快速确认"有没有改坏"），可以用**随机生成的合成行情**把整条流程跑一遍：

```bash
make synthetic-test                      # = bash scripts/synthetic_test.sh，约 4 分钟
make synthetic-test ARGS="--quick"       # 跳过第 4 步的未来函数/预热检查（约 2 分钟）
make synthetic-test ARGS="--regen"       # 重新生成合成数据
make guard-test                          # 只跑第 3 步：实盘保护逻辑自检（几秒钟）
```

它会依次做：

1. `scripts/make_synthetic_data.py` 生成 6 个币（BTC ETH SOL XRP DOGE LINK）、约 2 年、5m/15m/1h/4h/1d 的合成 K 线，
   以及最近约 92 天的标记价格和资金费率（模仿 OKX 只有约 3 个月历史），放在 `user_data/data/okx-synthetic/`（约 45 MB，不提交 git）。
   行情里有牛市/熊市/震荡/急跌/急涨切换、波动聚集、日内时段效应和不定期的"巨量砸盘"，保证每个策略都能产生交易。
2. 用 `configs/config.synthetic-test.json` 批量回测全部策略 + 8% 回撤检查，结果放在 `user_data/backtest_results/synthetic/run_<时间>/`。
3. `scripts/check_live_guards.py` 检查只在模拟盘/实盘才会出现、回测测不到的保护逻辑：
   止损没能挂上而价格已越过时立即平仓（`rust_stop_breached` / `rust_stop_invalid`），
   以及日线信号只在 K 线收盘后 10 分钟内入场（详见 `docs/strategies.md` 1.3 和第 8 节第 19 条）。
4. 对 3 个移植策略和 SampleFuturesSanity 跑 freqtrade 的 `lookahead-analysis`（未来函数检查）和 `recursive-analysis`（指标预热检查），
   日志和 CSV 放在同一目录的 `analysis/` 里。

**合成数据的收益、回撤、胜率没有任何意义**，只看"每一步有没有报错"。回撤检查"未通过"在合成数据上是正常的。

几点说明：

- **为什么要 `scripts/ft_offline.py`**：freqtrade 哪怕只是回测，也要先联网下载交易对规格和杠杆档位。
  `ft_offline.py` 把这两样换成合成数据目录里的假规格、禁止一切交易所网络请求，然后照常运行 freqtrade。
  它只允许回测/分析类命令，而且数据目录必须有合成数据标记文件，不能拿来"假装"连上交易所。真实回测不需要它。
- **为什么合成数据单独放在 `okx-synthetic`**：`freqtrade download-data` 会在已有文件末尾增量追加，
  如果合成数据和真实数据放在同一个目录，真假数据会混在一起。生成脚本也拒绝往 `user_data/data/okx` 写。
- **lookahead-analysis 对两个日线策略报 `mtf_bias_4h`、`mtf_bias_1h` 等指标列"有偏差"是误报**：
  这个工具截断数据时会保留"刚开盘、还没收盘"的那根日线，而那一天的 4h/1h K 线只截到了开头几根，
  所以那一行的多周期偏向和完整数据不同。实际回测和实盘里，日线收盘后才会用这一行，那时当天的 4h/1h K 线都已收盘。
  判断标准是 `biased_entry_signals`（入场信号偏差）和 `biased_exit_signals`（离场信号偏差）都为 0。
  另外用"只保留当时已经收盘的 K 线重新计算"的方法单独验证过，三个策略抽查的行全部一致。
- **recursive-analysis 里 `ema200` 有几个百分点的差异是正常的**：EMA200 要几百根 K 线才能"忘掉"起点，
  `startup_candle_count = 300` 时还残留约 5%（0.99^300）的起点影响。回测从全部历史开始算，实盘只用最近约 600 根（OKX）/ 800 根（币安），
  所以实盘和回测的 EMA200 会有很小的差别，个别贴近均线的信号可能不一样。这不是未来函数。

---

## 用 FreqUI 网页看回测结果（含 SSH 隧道）

FreqUI 是 freqtrade 自带的网页界面（目前是英文；汉化是以后单独做的任务）。

1. 先在 `.env` 里填好 `FREQTRADE__API_SERVER__USERNAME`、`FREQTRADE__API_SERVER__PASSWORD`（≥10 位）、
   `FREQTRADE__API_SERVER__JWT_SECRET_KEY`、`FREQTRADE__API_SERVER__WS_TOKEN`（后两个用
   `python3 -c "import secrets; print(secrets.token_urlsafe(48))"` 生成）。没填的话会拒绝启动。
2. 在 VPS 上启动：`make ui`（Docker）或 `make ui-venv`（venv，前台运行）。
3. 服务只监听 VPS 的 `127.0.0.1:8080`，公网访问不到（**不要**在防火墙/安全组里开放 8080）。在你的 Mac 上开一个 SSH 隧道：

```bash
ssh -L 8080:127.0.0.1:8080 你的用户名@你的VPS地址
```

4. 保持这个 SSH 窗口不关，在 Mac 浏览器打开 <http://127.0.0.1:8080>，用 `.env` 里的用户名密码登录，
   进入 **Backtest** 页面，在回测历史列表里打开结果，看资金曲线、每笔交易。
   列表里只有 `user_data/backtest_results/` 这一层的文件：`make backtest` 会自动复制过去；
   自己用 `freqtrade backtesting --backtest-directory 别的目录` 跑的结果、以及合成数据的结果，这里看不到。

`ui`（看回测）和 `freqtrade`（交易机器人）都用 8080 端口，同一台机器上不能同时开。

---

## Telegram 中文通知

freqtrade 自带的 Telegram 通知是英文的。本项目用 freqtrade 的 **webhook** 功能，按 `configs/config.notify-zh.json`
里的中文模板，直接调用 Telegram Bot API 发消息：开仓下单 / 开仓成交 / 开仓撤单 / 平仓下单 / 平仓成交 / 平仓撤单 /
机器人状态 / 启动 / 警告 / 异常 / 保护机制触发 / 强平预警 / 策略自定义消息。

### 设置步骤

1. **创建机器人**：Telegram 搜索 **@BotFather** → 发送 `/newbot` → 按提示起名 → 得到 token（形如 `123456789:AAH...`）。
2. **先给你的机器人发一条消息**（随便发，比如 `/start`），否则它没法给你发消息。
3. **拿 chat id**：浏览器打开 `https://api.telegram.org/bot<你的token>/getUpdates`，找到 `"chat":{"id":123456789,...}`，
   这个数字就是 chat id（如果是群组，id 是负数，要带负号）。
   要在**启动机器人之前**做这一步：freqtrade 的命令机器人运行时会把消息取走，getUpdates 就看不到了。
4. 填到 `.env`：

```bash
FREQTRADE__TELEGRAM__TOKEN=123456789:AAH...
FREQTRADE__TELEGRAM__CHAT_ID=123456789
FREQTRADE__WEBHOOK__ENABLED=true      # 中文交易通知
FREQTRADE__TELEGRAM__ENABLED=true     # 可选：英文命令机器人（/status /profit /balance /stop 等）
```

5. 测试：`make notify-test`（检查模板 + 预览），`make notify-send`（真的发一条测试消息到你的 Telegram）。

说明：

- token 和 chat id **只放在 `.env`**。启动时 `scripts/preflight.sh` 自动拼出
  `https://api.telegram.org/bot<token>/sendMessage?chat_id=<chat_id>` 交给 freqtrade（环境变量 `FREQTRADE__WEBHOOK__URL`）。
- 自带的 Telegram 命令机器人只用来**接收命令**，它的**回复是英文**（freqtrade 不支持翻译）。它自己的交易通知已经全部关掉，所以不会和中文通知重复。
- 改模板：编辑 `configs/config.notify-zh.json`，`{...}` 是 freqtrade 提供的占位符，改完运行 `make notify-test` 检查。
- 日志里如果出现 webhook 报错，可能带有 token，分享日志前请先删掉 token。

### 通知里的英文代码对照

模板只能原样输出 freqtrade 的字段值，下面这些会以英文出现：

| 字段 | 英文值 → 中文含义 |
|---|---|
| 方向 | `Long` = 做多，`Short` = 做空 |
| 离场原因 | `roi` = 达到止盈目标，`stop_loss` = 止损，`stoploss_on_exchange` = 交易所止损单触发，`trailing_stop_loss` = 移动止损，`exit_signal` = 策略离场信号，`force_exit` = 手动/强制平仓，`emergency_exit` = 紧急平仓，`liquidation` = 被强平，`partial_exit` = 部分平仓，其他 = 策略自定义原因 |
| 订单类型 | `limit` = 限价单，`market` = 市价单 |
| 保证金模式 | `isolated` = 逐仓，`cross` = 全仓 |
| 状态 / 警告 | 机器人自带的英文句子，例如 `running` = 运行中，`stopped` = 已停止 |

所有时间都是 **UTC**（北京时间 = UTC + 8 小时）。如果策略想发完全中文的消息，可以在策略里调用 `self.dp.send_msg("中文内容")`，会以【策略消息】发出。

---

## OKX 数据 vs 币安：已知差异（必读）

| 差异 | 影响 | 处理 |
|---|---|---|
| OKX 的 mark 价格和资金费率历史**只有最近约 3 个月** | 3 个月以前的回测，资金费按 0 计算（配置里 `futures_funding_rate: 0`），结果有偏差 | 持仓时间长的策略要特别留意；最终以币安数据为准 |
| 交易对不完全相同 | OKX 有的币币安没有（反之亦然），个别币名字不同（如币安的 `1000PEPE`） | 交易对列表只选了两边都有、名字相同的 20 个主流币 |
| 价格、成交量、插针、手续费不同 | 同一策略在两个交易所结果会有差别 | 不要只看 OKX 结果就上实盘 |
| 杠杆档位 / 维持保证金率不同 | 强平价计算不同 | 5x 以内影响较小 |

**上实盘之前，必须在币安允许访问的地区的服务器上，用币安数据重新下载、重新回测并通过 8% 检查：**

```bash
bash scripts/download_okx_data.sh --config configs/config.binance-futures.json --timerange 20240101-
bash scripts/backtest_all.sh   --config configs/config.binance-futures.json
```

（脚本名里虽然是 okx，但换成币安配置就是下载/回测币安数据。）

> 美国 IP 访问币安会返回 HTTP 451（Bybit 返回 403），这是交易所的合规限制。**不要用代理 / VPN 绕过**，
> 这违反交易所条款，账户可能被冻结、资金可能被限制。请使用位于币安允许地区的服务器。

---

## 以后上币安（第二、三阶段）

### 币安账户要求

- 账户所在地区和服务器所在地区都必须是币安允许的。
- 合约设置里：**持仓模式 = 单向持仓（One-way Mode）**，**资产模式 = 单币种保证金（Single-Asset Mode）**。freqtrade 启动时会检查，不对会报错（它不会替你改）。
- **API Key**：只开「允许合约」，**绝对不要开「允许提现」**；开启「限制 IP 访问」，只填服务器的公网 IP。
- 遵守币安合约《量化交易规则》（下单太频繁、撤单率太高、单笔太小都可能被限制交易）。

### 运行模拟盘（第二阶段）

在 `.env` 里设 `FREQTRADE__STRATEGY=策略类名`，填好 FreqUI 登录信息和 Telegram，然后：

```bash
make up          # Docker：启动机器人（默认 dry-run 模拟盘，模拟资金 1000 USDT）
make logs        # 看日志
make down        # 停止
# 或 venv：bash scripts/run_bot.sh
```

模拟盘会用币安实时行情，按策略"假装"下单，并发中文 Telegram 通知 —— 这就是第二阶段的"提醒"。
建议至少跑 2–4 周，对比模拟盘和回测的表现。

### 上实盘前检查清单（第三阶段）

- [ ] 策略在**币安数据**上回测通过：两种回撤都 ≤ 8%、杠杆 ≤ 5x、交易数足够、样本外区间也通过
- [ ] 模拟盘跑了至少 2–4 周，结果和回测大致一致
- [ ] 币安账户：单向持仓 + 单币种保证金；API Key 无提现权限 + IP 白名单
- [ ] `.env`：`FREQTRADE__EXCHANGE__KEY`、`FREQTRADE__EXCHANGE__SECRET` 已填；`FREQTRADE__DRY_RUN=false`
  （**只通过 .env 切换实盘**，不要改配置文件里的 `dry_run`，这样模拟盘和实盘的数据库会自动分开。
  只写小写 `true` / `false`；写成 `0`、`no`、留空等，启动检查会拒绝启动）
- [ ] Telegram 中文通知能正常收到；会用 `/stop` 命令紧急停机
- [ ] 先用小资金（1000 USDT）跑，稳定后再考虑加到 10000 USDT

实盘配置已默认：逐仓、交易所挂止损单（按标记价格触发，机器人掉线也能止损）、订单簿取价。

---

## 路线图

| 阶段 | 内容 | 在哪运行 | 状态 |
|---|---|---|---|
| 1 回测研究 | OKX 数据回测，筛选满足"回撤 ≤ 8%、杠杆 ≤ 5x"的策略 | 美国 VPS / Mac | **进行中** |
| 1.5 币安复核 | 用币安数据重新回测确认 | 币安允许地区的服务器 | 待开始 |
| 2 模拟盘 + 提醒 | 币安 dry-run + 中文 Telegram 通知 | 币安允许地区的服务器 | 待开始 |
| 3 自动实盘 | 小资金实盘，稳定后加资金 | 币安允许地区的服务器 | 待开始 |
| 另 | FreqUI 网页汉化 | — | 以后单独做 |

---

## 常见问题

- **`Permission denied`（Docker）**：`sudo chown -R 1000:1000 user_data`。
- **回测提示 `No data found`**：该周期/区间的数据没下载，先 `make download`；用 `--detail` 时，对应的小周期也要下载。
- **启动提示「启动检查 错误」**：按提示在 `.env` 里补上对应变量。
- **8080 端口被占用**：`ui` 和 `freqtrade` 不能同时运行，先 `make down`。
- **升级 freqtrade**：同时改 `requirements.txt` 和 `docker/docker-compose.yml` 里的版本号，看官方更新日志后再升级，升级后重新跑一遍 `make validate` 和回测。
