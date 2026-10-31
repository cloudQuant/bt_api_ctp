# 迭代 53 CTP Unknown 订单状态修复轮次记录

日期：2026-10-31
状态：源码与隔离 fake 验证完成，尚未提交。

## 范围

- 仓库：`D:\\bt_api_py\\bt_api\\bt_api_ctp`，分支 `dev`。
- 本轮基线 HEAD：`f7cdaeeee05d021d3edeacd6fe3d1d6eb225b8b9`。
- 源码：`src/bt_api_ctp/containers/ctp/ctp_order.py`、`src/bt_api_ctp/fields.py`、`src/bt_api_ctp/status.py`、`src/bt_api_ctp/feeds/live_ctp_feed.py`、`src/bt_api_ctp/errors/__init__.py`。
- 仅更新两个既有纯合同断言：`tests/test_fields.py`、`tests/test_receipts.py`。未新增持久测试，未更改 execution/live 测试。
- 本轮未提交、未暂存，未修改 Backtrader 或 `bt_api_base`。

## 状态映射合同

- CTP 原生 `OrderStatus='a'` 表示 `OST_Unknown`，SDK 现在将它映射为 `OrderStatus.UNKNOWN` / `unknown`。缺失、空白、`None` 及未来未知状态码也映射为 Unknown。
- 已知码 `0–5` 与 `b/c` 的既有映射保持不变。字段归一化函数继续保留第三个 `default` 参数供旧调用方使用，但缺失或未知状态不能通过该参数伪装成 submitted。明确的报单拒绝与撤单拒绝状态仍优先。
- `TERMINAL_ORDER_STATUSES` 从 `0/4/5/a` 收窄为 `0/4/5`；原有状态 `2` 在该表中的行为保持不变。独立 `account_provider` 路径未改。
- 当前 Backtrader 的 `RegisteredSim._handle_order` 在基础状态解析前把 `a` 作为 unresolved callback 处理并锁存 unknown；其 terminal 表懒加载 SDK 表。因此本轮 SDK 变更与当前该路径一致；未改 Backtrader。

## native 报单返回合同

官方 CTP 头文件将 `ReqOrderInsert` 声明为 `virtual int`（`ctp/api/6.7.7/windows/ThostFtdcTraderApi.h:621`）；已生成 SWIG wrapper 将原生返回强制转换为 `int`（`ctp/ctp_wrap.cpp:487296`）。Python 层 `TraderClient.submit_order_insert` 的接口标注较宽，并且只在 `type(result) is int` 时记录 `submit_code`，所以 feed 不能用 `ret == 0` 单独判定成功。

- 只有 exact built-in `int` 零返回，才通过私有工厂生成本地 `SUBMITTED` 投影。此行没有 provider `OrderStatus` 字段，`order_status_source` 仅是内存诊断，不由通用 `OrderData.get_all_data()` 持久化；它不证明 provider 回报或交易所 ACK。
- exact built-in `int` 非零返回保持原行为：返回空行的 `RequestData` 且 `status=False`。本轮没有把它改写为 unknown，也没有增加重试。
- 返回 `False`、`0.0`、`None` 或任何非 exact-int 对象时，native 调用可能已经发出，因此 `make_order` 抛出公开异常 `bt_api_ctp.errors.CtpOrderSubmissionOutcomeUnknown`，不把它转成 `status=False`，也不返回可能被消费者误当成成功的普通对象。异常带固定 `code`、`execution_unknown=True`、`definite_reject=False`，以及已在 native 调用前 exact 校验的 `client_order_id` 和正 `request_id`，再附有限返回类型名诊断；该名称不是类型判定证据（自定义类型也可以命名为 `int`）。若读取自定义 metaclass 的 `__name__` 失败或名称不合格式，诊断固定回退到 `unavailable`。不保留或格式化 malformed 原始值。
- feed 每次只调用一次 `submit_order_insert`。该异常不触发 SDK 重发。直接调用 SDK 的消费者必须保留异常属性和 client order id，将订单留在未知态并通过原身份协调；不得将异常折叠为 confirmed reject，也不得因异常自动重发。无法控制任意外部包装器丢弃属性的行为。
- `RequestData.status` 本身允许 `bool | None`，但 malformed submit 不再依赖 `status=None` 表示风险。Backtrader 当前 Store 的 SDK 命令异常路径显式读取 `execution_unknown` / `definite_reject`；command completion 保留排队时的 `client_order_id` 并锁存 unknown。BT 的 normalized-error 投影不保留此异常的 `request_id` 和返回类型名，因此后续协调必须依赖保留的 client order id。
- ExecutionSession 的 normalized mapping 可能同时含 `status='submitted'` 与 `execution_unknown=True`；消费者必须一起检查这两个字段，以 unknown 标记为准，不能只凭 status 判定已提交成功。本轮未改 BtApi/ExecutionSession mapping。任意外部包装器若丢弃异常属性或 unknown 标记，SDK 无法保证其消费者安全。

