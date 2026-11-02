"""CTP-owned registered-simulation native order gate and callback UNKNOWN latch.

This module is the SDK home for the CTP-specific gate/session and callback
ambiguity behavior formerly hosted by Backtrader's ``RegisteredSimWrapper``.
It uses the exact ``TraderClient`` gate as the authority; local configuration,
session projections, and callback facts can only narrow admission.  It does
not allocate an OrderRef, issue an order reservation, or replace the durable
managed execution owner used by the unified facade.

One old recovery responsibility is intentionally not reproduced here yet:
the former Store retires its native UNKNOWN only after its durable Journal,
fresh complete query, exact terminal projection, and Broker event queue agree.
The SDK currently has no typed authority that verifies that full legacy proof.
The UNKNOWN remains sticky; a QueryFacade lease or caller boolean cannot clear
it. The Store-side proof path must be migrated as a typed SDK verifier before
this authority can safely retire the record. The legacy ``InvestUnitID`` route
helper also has no CTP SDK contract yet; native route writes must stay
unsupported until that exact mapping is defined in the SDK.

The registered-simulation gate is deliberately distinct from the managed
callback-ingress lease path.  For that path, the execution owner must retain
its exact Store claim and call ``TraderClient.acquire_managed_native_call_lease``
before invoking a native request.
"""

from __future__ import annotations

import contextlib
import inspect
import re
import threading
import time
from collections import OrderedDict
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from types import MappingProxyType
from typing import Any, Iterator


_HASH_RE = re.compile(r"^[0-9a-f]{64}$", re.ASCII)
_TRADING_DAY_RE = re.compile(r"^[0-9]{8}$", re.ASCII)
_CALLBACK_UNKNOWN_LIMIT = 1_024


class CtpNativeOrderAuthorityError(RuntimeError):
    """Finite failure from the SDK-owned CTP native-order gate."""

    def __init__(self, code: str, *, evidence: CtpNativeDispatchEvidence | None = None) -> None:
        if type(code) is not str or not code or code != code.strip():
            code = "ctp_native_order_authority_failed"
        self.code = code
        self.evidence = evidence
        super().__init__(code)


@dataclass(frozen=True)
class CtpNativeSessionIdentity:
    """A detached observation of one CTP client login/session generation."""

    account_fingerprint: str = field(repr=False)
    connection_generation: int
    trading_day: str
    read_only_ready: bool
    trading_ready: bool
    front_id: int | None
    session_id: int | None

    def __post_init__(self) -> None:
        if type(self.account_fingerprint) is not str or not self.account_fingerprint:
            raise CtpNativeOrderAuthorityError("ctp_native_session_account_invalid")
        if type(self.connection_generation) is not int or self.connection_generation <= 0:
            raise CtpNativeOrderAuthorityError("ctp_native_session_generation_invalid")
        if type(self.trading_day) is not str or _TRADING_DAY_RE.fullmatch(self.trading_day) is None:
            raise CtpNativeOrderAuthorityError("ctp_native_session_trading_day_invalid")
        if type(self.read_only_ready) is not bool or type(self.trading_ready) is not bool:
            raise CtpNativeOrderAuthorityError("ctp_native_session_readiness_invalid")
        if (self.front_id is None) != (self.session_id is None):
            raise CtpNativeOrderAuthorityError("ctp_native_session_ids_incomplete")
        if self.front_id is not None and (
            type(self.front_id) is not int
            or self.front_id <= 0
            or type(self.session_id) is not int
            or self.session_id <= 0
        ):
            raise CtpNativeOrderAuthorityError("ctp_native_session_ids_invalid")


@dataclass(frozen=True)
class CtpCallbackUnknownFacts:
    """Bounded sticky projection of unresolved CTP order-status callbacks."""

    order_refs: tuple[str, ...] | None
    order_count: int | None
    unresolved: bool
    identity_incomplete: bool
    overflow: bool
    missing_order_ref: bool


@dataclass(frozen=True)
class CtpNativeDispatchEvidence:
    """Detached evidence about one CTP native call boundary.

    ``KNOWN`` means only that the exact SDK method returned its documented
    integer zero.  It is dispatch evidence, never an order acknowledgement or
    fill.  A missing/ambiguous result is sticky ``UNKNOWN`` and is never
    automatically replayable.
    """

    sequence: int
    operation: str
    status: str
    request_id: int
    instrument_id: str
    exchange_id: str
    order_ref: str | None
    order_sys_id: str | None
    front_id: int | None
    session_id: int | None
    broker_id: str | None
    investor_id: str | None
    user_id: str | None
    native_return_code: int | None
    error_type: str | None
    observed_at_utc: str

    @property
    def resend_permitted(self) -> bool:
        """A native request is never replayed automatically."""

        return False


class _CtpNativeDispatchTicket:
    """One-use local observer for the result of a native call boundary."""

    __slots__ = ("_authority", "_identity", "_key", "_sequence", "_finished")

    def __init__(
        self,
        authority: "CtpRegisteredSimNativeAuthority",
        identity: Mapping[str, Any],
        key: str,
        sequence: int,
    ) -> None:
        self._authority = authority
        self._identity = identity
        self._key = key
        self._sequence = sequence
        self._finished = False

    def record_native_return(self, value: Any) -> CtpNativeDispatchEvidence:
        """Record the exact SDK return value; nonzero or non-int is UNKNOWN."""

        if self._finished:
            raise CtpNativeOrderAuthorityError("ctp_native_dispatch_ticket_reused")
        self._finished = True
        evidence = self._authority._finish_dispatch_ticket(
            self, native_return_code=value if type(value) is int else None
        )
        if evidence.status != "KNOWN":
            raise CtpNativeOrderAuthorityError(
                "ctp_native_dispatch_outcome_unknown", evidence=evidence
            )
        return evidence

    def record_pre_dispatch_rejection(self, error: BaseException) -> bool:
        """Close only a canonical CTP cancel-gate rejection with absent evidence."""

        if self._finished:
            raise CtpNativeOrderAuthorityError("ctp_native_dispatch_ticket_reused")
        accepted = self._authority._is_verified_pre_dispatch_rejection(self, error)
        if accepted:
            self._finished = True
            self._authority._finish_pre_dispatch_ticket(self)
        return accepted


