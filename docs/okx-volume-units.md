# OKX 永续 K 线的成交量单位

> 2026-11-06 核对，版本：freqtrade 2026.10 + ccxt 4.5.92（freqtrade 要求 ccxt ≥ 4.5.76）。
> 下面的 `文件:行号` 都在 `.venv/lib/python3.13/site-packages/` 里。

## 结论

- freqtrade 下载和实时拿到的 OKX USDT 永续 K 线，`volume` 列是 **基础币数量**（例如 BTC 的个数）。
  它**不是合约张数**，也**不是 USDT**。
- 成交额（USDT）= 价格 × volume。**不要再乘合约面值 contractSize**：BTC 1 张 = 0.01 BTC，乘了以后成交额会小 100 倍。
- 币安 U 本位的 volume 也是基础币数量，两个交易所口径一致。
  VolumeSpikeShort 的 5 万 USDT、DailyThreeSoldiersShort 的 500 万 USDT 流动性门槛在两边是同一个意思。
- 2026-11-06 第一次真实回测时，成交额就是按正确单位算的，**不需要因为单位问题重跑**。
  前提是 VPS 上装的也是 `requirements.txt` 固定的 freqtrade 2026.10。想确认的话，用下面"自检"里的 `--datadir` 命令跑一次真实数据。

## OKX 原始返回的三列成交量

OKX K 线接口（`/api/v5/market/candles`、`history-candles`）每根 K 线有三列成交量：

| 列 | OKX 字段 | 永续合约 | 现货 | 例：BTC-USDT-SWAP |
|---|---|---|---|---|
| 第 6 列 | `vol` | 张数 | 币的数量 | 1250 张 |
| 第 7 列 | `volCcy` | **币的数量** ← ccxt 取这一列 | USDT | 12.5 BTC（= 1250 × 0.01） |
| 第 8 列 | `volCcyQuote` | USDT | USDT | 约 116.9 万 USDT |

## 证据

**ccxt（把 OKX 原始数据转成 `[时间, 开, 高, 低, 收, 成交量]`）**

- `ccxt/okx.py:2615-2638` 是 `parse_ohlcv`。注释里写明三列分别是 contract / base / quote volume。
  第 2630 行是 `volumeIndex = 5 if (type == 'spot') else 6`，意思是永续取下标 6，也就是 `volCcy`（币的数量）。
- `ccxt/async_support/okx.py:2631` 是同样的代码。freqtrade 下载数据、回测都走异步接口
  （`freqtrade/exchange/exchange.py:3255` 的 `self._api_async.fetch_ohlcv`）。
  `fetch_ohlcv` 在最后一行（`:2744`）把 market 传给 `parse_ohlcvs`。
  所以 `type` 就是这个交易对的类型 `swap`（`ccxt/base/exchange.py:6378-6379`）。
- `ccxt/pro/okx.py:1133`：实盘 websocket 行情（`handle_ohlcv`）调用的是同一个 `parse_ohlcv(data[i], market)`，口径相同。
- 旁证 `ccxt/okx.py:2229`：ticker 的注释写着 "volCcy24h ... for derivatives this is base-amount"。

**freqtrade（拿到以后有没有再换算）**

- `freqtrade/exchange/exchange.py:152-154` 是默认值：`ohlcv_volume_currency = "base"`，`ohlcv_volume_in_contracts = False`。
  也就是说，K 线成交量按币的数量处理。
- `freqtrade/exchange/okx.py:29-52`：OKX 没有改这两项。它只设置了 `tickers_have_quoteVolume: False`（第 40 行），
  所以按成交量选币时要用 K 线来算。
- `freqtrade/exchange/gate.py:53-54`：只有 Gate 设置了 `ohlcv_volume_in_contracts: True`，
  代码注释是 "Futures candle volume is in contracts"。
- `freqtrade/plugins/pairlist/VolumePairList.py:200-219` 是 freqtrade 自己的算法：
  成交额 = volume × 典型价 × contractSize。只有 `ohlcv_volume_in_contracts` 为 True 时才乘 contractSize，否则乘 1。
- `freqtrade/data/converter/converter.py:63-75`：`ohlcv_to_dataframe` 只转换数据类型，不换算单位。
- `freqtrade/exchange/exchange.py:699-735`：张数和币数的换算只用在订单、成交、持仓上，K 线不做这一步。

## 项目里怎么处理

- `user_data/strategies/rust_port_common.py:129` 是 `ohlcv_quote_volume()`，`:475` 是 `RustPortMixin.quote_volume()`。
  它们用的规则和 freqtrade 的 VolumePairList 完全一样，直接读交易所对象上的这两个选项
  （包括配置文件 `exchange._ft_has_params` 里的覆盖）：
  - OKX 和币安：成交额 = close × volume，和改动前的写法逐位相同；
  - 如果以后换到"K 线成交量是张数"的交易所：自动乘 `dp.market(pair)['contractSize']`；
  - 如果是张数，但拿不到 contractSize：成交额记为空值，这个币不出信号，并写一条 ERROR 日志。宁可没有信号，也不猜单位。
- 用到它的地方：`VolumeSpikeShort.py:107` 和 `DailyThreeSoldiersShort.py:99`。`RustBaselines.py` 的对照版是继承来的，自动生效。
  BigTrendShort 只看"本根量 ÷ 前 20 根均量"这个比值，单位互相抵消，不受影响。
- 改动前后都在合成数据上回测了一遍（合成数据里 BTC 的 contractSize 也是 0.01）。
  VolumeSpikeShort 177 笔、DailyThreeSoldiersShort 197 笔，每一笔都完全相同。

## 自检（升级 freqtrade 或 ccxt 之后再跑一次）

```bash
make guard-test                                   # 第 4 项就是成交量单位检查
.venv/bin/python scripts/check_volume_units.py    # 单独跑，结果更详细
# 在 VPS 上下载数据之后，用真实数据核对量级：
.venv/bin/python scripts/check_volume_units.py --datadir user_data/data/okx
```

最后一条命令看 BTC 最近 180 天的日成交额中位数。正常是几十亿到一两百亿 USDT。
如果超过 1000 亿，多半是 volume 变成了张数（大了 100 倍），这时先别回测，把输出发过来。

## 其他注意

- OKX 每个币的合约面值不一样，比如 BTC 0.01、ETH 0.1、DOGE 1000。它只影响下单，freqtrade 会自动换算，和 K 线成交量无关。
- 币安有 1000PEPE、1000SHIB 这类"1000 倍"合约：价格和数量都按 1000 个币算，两者相乘的成交额不变。
  但它们在 OKX 上叫 PEPE、SHIB，名字对不上，所以 `configs/pairs.okx-research.json` 没有收这些币。
- 标记价格（mark）K 线没有成交量，只用来算资金费。
