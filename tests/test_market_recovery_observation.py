"""Local recovery observations using the real provider and synthetic MD callbacks."""

from __future__ import annotations

import threading

import pytest
from test_market_provider import _drive_until, _failing_construction_provider, _instrument_key, _key

from bt_api_ctp import market_provider as market_provider_module
from bt_api_ctp import recovery_observation as observation_module
from bt_api_ctp.market_provider import CtpQuoteV2MarketProviderError


def _provider(monkeypatch, capacity=16, **options):
    return _failing_construction_provider(
        monkeypatch, recovery_observation_capacity=capacity, **options
    )


def _pending(monkeypatch, capacity=16, **options):
    provider, clients, _, fail = _provider(monkeypatch, capacity, **options)
    first = provider.subscribe(_key(), generation="g1")
    second = provider.subscribe(_instrument_key("IH2612"), generation="g1")
    clients[0].emit_disconnect()
    _drive_until(provider, lambda: provider.source_epoch_reset_required(first))
    return provider, clients, first, second, fail


def _read(provider, after=0, limit=100):
    return provider.read_recovery_observations(after_sequence=after, limit=limit)


def test_retry_reentry_watermark_and_completion_keep_original_episode(monkeypatch):
    monkeypatch.setattr(market_provider_module, "_SOURCE_RECOVERY_RETRY_DELAYS_SECONDS", (0.01,))
    provider, clients, _, fail = _provider(monkeypatch)
    first = provider.subscribe(_key(), generation="g1")
    second = provider.subscribe(_instrument_key("IH2612"), generation="g1")
    fail[0] = True
    clients[0].emit_disconnect()
    _drive_until(
        provider,
        lambda: (
            provider._source_recovery is not None
            and provider._source_recovery.state == "retry_wait"
        ),
    )
    begin = _read(provider).observations[0]
    assert begin.reason == "BEGIN"
    assert len(begin.members) == 2
    fail[0] = False
    _drive_until(provider, lambda: provider.source_epoch_reset_required(first))
    clients[-1].emit_disconnect()
    provider.maintain()
    assert provider._source_recovery is not None
    intermediate = _read(provider)
    assert all(item.state != "RECOVERED" for item in intermediate.observations)
    _drive_until(provider, lambda: provider.source_epoch_reset_required(first))
    assert provider.acknowledge_source_epoch_reset(first, epoch="e2")
    assert provider.acknowledge_source_epoch_reset(second, epoch="e2")
    page = _read(provider)
    assert page.observations[-1].state == "RECOVERED"
    assert all(item.episode_id == begin.episode_id for item in page.observations)
    assert all(
        item.coverage_started_monotonic_ns == begin.observed_monotonic_ns
        for item in page.observations
    )
    assert provider.close()


@pytest.mark.parametrize("action", ["release", "close"])
def test_changed_members_or_close_invalidates_original_episode(monkeypatch, action):
    provider, _, first, second, _ = _pending(monkeypatch, max_parked_symbols=0)
    assert provider.acknowledge_source_epoch_reset(first, epoch="e2")
    if action == "release":
        assert provider.unsubscribe(second)
    else:
        assert provider.close()
    records = _read(provider).observations
    assert records[-1].state == "UNKNOWN"
    assert records[-1].reason == ("MEMBERS_CHANGED" if action == "release" else "CLOSED")
    assert records[-1].coverage_started_monotonic_ns is None
    assert not any(record.state == "RECOVERED" for record in records)
    if action == "release":
        assert provider._source_recovery is None
        assert provider.close()


def test_failure_and_failure_before_begin_are_distinct(monkeypatch):
    provider, _, first, second, _ = _pending(monkeypatch)
    provider._stream._fail_closed_quote_v2_subscription()
    assert not provider.acknowledge_source_epoch_reset(second, epoch="e2")
    assert _read(provider).observations[-1].state == "FAILED"
    assert provider.close()
    provider, clients, _, _ = _provider(monkeypatch)
    provider.subscribe(_key(), generation="g1")
    provider._source_scope = None
    clients[0].emit_disconnect()
    provider.maintain()
    unknown = _read(provider).observations[-1]
    assert (unknown.state, unknown.reason) == ("UNKNOWN", "BEGIN_FAILED")
    assert provider.close()


