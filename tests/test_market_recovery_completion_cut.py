"""Synthetic native callback races around CTP recovery completion."""

from __future__ import annotations

import threading

import pytest
from test_market_provider import _drive_until, _failing_construction_provider, _instrument_key, _key


def _reset_pending(monkeypatch, *, max_parked_symbols=0):
    provider, clients, _, _ = _failing_construction_provider(
        monkeypatch, max_parked_symbols=max_parked_symbols
    )
    first = provider.subscribe(_key(), generation="generation-1")
    second = provider.subscribe(_instrument_key("IH2612"), generation="generation-1")
    clients[0].emit_disconnect()
    _drive_until(provider, lambda: provider.source_epoch_reset_required(first))
    return provider, clients, first, second


def test_native_disconnect_cannot_split_last_ack_from_completion(monkeypatch):
    provider, clients, first, second = _reset_pending(monkeypatch)
    assert provider.acknowledge_source_epoch_reset(first, epoch="epoch-2")
    stream = provider._stream
    callback_attempting = threading.Event()
    callback_finished = threading.Event()
    callback_observed_completed = []
    original = provider._current_source_scope
    threads = []

    def concurrent_scope_read(stream_arg):
        scope = original(stream_arg)
        if not threads:
            def callback():
                callback_attempting.set()
                with stream._md_callback_lock:
                    callback_observed_completed.append(provider._source_recovery is None)
                    clients[-1].emit_disconnect()
                callback_finished.set()

            thread = threading.Thread(target=callback)
            threads.append(thread)
            thread.start()
            assert callback_attempting.wait(1)
            callback_finished.wait(0.2)
        return scope

    monkeypatch.setattr(provider, "_current_source_scope", concurrent_scope_read)
    assert provider.acknowledge_source_epoch_reset(second, epoch="epoch-2")
    threads[0].join(timeout=1)
    assert callback_finished.is_set()
    assert callback_observed_completed == [True]
    assert stream.quote_v2_service_recovery_required
    provider.maintain()
    assert provider._source_recovery is not None
    _drive_until(provider, lambda: provider.source_epoch_reset_required(first))
    assert provider._source_recovery is not None
    assert provider.close() is True


@pytest.mark.parametrize(
    "fault", ["disconnect", "fail_closed", "generation", "stream_generation", "symbol_not_ready"]
)
def test_invalid_source_before_final_ack_cannot_complete(monkeypatch, fault):
    provider, clients, first, second = _reset_pending(monkeypatch)
    recovery = provider._source_recovery
    assert provider.acknowledge_source_epoch_reset(first, epoch="epoch-2")
    stream = provider._stream
    if fault == "disconnect":
        clients[-1].emit_disconnect()
    elif fault == "fail_closed":
        stream._fail_closed_quote_v2_subscription()
    elif fault == "generation":
        clients[-1].connection_generation += 1
    elif fault == "stream_generation":
        with stream._md_callback_lock:
            stream._connection_generation += 1
    else:
        with stream._md_callback_lock:
            stream._quote_v2_service_acknowledged_symbols.discard("IH2612")
    assert not provider.acknowledge_source_epoch_reset(second, epoch="epoch-2")
    assert provider._source_recovery is recovery
    assert recovery.state == "failed"
    assert provider._source_scope is None
    assert provider.close() is True


def test_member_mismatch_cannot_complete(monkeypatch):
    provider, _, first, second = _reset_pending(monkeypatch)
    recovery = provider._source_recovery
    assert provider.acknowledge_source_epoch_reset(first, epoch="epoch-2")
    second_state = provider._known_handles[second]
    second_state.source_scope = recovery.old_scope
    assert not provider.acknowledge_source_epoch_reset(second, epoch="epoch-2")
    assert provider._source_recovery is recovery
    assert recovery.state == "failed"
    assert provider.close() is True


def test_all_members_acknowledge_before_completion(monkeypatch):
    provider, _, first, second = _reset_pending(monkeypatch)
    recovery = provider._source_recovery
    assert provider.acknowledge_source_epoch_reset(first, epoch="epoch-2")
    assert provider._source_recovery is recovery
    assert provider.acknowledge_source_epoch_reset(second, epoch="epoch-2")
    assert provider._source_recovery is None
    assert provider._source_scope is recovery.new_scope
    assert provider.close() is True


@pytest.mark.parametrize("max_parked_symbols", [0, 2])
def test_release_unacknowledged_member_completes_remaining_set(monkeypatch, max_parked_symbols):
    provider, _, first, second = _reset_pending(
        monkeypatch, max_parked_symbols=max_parked_symbols
    )
    recovery = provider._source_recovery
    assert provider.acknowledge_source_epoch_reset(first, epoch="epoch-2")
    assert provider.unsubscribe(second)
    assert provider._source_recovery is None
    assert provider._source_scope is recovery.new_scope
    assert set(provider._handles_by_symbol) == {"IF2612"}
    assert provider._session_symbols() == (
        frozenset({"IF2612", "IH2612"}) if max_parked_symbols else frozenset({"IF2612"})
    )
    assert provider.close() is True


@pytest.mark.parametrize("max_parked_symbols", [0, 2])
def test_disconnect_during_release_does_not_clear_recovery(monkeypatch, max_parked_symbols):
    provider, clients, first, second = _reset_pending(
        monkeypatch, max_parked_symbols=max_parked_symbols
    )
    recovery = provider._source_recovery
    assert provider.acknowledge_source_epoch_reset(first, epoch="epoch-2")
    remove = provider._remove_handle

    def remove_then_disconnect(handle):
        remove(handle)
        clients[-1].emit_disconnect()

    monkeypatch.setattr(provider, "_remove_handle", remove_then_disconnect)
    assert provider.unsubscribe(second)
    assert provider._source_recovery is recovery
    monkeypatch.setattr(provider, "_remove_handle", remove)
    provider.maintain()
    assert provider._source_recovery is not None
    _drive_until(provider, lambda: provider.source_epoch_reset_required(first))
    assert provider._source_recovery is not None
    assert provider.close() is True
