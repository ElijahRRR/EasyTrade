# 币安模拟盘前向测试：冻结版 NFI X8，1 倍杠杆（第 4 轮）

> 一句话：把**固定版本**的 NostalgiaForInfinity X8，用 **1 倍杠杆**（`NostalgiaForInfinityX8Lev1`）在**币安 U 本位永续**上
> 跑 **2 ~ 3 个月模拟盘**（paper trading，用实时行情"假装"下单，不动真钱），用作者**没见过的**行情检验它。
> 账户权益（含浮亏）回撤超过 **15%** 就停止开新仓、等人工检查。

在**币安允许访问的地区**的服务器上（你那台跑 Rust 引擎的 Vultr 服务器；美国 VPS 不行，见第 2 节），建议先在 tmux 里试一遍：

```bash
# 第一次（这台服务器上还没有 EasyTrade 时；详见第 6 节）：
sudo apt update && sudo apt install -y python3-venv git make curl
git clone 你的仓库地址 ~/easytrade              # 和美国 VPS 上的是同一个仓库（在那边 git remote get-url origin 能看到地址）
# 以后每次从这里开始：
cd ~/easytrade && git pull
make venv                                  # 第一次：建 .venv、装固定版本的 freqtrade（已经有就跳过）
bash scripts/fetch_benchmarks.sh           # 下载固定版本的 NFI，核对 sha256（几秒钟）
# 建密钥文件 .env.dryrun-nfi（权限 600），见第 5 节
bash scripts/run_dryrun.sh nfi --check-only    # 只检查、不启动：所有问题一次列出来
bash scripts/run_dryrun.sh nfi                 # 前台试跑；看到开始拉K线、没有报错后 Ctrl+C
# 然后装成 systemd 服务长期跑（机器人 + 看门狗），见第 7 节
```

---

## 1. 这个测试能说明什么、不能说明什么

**为什么要做**：第 3 轮的回测和第 4 轮的逐年回测（[`docs/nfi-yearly.md`](nfi-yearly.md)）都在 NFI 作者可能**看过**的数据里——
NFI X8 这个文件 2026-09-06 才建，到 2026-11-07 已经提交了约 1223 次（其中 488 次的标题是 "add protection"）。
**2026-11 以后的行情，作者也没见过**：这才是真正的样本外。我们把代码冻结在 commit `74c03087b8ced3899509ba8d36f5f1565bc5f49a`（v18.0.214），
之后不管上游怎么改，这一轮都只跑这一份。

**能说明的**：

- 冻结的 NFI 在 1 倍杠杆下，这 2 ~ 3 个月在币安上赚不赚钱、账户回撤（含浮亏）多大；
- 和回测比：交易频率、胜率、持仓时间、加仓次数是不是差不多（差得很远 = 回测和实盘的条件不一样，要查原因）；
- 整条链路能不能稳定跑几个月：取价、下单逻辑、中文通知、看门狗、重启后接着跑。

**不能说明的**（一定要心里有数）：

1. **样本很小**：2 ~ 3 个月大约只有 **20 ~ 40 笔**平仓交易（第 3 轮 3 倍杠杆、20 个币，8 个月样本外 35 笔；这次 58 个币、1 倍）。
   几十笔交易的胜率、盈亏因子误差很大。
2. **证明不了尾部安全**：除非这段时间正好碰上一次大跌（像 2022 年那样），否则"扛得住暴跌"是测不出来的。
   **没出事 ≠ 安全**。2022 年熊市只能靠逐年回测看（而且那也是样本内）。
3. **NFI 拿着亏损的仓位不平**：第 3 轮样本外只看平仓的回撤 1.92%、含浮亏 9.28%；胜率 97% 是因为亏的仓位一直拿着、等反弹或加仓摊平。
   所以这里一律看**账户权益（已平仓余额 + 持仓浮动盈亏）**；测试结束时还开着的仓位，浮亏要算进结果。
