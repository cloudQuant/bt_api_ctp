from __future__ import annotations

import threading
from types import SimpleNamespace

import pytest

from bt_api_ctp.ctp.client import TraderClient, _TraderSpi


@pytest.mark.parametrize(
    ("callback_name", "push_name", "event_name", "field"),
    [
        (
            "OnRtnOrder",
            "_push_order_event",
            "wait_order_event",
            SimpleNamespace(OrderRef="105", OrderStatus="3"),
        ),
        (
            "OnRtnTrade",
            "_push_trade_event",
            "wait_trade_event",
            SimpleNamespace(TradeID="T105", OrderRef="105"),
        ),
    ],
)
def test_stale_order_and_trade_spi_events_are_dropped_after_api_swap(
    monkeypatch,
    callback_name,
    push_name,
    event_name,
    field,
):
    client = TraderClient("tcp://test", "9999", "sim-account", "secret")
    original_api = object()
    client._api = original_api
    spi = _TraderSpi(client, original_api)
    with client._query_state_lock:
        client._spi = spi

    observed_callbacks = []
    if callback_name == "OnRtnOrder":
        client.on_order = observed_callbacks.append
    else:
        client.on_trade = observed_callbacks.append

    original_push = getattr(client, push_name)
    push_entered = threading.Event()
    resume_push = threading.Event()

    def pause_before_push(*args, **kwargs):
        push_entered.set()
        if not resume_push.wait(timeout=5):
            raise AssertionError("test did not resume the event push")
        return original_push(*args, **kwargs)

    monkeypatch.setattr(client, push_name, pause_before_push)
    callback_errors = []

    def invoke_callback():
        try:
            getattr(spi, callback_name)(field)
        except BaseException as exc:  # surfaced in the main test thread
            callback_errors.append(exc)

    callback_thread = threading.Thread(target=invoke_callback)
    callback_thread.start()
    if not push_entered.wait(timeout=5):
        resume_push.set()
        callback_thread.join(timeout=5)
        pytest.fail("callback did not reach the deterministic check/push window")

    client._api = object()
    resume_push.set()
    callback_thread.join(timeout=5)

    assert not callback_thread.is_alive()
    assert callback_errors == []
    assert getattr(client, event_name)(timeout=0.01) is None
    assert observed_callbacks == []


@pytest.mark.parametrize(
    ("callback_name", "event_queue_name", "field"),
    [
        ("OnRtnOrder", "_order_events", SimpleNamespace(OrderRef="105")),
        ("OnRtnTrade", "_trade_events", SimpleNamespace(TradeID="T105")),
    ],
)
def test_buffered_event_is_drained_after_native_api_replacement(
    callback_name,
    event_queue_name,
    field,
):
    client = TraderClient("tcp://test", "9999", "sim-account", "secret")
    original_api = object()
    client._api = original_api
    spi = _TraderSpi(client, original_api)
    with client._query_state_lock:
        client._spi = spi

    getattr(spi, callback_name)(field)
    event_queue = getattr(client, event_queue_name)
    queued_entry = event_queue.get_nowait()
    assert not hasattr(queued_entry, "native_api")
    assert not hasattr(queued_entry, "spi")
    event_queue.put(queued_entry)

    client._api = object()

    assert client._order_events.empty()
    assert client._trade_events.empty()
    assert client.wait_order_event(timeout=0.01) is None
    assert client.wait_trade_event(timeout=0.01) is None


def test_buffered_error_event_is_drained_after_native_api_replacement():
    client = TraderClient("tcp://test", "9999", "sim-account", "secret")
    original_api = object()
    client._api = original_api
    spi = _TraderSpi(client, original_api)
    with client._query_state_lock:
        client._spi = spi

    spi.OnRspError(SimpleNamespace(ErrorID=33, ErrorMsg="old session"), 17, True)
    assert not client._error_events.empty()

    client._api = object()

    assert client._error_events.empty()
    assert client.wait_error_event(timeout=0.01) is None


