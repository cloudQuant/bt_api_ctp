from __future__ import annotations

import gc
import threading
import weakref
from dataclasses import FrozenInstanceError, fields, replace
from types import SimpleNamespace

import pytest

from bt_api_ctp.ctp.client import (
    CtpNativeCallbackConsumerError,
    TraderClient,
    _TraderSpi,
)


class _FakeTraderApi:
    def __init__(self) -> None:
        self.authenticate_request_ids: list[int] = []
        self.login_request_ids: list[int] = []

    def ReqAuthenticate(self, _field, request_id):
        self.authenticate_request_ids.append(request_id)
        return 0

    def ReqUserLogin(self, _field, request_id):
        self.login_request_ids.append(request_id)
        return 0

    def ReqOrderInsert(self, *_args):
        raise AssertionError("callback records must not expose the native API")

    def ReqOrderAction(self, *_args):
        raise AssertionError("callback records must not expose the native API")

    def RegisterSpi(self, _spi):
        return None

    def Release(self):
        return None


def _connect_and_login(client: TraderClient, api: _FakeTraderApi) -> _TraderSpi:
    client._api = api
    spi = _TraderSpi(client, api)
    client._spi = spi
    spi.OnFrontConnected()
    spi.OnRspAuthenticate(
        None,
        SimpleNamespace(ErrorID=0, ErrorMsg=""),
        api.authenticate_request_ids[-1],
        True,
    )
    spi.OnRspUserLogin(
        SimpleNamespace(FrontID=7, SessionID=19, TradingDay="20260925", MaxOrderRef="90"),
        SimpleNamespace(ErrorID=0, ErrorMsg=""),
        api.login_request_ids[-1],
        True,
    )
    return spi


def _client() -> TraderClient:
    return TraderClient("tcp://fake-front", "9999", "investor-1", "fake-secret")


def _contains_order_write_api(value) -> bool:
    if callable(getattr(value, "ReqOrderInsert", None)) or callable(
        getattr(value, "ReqOrderAction", None)
    ):
        return True
    if isinstance(value, dict):
        return any(
            _contains_order_write_api(key) or _contains_order_write_api(item)
            for key, item in value.items()
        )
    if isinstance(value, (tuple, list, set, frozenset)):
        return any(_contains_order_write_api(item) for item in value)
    return False


def _event_contains_order_write_api(event) -> bool:
    return any(_contains_order_write_api(getattr(event, item.name)) for item in fields(event))


def _wait_for_condition(client: TraderClient, predicate) -> None:
    with client._query_state_lock:
        assert client._native_callback_event_condition.wait_for(predicate, timeout=1)


def _start_consumer_wait(client: TraderClient, token: object):
    started = threading.Event()
    result: dict[str, object] = {}

    def wait() -> None:
        started.set()
        try:
            result["event"] = client._wait_native_callback_event_for_consumer(token, timeout=None)
        except BaseException as exc:  # the test records the worker failure for its caller
            result["error"] = exc

    thread = threading.Thread(target=wait, daemon=True)
    thread.start()
    assert started.wait(timeout=1)
    _wait_for_condition(
        client,
        lambda: client._native_callback_consumer_lease is not None
        and client._native_callback_consumer_lease.waiting,
    )
    return thread, result