4. **模拟盘 ≠ 实盘**：freqtrade 按订单簿模拟成交（限价单碰到对手盘就算成交，不排队、只看前 20 档深度；手续费按币安的费率估算），
   成交比实盘乐观，小币尤其如此。资金费按币安真实的费率算。
5. **1000 USDT 的模拟资金**：币安最小下单额（名义价值约 5 USDT）会挡掉一部分 NFI 的小额加仓；
   以后换成 10000 USDT，加仓的样子会不一样——要**新开一轮**（第 12 节），不能接着这一轮的数字算。

所以："通过"只是"可以考虑用小资金实盘"的**必要条件之一**，不是充分条件。

## 2. 在哪跑

- **只能在币安允许访问的地区的服务器上跑**：你那台跑 Rust 引擎的 Vultr 服务器就可以。
- **美国 VPS 不行**：币安对美国 IP 返回 HTTP 451。美国 VPS 继续做 OKX 回测（`make research-nfi-yearly` 等）。
- `scripts/run_dryrun.sh` 每次启动前都会访问币安的公开接口（`/fapi/v1/ping`、`/api/v3/ping`，不需要 API Key）：
  - 返回 HTTP 451 / 403：说明这台服务器所在的地区不能用币安，脚本直接停下（退出码 21），请换到币安允许的地区的服务器；
  - 连不上（网络、DNS、防火墙）：脚本停下（退出码 23），过一会儿再试。
- 和 Rust 引擎放在同一台服务器上没问题：目录、数据库、端口都是分开的（FreqUI 只监听 `127.0.0.1:8081`）。
  注意内存和 CPU（第 3 节）。

## 3. 要多少资源（估算）

| | 大概 |
|---|---|
| 内存 | freqtrade + NFI，58 个币 × 5 个周期（5m / 15m / 1h / 4h / 1d）× 几百列指标：估计 **1 ~ 2 GB**。脚本要求启动时可用内存 ≥ 1.5 GB（`EASYTRADE_DRYRUN_MIN_MEM_MB` 可改），低于 3 GB 会提醒 |
| CPU | 每根 5 分钟K线收盘后把所有币算一遍；1 核可能要一两分钟才开仓，建议 **2 核以上** |
| 硬盘 | 数据库 2 ~ 3 个月几 MB 到几十 MB；日志自动滚动（10 MB × 11 个，最多约 110 MB）；冻结的策略代码约 3.5 MB。**不下载历史K线**（模拟盘每次启动从交易所拉最近的K线）。脚本要求剩余 ≥ 1 GB |
| 网络 | 每 5 秒一轮，拉K线和订单簿；限速 freqtrade 自己控制。每次启动（包括重启）都要先拉每个币每个周期 800 根K线，**几分钟后**才开始正常运行 |

## 4. 配置：`configs/config.dryrun-nfi.json`

