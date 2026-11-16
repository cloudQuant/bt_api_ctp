"""Synthetic regressions for status ingress session and source fencing."""

import threading
import time

import pytest
from test_ctp_callback_ingress_client import (
    _FakeTraderApiFactory,
    _new_active_client,
)
from test_iter67_instrument_status_ingress import DAY, SYMBOL, VENUE, FakeTrader, _feed_with, native

import bt_api_ctp.ctp.client as client_module
import bt_api_ctp.instrument_status_ingress as status_ingress
from bt_api_ctp.ctp import callback_ingress as ingress
from bt_api_ctp.ctp.client import TraderClient
from bt_api_ctp.direct_write_provider import CtpDirectWriteProvider
from bt_api_ctp.instrument_status_ingress import InstrumentStatusIngressBridge
from bt_api_ctp.instrument_status_observation import STATUS_CLOSED
from bt_api_ctp.registered_sim_session import capture_query_session, validate_query_session


def _payload(
    *,
    generation=7,
    tag_generation=None,
    phase=ingress.CtpTraderCallbackPhase.ACTIVE,
    status="2",
    sequence=1,
):
    tags = ingress.CtpTraderCallbackSourceTagsV2(
        source_instance_id="source-1",
        native_client_epoch="epoch-1",
        native_api_source_id="api-1",
        native_spi_source_id="spi-1",
        native_api_generation=1,
        connection_generation=generation if tag_generation is None else tag_generation,
    )
    return ingress.build_callback_ingress_record_v2(
        owner_intent_id="ctp-status-test",
        callback_name="OnRtnInstrumentStatus",
        source_phase=phase,
        source_tags=tags,
        connection_generation=generation,
        source_sequence=sequence,
        callback_monotonic_ns=777,
        args=(native(status),),
    ).to_payload()


def _provider():
    trader = FakeTrader()
    provider = CtpDirectWriteProvider(_feed_with(trader))
    provider.ensure_listening()
    return trader, provider


def test_managed_native_initial_registration_generation_is_accepted(monkeypatch):
    client, api, _, _ = _new_active_client(monkeypatch)
    provider = CtpDirectWriteProvider(_feed_with(client))
    try:
        provider.ensure_listening()
        captured = []
        client.add_instrument_status_listener(captured.append)
        api.spi.OnRtnInstrumentStatus(native("6"))
        assert len(captured) == 1
        assert (
            captured[0]["source_connection_generation"],
            captured[0]["connection_generation"],
        ) == (0, 1)
        view = provider.instrument_status_view(VENUE, SYMBOL)
        assert (view.usable, view.effective_status_category) == (True, STATUS_CLOSED)
        bound_listener = provider._instrument_status_listener
        provider.close()
        assert bound_listener not in client._instrument_status_listeners
        api.spi.OnRtnInstrumentStatus(native("2"))
        assert len(captured) == 2
        assert provider.instrument_status_view(VENUE, SYMBOL).usable is False
    finally:
        provider.close()
        client.stop()


def test_direct_native_initial_and_reconnect_generations_are_accepted(monkeypatch):
    _FakeTraderApiFactory.instances = []
    _FakeTraderApiFactory.block_join = False
    _FakeTraderApiFactory.register_spi_exception = None
    _FakeTraderApiFactory.init_exception = None
    monkeypatch.setattr(client_module, "_check_native_module", lambda: None)
    monkeypatch.setattr(client_module, "_flow_dir", lambda _name: "fake-flow")
    monkeypatch.setattr(client_module, "_register_ctp_native_api", lambda _api: None)
    monkeypatch.setattr(client_module, "CThostFtdcTraderApi", _FakeTraderApiFactory)
    client = TraderClient(
        "tcp://fake", "9999", "investor-1", "not-a-real-secret", auto_settlement_confirm=False
    )
    client.start()
    api = _FakeTraderApiFactory.instances[-1]
    provider = CtpDirectWriteProvider(_feed_with(client))
    try:
        provider.ensure_listening()
        captured = []
        client.add_instrument_status_listener(captured.append)
        api.spi.OnRtnInstrumentStatus(native("2"))
        assert (
            captured[-1]["source_connection_generation"],
            captured[-1]["connection_generation"],
        ) == (0, 1)
        assert provider.instrument_status_view(VENUE, SYMBOL).usable is True
        initial_scope = client.get_query_session_scope()
        assert initial_scope.source_tags[-1] == 0
        assert initial_scope.connection_generation == 1
        api.spi.OnFrontDisconnected(1001)
        assert provider.instrument_status_view(VENUE, SYMBOL).usable is False
        api.spi.OnFrontConnected()
        api.spi.OnRtnInstrumentStatus(native("6"))
        assert (
            captured[-1]["source_connection_generation"],
            captured[-1]["connection_generation"],
        ) == (0, 2)
        view = provider.instrument_status_view(VENUE, SYMBOL)
        assert (view.usable, view.effective_status_category) == (True, STATUS_CLOSED)
        reconnect_scope = client.get_query_session_scope()
        assert reconnect_scope.source_tags[-1] == 0
        assert reconnect_scope.connection_generation == 2
        assert reconnect_scope != initial_scope
    finally:
        provider.close()
        client.stop()


