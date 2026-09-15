"""Regression coverage for native CTP director lifetime during shutdown."""

from __future__ import annotations

import threading
from types import SimpleNamespace
from typing import Any

import pytest

from bt_api_ctp.ctp import client as client_module
from bt_api_ctp.ctp.client import MdClient, TraderClient, _MdSpi


class _LiveJoinThread:
    def is_alive(self) -> bool:
        return True


class _NativeApi:
    def __init__(self, retained_spi: object | None = None) -> None:
        self.calls: list[tuple[str, Any]] = []
        self._retained_spi = retained_spi

    def RegisterSpi(self, spi: object | None) -> None:
        if spi is None and self._retained_spi is not None:
            assert any(
                item[1] is self._retained_spi for item in client_module._RETIRED_CTP_NATIVE_SESSIONS
            )
        self.calls.append(("register", spi))

    def Release(self) -> None:
        self.calls.append(("release", None))


class _BlockingNativeApi(_NativeApi):
    def __init__(self, retained_spi: object | None = None) -> None:
        super().__init__(retained_spi)
        self.join_started = threading.Event()
        self.allow_join_return = threading.Event()

    def SubscribePrivateTopic(self, _mode: int) -> None:
        return None

    def SubscribePublicTopic(self, _mode: int) -> None:
        return None

    def RegisterFront(self, _front: str) -> None:
        return None

    def Init(self) -> None:
        return None

    def Join(self) -> None:
        self.join_started.set()
        assert self.allow_join_return.wait(1.0)


class _StopDuringInitApi(_BlockingNativeApi):
    def __init__(self, stop_client, retained_spi: object | None = None) -> None:
        super().__init__(retained_spi)
        self._stop_client = stop_client

    def Init(self) -> None:
        self._stop_client()

    def Join(self) -> None:
        # stop() may run from Init() after the native side has created its
        # callback thread.  The client must observe this return so the
        # retained session can be released safely.
        self.join_started.set()


class _InitRaisesAfterStopApi(_BlockingNativeApi):
    """Raise from Init after another thread has requested stop."""

    def __init__(self, retained_spi: object | None = None) -> None:
        super().__init__(retained_spi)
        self.init_entered = threading.Event()
        self.allow_init_error = threading.Event()

    def Init(self) -> None:
        self.init_entered.set()
        assert self.allow_init_error.wait(1.0)
        raise RuntimeError("init_failed_after_stop")


class _RegisterSpiBarrierApi(_NativeApi):
    """Fake native API that holds the first SPI registration open."""

    def __init__(self, retained_spi: object | None = None) -> None:
        super().__init__(retained_spi)
        self.register_spi_entered = threading.Event()
        self.allow_register_spi_return = threading.Event()

    def RegisterSpi(self, spi: object | None) -> None:
        super().RegisterSpi(spi)
        if spi is not None:
            self.register_spi_entered.set()
            assert self.allow_register_spi_return.wait(1.0)

    def SubscribePrivateTopic(self, mode: int) -> None:
        self.calls.append(("private_topic", mode))

    def SubscribePublicTopic(self, mode: int) -> None:
        self.calls.append(("public_topic", mode))

    def RegisterFront(self, front: str) -> None:
        self.calls.append(("front", front))

    def Init(self) -> None:
        self.calls.append(("init", None))

    def Join(self) -> None:
        self.calls.append(("join", None))


class _CreateBarrier:
    """Delay native API creation until a concurrent stop is issued."""

    def __init__(self, api: _NativeApi) -> None:
        self.api = api
        self.entered = threading.Event()
        self.allow_return = threading.Event()

    def create(self, _flow: str) -> _NativeApi:
        self.entered.set()
        assert self.allow_return.wait(1.0)
        return self.api


class _FailBeforeInitApi(_NativeApi):
    """Fail before Init so retry cleanup must use the synchronous path."""

    def SubscribePrivateTopic(self, mode: int) -> None:
        self.calls.append(("private_topic", mode))

    def SubscribePublicTopic(self, mode: int) -> None:
        self.calls.append(("public_topic", mode))

    def RegisterFront(self, front: str) -> None:
        self.calls.append(("front", front))
        raise RuntimeError("front_registration_failed")


class _ImmediateJoinApi(_NativeApi):
    def __init__(self, retained_spi: object | None = None) -> None:
        super().__init__(retained_spi)
        self.join_returned = threading.Event()

    def SubscribePrivateTopic(self, mode: int) -> None:
        self.calls.append(("private_topic", mode))

    def SubscribePublicTopic(self, mode: int) -> None:
        self.calls.append(("public_topic", mode))

    def RegisterFront(self, front: str) -> None:
        self.calls.append(("front", front))

    def Init(self) -> None:
        self.calls.append(("init", None))

    def Join(self) -> None:
        self.join_returned.set()


