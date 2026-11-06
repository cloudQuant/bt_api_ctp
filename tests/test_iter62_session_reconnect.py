"""Iteration 62 continuity-7: CTP session recovery after a front disconnect.

SimNow 2026-11-06: a direct-mode process ran from the day session into the
night session.  Its market-data front disconnected after the close (4097), the
replacement session's login was refused by the client itself (``error_id=0``)
and nothing ever logged in again; no tick reached the strategy, and the trader
session never showed the new trading day.

Covered here (all UNIT/FAKE or INTEGRATION_SIM: the real ``MdClient`` /
``TraderClient`` / ``CtpMarketStream`` / provider / direct adapter run over a
fake vendor API whose callbacks are serialised on their own thread like the
native library; no CTP library, front, account or network is involved):

* a transient login refusal -- the front is not ready yet -- is retried on the
  same link with bounded backoff, and every reconnect logs in again;
* a refusal naming another account, or a credential refusal, is final;
* a re-login with a new trading day is adopted (MD and trader);
* the direct market stream survives a disconnect, a closed front for longer
  than the native start deadline, a refused first login and a new trading day,
  and resumes on the new session under a new epoch without a restart;
* nothing of the lost session is delivered after the switch;
* 20 reconnect cycles leave no thread, retained native session or client.
"""

from __future__ import annotations

import gc
import logging
import queue
import threading
import time
import weakref
from types import SimpleNamespace

import pytest
from bt_api_base.market_protocol import MarketInstrumentKey
from bt_api_base.market_stream_protocol import MarketStreamRequest
from bt_api_base.market_stream_provider_protocol import (
    MarketStreamProviderContractError,
    SelectedMarketStreamRoute,
)

from bt_api_ctp import market_provider as market_provider_module
from bt_api_ctp.ctp import client as client_module
from bt_api_ctp.ctp import session_relogin
from bt_api_ctp.ctp.client import MdClient, TraderClient, _MdSpi, _TraderSpi
from bt_api_ctp.direct_write_provider import CtpDirectWriteProvider
from bt_api_ctp.feeds import live_ctp_feed
from bt_api_ctp.feeds.live_ctp_feed import CtpRequestDataFuture
from bt_api_ctp.market_provider import CtpQuoteV2MarketProvider
from bt_api_ctp.market_stream_provider import (
    SOURCE_RESTART_RECOVERY_CODE,
    CtpDirectMarketStreamAdapter,
)

DAY_1 = "20261106"
DAY_2 = "20261109"
TD_FRONT = "tcp://127.0.0.1:19801"
MD_FRONT = "tcp://127.0.0.1:19802"


# --------------------------------------------------------------------------
# manual timers (deterministic backoff checks)
# --------------------------------------------------------------------------


class _ManualTimer:
    def __init__(self, owner: _ManualTimers, delay, function, args=()):
        self.owner = owner
        self.delay = delay
        self.function = function
        self.args = args
        self.daemon = False
        self.cancelled = False
        self.fired = False

    def start(self) -> None:
        self.owner.armed.append(self)

    def cancel(self) -> None:
        self.cancelled = True

    def fire(self) -> None:
        assert not self.cancelled
        self.fired = True
        self.function(*self.args)


class _ManualTimers:
    def __init__(self) -> None:
        self.armed: list[_ManualTimer] = []

    def factory(self, delay, function, args=()):
        return _ManualTimer(self, delay, function, args)

    def live(self) -> list[_ManualTimer]:
        return [timer for timer in self.armed if not timer.cancelled and not timer.fired]


# --------------------------------------------------------------------------
# MdClient over an offline SPI (synchronous, no native thread)
# --------------------------------------------------------------------------


class _OfflineMdApi:
    def __init__(self) -> None:
        self.login_requests: list[int] = []
        self.subscriptions: list[list[str]] = []
        self.spi = None
        self.released = False

    def RegisterSpi(self, spi):
        self.spi = spi

    def ReqUserLogin(self, field, request_id):
        self.login_requests.append(request_id)
        return 0

    def SubscribeMarketData(self, instruments):
        self.subscriptions.append(list(instruments))
        return 0

    def Release(self):
        self.released = True


def _offline_md(*, retry: bool = True, allow_zero: bool = True):
    client = MdClient(MD_FRONT, "9999", "investor", "secret", allow_zero_login_response=allow_zero)
    api = _OfflineMdApi()
    spi = _MdSpi(client, native_api=api)
    client._api = api
    client._spi = spi
    timers = _ManualTimers()
    client._relogin_timer_factory = timers.factory
    client.login_retry_enabled = retry
    refusals: list[object] = []
    errors: list[object] = []
    client.on_login_failure = refusals.append
    client.on_error = errors.append
    client.subscribe(["rb2701", "hc2701"])
    return client, api, spi, timers, refusals, errors