## 隔离验证

先前直接在原工作区运行的 `127 passed` 仅作诊断，不计入本轮验收：`tests/test_receipts.py` 显式导入 `bt_api_ctp`，dirty package `__init__.py` 又导入未跟踪的 `query_facts.py` 与 `simnow.py`；其他测试导入子模块时同样会执行该 initializer。

本轮随后建立干净临时快照 `C:\\Users\\yunji\\AppData\\Local\\Temp\\ctp-order-unknown-r1-3cdb4751b3774be6a779e07244c6ffab`：从本轮基线 `git archive HEAD` 解包，再只覆盖上文七个源码/既有测试文件（四个目标源码、errors 导出、两个既有断言）。快照未包含 dirty package initializer 或未跟踪依赖。运行：

```text
python -m pytest -p no:asyncio -q tests/containers/ctp/test_ctp_order.py tests/test_ctp_order_integration.py tests/test_position_dimension_projection.py tests/test_fields.py tests/test_receipts.py
```

结果：`127 passed, 1 warning, 0.82s`。唯一警告是当前 pytest 未识别项目现有配置项 `asyncio_default_fixture_loop_scope`。该警告不影响这批同步纯测试。

隔离快照中临时 Python fake 调用实际 `CtpRequestDataFuture.make_order` 分支，以内存 CTP field/module 与 Trader 替代 native leaf；没有加载 CTP native API、访问网络或账户。exact int `0` 返回本地 SUBMITTED，exact int `7` 保持 False/空行；`False`、`0.0`、`None`、类名人为改成 `int` 的对象，以及其 metaclass 拒绝读取 `__name__` 的对象，均得到 UNKNOWN 专用异常。最后一种类型名使用固定 `unavailable` fallback。每支 fake Trader 最多一次 submit；异常携带预先校验的 client id/正 request id，不带 raw 值。错误 request id 在 native stub 调用前被拒绝。结果：通过。

测试后以 SHA-256 比对原开发 checkout 与隔离快照的全部七个 overlay；七者逐一相等，证明隔离运行覆盖的正是当前源码/断言。最终 pins：

```text
610840eafa6480b895e6f803b66dda8a159b81e313621ba89ee33c623e8bc9b9  src/bt_api_ctp/containers/ctp/ctp_order.py
8eb7ef3fd0022b386a41fc4633317de05a5e61c7be3e4a19e7f5abef57198ba3  src/bt_api_ctp/feeds/live_ctp_feed.py
b88959f5d482859bc851b1f52823ab01fb2d59626234385c280e69f4f1d2fed7  src/bt_api_ctp/fields.py
6e5e0d7c36d23af652f7e8a985f1293af51184be26122a94fa6a39a2c639398c  src/bt_api_ctp/status.py
2da9356fbc066bb972eec11b56a96eef95c724e81d56d5c5f642a9c7460d3ef5  src/bt_api_ctp/errors/__init__.py
41f0e38eb219a8af3341d9b0c8b7c4045265a42baea8a654c8b7964556d0ed29  tests/test_fields.py
2583846eb089a96b8a0e68878d037aaeda99f323b5a9a8e16549651c80ccfec0  tests/test_receipts.py
```

独立 reviewer 首轮 HOLD 指出用 `type(ret).__name__ == 'int'` 会误拒自定义类型，且 hostile metaclass 可能令诊断访问失败。本轮移除了按名称分类，并增加固定 `unavailable` fallback、native 前 request-id 校验及对应 fake。修复后的最终 source review 为 GO（只读差异复审；reviewer 未改文件、未运行测试、未触碰账户）。

## 工作区保护与验收边界

- 原工作区的 `src/bt_api_ctp/__init__.py` 修改以及未跟踪的 `src/bt_api_ctp/query_facts.py`、`src/bt_api_ctp/simnow.py` 均原样保留，未覆盖或纳入本轮快照。
- 这轮证据只覆盖源码、隔离临时快照中的既有纯单测和内存 fake。没有调用 CTP native 请求、真实账户、认证、网络、私密输入或执行签发器；不构成生产账户验收。
