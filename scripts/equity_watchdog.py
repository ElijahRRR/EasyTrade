#!/usr/bin/env python3
"""
账户权益看门狗：盯着正在运行的 freqtrade 机器人（模拟盘，以后也可以是实盘），账户权益（含持仓浮亏）
从最高点回撤超过上限（项目硬规则 15%）就让机器人【停止开新仓】并发中文告警；之后一直保持暂停，
直到人工复盘后手动解除。看门狗自己永远不会恢复开仓。说明：docs/forward-monitoring.md

为什么要它：freqtrade 没有"账户级"的硬止损。它的 MaxDrawdown 保护只看已平仓的交易，
  不看持仓浮亏；NFI 这种会扛单、加仓的策略，浮亏可以比已平仓回撤大很多（第 3 轮回测：平仓口径 1.92%，含浮亏 9.28%）。

用法（在跑机器人的那台服务器上；venv 的 python 或系统自带的 python3 都行，只用标准库）：
  bash scripts/run_dryrun.sh nfi-watchdog                 # 推荐：读好 .env.dryrun-nfi 后启动（systemd 用这个，见文档）
  python3 scripts/equity_watchdog.py --config configs/config.dryrun-nfi.json            # 一直运行，默认每 60 秒查一次
  python3 scripts/equity_watchdog.py --config configs/config.dryrun-nfi.json --once     # 只查一次，看看现在的回撤
  python3 scripts/equity_watchdog.py --config configs/config.dryrun-nfi.json --status   # 看记下的状态（峰值、是否暂停）
  python3 scripts/equity_watchdog.py --config configs/config.dryrun-nfi.json --test-alert   # 发一条测试告警
  人工复盘以后：
  python3 scripts/equity_watchdog.py --config configs/config.dryrun-nfi.json --reset --note "复盘结论……"
  python3 scripts/equity_watchdog.py --config configs/config.dryrun-nfi.json --resume   # 让机器人恢复开新仓（= FreqUI 的"启动交易"按钮）

怎么算：
  账户权益 = 钱包余额（/api/v1/balance 里 USDT 的 balance：初始资金 + 已平仓盈亏 + 分批平仓已实现的盈亏，不含浮亏）
           + 每个持仓的浮动盈亏（/api/v1/status 的 profit_abs：按现价、扣开仓和预估平仓手续费，和 FreqUI 显示的一样）
  峰值 = 看门狗每次读到的权益的最高值（不低于初始资金：模拟盘取配置里的 dry_run_wallet，实盘用 --start-balance），
         存在状态文件里（默认 user_data/dryrun/nfi-watchdog.json），重启看门狗不会丢。
  回撤 = (峰值 - 现在的权益) / 峰值。
  * 回撤 >= 预警线（默认 = 上限的 80%，即 12%）：发一次预警（回落到预警线以下 1 个百分点后才会再发）。
  * 回撤 > 上限（默认 15% = scripts/check_drawdown.py 的 DEFAULT_MAX_DD_PCT，环境变量 MAX_DD_PCT 或 --max-dd 可改）：
    调 POST /api/v1/stopentry（freqtrade 2026.10 里它和 /pause 是同一个接口：机器人进入 paused 状态，不再开新仓，
    已有持仓照常由策略管理、照常平仓），发告警，状态文件记下"已暂停"。
    加 --flatten 时还会调 POST /api/v1/forceexit（tradeid = all、ordertype = market）把所有持仓按市价平掉（默认不平）。
  * 暂停以后每次检查：机器人又变回 running（例如重启过：freqtrade 的暂停状态不会保存，重启后按配置的
    initial_state = running 开始）就再调一次 stopentry 并告警；每 12 小时提醒一次还在等人工复盘。
  * 连不上机器人：不会退出，等待时间逐步加长（最长 5 分钟），连续连不上超过 10 分钟发告警，恢复后再说一声。
  * 看门狗自己出错（例如状态文件坏了）：同样逐步加长等待、超过 10 分钟告警一次（这段时间没在保护账户）；
    连续出错 10 次就退出（退出码 1），让 systemd 重启它、systemctl status 也看得出来。
  * 回撤上限只能比项目硬规则（15%）更严：--max-dd / MAX_DD_PCT 比 15 大时拒绝启动（除非加 --allow-looser-than-rule）。

退出码：0 = 正常（--once 查到的回撤没超限）；1 = 参数 / 配置错误（例如没设 API 密码、上限比 15% 宽），或 --once 连不上机器人，
        或一直运行时看门狗自己连续出错 10 次；3 = 处于暂停状态（--once / --status：回撤超过上限，还没人工解除）。
        一直运行时不会因为 API 出错退出。

环境变量（推荐放在 .env.dryrun-nfi 里，权限 600，不进 git；看门狗只从那个文件里读下面这几个，别的不碰）：
  FREQTRADE__API_SERVER__USERNAME / FREQTRADE__API_SERVER__PASSWORD   机器人 REST API（FreqUI）的账号密码，和机器人用的同一组
  FREQTRADE__TELEGRAM__TOKEN / FREQTRADE__TELEGRAM__CHAT_ID           告警发到哪个 Telegram（不设就只打印在日志里）
  EASYTRADE_API_URL                     REST API 地址（默认按配置的 api_server 拼：http://127.0.0.1:8081）
  FREQTRADE__API_SERVER__LISTEN_PORT    改过端口时按它拼地址（和机器人读同一个变量）
  MAX_DD_PCT                            回撤上限（%），和别的脚本共用；--max-dd 优先；只能 <= 15（项目硬规则）
"""

from __future__ import annotations

import argparse
import base64
import contextlib
import json
import math
import os
import sys
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterator
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCRIPTS_DIR = Path(__file__).resolve().parent
ROOT_DIR = SCRIPTS_DIR.parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import check_drawdown as cd  # noqa: E402  (只用标准库部分：回撤上限的唯一定义 DEFAULT_MAX_DD_PCT)

DEFAULT_CONFIG = ROOT_DIR / "configs" / "config.dryrun-nfi.json"
DEFAULT_ENV_FILE = ".env.dryrun-nfi"
DEFAULT_PORT = 8081
API_URL_ENV = "EASYTRADE_API_URL"
USER_ENV = "FREQTRADE__API_SERVER__USERNAME"
PASS_ENV = "FREQTRADE__API_SERVER__PASSWORD"
PORT_ENV = "FREQTRADE__API_SERVER__LISTEN_PORT"
TG_TOKEN_ENV = "FREQTRADE__TELEGRAM__TOKEN"
TG_CHAT_ENV = "FREQTRADE__TELEGRAM__CHAT_ID"
ENV_KEYS = (USER_ENV, PASS_ENV, PORT_ENV, TG_TOKEN_ENV, TG_CHAT_ENV, API_URL_ENV, cd.MAX_DD_ENV)
TELEGRAM_API = "https://api.telegram.org"
LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1")

