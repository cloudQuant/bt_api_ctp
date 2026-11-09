"""Synthetic TD/MD source binding; no provider connections or account writes."""
from __future__ import annotations

from dataclasses import replace
import threading
import time

import pytest

from bt_api_ctp import gateway_clock_observation as clock
from bt_api_ctp.ctp.client import _TraderSpi
from bt_api_ctp.gateway_clock_observation import CtpGatewayClockError
from bt_api_py import clock_verification
from bt_api_py.clock_verification import ClockVerifier
from bt_api_py.ctp_gateway_clock_source import (
    CtpGatewayCalibratedClockError,
    CtpGatewayCalibratedClockSource,
    capture_gateway_server_clock_calibration,
)
from test_iter63_gateway_session import (
    KEY, _close_after_bounded_md_wrapper_exit, prepared as prepared,
)
from test_market_provider import _key


@pytest.fixture
def source(prepared, monkeypatch, request):
    original = _TraderSpi.OnRspUserLogin
    login_time = getattr(request, "param", "09:30:00")

    def synthetic_login(spi, response, error, request_id, is_last):
        response.FFEXTime = login_time
        return original(spi, response, error, request_id, is_last)

    monkeypatch.setattr(_TraderSpi, "OnRspUserLogin", synthetic_login)
    create, _, _, feeds, _, _, _, _ = prepared
    monkeypatch.setattr(clock_verification, "_sntp_query", lambda _server, _timeout: (0, 1000))
    verifier = ClockVerifier(servers=("synthetic",))
    calibration = capture_gateway_server_clock_calibration(verifier, allowed_servers=frozenset({"synthetic"}))
    session = create()
    session.connect(generation="service-g1", timeout=1.0)
    market, direct, key = session.market_provider, session.direct_provider, _key()
    handle = market.subscribe(key, generation="market-g1")
    trader = feeds[0].trader_client
    with trader._query_state_lock:
        observed_mono = trader._login_local_monotonic_ns
        observed_wall = trader._login_local_received_ns
    now = {"mono": observed_mono + 1_000_000_000, "wall": observed_wall + 1_000_000_000}
    monkeypatch.setattr(clock, "_monotonic_ns", lambda: now["mono"])
    monkeypatch.setattr(clock, "_wall_ns", lambda: now["wall"])
    yield session, market, handle, key, trader, now, calibration
    _close_after_bounded_md_wrapper_exit(session, market, direct)


def _read(source, **kwargs):
    session, _, handle, key, _, _, _ = source
    options = dict(key=key, generation="market-g1", stream_id=key.stream_id, epoch="hub-e1")
    options.update(kwargs)
    return session.read_market_login_clock(handle, **options)


def _tick(source, *, trading_day=None, action_day=None):
    _, market, handle, key, trader, _, _ = source
    day = trading_day or trader._trading_day
    market._native_client.emit_tick(
        InstrumentID=key.instrument.native_symbol, ExchangeID="CFFEX", LastPrice=4000.0,
        TradingDay=day, ActionDay=day if action_day is None else action_day,
        UpdateTime="09:30:01", UpdateMillisec=0,
    )
    events = market.poll(handle, limit=32)
    assert events
    return events[-1].data


def _read_tick(source, payload, **kwargs):
    session, _, handle, key, _, _, _ = source
    options = dict(key=key, generation="market-g1", stream_id=key.stream_id, epoch="hub-e1",
                   provider_sequence=payload["provider_sequence"],
                   provider_subscription_epoch=payload["provider_subscription_epoch"])
    options.update(kwargs)
    return session.read_market_tick_clock(handle, **options)


