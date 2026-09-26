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

    def Join(self) -> int:
        self.join_started.set()
        assert self.allow_join_return.wait(1.0)
        return 0


class _StopDuringInitApi(_BlockingNativeApi):
    def __init__(self, stop_client, retained_spi: object | None = None) -> None:
        super().__init__(retained_spi)
        self._stop_client = stop_client

    def Init(self) -> None:
        self._stop_client()

    def Join(self) -> int:
        # stop() may run from Init() after the native side has created its
        # callback thread.  The client must observe this return so the
        # retained session can be released safely.
        self.join_started.set()
        return 0


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

    def Join(self) -> int:
        self.calls.append(("join", None))
        return 0


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

    def Join(self) -> int:
        self.join_returned.set()
        return 0


class _StopReceiptApi(_NativeApi):
    def __init__(self, retained_spi: object | None = None, *, release_raises: bool = False) -> None:
        super().__init__(retained_spi)
        self.join_entered = threading.Event()
        self.allow_join_return = threading.Event()
        self.release_raises = release_raises

    def Join(self) -> int:
        self.calls.append(("join", None))
        self.join_entered.set()
        assert self.allow_join_return.wait(2.0)
        return 0

    def Release(self) -> None:
        self.calls.append(("release", None))
        if self.release_raises:
            raise RuntimeError("release_failed")


def _install_receipt_join(client_type, *, release_raises: bool = False):
    client = client_type("tcp://test", "9999", "account", "secret")
    spi = object()
    api = _StopReceiptApi(spi, release_raises=release_raises)
    lock = client._state_lock if client_type is MdClient else client._query_state_lock
    with lock:
        client._api = api
        client._spi = spi
        client._join_active = True
        client._native_init_started = True
        client._connection_generation = 23
    observer = threading.Thread(target=client._join_native_api, args=(api,), daemon=True)
    with lock:
        client._thread = observer
    observer.start()
    assert api.join_entered.wait(1.0)
    return client, api, observer


@pytest.fixture(autouse=True)
def _isolate_retired_native_sessions():
    with client_module._RETIRED_CTP_NATIVE_SESSIONS_LOCK:
        original = list(client_module._RETIRED_CTP_NATIVE_SESSIONS)
        original_releasing = set(client_module._RELEASING_CTP_NATIVE_SESSION_API_IDS)
        original_claimed = set(client_module._CLAIMED_CTP_NATIVE_JOIN_API_IDS)
        original_returned = set(client_module._RETURNED_CTP_NATIVE_JOIN_API_IDS)
        original_poisoned = set(client_module._POISONED_CTP_NATIVE_SESSION_API_IDS)
        original_released_apis = list(client_module._RELEASED_CTP_NATIVE_SESSION_APIS)
        original_released_ids = set(client_module._RELEASED_CTP_NATIVE_SESSION_API_IDS)
        client_module._RETIRED_CTP_NATIVE_SESSIONS.clear()
        client_module._RELEASING_CTP_NATIVE_SESSION_API_IDS.clear()
        client_module._CLAIMED_CTP_NATIVE_JOIN_API_IDS.clear()
        client_module._RETURNED_CTP_NATIVE_JOIN_API_IDS.clear()
        client_module._POISONED_CTP_NATIVE_SESSION_API_IDS.clear()
        client_module._RELEASED_CTP_NATIVE_SESSION_APIS.clear()
        client_module._RELEASED_CTP_NATIVE_SESSION_API_IDS.clear()
    try:
        yield
    finally:
        with client_module._RETIRED_CTP_NATIVE_SESSIONS_LOCK:
            client_module._RETIRED_CTP_NATIVE_SESSIONS.clear()
            client_module._RETIRED_CTP_NATIVE_SESSIONS.extend(original)
            client_module._RELEASING_CTP_NATIVE_SESSION_API_IDS.clear()
            client_module._RELEASING_CTP_NATIVE_SESSION_API_IDS.update(original_releasing)
            client_module._CLAIMED_CTP_NATIVE_JOIN_API_IDS.clear()
            client_module._CLAIMED_CTP_NATIVE_JOIN_API_IDS.update(original_claimed)
            client_module._RETURNED_CTP_NATIVE_JOIN_API_IDS.clear()
            client_module._RETURNED_CTP_NATIVE_JOIN_API_IDS.update(original_returned)
            client_module._POISONED_CTP_NATIVE_SESSION_API_IDS.clear()
            client_module._POISONED_CTP_NATIVE_SESSION_API_IDS.update(original_poisoned)
            client_module._RELEASED_CTP_NATIVE_SESSION_APIS.clear()
            client_module._RELEASED_CTP_NATIVE_SESSION_APIS.update(original_released_apis)
            client_module._RELEASED_CTP_NATIVE_SESSION_API_IDS.clear()
            client_module._RELEASED_CTP_NATIVE_SESSION_API_IDS.update(original_released_ids)


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