def test_direct_query_source_fallback_does_not_relabel_registered_scope(monkeypatch):
    _FakeTraderApiFactory.instances = []
    _FakeTraderApiFactory.block_join = False
    _FakeTraderApiFactory.register_spi_exception = None
    _FakeTraderApiFactory.init_exception = None
    monkeypatch.setattr(client_module, "_check_native_module", lambda: None)
    monkeypatch.setattr(client_module, "_flow_dir", lambda _name: "fake-flow")
    monkeypatch.setattr(client_module, "_register_ctp_native_api", lambda _api: None)
    monkeypatch.setattr(client_module, "CThostFtdcTraderApi", _FakeTraderApiFactory)
    client = TraderClient(
        "tcp://fake", "9999", "investor-1", "not-a-real-secret", auto_settlement_confirm=False
    )
    client.start()
    api = _FakeTraderApiFactory.instances[-1]
    try:
        tags = api.spi._callback_source_tags
        assert type(tags) is ingress.CtpTraderCallbackSourceTagsV2
        original = client.get_query_session_scope()
        assert original.source_tags[-1] == 0
        binding = capture_query_session(client, environment="simnow")
        assert validate_query_session(client, binding, now_monotonic=time.monotonic())
        api.spi._callback_source_tags = None
        fallback = client.get_query_session_scope()
        assert fallback.source_tags[-1] == 1
        assert fallback.source_tags != original.source_tags
        assert not validate_query_session(client, binding, now_monotonic=time.monotonic())
        api.spi._callback_source_tags = tags
        restored = client.get_query_session_scope()
        assert restored.source_tags == original.source_tags
        assert validate_query_session(client, binding, now_monotonic=time.monotonic())
    finally:
        client.stop()


@pytest.mark.parametrize(
    "payload",
    [
        _payload(generation=6),
        _payload(phase=ingress.CtpTraderCallbackPhase.PRE_LOGIN),
    ],
)
def test_wrong_source_cannot_become_current(payload):
    trader, provider = _provider()
    trader.status_listeners[0](payload)
    view = provider.instrument_status_view(VENUE, SYMBOL)
    assert view.usable is False


@pytest.mark.parametrize(
    "payload",
    [
        _payload(generation=6, sequence=999),
        _payload(phase=ingress.CtpTraderCallbackPhase.POISONED, sequence=999),
    ],
)
def test_wrong_source_does_not_overwrite_current_halt(payload):
    trader, provider = _provider()
    trader.status_listeners[0](_payload(status="6"))
    trader.status_listeners[0](payload)
    view = provider.instrument_status_view(VENUE, SYMBOL)
    assert (view.usable, view.effective_status_category) == (True, STATUS_CLOSED)


def test_corrupted_digest_cannot_overwrite_current_halt():
    trader, provider = _provider()
    trader.status_listeners[0](_payload(status="6"))
    wrong = _payload(sequence=2)
    wrong["digest"] = "0" * 64
    trader.status_listeners[0](wrong)
    view = provider.instrument_status_view(VENUE, SYMBOL)
    assert (view.usable, view.effective_status_category) == (True, STATUS_CLOSED)


def test_registration_generation_can_predate_current_connection():
    trader, provider = _provider()
    trader.status_listeners[0](_payload(tag_generation=6, status="6"))
    view = provider.instrument_status_view(VENUE, SYMBOL)
    assert (view.usable, view.effective_status_category) == (True, STATUS_CLOSED)


def _resign(payload):
    unsigned = dict(payload)
    unsigned.pop("digest")
    payload["digest"] = ingress._digest_payload(unsigned)


@pytest.mark.parametrize(
    "field,value",
    [
        ("source_instance_id", ""),
        ("native_client_epoch", "bad source"),
        ("native_api_generation", True),
        ("source_connection_generation", 8),
    ],
)
def test_invalid_source_tag_is_refused_even_with_recomputed_digest(field, value):
    trader, provider = _provider()
    trader.status_listeners[0](_payload(status="6"))
    wrong = _payload(sequence=2)
    wrong[field] = value
    _resign(wrong)
    trader.status_listeners[0](wrong)
    view = provider.instrument_status_view(VENUE, SYMBOL)
    assert (view.usable, view.effective_status_category) == (True, STATUS_CLOSED)


