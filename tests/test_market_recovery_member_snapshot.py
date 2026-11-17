"""Owner-local recovery member snapshots, using synthetic MD callbacks only."""

import copy
import pickle
import threading
import time
from dataclasses import FrozenInstanceError, replace
from types import SimpleNamespace

import pytest
from test_market_provider import (
    _ENTITLEMENT_REF,
    _SOURCE_REF,
    _drive_until,
    _instrument_key,
    _key,
    _stream_factory,
    _SyntheticMdClient,
)
from test_market_recovery_observation import _pending, _provider

from bt_api_ctp.ctp.client import CtpNativeStopReceipt, MdClient
from bt_api_ctp.market_provider import (
    BoundedCtpQuoteV2Queue,
    CtpQuoteV2MarketProvider,
    CtpQuoteV2MarketProviderError,
)
from bt_api_ctp.md_identity import MdIdentityObservation


@pytest.fixture(autouse=True)
def _synthetic_login_identity(monkeypatch):
    """Model the native client's stable identity and its pre-callback revocation."""
    original_disconnect = _SyntheticMdClient.emit_disconnect

    def identity(client):
        if getattr(client, "_snapshot_inactive", False):
            return None
        if not hasattr(client, "_snapshot_identity"):
            client._snapshot_identity = MdIdentityObservation(
                "tcp://127.0.0.1:1",
                "synthetic-broker",
                "synthetic-user",
                client.connection_generation,
                1,
                "20261020",
                True,
            )
        return client._snapshot_identity

    def disconnect(client, reason=0):
        client._snapshot_inactive = True
        return original_disconnect(client, reason)

    monkeypatch.setattr(_SyntheticMdClient, "active_md_identity", property(identity), raising=False)
    monkeypatch.setattr(_SyntheticMdClient, "emit_disconnect", disconnect)


def test_current_members_are_detached_sorted_and_emit_no_watermark(monkeypatch):
    provider, clients, _, _ = _provider(monkeypatch)
    first = provider.subscribe(_key(), generation="g1")
    second = provider.subscribe(_instrument_key("IH2612"), generation="g2")
    buffer = provider._recovery_observation
    before = (buffer._sequence, tuple(buffer._records), buffer._last_time)
    snapshot = provider.capture_recovery_member_snapshot()
    assert snapshot.source_epoch == buffer._source_epoch
    assert snapshot.sequence == 0
    assert snapshot.md_generation == clients[0].connection_generation
    assert {(m.key, m.generation) for m in snapshot.members} == {
        (first.key, "g1"),
        (second.key, "g2"),
    }
    assert snapshot.members == tuple(
        sorted(snapshot.members, key=lambda m: (m.key.stream_id, m.generation))
    )
    assert (buffer._sequence, tuple(buffer._records), buffer._last_time) == before
    provider.assert_recovery_member_snapshot_current(snapshot)
    with pytest.raises(FrozenInstanceError):
        snapshot.sequence = 10
    for operation in (copy.copy, copy.deepcopy, pickle.dumps):
        with pytest.raises(TypeError):
            operation(snapshot)
    with pytest.raises(CtpQuoteV2MarketProviderError, match="RECOVERY_OBSERVATION_INVALID"):
        provider.assert_recovery_member_snapshot_current(replace(snapshot))
    assert provider.close()


def test_disabled_empty_and_wrong_owner_fail_closed(monkeypatch):
    disabled, _, _, _ = _provider(monkeypatch, 0)
    with pytest.raises(CtpQuoteV2MarketProviderError, match="RECOVERY_OBSERVATION_DISABLED"):
        disabled.capture_recovery_member_snapshot()
    empty, _, _, _ = _provider(monkeypatch)
    with pytest.raises(CtpQuoteV2MarketProviderError, match="RECOVERY_OBSERVATION_UNAVAILABLE"):
        empty.capture_recovery_member_snapshot()
    provider, _, _, _ = _provider(monkeypatch)
    provider.subscribe(_key(), generation="g1")
    snapshot = provider.capture_recovery_member_snapshot()
    failures = []

    def wrong_owner():
        for call in (
            provider.capture_recovery_member_snapshot,
            lambda: provider.assert_recovery_member_snapshot_current(snapshot),
        ):
            try:
                call()
            except CtpQuoteV2MarketProviderError as exc:
                failures.append(exc.code)

    worker = threading.Thread(target=wrong_owner)
    worker.start()
    worker.join(2)
    assert not worker.is_alive()
    assert failures == ["CTP_MARKET_OWNER_THREAD_REQUIRED"] * 2
    assert disabled.close() and empty.close() and provider.close()