@pytest.mark.parametrize(
    ("install", "pending_code", "api_factory_name", "create_method"),
    [
        (
            _install_live_md_session,
            "ctp_md_client_native_join_pending",
            "CThostFtdcMdApi",
            "CreateFtdcMdApi",
        ),
        (
            _install_live_trader_session,
            "ctp_trader_client_native_join_pending",
            "CThostFtdcTraderApi",
            "CreateFtdcTraderApi",
        ),
    ],
    ids=["md", "trader"],
)
def test_pending_join_blocks_restarting_the_same_client_until_join_returns(
    monkeypatch: pytest.MonkeyPatch,
    install,
    pending_code: str,
    api_factory_name: str,
    create_method: str,
) -> None:
    client, api, _spi, _thread = install()
    api.Join = lambda: 0

    client.stop()

    native_creation_attempts: list[str] = []

    def fail_create(_flow: str) -> None:
        native_creation_attempts.append("called")
        raise AssertionError("a pending Join must fence native API creation")

    monkeypatch.setattr(client_module, "_check_native_module", lambda: None)
    monkeypatch.setattr(
        client_module,
        api_factory_name,
        SimpleNamespace(**{create_method: fail_create}),
    )
    with pytest.raises(RuntimeError, match=pending_code):
        client.start(block=False)
    assert native_creation_attempts == []
    assert api.calls == [("register", None)]

    # The native API is released only after the retained session observes Join.
    client._join_native_api(api)
    assert api.calls == [("register", None), ("release", None)]
    assert client_module._RETIRED_CTP_NATIVE_SESSIONS == []

    # Once the Join observer has completed its release attempt, retry reserves
    # normally and no longer carries a false pending-session blocker.
    generation = client._reserve_start_generation()
    client._clear_start_reservation(generation)


@pytest.mark.parametrize(
    "install",
    [_install_live_md_session, _install_live_trader_session],
    ids=["md", "trader"],
)
def test_concurrent_join_observer_start_claims_api_once(install) -> None:
    client, api, _spi, _thread = install()
    # Model stop racing before an observer thread has been registered.
    client._thread = None
    join_entered = threading.Event()
    allow_join_return = threading.Event()
    join_calls: list[int] = []
    join_calls_lock = threading.Lock()

    def join() -> int:
        with join_calls_lock:
            join_calls.append(1)
        join_entered.set()
        assert allow_join_return.wait(1.0)
        return 0

    api.Join = join
    client.stop()
    assert id(api) in client._pending_native_join_api_ids

    start_barrier = threading.Barrier(3)
    results: list[bool] = []

    def start_observer() -> None:
        start_barrier.wait()
        results.append(client._start_join_observer(api))

    starters = [threading.Thread(target=start_observer) for _ in range(2)]
    for starter in starters:
        starter.start()
    start_barrier.wait()
    for starter in starters:
        starter.join(1.0)
        assert not starter.is_alive()

    assert results.count(True) == 1
    assert results.count(False) == 1
    assert join_entered.wait(1.0)
    assert len(join_calls) == 1
    assert id(api) in client._pending_native_join_api_ids
    assert client._start_join_observer(api) is False
    assert len(join_calls) == 1

    allow_join_return.set()
    join_thread = next(
        thread for entry_api, _, thread in client_module._RETIRED_CTP_NATIVE_SESSIONS
        if entry_api is api
    )
    join_thread.join(1.0)
    assert not join_thread.is_alive()
    assert api.calls == [("register", None), ("release", None)]
    assert id(api) not in client._pending_native_join_api_ids
    assert client_module._RETIRED_CTP_NATIVE_SESSIONS == []


