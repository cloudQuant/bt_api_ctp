"""Opt-in no-Join CTP shutdown policy regressions using fake native leaves."""

from __future__ import annotations

import threading
from types import SimpleNamespace
from typing import Any

import pytest

from bt_api_ctp.ctp import client as client_module
from bt_api_ctp.ctp.client import (
    CtpNativeStartupStopReceipt,
    CtpNativeStopReceipt,
    MdClient,
    TraderClient,
)


class _NativeApi:
    def __init__(
        self,
        *,
        block_init: bool = False,
        block_register_spi: bool = False,
        block_request: bool = False,
        block_release: bool = False,
        fail_release: bool = False,
        fail_init: bool = False,
        fail_front: bool = False,
        fail_register_spi: bool = False,
        fail_private_topic: bool = False,
        fail_public_topic: bool = False,
    ) -> None:
        self.calls: list[tuple[str, Any]] = []
        self._lock = threading.Lock()
        self._spi = None
        self.block_init = block_init
        self.block_register_spi = block_register_spi
        self.block_request = block_request
        self.block_release = block_release
        self.fail_release = fail_release
        self.fail_init = fail_init
        self.fail_front = fail_front
        self.fail_register_spi = fail_register_spi
        self.fail_private_topic = fail_private_topic
        self.fail_public_topic = fail_public_topic
        self.release_thread_ids: list[int] = []
        self.init_entered = threading.Event()
        self.init_returned = threading.Event()
        self.allow_init = threading.Event()
        self.register_spi_entered = threading.Event()
        self.allow_register_spi = threading.Event()
        self.request_entered = threading.Event()
        self.allow_request = threading.Event()
        self.release_entered = threading.Event()
        self.allow_release = threading.Event()
        self.release_returned = threading.Event()
        self.detached = threading.Event()
        self.join_entered = threading.Event()

    def _record(self, name: str, value: Any = None) -> None:
        with self._lock:
            self.calls.append((name, value))

    def RegisterSpi(self, spi: object | None) -> None:
        self._record("register_spi", spi)
        if spi is None:
            if self._spi is not None:
                assert any(
                    retained_api is self and retained_spi is self._spi
                    for retained_api, retained_spi, _ in client_module._RETIRED_CTP_NATIVE_SESSIONS
                )
            self.detached.set()
        else:
            self._spi = spi
            self.register_spi_entered.set()
            if self.block_register_spi:
                assert self.allow_register_spi.wait(3.0)
            if self.fail_register_spi:
                raise RuntimeError("fake_register_spi_failure")

    def RegisterFront(self, front: str) -> None:
        self._record("register_front", front)
        if self.fail_front:
            raise RuntimeError("fake_register_front_failure")

    def SubscribePrivateTopic(self, mode: int) -> None:
        self._record("private_topic", mode)
        if self.fail_private_topic:
            raise RuntimeError("fake_private_topic_failure")

    def SubscribePublicTopic(self, mode: int) -> None:
        self._record("public_topic", mode)
        if self.fail_public_topic:
            raise RuntimeError("fake_public_topic_failure")

    def Init(self) -> None:
        self._record("init")
        self.init_entered.set()
        try:
            if self.block_init:
                assert self.allow_init.wait(3.0)
            if self.fail_init:
                raise RuntimeError("fake_init_failure_after_entry")
        finally:
            self.init_returned.set()

    def Join(self) -> int:
        self._record("join")
        self.join_entered.set()
        return 0

    def Release(self) -> None:
        self._record("release")
        self.release_thread_ids.append(threading.get_ident())
        self.release_entered.set()
        if self.block_release:
            assert self.allow_release.wait(3.0)
        if self.fail_release:
            raise RuntimeError("fake_release_failure")
        self.release_returned.set()

    def SubscribeMarketData(self, instruments: list[str]) -> int:
        self._record("subscribe_market_data", tuple(instruments))
        self.request_entered.set()
        if self.block_request:
            assert self.allow_request.wait(3.0)
        return 0

    def UnSubscribeMarketData(self, instruments: list[str]) -> int:
        self._record("unsubscribe_market_data", tuple(instruments))
        return 0

    def ReqQryTradingAccount(self) -> int:
        self._record("query_account")
        self.request_entered.set()
        if self.block_request:
            assert self.allow_request.wait(3.0)
        return 0