def test_order_callback_captures_immutable_raw_source_facts_and_keeps_queue_api() -> None:
    client = _client()
    api = _FakeTraderApi()
    spi = _connect_and_login(client, api)
    native_generation = client._native_api_generation
    connection_generation = client._connection_generation
    order = SimpleNamespace(
        BrokerID="9999",
        InvestorID="investor-1",
        UserID="investor-1",
        InstrumentID="rb2610",
        ExchangeID="SHFE",
        OrderRef=" 00017 ",
        OrderSysID=" 0000000042 ",
        FrontID=7,
        SessionID=19,
        RequestID=31,
        SequenceNo=220,
        NotifySequence=8,
        OrderStatus="3",
        OrderSubmitStatus="0",
        TradingDay="20260925",
    )

    spi.OnRtnOrder(order)

    event = client.wait_native_callback_event(timeout=0)
    assert event is not None
    assert event.event_type == "OnRtnOrder"
    assert event.native_api_source_id == client._native_api_source_id
    assert event.native_spi_source_id == spi._native_spi_source_id
    assert not hasattr(event, "origin_api")
    assert not hasattr(event, "origin_spi")
    assert not _event_contains_order_write_api(event)
    assert event.source_session_epoch == event.native_client_epoch
    assert event.native_api_generation == native_generation
    assert event.connection_generation == connection_generation
    assert event.login_verified is True
    assert (event.login_broker_id, event.login_investor_id) == ("9999", "investor-1")
    assert (event.login_front, event.login_trading_day) == (
        "tcp://fake-front",
        "20260925",
    )
    assert (event.login_front_id, event.login_session_id) == (7, 19)
    assert event.callback_session_matches_login is True
    assert event.raw_value("OrderRef") == " 00017 "
    assert event.raw_value("OrderSysID") == " 0000000042 "
    assert event.raw_value("SequenceNo") == 220
    assert event.raw_value("NotifySequence") == 8
    assert event.source_sequence == 1
    assert event.stable_source_key[-1] == event.source_sequence
    assert event.scope_binding == "unbound"
    assert event.trust_boundary == "source_facts_only"
    assert event.managed_session_epoch is None
    assert event.managed_session_epoch_bound is False
    assert not hasattr(event, "account_key")
    assert not hasattr(event, "scope_key")
    with pytest.raises(FrozenInstanceError):
        event.source_sequence = 99
    with pytest.raises(ValueError, match="managed session epoch"):
        replace(event, managed_session_epoch="caller-value")
    with pytest.raises(ValueError, match="cannot bind an account or scope"):
        replace(event, scope_binding="bound")

    # The existing public order queue continues to return its original dict shape.
    queued_order = client.wait_order_event(timeout=0)
    assert queued_order["OrderRef"] == " 00017 "
    assert queued_order["RequestID"] == 31


def test_action_callback_order_and_replayed_fields_get_distinct_source_keys() -> None:
    client = _client()
    api = _FakeTraderApi()
    spi = _connect_and_login(client, api)
    action = SimpleNamespace(
        BrokerID="9999",
        InvestorID="investor-1",
        UserID="investor-1",
        InstrumentID="rb2610",
        ExchangeID="SHFE",
        OrderRef="17",
        OrderSysID="42",
        FrontID=7,
        SessionID=19,
        OrderActionRef=41,
        RequestID=53,
        ActionFlag="0",
        StatusMsg=" raw status ",
    )
    rsp_info = SimpleNamespace(ErrorID=31, ErrorMsg=" exact callback error ")

    spi.OnRspOrderAction(action, SimpleNamespace(ErrorID=0, ErrorMsg=""), 53, True)
    spi.OnErrRtnOrderAction(action, rsp_info)
    spi.OnRspOrderAction(action, SimpleNamespace(ErrorID=0, ErrorMsg=""), 53, True)

    events = tuple(client.wait_native_callback_event(timeout=0) for _ in range(3))
    assert all(event is not None for event in events)
    first, second, replay = events
    assert [event.event_type for event in events] == [
        "OnRspOrderAction",
        "OnErrRtnOrderAction",
        "OnRspOrderAction",
    ]
    assert [event.source_sequence for event in events] == [1, 2, 3]
    assert len({event.stable_source_key for event in events}) == 3
    assert first.raw_value("nRequestID") == 53
    assert first.raw_value("bIsLast") is True
    assert first.raw_value("RequestID") == 53
    assert first.raw_value("OrderActionRef") == 41
    assert second.raw_value("ErrorID") == 31
    assert second.raw_value("ErrorMsg") == " exact callback error "
    assert second.raw_value("OrderActionRef") == 41
    assert replay.raw_correlation_fields == first.raw_correlation_fields
    assert replay.native_correlation_key == first.native_correlation_key
    assert replay.stable_source_key != first.stable_source_key
    assert first.native_client_epoch == replay.native_client_epoch
    assert first.native_api_generation == replay.native_api_generation
    assert first.connection_generation == replay.connection_generation
    assert all(event.scope_binding == "unbound" for event in events)