| 设置 | 值 | 为什么 |
|---|---|---|
| 交易所 / 模式 | 币安、U 本位永续（futures）、逐仓（isolated）、USDT | 和以后实盘一样。实盘时币安账户要设成单向持仓 + 单币种保证金（README「币安账户要求」）；模拟盘不碰真账户，不检查这个 |
| 模拟盘 | `dry_run: true`，启动时还加 freqtrade 的 `--dry-run` | 双保险：环境变量把 `dry_run` 改成 false 时，启动前检查直接拒绝（退出码 20） |
| 模拟资金 | 1000 USDT（`dry_run_wallet`） | 现在的资金；以后 10000 USDT = 新开一轮 |
| 策略 | `NostalgiaForInfinityX8Lev1` | NFI 固定版本 + 我们的封顶子类，**每一条杠杆路径都封顶 1 倍**（`user_data/strategies/benchmarks/BenchmarkCaps.py`、[`docs/nfi-yearly.md`](nfi-yearly.md) 第 3、4 节） |
| 同时持仓 | 6 个，stake = unlimited，`tradable_balance_ratio` 0.95 | NFI 推荐 6 ~ 12 个；0.95 和回测相同（NFI 的例子是 0.99） |
| NFI 要求的离场设置 | `use_exit_signal` true、`exit_profit_only` false、`ignore_roi_if_entry_signal` true；`timeframe` 5m | NFI README / 策略文件开头的要求 |
| 限价单超时 | 开仓 3 分钟、平仓 2 分钟没成交就撤 | 照 NFI 的 exampleconfig |
| 下单方式 | 全部限价单；止损不挂在交易所 | 照 NFI 的 exampleconfig（NFI 的名义止损 -99%，靠策略内部规则平仓 / 减仓） |
| 取价 | `price_side: other` + 订单簿（买看卖一、卖看买一） | 照 NFI 的币安例子（exampleconfig_secret.json）；币安合约的 ticker 没有价格，freqtrade 要求用订单簿 |
| 交易对 | `pairs.binance-research.json` 的 60 个，去掉 STX、BLUR = **58 个**，固定列表（StaticPairList） | STX、BLUR 在 NFI 固定版本的 `configs/blacklist-binance.json` 里（其余 58 个按固定版本核对过，都不在）。NFI 推荐 40 ~ 80 个按成交量的动态列表；我们用固定列表，整个测试期间币不变、结果能复现。前 20 个就是逐年回测用的那 20 个币，多出来的币让交易多一些 |
| 强制开仓 | `force_entry_enable: false` | FreqUI / Telegram 的强制开仓可以自带杠杆参数，绕过 1 倍封顶 |
| 数据库 / 日志 | `user_data/dryrun/nfi.sqlite`、`user_data/dryrun/nfi.log` | 自己的，不和别的机器人混 |
| FreqUI | `127.0.0.1:8081` | 只听本机，SSH 隧道看（第 6 节）。**必须开着**：账户权益看门狗通过它读权益、暂停开新仓（`FREQTRADE__API_SERVER__ENABLED=false` 会拒绝启动）。8081 和 `config.binance-xsmomentum.json` 相同，两个不要同时跑 |
| 通知 | `config.notify-zh.json`（中文 webhook 通知，默认关闭） | 和第 2 阶段一样 |
| **没写的** | 任何杠杆设置（`futures_mode_leverage*`）、`nfi_parameters`、止损 / ROI / 追踪止损、`position_adjustment_enable` 等 | NFI 要求不要覆盖它的设置；写了 `scripts/check_live_guards.py` 会拒绝启动（杠杆设置写了也会被封顶到 1 倍） |

每次启动前，`scripts/check_live_guards.py --dryrun-config`（`make guard-test` 用同一套规则做回归检查）把**合并后的配置**
（文件 + 环境变量，和 freqtrade 读到的一样）查一遍：模拟盘、币安合约逐仓、策略是 1 倍版本（读 `BenchmarkCaps.py` 源码确认封顶 = 1、
项目上限 = 5）、FreqUI / REST API 开着且只听 127.0.0.1（看门狗靠它暂停开新仓，关掉就拒绝启动）、NFI 要求的设置没被改、数据库在 `user_data/dryrun/`、配置文件里没有密钥 / Telegram Token / 服务器 IP，
最后再跑一遍 freqtrade 自己的配置校验。

## 5. 密钥文件 `.env.dryrun-nfi`

模拟盘**不需要币安 API Key**。这个文件只放 FreqUI 登录信息和（可选的）Telegram，权限必须是 600（否则拒绝启动）。
`.env.*` 已经在 `.gitignore` 里，不会提交。在仓库目录里：

```bash
umask 077
cat > .env.dryrun-nfi <<'EOF'
# 币安模拟盘（NFI 1 倍）的密钥。不要放币安 API Key。等号两边不要有空格；有特殊字符的值用单引号括起来。
FREQTRADE__API_SERVER__USERNAME=
FREQTRADE__API_SERVER__PASSWORD=
FREQTRADE__API_SERVER__JWT_SECRET_KEY=
FREQTRADE__API_SERVER__WS_TOKEN=
# 中文通知（可选）：改成 true，并填 Telegram 的 token 和 chat id（获取方法见 .env.example）
FREQTRADE__WEBHOOK__ENABLED=false
FREQTRADE__TELEGRAM__TOKEN=
FREQTRADE__TELEGRAM__CHAT_ID=
# freqtrade 自带的 Telegram 命令机器人：要用一个只给这个模拟盘用的 Token，不能和别的机器人共用（会互相抢消息）
FREQTRADE__TELEGRAM__ENABLED=false
EOF
chmod 600 .env.dryrun-nfi
nano .env.dryrun-nfi
```

