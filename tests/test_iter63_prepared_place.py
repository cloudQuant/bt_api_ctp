"""Synthetic prepared place and insert-error matching; no real native I/O."""

from __future__ import annotations

import threading
from dataclasses import replace
from decimal import Decimal
from types import MappingProxyType, SimpleNamespace

import pytest
from bt_api_base.direct_place_protocol import PreparedPlaceRequest
from bt_api_base.direct_write_protocol import DirectExecutionEventKind, DirectWriteError
from bt_api_base.market_protocol import MarketInstrumentKey
from bt_api_py._contracts.models import OrderRequest, OrderType, Side
from test_iter63_prepared_cancel import Verifier, make_system

from bt_api_ctp.ctp import ctp_structs_order
from bt_api_ctp.gateway_place_return_projection import (
    CtpGatewayPlaceReturnError,
    gateway_place_error_key,
    project_gateway_place_error,
)
from bt_api_ctp.gateway_return_buffer import CtpGatewayReturnBuffer
from bt_api_ctp.registered_sim_session import capture_query_session


def order(**kwargs):
    values = dict(
        symbol="SA2701",
        side=Side.BUY,
        order_type=OrderType.LIMIT,
        quantity=Decimal("1"),
        account_id="logical-account",
        client_order_id="synthetic",
        price=Decimal("1000.1"),
        time_in_force="GFD",
        offset="open",
        exchange_id="CZCE",
        quantity_unit="lots",
        idempotency_key="place-key",
    )
    values.update(kwargs)
    return OrderRequest(**values)


@pytest.fixture
def system(monkeypatch):
    provider, trader, native = make_system(monkeypatch)
    native.ReqOrderInsert = lambda field, request_id: native.calls.append((field, request_id)) or 0
    monkeypatch.setattr(ctp_structs_order, "CThostFtdcInputOrderField", SimpleNamespace)
    return provider, trader, native


def test_prepare_zero_io_exact_fields_and_release(system):
    provider, trader, native = system
    before = trader._req_id
    prepared = provider.prepare_order(order())
    assert trader._req_id == before + 1 and not native.calls
    assert prepared.identity.fields["order_ref"] == prepared.request_fields["OrderRef"]
    assert prepared.request_fields["LimitPrice"] == float(Decimal("1000.1")).hex()
    assert prepared.source_binding["schema"] == "ctp-place-source-v1"
    assert prepared.request_keys[0].startswith("ctp-place-v1:")
    provider.release_prepared_order(prepared)
    with pytest.raises(DirectWriteError):
        provider.release_prepared_order(prepared)
    assert not native.calls


@pytest.mark.parametrize(
    "changed",
    [
        {"exchange_id": None},
        {"quantity_unit": "base"},
        {"hedge_flag": "2"},
        {"hedge_class": "hedge"},
        {"hedge_class": "arbitrage"},
        {"exchange_id": "ZCE"},
        {"symbol": "CZCE.SA2701"},
    ],
)
def test_unrepresentable_request_fails_before_ids_or_io(system, changed):
    provider, trader, native = system
    ids = trader._req_id, trader._max_order_ref
    with pytest.raises(DirectWriteError, match="ctp_prepared_place_request_invalid"):
        provider.prepare_order(order(**changed))
    assert (trader._req_id, trader._max_order_ref) == ids and not native.calls


def test_missing_trader_never_connects(system, monkeypatch):
    provider, _, native = system
    provider._allow_implicit_connect = True
    provider._feed._trader = None
    monkeypatch.setattr(
        provider._feed, "ensure_read_only_connected", lambda: pytest.fail("connect")
    )
    with pytest.raises(DirectWriteError):
        provider.prepare_order(order())
    assert not native.calls


@pytest.mark.parametrize("change", ["api", "spi", "login"])
def test_lost_original_source_after_prepare_is_not_sent(system, change):
    provider, trader, native = system
    prepared = provider.prepare_order(order())
    if change == "api":
        trader._session_native_api = object()
    elif change == "spi":
        trader._spi = object()
    else:
        trader._login_identity_observation = None
    assert provider.submit_prepared_order(prepared, object(), Verifier()).sent is False
    assert not native.calls


def test_commit_then_one_native_send_with_original_ids_and_no_locks(system):
    provider, trader, native = system
    prepared = provider.prepare_order(order())
    ids = trader._req_id, trader._max_order_ref
    original = native.ReqOrderInsert

    def checked(field, request_id):
        assert not trader._query_state_lock._is_owned()
        acquired = []

        def take_provider():
            with provider._lock:
                acquired.append(True)

        t = threading.Thread(target=take_provider)
        t.start()
        t.join(timeout=2)
        assert not t.is_alive() and acquired == [True]
        return original(field, request_id)

    native.ReqOrderInsert = checked
    verifier = Verifier()
    result = provider.submit_prepared_order(prepared, object(), verifier)
    assert result.sent is True and result.identity == prepared.identity and verifier.calls == 1
    assert len(native.calls) == 1 and native.calls[0][1] == prepared.request_fields["RequestID"]
    assert (trader._req_id, trader._max_order_ref) == ids
    assert (
        trader.get_order_insert_evidence(
            native.calls[0][1], order_ref=prepared.identity.client_order_id
        )
        is not None
    )
    with pytest.raises(DirectWriteError):
        provider.submit_prepared_order(prepared, object(), verifier)