def test_bounded_gap_cursor_replay_disabled_owner_and_isolation(monkeypatch):
    provider, _, first, _, _ = _pending(monkeypatch, capacity=2)
    first_page = _read(provider, limit=1)
    assert len(first_page.observations) == 1
    page = _read(provider, after=0)
    assert page.gap and page.first_available_sequence > 1
    repeated = _read(provider, after=page.last_sequence)
    assert repeated.observations and repeated.observations[0].sequence > page.last_sequence
    for cursor, limit in [(True, 1), (0, True), (0, 257), (1 << 63, 1)]:
        with pytest.raises(CtpQuoteV2MarketProviderError, match="RECOVERY_OBSERVATION_INVALID"):
            _read(provider, cursor, limit)
    errors = []

    def wrong_owner():
        try:
            _read(provider)
        except CtpQuoteV2MarketProviderError as exc:
            errors.append(exc.code)

    thread = threading.Thread(target=wrong_owner)
    thread.start()
    thread.join(timeout=1)
    assert errors == ["CTP_MARKET_OWNER_THREAD_REQUIRED"]
    other, _, _, _ = _provider(monkeypatch, 2)
    assert _read(other).source_epoch != page.source_epoch
    disabled, _, _, _ = _provider(monkeypatch, 0)
    with pytest.raises(CtpQuoteV2MarketProviderError, match="RECOVERY_OBSERVATION_DISABLED"):
        _read(disabled)
    assert provider.close() and other.close() and disabled.close()


def test_observation_clock_failure_does_not_change_recovery(monkeypatch):
    provider, _, first, second, _ = _pending(monkeypatch)

    def broken_clock():
        raise RuntimeError("synthetic clock failure")

    with monkeypatch.context() as context:
        context.setattr(observation_module.time, "monotonic_ns", broken_clock)
        assert provider.acknowledge_source_epoch_reset(first, epoch="e2")
        assert provider.acknowledge_source_epoch_reset(second, epoch="e2")
    assert provider._source_recovery is None
    with pytest.raises(CtpQuoteV2MarketProviderError, match="RECOVERY_OBSERVATION_UNAVAILABLE"):
        _read(provider)
    assert provider.close()


def test_reentry_exception_marks_episode_unknown_without_recovered(monkeypatch):
    provider, clients, first, _, _ = _pending(monkeypatch)
    clients[-1].emit_disconnect()

    def broken_begin(*args, **kwargs):
        raise RuntimeError("synthetic reentry failure")

    with monkeypatch.context() as context:
        context.setattr(provider, "_begin_source_recovery", broken_begin)
        with pytest.raises(RuntimeError, match="synthetic reentry failure"):
            provider.maintain()
    records = _read(provider).observations
    assert (records[-1].state, records[-1].reason) == ("UNKNOWN", "REENTRY_FAILED")
    assert not any(record.state == "RECOVERED" for record in records)
    assert provider.close()


def test_scope_mismatch_invalidates_even_if_later_fixed(monkeypatch):
    provider, _, first, second, _ = _pending(monkeypatch)
    recovery = provider._source_recovery
    state = provider._known_handles[second]
    state.source_scope = recovery.old_scope
    assert _read(provider).observations[-1].reason == "SCOPE_CHANGED"
    state.source_scope = recovery.new_scope
    assert provider.acknowledge_source_epoch_reset(first, epoch="e2")
    assert provider.acknowledge_source_epoch_reset(second, epoch="e2")
    assert not any(record.state == "RECOVERED" for record in _read(provider).observations)
    assert provider.close()


def test_future_cursor_is_checked_before_a_new_watermark(monkeypatch):
    provider, _, _, _, _ = _pending(monkeypatch)
    page = _read(provider)
    with pytest.raises(CtpQuoteV2MarketProviderError, match="RECOVERY_OBSERVATION_INVALID"):
        _read(provider, after=page.last_sequence + 1)
    next_page = _read(provider, after=page.last_sequence)
    assert next_page.observations[0].sequence == page.last_sequence + 1
    assert provider.close()


def test_failure_without_stream_lock_records_failed(monkeypatch):
    provider, _, _, _, _ = _pending(monkeypatch)
    recovery = provider._source_recovery
    assert recovery.reconnect_worker is not None
    recovery.reconnect_done.clear()
    provider._fail_source_recovery(reason="synthetic")
    assert recovery.state == "failed"
    assert _read(provider).observations[-1].state == "FAILED"
    recovery.reconnect_done.set()
    assert provider.close()


