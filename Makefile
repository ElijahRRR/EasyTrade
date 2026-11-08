# ------------------------------------------------------------------
# 常用命令快捷方式。用法：make <目标> [ARGS="..."]
#   例：make download ARGS="--timerange 20240101- --timeframes '1h 4h 1d'"
#       make backtest ARGS="--strategies MyStrategy --timerange 20240101-20250101"
# 运行方式自动检测（.venv > PATH 里的 freqtrade > Docker），
# 也可以强制：make backtest FT_RUNNER=docker
# 账户回撤上限（项目硬规则，含浮亏）默认 15%（2026-11-07 起；以前是 8%），backtest / check / research 共用；
# 临时改：make research MAX_DD_PCT=12（定义在 scripts/_common.sh 和 scripts/check_drawdown.py）
# ------------------------------------------------------------------
SHELL := /bin/bash
COMPOSE := docker compose -f docker/docker-compose.yml
ARGS ?=
export FT_RUNNER

.DEFAULT_GOAL := help
.PHONY: help venv pull validate download backtest check research research-synthetic research-benchmarks research-nfi-yearly research-nfi-yearly-synthetic synthetic-data synthetic-test guard-test forward-check pairs notify-test notify-send ui ui-venv up down logs ps zh-build zh-install docker-build dryrun-nfi-check dryrun-nfi forward-report watchdog prereg-check prereg-write download-spot listings pipeline-week synthetic-data-binance

help:  ## 显示本帮助
	@echo "可用命令（make <目标>）："
	@grep -E '^[a-zA-Z_-]+:.*## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*## "}; {printf "  %-30s %s\n", $$1, $$2}'

venv:  ## 创建 .venv 并安装固定版本的 freqtrade（需要 Python >= 3.12）
	python3 -m venv .venv
	.venv/bin/pip install --upgrade pip
	.venv/bin/pip install -r requirements.txt
	.venv/bin/freqtrade install-ui
	@if [ -f frequi-zh/dist/index.html ]; then bash frequi-zh/install-into-venv.sh .venv; \
	else echo "提示：上面装的是英文版 FreqUI。要中文界面：make zh-build && make zh-install"; fi

pull:  ## 拉取 Docker 镜像
	$(COMPOSE) pull cli

validate:  ## 校验所有入口配置（freqtrade show-config）
	@source scripts/_common.sh; \
	for c in configs/config.okx-backtest.json configs/config.okx-research.json configs/config.binance-futures.json configs/config.binance-xsmomentum.json configs/config.binance-research.json configs/config.okx-research-listings.json configs/config.okx-research-funding.json configs/config.okx-pairlist-volume.json configs/config.synthetic-test.json configs/config.dryrun-nfi.json; do \
		if ft show-config --config $$c > /dev/null 2>&1; then echo "通过  $$c"; \
		else echo "失败  $$c"; ft show-config --config $$c 2>&1 | tail -n 20; exit 1; fi; \
	done

download:  ## 下载 OKX 合约历史数据（默认 2021-01-01 起、含 1m，要好几个小时；只要研究数据：ARGS="--timeframes '1h 4h 1d'"）
	bash scripts/download_okx_data.sh $(ARGS)

backtest:  ## 批量回测 + 账户回撤检查（上限默认 15%，可改：make backtest MAX_DD_PCT=12；参数见 scripts/backtest_all.sh）
	bash scripts/backtest_all.sh $(ARGS)

check:  ## 只做回撤检查（上限默认 15%、MAX_DD_PCT 可改；默认检查最近一次回测；ARGS 可填结果目录）
	@source scripts/_common.sh; py_ft scripts/check_drawdown.py $(ARGS)

research:  ## 策略研究（第 3 轮）：2021 年起的数据，训练段/样本外/走步验证 + 子账户组合检查 + 中文排名表（回撤上限默认 15%、MAX_DD_PCT 可改；默认不调参，约 1~2 小时；参数见 scripts/research.sh）
	bash scripts/research.sh $(ARGS)