def test_native_market_recheck_runs_after_grant_before_native_insert(system):
    provider, trader, native = system
    prepared = provider.prepare_order(order())
    verifier = Verifier()
    calls = []

    def recheck():
        assert verifier.calls == 1
        assert trader._query_state_lock._is_owned()
        assert not native.calls
        calls.append("recheck")

    result = provider.submit_prepared_order(
        prepared, object(), verifier, native_market_recheck=recheck)
    assert result.sent is True and calls == ["recheck"] and len(native.calls) == 1


@pytest.mark.parametrize("recheck", [lambda: False, lambda: (_ for _ in ()).throw(RuntimeError()), 42])
def test_native_market_recheck_failure_is_definitely_unsent(system, recheck):
    provider, trader, native = system
    prepared = provider.prepare_order(order())
    result = provider.submit_prepared_order(
        prepared, object(), Verifier(), native_market_recheck=recheck)
    assert result.sent is False and result.error_code == "ctp_prepared_place_not_sent"
    assert not native.calls
    assert trader.get_order_insert_evidence(
        prepared.request_fields["RequestID"], order_ref=prepared.identity.client_order_id
    ) is None


def test_native_market_recheck_cannot_change_source_before_insert(system):
    provider, trader, native = system
    prepared = provider.prepare_order(order())

    def recheck():
        trader._spi._native_spi_source_id = "changed"

    result = provider.submit_prepared_order(
        prepared, object(), Verifier(), native_market_recheck=recheck)
    assert result.sent is False and result.error_code == "ctp_prepared_place_not_sent"
    assert not native.calls


def test_async_disconnect_before_native_send_preserves_generation_and_blocks_send(system):
    provider, trader, native = system
    prepared = provider.prepare_order(order())
    generation = trader.connection_generation
    callback = threading.Thread(target=trader._spi.OnFrontDisconnected, args=(0,))
    callback.start()
    callback.join(timeout=2)
    assert not callback.is_alive()
    assert trader.connection_generation == generation
    assert provider.submit_prepared_order(prepared, object(), Verifier()).sent is False
    assert not native.calls


def test_async_disconnect_can_finish_while_synthetic_native_insert_is_inflight(system):
    provider, trader, native = system
    prepared = provider.prepare_order(order())
    entered, release, disconnected = threading.Event(), threading.Event(), threading.Event()
    generation = trader.connection_generation
    spi = trader._spi

    def hung_insert(field, request_id):
        native.calls.append((field, request_id))
        entered.set()
        if not release.wait(2):
            raise RuntimeError("synthetic insert did not resume")
        return 0

    def disconnect():
        if entered.wait(2):
            spi.OnFrontDisconnected(0)
            disconnected.set()
        release.set()

    native.ReqOrderInsert = hung_insert
    worker = threading.Thread(target=disconnect)
    worker.start()
    try:
        result = provider.submit_prepared_order(prepared, object(), Verifier())
    finally:
        release.set()
        worker.join(timeout=2)
    assert not worker.is_alive() and disconnected.is_set()
    assert trader.connection_generation == generation
    assert len(native.calls) == 1 and result.sent is True


def _test_td_claim_coordinator(trader):
    from bt_api_py.gateway_dispatch_claim import GatewayDispatchClaimCoordinator

    coordinator = GatewayDispatchClaimCoordinator._for_test("synthetic-account", "owner-1")
    for domain in ("registry", "market", "parent_facts", "control", "session_source", "risk"):
        token = coordinator.begin_change(domain)
        coordinator.finish_change(token, "NOT_SELECTED" if domain == "risk" else "READY")
    trader._bind_gateway_td_dispatch_coordinator(
        coordinator, account_scope="synthetic-account", service_generation="owner-1")
    return coordinator


def _candidate_for_test(coordinator):
    from bt_api_py.gateway_dispatch_claim import DispatchCommandBinding
    import time

    binding = DispatchCommandBinding(
        account="synthetic-account", owner_generation="owner-1", journal_id="j",
        strategy="s", principal="p", run_id="r", request_key="request",
        reservation_id="reservation", payload_digest="a" * 64,
        native_identity_digest="b" * 64, marker_ref="marker")
    first, second = coordinator.capture(), coordinator.capture()
    return coordinator.prepare(binding, first, second, deadline_ns=time.monotonic_ns() + 1_000_000_000)


