"""Synthetic status-source failures revoke prior usable observations."""

import threading

import pytest
from test_ctp_callback_ingress_client import _FakeTraderApiFactory, _new_active_client
from test_gateway_instrument_status_snapshot import _open
from test_instrument_status_source_fence import _payload, _provider
from test_iter67_instrument_status_ingress import SYMBOL, VENUE, _feed_with, native

import bt_api_ctp.ctp.client as client_module
from bt_api_ctp.ctp import callback_ingress as ingress
from bt_api_ctp.ctp.client import TraderClient, _TraderSpi
from bt_api_ctp.direct_write_provider import CtpDirectWriteProvider

pytest_plugins = ("test_gateway_instrument_status_snapshot",)


@pytest.mark.parametrize("failure", ["malformed", "sequence_conflict"])
def test_current_callback_refusal_revokes_previous_status(failure):
    trader, provider = _provider()
    listener = trader.status_listeners[0]
    listener(_payload(status="2", sequence=2))
    assert provider.instrument_status_view(VENUE, SYMBOL).usable
    if failure == "malformed":
        payload = _payload(status="6", sequence=3)
        payload["digest"] = "0" * 64
    else:
        payload = _payload(status="6", sequence=2)
    listener(payload)
    view = provider.instrument_status_view(VENUE, SYMBOL)
    assert view.usable is False
    assert view.reason_code == "ctp_instrument_status_source_unavailable"
    provider.close()


def test_without_installed_listener_has_no_available_status():
    trader, provider = _provider()
    listener = trader.status_listeners[0]
    listener(_payload(status="2"))
    assert provider.instrument_status_view(VENUE, SYMBOL).usable
    trader.remove_instrument_status_listener(listener)
    view = provider.instrument_status_view(VENUE, SYMBOL)
    assert view is None or view.usable is False
    provider.close()


@pytest.fixture(params=["direct", "managed"])
def native_source(request, monkeypatch):
    if request.param == "managed":
        client, api, _, _ = _new_active_client(monkeypatch)
    else:
        _FakeTraderApiFactory.instances = []
        _FakeTraderApiFactory.block_join = False
        _FakeTraderApiFactory.register_spi_exception = None
        _FakeTraderApiFactory.init_exception = None
        monkeypatch.setattr(client_module, "_check_native_module", lambda: None)
        monkeypatch.setattr(client_module, "_flow_dir", lambda _name: "fake-flow")
        monkeypatch.setattr(client_module, "_register_ctp_native_api", lambda _api: None)
        monkeypatch.setattr(client_module, "CThostFtdcTraderApi", _FakeTraderApiFactory)
        client = TraderClient("tcp://fake", "9999", "investor-1", "synthetic", auto_settlement_confirm=False)
        client.start()
        api = _FakeTraderApiFactory.instances[-1]
    provider = CtpDirectWriteProvider(_feed_with(client))
    provider.ensure_listening()
    yield client, api, provider
    provider.close()
    client.stop()


@pytest.mark.parametrize("failure", ["missing_getter", "capture_exception"])
def test_real_native_capture_failure_stales_prior_status(native_source, monkeypatch, failure):
    _, api, provider = native_source
    api.spi.OnRtnInstrumentStatus(native("2"))
    assert provider.instrument_status_view(VENUE, SYMBOL).usable
    if failure == "missing_getter":
        broken = native("6")
        del broken.InstrumentID
    else:
        broken = native("6")
        def fail_capture(*_args, **_kwargs):
            raise ValueError("secret /private/native")
        monkeypatch.setattr(ingress, "build_callback_ingress_record_v2", fail_capture)
    api.spi.OnRtnInstrumentStatus(broken)
    view = provider.instrument_status_view(VENUE, SYMBOL)
    assert view.usable is False
    assert view.reason_code == "ctp_instrument_status_source_unavailable"


def test_native_listener_exception_revokes_prior_status_without_exception_text(native_source, caplog):
    client, api, provider = native_source
    api.spi.OnRtnInstrumentStatus(native("2"))
    assert provider.instrument_status_view(VENUE, SYMBOL).usable
    def broken_listener(_payload):
        raise ValueError("secret /private/native")
    client.add_instrument_status_listener(broken_listener)
    api.spi.OnRtnInstrumentStatus(native("6"))
    assert provider.instrument_status_view(VENUE, SYMBOL).usable is False
    assert "secret" not in caplog.text
    client.remove_instrument_status_listener(broken_listener)