research-synthetic:  ## 用合成数据离线跑一遍策略研究流程（不联网，约 15 ~ 20 分钟；数字没有意义；第 5 轮新策略的例子见 README「第 5 步」）
	bash scripts/research.sh --synthetic $(ARGS)

# ---------- 第 5 轮：4 个新机制（预注册，每个只测一次；见 docs/round5-preregistration.md 和 README「第 5 步」） ----------
prereg-check:  ## 第 5 轮：看预注册登记表 configs/preregistration.json，核对每个策略文件（和一起固定的混入类、research-override）的 sha256 和登记的是否一致（不联网，几秒钟）
	@source scripts/_common.sh; plain_py scripts/check_prereg.py show $(ARGS)

prereg-write:  ## 第 5 轮（整合的人用）：策略文件定稿后，计算 sha256 写进登记表（已登记过、又不一致时拒绝；确认是正式运行之前改的才加 ARGS="--force"）
	@source scripts/_common.sh; plain_py scripts/check_prereg.py write $(ARGS)

download-spot:  ## 第 5 轮：只下 OKX 现货 BASE/USDT 1h（PremiumFade 用；默认 2019-11-01 起，含 2020 留出段和预热；ARGS 里的 --timerange 会覆盖）
	bash scripts/download_okx_data.sh --spot-only --config configs/config.okx-research.json --timerange 20191101- $(ARGS)

pipeline-week:  ## 第 5 轮管道检查：一个策略回测一周（默认 2026-11-01 ~ 11-07），只打印交易笔数和 funding_fees 是否非零，不打印收益（ARGS="--strategy XSFundingCarry --config configs/config.okx-research-funding.json"）
	bash scripts/round5_week_check.sh $(ARGS)

listings:  ## 第 5 轮：扫描本地 1d 永续K线，生成新币上线日文件 user_data/listings/<数据目录名>_first_candle.json（NewListingShortHedged 用；make research 会自动做；ARGS="--datadir user_data/data/okx --data-start 20190101"）
	@source scripts/_common.sh; py_ft scripts/build_listings.py $(ARGS)

# ---------- 第 5 轮运行 C：XSFundingCarry 用 OKX 资金费历史归档（2026-11-08 起；配置 configs/config.okx-research-funding.json，
# 数据目录 user_data/data/okx-funding，只给 XSFundingCarry 用，绝不写进 user_data/data/okx；脚本说明见 scripts/okx_funding_archive.py） ----------
.PHONY: okx-funding-probe okx-funding-data okx-funding-status okx-funding-check
OKX_FUNDING_CONFIG := configs/config.okx-research-funding.json
ARCHIVE_ARGS ?=

okx-funding-probe:  ## 运行 C：第一次导入前先看一眼 OKX 资金费归档接口返回的格式（要联网：1 次列表请求 + 下载 1 个文件；ARGS="--family ETH-USDT --month 2024-05" 或 "--lister priapi"）
	@source scripts/_common.sh; py_ft scripts/okx_funding_archive.py probe $(ARGS)

okx-funding-data:  ## 运行 C：准备 user_data/data/okx-funding（复制 user_data/data/okx 已有的合约K线 → 下载 2020-08 起的K线（研究 2022-04 开始 + 600 天预热）和最近 120 天的标记价格/资金费 → 导入 2022-03 起的资金费归档、更早的标记价格用K线代替；第一次几十分钟。ARGS 给下载脚本，ARCHIVE_ARGS 给导入脚本）
	@source scripts/_common.sh; CFG="$(OKX_FUNDING_CONFIG)"; \
	if [ -d user_data/data/okx/futures ]; then py_ft scripts/okx_funding_archive.py seed --config "$$CFG" --from user_data/data/okx; \
	else echo "（user_data/data/okx 里没有合约K线，不复制，全部重新下载）"; fi; \
	rc=0; bash scripts/download_okx_data.sh --config "$$CFG" --timeframes "1h 4h 1d" --timerange 20200801- $(ARGS) || rc=$$?; \
	if [ $$rc -ne 0 ]; then echo "!! 下载有出错的步骤（退出码 $$rc），照样试着导入归档（还没有接口资金费的币导入脚本会拒绝，免得以后没法核对）；网络好了以后再运行一次 make okx-funding-data"; fi; \
	urc=0; py_ft scripts/okx_funding_archive.py update --config "$$CFG" $(ARCHIVE_ARGS) || urc=$$?; \
	if [ $$urc -ne 0 ]; then echo "!! 资金费归档导入没有完成（退出码 $$urc：1 = 参数不对或者有币还没有接口下载的资金费（先让下载成功）；2 = 联网出错，重新运行接着下；3 = 格式认不出来，先 make okx-funding-probe）"; exit $$urc; fi; \
	exit $$rc