def test_td_callback_invalidates_candidate_before_locked_disconnect_mutation(system, monkeypatch):
    from bt_api_py.gateway_dispatch_claim import DispatchClaimError

    _, trader, _ = system
    coordinator = _test_td_claim_coordinator(trader)
    candidate = _candidate_for_test(coordinator)
    original_begin, original_finish = coordinator.begin_change, coordinator.finish_change
    transitions = []

    def begin(domain):
        assert not trader._query_state_lock._is_owned()
        transitions.append("begin")
        return original_begin(domain)

    def finish(token, status):
        assert not trader._query_state_lock._is_owned()
        transitions.append(status)
        return original_finish(token, status)

    monkeypatch.setattr(coordinator, "begin_change", begin)
    monkeypatch.setattr(coordinator, "finish_change", finish)
    generation = trader.connection_generation
    worker = threading.Thread(target=trader._spi.OnFrontDisconnected, args=(0,))
    worker.start()
    worker.join(timeout=2)
    assert not worker.is_alive() and transitions == ["begin", "UNAVAILABLE"]
    assert not trader._connected and trader.connection_generation == generation
    with pytest.raises(DispatchClaimError, match="DOMAIN_UNAVAILABLE"):
        coordinator.try_claim(candidate)


def test_td_disconnect_after_claim_does_not_wait_for_hung_native(system):
    from bt_api_py.gateway_dispatch_claim import DispatchClaimError

    provider, trader, native = system
    coordinator = _test_td_claim_coordinator(trader)
    candidate = _candidate_for_test(coordinator)
    prepared = provider.prepare_order(order())
    entered, release, disconnected = threading.Event(), threading.Event(), threading.Event()
    spi = trader._spi

    def native_insert(field, request_id):
        native.calls.append((field, request_id))
        entered.set()
        if not release.wait(2):
            raise RuntimeError("synthetic insert did not resume")
        return 0

    def disconnect():
        if entered.wait(2):
            spi.OnFrontDisconnected(0)
            disconnected.set()
        release.set()

    native.ReqOrderInsert = native_insert
    worker = threading.Thread(target=disconnect)
    worker.start()
    try:
        result = provider.submit_prepared_order(
            prepared, object(), Verifier(), native_market_recheck=lambda: coordinator.try_claim(candidate))
    finally:
        release.set()
        worker.join(timeout=2)
    assert not worker.is_alive() and disconnected.is_set()
    assert result.sent is True and len(native.calls) == 1
    with pytest.raises(DispatchClaimError, match="DOMAIN_UNAVAILABLE"):
        coordinator.capture()


def test_closed_td_coordinator_does_not_suppress_real_disconnect_cleanup(system):
    _, trader, _ = system
    coordinator = _test_td_claim_coordinator(trader)
    coordinator.close()
    trader._spi.OnFrontDisconnected(0)
    assert trader._connected is False and trader._session_native_api is None


def test_td_connected_callback_finishes_invalidation_before_native_auth(system, monkeypatch):
    from bt_api_py.gateway_dispatch_claim import DispatchClaimError

    _, trader, _ = system
    coordinator = _test_td_claim_coordinator(trader)
    called = []

    def auth(*_args):
        assert not trader._query_state_lock._is_owned()
        with pytest.raises(DispatchClaimError, match="DOMAIN_UNAVAILABLE"):
            coordinator.capture()
        called.append(True)

    monkeypatch.setattr(trader, "_submit_authenticate", auth)
    trader._spi.OnFrontConnected()
    assert called == [True]


def test_td_auth_and_login_callbacks_invalidate_before_mutation_and_external_calls(system, monkeypatch):
    from bt_api_py.gateway_dispatch_claim import DispatchClaimError

    _, trader, _ = system
    coordinator = _test_td_claim_coordinator(trader)
    events = []

    def submit_login(_api, _generation, _request_id, _field):
        assert not trader._query_state_lock._is_owned()
        with pytest.raises(DispatchClaimError, match="DOMAIN_UNAVAILABLE"):
            coordinator.capture()
        events.append("submit_login")

    monkeypatch.setattr(trader, "_submit_user_login", submit_login)
    with trader._query_state_lock:
        trader._authentication_state = "authenticating"
        trader._authentication_request_id = 47
        trader._authentication_connection_generation = trader.connection_generation
    trader._spi.OnRspAuthenticate(None, SimpleNamespace(ErrorID=0), 47, True)
    assert events == ["submit_login"]
    login_id = trader._login_request_id

    def on_login(_response):
        assert not trader._query_state_lock._is_owned()
        with pytest.raises(DispatchClaimError, match="DOMAIN_UNAVAILABLE"):
            coordinator.capture()
        events.append("login_callback")

    trader.on_login = on_login
    trader._spi.OnRspUserLogin(
        SimpleNamespace(BrokerID=trader._bound_broker_id, UserID=trader._bound_user_id,
                        FrontID=1, SessionID=2, TradingDay="20261010", MaxOrderRef="7"),
        SimpleNamespace(ErrorID=0), login_id, True)
    assert trader._trading_day == "20261010"
    assert events == ["submit_login", "login_callback"]