class CtpRegisteredSimNativeAuthority:
    """Own a direct registered-simulation gate for one exact CTP client.

    ``allow_writes`` is only the constructor-time local switch inherited from
    the former Store adapter.  It never arms the native gate.  An arm still
    requires the SDK's registered-simulation admission, exact front/session,
    one-shot native capability, and fresh gate revalidation on every call.
    """

    def __init__(
        self,
        client: Any,
        *,
        allow_writes: bool = False,
        history_limit: int = 64,
        callback_unknown_limit: int = _CALLBACK_UNKNOWN_LIMIT,
        terminal_order_limit: int = 512,
        cancel_target_limit: int = 8,
    ) -> None:
        trader_type = _canonical_trader_client_type()
        if trader_type is None or type(client) is not trader_type:
            raise CtpNativeOrderAuthorityError("ctp_native_client_unavailable")
        if type(allow_writes) is not bool:
            raise TypeError("allow_writes must be bool")
        if type(history_limit) is not int or history_limit <= 0:
            raise ValueError("history_limit must be a positive int")
        if type(callback_unknown_limit) is not int or callback_unknown_limit <= 0:
            raise ValueError("callback_unknown_limit must be a positive int")
        if type(terminal_order_limit) is not int or terminal_order_limit <= 0:
            raise ValueError("terminal_order_limit must be a positive int")
        if type(cancel_target_limit) is not int or cancel_target_limit <= 0:
            raise ValueError("cancel_target_limit must be a positive int")

        self._client = client
        self._allow_writes = allow_writes
        self._lock = threading.RLock()
        self._observation_lock = threading.Lock()
        self._capability: object | None = None
        self._gate_state: Mapping[str, Any] | None = None
        self._armed_key: tuple[str, str] | None = None
        self._expected_session: CtpNativeSessionIdentity | None = None
        self._frozen = False
        self._freeze_reason = ""
        self._arm_templates: Mapping[tuple[str, str], Mapping[str, str]] = MappingProxyType({})
        self._arm_templates_configured = False
        self._arm_history: list[dict[str, Any]] = []
        self._history_limit = history_limit
        self._dispatch_history: list[CtpNativeDispatchEvidence] = []
        self._dispatch_sequence = 0
        self._unknown_dispatch: CtpNativeDispatchEvidence | None = None
        self._active_dispatch_tickets: dict[int, _CtpNativeDispatchTicket] = {}
        self._order_state: OrderedDict[str, str] = OrderedDict()
        self._terminal_order_limit = terminal_order_limit
        self._known_order_refs: OrderedDict[str, None] = OrderedDict()
        self._known_order_ref_limit = max(1_024, history_limit * 8)
        self._dispatching_order_refs: set[str] = set()
        self._cancel_target_limit = cancel_target_limit
        self._cancel_target_issuer: Any = None
        self._cancel_target_seal: Any = None
        self._cancel_targets: dict[int, Any] = {}
        self._active_cancel_target_id: int | None = None
        self._callback_unknown_orders: OrderedDict[str | None, None] = OrderedDict()
        self._callback_unknown_limit = callback_unknown_limit
        self._callback_unknown_overflow = False
        self._callback_unknown_identity_incomplete = False
        self._callback_unknown_missing_order_ref = False
        self._callback_unknown_projection = CtpCallbackUnknownFacts(
            (), 0, False, False, False, False
        )
        self._last_arm_snapshot: Mapping[str, Any] | None = None

    def is_bound_to_client(self, client: Any) -> bool:
        """Return whether this authority owns the exact canonical client.

        SDK composition uses this identity check when attaching the
        simulation-only authority to a selected provider. Matching session
        fields are insufficient: two distinct TraderClient objects can report
        the same front/session values.
        """

        trader_type = _canonical_trader_client_type()
        return trader_type is not None and type(client) is trader_type and self._client is client

    @property
    def allow_writes(self) -> bool:
        """Return the immutable local switch; this is not an authorization."""

        return self._allow_writes

    @staticmethod
    def capture_session(client: Any) -> CtpNativeSessionIdentity:
        """Read a detached session identity through the canonical SDK getter."""

        trader_type = _canonical_trader_client_type()
        if trader_type is None or type(client) is not trader_type:
            raise CtpNativeOrderAuthorityError("ctp_native_client_unavailable")
        getter = getattr(client, "get_session_state", None)
        if getattr(getter, "__func__", None) is not getattr(
            trader_type, "get_session_state", None
        ):
            raise CtpNativeOrderAuthorityError("ctp_native_session_getter_unavailable")
        lock = getattr(client, "_query_state_lock", None)
        if lock is None:
            raise CtpNativeOrderAuthorityError("ctp_native_session_lock_unavailable")
        try:
            with lock:
                state = getter()
        except BaseException as exc:
            if not isinstance(exc, Exception):
                raise
            raise CtpNativeOrderAuthorityError("ctp_native_session_unobserved") from None
        if not isinstance(state, Mapping):
            raise CtpNativeOrderAuthorityError("ctp_native_session_unobserved")
        front_id = state.get("front_id")
        session_id = state.get("session_id")
        if front_id in (None, 0) and session_id in (None, 0):
            front_id = session_id = None
        elif type(front_id) is not int or type(session_id) is not int:
            raise CtpNativeOrderAuthorityError("ctp_native_session_ids_invalid")
        try:
            return CtpNativeSessionIdentity(
                account_fingerprint=state.get("account_fingerprint"),
                connection_generation=state.get("connection_generation"),
                trading_day=state.get("trading_day"),
                read_only_ready=state.get("read_only_ready"),
                trading_ready=state.get("trading_ready"),
                front_id=front_id,
                session_id=session_id,
            )
        except CtpNativeOrderAuthorityError:
            raise

    @contextlib.contextmanager
    def expected_session_scope(
        self, expected: CtpNativeSessionIdentity
    ) -> Iterator[CtpNativeSessionIdentity]:
        """Hold this authority lock while work is bound to one live session."""

        if type(expected) is not CtpNativeSessionIdentity:
            raise CtpNativeOrderAuthorityError("ctp_native_expected_session_untyped")
        with self._lock:
            current = self._current_session()
            self._require_session_match(current, expected, require_trading_ready=True)
            prior = self._expected_session
            if prior is not None and prior != expected:
                raise CtpNativeOrderAuthorityError("ctp_native_nested_session_conflict")
            self._expected_session = expected
            try:
                yield expected
            finally:
                self._expected_session = prior

    def configure_arm_templates(self, templates: Mapping[Any, Any]) -> None:
        """Seal an exact instrument/exchange allowlist for this authority."""

        if not isinstance(templates, Mapping):
            raise ValueError("arm templates must be a mapping")
        prepared: dict[tuple[str, str], Mapping[str, str]] = {}
        for raw_key, raw_value in templates.items():
            if not isinstance(raw_key, tuple) or len(raw_key) != 2:
                raise ValueError("each arm-template key must be an instrument/exchange pair")
            if not isinstance(raw_value, Mapping):
                raise ValueError("each arm template must be a mapping")
            instrument = _exact_text(raw_key[0], "InstrumentID")
            exchange = _exact_text(raw_key[1], "ExchangeID")
            template = {
                "md_front": _exact_text(raw_value.get("md_front"), "md_front"),
                "strategy_identity_sha256": _exact_hash(
                    raw_value.get("strategy_identity_sha256"), "strategy_identity_sha256"
                ),
                "execution_cycle_id": _exact_text(
                    raw_value.get("execution_cycle_id"), "execution_cycle_id"
                ),
                "preflight_sha256": _exact_hash(
                    raw_value.get("preflight_sha256"), "preflight_sha256"
                ),
            }
            prepared[(instrument, exchange)] = MappingProxyType(template)
        immutable = MappingProxyType(prepared)
        with self._lock:
            self._require_armable()
            if self._arm_templates_configured:
                if dict(self._arm_templates) != prepared:
                    raise CtpNativeOrderAuthorityError("ctp_native_arm_templates_immutable")
                return
            if self._capability is not None:
                raise CtpNativeOrderAuthorityError("ctp_native_disarm_before_template_change")
            self._arm_templates = immutable
            self._arm_templates_configured = True

    def arm_registered_sim_execution(
        self,
        instrument_id: str,
        exchange_id: str = "",
        md_front: str = "",
        *,
        strategy_identity_sha256: str,
        execution_cycle_id: str,
        preflight_sha256: str,
    ) -> Mapping[str, Any]:
        """Obtain and verify the SDK's one-instrument simulation capability."""

        instrument_id = _exact_text(instrument_id, "InstrumentID")
        exchange_id = _exact_text(exchange_id, "ExchangeID")
        md_front = _exact_text(md_front, "md_front")
        strategy_identity_sha256 = _exact_hash(
            strategy_identity_sha256, "strategy_identity_sha256"
        )
        execution_cycle_id = _exact_text(execution_cycle_id, "execution_cycle_id")
        preflight_sha256 = _exact_hash(preflight_sha256, "preflight_sha256")
        key = (instrument_id, exchange_id)

        with self._lock:
            self._require_armable()
            if self._capability is not None:
                raise CtpNativeOrderAuthorityError("ctp_native_disarm_before_rearm")
            current = self._current_session()
            expected = self._expected_session
            self._require_session_match(current, expected, require_trading_ready=True)
            arm = _canonical_bound_method(self._client, "arm_execution_for_registered_sim")
            if arm is None:
                raise CtpNativeOrderAuthorityError("ctp_native_sim_admission_unavailable")
            try:
                admitted = arm(
                    instrument_id=instrument_id,
                    exchange_id=exchange_id,
                    md_front=md_front,
                    strategy_identity_sha256=strategy_identity_sha256,
                    execution_cycle_id=execution_cycle_id,
                    preflight_sha256=preflight_sha256,
                )
            except BaseException as exc:
                self._freeze("native_admission_outcome_unknown")
                if not isinstance(exc, Exception):
                    raise
                raise CtpNativeOrderAuthorityError(
                    "ctp_native_sim_admission_failed"
                ) from None
            if not isinstance(admitted, tuple) or len(admitted) != 2 or admitted[0] is None:
                self._freeze("native_admission_result_unobserved")
                raise CtpNativeOrderAuthorityError("ctp_native_sim_admission_unobserved")
            capability, admitted_state = admitted
            self._capability = capability
            self._armed_key = key
            try:
                admitted_snapshot = self._gate_snapshot(admitted_state, key)
                if admitted_snapshot is None or not self._gate_matches_session(
                    admitted_snapshot, current
                ):
                    raise CtpNativeOrderAuthorityError("ctp_native_sim_admission_state_invalid")
                observed = self._read_gate_state()
                current_snapshot = self._gate_snapshot(observed, key)
                if current_snapshot != admitted_snapshot:
                    raise CtpNativeOrderAuthorityError("ctp_native_sim_admission_changed")
                after = self._current_session()
                self._require_session_match(after, current, require_trading_ready=True)
                self._gate_state = MappingProxyType(dict(observed))
                self._last_arm_snapshot = MappingProxyType(dict(admitted_snapshot))
                self._append_arm_history(instrument_id, exchange_id)
                return MappingProxyType(
                    {
                        "armed": True,
                        "instrument": instrument_id,
                        "exchange_id": exchange_id,
                        "state": MappingProxyType(dict(admitted_snapshot)),
                    }
                )
            except BaseException:
                self._freeze("native_admission_verification_failed")
                try:
                    self._disarm_locked("native_admission_verification_failed")
                except BaseException:
                    pass
                raise

    def ensure_registered_sim_arm_for(
        self, instrument_id: str, exchange_id: str
    ) -> Mapping[str, Any]:
        """Keep or switch the single armed route using sealed templates."""

        instrument_id = _exact_text(instrument_id, "InstrumentID")
        exchange_id = _exact_text(exchange_id, "ExchangeID")
        key = (instrument_id, exchange_id)
        with self._lock:
            self._require_armable()
            current = self._current_session()
            self._require_session_match(
                current, self._expected_session, require_trading_ready=True
            )
            template = self._arm_templates.get(key)
            if self._arm_templates_configured and template is None:
                raise CtpNativeOrderAuthorityError("ctp_native_arm_template_missing")
            if self._current_grant_matches(key):
                return MappingProxyType(
                    {"armed": True, "instrument": instrument_id, "switched": False}
                )
            observed = self._read_gate_state()
            if not isinstance(observed, Mapping):
                raise CtpNativeOrderAuthorityError("ctp_native_gate_state_unobserved")
            armed = observed.get("armed")
            switched = False
            if armed is True:
                if self._capability is None:
                    raise CtpNativeOrderAuthorityError("ctp_native_gate_has_unowned_grant")
                if not self._current_grant_matches(self._armed_key):
                    raise CtpNativeOrderAuthorityError("ctp_native_gate_grant_stale")
                switched = self._armed_key != key
                self._disarm_locked("registered_sim_instrument_switch")
            elif armed is False:
                switched = self._armed_key is not None
                self._capability = None
                self._armed_key = None
                self._gate_state = None
            else:
                raise CtpNativeOrderAuthorityError("ctp_native_gate_state_malformed")
            if template is None:
                raise CtpNativeOrderAuthorityError("ctp_native_arm_template_unconfigured")
            result = self.arm_registered_sim_execution(
                instrument_id,
                exchange_id,
                template["md_front"],
                strategy_identity_sha256=template["strategy_identity_sha256"],
                execution_cycle_id=template["execution_cycle_id"],
                preflight_sha256=template["preflight_sha256"],
            )
            return MappingProxyType({**dict(result), "switched": switched})

    def disarm_registered_sim_execution(
        self, reason: str = "registered_sim_disarmed"
    ) -> Mapping[str, Any]:
        """Revoke the exact local capability and return detached SDK state."""

        reason = _bounded_reason(reason)
        with self._lock:
            return MappingProxyType(self._disarm_locked(reason))

    def freeze_registered_sim_execution(self, reason: str) -> Mapping[str, Any]:
        """Freeze this authority and attempt one SDK-native gate revocation."""

        reason = _bounded_reason(reason)
        with self._lock:
            self._freeze(reason)
            result = self._disarm_locked(reason)
            return MappingProxyType(
                {"frozen": True, "reason": self._freeze_reason, "native": result["native"]}
            )

    def require_write(self, instrument_id: str, exchange_id: str) -> None:
        """Revalidate session, gate, sticky UNKNOWN, and the exact CTP route."""

        instrument_id = _exact_text(instrument_id, "InstrumentID")
        exchange_id = _exact_text(exchange_id, "ExchangeID")
        key = (instrument_id, exchange_id)
        with self._lock:
            self._require_armable()
            if self.callback_unknown_facts().unresolved:
                raise CtpNativeOrderAuthorityError("ctp_native_callback_unknown_unresolved")
            expected = self._expected_session
            current = self._current_session()
            self._require_session_match(current, expected, require_trading_ready=True)
            if not self._current_grant_matches(key):
                raise CtpNativeOrderAuthorityError("ctp_native_gate_grant_mismatch")
            require_write = _canonical_bound_method(self._client, "require_execution_write")
            if require_write is None or self._capability is None:
                raise CtpNativeOrderAuthorityError("ctp_native_write_gate_unavailable")
            try:
                require_write(self._capability, instrument_id, exchange_id)
            except BaseException as exc:
                if not isinstance(exc, Exception):
                    self._freeze_after_interruption("native_write_admission_interrupted")
                    raise
                raise CtpNativeOrderAuthorityError("ctp_native_write_rejected") from None
            after = self._current_session()
            self._require_session_match(after, current, require_trading_ready=True)
            if not self._current_grant_matches(key):
                self._freeze_after_interruption("native_gate_changed_during_admission")
                raise CtpNativeOrderAuthorityError("ctp_native_gate_changed_during_admission")

    @contextlib.contextmanager
    def native_write_scope(
        self,
        instrument_id: str,
        exchange_id: str,
        *,
        order_ref: str | None = None,
        operation: str | None = None,
        request_id: int | None = None,
        dispatch_identity: Mapping[str, Any] | None = None,
        cancel_target: Any = None,
    ) -> Iterator[_CtpNativeDispatchTicket | None]:
        """Serialize one admitted native write with callback UNKNOWN publication.

        The caller must verify its durable managed claim and explicit SDK
        authorization before entering. This scope supplies the CTP-local
        session/gate boundary and never upgrades a facade authorization.

        ``operation``, ``request_id``, and a detached ``dispatch_identity``
        are mandatory. The yielded one-use ticket must
        record the exact native return with ``record_native_return``. Leaving
        without that observation, or raising after entering the native call,
        records UNKNOWN and revokes the local gate. A canonical SDK cancel
        gate rejection can be marked pre-dispatch only through the ticket's
        typed evidence check. No SWIG field object is retained or inspected.

        ``cancel_target`` is optional for legacy direct cancels. If supplied,
        it must have been registered by this authority after the Store has
        completed its durable owner/query checks; the exact detached native
        Action identity is checked again here and the registration is consumed
        on exit.
        """

        instrument_id = _exact_text(instrument_id, "InstrumentID")
        exchange_id = _exact_text(exchange_id, "ExchangeID")
        state_ref = _exact_text(order_ref, "OrderRef") if order_ref is not None else ""
        with self._lock:
            if operation is None:
                raise CtpNativeOrderAuthorityError(
                    "ctp_native_dispatch_identity_required"
                )
            self.require_write(instrument_id, exchange_id)
            ticket: _CtpNativeDispatchTicket | None = None
            target_id: int | None = None
            if operation is not None:
                identity = _normalize_dispatch_identity(
                    dispatch_identity,
                    operation=operation,
                    request_id=request_id,
                    instrument_id=instrument_id,
                    exchange_id=exchange_id,
                    order_ref=state_ref or None,
                )
                if cancel_target is not None:
                    if operation != "cancel":
                        raise CtpNativeOrderAuthorityError(
                            "ctp_native_cancel_target_operation_mismatch"
                        )
                    target_id = self._claim_cancel_target_locked(
                        cancel_target,
                        identity,
                        instrument_id=instrument_id,
                        exchange_id=exchange_id,
                        request_id=request_id,
                    )
                elif operation == "cancel" and self._active_cancel_target_id is not None:
                    raise CtpNativeOrderAuthorityError(
                        "ctp_native_cancel_target_dispatch_conflict"
                    )
                state_ref = (
                    state_ref
                    or identity["order_ref"]
                    or "UNKNOWN-{}-{}".format(operation, request_id)
                )
            with self._observation_lock:
                if self._callback_unknown_projection.unresolved:
                    if target_id is not None:
                        self._discard_cancel_target_locked(target_id, cancel_target)
                    raise CtpNativeOrderAuthorityError(
                        "ctp_native_callback_unknown_unresolved"
                    )
                if state_ref and state_ref in self._dispatching_order_refs:
                    if target_id is not None:
                        self._discard_cancel_target_locked(target_id, cancel_target)
                    raise CtpNativeOrderAuthorityError("ctp_native_order_dispatch_reused")
                if state_ref:
                    self._dispatching_order_refs.add(state_ref)
            if operation is not None:
                self._dispatch_sequence += 1
                ticket = _CtpNativeDispatchTicket(
                    self, identity, state_ref, self._dispatch_sequence
                )
                self._active_dispatch_tickets[id(ticket)] = ticket
            try:
                yield ticket
            except BaseException as exc:
                if ticket is not None and not ticket._finished:
                    if not ticket.record_pre_dispatch_rejection(exc):
                        ticket._finished = True
                        evidence = self._finish_dispatch_ticket(
                            ticket, error_type=type(exc).__name__
                        )
                        if isinstance(exc, Exception):
                            raise CtpNativeOrderAuthorityError(
                                "ctp_native_dispatch_outcome_unknown", evidence=evidence
                            ) from None
                raise
            else:
                if ticket is not None and not ticket._finished:
                    ticket._finished = True
                    evidence = self._finish_dispatch_ticket(
                        ticket, error_type="native_return_unobserved"
                    )
                    raise CtpNativeOrderAuthorityError(
                        "ctp_native_dispatch_outcome_unknown", evidence=evidence
                    )
            finally:
                if ticket is not None:
                    self._active_dispatch_tickets.pop(id(ticket), None)
                if state_ref:
                    with self._observation_lock:
                        self._dispatching_order_refs.discard(state_ref)
                if target_id is not None:
                    self._discard_cancel_target_locked(target_id, cancel_target)

    @contextlib.contextmanager
    def native_cancel_scope(
        self,
        target: Any,
        field_identity: Mapping[str, Any],
        *,
        request_id: int,
    ) -> Iterator[_CtpNativeDispatchTicket]:
        """Admit one exact one-shot Action against a registered current target.

        ``field_identity`` is a detached scalar projection of the native Action
        fields. It must be captured by the CTP writer before this call; passing
        a SWIG object is intentionally unsupported.
        """

        if not isinstance(field_identity, Mapping):
            raise CtpNativeOrderAuthorityError("ctp_native_cancel_field_identity_untyped")
        instrument_id = _exact_text(field_identity.get("instrument_id"), "InstrumentID")
        exchange_id = _exact_text(field_identity.get("exchange_id"), "ExchangeID")
        order_ref = _exact_text(field_identity.get("order_ref"), "OrderRef")
        with self.native_write_scope(
            instrument_id,
            exchange_id,
            order_ref=order_ref,
            operation="cancel",
            request_id=request_id,
            dispatch_identity=field_identity,
            cancel_target=target,
        ) as ticket:
            if ticket is None:
                raise CtpNativeOrderAuthorityError("ctp_native_cancel_dispatch_ticket_missing")
            yield ticket

    def bind_cancel_target_issuer(self, issuer: Any, seal: Any) -> None:
        """Bind the one exact Store issuer/seal used for validated target handoff.

        This binds object identity only. The Store remains responsible for
        proving the durable cancel owner and current complete query before it
        registers a target; this CTP authority rechecks exact CTP identity,
        current session/gate, freshness, and one-shot use at the native edge.
        """

        if issuer is None or seal is None:
            raise CtpNativeOrderAuthorityError("ctp_native_cancel_issuer_invalid")
        with self._lock:
            if self._frozen:
                raise CtpNativeOrderAuthorityError("ctp_native_gate_frozen")
            if self._cancel_target_issuer is not None:
                if self._cancel_target_issuer is issuer and self._cancel_target_seal is seal:
                    return
                raise CtpNativeOrderAuthorityError("ctp_native_cancel_issuer_immutable")
            self._cancel_target_issuer = issuer
            self._cancel_target_seal = seal

    def register_cancel_target(self, target: Any) -> None:
        """Register one exact Store-issued CTP target for a single native Action."""

        target_type = _registered_sim_cancel_target_type()
        if target_type is None or type(target) is not target_type:
            raise CtpNativeOrderAuthorityError("ctp_native_cancel_target_untyped")
        with self._lock:
            if self._cancel_target_issuer is None or self._cancel_target_seal is None:
                raise CtpNativeOrderAuthorityError("ctp_native_cancel_issuer_unbound")
            if (
                target._issuer is not self._cancel_target_issuer
                or target._seal is not self._cancel_target_seal
            ):
                raise CtpNativeOrderAuthorityError("ctp_native_cancel_target_issuer_mismatch")
            if time.monotonic() >= target.source_expires_monotonic:
                raise CtpNativeOrderAuthorityError("ctp_native_cancel_target_expired")
            expected = self._expected_session
            current = self._current_session()
            self._require_session_match(current, expected, require_trading_ready=True)
            if expected is None or not self._cancel_target_session_matches(target, expected):
                raise CtpNativeOrderAuthorityError("ctp_native_cancel_target_session_mismatch")
            key = (target.instrument_id, target.exchange_id)
            if not self._current_grant_matches(key):
                raise CtpNativeOrderAuthorityError("ctp_native_cancel_target_route_unarmed")
            if id(target) in self._cancel_targets:
                raise CtpNativeOrderAuthorityError("ctp_native_cancel_target_reused")
            if len(self._cancel_targets) >= self._cancel_target_limit:
                raise CtpNativeOrderAuthorityError("ctp_native_cancel_target_capacity_exceeded")
            self._cancel_targets[id(target)] = target

    def discard_cancel_target(self, target: Any) -> None:
        """Discard an unused exact target after pre-dispatch cleanup."""

        with self._lock:
            target_id = id(target)
            if self._cancel_targets.get(target_id) is target:
                self._cancel_targets.pop(target_id, None)

    def dispatch_history(self) -> tuple[CtpNativeDispatchEvidence, ...]:
        """Return bounded immutable call-boundary evidence."""

        with self._lock:
            return tuple(self._dispatch_history)

    def unknown_dispatch(self) -> CtpNativeDispatchEvidence | None:
        """Return the sticky native UNKNOWN, if one has been observed."""

        with self._lock:
            return self._unknown_dispatch

    def execution_facts(self) -> Mapping[str, Any]:
        """Return a detached local order/gate projection without credentials."""

        with self._lock:
            with self._observation_lock:
                dispatching = tuple(sorted(self._dispatching_order_refs))
                callback_unknown = self._callback_unknown_projection
            unknown_ids = tuple(
                key for key, state in self._order_state.items() if state == "unknown"
            )
            active_ids = tuple(
                key for key, state in self._order_state.items() if state == "active"
            )
            terminal_ids = tuple(
                key for key, state in self._order_state.items() if state == "terminal"
            )
            return MappingProxyType(
                {
                    "observed": True,
                    "unknown_order_ids": unknown_ids,
                    "active_order_ids": active_ids,
                    "terminal_order_ids": terminal_ids,
                    "dispatching_order_ids": dispatching,
                    "unknown_dispatch": self._unknown_dispatch,
                    "frozen": self._frozen,
                    "armed_route": self._armed_key,
                    "callback_unknown": callback_unknown,
                }
            )

    def order_capacity_facts(self) -> Mapping[str, Any]:
        """Return count-only order capacity facts without blocking callbacks."""

        schema = "bt_api_ctp.registered_sim_order_capacity.v1"
        if not self._lock.acquire(blocking=False):
            return MappingProxyType(
                {"schema": schema, "status": "UNAVAILABLE", "reason_code": "state_lock_busy"}
            )
        try:
            if not self._observation_lock.acquire(blocking=False):
                return MappingProxyType(
                    {
                        "schema": schema,
                        "status": "UNAVAILABLE",
                        "reason_code": "observation_lock_busy",
                    }
                )
            try:
                state = self._order_state
                dispatching = self._dispatching_order_refs
                terminal_limit = self._terminal_order_limit
                if not isinstance(state, OrderedDict):
                    return MappingProxyType(
                        {
                            "schema": schema,
                            "status": "UNAVAILABLE",
                            "reason_code": "order_state_unavailable",
                        }
                    )
                if type(dispatching) is not set:
                    return MappingProxyType(
                        {
                            "schema": schema,
                            "status": "UNAVAILABLE",
                            "reason_code": "dispatch_state_unavailable",
                        }
                    )
                if dispatching:
                    return MappingProxyType(
                        {
                            "schema": schema,
                            "status": "UNAVAILABLE",
                            "reason_code": "native_dispatch_in_progress",
                            "dispatching_order_count": len(dispatching),
                        }
                    )
                if type(terminal_limit) is not int or terminal_limit <= 0:
                    return MappingProxyType(
                        {
                            "schema": schema,
                            "status": "UNAVAILABLE",
                            "reason_code": "terminal_limit_unavailable",
                        }
                    )
                if len(state) > 100_000:
                    return MappingProxyType(
                        {
                            "schema": schema,
                            "status": "UNAVAILABLE",
                            "reason_code": "order_state_scan_bound_exceeded",
                        }
                    )
                counts = {"active": 0, "unknown": 0, "terminal": 0}
                for order_state in state.values():
                    if order_state not in counts:
                        return MappingProxyType(
                            {
                                "schema": schema,
                                "status": "UNAVAILABLE",
                                "reason_code": "unknown_order_state",
                            }
                        )
                    counts[order_state] += 1
                terminal_respected = counts["terminal"] <= terminal_limit
                return MappingProxyType(
                    {
                        "schema": schema,
                        "status": "OBSERVED" if terminal_respected else "UNSTABLE_OR_OVER_CAPACITY",
                        "active_order_count": counts["active"],
                        "unknown_order_count": counts["unknown"],
                        "terminal_order_count": counts["terminal"],
                        "dispatching_order_count": 0,
                        "active_unknown_limit": None,
                        "active_unknown_bounded": False,
                        "terminal_order_limit": terminal_limit,
                        "terminal_limit_respected": terminal_respected,
                    }
                )
            finally:
                self._observation_lock.release()
        finally:
            self._lock.release()

    def registered_sim_arm_history(self) -> tuple[Mapping[str, Any], ...]:
        """Compatibility-shaped bounded arm history for the CTP Store port."""

        with self._lock:
            return tuple(
                MappingProxyType(
                    {
                        "instrument": item["instrument"],
                        "exchange": item["exchange_id"],
                        "at_epoch": item["at_epoch"],
                    }
                )
                for item in self._arm_history
            )

    def registered_sim_dispatch_history(self) -> tuple[CtpNativeDispatchEvidence, ...]:
        """Compatibility name for the bounded CTP native-call history."""

        return self.dispatch_history()

    def registered_sim_execution_facts(self) -> Mapping[str, Any]:
        """Compatibility-shaped local projection used by the CTP Store port."""

        with self._lock:
            with self._observation_lock:
                dispatching = tuple(sorted(self._dispatching_order_refs))
                templates_configured = self._arm_templates_configured
                template_keys = tuple(self._arm_templates)
                callback_facts = self._callback_unknown_projection
            sdk_state: Mapping[str, Any] | None
            try:
                state = self._read_gate_state()
                sdk_state = _public_gate_projection(state) if isinstance(state, Mapping) else None
            except Exception:
                sdk_state = None
            grant_matches = (
                self._current_grant_matches(self._armed_key)
                if self._armed_key is not None
                else False
            )
            if dispatching:
                unknown_ids = active_ids = terminal_ids = None
            else:
                unknown_ids = [
                    key for key, value in self._order_state.items() if value == "unknown"
                ]
                active_ids = [
                    key for key, value in self._order_state.items() if value == "active"
                ]
                terminal_ids = [
                    key for key, value in self._order_state.items() if value == "terminal"
                ]
            return MappingProxyType(
                {
                    "observed": True,
                    "unknown_ids": unknown_ids,
                    "active_order_ids": active_ids,
                    "dispatching_order_ids": list(dispatching),
                    "terminal_order_ids": terminal_ids,
                    "frozen": None if dispatching else self._frozen,
                    "armed_key": None if dispatching else self._armed_key,
                    "current_grant_matches_sdk": None if dispatching else grant_matches,
                    "sdk_gate_projection": None if dispatching else sdk_state,
                    "arm_templates_configured": templates_configured,
                    "arm_template_keys": template_keys,
                    "callback_unknown_order_ids": (
                        list(callback_facts.order_refs)
                        if callback_facts.order_refs is not None
                        else None
                    ),
                    "callback_unknown_order_count": callback_facts.order_count,
                    "callback_unknown_unresolved": callback_facts.unresolved,
                    "callback_unknown_identity_incomplete": callback_facts.identity_incomplete,
                    "callback_unknown_overflow": callback_facts.overflow,
                }
            )

    def registered_sim_order_capacity_facts(self) -> Mapping[str, Any]:
        """Compatibility name for the count-only capacity projection."""

        return self.order_capacity_facts()

    def _finish_dispatch_ticket(
        self,
        ticket: _CtpNativeDispatchTicket,
        *,
        native_return_code: int | None = None,
        error_type: str | None = None,
    ) -> CtpNativeDispatchEvidence:
        if self._active_dispatch_tickets.get(id(ticket)) is not ticket:
            raise CtpNativeOrderAuthorityError("ctp_native_dispatch_ticket_not_active")
        identity = ticket._identity
        known = type(native_return_code) is int and native_return_code == 0 and error_type is None
        status = "KNOWN" if known else "UNKNOWN"
        evidence = CtpNativeDispatchEvidence(
            sequence=ticket._sequence,
            operation=identity["operation"],
            status=status,
            request_id=identity["request_id"],
            instrument_id=identity["instrument_id"],
            exchange_id=identity["exchange_id"],
            order_ref=identity["order_ref"],
            order_sys_id=identity["order_sys_id"],
            front_id=identity["front_id"],
            session_id=identity["session_id"],
            broker_id=identity["broker_id"],
            investor_id=identity["investor_id"],
            user_id=identity["user_id"],
            native_return_code=native_return_code,
            error_type=_bounded_reason(error_type) if error_type is not None else None,
            observed_at_utc=datetime.now(timezone.utc).isoformat(),
        )
        self._dispatch_history.append(evidence)
        del self._dispatch_history[:-self._history_limit]
        if known:
            if identity["operation"] == "insert" and identity["order_ref"]:
                self._remember_order_state(identity["order_ref"], "active")
                self.track_order_ref(identity["order_ref"])
            return evidence

        self._unknown_dispatch = evidence
        self._remember_order_state(ticket._key, "unknown")
        self._freeze("native_{}_outcome_unknown".format(identity["operation"]))
        if self._capability is not None:
            try:
                self._disarm_locked(self._freeze_reason)
            except BaseException:
                # The local token is already discarded; uncertain native
                # revocation remains reflected by the frozen gate state.
                pass
        return evidence

    def _finish_pre_dispatch_ticket(self, ticket: _CtpNativeDispatchTicket) -> None:
        if self._active_dispatch_tickets.get(id(ticket)) is not ticket:
            raise CtpNativeOrderAuthorityError("ctp_native_dispatch_ticket_not_active")

    def _is_verified_pre_dispatch_rejection(
        self, ticket: _CtpNativeDispatchTicket, error: BaseException
    ) -> bool:
        identity = ticket._identity
        if identity["operation"] != "cancel":
            return False
        module_name = type(error).__module__
        if module_name != "bt_api_ctp.ctp.client":
            return False
        import sys

        sdk_module = sys.modules.get(module_name)
        gate_error = getattr(sdk_module, "CtpExecutionGateError", None) if sdk_module else None
        if (
            not isinstance(gate_error, type)
            or gate_error.__module__ != module_name
            or gate_error not in type(error).__mro__
            or not self._cancel_deadline_contract()
        ):
            return False
        getter = _canonical_bound_method(self._client, "get_order_action_evidence")
        if getter is None:
            return False
        try:
            evidence = getter(
                identity["request_id"],
                order_action_ref=identity["order_action_ref"],
            )
        except BaseException:
            return False
        return evidence is None

    def _cancel_deadline_contract(self) -> bool:
        submit = _canonical_bound_method(self._client, "submit_order_action")
        invoke = _canonical_bound_method(self._client, "_invoke_session_native_request")
        if submit is None or invoke is None:
            return False
        return _has_explicit_keyword(submit, "deadline_monotonic") and all(
            _has_explicit_keyword(invoke, name)
            for name in ("deadline_monotonic", "dispatch_outcome")
        )

    @staticmethod
    def _cancel_target_session_matches(target: Any, expected: CtpNativeSessionIdentity) -> bool:
        return all(
            getattr(target, target_name, None) == getattr(expected, session_name)
            for target_name, session_name in (
                ("account_fingerprint", "account_fingerprint"),
                ("connection_generation", "connection_generation"),
                ("trading_day", "trading_day"),
                ("front_id", "front_id"),
                ("session_id", "session_id"),
            )
        )

    def _claim_cancel_target_locked(
        self,
        target: Any,
        identity: Mapping[str, Any],
        *,
        instrument_id: str,
        exchange_id: str,
        request_id: int | None,
    ) -> int:
        target_type = _registered_sim_cancel_target_type()
        target_id = id(target)
        if (
            target_type is None
            or type(target) is not target_type
            or self._cancel_targets.get(target_id) is not target
            or self._active_cancel_target_id is not None
            or target._issuer is not self._cancel_target_issuer
            or target._seal is not self._cancel_target_seal
            or time.monotonic() >= target.source_expires_monotonic
            or not self._cancel_deadline_contract()
            or type(request_id) is not int
            or request_id <= 0
            or self._expected_session is None
            or not self._cancel_target_session_matches(target, self._expected_session)
            or target.instrument_id != instrument_id
            or target.exchange_id != exchange_id
            or not self._current_grant_matches((instrument_id, exchange_id))
        ):
            raise CtpNativeOrderAuthorityError("ctp_native_cancel_target_stale_or_unissued")
        expected_fields = {
            "order_ref": target.order_ref,
            "order_sys_id": target.order_sys_id,
            "instrument_id": target.instrument_id,
            "exchange_id": target.exchange_id,
            "front_id": target.front_id,
            "session_id": target.session_id,
            "broker_id": target.broker_id,
            "investor_id": target.investor_id,
            "order_action_ref": request_id,
            "action_flag": "0",
        }
        if any(identity.get(name) != value for name, value in expected_fields.items()):
            raise CtpNativeOrderAuthorityError("ctp_native_cancel_action_identity_mismatch")
        self._active_cancel_target_id = target_id
        return target_id

    def _discard_cancel_target_locked(self, target_id: int, target: Any) -> None:
        if self._cancel_targets.get(target_id) is target:
            self._cancel_targets.pop(target_id, None)
        if self._active_cancel_target_id == target_id:
            self._active_cancel_target_id = None

    def _remember_order_state(self, order_ref: str, state: str) -> None:
        if not order_ref:
            return
        current = self._order_state.get(order_ref)
        if current == "unknown" and state != "unknown":
            return
        if current == "terminal" and state == "active":
            return
        self._order_state[order_ref] = state
        self._order_state.move_to_end(order_ref)
        terminal = [key for key, value in self._order_state.items() if value == "terminal"]
        while len(terminal) > self._terminal_order_limit:
            self._order_state.pop(terminal.pop(0), None)

    def freeze_unknown_dispatch(self, reason: str = "native_dispatch_outcome_unknown") -> None:
        """Keep the native gate frozen after an ambiguous dispatch outcome."""

        self.freeze_registered_sim_execution(reason)

    def track_order_ref(self, order_ref: str) -> None:
        """Record a known native identity for callback session filtering only."""

        order_ref = _exact_text(order_ref, "OrderRef")
        with self._observation_lock:
            self._known_order_refs[order_ref] = None
            self._known_order_refs.move_to_end(order_ref)
            while len(self._known_order_refs) > self._known_order_ref_limit:
                self._known_order_refs.popitem(last=False)

    def remember_order_terminal(self, order_ref: str) -> None:
        """Retain an SDK-verified terminal callback status in the local projection."""

        order_ref = _exact_text(order_ref, "OrderRef")
        with self._lock:
            self._remember_order_state(order_ref, "terminal")

    def observe_order_callback(self, payload: Any) -> None:
        """Latch an in-scope UNKNOWN/invalid CTP ``OnRtnOrder`` status.

        The original payload is never retained. A positively identified
        callback from another front/session is ignored; missing or unreadable
        identity is retained as incomplete UNKNOWN evidence.
        """

        try:
            order_ref = _callback_text(payload, "OrderRef")
            status = _callback_text(payload, "OrderStatus").lower()
            front_id = _callback_positive_int(payload, "FrontID")
            session_id = _callback_positive_int(payload, "SessionID")
            status_map = _order_status_map()
        except Exception:
            self._latch_callback_unknown(None, identity_incomplete=True)
            return
        if status != "a" and status in status_map:
            if not order_ref:
                return
            with self._observation_lock:
                order_is_known = order_ref in self._known_order_refs
                if order_is_known:
                    self._known_order_refs.move_to_end(order_ref)
            if not order_is_known:
                try:
                    current = self._current_session()
                except Exception:
                    return
                if (
                    front_id is not None
                    and session_id is not None
                    and current.front_id is not None
                    and current.session_id is not None
                    and (front_id != current.front_id or session_id != current.session_id)
                ):
                    return
            try:
                next_state = (
                    "terminal" if status in _terminal_order_statuses() else "active"
                )
            except Exception:
                return
            with self._lock:
                self._remember_order_state(order_ref, next_state)
            return
        if (front_id is None) != (session_id is None):
            self._latch_callback_unknown(order_ref or None, identity_incomplete=True)
            return
        with self._observation_lock:
            order_is_known = bool(order_ref and order_ref in self._known_order_refs)
            if order_is_known:
                self._known_order_refs.move_to_end(order_ref)
        identity_incomplete = not bool(order_ref)
        if not order_is_known:
            try:
                current = self._current_session()
            except Exception:
                self._latch_callback_unknown(order_ref or None, identity_incomplete=True)
                return
            if front_id is not None and session_id is not None:
                if current.front_id is not None and current.session_id is not None:
                    if front_id != current.front_id or session_id != current.session_id:
                        return
                else:
                    identity_incomplete = True
            else:
                identity_incomplete = True
        self._latch_callback_unknown(order_ref or None, identity_incomplete=identity_incomplete)

    def callback_unknown_facts(self) -> CtpCallbackUnknownFacts:
        """Return the separate sticky callback-UNKNOWN ledger projection."""

        with self._observation_lock:
            return self._callback_unknown_projection

    def order_insert_evidence(
        self,
        request_id: int,
        order_ref: str,
        *,
        instrument_id: str,
        exchange_id: str,
    ) -> Any | None:
        """Read the SDK's exact insert callback evidence for this session.

        ``None`` means no matching evidence object was observed. It does not
        prove that no native call occurred. An unknown result after an
        ambiguous/failed native submission freezes this authority; a local
        return code of zero with the SDK's explicit ``awaiting_native_callback``
        reason remains dispatch evidence, not an order acknowledgement.
        """

        if type(request_id) is not int or request_id <= 0:
            raise CtpNativeOrderAuthorityError("ctp_native_insert_request_id_invalid")
        order_ref = _exact_text(order_ref, "OrderRef")
        instrument_id = _exact_text(instrument_id, "InstrumentID")
        exchange_id = _exact_text(exchange_id, "ExchangeID")
        getter = _canonical_bound_method(self._client, "get_order_insert_evidence")
        if getter is None:
            raise CtpNativeOrderAuthorityError("ctp_native_insert_evidence_unavailable")
        evidence = getter(request_id, order_ref=order_ref)
        if evidence is None:
            return None
        try:
            from bt_api_ctp.ctp.client import CtpOrderInsertEvidence
        except ImportError:
            raise CtpNativeOrderAuthorityError("ctp_native_insert_evidence_unavailable") from None
        if type(evidence) is not CtpOrderInsertEvidence:
            self._freeze_after_interruption("native_insert_evidence_untyped")
            raise CtpNativeOrderAuthorityError("ctp_native_insert_evidence_untyped")
        current = self._current_session()
        account_fingerprint = _native_evidence_account(current.account_fingerprint)
        if (
            evidence.request_id != request_id
            or evidence.order_ref != order_ref
            or evidence.instrument_id != instrument_id
            or evidence.exchange_id != exchange_id
            or evidence.account_fingerprint != account_fingerprint
            or evidence.connection_generation != current.connection_generation
            or evidence.trading_day != current.trading_day
        ):
            self._freeze_after_interruption("native_insert_evidence_session_mismatch")
            raise CtpNativeOrderAuthorityError("ctp_native_insert_evidence_session_mismatch")
        if evidence.status == "unknown" and (
            evidence.submit_code != 0
            or evidence.reason != "awaiting_native_callback"
            or evidence.callback_received is True
            or evidence.evidence_received is True
        ):
            self._freeze_after_interruption("native_insert_outcome_unknown")
        return evidence

    def order_action_evidence(
        self,
        request_id: int,
        order_action_ref: str,
        *,
        order_ref: str,
        instrument_id: str,
        exchange_id: str,
    ) -> Any | None:
        """Read exact cancellation callback evidence without claiming cancel."""

        if type(request_id) is not int or request_id <= 0:
            raise CtpNativeOrderAuthorityError("ctp_native_action_request_id_invalid")
        order_action_ref = _exact_text(order_action_ref, "OrderActionRef")
        order_ref = _exact_text(order_ref, "OrderRef")
        instrument_id = _exact_text(instrument_id, "InstrumentID")
        exchange_id = _exact_text(exchange_id, "ExchangeID")
        getter = _canonical_bound_method(self._client, "get_order_action_evidence")
        if getter is None:
            raise CtpNativeOrderAuthorityError("ctp_native_action_evidence_unavailable")
        evidence = getter(request_id, order_action_ref=order_action_ref)
        if evidence is None:
            return None
        try:
            from bt_api_ctp.order_action import CtpOrderActionEvidence
        except ImportError:
            raise CtpNativeOrderAuthorityError("ctp_native_action_evidence_unavailable") from None
        if type(evidence) is not CtpOrderActionEvidence:
            self._freeze_after_interruption("native_action_evidence_untyped")
            raise CtpNativeOrderAuthorityError("ctp_native_action_evidence_untyped")
        current = self._current_session()
        account_fingerprint = _native_evidence_account(current.account_fingerprint)
        if (
            evidence.request_id != request_id
            or evidence.order_action_ref != order_action_ref
            or evidence.order_ref != order_ref
            or evidence.instrument_id != instrument_id
            or evidence.exchange_id != exchange_id
            or evidence.account_fingerprint != account_fingerprint
            or evidence.connection_generation != current.connection_generation
            or evidence.trading_day != current.trading_day
        ):
            self._freeze_after_interruption("native_action_evidence_session_mismatch")
            raise CtpNativeOrderAuthorityError("ctp_native_action_evidence_session_mismatch")
        if evidence.status == "unknown" and (
            evidence.submit_code != 0
            or evidence.reason != "awaiting_native_callback"
            or evidence.callback_received is True
            or evidence.evidence_received is True
        ):
            self._freeze_after_interruption("native_action_outcome_unknown")
        return evidence

    def registered_sim_callback_unknown_facts(self) -> Mapping[str, Any]:
        """Return detached compatibility-shaped callback UNKNOWN facts."""

        facts = self.callback_unknown_facts()
        return MappingProxyType(
            {
                "observed": True,
                "callback_unknown_order_ids": (
                    list(facts.order_refs) if facts.order_refs is not None else None
                ),
                "callback_unknown_order_count": facts.order_count,
                "callback_unknown_unresolved": facts.unresolved,
                "callback_unknown_identity_incomplete": facts.identity_incomplete,
                "callback_unknown_overflow": facts.overflow,
            }
        )

    def arm_history(self) -> tuple[Mapping[str, Any], ...]:
        """Return bounded detached history without capability material."""

        with self._lock:
            return tuple(MappingProxyType(dict(item)) for item in self._arm_history)

    def status(self) -> Mapping[str, Any]:
        """Return a finite gate observation without exposing opaque authority."""

        with self._lock:
            try:
                state = self._read_gate_state()
                observed = isinstance(state, Mapping)
                armed = state.get("armed") if observed else None
                reason = state.get("revocation_reason") if observed else None
            except Exception:
                observed, armed, reason = False, None, None
            facts = self.callback_unknown_facts()
            return MappingProxyType(
                {
                    "observed": observed,
                    "allow_writes": self._allow_writes,
                    "armed": armed if type(armed) is bool else None,
                    "instrument": self._armed_key[0] if self._armed_key else None,
                    "exchange_id": self._armed_key[1] if self._armed_key else None,
                    "frozen": self._frozen,
                    "freeze_reason": self._freeze_reason,
                    "revocation_reason": reason if type(reason) is str else None,
                    "callback_unknown_unresolved": facts.unresolved,
                }
            )

    def _current_session(self) -> CtpNativeSessionIdentity:
        return self.capture_session(self._client)

    @staticmethod
    def _require_session_match(
        current: CtpNativeSessionIdentity,
        expected: CtpNativeSessionIdentity | None,
        *,
        require_trading_ready: bool,
    ) -> None:
        if (
            current.read_only_ready is not True
            or (require_trading_ready and current.trading_ready is not True)
        ):
            raise CtpNativeOrderAuthorityError("ctp_native_session_not_ready")
        if expected is None:
            return
        fields = (
            "account_fingerprint",
            "connection_generation",
            "trading_day",
        )
        if any(getattr(current, name) != getattr(expected, name) for name in fields):
            raise CtpNativeOrderAuthorityError("ctp_native_session_changed")
        if expected.front_id is not None and (
            current.front_id != expected.front_id or current.session_id != expected.session_id
        ):
            raise CtpNativeOrderAuthorityError("ctp_native_session_ids_changed")

    def _read_gate_state(self) -> Any:
        getter = _canonical_bound_method(self._client, "get_execution_gate_state")
        if getter is None:
            return None
        return getter()

    def _gate_snapshot(
        self, state: Any, key: tuple[str, str]
    ) -> Mapping[str, Any] | None:
        if not isinstance(state, Mapping):
            return None
        trader_type = _canonical_trader_client_type()
        if trader_type is None or type(self._client) is not trader_type:
            return None
        try:
            from bt_api_ctp.ctp.client import canonical_ctp_instrument

            sdk_instrument = canonical_ctp_instrument(key[0], key[1])
        except Exception:
            return None
        proof = state.get("proof_sha256")
        profile = state.get("environment_profile")
        if (
            not sdk_instrument
            or state.get("managed") is not True
            or state.get("armed") is not True
            or state.get("instrument") != sdk_instrument
            or type(state.get("connection_generation")) is not int
            or state.get("connection_generation") <= 0
            or type(state.get("trading_day")) is not str
            or _TRADING_DAY_RE.fullmatch(state.get("trading_day", "")) is None
            or type(proof) is not str
            or _HASH_RE.fullmatch(proof) is None
            or type(profile) is not str
            or not profile
            or "revocation_reason" not in state
            or state.get("revocation_reason") is not None
        ):
            return None
        return MappingProxyType(
            {
                "instrument": key[0],
                "exchange_id": key[1],
                "sdk_instrument": sdk_instrument,
                "connection_generation": state["connection_generation"],
                "trading_day": state["trading_day"],
                "environment_profile": profile,
                "proof_sha256": proof,
            }
        )

    @staticmethod
    def _gate_matches_session(
        snapshot: Mapping[str, Any], current: CtpNativeSessionIdentity
    ) -> bool:
        return bool(
            snapshot.get("connection_generation") == current.connection_generation
            and snapshot.get("trading_day") == current.trading_day
        )

    def _current_grant_matches(self, key: tuple[str, str] | None) -> bool:
        if (
            key is None
            or self._capability is None
            or self._armed_key != key
            or self._last_arm_snapshot is None
        ):
            return False
        try:
            state = self._read_gate_state()
            snapshot = self._gate_snapshot(state, key)
            current = self._current_session()
        except Exception:
            return False
        expected = self._expected_session
        try:
            self._require_session_match(current, expected, require_trading_ready=True)
        except CtpNativeOrderAuthorityError:
            return False
        return bool(
            snapshot == self._last_arm_snapshot
            and self._gate_matches_session(snapshot or {}, current)
        )

    def _require_armable(self) -> None:
        if not self._allow_writes:
            raise CtpNativeOrderAuthorityError("ctp_native_writes_disabled")
        if self._unknown_dispatch is not None:
            raise CtpNativeOrderAuthorityError("ctp_native_dispatch_unknown_unresolved")
        if self._frozen:
            raise CtpNativeOrderAuthorityError("ctp_native_gate_frozen")
        if self.callback_unknown_facts().unresolved:
            raise CtpNativeOrderAuthorityError("ctp_native_callback_unknown_unresolved")

    def _append_arm_history(self, instrument: str, exchange: str) -> None:
        self._arm_history.append(
            {"instrument": instrument, "exchange_id": exchange, "at_epoch": time.time()}
        )
        del self._arm_history[:-self._history_limit]

    def _disarm_locked(self, reason: str) -> dict[str, Any]:
        client = self._client
        capability = self._capability
        self._capability = None
        self._armed_key = None
        self._gate_state = None
        self._last_arm_snapshot = None
        if capability is None:
            return {"disarmed": True, "reason": reason, "native": False}
        disarm = _canonical_bound_method(client, "disarm_execution_gate")
        if disarm is None:
            self._freeze("native_disarm_unavailable")
            raise CtpNativeOrderAuthorityError("ctp_native_disarm_unavailable")
        try:
            state = disarm(capability, reason)
        except BaseException as exc:
            self._freeze("native_disarm_failed")
            if not isinstance(exc, Exception):
                raise
            raise CtpNativeOrderAuthorityError("ctp_native_disarm_failed") from None
        if not isinstance(state, Mapping) or state.get("armed") is not False:
            self._freeze("native_disarm_unobserved")
            raise CtpNativeOrderAuthorityError("ctp_native_disarm_unobserved")
        return {"disarmed": True, "reason": reason, "native": True, "state": dict(state)}

    def _freeze(self, reason: str) -> None:
        if not self._frozen:
            self._frozen = True
            self._freeze_reason = _bounded_reason(reason)

    def _freeze_after_interruption(self, reason: str) -> None:
        with self._lock:
            self._freeze(reason)
            if self._capability is not None:
                try:
                    self._disarm_locked(self._freeze_reason)
                except Exception:
                    pass

    def _latch_callback_unknown(
        self, order_ref: str | None, *, identity_incomplete: bool
    ) -> None:
        with self._observation_lock:
            changed = False
            if (
                (order_ref is None or identity_incomplete)
                and not self._callback_unknown_identity_incomplete
            ):
                self._callback_unknown_identity_incomplete = True
                changed = True
            if order_ref is None and not self._callback_unknown_missing_order_ref:
                self._callback_unknown_missing_order_ref = True
                changed = True
            if (
                order_ref not in self._callback_unknown_orders
                and not self._callback_unknown_overflow
            ):
                if len(self._callback_unknown_orders) >= self._callback_unknown_limit:
                    self._callback_unknown_overflow = True
                    changed = True
                else:
                    self._callback_unknown_orders[order_ref] = None
                    changed = True
            if not changed:
                return
            complete = (
                not self._callback_unknown_overflow
                and not self._callback_unknown_missing_order_ref
            )
            entries = tuple(
                value for value in self._callback_unknown_orders if type(value) is str
            )
            self._callback_unknown_projection = CtpCallbackUnknownFacts(
                entries if complete else None,
                len(self._callback_unknown_orders) if complete else None,
                bool(self._callback_unknown_orders) or self._callback_unknown_overflow,
                self._callback_unknown_identity_incomplete,
                self._callback_unknown_overflow,
                self._callback_unknown_missing_order_ref,
            )


