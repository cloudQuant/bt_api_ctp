# CTP 合约枚举与只读参考查询

这些方法复用 `TraderClient` 的同一连接、串行查询通道、连接代次和终包累积器。
查询不进行结算确认、报单或撤单，不改变交易解锁条件。独立研究程序构造客户端时必须显式传入
`auto_settlement_confirm=False`，登录就绪应检查 `is_read_only_ready`；这不代表
`is_trading_ready`。

## 接口

| 方法 | 参数默认值 | 请求类型 / 原生方法 |
|---|---|---|
| `query_instruments_result` | `instrument_id="", exchange_id="", product_id="", timeout=5` | `instruments` / `ReqQryInstrument` |
| `query_depth_market_data_result` | `instrument_id="", exchange_id="", timeout=5` | `depth_market_data` / `ReqQryDepthMarketData` |
| `query_instrument_margin_rate_result` | 必需 `instrument_id`；`exchange_id="", hedge_flag="1", timeout=5` | `margin_rate` / `ReqQryInstrumentMarginRate` |
| `query_instrument_commission_rate_result` | 必需 `instrument_id`；`exchange_id="", timeout=5` | `commission_rate` / `ReqQryInstrumentCommissionRate` |
| `query_option_instrument_trade_cost_result` | 必需 `instrument_id`；`exchange_id="", hedge_flag="1", input_price=0.0, underlying_price=0.0, timeout=5` | `option_trade_cost` / `ReqQryOptionInstrTradeCost` |
| `query_option_instrument_commission_rate_result` | 必需 `instrument_id`；`exchange_id="", timeout=5` | `option_commission_rate` / `ReqQryOptionInstrCommRate` |

`CtpRequestData` / `CtpRequestDataFuture` 提供上述 typed 查询的同名代理。
已经使用 `BtApi` 的应用应继续通过现有公共 `query_ctp_result` 入口及对应查询类型调用，
不要为了读取期权信息再创建第二个交易连接。部署时需要确认上层 SDK 版本包含对应类型映射。

全合约查询不设 `ProductClass` 过滤，也不根据合约名排除期权或组合：

```python
# client 是应用已建立的只读 TraderClient；这里不新建连接。
result = client.query_instruments_result(timeout=120)
session = client.get_session_state()
if (
    not result.complete
    or result.connection_generation != session["connection_generation"]
    or result.account_fingerprint != session["account_fingerprint"]
):
    raise RuntimeError("instrument reference evidence incomplete or stale")

futures = [row for row in result.records if row["asset_type"] == "future"]
options = [row for row in result.records if row["asset_type"] == "option"]
```

“全部”指当前柜台、账号、交易日可见且服务器返回的集合，不能宣称覆盖交易所全部已上市合约。
空过滤的行情查询含义相同；它是 TD 参考快照，完整终包不证明报价新鲜、三腿同步或可成交。

## 合约字段

`query_instruments_result.records` 同时保留原生字段与规范化字段。规范化纯函数也可单独导入：

```python
from bt_api_ctp import normalize_ctp_instrument
metadata = normalize_ctp_instrument(native_instrument_record)
```

| 原生字段 | 规范化字段 |
|---|---|
| `InstrumentID`, `ExchangeID`, `ProductID` | `instrument_id`, `exchange_id`, `product_id` |
| `InstrumentName`, `ExchangeInstID` | `instrument_name`, `exchange_instrument_id` |
| `ProductClass` | `product_class`, `asset_type`, `contract_type` |
| `VolumeMultiple`, `PriceTick` | `multiplier`, `price_tick` |
| `UnderlyingInstrID`, `UnderlyingMultiple` | `underlying_instrument`, `underlying_multiple` |
| `StrikePrice`, `OptionsType` | `strike_price`, `option_type`（`call` / `put`） |
| `CreateDate`, `OpenDate`, `ExpireDate` | `create_date`, `open_date`, `expiry_date` |
| `StartDelivDate`, `EndDelivDate` | `start_delivery_date`, `end_delivery_date` |
| `DeliveryYear`, `DeliveryMonth` | `delivery_year`, `delivery_month` |
| `InstLifePhase`, `IsTrading` | `life_phase`, `is_trading`, `status` |
| `MinLimitOrderVolume`, `MaxLimitOrderVolume` | `min_limit_order_volume`, `max_limit_order_volume` |
| `MinMarketOrderVolume`, `MaxMarketOrderVolume` | `min_market_order_volume`, `max_market_order_volume` |