def test_tick_clock_binds_exact_native_event_and_original_deadline(source, monkeypatch):
    payload = _tick(source)
    mono = payload["received_monotonic_ns"]
    from datetime import datetime, timezone
    dt = datetime.fromisoformat(payload["received_at_utc"].replace("Z", "+00:00"))
    delta = dt - datetime(1970, 1, 1, tzinfo=timezone.utc)
    wall = (delta.days * 86400 + delta.seconds) * 1_000_000_000 + delta.microseconds * 1000
    monkeypatch.setattr(clock, "_monotonic_ns", lambda: mono + 1_000_000_000)
    monkeypatch.setattr(clock, "_wall_ns", lambda: wall + 1_000_000_000)
    result = _read_tick(source, payload, max_age_seconds=3)
    assert result["schema_version"] == 2 and result["observation_kind"] == "native_md_tick"
    assert result["venue_observed_at_utc"] == payload["provider_event_time_utc"]
    assert result["source_event_received_wall_ns"] == wall
    assert result["age_ns"] == 1_000_000_000 and result["remaining_ns"] == 2_000_000_000
    assert result["provider_sequence"] == payload["provider_sequence"]
    assert "login_received_wall_ns" not in result
    with pytest.raises(CtpGatewayClockError, match="CLOCK_OBSERVATION_UNAVAILABLE"):
        _read_tick(source, payload, provider_sequence=payload["provider_sequence"] + 100)
    monkeypatch.setattr(clock, "_monotonic_ns", lambda: mono + 3_000_000_000)
    monkeypatch.setattr(clock, "_wall_ns", lambda: wall + 3_000_000_000)
    with pytest.raises(CtpGatewayClockError, match="CLOCK_OBSERVATION_EXPIRED"):
        _read_tick(source, payload, max_age_seconds=3)


@pytest.mark.parametrize("day,action", [("20261105", "20261105"), (None, "")])
def test_tick_clock_rejects_wrong_trading_day_or_missing_native_action_day(source, day, action):
    payload = _tick(source, trading_day=day, action_day=action)
    with pytest.raises(CtpGatewayClockError, match="CLOCK_OBSERVATION_UNAVAILABLE"):
        _read_tick(source, payload)


def test_new_native_tick_is_not_limited_by_original_login_age(source, monkeypatch):
    trader = source[4]
    # Offline fixture represents a login which actually happened 301 seconds
    # earlier. Both the sealed observation and its owning client agree.
    with trader._query_state_lock:
        trader._login_local_monotonic_ns -= 301_000_000_000
        trader._login_local_received_ns -= 301_000_000_000
        trader._login_clock_observation = replace(
            trader._login_clock_observation,
            local_monotonic_ns=trader._login_local_monotonic_ns,
            local_received_ns=trader._login_local_received_ns,
        )
    payload = _tick(source)
    monkeypatch.setattr(clock, "_monotonic_ns", time.monotonic_ns)
    monkeypatch.setattr(clock, "_wall_ns", time.time_ns)
    with pytest.raises(CtpGatewayClockError, match="CLOCK_OBSERVATION_EXPIRED"):
        _read(source)
    result = _read_tick(source, payload)
    assert result["age_ns"] < 300_000_000_000


def test_calibrated_tick_clock_keeps_matching_source_facts(source, monkeypatch):
    payload = _tick(source)
    monkeypatch.setattr(clock, "_monotonic_ns", time.monotonic_ns)
    monkeypatch.setattr(clock, "_wall_ns", time.time_ns)
    session, _, handle, key, _, _, anchor = source
    reader = CtpGatewayCalibratedClockSource(session, anchor)
    result = reader.read(handle, key=key, generation="market-g1", stream_id=key.stream_id,
                         epoch="hub-e1", provider_sequence=payload["provider_sequence"],
                         provider_subscription_epoch=payload["provider_subscription_epoch"])
    assert result["source_calibration"]["method"] == "sntp"
    assert result["venue_observed_at_utc"] == payload["provider_event_time_utc"]
    internal, source_mono = reader.read_internal(
        handle, key=key, generation="market-g1", stream_id=key.stream_id,
        epoch="hub-e1", provider_sequence=payload["provider_sequence"],
        provider_subscription_epoch=payload["provider_subscription_epoch"])
    assert internal["venue_observed_at_utc"] == payload["provider_event_time_utc"]
    assert payload["received_monotonic_ns"] <= source_mono <= time.monotonic_ns()
    assert "source_observed_monotonic_ns" not in internal
    with pytest.raises(CtpGatewayCalibratedClockError, match="CLOCK_SOURCE_UNAVAILABLE"):
        reader.read(handle, key=key, generation="market-g1", stream_id=key.stream_id,
                    epoch="hub-e1", provider_sequence=payload["provider_sequence"])