def test_listener_failure_before_first_projection_stays_unavailable(native_source, monkeypatch):
    _, api, provider = native_source
    original = provider._instrument_status_session_facts

    def fail_facts():
        raise RuntimeError("secret /private/native")

    monkeypatch.setattr(provider, "_instrument_status_session_facts", fail_facts)
    api.spi.OnRtnInstrumentStatus(native("2"))
    monkeypatch.setattr(provider, "_instrument_status_session_facts", original)
    api.spi.OnRtnInstrumentStatus(native("6"))
    view = provider.instrument_status_view(VENUE, SYMBOL)
    assert view is None or not view.usable


def test_failure_stays_unavailable_until_new_generation_and_new_push(native_source):
    client, api, provider = native_source
    if client._callback_ingress is not None:
        pytest.skip("managed ingress poison requires its own owner lifecycle")
    api.spi.OnRtnInstrumentStatus(native("2"))
    broken = native("6")
    del broken.InstrumentID
    api.spi.OnRtnInstrumentStatus(broken)
    assert not provider.instrument_status_view(VENUE, SYMBOL).usable
    api.spi.OnRtnInstrumentStatus(native("6"))
    assert not provider.instrument_status_view(VENUE, SYMBOL).usable
    api.spi.OnFrontDisconnected(1001)
    api.spi.OnFrontConnected()
    assert not provider.instrument_status_view(VENUE, SYMBOL).usable
    api.spi.OnRtnInstrumentStatus(native("6"))
    view = provider.instrument_status_view(VENUE, SYMBOL)
    assert view.usable and view.effective_status_category == "closed"


def test_projection_exception_stales_current_scope(monkeypatch):
    trader, provider = _provider()
    listener = trader.status_listeners[0]
    listener(_payload(status="2"))
    bridge = provider._instrument_status_bridge
    monkeypatch.setattr(bridge, "observe_payload", lambda _payload: (_ for _ in ()).throw(
        ValueError("secret /private/native")))
    listener(_payload(status="6", sequence=2))
    assert provider.instrument_status_view(VENUE, SYMBOL).usable is False
    provider.close()


def test_duplicate_and_old_sequence_preserve_current_status():
    trader, provider = _provider()
    listener = trader.status_listeners[0]
    current = _payload(status="6", sequence=2)
    listener(current)
    listener(current)
    listener(_payload(status="2", sequence=1))
    view = provider.instrument_status_view(VENUE, SYMBOL)
    assert view.usable and view.effective_status_category == "closed"
    provider.close()


def test_delayed_old_native_capture_failure_cannot_stale_new_generation(native_source, monkeypatch):
    client, api, provider = native_source
    if client._callback_ingress is not None:
        pytest.skip("managed ingress capture is serialized under the native query lock")
    api.spi.OnRtnInstrumentStatus(native("2"))
    entered, release = threading.Event(), threading.Event()
    original = ingress.build_callback_ingress_record_v2

    def delayed_capture(*args, **kwargs):
        if threading.current_thread().name == "old-status":
            entered.set()
            assert release.wait(2)
            raise ValueError("old callback capture failed")
        return original(*args, **kwargs)

    monkeypatch.setattr(ingress, "build_callback_ingress_record_v2", delayed_capture)
    worker = threading.Thread(target=api.spi.OnRtnInstrumentStatus, args=(native("2"),), name="old-status")
    worker.start()
    try:
        assert entered.wait(2)
        api.spi.OnFrontDisconnected(1001)
        api.spi.OnFrontConnected()
        api.spi.OnRtnInstrumentStatus(native("6"))
    finally:
        release.set()
        worker.join(2)
    assert not worker.is_alive()
    view = provider.instrument_status_view(VENUE, SYMBOL)
    assert view.usable and view.effective_status_category == "closed"


