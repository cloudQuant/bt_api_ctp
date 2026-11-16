"""Production feed from CTP ``OnRtnInstrumentStatus`` ingress to the ledger.

This module is the missing wiring between the provider-local callback surface
and :mod:`bt_api_ctp.instrument_status_observation`: it consumes one exact
callback ingress payload (``ctp_trader_callback_ingress.v2`` as produced by
``CtpTraderCallbackIngressRecordV2.to_payload()``), selects only the seven
safe native fields, projects them with a *trusted* session generation and
trading day, and applies the result to an :class:`InstrumentStatusLedger`.

The ledger keeps the existing generation-scoped validity:

* a changed session generation or trading day invalidates every prior
  observation, including a prior source-unavailable mark;
* duplicate, out-of-order and sequence-conflict callbacks never overwrite the
  current status;
* an unavailable source stales every observation;
* ordering uses the callback's source sequence, never the receive clock.

Nothing here reads a wall clock, contacts a provider, sends an order, or
authorizes trading.  A malformed payload is refused with one finite code; it
never raises into a native callback thread and never invents a status.
"""

from __future__ import annotations

import threading

from bt_api_ctp.instrument_status_observation import (
    ACTION_REFUSED,
    CODE_FIELDS_INVALID,
    CODE_OBSERVATION_INVALID,
    CODE_RECEIVE_TIME_INVALID,
    CODE_SEQUENCE_INVALID,
    RECEIVE_CLOCK_MONOTONIC_NS,
    CtpInstrumentStatusObservationError,
    InstrumentStatusApplyResult,
    InstrumentStatusGenerationResult,
    InstrumentStatusLedger,
    InstrumentStatusSessionFacts,
    InstrumentStatusView,
    native_fields_from_ingress_payload,
    project_native_instrument_status,
)

__all__ = ("InstrumentStatusIngressBridge",)


def _refused(code: str) -> InstrumentStatusApplyResult:
    return InstrumentStatusApplyResult(False, ACTION_REFUSED, code)


class InstrumentStatusIngressBridge:
    """Thread-safe switch from ingress payloads to one provider ledger.

    ``session_generation`` is the provider's trusted connection generation
    (as text) and ``trading_day`` is the provider's trusted ``YYYYMMDD`` login
    day.  Both are supplied by the caller from accepted session facts; this
    class never derives them from the payload, a clock or the native status.
    """

    def __init__(self, *, session_generation: object, trading_day: object) -> None:
        # InstrumentStatusLedger validates both values and raises a finite
        # provider error; constructing a bridge with unproven facts fails.
        self._lock = threading.Lock()
        self._ledger = InstrumentStatusLedger(
            session_generation=session_generation, trading_day=trading_day
        )
        self._session_generation = session_generation
        self._trading_day = trading_day

    @property
    def session_generation(self) -> object:
        with self._lock:
            return self._session_generation

    @property
    def trading_day(self) -> object:
        with self._lock:
            return self._trading_day

    def begin_session_generation(
        self, session_generation: object, trading_day: object
    ) -> InstrumentStatusGenerationResult:
        """Advance trusted session facts and invalidate older observations."""

        with self._lock:
            result = self._ledger.begin_session_generation(session_generation, trading_day)
            self._session_generation = session_generation
            self._trading_day = trading_day
            return result

    def mark_source_unavailable(self) -> None:
        """Stale every observation because the provider cannot prove its source."""

        with self._lock:
            self._ledger.mark_source_unavailable()

    def observe_payload(self, payload: object) -> InstrumentStatusApplyResult:
        """Apply one ingress payload; refuse malformed input without raising."""

        try:
            if type(payload) is not dict:
                return _refused(CODE_FIELDS_INVALID)
            sequence = payload.get("sequence")
            receive_time = payload.get("monotonic_ns")
            if type(sequence) is not int or sequence < 1:
                return _refused(CODE_SEQUENCE_INVALID)
            if type(receive_time) is not int or receive_time < 1:
                return _refused(CODE_RECEIVE_TIME_INVALID)
            with self._lock:
                session_generation = self._session_generation
                trading_day = self._trading_day
            observation = project_native_instrument_status(
                native_fields_from_ingress_payload(payload),
                session=InstrumentStatusSessionFacts(
                    session_generation=session_generation,
                    trading_day=trading_day,
                    source_sequence=sequence,
                    receive_clock_domain=RECEIVE_CLOCK_MONOTONIC_NS,
                    receive_time=receive_time,
                    source_available=True,
                ),
            )
            return self._ledger.observe(observation)
        except CtpInstrumentStatusObservationError as exc:
            # Keep the provider's one finite refusal code (fields, receive
            # time, generation, ...).  It never carries a value.
            return _refused(exc.code)
        except Exception:
            # The ingress payload is untrusted native input.  Report a finite
            # refusal instead of leaking provider text or raising on a SPI
            # callback thread.
            return _refused(CODE_OBSERVATION_INVALID)

    def view(self, venue: object, native_symbol: object) -> InstrumentStatusView:
        """Return the fail-closed effective answer for one instrument."""

        return self._ledger.view(venue, native_symbol)
