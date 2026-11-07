"""Iteration 62 review (2026-11-07, section 4 item 3): account facts read while
the CTP session changes, over a real ``TraderClient``.

``CtpDirectWriteProvider.account_facts``, ``CtpRequestDataFuture`` and
``TraderClient`` are the real classes; only the vendor ``CThostFtdcTraderApi``
is a fake.  The fake answers through the SPI the client registered, on a
separate "vendor" thread, so every session field the provider reads before and
after its queries is produced by the real callbacks (OnFrontConnected,
OnRspAuthenticate, OnRspUserLogin, OnFrontDisconnected, OnRspQry*).

UNIT/FAKE evidence only: no network, no account, no SimNow.
"""

from __future__ import annotations

import threading
from types import SimpleNamespace
from typing import Any, Callable

import pytest

from bt_api_ctp.ctp import client as client_module
from bt_api_ctp.direct_write_provider import CtpDirectWriteProvider
from bt_api_ctp.feeds.live_ctp_feed import CtpRequestDataFuture

DAY = "20261106"
NEXT_DAY = "20261109"
BROKER = "9999"
INVESTOR = "investor-1"
_OK = SimpleNamespace(ErrorID=0, ErrorMsg="")
_CHANGED = "session_changed_during_query"

_ROWS: dict[str, tuple[dict[str, Any], ...]] = {
    "positions": (
        {
            "InstrumentID": "rb2701",
            "ExchangeID": "SHFE",
            "PosiDirection": "2",
            "Position": 2,
            "TodayPosition": 1,
        },
    ),
    "orders": (
        {
            "InstrumentID": "rb2701",
            "ExchangeID": "SHFE",
            "OrderRef": "000000000041",
            "FrontID": 7,
            "SessionID": 19,
            "OrderSysID": "  1234",
            "OrderSubmitStatus": "3",
            "OrderStatus": "3",
            "Direction": "0",
            "CombOffsetFlag": "0",
            "VolumeTotalOriginal": 1,
            "VolumeTraded": 0,
            "VolumeTotal": 1,
            "LimitPrice": 3000.0,
            "TradingDay": DAY,
        },
    ),
    "trades": (
        {
            "InstrumentID": "rb2701",
            "ExchangeID": "SHFE",
            "OrderRef": "000000000040",
            "OrderSysID": "  1233",
            "TradeID": "  77",
            "Direction": "0",
            "OffsetFlag": "0",
            "Price": 3000.0,
            "Volume": 1,
            "TradeDate": DAY,
            "TradeTime": "09:01:02",
            "TradingDay": DAY,
        },
    ),
    "account": ({"CurrencyID": "CNY", "Balance": 1000.0, "Available": 900.0, "CurrMargin": 100.0},),
}


def _on_vendor_thread(action: Callable[[], None]) -> None:
    """Run ``action`` on another thread, as the vendor delivers callbacks, and wait."""

    failures: list[BaseException] = []

    def run() -> None:
        try:
            action()
        except BaseException as exc:  # re-raised on the test thread below
            failures.append(exc)

    thread = threading.Thread(target=run, name="fake-ctp-vendor", daemon=True)
    thread.start()
    thread.join(5.0)
    assert not thread.is_alive()
    if failures:
        raise failures[0]