- 用户名自己定；密码至少 10 位；`JWT_SECRET_KEY`、`WS_TOKEN` 各用一次 `python3 -c "import secrets; print(secrets.token_urlsafe(48))"` 生成。
- 中文通知只是往 Telegram 发消息，可以和别的程序共用同一个 Telegram 机器人；只有 freqtrade 自带的**命令**机器人必须用自己的 Token。
- 这个文件里如果有 `FREQTRADE__EXCHANGE__KEY / SECRET`、`FREQTRADE__STRATEGY`、`FREQTRADE__DB_URL`，脚本会去掉并提示（策略和数据库以配置文件为准）；
  有 `FREQTRADE__DRY_RUN=false` 会**拒绝启动**。

## 6. 第一次启动、日常查看

1. **第一次在这台服务器上用 EasyTrade**（它现在只跑你的 Rust 引擎，多半还没有这个仓库）：

   ```bash
   sudo apt update && sudo apt install -y python3-venv git make curl
   python3 --version                        # 要 >= 3.12（Ubuntu 24.04 自带 3.12；更老的系统先装 3.12）
   git clone 你的仓库地址 ~/easytrade          # 和美国 VPS 上的是同一个仓库；私有仓库要先在这台服务器上配好 GitHub 的 SSH key 或 token
   cd ~/easytrade
   make venv                                # 建 .venv、装固定版本的 freqtrade（几分钟）
   ```

   目录放哪里都行，这份文档假设是 `~/easytrade`。换了目录的话：systemd 的安装命令（第 7 节）是在仓库目录里运行、自动用当前目录，
   不用改；`docs/forward-monitoring.md` 里 cron 那一行和 `systemctl edit` 例子里的路径要跟着改。

   **以后每次**：`cd ~/easytrade && git pull`；没有 `.venv` 就 `make venv`（Python ≥ 3.12）。
2. `bash scripts/fetch_benchmarks.sh`：按固定 commit 下载 NFI、核对 sha256（不下载任何自动更新工具）。
3. 建 `.env.dryrun-nfi`（第 5 节）。
4. `bash scripts/run_dryrun.sh nfi --check-only`：每一项都检查，最后汇总。全部"通过"才往下走。
5. `bash scripts/run_dryrun.sh nfi`：前台试跑。第一次正式启动时会把 `BenchmarkCaps.py` 和 NFI 文件**复制到 `user_data/dryrun/nfi-strategy/`**
   并记下 sha256（`FROZEN.txt`）——这一轮模拟盘从此只用这份副本。看到 freqtrade 开始拉K线、没有报错，Ctrl+C 停掉。
6. 装成 systemd 服务（第 7 节），以后就不用管它了。

**在 FreqUI 里看**（机器人只听服务器本机的 8081 端口，公网访问不到；**不要**在防火墙 / 安全组里开放 8081）。在你的 Mac 上开一个 SSH 隧道：

```bash
ssh -N -L 8081:127.0.0.1:8081 你的用户名@你的Vultr服务器地址
```

保持这个窗口不关，浏览器打开 <http://127.0.0.1:8081>，用 `.env.dryrun-nfi` 里的用户名密码登录。
可以看持仓、每笔交易、收益曲线、日志。**只看，不要动手**：不要强制开仓 / 平仓、不要改设置（第 8 节）。

其他：

```bash
tail -f user_data/dryrun/nfi.log            # freqtrade 完整日志
cat user_data/dryrun/nfi-starts.log         # 每次启动一行（时间、仓库 commit、冻结副本的时间）
cat user_data/dryrun/nfi-strategy/FROZEN.txt   # 这一轮跑的是哪份代码
bash scripts/run_dryrun.sh nfi --check-only # 随时可以再查一遍（不影响正在跑的机器人）
```

