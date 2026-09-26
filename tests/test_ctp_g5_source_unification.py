from __future__ import annotations

import threading
from types import SimpleNamespace

import pytest

import bt_api_ctp.ctp.client as client_module
from bt_api_ctp.ctp.client import CtpExecutionGateError, TraderClient, _TraderSpi


class _FakeTraderApi:
    def __init__(self, *, fail_order_action=False):
        self.order_action_calls = []
        self.order_action_snapshots = []
        self.fail_order_action = fail_order_action

    def ReqOrderAction(self, _field, _request_id):
        self.order_action_calls.append((_field, _request_id))
        self.order_action_snapshots.append(
            {
                name: getattr(_field, name, None)
                for name in (
                    "BrokerID",
                    "InvestorID",
                    "UserID",
                    "InstrumentID",
                    "ExchangeID",
                    "OrderRef",
                    "OrderSysID",
                    "FrontID",
                    "SessionID",
                    "RequestID",
                    "OrderActionRef",
                    "ActionFlag",
                )
            }
        )
        if self.fail_order_action:
            raise RuntimeError("fake native submit failure")
        return 0


def _action(**changes):
    values = {
        "BrokerID": "9999",
        "InvestorID": "investor-1",
        "UserID": "investor-1",
        "InstrumentID": "rb2610",
        "ExchangeID": "SHFE",
        "OrderRef": " 00017 ",
        "OrderSysID": "",
        "FrontID": 7,
        "SessionID": 19,
        "OrderActionRef": "41",
        "RequestID": 53,
        "ActionFlag": "0",
    }
    values.update(changes)
    return SimpleNamespace(**values)


def _client_with_api():
    client = TraderClient("tcp://fake", "9999", "investor-1", "fake-secret")
    api = _FakeTraderApi()
    client._api = api
    spi = _TraderSpi(client, api)
    client._spi = spi
    client._connection_generation = 4
    client._trading_day = "20260925"
    client._require_execution_write_locked = lambda *_args, **_kwargs: None
    client._require_native_field_identity_locked = lambda *_args, **_kwargs: None
    client._require_native_field_identity_snapshot_locked = lambda *_args, **_kwargs: None
    return client, api, spi


def _managed_cancel_identity(**changes):
    values = {
        "runtime_order_id": "bt-managed-v1:" + "a" * 64,
        "managed_intent_id": "intent.order-1",
        "runtime_action_id": "cancel.action-1",
        "managed_cancel_intent_id": "cancel.action-1",
    }
    values.update(changes)
    return values


def _managed_cancel_field(**changes):
    values = {
        "OrderRef": "000000000017",
        "OrderSysID": "SYS-17",
        "FrontID": 7,
        "SessionID": 19,
        "OrderActionRef": 53,
        "RequestID": 53,
    }
    values.update(changes)
    return _action(**values)


class _FakeNativeQueryField:
    _QUERY_FIELD_NAMES = {
        "BrokerID",
        "InvestorID",
        "InstrumentID",
        "ExchangeID",
        "OrderSysID",
        "TradeID",
        "TradeTimeStart",
        "TradeTimeEnd",
        "ProductID",
        "HedgeFlag",
        "InputPrice",
        "UnderlyingPrice",
    }

    def __init__(self, behavior=None):
        object.__setattr__(self, "_query_values", {})
        object.__setattr__(self, "_behavior", behavior)

    def __setattr__(self, name, value):
        if name in self._QUERY_FIELD_NAMES:
            if self._behavior == "truncate" and name == "InstrumentID":
                value = value[:-1]
            elif self._behavior == "ignore" and name == "OrderSysID":
                return
            elif self._behavior == "normalize" and name == "ExchangeID":
                value = value.lower()
            elif self._behavior == "numeric_truncate" and name == "InputPrice":
                value = float(int(value * 10) / 10)
            elif self._behavior == "numeric_ignore" and name == "InputPrice":
                return
            elif self._behavior == "numeric_normalize" and name == "InputPrice":
                value = round(value, 2)
            self._query_values[name] = value
            return
        object.__setattr__(self, name, value)

    def __getattribute__(self, name):
        if name in object.__getattribute__(self, "_QUERY_FIELD_NAMES"):
            behavior = object.__getattribute__(self, "_behavior")
            if behavior == "missing_getter" and name == "OrderSysID":
                raise AttributeError(name)
            if behavior == "numeric_missing_getter" and name == "InputPrice":
                raise AttributeError(name)
            if behavior == "invalid_type" and name == "ExchangeID":
                return b"SHFE"
            if behavior == "numeric_invalid_type" and name == "InputPrice":
                return 10
            values = object.__getattribute__(self, "_query_values")
            if name not in values:
                raise AttributeError(name)
            return values[name]
        return object.__getattribute__(self, name)


