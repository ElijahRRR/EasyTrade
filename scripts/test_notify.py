#!/usr/bin/env python3
"""
检查 / 预览 / 测试发送 中文 Telegram 通知模板（configs/config.notify-zh.json）。

需要 freqtrade 环境（.venv 或 Docker），因为要用 freqtrade 自己的配置加载和 webhook 格式化逻辑：
  .venv/bin/python scripts/test_notify.py                 # 检查全部模板并打印预览
  .venv/bin/python scripts/test_notify.py --send          # 再真的发一条测试消息到 Telegram
  make notify-test / make notify-send                     # Docker 用户用这两个

--send 需要环境变量（放在 .env 里，scripts/run_bot.sh 和 docker-compose 会自动读取）：
  FREQTRADE__TELEGRAM__TOKEN=123456:ABC...   BotFather 给的 token
  FREQTRADE__TELEGRAM__CHAT_ID=987654321     你的 chat id
如果直接用 .venv/bin/python 运行，脚本会自己读取项目根目录的 .env。

检查内容：每个模板里的 {占位符} 都是 freqtrade 在该事件里真正会提供的字段，
并且用模拟数据格式化一遍，确保不会因为格式写错导致通知发不出去。
"""

from __future__ import annotations

import argparse
import os
import string
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = ROOT / "configs" / "config.binance-futures.json"


def load_dotenv(path: Path) -> None:
    """极简 .env 读取：KEY=VALUE，忽略注释和空行；已存在的环境变量不覆盖。"""
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key.startswith("export "):
            key = key[len("export "):].strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def telegram_url() -> str | None:
    token = os.environ.get("FREQTRADE__TELEGRAM__TOKEN", "")
    chat_id = os.environ.get("FREQTRADE__TELEGRAM__CHAT_ID", "")
    if os.environ.get("FREQTRADE__WEBHOOK__URL"):
        return os.environ["FREQTRADE__WEBHOOK__URL"]
    if token and chat_id:
        return f"https://api.telegram.org/bot{token}/sendMessage?chat_id={chat_id}"
    return None


def sample_messages() -> dict:
    """模拟 freqtrade 各类事件的消息字段（字段名与 freqtrade 2026.10 源码一致）。"""
    from freqtrade.enums import RPCMessageType as T

    now = datetime.now(UTC).replace(microsecond=0)
    opened = now - timedelta(hours=5, minutes=12)
    base = {
        "trade_id": 42,
        "buy_tag": "breakout",
        "enter_tag": "breakout",
        "exchange": "Binance",
        "pair": "BTC/USDT:USDT",
        "base_currency": "BTC",
        "quote_currency": "USDT",
        "leverage": 3.0,
        "direction": "Long",
        "limit": 67250.5,
        "order_rate": 67250.5,
        "open_rate": 67250.5,
        "order_type": "limit",
        "stake_amount": 237.5,
        "stake_currency": "USDT",
        "fiat_currency": None,
        "amount": 0.0106,
        "open_date": opened,
        "current_rate": 67251.0,
        "sub_trade": False,
    }
    exit_extra = {
        "gain": "profit",
        "close_rate": 68100.0,
        "profit_amount": 26.91,
        "profit_ratio": 0.0377,
        "exit_reason": "roi",
        "close_date": now,
        "cumulative_profit": 0.0,
        "final_profit_ratio": 0.0377,
        "is_final_exit": True,
    }
    return {
        "entry": {**base, "type": T.ENTRY},
        "entry_fill": {**base, "type": T.ENTRY_FILL},
        "entry_cancel": {**base, "type": T.ENTRY_CANCEL, "reason": "cancelled due to timeout"},
        "exit": {**base, **exit_extra, "type": T.EXIT},
        "exit_fill": {**base, **exit_extra, "type": T.EXIT_FILL, "current_rate": None},
        "exit_cancel": {
            **base,
            "type": T.EXIT_CANCEL,
            "gain": "loss",
            "profit_amount": -3.2,
            "profit_ratio": -0.0135,
            "exit_reason": "exit_signal",
            "close_date": now,
            "reason": "cancelled due to timeout",
        },
        "status": {"type": T.STATUS, "status": "running"},
        "startup": {"type": T.STARTUP, "status": "*Exchange:* `binance`\n*Stake per trade:* `unlimited USDT`"},
        "warning": {"type": T.WARNING, "status": "Pair BTC/USDT:USDT has no data"},
        "exception": {"type": T.EXCEPTION, "status": "OperationalException:\n```\nexample\n```"},
        "protection_trigger": {
            "type": T.PROTECTION_TRIGGER,
            "id": 1,
            "pair": "BTC/USDT:USDT",
            "base_currency": "BTC",
            "lock_time": now.strftime("%Y-%m-%d %H:%M:%S"),
            "lock_timestamp": int(now.timestamp() * 1000),
            "lock_end_time": (now + timedelta(hours=2)).strftime("%Y-%m-%d %H:%M:%S"),
            "lock_end_timestamp": int((now + timedelta(hours=2)).timestamp() * 1000),
            "reason": "2 stoplosses in 360 min",
            "side": "long",
            "active": True,
        },
        "protection_trigger_global": {
            "type": T.PROTECTION_TRIGGER_GLOBAL,
            "id": 2,
            "pair": "*",
            "base_currency": None,
            "lock_time": now.strftime("%Y-%m-%d %H:%M:%S"),
            "lock_timestamp": int(now.timestamp() * 1000),
            "lock_end_time": (now + timedelta(hours=12)).strftime("%Y-%m-%d %H:%M:%S"),
            "lock_end_timestamp": int((now + timedelta(hours=12)).timestamp() * 1000),
            "reason": "Max drawdown 0.06 reached",
            "side": "*",
            "active": True,
        },
        "liquidation_warning": {
            "type": T.LIQUIDATION_WARNING,
            "exchange": "Binance",
            "margin_mode": "isolated",
            "trade_id": 42,
            "pair": "BTC/USDT:USDT",
            "base_currency": "BTC",
            "quote_currency": "USDT",
            "direction": "Short",
            "leverage": 5.0,
            "current_rate": 70100.0,
            "liquidation_price": 72000.0,
            "remaining_ratio": 0.15,
            "warn_ratio": 0.2,
            "positions_at_risk": 1,
            "open_positions": 3,
        },
        "strategy_msg": {"type": T.STRATEGY_MSG, "msg": "这是策略通过 self.dp.send_msg() 发出的中文消息"},
    }