## 7. 用 systemd 长期运行（机器人 + 看门狗）

模板文件：[`docs/systemd/easytrade-dryrun@.service`](systemd/easytrade-dryrun@.service)。一个模板、两个实例：

| 实例 | 做什么 |
|---|---|
| `easytrade-dryrun@nfi` | 模拟盘机器人：`bash scripts/run_dryrun.sh nfi`（每次启动先做全部检查，不通过就不启动） |
| `easytrade-dryrun@nfi-watchdog` | 账户权益看门狗：`bash scripts/run_dryrun.sh nfi-watchdog`（= `scripts/equity_watchdog.py`，见第 8 节） |

安装（在仓库目录里运行；用平时登录的普通用户，不要用 root 跑机器人）：

```bash
sed -e "s#__USER__#$(id -un)#g" -e "s#__DIR__#$(pwd)#g" docs/systemd/easytrade-dryrun@.service \
    | sudo tee /etc/systemd/system/easytrade-dryrun@.service > /dev/null
sudo systemctl daemon-reload
sudo systemctl enable --now easytrade-dryrun@nfi
sudo systemctl enable --now easytrade-dryrun@nfi-watchdog
```

```bash
systemctl status easytrade-dryrun@nfi              # 在不在跑；没跑起来时最后几行就是原因
journalctl -u easytrade-dryrun@nfi -n 100          # 启动检查的输出、freqtrade 日志
sudo systemctl restart easytrade-dryrun@nfi        # 重启：还是这一轮（同一个数据库、同一份冻结的代码），持仓接着管
sudo systemctl stop easytrade-dryrun@nfi easytrade-dryrun@nfi-watchdog   # 停止
```

- 服务器重启后会自动启动。密钥从 `.env.dryrun-nfi` 读（权限 600；两个实例共用这一个文件）。
- 自动重启规则：启动前检查不通过（退出码 20 安全检查 / 21 地区不能用币安 / 22 内存硬盘不够）**不重启**，要人来看；
  连不上币安（23）或 freqtrade 出错退出：60 秒后重启，1 小时内最多 5 次。
- `git pull` 以后不需要重启；重启也不会换成新代码（冻结副本），只会提示"仓库里的 BenchmarkCaps.py 已经更新过"。

## 8. 停止规则（15% 回撤上限）

项目硬规则：**账户权益回撤（含浮亏）≤ 15%**（唯一的定义在 `scripts/check_drawdown.py` 的 `DEFAULT_MAX_DD_PCT`）。

- **账户权益看门狗** [`scripts/equity_watchdog.py`](../scripts/equity_watchdog.py)（systemd 实例 `easytrade-dryrun@nfi-watchdog`）：
  定时通过 FreqUI 的接口（127.0.0.1:8081）读这个模拟盘的账户权益（钱包余额 + 持仓浮动盈亏），记下最高点；
  回撤**超过 15%** 时让机器人**停止开新仓**（freqtrade 的 paused 状态：不开新仓、也不给已有持仓加仓，已有持仓照常由 NFI 减仓 / 平仓）。
  它**不会自己恢复**：要你人工复盘以后手动解除。机器人中途重启时，`scripts/run_dryrun.sh` 看到看门狗记的"已暂停"
  （`user_data/dryrun/nfi-watchdog.json`），也会让机器人以 paused 状态启动。
  怎么看状态、复盘后怎么解除（`--status`、`--reset --note "…"`、`--resume`）：它开头的说明和 [`docs/forward-monitoring.md`](forward-monitoring.md)。
- **人工检查看什么**：是一两个币单边行情拖的、还是普遍在亏；NFI 是在加仓摊平还是在减仓；最大的那笔浮亏占资金多少；
  有没有异常（杠杆不是 1 倍、同一个币反复开仓、日志里大量报错）。然后二选一：
  1. 这一轮到此结束，按"没通过"记录（第 10 节）；
  2. 你确认是可以接受的情况，记下原因和时间，再让它恢复开新仓——**这一轮的结论要带上这条记录**。