def test_tick_clock_cache_eviction_fails_closed(source):
    payload = _tick(source)
    state = source[1]._state_for_handle(source[2])
    state.clock_events.clear()
    with pytest.raises(CtpGatewayClockError, match="CLOCK_OBSERVATION_UNAVAILABLE"):
        _read_tick(source, payload)


def test_refreshed_clock_must_predate_tick_and_stop_revokes_it(source, monkeypatch):
    from bt_api_py.gateway_server_clock_refresh import GatewayServerClockRefresher

    monkeypatch.setattr(clock, "_monotonic_ns", time.monotonic_ns)
    monkeypatch.setattr(clock, "_wall_ns", time.time_ns)
    verifier = ClockVerifier(servers=("synthetic",))
    refresher = GatewayServerClockRefresher(
        verifier, allowed_servers=frozenset({"synthetic"}), max_error_bound_ns=10_000_000,
        max_sample_validity_seconds=60, refresh_interval_seconds=30)
    session, _, handle, key, _, _, anchor = source
    reader = CtpGatewayCalibratedClockSource(session, anchor, refresher=refresher)
    refresher.start()
    try:
        deadline = time.monotonic() + 2
        while refresher.current() is None and time.monotonic() < deadline:
            threading.Event().wait(0.01)
        assert refresher.current() is not None
        payload = _tick(source)
        options = dict(key=key, generation="market-g1", stream_id=key.stream_id,
                       epoch="hub-e1", provider_sequence=payload["provider_sequence"],
                       provider_subscription_epoch=payload["provider_subscription_epoch"])
        assert reader.read(handle, **options)["schema_version"] == 2
        assert refresher.stop() is True
        with pytest.raises(CtpGatewayCalibratedClockError, match="CLOCK_CALIBRATION_UNAVAILABLE"):
            reader.read(handle, **options)
        # v1 retains its original pre-login sample instead of silently replacing it.
        assert reader.read(handle, key=key, generation="market-g1", stream_id=key.stream_id,
                           epoch="hub-e1")["schema_version"] == 1
    finally:
        refresher.stop()


def test_tick_calibration_uses_exact_source_time_despite_read_delay(source, monkeypatch):
    from bt_api_py.gateway_server_clock_refresh import GatewayServerClockRefresher

    monkeypatch.setattr(clock, "_monotonic_ns", time.monotonic_ns)
    monkeypatch.setattr(clock, "_wall_ns", time.time_ns)
    refresher = GatewayServerClockRefresher(
        ClockVerifier(servers=("synthetic",)), allowed_servers=frozenset({"synthetic"}),
        max_error_bound_ns=10_000_000, max_sample_validity_seconds=60,
        refresh_interval_seconds=30)
    session, _, handle, key, _, _, anchor = source
    reader = CtpGatewayCalibratedClockSource(session, anchor, refresher=refresher)
    refresher.start()
    try:
        deadline = time.monotonic() + 2
        while refresher.current() is None and time.monotonic() < deadline:
            threading.Event().wait(0.01)
        snapshot = refresher.current()
        assert snapshot is not None
        payload = _tick(source)
        assert snapshot.anchor_monotonic_ns < payload["received_monotonic_ns"]
        original = session.read_market_tick_clock
        boundaries = []

        def delayed(*args, **kwargs):
            entered = time.monotonic_ns()
            # Force the old before-age lower bound earlier than the anchor,
            # regardless of the fixture's setup scheduling delay.
            delay = (entered - snapshot.anchor_monotonic_ns) / 1e9 + 0.05
            threading.Event().wait(delay)
            result = original(*args, **kwargs)
            boundaries.append((entered, result[0]["age_ns"], result[1]))
            return result

        monkeypatch.setattr(session, "read_market_tick_clock", delayed)
        result = reader.read(
            handle, key=key, generation="market-g1", stream_id=key.stream_id,
            epoch="hub-e1", provider_sequence=payload["provider_sequence"],
            provider_subscription_epoch=payload["provider_subscription_epoch"])
        entered, age, source_mono = boundaries[0]
        assert entered - age < snapshot.anchor_monotonic_ns
        assert source_mono - age == payload["received_monotonic_ns"]
        assert result["schema_version"] == 2
        assert "source_observed_monotonic_ns" not in result
    finally:
        refresher.stop()