def _md_answer(spi, *, day=DAY_1, broker="", user="", error_id=0, request_id=0):
    rsp = None if error_id else SimpleNamespace(BrokerID=broker, UserID=user, TradingDay=day)
    spi.OnRspUserLogin(
        rsp, SimpleNamespace(ErrorID=error_id, ErrorMsg="venue text"), request_id, True
    )


def test_md_refusal_with_error_id_zero_is_retried_on_the_same_link_with_backoff(caplog):
    """The SimNow case: a login answered without a valid trading day."""

    client, api, spi, timers, refusals, errors = _offline_md()
    spi.OnFrontConnected()
    assert len(api.login_requests) == 1
    with caplog.at_level(logging.WARNING, logger=client_module.__name__):
        _md_answer(spi, day="")  # error_id 0, rejected by the client itself
    assert client.is_ready is False
    assert [timer.delay for timer in timers.live()] == [1.0]
    assert refusals[-1].reason == "trading_day_invalid" and refusals[-1].retryable is True
    assert errors == []  # the refusal callback replaces on_error
    message = " ".join(record.getMessage() for record in caplog.records)
    assert "reason=trading_day_invalid" in message and "retry_in=1.0" in message
    assert "venue text" not in message

    timers.live()[0].fire()  # same link: a fresh login request
    assert len(api.login_requests) == 2
    _md_answer(spi, day="")
    assert [timer.delay for timer in timers.live()] == [2.0]

    timers.live()[0].fire()
    _md_answer(spi, day=DAY_1)
    assert client.is_ready is True
    assert client.login_trading_day == DAY_1
    assert api.subscriptions[-1] == ["rb2701", "hc2701"]  # resubscribed on login
    assert timers.live() == []


def test_md_backoff_is_bounded_and_capped_per_connection():
    delays = [session_relogin.relogin_delay(n) for n in range(1, 10)]
    assert delays == [1.0, 2.0, 5.0, 10.0, 20.0, 30.0, 60.0, 60.0, 60.0]
    cap = session_relogin.MAX_RELOGIN_ATTEMPTS_PER_CONNECTION
    assert session_relogin.relogin_delay(cap) == 60.0
    assert session_relogin.relogin_delay(cap + 1) is None

    client, api, spi, timers, _refusals, _errors = _offline_md()
    spi.OnFrontConnected()
    client._relogin_failures = cap  # the next refusal exceeds the cap
    _md_answer(spi, day="")
    assert timers.live() == []
    # A new connection counts afresh and logs in at once.
    spi.OnFrontDisconnected(4097)
    spi.OnFrontConnected()
    assert len(api.login_requests) == 2
    _md_answer(spi, day="")
    assert [timer.delay for timer in timers.live()] == [1.0]


@pytest.mark.parametrize(
    ("answer", "reason"),
    [
        ({"broker": "other-broker"}, "broker_id_mismatch"),
        ({"user": "other-investor"}, "user_id_mismatch"),
    ],
)
def test_md_login_naming_another_account_is_final(answer, reason):
    client, api, spi, timers, refusals, _errors = _offline_md()
    spi.OnFrontConnected()
    _md_answer(spi, **answer)
    assert client.is_ready is False
    assert client.active_md_identity is None
    assert refusals[-1].reason == reason and refusals[-1].retryable is False
    assert timers.live() == []


def test_md_credential_refusal_is_final_but_a_not_ready_front_is_retried():
    client, api, spi, timers, refusals, _errors = _offline_md()
    spi.OnFrontConnected()
    _md_answer(spi, error_id=3)  # INVALID_LOGIN: never repeated on the link
    assert refusals[-1].retryable is False and timers.live() == []

    spi.OnFrontDisconnected(4097)
    spi.OnFrontConnected()
    _md_answer(spi, error_id=7)  # NOT_INITED: the system is not ready yet
    assert refusals[-1].retryable is True
    assert [timer.delay for timer in timers.live()] == [1.0]


def test_md_disconnect_and_stop_void_a_pending_retry():
    client, api, spi, timers, _refusals, _errors = _offline_md()
    spi.OnFrontConnected()
    _md_answer(spi, day="")
    (timer,) = timers.live()
    spi.OnFrontDisconnected(4097)
    assert timer.cancelled is True
    # Even an uncancellable late fire cannot send on a lost link.
    timer.cancelled = False
    timer.fire()
    assert len(api.login_requests) == 1

    spi.OnFrontConnected()
    _md_answer(spi, day="")
    (timer,) = timers.live()
    client.stop()
    assert timer.cancelled is True
    assert api.released is True


def test_md_retry_is_off_unless_enabled():
    client, api, spi, timers, refusals, _errors = _offline_md(retry=False)
    spi.OnFrontConnected()
    _md_answer(spi, day="")
    assert refusals[-1].retryable is True
    assert timers.live() == []
    assert MdClient(MD_FRONT, "9999", "investor", "secret").login_retry_enabled is False


