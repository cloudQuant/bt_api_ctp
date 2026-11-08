"""Synthetic prepared cancel/source matching; never opens a native connection."""

from __future__ import annotations

from dataclasses import replace
from types import MappingProxyType

import pytest
from bt_api_base.direct_cancel_protocol import PreparedCancelRequest
from bt_api_base.direct_write_protocol import DirectExecutionEventKind
from bt_api_base.market_protocol import MarketInstrumentKey
from test_iter63_prepared_cancel import Verifier, make_system, target

from bt_api_ctp.gateway_cancel_return_projection import (
    CtpGatewayCancelReturnError,
    gateway_cancel_error_key,
    project_gateway_cancel_error,
)
from bt_api_ctp.gateway_cancel_source import cancel_attempt_key
from bt_api_ctp.gateway_return_buffer import CtpGatewayReturnBuffer
from bt_api_ctp.registered_sim_session import capture_query_session


@pytest.fixture
def attempt(monkeypatch):
    provider, trader, native = make_system(monkeypatch)
    prepared = provider.prepare_cancel_order(target(), "SA2701")
    binding = capture_query_session(trader, environment=None)
    buf = CtpGatewayReturnBuffer(broker_id="3070", investor_id="offline-account")
    row = {
        name: value
        for name, value in prepared.request_fields.items()
        if name not in {"DispatchFrontID", "DispatchSessionID"}
    }
    assert buf._append(
        kind="action_response",
        row=row,
        source_ref=binding.source_ref,
        source_tags=binding._source_tags,
        connection_generation=prepared.session_generation,
        reception_trading_day=prepared.trading_day,
        front_id=prepared.request_fields["DispatchFrontID"],
        session_id=prepared.request_fields["DispatchSessionID"],
        request_id=row["RequestID"],
        is_last=True,
        error_code=5,
        login_request_id=trader._current_login_identity_locked().request_id,
    )
    return provider, trader, native, prepared, buf.offer().records[0]


def project(record, prepared):
    return project_gateway_cancel_error(
        record,
        prepared,
        account_scope="account-scope",
        expected_broker="3070",
        expected_investor="offline-account",
        instrument=MarketInstrumentKey("ctp", "CZCE", "simnow", "future", "SA2701", "SA2701"),
    )


def test_exact_callback_and_error_return_use_same_prepared_source(attempt):
    _, _, _, prepared, record = attempt
    projection = project(record, prepared)
    assert (
        gateway_cancel_error_key(
            record, expected_broker="3070", expected_investor="offline-account"
        )
        == prepared.request_keys[0]
    )
    assert projection.cancel_key == prepared.request_keys[0]
    assert projection.prepared_digest == prepared.digest()
    assert dict(projection.source_binding) == dict(prepared.source_binding)
    assert projection.fact.identity == prepared.target_identity
    assert projection.fact.kind is DirectExecutionEventKind.CANCEL_ERROR
    assert projection.fact.schema_version == 2
    assert projection.fact.cancel_request_key == projection.cancel_key
    assert projection.fact.error_code == "ctp_cancel_error_5"
    assert not projection.fact.identity_keys and not projection.fact.binding_keys
    assert projection.fact.status is None and projection.fact.order_quantity is None
    with pytest.raises(TypeError):
        projection.source_binding["schema"] = "changed"
    err = replace(record, kind="action_error", request_id=None, is_last=None)
    assert project(err, prepared).cancel_key == projection.cancel_key
    assert project(err, prepared).fact.source_ref != projection.fact.source_ref


def test_separate_attempts_with_same_order_error_have_distinct_fact_digest(attempt):
    provider, trader, _, first, record = attempt
    second = provider.prepare_cancel_order(target(), "SA2701")
    changed = dict(record.row)
    changed["RequestID"] = second.request_fields["RequestID"]
    changed["OrderActionRef"] = second.request_fields["OrderActionRef"]
    other = replace(record, row=MappingProxyType(changed), request_id=changed["RequestID"])
    a, b = project(record, first), project(other, second)
    assert a.fact.identity == b.fact.identity
    assert a.cancel_key != b.cancel_key
    assert a.fact.fact_digest != b.fact.fact_digest


