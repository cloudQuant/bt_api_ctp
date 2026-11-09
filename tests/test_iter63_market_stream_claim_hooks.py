"""Synthetic CTP stream version hooks; no native session or account access."""
from __future__ import annotations

import threading
from types import SimpleNamespace

import pytest

from bt_api_py.gateway_dispatch_claim import DispatchClaimError, GatewayDispatchClaimCoordinator
from bt_api_ctp.feeds import live_ctp_feed
from bt_api_ctp.feeds.live_ctp_feed import ConnectionState, CtpMarketStream


DOMAINS = ("registry", "market", "parent_facts", "control", "session_source", "risk")


def ready():
    coordinator = GatewayDispatchClaimCoordinator._for_test("account", "generation")
    for domain in DOMAINS:
        token = coordinator.begin_change(domain)
        coordinator.finish_change(token, "NOT_SELECTED" if domain == "risk" else "READY")
    return coordinator


def stop_receipt(*, released=True, generation=1):
    return live_ctp_feed.ctp_client.CtpNativeStopReceipt(
        connection_generation=generation,
        join_required=False,
        join_completed=False,
        native_released=released,
        thread_alive=not released,
        timed_out=not released,
    )


def stream():
    return CtpMarketStream(
        None,
        broker_id="synthetic-broker",
        user_id="synthetic-user",
        password="synthetic-password",
        td_front="tcp://127.0.0.1:1",
        md_front="tcp://127.0.0.1:1",
        ctp_env_profile="config_front_pair",
        ctp_environment="simnow",
        topics=[{"topic": "tick", "symbol": "IF2612"}],
    )


def test_coordinator_binding_is_exact_and_only_before_start():
    value, coordinator = stream(), ready()
    with pytest.raises(ValueError, match="owner_mismatch"):
        value._bind_gateway_md_stream_dispatch_coordinator(
            coordinator, account_scope="other", service_generation="generation"
        )
    value._bind_gateway_md_stream_dispatch_coordinator(
        coordinator, account_scope="account", service_generation="generation"
    )
    with pytest.raises(DispatchClaimError, match="DOMAIN_UNAVAILABLE"):
        coordinator.capture()
    with pytest.raises(ValueError, match="already_started"):
        value._bind_gateway_md_stream_dispatch_coordinator(
            coordinator, account_scope="account", service_generation="generation"
        )


def test_gateway_client_bound_before_native_without_stream_lock(monkeypatch):
    value, coordinator = stream(), ready()
    calls = []

    class Client:
        trusted_flow_binding = None
        connection_generation = 1

        def _unlocked(self, operation):
            assert not value._md_callback_lock._is_owned()
            calls.append(operation)

        def _bind_gateway_md_dispatch_coordinator(self, found, *, account_scope, service_generation):
            self._unlocked("bind")
            assert found is coordinator
            assert (account_scope, service_generation) == ("account", "generation")

        def subscribe(self, instruments):
            self._unlocked("subscribe")
            assert instruments == ["IF2612"]

        def start(self, *, block, join_observer):
            self._unlocked("start")
            assert block is False and join_observer is False

        def stop_and_wait(self, timeout):
            self._unlocked("stop")
            assert timeout == 0.0
            return stop_receipt()

    monkeypatch.setattr(live_ctp_feed.ctp_client, "MdClient", lambda *_a, **_k: Client())
    value._bind_gateway_md_stream_dispatch_coordinator(
        coordinator, account_scope="account", service_generation="generation"
    )
    value.connect()
    assert calls == ["bind", "subscribe", "start"]
    with pytest.raises(DispatchClaimError, match="DOMAIN_UNAVAILABLE"):
        coordinator.capture()
    value.disconnect()
    assert calls == ["bind", "subscribe", "start", "stop"]