- **出现这些情况也要立即停下来看**：任何一笔交易的杠杆不是 1 倍（说明封顶没生效）；机器人反复崩溃；启动检查报冻结副本被改过。
- **测试期间不要动手干预**，否则测的就不是 NFI 了：不要强制开仓 / 平仓、不要建 NFI 的"手动持有"文件（`user_data/nfi-hold-trades.json`，
  脚本发现就拒绝启动）、不要改配置、不要换代码。必须干预时记下时间和原因。重启（同一个数据库）没关系。

## 9. 前向报告

[`scripts/forward_report.py`](../scripts/forward_report.py)：只读打开这个模拟盘的数据库，出中文的前向报告
（跑了多少天、交易数、收益、盈亏因子、**账户权益回撤（含浮亏）**、最差的一笔、现在的持仓等），对照第 10 节的标准。
建议每周看一次，测试结束时出最终报告。用法（要先下载哪些K线算浮亏）见它开头的说明和 [`docs/forward-monitoring.md`](forward-monitoring.md)。

看报告时注意：NFI 会加仓 / 分批平仓，数据库里每笔交易记的数量和均价是**最后的**值，持仓中间的浮亏要按逐笔订单重建才准
（和回测的回撤检查同一个道理，见 `docs/benchmarks.md` 第 7 节第 1 点）。还开着的仓位一定要算浮亏，NFI 的"胜率"只看平仓会好看得多。

## 10. 跑完怎么判断（成功标准）

**全部满足**才算这一轮"通过"（第 1 ~ 4 条 `scripts/forward_report.py` 自动判断；第 5、6 条报告最后的"其他检查"也会列出来（参考用，要你确认）；第 7 条看下面的文件自己确认）：

| # | 标准 | 说明 |
|---|---|---|
| 1 | 跑满 **≥ 60 天**，并且 **≥ 30 笔**平仓交易 | 60 天时不到 30 笔：延长到 90 天；90 天还不到 30 笔 = **样本不够、没有结论**（不算通过） |
| 2 | 整个期间账户权益回撤（含浮亏）**≤ 15%** | 看门狗一次都没触发过 |
| 3 | 总收益 **> 0** | 结束时还开着的仓位按当时的价格算浮亏 / 浮盈 |
| 4 | 盈亏因子（总盈利 ÷ 总亏损）**≥ 1.2** | 和回测的通过标准相同 |
| 5 | 没有任何一笔交易亏损超过初始资金的 **5%**（1000 USDT 时 = 50 USDT） | 含加仓以后的总亏损；还开着的仓位按浮亏算。三笔这样的亏损就到 15% 上限了 |
| 6 | 所有交易的杠杆都是 **1 倍** | 不是 1 倍 = 封顶有问题，这一轮作废 |
| 7 | 整个期间跑的是**同一份冻结代码**、没有手动干预（或者干预都有记录、你认可） | `FROZEN.txt` 和 `nfi-starts.log` 里只有一个冻结时间 |

通过了也**不是直接上 10000 USDT 实盘**：下一步是你看过报告以后，决定要不要用小资金（1000 USDT）、同样的看门狗上实盘；
实盘要单独的配置、币安子账户和 API Key（不在这份文档里）。没通过就把报告发回来一起看原因。

## 11. 冻结版本：NFI 不会自动更新

- NFI 固定在 commit `74c03087b8ced3899509ba8d36f5f1565bc5f49a`（v18.0.214），`scripts/fetch_benchmarks.sh` 按这个 commit 下载、核对 sha256。
- 每次启动都核对：`vendor/` 里的 NFI 是固定版本；冻结副本 `user_data/dryrun/nfi-strategy/` 和 `FROZEN.txt` 里记的 sha256 一样（不一样就拒绝启动）。
- **绝对不要装 NFI 的自动更新工具**：Docker 的 `nfi-updater` 服务、`tools/checkupdates.sh`、任何 cron 定时更新。
  它们会在没人看过的情况下换掉策略和黑名单，这一轮就不是在测同一个东西了。`fetch_benchmarks.sh` 也不会下载它们。