@pytest.mark.parametrize(
    "mutation",
    [
        "source_ref",
        "source_tags",
        "login",
        "day",
        "generation",
        "front",
        "session",
        "response_request",
        "missing_request",
        "action_ref",
        "request_id",
        "symbol",
        "exchange",
        "action",
        "order_ref",
        "target_front",
        "target_session",
        "order_sys",
        "missing_broker",
        "blank_broker",
        "wrong_broker",
        "missing_investor",
        "wrong_investor",
        "zero_error",
        "no_error",
    ],
)
def test_inconsistent_source_account_attempt_or_target_rejected(attempt, mutation):
    _, _, _, prepared, record = attempt
    row = dict(record.row)
    changes = {}
    if mutation == "source_ref":
        changes["source_ref"] = "ctp-query-source-v1:" + "f" * 64
    elif mutation == "source_tags":
        changes["source_tags"] = ("changed", *record.source_tags[1:])
    elif mutation == "login":
        changes["login_request_id"] = record.login_request_id + 1
    elif mutation == "day":
        changes["reception_trading_day"] = "20261010"
    elif mutation == "generation":
        changes["connection_generation"] = record.connection_generation + 1
    elif mutation == "front":
        changes["front_id"] = record.front_id + 1
    elif mutation == "session":
        changes["session_id"] = record.session_id + 1
    elif mutation == "response_request":
        changes["request_id"] = record.request_id + 1
    elif mutation == "missing_request":
        row.pop("RequestID")
    elif mutation == "action_ref":
        row["OrderActionRef"] += 1
    elif mutation == "request_id":
        row["RequestID"] += 1
    elif mutation == "symbol":
        row["InstrumentID"] = "other"
    elif mutation == "exchange":
        row["ExchangeID"] = "SHFE"
    elif mutation == "action":
        row["ActionFlag"] = "1"
    elif mutation == "order_ref":
        row["OrderRef"] = "other"
    elif mutation == "target_front":
        row["FrontID"] += 1
    elif mutation == "target_session":
        row["SessionID"] += 1
    elif mutation == "order_sys":
        row["OrderSysID"] = "other"
    elif mutation == "missing_broker":
        row.pop("BrokerID")
    elif mutation == "blank_broker":
        row["BrokerID"] = ""
    elif mutation == "wrong_broker":
        row["BrokerID"] = "other"
    elif mutation == "missing_investor":
        row.pop("InvestorID")
    elif mutation == "wrong_investor":
        row["InvestorID"] = "other"
    elif mutation == "zero_error":
        changes["error_code"] = 0
    elif mutation == "no_error":
        changes["error_code"] = None
    changes["row"] = MappingProxyType(row)
    with pytest.raises(CtpGatewayCancelReturnError):
        project(replace(record, **changes), prepared)


def test_legacy_and_other_callback_types_remain_unsupported(attempt):
    _, _, _, prepared, record = attempt
    legacy = PreparedCancelRequest(
        prepared.reservation_id,
        prepared.target_identity,
        prepared.symbol,
        prepared.trading_day,
        prepared.session_generation,
        dict(prepared.request_fields),
        prepared.request_keys,
    )
    with pytest.raises(CtpGatewayCancelReturnError, match="UNSUPPORTED_RETURN"):
        project(record, legacy)
    with pytest.raises(CtpGatewayCancelReturnError, match="UNSUPPORTED_RETURN"):
        project(replace(record, kind="insert_error"), prepared)


def test_durable_preparation_must_itself_match_original_target(attempt):
    _, _, _, prepared, record = attempt
    original = prepared.target_identity
    altered_target = replace(original, fields={**original.fields, "front_id": 777})
    forged = replace(prepared, target_identity=altered_target)
    with pytest.raises(CtpGatewayCancelReturnError):
        project(record, forged)


def test_changed_sealed_login_before_native_rejected(attempt):
    provider, trader, native, prepared, _ = attempt
    original = trader._invoke_session_native_request
    verifier = Verifier()

    def change_login(api, method, *args, **kwargs):
        login = trader._login_identity_observation
        trader._login_identity_observation = replace(login, request_id=login.request_id + 1)
        return original(api, method, *args, **kwargs)

    trader._invoke_session_native_request = change_login
    result = provider.submit_prepared_cancel(prepared, object(), verifier)
    assert result.sent is False and not native.calls and verifier.calls == 0