@pytest.fixture(autouse=True)
def _isolate_retired_native_sessions():
    with client_module._RETIRED_CTP_NATIVE_SESSIONS_LOCK:
        original = list(client_module._RETIRED_CTP_NATIVE_SESSIONS)
        client_module._RETIRED_CTP_NATIVE_SESSIONS.clear()
    try:
        yield
    finally:
        with client_module._RETIRED_CTP_NATIVE_SESSIONS_LOCK:
            client_module._RETIRED_CTP_NATIVE_SESSIONS.clear()
            client_module._RETIRED_CTP_NATIVE_SESSIONS.extend(original)


def _install_live_md_session() -> tuple[MdClient, _NativeApi, object, _LiveJoinThread]:
    client = MdClient("tcp://test", "9999", "account", "secret")
    spi = object()
    thread = _LiveJoinThread()
    api = _NativeApi(spi)
    client._api = api
    client._spi = spi
    client._thread = thread
    client._join_active = True
    return client, api, spi, thread


def _install_live_trader_session() -> tuple[TraderClient, _NativeApi, object, _LiveJoinThread]:
    client = TraderClient("tcp://test", "9999", "account", "secret")
    spi = object()
    thread = _LiveJoinThread()
    api = _NativeApi(spi)
    client._api = api
    client._spi = spi
    client._thread = thread
    client._join_active = True
    return client, api, spi, thread


def _patch_native_factory(
    monkeypatch: pytest.MonkeyPatch,
    api_factory_name: str,
    create,
) -> None:
    factory_method = (
        "CreateFtdcMdApi" if api_factory_name == "CThostFtdcMdApi" else "CreateFtdcTraderApi"
    )
    monkeypatch.setattr(
        client_module,
        api_factory_name,
        SimpleNamespace(**{factory_method: create}),
    )


@pytest.mark.parametrize(
    "install",
    [_install_live_md_session, _install_live_trader_session],
    ids=["md", "trader"],
)
def test_live_join_stop_unregisters_and_retains_swig_director(install) -> None:
    client, api, spi, thread = install()

    client.stop()

    assert api.calls == [("register", None)]
    assert client._api is None
    assert client._spi is None
    assert client._thread is None
    assert any(
        entry_api is api and entry_spi is spi and entry_thread is thread
        for entry_api, entry_spi, entry_thread in client_module._RETIRED_CTP_NATIVE_SESSIONS
    )


@pytest.mark.parametrize(
    "client",
    [
        MdClient("tcp://test", "9999", "account", "secret"),
        TraderClient("tcp://test", "9999", "account", "secret"),
    ],
    ids=["md", "trader"],
)
def test_join_returned_stop_releases_native_api_immediately(client) -> None:
    api = _NativeApi()
    client._api = api
    client._spi = object()
    client._join_active = False

    client.stop()

    assert api.calls == [("register", None), ("release", None)]
    assert client_module._RETIRED_CTP_NATIVE_SESSIONS == []


def test_stale_md_callback_is_fenced_after_live_join_stop() -> None:
    client = MdClient("tcp://test", "9999", "account", "secret")
    api = _NativeApi()
    spi = object.__new__(_MdSpi)
    spi._c = client
    spi._native_api = api
    client._api = api
    client._spi = spi
    client._thread = _LiveJoinThread()
    client._join_active = True
    seen = []
    client.on_tick = seen.append

    client.stop()
    spi.OnRtnDepthMarketData(SimpleNamespace(InstrumentID="SA2609"))

    assert seen == []


@pytest.mark.parametrize(
    ("client_factory", "api_factory_name", "spi_factory_name"),
    [
        (MdClient, "CThostFtdcMdApi", "_MdSpi"),
        (TraderClient, "CThostFtdcTraderApi", "_TraderSpi"),
    ],
    ids=["md", "trader"],
)
def test_nonblocking_start_marks_join_live_before_stop(
    monkeypatch: pytest.MonkeyPatch,
    client_factory,
    api_factory_name: str,
    spi_factory_name: str,
) -> None:
    spi = object()
    api = _BlockingNativeApi(spi)
    monkeypatch.setattr(client_module, "_check_native_module", lambda: None)
    monkeypatch.setattr(
        client_module,
        api_factory_name,
        SimpleNamespace(
            **{
                (
                    "CreateFtdcMdApi"
                    if api_factory_name == "CThostFtdcMdApi"
                    else "CreateFtdcTraderApi"
                ): lambda _flow: api
            }
        ),
    )
    monkeypatch.setattr(client_module, spi_factory_name, lambda *_args: spi)
    client = client_factory("tcp://test", "9999", "account", "secret")

    client.start(block=False)
    assert api.join_started.wait(1.0)
    client.stop()
    api.allow_join_return.set()

    retired = client_module._RETIRED_CTP_NATIVE_SESSIONS
    assert api.calls == [("register", spi), ("register", None)]
    assert any(entry_api is api and entry_spi is spi for entry_api, entry_spi, _ in retired)
    retired_thread = next(thread for entry_api, _, thread in retired if entry_api is api)
    assert retired_thread is not None
    retired_thread.join(1.0)
    assert not retired_thread.is_alive()
    assert api.calls == [("register", spi), ("register", None), ("release", None)]
    assert client_module._RETIRED_CTP_NATIVE_SESSIONS == []

    # The registry owns the completed native session exactly once; a second
    # logical stop cannot double-release it.
    client.stop()
    assert api.calls.count(("release", None)) == 1


