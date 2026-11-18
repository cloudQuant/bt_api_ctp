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

from bt_api_ctp.ctp.callback_ingress import (
    _MAX_RECORD_BYTES,
    CtpTraderCallbackArgumentV2,
    CtpTraderCallbackClass,
    CtpTraderCallbackFieldValueV2,
    CtpTraderCallbackIngressRecordV2,
    CtpTraderCallbackPhase,
    _canonical_bytes,
)
from bt_api_ctp.instrument_status_observation import (
    ACTION_REFUSED,
    CODE_FIELDS_INCOMPLETE,
    CODE_FIELDS_INVALID,
    CODE_GENERATION_MISMATCH,
    CODE_OBSERVATION_INVALID,
    CODE_RECEIVE_TIME_INVALID,
    CODE_SEQUENCE_INVALID,
    RECEIVE_CLOCK_MONOTONIC_NS,
    CtpInstrumentStatusObservationError,
    InstrumentStatusApplyResult,
    InstrumentStatusGenerationResult,
    InstrumentStatusLedger,
    InstrumentStatusPriorPause,
    InstrumentStatusSessionFacts,
    InstrumentStatusView,
    native_fields_from_ingress_payload,
    project_native_instrument_status,
)

__all__ = ("InstrumentStatusIngressBridge",)


def _refused(code: str) -> InstrumentStatusApplyResult:
    return InstrumentStatusApplyResult(False, ACTION_REFUSED, code)


def _snapshot_payload(payload: object) -> object:
    """Detach bounded exact built-in values before validating any fields."""

    remaining = [_MAX_RECORD_BYTES]

    def copy_value(value: object, depth: int) -> object:
        remaining[0] -= 1
        if remaining[0] < 0 or depth > 16:
            raise ValueError("callback snapshot exceeds bounds")
        if type(value) is str:
            remaining[0] -= len(value.encode("utf-8", errors="strict"))
            if remaining[0] < 0:
                raise ValueError("callback snapshot exceeds bounds")
            return value
        if type(value) in (int, float, bool) or value is None:
            return value
        if type(value) is list:
            return [copy_value(item, depth + 1) for item in value]
        if type(value) is dict:
            result = {}
            for key, item in value.items():
                if type(key) is not str:
                    raise ValueError("callback snapshot key invalid")
                result[copy_value(key, depth + 1)] = copy_value(item, depth + 1)
            return result
        raise ValueError("callback snapshot type invalid")

    return copy_value(payload, 0)


def _record_from_snapshot(payload: dict[str, object]) -> CtpTraderCallbackIngressRecordV2:
    """Apply the native record contract to a detached scalar-only snapshot."""

    arguments = tuple(
        CtpTraderCallbackArgumentV2(
            slot=item["argument_slot"],
            name=item["name"],
            present=item["present"],
            scalar_captured=item["scalar_captured"],
            value=item["value"],
            pointer_is_null=item["pointer_is_null"],
        )
        for item in payload["named_args"]
    )
    fields = tuple(
        (
            item["argument_slot"],
            item["field_name"],
            CtpTraderCallbackFieldValueV2(
                present=item["present"],
                scalar_captured=item["scalar_captured"],
                value=item["value"],
            ),
        )
        for item in payload["flattened_fields"]
    )
    record = CtpTraderCallbackIngressRecordV2(
        owner_intent_id=payload["owner_intent_id"],
        callback_name=payload["callback_name"],
        callback_class=CtpTraderCallbackClass(payload["callback_class"]),
        source_phase=CtpTraderCallbackPhase(payload["phase"]),
        source_instance_id=payload["source_instance_id"],
        native_client_epoch=payload["native_client_epoch"],
        native_api_source_id=payload["native_api_source_id"],
        native_spi_source_id=payload["native_spi_source_id"],
        native_api_generation=payload["native_api_generation"],
        source_connection_generation=payload["source_connection_generation"],
        connection_generation=payload["connection_generation"],
        source_sequence=payload["sequence"],
        callback_monotonic_ns=payload["monotonic_ns"],
        callback_arguments=arguments,
        flattened_fields=fields,
        capture_complete=payload["capture_complete"],
        missing_field_names=tuple(payload["missing_getters"]),
        canonical_sha256=payload["digest"],
    )
    if record.to_payload() != payload:
        raise ValueError("callback snapshot shape invalid")
    return record


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
            snapshot = _snapshot_payload(payload)
            encoded = _canonical_bytes(snapshot)
            if len(encoded) > _MAX_RECORD_BYTES:
                return _refused(CODE_FIELDS_INVALID)
            sequence = snapshot.get("sequence")
            receive_time = snapshot.get("monotonic_ns")
            if type(sequence) is not int or sequence < 1:
                return _refused(CODE_SEQUENCE_INVALID)
            if type(receive_time) is not int or receive_time < 1:
                return _refused(CODE_RECEIVE_TIME_INVALID)
            if snapshot.get("capture_complete") is not True:
                return _refused(CODE_FIELDS_INCOMPLETE)
            if (
                set(snapshot)
                != {
                    "schema",
                    "owner_intent_id",
                    "callback_name",
                    "callback_class",
                    "phase",
                    "source_instance_id",
                    "native_client_epoch",
                    "native_api_source_id",
                    "native_spi_source_id",
                    "native_api_generation",
                    "source_connection_generation",
                    "connection_generation",
                    "sequence",
                    "monotonic_ns",
                    "named_args",
                    "flattened_fields",
                    "capture_complete",
                    "missing_getters",
                    "digest",
                }
                or type(snapshot.get("named_args")) is not list
            ):
                return _refused(CODE_FIELDS_INVALID)
            record = _record_from_snapshot(snapshot)
            if (
                record.source_phase is not CtpTraderCallbackPhase.ACTIVE
                or record.callback_class is not CtpTraderCallbackClass.AUDIT_INFORMATIONAL
            ):
                return _refused(CODE_FIELDS_INVALID)
            fields = native_fields_from_ingress_payload(record.to_payload())
            with self._lock:
                if str(record.connection_generation) != self._session_generation:
                    return _refused(CODE_GENERATION_MISMATCH)
                observation = project_native_instrument_status(
                    fields,
                    session=InstrumentStatusSessionFacts(
                        session_generation=self._session_generation,
                        trading_day=self._trading_day,
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

    @property
    def source_available(self) -> bool:
        return self._ledger.source_available

    def prior_pause(
        self, venue: object, native_symbol: object
    ) -> InstrumentStatusPriorPause | None:
        return self._ledger.prior_pause(venue, native_symbol)

    def export_prior_pauses(self) -> tuple[InstrumentStatusPriorPause, ...]:
        return self._ledger.export_prior_pauses()

    def seed_prior_pauses(
        self, prior_pauses: object, *, reason: str | None = None
    ) -> None:
        with self._lock:
            self._ledger.seed_prior_pauses(prior_pauses, reason=reason)

    def carry_forward_from(
        self, previous_bridge: object, *, reason: str | None = None
    ) -> None:
        if type(previous_bridge) is not InstrumentStatusIngressBridge or previous_bridge is self:
            return
        previous_ledger = previous_bridge._ledger
        with self._lock:
            self._ledger.carry_forward_from(previous_ledger, reason=reason)