@pytest.fixture(autouse=True)
def _isolate_native_session_registries():
    names = (
        "_RETIRED_CTP_NATIVE_SESSIONS",
        "_RELEASING_CTP_NATIVE_SESSION_API_IDS",
        "_CLAIMED_CTP_NATIVE_JOIN_API_IDS",
        "_RETURNED_CTP_NATIVE_JOIN_API_IDS",
        "_POISONED_CTP_NATIVE_SESSION_API_IDS",
        "_RELEASED_CTP_NATIVE_SESSION_APIS",
        "_RELEASED_CTP_NATIVE_SESSION_API_IDS",
    )
    with client_module._RETIRED_CTP_NATIVE_SESSIONS_LOCK:
        saved = {
            name: list(getattr(client_module, name))
            if name in ("_RETIRED_CTP_NATIVE_SESSIONS", "_RELEASED_CTP_NATIVE_SESSION_APIS")
            else set(getattr(client_module, name))
            for name in names
        }
        for name in names:
            getattr(client_module, name).clear()
    try:
        yield
    finally:
        with client_module._RETIRED_CTP_NATIVE_SESSIONS_LOCK:
            for name in names:
                target = getattr(client_module, name)
                target.clear()
                target.extend(saved[name]) if isinstance(target, list) else target.update(saved[name])


def _client_with_fake_api(monkeypatch, client_type, api: _NativeApi):
    monkeypatch.setattr(client_module, "_check_native_module", lambda: None)
    monkeypatch.setattr(client_module, "_flow_dir", lambda _name: "fake-flow")
    if client_type is MdClient:
        factory_name, create_name = "CThostFtdcMdApi", "CreateFtdcMdApi"
    else:
        factory_name, create_name = "CThostFtdcTraderApi", "CreateFtdcTraderApi"
    monkeypatch.setattr(
        client_module,
        factory_name,
        SimpleNamespace(**{create_name: lambda _flow: api}),
    )
    client = client_type("tcp://test", "9999", "account", "secret")
    return client


def _wait(event: threading.Event) -> None:
    assert event.wait(2.0)


@pytest.mark.parametrize("client_type", [MdClient, TraderClient], ids=["md", "trader"])
@pytest.mark.parametrize("invalid", [0, 1, None, "false"])
def test_join_observer_policy_requires_exact_bool_before_factory(
    monkeypatch, client_type, invalid
) -> None:
    client = client_type("tcp://test", "9999", "account", "secret")
    factory_calls: list[bool] = []
    factory_name = "CThostFtdcMdApi" if client_type is MdClient else "CThostFtdcTraderApi"
    create_name = "CreateFtdcMdApi" if client_type is MdClient else "CreateFtdcTraderApi"
    monkeypatch.setattr(
        client_module,
        factory_name,
        SimpleNamespace(**{create_name: lambda _flow: factory_calls.append(True)}),
    )

    with pytest.raises(ValueError, match="join_observer must be an exact bool"):
        client.start(block=False, join_observer=invalid)
    with pytest.raises(ValueError, match="join_observer=False cannot be combined"):
        client.start(block=True, join_observer=False)
    assert factory_calls == []


@pytest.mark.parametrize("client_type", [MdClient, TraderClient], ids=["md", "trader"])
def test_no_join_start_keeps_init_live_separate_and_releases_once(
    monkeypatch, client_type
) -> None:
    api = _NativeApi()
    client = _client_with_fake_api(monkeypatch, client_type, api)

    client.start(block=False, join_observer=False)

    assert client._native_init_started is True
    assert client._join_active is False
    assert [name for name, _ in api.calls].count("join") == 0
    assert client.stop_and_wait(timeout=2.0).complete
    assert [name for name, _ in api.calls].count("release") == 1
    assert [name for name, _ in api.calls].count("join") == 0
    assert client_module._RETIRED_CTP_NATIVE_SESSIONS == []


@pytest.mark.parametrize("client_type", [MdClient, TraderClient], ids=["md", "trader"])
def test_default_start_still_observes_join_and_releases_once(monkeypatch, client_type) -> None:
    api = _NativeApi()
    client = _client_with_fake_api(monkeypatch, client_type, api)

    client.start(block=False)
    spi = client._spi
    assert spi is not None
    assert api._spi is spi
    _wait(api.join_entered)
    receipt = client.stop_and_wait(timeout=2.0)

    assert type(receipt) is CtpNativeStopReceipt
    assert receipt.complete
    assert [name for name, _ in api.calls].count("join") == 1
    assert [name for name, _ in api.calls].count("release") == 1
    assert any(
        name == "register_spi" and value is spi for name, value in api.calls
    )
    assert any(
        retained_api is api and retained_spi is spi
        for retained_api, retained_spi, _ in client_module._RETIRED_CTP_NATIVE_SESSIONS
    ) is False