def test_md_reconnect_relogs_in_adopts_a_new_trading_day_and_resubscribes(caplog):
    client, api, spi, _timers, _refusals, _errors = _offline_md(allow_zero=False)
    spi.OnFrontConnected()
    _md_answer(spi, broker="9999", user="investor", request_id=api.login_requests[-1])
    assert client.login_trading_day == DAY_1
    assert api.subscriptions == [["rb2701", "hc2701"]]

    spi.OnFrontDisconnected(4097)
    assert client.login_trading_day is None
    spi.OnFrontConnected()  # the native API reconnected by itself
    assert len(api.login_requests) == 2
    with caplog.at_level(logging.INFO, logger=client_module.__name__):
        _md_answer(
            spi, day=DAY_2, broker="9999", user="investor", request_id=api.login_requests[-1]
        )
    assert client.is_ready is True
    assert client.login_trading_day == DAY_2
    assert client.active_md_identity.trading_day == DAY_2
    assert client.active_md_identity.connection_generation == 2
    assert api.subscriptions == [["rb2701", "hc2701"], ["rb2701", "hc2701"]]
    assert "trading_day_changed=True" in " ".join(r.getMessage() for r in caplog.records)


# --------------------------------------------------------------------------
# TraderClient over an offline SPI
# --------------------------------------------------------------------------


class _OfflineTraderApi:
    def __init__(self) -> None:
        self.calls: list[tuple[str, int]] = []

    def ReqAuthenticate(self, field, request_id):
        self.calls.append(("auth", request_id))
        return 0

    def ReqUserLogin(self, field, request_id):
        self.calls.append(("login", request_id))
        return 0


def _offline_trader(*, retry: bool = True):
    client = TraderClient(TD_FRONT, "9999", "investor", "secret", auto_settlement_confirm=False)
    api = _OfflineTraderApi()
    spi = _TraderSpi(client, api)
    client._api = api
    client._spi = spi
    client._query_interval = 0
    timers = _ManualTimers()
    client._session_retry_timer_factory = timers.factory
    client.session_retry_enabled = retry
    return client, api, spi, timers


def _last(api, kind):
    return [request_id for call, request_id in api.calls if call == kind][-1]


def _auth_answer(spi, api, *, error_id=0):
    spi.OnRspAuthenticate(
        SimpleNamespace(), SimpleNamespace(ErrorID=error_id, ErrorMsg="t"), _last(api, "auth"), True
    )


def _login_answer(spi, api, *, day=DAY_1, user="investor", broker="9999", error_id=0, session=5):
    spi.OnRspUserLogin(
        SimpleNamespace(
            BrokerID=broker,
            UserID=user,
            TradingDay=day,
            FrontID=1,
            SessionID=session,
            MaxOrderRef="0",
        ),
        SimpleNamespace(ErrorID=error_id, ErrorMsg="venue text"),
        _last(api, "login"),
        True,
    )


def _trader_connect_and_login(client, api, spi, *, day=DAY_1, session=5):
    spi.OnFrontConnected()
    _auth_answer(spi, api)
    _login_answer(spi, api, day=day, session=session)


def test_trader_relogin_after_reconnect_adopts_a_new_trading_day(caplog):
    client, api, spi, _timers = _offline_trader()
    _trader_connect_and_login(client, api, spi)
    assert client.get_session_state()["trading_day"] == DAY_1
    feed = CtpRequestDataFuture(
        broker_id="9999",
        user_id="investor",
        password="secret",
        td_front=TD_FRONT,
        md_front=MD_FRONT,
        auto_settlement_confirm=False,
    )
    feed._trader = client
    provider = CtpDirectWriteProvider(feed)

    with caplog.at_level(logging.INFO, logger=client_module.__name__):
        spi.OnFrontDisconnected(4097)
        assert provider.trading_day() is None
        spi.OnFrontConnected()  # authenticates again on every connection
        assert [call for call, _ in api.calls] == ["auth", "login", "auth"]
        _auth_answer(spi, api)
        _login_answer(spi, api, day=DAY_2, session=6)

    state = client.get_session_state()
    assert state["read_only_ready"] is True
    assert state["trading_day"] == DAY_2
    assert state["connection_generation"] == 2
    assert client.last_login_trading_day == DAY_2
    # The provider -- and through it R-3 -- sees the new day and generation.
    assert provider.trading_day() == DAY_2
    assert provider.session_generation() == 2
    messages = " ".join(record.getMessage() for record in caplog.records)
    assert "CTP trader front disconnected (reason=4097, generation=1)" in messages
    assert "trading_day_changed=True" in messages
    provider.close()