class _FakeQueryApi:
    def __init__(self, client):
        self.client = client
        self.calls = []
        self.source_at_dispatch = None
        self.field_values_at_dispatch = None

    def _submit(self, request_type, field, request_id):
        self.calls.append((request_type, field, request_id))
        before_send = self.client.get_query_result(request_id)
        self.source_at_dispatch = before_send.query_source
        self.field_values_at_dispatch = {
            name: getattr(field, name)
            for name, _value in (
                self.source_at_dispatch.request_filters
                + self.source_at_dispatch.request_parameters
            )
        }
        self.client._handle_query_callback(request_type, None, None, request_id, True)
        return 0

    def ReqQryOrder(self, field, request_id):
        return self._submit("orders", field, request_id)

    def ReqQryInstrument(self, field, request_id):
        return self._submit("instruments", field, request_id)

    def ReqQryTrade(self, field, request_id):
        return self._submit("trades", field, request_id)

    def ReqQryInvestorPosition(self, field, request_id):
        return self._submit("positions", field, request_id)

    def ReqQryOptionInstrTradeCost(self, field, request_id):
        return self._submit("option_trade_cost", field, request_id)


def _query_client_with_fake_native_api(monkeypatch, *, behavior=None, field_type="orders"):
    monkeypatch.setattr(
        TraderClient,
        "is_read_only_ready",
        property(lambda _self: True),
    )
    class_name = {
        "orders": "CThostFtdcQryOrderField",
        "trades": "CThostFtdcQryTradeField",
        "positions": "CThostFtdcQryInvestorPositionField",
        "instruments": "CThostFtdcQryInstrumentField",
    }[field_type]
    monkeypatch.setattr(
        client_module,
        class_name,
        lambda: _FakeNativeQueryField(behavior),
    )
    client = TraderClient("tcp://fake", "9999", "investor-1", "fake-secret")
    client._query_interval = 0
    client._last_query_submitted_at = 0
    client._record_request = lambda *_args, **_kwargs: None
    api = _FakeQueryApi(client)
    client._api = api
    return client, api


def test_query_source_records_exact_order_target_filters_and_explicit_names():
    client = TraderClient("tcp://fake", "9999", "investor-1", "fake-secret")
    captured = {}

    def execute(request_type, _submit, timeout, **kwargs):
        captured.update(request_type=request_type, timeout=timeout, **kwargs)
        return None

    client._execute_query = execute
    client.query_orders_result(
        instrument_id="rb2610", exchange_id="SHFE", order_sys_id="SYS-17", timeout=0
    )

    assert captured["request_type"] == "orders"
    expected_intent = {
        "BrokerID": "9999",
        "InvestorID": "investor-1",
        "InstrumentID": "rb2610",
        "ExchangeID": "SHFE",
        "OrderSysID": "SYS-17",
    }
    assert captured["request_intent_filters"] == expected_intent
    native_field = captured["request_filter_field"]
    assert {name: getattr(native_field, name) for name in expected_intent} == expected_intent
    assert captured.get("explicit_request_filters", ()) == ()

    client._execute_query = lambda *args, **kwargs: None
    accumulator = client._new_query_accumulator(
        "margin_rate",
        {"HedgeFlag": "1", "InstrumentID": "rb2610"},
        explicit_request_filters=("HedgeFlag",),
    )
    source = accumulator.result().query_source
    assert source is not None
    assert source.request_intent_filters == source.request_filters == (
        ("HedgeFlag", "1"),
        ("InstrumentID", "rb2610"),
    )
    assert source.request_filters == (("HedgeFlag", "1"), ("InstrumentID", "rb2610"))
    assert source.explicit_request_filters == ("HedgeFlag",)

    with pytest.raises(TypeError, match="exact strings"):
        client._new_query_accumulator("orders", {"OrderRef": 17})

    filter_calls = []
    client._execute_query = lambda *args, **kwargs: filter_calls.append(kwargs) or None
    client.query_instrument_margin_rate_result("rb2610")
    client.query_instrument_margin_rate_result("rb2610", hedge_flag="1")
    assert filter_calls[0]["explicit_request_filters"] == ()
    assert filter_calls[1]["explicit_request_filters"] == ("HedgeFlag",)