@pytest.mark.parametrize("client_type", [MdClient, TraderClient], ids=["md", "trader"])
def test_no_join_stop_and_wait_bounds_release_only_observation(
    monkeypatch, client_type
) -> None:
    api = _NativeApi(block_release=True)
    client = _client_with_fake_api(monkeypatch, client_type, api)
    client.start(block=False, join_observer=False)
    spi = client._spi
    assert spi is not None
    assert api._spi is spi

    pending = client.stop_and_wait(timeout=0.02)
    _wait(api.release_entered)
    assert type(pending) is CtpNativeStopReceipt
    assert pending.join_required is False
    assert pending.join_completed is True
    assert pending.native_released is False
    assert pending.thread_alive is False
    assert pending.timed_out is True
    assert pending.complete is False
    assert not api.release_returned.is_set()
    assert any(
        retained_api is api and retained_spi is spi
        for retained_api, retained_spi, _ in client_module._RETIRED_CTP_NATIVE_SESSIONS
    )
    assert [name for name, _ in api.calls].count("join") == 0

    api.allow_release.set()
    _wait(api.release_returned)
    complete = client.stop_and_wait(timeout=1.0)
    assert type(complete) is CtpNativeStopReceipt
    assert complete.complete
    assert complete.native_released is True
    assert [name for name, _ in api.calls].count("release") == 1
    assert [name for name, _ in api.calls].count("join") == 0


@pytest.mark.parametrize("client_type", [MdClient, TraderClient], ids=["md", "trader"])
def test_no_join_receipt_waits_for_exact_deferred_owner_to_clear(monkeypatch, client_type) -> None:
    api = _NativeApi()
    client = _client_with_fake_api(monkeypatch, client_type, api)
    client.start(block=False, join_observer=False)
    spi = client._spi
    real_release = client_module._release_retired_ctp_native_session_without_join
    release_confirmed = threading.Event()
    allow_owner_clear = threading.Event()

    def release_then_hold_owner(api_arg):
        result = real_release(api_arg)
        if api_arg is api and result:
            release_confirmed.set()
            assert allow_owner_clear.wait(3.0)
        return result

    monkeypatch.setattr(
        client_module,
        "_release_retired_ctp_native_session_without_join",
        release_then_hold_owner,
    )
    client.stop()
    _wait(release_confirmed)

    if client_type is MdClient:
        assert client._deferred_native_release == (api, spi)
    else:
        owner = client._callback_ingress_deferred_cleanup
        assert owner is not None and owner[0] is api and owner[1] is spi

    during_clear = client.stop_and_wait(timeout=0.02)
    assert type(during_clear) is CtpNativeStopReceipt
    assert during_clear.join_required is False
    assert during_clear.join_completed is True
    assert during_clear.native_released is True
    assert during_clear.timed_out is True
    assert during_clear.complete is False

    allow_owner_clear.set()
    complete = client.stop_and_wait(timeout=1.0)
    assert type(complete) is CtpNativeStopReceipt
    assert complete.complete
    assert [name for name, _ in api.calls].count("release") == 1
    assert [name for name, _ in api.calls].count("join") == 0
    if client_type is MdClient:
        assert client._deferred_native_release is None
    else:
        assert client._callback_ingress_deferred_cleanup is None


@pytest.mark.parametrize("client_type", [MdClient, TraderClient], ids=["md", "trader"])
def test_no_join_release_wait_from_callback_returns_incomplete_without_deadlock(
    monkeypatch, client_type
) -> None:
    api = _NativeApi()
    client = _client_with_fake_api(monkeypatch, client_type, api)
    client.start(block=False, join_observer=False)
    callback_observations = []

    def stop_inside_callback(_value) -> None:
        receipt = client.stop_and_wait(timeout=2.0)
        # This is evaluated before the callback lease exits, so it proves the
        # release worker did not race the callback's final instructions.
        callback_observations.append((receipt, api.release_entered.is_set()))

    if client_type is MdClient:
        client.on_tick = stop_inside_callback
        client._spi.OnRtnDepthMarketData(SimpleNamespace(InstrumentID="IF2612"))
    else:
        client.on_trade = stop_inside_callback
        client._spi.OnRtnTrade(SimpleNamespace(TradeID="fake"))

    assert len(callback_observations) == 1
    receipt, released_inside_callback = callback_observations[0]
    assert type(receipt) is CtpNativeStopReceipt
    assert receipt.join_required is False
    assert receipt.join_completed is True
    assert receipt.native_released is False
    assert receipt.timed_out is True
    assert receipt.complete is False
    assert released_inside_callback is False
    _wait(api.release_returned)
    assert [name for name, _ in api.calls].count("release") == 1
    assert [name for name, _ in api.calls].count("join") == 0