def test_trader_not_ready_refusals_are_retried_with_backoff_until_login():
    client, api, spi, timers = _offline_trader()
    spi.OnFrontConnected()
    _auth_answer(spi, api, error_id=7)  # NOT_INITED: before the night init
    assert [timer.delay for timer in timers.live()] == [1.0]
    timers.live()[0].fire()
    assert [call for call, _ in api.calls] == ["auth", "auth"]
    _auth_answer(spi, api)
    _login_answer(spi, api, error_id=8)  # FRONT_NOT_ACTIVE
    assert [timer.delay for timer in timers.live()] == [2.0]
    timers.live()[0].fire()
    assert [call for call, _ in api.calls] == ["auth", "auth", "login", "login"]
    _login_answer(spi, api, day=DAY_2)
    assert client.is_read_only_ready is True
    assert client.get_session_state()["trading_day"] == DAY_2
    assert timers.live() == []


@pytest.mark.parametrize(
    "refuse",
    [
        lambda spi, api: _auth_answer(spi, api, error_id=63),  # AUTH_FAILED
        lambda spi, api: (_auth_answer(spi, api), _login_answer(spi, api, error_id=3)),
        lambda spi, api: (_auth_answer(spi, api), _login_answer(spi, api, error_id=75)),
        lambda spi, api: (_auth_answer(spi, api), _login_answer(spi, api, user="someone-else")),
        lambda spi, api: (_auth_answer(spi, api), _login_answer(spi, api, broker="8888")),
    ],
)
def test_trader_credential_and_identity_refusals_are_final(refuse):
    client, api, spi, timers = _offline_trader()
    spi.OnFrontConnected()
    refuse(spi, api)
    assert client.is_read_only_ready is False
    assert client.get_session_state()["trading_day"] == ""
    assert timers.live() == []


def test_trader_disconnect_voids_a_pending_retry_and_retry_is_off_by_default():
    client, api, spi, timers = _offline_trader()
    spi.OnFrontConnected()
    _auth_answer(spi, api, error_id=7)
    (timer,) = timers.live()
    spi.OnFrontDisconnected(4097)
    assert timer.cancelled is True
    timer.cancelled = False
    timer.fire()  # a late fire on a lost link sends nothing
    assert [call for call, _ in api.calls] == ["auth"]

    plain, plain_api, plain_spi, plain_timers = _offline_trader(retry=False)
    plain_spi.OnFrontConnected()
    _auth_answer(plain_spi, plain_api, error_id=7)
    assert plain_timers.live() == []
    assert TraderClient(TD_FRONT, "9999", "investor", "secret").session_retry_enabled is False


def test_request_feed_session_enables_the_trader_retry(monkeypatch):
    made: list[TraderClient] = []

    class _Recorder(TraderClient):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            made.append(self)

        def start(self, *args, **kwargs):
            return None

        def wait_ready(self, timeout=None, **_kwargs):
            return False

    monkeypatch.setattr(live_ctp_feed.ctp_client, "TraderClient", _Recorder)
    feed = CtpRequestDataFuture(
        broker_id="9999",
        user_id="investor",
        password="secret",
        td_front=TD_FRONT,
        md_front=MD_FRONT,
        auto_settlement_confirm=False,
    )
    feed._connect_timeout = 0.01
    feed.connect()
    assert made and made[0].session_retry_enabled is True


def test_same_day_relogin_through_the_spi_is_a_readback_candidate_but_a_new_day_is_not():
    """I62-17 re-arm precondition after a real SPI reconnect; R-3 owns a new day."""

    client, api, spi, _timers = _offline_trader()
    _trader_connect_and_login(client, api, spi)
    feed = CtpRequestDataFuture(
        broker_id="9999",
        user_id="investor",
        password="secret",
        td_front=TD_FRONT,
        md_front=MD_FRONT,
        auto_settlement_confirm=False,
    )
    feed._trader = client
    provider = CtpDirectWriteProvider(feed)
    provider._ensure_readback_monitor = lambda: None  # no background thread here
    state = dict(client.get_session_state())
    state["trading_ready"] = True  # as after the startup settlement step
    provider._note_trading_ready(client, state)

    spi.OnFrontDisconnected(4097)
    _trader_connect_and_login(client, api, spi, session=6)  # same day
    assert provider._readback_candidate(client) == 2

    spi.OnFrontDisconnected(4097)
    _trader_connect_and_login(client, api, spi, day=DAY_2, session=7)
    assert provider._readback_candidate(client) is None  # left to R-3
    provider.close()


# --------------------------------------------------------------------------
# fake vendor MD API with a serialised callback thread
# --------------------------------------------------------------------------


class _MdFront:
    """The simulated venue side shared by every native MD API of one test."""

    def __init__(self, trading_day: str = DAY_1) -> None:
        self.trading_day = trading_day
        self.accepting = True  # a connection attempt reaches the front
        self.login_script: list[object] = []  # answers before "accept"
        self.market_open = True  # a subscription is followed by a tick
        self.apis: list[_FakeMdApi] = []
        self.lock = threading.Lock()