@pytest.mark.parametrize(
    "mutation",
    [
        "missing_named",
        "duplicate_named",
        "wrong_name",
        "missing_pointer_flag",
        "wrong_slot",
        "duplicate_field",
        "unknown_field",
        "invalid_field_type",
    ],
)
def test_malformed_snapshot_is_refused_even_when_digest_is_recomputed(mutation):
    trader, provider = _provider()
    trader.status_listeners[0](_payload(status="6"))
    wrong = _payload(sequence=2)
    if mutation == "missing_named":
        wrong["named_args"] = []
    elif mutation == "duplicate_named":
        wrong["named_args"].append(dict(wrong["named_args"][0]))
    elif mutation == "wrong_name":
        wrong["named_args"][0]["name"] = "wrong"
    elif mutation == "missing_pointer_flag":
        del wrong["named_args"][0]["pointer_is_null"]
    elif mutation == "wrong_slot":
        wrong["flattened_fields"][0]["argument_slot"] = 1
    elif mutation == "duplicate_field":
        wrong["flattened_fields"].append(dict(wrong["flattened_fields"][0]))
    elif mutation == "unknown_field":
        extra = dict(wrong["flattened_fields"][0])
        extra["field_name"] = "UnknownField"
        wrong["flattened_fields"].append(extra)
    else:
        field = next(
            item for item in wrong["flattened_fields"] if item["field_name"] == "InstrumentStatus"
        )
        field["value"] = True
    _resign(wrong)
    trader.status_listeners[0](wrong)
    view = provider.instrument_status_view(VENUE, SYMBOL)
    assert (view.usable, view.effective_status_category) == (True, STATUS_CLOSED)


def test_external_payload_mutation_cannot_change_the_detached_projection(monkeypatch):
    target = InstrumentStatusIngressBridge(session_generation="7", trading_day=DAY)
    payload = _payload(status="2")
    original = status_ingress._record_from_snapshot

    def mutate_original(snapshot):
        field = next(
            item for item in payload["flattened_fields"] if item["field_name"] == "InstrumentStatus"
        )
        field["value"] = "6"
        _resign(payload)
        return original(snapshot)

    monkeypatch.setattr(status_ingress, "_record_from_snapshot", mutate_original)
    assert target.observe_payload(payload).applied
    assert target.view(VENUE, SYMBOL).effective_status_category == "continuous"
    assert (
        next(
            item for item in payload["flattened_fields"] if item["field_name"] == "InstrumentStatus"
        )["value"]
        == "6"
    )


@pytest.mark.parametrize(
    "field",
    [
        "source_instance_id",
        "native_client_epoch",
        "native_api_source_id",
        "native_spi_source_id",
        "native_api_generation",
        "source_connection_generation",
    ],
)
def test_missing_source_tag_cannot_overwrite_current_halt(field):
    trader, provider = _provider()
    trader.status_listeners[0](_payload(status="6"))
    wrong = _payload(sequence=2)
    del wrong[field]
    unsigned = dict(wrong)
    unsigned.pop("digest")
    wrong["digest"] = ingress._digest_payload(unsigned)
    trader.status_listeners[0](wrong)
    view = provider.instrument_status_view(VENUE, SYMBOL)
    assert (view.usable, view.effective_status_category) == (True, STATUS_CLOSED)


def test_delayed_prior_trader_cannot_override_halt_at_same_generation():
    trader, provider = _provider()
    old_listener = trader.status_listeners[0]
    old_listener(_payload(status="2"))
    other = FakeTrader()
    provider._feed._trader = other
    assert provider.instrument_status_view(VENUE, SYMBOL).usable is False
    provider.ensure_listening()
    assert trader.status_listeners == []
    other.status_listeners[0](_payload(status="6"))
    old_listener(_payload(status="2", sequence=999))
    view = provider.instrument_status_view(VENUE, SYMBOL)
    assert (view.usable, view.effective_status_category) == (True, STATUS_CLOSED)
    provider.close()
    assert other.status_listeners == []


@pytest.mark.parametrize(
    "field,value",
    [
        ("connected", False),
        ("read_only_ready", False),
        ("trading_day", "invalid"),
        ("connection_generation", True),
    ],
)
def test_unusable_current_session_stales_existing_view(field, value):
    trader, provider = _provider()
    trader.status_listeners[0](_payload(status="6"))
    trader.session_state[field] = value
    view = provider.instrument_status_view(VENUE, SYMBOL)
    assert view.usable is False