@pytest.mark.parametrize("client_type", [MdClient, TraderClient], ids=["md", "trader"])
def test_no_join_defers_release_until_callback_lease_returns(monkeypatch, client_type) -> None:
    api = _NativeApi()
    client = _client_with_fake_api(monkeypatch, client_type, api)
    client.start(block=False, join_observer=False)
    spi = client._spi
    callback_entered = threading.Event()
    allow_callback_return = threading.Event()

    def blocking_callback(_value) -> None:
        callback_entered.set()
        assert allow_callback_return.wait(3.0)

    if client_type is MdClient:
        client.on_tick = blocking_callback
        invoke_callback = lambda: spi.OnRtnDepthMarketData(
            SimpleNamespace(InstrumentID="IF2612")
        )
    else:
        client.on_trade = blocking_callback
        invoke_callback = lambda: spi.OnRtnTrade(SimpleNamespace(TradeID="fake"))

    callback = threading.Thread(target=invoke_callback)
    callback.start()
    _wait(callback_entered)
    client.stop()
    assert not api.detached.is_set()
    assert not api.release_entered.is_set()
    assert any(
        retained_api is api and retained_spi is spi
        for retained_api, retained_spi, _ in client_module._RETIRED_CTP_NATIVE_SESSIONS
    )

    allow_callback_return.set()
    callback.join(2.0)
    assert not callback.is_alive()
    _wait(api.release_returned)
    assert [name for name, _ in api.calls].count("release") == 1
    assert [name for name, _ in api.calls].count("join") == 0


def test_md_no_join_preinit_stop_waits_for_startup_and_callback_leases(monkeypatch) -> None:
    api = _NativeApi(block_register_spi=True)
    client = _client_with_fake_api(monkeypatch, MdClient, api)
    startup_errors: list[BaseException] = []

    def start() -> None:
        try:
            client.start(block=False, join_observer=False)
        except BaseException as exc:  # pragma: no cover - asserted below
            startup_errors.append(exc)

    starter = threading.Thread(target=start)
    starter.start()
    _wait(api.register_spi_entered)
    spi = client._spi
    assert spi is not None
    callback_entered = threading.Event()
    allow_callback_return = threading.Event()

    def blocking_callback(_tick) -> None:
        callback_entered.set()
        assert allow_callback_return.wait(3.0)

    client.on_tick = blocking_callback
    callback = threading.Thread(
        target=lambda: spi.OnRtnDepthMarketData(SimpleNamespace(InstrumentID="IF2612"))
    )
    callback.start()
    _wait(callback_entered)

    pending = client.stop_and_wait(timeout=0.02)
    assert type(pending) is CtpNativeStopReceipt
    assert pending.join_required is False
    assert pending.join_completed is True
    assert pending.native_released is False
    assert pending.thread_alive is False
    assert pending.timed_out is True
    assert pending.complete is False

    api.allow_register_spi.set()
    starter.join(2.0)
    assert not starter.is_alive()
    assert startup_errors == []
    assert not api.detached.is_set()
    assert not api.release_entered.is_set()
    assert [name for name, _ in api.calls].count("init") == 0
    assert [name for name, _ in api.calls].count("join") == 0

    allow_callback_return.set()
    callback.join(2.0)
    assert not callback.is_alive()
    _wait(api.release_returned)
    assert [name for name, _ in api.calls].count("release") == 1
    assert [name for name, _ in api.calls].count("join") == 0


@pytest.mark.parametrize("client_type", [MdClient, TraderClient], ids=["md", "trader"])
def test_no_join_defers_release_until_startup_call_returns(monkeypatch, client_type) -> None:
    api = _NativeApi(block_init=True)
    client = _client_with_fake_api(monkeypatch, client_type, api)
    errors: list[BaseException] = []

    def start() -> None:
        try:
            client.start(block=False, join_observer=False)
        except BaseException as exc:  # pragma: no cover - assertion below
            errors.append(exc)

    starter = threading.Thread(target=start)
    starter.start()
    _wait(api.init_entered)
    client.stop()
    assert not api.detached.is_set()
    assert not api.release_entered.is_set()

    api.allow_init.set()
    starter.join(2.0)
    assert not starter.is_alive()
    assert errors == []
    _wait(api.release_returned)
    assert [name for name, _ in api.calls].count("join") == 0
    assert [name for name, _ in api.calls].count("release") == 1


