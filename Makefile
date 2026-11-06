# ------------------------------------------------------------------
# 常用命令快捷方式。用法：make <目标> [ARGS="..."]
#   例：make download ARGS="--timerange 20240101- --timeframes '5m 1h'"
#       make backtest ARGS="--strategies MyStrategy --timerange 20240101-20250101"
# 运行方式自动检测（.venv > PATH 里的 freqtrade > Docker），
# 也可以强制：make backtest FT_RUNNER=docker
# ------------------------------------------------------------------
SHELL := /bin/bash
COMPOSE := docker compose -f docker/docker-compose.yml
ARGS ?=
export FT_RUNNER

.DEFAULT_GOAL := help
.PHONY: help venv pull validate download backtest check research research-synthetic synthetic-data synthetic-test guard-test pairs notify-test notify-send ui ui-venv up down logs ps zh-build zh-install docker-build

help:  ## 显示本帮助
	@echo "可用命令（make <目标>）："
	@grep -E '^[a-zA-Z_-]+:.*## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*## "}; {printf "  %-16s %s\n", $$1, $$2}'

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
	for c in configs/config.okx-backtest.json configs/config.okx-research.json configs/config.binance-futures.json configs/config.okx-pairlist-volume.json configs/config.synthetic-test.json; do \
		if ft show-config --config $$c > /dev/null 2>&1; then echo "通过  $$c"; \
		else echo "失败  $$c"; ft show-config --config $$c 2>&1 | tail -n 20; exit 1; fi; \
	done

download:  ## 下载 OKX 合约历史数据（参数见 scripts/download_okx_data.sh）
	bash scripts/download_okx_data.sh $(ARGS)

backtest:  ## 批量回测 + 8% 回撤检查（参数见 scripts/backtest_all.sh）
	bash scripts/backtest_all.sh $(ARGS)

check:  ## 只做回撤检查（默认检查最近一次回测；ARGS 可填结果目录）
	@source scripts/_common.sh; py_ft scripts/check_drawdown.py $(ARGS)

research:  ## 第 2 步策略研究：训练段/样本外/走步验证 + 自动调参 + 中文排名表（可能要几个小时；参数见 scripts/research.sh）
	bash scripts/research.sh $(ARGS)

research-synthetic:  ## 用合成数据离线跑一遍策略研究流程（不联网，几分钟；数字没有意义）
	bash scripts/research.sh --synthetic $(ARGS)

synthetic-data:  ## 生成合成测试数据（不是真实行情，只用于流程自检）
	@source scripts/_common.sh; FT_RUNNER_RESOLVED=synthetic; py_ft scripts/make_synthetic_data.py $(ARGS)

synthetic-test:  ## 用合成数据离线自检整条流程（回测 + 回撤检查 + 未来函数检查）
	bash scripts/synthetic_test.sh $(ARGS)

guard-test:  ## 实盘保护逻辑自检（止损补救、只在信号刚收盘时入场；不联网，几秒钟）
	@source scripts/_common.sh; py_ft scripts/check_live_guards.py

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