def test_delayed_old_scope_cannot_replace_new_halt(monkeypatch):
    trader, provider = _provider()
    sampled = threading.Event()
    release = threading.Event()
    original = provider._instrument_status_session_facts

    def delayed_facts():
        facts = original()
        if threading.current_thread().name == "old-status" and not sampled.is_set():
            sampled.set()
            assert release.wait(2)
        return facts

    monkeypatch.setattr(provider, "_instrument_status_session_facts", delayed_facts)
    worker = threading.Thread(
        target=trader.status_listeners[0], args=(_payload(generation=7),), name="old-status"
    )
    worker.start()
    assert sampled.wait(2)
    trader.session_state["connection_generation"] = 8
    new_worker = threading.Thread(
        target=trader.status_listeners[0], args=(_payload(generation=8, status="6"),)
    )
    new_worker.start()
    release.set()
    worker.join(2)
    new_worker.join(2)
    assert not worker.is_alive()
    assert not new_worker.is_alive()
    view = provider.instrument_status_view(VENUE, SYMBOL)
    assert (view.session_generation, view.trading_day, view.effective_status_category) == (
        "8",
        DAY,
        STATUS_CLOSED,
    )


def test_late_old_callback_recheck_cannot_stale_new_halt(monkeypatch):
    trader, provider = _provider()
    sampled = threading.Event()
    release = threading.Event()
    original = provider._instrument_status_session_facts
    calls = 0

    def delayed_recheck():
        nonlocal calls
        if threading.current_thread().name == "old-status":
            calls += 1
            if calls == 2:
                sampled.set()
                assert release.wait(2)
                return None
        return original()

    monkeypatch.setattr(provider, "_instrument_status_session_facts", delayed_recheck)
    old = threading.Thread(target=trader.status_listeners[0], args=(_payload(),), name="old-status")
    old.start()
    assert sampled.wait(2)
    trader.status_listeners[0](_payload(status="6", sequence=2))
    release.set()
    old.join(2)
    assert not old.is_alive()
    view = provider.instrument_status_view(VENUE, SYMBOL)
    assert (view.usable, view.effective_status_category) == (True, STATUS_CLOSED)


@pytest.mark.parametrize("candidate_sequence,expected_usable", [(1, True), (3, False)])
def test_contended_callback_only_invalidates_if_newer(
    monkeypatch, candidate_sequence, expected_usable
):
    trader, provider = _provider()
    trader.status_listeners[0](_payload(status="6", sequence=2))
    original = provider._instrument_status_session_facts

    def conflicting_facts():
        facts = original()
        with provider._instrument_status_lock:
            provider._instrument_status_version += 1
        return facts

    monkeypatch.setattr(provider, "_instrument_status_session_facts", conflicting_facts)
    trader.status_listeners[0](_payload(status="2", sequence=candidate_sequence))
    monkeypatch.setattr(provider, "_instrument_status_session_facts", original)
    view = provider.instrument_status_view(VENUE, SYMBOL)
    assert view.usable is expected_usable
    if expected_usable:
        assert view.effective_status_category == STATUS_CLOSED
    else:
        trader.status_listeners[0](_payload(status="6", sequence=4))
        view = provider.instrument_status_view(VENUE, SYMBOL)
        assert (view.usable, view.reason_code) == (
            False,
            "ctp_instrument_status_source_unavailable",
        )


def test_managed_native_lock_callback_does_not_deadlock_concurrent_view():
    native_lock = threading.RLock()
    sampling = threading.Event()

    class LockedTrader(FakeTrader):
        def get_session_state(self):
            if threading.current_thread().name == "status-view":
                sampling.set()
            with native_lock:
                return super().get_session_state()

    trader = LockedTrader()
    provider = CtpDirectWriteProvider(_feed_with(trader))
    provider.ensure_listening()
    finished = threading.Event()
    view_thread = None

    def managed_callback():
        nonlocal view_thread
        with native_lock:
            view_thread = threading.Thread(
                target=provider.instrument_status_view,
                args=(VENUE, SYMBOL),
                name="status-view",
                daemon=True,
            )
            view_thread.start()
            assert sampling.wait(2)
            trader.status_listeners[0](_payload(status="6"))
        finished.set()

    callback = threading.Thread(target=managed_callback, daemon=True)
    callback.start()
    assert finished.wait(2), "native and provider status locks deadlocked"
    callback.join(2)
    view_thread.join(2)
    assert not view_thread.is_alive()
    view = provider.instrument_status_view(VENUE, SYMBOL)
    assert (view.usable, view.effective_status_category) == (True, STATUS_CLOSED)
