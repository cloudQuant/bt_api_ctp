# CTP (China Futures)

CTP exchange plugin for bt_api, supporting Chinese futures market trading.

[![PyPI Version](https://img.shields.io/pypi/v/bt_api_ctp.svg)](https://pypi.org/project/bt_api_ctp/)
[![Python Versions](https://img.shields.io/pypi/pyversions/bt_api_ctp.svg)](https://pypi.org/project/bt_api_ctp/)
[![License](https://img.shields.io/badge/License-MIT-blue.svg)](https://opensource.org/licenses/MIT)
[![CI](https://github.com/cloudQuant/bt_api_ctp/actions/workflows/ci.yml/badge.svg)](https://github.com/cloudQuant/bt_api_ctp/actions)
[![Docs](https://readthedocs.org/projects/bt-api-ctp/badge/?version=latest)](https://bt-api-ctp.readthedocs.io/)

---

## English | [中文](#中文)

### Overview

This package provides the **CTP (China Futures)** exchange plugin for the [bt_api](https://github.com/cloudQuant/bt_api_py) framework. It offers a unified interface for interacting with Chinese futures exchanges via the CTP protocol.

### Supported Exchanges

| Exchange | Code | Description |
|----------|------|-------------|
| Shanghai Futures Exchange | SHFE | metals, energy |
| Dalian Commodity Exchange | DCE | agricultural, chemicals |
| Zhengzhou Commodity Exchange | CZCE | agricultural, chemicals |
| China Financial Futures Exchange | CFFEX | equity index futures |
| Shanghai International Energy Exchange | INE | crude oil, iron ore |
| Guangzhou Futures Exchange | GFEX | industrial, agricultural |

### Features

- Futures trading via CTP protocol (v6.x)
- Support for all Chinese futures exchanges
- Real-time market data via market data API
- Order placement and cancellation via trade API
- Position and margin tracking
- Auto-selection between SimNow environments (set1 / set2 / 7x24)
- SimNow simulated trading support

### Installation

```bash
pip install bt_api_ctp
```

Or install from source:

```bash
git clone https://github.com/cloudQuant/bt_api_ctp
cd bt_api_ctp
pip install -e .
```

### Quick Start

```python
from bt_api_py import BtApi

# Configure CTP futures exchange
exchange_kwargs = {
    "CTP___FUTURE": {
        "user_id": "your_user_id",
        "password": "your_password",
        "broker_id": "your_broker_id",
        "md_front": "tcp://182.254.243.31:30011",
        "td_front": "tcp://182.254.243.31:30001",
    }
}

api = BtApi(exchange_kwargs=exchange_kwargs)

# Connect and subscribe
api.connect()
api.subscribe("CTP___FUTURE___rb2401", [{"topic": "tick", "symbol": "rb2401"}])

# Get data from queue
data_queue = api.get_data_queue("CTP___FUTURE")
msg = data_queue.get(timeout=10)
print(type(msg).__name__, msg)
```

### CtpGatewayAdapter API

The `CtpGatewayAdapter` provides direct access to CTP futures:

```python
from bt_api_ctp.gateway.adapter import CtpGatewayAdapter

# Initialize adapter
adapter = CtpGatewayAdapter(
    md_front="tcp://182.254.243.31:30011",  # market data front
    td_front="tcp://182.254.243.31:30001",  # trade front
    user_id="your_user_id",
    password="your_password",
    broker_id="your_broker_id",
    gateway_startup_timeout_sec=10.0,
)

# Connect
adapter.connect()

# Subscribe to symbols
adapter.subscribe_symbols(["rb2401.SHFE", "IF2404.CFFEX"])

# Get balance and positions
balance = adapter.get_balance()
positions = adapter.get_positions()

# Place an order
order = adapter.place_order({
    "symbol": "rb2401.SHFE",
    "side": "buy",
    "size": 1,
    "price": 4000.0,
    "offset": "open",
})

# Cancel an order
adapter.cancel_order({
    "symbol": "rb2401.SHFE",
    "order_id": order["order_id"],
    "front_id": order["front_id"],
    "session_id": order["session_id"],
    "order_ref": order["order_ref"],
})

# Disconnect
adapter.disconnect()
```

### Constructor Parameters

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `md_front` | str | `""` | Market data front address (tcp://host:port) |
| `td_front` | str | `""` | Trade front address (tcp://host:port) |
| `user_id` | str | `""` | CTP user ID / investor ID |
| `password` | str | `""` | CTP password |
| `broker_id` | str | `""` | Broker ID |
| `auth_code` | str | `"0000000000000000"` | Auth code (for production) |
| `app_id` | str | `"simnow_client_test"` | App ID |
| `gateway_startup_timeout_sec` | float | `10.0` | Connection timeout |

### Supported Operations

| Operation | Method | Status |
|-----------|--------|--------|
| Connect | `connect()` | ✅ |
| Disconnect | `disconnect()` | ✅ |
| Subscribe Symbols | `subscribe_symbols(symbols)` | ✅ |
| Get Balance | `get_balance()` | ✅ |
| Get Positions | `get_positions()` | ✅ |
| Place Order | `place_order(payload)` | ✅ |
| Cancel Order | `cancel_order(payload)` | ✅ |

### Order Payload Format

```python
{
    "symbol": "rb2401.SHFE",     # instrument.exchange or just instrument
    "side": "buy",                # "buy" or "sell"
    "size": 1,                   # order volume
    "price": 4000.0,             # limit price (required for limit orders)
    "offset": "open",             # "open", "close", "close_today"
    "client_order_id": "...",    # optional, client-side order ref
    "exchange_id": "SHFE",        # optional, exchange code
}
```

### Order Response Format

```python
{
    "id": "...",
    "order_id": "...",
    "external_order_id": "...",
    "order_ref": "...",
    "front_id": 1,
    "session_id": 123456,
    "exchange_id": "SHFE",
    "details": {"bt_order_ref": "..."},
}
```

### Environment Configuration

CTP supports three environments via `CTP_ENV` or `env` parameter:

| Environment | Description | Trading Hours |
|-------------|-------------|---------------|
| `auto` (default) | Auto-select based on time | Matches production |
| `set1` | Production-like environment | Trading session only |
| `set2` | 7x24 environment | Non-trading hours |

For SimNow test accounts, leave `md_front`/`td_front` empty to auto-select the appropriate SimNow environment.

### Online Documentation

| Resource | Link |
|----------|------|
| English Docs | https://bt-api-ctp.readthedocs.io/ |
| Chinese Docs | https://bt-api-ctp.readthedocs.io/zh/latest/ |
| GitHub Repository | https://github.com/cloudQuant/bt_api_ctp |
| Issue Tracker | https://github.com/cloudQuant/bt_api_ctp/issues |

### Requirements

- Python 3.9+
- bt_api_base >= 0.15

### Architecture

```
bt_api_ctp/
├── src/bt_api_ctp/           # Source code
│   ├── containers/ctp/       # Data containers (CtpTicker, CtpOrder, etc.)
│   ├── feeds/                # Feed implementations (live_ctp_feed.py)
│   ├── gateway/              # Gateway adapter (CtpGatewayAdapter)
│   ├── ctp/                  # CTP protocol (structs, client, trader/md API)
│   ├── errors/               # Error translators
│   ├── exchange_data.py       # Exchange metadata
│   └── ctp_env_selector.py    # SimNow environment selector
├── tests/                    # Unit tests
└── docs/                     # Documentation
```

### License

MIT License - see [LICENSE](LICENSE) for details.

### Support

- Report bugs via [GitHub Issues](https://github.com/cloudQuant/bt_api_ctp/issues)
- Email: yunjinqi@gmail.com

---

## 中文

### 概述

本包为 [bt_api](https://github.com/cloudQuant/bt_api_py) 框架提供 **CTP（中国期货）** 交易所插件。通过 CTP 协议与中国期货交易所进行交互的统一接口。

### 支持的交易所

| 交易所 | 代码 | 描述 |
|--------|------|------|
| 上海期货交易所 | SHFE | 金属、能源 |
| 大连商品交易所 | DCE | 农产品、化工 |
| 郑州商品交易所 | CZCE | 农产品、化工 |
| 中国金融期货交易所 | CFFEX | 股指期货 |
| 上海国际能源交易中心 | INE | 原油、铁矿石 |
| 广州期货交易所 | GFEX | 工业品、农产品 |

### 功能特点

- 通过 CTP 协议（v6.x）进行期货交易
- 支持所有中国期货交易所
- 通过行情 API 获取实时市场数据
- 通过交易 API 下单和撤单
- 持仓和保证金跟踪
- 自动选择 SimNow 环境（set1 / set2 / 7x24）
- SimNow 模拟交易支持

### 安装

```bash
pip install bt_api_ctp
```

或从源码安装：

```bash
git clone https://github.com/cloudQuant/bt_api_ctp
cd bt_api_ctp
pip install -e .
```

### 快速开始

```python
from bt_api_py import BtApi

# 配置 CTP 期货交易所
exchange_kwargs = {
    "CTP___FUTURE": {
        "user_id": "your_user_id",
        "password": "your_password",
        "broker_id": "your_broker_id",
        "md_front": "tcp://182.254.243.31:30011",  # 行情前置
        "td_front": "tcp://182.254.243.31:30001",  # 交易前置
    }
}

api = BtApi(exchange_kwargs=exchange_kwargs)

# 连接并订阅
api.connect()
api.subscribe("CTP___FUTURE___rb2401", [{"topic": "tick", "symbol": "rb2401"}])

# 从队列获取数据
data_queue = api.get_data_queue("CTP___FUTURE")
msg = data_queue.get(timeout=10)
print(type(msg).__name__, msg)
```

### CtpGatewayAdapter API

`CtpGatewayAdapter` 提供对 CTP 期货的直接访问：

```python
from bt_api_ctp.gateway.adapter import CtpGatewayAdapter

# 初始化适配器
adapter = CtpGatewayAdapter(
    md_front="tcp://182.254.243.31:30011",  # 行情前置地址
    td_front="tcp://182.254.243.31:30001",  # 交易前置地址
    user_id="your_user_id",
    password="your_password",
    broker_id="your_broker_id",
    gateway_startup_timeout_sec=10.0,
)

# 连接
adapter.connect()

# 订阅合约
adapter.subscribe_symbols(["rb2401.SHFE", "IF2404.CFFEX"])

# 获取资金和持仓
balance = adapter.get_balance()
positions = adapter.get_positions()

# 下单
order = adapter.place_order({
    "symbol": "rb2401.SHFE",
    "side": "buy",
    "size": 1,
    "price": 4000.0,
    "offset": "open",
})

# 撤单
adapter.cancel_order({
    "symbol": "rb2401.SHFE",
    "order_id": order["order_id"],
    "front_id": order["front_id"],
    "session_id": order["session_id"],
    "order_ref": order["order_ref"],
})

# 断开连接
adapter.disconnect()
```

### 构造函数参数

| 参数 | 类型 | 默认值 | 描述 |
|------|------|--------|------|
| `md_front` | str | `""` | 行情前置地址（tcp://host:port）|
| `td_front` | str | `""` | 交易前置地址（tcp://host:port）|
| `user_id` | str | `""` | CTP 用户 ID / 投资者 ID |
| `password` | str | `""` | CTP 密码 |
| `broker_id` | str | `""` | 经纪公司 ID |
| `auth_code` | str | `"0000000000000000"` | 认证码（生产环境用）|
| `app_id` | str | `"simnow_client_test"` | App ID |
| `gateway_startup_timeout_sec` | float | `10.0` | 连接超时时间 |

### 支持的操作

| 操作 | 方法 | 状态 |
|------|------|------|
| 连接 | `connect()` | ✅ |
| 断开 | `disconnect()` | ✅ |
| 订阅合约 | `subscribe_symbols(symbols)` | ✅ |
| 获取资金 | `get_balance()` | ✅ |
| 获取持仓 | `get_positions()` | ✅ |
| 下单 | `place_order(payload)` | ✅ |
| 撤单 | `cancel_order(payload)` | ✅ |

### 下单 Payload 格式

```python
{
    "symbol": "rb2401.SHFE",     # 合约代码.交易所或仅合约代码
    "side": "buy",               # "buy" 或 "sell"
    "size": 1,                  # 委托数量
    "price": 4000.0,           # 限价（限价单必须）
    "offset": "open",           # "open", "close", "close_today"
    "client_order_id": "...",   # 可选，客户端订单引用
    "exchange_id": "SHFE",       # 可选，交易所代码
}
```

### 订单响应格式

```python
{
    "id": "...",
    "order_id": "...",
    "external_order_id": "...",
    "order_ref": "...",
    "front_id": 1,
    "session_id": 123456,
    "exchange_id": "SHFE",
    "details": {"bt_order_ref": "..."},
}
```

### 环境配置

CTP 支持通过 `CTP_ENV` 或 `env` 参数配置三种环境：

| 环境 | 描述 | 交易时段 |
|------|------|----------|
| `auto`（默认）| 根据时间自动选择 | 与生产一致 |
| `set1` | 类生产环境 | 仅交易时段 |
| `set2` | 7x24 环境 | 非交易时段 |

使用 SimNow 测试账户时，留空 `md_front`/`td_front` 可自动选择合适的 SimNow 环境。

### 在线文档

| 资源 | 链接 |
|------|------|
| 英文文档 | https://bt-api-ctp.readthedocs.io/ |
| 中文文档 | https://bt-api-ctp.readthedocs.io/zh/latest/ |
| GitHub 仓库 | https://github.com/cloudQuant/bt_api_ctp |
| 问题反馈 | https://github.com/cloudQuant/bt_api_ctp/issues |

### 系统要求

- Python 3.9+
- bt_api_base >= 0.15

### 架构

```
bt_api_ctp/
├── src/bt_api_ctp/           # 源代码
│   ├── containers/ctp/       # 数据容器 (CtpTicker, CtpOrder 等)
│   ├── feeds/                # Feed 实现 (live_ctp_feed.py)
│   ├── gateway/              # 网关适配器 (CtpGatewayAdapter)
│   ├── ctp/                  # CTP 协议 (structs, client, trader/md API)
│   ├── errors/               # 错误翻译器
│   ├── exchange_data.py       # 交易所元数据
│   └── ctp_env_selector.py    # SimNow 环境选择器
├── tests/                    # 单元测试
└── docs/                     # 文档
```

### 许可证

MIT 许可证 - 详见 [LICENSE](LICENSE)。

### 技术支持

- 通过 [GitHub Issues](https://github.com/cloudQuant/bt_api_ctp/issues) 反馈问题
- 邮箱: yunjinqi@gmail.com
