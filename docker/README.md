# Docker 补充说明

`docker-compose.yml` 在 Mac 和 Linux VPS 上通用。所有命令都在**项目根目录**执行，最省事的是用 `make`：

| 命令 | 作用 |
|---|---|
| `make pull` | 拉取镜像 `freqtradeorg/freqtrade:2026.10` |
| `make validate FT_RUNNER=docker` | 校验配置 |
| `make download FT_RUNNER=docker` | 下载数据（在 `cli` 服务里跑一次性命令） |
| `make backtest FT_RUNNER=docker` | 批量回测 |
| `make docker-build` | 构建带中文 FreqUI 的镜像 `easytrade/freqtrade-zh`（`ui` / `freqtrade` 服务用，见下文） |
| `make ui` | 启动 FreqUI 看回测结果（`127.0.0.1:8080`） |
| `make up` / `make down` / `make logs` | 启动 / 停止 / 查看交易机器人（只能在币安允许的地区） |

不用 make 时的等价写法：

```bash
docker compose -f docker/docker-compose.yml run --rm cli backtesting --config configs/config.okx-backtest.json --strategy 策略名
docker compose -f docker/docker-compose.yml up -d ui
docker compose -f docker/docker-compose.yml up -d freqtrade
docker compose -f docker/docker-compose.yml --profile ui --profile trade down
```

## 中文界面镜像（Dockerfile.freqtrade-zh）

`ui` 和 `freqtrade` 不直接用官方镜像，而是用本地构建的 `easytrade/freqtrade-zh:2026.10-frequi-3.2.1`：

1. 第一阶段 `node:22-bookworm`：执行 `frequi-zh/build.sh`，克隆官方 FreqUI 3.2.1、打 `frequi-zh/patches/` 里的汉化补丁、构建。
2. 第二阶段 `freqtradeorg/freqtrade:2026.10`：用 `frequi-zh/install-into-venv.sh` 把构建结果装进
   `/freqtrade/freqtrade/rpc/api_server/ui/installed/`（和 `freqtrade install-ui` 同一个目录、同样写 `.uiversion`）。

- 构建上下文是项目根目录，但 `Dockerfile.freqtrade-zh.dockerignore` 只放进 frequi-zh 的脚本和补丁（约 0.5 MB），`.env` 和 `user_data/` 不会进镜像。
- 这个镜像**只在本地构建，永远不会从 Docker Hub 拉取**（compose 里设置了 `pull_policy: never`；`docker compose pull` 也会跳过它）。本地没有这个镜像时，`docker compose up` 会自动构建。
- 构建时需要访问 GitHub（克隆 FreqUI）和 npm 源（依赖）。改了补丁或版本后执行 `make docker-build`。
- `cli` 服务不需要网页，仍然用官方镜像。详细说明见 [`frequi-zh/README.md`](../frequi-zh/README.md)。

## 三个服务

| 服务 | 用途 | 端口 | 读取 .env |
|---|---|---|---|
| `cli` | 一次性命令：download-data、backtesting、list-data、show-config… | 无 | 否（不需要密钥） |
| `ui` | FreqUI 网页（webserver 模式，中文版），查看回测结果 | `127.0.0.1:8080` | 是 |
| `freqtrade` | 交易机器人，币安合约，默认 dry-run（自带中文版 FreqUI） | `127.0.0.1:8080` | 是 |

- 每个服务都设置了 profile，单独执行 `docker compose up -d` 不会启动任何东西，必须写服务名，避免误启动。
- `ui` 和 `freqtrade` 启动前会先运行 `scripts/preflight.sh` 做安全检查（FreqUI 密码、JWT 密钥、Telegram、策略名、实盘 API Key）。
- 端口只绑定宿主机 `127.0.0.1`，外网访问不到；远程查看用 SSH 隧道：`ssh -L 8080:127.0.0.1:8080 用户名@VPS地址`。
- 挂载：`user_data/`（读写）、`configs/`、`scripts/`（只读）。

## Mac（Apple Silicon / M4 Max）

- 安装 Docker Desktop 的 **Apple 芯片版**。freqtrade 官方镜像 `2026.10` 有 `arm64` 版本，不需要 Rosetta 模拟，速度正常。
- Docker Desktop → Settings → Resources：内存建议 ≥ 8 GB，CPU 给 6 核以上（回测 1m 数据、多策略时更快）。
- 项目放在 `/Users/你的用户名/...` 下即可（Docker Desktop 默认共享这个目录）。
- 首次 `make pull` 会下载约 1 GB 的镜像。
- Mac 上不需要 `chown`；Docker Desktop 会自动处理文件权限。
- Mac 的本地网络能否访问 OKX / 币安，取决于你所在地区和网络环境；不能访问时，就在 VPS 上下载数据、跑回测，再用 SSH 隧道看 FreqUI。

## Linux VPS

- 安装：`curl -fsSL https://get.docker.com | sudo sh`，然后 `sudo usermod -aG docker $USER` 并重新登录。
- 容器内用户 uid 为 1000，`user_data/` 需要它能写：`sudo chown -R 1000:1000 user_data`。
- 防火墙 / 云厂商安全组**不要**开放 8080 端口。
- 美国 VPS 只能做第一阶段（OKX 数据回测 + FreqUI 看结果）。`freqtrade` 交易服务连的是币安，美国 IP 会被拒（HTTP 451），
  需要部署在币安允许的地区的服务器上；不要用代理/VPN 绕过。