@pytest.mark.parametrize(
    "install",
    [_install_live_md_session, _install_live_trader_session],
    ids=["md", "trader"],
)
def test_bounded_native_join_observation_reports_timeout_then_exit_code(install) -> None:
    client, api, _spi, _thread = install()
    join_entered = threading.Event()
    allow_join_return = threading.Event()

    def join() -> int:
        join_entered.set()
        assert allow_join_return.wait(1.0)
        return 23

    api.Join = join
    client.stop()
    observer = threading.Thread(target=client._join_native_api, args=(api,))
    observer.start()
    assert join_entered.wait(1.0)

    pending = client.wait_native_join(0.001)
    assert pending.join_call_finished is False
    assert pending.observation.state == "pending"

    allow_join_return.set()
    observer.join(1.0)
    assert not observer.is_alive()
    completed = client.wait_native_join(0.1)
    assert completed.join_call_finished is True
    assert completed.observation.state == "returned"
    assert completed.observation.return_code == 23
    assert completed.observation.error_type is None


def test_failed_native_join_is_reported_and_remains_fenced() -> None:
    client, api, _spi, _thread = _install_live_trader_session()

    def fail_join() -> int:
        raise RuntimeError("join_failed")

    api.Join = fail_join
    client.stop()
    errors: list[BaseException] = []

    def observe_join() -> None:
        try:
            client._join_native_api(api)
        except BaseException as exc:
            errors.append(exc)

    observer = threading.Thread(target=observe_join)
    observer.start()
    observer.join(1.0)

    assert not observer.is_alive()
    assert len(errors) == 1
    assert str(errors[0]) == "join_failed"
    failed = client.wait_native_join(0.1)
    assert failed.join_call_finished is True
    assert failed.observation.state == "failed"
    assert failed.observation.error_type == "RuntimeError"
    assert api.calls == [("register", None)]
    assert any(entry_api is api for entry_api, _, _ in client_module._RETIRED_CTP_NATIVE_SESSIONS)
    with pytest.raises(RuntimeError, match="ctp_trader_client_native_join_pending"):
        client._reserve_start_generation()


@pytest.mark.parametrize(
    ("install", "pending_code"),
    [
        (_install_live_md_session, "ctp_md_client_native_join_pending"),
        (_install_live_trader_session, "ctp_trader_client_native_join_pending"),
    ],
    ids=["md", "trader"],
)
def test_release_failure_after_join_keeps_retired_session_and_restart_fenced(
    install, pending_code: str
) -> None:
    client, api, spi, _thread = install()
    join_calls: list[int] = []

    def join() -> int:
        join_calls.append(1)
        return 0

    api.Join = join

    def fail_release() -> None:
        api.calls.append(("release", None))
        raise RuntimeError("release_failed")

    api.Release = fail_release
    client.stop()
    client._join_native_api(api)

    assert api.calls == [("register", None), ("release", None)]
    assert any(
        entry_api is api and entry_spi is spi
        for entry_api, entry_spi, _ in client_module._RETIRED_CTP_NATIVE_SESSIONS
    )
    assert id(api) in client._pending_native_join_api_ids
    assert id(api) not in client_module._RELEASING_CTP_NATIVE_SESSION_API_IDS
    assert id(api) in client_module._POISONED_CTP_NATIVE_SESSION_API_IDS
    assert client.wait_native_join(0.1).observation.state == "returned"
    assert join_calls == [1]
    assert client._start_join_observer(api) is False
    assert join_calls == [1]
    assert client.wait_native_join(0.1).observation.state == "returned"
    with pytest.raises(RuntimeError, match=pending_code):
        client._reserve_start_generation()

    # Release may have partially freed native state before it raised. Repeated
    # observer cleanup must not retry it, and repeated stop must remain inert.
    assert client_module._release_retired_ctp_native_session_after_join(api) is False
    client.stop()
    assert api.calls == [("register", None), ("release", None)]
    assert any(
        entry_api is api and entry_spi is spi
        for entry_api, entry_spi, _ in client_module._RETIRED_CTP_NATIVE_SESSIONS
    )
    assert id(api) in client._pending_native_join_api_ids
    with pytest.raises(RuntimeError, match=pending_code):
        client._reserve_start_generation()


