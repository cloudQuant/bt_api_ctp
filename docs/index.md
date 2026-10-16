# CTP (China Futures) Documentation

## English

### Overview

bt_api_ctp provides the native CTP futures provider, direct request feed, market stream, and typed containers. The plugin registers through the explicit PluginLoader call to register_plugin(registry); importing the package does not register it by itself.

### Native QuoteV2 feed

CtpMarketStream emits CtpTickerData rows with QuoteV2 values, event/receive clock facts, source, subscription epoch, ingest sequence, and option metadata when reference data supplies it. ActionDay fallback is marked ambiguous. The stream computes delta volume from cumulative CTP volume exactly once and leaves its baseline unchanged for invalid quotes. Thin and locked books carry quality flags. execution_eligible stays false unless parent authority qualifies the event.

The package's direct feed can read market, account, order, and contract data. The legacy standalone gateway package remains in the source tree pending the complete migration, but is retired and unsupported as a public integration. The direct feed does not provide a complete ServiceHost MarketHub/managed-account projection. Managed live writes belong to ServiceExecutionSession through service-v2 after host integration.

### Environment and data collection

CTP_ENV and the env parameter select the supported SimNow profile. Leave front addresses empty to use the package's environment selector. The collection flow stores native tick records and Parquet output; see the README for the storage layout and trading-day diagnostics.

### API reference

- Native feed: feeds/live_ctp_feed.py
- Ticker projection: containers/ctp/ctp_ticker.py
- Contract evidence: instrument.py
- Environment selector: ctp_env_selector.py

## 中文

### 概述

bt_api_ctp 提供原生 CTP 期货 provider、直连请求 feed、行情流和类型化数据容器。插件由 PluginLoader 显式调用 register_plugin(registry) 注册；单独导入包不会自动修改注册表。

### 原生 QuoteV2 行情流

CtpMarketStream 输出 CtpTickerData，保留 QuoteV2 字段、事件/接收时钟信息、来源、订阅代次、摄取序号，以及参考合约资料提供的期权元数据。ActionDay 回退会标记日期歧义。行情流只将 CTP 累计量转换一次为增量；无效行情不会推进成交量基线。过薄或锁定盘口会设置质量标记。除非父级权威明确认证，execution_eligible 始终为 false。

包内直连 feed 可读取行情、账户、订单和合约资料。完整迁移尚未完成，旧独立网关包仍保留在源码树中，但已退役，不再作为受支持的公共集成接口。直连 feed 也不提供完整的 ServiceHost MarketHub/托管账户投影。宿主完成集成后，托管实盘写操作应通过 service-v2 的 ServiceExecutionSession。

### 环境与数据采集

CTP_ENV 和 env 参数用于选择受支持的 SimNow 配置。将前置地址留空可使用包内环境选择器。采集流程保存原生 tick 记录和 Parquet 文件；存储布局及交易日诊断见 README。

### API 参考

- 原生 feed：feeds/live_ctp_feed.py
- 行情投影：containers/ctp/ctp_ticker.py
- 合约凭据：instrument.py
- 环境选择器：ctp_env_selector.py