def test_completion_observation_precedes_concurrent_disconnect(monkeypatch):
    provider, clients, first, second, _ = _pending(monkeypatch)
    assert provider.acknowledge_source_epoch_reset(first, epoch="e2")
    stream = provider._stream
    attempting = threading.Event()
    finished = threading.Event()
    original = provider._current_source_scope
    threads = []
    observed = []

    def scope_with_callback(stream_arg):
        scope = original(stream_arg)
        if not threads:

            def callback():
                attempting.set()
                with stream._md_callback_lock:
                    observed.append(provider._source_recovery is None)
                    clients[-1].emit_disconnect()
                finished.set()

            thread = threading.Thread(target=callback)
            threads.append(thread)
            thread.start()
            assert attempting.wait(1)
            finished.wait(0.2)
        return scope

    monkeypatch.setattr(provider, "_current_source_scope", scope_with_callback)
    assert provider.acknowledge_source_epoch_reset(second, epoch="e2")
    threads[0].join(timeout=1)
    assert finished.is_set() and observed == [True]
    assert _read(provider).observations[-1].state == "RECOVERED"
    provider.maintain()
    assert _read(provider).observations[-1].state == "RECOVERING"
    _drive_until(provider, lambda: provider.source_epoch_reset_required(first))
    assert provider.close()


@pytest.mark.parametrize("mode", ["regressed", "zero", "boolean"])
def test_bad_watermark_clock_poisons_only_observation(monkeypatch, mode):
    provider, _, first, second, _ = _pending(monkeypatch)
    last = provider._recovery_observation._last_time
    value = {"regressed": last - 1, "zero": 0, "boolean": True}[mode]
    with monkeypatch.context() as context:
        context.setattr(observation_module.time, "monotonic_ns", lambda: value)
        with pytest.raises(CtpQuoteV2MarketProviderError, match="RECOVERY_OBSERVATION_UNAVAILABLE"):
            _read(provider)
    assert provider.acknowledge_source_epoch_reset(first, epoch="e2")
    assert provider.acknowledge_source_epoch_reset(second, epoch="e2")
    with pytest.raises(CtpQuoteV2MarketProviderError, match="RECOVERY_OBSERVATION_UNAVAILABLE"):
        _read(provider)
    assert provider.close()


def test_replacement_native_scope_mismatch_invalidates(monkeypatch):
    provider, _, first, second, _ = _pending(monkeypatch)
    recovery = provider._source_recovery
    stream = provider._stream
    with stream._md_callback_lock:
        stream._managed_quote_clock_source_scope = recovery.old_scope
    assert _read(provider).observations[-1].reason == "SCOPE_CHANGED"
    with stream._md_callback_lock:
        stream._managed_quote_clock_source_scope = recovery.new_scope
    assert provider.acknowledge_source_epoch_reset(first, epoch="e2")
    assert provider.acknowledge_source_epoch_reset(second, epoch="e2")
    assert not any(item.state == "RECOVERED" for item in _read(provider).observations)
    assert provider.close()


def test_scope_invalidation_exception_refuses_same_read(monkeypatch):
    provider, _, _, _, _ = _pending(monkeypatch)
    stream = provider._stream
    provider._stream = None

    def broken_invalidation(*args):
        raise RuntimeError("synthetic observation error")

    monkeypatch.setattr(provider._recovery_observation, "invalidate", broken_invalidation)
    with pytest.raises(CtpQuoteV2MarketProviderError, match="RECOVERY_OBSERVATION_UNAVAILABLE"):
        _read(provider)
    provider._stream = stream
    assert provider.close()


@pytest.mark.parametrize("member_count", [2, 6])
def test_each_ack_has_constant_number_of_complete_native_scope_reads(monkeypatch, member_count):
    provider, clients, _, _ = _provider(monkeypatch)
    handles = [provider.subscribe(_key(), generation="g1")]
    for index in range(member_count - 1):
        handles.append(provider.subscribe(_instrument_key(f"IC{index:04d}"), generation="g1"))
    clients[0].emit_disconnect()
    _drive_until(provider, lambda: provider.source_epoch_reset_required(handles[0]))
    stream = provider._stream
    original = stream.get_quote_v2_service_ready_scope
    calls = []

    def count_scope():
        calls.append(None)
        return original()

    monkeypatch.setattr(stream, "get_quote_v2_service_ready_scope", count_scope)
    assert provider.acknowledge_source_epoch_reset(handles[0], epoch="e2")
    assert len(calls) == 2
    assert provider.close()


@pytest.mark.parametrize("capacity", [True, -1, 1, 4097])
def test_observation_capacity_rejects_invalid_values(monkeypatch, capacity):
    with pytest.raises(CtpQuoteV2MarketProviderError, match="INVALID_CTP_MARKET_PROVIDER_CONFIG"):
        _provider(monkeypatch, capacity)