def test_reconnect_and_native_api_replacement_separate_callback_epochs() -> None:
    client = _client()
    first_api = _FakeTraderApi()
    first_spi = _connect_and_login(client, first_api)
    first_spi.OnRspOrderAction(
        SimpleNamespace(OrderActionRef=4, RequestID=8, FrontID=7, SessionID=19),
        SimpleNamespace(ErrorID=0, ErrorMsg=""),
        8,
        True,
    )
    first_event = client.wait_native_callback_event(timeout=0)
    assert first_event is not None

    first_spi.OnFrontDisconnected(0x2001)
    first_spi.OnFrontConnected()
    first_spi.OnRspAuthenticate(
        None,
        SimpleNamespace(ErrorID=0, ErrorMsg=""),
        first_api.authenticate_request_ids[-1],
        True,
    )
    first_spi.OnRspUserLogin(
        SimpleNamespace(FrontID=7, SessionID=20, TradingDay="20260925", MaxOrderRef="90"),
        SimpleNamespace(ErrorID=0, ErrorMsg=""),
        first_api.login_request_ids[-1],
        True,
    )
    first_spi.OnRtnOrder(SimpleNamespace(FrontID=7, SessionID=20, TradingDay="20260925"))
    reconnect_event = client.wait_native_callback_event(timeout=0)
    assert reconnect_event is not None
    assert reconnect_event.native_client_epoch == first_event.native_client_epoch
    assert reconnect_event.native_api_generation == first_event.native_api_generation
    assert reconnect_event.connection_generation > first_event.connection_generation
    assert reconnect_event.source_sequence > first_event.source_sequence
    assert reconnect_event.login_session_id == 20

    second_api = _FakeTraderApi()
    second_spi = _connect_and_login(client, second_api)
    new_native_event_count = client._native_callback_events.qsize()
    first_spi.OnRspOrderAction(
        SimpleNamespace(OrderActionRef=4, RequestID=8),
        SimpleNamespace(ErrorID=0, ErrorMsg="late old API callback"),
        8,
        True,
    )
    assert client._native_callback_events.qsize() == new_native_event_count

    second_spi.OnErrRtnOrderAction(
        SimpleNamespace(
            OrderActionRef=9,
            RequestID=14,
            FrontID=7,
            SessionID=99,
            BrokerID="9999",
            InvestorID="investor-1",
        ),
        SimpleNamespace(ErrorID=40, ErrorMsg="new API callback"),
    )
    second_event = client.wait_native_callback_event(timeout=0)
    assert second_event is not None
    assert second_event.native_client_epoch != first_event.native_client_epoch
    assert second_event.source_session_epoch != first_event.source_session_epoch
    assert second_event.native_api_generation > first_event.native_api_generation
    assert second_event.native_api_source_id == client._native_api_source_id
    assert second_event.native_spi_source_id == second_spi._native_spi_source_id
    assert second_event.callback_session_matches_login is False
    assert second_event.stable_source_key != first_event.stable_source_key


def test_queued_callback_record_does_not_retain_native_api_or_spi() -> None:
    client = _client()
    api = _FakeTraderApi()
    api_ref = weakref.ref(api)
    spi = _connect_and_login(client, api)
    spi.OnRtnOrder(SimpleNamespace(OrderRef="19", RequestID=27, SequenceNo=300))
    assert client._native_callback_events.qsize() == 1

    # Releasing the client-owned registration drops its native references; the
    # queued immutable source record must not keep either object alive.
    client._api = None
    del spi
    del api
    gc.collect()

    assert api_ref() is None
    event = client.wait_native_callback_event(timeout=0)
    assert event is not None
    assert not _event_contains_order_write_api(event)
    assert isinstance(event.native_api_source_id, str)
    assert isinstance(event.native_spi_source_id, str)


def test_legacy_native_callback_wait_is_safe_before_api_start() -> None:
    client = _client()
    assert client.wait_native_callback_event(timeout=0) is None
    with pytest.raises(CtpNativeCallbackConsumerError) as error:
        client._claim_native_callback_event_consumer()
    assert error.value.code == "ctp_native_callback_consumer_native_api_unavailable"