@pytest.mark.parametrize("advance_generation", [False, True])
def test_delayed_listener_failure_invalidates_same_scope_only(native_source, monkeypatch, advance_generation):
    client, api, provider = native_source
    if client._callback_ingress is not None:
        pytest.skip("managed callbacks hold the query lock during listener dispatch")
    api.spi.OnRtnInstrumentStatus(native("2"))
    entered, release = threading.Event(), threading.Event()
    original = provider._instrument_status_session_facts

    def delayed_failure():
        if threading.current_thread().name == "old-status":
            entered.set()
            assert release.wait(2)
            raise RuntimeError("secret /private/native")
        return original()

    monkeypatch.setattr(provider, "_instrument_status_session_facts", delayed_failure)
    worker = threading.Thread(target=api.spi.OnRtnInstrumentStatus, args=(native("2"),), name="old-status")
    worker.start()
    try:
        assert entered.wait(2)
        if advance_generation:
            api.spi.OnFrontDisconnected(1001)
            api.spi.OnFrontConnected()
        api.spi.OnRtnInstrumentStatus(native("6"))
    finally:
        release.set()
        worker.join(2)
    assert not worker.is_alive()
    view = provider.instrument_status_view(VENUE, SYMBOL)
    if advance_generation:
        assert view.usable and view.effective_status_category == "closed"
    else:
        assert not view.usable and view.reason_code == "ctp_instrument_status_source_unavailable"


@pytest.mark.parametrize("old_failure", [False, True])
def test_same_generation_new_spi_fences_delayed_old_callback(native_source, monkeypatch, old_failure):
    client, api, provider = native_source
    if client._callback_ingress is not None:
        pytest.skip("managed callbacks hold the query lock during listener dispatch")
    api.spi.OnRtnInstrumentStatus(native("2"))
    old_spi = api.spi
    entered, release = threading.Event(), threading.Event()
    original = provider._instrument_status_session_facts

    def delayed_sample():
        if threading.current_thread().name == "old-spi":
            entered.set()
            assert release.wait(2)
            if old_failure:
                raise RuntimeError("secret /private/native")
        return original()

    monkeypatch.setattr(provider, "_instrument_status_session_facts", delayed_sample)
    worker = threading.Thread(target=old_spi.OnRtnInstrumentStatus, args=(native("2"),), name="old-spi")
    worker.start()
    try:
        assert entered.wait(2)
        with client._query_state_lock:
            replacement = _TraderSpi(client, api)
            replacement._callback_source_tags = old_spi._callback_source_tags
            client._spi = replacement
            api.spi = replacement
        absent = provider.instrument_status_view(VENUE, SYMBOL)
        assert absent is None or not absent.usable
        replacement.OnRtnInstrumentStatus(native("6"))
    finally:
        release.set()
        worker.join(2)
    assert not worker.is_alive()
    view = provider.instrument_status_view(VENUE, SYMBOL)
    assert view.usable and view.effective_status_category == "closed"


@pytest.mark.parametrize("failure", ["missing_getter", "capture_exception", "hook_exception"])
def test_native_capture_failure_immediately_revokes_gateway_snapshot(status_prepared, monkeypatch, failure):
    session, _, trader, _ = _open(status_prepared)
    trader._spi.OnRtnInstrumentStatus(native("2"))
    snapshot = session.capture_instrument_status(VENUE, SYMBOL)
    assert snapshot.view.usable and session.snapshot_is_current(snapshot)
    field = native("6")
    if failure in {"missing_getter", "hook_exception"}:
        del field.InstrumentID
    if failure == "capture_exception":
        def fail_capture(*_args, **_kwargs):
            raise RuntimeError("secret /private/native")
        monkeypatch.setattr(ingress, "build_callback_ingress_record_v2", fail_capture)
    if failure == "hook_exception":
        bridge = session._direct._instrument_status_bridge
        original_mark = bridge.mark_source_unavailable
        def fail_bridge():
            raise RuntimeError("secret /private/native")
        monkeypatch.setattr(bridge, "mark_source_unavailable", fail_bridge)
    trader._spi.OnRtnInstrumentStatus(field)
    # No subsequent view or native callback is needed to invalidate the cut.
    assert not session.snapshot_is_current(snapshot)
    if failure == "hook_exception":
        monkeypatch.setattr(bridge, "mark_source_unavailable", original_mark)
    assert session.close() is True


def test_detaching_native_listener_immediately_revokes_gateway_snapshot(status_prepared):
    session, _, trader, _ = _open(status_prepared)
    trader._spi.OnRtnInstrumentStatus(native("2"))
    snapshot = session.capture_instrument_status(VENUE, SYMBOL)
    assert snapshot.view.usable and session.snapshot_is_current(snapshot)
    trader.remove_instrument_status_listener(session._direct._instrument_status_listener)
    assert not session.snapshot_is_current(snapshot)
    assert not session._direct.instrument_status_view(VENUE, SYMBOL).usable
    assert session.close() is True