类型直接采用随包 CTP `ThostFtdcUserApiDataType.h` 枚举：`1=future`、`2=option`、
`3=combination`、`4=spot`、`5=efp`、`6=spot_option`、`7=tas`、`I=mi`。
其他值或缺失为 `unknown`，不从符号名称猜测类型。
`IsTrading` 缺失不会被当作可交易。生命周期是参考状态，不是实时交易时段状态。
正值价格/数量字段遇到缺失、非有限值、非正值或 CTP `DBL_MAX` 哨兵时规范化为 `None`，
原生字段仍被保留供审计。

`exercise_style=None`：InstrumentField 不证明美式/欧式行权。`ExpireDate` 也不能替代完整
行权窗口、交割/结算类型和交易日历。现有 `trading_days_to_expiry`、前一交易日成交量等
证据仍保持缺失，必须由独立的、带来源和日期的规则/行情记录补齐。

## 费用与完成语义

期权费用查询返回原生 `FixedMargin`、`MiniMargin`、`Royalty` 及交易所对应字段；
手续费查询保留开仓、平仓、平今和行权手续费字段。它们不自动合成组合保证金，
不允许把期权卖方资金需求套用为期货的简单保证金比例。
`input_price` 与 `underlying_price` 接受有限非负数；默认零值按原生零值传递，
不能据此宣称使用当前市场价格计算。研究程序应保存请求价格及对应来源/时间。

所有方法返回 `QueryResult`，包括 `request_type`、`request_id`、`connection_generation`、
`account_fingerprint`、`is_last_seen`、`complete`、`timed_out`、`unsupported`、错误码和
脱离原生回调对象的 `records`。超时或错误可包含部分记录；只有完整成功结果才有完整集合语义。
调用方组合不同查询时必须再验证相同且当前的账号、连接代次与交易日。
原生 API/必要字段缺失返回不完整的 `unsupported` 结果，不丢弃过滤字段改查全市场。

`get_request_counts()` 新增 `query_depth_market_data`、`query_option_trade_cost`、
`query_option_commission_rate`。实际只读运行应保留运行前后计数，并验证
`settlement_confirm`、`order_insert`、`order_action` 都为零。

## 下单路径与合约目录（迭代 62，I62-12a）

下单路径从不启动全量合约查询。`CtpDirectWriteProvider.resolve_exchange_id(symbol)` 解析下单交易所时：

- 当日目录已在内存中，或已由任一进程发布（本机共享文件）时，使用该目录；目录里缺少的合约按需做单合约查询并补入；
- 否则直接做单合约查询（`ReqQryInstrument`，只带 `InstrumentID`）。

每个来源范围、每个 CTP 交易日的那一次全量 `ReqQryInstrument` 只由 `CtpDirectWriteProvider.warm_instrument_catalog()`
执行，宿主应在该会话没有其他查询时调用（例如开盘前）；其他进程复用发布的文件。原因：SimNow 上（2026-11-06 观察）全量查询在 60 秒内只返回
不到 2,000 行（合约总数超过 10,000），期间还占着该会话唯一的查询槽位（见下）。
目录条目只是参考数据，不是报价、持仓、当前交易状态或交易权限的证据。

## 查询槽位（迭代 62，I62-14）

每个交易会话同一时刻只有一笔在途原生查询。本地超时（封存）并不会取消柜台那一侧的请求，
在它的最后一个响应包（包括封存之后迟到的终包或错误响应）到达之前，它仍占着槽位：

- 新查询在自己的超时和截止时间内等待槽位释放，再提交；
- 等不到时，本地失败并返回 `query_slot_busy`（`timed_out=True`，完成来源 `query_slot_busy`），不发送原生请求，
  因此不会出现连续的 `-2` 拒绝；
- 断线、客户端停止、连接代次变化或提交失败会清除槽位；在 SPI 回调线程上发起的查询不等待（终包由同一线程送达），
  槽位忙时立即失败。

槽位上的等待受本次查询的 `timeout` 和 `deadline_monotonic` 约束，并响应取消；它不改变既有的查询间隔和串行通道。

## 交易权限事实（迭代 62，I62-13）

`CtpDirectWriteProvider.trading_permission_facts(symbols, exchange_ids=None, include_rates=True, timeout=5.0, max_duration=None)`
返回 `TradingPermissionFacts`（`bt_api_ctp.trading_permission_facts`；类型是 provider 本地的中立类型，不含账号和原生字段名；
由 `bt_api_ctp.trading_permission_projection` 从原生行投影）。它只用只读、带过滤的查询，不查全量目录：

