"""Source-bound static futures rules from one borrowed CTP Trader session."""
from __future__ import annotations

import hashlib
import json
import math
import os
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from types import MappingProxyType

from bt_api_base.account_protocol import AccountKey, account_venue_contains_instrument_venue
from bt_api_base.market_protocol import MarketInstrumentKey
from bt_api_ctp.gateway_return_query import CtpGatewayReturnQueryReader
from bt_api_ctp.query import (
    QueryResult, _QuerySource, _QUERY_SOURCE_SEAL, _QUERY_MONOTONIC_CLOCK_DOMAIN,
    _QUERY_EVIDENCE_MAX_TTL_SECONDS, _query_records_digest,
)

_UTC = timezone.utc
_SEAL = object()
_MAX_OUTSTANDING = 8
_MAX_OBSERVATIONS = 128


class CtpGatewayInstrumentQueryError(RuntimeError):
    """Finite local failure; no raw native fields are included."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class CtpGatewayInstrumentRules:
    instrument: MarketInstrumentKey
    rules: Mapping[str, object]
    connection_generation: int
    trading_day: str
    completed_at_utc: datetime
    expires_monotonic: float
    _source: _QuerySource = field(repr=False, compare=False)
    _result: QueryResult = field(repr=False, compare=False)
    _binding: object = field(repr=False, compare=False)
    _seal: object = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        if self._seal is not _SEAL:
            raise CtpGatewayInstrumentQueryError("OBSERVATION_UNTRUSTED")


class _Handle:
    __slots__ = ()


def _text(row: Mapping, field_name: str) -> str:
    value = row.get(field_name)
    if (type(value) is not str or not value or value != value.strip()
            or not value.isprintable() or len(value.encode("utf-8")) > 256):
        raise CtpGatewayInstrumentQueryError("ROW_INVALID")
    return value


def _positive_int(row: Mapping, field_name: str) -> int:
    value = row.get(field_name)
    if type(value) is not int or not 0 < value < 2**31:
        raise CtpGatewayInstrumentQueryError("ROW_INVALID")
    return value


def _rules(row: object, instrument: MarketInstrumentKey) -> Mapping[str, object]:
    if not isinstance(row, Mapping):
        raise CtpGatewayInstrumentQueryError("ROW_INVALID")
    if (_text(row, "InstrumentID") != instrument.instrument_id
            or _text(row, "ExchangeID") != instrument.venue
            or row.get("ProductClass") != "1" or type(row.get("ProductClass")) is not str):
        raise CtpGatewayInstrumentQueryError("ROW_INVALID")
    product = _text(row, "ProductID")
    multiplier = _positive_int(row, "VolumeMultiple")
    minimum = _positive_int(row, "MinLimitOrderVolume")
    maximum = _positive_int(row, "MaxLimitOrderVolume")
    tick = row.get("PriceTick")
    if type(tick) not in (int, float) or (type(tick) is float and not math.isfinite(tick)):
        raise CtpGatewayInstrumentQueryError("ROW_INVALID")
    try:
        price_tick = Decimal(str(tick))
    except (ValueError, InvalidOperation):
        raise CtpGatewayInstrumentQueryError("ROW_INVALID") from None
    trading = row.get("IsTrading")
    if (not price_tick.is_finite() or not 0 < price_tick < Decimal("1e308") or minimum > maximum
            or type(trading) is not int or trading not in (0, 1)):
        raise CtpGatewayInstrumentQueryError("ROW_INVALID")
    # Only the native fields consumed here enter the public fingerprint.
    canonical = {"ExchangeID": instrument.venue, "InstrumentID": instrument.instrument_id,
                 "ProductID": product, "ProductClass": "1", "VolumeMultiple": multiplier,
                 "PriceTick": str(price_tick), "MinLimitOrderVolume": minimum,
                 "MaxLimitOrderVolume": maximum, "IsTrading": trading}
    fingerprint = hashlib.sha256(json.dumps(canonical, sort_keys=True, separators=(",", ":"),
                                            ensure_ascii=True).encode("ascii")).hexdigest()
    return MappingProxyType({
        "exchange_name": "CTP___FUTURE", "symbol": instrument.instrument_id,
        "asset_type": "future", "base_currency": product, "quote_currency": "CNY",
        "contract_type": "future", "linear": True, "contract_value": Decimal(multiplier),
        "contract_multiplier": Decimal(1), "price_tick": price_tick,
        "quantity_step": Decimal(1), "min_quantity": Decimal(minimum),
        "max_quantity": Decimal(maximum), "min_notional": None, "quantity_unit": "lots",
        "status": "trading" if trading == 1 else "disabled",
        "raw_rule_fingerprint": fingerprint, "source": "ctp_gateway_instrument_query",
        "available": True, "unavailable_reason": None,
    })


class CtpGatewayInstrumentQueryReader:
    """Borrow an existing exact Trader; never connect or start market data."""

    def __init__(self, trader, *, account_key: AccountKey) -> None:
        if (type(account_key) is not AccountKey or account_key.provider != "ctp"
                or not account_key.subaccount_id):
            raise CtpGatewayInstrumentQueryError("INVALID_CONFIGURATION")
        try:
            self._guard = CtpGatewayReturnQueryReader(
                trader, environment=account_key.environment,
                broker_id=account_key.subaccount_id, investor_id=account_key.account_id)
        except Exception:
            raise CtpGatewayInstrumentQueryError("SESSION_UNTRUSTED") from None
        self._trader = trader
        self._native_owner_trader = trader
        self._account_key = account_key
        self._native_owner_account_key = account_key
        self._gateway_session = None
        self._native_owner_gateway_session = None
        self._gateway_service_generation = None
        self._pid = os.getpid()
        self._thread = threading.current_thread()
        self._observations_lock = threading.Lock()
        try:
            with trader._query_state_lock:
                self._guard._capture_locked()
                self._login_identity = trader._current_login_identity_locked()
                if self._login_identity is None:
                    raise ValueError
        except Exception:
            raise CtpGatewayInstrumentQueryError("SESSION_UNTRUSTED") from None
        # Entries are [native handle, instrument, terminal, observation].
        self._pending: dict[_Handle, list] = {}
        self._observations: list[CtpGatewayInstrumentRules] = []

    def _check_owner(self) -> None:
        if os.getpid() != self._pid or threading.current_thread() is not self._thread:
            raise CtpGatewayInstrumentQueryError("OWNER_REQUIRED")

    def _check_session(self):
        try:
            session = self._gateway_session
            if (self._trader is not self._native_owner_trader
                    or self._account_key is not self._native_owner_account_key
                    or session is not self._native_owner_gateway_session):
                raise ValueError
            if session is not None:
                session._require_instrument_connected()
                if (session._trader is not self._trader or session._account_key is not self._account_key
                        or session._service_generation != self._gateway_service_generation):
                    raise ValueError
            with self._trader._query_state_lock:
                binding = self._guard._capture_locked()
            if session is not None:
                session._require_instrument_connected()
                if (session._trader is not self._trader or session._account_key is not self._account_key
                        or session._service_generation != self._gateway_service_generation):
                    raise ValueError
            return binding
        except Exception as exc:
            from bt_api_ctp.gateway_session import CtpGatewaySessionError

            if (type(exc) is CtpGatewaySessionError
                    and exc.code in {"INSTRUMENT_SESSION_BUSY", "INSTRUMENT_SESSION_RECONNECTING"}):
                raise CtpGatewayInstrumentQueryError(exc.code) from None
            raise CtpGatewayInstrumentQueryError("SESSION_CHANGED") from None

    def begin(self, instrument: MarketInstrumentKey, *, timeout_seconds: float = 5.0):
        self._check_owner()
        if (type(instrument) is not MarketInstrumentKey or instrument.provider != "ctp"
                or instrument.environment != self._account_key.environment
                or instrument.product_type != "future"
                or instrument.instrument_id != instrument.native_symbol
                or not account_venue_contains_instrument_venue(self._account_key, instrument.venue)
                or type(timeout_seconds) not in (int, float) or not math.isfinite(timeout_seconds)
                or not 0 < timeout_seconds <= 300):
            raise CtpGatewayInstrumentQueryError("INVALID_REQUEST")
        if len(self._pending) >= _MAX_OUTSTANDING:
            raise CtpGatewayInstrumentQueryError("CAPACITY_EXCEEDED")
        self._check_session()
        try:
            native = self._trader.begin_instruments_query(
                instrument_id=instrument.instrument_id, exchange_id=instrument.venue,
                timeout=timeout_seconds, max_records=2)
        except Exception:
            raise CtpGatewayInstrumentQueryError("QUERY_FAILED") from None
        handle = _Handle()
        self._pending[handle] = [native, instrument, False, None]
        return handle

    def _pending_for(self, handle):
        if type(handle) is not _Handle or handle not in self._pending:
            raise CtpGatewayInstrumentQueryError("HANDLE_UNTRUSTED")
        return self._pending[handle]

    def cancel(self, handle) -> bool:
        self._check_owner()
        entry = self._pending_for(handle)
        native = entry[0]
        if self._trader is not self._native_owner_trader:
            raise CtpGatewayInstrumentQueryError("HANDLE_UNTRUSTED")
        if entry[2]:
            return False
        try:
            cancelled = self._trader.cancel_instruments_query(native)
        except Exception:
            raise CtpGatewayInstrumentQueryError("QUERY_FAILED") from None
        if type(cancelled) is not bool:
            raise CtpGatewayInstrumentQueryError("QUERY_UNTRUSTED")
        if cancelled:
            entry[2] = True
            try:
                released = self._trader.release_instruments_query(native)
                if type(released) is not bool:
                    raise ValueError
                if released:
                    entry[0] = None
            except Exception:
                raise CtpGatewayInstrumentQueryError("QUERY_FAILED") from None
        return cancelled

    def release(self, handle) -> bool:
        """Release a terminal local handle; observations remain independently valid."""
        self._check_owner()
        entry = self._pending_for(handle)
        if self._trader is not self._native_owner_trader:
            raise CtpGatewayInstrumentQueryError("HANDLE_UNTRUSTED")
        if not entry[2]:
            return False
        if entry[0] is not None:
            try:
                if self._trader.release_instruments_query(entry[0]) is not True:
                    return False
            except Exception:
                raise CtpGatewayInstrumentQueryError("QUERY_FAILED") from None
        del self._pending[handle]
        return True

    def poll(self, handle) -> CtpGatewayInstrumentRules | None:
        self._check_owner()
        entry = self._pending_for(handle)
        native, instrument = entry[:2]
        self._check_session()
        if entry[2]:
            if entry[3] is None:
                raise CtpGatewayInstrumentQueryError("QUERY_FAILED")
            self.assert_current(entry[3])
            return entry[3]
        binding = self._guard._binding
        try:
            result = self._trader.poll_instruments_query(native)
        except Exception:
            raise CtpGatewayInstrumentQueryError("QUERY_FAILED") from None
        self._check_session()
        if result is None:
            return None
        entry[2] = True
        try:
            if self._trader.release_instruments_query(native) is not True:
                raise ValueError
            entry[0] = None
        except Exception:
            raise CtpGatewayInstrumentQueryError("QUERY_FAILED") from None
        source = self._validate_result(result, binding, instrument)
        if len(result.records) != 1:
            raise CtpGatewayInstrumentQueryError("ROW_COUNT_INVALID")
        rules = _rules(result.records[0], instrument)
        self._check_session()
        observation = CtpGatewayInstrumentRules(
            instrument, rules, binding.connection_generation, binding.trading_day,
            source.completed_at_utc, source.trusted_expires_monotonic,
            source, result, binding, _SEAL)
        with self._observations_lock:
            self._observations = [o for o in self._observations if time.monotonic() < o.expires_monotonic]
            if len(self._observations) >= _MAX_OBSERVATIONS:
                raise CtpGatewayInstrumentQueryError("CAPACITY_EXCEEDED")
            self._observations.append(observation)
        try:
            self.assert_current(observation)
        except Exception:
            with self._observations_lock:
                self._observations = [item for item in self._observations if item is not observation]
            raise
        entry[3] = observation
        return observation

    @staticmethod
    def _validate_result(result, binding, instrument):
        try:
            if type(result) is not QueryResult or type(result.query_source) is not _QuerySource:
                raise ValueError
            source = result.query_source
            expected = tuple(sorted({"InstrumentID": instrument.instrument_id,
                                     "ExchangeID": instrument.venue}.items()))
            now = time.monotonic()
            wall = datetime.now(_UTC)
            if (source._seal is not _QUERY_SOURCE_SEAL or source.issuer is not binding._issuer
                    or result.request_type != "instruments" or source.request_type != "instruments"
                    or type(result.request_id) is not int or not 0 < result.request_id <= 2_147_483_647
                    or type(source.request_id) is not int or source.request_id != result.request_id
                    or type(result.connection_generation) is not int
                    or result.connection_generation != binding.connection_generation
                    or type(source.connection_generation) is not int
                    or source.connection_generation != binding.connection_generation
                    or result.account_fingerprint != binding.account_fingerprint
                    or source.account_fingerprint != binding.account_fingerprint
                    or source.trading_day != binding.trading_day or source.broker_id != binding.broker_id
                    or source.investor_id != binding.investor_id or source.source_tags != binding._source_tags
                    or type(source.source_tags) is not tuple or len(source.source_tags) != 6
                    or any(type(v) is not str for v in source.source_tags[:4])
                    or any(type(v) is not int for v in source.source_tags[4:])
                    or source.request_filters != expected or source.request_intent_filters != expected
                    or source.request_parameters != () or source.request_intent_parameters != ()
                    or source.explicit_request_filters != ("ExchangeID", "InstrumentID")
                    or type(result.record_limit) is not int or result.record_limit != 2
                    or type(source.record_limit) is not int or source.record_limit != 2
                    or result.record_limit_exceeded is not False or source.record_limit_exceeded is not False
                    or result.complete is not True or result.is_last_seen is not True
                    or result.timed_out is not False or result.unsupported is not False
                    or type(result.late_callback_count) is not int or result.late_callback_count != 0
                    or result.error_code not in (None, 0) or type(result.error_code) not in (int, type(None))
                    or result.error_message != "" or type(result.error_message) is not str
                    or type(result.submit_code) is not int or result.submit_code != 0
                    or result.completion_origin != "success" or result.first_completion_origin != "success"
                    or type(result.records) is not tuple or len(result.records) > 2
                    or source.clock_domain_id != _QUERY_MONOTONIC_CLOCK_DOMAIN
                    or type(source.started_at_utc) is not datetime or type(source.completed_at_utc) is not datetime
                    or source.started_at_utc.utcoffset() != timedelta(0)
                    or source.completed_at_utc.utcoffset() != timedelta(0)
                    or result.started_at_utc != source.started_at_utc
                    or result.completed_at_utc != source.completed_at_utc
                    or not source.started_at_utc <= source.completed_at_utc <= wall
                    or type(source.started_monotonic) not in (int, float)
                    or type(source.completed_monotonic) not in (int, float)
                    or not math.isfinite(source.started_monotonic) or not math.isfinite(source.completed_monotonic)
                    or not binding._captured_monotonic <= source.started_monotonic <= source.completed_monotonic <= now
                    or type(source.trusted_expires_monotonic) not in (int, float)
                    or source.trusted_expires_monotonic != source.completed_monotonic + _QUERY_EVIDENCE_MAX_TTL_SECONDS
                    or not now < source.trusted_expires_monotonic
                    or type(source.trusted_expires_at_utc) is not datetime
                    or source.trusted_expires_at_utc != source.completed_at_utc + timedelta(seconds=_QUERY_EVIDENCE_MAX_TTL_SECONDS)
                    or wall >= source.trusted_expires_at_utc
                    or type(source.records_sha256) is not str
                    or _query_records_digest(result.records) != source.records_sha256):
                raise ValueError
            return source
        except Exception:
            raise CtpGatewayInstrumentQueryError("QUERY_UNTRUSTED") from None

    def assert_current(self, observation: CtpGatewayInstrumentRules) -> None:
        self._check_owner()
        with self._observations_lock:
            known = any(item is observation for item in self._observations)
        if (type(observation) is not CtpGatewayInstrumentRules or not known
                or observation._seal is not _SEAL):
            raise CtpGatewayInstrumentQueryError("OBSERVATION_UNTRUSTED")
        self._check_session()
        binding = self._guard._binding
        source = self._validate_result(observation._result, binding, observation.instrument)
        if (observation._binding is not self._guard._binding or observation._source is not source
                or observation.connection_generation != binding.connection_generation
                or observation.trading_day != binding.trading_day
                or observation.completed_at_utc != source.completed_at_utc
                or observation.expires_monotonic != source.trusted_expires_monotonic):
            raise CtpGatewayInstrumentQueryError("OBSERVATION_UNTRUSTED")
        self._check_session()
        if (time.monotonic() >= observation.expires_monotonic
                or datetime.now(_UTC) >= source.trusted_expires_at_utc):
            raise CtpGatewayInstrumentQueryError("OBSERVATION_UNTRUSTED")

    def observation_current(self, observation: CtpGatewayInstrumentRules) -> bool | None:
        """Probe existing evidence without I/O; None means a local lock is busy.

        This is a point-in-time check, not an atomic guarantee through a later
        transport response. It does not capture a new query session.
        """
        if os.getpid() != self._pid or type(observation) is not CtpGatewayInstrumentRules:
            return False
        if not self._observations_lock.acquire(blocking=False):
            return None
        try:
            if not any(item is observation for item in self._observations):
                return False
        finally:
            self._observations_lock.release()
        binding = self._guard._binding
        try:
            source = self._validate_result(observation._result, binding, observation.instrument)
            if (observation._seal is not _SEAL or observation._binding is not binding
                    or observation._source is not source
                    or observation.connection_generation != binding.connection_generation
                    or observation.trading_day != binding.trading_day
                    or observation.completed_at_utc != source.completed_at_utc
                    or observation.expires_monotonic != source.trusted_expires_monotonic):
                return False
        except Exception:
            return False
        trader = self._native_owner_trader
        if not trader._query_state_lock.acquire(blocking=False):
            return None
        try:
            session = self._gateway_session
            if (self._trader is not trader or self._guard._trader is not trader
                    or self._account_key is not self._native_owner_account_key
                    or session is not self._native_owner_gateway_session
                    or trader._api is not self._guard._api or trader._spi is not self._guard._spi
                    or self._guard._spi._native_api is not self._guard._api
                    or trader._session_native_api is not self._guard._api
                    or not self._guard._spi._is_current_locked()
                    or not trader._trade_callback_scope_matches_locked(self._guard._callback_scope)
                    or trader._current_login_identity_locked() is not self._login_identity
                    or trader._query_evidence_issuer is not binding._issuer
                    or trader._query_source_tags_locked() != binding._source_tags
                    or trader._connection_generation != binding.connection_generation
                    or trader._trading_day != binding.trading_day
                    or trader._bound_broker_id != binding.broker_id
                    or trader._bound_user_id != binding.investor_id
                    or trader._account_fingerprint != binding.account_fingerprint
                    or trader.ctp_env_profile != binding.environment_profile
                    or (session is not None and (
                        session._state != "connected" or session._trader is not trader
                        or session._account_key is not self._account_key
                        or session._service_generation != self._gateway_service_generation))):
                return False
            if not self._observations_lock.acquire(blocking=False):
                return None
            try:
                if not any(item is observation for item in self._observations):
                    return False
            finally:
                self._observations_lock.release()
            return bool(time.monotonic() < observation.expires_monotonic
                        and datetime.now(_UTC) < source.trusted_expires_at_utc)
        except Exception:
            return False
        finally:
            trader._query_state_lock.release()
