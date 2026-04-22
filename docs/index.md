# CTP (China Futures) Documentation

## English

Welcome to the CTP (China Futures) documentation for bt_api.

### Overview

CTP (Comprehensive Transaction Platform) is the standard trading system for Chinese futures exchanges. This plugin connects to SHFE, DCE, CZCE, CFFEX, INE, and GFEX through the CTP v6.x protocol.

### Supported Exchanges

| Exchange | Code | Products |
|----------|------|----------|
| Shanghai Futures Exchange | SHFE | metals, energy |
| Dalian Commodity Exchange | DCE | agricultural, chemicals |
| Zhengzhou Commodity Exchange | CZCE | agricultural, chemicals |
| China Financial Futures Exchange | CFFEX | equity index futures |
| Shanghai International Energy Exchange | INE | crude oil, iron ore |
| Guangzhou Futures Exchange | GFEX | industrial, agricultural |

### Installation

```bash
pip install bt_api_ctp
```

### Quick Start

```python
from bt_api_py import BtApi

api = BtApi(exchange_kwargs={
    "CTP___FUTURE": {
        "user_id": "your_user_id",
        "password": "your_password",
        "broker_id": "your_broker_id",
        "md_front": "tcp://182.254.243.31:30011",
        "td_front": "tcp://182.254.243.31:30001",
    }
})

api.connect()
api.subscribe("CTP___FUTURE___rb2401", [{"topic": "tick", "symbol": "rb2401"}])
data_queue = api.get_data_queue("CTP___FUTURE")
msg = data_queue.get(timeout=10)
```

### API Reference

#### CtpGatewayAdapter

The main gateway adapter for CTP futures trading.

**Constructor Parameters:**

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `md_front` | str | `""` | Market data front address |
| `td_front` | str | `""` | Trade front address |
| `user_id` | str | `""` | CTP user ID |
| `password` | str | `""` | CTP password |
| `broker_id` | str | `""` | Broker ID |
| `auth_code` | str | `"0000000000000000"` | Auth code |
| `app_id` | str | `"simnow_client_test"` | App ID |
| `gateway_startup_timeout_sec` | float | `10.0` | Connection timeout |

**Methods:**

| Method | Description |
|--------|-------------|
| `connect()` | Connect to market data and trade fronts |
| `disconnect()` | Disconnect from all fronts |
| `subscribe_symbols(symbols)` | Subscribe to market data for given instruments |
| `get_balance()` | Query account balance/margin |
| `get_positions()` | Query all positions |
| `place_order(payload)` | Place a futures order |
| `cancel_order(payload)` | Cancel an existing order |

#### place_order Payload

```python
{
    "symbol": "rb2401.SHFE",     # instrument.exchange or instrument
    "side": "buy",               # "buy" or "sell"
    "size": 1,                   # order volume
    "price": 4000.0,             # limit price
    "offset": "open",            # "open", "close", "close_today"
    "client_order_id": "...",    # optional
    "exchange_id": "SHFE",       # optional
}
```

#### cancel_order Payload

```python
{
    "symbol": "rb2401.SHFE",
    "order_id": "...",
    "external_order_id": "...",
    "front_id": 1,
    "session_id": 123456,
    "order_ref": "...",
    "exchange_id": "SHFE",
}
```

#### Order Response

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

CTP uses `CTP_ENV` to select SimNow environment:

| Env | Description |
|-----|-------------|
| `auto` | Auto-select based on current time |
| `set1` | Production-like (trading hours) |
| `set2` | 7x24 (non-trading hours) |

Leave `md_front`/`td_front` empty to use auto-selected SimNow addresses.

### Feed Classes

| Class | Description |
|-------|-------------|
| `CtpRequestDataFuture` | REST-style requests (place order, query balance/position) |
| `CtpMarketStream` | Real-time market data streaming |
| `CtpTradeStream` | Trade and order event streaming |

### Container Classes

| Class | Description |
|-------|-------------|
| `CtpTickerData` | Real-time tick data |
| `CtpOrderData` | Order status updates |
| `CtpTradeData` | Trade/fill updates |
| `CtpPositionData` | Position data |
| `CtpAccountData` | Account/balance data |

---

## 中文

欢迎使用 bt_api 的 CTP（中国期货）文档。

### 概述