def test_membership_add_remove_and_same_key_generation_aba_invalidate(monkeypatch):
    provider, _, _, _ = _provider(monkeypatch)
    first = provider.subscribe(_key(), generation="g1")
    second = provider.subscribe(_instrument_key("IH2612"), generation="g2")
    original = provider.capture_recovery_member_snapshot()
    assert provider.unsubscribe(second)
    with pytest.raises(CtpQuoteV2MarketProviderError, match="RECOVERY_OBSERVATION_UNAVAILABLE"):
        provider.assert_recovery_member_snapshot_current(original)
    replacement = provider.subscribe(_instrument_key("IH2612"), generation="g2")
    assert replacement is not second
    current = provider.capture_recovery_member_snapshot()
    assert current.members == original.members
    with pytest.raises(CtpQuoteV2MarketProviderError, match="RECOVERY_OBSERVATION_UNAVAILABLE"):
        provider.assert_recovery_member_snapshot_current(original)
    provider.assert_recovery_member_snapshot_current(current)
    assert first is not replacement and provider.close()


def test_another_provider_cannot_adopt_an_issued_snapshot(monkeypatch):
    original, _, _, _ = _provider(monkeypatch)
    original.subscribe(_key(), generation="g1")
    snapshot = original.capture_recovery_member_snapshot()
    other, _, _, _ = _provider(monkeypatch)
    other.subscribe(_key(), generation="g1")
    with pytest.raises(CtpQuoteV2MarketProviderError, match="RECOVERY_OBSERVATION_INVALID"):
        other.assert_recovery_member_snapshot_current(snapshot)
    assert original.close() and other.close()


def test_snapshot_tampering_cannot_adopt_changed_sequence(monkeypatch):
    provider, _, _, _ = _provider(monkeypatch)
    provider.subscribe(_key(), generation="g1")
    snapshot = provider.capture_recovery_member_snapshot()
    provider._recovery_observation._sequence += 1
    object.__setattr__(snapshot, "sequence", provider._recovery_observation._sequence)
    with pytest.raises(CtpQuoteV2MarketProviderError):
        provider.assert_recovery_member_snapshot_current(snapshot)
    assert provider.close()


@pytest.mark.parametrize("field,value", [
    ("front", "tcp://127.0.0.1:99"),
    ("broker_id", "other-broker"),
    ("user_id", "other-user"),
    ("connection_generation", 8),
    ("request_id", 2),
    ("trading_day", "20261021"),
    ("authenticated", False),
])
def test_md_identity_field_tampering_invalidates_issued_snapshot(monkeypatch, field, value):
    provider, _, _, _ = _provider(monkeypatch)
    provider.subscribe(_key(), generation="g1")
    snapshot = provider.capture_recovery_member_snapshot()
    identity = snapshot._md_identity
    original = getattr(identity, field)
    object.__setattr__(identity, field, value)
    with pytest.raises(CtpQuoteV2MarketProviderError):
        provider.assert_recovery_member_snapshot_current(snapshot)
    object.__setattr__(identity, field, original)
    assert provider.close()