@pytest.mark.parametrize("client_type", [MdClient, TraderClient], ids=["md", "trader"])
def test_no_join_defers_release_until_request_lease_returns(monkeypatch, client_type) -> None:
    api = _NativeApi(block_request=True)
    client = _client_with_fake_api(monkeypatch, client_type, api)
    client.start(block=False, join_observer=False)
    if client_type is MdClient:
        client._loggedin = True
        submit = lambda: client.subscribe_additional(["IF2612"])
    else:
        submit = lambda: client._invoke_session_native_request(
            api, "ReqQryTradingAccount"
        )
    request = threading.Thread(target=submit)
    request.start()
    _wait(api.request_entered)

    client.stop()
    assert not api.detached.is_set()
    assert not api.release_entered.is_set()
    api.allow_request.set()
    request.join(2.0)
    assert not request.is_alive()
    _wait(api.release_returned)
    assert [name for name, _ in api.calls].count("join") == 0
    assert [name for name, _ in api.calls].count("release") == 1


@pytest.mark.parametrize("client_type", [MdClient, TraderClient], ids=["md", "trader"])
def test_no_join_startup_failure_releases_without_join(monkeypatch, client_type) -> None:
    api = _NativeApi(fail_front=True)
    client = _client_with_fake_api(monkeypatch, client_type, api)

    with pytest.raises(RuntimeError, match="fake_register_front_failure"):
        client.start(block=False, join_observer=False)

    _wait(api.release_returned)
    assert [name for name, _ in api.calls].count("join") == 0
    assert [name for name, _ in api.calls].count("release") == 1
    assert client_module._RETIRED_CTP_NATIVE_SESSIONS == []


@pytest.mark.parametrize("client_type", [MdClient, TraderClient], ids=["md", "trader"])
def test_no_join_init_exception_after_entry_releases_without_join(monkeypatch, client_type) -> None:
    api = _NativeApi(fail_init=True)
    client = _client_with_fake_api(monkeypatch, client_type, api)

    with pytest.raises(RuntimeError, match="fake_init_failure_after_entry"):
        client.start(block=False, join_observer=False)

    assert api.init_entered.is_set()
    assert api.init_returned.is_set()
    _wait(api.release_returned)
    names = [name for name, _ in api.calls]
    assert names.index("init") < names.index("register_spi", names.index("init") + 1)
    assert names.index("register_spi", names.index("init") + 1) < names.index("release")
    assert names.count("release") == 1
    assert names.count("join") == 0
    assert client_module._RETIRED_CTP_NATIVE_SESSIONS == []


@pytest.mark.parametrize("client_type", [MdClient, TraderClient], ids=["md", "trader"])
def test_no_join_spi_constructor_failure_releases_unpublished_api_without_join(
    monkeypatch, client_type
) -> None:
    api = _NativeApi()
    client = _client_with_fake_api(monkeypatch, client_type, api)

    def fail_spi(*_args):
        raise RuntimeError("fake_spi_constructor_failure")

    if client_type is MdClient:
        monkeypatch.setattr(client, "_create_md_spi", fail_spi)
    else:
        monkeypatch.setattr(client_module, "_TraderSpi", fail_spi)
    with pytest.raises(RuntimeError, match="fake_spi_constructor_failure"):
        client.start(block=False, join_observer=False)

    _wait(api.release_returned)
    assert [name for name, _ in api.calls].count("release") == 1
    assert [name for name, _ in api.calls].count("join") == 0
    assert client_module._RETIRED_CTP_NATIVE_SESSIONS == []


@pytest.mark.parametrize("client_type", [MdClient, TraderClient], ids=["md", "trader"])
def test_no_join_concurrent_stop_releases_exact_session_once(monkeypatch, client_type) -> None:
    api = _NativeApi(block_release=True)
    client = _client_with_fake_api(monkeypatch, client_type, api)
    client.start(block=False, join_observer=False)
    spi = client._spi
    barrier = threading.Barrier(3)
    errors: list[BaseException] = []

    def stop() -> None:
        barrier.wait()
        try:
            client.stop()
        except BaseException as exc:  # pragma: no cover - asserted below
            errors.append(exc)

    stoppers = [threading.Thread(target=stop), threading.Thread(target=stop)]
    for stopper in stoppers:
        stopper.start()
    barrier.wait()
    for stopper in stoppers:
        stopper.join(2.0)
        assert not stopper.is_alive()

    assert errors == []
    _wait(api.release_entered)
    assert [name for name, _ in api.calls].count("release") == 1
    assert [name for name, _ in api.calls].count("join") == 0
    assert any(
        retained_api is api and retained_spi is spi
        for retained_api, retained_spi, _ in client_module._RETIRED_CTP_NATIVE_SESSIONS
    )

    api.allow_release.set()
    _wait(api.release_returned)
    assert client.stop_and_wait(timeout=1.0).complete
    assert [name for name, _ in api.calls].count("release") == 1