@pytest.mark.parametrize("kind", ["authenticate", "login"])
def test_td_submit_refusal_invalidates_after_native_return_without_lock_inversion(system, monkeypatch, kind):
    from bt_api_py.gateway_dispatch_claim import DispatchClaimError

    _, trader, native = system
    coordinator = _test_td_claim_coordinator(trader)
    candidate = _candidate_for_test(coordinator)
    original_begin = coordinator.begin_change
    transitions = []

    def begin(domain):
        assert not trader._query_state_lock._is_owned()
        transitions.append(domain)
        return original_begin(domain)

    def native_refusal(*_args, **_kwargs):
        assert not trader._query_state_lock._is_owned()
        transitions.append("native")
        return -1

    monkeypatch.setattr(coordinator, "begin_change", begin)
    monkeypatch.setattr(trader, "_invoke_session_native_request", native_refusal)
    generation = trader.connection_generation
    if kind == "authenticate":
        trader._authentication_state = "authenticating"
        trader._authentication_request_id = 8
        trader._authentication_connection_generation = generation
        trader._submit_authenticate(native, generation, 8, object())
        assert trader._authentication_state == "failed"
    else:
        trader._login_state = "logging_in"
        trader._login_request_id = 8
        trader._login_connection_generation = generation
        trader._submit_user_login(native, generation, 8, object())
        assert trader._login_state == "failed"
    assert transitions == ["native", "session_source"]
    with pytest.raises(DispatchClaimError, match="DOMAIN_UNAVAILABLE"):
        coordinator.try_claim(candidate)


def test_td_retry_timer_invalidates_before_state_change_and_native_submit(system, monkeypatch):
    from bt_api_py.gateway_dispatch_claim import DispatchClaimError

    _, trader, native = system
    coordinator = _test_td_claim_coordinator(trader)
    candidate = _candidate_for_test(coordinator)
    retry_token = object()
    trader._session_retry_token = retry_token
    trader._authentication_state = "failed"
    seen = []

    def submit(*_args):
        assert not trader._query_state_lock._is_owned()
        with pytest.raises(DispatchClaimError, match="DOMAIN_UNAVAILABLE"):
            coordinator.try_claim(candidate)
        seen.append(True)

    monkeypatch.setattr(trader, "_submit_authenticate", submit)
    trader._session_retry_due(retry_token, trader.connection_generation,
                              "authenticate", native, trader._spi)
    assert seen == [True] and trader._authentication_state == "authenticating"


def test_td_settlement_response_invalidates_before_state_mutation(system, monkeypatch):
    from bt_api_py.gateway_dispatch_claim import DispatchClaimError

    _, trader, _ = system
    coordinator = _test_td_claim_coordinator(trader)
    candidate = _candidate_for_test(coordinator)
    trader._settlement_state = "confirming"
    trader._settlement_request_id = 123
    trader._settlement_connection_generation = trader.connection_generation
    trader._settlement_account_fingerprint = trader._account_fingerprint
    trader._settlement_trading_day = trader._trading_day
    original_begin = coordinator.begin_change

    def begin(domain):
        assert not trader._query_state_lock._is_owned()
        return original_begin(domain)

    monkeypatch.setattr(coordinator, "begin_change", begin)
    trader._spi.OnRspSettlementInfoConfirm(
        SimpleNamespace(BrokerID=trader._bound_broker_id, InvestorID=trader._bound_user_id,
                        ConfirmDate=trader._trading_day), SimpleNamespace(ErrorID=0), 123, True)
    assert trader._settlement_state == "confirmed" and trader._ready is False
    with pytest.raises(DispatchClaimError, match="DOMAIN_UNAVAILABLE"):
        coordinator.try_claim(candidate)


@pytest.mark.parametrize("operation", ["stop", "abort", "uninitialized"])
def test_td_lifecycle_changes_invalidate_outside_state_lock_and_native_cleanup(system, monkeypatch, operation):
    from bt_api_py.gateway_dispatch_claim import DispatchClaimError

    _, trader, native = system
    coordinator = _test_td_claim_coordinator(trader)
    candidate = _candidate_for_test(coordinator)
    original_begin, original_finish = coordinator.begin_change, coordinator.finish_change
    trace = []

    def begin(domain):
        assert not trader._query_state_lock._is_owned()
        trace.append("begin")
        return original_begin(domain)

    def finish(token, status):
        assert not trader._query_state_lock._is_owned()
        trace.append(status)
        return original_finish(token, status)

    def cleanup(*_args, **_kwargs):
        assert not trader._query_state_lock._is_owned()
        assert trace[-1] == "UNAVAILABLE"
        with pytest.raises(DispatchClaimError, match="DOMAIN_UNAVAILABLE"):
            coordinator.try_claim(candidate)
        trace.append("native_cleanup")
        return True

    monkeypatch.setattr(coordinator, "begin_change", begin)
    monkeypatch.setattr(coordinator, "finish_change", finish)
    monkeypatch.setattr(trader, "_release_native_api_immediately", cleanup)
    if operation == "stop":
        assert trader._stop_native_session() is True
    elif operation == "abort":
        monkeypatch.setattr(trader, "_is_start_current_locked", lambda *_args: True)
        assert trader._abort_startup(native, trader._spi, 1) is True
    else:
        monkeypatch.setattr(trader, "_stop_native_session", cleanup)
        assert trader._stop_uninitialized_native_api(native, spi=trader._spi) is True
    assert trace == ["begin", "UNAVAILABLE", "native_cleanup"]