class _FakeMdApi:
    def __init__(self, front: _MdFront) -> None:
        self.front = front
        front.apis.append(self)
        self.spi = None
        self._events: queue.Queue = queue.Queue()
        self._thread: threading.Thread | None = None
        self.login_requests: list[int] = []
        self.subscriptions: list[list[str]] = []
        self.released = False
        self._volume: dict[str, int] = {}
        self._second = 0

    # vendor surface ------------------------------------------------------
    def RegisterSpi(self, spi):
        self.spi = spi

    def RegisterFront(self, _front):
        return None

    def Init(self):
        self._thread = threading.Thread(target=self._run, name="fake-ctp-md-callback", daemon=True)
        self._thread.start()
        if self.front.accepting:
            self._post(self._connected)

    def Release(self):
        self.released = True
        self._events.put(None)
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(2.0)

    def ReqUserLogin(self, _field, request_id):
        self.login_requests.append(request_id)
        with self.front.lock:
            answer = self.front.login_script.pop(0) if self.front.login_script else "accept"
        self._post(lambda: self._login_answer(answer))
        return 0

    def SubscribeMarketData(self, instruments):
        symbols = list(instruments)
        self.subscriptions.append(symbols)
        self._post(lambda: self._acks(symbols))
        return 0

    def UnSubscribeMarketData(self, _instruments):
        return 0

    # callback thread ------------------------------------------------------
    def _post(self, function) -> None:
        self._events.put(function)

    def _run(self) -> None:
        while True:
            function = self._events.get()
            if function is None:
                return
            if self.spi is not None:
                function()

    def _connected(self) -> None:
        self.spi.OnFrontConnected()

    def _login_answer(self, answer) -> None:
        day = self.front.trading_day
        if answer == "accept":
            rsp, info = SimpleNamespace(BrokerID="", UserID="", TradingDay=day), 0
        elif answer == "not_ready":  # error_id 0, no valid trading day (SimNow)
            rsp, info = SimpleNamespace(BrokerID="", UserID="", TradingDay=""), 0
        elif answer == "other_user":
            rsp, info = SimpleNamespace(BrokerID="", UserID="someone-else", TradingDay=day), 0
        else:
            rsp, info = None, int(answer)
        self.spi.OnRspUserLogin(rsp, SimpleNamespace(ErrorID=info, ErrorMsg="t"), 0, True)

    def _acks(self, symbols) -> None:
        for index, symbol in enumerate(symbols):
            self.spi.OnRspSubMarketData(
                SimpleNamespace(InstrumentID=symbol),
                SimpleNamespace(ErrorID=0),
                0,
                index == len(symbols) - 1,
            )
        if self.front.market_open:
            for symbol in symbols:
                self._tick(symbol)

    def _tick(self, symbol: str) -> None:
        self._second = (self._second + 1) % 60
        volume = self._volume.get(symbol, 100) + 3
        self._volume[symbol] = volume
        day = self.front.trading_day
        self.spi.OnRtnDepthMarketData(
            SimpleNamespace(
                InstrumentID=symbol,
                ExchangeID="SHFE",
                LastPrice=3_100.0,
                PreSettlementPrice=3_090.0,
                OpenPrice=3_095.0,
                HighestPrice=3_110.0,
                LowestPrice=3_080.0,
                BidPrice1=3_099.0,
                BidVolume1=5,
                AskPrice1=3_101.0,
                AskVolume1=6,
                OpenInterest=1_000.0,
                Volume=volume,
                Turnover=1.0,
                UpperLimitPrice=3_300.0,
                LowerLimitPrice=2_900.0,
                TradingDay=day,
                ActionDay=day,
                UpdateTime=f"21:00:{self._second:02d}",
                UpdateMillisec=0,
            )
        )

    # test controls (delivered on the callback thread, like the vendor) ---
    def drop(self, reason: int = 4097) -> None:
        self._post(lambda: self.spi.OnFrontDisconnected(reason))

    def reconnect(self) -> None:
        self._post(self._connected)

    def tick(self, symbol: str) -> None:
        self._post(lambda: self._tick(symbol))

    def settle(self, timeout: float = 2.0) -> None:
        """Wait until every callback queued so far was delivered."""
        if self._thread is None or not self._thread.is_alive():
            return
        done = threading.Event()
        self._post(done.set)
        assert done.wait(timeout)


@pytest.fixture
def md_front(monkeypatch):
    front = _MdFront()
    monkeypatch.setattr(client_module, "_check_native_module", lambda: None)
    monkeypatch.setattr(client_module, "_flow_dir", lambda _name: "offline-flow")
    monkeypatch.setattr(
        client_module.CThostFtdcMdApi,
        "CreateFtdcMdApi",
        staticmethod(lambda _flow: _FakeMdApi(front)),
    )
    # Real timers, short delays: the retry path itself runs as in production.
    monkeypatch.setattr(client_module, "relogin_delay", lambda n: 0.02 if n <= 360 else None)
    # The direct session's provider: a short native start deadline proves that
    # a closed front outlives it without failing the recovery.
    original = market_provider_module.CtpQuoteV2MarketProvider

    class _ShortDeadlineProvider(original):
        def __init__(self, **kwargs):
            kwargs["connect_timeout_seconds"] = 2.0
            super().__init__(**kwargs)

    monkeypatch.setattr(market_provider_module, "CtpQuoteV2MarketProvider", _ShortDeadlineProvider)
    yield front
    for api in front.apis:
        if not api.released:
            api.Release()