class _FakeTraderApi:
    """Fake vendor trader API: scripted logins, fixed query rows.

    ``logins`` lists (FrontID, SessionID, TradingDay) for each login it
    accepts, in order.  ``during[kind]`` runs while that query is in flight
    (after its rows, before its last packet); the request then dies with the
    link and is never answered, as with the vendor API.
    """

    def __init__(self, logins: list[tuple[int, int, str]]) -> None:
        self.logins = list(logins)
        self.spi: Any = None
        self.during: dict[str, Callable[[], None]] = {}
        self.hold_login = False
        self.requests: list[str] = []
        self.login_request_ids: list[int] = []
        self.released = threading.Event()
        self._init_thread: threading.Thread | None = None

    # -- lifecycle ----------------------------------------------------------
    def RegisterSpi(self, spi: Any) -> None:
        if spi is not None:
            self.spi = spi

    def SubscribePrivateTopic(self, _mode: int) -> None:
        return None

    def SubscribePublicTopic(self, _mode: int) -> None:
        return None

    def RegisterFront(self, _front: str) -> None:
        return None

    def Init(self) -> None:
        # The vendor connects on its own thread after Init returns.
        self._init_thread = threading.Thread(
            target=self.front_connected, name="fake-ctp-vendor-init", daemon=True
        )
        self._init_thread.start()

    def Join(self) -> int:
        return 0

    def Release(self) -> None:
        self.released.set()

    # -- vendor events --------------------------------------------------------
    def front_connected(self) -> None:
        self.spi.OnFrontConnected()

    def front_disconnected(self, reason: int = 0x1001) -> None:
        self.spi.OnFrontDisconnected(reason)

    def reconnect(self) -> None:
        """The link drops and the vendor reconnects on its own (same API object)."""

        self.front_disconnected()
        self.front_connected()

    # -- requests -----------------------------------------------------------
    def ReqAuthenticate(self, _field: Any, request_id: int) -> int:
        self.requests.append("authenticate")
        self.spi.OnRspAuthenticate(None, _OK, request_id, True)
        return 0

    def ReqUserLogin(self, field: Any, request_id: int) -> int:
        self.requests.append("login")
        self.login_request_ids.append(request_id)
        if self.hold_login:
            return 0  # the answer never comes while the test runs
        front_id, session_id, trading_day = self.logins.pop(0)
        self.spi.OnRspUserLogin(
            SimpleNamespace(
                BrokerID=field.BrokerID,
                UserID=field.UserID,
                TradingDay=trading_day,
                FrontID=front_id,
                SessionID=session_id,
                MaxOrderRef="0",
            ),
            _OK,
            request_id,
            True,
        )
        return 0

    def _answer(self, kind: str, callback_name: str, request_id: int) -> int:
        self.requests.append(kind)
        rows = _ROWS[kind]
        change = self.during.pop(kind, None)

        def deliver() -> None:
            callback = getattr(self.spi, callback_name)
            if change is not None:
                for row in rows:
                    callback(row, None, request_id, False)
                change()
                return
            for index, row in enumerate(rows):
                callback(row, None, request_id, index == len(rows) - 1)

        _on_vendor_thread(deliver)
        return 0

    def ReqQryInvestorPosition(self, _field: Any, request_id: int) -> int:
        return self._answer("positions", "OnRspQryInvestorPosition", request_id)

    def ReqQryOrder(self, _field: Any, request_id: int) -> int:
        return self._answer("orders", "OnRspQryOrder", request_id)

    def ReqQryTrade(self, _field: Any, request_id: int) -> int:
        return self._answer("trades", "OnRspQryTrade", request_id)

    def ReqQryTradingAccount(self, _field: Any, request_id: int) -> int:
        return self._answer("account", "OnRspQryTradingAccount", request_id)


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
        saved = {name: list(getattr(client_module, name)) for name in names}
        for name in names:
            getattr(client_module, name).clear()
    try:
        yield
    finally:
        with client_module._RETIRED_CTP_NATIVE_SESSIONS_LOCK:
            for name in names:
                target = getattr(client_module, name)
                target.clear()
                if isinstance(target, list):
                    target.extend(saved[name])
                else:
                    target.update(saved[name])


@pytest.fixture
def native(monkeypatch):
    """Route ``CreateFtdcTraderApi`` to the queued fakes; stop every client after."""

    queued: list[_FakeTraderApi] = []
    created: list[_FakeTraderApi] = []

    def create(_flow: str) -> _FakeTraderApi:
        api = queued.pop(0)
        created.append(api)
        return api

    monkeypatch.setenv("BT_API_PY_CTP_QUERY_INTERVAL_SEC", "0")
    monkeypatch.setattr(client_module, "_check_native_module", lambda: None)
    monkeypatch.setattr(client_module, "_flow_dir", lambda _prefix: "fake-flow")
    monkeypatch.setattr(
        client_module, "CThostFtdcTraderApi", SimpleNamespace(CreateFtdcTraderApi=create)
    )
    yield queued
    for api in created:
        if api._init_thread is not None:
            api._init_thread.join(2.0)
        if api.spi is not None:
            api.spi._c.stop()
    for api in created:
        assert api.released.wait(2.0), "fake native API not released"