def test_disconnect_during_blocked_native_start_does_not_wait_or_claim_stop(monkeypatch):
    value, coordinator = stream(), ready()
    entered, release = threading.Event(), threading.Event()
    errors, calls = [], []

    class Client:
        trusted_flow_binding = None
        connection_generation = 1

        def _bind_gateway_md_dispatch_coordinator(self, *_args, **_kwargs):
            assert not value._md_callback_lock._is_owned()

        def subscribe(self, _instruments):
            assert not value._md_callback_lock._is_owned()

        def start(self, *, block, join_observer):
            assert not value._md_callback_lock._is_owned()
            entered.set()
            assert release.wait(3)

        def stop_and_wait(self, timeout):
            assert not value._md_callback_lock._is_owned()
            calls.append("stop")
            return stop_receipt()

    monkeypatch.setattr(live_ctp_feed.ctp_client, "MdClient", lambda *_a, **_k: Client())
    value._bind_gateway_md_stream_dispatch_coordinator(
        coordinator, account_scope="account", service_generation="generation"
    )

    def start():
        try:
            value.connect()
        except Exception as exc:
            errors.append(str(exc))

    worker = threading.Thread(target=start)
    worker.start()
    try:
        assert entered.wait(2)
        original = value._md_client
        with pytest.raises(RuntimeError, match="start_in_progress"):
            value.disconnect()
        assert value._md_client is original
        assert value.state is ConnectionState.ERROR
        assert calls == []
        with pytest.raises(DispatchClaimError, match="DOMAIN_UNAVAILABLE"):
            coordinator.capture()
    finally:
        release.set()
        worker.join(timeout=3)
    assert not worker.is_alive()
    assert errors == ["ctp_gateway_md_start_cancelled"]
    value.disconnect()
    assert calls == ["stop"]


def test_native_callbacks_invalidate_without_reenabling_source(monkeypatch):
    value, coordinator = stream(), ready()

    class Client:
        trusted_flow_binding = None
        connection_generation = 1

        def _bind_gateway_md_dispatch_coordinator(self, *_a, **_k):
            pass

        def subscribe(self, _instruments):
            pass

        def start(self, **_kwargs):
            pass

        def stop(self):
            pass

    monkeypatch.setattr(live_ctp_feed.ctp_client, "MdClient", lambda *_a, **_k: Client())
    value._bind_gateway_md_stream_dispatch_coordinator(
        coordinator, account_scope="account", service_generation="generation"
    )
    value.connect()
    token = coordinator.begin_change("session_source")
    coordinator.finish_change(token, "READY")
    assert coordinator.capture()
    value._md_client.on_disconnect(1)
    with pytest.raises(DispatchClaimError, match="DOMAIN_UNAVAILABLE"):
        coordinator.capture()
    assert value.state is ConnectionState.ERROR


def test_binding_rejects_an_active_unbound_writer(monkeypatch):
    value, coordinator = stream(), ready()
    entered, release = threading.Event(), threading.Event()

    class Client:
        trusted_flow_binding = None
        connection_generation = 1

        def subscribe(self, _instruments):
            pass

        def start(self, **_kwargs):
            entered.set()
            assert release.wait(3)

        def stop(self):
            pass

    monkeypatch.setattr(live_ctp_feed.ctp_client, "MdClient", lambda *_a, **_k: Client())
    worker = threading.Thread(target=value.connect)
    worker.start()
    try:
        assert entered.wait(2)
        with pytest.raises(ValueError, match="writer_active"):
            value._bind_gateway_md_stream_dispatch_coordinator(
                coordinator, account_scope="account", service_generation="generation"
            )
    finally:
        release.set()
        worker.join(timeout=3)
    assert not worker.is_alive()


def test_blocked_stop_retains_exact_client_and_fences_replacement(monkeypatch):
    value, coordinator = stream(), ready()
    entered, release = threading.Event(), threading.Event()

    class Client:
        trusted_flow_binding = None
        connection_generation = 1

        def _bind_gateway_md_dispatch_coordinator(self, *_a, **_k):
            pass

        def subscribe(self, _instruments):
            pass

        def start(self, **_kwargs):
            pass

        def stop_and_wait(self, timeout):
            assert not value._md_callback_lock._is_owned()
            entered.set()
            assert release.wait(3)
            return stop_receipt()

    monkeypatch.setattr(live_ctp_feed.ctp_client, "MdClient", lambda *_a, **_k: Client())
    value._bind_gateway_md_stream_dispatch_coordinator(
        coordinator, account_scope="account", service_generation="generation"
    )
    value.connect()
    original = value._md_client
    worker = threading.Thread(target=value.disconnect)
    worker.start()
    try:
        assert entered.wait(2)
        assert value._md_client is original
        assert value.state is ConnectionState.ERROR
        with pytest.raises(RuntimeError, match="stop_in_progress_or_uncertain"):
            value.disconnect()
        with pytest.raises(RuntimeError, match="client_not_detached"):
            value.connect()
        with pytest.raises(DispatchClaimError, match="DOMAIN_UNAVAILABLE"):
            coordinator.capture()
    finally:
        release.set()
        worker.join(timeout=3)
    assert not worker.is_alive()
    assert value._md_client is None