def _instrument(symbol: str = "rb2701") -> MarketInstrumentKey:
    return MarketInstrumentKey(
        provider="ctp",
        venue="SHFE",
        environment="simnow",
        product_type="future",
        instrument_id=symbol,
        native_symbol=symbol,
    )


def _direct_session(symbol: str = "rb2701"):
    """One direct session per route/instrument, as the SDK owner opens it."""
    feed = CtpRequestDataFuture(
        broker_id="9999",
        user_id="investor",
        password="secret",
        td_front=TD_FRONT,
        md_front=MD_FRONT,
        auto_settlement_confirm=False,
        ctp_env_profile="config_front_pair",
        ctp_environment="simnow",
    )
    route = SelectedMarketStreamRoute(
        exchange_name="CTP___FUTURE", client=feed, instrument=_instrument(symbol)
    )
    session = CtpDirectMarketStreamAdapter(route).open_session(wake_callback=lambda: None)
    binding = session.subscribe(
        MarketStreamRequest(exchange_name="CTP___FUTURE", instrument=_instrument(symbol))
    )
    return session, {symbol: binding}


def _poll(session, *, timeout: float = 3.0, want: int = 1):
    """Poll until ``want`` events arrived (a restart report raises)."""
    deadline = time.monotonic() + timeout
    events: list[object] = []
    while time.monotonic() < deadline and len(events) < want:
        events.extend(session.poll(timeout_ms=20, max_events=16))
    return events


def _outage(session) -> bool:
    """Whether the provider reports a lost source being replaced (or failed)."""
    provider = session._provider
    return any(
        provider.source_recovery_state(channel.handle) is not None
        for channel in session._channels.values()
    )


def _poll_until_restart(session, *, timeout: float = 5.0) -> None:
    """Poll until a confirmed restart holds the channels for a resync.

    Every poll of the outage before it fails as well (D62-38 point 7).
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            session.poll(timeout_ms=20, max_events=16)
        except MarketStreamProviderContractError:
            if any(channel.awaiting_resync for channel in session._channels.values()):
                return
            assert _outage(session)
    raise AssertionError("no source restart was reported")


def _wait(predicate, timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("condition not reached")


def _pump_until(session, predicate, timeout: float = 3.0) -> None:
    """Poll the session (the owner thread drives the recovery) until true.

    A poll during the outage fails (D62-38 point 7) and never delivers an event.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            assert session.poll(timeout_ms=10, max_events=8) == ()
        except MarketStreamProviderContractError:
            assert _outage(session)
        if predicate():
            return
    raise AssertionError("condition not reached")


def _provider(session) -> CtpQuoteV2MarketProvider:
    return session._provider


def test_direct_stream_resumes_after_a_same_day_reconnect_without_restart(md_front):
    session, bindings = _direct_session()
    binding = bindings["rb2701"]
    first = _poll(session)
    assert [event.cursor.sequence for event in first] == [1]
    old_api = md_front.apis[0]
    old_client = session._provider._native_client

    old_api.drop()
    _poll_until_restart(session)
    assert session.channel_recovery_code(binding) == SOURCE_RESTART_RECOVERY_CODE
    assert old_api.released is True
    new_api = md_front.apis[-1]
    assert new_api is not old_api
    assert new_api.subscriptions == [["rb2701"]]  # resubscribed on the new login

    with pytest.raises(MarketStreamProviderContractError):
        session.resync(binding, acknowledge_data_loss=False)
    rebound = session.resync(binding, acknowledge_data_loss=True)
    assert rebound.source.epoch != binding.source.epoch
    assert rebound.source.stream_id == binding.source.stream_id
    assert rebound.source.connection_generation > binding.source.connection_generation
    assert rebound.initial_cursor is None and rebound.recovery_required is False
    assert session.channel_recovery_code(rebound) is None

    # A late callback of the lost client is fenced: nothing old after the switch.
    old_client.on_tick(SimpleNamespace(InstrumentID="rb2701", LastPrice=1.0))
    new_api.tick("rb2701")
    events = _poll(session)
    assert [event.cursor.sequence for event in events] == [1]
    assert {event.cursor.epoch for event in events} == {rebound.source.epoch}
    assert events[0].source == rebound.source
    session.close()