- NFI 的黑名单也不更新：用的是固定版本那时的判断（去掉 STX、BLUR）。
- 想测更新的 NFI：那是**新的一轮**（结果不能和这一轮混在一起），先改 `fetch_benchmarks.sh` 的固定版本（`docs/benchmarks.md` 第 9 节），再按第 12 节重新开始。

## 12. 重新开始一轮

这些情况要新开一轮：资金改成 10000 USDT（改 `dry_run_wallet`）、换 NFI 版本、`BenchmarkCaps.py` 改过并且要用新版本、中途有过不能接受的手动干预。

```bash
sudo systemctl stop easytrade-dryrun@nfi easytrade-dryrun@nfi-watchdog
OLD="user_data/dryrun.round-$(date +%Y%m%d)"
mv user_data/dryrun "$OLD"                                                    # 数据库、日志、冻结副本整个留档
mv user_data/nfix8-profit_max-easytrade-nfi-dryrun-binance-USDT.json "$OLD/" 2> /dev/null || true   # NFI 的止盈目标缓存（新一轮要从空的开始）
bash scripts/run_dryrun.sh nfi --check-only
sudo systemctl start easytrade-dryrun@nfi easytrade-dryrun@nfi-watchdog
```

脚本发现"还没有数据库、却有上一轮的止盈目标缓存"会拒绝启动并提示上面这条 `mv`。

## 13. 文件一览

| 文件 | 内容 |
|---|---|
| `configs/config.dryrun-nfi.json` | 模拟盘配置（第 4 节） |
| `scripts/run_dryrun.sh` | 启动前检查 + 启动（`--check-only` 只检查；`--config-check-only` 只查配置，不联网） |
| `scripts/check_live_guards.py` | 配置检查规则（`--dryrun-config`）；`make guard-test` 的第 7 部分是它的回归检查 |
| `scripts/equity_watchdog.py` | 账户权益看门狗（第 8 节） |
| `scripts/forward_report.py` | 前向报告（第 9 节） |
| `docs/forward-monitoring.md` | 看门狗和前向报告的详细说明 |
| `docs/systemd/easytrade-dryrun@.service` | systemd 模板（第 7 节） |
| `.env.dryrun-nfi` | 密钥（不进 git，权限 600） |
| `user_data/dryrun/nfi.sqlite` | 模拟盘数据库（所有交易、订单） |
| `user_data/dryrun/nfi.log` | freqtrade 日志 |
| `user_data/dryrun/nfi-starts.log` | 每次启动一行 |
| `user_data/dryrun/nfi-watchdog.json` | 看门狗的状态（权益最高点、是否已暂停开新仓） |
| `user_data/dryrun/nfi-strategy/` | 冻结的策略代码副本 + `FROZEN.txt`（含 NFI，GPL-3.0：**不要把这个目录发给别人**） |
| `user_data/nfix8-profit_max-easytrade-nfi-dryrun-binance-USDT.json` | NFI 自己的止盈目标缓存（重启后接着用） |

`user_data/dryrun/` 不进 git。

## 14. 安全说明

- 模拟盘不下真单：配置里 `dry_run: true`，启动时还加 freqtrade 的 `--dry-run`（它会去掉交易所密钥）；环境变量把它改成实盘时启动前检查直接拒绝。
  这份配置**不能**拿去跑实盘。
- 不需要、也不要给这台服务器上的模拟盘配币安 API Key。
- FreqUI 只听 `127.0.0.1`，靠 SSH 隧道访问；密钥文件权限 600；仓库里的配置文件不写任何密钥、Token、服务器 IP（`make guard-test` 会查）。
- 开发机上**没有下载、导入或运行过** NFI：读它的源码和配置只用 `curl … | grep` 当文本看；测试 `BenchmarkCaps.py` 用的是我们自己写的替身基类。
  NFI 只在你的服务器上、由 `fetch_benchmarks.sh` 按固定 commit 下载并核对 sha256 以后才运行。