def test_new_trader_same_numeric_session_and_request_has_distinct_source(monkeypatch):
    first, _, _ = make_system(monkeypatch)
    second, _, _ = make_system(monkeypatch)
    a = first.prepare_cancel_order(target(), "SA2701")
    b = second.prepare_cancel_order(target(), "SA2701")
    assert a.request_fields["RequestID"] == b.request_fields["RequestID"]
    assert a.request_fields["DispatchFrontID"] == b.request_fields["DispatchFrontID"]
    assert a.request_fields["DispatchSessionID"] == b.request_fields["DispatchSessionID"]
    assert a.source_binding != b.source_binding and a.request_keys != b.request_keys


def test_invalid_calendar_day_rejected_by_attempt_key(attempt):
    _, _, _, prepared, _ = attempt
    fields = prepared.request_fields
    with pytest.raises(ValueError, match="INVALID_CANCEL_SOURCE"):
        cancel_attempt_key(
            broker=fields["BrokerID"],
            investor=fields["InvestorID"],
            day="20260231",
            front=fields["DispatchFrontID"],
            session=fields["DispatchSessionID"],
            request_id=fields["RequestID"],
            action_ref=fields["OrderActionRef"],
            source_binding=prepared.source_binding,
        )


def test_zero_registration_generation_remains_unavailable(monkeypatch):
    from types import SimpleNamespace

    provider, trader, native = make_system(monkeypatch)
    tags = trader._query_source_tags_locked()
    trader._spi._callback_source_tags = SimpleNamespace(
        source_instance_id=tags[0],
        native_client_epoch=tags[1],
        native_api_source_id=tags[2],
        native_spi_source_id=tags[3],
        native_api_generation=tags[4],
        connection_generation=0,
    )
    with pytest.raises(Exception, match="ctp_prepared_cancel_source_unavailable"):
        provider.prepare_cancel_order(target(), "SA2701")
    assert not native.calls


def test_venue_target_requires_exact_padded_native_id(monkeypatch):
    provider, trader, _ = make_system(monkeypatch)
    prepared = provider.prepare_cancel_order(target(venue=True), "SA2701")
    binding = capture_query_session(trader, environment=None)
    row = {
        name: value
        for name, value in prepared.request_fields.items()
        if name not in {"DispatchFrontID", "DispatchSessionID"}
    }
    buf = CtpGatewayReturnBuffer(broker_id="3070", investor_id="offline-account")
    assert buf._append(
        kind="action_error",
        row=row,
        source_ref=binding.source_ref,
        source_tags=binding._source_tags,
        connection_generation=prepared.session_generation,
        reception_trading_day=prepared.trading_day,
        front_id=prepared.request_fields["DispatchFrontID"],
        session_id=prepared.request_fields["DispatchSessionID"],
        error_code=5,
        login_request_id=trader._current_login_identity_locked().request_id,
    )
    record = buf.offer().records[0]
    assert project(record, prepared).fact.identity == prepared.target_identity
    bad = dict(record.row)
    bad["OrderSysID"] = bad["OrderSysID"].strip()
    with pytest.raises(CtpGatewayCancelReturnError):
        project(replace(record, row=MappingProxyType(bad)), prepared)


def test_login_mutation_during_source_capture_never_creates_preparation(monkeypatch):
    from bt_api_ctp import registered_sim_session

    provider, trader, native = make_system(monkeypatch)
    original = registered_sim_session.capture_query_session

    def capture_then_change(*args, **kwargs):
        result = original(*args, **kwargs)
        login = trader._login_identity_observation
        trader._login_identity_observation = replace(login, request_id=login.request_id + 1)
        return result

    monkeypatch.setattr(registered_sim_session, "capture_query_session", capture_then_change)
    with pytest.raises(Exception, match="ctp_prepared_cancel_source_unavailable"):
        provider.prepare_cancel_order(target(), "SA2701")
    assert not provider._cancel_reservations and not native.calls