@pytest.mark.parametrize(
    ("client_factory", "api_factory_name", "spi_factory_name"),
    [
        (MdClient, "CThostFtdcMdApi", "_MdSpi"),
        (TraderClient, "CThostFtdcTraderApi", "_TraderSpi"),
    ],
    ids=["md", "trader"],
)
def test_stop_during_init_releases_only_after_join_returns(
    monkeypatch: pytest.MonkeyPatch,
    client_factory,
    api_factory_name: str,
    spi_factory_name: str,
) -> None:
    spi = object()
    holder = {}
    api = _StopDuringInitApi(lambda: holder["client"].stop(), spi)
    monkeypatch.setattr(client_module, "_check_native_module", lambda: None)
    monkeypatch.setattr(
        client_module,
        api_factory_name,
        SimpleNamespace(
            **{
                (
                    "CreateFtdcMdApi"
                    if api_factory_name == "CThostFtdcMdApi"
                    else "CreateFtdcTraderApi"
                ): lambda _flow: api
            }
        ),
    )
    monkeypatch.setattr(client_module, spi_factory_name, lambda *_args: spi)
    holder["client"] = client_factory("tcp://test", "9999", "account", "secret")

    holder["client"].start(block=False)

    assert api.join_started.wait(1.0)
    assert api.calls == [("register", spi), ("register", None), ("release", None)]
    assert client_module._RETIRED_CTP_NATIVE_SESSIONS == []


@pytest.mark.parametrize(
    ("client_factory", "api_factory_name", "spi_factory_name"),
    [
        (MdClient, "CThostFtdcMdApi", "_MdSpi"),
        (TraderClient, "CThostFtdcTraderApi", "_TraderSpi"),
    ],
    ids=["md", "trader"],
)
def test_init_failure_after_concurrent_stop_still_observes_join(
    monkeypatch: pytest.MonkeyPatch,
    client_factory,
    api_factory_name: str,
    spi_factory_name: str,
) -> None:
    spi = object()
    api = _InitRaisesAfterStopApi(spi)
    errors: list[BaseException] = []
    monkeypatch.setattr(client_module, "_check_native_module", lambda: None)
    _patch_native_factory(monkeypatch, api_factory_name, lambda _flow: api)
    monkeypatch.setattr(client_module, spi_factory_name, lambda *_args: spi)
    client = client_factory("tcp://test", "9999", "account", "secret")

    def start_client() -> None:
        try:
            client.start(block=False)
        except BaseException as exc:
            errors.append(exc)

    start_thread = threading.Thread(target=start_client)
    start_thread.start()
    assert api.init_entered.wait(1.0)

    stop_thread = threading.Thread(target=client.stop)
    stop_thread.start()
    assert client._startup_cancel_event.wait(1.0)
    api.allow_init_error.set()
    start_thread.join(1.0)
    stop_thread.join(1.0)
    assert not start_thread.is_alive()
    assert not stop_thread.is_alive()
    assert len(errors) == 1
    assert isinstance(errors[0], RuntimeError)
    assert str(errors[0]) == "init_failed_after_stop"

    assert api.join_started.wait(1.0)
    retired_thread = next(
        thread
        for entry_api, _spi, thread in client_module._RETIRED_CTP_NATIVE_SESSIONS
        if entry_api is api
    )
    assert retired_thread is not None
    api.allow_join_return.set()
    retired_thread.join(1.0)

    assert not retired_thread.is_alive()
    assert api.calls == [("register", spi), ("register", None), ("release", None)]
    assert client_module._RETIRED_CTP_NATIVE_SESSIONS == []