def _feed() -> CtpRequestDataFuture:
    return CtpRequestDataFuture(
        broker_id=BROKER,
        user_id=INVESTOR,
        password="synthetic-only",
        td_front="tcp://127.0.0.1:19991",
        md_front="tcp://127.0.0.1:19992",
        auto_settlement_confirm=False,
        connect_timeout=5,
    )


def _read_all(provider: CtpDirectWriteProvider):
    return provider.account_facts(
        positions=True, active_orders=True, trades=True, balance=True, all_orders=True
    )


def _session(client: Any) -> tuple[Any, Any, Any, Any]:
    state = client.get_session_state()
    return (
        state["front_id"],
        state["session_id"],
        state["trading_day"],
        state["connection_generation"],
    )


def _assert_not_one_session(facts) -> None:
    assert facts.complete is False
    assert _CHANGED in facts.blockers
    assert facts.positions is None and facts.active_orders is None
    assert facts.orders is None and facts.trades is None
    assert facts.balance is None
    # The facts still name the session they started in.
    assert facts.session_generation == 1 and facts.trading_day == DAY


def _before_orders(feed: CtpRequestDataFuture, action: Callable[[], None]) -> None:
    """Run ``action`` once, after the position query returned and before the
    order query starts (another owner acting on the feed between two queries)."""

    original = feed.query_orders_result
    pending = [action]

    def query_orders_result(**kwargs: Any):
        if pending:
            pending.pop()()
        return original(**kwargs)

    feed.query_orders_result = query_orders_result


# ---------------------------------------------------------------------------
# 1. guard: one session from the first query to the last
# ---------------------------------------------------------------------------
def test_facts_of_one_unchanged_session_are_complete(native):
    api = _FakeTraderApi([(7, 19, DAY)])
    native.append(api)
    feed = _feed()
    provider = CtpDirectWriteProvider(feed)

    facts = _read_all(provider)

    assert type(feed.trader_client) is client_module.TraderClient
    assert api.requests == ["authenticate", "login", "positions", "orders", "trades", "account"]
    assert facts.complete is True and facts.blockers == ()
    assert facts.session_generation == 1 and facts.trading_day == DAY
    assert [(p.symbol, p.side, p.total, p.today) for p in facts.positions] == [
        ("rb2701", "long", 2.0, 1.0)
    ]
    assert len(facts.active_orders) == 1 and len(facts.orders) == 1
    assert len(facts.trades) == 1
    assert facts.balance is not None and facts.balance.available == 900.0
    assert _session(feed.trader_client) == (7, 19, DAY, 1)


# ---------------------------------------------------------------------------
# 2. reconnect and re-login while a query is in flight
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("session_id", [20, 19], ids=["new_session_id", "equal_login"])
@pytest.mark.parametrize("in_flight", ["positions", "orders", "trades"])
def test_reconnect_while_a_query_is_in_flight_voids_every_section(native, in_flight, session_id):
    """``equal_login``: the new login repeats FrontID/SessionID/day, so only the
    connection generation tells the two links apart."""

    api = _FakeTraderApi([(7, 19, DAY), (7, session_id, DAY)])
    api.during[in_flight] = api.reconnect
    native.append(api)
    feed = _feed()
    provider = CtpDirectWriteProvider(feed)

    facts = _read_all(provider)

    _assert_not_one_session(facts)
    # The vendor reconnected the same client: the session the provider reads
    # again names the new link (generation 2).
    assert _session(feed.trader_client) == (7, session_id, DAY, 2)
    assert api.requests.count("login") == 2


# ---------------------------------------------------------------------------
# 3. ABA: another client with an equal session replaces the first one
# ---------------------------------------------------------------------------
def test_feed_reconnected_to_a_new_client_between_queries_is_not_one_session(native):
    """Direct feed: ``disconnect()`` + ``connect()`` between two queries.  The
    new client logs in with the same FrontID/SessionID/day and its generation
    starts again at 1, so every later result carries the starting generation."""

    first, second = _FakeTraderApi([(7, 19, DAY)]), _FakeTraderApi([(7, 19, DAY)])
    native.extend([first, second])
    feed = _feed()
    provider = CtpDirectWriteProvider(feed)

    def swap() -> None:
        feed.disconnect()
        feed.connect()

    _before_orders(feed, swap)

    facts = _read_all(provider)

    _assert_not_one_session(facts)
    assert feed.trader_client is not first.spi._c
    assert _session(feed.trader_client) == (7, 19, DAY, 1)  # generation reads back equal
    assert first.requests[-1] == "positions"
    assert second.requests == ["authenticate", "login", "orders", "trades", "account"]


