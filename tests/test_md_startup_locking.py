"""Bounded fake-thread regressions for the MD native startup lifecycle."""

from __future__ import annotations

import threading

from bt_api_ctp.ctp import client as client_module
from bt_api_ctp.ctp.client import MdClient

_WAIT_SECONDS = 1.0


class _StartupApi:
    def __init__(self):
        self.spi = None
        self.fronts = []
        self.release_calls = 0
        self.register_none_calls = 0
        self.init_calls = 0
        self.join_calls = 0
        self.login_calls = []
        self._lock = threading.Lock()

    def RegisterSpi(self, spi):
        with self._lock:
            self.spi = spi
            if spi is None:
                self.register_none_calls += 1

    def RegisterFront(self, front):
        self.fronts.append(front)

    def Init(self):
        self.init_calls += 1

    def ReqUserLogin(self, field, request_id):
        self.login_calls.append((field, request_id))
        return 0

    def Join(self):
        self.join_calls += 1
        return 0

    def Release(self):
        with self._lock:
            self.release_calls += 1


def _install_api(monkeypatch, api):
    monkeypatch.setattr(client_module, "_check_native_module", lambda: None)
    monkeypatch.setattr(client_module, "_flow_dir", lambda _name: "offline-flow")
    monkeypatch.setattr(
        client_module,
        "_register_ctp_native_api",
        lambda _api: None,
    )
    monkeypatch.setattr(
        client_module.CThostFtdcMdApi,
        "CreateFtdcMdApi",
        staticmethod(lambda _flow: api),
    )
    return MdClient("tcp://md.example:123", "broker", "user", "password")


def _start_in_thread(client, errors):
    def run():
        try:
            client.start(block=False)
        except BaseException as exc:  # record the original failure for the test thread
            errors.append(exc)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread


def test_register_spi_can_wait_for_callback_thread_without_state_lock(monkeypatch):
    api = _StartupApi()
    callback_finished = threading.Event()
    callback_finished_before_register_return = []

    def register_spi(spi):
        api.spi = spi
        callback_thread = threading.Thread(
            target=lambda: (spi.OnFrontConnected(), callback_finished.set()), daemon=True
        )
        callback_thread.start()
        callback_finished_before_register_return.append(callback_finished.wait(_WAIT_SECONDS))

    api.RegisterSpi = register_spi
    client = _install_api(monkeypatch, api)
    client._start_join_observer = lambda _api: False
    errors = []
    thread = _start_in_thread(client, errors)
    thread.join(2.0)

    assert not thread.is_alive()
    assert errors == []
    assert callback_finished_before_register_return == [True]
    assert len(api.login_calls) == 1
    assert api.fronts == ["tcp://md.example:123"]
    assert api.init_calls == 1


def test_stop_defers_release_until_blocked_register_spi_returns(monkeypatch):
    api = _StartupApi()
    register_entered = threading.Event()
    allow_register_return = threading.Event()

    def register_spi(spi):
        api.spi = spi
        if spi is not None:
            register_entered.set()
            assert allow_register_return.wait(_WAIT_SECONDS)
        else:
            api.register_none_calls += 1

    api.RegisterSpi = register_spi
    client = _install_api(monkeypatch, api)
    errors = []
    start_thread = _start_in_thread(client, errors)
    assert register_entered.wait(_WAIT_SECONDS)
    assert client._startup_native_call_refs == {id(api): 1}

    stop_thread = threading.Thread(target=client.stop, daemon=True)
    stop_thread.start()
    stop_thread.join(0.5)
    assert not stop_thread.is_alive(), "stop must not wait for vendor RegisterSpi"
    assert api.register_none_calls == 0
    assert api.release_calls == 0
    assert api.fronts == []
    assert api.init_calls == 0
    assert client._startup_native_call_refs == {id(api): 1}
    deferred = client._deferred_startup_cleanups[id(api)]
    assert deferred[0] is api
    assert deferred[1] is not None
    assert deferred[2] is False

    allow_register_return.set()
    start_thread.join(2.0)
    assert not start_thread.is_alive()
    assert errors == []
    assert api.register_none_calls == 1
    assert api.release_calls == 1
    assert api.fronts == []
    assert api.init_calls == 0
    assert client._startup_native_call_refs == {}
    assert client._deferred_startup_cleanups == {}


def test_stop_during_init_waits_for_join_before_release(monkeypatch):
    api = _StartupApi()
    init_entered = threading.Event()
    allow_init_return = threading.Event()
    join_entered = threading.Event()
    allow_join_return = threading.Event()
    released = threading.Event()

    def init():
        api.init_calls += 1
        init_entered.set()
        assert allow_init_return.wait(_WAIT_SECONDS)

    def join():
        api.join_calls += 1
        join_entered.set()
        assert allow_join_return.wait(_WAIT_SECONDS)
        return 0

    api.Init = init
    api.Join = join
    api.Release = lambda: (setattr(api, "release_calls", api.release_calls + 1), released.set())
    client = _install_api(monkeypatch, api)
    errors = []
    start_thread = _start_in_thread(client, errors)
    assert init_entered.wait(_WAIT_SECONDS)
    assert client._startup_native_call_refs == {id(api): 1}

    stop_thread = threading.Thread(target=client.stop, daemon=True)
    stop_thread.start()
    stop_thread.join(0.5)
    assert not stop_thread.is_alive(), "stop must return while Init is blocked"
    assert api.register_none_calls == 0
    assert api.release_calls == 0
    deferred = client._deferred_startup_cleanups[id(api)]
    assert deferred[0] is api
    assert deferred[1] is not None
    assert deferred[2] is True

    allow_init_return.set()
    start_thread.join(2.0)
    assert not start_thread.is_alive()
    assert errors == []
    assert join_entered.wait(_WAIT_SECONDS)
    assert api.register_none_calls == 1
    assert api.release_calls == 0, "Release must wait for the sole Join call"

    allow_join_return.set()
    assert released.wait(_WAIT_SECONDS)
    assert api.join_calls == 1
    assert api.release_calls == 1
    assert client._startup_native_call_refs == {}
    assert client._deferred_startup_cleanups == {}


class _StartupAbort(BaseException):
    pass


def test_baseexception_from_init_is_propagated_after_safe_join_cleanup(monkeypatch):
    api = _StartupApi()
    released = threading.Event()
    api.Init = lambda: (_ for _ in ()).throw(_StartupAbort("startup-abort"))
    api.Release = lambda: (setattr(api, "release_calls", api.release_calls + 1), released.set())
    client = _install_api(monkeypatch, api)
    errors = []

    thread = _start_in_thread(client, errors)
    thread.join(2.0)

    assert not thread.is_alive()
    assert len(errors) == 1
    assert type(errors[0]) is _StartupAbort
    assert str(errors[0]) == "startup-abort"
    assert api.register_none_calls == 1
    assert api.join_calls == 1
    assert released.wait(_WAIT_SECONDS)
    assert api.release_calls == 1
    assert client._startup_native_call_refs == {}
    assert client._deferred_startup_cleanups == {}