def test_query_source_captures_intent_and_native_getter_readback_before_send(monkeypatch):
    client, api = _query_client_with_fake_native_api(monkeypatch)

    result = client.query_orders_result(
        instrument_id="rb2610", exchange_id="SHFE", order_sys_id="SYS-17", timeout=0.1
    )

    expected = (
        ("BrokerID", "9999"),
        ("ExchangeID", "SHFE"),
        ("InstrumentID", "rb2610"),
        ("InvestorID", "investor-1"),
        ("OrderSysID", "SYS-17"),
    )
    assert result.complete is True
    assert len(api.calls) == 1
    assert api.source_at_dispatch is not None
    assert api.source_at_dispatch.request_intent_filters == expected
    assert api.source_at_dispatch.request_filters == expected
    assert result.query_source.request_filters == expected
    assert api.field_values_at_dispatch == dict(expected)


def test_unfiltered_order_query_readback_matches_main_consumer_empty_scope(monkeypatch):
    client, api = _query_client_with_fake_native_api(monkeypatch)

    result = client.query_orders_result(timeout=0.1)

    expected = (
        ("BrokerID", "9999"),
        ("ExchangeID", ""),
        ("InstrumentID", ""),
        ("InvestorID", "investor-1"),
        ("OrderSysID", ""),
    )
    assert result.complete is True
    assert api.source_at_dispatch.request_intent_filters == expected
    assert api.source_at_dispatch.request_filters == expected
    assert api.field_values_at_dispatch == dict(expected)


def test_trade_and_position_sources_capture_only_consumer_filter_keys(monkeypatch):
    client, trade_api = _query_client_with_fake_native_api(monkeypatch, field_type="trades")
    trades = client.query_trades_result(timeout=0.1)
    expected_trades = (
        ("BrokerID", "9999"),
        ("ExchangeID", ""),
        ("InstrumentID", ""),
        ("InvestorID", "investor-1"),
        ("TradeID", ""),
        ("TradeTimeEnd", ""),
        ("TradeTimeStart", ""),
    )
    assert trades.complete is True
    assert trade_api.source_at_dispatch.request_filters == expected_trades

    position_client, position_api = _query_client_with_fake_native_api(
        monkeypatch, field_type="positions"
    )
    positions = position_client.query_positions_result(timeout=0.1)
    expected_positions = (("BrokerID", "9999"), ("InvestorID", "investor-1"))
    assert positions.complete is True
    assert position_api.source_at_dispatch.request_filters == expected_positions


@pytest.mark.parametrize(
    "behavior", ["truncate", "ignore", "normalize", "missing_getter", "invalid_type"]
)
def test_query_filter_setter_or_getter_mismatch_rejects_before_native_send(
    monkeypatch, behavior
):
    client, api = _query_client_with_fake_native_api(monkeypatch, behavior=behavior)

    result = client.query_orders_result(
        instrument_id="rb2610", exchange_id="SHFE", order_sys_id="SYS-17", timeout=0.1
    )

    assert result.complete is False
    assert result.unsupported is True
    assert result.query_source is None
    assert result.error_message.startswith("native_query_filter_readback_failed:")
    assert api.calls == []


