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

This package provides **CTP exchange plugin for bt_api** for the [bt_api](https://github.com/cloudQuant/bt_api_py) framework. It offers a unified interface for interacting with **CTP (China Futures)** exchange.

### Features

- Futures trading via CTP protocol
- Support for Chinese futures exchanges
- Real-time market data
- Order placement for futures
- Position and margin tracking

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
from bt_api_ctp import CTPApi

# Initialize
feed = CTPApi(api_key="your_key", secret="your_secret")

# Get ticker data
ticker = feed.get_ticker("rb2401")
print(ticker)
```

### Supported Operations

| Operation | Status |
|-----------|--------|
| Ticker | ✅ |
| OrderBook | ✅ |
| Trades | ✅ |
| Bars/Klines | ✅ |
| Orders | ✅ |
| Balances | ✅ |
| Positions | ✅ |

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
├── src/bt_api_ctp/     # Source code
│   ├── containers/     # Data containers
│   ├── feeds/          # API feeds
│   ├── gateway/       # Gateway adapter
│   └── plugin.py      # Plugin registration
├── tests/             # Unit tests
└── docs/             # Documentation
```

### License

MIT License - see [LICENSE](LICENSE) for details.

### Support

- Report bugs via [GitHub Issues](https://github.com/cloudQuant/bt_api_ctp/issues)
- Email: yunjinqi@gmail.com

---

## 中文

### 概述

本包为 [bt_api](https://github.com/cloudQuant/bt_api_py) 框架提供 **CTP exchange plugin for bt_api**。它提供了与 **CTP期货** 交易所交互的统一接口。

### 功能特点

- 通过CTP协议的期货交易
- 支持中国期货交易所
- 实时市场数据
- 期货订单下达
- 持仓和保证金跟踪

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
from bt_api_ctp import CTPApi

# 初始化
feed = CTPApi(api_key="your_key", secret="your_secret")

# 获取行情数据
ticker = feed.get_ticker("rb2401")
print(ticker)
```

### 支持的操作

| 操作 | 状态 |
|------|------|
| Ticker | ✅ |
| OrderBook | ✅ |
| Trades | ✅ |
| Bars/Klines | ✅ |
| Orders | ✅ |
| Balances | ✅ |
| Positions | ✅ |

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
├── src/bt_api_ctp/     # 源代码
│   ├── containers/     # 数据容器
│   ├── feeds/          # API 源
│   ├── gateway/        # 网关适配器
│   └── plugin.py       # 插件注册
├── tests/             # 单元测试
└── docs/             # 文档
```

### 许可证

MIT 许可证 - 详见 [LICENSE](LICENSE)。

### 技术支持

- 通过 [GitHub Issues](https://github.com/cloudQuant/bt_api_ctp/issues) 反馈问题
- 邮箱: yunjinqi@gmail.com
