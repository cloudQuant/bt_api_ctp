# CTP (China Futures)

Position-mode acknowledgement codes are provider declarations: the Base default is unspecified. Binance, CTP and non-swap OKX preserve the previous `200`; OKX swap preserves `0`, and ten historical policies retain `200`. These inherited values do not prove venue capability. Third-party Base subclasses no longer receive a default; dynamic policies, packaging and behavior remain unvalidated.

CTP recovery row instrument/side/offset fields, active-order classification, order/intent matching and trade-key projection live in `bt_api_ctp.recovery_row_projection`; the SDK supplies its original value selector, quantity checks and instrument resolver, retaining proof, journal, session and authorization. The provider participates in field decisions; import timing and behavior remain unvalidated.

CTP managed-runtime route and instrument-field validation lives in `bt_api_ctp.managed_runtime_route_validation`; the SDK keeps grant, session and lifecycle checks. This source move is unaccepted. `bt_api_ctp.preflight_settings` owns the automatic settlement-confirm setting check and explicit disablement; the SDK keeps route selection, call timing and error mapping. This source move is unaccepted.

Execution-ledger identity format is declared per provider via `bt_api_base.execution_ledger_identity_policy`, not hard-coded venue names. The 13 built-in providers retain their prior format choices; this does not prove identity. Missing or undiscoverable declarations fail, including historical/custom providers, and successful selections are process-cached. Migration, packaging and behavior remain unaccepted. A separate provider `account_risk_source_policy` declares whether native account binding is still required for neutral risk reads. CTP remains unavailable; the other 12 retain the old branch, not proof of source. Undeclared providers fail and discovery/behavior are unaccepted.

CTP managed Risk order payload construction and cancel-target matching live in `bt_api_ctp.risk_dispatch_binding`; Base declares the optional provider contract, SDK selects evidence and the Risk journal retains claims and fencing. Target orders with a schema now require a matching provider declaration: undeclared schemas fail, a breaking change. Journal compatibility and behavior remain unvalidated. The CTP provider also owns native request field comparison, dispatch/zero-fill projections and zero-fill evidence field matching; SDK retains session, exact type/source checks and journal lifecycle. The provider is trusted for field matching. Risk-order position-dimension source projection is selected by the provider policy, without an SDK venue-name branch. CTP declares the projection at `bt_api_ctp.risk_source_position_projection`; the other 12 built-ins retain the previous generic path. Missing declarations or required projectors fail closed, so third-party policies need updates. SDK retains source/session checks and error mapping; provider discovery and behavior are unvalidated.

Order-status and trade-fee field candidates are selected by provider policy. Binance, OKX, CTP and the historical policy retain the prior aliases and order; a policy without declarations fails explicitly. Third-party policies, field coverage and behavior remain unvalidated. Placement position-mode fencing is also provider-declared: OKX/Binance retain the existing guard, other built-in routes retain the previous unguarded behavior, and undeclared routes reject placement. This breaking change does not prove venue capability.

CTP managed order and cancel identity value types live in `bt_api_ctp.managed_request_identity`. SDK request models resolve them lazily when those fields are used, with lazy legacy SDK exports. Module identity, serialization, annotation resolution, source binding, optional packaging and behavior remain unaccepted; these values grant no execution authority.

The working-tree normalizer explicitly declares CTP tick trading/action day aliases. The working-tree normalizer declares its native order identity aliases, order provenance fields and scoped v5/v6 source schemas; field coverage and behavior remain unvalidated.

The working-tree `simnow_execution_projection` owns native order, cancel and query value types, mappings and native query collection; the SDK supplies its unified request constructor and retains session composition. Cross-package imports and behavior remain unvalidated.

The working-tree client delegates SDK approval, binding comparisons and selected-front scope to the SDK while retaining native session checks and lock ownership. The remaining SDK dependency and retired runtime provenance are unresolved; this source change is not behavior-accepted.