def test_unlocked_td_api_replacement_invalidates_before_native_source_changes(system, monkeypatch):
    from bt_api_py.gateway_dispatch_claim import DispatchClaimError

    _, trader, _ = system
    coordinator = _test_td_claim_coordinator(trader)
    candidate = _candidate_for_test(coordinator)
    original_begin, original_finish = coordinator.begin_change, coordinator.finish_change
    transitions = []

    def begin(domain):
        assert not trader._query_state_lock._is_owned()
        transitions.append("begin")
        return original_begin(domain)

    def finish(token, status):
        assert not trader._query_state_lock._is_owned()
        transitions.append(status)
        return original_finish(token, status)

    monkeypatch.setattr(coordinator, "begin_change", begin)
    monkeypatch.setattr(coordinator, "finish_change", finish)
    replacement = object()
    trader._api = replacement
    assert trader._api is replacement and trader._spi is None
    assert transitions == ["begin", "UNAVAILABLE"]
    with pytest.raises(DispatchClaimError, match="DOMAIN_UNAVAILABLE"):
        coordinator.try_claim(candidate)


def test_td_start_reservation_invalidates_before_gate_revocation(system, monkeypatch):
    from bt_api_py.gateway_dispatch_claim import DispatchClaimError

    _, trader, _ = system
    trader._api = None
    coordinator = _test_td_claim_coordinator(trader)
    candidate = _candidate_for_test(coordinator)
    original_begin = coordinator.begin_change

    def begin(domain):
        assert not trader._query_state_lock._is_owned()
        return original_begin(domain)

    monkeypatch.setattr(coordinator, "begin_change", begin)
    assert trader._reserve_start_generation() > 0
    with pytest.raises(DispatchClaimError, match="DOMAIN_UNAVAILABLE"):
        coordinator.try_claim(candidate)


def test_md_connection_and_login_callbacks_invalidate_outside_md_lock(monkeypatch):
    from bt_api_ctp.ctp.client import MdClient, _MdSpi
    from bt_api_py.gateway_dispatch_claim import DispatchClaimError, GatewayDispatchClaimCoordinator

    client = MdClient("tcp://offline.invalid:1", "9999", "account", "unused")
    spi = _MdSpi(client)
    coordinator = GatewayDispatchClaimCoordinator._for_test("synthetic-account", "owner-1")
    for domain in ("registry", "market", "parent_facts", "control", "session_source", "risk"):
        token = coordinator.begin_change(domain)
        coordinator.finish_change(token, "NOT_SELECTED" if domain == "risk" else "READY")
    client._bind_gateway_md_dispatch_coordinator(
        coordinator, account_scope="synthetic-account", service_generation="owner-1")
    original_begin, original_finish = coordinator.begin_change, coordinator.finish_change
    transitions = []

    def begin(domain):
        assert not client._state_lock._is_owned()
        transitions.append("begin")
        return original_begin(domain)

    def finish(token, status):
        assert not client._state_lock._is_owned()
        transitions.append(status)
        return original_finish(token, status)

    def external_callback(*_args):
        assert not client._state_lock._is_owned()
        assert transitions[-1] == "UNAVAILABLE"
        with pytest.raises(DispatchClaimError, match="DOMAIN_UNAVAILABLE"):
            coordinator.capture()
        transitions.append("callback")

    monkeypatch.setattr(coordinator, "begin_change", begin)
    monkeypatch.setattr(coordinator, "finish_change", finish)
    client.on_login = external_callback
    client.on_disconnect = external_callback
    spi.OnFrontConnected()
    request_id = client._login_request_id
    spi.OnRspUserLogin(SimpleNamespace(BrokerID="9999", UserID="account", TradingDay="20261009"),
                       SimpleNamespace(ErrorID=0), request_id, True)
    assert client._loggedin is True
    generation = client._connection_generation
    worker = threading.Thread(target=spi.OnFrontDisconnected, args=(0,))
    worker.start()
    worker.join(timeout=2)
    assert not worker.is_alive() and not client._connected
    assert client._connection_generation == generation
    assert transitions == ["begin", "UNAVAILABLE", "begin", "UNAVAILABLE", "callback",
                           "begin", "UNAVAILABLE", "callback"]


def test_md_login_refusal_invalidates_after_native_return(monkeypatch):
    from bt_api_ctp.ctp.client import MdClient, _MdSpi
    from bt_api_py.gateway_dispatch_claim import DispatchClaimError, GatewayDispatchClaimCoordinator

    client = MdClient("tcp://offline.invalid:1", "9999", "account", "unused")
    native = object()
    spi = _MdSpi(client, native_api=native)
    client._api, client._spi = native, spi
    client._connection_generation = 1
    client._connected = True
    client._login_request_pending = True
    client._login_request_id = 4
    client._login_request_generation = 1
    coordinator = GatewayDispatchClaimCoordinator._for_test("synthetic-account", "owner-1")
    for domain in ("registry", "market", "parent_facts", "control", "session_source", "risk"):
        token = coordinator.begin_change(domain)
        coordinator.finish_change(token, "NOT_SELECTED" if domain == "risk" else "READY")
    client._bind_gateway_md_dispatch_coordinator(
        coordinator, account_scope="synthetic-account", service_generation="owner-1")
    candidate = _candidate_for_test(coordinator)
    original_begin = coordinator.begin_change
    trace = []

    def begin(domain):
        assert not client._state_lock._is_owned()
        trace.append("begin")
        return original_begin(domain)

    def refuse(*_args):
        assert not client._state_lock._is_owned()
        trace.append("native")
        return -1

    monkeypatch.setattr(coordinator, "begin_change", begin)
    monkeypatch.setattr(client, "_invoke_native_request", refuse)
    client._submit_md_login(native, spi, 1, 4, object())
    assert trace == ["native", "begin"] and not client._connected
    with pytest.raises(DispatchClaimError, match="DOMAIN_UNAVAILABLE"):
        coordinator.try_claim(candidate)