def test_unassigned_optional_query_fields_are_not_claimed_as_filter_readback(monkeypatch):
    client, api = _query_client_with_fake_native_api(monkeypatch, field_type="instruments")

    result = client.query_instruments_result(timeout=0.1)

    assert result.complete is True
    assert len(api.calls) == 1
    source = result.query_source
    assert source.request_intent_filters == (("InstrumentID", ""),)
    assert source.request_filters == (("InstrumentID", ""),)
    assert "ExchangeID" not in dict(source.request_filters)
    assert "ProductID" not in dict(source.request_filters)


def _option_cost_client(monkeypatch, *, behavior=None):
    monkeypatch.setattr(
        TraderClient,
        "is_read_only_ready",
        property(lambda _self: True),
    )
    monkeypatch.setattr(
        client_module,
        "CThostFtdcQryOptionInstrTradeCostField",
        lambda: _FakeNativeQueryField(behavior),
    )
    client = TraderClient("tcp://fake", "9999", "investor-1", "fake-secret")
    client._query_interval = 0
    client._last_query_submitted_at = 0
    client._record_request = lambda *_args, **_kwargs: None
    api = _FakeQueryApi(client)
    client._api = api
    return client, api


def test_numeric_query_parameters_keep_typed_intent_and_native_readback(monkeypatch):
    client, api = _option_cost_client(monkeypatch)

    result = client.query_option_instrument_trade_cost_result(
        "rb2610", input_price=123.456, underlying_price=789.125, timeout=0.1
    )

    expected_parameters = (("InputPrice", 123.456), ("UnderlyingPrice", 789.125))
    assert result.complete is True
    assert api.source_at_dispatch.request_intent_parameters == expected_parameters
    assert api.source_at_dispatch.request_parameters == expected_parameters
    assert dict(api.source_at_dispatch.request_filters)["InstrumentID"] == "rb2610"
    assert "InputPrice" not in dict(api.source_at_dispatch.request_filters)
    assert api.field_values_at_dispatch["InputPrice"] == 123.456
    assert api.field_values_at_dispatch["UnderlyingPrice"] == 789.125


@pytest.mark.parametrize(
    "behavior",
    [
        "numeric_truncate",
        "numeric_ignore",
        "numeric_normalize",
        "numeric_missing_getter",
        "numeric_invalid_type",
    ],
)
def test_numeric_query_parameter_readback_mismatch_rejects_before_send(
    monkeypatch, behavior
):
    client, api = _option_cost_client(monkeypatch, behavior=behavior)

    result = client.query_option_instrument_trade_cost_result(
        "rb2610", input_price=123.456, underlying_price=789.125, timeout=0.1
    )

    assert result.complete is False
    assert result.unsupported is True
    assert result.query_source is None
    assert result.error_message.startswith("native_query_parameter_readback_failed:")
    assert api.calls == []


def test_order_source_and_legacy_queue_commit_under_one_api_generation_lock():
    client, api, spi = _client_with_api()
    event_recording = threading.Event()
    resume_callback = threading.Event()
    setter_attempted = threading.Event()
    setter_done = threading.Event()
    original_put = client._native_callback_events.put

    def pause_source_put(event):
        event_recording.set()
        assert resume_callback.wait(2)
        original_put(event)

    client._native_callback_events.put = pause_source_put
    callback_thread = threading.Thread(target=lambda: spi.OnRtnOrder(_action()), daemon=True)
    callback_thread.start()
    assert event_recording.wait(1)

    def replace_api():
        setter_attempted.set()
        client._api = _FakeTraderApi()
        setter_done.set()

    setter_thread = threading.Thread(target=replace_api, daemon=True)
    setter_thread.start()
    assert setter_attempted.wait(1)
    assert not setter_done.wait(0.05)

    resume_callback.set()
    callback_thread.join(2)
    setter_thread.join(2)
    assert not callback_thread.is_alive()
    assert not setter_thread.is_alive()
    assert setter_done.is_set()

    event = client.wait_native_callback_event(timeout=0)
    queued = client.wait_order_event(timeout=0)
    assert event is not None
    assert event.native_api_generation < client._native_api_generation
    assert queued["OrderRef"] == " 00017 "
    assert api is not client._api