| 事实 | 原生查询 | 次数 |
|---|---|---|
| `investor_active` | `query_investor_result` / `ReqQryInvestor` | 1 |
| `instrument_trading` | `query_instruments_result(instrument_id=...)` / `ReqQryInstrument` | 每个合约 1 |
| `trading_code` | `query_trading_codes_result(exchange_id=...)` / `ReqQryTradingCode` | 每个交易所 1 |
| `commission_rate` | `query_instrument_commission_rate_result` / `ReqQryInstrumentCommissionRate` | 每个合约 1（`include_rates`） |
| `margin_rate` | `query_instrument_margin_rate_result`（`HedgeFlag` 缺省 `1`，投机）/ `ReqQryInstrumentMarginRate` | 每个合约 1（`include_rates`） |

N 个合约、E 个交易所、读取费率时共 `1 + 3N + E` 次原生查询（`native_queries`）；不读费率时为 `1 + N + E`。
例如两个上期所合约（同一交易所）为 `1 + 3×2 + 1 = 8` 次。每次查询都经过上面的槽位和既有的查询间隔（默认 1.05 秒，
环境变量 `BT_API_PY_CTP_QUERY_INTERVAL_SEC` 可改），所以整次读取约每秒一个查询。

`query_investor_result` 与 `query_trading_codes_result` 的返回值同样是 `QueryResult`，请求类型分别为 `investor`、`trading_code`；
`TraderClient` 提供这两个方法；`CtpRequestData`（及其子类 `CtpRequestDataFuture`）提供同名代理，参数只接受关键字（`timeout`，以及 `exchange_id`）。
这两个查询类型不在 `BtApi.query_ctp_result` 的类型映射（`query_result_evidence.ctp_query_method_name`）里；
已经使用 `BtApi` 的应用通过只读入口 `BtApi.direct_trading_permissions(exchange_name, symbols, **kwargs)` 取得上述事实，不需要写入准入。

每个事实是 `known_positive`、`known_negative` 或 `unknown`，并带有限原因码；缺失、失败、超时或过期的查询使该事实为 `unknown`。
只有 `investor_active`、`trading_code`、`instrument_trading` 可以是 `known_negative`（投资者未激活；交易所上没有任何有效交易编码；
合约不在交易、已到期或未开始），缺少费率行不是否定。单个合约的结果 `outcome`：

| `outcome` | 含义 |
|---|---|
| `known_negative` | 至少一个可否定事实明确为否定 |
| `no_negative_found` | 该合约读取到的所有事实（含费率事实）都是 `known_positive`（最好的结果） |
| `unknown` | 其余情况（例如缺少费率行：它不是否定，但使结果不能达到 `no_negative_found`） |

`proves_permission` 恒为 `False`（`TradingPermissionFacts` 构造时拒绝其他取值）：激活的投资者或有效交易编码只是单项输入，不是交易权限；
柜台风控和场所仍是最终判定方。事实绑定到一个会话代次和一个交易日，读取期间会话变化则所有事实为 `unknown`（`session_consistent=False`），
使用方必须把其他会话或交易日的事实当作 `unknown`。`max_duration` 限制整次读取，到时仍未开始的查询对应的事实保持 `unknown`。
直连下单使用投机（speculation）的 `HedgeFlag`，所以只有有效的投机交易编码算正面；有效但不含投机的编码保持 `unknown`。

## 离线验证

`tests/test_instrument_queries.py` 覆盖原生请求字段、多个响应包、终包、拒绝、超时、
旧 SPI、快照独立性、同客户端代理、零写计数、类型映射与缺失字段。
这些故障注入测试证明 Python 查询契约，不证明任何实际柜台支持、新鲜行情或交易能力。

迭代 62 的 `tests/test_iter62_instrument_catalog_cache.py`、`tests/test_iter62_query_slot.py`、
`tests/test_iter62_trading_permissions.py` 分别覆盖合约目录缓存与下单路径、查询槽位、交易权限事实，
证据等级为 UNIT/FAKE（假原生 API；目录缓存另有本机多进程、临时目录），无网络、无账户。本次文档更新没有运行这些测试。
SimNow 上的观察来自迭代 62 的运行记录，不是这些测试的结果；本节没有真实账户证据。