def test_identity_mutation_between_capture_samples_refuses_issue(monkeypatch):
    provider, _, _, _ = _provider(monkeypatch)
    provider.subscribe(_key(), generation="g1")
    original = provider._recovery_member_inputs_locked
    calls = []

    def mutate_on_second_sample(stream, buffer):
        calls.append(None)
        if len(calls) == 2:
            object.__setattr__(provider._native_client.active_md_identity, "trading_day", "20261021")
        return original(stream, buffer)

    monkeypatch.setattr(provider, "_recovery_member_inputs_locked", mutate_on_second_sample)
    with pytest.raises(CtpQuoteV2MarketProviderError):
        provider.capture_recovery_member_snapshot()
    assert len(calls) == 2 and not provider._recovery_member_snapshots
    assert provider.close()


def test_identity_mutation_after_first_sample_refuses_issue(monkeypatch):
    provider, _, _, _ = _provider(monkeypatch)
    provider.subscribe(_key(), generation="g1")
    original = provider._recovery_member_inputs_locked
    calls = []

    def mutate_after_first_sample(stream, buffer):
        result = original(stream, buffer)
        calls.append(None)
        if len(calls) == 1:
            object.__setattr__(result[7], "trading_day", "20261021")
        return result

    monkeypatch.setattr(provider, "_recovery_member_inputs_locked", mutate_after_first_sample)
    with pytest.raises(CtpQuoteV2MarketProviderError):
        provider.capture_recovery_member_snapshot()
    assert len(calls) == 2 and not provider._recovery_member_snapshots
    assert provider.close()


def test_scope_clock_fields_cannot_be_coherently_changed_after_issue(monkeypatch):
    provider, _, _, _ = _provider(monkeypatch)
    provider.subscribe(_key(), generation="g1")
    snapshot = provider.capture_recovery_member_snapshot()
    scope = snapshot._scope
    original_domain, original_session = scope.clock_domain_id, scope.clock_source_session_id
    new_session = "f" * 32 if original_session != "f" * 32 else "e" * 32
    new_domain = original_domain.removesuffix(original_session) + new_session
    object.__setattr__(scope, "clock_source_session_id", new_session)
    object.__setattr__(scope, "clock_domain_id", new_domain)
    assert provider._current_source_scope(provider._stream) is scope
    with pytest.raises(CtpQuoteV2MarketProviderError):
        provider.assert_recovery_member_snapshot_current(snapshot)
    object.__setattr__(scope, "clock_source_session_id", original_session)
    object.__setattr__(scope, "clock_domain_id", original_domain)
    assert provider.close()


def test_scope_clock_change_between_capture_samples_refuses_issue(monkeypatch):
    provider, _, _, _ = _provider(monkeypatch)
    provider.subscribe(_key(), generation="g1")
    original = provider._recovery_member_inputs_locked
    calls = []

    def mutate_on_second_sample(stream, buffer):
        calls.append(None)
        if len(calls) == 2:
            object.__setattr__(provider._source_scope, "clock_source", "other-monotonic")
        return original(stream, buffer)

    monkeypatch.setattr(provider, "_recovery_member_inputs_locked", mutate_on_second_sample)
    with pytest.raises(CtpQuoteV2MarketProviderError):
        provider.capture_recovery_member_snapshot()
    assert len(calls) == 2 and not provider._recovery_member_snapshots
    assert provider.close()


@pytest.mark.parametrize("field", [
    "md_front", "broker_id", "user_id", "password", "ctp_env_name",
    "ctp_env_profile", "ctp_env_readiness", "asset_type",
])
def test_source_binding_drift_refuses_capture_and_assert(monkeypatch, field):
    provider, _, _, _ = _provider(monkeypatch)
    provider.subscribe(_key(), generation="g1")
    snapshot = provider.capture_recovery_member_snapshot()
    stream = provider._stream
    with monkeypatch.context() as patch:
        patch.setattr(stream, field, object() if field == "password" else "changed")
        assert not stream.quote_v2_service_binding_matches()
        with pytest.raises(CtpQuoteV2MarketProviderError):
            provider.assert_recovery_member_snapshot_current(snapshot)
        with pytest.raises(CtpQuoteV2MarketProviderError):
            provider.capture_recovery_member_snapshot()
    assert provider.close()