@pytest.mark.parametrize("client_type", [MdClient, TraderClient], ids=["md", "trader"])
def test_no_join_failed_release_is_retained_and_never_retried(
    monkeypatch, client_type
) -> None:
    api = _NativeApi(fail_release=True)
    client = _client_with_fake_api(monkeypatch, client_type, api)
    client.start(block=False, join_observer=False)
    receipt = client.stop_and_wait(timeout=2.0)

    assert type(receipt) is CtpNativeStopReceipt
    assert receipt.join_required is False
    assert receipt.join_completed is True
    assert receipt.native_released is False
    assert receipt.timed_out is False
    assert receipt.complete is False
    assert api.release_entered.is_set()
    assert [name for name, _ in api.calls].count("release") == 1
    assert [name for name, _ in api.calls].count("join") == 0
    assert id(api) in client_module._POISONED_CTP_NATIVE_SESSION_API_IDS
    assert any(
        retained_api is api and retained_spi is not None
        for retained_api, retained_spi, _ in client_module._RETIRED_CTP_NATIVE_SESSIONS
    )

    client.stop()
    repeated = client.stop_and_wait(timeout=0.1)
    assert type(repeated) is CtpNativeStopReceipt
    assert repeated.complete is False
    assert [name for name, _ in api.calls].count("release") == 1
    assert [name for name, _ in api.calls].count("join") == 0


@pytest.mark.parametrize("client_type", [MdClient, TraderClient], ids=["md", "trader"])
def test_no_join_cleanup_worker_start_failure_stays_incomplete_without_retry(
    monkeypatch, client_type
) -> None:
    api = _NativeApi()
    client = _client_with_fake_api(monkeypatch, client_type, api)
    client.start(block=False, join_observer=False)
    spi = client._spi
    thread_start_attempts: list[str] = []

    def fail_cleanup_thread_start(thread, *args, **kwargs):
        thread_start_attempts.append(thread.name)
        raise RuntimeError("fake_cleanup_thread_start_failure")

    monkeypatch.setattr(threading.Thread, "start", fail_cleanup_thread_start)
    # A failed worker launch must retain custody and report an incomplete stop;
    # no synchronous Release or fallback Join is safe here.
    client.stop()
    if client_type is MdClient:
        first = client.stop_and_wait(timeout=0.01)
        assert type(first) is CtpNativeStopReceipt
        assert first.complete is False
        assert first.join_required is False
        assert first.join_completed is True
        assert first.native_released is False
        assert first.timed_out is True
    else:
        first = client.stop_startup_and_wait(timeout=0.01)
        assert type(first) is CtpNativeStartupStopReceipt
        assert first.complete is False
        assert first.outcome == "api_created_unconfirmed"
        assert first.join_required is False
        assert first.native_released is False
    assert api.release_entered.is_set() is False
    assert [name for name, _ in api.calls].count("release") == 0
    assert [name for name, _ in api.calls].count("join") == 0
    assert len(thread_start_attempts) == 1
    assert any(
        retained_api is api and retained_spi is spi
        for retained_api, retained_spi, _ in client_module._RETIRED_CTP_NATIVE_SESSIONS
    )

    client.stop()
    second = (
        client.stop_and_wait(timeout=0.01)
        if client_type is MdClient
        else client.stop_startup_and_wait(timeout=0.01)
    )
    assert second.complete is False
    assert len(thread_start_attempts) == 1
    assert [name for name, _ in api.calls].count("release") == 0


def _assert_released_startup_receipt(client: TraderClient, receipt: object) -> None:
    assert type(receipt) is CtpNativeStartupStopReceipt
    assert receipt.outcome == "api_created_released"
    assert receipt.complete is True
    assert receipt.api_attempt_id == client.native_startup_attempt.api_attempt_id
    assert receipt.connection_generation == 0
    assert receipt.native_released is True
    assert receipt.join_required is False
    assert receipt.join_completed is True
    assert receipt.thread_alive is False
    assert receipt.timed_out is False


def _release_count(api: _NativeApi) -> int:
    return [name for name, _ in api.calls].count("release")


_TRADER_PRE_INIT_FAILURES = {
    "register_spi": ("fail_register_spi", "fake_register_spi_failure"),
    "private_topic": ("fail_private_topic", "fake_private_topic_failure"),
    "public_topic": ("fail_public_topic", "fake_public_topic_failure"),
    "register_front": ("fail_front", "fake_register_front_failure"),
}