@pytest.mark.parametrize("operation", ["reserve_start", "stop", "abort"])
def test_md_lifecycle_publishes_invalidation_before_native_cleanup(monkeypatch, operation):
    from bt_api_ctp.ctp.client import MdClient, _MdSpi
    from bt_api_py.gateway_dispatch_claim import DispatchClaimError, GatewayDispatchClaimCoordinator

    client = MdClient("tcp://offline.invalid:1", "9999", "account", "unused")
    coordinator = GatewayDispatchClaimCoordinator._for_test("synthetic-account", "owner-1")
    for domain in ("registry", "market", "parent_facts", "control", "session_source", "risk"):
        token = coordinator.begin_change(domain)
        coordinator.finish_change(token, "NOT_SELECTED" if domain == "risk" else "READY")
    client._bind_gateway_md_dispatch_coordinator(
        coordinator, account_scope="synthetic-account", service_generation="owner-1")
    candidate = _candidate_for_test(coordinator)
    original_begin, original_finish = coordinator.begin_change, coordinator.finish_change
    trace = []

    def begin(domain):
        assert not client._state_lock._is_owned()
        trace.append("begin")
        return original_begin(domain)

    def finish(token, status):
        assert not client._state_lock._is_owned()
        trace.append(status)
        return original_finish(token, status)

    def cleanup(*_args):
        assert not client._state_lock._is_owned()
        assert trace[-1] == "UNAVAILABLE"
        with pytest.raises(DispatchClaimError, match="DOMAIN_UNAVAILABLE"):
            coordinator.try_claim(candidate)
        trace.append("native_cleanup")
        return True

    monkeypatch.setattr(coordinator, "begin_change", begin)
    monkeypatch.setattr(coordinator, "finish_change", finish)
    monkeypatch.setattr(client, "_release_native_api_immediately", cleanup)
    if operation == "reserve_start":
        assert client._reserve_start_generation() > 0
        assert trace == ["begin", "UNAVAILABLE"]
        with pytest.raises(DispatchClaimError, match="DOMAIN_UNAVAILABLE"):
            coordinator.try_claim(candidate)
        return
    api = object()
    spi = _MdSpi(client, native_api=api)
    client._api, client._spi = api, spi
    if operation == "stop":
        assert client._stop_native_session() is True
    else:
        monkeypatch.setattr(client, "_is_start_current_locked", lambda *_args: True)
        assert client._abort_startup(api, spi, 1) is True
    assert trace == ["begin", "UNAVAILABLE", "native_cleanup"]


def test_md_subscription_native_hang_does_not_hold_coordinator_or_state_lock(monkeypatch):
    from bt_api_ctp.ctp.client import MdClient
    from bt_api_py.gateway_dispatch_claim import DispatchClaimError, GatewayDispatchClaimCoordinator

    client = MdClient("tcp://offline.invalid:1", "9999", "account", "unused")
    client._api, client._loggedin = object(), True
    coordinator = GatewayDispatchClaimCoordinator._for_test("synthetic-account", "owner-1")
    for domain in ("registry", "market", "parent_facts", "control", "session_source", "risk"):
        token = coordinator.begin_change(domain)
        coordinator.finish_change(token, "NOT_SELECTED" if domain == "risk" else "READY")
    client._bind_gateway_md_dispatch_coordinator(
        coordinator, account_scope="synthetic-account", service_generation="owner-1")
    entered, release = threading.Event(), threading.Event()
    completed = []

    def native(*_args):
        assert not client._state_lock._is_owned()
        entered.set()
        if not release.wait(2):
            raise RuntimeError("synthetic subscription did not resume")
        return 0

    monkeypatch.setattr(client, "_invoke_native_request", native)
    worker = threading.Thread(target=lambda: completed.append(client.subscribe_additional(["IF9999"])))
    worker.start()
    try:
        assert entered.wait(1)
        with pytest.raises(DispatchClaimError, match="DOMAIN_UNAVAILABLE"):
            coordinator.capture()
        token = coordinator.begin_change("control")
        coordinator.finish_change(token, "UNAVAILABLE")
        assert client._state_lock.acquire(timeout=1)
        client._state_lock.release()
    finally:
        release.set()
        worker.join(timeout=2)
    assert not worker.is_alive() and completed == [("IF9999",)]