@pytest.mark.parametrize("change", ["member", "key", "instrument", "states", "revision", "bool"])
def test_issued_snapshot_and_nested_fields_cannot_be_modified(monkeypatch, change):
    provider, _, _, _ = _provider(monkeypatch)
    provider.subscribe(_key(), generation="g1")
    snapshot = provider.capture_recovery_member_snapshot()
    if change == "member":
        object.__setattr__(snapshot.members[0], "generation", "g2")
    elif change == "key":
        object.__setattr__(snapshot.members[0].key, "source_ref", "changed")
    elif change == "instrument":
        object.__setattr__(snapshot.members[0].key.instrument, "native_symbol", "changed")
    elif change == "states":
        object.__setattr__(snapshot, "_states", ((object(), object()),))
    elif change == "revision":
        provider._recovery_member_revision += 1
        object.__setattr__(snapshot, "_revision", provider._recovery_member_revision)
    else:
        object.__setattr__(snapshot, "sequence", False)
    with pytest.raises(CtpQuoteV2MarketProviderError):
        provider.assert_recovery_member_snapshot_current(snapshot)
    assert provider.close()


def test_full_member_limit_rejects_without_truncation(monkeypatch):
    provider, _, _, _ = _provider(monkeypatch)
    provider.subscribe(_key(), generation="g1")
    stream = provider._stream
    request_id = next(iter(stream._quote_v2_service_completed_request_ids))

    def add_synthetic_member(index):
        key = _instrument_key(f"IC{index:04d}")
        symbol = key.instrument.native_symbol
        queue = BoundedCtpQuoteV2Queue()
        stream._quote_v2_subscription_metadata[symbol] = {"topic": "tick", "symbol": symbol}
        stream._quote_v2_service_required_symbols.add(symbol)
        stream._quote_v2_service_acknowledged_symbols.add(symbol)
        stream._quote_v2_service_symbol_request_ids[symbol] = request_id
        provider._fanout.register(symbol, queue)
        return provider._register_handle(key, "g1", queue, stream, provider._source_scope)

    for index in range(1023):
        add_synthetic_member(index)
    snapshot = provider.capture_recovery_member_snapshot()
    assert len(snapshot.members) == 1024
    provider.assert_recovery_member_snapshot_current(snapshot)
    add_synthetic_member(1023)
    with pytest.raises(CtpQuoteV2MarketProviderError, match="RECOVERY_OBSERVATION_UNAVAILABLE"):
        provider.capture_recovery_member_snapshot()
    with pytest.raises(CtpQuoteV2MarketProviderError, match="RECOVERY_OBSERVATION_UNAVAILABLE"):
        provider.assert_recovery_member_snapshot_current(snapshot)
    assert provider.close()


def test_changed_sequence_during_capture_is_refused(monkeypatch):
    provider, _, _, _ = _provider(monkeypatch)
    provider.subscribe(_key(), generation="g1")
    buffer = provider._recovery_observation
    original = provider._recovery_member_inputs_locked
    calls = []

    def change_after_sample(stream, current):
        result = original(stream, current)
        calls.append(None)
        if len(calls) == 1:
            buffer._sequence += 1
        return result

    monkeypatch.setattr(provider, "_recovery_member_inputs_locked", change_after_sample)
    with pytest.raises(CtpQuoteV2MarketProviderError, match="RECOVERY_OBSERVATION_UNAVAILABLE"):
        provider.capture_recovery_member_snapshot()
    assert len(calls) == 2 and not provider._recovery_member_snapshots
    assert provider.close()


def test_identity_lost_during_assert_is_refused(monkeypatch):
    provider, clients, _, _ = _provider(monkeypatch)
    provider.subscribe(_key(), generation="g1")
    snapshot = provider.capture_recovery_member_snapshot()
    original = clients[0].active_md_identity
    calls = []

    def identity(_client):
        calls.append(None)
        return original if len(calls) == 1 else None

    with monkeypatch.context() as patch:
        patch.setattr(_SyntheticMdClient, "active_md_identity", property(identity))
        with pytest.raises(CtpQuoteV2MarketProviderError, match="RECOVERY_OBSERVATION_UNAVAILABLE"):
            provider.assert_recovery_member_snapshot_current(snapshot)
    assert len(calls) == 2
    assert provider.close()