def test_action_source_and_exact_order_target_share_one_generation_record():
    client, _api, spi = _client_with_api()
    client.submit_order_action(_action(), 53)

    spi.OnRspOrderAction(_action(), SimpleNamespace(ErrorID=0, ErrorMsg=""), 53, True)

    source = client.wait_native_callback_event(timeout=0)
    evidence = client.get_order_action_evidence(53, order_action_ref="41")
    assert source is not None
    assert source.event_type == "OnRspOrderAction"
    assert evidence is not None
    assert evidence.status == "accepted"
    assert evidence.evidence_received is True
    assert evidence.order_ref == " 00017 "
    assert evidence.connection_generation == source.connection_generation
    assert evidence.trading_day == "20260925"


def test_managed_cancel_accepts_complete_i9_target_tuple():
    client, api, _spi = _client_with_api()
    field = _managed_cancel_field()

    result = client.submit_order_action(field, 53, **_managed_cancel_identity())

    assert result == 0
    sent_field, sent_request_id = api.order_action_calls[0]
    assert sent_field is not field
    assert sent_request_id == 53
    evidence = client.get_order_action_evidence(53, order_action_ref="53")
    assert evidence is not None
    assert evidence.order_ref == "000000000017"
    assert evidence.order_sys_id == "SYS-17"
    assert evidence.front_id == 7
    assert evidence.session_id == 19
    assert api.order_action_snapshots[0]["OrderRef"] == evidence.order_ref
    assert api.order_action_snapshots[0]["OrderSysID"] == evidence.order_sys_id
    assert api.order_action_snapshots[0]["OrderActionRef"] == evidence.request_id
    assert api.order_action_snapshots[0]["RequestID"] == evidence.request_id


@pytest.mark.parametrize(
    "field_changes, managed_changes",
    [
        ({"OrderRef": "", "FrontID": 0, "SessionID": 0}, {}),
        ({"OrderSysID": "", "ExchangeID": ""}, {}),
        ({"OrderActionRef": 54}, {}),
        ({"RequestID": 54}, {}),
        ({"ActionFlag": "1"}, {}),
        ({}, {"managed_cancel_intent_id": None}),
        ({}, {"runtime_action_id": "other.action"}),
    ],
)
def test_managed_cancel_requires_complete_i9_target_and_action_identity(
    field_changes, managed_changes
):
    client, api, _spi = _client_with_api()
    kwargs = _managed_cancel_identity(**managed_changes)

    with pytest.raises(CtpExecutionGateError, match="ctp_execution_gate_managed_cancel"):
        client.submit_order_action(_managed_cancel_field(**field_changes), 53, **kwargs)

    assert api.order_action_calls == []


def test_managed_cancel_action_id_is_not_reused_after_native_exception():
    client, api, _spi = _client_with_api()
    api.fail_order_action = True

    with pytest.raises(RuntimeError, match="fake native submit failure"):
        client.submit_order_action(_managed_cancel_field(), 53, **_managed_cancel_identity())

    api.fail_order_action = False
    retry_field = _managed_cancel_field(RequestID=54, OrderActionRef=54)
    with pytest.raises(
        CtpExecutionGateError, match="ctp_execution_gate_managed_cancel_action_reused"
    ):
        client.submit_order_action(retry_field, 54, **_managed_cancel_identity())

    assert len(api.order_action_calls) == 1


class _ChangingOrderRefField:
    def __init__(self, *, switch_after_first_read=False):
        for name, value in vars(_managed_cancel_field()).items():
            setattr(self, name, value)
        self.order_ref_reads = 0
        self.switch_after_first_read = switch_after_first_read

    @property
    def OrderRef(self):
        self.order_ref_reads += 1
        if self.switch_after_first_read and self.order_ref_reads > 1:
            return "999999999999"
        return self._order_ref

    @OrderRef.setter
    def OrderRef(self, value):
        self._order_ref = value