okx-funding-status:  ## 运行 C：看 user_data/data/okx-funding 的资金费归档清单（每个币多少条、从哪天开始、替身标记价格多少根、和接口数据的重叠核对）
	@source scripts/_common.sh; py_ft scripts/okx_funding_archive.py status --config "$(OKX_FUNDING_CONFIG)" $(ARGS)

okx-funding-check:  ## 只跑资金费归档导入的自检（假的 HTTP 层 + 合成数据的端到端回测，不联网，约半分钟；make guard-test 里也有；ARGS="--no-e2e" 只要几秒）
	@source scripts/_common.sh; py_ft scripts/check_okx_funding_archive.py $(ARGS)

research-benchmarks:  ## 基准策略研究：别人公开的策略（freqtrade-strategies、NFI）用同一套流程比较，只用默认参数（先 bash scripts/fetch_benchmarks.sh；见 docs/benchmarks.md）
	bash scripts/research_benchmarks.sh $(ARGS)

# 用法例子：make research-nfi-yearly ARGS='--classes "Lev1 Lev2" --years 2021-2023 --pairs 10'
research-nfi-yearly:  ## 第 4 轮 NFI X8 逐年回测：固定版本，1 倍/2 倍/5 倍封顶三个版本，2021 年起每年单独回测，给出能不能上模拟盘的结论（先 bash scripts/fetch_benchmarks.sh；很慢，在 tmux 里跑；见 docs/nfi-yearly.md）
	bash scripts/research_nfi_yearly.sh $(ARGS)

research-nfi-yearly-synthetic:  ## 用合成数据和替身策略试跑一遍逐年回测流程（不下载、不用 NFI，只检查脚本能跑通，约 10 分钟）
	bash scripts/research_nfi_yearly.sh --synthetic

synthetic-data:  ## 生成合成测试数据（不是真实行情，只用于流程自检；第 4 轮以前那份：ARGS="--legacy"）
	@source scripts/_common.sh; FT_RUNNER_RESOLVED=synthetic; py_ft scripts/make_synthetic_data.py $(ARGS)

synthetic-data-binance:  ## 生成"像币安"的合成数据 → user_data/data/binance-synthetic（25 个币、资金费率和标记价格覆盖全部区间；XSFundingCarry 自检用）
	@source scripts/_common.sh; FT_RUNNER_RESOLVED=synthetic; py_ft scripts/make_synthetic_data.py --exchange binance $(ARGS)

synthetic-test:  ## 用合成数据离线自检整条流程（回测 + 回撤检查 + 未来函数检查）
	bash scripts/synthetic_test.sh $(ARGS)

guard-test:  ## 实盘保护逻辑 + 回撤算法 + 模拟盘工具 + 研究报告自检（止损补救、只在信号刚收盘时入场、模拟盘配置、加仓/分批平仓的含浮亏回撤、前向报告和看门狗、子账户组合算法、第 5 轮报告的登记表规则和附加检查、第 5 轮的资金费窗口 / 下架币 / 正式运行规则、OKX 资金费归档导入（含一次合成数据的端到端回测）；不联网，约 1 分钟）
	@source scripts/_common.sh; py_ft scripts/check_live_guards.py && py_ft scripts/check_drawdown_math.py && py_ft scripts/check_forward_tools.py && py_ft scripts/check_portfolio_math.py && py_ft scripts/check_report_round5.py && py_ft scripts/check_round5_guards.py && py_ft scripts/check_okx_funding_archive.py