def test_forgery_revoke_and_native_exception(system):
    provider, _, native = system
    prepared = provider.prepare_order(order())
    forged = PreparedPlaceRequest.from_mapping(prepared.to_mapping())
    with pytest.raises(DirectWriteError):
        provider.submit_prepared_order(forged, object(), Verifier())
    verifier = Verifier()
    verifier.revoked = True
    assert provider.submit_prepared_order(prepared, object(), verifier).sent is False
    assert not native.calls
    prepared = provider.prepare_order(order())

    def fail(field, request_id):
        native.calls.append((field, request_id))
        raise RuntimeError("unknown native result")

    native.ReqOrderInsert = fail
    assert provider.submit_prepared_order(prepared, object(), Verifier()).sent is None
    assert len(native.calls) == 1


@pytest.mark.parametrize("changed", ["source", "login", "field", "grant"])
def test_final_lock_rechecks_source_fields_and_grant(system, changed):
    provider, trader, native = system
    prepared = provider.prepare_order(order())
    verifier = Verifier()
    orig = trader._invoke_session_native_request

    def alter(api, method, *args, **kwargs):
        if changed == "source":
            trader._spi._native_spi_source_id = "changed"
        elif changed == "login":
            login = trader._login_identity_observation
            trader._login_identity_observation = replace(login, request_id=login.request_id + 1)
        elif changed == "field":
            args[0].LimitPrice += 1.0
        else:
            verifier.revoked = True
        return orig(api, method, *args, **kwargs)

    trader._invoke_session_native_request = alter
    assert provider.submit_prepared_order(prepared, object(), verifier).sent is False
    assert not native.calls


@pytest.fixture
def callback(system):
    provider, trader, native = system
    prepared = provider.prepare_order(order())
    binding = capture_query_session(trader, environment=None)
    row = dict(prepared.request_fields)
    row["LimitPrice"] = float.fromhex(row["LimitPrice"])
    buffer = CtpGatewayReturnBuffer(broker_id="3070", investor_id="offline-account")
    assert buffer._append(
        kind="insert_response",
        row=row,
        source_ref=binding.source_ref,
        source_tags=binding._source_tags,
        connection_generation=prepared.session_generation,
        reception_trading_day=prepared.trading_day,
        front_id=prepared.identity.fields["front_id"],
        session_id=prepared.identity.fields["session_id"],
        request_id=row["RequestID"],
        is_last=True,
        error_code=5,
        login_request_id=trader._current_login_identity_locked().request_id,
    )
    return prepared, buffer.offer().records[0]


def project(record, prepared):
    return project_gateway_place_error(
        record,
        prepared,
        account_scope="logical-account",
        expected_broker="3070",
        expected_investor="offline-account",
        instrument=MarketInstrumentKey("ctp", "CZCE", "simnow", "future", "SA2701", "SA2701"),
    )


def test_exact_place_error_projects_without_terminal_state(callback):
    prepared, record = callback
    result = project(record, prepared)
    assert (
        gateway_place_error_key(record, expected_broker="3070", expected_investor="offline-account")
        == prepared.request_keys[0]
    )
    assert (
        result.place_key == prepared.request_keys[0] and result.prepared_digest == prepared.digest()
    )
    assert (
        result.fact.kind is DirectExecutionEventKind.ORDER_ERROR and result.fact.schema_version == 3
    )
    assert result.fact.status is None and result.fact.place_request_key == result.place_key
    assert result.fact.identity == prepared.identity and not result.fact.identity_keys
    assert (
        project(
            replace(record, kind="insert_error", request_id=None, is_last=None), prepared
        ).place_key
        == result.place_key
    )


@pytest.mark.parametrize(
    "change",
    [
        "broker",
        "investor",
        "request",
        "ref",
        "price",
        "direction",
        "source",
        "login",
        "day",
        "generation",
        "error",
    ],
)
def test_forged_insert_error_remains_unresolved(callback, change):
    prepared, record = callback
    row = dict(record.row)
    extra = {}
    if change == "broker":
        row["BrokerID"] = ""
    elif change == "investor":
        row.pop("InvestorID")
    elif change == "request":
        row["RequestID"] += 1
    elif change == "ref":
        row["OrderRef"] = "000000000999"
    elif change == "price":
        row["LimitPrice"] += 1.0
    elif change == "direction":
        row["Direction"] = "1"
    elif change == "source":
        extra["source_ref"] = "ctp-query-source-v1:" + "f" * 64
    elif change == "login":
        extra["login_request_id"] = record.login_request_id + 1
    elif change == "day":
        extra["reception_trading_day"] = "20261010"
    elif change == "generation":
        extra["connection_generation"] = record.connection_generation + 1
    elif change == "error":
        extra["error_code"] = 0
    with pytest.raises(CtpGatewayPlaceReturnError):
        project(replace(record, row=MappingProxyType(row), **extra), prepared)


def test_close_clears_prepared_place_ticket(system):
    provider, _, native = system
    prepared = provider.prepare_order(order())
    assert provider._place_reservations
    provider.close()
    assert not provider._place_reservations
    with pytest.raises(DirectWriteError):
        provider.release_prepared_order(prepared)
    assert not native.calls