@pytest.mark.parametrize(
    ("client_factory", "pending_code"),
    [
        (MdClient, "ctp_md_client_native_join_pending"),
        (TraderClient, "ctp_trader_client_native_join_pending"),
    ],
    ids=["md", "trader"],
)
def test_immediate_release_failure_poisons_api_and_fences_restart(
    client_factory, pending_code: str
) -> None:
    client = client_factory("tcp://test", "9999", "account", "secret")
    api = _NativeApi()
    spi = object()

    def fail_release() -> None:
        api.calls.append(("release", None))
        raise RuntimeError("release_failed")

    api.Release = fail_release
    client._api = api
    client._spi = spi
    client._thread = None
    client._join_active = False
    client._native_init_started = False

    client.stop()

    assert api.calls == [("register", None), ("release", None)]
    assert any(
        entry_api is api and entry_spi is spi
        for entry_api, entry_spi, _ in client_module._RETIRED_CTP_NATIVE_SESSIONS
    )
    assert id(api) in client_module._POISONED_CTP_NATIVE_SESSION_API_IDS
    assert id(api) in client._pending_native_join_api_ids
    assert client_module._claim_ctp_native_join(api) is False
    with pytest.raises(RuntimeError, match=pending_code):
        client._reserve_start_generation()

    assert client_module._release_ctp_native_api_once(api, spi=spi) is False
    client.stop()
    assert api.calls == [("register", None), ("release", None)]


@pytest.mark.parametrize("client_type", [MdClient, TraderClient], ids=["md", "trader"])
def test_stop_and_wait_returns_pending_then_complete_for_exact_join(client_type) -> None:
    client, api, observer = _install_receipt_join(client_type)

    pending = client.stop_and_wait(timeout=0.01)
    assert type(pending) is client_module.CtpNativeStopReceipt
    assert pending.connection_generation == 23
    assert pending.join_required is True
    assert pending.join_completed is False
    assert pending.native_released is False
    assert pending.thread_alive is True
    assert pending.timed_out is True
    assert pending.complete is False
    assert api.calls.count(("register", None)) == 1
    assert ("release", None) not in api.calls

    api.allow_join_return.set()
    completed = client.stop_and_wait(timeout=1.0)
    assert completed.connection_generation == 23
    assert completed.join_required is True
    assert completed.join_completed is True
    assert completed.native_released is True
    assert completed.thread_alive is False
    assert completed.timed_out is False
    assert completed.complete is True
    assert not observer.is_alive()
    assert api.calls.count(("join", None)) == 1
    assert api.calls.count(("release", None)) == 1
    assert client_module._RETIRED_CTP_NATIVE_SESSIONS == []


@pytest.mark.parametrize("client_type", [MdClient, TraderClient], ids=["md", "trader"])
def test_stop_and_wait_never_reports_release_failure_as_complete(client_type) -> None:
    client, api, observer = _install_receipt_join(client_type, release_raises=True)
    pending = client.stop_and_wait(timeout=0.01)
    assert pending.complete is False

    api.allow_join_return.set()
    receipt = client.stop_and_wait(timeout=1.0)
    assert not observer.is_alive()
    assert receipt.join_completed is True
    assert receipt.native_released is False
    assert receipt.thread_alive is False
    assert receipt.timed_out is False
    assert receipt.complete is False
    assert api.calls.count(("release", None)) == 1
    assert id(api) in client_module._POISONED_CTP_NATIVE_SESSION_API_IDS
    assert any(entry[0] is api for entry in client_module._RETIRED_CTP_NATIVE_SESSIONS)