forward-check:  ## 只跑前向报告 + 看门狗自检（合成数据库 + 本机假接口，不联网，几秒钟；make guard-test 也会跑）
	@source scripts/_common.sh; py_ft scripts/check_forward_tools.py

pairs:  ## 按 OKX 当前成交额重新生成交易对列表
	bash scripts/update_okx_pairs.sh $(ARGS)

notify-test:  ## 检查并预览中文通知模板
	@source scripts/_common.sh; py_ft scripts/test_notify.py

notify-send:  ## 发一条测试通知到 Telegram（需要 .env 里的 token / chat id）
	@source scripts/_common.sh; \
	if [[ "$$FT_RUNNER_RESOLVED" == "docker" ]]; then \
		$(COMPOSE) run --rm --entrypoint python freqtrade scripts/test_notify.py --send; \
	else py_ft scripts/test_notify.py --send; fi

ui:  ## [Docker] 启动 FreqUI 查看回测结果 → 浏览器打开 http://127.0.0.1:8080
	$(COMPOSE) up -d ui

ui-venv:  ## [venv] 启动 FreqUI 查看回测结果（前台运行，Ctrl+C 停止）
	bash scripts/run_bot.sh ui

up:  ## [Docker] 启动交易机器人（币安合约，默认模拟盘；只能在币安允许的地区）
	$(COMPOSE) up -d freqtrade

down:  ## [Docker] 停止所有容器
	$(COMPOSE) --profile ui --profile trade down

logs:  ## [Docker] 查看机器人日志
	$(COMPOSE) logs -f --tail 200 freqtrade ui

ps:  ## [Docker] 查看容器状态
	$(COMPOSE) --profile ui --profile trade ps

zh-build:  ## 构建 FreqUI 中文版到 frequi-zh/dist（需要 git、Node.js >= 22；见 frequi-zh/README.md）
	bash frequi-zh/build.sh

zh-install:  ## [venv] 把 frequi-zh/dist 装进 .venv 里的 freqtrade（替换英文版 FreqUI）
	bash frequi-zh/install-into-venv.sh .venv

docker-build:  ## [Docker] 构建带中文 FreqUI 的镜像（ui / freqtrade 服务用；改补丁或升级后执行）
	$(COMPOSE) --profile ui --profile trade build ui freqtrade

# ---------- 第 4 轮：币安模拟盘前向测试（冻结版 NFI X8，1 倍杠杆；只能在币安允许访问的地区的服务器上运行；见 docs/forward-test.md） ----------
dryrun-nfi-check:  ## [venv] 币安模拟盘（冻结版 NFI 1 倍）启动前检查，不启动（只能在币安允许访问的地区运行；见 docs/forward-test.md）
	bash scripts/run_dryrun.sh nfi --check-only

dryrun-nfi:  ## [venv] 启动币安模拟盘（冻结版 NFI 1 倍，前台运行；长期运行用 systemd，见 docs/forward-test.md 第 7 节）
	bash scripts/run_dryrun.sh nfi

forward-report:  ## 模拟盘前向报告：只读模拟盘数据库，算含浮亏回撤、胜率、盈亏比，对照成功标准（先 freqtrade download-data 补K线；退出码 3 = 回撤超限或看门狗正处于暂停；见 docs/forward-monitoring.md）
	@source scripts/_common.sh; py_ft scripts/forward_report.py $(ARGS)

watchdog:  ## [venv] 账户权益看门狗（前台运行，Ctrl+C 停止；长期跑用 systemd 实例 easytrade-dryrun@nfi-watchdog）：含浮亏回撤超过 15% 就停止开新仓、发中文告警
	bash scripts/run_dryrun.sh nfi-watchdog $(ARGS)