def test_refreshed_calibration_after_tick_is_rejected(source, monkeypatch):
    from bt_api_py.gateway_server_clock_refresh import GatewayServerClockRefresher

    monkeypatch.setattr(clock, "_monotonic_ns", time.monotonic_ns)
    monkeypatch.setattr(clock, "_wall_ns", time.time_ns)
    payload = _tick(source)
    refresher = GatewayServerClockRefresher(
        ClockVerifier(servers=("synthetic",)), allowed_servers=frozenset({"synthetic"}),
        max_error_bound_ns=10_000_000, max_sample_validity_seconds=60,
        refresh_interval_seconds=30)
    session, _, handle, key, _, _, anchor = source
    reader = CtpGatewayCalibratedClockSource(session, anchor, refresher=refresher)
    refresher.start()
    try:
        deadline = time.monotonic() + 2
        while refresher.current() is None and time.monotonic() < deadline:
            threading.Event().wait(0.01)
        snapshot = refresher.current()
        assert snapshot is not None
        assert snapshot.anchor_monotonic_ns > payload["received_monotonic_ns"]
        with pytest.raises(CtpGatewayCalibratedClockError, match="CLOCK_CALIBRATION_UNAVAILABLE"):
            reader.read(handle, key=key, generation="market-g1", stream_id=key.stream_id,
                        epoch="hub-e1", provider_sequence=payload["provider_sequence"],
                        provider_subscription_epoch=payload["provider_subscription_epoch"])
    finally:
        refresher.stop()


def test_source_binds_real_synthetic_login_and_md_descriptor(source):
    result = _read(source)
    session, market, handle, key, trader, now, _ = source
    assert result["schema_version"] == 1 and result["account_scope"] == KEY.account_key_id
    assert result["venue"] == "CFFEX" and result["venue_time"] == "09:30:00"
    assert result["td_connection_generation"] == trader.connection_generation
    assert result["trading_day"] == trader.get_session_state()["trading_day"]
    assert result["market_source"] == market.describe_source(
        handle, key=key, generation="market-g1", stream_id=key.stream_id, epoch="hub-e1"
    ).to_dict()
    assert result["age_ns"] == 1_000_000_000 and result["remaining_ns"] == 299_000_000_000
    assert result["source_observed_wall_ns"] == now["wall"]
    assert len(result["td_source_ref"]) == 64
    assert "execution_eligible" not in result and "verified" not in result
    assert session._service_generation == result["service_generation"]


def test_repeated_reads_never_renew_original_login_deadline(source):
    first = _read(source, max_age_seconds=3)
    now = source[5]
    now["mono"] += 1_000_000_000
    now["wall"] += 1_000_000_000
    second = _read(source, max_age_seconds=3)
    assert first["remaining_ns"] == 2_000_000_000
    assert second["remaining_ns"] == 1_000_000_000
    assert first["td_source_ref"] == second["td_source_ref"]
    now["mono"] += 1_000_000_000
    now["wall"] += 1_000_000_000
    with pytest.raises(CtpGatewayClockError, match="CLOCK_OBSERVATION_EXPIRED"):
        _read(source, max_age_seconds=3)