@pytest.mark.parametrize(
    ("callback_name", "callback_args"),
    [
        (
            "OnRspOrderInsert",
            (
                SimpleNamespace(
                    BrokerID="9999",
                    InvestorID="sim-account",
                    UserID="sim-account",
                    InstrumentID="IF2701",
                    ExchangeID="SHFE",
                    OrderRef="105",
                    RequestID=17,
                ),
                SimpleNamespace(ErrorID=33, ErrorMsg="stale insert response"),
                17,
                True,
            ),
        ),
        (
            "OnErrRtnOrderInsert",
            (
                SimpleNamespace(
                    BrokerID="9999",
                    InvestorID="sim-account",
                    UserID="sim-account",
                    InstrumentID="IF2701",
                    ExchangeID="SHFE",
                    OrderRef="105",
                    RequestID=17,
                ),
                SimpleNamespace(ErrorID=33, ErrorMsg="stale insert error"),
            ),
        ),
        (
            "OnRspOrderAction",
            (
                SimpleNamespace(
                    BrokerID="9999",
                    InvestorID="sim-account",
                    OrderActionRef="41",
                    OrderRef="105",
                    RequestID=17,
                    FrontID=3,
                    SessionID=8,
                    ExchangeID="SHFE",
                    OrderSysID="SYS105",
                    ActionFlag="0",
                    InstrumentID="IF2701",
                ),
                SimpleNamespace(ErrorID=33, ErrorMsg="stale cancel response"),
                17,
                True,
            ),
        ),
        (
            "OnErrRtnOrderAction",
            (
                SimpleNamespace(
                    BrokerID="9999",
                    InvestorID="sim-account",
                    OrderActionRef="41",
                    OrderRef="105",
                    RequestID=17,
                    FrontID=3,
                    SessionID=8,
                    ExchangeID="SHFE",
                    OrderSysID="SYS105",
                    ActionFlag="0",
                    InstrumentID="IF2701",
                ),
                SimpleNamespace(ErrorID=33, ErrorMsg="stale cancel error"),
            ),
        ),
    ],
)
def test_stale_insert_or_cancel_diagnostic_is_not_admitted(
    monkeypatch,
    callback_name,
    callback_args,
):
    client = TraderClient("tcp://test", "9999", "sim-account", "secret")
    original_api = object()
    client._api = original_api
    spi = _TraderSpi(client, original_api)
    with client._query_state_lock:
        client._spi = spi

    observed_errors = []
    client.on_error = observed_errors.append
    original_push = client._push_error_event
    push_entered = threading.Event()
    resume_push = threading.Event()

    def pause_before_error_admission(*args, **kwargs):
        push_entered.set()
        if not resume_push.wait(timeout=5):
            raise AssertionError("test did not resume the error callback")
        return original_push(*args, **kwargs)

    monkeypatch.setattr(client, "_push_error_event", pause_before_error_admission)
    callback_errors = []

    def invoke_callback():
        try:
            getattr(spi, callback_name)(*callback_args)
        except BaseException as exc:  # surfaced in the main test thread
            callback_errors.append(exc)

    callback_thread = threading.Thread(target=invoke_callback)
    callback_thread.start()
    if not push_entered.wait(timeout=5):
        resume_push.set()
        callback_thread.join(timeout=5)
        pytest.fail("callback did not reach the deterministic error-admission window")

    client._api = object()
    resume_push.set()
    callback_thread.join(timeout=5)

    assert not callback_thread.is_alive()
    assert callback_errors == []
    assert client.wait_error_event(timeout=0.01) is None
    assert observed_errors == []


