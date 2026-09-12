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

## 离线验证

`tests/test_instrument_queries.py` 覆盖原生请求字段、多个响应包、终包、拒绝、超时、
旧 SPI、快照独立性、同客户端代理、零写计数、类型映射与缺失字段。
这些故障注入测试证明 Python 查询契约，不证明任何实际柜台支持、新鲜行情或交易能力。