def test_mismatched_actual_market_key_and_generation_are_rejected(source):
    key = source[3]
    wrong = replace(key, instrument=replace(key.instrument, native_symbol="OTHER", instrument_id="OTHER"))
    with pytest.raises(CtpGatewayClockError, match="MARKET_SOURCE_UNAVAILABLE"):
        _read(source, key=wrong, stream_id=wrong.stream_id)
    with pytest.raises(CtpGatewayClockError, match="MARKET_SOURCE_UNAVAILABLE"):
        _read(source, generation="old-market-generation")
    with pytest.raises(CtpGatewayClockError, match="CLOCK_SCOPE_INVALID"):
        _read(source, stream_id="different")


def test_changed_market_account_binding_is_rejected(source):
    market = source[1]
    stream = market._stream
    original = stream.user_id
    stream.user_id = "different-synthetic-user"
    try:
        with pytest.raises(CtpGatewayClockError, match="MARKET_SOURCE_UNAVAILABLE"):
            _read(source)
    finally:
        stream.user_id = original


@pytest.mark.parametrize("source", [None, "", "9:30:00", "25:00:00", "09:60:00"], indirect=True)
def test_missing_or_invalid_native_venue_clock_is_unavailable(source):
    with pytest.raises(CtpGatewayClockError, match="CLOCK_OBSERVATION_UNAVAILABLE"):
        _read(source)


def test_mutated_clock_cache_cannot_replace_sealed_native_login(source):
    trader = source[4]
    with trader._query_state_lock:
        trader._login_exchange_times["FFEX"] = "10:00:00"
    with pytest.raises(CtpGatewayClockError, match="TD_SOURCE_CHANGED"):
        _read(source)


def test_td_clock_mutation_during_md_source_recheck_is_rejected(source, monkeypatch):
    market, trader = source[1], source[4]
    original = market.describe_source
    count = 0
    def changing(*args, **kwargs):
        nonlocal count
        result = original(*args, **kwargs)
        count += 1
        if count == 2:
            with trader._query_state_lock:
                trader._login_exchange_times["FFEX"] = "10:00:00"
        return result
    monkeypatch.setattr(market, "describe_source", changing)
    with pytest.raises(CtpGatewayClockError, match="TD_SOURCE_CHANGED"):
        _read(source)


def test_md_epoch_change_during_td_snapshot_is_rejected(source, monkeypatch):
    market = source[1]
    original = market.describe_source
    count = 0
    def changing(*args, **kwargs):
        nonlocal count
        result = original(*args, **kwargs)
        count += 1
        return replace(result, epoch="new-hub-epoch") if count == 2 else result
    monkeypatch.setattr(market, "describe_source", changing)
    with pytest.raises(CtpGatewayClockError, match="MARKET_SOURCE_CHANGED"):
        _read(source)


def test_td_generation_change_and_wall_step_fail_closed(source):
    trader, now = source[4], source[5]
    now["wall"] += 1_000_000_000
    with pytest.raises(CtpGatewayClockError, match="CLOCK_WALL_DISCONTINUITY"):
        _read(source)
    now["wall"] -= 1_000_000_000
    with trader._query_state_lock:
        trader._connection_generation += 1
    try:
        with pytest.raises(CtpGatewayClockError, match="CLOCK_SOURCE_UNAVAILABLE"):
            _read(source)
    finally:
        with trader._query_state_lock:
            trader._connection_generation -= 1


def test_wrong_thread_fails_without_observation(source):
    errors = []
    def run():
        try:
            _read(source)
        except Exception as exc:
            errors.append(exc)
    worker = threading.Thread(target=run)
    worker.start()
    worker.join(timeout=2)
    assert not worker.is_alive() and len(errors) == 1
    assert "WRONG_OWNER" in str(errors[0])