def test_startup_abort_does_not_invert_state_lock_with_spi_event(monkeypatch):
    class _NativeApi:
        def RegisterSpi(self, _spi):
            pass

        def Release(self):
            pass

    client = TraderClient("tcp://test", "9999", "sim-account", "secret")
    native_api = _NativeApi()
    client._api = native_api
    spi = _TraderSpi(client, native_api)
    generation = 7
    with client._query_state_lock:
        client._spi = spi
        client._starting_generation = generation
        client._lifecycle_generation = generation

    real_state_lock = client._query_state_lock
    callback_ident = []
    track_callback_attempt = threading.Event()
    callback_waiting_on_state = threading.Event()

    class _ObservedRLock:
        def __enter__(self):
            if (
                track_callback_attempt.is_set()
                and callback_ident
                and threading.get_ident() == callback_ident[0]
            ):
                callback_waiting_on_state.set()
            real_state_lock.acquire()
            return self

        def __exit__(self, *_args):
            real_state_lock.release()

    client._query_state_lock = _ObservedRLock()
    push_entered = threading.Event()
    resume_push = threading.Event()
    original_push = client._push_order_event

    def pause_before_push(*args, **kwargs):
        callback_ident.append(threading.get_ident())
        push_entered.set()
        if not resume_push.wait(timeout=5):
            raise AssertionError("test did not resume the order callback")
        return original_push(*args, **kwargs)

    monkeypatch.setattr(client, "_push_order_event", pause_before_push)
    callback_errors = []

    def invoke_callback():
        try:
            spi.OnRtnOrder(SimpleNamespace(OrderRef="105"))
        except BaseException as exc:  # surfaced in the main test thread
            callback_errors.append(exc)

    callback_thread = threading.Thread(target=invoke_callback, daemon=True)
    callback_thread.start()
    if not push_entered.wait(timeout=5):
        resume_push.set()
        pytest.fail("callback did not reach the state-lock inversion window")

    startup_abort_done = threading.Event()
    abort_errors = []

    def abort_while_owning_state_lock():
        try:
            with client._query_state_lock:
                track_callback_attempt.set()
                resume_push.set()
                if not callback_waiting_on_state.wait(timeout=5):
                    raise AssertionError("callback did not wait for the held state lock")
                assert client._abort_startup(native_api, spi, generation) is True
        except BaseException as exc:  # surfaced in the main test thread
            abort_errors.append(exc)
        finally:
            startup_abort_done.set()

    abort_thread = threading.Thread(target=abort_while_owning_state_lock, daemon=True)
    abort_thread.start()
    assert startup_abort_done.wait(timeout=5), "startup abort deadlocked with SPI dispatch"
    abort_thread.join(timeout=1)
    callback_thread.join(timeout=5)

    assert not abort_thread.is_alive()
    assert not callback_thread.is_alive()
    assert abort_errors == []
    assert callback_errors == []
    assert client.wait_order_event(timeout=0.01) is None


@pytest.mark.parametrize("operation", ["replace_api", "stop"])
def test_public_event_callback_can_wait_for_cross_thread_api_change(operation):
    class _NativeApi:
        def RegisterSpi(self, _spi):
            pass

        def Release(self):
            pass

    client = TraderClient("tcp://test", "9999", "sim-account", "secret")
    native_api = _NativeApi()
    client._api = native_api
    spi = _TraderSpi(client, native_api)
    with client._query_state_lock:
        client._spi = spi

    mutation_done = threading.Event()
    observed_callbacks = []
    callback_errors = []

    def replace_or_stop():
        try:
            if operation == "stop":
                client.stop()
            else:
                client._api = _NativeApi()
        except BaseException as exc:  # surfaced in the main test thread
            callback_errors.append(exc)
        finally:
            mutation_done.set()

    def on_order(order_field):
        observed_callbacks.append(order_field.OrderRef)
        mutation_thread = threading.Thread(target=replace_or_stop, daemon=True)
        mutation_thread.start()
        assert mutation_done.wait(timeout=5), "callback held a lock needed by API mutation"
        mutation_thread.join(timeout=1)
        assert not mutation_thread.is_alive()

    client.on_order = on_order
    callback_thread = threading.Thread(
        target=lambda: spi.OnRtnOrder(SimpleNamespace(OrderRef="105")),
        daemon=True,
    )
    callback_thread.start()
    callback_thread.join(timeout=5)

    assert not callback_thread.is_alive()
    assert callback_errors == []
    assert observed_callbacks == ["105"]
    # The callback was admitted before replacement, so it may finish. The
    # queued snapshot is generation-fenced and cannot be consumed afterward.
    assert client.wait_order_event(timeout=0.01) is None
