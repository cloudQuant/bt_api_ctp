"""Iteration 67.06 G3: CTP OnRtnInstrumentStatus ingress -> ledger wiring.

UNIT/FAKE only: synthetic native structs and the provider-local ingress record
builder.  No network, account, clock proof or trading authorization.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from bt_api_ctp.ctp import callback_ingress as ingress
from bt_api_ctp.ctp.client import TraderClient, _TraderSpi
from bt_api_ctp.direct_write_provider import CtpDirectWriteProvider
from bt_api_ctp.feeds.live_ctp_feed import CtpRequestDataFuture
from bt_api_ctp.instrument_status_ingress import InstrumentStatusIngressBridge
from bt_api_ctp.instrument_status_observation import (
    STATUS_CLOSED,
    STATUS_CONTINUOUS,
    STATUS_UNKNOWN,
    CtpInstrumentStatusObservationError,
)

VENUE = "SHFE"
SYMBOL = "rb2701"
GENERATION = "7"
DAY = "20261116"


def native(status="2", **overrides):
    values = {
        "ExchangeID": VENUE,
        "ExchangeInstID": SYMBOL,
        "InstrumentID": SYMBOL,
        "InstrumentStatus": status,
        "TradingSegmentSN": 2,
        "EnterTime": "09:00:00",
        "EnterReason": "1",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def status_payload(status="2", *, sequence=1, monotonic_ns=777, field=None, generation=7):
    record = ingress.build_callback_ingress_record_v2(
        owner_intent_id="ctp-status-test",
        callback_name="OnRtnInstrumentStatus",
        source_phase=ingress.CtpTraderCallbackPhase.ACTIVE,
        source_tags=ingress.CtpTraderCallbackSourceTagsV2(
            source_instance_id="source-1",
            native_client_epoch="epoch-1",
            native_api_source_id="api-1",
            native_spi_source_id="spi-1",
            native_api_generation=1,
            connection_generation=generation,
        ),
        connection_generation=generation,
        source_sequence=sequence,
        callback_monotonic_ns=monotonic_ns,
        args=(field if field is not None else native(status),),
    )
    assert record.capture_complete is True
    return record.to_payload()


def bridge():
    return InstrumentStatusIngressBridge(session_generation=GENERATION, trading_day=DAY)


# ---------------------------------------------------------------------------
# bridge: payload -> projection -> ledger
# ---------------------------------------------------------------------------


def test_payload_is_projected_into_the_ledger_and_answers_the_view():
    target = bridge()
    result = target.observe_payload(status_payload("2"))
    assert (result.applied, result.action, result.reason_code) == (True, "applied", None)
    view = target.view(VENUE, SYMBOL)
    assert (view.state, view.effective_status_category, view.usable) == (
        "current",
        STATUS_CONTINUOUS,
        True,
    )
    assert (view.session_generation, view.trading_day) == (GENERATION, DAY)


def test_duplicate_out_of_order_and_conflict_never_overwrite_the_current_status():
    target = bridge()
    assert target.observe_payload(status_payload("2", sequence=2)).applied is True

    duplicate = target.observe_payload(status_payload("2", sequence=2))
    assert (duplicate.applied, duplicate.action) == (False, "duplicate")
    out_of_order = target.observe_payload(status_payload("6", sequence=1))
    assert (out_of_order.applied, out_of_order.action) == (False, "out_of_order")
    conflict = target.observe_payload(status_payload("6", sequence=2))
    assert (conflict.applied, conflict.action) == (False, "sequence_conflict")

    view = target.view(VENUE, SYMBOL)
    assert view.effective_status_category == STATUS_CONTINUOUS
    assert view.usable is True


def test_ordering_uses_the_source_sequence_never_the_receive_stamp():
    target = bridge()
    assert target.observe_payload(status_payload("6", sequence=1, monotonic_ns=10**9)).applied
    assert target.observe_payload(status_payload("2", sequence=2, monotonic_ns=1)).applied
    view = target.view(VENUE, SYMBOL)
    assert view.effective_status_category == STATUS_CONTINUOUS

    late = target.observe_payload(status_payload("6", sequence=1, monotonic_ns=2**62))
    assert (late.applied, late.action) == (False, "out_of_order")
    assert target.view(VENUE, SYMBOL).effective_status_category == STATUS_CONTINUOUS


def test_generation_and_trading_day_changes_stale_prior_observations():
    target = bridge()
    assert target.observe_payload(status_payload("6", sequence=1)).applied
    changed = target.begin_session_generation("8", DAY)
    assert changed.action == "invalidated"
    stale = target.view(VENUE, SYMBOL)
    assert (stale.state, stale.usable) == ("stale", False)
    assert stale.reason_code == "ctp_instrument_status_stale_generation"

    # A new observation in the new generation is current again.
    assert target.observe_payload(status_payload("2", sequence=2, generation=8)).applied
    assert target.view(VENUE, SYMBOL).effective_status_category == STATUS_CONTINUOUS

    day_changed = target.begin_session_generation("8", "20261117")
    assert day_changed.action == "invalidated"
    assert target.view(VENUE, SYMBOL).reason_code == "ctp_instrument_status_stale_trading_day"


def test_unavailable_source_stales_every_observation_and_never_resurrects_it():
    target = bridge()
    assert target.observe_payload(status_payload("6", sequence=1)).applied
    target.mark_source_unavailable()
    stale = target.view(VENUE, SYMBOL)
    assert (stale.state, stale.usable, stale.reason_code) == (
        "stale",
        False,
        "ctp_instrument_status_source_unavailable",
    )


def test_unknown_native_status_is_an_explicit_unknown_not_tradable():
    target = bridge()
    assert target.observe_payload(status_payload("9", sequence=1)).applied
    view = target.view(VENUE, SYMBOL)
    assert view.effective_status_category == STATUS_UNKNOWN
    assert (view.state, view.usable) == ("current", False)
    assert view.reason_code == "ctp_instrument_status_status_unknown"


def test_malformed_payloads_are_refused_with_finite_codes_and_never_raise():
    target = bridge()
    assert target.observe_payload(None).reason_code == "ctp_instrument_status_fields_invalid"
    assert (
        target.observe_payload({"sequence": 0}).reason_code
        == "ctp_instrument_status_sequence_invalid"
    )
    assert (
        target.observe_payload({"sequence": 1, "monotonic_ns": 0}).reason_code
        == "ctp_instrument_status_receive_time_invalid"
    )
    incomplete = dict(status_payload("2"))
    incomplete["capture_complete"] = False
    assert (
        target.observe_payload(incomplete).reason_code == "ctp_instrument_status_fields_incomplete"
    )
    assert target.view(VENUE, SYMBOL).usable is False


def test_invalid_identity_is_an_explicit_unknown():
    target = bridge()
    view = target.view("bad venue", SYMBOL)
    assert (view.state, view.usable) == ("unknown", False)
    assert view.reason_code == "ctp_instrument_status_fields_invalid"


def test_bridge_requires_trusted_session_facts():
    with pytest.raises(CtpInstrumentStatusObservationError):
        InstrumentStatusIngressBridge(session_generation="7", trading_day="")


# ---------------------------------------------------------------------------
# client: direct-mode native callback -> bounded ingress payload
# ---------------------------------------------------------------------------


def _offline_client():
    client = TraderClient("tcp://fake", "9999", "offline", "fixture")
    client._connected = True
    client._trading_day = "20261116"
    client._connection_generation = 7
    spi = _TraderSpi(client)
    spi._callback_source_tags = ingress.CtpTraderCallbackSourceTagsV2(
        source_instance_id="source-1",
        native_client_epoch="epoch-1",
        native_api_source_id="api-1",
        native_spi_source_id="spi-1",
        native_api_generation=1,
        connection_generation=7,
    )
    return client, spi


def test_direct_native_status_callback_publishes_one_bounded_payload():
    client, spi = _offline_client()
    seen = []
    client.add_instrument_status_listener(seen.append)
    spi.OnRtnInstrumentStatus(native("2"))
    assert len(seen) == 1
    payload = seen[0]
    assert payload["callback_name"] == "OnRtnInstrumentStatus"
    assert payload["phase"] == "ACTIVE"
    assert payload["sequence"] == 1 and payload["connection_generation"] == 7
    assert payload["capture_complete"] is True

    bridge_instance = bridge()
    assert bridge_instance.observe_payload(payload).applied is True
    assert bridge_instance.view(VENUE, SYMBOL).effective_status_category == STATUS_CONTINUOUS

    spi.OnRtnInstrumentStatus(native("6"))
    assert [item["sequence"] for item in seen] == [1, 2]
    assert bridge_instance.observe_payload(seen[1]).applied is True
    assert bridge_instance.view(VENUE, SYMBOL).effective_status_category == STATUS_CLOSED


def test_no_listener_means_no_capture_and_removal_stops_delivery():
    client, spi = _offline_client()
    spi.OnRtnInstrumentStatus(native("2"))  # no observer: nothing is built
    seen = []
    listener = seen.append
    client.add_instrument_status_listener(listener)
    spi.OnRtnInstrumentStatus(native("2"))
    assert len(seen) == 1
    client.remove_instrument_status_listener(listener)
    spi.OnRtnInstrumentStatus(native("6"))
    assert len(seen) == 1


def test_incomplete_native_capture_publishes_a_refused_payload_only():
    client, spi = _offline_client()
    seen = []
    client.add_instrument_status_listener(seen.append)
    broken = native("2")
    del broken.EnterReason
    spi.OnRtnInstrumentStatus(broken)
    # The record is complete at the ingress level (the optional getter is
    # recorded as absent), so the payload is still published; the provider
    # helper refuses it and no status is created.
    assert len(seen) == 1
    target = bridge()
    assert target.observe_payload(seen[0]).reason_code == "ctp_instrument_status_fields_incomplete"
    assert target.view(VENUE, SYMBOL).usable is False


# ---------------------------------------------------------------------------
# provider: listener -> bridge -> view, with trusted session facts
# ---------------------------------------------------------------------------


class FakeTrader:
    def __init__(self):
        self.connection_generation = 7
        self.front_id = 1
        self.session_id = 2
        self.trading_day = DAY
        self.session_state = {
            "connected": True,
            "read_only_ready": True,
            "trading_day": DAY,
            "connection_generation": 7,
            "front_id": 1,
            "session_id": 2,
        }
        self.execution_listeners = []
        self.status_listeners = []

    def get_session_state(self):
        return dict(self.session_state)

    def add_execution_listener(self, listener):
        self.execution_listeners.append(listener)

    def remove_execution_listener(self, listener):
        self.execution_listeners.remove(listener)

    def add_instrument_status_listener(self, listener):
        self.status_listeners.append(listener)

    def remove_instrument_status_listener(self, listener):
        self.status_listeners.remove(listener)

    def instrument_status_listener_state(self, listener):
        state = self.session_state or {}
        return (state.get("connection_generation"), state.get("trading_day"),
                any(item is listener for item in self.status_listeners), True, self)


def _feed_with(trader):
    feed = CtpRequestDataFuture(
        broker_id="broker-test",
        user_id="investor-test",
        password="synthetic-only",
        td_front="tcp://127.0.0.1:19991",
        md_front="tcp://127.0.0.1:19992",
        auto_settlement_confirm=False,
    )
    feed._connect_timeout = 0.01
    feed._trader = trader
    return feed


def test_provider_consumes_the_native_payload_and_answers_its_ledger():
    trader = FakeTrader()
    provider = CtpDirectWriteProvider(_feed_with(trader))
    provider.ensure_listening()
    assert len(trader.status_listeners) == 1

    trader.status_listeners[0](status_payload("2", sequence=1))
    view = provider.instrument_status_view(VENUE, SYMBOL)
    assert (view.state, view.effective_status_category, view.usable) == (
        "current",
        STATUS_CONTINUOUS,
        True,
    )
    assert (view.session_generation, view.trading_day) == ("7", DAY)


def test_provider_stales_prior_observations_on_session_day_change():
    trader = FakeTrader()
    provider = CtpDirectWriteProvider(_feed_with(trader))
    provider.ensure_listening()
    trader.status_listeners[0](status_payload("2", sequence=1))
    assert provider.instrument_status_view(VENUE, SYMBOL).usable is True

    trader.session_state["trading_day"] = "20261117"
    view = provider.instrument_status_view(VENUE, SYMBOL)
    assert (view.state, view.usable) == ("stale", False)
    assert view.reason_code == "ctp_instrument_status_stale_trading_day"


def test_provider_marks_the_source_unavailable_when_session_facts_disappear():
    trader = FakeTrader()
    provider = CtpDirectWriteProvider(_feed_with(trader))
    provider.ensure_listening()
    trader.status_listeners[0](status_payload("2", sequence=1))
    assert provider.instrument_status_view(VENUE, SYMBOL).usable is True

    trader.session_state = None
    view = provider.instrument_status_view(VENUE, SYMBOL)
    assert (view.state, view.usable) == ("stale", False)
    assert view.reason_code == "ctp_instrument_status_source_unavailable"


def test_provider_without_a_session_returns_an_explicit_absence():
    trader = FakeTrader()
    trader.session_state = None
    provider = CtpDirectWriteProvider(_feed_with(trader))
    assert provider.instrument_status_view(VENUE, SYMBOL) is None


def test_provider_close_detaches_the_status_tap_and_stales_observations():
    trader = FakeTrader()
    provider = CtpDirectWriteProvider(_feed_with(trader))
    provider.ensure_listening()
    trader.status_listeners[0](status_payload("2", sequence=1))
    provider.close()
    assert trader.status_listeners == []
    view = provider.instrument_status_view(VENUE, SYMBOL)
    assert (view.state, view.usable) == ("stale", False)