def test_failed_stop_retains_uncertain_client(monkeypatch):
    value, coordinator = stream(), ready()

    class Client:
        trusted_flow_binding = None
        connection_generation = 1

        def _bind_gateway_md_dispatch_coordinator(self, *_a, **_k):
            pass

        def subscribe(self, _instruments):
            pass

        def start(self, **_kwargs):
            pass

        def stop_and_wait(self, timeout):
            raise RuntimeError("synthetic_stop_failure")

    monkeypatch.setattr(live_ctp_feed.ctp_client, "MdClient", lambda *_a, **_k: Client())
    value._bind_gateway_md_stream_dispatch_coordinator(
        coordinator, account_scope="account", service_generation="generation"
    )
    value.connect()
    original = value._md_client
    with pytest.raises(RuntimeError, match="synthetic_stop_failure"):
        value.disconnect()
    assert value._md_client is original
    assert value.state is ConnectionState.ERROR
    with pytest.raises(RuntimeError, match="stop_in_progress_or_uncertain"):
        value.disconnect()
    with pytest.raises(RuntimeError, match="client_not_detached"):
        value.connect()


def test_no_join_release_is_confirmed_before_client_detach(monkeypatch):
    value, coordinator = stream(), ready()
    released = threading.Event()
    calls = []

    class Client:
        trusted_flow_binding = None
        connection_generation = 1

        def _bind_gateway_md_dispatch_coordinator(self, *_a, **_k):
            pass

        def subscribe(self, _instruments):
            pass

        def start(self, **_kwargs):
            pass

        def stop_and_wait(self, timeout):
            assert not value._md_callback_lock._is_owned()
            calls.append(timeout)
            return stop_receipt(released=released.is_set())

    monkeypatch.setattr(live_ctp_feed.ctp_client, "MdClient", lambda *_a, **_k: Client())
    value._bind_gateway_md_stream_dispatch_coordinator(
        coordinator, account_scope="account", service_generation="generation"
    )
    value.connect()
    original = value._md_client
    with pytest.raises(RuntimeError, match="stop_unconfirmed"):
        value.disconnect()
    assert value._md_client is original
    assert value.state is ConnectionState.ERROR
    # The failed stop ended the change UNAVAILABLE (not a permanent poison).
    assert coordinator._domains["session_source"][4] is False
    with pytest.raises(DispatchClaimError, match="DOMAIN_UNAVAILABLE"):
        coordinator.capture()
    with pytest.raises(RuntimeError, match="client_not_detached"):
        value.connect()
    released.set()
    value.disconnect()
    assert value._md_client is None
    assert value.state is ConnectionState.DISCONNECTED
    assert calls == [0.0, 0.0]


def test_real_md_client_retains_stream_while_native_release_blocks(monkeypatch):
    value, coordinator = stream(), ready()
    entered, release = threading.Event(), threading.Event()

    class Native:
        def RegisterSpi(self, _spi):
            pass

        def RegisterFront(self, _front):
            pass

        def Init(self):
            pass

        def Release(self):
            entered.set()
            assert release.wait(3)

    native = Native()
    monkeypatch.setattr(live_ctp_feed.ctp_client, "_check_native_module", lambda: None)
    monkeypatch.setattr(live_ctp_feed.ctp_client, "_flow_dir", lambda _name: "fake-flow")
    monkeypatch.setattr(
        live_ctp_feed.ctp_client, "CThostFtdcMdApi",
        SimpleNamespace(CreateFtdcMdApi=lambda _flow: native),
    )
    value._bind_gateway_md_stream_dispatch_coordinator(
        coordinator, account_scope="account", service_generation="generation"
    )
    value.connect()
    original = value._md_client
    try:
        with pytest.raises(RuntimeError, match="stop_unconfirmed"):
            value.disconnect()
        assert entered.wait(2)
        assert value._md_client is original
        with pytest.raises(RuntimeError, match="client_not_detached"):
            value.connect()
    finally:
        release.set()
    assert original.stop_and_wait(timeout=2.0).complete is True
    value.disconnect()
    assert value._md_client is None