def test_direct_stream_survives_the_closed_gap_a_refused_login_and_a_new_trading_day(md_front):
    """The SimNow 2026-11-06 sequence, end to end on the direct path."""

    session, bindings = _direct_session()
    binding = bindings["rb2701"]
    assert _poll(session)
    provider = _provider(session)
    old_api = md_front.apis[0]

    # 16:01 the front closes after the day session: the replacement still
    # reaches it and its logins are refused by the client (error_id 0).
    md_front.login_script = ["not_ready"] * 50
    md_front.market_open = False
    old_api.drop()
    _pump_until(session, lambda: len(md_front.apis) == 2 and md_front.apis[1].login_requests)
    replacement = md_front.apis[1]
    replacement.settle()
    _pump_until(session, lambda: len(replacement.login_requests) >= 2)  # same-link retry
    md_front.accepting = False
    replacement.drop()
    replacement.settle()

    # Closed for longer than the native start deadline: the recovery does not
    # fail, and every poll reports the outage (D62-38 point 7: DELAYED during a
    # disconnect) with no declared code, so a consumer cannot resync yet.
    for _ in range(3):
        time.sleep(0.8)
        with pytest.raises(MarketStreamProviderContractError):
            session.poll(timeout_ms=10, max_events=8)
        assert session.channel_recovery_code(binding) is None
    recovery = provider._source_recovery
    assert recovery is not None and recovery.state == "connecting"
    assert provider.source_recovery_state(session._channels[binding.token].handle) == "recovering"
    assert replacement.spi is not None and replacement.released is False

    # The night session: the front reopens with the next trading day.
    md_front.trading_day = DAY_2
    md_front.login_script = []
    md_front.accepting = True
    md_front.market_open = True
    replacement.reconnect()
    _poll_until_restart(session)
    assert len(md_front.apis) == 2  # the same replacement logged in again
    assert replacement.subscriptions[-1] == ["rb2701"]
    rebound = session.resync(binding, acknowledge_data_loss=True)
    replacement.tick("rb2701")
    events = _poll(session, want=2)
    assert [event.cursor.sequence for event in events] == [1, 2]
    first = events[0].data
    assert first.trading_date in (DAY_2, "2026-11-09")
    # D62-38: the first tick of the new scope is continuous for price, with a
    # volume baseline only.
    assert first.continuity_status == "continuous"
    assert first.volume_complete is False
    assert events[0].source == rebound.source
    session.close()


def test_the_outage_is_reported_on_every_poll_until_the_restart(md_front):
    """SimNow 2026-11-07: a feed stayed LIVE through a disconnect (D62-38 point 7).

    From the source loss to the confirmed restart every poll fails with no
    declared code (the SDK owner keeps the channel in recovery as
    ``PROVIDER_POLL_FAILED``, never resynced); the restart then declares
    ``PROVIDER_RESYNC_REQUIRED`` and an acknowledged resync resumes delivery.
    """
    session, bindings = _direct_session()
    binding = bindings["rb2701"]
    assert _poll(session)
    provider = _provider(session)
    handle = session._channels[binding.token].handle
    assert provider.source_recovery_state(handle) is None
    # The replacement reaches the front but its logins are refused as not ready.
    md_front.login_script = ["not_ready"] * 50
    md_front.apis[0].drop()
    _pump_until(session, lambda: len(md_front.apis) == 2 and md_front.apis[1].login_requests)
    replacement = md_front.apis[1]
    replacement.settle()
    for _ in range(5):
        with pytest.raises(MarketStreamProviderContractError):
            session.poll(timeout_ms=10, max_events=8)
        assert session.channel_recovery_code(binding) is None
        assert provider.source_recovery_state(handle) == "recovering"
    # The front is ready again: the next login succeeds and the source restarts.
    md_front.login_script = []
    replacement.reconnect()
    _poll_until_restart(session)
    assert session.channel_recovery_code(binding) == SOURCE_RESTART_RECOVERY_CODE
    assert provider.source_recovery_state(handle) is None
    rebound = session.resync(binding, acknowledge_data_loss=True)
    assert session.channel_recovery_code(rebound) is None
    replacement.tick("rb2701")
    events = _poll(session)
    assert events and events[0].cursor.sequence == 1 and events[0].source == rebound.source
    session.close()


def test_a_relogin_naming_another_account_fails_the_direct_stream_closed(md_front):
    session, bindings = _direct_session()
    binding = bindings["rb2701"]
    assert _poll(session)
    provider = _provider(session)
    md_front.login_script = ["other_user"]
    md_front.apis[0].drop()
    _pump_until(session, lambda: len(md_front.apis) == 2 and md_front.apis[1].login_requests)
    md_front.apis[1].settle()
    for _ in range(5):
        with pytest.raises(MarketStreamProviderContractError):
            session.poll(timeout_ms=10, max_events=8)  # the outage stays reported
        assert session.channel_recovery_code(binding) is None
    assert provider._source_recovery is not None
    assert provider._source_recovery.state == "failed"
    assert provider.source_recovery_state(session._channels[binding.token].handle) == "failed"
    assert md_front.apis[1].login_requests == [md_front.apis[1].login_requests[0]]
    with pytest.raises(MarketStreamProviderContractError):
        session.resync(binding, acknowledge_data_loss=True)
    assert session.close() is True