def placeholders(text: str) -> set[str]:
    names = set()
    for _, field, _, _ in string.Formatter().parse(text):
        if field:
            names.add(field.split(".")[0].split("[")[0])
    return names


def walk_strings(obj):
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for v in obj.values():
            yield from walk_strings(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from walk_strings(v)


def main() -> int:
    parser = argparse.ArgumentParser(description="检查/预览/测试发送中文通知模板")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG, help="包含 webhook 配置的入口配置文件")
    parser.add_argument("--send", action="store_true", help="真的发一条测试消息到 Telegram")
    parser.add_argument("--event", default="exit_fill", help="--send 时发送哪个事件的示例（默认 exit_fill）")
    args = parser.parse_args()

    load_dotenv(ROOT / ".env")

    try:
        from freqtrade.configuration.load_config import load_from_files
        from freqtrade.rpc.webhook import Webhook
    except ImportError:
        print("需要 freqtrade 环境：请用 .venv/bin/python 运行，或 make notify-test（Docker）。")
        return 2

    config = load_from_files([str(args.config)])
    wh = config.get("webhook")
    if not wh:
        print(f"{args.config} 里没有 webhook 配置")
        return 2

    samples = sample_messages()
    hook = Webhook.__new__(Webhook)  # 不需要真正初始化 RPC，只借用它的格式化逻辑
    hook._config = config

    errors = 0
    rendered: dict[str, dict] = {}
    for event, msg in samples.items():
        tmpl = wh.get(event)
        if not tmpl:
            print(f"[未配置] {event}")
            continue
        missing = set()
        for s in walk_strings(tmpl):
            missing |= placeholders(s) - set(msg.keys())
        if missing:
            errors += 1
            print(f"[错误] {event}: 这些占位符 freqtrade 不会提供 -> {sorted(missing)}")
            continue
        try:
            payload = hook.recursive_format(tmpl, msg)
        except (KeyError, ValueError, TypeError, IndexError) as exc:
            errors += 1
            print(f"[错误] {event}: 格式化失败 -> {exc!r}")
            continue
        rendered[event] = payload
        print(f"----- [{event}] -----")
        print(payload.get("text", payload))
    print()
    if errors:
        print(f"共有 {errors} 个模板有问题，请修改 configs/config.notify-zh.json")
        return 1
    print("全部模板检查通过。")

    if args.send:
        url = telegram_url()
        if not url:
            print("没有设置 FREQTRADE__TELEGRAM__TOKEN / FREQTRADE__TELEGRAM__CHAT_ID，无法发送。")
            return 2
        if args.event not in rendered:
            print(f"没有可发送的事件 {args.event}")
            return 2
        import requests

        payload = dict(rendered[args.event])
        payload["text"] = "[测试消息]\n" + payload.get("text", "")
        resp = requests.post(url, json=payload, timeout=10)
        if resp.ok:
            print("已发送，请到 Telegram 查看。")
            return 0
        # 不打印 URL，避免把 token 输出到屏幕/日志
        print(f"发送失败：HTTP {resp.status_code} {resp.text[:300]}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