def test_server_calibration_binds_prelogin_sample_without_exporting_monotonic(source, monkeypatch):
    monkeypatch.setattr(clock, "_monotonic_ns", time.monotonic_ns)
    monkeypatch.setattr(clock, "_wall_ns", time.time_ns)
    session, _, handle, key, _, _, anchor = source
    reader = CtpGatewayCalibratedClockSource(session, anchor)
    result = reader.read(handle, key=key, generation="market-g1", stream_id=key.stream_id, epoch="hub-e1")
    assert result["source_calibration"]["method"] == "sntp"
    assert result["source_calibration"]["offset_ns"] == 0
    assert result["source_calibration"]["error_bound_ns"] >= 1000
    assert result["source_calibration"]["remaining_ns"] > 0
    assert "measured_monotonic_ns" not in result["source_calibration"]
    assert "login_monotonic_ns" not in result


def test_postlogin_calibration_and_wall_step_are_rejected(source, monkeypatch):
    monkeypatch.setattr(clock, "_monotonic_ns", time.monotonic_ns)
    monkeypatch.setattr(clock, "_wall_ns", time.time_ns)
    session, _, handle, key, _, _, anchor = source
    verifier = ClockVerifier(servers=("synthetic",))
    after_login = capture_gateway_server_clock_calibration(verifier, allowed_servers=frozenset({"synthetic"}))
    with pytest.raises(CtpGatewayCalibratedClockError, match="CLOCK_CALIBRATION_UNAVAILABLE"):
        CtpGatewayCalibratedClockSource(session, after_login).read(
            handle, key=key, generation="market-g1", stream_id=key.stream_id, epoch="hub-e1"
        )
    altered = replace(anchor, anchor_wall_ns=anchor.anchor_wall_ns + 1_000_000_000)
    with pytest.raises(CtpGatewayCalibratedClockError, match="CLOCK_WALL_DISCONTINUITY"):
        CtpGatewayCalibratedClockSource(session, altered).read(
            handle, key=key, generation="market-g1", stream_id=key.stream_id, epoch="hub-e1"
        )


def test_expired_server_calibration_is_rejected(source, monkeypatch):
    monkeypatch.setattr(clock, "_monotonic_ns", time.monotonic_ns)
    monkeypatch.setattr(clock, "_wall_ns", time.time_ns)
    session, _, handle, key, _, _, anchor = source
    expired = replace(anchor, evidence=replace(anchor.evidence, valid_until_monotonic_ns=time.monotonic_ns() - 1))
    with pytest.raises(CtpGatewayCalibratedClockError, match="CLOCK_CALIBRATION_UNAVAILABLE"):
        CtpGatewayCalibratedClockSource(session, expired).read(
            handle, key=key, generation="market-g1", stream_id=key.stream_id, epoch="hub-e1"
        )


def test_server_sntp_requires_sdk_query_and_explicit_source_allowlist(monkeypatch):
    calls = []
    monkeypatch.setattr(clock_verification, "_sntp_query", lambda host, _timeout: calls.append(host) or (0, 1000))
    injected = ClockVerifier(servers=("synthetic",), query=lambda _server, _timeout: (0, 0))
    with pytest.raises(CtpGatewayCalibratedClockError, match="INVALID_CLOCK_SOURCE"):
        capture_gateway_server_clock_calibration(injected, allowed_servers=frozenset({"synthetic"}))
    owned = ClockVerifier(servers=("synthetic",))
    with pytest.raises(CtpGatewayCalibratedClockError, match="INVALID_CLOCK_SOURCE"):
        capture_gateway_server_clock_calibration(owned, allowed_servers=frozenset({"other"}))
    with pytest.raises(CtpGatewayCalibratedClockError, match="INVALID_CLOCK_SOURCE"):
        capture_gateway_server_clock_calibration(owned, allowed_servers=frozenset())
    assert not calls
    anchor = capture_gateway_server_clock_calibration(owned, allowed_servers=frozenset({"synthetic"}))
    assert anchor.evidence.server == "synthetic" and calls == ["synthetic"]