def test_a_replacement_lost_before_the_resync_restarts_the_recovery(md_front):
    session, bindings = _direct_session()
    binding = bindings["rb2701"]
    assert _poll(session)
    md_front.apis[0].drop()
    _poll_until_restart(session)
    first_replacement = md_front.apis[-1]
    # The new session is lost before the consumer took its epoch: nothing of
    # it may be acknowledged, and the recovery starts over from it.
    first_replacement.drop()
    first_replacement.settle()
    with pytest.raises(MarketStreamProviderContractError):
        session.resync(binding, acknowledge_data_loss=True)
    assert _provider(session)._source_recovery.state == "reset_pending"
    # The channel stays held (the owner keeps it in recovery); the consumer
    # retries its acknowledged resync while polls drive the new recovery.
    rebound = None
    deadline = time.monotonic() + 5.0
    while rebound is None and time.monotonic() < deadline:
        assert session.poll(timeout_ms=20, max_events=8) == ()
        try:
            rebound = session.resync(binding, acknowledge_data_loss=True)
        except MarketStreamProviderContractError:
            continue
    assert rebound is not None
    assert first_replacement.released is True
    second_replacement = md_front.apis[-1]
    assert second_replacement is not first_replacement
    assert rebound.source.connection_generation > binding.source.connection_generation
    second_replacement.tick("rb2701")
    events = _poll(session)
    assert events[0].cursor.sequence == 1 and events[0].source == rebound.source
    session.close()


def test_a_rebind_failing_after_the_acknowledgement_holds_the_channel_and_recovers(
    md_front, monkeypatch
):
    session, bindings = _direct_session()
    binding = bindings["rb2701"]
    assert _poll(session)
    md_front.apis[0].drop()
    _poll_until_restart(session)
    provider = _provider(session)
    describe = provider.describe_source
    failures: list[int] = []

    def fail_once(*args, **kwargs):
        if not failures:
            failures.append(1)
            return None
        return describe(*args, **kwargs)

    monkeypatch.setattr(provider, "describe_source", fail_once)
    with pytest.raises(MarketStreamProviderContractError):
        session.resync(binding, acknowledge_data_loss=True)
    assert provider._source_recovery is None  # the restart was acknowledged
    new_api = md_front.apis[-1]
    new_api.tick("rb2701")
    new_api.settle()
    # Held: nothing of the new session under the channel's old binding.
    for _ in range(5):
        assert session.poll(timeout_ms=10, max_events=8) == ()
    rebound = session.resync(binding, acknowledge_data_loss=True)
    assert rebound.source.connection_generation > binding.source.connection_generation
    events = _poll(session)
    assert events[0].cursor.sequence == 1 and events[0].source == rebound.source
    session.close()


def test_close_is_confirmed_while_the_front_stays_closed(md_front):
    session, bindings = _direct_session()
    assert _poll(session)
    md_front.accepting = False
    md_front.apis[0].drop()
    _pump_until(session, lambda: len(md_front.apis) == 2)
    _pump_until(session, lambda: _provider(session)._source_recovery.state == "connecting")
    for _ in range(3):
        with pytest.raises(MarketStreamProviderContractError):
            session.poll(timeout_ms=10, max_events=8)
    assert _provider(session)._source_recovery.state == "connecting"
    assert session.unsubscribe(bindings["rb2701"]) is True
    assert session.close() is True
    _wait(lambda: all(api.released for api in md_front.apis))


def test_twenty_reconnect_cycles_leave_no_thread_session_or_client_behind(md_front):
    session, bindings = _direct_session()
    binding = bindings["rb2701"]
    assert _poll(session)
    gc.collect()
    threads_before = threading.active_count()
    retained_before = len(client_module._RETIRED_CTP_NATIVE_SESSIONS)
    lost_clients = []
    for cycle in range(20):
        api = md_front.apis[-1]
        lost_clients.append(weakref.ref(_provider(session)._native_client))
        if cycle % 2:
            md_front.login_script = ["not_ready"]  # a refused first login too
        api.drop()
        _poll_until_restart(session)
        binding = session.resync(binding, acknowledge_data_loss=True)
        md_front.apis[-1].tick("rb2701")
        events = _poll(session)
        assert events and events[0].cursor.sequence == 1
        assert events[0].source == binding.source
    assert all(api.released for api in md_front.apis[:-1])
    _wait(lambda: threading.active_count() <= threads_before + 1)
    gc.collect()
    assert len(client_module._RETIRED_CTP_NATIVE_SESSIONS) <= retained_before
    assert sum(ref() is not None for ref in lost_clients) == 0
    assert session.close() is True
    _wait(lambda: threading.active_count() <= threads_before)