The working-tree managed order-query field and open/terminal row projections are CTP-owned; the SDK still holds the source fence and evidence issuance. The provider is a dependency for target field-consistency validation, which has not been behavior-validated.

The working-tree terminal-order query value contracts are provider-owned; the SDK reader retains source-bound issuance and readback. The optional managed Execution dependency, class origins, and behavior remain unvalidated.

Managed CTP native order identifier encoding now belongs to `bt_api_ctp.managed_order_identifier`; it requires the optional managed Execution dependency and does not establish order authority. Its SDK integration remains unvalidated.

The working-tree Trader connection configuration type resides in the CTP provider. The SDK CTP managed-runtime module imports the same type; the changed module path and distribution behavior remain unvalidated.

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

### Provider and framework boundary

The working-tree `execution_instrument` module owns CTP execution and native-wire instrument normalization. The SDK retains its proof and authorization checks and loads the provider parser lazily; the client uses the same parser. The shared parser changes a trust dependency and has not passed behavior or distribution acceptance.

This package implements the native CTP client, request feed, market/trade stream adapters, and typed provider data. Its `bt_api.plugins` entry point registers only `CTP___FUTURE` feeds, exchange data, balance handling, and stream handlers when `bt_api_base.PluginLoader` loads it; importing the package alone does not register it. `bt_api_py` provides the `BtApi` facade and event queue, while Backtrader supplies the strategy and engine layer. Managed live-order execution uses `ServiceExecutionSession`/service-v2 only when this provider is integrated with ServiceHost; provider APIs or SimNow support do not establish that integration or real-account acceptance. See [Trading System Architecture](https://github.com/cloudQuant/backtrader/blob/dev/docs/TRADING_SYSTEM_ARCHITECTURE.md).

The working-tree CTP request feed also exposes verified stream configuration through a provider-owned hook. Native quote V2 schema declaration, receipt matching and timestamp projection belong to the provider; the SDK selects the route policy and retains ingress binding and its attestation issuer. The SDK integration is source work in progress and is not runtime or account acceptance.

CTP native query-method selection and public session-field projection reside in `bt_api_ctp.query_result_evidence`; the SDK retains route and transport guards. This source migration has not been behavior-validated.

CTP recovery-row account/day field selection and the existing active-order status projection are provider-owned. The status interpretation and SDK integration have not been behavior-validated.

`trade_stream_readiness` selects the CTP trade stream and compares its Trader/feed native session facts. The SDK keeps subscription state and approval authority; source and runtime behavior remain unvalidated.

`managed_dispatch_contracts` holds CTP managed request/binding and native sender/result contracts. `managed_dispatch_worker_binding` owns request staging, OrderRef and binding checks as well as the SDK owner’s CTP queue receipt, request/target matching and dispatch receipt construction. SDK retains owner thread, session and authorization; provider field comparisons are trust dependencies and remain unvalidated. The durable claim/UNKNOWN/completion worker remains Execution-owned and composes its mixin. This source change has not passed import, distribution or behavior acceptance.

The optional managed integration also owns the CTP callback session, native-source bridge and callback-ledger adapter. Execution still owns durable storage, lease and event types. These source moves have not passed import, distribution or behavior acceptance.

`callback_ingress_projection` owns CTP ingress snapshot schema/digest checks and native field reads; Execution supplies its digest-shape validator and error type and retains owner, transaction and persistent state. The provider participates in source validation; behavior remains unaccepted. `execution_callback_mapping` owns CTP native callback field parsing and maps into Execution-owned typed correlation/events. Its optional `managed_execution` extra selects Execution; the maps and installation have not been behavior-validated.

The CTP provider constructs native submit and cancel fields from already claimed payloads in `native_order_fields`; the SDK owner keeps session binding and dispatch authorization. This source migration is not native or behavior acceptance.

The working-tree CTP risk-dispatch evidence entry point declares the existing persisted fields and schema for the exact `ctp` Risk provider. The SDK journal still checks claim, chain and fence; the entry point and behavior have not been validated.

CTP dispatch binding and terminal proof/ack schemas, claim-field matching, zero-fill/terminal evidence and order/fill observation validation reside in `bt_api_ctp.risk_dispatch_binding`; the SDK retains and supplies the digest validator, journal and risk types, risk claim and writer fence. This source migration is not behavior or distribution acceptance.

CTP recovery action and remaining-allowance field matching reside in `bt_api_ctp.recovery_plan_validation`; the SDK retains session, approval and authorization lifecycles. These comparisons do not grant authorization, and the source migration has not been behavior-validated.

CTP query terminal-packet validation and native account-row projection are provider-owned; the SDK retains the session authorization guards. The source migration has not been behavior-validated.

The working-tree active-front credential binding checks for native CTP clients and authenticated market identity reside in `bt_api_ctp.credential_binding_fronts`; the SDK supplies its own live state and error boundary. This migration is not runtime or account acceptance.

The working-tree isolated SimNow trader probe resides in `bt_api_ctp.simnow_probe` and is available through the lazy `bt_api_py.ctp_probe` entry. Its moved subprocess and installation behavior have not been validated.

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

### Tick Collection and Parquet Storage

The package ships an exchange-agnostic collector that subscribes to the **whole market**
(futures + options, all six exchanges) and persists every depth snapshot to Parquet:

```bash
python -m bt_api_ctp.collector --config collector.yaml --until-close --wait-open
```

Run it before the open; it waits for the session, collects to the close (day session
ends 15:15, night session 02:30), then writes the completeness report and exits by
itself — designed for cron/systemd/launchd scheduling. See
[`deploy/collector/README.md`](deploy/collector/README.md) for ready-to-use units and
the checklist (holiday calendar file, logging, exit codes).

Files land as `<data_root>/<trading_day>/<exchange>/<instrument_id>.parquet`. The
directory names the **trading day**: Friday night data belongs to the following
Monday and is stored next to Monday's day session, matching the exchange calendar.

**Reading the data** — one file per instrument, one row per snapshot, sorted by
`(action_day, update_time, update_millisec)` and deduplicated:

```python
import pyarrow.parquet as pq

table = pq.read_table("20260916/SHFE/rb2701.parquet")
rows = table.to_pylist()
# Key columns per row:
#   trading_day / action_day        "20260916" (night ticks: action_day = previous day)
#   update_time / update_millisec   "21:00:01" / 500        quote time, ms precision
#   local_receive_time              1789539477726693000     local wall clock (ns)
#   last_price, volume, turnover, open_interest
#   open/high/low/average_price, upper_limit/lower_limit
#   pre_settlement/pre_close/pre_open_interest
#   bid_price_1..5 / bid_volume_1..5 / ask_price_1..5 / ask_volume_1..5
# Missing values are NULL (CTP sends DBL_MAX for absent prices; it is normalized).

# With pandas:
import pandas as pd
df = pq.read_table("20260916/SHFE/rb2701.parquet").to_pandas()
```

`<data_root>/<trading_day>/report.json` lists per-instrument row counts, first/last
quote time and in-session gaps — scheduled breaks (10:15-10:30, lunch, overnight)
are not counted as gaps.

### Native CTP market feed and QuoteV2

Use the package's native CTP market feed for direct quote collection. QuoteV2 rows retain source, clock, cumulative-volume, depth, and quality evidence; execution eligibility remains controlled by parent authority.

### Environment selection

Select the front pair with `CTP_ENV` or the `env` parameter. Leave `md_front` and `td_front` empty to use the selector's configured front pair.

| Value | Selection |
| --- | --- |
| `auto` | Default; select the configured front pair for the current session. |
| `set1` | Production-session front pair. |
| `set2` | 7×24 / non-trading-hours front pair. |

Live order execution is managed through `ServiceExecutionSession` / service-v2 after this provider is integrated with ServiceHost.

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

### Provider 与框架职责边界

本包实现原生 CTP 客户端、请求 feed、行情/交易流适配及类型化 provider 数据。`bt_api.plugins` 入口由 `bt_api_base.PluginLoader` 加载时，仅注册 `CTP___FUTURE` 的 feed、交易所数据、余额处理器和流处理器；单独导入不会注册。`bt_api_py` 提供 `BtApi` 外观和事件队列，Backtrader 提供策略与引擎层。只有将该 provider 接入 ServiceHost 后，managed 实盘订单执行才由 `ServiceExecutionSession`/service-v2 管理；provider API 或 SimNow 支持本身不代表已完成该集成或通过真实账户验收。参见[交易系统架构](https://github.com/cloudQuant/backtrader/blob/dev/docs/TRADING_SYSTEM_ARCHITECTURE.md)。

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

### 全市场 Tick 采集与 Parquet 落盘

内置交易所解耦的采集器，可订阅**全市场**（期货 + 期权，六大交易所）并把每条
深度行情快照写入 Parquet：

```bash
python -m bt_api_ctp.collector --config collector.yaml --until-close --wait-open
```

在开盘前启动即可：脚本等待开盘、一直采到收盘（白盘 15:15、夜盘 02:30）后自动
写完整性报告并退出 —— 专为 cron/systemd/launchd 无人值守设计。开箱即用的
调度单元与检查清单（节假日日历、日志、退出码）见
[`deploy/collector/README.md`](deploy/collector/README.md)。

数据按 `<data_root>/<交易日>/<交易所>/<合约代码>.parquet` 落盘。目录名是
**交易日**：周五夜盘的数据属于下周一，与周一白盘数据存放在同一目录，与交易所
口径一致。

**读取数据** —— 每个合约一个文件，每行一条快照，已按
`(action_day, update_time, update_millisec)` 去重排序：

```python
import pyarrow.parquet as pq

table = pq.read_table("20260916/SHFE/rb2701.parquet")
rows = table.to_pylist()
# 每行关键列：
#   trading_day / action_day        "20260916"（夜盘 tick 的 action_day 为前一自然日）
#   update_time / update_millisec   "21:00:01" / 500        行情时间，毫秒精度
#   local_receive_time              1789539477726693000     本地墙钟接收时间（纳秒）
#   last_price、volume、turnover、open_interest
#   open/high/low/average_price、upper_limit/lower_limit
#   pre_settlement/pre_close/pre_open_interest
#   bid_price_1..5 / bid_volume_1..5 / ask_price_1..5 / ask_volume_1..5
# 缺失值为 NULL（CTP 对无价档位发送 DBL_MAX 哨兵，已归一化为 NULL）。

# 用 pandas 读取：
import pandas as pd
df = pq.read_table("20260916/SHFE/rb2701.parquet").to_pandas()
```

`<data_root>/<交易日>/report.json` 记录每个合约的行数、首末行情时间与
**交易时段内**的缺口 —— 小节休息（10:15-10:30）、午休、隔夜均不计为缺口。

### 原生 CTP 行情流与 QuoteV2

直接采集行情请使用本包的原生 CTP 行情流。QuoteV2 行保留来源、时钟、累计成交量、盘口深度和质量证据；执行资格仍由父级权威决定。

### 环境选择

可通过 `CTP_ENV` 环境变量或 `env` 参数选择前置地址。将 `md_front` 和 `td_front` 留空时，使用选择器配置的前置地址。

| 值 | 选择方式 |
| --- | --- |
| `auto` | 默认值；按当前时段选择已配置的前置地址。 |
| `set1` | 交易时段使用的前置地址。 |
| `set2` | 7×24 / 非交易时段使用的前置地址。 |

接入 ServiceHost 后，实盘订单执行由 `ServiceExecutionSession` / service-v2 管理。

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