@pytest.mark.parametrize("step", sorted(_TRADER_PRE_INIT_FAILURES))
def test_trader_no_join_preinit_failure_releases_inline_with_complete_receipts(
    monkeypatch, step
) -> None:
    flag, message = _TRADER_PRE_INIT_FAILURES[step]
    api = _NativeApi(**{flag: True})
    client = _client_with_fake_api(monkeypatch, TraderClient, api)

    with pytest.raises(RuntimeError, match=message):
        client.start(block=False, join_observer=False)

    # Init was never entered, so no vendor callback thread exists and a no-Join
    # generation has no Join to race: the failing start released the exact
    # API on its own thread, after detaching the SPI, before it raised.
    assert api.release_returned.is_set()
    assert api.release_thread_ids == [threading.get_ident()]
    names = [name for name, _ in api.calls]
    assert names.count("init") == 0
    assert names.count("join") == 0
    assert names.count("release") == 1
    assert api.calls.index(("register_spi", None)) < names.index("release")
    assert client_module._RETIRED_CTP_NATIVE_SESSIONS == []
    assert client._callback_ingress_deferred_cleanup is None
    assert client._pending_native_join_api_ids == set()

    # Both receipts are complete without waiting.
    _assert_released_startup_receipt(client, client.stop_startup_and_wait(timeout=0))
    stop_receipt = client.stop_and_wait(timeout=0)
    assert type(stop_receipt) is CtpNativeStopReceipt
    assert stop_receipt.complete
    assert _release_count(api) == 1

    # The released attempt leaves no custody behind, so a retry starts at once.
    retry_api = _NativeApi()
    monkeypatch.setattr(
        client_module,
        "CThostFtdcTraderApi",
        SimpleNamespace(CreateFtdcTraderApi=lambda _flow: retry_api),
    )
    client.start(block=False, join_observer=False)
    assert client.stop_and_wait(timeout=2.0).complete
    assert _release_count(api) == 1
    assert _release_count(retry_api) == 1
    assert [name for name, _ in retry_api.calls].count("join") == 0


def test_trader_no_join_preinit_failure_with_ingress_owner_releases_inline_once(
    monkeypatch,
) -> None:
    from test_ctp_callback_ingress_client import _OwnerIntent, _session_binder, _Sink

    api = _NativeApi(fail_front=True)
    client = _client_with_fake_api(monkeypatch, TraderClient, api)
    owner = _OwnerIntent()
    client.install_callback_ingress_sink(
        owner,
        _Sink(),
        _session_binder,
        lambda verified_owner, binding: binding if verified_owner is owner else None,
    )
    started_threads: list[str] = []
    real_thread_start = threading.Thread.start

    def record_thread_start(thread, *args, **kwargs):
        started_threads.append(thread.name)
        return real_thread_start(thread, *args, **kwargs)

    monkeypatch.setattr(threading.Thread, "start", record_thread_start)

    with pytest.raises(RuntimeError, match="fake_register_front_failure"):
        client.start(block=False, join_observer=False)

    # Clearing ``_api`` with an ingress owner installed must not also schedule
    # the lease-drain worker: the inline release is the only Release owner.
    assert "bt-api-ctp-ingress-release" not in started_threads
    assert api.release_thread_ids == [threading.get_ident()]
    assert _release_count(api) == 1
    assert [name for name, _ in api.calls].count("init") == 0
    assert client._callback_ingress.poisoned is True
    assert client._callback_ingress_deferred_cleanup is None
    _assert_released_startup_receipt(client, client.stop_startup_and_wait(timeout=0))
    assert client.stop_and_wait(timeout=0).complete
    assert _release_count(api) == 1


def test_trader_no_join_stop_during_spi_construction_releases_inline(monkeypatch) -> None:
    api = _NativeApi()
    client = _client_with_fake_api(monkeypatch, TraderClient, api)
    real_spi = client_module._TraderSpi

    def stop_then_construct(owner, native_api):
        owner.stop()
        return real_spi(owner, native_api)

    monkeypatch.setattr(client_module, "_TraderSpi", stop_then_construct)

    # The stop cancels the reserved generation; start returns without error.
    client.start(block=False, join_observer=False)

    assert api.release_thread_ids == [threading.get_ident()]
    assert all(
        not (name == "register_spi" and value is not None) for name, value in api.calls
    )
    names = [name for name, _ in api.calls]
    assert names.count("init") == 0
    assert names.count("join") == 0
    assert names.count("release") == 1
    assert api.calls.index(("register_spi", None)) < names.index("release")
    assert client_module._RETIRED_CTP_NATIVE_SESSIONS == []
    _assert_released_startup_receipt(client, client.stop_startup_and_wait(timeout=0))
    assert client.stop_and_wait(timeout=0).complete
    assert _release_count(api) == 1