def test_managed_cancel_sends_detached_snapshot_when_caller_field_changes():
    client, api, _spi = _client_with_api()
    field = _ChangingOrderRefField(switch_after_first_read=True)

    client.submit_order_action(field, 53, **_managed_cancel_identity())

    sent_field, sent_request_id = api.order_action_calls[0]
    evidence = client.get_order_action_evidence(53, order_action_ref="53")
    assert evidence is not None
    assert field.order_ref_reads == 1
    assert sent_field is not field
    assert sent_request_id == 53
    assert api.order_action_snapshots[0]["OrderRef"] == "000000000017"
    assert api.order_action_snapshots[0]["OrderSysID"] == "SYS-17"
    assert api.order_action_snapshots[0]["FrontID"] == evidence.front_id == 7
    assert api.order_action_snapshots[0]["SessionID"] == evidence.session_id == 19
    assert api.order_action_snapshots[0]["RequestID"] == sent_request_id
    assert api.order_action_snapshots[0]["OrderActionRef"] == sent_request_id
    assert evidence.order_ref == "000000000017"


class _NormalizingOrderSysIdField:
    def __init__(self):
        for name, value in vars(_managed_cancel_field()).items():
            if name == "OrderSysID":
                self._order_sys_id = value
            else:
                setattr(self, name, value)

    @property
    def OrderSysID(self):
        return self._order_sys_id

    @OrderSysID.setter
    def OrderSysID(self, value):
        self._order_sys_id = value[:3]


def test_managed_cancel_rejects_native_setter_normalization_before_send():
    client, api, _spi = _client_with_api()

    with pytest.raises(
        CtpExecutionGateError,
        match="ctp_execution_gate_managed_cancel_native_snapshot_mismatch",
    ):
        client.submit_order_action(
            _NormalizingOrderSysIdField(), 53, **_managed_cancel_identity()
        )

    assert api.order_action_calls == []


def test_action_source_and_target_update_cannot_split_across_api_generation():
    client, _api, spi = _client_with_api()
    client.submit_order_action(_action(), 53)
    source_recording = threading.Event()
    resume_callback = threading.Event()
    setter_attempted = threading.Event()
    setter_done = threading.Event()
    original_put = client._native_callback_events.put

    def pause_source_put(event):
        source_recording.set()
        assert resume_callback.wait(2)
        original_put(event)

    client._native_callback_events.put = pause_source_put
    callback_thread = threading.Thread(
        target=lambda: spi.OnRspOrderAction(
            _action(), SimpleNamespace(ErrorID=0, ErrorMsg=""), 53, True
        ),
        daemon=True,
    )
    callback_thread.start()
    assert source_recording.wait(1)

    def replace_api():
        setter_attempted.set()
        client._api = _FakeTraderApi()
        setter_done.set()

    setter_thread = threading.Thread(target=replace_api, daemon=True)
    setter_thread.start()
    assert setter_attempted.wait(1)
    assert not setter_done.wait(0.05)

    resume_callback.set()
    callback_thread.join(2)
    setter_thread.join(2)
    assert not callback_thread.is_alive()
    assert not setter_thread.is_alive()
    assert setter_done.is_set()

    source = client.wait_native_callback_event(timeout=0)
    evidence = client.get_order_action_evidence(53, order_action_ref="41")
    assert source is not None
    assert evidence is not None
    assert source.native_api_generation < client._native_api_generation
    assert evidence.status == "accepted"
    assert evidence.connection_generation == source.connection_generation


def test_action_callback_with_wrong_orderref_cannot_resolve_the_reserved_target():
    client, _api, spi = _client_with_api()
    client.submit_order_action(_action(), 53)

    spi.OnRspOrderAction(
        _action(OrderRef="different-order"),
        SimpleNamespace(ErrorID=0, ErrorMsg=""),
        53,
        True,
    )

    evidence = client.get_order_action_evidence(53, order_action_ref="41")
    assert evidence is not None
    assert evidence.status == "unknown"
    assert evidence.evidence_received is False
    assert evidence.reason == "callback_identity_mismatch"


