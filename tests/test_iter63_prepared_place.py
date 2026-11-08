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