def test_wrong_generation_stop_receipt_cannot_detach(monkeypatch):
    value, coordinator = stream(), ready()

    class Client:
        trusted_flow_binding = None
        connection_generation = 1

        def _bind_gateway_md_dispatch_coordinator(self, *_a, **_k):
            pass

        def subscribe(self, _instruments):
            pass

        def start(self, **_kwargs):
            pass

        def stop_and_wait(self, timeout):
            return stop_receipt(generation=2)

    monkeypatch.setattr(live_ctp_feed.ctp_client, "MdClient", lambda *_a, **_k: Client())
    value._bind_gateway_md_stream_dispatch_coordinator(
        coordinator, account_scope="account", service_generation="generation"
    )
    value.connect()
    original = value._md_client
    with pytest.raises(RuntimeError, match="stop_unconfirmed"):
        value.disconnect()
    assert value._md_client is original


def test_managed_reconnect_preflight_does_not_poison_source(monkeypatch):
    value, coordinator = stream(), ready()

    class Client:
        trusted_flow_binding = None
        connection_generation = 1

        def _bind_gateway_md_dispatch_coordinator(self, *_a, **_k):
            pass

        def subscribe(self, _instruments):
            pass

        def start(self, **_kwargs):
            pass

        def stop_and_wait(self, timeout):
            return stop_receipt()

    monkeypatch.setattr(live_ctp_feed.ctp_client, "MdClient", lambda *_a, **_k: Client())
    value._bind_gateway_md_stream_dispatch_coordinator(
        coordinator, account_scope="account", service_generation="generation"
    )
    assert value.enable_quote_v2_service_readiness() is True
    value.connect()
    value.disconnect()
    with pytest.raises(RuntimeError, match="reconnect_requires_confirmed_stop"):
        value.connect()
    assert value._md_client is None
    token = coordinator.begin_change("session_source")
    coordinator.finish_change(token, "UNAVAILABLE")


def test_start_snapshot_rejects_concurrent_subscription_changes(monkeypatch):
    value, coordinator = stream(), ready()
    entered, release = threading.Event(), threading.Event()
    snapshots, errors = [], []

    class Client:
        trusted_flow_binding = None
        connection_generation = 1

        def _bind_gateway_md_dispatch_coordinator(self, *_a, **_k):
            pass

        def subscribe(self, instruments):
            snapshots.append(tuple(instruments))
            entered.set()
            assert release.wait(3)

        def subscribe_additional(self, instruments):
            snapshots.append(tuple(instruments))
            return tuple(instruments)

        def start(self, **_kwargs):
            pass

    monkeypatch.setattr(live_ctp_feed.ctp_client, "MdClient", lambda *_a, **_k: Client())
    value._bind_gateway_md_stream_dispatch_coordinator(
        coordinator, account_scope="account", service_generation="generation"
    )

    def connect():
        try:
            value.connect()
        except Exception as exc:
            errors.append(exc)

    worker = threading.Thread(target=connect)
    worker.start()
    try:
        assert entered.wait(2)
        with pytest.raises(RuntimeError, match="start_in_progress"):
            value.subscribe_topics([{"topic": "tick", "symbol": "IF2701"}])
        with pytest.raises(RuntimeError, match="start_in_progress"):
            value.unsubscribe_topics([{"topic": "tick", "symbol": "IF2612"}])
        assert set(value._quote_v2_subscription_metadata) == {"IF2612"}
    finally:
        release.set()
        worker.join(timeout=3)
    assert not worker.is_alive()
    assert errors == []
    token = coordinator.begin_change("session_source")
    coordinator.finish_change(token, "UNAVAILABLE")
    value.subscribe_topics([{"topic": "tick", "symbol": "IF2701"}])
    assert snapshots == [("IF2612",), ("IF2701",)]
    assert set(value._quote_v2_subscription_metadata) == {"IF2612", "IF2701"}


@pytest.mark.parametrize("operation", [
    lambda value: value._on_login(None),
    lambda value: value._on_error(None),
    lambda value: value._on_login_failure(None),
    lambda value: value.subscribe_topics([{"topic": "tick", "symbol": "IF2701"}]),
    lambda value: value.unsubscribe_topics([{"topic": "tick", "symbol": "IF2612"}]),
    lambda value: value.enable_quote_v2_service_readiness(),
])
def test_stream_writers_invalidate_source(operation):
    value, coordinator = stream(), ready()
    value._bind_gateway_md_stream_dispatch_coordinator(
        coordinator, account_scope="account", service_generation="generation"
    )
    token = coordinator.begin_change("session_source")
    coordinator.finish_change(token, "READY")
    operation(value)
    with pytest.raises(DispatchClaimError, match="DOMAIN_UNAVAILABLE"):
        coordinator.capture()