def test_feed_rebound_while_the_old_session_lives_on_is_not_one_session(native):
    """The feed's client pointer is replaced while the old client keeps its
    session (as a runtime-owned feed is detached and resolved again without a
    stop).  Re-reading the old client gives exactly the starting session, so
    only the client identity tells the two sources apart."""

    first, second = _FakeTraderApi([(7, 19, DAY)]), _FakeTraderApi([(7, 19, DAY)])
    native.extend([first, second])
    feed = _feed()
    provider = CtpDirectWriteProvider(feed)
    owner = _feed()

    def rebind() -> None:
        owner.connect()
        with feed._connect_lock:
            feed._trader = owner.trader_client

    _before_orders(feed, rebind)

    facts = _read_all(provider)

    _assert_not_one_session(facts)
    old_client = first.spi._c
    assert old_client.is_read_only_ready is True
    assert _session(old_client) == (7, 19, DAY, 1)
    assert _session(feed.trader_client) == (7, 19, DAY, 1)
    assert feed.trader_client is not old_client
    assert second.requests == ["authenticate", "login", "orders", "trades", "account"]


# ---------------------------------------------------------------------------
# 4. the trading day changes while the queries run
# ---------------------------------------------------------------------------
def test_trading_day_switch_while_a_query_is_in_flight_voids_every_section(native):
    """A day switch arrives with a re-login (same FrontID/SessionID here)."""

    api = _FakeTraderApi([(7, 19, DAY), (7, 19, NEXT_DAY)])
    api.during["trades"] = api.reconnect
    native.append(api)
    feed = _feed()
    provider = CtpDirectWriteProvider(feed)

    facts = _read_all(provider)

    _assert_not_one_session(facts)
    assert _session(feed.trader_client) == (7, 19, NEXT_DAY, 2)


def test_a_login_answer_outside_a_login_never_changes_the_session_day(native):
    """Within one link the day is fixed: a stray login answer is ignored, so a
    day switch always comes with a new connection generation."""

    api = _FakeTraderApi([(7, 19, DAY)])
    native.append(api)
    feed = _feed()
    feed.ensure_read_only_connected()
    client = feed.trader_client

    def repeated_login_answer() -> None:
        api.spi.OnRspUserLogin(
            SimpleNamespace(
                BrokerID=BROKER,
                UserID=INVESTOR,
                TradingDay=NEXT_DAY,
                FrontID=7,
                SessionID=21,
                MaxOrderRef="0",
            ),
            _OK,
            api.login_request_ids[-1],
            True,
        )

    _on_vendor_thread(repeated_login_answer)

    assert _session(client) == (7, 19, DAY, 1)
    assert client.get_session_state()["login_late_callback_count"] == 1


# ---------------------------------------------------------------------------
# 5. the session is lost when the last query ends
# ---------------------------------------------------------------------------
def _drop(api: _FakeTraderApi) -> None:
    api.front_disconnected()


def _reconnect_without_login(api: _FakeTraderApi) -> None:
    api.hold_login = True
    api.reconnect()


@pytest.mark.parametrize(
    "lose", [_drop, _reconnect_without_login], ids=["disconnected", "not_logged_in"]
)
def test_session_lost_when_the_query_ends_voids_every_section(native, lose):
    api = _FakeTraderApi([(7, 19, DAY)])
    api.during["account"] = lambda: lose(api)
    native.append(api)
    feed = _feed()
    provider = CtpDirectWriteProvider(feed)

    facts = _read_all(provider)

    _assert_not_one_session(facts)
    assert feed.trader_client.is_read_only_ready is False
    assert feed.trader_client.get_session_state()["trading_day"] == ""