def test_feed_binds_order_action_ref_and_field_request_id_before_submit():
    from bt_api_ctp.feeds.live_ctp_feed import CtpRequestDataFuture

    submitted = {}

    class _FeedTrader:
        _front_id = 7
        _session_id = 19

        def _next_request_id(self):
            return 53

        def submit_order_action(self, field, request_id, **_kwargs):
            submitted["field"] = field
            submitted["request_id"] = request_id
            submitted["kwargs"] = _kwargs
            return 0

    feed = CtpRequestDataFuture(broker_id="9999", user_id="investor-1")
    feed._trader = _FeedTrader()
    feed._ensure_execution_permitted = lambda *_args, **_kwargs: None
    feed._ensure_trading_ready = lambda: None

    feed.cancel_order("rb2610", order_id="SYS-17", exchange_id="SHFE")

    assert submitted["request_id"] == 53
    assert submitted["field"].RequestID == 53
    assert submitted["field"].OrderActionRef == 53
    assert submitted["field"].OrderSysID == "SYS-17"
    assert submitted["field"].ExchangeID == "SHFE"


def test_managed_feed_cancel_rejects_incomplete_target_before_request_id_or_native_submit():
    from bt_api_ctp.feeds.live_ctp_feed import CtpRequestDataFuture

    submitted = []

    class _FeedTrader:
        _front_id = 7
        _session_id = 19
        request_ids = 0

        def _next_request_id(self):
            self.request_ids += 1
            return self.request_ids

        def submit_order_action(self, *args, **_kwargs):
            submitted.append(args)
            return 0

    feed = CtpRequestDataFuture(broker_id="9999", user_id="investor-1")
    trader = _FeedTrader()
    feed._trader = trader
    feed._ensure_execution_permitted = lambda *_args, **_kwargs: None
    feed._ensure_trading_ready = lambda: None

    with pytest.raises(CtpExecutionGateError, match="managed_cancel_target_incomplete"):
        feed.cancel_order(
            "rb2610",
            order_id="SYS-17",
            exchange_id="SHFE",
            runtime_order_id="bt-managed-v1:" + "a" * 64,
            managed_intent_id="intent.order-1",
            runtime_action_id="cancel.action-1",
            managed_cancel_intent_id="cancel.action-1",
        )

    assert trader.request_ids == 0
    assert submitted == []


def test_managed_feed_cancel_passes_full_target_and_action_identity():
    from bt_api_ctp.feeds.live_ctp_feed import CtpRequestDataFuture

    submitted = {}

    class _FeedTrader:
        def _next_request_id(self):
            return 53

        def submit_order_action(self, field, request_id, **kwargs):
            submitted.update(field=field, request_id=request_id, kwargs=kwargs)
            return 0

    feed = CtpRequestDataFuture(broker_id="9999", user_id="investor-1")
    feed._trader = _FeedTrader()
    feed._ensure_execution_permitted = lambda *_args, **_kwargs: None
    feed._ensure_trading_ready = lambda: None
    managed = _managed_cancel_identity()

    feed.cancel_order(
        "rb2610",
        order_id="SYS-17",
        exchange_id="SHFE",
        order_ref="000000000017",
        front_id=7,
        session_id=19,
        **managed,
    )

    field = submitted["field"]
    assert submitted["request_id"] == 53
    assert field.RequestID == field.OrderActionRef == 53
    assert field.UserID == "investor-1"
    assert field.OrderRef == "000000000017"
    assert field.OrderSysID == "SYS-17"
    assert field.ExchangeID == "SHFE"
    assert field.FrontID == 7
    assert field.SessionID == 19
    assert submitted["kwargs"]["runtime_order_id"] == managed["runtime_order_id"]
    assert submitted["kwargs"]["managed_intent_id"] == managed["managed_intent_id"]
    assert submitted["kwargs"]["runtime_action_id"] == managed["runtime_action_id"]
    assert (
        submitted["kwargs"]["managed_cancel_intent_id"]
        == managed["managed_cancel_intent_id"]
    )