def _canonical_trader_client_type() -> type | None:
    try:
        from bt_api_ctp.ctp.client import TraderClient
    except ImportError:
        return None
    return TraderClient if type(TraderClient) is type else None


def _canonical_bound_method(client: Any, name: str) -> Any | None:
    """Return a method only when it is the unshadowed SDK implementation."""

    trader_type = _canonical_trader_client_type()
    if trader_type is None or type(client) is not trader_type:
        return None
    expected = getattr(trader_type, name, None)
    method = getattr(client, name, None)
    if not callable(expected) or getattr(method, "__func__", None) is not expected:
        return None
    return method


def _exact_text(value: Any, name: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise ValueError(f"{name} must be non-empty trimmed text")
    return value


def _exact_hash(value: Any, name: str) -> str:
    text = _exact_text(value, name)
    if _HASH_RE.fullmatch(text) is None:
        raise ValueError(f"{name} must be lowercase SHA-256")
    return text


def _bounded_reason(value: Any) -> str:
    if type(value) is not str or not value or value != value.strip():
        return "ctp_native_gate_revoked"
    safe = "".join(
        char if char.isascii() and (char.isalnum() or char in "._:-") else "_"
        for char in value
    )
    return safe[:128] or "ctp_native_gate_revoked"


def _native_evidence_account(account_fingerprint: str) -> str:
    return (
        account_fingerprint
        if account_fingerprint.startswith("acct_")
        else "acct_" + account_fingerprint
    )


def _callback_text(payload: Any, name: str) -> str:
    try:
        value = payload.get(name) if isinstance(payload, Mapping) else getattr(payload, name)
    except Exception:
        return ""
    return value.strip() if type(value) is str else ""


def _callback_positive_int(payload: Any, name: str) -> int | None:
    try:
        value = payload.get(name) if isinstance(payload, Mapping) else getattr(payload, name)
    except Exception:
        return None
    if type(value) is int and value > 0:
        return value
    if type(value) is str and value.isascii() and value.isdigit():
        parsed = int(value)
        return parsed if parsed > 0 else None
    return None


def _order_status_map() -> Mapping[str, str]:
    from bt_api_ctp.fields import ORDER_STATUS_MAP

    return ORDER_STATUS_MAP


def _terminal_order_statuses() -> frozenset[str]:
    from bt_api_ctp.status import TERMINAL_ORDER_STATUSES

    return TERMINAL_ORDER_STATUSES


def _public_gate_projection(state: Any) -> Mapping[str, Any] | None:
    """Copy only non-secret SDK gate observations for compatibility facts."""

    if not isinstance(state, Mapping):
        return None
    authorized = state.get("authorized_instruments")
    if isinstance(authorized, (tuple, list)) and all(type(item) is str for item in authorized):
        authorized = tuple(authorized)
    else:
        authorized = None
    return MappingProxyType(
        {
            "managed": state.get("managed") if type(state.get("managed")) is bool else None,
            "armed": state.get("armed") if type(state.get("armed")) is bool else None,
            "instrument": state.get("instrument") if type(state.get("instrument")) is str else None,
            "connection_generation": (
                state.get("connection_generation")
                if type(state.get("connection_generation")) is int
                else None
            ),
            "trading_day": (
                state.get("trading_day") if type(state.get("trading_day")) is str else None
            ),
            "authorized_instruments": authorized,
        }
    )


def _registered_sim_cancel_target_type() -> type | None:
    try:
        from bt_api_ctp.receipts import RegisteredSimCancelTarget
    except ImportError:
        return None
    return RegisteredSimCancelTarget if type(RegisteredSimCancelTarget) is type else None


def _has_explicit_keyword(method: Any, keyword: str) -> bool:
    try:
        parameters = inspect.signature(method).parameters
    except (TypeError, ValueError):
        return False
    parameter = parameters.get(keyword)
    return parameter is not None and parameter.kind in (
        inspect.Parameter.POSITIONAL_OR_KEYWORD,
        inspect.Parameter.KEYWORD_ONLY,
    )


def _normalize_dispatch_identity(
    value: Mapping[str, Any] | None,
    *,
    operation: str,
    request_id: int | None,
    instrument_id: str,
    exchange_id: str,
    order_ref: str | None,
) -> Mapping[str, Any]:
    if operation not in {"insert", "cancel"}:
        raise CtpNativeOrderAuthorityError("ctp_native_dispatch_operation_invalid")
    if type(request_id) is not int or request_id <= 0:
        raise CtpNativeOrderAuthorityError("ctp_native_dispatch_request_id_invalid")
    if not isinstance(value, Mapping):
        raise CtpNativeOrderAuthorityError("ctp_native_dispatch_identity_untyped")
    allowed = {
        "order_ref",
        "order_sys_id",
        "instrument_id",
        "exchange_id",
        "front_id",
        "session_id",
        "broker_id",
        "investor_id",
        "user_id",
        "order_action_ref",
        "action_flag",
    }
    if any(type(name) is not str or name not in allowed for name in value):
        raise CtpNativeOrderAuthorityError("ctp_native_dispatch_identity_fields_invalid")
    try:
        observed_instrument = _exact_text(value.get("instrument_id"), "InstrumentID")
        observed_exchange = _exact_text(value.get("exchange_id"), "ExchangeID")
    except ValueError:
        raise CtpNativeOrderAuthorityError("ctp_native_dispatch_route_invalid") from None
    if observed_instrument != instrument_id or observed_exchange != exchange_id:
        raise CtpNativeOrderAuthorityError("ctp_native_dispatch_route_mismatch")
    observed_order_ref = value.get("order_ref")
    if observed_order_ref is not None:
        try:
            observed_order_ref = _exact_text(observed_order_ref, "OrderRef")
        except ValueError:
            raise CtpNativeOrderAuthorityError("ctp_native_dispatch_order_ref_invalid") from None
    if order_ref is not None and observed_order_ref != order_ref:
        raise CtpNativeOrderAuthorityError("ctp_native_dispatch_order_ref_mismatch")
    if operation == "insert" and observed_order_ref is None:
        raise CtpNativeOrderAuthorityError("ctp_native_insert_order_ref_missing")

    normalized: dict[str, Any] = {
        "operation": operation,
        "request_id": request_id,
        "instrument_id": instrument_id,
        "exchange_id": exchange_id,
        "order_ref": observed_order_ref,
        "order_sys_id": None,
        "front_id": None,
        "session_id": None,
        "broker_id": None,
        "investor_id": None,
        "user_id": None,
        "order_action_ref": None,
        "action_flag": None,
    }
    for name in ("order_sys_id", "broker_id", "investor_id", "user_id"):
        raw = value.get(name)
        if raw is not None:
            try:
                normalized[name] = _exact_text(raw, name)
            except ValueError:
                raise CtpNativeOrderAuthorityError(
                    "ctp_native_dispatch_identity_field_invalid"
                ) from None
    for name in ("front_id", "session_id"):
        raw = value.get(name)
        if raw is not None:
            if type(raw) is not int or raw <= 0:
                raise CtpNativeOrderAuthorityError(
                    "ctp_native_dispatch_identity_field_invalid"
                )
            normalized[name] = raw
    if operation == "cancel":
        action_ref = value.get("order_action_ref")
        if type(action_ref) is not int or action_ref != request_id:
            raise CtpNativeOrderAuthorityError("ctp_native_cancel_action_ref_mismatch")
        action_flag = value.get("action_flag")
        if type(action_flag) is not str or action_flag != "0":
            raise CtpNativeOrderAuthorityError("ctp_native_cancel_action_flag_invalid")
        normalized["order_action_ref"] = action_ref
        normalized["action_flag"] = action_flag
    return MappingProxyType(normalized)


__all__ = [
    "CtpCallbackUnknownFacts",
    "CtpNativeDispatchEvidence",
    "CtpNativeOrderAuthorityError",
    "CtpNativeSessionIdentity",
    "CtpRegisteredSimNativeAuthority",
]