@pytest.mark.parametrize(
    "change",
    [
        "scope",
        "client",
        "md_identity",
        "stream",
        "buffer",
        "sequence",
        "poison",
        "episode",
        "required",
    ],
)
def test_replaced_sources_and_unhealthy_state_refuse(monkeypatch, change):
    provider, _, _, _ = _provider(monkeypatch)
    provider.subscribe(_key(), generation="g1")
    snapshot = provider.capture_recovery_member_snapshot()
    stream = provider._stream
    buffer = provider._recovery_observation
    with monkeypatch.context() as patch:
        if change == "scope":
            patch.setattr(provider, "_source_scope", replace(provider._source_scope))
        elif change == "client":
            patch.setattr(provider, "_native_client", object())
        elif change == "md_identity":
            client = provider._native_client
            patch.setattr(client, "_snapshot_identity", replace(client.active_md_identity))
        elif change == "stream":
            patch.setattr(provider, "_stream", object())
        elif change == "buffer":
            patch.setattr(provider, "_recovery_observation", object())
        elif change == "sequence":
            patch.setattr(buffer, "_sequence", buffer._sequence + 1)
        elif change == "poison":
            patch.setattr(buffer, "_poisoned", True)
        elif change == "episode":
            patch.setattr(buffer, "_episode", {})
        else:
            patch.setattr(stream, "_quote_v2_service_required_symbols", set())
        with pytest.raises(CtpQuoteV2MarketProviderError, match="RECOVERY_OBSERVATION_UNAVAILABLE"):
            provider.assert_recovery_member_snapshot_current(snapshot)
    assert provider.close()


def test_disconnect_recovery_and_close_fence_snapshot(monkeypatch):
    provider, clients, _, _ = _provider(monkeypatch)
    handle = provider.subscribe(_key(), generation="g1")
    snapshot = provider.capture_recovery_member_snapshot()
    clients[0].emit_disconnect()
    with pytest.raises(CtpQuoteV2MarketProviderError, match="RECOVERY_OBSERVATION_UNAVAILABLE"):
        provider.assert_recovery_member_snapshot_current(snapshot)
    provider.maintain()
    with pytest.raises(CtpQuoteV2MarketProviderError, match="RECOVERY_OBSERVATION_UNAVAILABLE"):
        provider.capture_recovery_member_snapshot()
    _drive_until(provider, lambda: provider.source_epoch_reset_required(handle))
    assert provider.acknowledge_source_epoch_reset(handle, epoch="e2")
    assert provider.close()
    # Separate fixture settles recovery for deterministic clean shutdown.
    pending, _, first, second, _ = _pending(monkeypatch)
    assert pending.acknowledge_source_epoch_reset(first, epoch="e2")
    assert pending.acknowledge_source_epoch_reset(second, epoch="e2")
    fresh = pending.capture_recovery_member_snapshot()
    assert fresh.md_generation == pending._source_scope.connection_generation
    assert pending.close()
    with pytest.raises(CtpQuoteV2MarketProviderError):
        pending.assert_recovery_member_snapshot_current(fresh)


def test_disconnect_waiting_on_callback_lock_refuses_capture(monkeypatch):
    provider, clients, _, _ = _provider(monkeypatch)
    handle = provider.subscribe(_key(), generation="g1")
    entered = threading.Event()
    original = provider._recovery_member_inputs_locked
    calls = []

    def start_disconnect(stream, buffer):
        values = original(stream, buffer)
        calls.append(None)
        if len(calls) == 1:

            def disconnect():
                clients[0]._snapshot_inactive = True
                entered.set()
                clients[0].emit_disconnect()

            worker = threading.Thread(target=disconnect)
            worker.start()
            workers.append(worker)
            assert entered.wait(2)
        return values

    workers = []
    monkeypatch.setattr(provider, "_recovery_member_inputs_locked", start_disconnect)
    with pytest.raises(CtpQuoteV2MarketProviderError, match="RECOVERY_OBSERVATION_UNAVAILABLE"):
        provider.capture_recovery_member_snapshot()
    assert len(calls) == 1 and not provider._recovery_member_snapshots
    workers[0].join(2)
    assert not workers[0].is_alive()
    monkeypatch.setattr(provider, "_recovery_member_inputs_locked", original)
    _drive_until(provider, lambda: provider.source_epoch_reset_required(handle))
    assert provider.acknowledge_source_epoch_reset(handle, epoch="e2")
    assert provider.close()