def test_trader_no_join_stop_before_init_without_call_in_flight_releases_inline(
    monkeypatch,
) -> None:
    api = _NativeApi()
    client = _client_with_fake_api(monkeypatch, TraderClient, api)
    real_run_startup_call = client._run_startup_call

    def stop_before_init(native_api, spi, generation, callback, **kwargs):
        if kwargs.get("starts_native_thread"):
            # Every registration call has returned; Init has not started.
            client.stop()
        return real_run_startup_call(native_api, spi, generation, callback, **kwargs)

    monkeypatch.setattr(client, "_run_startup_call", stop_before_init)

    client.start(block=False, join_observer=False)

    names = [name for name, _ in api.calls]
    assert names[:4] == ["register_spi", "private_topic", "public_topic", "register_front"]
    assert names.count("init") == 0
    assert names.count("join") == 0
    assert names.count("release") == 1
    assert api.calls.index(("register_spi", None)) < names.index("release")
    assert api.release_thread_ids == [threading.get_ident()]
    assert client_module._RETIRED_CTP_NATIVE_SESSIONS == []
    _assert_released_startup_receipt(client, client.stop_startup_and_wait(timeout=0))
    assert client.stop_and_wait(timeout=0).complete
    assert _release_count(api) == 1


@pytest.mark.parametrize("receipt_method", ["stop_startup_and_wait", "stop_and_wait"])
def test_trader_no_join_stop_during_preinit_call_receipt_waits_for_release(
    monkeypatch, receipt_method
) -> None:
    api = _NativeApi(block_register_spi=True, block_release=True)
    client = _client_with_fake_api(monkeypatch, TraderClient, api)
    errors: list[BaseException] = []

    def start() -> None:
        try:
            client.start(block=False, join_observer=False)
        except BaseException as exc:  # pragma: no cover - asserted below
            errors.append(exc)

    starter = threading.Thread(target=start)
    starter.start()
    _wait(api.register_spi_entered)

    client.stop()
    # RegisterSpi still holds its startup lease: no detach and no Release yet.
    assert not api.detached.is_set()
    assert not api.release_entered.is_set()

    api.allow_register_spi.set()
    starter.join(2.0)
    assert not starter.is_alive()
    assert errors == []
    # The final lease scheduled Release; hold it so the receipt must wait.
    _wait(api.release_entered)
    allow_release = threading.Timer(0.05, api.allow_release.set)
    allow_release.start()
    try:
        receipt = getattr(client, receipt_method)(timeout=2.0)
    finally:
        api.allow_release.set()
        allow_release.join()

    if receipt_method == "stop_startup_and_wait":
        _assert_released_startup_receipt(client, receipt)
    else:
        assert type(receipt) is CtpNativeStopReceipt
        assert receipt.complete
        assert receipt.join_required is False
    names = [name for name, _ in api.calls]
    assert names.count("init") == 0
    assert names.count("join") == 0
    assert names.count("release") == 1
    assert client._callback_ingress_deferred_cleanup is None


@pytest.mark.parametrize("receipt_method", ["stop_and_wait", "stop_startup_and_wait"])
def test_trader_no_join_after_init_receipt_completes_once_callback_lease_drains(
    monkeypatch, receipt_method
) -> None:
    api = _NativeApi()
    client = _client_with_fake_api(monkeypatch, TraderClient, api)
    client.start(block=False, join_observer=False)
    assert client._native_init_started is True
    assert client.connection_generation == 0
    spi = client._spi
    callback_entered = threading.Event()
    allow_callback_return = threading.Event()

    def blocking_callback(_value) -> None:
        callback_entered.set()
        assert allow_callback_return.wait(3.0)
        api._record("callback_returned")

    client.on_trade = blocking_callback
    callback = threading.Thread(target=lambda: spi.OnRtnTrade(SimpleNamespace(TradeID="fake")))
    callback.start()
    _wait(callback_entered)

    client.stop()
    assert not api.detached.is_set()
    assert not api.release_entered.is_set()
    drain = threading.Timer(0.05, allow_callback_return.set)
    drain.start()
    try:
        # A stop already detached the API; the receipt still waits for the
        # lease drain and the deferred Release instead of reporting custody.
        receipt = getattr(client, receipt_method)(timeout=2.0)
    finally:
        allow_callback_return.set()
        drain.join()
        callback.join(2.0)

    assert not callback.is_alive()
    if receipt_method == "stop_startup_and_wait":
        _assert_released_startup_receipt(client, receipt)
    else:
        assert type(receipt) is CtpNativeStopReceipt
        assert receipt.complete
        assert receipt.join_required is False
    names = [name for name, _ in api.calls]
    assert names.index("callback_returned") < names.index("release")
    assert names.count("release") == 1
    assert names.count("join") == 0


def test_simnow_trader_probe_starts_without_join_observer() -> None:
    from bt_api_ctp import simnow_probe

    script = simnow_probe._SIMNOW_PROBE_SCRIPT
    assert "client.start(block=False, join_observer=False)" in script
    assert script.count(".start(") == 1