@pytest.mark.parametrize("client_type", [MdClient, TraderClient], ids=["md", "trader"])
def test_stop_and_wait_tracks_native_join_without_python_observer(client_type) -> None:
    client = client_type("tcp://test", "9999", "account", "secret")
    api = _StopReceiptApi()
    lock = client._state_lock if client_type is MdClient else client._query_state_lock
    with lock:
        client._api = api
        client._join_active = True
        client._native_init_started = True
        client._connection_generation = 41

    pending = client.stop_and_wait(timeout=0.01)
    assert pending.join_required is True
    assert pending.join_completed is False
    assert pending.native_released is False
    assert pending.thread_alive is False
    assert pending.timed_out is True
    assert pending.complete is False
    assert ("join", None) not in api.calls

    errors: list[BaseException] = []

    def synchronous_join_caller() -> None:
        try:
            client._join_native_api(api)
        except BaseException as exc:
            errors.append(exc)

    caller = threading.Thread(target=synchronous_join_caller)
    caller.start()
    assert api.join_entered.wait(1.0)
    api.allow_join_return.set()
    caller.join(1.0)
    assert not caller.is_alive()
    assert errors == []

    unobserved = client.stop_and_wait(timeout=0.01)
    assert unobserved.join_completed is True
    assert unobserved.native_released is True
    assert unobserved.thread_alive is False
    assert unobserved.timed_out is False
    assert unobserved.complete is True
    assert api.calls.count(("join", None)) == 1
    assert api.calls.count(("release", None)) == 1