def test_real_md_client_disconnect_state_before_stream_callback_is_not_healthy(monkeypatch):
    from test_instrument_metadata import _SyntheticLoginField

    from bt_api_ctp.ctp import client as client_module
    from bt_api_ctp.feeds import live_ctp_feed

    class FakeApi:
        def __init__(self):
            self.request_id = None

        def ReqUserLogin(self, _field, request_id):
            self.request_id = request_id
            return 0

        def SubscribeMarketData(self, _symbols):
            return 0

    def start(client, *, block=False, join_observer=True):
        assert block is False and join_observer is False
        api = FakeApi()
        client._api = api
        spi = client._create_md_spi(api)
        client._spi = spi
        spi.OnFrontConnected()
        spi.OnRspUserLogin(
            SimpleNamespace(
                BrokerID=client._bound_broker_id,
                UserID=client._bound_user_id,
                TradingDay="20261020",
            ),
            SimpleNamespace(ErrorID=0, ErrorMsg=""),
            api.request_id,
            True,
        )
        symbol = client._pending_instruments[0]
        spi.OnRspSubMarketData(
            SimpleNamespace(InstrumentID=symbol), SimpleNamespace(ErrorID=0), 1, True
        )
        spi.OnRtnDepthMarketData(
            SimpleNamespace(
                InstrumentID=symbol,
                ExchangeID="CFFEX",
                LastPrice=4001.0,
                Volume=1,
                TradingDay="20261020",
                ActionDay="20261020",
                UpdateTime="09:30:01",
                UpdateMillisec=0,
            )
        )

    monkeypatch.setattr(client_module, "CThostFtdcReqUserLoginField", _SyntheticLoginField)
    monkeypatch.setattr(live_ctp_feed.ctp_client, "MdClient", MdClient)
    monkeypatch.setattr(MdClient, "start", start)
    monkeypatch.setattr(MdClient, "stop", lambda _client: None)
    monkeypatch.setattr(
        MdClient,
        "stop_and_wait",
        lambda client, **_: CtpNativeStopReceipt(
            client.connection_generation, False, True, True, False, False
        ),
    )
    provider = CtpQuoteV2MarketProvider(
        stream_factory=_stream_factory,
        source_ref=_SOURCE_REF,
        entitlement_ref=_ENTITLEMENT_REF,
        connect_timeout_seconds=1,
        recovery_observation_capacity=16,
    )
    provider.subscribe(_key(), generation="g1")
    stream = provider._stream
    client = provider._native_client
    snapshot = provider.capture_recovery_member_snapshot()
    with stream._md_callback_lock:
        worker = threading.Thread(target=lambda: client._spi.OnFrontDisconnected(1001))
        worker.start()
        deadline = time.monotonic() + 2
        while client.active_md_identity is not None and time.monotonic() < deadline:
            time.sleep(0.001)
        assert client.active_md_identity is None
        # Native state is already disconnected; stream callback is still waiting.
        with pytest.raises(CtpQuoteV2MarketProviderError, match="RECOVERY_OBSERVATION_UNAVAILABLE"):
            provider.capture_recovery_member_snapshot()
        with pytest.raises(CtpQuoteV2MarketProviderError, match="RECOVERY_OBSERVATION_UNAVAILABLE"):
            provider.assert_recovery_member_snapshot_current(snapshot)
    worker.join(2)
    assert not worker.is_alive()
    assert provider.close()
