"""Source readiness for retained CTP events, using the real provider and fake MD."""

import importlib.util
import sys
import threading
import time
from dataclasses import replace
from pathlib import Path

import pytest

from bt_api_ctp.feeds import live_ctp_feed
from bt_api_ctp.market_provider import CtpQuoteV2MarketProvider, CtpQuoteV2MarketProviderError

# Load the existing synthetic fixture under a unique name, including under
# pytest importlib mode; it never constructs the real native factory.
_fixture_path = Path(__file__).with_name("test_market_provider.py")
_spec = importlib.util.spec_from_file_location("r21_retained_market_fixture", _fixture_path)
_fixture = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = _fixture
_spec.loader.exec_module(_fixture)


@pytest.fixture
def session(monkeypatch):
    _fixture._SyntheticMdClient.instances.clear()
    monkeypatch.setattr(live_ctp_feed.ctp_client, "MdClient", _fixture._SyntheticMdClient)
    provider = CtpQuoteV2MarketProvider(
        stream_factory=_fixture._stream_factory,
        source_ref=_fixture._SOURCE_REF,
        entitlement_ref=_fixture._ENTITLEMENT_REF,
        connect_timeout_seconds=1.0,
    )
    handle = provider.subscribe(_fixture._key(), generation="synthetic-generation")
    client = _fixture._SyntheticMdClient.instances[-1]
    try:
        yield provider, handle, client
    finally:
        deadline = time.monotonic() + 3
        while not provider.close():
            assert time.monotonic() < deadline
            provider.maintain()
            time.sleep(0.01)


def test_healthy_check_keeps_queue_and_poll_delivers_once(session):
    provider, handle, client = session
    _fixture._emit_synthetic_tick(client, "IF2612", 2)
    state = provider._known_handles[handle]
    size = state.queue.size
    assert size > 0
    assert provider.retained_events_ready(handle) is True
    assert provider.retained_events_ready(handle) is True
    assert state.queue.size == size
    events = provider.poll(handle, limit=256)
    assert len(events) == size
    assert state.queue.size == 0
    assert provider.poll(handle, limit=256) == ()


def test_foreign_inactive_and_wrong_owner_are_rejected(session):
    provider, handle, _ = session
    assert provider.retained_events_ready(object()) is False
    assert provider.retained_events_ready(replace(handle)) is False
    errors = []

    def other_thread():
        try:
            provider.retained_events_ready(handle)
        except CtpQuoteV2MarketProviderError as error:
            errors.append(error.code)

    thread = threading.Thread(target=other_thread)
    thread.start()
    thread.join(2)
    assert not thread.is_alive()
    assert errors == ["CTP_MARKET_OWNER_THREAD_REQUIRED"]
    assert provider.unsubscribe(handle) is True
    assert provider.retained_events_ready(handle) is False


def test_scope_symbol_reset_and_failclosed_share_poll_behavior(session, monkeypatch):
    provider, handle, _ = session
    state = provider._known_handles[handle]
    stream = state.stream
    state.source_epoch_reset_pending = True
    assert provider.retained_events_ready(handle) is False
    assert provider.poll(handle, limit=1) == ()
    state.source_epoch_reset_pending = False
    with monkeypatch.context() as patch:
        patch.setattr(stream, "is_quote_v2_service_symbol_ready", lambda _symbol: False)
        assert provider.retained_events_ready(handle) is False
        assert provider.poll(handle, limit=1) == ()
    with monkeypatch.context() as patch:
        patch.setattr(provider, "_current_source_scope", lambda _stream: None)
        assert provider.retained_events_ready(handle) is False
        assert provider.poll(handle, limit=1) == ()
    with monkeypatch.context() as patch:
        patch.setattr(provider, "_current_source_scope", lambda _stream: object())
        for method in (
            lambda: provider.retained_events_ready(handle),
            lambda: provider.poll(handle, limit=1),
        ):
            with pytest.raises(
                CtpQuoteV2MarketProviderError, match="CTP_MARKET_SOURCE_SCOPE_CHANGED"
            ):
                method()
    with monkeypatch.context() as patch:
        patch.setattr(type(stream), "quote_v2_service_fail_closed", property(lambda _self: True))
        for method in (
            lambda: provider.retained_events_ready(handle),
            lambda: provider.poll(handle, limit=1),
        ):
            with pytest.raises(
                CtpQuoteV2MarketProviderError, match="CTP_MARKET_SOURCE_SCOPE_CHANGED"
            ):
                method()


def test_disconnect_false_while_reset_false_and_recovery_cannot_deliver(session, monkeypatch):
    provider, handle, client = session
    stopping, release = threading.Event(), threading.Event()
    stop = client.stop_and_wait

    def slow_stop(*, timeout):
        stopping.set()
        assert release.wait(2)
        return stop(timeout=timeout)

    monkeypatch.setattr(client, "stop_and_wait", slow_stop)
    client.emit_disconnect()
    try:
        assert provider.source_epoch_reset_required(handle) is False
        assert provider.retained_events_ready(handle) is False
        assert stopping.wait(1)
        assert provider.source_epoch_reset_required(handle) is False
        assert provider.source_recovery_state(handle) == "recovering"
        assert provider.retained_events_ready(handle) is False
        assert provider.poll(handle, limit=1) == ()
        recovery = provider._source_recovery
        with monkeypatch.context() as patch:
            # Even when a drive step finishes recovery, this particular
            # observation must not deliver retained data in the same call.
            patch.setattr(
                provider,
                "_drive_source_recovery",
                lambda: setattr(provider, "_source_recovery", None),
            )
            try:
                assert provider.retained_events_ready(handle) is False
                assert provider._source_recovery is None
            finally:
                provider._source_recovery = recovery
    finally:
        release.set()