def test_runtime_shutdown_consumer_accepts_completed_sync_join_and_rejects_active_join(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from backtrader_runtime import ctp_native_shutdown

    monkeypatch.setattr(ctp_native_shutdown, "_STOP_WAIT_TIMEOUT_SECONDS", 0.01)

    completed_client = MdClient("tcp://test", "9999", "account", "secret")
    completed_api = _StopReceiptApi()
    completed_api.allow_join_return.set()
    with completed_client._state_lock:
        completed_client._api = completed_api
        completed_client._join_active = True
        completed_client._native_init_started = True
        completed_client._connection_generation = 52
    completed_client._join_native_api(completed_api)
    assert ctp_native_shutdown.stop_ctp_native_client(completed_client) is True

    active_client = MdClient("tcp://test", "9999", "account", "secret")
    active_api = _StopReceiptApi()
    with active_client._state_lock:
        active_client._api = active_api
        active_client._join_active = True
        active_client._native_init_started = True
        active_client._connection_generation = 53
    join_errors: list[BaseException] = []

    def _run_sync_join() -> None:
        try:
            active_client._join_native_api(active_api)
        except BaseException as exc:
            join_errors.append(exc)

    join_caller = threading.Thread(target=_run_sync_join)
    join_caller.start()
    assert active_api.join_entered.wait(1.0)
    assert ctp_native_shutdown.stop_ctp_native_client(active_client) is False
    active_api.allow_join_return.set()
    join_caller.join(1.0)
    assert not join_caller.is_alive()
    assert join_errors == []


@pytest.mark.parametrize("client_type", [MdClient, TraderClient], ids=["md", "trader"])
def test_stop_and_wait_reports_immediate_release_failure_without_retry(client_type) -> None:
    client = client_type("tcp://test", "9999", "account", "secret")
    api = _StopReceiptApi(release_raises=True)
    lock = client._state_lock if client_type is MdClient else client._query_state_lock
    with lock:
        client._api = api
        client._connection_generation = 31

    receipt = client.stop_and_wait(timeout=0.1)
    assert receipt.connection_generation == 31
    assert receipt.join_required is False
    assert receipt.join_completed is True
    assert receipt.native_released is False
    assert receipt.thread_alive is False
    assert receipt.timed_out is False
    assert receipt.complete is False
    assert api.calls == [("register", None), ("release", None)]

    repeated = client.stop_and_wait(timeout=0.1)
    assert repeated == receipt
    assert api.calls == [("register", None), ("release", None)]


def test_stop_and_wait_validates_finite_nonnegative_timeout_and_idle_client() -> None:
    client = TraderClient("tcp://test", "9999", "account", "secret")
    for invalid in (True, float("nan"), float("inf"), -0.1, "1"):
        with pytest.raises(ValueError, match="invalid CTP native stop timeout"):
            client.stop_and_wait(invalid)

    receipt = client.stop_and_wait(timeout=0)
    assert receipt.connection_generation == 0
    assert receipt.join_required is False
    assert receipt.join_completed is True
    assert receipt.native_released is True
    assert receipt.thread_alive is False
    assert receipt.timed_out is False
    assert receipt.complete is True


def test_concurrent_immediate_release_does_not_repeat_or_leave_pending_fence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = _NativeApi()
    spi = object()
    state_lock = threading.RLock()
    pending_api_ids: set[int] = set()
    release_committed = threading.Event()
    allow_first_caller_to_return = threading.Event()
    original_release_once = client_module._release_ctp_native_api_once
    first_result: list[bool] = []
    second_result: list[bool] = []

    def pause_after_release_commit(*args: Any, **kwargs: Any) -> bool:
        released = original_release_once(*args, **kwargs)
        if released:
            release_committed.set()
            assert allow_first_caller_to_return.wait(1.0)
        return released

    monkeypatch.setattr(
        client_module, "_release_ctp_native_api_once", pause_after_release_commit
    )
    first = threading.Thread(
        target=lambda: first_result.append(
            client_module._release_ctp_native_api_immediately(
                api,
                spi,
                state_lock=state_lock,
                pending_api_ids=pending_api_ids,
            )
        )
    )
    first.start()
    assert release_committed.wait(1.0)
    assert id(api) in pending_api_ids

    second = threading.Thread(
        target=lambda: second_result.append(
            client_module._release_ctp_native_api_immediately(
                api,
                spi,
                state_lock=state_lock,
                pending_api_ids=pending_api_ids,
            )
        )
    )
    second.start()
    second.join(timeout=1.0)
    assert not second.is_alive()
    assert second_result == [False]
    assert [call for call in api.calls if call == ("release", None)] == [("release", None)]
    assert [call for call in api.calls if call == ("register", None)] == [("register", None)]
    # The second caller observes the committed successful Release and may
    # safely clear the fence even while the first Python frame is returning.
    assert pending_api_ids == set()

    allow_first_caller_to_return.set()
    first.join(timeout=1.0)
    assert not first.is_alive()
    assert first_result == [True]
    assert pending_api_ids == set()
    assert [call for call in api.calls if call == ("release", None)] == [("release", None)]
    assert [call for call in api.calls if call == ("register", None)] == [("register", None)]


def test_immediate_release_retry_after_pending_clear_does_not_readd_fence() -> None:
    api = _NativeApi()
    spi = object()
    state_lock = threading.RLock()
    pending_api_ids: set[int] = set()

    assert client_module._release_ctp_native_api_immediately(
        api,
        spi,
        state_lock=state_lock,
        pending_api_ids=pending_api_ids,
    ) is True
    assert pending_api_ids == set()

    assert client_module._release_ctp_native_api_immediately(
        api,
        spi,
        state_lock=state_lock,
        pending_api_ids=pending_api_ids,
    ) is False
    assert pending_api_ids == set()
    assert [call for call in api.calls if call == ("release", None)] == [("release", None)]
    assert [call for call in api.calls if call == ("register", None)] == [("register", None)]


@pytest.mark.parametrize("timeout", [-0.1, float("inf"), float("nan"), 60.1, True])
def test_native_join_wait_rejects_unbounded_or_invalid_timeouts(timeout) -> None:
    client = TraderClient("tcp://test", "9999", "account", "secret")

    with pytest.raises(ValueError, match="ctp_native_join_wait_timeout_out_of_range"):
        client.wait_native_join(timeout)


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