def test_native_callback_event_consumer_lease_is_exclusive_and_releases_safely() -> None:
    client = _client()
    api = _FakeTraderApi()
    spi = _connect_and_login(client, api)
    token = client._claim_native_callback_event_consumer()

    with pytest.raises(CtpNativeCallbackConsumerError) as competing_claim:
        client._claim_native_callback_event_consumer()
    assert competing_claim.value.code == "ctp_native_callback_consumer_already_claimed"
    with pytest.raises(CtpNativeCallbackConsumerError) as legacy_wait:
        client.wait_native_callback_event(timeout=0)
    assert legacy_wait.value.code == "ctp_native_callback_consumer_queue_leased"

    spi.OnRtnOrder(SimpleNamespace(OrderRef="22", RequestID=14))
    event = client._wait_native_callback_event_for_consumer(token, timeout=0)
    assert event is not None
    assert event.raw_value("OrderRef") == "22"

    thread, result = _start_consumer_wait(client, token)
    with client._query_state_lock:
        with pytest.raises(CtpNativeCallbackConsumerError) as competing_wait:
            client._wait_native_callback_event_for_consumer(token, timeout=0)
        assert competing_wait.value.code == "ctp_native_callback_consumer_competing_wait"
        client._release_native_callback_event_consumer(token)
        with pytest.raises(CtpNativeCallbackConsumerError) as premature_claim:
            client._claim_native_callback_event_consumer()
    assert premature_claim.value.code == "ctp_native_callback_consumer_previous_wait_active"
    thread.join(timeout=1)
    assert not thread.is_alive()
    assert isinstance(result.get("error"), CtpNativeCallbackConsumerError)
    assert result["error"].code == "ctp_native_callback_consumer_stale_token"

    # Release is idempotent, and the old token cannot affect the next lease.
    client._release_native_callback_event_consumer(token)
    client._release_native_callback_event_consumer(token)
    replacement = client._claim_native_callback_event_consumer()
    assert replacement is not token
    client._release_native_callback_event_consumer(token)
    with pytest.raises(CtpNativeCallbackConsumerError) as still_owned:
        client.wait_native_callback_event(timeout=0)
    assert still_owned.value.code == "ctp_native_callback_consumer_queue_leased"
    client._release_native_callback_event_consumer(replacement)


def test_native_callback_consumer_claim_rejects_an_in_flight_legacy_wait() -> None:
    client = _client()
    client._api = _FakeTraderApi()
    result: dict[str, object] = {}
    started = threading.Event()

    def wait_legacy() -> None:
        started.set()
        result["event"] = client.wait_native_callback_event(timeout=1)

    thread = threading.Thread(target=wait_legacy, daemon=True)
    thread.start()
    assert started.wait(timeout=1)
    _wait_for_condition(client, lambda: client._native_callback_legacy_waiters == 1)

    with pytest.raises(CtpNativeCallbackConsumerError) as error:
        client._claim_native_callback_event_consumer()
    assert error.value.code == "ctp_native_callback_consumer_legacy_wait_in_flight"
    thread.join(timeout=2)
    assert not thread.is_alive()
    assert result["event"] is None
    token = client._claim_native_callback_event_consumer()
    client._release_native_callback_event_consumer(token)


def test_native_callback_consumer_token_goes_stale_on_api_generation_change() -> None:
    client = _client()
    client._api = _FakeTraderApi()
    old_token = client._claim_native_callback_event_consumer()
    thread, result = _start_consumer_wait(client, old_token)

    client._api = _FakeTraderApi()

    thread.join(timeout=1)
    assert not thread.is_alive()
    assert isinstance(result.get("error"), CtpNativeCallbackConsumerError)
    assert result["error"].code == "ctp_native_callback_consumer_stale_token"
    client._release_native_callback_event_consumer(old_token)
    new_token = client._claim_native_callback_event_consumer()
    assert new_token is not old_token
    client._release_native_callback_event_consumer(new_token)


def test_native_callback_consumer_wait_is_woken_and_revoked_by_stop() -> None:
    client = _client()
    client._api = _FakeTraderApi()
    token = client._claim_native_callback_event_consumer()
    thread, result = _start_consumer_wait(client, token)

    client.stop()

    thread.join(timeout=1)
    assert not thread.is_alive()
    assert isinstance(result.get("error"), CtpNativeCallbackConsumerError)
    assert result["error"].code == "ctp_native_callback_consumer_stale_token"
    client._release_native_callback_event_consumer(token)
    with pytest.raises(CtpNativeCallbackConsumerError) as error:
        client._claim_native_callback_event_consumer()
    assert error.value.code == "ctp_native_callback_consumer_native_api_unavailable"