@pytest.mark.parametrize(
    ("client_factory", "api_factory_name", "spi_factory_name"),
    [
        (MdClient, "CThostFtdcMdApi", "_MdSpi"),
        (TraderClient, "CThostFtdcTraderApi", "_TraderSpi"),
    ],
    ids=["md", "trader"],
)
def test_stop_before_first_register_spi_cancels_reserved_startup(
    monkeypatch: pytest.MonkeyPatch,
    client_factory,
    api_factory_name: str,
    spi_factory_name: str,
) -> None:
    api = _NativeApi()
    create_barrier = _CreateBarrier(api)
    spi = object()
    errors: list[BaseException] = []
    monkeypatch.setattr(client_module, "_check_native_module", lambda: None)
    _patch_native_factory(monkeypatch, api_factory_name, create_barrier.create)
    monkeypatch.setattr(client_module, spi_factory_name, lambda *_args: spi)
    client = client_factory("tcp://test", "9999", "account", "secret")

    def start_client() -> None:
        try:
            client.start(block=False)
        except BaseException as exc:  # pragma: no cover - assertion below
            errors.append(exc)

    start_thread = threading.Thread(target=start_client)
    start_thread.start()
    assert create_barrier.entered.wait(1.0)

    client.stop()
    create_barrier.allow_return.set()
    start_thread.join(1.0)

    assert not start_thread.is_alive()
    assert errors == []
    assert api.calls == [("register", None), ("release", None)]
    assert client_module._RETIRED_CTP_NATIVE_SESSIONS == []


@pytest.mark.parametrize(
    ("client_factory", "api_factory_name", "spi_factory_name"),
    [
        (MdClient, "CThostFtdcMdApi", "_MdSpi"),
        (TraderClient, "CThostFtdcTraderApi", "_TraderSpi"),
    ],
    ids=["md", "trader"],
)
def test_stop_during_first_register_spi_blocks_all_later_startup_calls(
    monkeypatch: pytest.MonkeyPatch,
    client_factory,
    api_factory_name: str,
    spi_factory_name: str,
) -> None:
    spi = object()
    api = _RegisterSpiBarrierApi()
    errors: list[BaseException] = []
    monkeypatch.setattr(client_module, "_check_native_module", lambda: None)
    _patch_native_factory(monkeypatch, api_factory_name, lambda _flow: api)
    monkeypatch.setattr(client_module, spi_factory_name, lambda *_args: spi)
    client = client_factory("tcp://test", "9999", "account", "secret")

    def start_client() -> None:
        try:
            client.start(block=False)
        except BaseException as exc:  # pragma: no cover - assertion below
            errors.append(exc)

    start_thread = threading.Thread(target=start_client)
    start_thread.start()
    assert api.register_spi_entered.wait(1.0)

    stop_thread = threading.Thread(target=client.stop)
    stop_thread.start()
    assert client._startup_cancel_event.wait(1.0)
    api.allow_register_spi_return.set()
    start_thread.join(1.0)
    stop_thread.join(1.0)

    assert not start_thread.is_alive()
    assert not stop_thread.is_alive()
    assert errors == []
    assert api.calls == [("register", spi), ("register", None), ("release", None)]
    assert client_module._RETIRED_CTP_NATIVE_SESSIONS == []


@pytest.mark.parametrize(
    ("client_factory", "api_factory_name", "spi_factory_name"),
    [
        (MdClient, "CThostFtdcMdApi", "_MdSpi"),
        (TraderClient, "CThostFtdcTraderApi", "_TraderSpi"),
    ],
    ids=["md", "trader"],
)
def test_pre_init_start_failures_and_retry_do_not_accumulate_retired_sessions(
    monkeypatch: pytest.MonkeyPatch,
    client_factory,
    api_factory_name: str,
    spi_factory_name: str,
) -> None:
    failed_apis = [_FailBeforeInitApi(), _FailBeforeInitApi()]
    success_api = _ImmediateJoinApi()
    api_queue = [*failed_apis, success_api]
    spi = object()
    monkeypatch.setattr(client_module, "_check_native_module", lambda: None)
    _patch_native_factory(monkeypatch, api_factory_name, lambda _flow: api_queue.pop(0))
    monkeypatch.setattr(client_module, spi_factory_name, lambda *_args: spi)
    client = client_factory("tcp://test", "9999", "account", "secret")

    for failed_api in failed_apis:
        with pytest.raises(RuntimeError, match="front_registration_failed"):
            client.start(block=False)
        assert failed_api.calls[-2:] == [("register", None), ("release", None)]
        assert client_module._RETIRED_CTP_NATIVE_SESSIONS == []

    client.start(block=False)
    assert success_api.join_returned.wait(1.0)
    client.stop()

    assert success_api.calls.count(("release", None)) == 1
    assert client_module._RETIRED_CTP_NATIVE_SESSIONS == []
