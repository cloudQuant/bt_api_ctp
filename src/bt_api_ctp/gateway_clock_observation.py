"""Bounded CTP login-clock observations for a current Gateway market source.

This module does not qualify ticks or assert synchronized clocks.  The caller
must authenticate and authorize the actual Gateway subscription, and resolve
its provider handle and epoch from the current owner, not from RPC payloads.
"""
from __future__ import annotations

import hashlib
import json
import math
import time
from datetime import date, datetime, time as civil_time, timezone

from bt_api_base.account_protocol import account_venue_contains_instrument_venue
from bt_api_base.market_protocol import MarketSourceDescriptor, MarketSubscriptionKey


class CtpGatewayClockError(RuntimeError):
    """Finite local error without credentials or native field values."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


# Named indirections also permit deterministic offline tests; never read a
# second process's monotonic clock or infer a calendar date from TradingDay.
_monotonic_ns = time.monotonic_ns
_wall_ns = time.time_ns
_VENUE_TIME = {"CFFEX": "FFEX", "SHFE": "SHFE", "DCE": "DCE",
               "CZCE": "CZCE", "INE": "INE", "GFEX": "GFEX"}
_MAX_TTL_SECONDS = 3600.0
_MAX_WALL_STEP_NS = 250_000_000


def _day(value: object) -> bool:
    if type(value) is not str or len(value) != 8 or any(c not in "0123456789" for c in value):
        return False
    try:
        date(int(value[:4]), int(value[4:6]), int(value[6:8]))
    except ValueError:
        return False
    return True


def _hms(value: object) -> bool:
    if (type(value) is not str or len(value) != 8 or value[2] != ":" or value[5] != ":"
            or any(c not in "0123456789" for c in value[:2] + value[3:5] + value[6:])):
        return False
    try:
        civil_time(int(value[:2]), int(value[3:5]), int(value[6:]))
    except ValueError:
        return False
    return True


def read_market_login_clock(session, handle, *, key, generation, stream_id, epoch,
                            max_age_seconds=300.0):
    """Read one current native TD login clock associated with one real MD source.

    ``handle``, ``generation`` and ``epoch`` are trusted owner-local stream
    state. They must be resolved by the authenticated Gateway hub, never passed
    through from an untrusted client. The fixed original login instant limits
    validity; repeated calls cannot renew it. The returned mapping is a
    transport observation, not an opaque authority token.
    """
    return _read_market_clock(session, handle, key=key, generation=generation,
                              stream_id=stream_id, epoch=epoch,
                              max_age_seconds=max_age_seconds)


def read_market_tick_clock(session, handle, *, key, generation, stream_id, epoch,
                           provider_sequence, provider_subscription_epoch,
                           max_age_seconds=300.0, _internal=False):
    """Read native facts for a polled tick selected by the trusted Hub.

    The Hub must independently resolve these selectors from its authenticated,
    successfully published frame. They do not authenticate a client request.
    """
    if (type(provider_sequence) is not int or provider_sequence <= 0
            or type(provider_subscription_epoch) is not int or provider_subscription_epoch <= 0):
        raise CtpGatewayClockError("CLOCK_REQUEST_INVALID")
    return _read_market_clock(session, handle, key=key, generation=generation,
                              stream_id=stream_id, epoch=epoch,
                              max_age_seconds=max_age_seconds,
                              tick_selector=(provider_sequence, provider_subscription_epoch),
                              internal=_internal)


def _read_market_clock(session, handle, *, key, generation, stream_id, epoch,
                       max_age_seconds, tick_selector=None, internal=False):
    from bt_api_ctp.ctp.client import _TRADER_LOGIN_CLOCK_SEAL, _TraderLoginClockObservation
    from bt_api_ctp.gateway_session import CtpGatewaySession

    if (type(session) is not CtpGatewaySession or type(key) is not MarketSubscriptionKey
            or type(max_age_seconds) not in (int, float) or not math.isfinite(max_age_seconds)
            or not 0 < max_age_seconds <= _MAX_TTL_SECONDS):
        raise CtpGatewayClockError("CLOCK_REQUEST_INVALID")
    session._check_owner()
    account = session._account_key
    instrument = key.instrument
    if (instrument.provider != "ctp" or instrument.environment != account.environment
            or instrument.product_type != "future" or instrument.venue not in _VENUE_TIME
            or not account_venue_contains_instrument_venue(account, instrument.venue)
            or type(generation) is not str or not generation
            or type(stream_id) is not str or stream_id != key.stream_id
            or type(epoch) is not str or not epoch):
        raise CtpGatewayClockError("CLOCK_SCOPE_INVALID")
    try:
        session._require_instrument_connected()
        provider = session._market
        def source():
            from bt_api_ctp.trusted_flow import flow_binding

            state = provider._state_for_handle(handle)
            if state is None or getattr(state, "active", False) is not True:
                raise CtpGatewayClockError("MARKET_SOURCE_UNAVAILABLE")
            stream = state.stream
            if (stream is not provider._stream
                    or stream.broker_id != account.subaccount_id
                    or stream.user_id != account.account_id
                    or stream.md_front != session._feed._execution_bound_md_front
                    or flow_binding(stream._trusted_flow) != flow_binding(session._flow)
                    or stream.quote_v2_service_binding_matches() is not True):
                raise CtpGatewayClockError("MARKET_SOURCE_UNAVAILABLE")
            result = provider.describe_source(handle, key=key, generation=generation,
                                              stream_id=stream_id, epoch=epoch)
            if type(result) is not MarketSourceDescriptor:
                raise CtpGatewayClockError("MARKET_SOURCE_UNAVAILABLE")
            return MarketSourceDescriptor.from_dict(result.to_dict())

        before = source()
        trader = session._trader
        with trader._query_state_lock:
            # RLock: retain the exact owner/source checks for the entire TD
            # snapshot; a disconnected or replaced session cannot be signed.
            if session.connection_status() != "ready":
                raise CtpGatewayClockError("TD_SESSION_UNAVAILABLE")
            login = trader._current_login_identity_locked()
            captured = trader._login_clock_observation
            owner = session._connection_owner
            tags = trader._query_source_tags_locked()
            if (login is None or not session._connection_owner_matches(owner)
                    or login.broker_id != account.subaccount_id or login.user_id != account.account_id
                    or login.connection_generation != trader._connection_generation
                    or login.trading_day != trader._trading_day or not _day(login.trading_day)
                    or type(captured) is not _TraderLoginClockObservation
                    or captured._seal is not _TRADER_LOGIN_CLOCK_SEAL or captured.login is not login
                    or captured.exchange_times != tuple(sorted(trader._login_exchange_times.items()))
                    or captured.local_received_ns != trader._login_local_received_ns
                    or captured.local_monotonic_ns != trader._login_local_monotonic_ns
                    or type(tags) is not tuple or len(tags) != 6):
                raise CtpGatewayClockError("TD_SOURCE_CHANGED")
            hms = trader._login_exchange_times.get(_VENUE_TIME[instrument.venue])
            observed_wall = trader._login_local_received_ns
            observed_mono = trader._login_local_monotonic_ns
            tick = None
            if tick_selector is not None:
                state = provider._state_for_handle(handle)
                # Native callback receipt and all fields were checked during
                # projection; the callable must share this process's clock.
                if state.source_scope._clock_source_callable is not time.monotonic_ns:
                    raise CtpGatewayClockError("CLOCK_SOURCE_UNAVAILABLE")
                matches = [data for scope_ref, data in state.clock_events
                           if scope_ref() is state.source_scope
                           and (data.get("provider_sequence"),
                                data.get("provider_subscription_epoch")) == tick_selector]
                if len(matches) != 1:
                    raise CtpGatewayClockError("CLOCK_OBSERVATION_UNAVAILABLE")
                tick = matches[0]
                if (tick.get("event_time_provenance") != "provider_reported"
                        or tick.get("trading_date") != date(
                            int(login.trading_day[:4]), int(login.trading_day[4:6]),
                            int(login.trading_day[6:8])).isoformat()
                        or tick.get("provider_connection_generation") != before.connection_generation
                        or type(tick.get("provider_event_time_utc")) is not str
                        or type(tick.get("received_at_utc")) is not str):
                    raise CtpGatewayClockError("CLOCK_OBSERVATION_UNAVAILABLE")
                received = datetime.fromisoformat(tick["received_at_utc"].replace("Z", "+00:00"))
                if received.tzinfo is None or received.utcoffset() is None:
                    raise CtpGatewayClockError("CLOCK_OBSERVATION_UNAVAILABLE")
                delta = received.astimezone(timezone.utc) - datetime(1970, 1, 1, tzinfo=timezone.utc)
                observed_wall = (delta.days * 86400 + delta.seconds) * 1_000_000_000 + delta.microseconds * 1000
                observed_mono = tick.get("received_monotonic_ns")
            now_mono, now_wall = _monotonic_ns(), _wall_ns()
            if ((tick is None and not _hms(hms)) or type(observed_wall) is not int or observed_wall <= 0
                    or type(observed_mono) is not int or observed_mono <= 0
                    or type(now_mono) is not int or type(now_wall) is not int):
                raise CtpGatewayClockError("CLOCK_OBSERVATION_UNAVAILABLE")
            age = now_mono - observed_mono
            ttl = int(max_age_seconds * 1_000_000_000)
            if age < 0 or age >= ttl:
                raise CtpGatewayClockError("CLOCK_OBSERVATION_EXPIRED")
            if abs((now_wall - observed_wall) - age) > _MAX_WALL_STEP_NS:
                raise CtpGatewayClockError("CLOCK_WALL_DISCONTINUITY")
            source_ref = hashlib.sha256(json.dumps(
                ["bt_api_ctp.gateway_login_clock.v1", account.account_key_id,
                 session._service_generation, tags, login.connection_generation, login.request_id],
                separators=(",", ":"), ensure_ascii=True,
            ).encode("ascii")).hexdigest()
            observation = {
                "schema_version": 1,
                "provider": "ctp",
                "account_scope": account.account_key_id,
                "venue": instrument.venue,
                "service_generation": session._service_generation,
                "td_connection_generation": login.connection_generation,
                "td_source_ref": source_ref,
                "trading_day": login.trading_day,
                "venue_time": hms,
                "login_received_wall_ns": observed_wall,
                "source_observed_wall_ns": now_wall,
                "age_ns": age,
                "remaining_ns": ttl - age,
                "market_source": before.to_dict(),
            }
            if tick is not None:
                observation.update({
                    "schema_version": 2,
                    "observation_kind": "native_md_tick",
                    "venue_observed_at_utc": tick["provider_event_time_utc"],
                    "source_event_received_wall_ns": observed_wall,
                    "provider_sequence": tick_selector[0],
                    "provider_subscription_epoch": tick_selector[1],
                })
                del observation["venue_time"], observation["login_received_wall_ns"]
        after = source()
        if before != after:
            raise CtpGatewayClockError("MARKET_SOURCE_CHANGED")
        session._require_instrument_connected()
        with trader._query_state_lock:
            current = trader._current_login_identity_locked()
            if (current is not login or trader._login_clock_observation is not captured
                    or captured.exchange_times != tuple(sorted(trader._login_exchange_times.items()))
                    or captured.local_received_ns != trader._login_local_received_ns
                    or captured.local_monotonic_ns != trader._login_local_monotonic_ns
                    or trader._query_source_tags_locked() != tags
                    or not session._connection_owner_matches(owner)):
                raise CtpGatewayClockError("TD_SOURCE_CHANGED")
        return (observation, now_mono) if internal else observation
    except CtpGatewayClockError:
        raise
    except Exception:
        raise CtpGatewayClockError("CLOCK_SOURCE_UNAVAILABLE") from None