def test_absent_exchange_never_resolves_or_allocates(system, monkeypatch):
    provider, trader, native = system
    before = trader._req_id, trader._max_order_ref
    monkeypatch.setattr(provider, "resolve_exchange_id", lambda *_: pytest.fail("catalog lookup"))
    monkeypatch.setattr(
        provider._feed, "ensure_read_only_connected", lambda: pytest.fail("connect")
    )
    with pytest.raises(DirectWriteError):
        provider.prepare_order(order(exchange_id=None))
    assert before == (trader._req_id, trader._max_order_ref) and not native.calls


@pytest.mark.parametrize("change", ["field", "login"])
def test_reentrant_mutation_after_grant_consumption_is_rechecked(system, change):
    provider, trader, native = system
    prepared = provider.prepare_order(order())
    original = trader._invoke_session_native_request
    observed = []

    def invoke(api, method, *args, **kwargs):
        observed.append(args[0])
        return original(api, method, *args, **kwargs)

    trader._invoke_session_native_request = invoke

    class MutatingVerifier(Verifier):
        def consume(self, grant, **kwargs):
            super().consume(grant, **kwargs)
            if change == "field":
                observed[0].VolumeTotalOriginal += 1
            else:
                login = trader._login_identity_observation
                trader._login_identity_observation = replace(login, request_id=login.request_id + 1)

    verifier = MutatingVerifier()
    result = provider.submit_prepared_order(prepared, object(), verifier)
    assert verifier.calls == 1 and result.sent is False and not native.calls


@pytest.mark.parametrize(
    "name",
    [
        "BrokerID",
        "InvestorID",
        "UserID",
        "InstrumentID",
        "ExchangeID",
        "Direction",
        "CombOffsetFlag",
        "CombHedgeFlag",
        "VolumeTotalOriginal",
        "MinVolume",
        "ForceCloseReason",
        "IsAutoSuspend",
        "UserForceClose",
        "ContingentCondition",
        "OrderPriceType",
        "TimeCondition",
        "VolumeCondition",
        "LimitPrice",
        "OrderRef",
        "RequestID",
    ],
)
def test_each_native_field_mutation_fails_before_send(system, name):
    provider, trader, native = system
    prepared = provider.prepare_order(order())
    original = trader._invoke_session_native_request

    def alter(api, method, *args, **kwargs):
        field = args[0]
        value = getattr(field, name)
        setattr(field, name, value + 1 if type(value) in (int, float) else value + "x")
        return original(api, method, *args, **kwargs)

    trader._invoke_session_native_request = alter
    assert provider.submit_prepared_order(prepared, object(), Verifier()).sent is False
    assert not native.calls


def test_real_coordinator_commits_before_prepared_native(system):
    from bt_api_py.direct_write import DirectWriteCoordinator
    from bt_api_py.write_admission import WriteAdmission

    provider, _, native = system
    coordinator = DirectWriteCoordinator(WriteAdmission({"CTP___FUTURE": True}), lambda _: provider)
    captured = []

    class Port:
        def __init__(self, marker):
            self.marker = marker

        def commit(self, record):
            assert not native.calls
            captured.append(record)
            return self.marker

    failure = coordinator.place_prepared(
        "CTP___FUTURE", order(), commit_port=Port(None), host_scope="logical-account"
    )
    assert failure.sent is False and not native.calls and not provider._place_reservations
    result = coordinator.place_prepared(
        "CTP___FUTURE", order(), commit_port=Port("marker"), host_scope="logical-account"
    )
    assert result.sent is True and result.marker_id == "marker" and len(native.calls) == 1
    assert captured[-1].prepared_place.request_fields["RequestID"] == native.calls[0][1]


def test_wrong_owner_and_modified_original_ticket_rejected(system):
    provider, _, native = system
    prepared = provider.prepare_order(order())
    errors = []

    def other_thread():
        try:
            provider.submit_prepared_order(prepared, object(), Verifier())
        except DirectWriteError as exc:
            errors.append(exc.code)

    thread = threading.Thread(target=other_thread)
    thread.start()
    thread.join(2)
    assert not thread.is_alive() and errors == ["ctp_direct_wrong_owner"]
    object.__setattr__(prepared, "symbol", "changed")
    with pytest.raises(DirectWriteError):
        provider.submit_prepared_order(prepared, object(), Verifier())
    assert not native.calls


def test_wrong_pid_rejected_before_ticket_or_native(system):
    provider, _, native = system
    prepared = provider.prepare_order(order())
    provider._settlement_owner_pid = -1
    with pytest.raises(DirectWriteError, match="ctp_direct_wrong_owner"):
        provider.submit_prepared_order(prepared, object(), Verifier())
    assert not native.calls


def test_invalid_source_before_prepare_does_not_allocate_ids(system):
    provider, trader, native = system
    trader._spi._native_spi_source_id = None
    before = trader._req_id, trader._max_order_ref
    with pytest.raises(DirectWriteError, match="ctp_prepared_place_session_unavailable"):
        provider.prepare_order(order())
    assert before == (trader._req_id, trader._max_order_ref) and not native.calls