WARN_RATIO = 0.8           # 预警线默认 = 上限 × 0.8（15% → 12%）
WARN_REARM_PCT = 1.0       # 回落到 预警线 - 1 个百分点 以下，预警才会再发
DD_EPS_PCT = 1e-9          # 浮点误差：回撤正好等于上限不算超过
STATE_VERSION = 1
MAX_EVENTS = 50

EXIT_OK, EXIT_ERROR, EXIT_HALTED = 0, 1, 3


# ----------------------------------------------------------------------------
# 公共小工具（scripts/forward_report.py 也用）
# ----------------------------------------------------------------------------
def utc_text(ts: float | None, with_seconds: bool = False) -> str:
    """unix 秒 → "2026-11-07 14:35 UTC"。"""
    if ts is None:
        return "-"
    fmt = "%Y-%m-%d %H:%M:%S" if with_seconds else "%Y-%m-%d %H:%M"
    return datetime.fromtimestamp(float(ts), tz=timezone.utc).strftime(fmt) + " UTC"


def duration_text(seconds: float) -> str:
    """秒 → "3 天 4 小时" / "5 小时 20 分钟" / "12 分钟"。"""
    seconds = max(0.0, float(seconds))
    days, rem = divmod(int(seconds), 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    if days:
        return f"{days} 天 {hours} 小时"
    if hours:
        return f"{hours} 小时 {minutes} 分钟"
    return f"{minutes} 分钟"


def load_env_keys(path: Path | None, keys: tuple[str, ...] = ENV_KEYS) -> list[str]:
    """从 .env 格式的文件里只读出 keys 里的变量（KEY=VALUE，忽略注释、空行、export 前缀、两边引号）。

    已经在环境变量里的不覆盖（例如 scripts/run_dryrun.sh 已经读过同一个文件）。返回读进来的变量名（不返回值）。
    文件不存在时什么都不做。别的变量（例如交易所 API Key）一律不读。
    """
    if path is None or not path.is_file():
        return []
    loaded = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key.startswith("export "):
            key = key[len("export "):].strip()
        if key not in keys or os.environ.get(key):
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        os.environ[key] = value
        loaded.append(key)
    return loaded


def resolve_env_file(arg: str | None) -> Path | None:
    """--env-file：none = 不读；没给时用 EASYTRADE_DRYRUN_ENV_FILE（scripts/run_dryrun.sh 同一个变量）或 .env.dryrun-nfi。"""
    raw = arg if arg is not None else os.environ.get("EASYTRADE_DRYRUN_ENV_FILE", DEFAULT_ENV_FILE)
    if not raw or raw.lower() == "none":
        return None
    p = Path(raw)
    return p if p.is_absolute() else ROOT_DIR / p


def _strip_jsonc(text: str) -> str:
    """去掉 // 和 /* */ 注释、以及 } ] 前面多余的逗号（freqtrade 配置允许这些）。字符串里的内容不动。"""
    out: list[str] = []
    i, n = 0, len(text)
    in_str = False
    while i < n:
        c = text[i]
        if in_str:
            out.append(c)
            if c == "\\" and i + 1 < n:
                out.append(text[i + 1])
                i += 2
                continue
            if c == '"':
                in_str = False
            i += 1
            continue
        if c == '"':
            in_str = True
            out.append(c)
            i += 1
        elif text.startswith("//", i):
            while i < n and text[i] != "\n":
                i += 1
        elif text.startswith("/*", i):
            end = text.find("*/", i + 2)
            i = n if end < 0 else end + 2
        else:
            out.append(c)
            i += 1
    s = "".join(out)
    import re

    return re.sub(r",(\s*[}\]])", r"\1", s)


def _merge(base: dict[str, Any], top: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for k, v in top.items():
        out[k] = _merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def load_config(path: Path) -> dict[str, Any]:
    """读 freqtrade 配置文件（带注释，含 add_config_files 引入的文件）。

    有 freqtrade（.venv 的 python）时用它自己的 load_from_files，和机器人读到的一样（环境变量覆盖除外）；
    系统自带的 python3 没有 freqtrade 时用一个简单的读法（同样处理注释和 add_config_files）。
    """
    try:
        from freqtrade.configuration.load_config import load_from_files
    except ImportError:
        load_from_files = None
    if load_from_files is not None:
        import logging

        logging.getLogger("freqtrade").setLevel(logging.WARNING)  # 不打印 "Using config: ..."
        return load_from_files([str(path)])

    def load(p: Path, level: int) -> dict[str, Any]:
        if level > 5:
            raise ValueError("add_config_files 套娃太多层")
        cfg = json.loads(_strip_jsonc(p.read_text(encoding="utf-8")))
        merged: dict[str, Any] = {}
        for sub in cfg.get("add_config_files") or []:
            merged = _merge(merged, load(p.parent / sub, level + 1))
        return _merge(merged, cfg)

    return load(path, 0)


def config_start_balance(cfg: dict[str, Any]) -> float | None:
    """模拟盘的初始资金：配置的 dry_run_wallet（数字，或 {币种: 数量} 里的 stake_currency）。"""
    w = cfg.get("dry_run_wallet")
    if isinstance(w, dict):
        w = w.get(cfg.get("stake_currency") or "USDT")
    try:
        v = float(w)
    except (TypeError, ValueError):
        return None
    return v if v > 0 else None


def config_db_path(cfg: dict[str, Any]) -> Path | None:
    """配置里 db_url = sqlite:///相对路径 → 项目根目录下的文件路径；不是 sqlite 文件返回 None。"""
    url = str(cfg.get("db_url") or "")
    if not url.startswith("sqlite:///") or url == "sqlite:///":
        return None
    p = Path(url[len("sqlite:///"):])
    return p if p.is_absolute() else ROOT_DIR / p


def telegram_settings() -> tuple[str, str] | None:
    token = os.environ.get(TG_TOKEN_ENV, "").strip()
    chat = os.environ.get(TG_CHAT_ENV, "").strip()
    return (token, chat) if token and chat else None


def send_telegram(text: str, token: str, chat_id: str, *, api_base: str = TELEGRAM_API,
                  timeout: float = 15.0) -> tuple[bool, str]:
    """用 Telegram Bot API 发一条消息。返回 (成功?, 中文说明)。说明里不会出现 token。"""
    url = f"{api_base.rstrip('/')}/bot{token}/sendMessage"
    body = json.dumps({"chat_id": chat_id, "text": text[:4000], "disable_web_page_preview": True}).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST", headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            ok = 200 <= resp.status < 300
            return ok, "已发送" if ok else f"Telegram 返回 HTTP {resp.status}"
    except urllib.error.HTTPError as e:
        return False, f"Telegram 返回 HTTP {e.code}（检查 token / chat id）"
    except (urllib.error.URLError, OSError) as e:
        reason = getattr(e, "reason", e)
        return False, f"连不上 Telegram（{type(reason).__name__}）"


class Notifier:
    """告警：总是打印到日志（stdout）；配了 Telegram 就同时发过去。发不出去不报错，只在日志里说。"""

    def __init__(self, telegram: tuple[str, str] | None, *, api_base: str = TELEGRAM_API,
                 log: Callable[[str], None] | None = None) -> None:
        self.telegram = telegram
        self.api_base = api_base
        self.log = log or log_line
        self.sent: list[str] = []

    def send(self, text: str) -> bool:
        self.sent.append(text)
        self.log("告警：" + text.replace("\n", " | "))
        if not self.telegram:
            return False
        ok, msg = send_telegram(text, *self.telegram, api_base=self.api_base)
        if not ok:
            self.log(f"告警没发出去：{msg}")
        return ok


def log_line(text: str) -> None:
    print(f"{utc_text(time.time(), with_seconds=True)}  {text}", flush=True)


# ----------------------------------------------------------------------------
# freqtrade REST API（2026.10：/api/v1/...，HTTP Basic 认证）
# ----------------------------------------------------------------------------
class ApiError(Exception):
    """kind：unreachable（连不上 / 超时）、auth（401 账号密码不对）、http（别的 HTTP 错误）、bad_response（返回内容不对）。"""

    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind


def check_api_url(url: str, allow_remote: bool) -> str:
    """只允许本机地址（127.0.0.1 / localhost / ::1），除非 --allow-remote：明文 HTTP 发密码到别的机器不安全。"""
    p = urllib.parse.urlsplit(url)
    if p.scheme not in ("http", "https") or not p.hostname:
        raise ValueError(f"API 地址要是 http://主机:端口 的形式，收到：{url!r}")
    if p.username or p.password:
        raise ValueError("API 地址里不要写账号密码（用环境变量 FREQTRADE__API_SERVER__USERNAME / PASSWORD）")
    if p.hostname not in LOOPBACK_HOSTS and not allow_remote:
        raise ValueError(
            f"API 地址 {p.hostname} 不是本机。看门狗默认只连本机的机器人（和机器人跑在同一台服务器上）；"
            "确实要连别的机器请用 SSH 隧道转到本机端口，或者加 --allow-remote（不推荐：账号密码会经过网络）"
        )
    return url.rstrip("/")


class FtApi:
    def __init__(self, base_url: str, username: str, password: str, timeout: float = 10.0) -> None:
        self.base = base_url.rstrip("/")
        self.timeout = timeout
        cred = base64.b64encode(f"{username}:{password}".encode()).decode("ascii")
        self._auth = f"Basic {cred}"

    def request(self, method: str, path: str, body: Any = None) -> Any:
        url = f"{self.base}/api/v1/{path.lstrip('/')}"
        data = None if body is None else json.dumps(body).encode("utf-8")
        headers = {"Authorization": self._auth, "Accept": "application/json"}
        if data is not None:
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as e:
            if e.code == 401:
                raise ApiError("auth", "API 登录失败（HTTP 401）：用户名或密码不对") from None
            raise ApiError("http", f"{method} /api/v1/{path} 返回 HTTP {e.code}") from None
        except (urllib.error.URLError, OSError) as e:
            reason = getattr(e, "reason", e)
            raise ApiError("unreachable", f"连不上机器人 API {self.base}（{type(reason).__name__}: {reason}）") from None
        try:
            return json.loads(raw.decode("utf-8") or "null")
        except ValueError:
            raise ApiError("bad_response", f"/api/v1/{path} 返回的不是 JSON") from None

    def show_config(self) -> dict[str, Any]:
        return self.request("GET", "show_config")

    def balance(self) -> dict[str, Any]:
        return self.request("GET", "balance")

    def status(self) -> list[dict[str, Any]]:
        return self.request("GET", "status")

    def stopentry(self) -> dict[str, Any]:
        return self.request("POST", "stopentry")

    def forceexit_all(self) -> dict[str, Any]:
        # ordertype 一定要写：不写时 freqtrade 用配置的 order_types.force_exit（config.dryrun-nfi.json 里是 limit），
        # 限价单 2 分钟没成交会被 unfilledtimeout 撤掉、没人重试，持仓就还在（rpc.py __exec_force_exit）
        return self.request("POST", "forceexit", {"tradeid": "all", "ordertype": "market"})

    def start(self) -> dict[str, Any]:
        return self.request("POST", "start")


def _fnum(v: Any) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _worst_upnl(t: dict[str, Any]) -> float | None:
    """拿不到现价的持仓：按持仓期间到过的最差价格（多单 min_rate、空单 max_rate）估浮亏（偏保守）。"""
    amount, rate = _fnum(t.get("amount")), _fnum(t.get("open_rate"))
    if amount is None or rate is None:
        return None
    short = bool(t.get("is_short"))
    worst = _fnum(t.get("max_rate") if short else t.get("min_rate")) or rate
    d = -1.0 if short else 1.0
    fo, fc = _fnum(t.get("fee_open")) or 0.0, _fnum(t.get("fee_close")) or 0.0
    return d * amount * (worst - rate) - fo * amount * rate - fc * amount * worst


def account_equity(balance: dict[str, Any], status: list[dict[str, Any]], stake: str) -> dict[str, Any]:
    """账户权益 = 钱包余额（stake 币种的 balance，不含浮亏）+ 各持仓的浮动盈亏（profit_abs）。

    freqtrade 2026.10：模拟盘的钱包余额 = 初始资金 + 已平仓盈亏 + 持仓里分批平仓已实现的盈亏（wallets._update_dry）；
    实盘是交易所的钱包余额，交易所把浮亏算进 total 的（币安等）freqtrade 已经减掉了（wallets._strip_unrealized_pnl）。
    /status 的 profit_abs = 按现价算的剩余仓位盈亏（含手续费），不含分批平仓已实现的部分，所以两者相加不会重复。
    现价拿不到（profit_abs 为空）的持仓按持仓期间最差价估，记在 estimated 里。
    """
    wallet = None
    for c in balance.get("currencies") or []:
        if c.get("currency") == stake and not c.get("is_position"):
            wallet = _fnum(c.get("balance"))
            break
    if wallet is None:
        raise ApiError("bad_response", f"/api/v1/balance 里找不到 {stake} 的钱包余额")
    upnl = 0.0
    estimated: list[str] = []
    n_open = 0
    for t in status or []:
        if not isinstance(t, dict) or t.get("is_open") is False:
            continue
        n_open += 1
        p = _fnum(t.get("profit_abs"))
        if p is None:
            p = _worst_upnl(t)
            estimated.append(str(t.get("pair")))
            if p is None:
                raise ApiError("bad_response", f"持仓 {t.get('pair')} 没有现价也没有开仓价，算不了浮亏")
        upnl += p
    return {"equity": wallet + upnl, "wallet": wallet, "upnl": upnl, "open_trades": n_open, "estimated": estimated,
            "balance_total": _fnum(balance.get("total"))}


# ----------------------------------------------------------------------------
# 状态文件（峰值、是否暂停……）。每次检查都重新读、写完原子替换；用文件锁和 --reset 互斥
# ----------------------------------------------------------------------------
def new_state() -> dict[str, Any]:
    return {
        "version": STATE_VERSION, "bot_name": None, "created_at": None,
        "peak": None, "peak_at": None, "start_balance": None,
        "last": None, "max_dd_seen": 0.0, "max_dd_seen_at": None,
        "warn_active": False,
        "halted": False, "halted_at": None, "halt": None, "pause_confirmed": False,
        "pause_fail_alerted": False, "repause_tries": 0, "flattened_at": None, "last_remind_at": None,
        "fail_since": None, "unreachable_alerted": False, "auth_alerted": False, "last_error": None,
        "peak_floor_off": False, "resets": [], "events": [],
    }


def load_state(path: Path) -> dict[str, Any]:
    st = new_state()
    if path.is_file():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except ValueError as e:
            raise ValueError(f"状态文件 {path} 坏了（不是 JSON：{e}）。先看一眼内容，确认后删掉它再启动") from None
        if isinstance(data, dict):
            st.update(data)
    return st


def save_state(path: Path, st: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(st, ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)


@contextlib.contextmanager
def state_lock(path: Path) -> Iterator[None]:
    """同一个状态文件同时只有一个人改（看门狗的一次检查 / --reset）。没有 fcntl 的系统（Windows）不加锁。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        import fcntl
    except ImportError:
        yield
        return
    with open(path.with_name(path.name + ".lock"), "a+") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)


def _event(st: dict[str, Any], now: float, kind: str, text: str) -> None:
    st.setdefault("events", []).append({"at": now, "kind": kind, "text": text})
    del st["events"][:-MAX_EVENTS]


# ----------------------------------------------------------------------------
# 看门狗
# ----------------------------------------------------------------------------
class Watchdog:
    def __init__(
        self,
        api: FtApi,
        state_path: Path,
        *,
        max_dd: float,
        warn_dd: float,
        notifier: Notifier,
        flatten: bool = False,
        start_balance: float | None = None,
        bot_name: str | None = None,
        unreachable_alert_s: float = 600.0,
        remind_s: float = 12 * 3600.0,
        clock: Callable[[], float] = time.time,
        log: Callable[[str], None] | None = None,
        quiet: bool = False,
        max_internal_errors: int = 10,
    ) -> None:
        if not (0 < warn_dd < max_dd):
            raise ValueError(f"预警线 {warn_dd:g}% 要在 0 和上限 {max_dd:g}% 之间")
        self.api = api
        self.state_path = state_path
        self.max_dd = max_dd
        self.warn_dd = warn_dd
        self.notifier = notifier
        self.flatten = flatten
        self.start_balance = start_balance
        self.bot_name = bot_name
        self.unreachable_alert_s = unreachable_alert_s
        self.remind_s = remind_s
        self.clock = clock
        self.log = log or log_line
        self.quiet = quiet
        self.fail_count = 0
        # 看门狗自己出错（不是 API 出错）：只记在内存里（出错的可能正是状态文件）
        self.max_internal_errors = max_internal_errors
        self.internal_errors = 0
        self.internal_fail_since: float | None = None
        self.internal_alerted = False

    # ---- 读机器人 ----
    def read_bot(self) -> dict[str, Any]:
        cfg = self.api.show_config()
        if not isinstance(cfg, dict):
            raise ApiError("bad_response", "/api/v1/show_config 返回的内容不对")
        stake = str(cfg.get("stake_currency") or "USDT")
        # 先读持仓、再读余额、再读一次持仓：中间正好有交易平仓 / 成交时两次持仓对不上，重读一遍，
        # 避免"余额已经算进这笔盈亏、浮亏里也还算着它"这种一次性的重复计算
        for _attempt in range(3):
            st1 = self.api.status()
            bal = self.api.balance()
            st2 = self.api.status()
            if not isinstance(st1, list) or not isinstance(st2, list) or not isinstance(bal, dict):
                raise ApiError("bad_response", "/api/v1/status 或 /api/v1/balance 返回的内容不对")
            if _trade_keys(st1) == _trade_keys(st2):
                break
        else:
            raise ApiError("bad_response", "持仓一直在变（连续 3 次前后两次读到的持仓不一样），这次先跳过")
        eq = account_equity(bal, st2, stake)
        eq.update(bot_state=str(cfg.get("state") or ""), dry_run=bool(cfg.get("dry_run")), stake=stake,
                  bot_name=str(cfg.get("bot_name") or ""), max_open_trades=cfg.get("max_open_trades"))
        return eq

    # ---- 一次检查 ----
    def poll_once(self) -> dict[str, Any]:
        now = self.clock()
        with state_lock(self.state_path):
            st = load_state(self.state_path)
            if st.get("created_at") is None:
                st["created_at"] = now
            try:
                snap = self.read_bot()
            except ApiError as e:
                self._on_failure(st, e, now)
                save_state(self.state_path, st)
                return {"ok": False, "error": str(e), "kind": e.kind, "halted": bool(st.get("halted"))}
            self._check_bot(st, snap)
            self._on_success(st, now)
            result = self._evaluate(st, snap, now)
            save_state(self.state_path, st)
        return result

    def _check_bot(self, st: dict[str, Any], snap: dict[str, Any]) -> None:
        name = snap.get("bot_name") or self.bot_name or ""
        if st.get("bot_name") and name and st["bot_name"] != name:
            raise SystemExit(
                f"状态文件 {self.state_path} 记的是机器人 {st['bot_name']}，现在连的是 {name}。"
                "换一个 --state，或者确认是新开一轮后删掉旧的状态文件"
            )
        if name:
            st["bot_name"] = name

    def _on_failure(self, st: dict[str, Any], err: ApiError, now: float) -> None:
        self.fail_count += 1
        if st.get("fail_since") is None:
            st["fail_since"] = now
        st["last_error"] = str(err)
        self.log(f"读机器人失败（连续第 {self.fail_count} 次）：{err}")
        if err.kind == "auth" and not st.get("auth_alerted"):
            st["auth_alerted"] = True
            self.notifier.send(
                f"【权益看门狗】{self._who(st)}API 登录失败（用户名或密码不对），看门狗现在看不到账户，也没法暂停开仓。\n"
                f"检查 .env.dryrun-nfi 里的 {USER_ENV} / {PASS_ENV} 是否和机器人用的一样。"
            )
        down = now - float(st["fail_since"])
        if down >= self.unreachable_alert_s and not st.get("unreachable_alerted"):
            st["unreachable_alerted"] = True
            _event(st, now, "unreachable", str(err))
            self.notifier.send(
                f"【权益看门狗】{self._who(st)}已经 {duration_text(down)} 连不上机器人（{err}）。\n"
                "这段时间看门狗看不到账户权益，回撤超限也没法暂停开仓。请检查机器人是不是挂了："
                "systemctl status easytrade-dryrun@nfi，或看 user_data/dryrun/nfi.log。"
            )

    def _on_success(self, st: dict[str, Any], now: float) -> None:
        if st.get("unreachable_alerted"):
            down = now - float(st.get("fail_since") or now)
            self.notifier.send(f"【权益看门狗】{self._who(st)}重新连上机器人（中断了大约 {duration_text(down)}）。")
            _event(st, now, "recovered", f"中断约 {duration_text(down)}")
        st.update(fail_since=None, unreachable_alerted=False, auth_alerted=False, last_error=None)
        self.fail_count = 0

    def _who(self, st: dict[str, Any]) -> str:
        name = st.get("bot_name") or self.bot_name
        return f"{name}：" if name else ""

    def _evaluate(self, st: dict[str, Any], snap: dict[str, Any], now: float) -> dict[str, Any]:
        equity = float(snap["equity"])
        # 峰值不低于初始资金；人工 --reset --rebase-peak 以后不再用初始资金垫底（否则峰值马上又回到初始资金）
        floor = None if st.get("peak_floor_off") else self.start_balance
        if self.start_balance is not None:
            st["start_balance"] = self.start_balance
        peak = st.get("peak")
        if peak is None:
            peak = max(equity, floor or 0.0)
            st["peak_at"] = now
            self.log(f"峰值从 {peak:.2f} 开始算（{'初始资金' if floor and floor >= equity else '第一次读到的权益'}）")
        elif floor is not None and floor > peak:
            peak = floor
        if equity > peak:
            peak = equity
            st["peak_at"] = now
        st["peak"] = peak
        dd = (peak - equity) / peak if peak > 0 else 0.0
        dd_pct = dd * 100
        if dd > float(st.get("max_dd_seen") or 0.0):
            st["max_dd_seen"], st["max_dd_seen_at"] = dd, now
        st["last"] = {"at": now, "equity": equity, "wallet": snap["wallet"], "upnl": snap["upnl"], "dd": dd,
                      "open_trades": snap["open_trades"], "bot_state": snap["bot_state"], "dry_run": snap["dry_run"],
                      "estimated": snap["estimated"], "balance_total": snap.get("balance_total")}
        stake = snap.get("stake") or "USDT"
        mode = "模拟盘" if snap["dry_run"] else "实盘"
        est = f"（{len(snap['estimated'])} 个持仓没有现价，按最差价估）" if snap["estimated"] else ""
        if not self.quiet:
            self.log(
                f"权益 {equity:.2f} {stake}（钱包 {snap['wallet']:.2f} + 浮动盈亏 {snap['upnl']:+.2f}，"
                f"持仓 {snap['open_trades']} 个）{est}  峰值 {peak:.2f}  回撤 {dd_pct:.2f}%"
                f"  机器人 {snap['bot_state'] or '?'}{'  [已暂停开新仓，等人工复盘]' if st.get('halted') else ''}"
            )
        summary = (
            f"机器人：{st.get('bot_name') or self.bot_name or '?'}（{mode}）\n"
            f"账户权益（含浮亏）：{equity:.2f} {stake}{est}\n"
            f"峰值：{peak:.2f} {stake}（{utc_text(st.get('peak_at'))}）\n"
            f"回撤：{dd_pct:.2f}%（预警线 {self.warn_dd:g}%，上限 {self.max_dd:g}%）\n"
            f"持仓 {snap['open_trades']} 个，浮动盈亏 {snap['upnl']:+.2f} {stake}"
        )
        actions: list[str] = []

        if not st.get("halted") and dd_pct > self.max_dd + DD_EPS_PCT:
            st.update(halted=True, halted_at=now, pause_confirmed=False, pause_fail_alerted=False,
                      halt={"dd": dd, "equity": equity, "peak": peak, "peak_at": st.get("peak_at")})
            _event(st, now, "halt", f"回撤 {dd_pct:.2f}% > {self.max_dd:g}%")
            paused, msg = self._pause(snap)
            actions.append("stopentry" if paused else "stopentry_failed")
            text = (
                f"【权益看门狗】回撤超过上限，{'已停止开新仓' if paused else '暂停开新仓失败！'}\n{summary}\n"
                + (
                    "已调用 /api/v1/stopentry：机器人不再开新仓，已有持仓照常由策略管理、照常平仓。\n"
                    if paused else f"调用 /api/v1/stopentry 失败（{msg}）。请马上在 FreqUI 里点“暂停开仓”或停掉机器人。\n"
                )
            )
            if self.flatten:
                ok, fmsg = self._flatten()
                actions.append("forceexit" if ok else "forceexit_failed")
                text += ("已按 --flatten 调用 /api/v1/forceexit（全部持仓下市价平仓单；请在 FreqUI 里确认都平掉了）。\n" if ok
                         else f"按 --flatten 平掉全部持仓失败（{fmsg}），请手动处理。\n")
                if ok:
                    st["flattened_at"] = now
            else:
                text += "没有强制平仓（要看门狗同时平仓，启动时加 --flatten）。\n"
            text += ("看门狗不会自动恢复开仓。人工复盘后：python3 scripts/equity_watchdog.py --reset --note \"复盘结论\"，"
                     "再在 FreqUI 里点“启动交易”（或 --resume）。说明：docs/forward-monitoring.md")
            if not paused:
                st["pause_fail_alerted"] = True
            st["last_remind_at"] = now
            self.notifier.send(text)
        elif st.get("halted"):
            state = snap["bot_state"]
            if state in ("paused", "stopped"):
                st["pause_confirmed"] = True
                st["repause_tries"] = 0
            elif state == "running":
                # 机器人又在开新仓：多半是重启过（暂停状态不保存），或者有人点了"启动交易"但没先 --reset
                was_confirmed = bool(st.get("pause_confirmed"))
                paused, msg = self._pause(snap)
                actions.append("stopentry" if paused else "stopentry_failed")
                st["pause_confirmed"] = False
                st["repause_tries"] = 0 if was_confirmed else int(st.get("repause_tries") or 0) + 1
                _event(st, now, "repause", "机器人状态是 running，再次暂停" + ("" if paused else f"失败：{msg}"))
                if was_confirmed:
                    self.notifier.send(
                        f"【权益看门狗】机器人又处于可以开新仓的状态（多半是重启过，freqtrade 的暂停状态重启后不保留），"
                        f"{'已再次停止开新仓' if paused else f'再次暂停失败（{msg}），请手动在 FreqUI 里暂停'}。\n{summary}\n"
                        "还在等人工复盘；复盘后先 --reset 再恢复开仓。"
                    )
                elif not st.get("pause_fail_alerted") and (not paused or st["repause_tries"] >= 3):
                    # 调了 stopentry 但机器人一直没进入 paused（或者调用本身失败）：只告警一次
                    st["pause_fail_alerted"] = True
                    why = f"调用失败（{msg}）" if not paused else f"已经连续 {st['repause_tries']} 次调用，机器人还是 running"
                    self.notifier.send(
                        f"【权益看门狗】暂停开新仓没有生效：{why}。请马上在 FreqUI 里点\u201c暂停开仓\u201d或停掉机器人。\n{summary}"
                    )
            if now - float(st.get("last_remind_at") or st.get("halted_at") or now) >= self.remind_s:
                st["last_remind_at"] = now
                h = st.get("halt") or {}
                self.notifier.send(
                    f"【权益看门狗】提醒：机器人从 {utc_text(st.get('halted_at'))} 起暂停开新仓"
                    f"（当时回撤 {float(h.get('dd') or 0) * 100:.2f}%），还在等人工复盘。\n{summary}"
                )

        if not st.get("halted"):
            if dd_pct >= self.warn_dd and not st.get("warn_active"):
                st["warn_active"] = True
                _event(st, now, "warn", f"回撤 {dd_pct:.2f}%")
                actions.append("warn")
                self.notifier.send(
                    f"【权益看门狗】回撤预警：{dd_pct:.2f}%，超过预警线 {self.warn_dd:g}%"
                    f"（超过 {self.max_dd:g}% 会自动停止开新仓）。\n{summary}"
                )
            elif dd_pct < self.warn_dd - WARN_REARM_PCT and st.get("warn_active"):
                st["warn_active"] = False
                self.log(f"回撤回落到 {dd_pct:.2f}%（低于预警线 {self.warn_dd:g}% 减 {WARN_REARM_PCT:g}），预警重新生效")
        return {"ok": True, "equity": equity, "peak": peak, "dd": dd, "halted": bool(st.get("halted")),
                "actions": actions, "bot_state": snap["bot_state"]}

    def _pause(self, snap: dict[str, Any]) -> tuple[bool, str]:
        if snap.get("bot_state") == "stopped":
            # 机器人整个停着（不开仓也不平仓）：不去动它（对 stopped 调 stopentry 会让它以暂停状态跑起来）
            return True, "机器人本来就是 stopped 状态"
        try:
            resp = self.api.stopentry()
        except ApiError as e:
            self.log(f"调用 stopentry 失败：{e}")
            return False, str(e)
        self.log(f"已调用 stopentry：{(resp or {}).get('status', '')}")
        return True, ""

    def _flatten(self) -> tuple[bool, str]:
        try:
            resp = self.api.forceexit_all()
        except ApiError as e:
            self.log(f"调用 forceexit 失败：{e}")
            return False, str(e)
        self.log(f"已调用 forceexit all：{(resp or {}).get('result', '')}")
        return True, ""

    # ---- 看门狗自己出错 ----
    def _on_internal_error(self, e: BaseException) -> None:
        """poll_once 抛了不是 API 的异常：超过 unreachable_alert_s 告警一次；连续 max_internal_errors 次就退出（systemd 会重启）。"""
        now = self.clock()
        self.internal_errors += 1
        if self.internal_fail_since is None:
            self.internal_fail_since = now
        down = now - self.internal_fail_since
        give_up = self.max_internal_errors > 0 and self.internal_errors >= self.max_internal_errors
        who = f"{self.bot_name}：" if self.bot_name else ""
        if not self.internal_alerted and (down >= self.unreachable_alert_s or give_up):
            self.internal_alerted = True
            self.notifier.send(
                f"【权益看门狗】{who}看门狗内部出错，现在没在保护账户（已经 {duration_text(down)}，连续 {self.internal_errors} 次）："
                f"{_internal_error_text(e)}\n这段时间回撤超限也不会暂停开仓。请看日志："
                f"journalctl -u easytrade-dryrun@nfi-watchdog -n 100；状态文件：{self.state_path}"
            )
        if give_up:
            self.notifier.send(
                f"【权益看门狗】{who}看门狗连续 {self.internal_errors} 次内部出错，现在退出（退出码 1，systemd 会重启它）。"
                "修好之前账户没有回撤保护，可以先在 FreqUI 里点\u201c暂停开仓\u201d。"
            )
            raise SystemExit(f"看门狗连续 {self.internal_errors} 次内部出错，退出：{_internal_error_text(e)}")

    def _on_internal_ok(self) -> None:
        if self.internal_alerted:
            who = f"{self.bot_name}：" if self.bot_name else ""
            down = self.clock() - float(self.internal_fail_since or self.clock())
            self.notifier.send(f"【权益看门狗】{who}看门狗恢复正常（内部出错大约 {duration_text(down)}）。")
        self.internal_errors = 0
        self.internal_fail_since = None
        self.internal_alerted = False

    # ---- 一直运行 ----
    def run(self, interval: float, *, max_backoff: float = 300.0, max_polls: int | None = None,
            sleep: Callable[[float], None] = time.sleep) -> None:
        """一直检查。API 出错、程序意外出错都不退出：记日志，等一会儿（连续出错时逐步加长，最长 max_backoff 秒）再试。"""
        n = 0
        while max_polls is None or n < max_polls:
            n += 1
            try:
                res = self.poll_once()
                ok = bool(res.get("ok"))
            except SystemExit:
                raise
            except Exception as e:  # noqa: BLE001  看门狗不能因为自己的 bug 一声不响地停下
                self.fail_count += 1
                ok = False
                self.log("看门狗内部出错（会继续重试）：\n" + traceback.format_exc())
                self._on_internal_error(e)
            else:
                self._on_internal_ok()
            if max_polls is not None and n >= max_polls:
                break
            if ok:
                wait = interval
            else:
                wait = min(interval * (2 ** max(0, self.fail_count - 1)), max(interval, max_backoff))
            sleep(wait)


def _internal_error_text(e: BaseException) -> str:
    text = f"{type(e).__name__}: {e}"
    return text if len(text) <= 300 else text[:300] + "……"


def _trade_keys(status: list[Any]) -> list[tuple[Any, Any]]:
    return sorted((t.get("trade_id"), t.get("amount")) for t in status if isinstance(t, dict))


# ----------------------------------------------------------------------------
# 人工操作：--status / --reset / --resume
# ----------------------------------------------------------------------------
def status_text(st: dict[str, Any], state_path: Path, max_dd: float) -> str:
    lines = [f"看门狗状态文件：{state_path}"]
    if st.get("bot_name"):
        lines.append(f"机器人：{st['bot_name']}")
    if st.get("peak") is None:
        lines.append("峰值：还没有（看门狗还没成功读到过账户，或者刚 --reset --rebase-peak）")
    else:
        lines.append(f"峰值：{float(st['peak']):.2f}（{utc_text(st.get('peak_at'))}）")
    last = st.get("last")
    if last:
        lines.append(
            f"最近一次：{utc_text(last.get('at'))}  权益 {float(last['equity']):.2f}  回撤 {float(last['dd']) * 100:.2f}%"
            f"  持仓 {last.get('open_trades')} 个  机器人 {last.get('bot_state') or '?'}"
        )
    lines.append(f"看门狗见过的最大回撤：{float(st.get('max_dd_seen') or 0) * 100:.2f}%（{utc_text(st.get('max_dd_seen_at'))}）")
    if st.get("halted"):
        h = st.get("halt") or {}
        lines.append(
            f"!! 已暂停开新仓：{utc_text(st.get('halted_at'))} 起，当时回撤 {float(h.get('dd') or 0) * 100:.2f}% "
            f"> 上限 {max_dd:g}%，等人工复盘后 --reset"
        )
    else:
        lines.append("没有处于暂停状态")
    if st.get("fail_since"):
        lines.append(f"!! 从 {utc_text(st['fail_since'])} 起连不上机器人：{st.get('last_error')}")
    for r in (st.get("resets") or [])[-3:]:
        lines.append(f"解除记录：{utc_text(r.get('at'))}  {'重新算峰值' if r.get('rebase_peak') else '保留峰值'}  {r.get('note')}")
    return "\n".join(lines)


def looser_than_rule(max_dd: float) -> bool:
    """回撤上限比项目硬规则（check_drawdown.DEFAULT_MAX_DD_PCT）宽。"""
    return max_dd > cd.DEFAULT_MAX_DD_PCT + 1e-9


def looser_than_rule_text(max_dd: float) -> str:
    return (
        f"回撤上限 {max_dd:g}% 比项目硬规则 {cd.DEFAULT_MAX_DD_PCT:g}% 宽（多半是 --max-dd 或环境变量 {cd.MAX_DD_ENV}，"
        f"例如 .env.dryrun-nfi 里写的）。看门狗就是执行这条规则的，只能改严、不能改宽；"
        "真的要这样（例如只是试一下），加 --allow-looser-than-rule"
    )


def reset_state(state_path: Path, *, note: str, rebase_peak: bool, max_dd: float,
                clock: Callable[[], float] = time.time, allow_looser: bool = False) -> tuple[int, str]:
    """人工复盘后解除暂停状态（只改状态文件，不碰机器人）。返回 (退出码, 中文说明)。"""
    if looser_than_rule(max_dd) and not allow_looser:
        return EXIT_ERROR, looser_than_rule_text(max_dd)
    if not note.strip():
        return EXIT_ERROR, "--reset 必须带 --note \"复盘结论\"（会记在状态文件里，方便以后查）"
    with state_lock(state_path):
        st = load_state(state_path)
        last_dd = (st.get("last") or {}).get("dd")
        if not rebase_peak and last_dd is not None and float(last_dd) * 100 > max_dd + DD_EPS_PCT:
            return EXIT_ERROR, (
                f"最近一次读到的回撤 {float(last_dd) * 100:.2f}% 仍然超过上限 {max_dd:g}%：保留原来的峰值解除的话，"
                "下一次检查会马上再暂停。如果复盘后决定从现在的权益重新开始算峰值，加 --rebase-peak"
                "（注意：这样账户还能在现在的基础上再亏 15% 才会再次暂停）"
            )
        now = clock()
        st.setdefault("resets", []).append({
            "at": now, "note": note.strip(), "rebase_peak": rebase_peak, "was_halted": bool(st.get("halted")),
            "old_peak": st.get("peak"), "halt": st.get("halt"),
        })
        _event(st, now, "reset", note.strip())
        st.update(halted=False, halted_at=None, halt=None, pause_confirmed=False, pause_fail_alerted=False,
                  repause_tries=0, warn_active=False, last_remind_at=None, flattened_at=None)
        if rebase_peak:
            st.update(peak=None, peak_at=None, peak_floor_off=True)
        save_state(state_path, st)
    peak_txt = "峰值清空，下一次检查从当时的权益重新算" if rebase_peak else "峰值保留"
    return EXIT_OK, (
        f"已解除暂停状态（{peak_txt}）。机器人现在还是暂停开新仓的：确认没问题后在 FreqUI 里点“启动交易”，"
        "或者运行 python3 scripts/equity_watchdog.py --resume"
    )


# ----------------------------------------------------------------------------
# 命令行
# ----------------------------------------------------------------------------
def _pct_arg(text: str) -> float:
    try:
        return cd.parse_max_dd_pct(text, "回撤")
    except ValueError as e:
        raise argparse.ArgumentTypeError(str(e)) from None


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="账户权益看门狗：含浮亏回撤超过上限就让机器人停止开新仓（docs/forward-monitoring.md）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--config", default=None,
                   help=f"机器人的配置（拿 API 端口、数据库位置、机器人名字、模拟盘初始资金）。默认 {DEFAULT_CONFIG.relative_to(ROOT_DIR)}（存在的话）；none = 不读")
    p.add_argument("--url", default=None, help=f"REST API 地址（默认环境变量 {API_URL_ENV}，再不行按配置拼 http://127.0.0.1:端口）")
    p.add_argument("--env-file", default=None,
                   help=f"从这个文件读 API 账号密码和 Telegram（默认 {DEFAULT_ENV_FILE}；none = 不读，只用环境变量）")
    p.add_argument("--state", default=None, help="状态文件（默认和数据库放一起：user_data/dryrun/nfi-watchdog.json）")
    p.add_argument("--interval", type=float, default=60.0, help="多少秒检查一次（默认 60）")
    p.add_argument("--max-dd", type=_pct_arg, default=None,
                   help=f"回撤上限 %%（默认 {cd.DEFAULT_MAX_DD_PCT:g} = 项目硬规则；环境变量 {cd.MAX_DD_ENV} 也可以改）")
    p.add_argument("--allow-looser-than-rule", action="store_true",
                   help=f"允许回撤上限比项目硬规则 {cd.DEFAULT_MAX_DD_PCT:g}%% 宽（不建议；默认拒绝启动）")
    p.add_argument("--warn-dd", type=_pct_arg, default=None, help="预警线 %%（默认 = 上限 × 0.8，即 12）")
    p.add_argument("--flatten", action="store_true",
                   help="超限时除了停止开新仓，还把所有持仓按市价平掉（forceexit，ordertype = market；默认不平）")
    p.add_argument("--start-balance", type=float, default=None,
                   help="初始资金：峰值不低于它（模拟盘默认取配置的 dry_run_wallet；实盘请填）")
    p.add_argument("--unreachable-alert-min", type=float, default=10.0, help="连续连不上机器人多少分钟后告警（默认 10）")
    p.add_argument("--remind-hours", type=float, default=12.0, help="暂停以后每隔多少小时提醒一次（默认 12）")
    p.add_argument("--max-backoff", type=float, default=300.0, help="连不上时最长等多少秒再试（默认 300）")
    p.add_argument("--timeout", type=float, default=10.0, help="每个 API 请求的超时秒数（默认 10）")
    p.add_argument("--allow-remote", action="store_true", help="允许连不是本机的 API 地址（不推荐）")
    p.add_argument("--no-telegram", action="store_true", help="告警只打印在日志里，不发 Telegram")
    p.add_argument("--quiet", action="store_true", help="日志里只打印告警和出错，不打印每次检查的数字")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--once", action="store_true", help="只检查一次就退出（退出码 3 = 处于暂停状态）")
    g.add_argument("--status", action="store_true", help="显示状态文件里记的东西（不连机器人）")
    g.add_argument("--reset", action="store_true", help="人工复盘后解除暂停状态（要带 --note；不会恢复开仓）")
    g.add_argument("--resume", action="store_true", help="--reset 之后，让机器人恢复开新仓（POST /api/v1/start）")
    g.add_argument("--test-alert", action="store_true", help="发一条测试告警，确认 Telegram 收得到")
    p.add_argument("--note", default="", help="--reset 时写复盘结论（必填）")
    p.add_argument("--rebase-peak", action="store_true", help="--reset 时把峰值清空，从下一次读到的权益重新算")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    # 先读 .env.dryrun-nfi（里面的 MAX_DD_PCT 也算：直接运行和经过 run_dryrun.sh 运行用同一个上限）
    env_file = resolve_env_file(args.env_file)
    load_env_keys(env_file)
    try:
        max_dd = args.max_dd if args.max_dd is not None else cd.default_max_dd_pct()
    except ValueError as e:
        print(f"错误：{e}", file=sys.stderr)
        return EXIT_ERROR
    if looser_than_rule(max_dd) and not args.allow_looser_than_rule and not args.status:
        print(f"错误：{looser_than_rule_text(max_dd)}", file=sys.stderr)
        return EXIT_ERROR
    warn_dd = args.warn_dd if args.warn_dd is not None else round(max_dd * WARN_RATIO, 4)

    cfg: dict[str, Any] = {}
    if args.config and args.config.lower() == "none":
        cfg_path = None
    else:
        cfg_path = Path(args.config) if args.config else (DEFAULT_CONFIG if DEFAULT_CONFIG.is_file() else None)
    if cfg_path is not None:
        if not cfg_path.is_absolute() and not cfg_path.is_file():
            cfg_path = ROOT_DIR / cfg_path
        try:
            cfg = load_config(cfg_path)
        except Exception as e:  # noqa: BLE001
            print(f"错误：读不了配置 {cfg_path}：{e}", file=sys.stderr)
            return EXIT_ERROR

    if args.state:
        state_path = Path(args.state)
        if not state_path.is_absolute():
            state_path = ROOT_DIR / state_path
    else:
        db = config_db_path(cfg)
        state_path = db.with_name(db.stem + "-watchdog.json") if db else ROOT_DIR / "user_data" / "dryrun" / "nfi-watchdog.json"

    if args.status:
        st = load_state(state_path)
        print(status_text(st, state_path, max_dd))
        return EXIT_HALTED if st.get("halted") else EXIT_OK
    if args.reset:
        code, msg = reset_state(state_path, note=args.note, rebase_peak=args.rebase_peak, max_dd=max_dd,
                                allow_looser=args.allow_looser_than_rule)
        print(msg, file=sys.stderr if code else sys.stdout)
        return code

    tg = None if args.no_telegram else telegram_settings()
    notifier = Notifier(tg)
    bot_name = str(cfg.get("bot_name") or "") or None
    if args.test_alert:
        if not tg:
            print(f"没有设置 {TG_TOKEN_ENV} / {TG_CHAT_ENV}（或者加了 --no-telegram），告警只会打印在日志里。")
            return EXIT_ERROR
        ok, msg = send_telegram(f"【权益看门狗】测试告警：{bot_name or '机器人'} 的告警能收到。", *tg)
        print(f"测试告警：{msg}")
        return EXIT_OK if ok else EXIT_ERROR

    api_cfg = cfg.get("api_server") or {}
    port = os.environ.get(PORT_ENV) or api_cfg.get("listen_port") or DEFAULT_PORT
    host = str(api_cfg.get("listen_ip_address") or "127.0.0.1")
    if host in ("0.0.0.0", "::", ""):
        host = "127.0.0.1"
    url = args.url or os.environ.get(API_URL_ENV) or f"http://{'[' + host + ']' if ':' in host else host}:{port}"
    try:
        url = check_api_url(url, args.allow_remote)
    except ValueError as e:
        print(f"错误：{e}", file=sys.stderr)
        return EXIT_ERROR
    user, password = os.environ.get(USER_ENV, ""), os.environ.get(PASS_ENV, "")
    if not user or not password:
        where = f"在 {env_file.name} 里" if env_file else "用环境变量"
        print(f"错误：没有 API 账号密码。请{where}设置 {USER_ENV} 和 {PASS_ENV}（和机器人用的同一组）", file=sys.stderr)
        return EXIT_ERROR
    api = FtApi(url, user, password, timeout=args.timeout)

    if args.resume:
        st = load_state(state_path)
        if st.get("halted"):
            print("看门狗还处于暂停状态：先人工复盘，再 --reset --note \"复盘结论\"，然后才能 --resume", file=sys.stderr)
            return EXIT_HALTED
        try:
            resp = api.start()
        except ApiError as e:
            print(f"错误：{e}", file=sys.stderr)
            return EXIT_ERROR
        print(f"已让机器人恢复开新仓（POST /api/v1/start）：{(resp or {}).get('status', '')}")
        return EXIT_OK

    start_balance = args.start_balance
    if start_balance is None and cfg.get("dry_run") is True:
        start_balance = config_start_balance(cfg)
    try:
        wd = Watchdog(api, state_path, max_dd=max_dd, warn_dd=warn_dd, notifier=notifier, flatten=args.flatten,
                      start_balance=start_balance, bot_name=bot_name,
                      unreachable_alert_s=args.unreachable_alert_min * 60, remind_s=args.remind_hours * 3600,
                      quiet=args.quiet)
    except ValueError as e:
        print(f"错误：{e}", file=sys.stderr)
        return EXIT_ERROR

    if args.once:
        try:
            res = wd.poll_once()
        except (SystemExit, ValueError) as e:
            print(f"错误：{e}", file=sys.stderr)
            return EXIT_ERROR
        if not res.get("ok"):
            return EXIT_ERROR
        return EXIT_HALTED if res.get("halted") else EXIT_OK

    log_line(
        f"看门狗启动：{url}，每 {args.interval:g} 秒检查一次；预警线 {warn_dd:g}%，上限 {max_dd:g}%"
        f"（{cd.max_dd_rule_note(max_dd)}）；超限时{'停止开新仓并平掉全部持仓' if args.flatten else '停止开新仓（不平仓）'}；"
        f"状态文件 {state_path}；告警{'发 Telegram' if tg else '只打印在日志里（没配 Telegram）'}"
    )
    notifier.send(
        f"【权益看门狗】已启动：{bot_name or url}，每 {args.interval:g} 秒检查一次账户权益（含浮亏），"
        f"回撤 {warn_dd:g}% 预警、超过 {max_dd:g}% 停止开新仓"
        f"{'并平掉全部持仓' if args.flatten else '（不平仓）'}。"
    )
    try:
        wd.run(args.interval, max_backoff=args.max_backoff)
    except KeyboardInterrupt:
        log_line("看门狗已停止（Ctrl+C）")
    except SystemExit as e:
        print(f"错误：{e}", file=sys.stderr)
        return EXIT_ERROR
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