CTP（综合交易平台）是中国期货交易所的标准交易系统。本插件通过 CTP v6.x 协议连接到上海期货交易所、大连商品交易所、郑州商品交易所、中国金融期货交易所、上海国际能源交易中心和广州期货交易所。

### 支持的交易所

| 交易所 | 代码 | 品种 |
|--------|------|------|
| 上海期货交易所 | SHFE | 金属、能源 |
| 大连商品交易所 | DCE | 农产品、化工 |
| 郑州商品交易所 | CZCE | 农产品、化工 |
| 中国金融期货交易所 | CFFEX | 股指期货 |
| 上海国际能源交易中心 | INE | 原油、铁矿石 |
| 广州期货交易所 | GFEX | 工业品、农产品 |

### 安装

```bash
pip install bt_api_ctp
```

### 快速开始

```python
from bt_api_py import BtApi

api = BtApi(exchange_kwargs={
    "CTP___FUTURE": {
        "user_id": "your_user_id",
        "password": "your_password",
        "broker_id": "your_broker_id",
        "md_front": "tcp://182.254.243.31:30011",
        "td_front": "tcp://182.254.243.31:30001",
    }
})

api.connect()
api.subscribe("CTP___FUTURE___rb2401", [{"topic": "tick", "symbol": "rb2401"}])
data_queue = api.get_data_queue("CTP___FUTURE")
msg = data_queue.get(timeout=10)
```

### API 参考

#### CtpGatewayAdapter

CTP 期货交易的主要网关适配器。

**构造函数参数：**

| 参数 | 类型 | 默认值 | 描述 |
|------|------|--------|------|
| `md_front` | str | `""` | 行情前置地址 |
| `td_front` | str | `""` | 交易前置地址 |
| `user_id` | str | `""` | CTP 用户 ID |
| `password` | str | `""` | CTP 密码 |
| `broker_id` | str | `""` | 经纪公司 ID |
| `auth_code` | str | `"0000000000000000"` | 认证码 |
| `app_id` | str | `"simnow_client_test"` | App ID |
| `gateway_startup_timeout_sec` | float | `10.0` | 连接超时时间 |

**方法：**

| 方法 | 描述 |
|------|------|
| `connect()` | 连接行情和交易前置 |
| `disconnect()` | 断开所有前置连接 |
| `subscribe_symbols(symbols)` | 订阅指定合约的行情数据 |
| `get_balance()` | 查询账户资金/保证金 |
| `get_positions()` | 查询所有持仓 |
| `place_order(payload)` | 下达期货订单 |
| `cancel_order(payload)` | 撤销现有订单 |

#### place_order Payload

```python
{
    "symbol": "rb2401.SHFE",     # 合约代码.交易所 或 合约代码
    "side": "buy",               # "buy" 或 "sell"
    "size": 1,                   # 委托数量
    "price": 4000.0,             # 限价
    "offset": "open",            # "open", "close", "close_today"
    "client_order_id": "...",    # 可选
    "exchange_id": "SHFE",       # 可选
}
```

#### cancel_order Payload

```python
{
    "symbol": "rb2401.SHFE",
    "order_id": "...",
    "external_order_id": "...",
    "front_id": 1,
    "session_id": 123456,
    "order_ref": "...",
    "exchange_id": "SHFE",
}
```

#### 订单响应

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

CTP 使用 `CTP_ENV` 选择 SimNow 环境：

| 环境 | 描述 |
|-----|------|
| `auto` | 根据当前时间自动选择 |
| `set1` | 类生产环境（交易时段）|
| `set2` | 7x24（非交易时段）|

留空 `md_front`/`td_front` 将使用自动选择的 SimNow 地址。

### Feed 类

| 类 | 描述 |
|---|------|
| `CtpRequestDataFuture` | REST 风格请求（下单、查询资金/持仓）|
| `CtpMarketStream` | 实时行情流 |
| `CtpTradeStream` | 交易和订单事件流 |

### 容器类

| 类 | 描述 |
|---|------|
| `CtpTickerData` | 实时tick数据 |
| `CtpOrderData` | 订单状态更新 |
| `CtpTradeData` | 成交/成交更新 |
| `CtpPositionData` | 持仓数据 |
| `CtpAccountData` | 账户/资金数据 |
