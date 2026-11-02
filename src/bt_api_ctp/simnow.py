"""Single owner for the CTP registered-sim session and admission truth.

This module owns the facts a registered-simulation dispatch window depends on:
the expected-session identity shape, the canonical ``TraderClient`` getters that
must be used to read it, the SDK execution-gate grant projection, the fixed
``InvestUnitID`` this route writes, and the native request-field assertions that
bind an outbound CTP struct to its verified cancel target.

It was previously duplicated inside the Backtrader ``registered_sim`` Store,
which made two repositories interpret the same provider contract. Backtrader now
keeps only the thin Store/Wrapper shell: every function here takes plain values
or a provider object and returns a plain value, so no framework type,
notification queue or ``Order`` is reachable from this package.

Nothing here grants admission or write permission. A returned ``None`` or
``False`` always means "not proven", never "allowed"; identity checks compare
against the class object actually stored in ``sys.modules`` so a duck-typed or
subclassed client fails closed.
"""

from __future__ import annotations

import re
import sys
import threading
from collections.abc import Mapping
from typing import Any, Dict, Optional, Tuple

from bt_api_ctp.query_facts import (
    SDK_CLIENT_MODULE_NAMES,
    SHA256_PATTERN,
    is_positive_int,
    read_fact,
)

__all__ = [
    "NATIVE_ACTION_FLAG_CANCEL",
    "NATIVE_FIELD_IDENTITY_INSERT_ORDER_REF_MISSING",
    "NATIVE_FIELD_IDENTITY_MISMATCH",
    "NATIVE_FIELD_IDENTITY_ORDER_REF_NOT_EXACT_TEXT",
    "RegisteredSimSessionIdentityError",
    "canonical_sdk_direct_grant_client",
    "canonical_sdk_route_instrument",
    "canonical_sdk_session_getters",
    "detached_gate_state",
    "empty_session_identity",
    "expected_session",
    "gate_state_snapshot",
    "native_action_matches_cancel_target",
    "native_field_identity_rejection",
    "native_order_ref",
    "public_gate_projection",
    "session_identity",
    "session_matches",
    "session_scope_state_snapshot",
    "write_route_invest_unit_id",
]

#: ``ActionFlag`` meaning "cancel" in the native ``InputOrderActionField``.
NATIVE_ACTION_FLAG_CANCEL = "0"

#: Finite codes for the native request-field assertions below. The framework
#: maps each one onto its own public error text; the code is the provider fact.
NATIVE_FIELD_IDENTITY_ORDER_REF_NOT_EXACT_TEXT = "order_ref_not_exact_text"
NATIVE_FIELD_IDENTITY_INSERT_ORDER_REF_MISSING = "insert_order_ref_missing"
NATIVE_FIELD_IDENTITY_MISMATCH = "request_identity_mismatch"


class RegisteredSimSessionIdentityError(ValueError):
    """An expected-session binding is not the exact account/generation/day."""


def expected_session(value: Any) -> Dict[str, Any]:
    """Validate a decision's exact account/generation/day dispatch binding."""

    if not isinstance(value, Mapping):
        raise RegisteredSimSessionIdentityError(
            "registered-sim expected session identity is invalid"
        )
    account = value.get("account_fingerprint")
    generation = value.get("connection_generation")
    trading_day = value.get("trading_day")
    if (
        not isinstance(account, str)
        or not account
        or account != account.strip()
        or type(generation) is not int
        or generation <= 0
        or not isinstance(trading_day, str)
        or re.fullmatch(r"[0-9]{8}", trading_day) is None
    ):
        raise RegisteredSimSessionIdentityError(
            "registered-sim expected session identity is invalid"
        )
    front_id = value.get("front_id")
    session_id = value.get("session_id")
    if (front_id is None) != (session_id is None):
        raise RegisteredSimSessionIdentityError("registered-sim expected session identity is invalid")
    if front_id is not None and (
        not is_positive_int(front_id) or not is_positive_int(session_id)
    ):
        raise RegisteredSimSessionIdentityError("registered-sim expected session identity is invalid")
    result = {
        "account_fingerprint": account,
        "connection_generation": generation,
        "trading_day": trading_day,
    }
    if front_id is not None:
        result.update({"front_id": front_id, "session_id": session_id})
    return result


def canonical_sdk_session_getters(client: Any) -> Optional[Tuple[Any, Any, Any]]:
    """Return canonical session getters and their shared SDK lock, if exact."""

    module_name = type(client).__module__
    if module_name not in SDK_CLIENT_MODULE_NAMES:
        return None
    sdk_module = sys.modules.get(module_name)
    client_type = getattr(sdk_module, "TraderClient", None) if sdk_module else None
    if not isinstance(client_type, type) or type(client) is not client_type:
        return None
    scope_getter = getattr(client, "get_query_session_scope", None)
    state_getter = getattr(client, "get_session_state", None)
    if (
        not callable(scope_getter)
        or getattr(scope_getter, "__self__", None) is not client
        or getattr(scope_getter, "__func__", None)
        is not getattr(client_type, "get_query_session_scope", None)
        or not callable(state_getter)
        or getattr(state_getter, "__self__", None) is not client
        or getattr(state_getter, "__func__", None)
        is not getattr(client_type, "get_session_state", None)
    ):
        return None
    session_lock = getattr(client, "_query_state_lock", None)
    if (
        session_lock is None
        or type(session_lock) is not type(threading.RLock())
        or not callable(getattr(session_lock, "__enter__", None))
        or not callable(getattr(session_lock, "__exit__", None))
    ):
        return None
    return scope_getter, state_getter, session_lock


def session_scope_state_snapshot(
    client: Any, *, require_canonical: bool = False
) -> Optional[Tuple[Dict[str, Any], Dict[str, Any]]]:
    """Read detached scope/state projections as one session observation.

    The canonical SDK scope omits CTP FrontID/SessionID, so these must be
    paired with its session state under the same RLock used by the SDK login
    callback.  Non-SDK fake clients remain useful for ordinary, non-authority
    execution facts, but never satisfy ``require_canonical``.
    """

    canonical_module = type(client).__module__ in SDK_CLIENT_MODULE_NAMES
    exact = canonical_sdk_session_getters(client)
    if require_canonical and exact is None:
        return None
    if canonical_module and exact is None:
        return None
    if exact is not None:
        scope_method, state_method, session_lock = exact
        try:
            with session_lock:
                raw_scope = scope_method()
                raw_state = state_method()
                client_ready = getattr(client, "is_ready", None)
                scope = {
                    name: read_fact(raw_scope, name)
                    for name in (
                        "account_fingerprint",
                        "connection_generation",
                        "trading_day",
                        "read_only_ready",
                        "issuer",
                        "broker_id",
                        "investor_id",
                        "user_id",
                    )
                }
                state = {
                    name: read_fact(raw_state, name)
                    for name in (
                        "account_fingerprint",
                        "connection_generation",
                        "trading_day",
                        "trading_ready",
                        "read_only_ready",
                        "front_id",
                        "session_id",
                        "broker_id",
                        "investor_id",
                        "user_id",
                    )
                }
                state["client_is_ready"] = client_ready
        except Exception:
            return None
        return scope, state

    scope_method = getattr(client, "get_query_session_scope", None)
    state_method = getattr(client, "get_session_state", None)
    if not callable(scope_method) or not callable(state_method):
        return None
    try:
        raw_scope = scope_method()
        raw_state = state_method()
        scope = {
            name: read_fact(raw_scope, name)
            for name in (
                "account_fingerprint",
                "connection_generation",
                "trading_day",
                "read_only_ready",
                "issuer",
                "broker_id",
                "investor_id",
                "user_id",
            )
        }
        state = {
            name: read_fact(raw_state, name)
            for name in (
                "account_fingerprint",
                "connection_generation",
                "trading_day",
                "trading_ready",
                "read_only_ready",
                "front_id",
                "session_id",
                "broker_id",
                "investor_id",
                "user_id",
            )
        }
        state["client_is_ready"] = getattr(client, "is_ready", None)
    except Exception:
        return None
    return scope, state


def empty_session_identity() -> Dict[str, Any]:
    """Return the all-unobserved session identity a failed read must yield."""

    return {
        "account_fingerprint": None,
        "connection_generation": None,
        "trading_day": None,
        "trading_ready": None,
        "read_only_ready": None,
        "front_id": None,
        "session_id": None,
    }


def session_identity(client: Any) -> Dict[str, Any]:
    """Read the SDK's public live session identity without performing I/O."""

    snapshot = session_scope_state_snapshot(client)
    if snapshot is None:
        return empty_session_identity()
    scope, state = snapshot
    if not isinstance(scope, Mapping) or not isinstance(state, Mapping):
        return {
            **empty_session_identity(),
        }
    scope_values = (
        read_fact(scope, "account_fingerprint"),
        read_fact(scope, "connection_generation"),
        read_fact(scope, "trading_day"),
    )
    state_values = (
        read_fact(state, "account_fingerprint"),
        read_fact(state, "connection_generation"),
        read_fact(state, "trading_day"),
    )
    values = scope_values if scope_values == state_values else (None, None, None)
    account, generation, trading_day = values
    identity_valid = (
        isinstance(account, str)
        and bool(account)
        and account == account.strip()
        and type(generation) is int
        and generation > 0
        and isinstance(trading_day, str)
        and re.fullmatch(r"[0-9]{8}", trading_day) is not None
    )
    ready = read_fact(state, "trading_ready")
    front_id = read_fact(state, "front_id")
    session_id = read_fact(state, "session_id")
    return {
        "account_fingerprint": account if identity_valid else None,
        "connection_generation": generation if identity_valid else None,
        "trading_day": trading_day if identity_valid else None,
        "trading_ready": (
            ready
            if type(ready) is bool and read_fact(state, "client_is_ready") is ready
            else None
        ),
        "read_only_ready": (
            read_fact(scope, "read_only_ready")
            if type(read_fact(scope, "read_only_ready")) is bool
            else None
        ),
        "front_id": front_id if is_positive_int(front_id) else None,
        "session_id": session_id if is_positive_int(session_id) else None,
    }


def session_matches(
    current: Mapping, expected: Optional[Mapping], *, require_trading_ready: bool
) -> bool:
    """Whether one observed identity is a usable session for a binding."""

    if not isinstance(current, Mapping):
        return False
    if (
        not isinstance(current.get("account_fingerprint"), str)
        or not current.get("account_fingerprint")
        or type(current.get("connection_generation")) is not int
        or current.get("connection_generation") <= 0
        or not isinstance(current.get("trading_day"), str)
        or re.fullmatch(r"[0-9]{8}", current.get("trading_day", "")) is None
    ):
        return False
    if require_trading_ready and current.get("trading_ready") is not True:
        return False
    if expected is None:
        return True
    try:
        normalized = expected_session(expected)
    except RegisteredSimSessionIdentityError:
        return False
    return all(current.get(name) == value for name, value in normalized.items())


def canonical_sdk_direct_grant_client(client: Any) -> Optional[Tuple[Any, Any]]:
    """Return the canonical SDK module/type for its one-contract direct grant."""

    module_name = type(client).__module__
    if module_name not in SDK_CLIENT_MODULE_NAMES:
        return None
    sdk_module = sys.modules.get(module_name)
    trader_client_type = getattr(sdk_module, "TraderClient", None) if sdk_module else None
    if not isinstance(trader_client_type, type) or type(client) is not trader_client_type:
        return None
    state_getter = getattr(client, "get_execution_gate_state", None)
    sdk_state_getter = getattr(trader_client_type, "get_execution_gate_state", None)
    scalar_armer = getattr(client, "arm_execution_for_registered_sim", None)
    sdk_scalar_armer = getattr(trader_client_type, "arm_execution_for_registered_sim", None)
    if (
        not callable(state_getter)
        or getattr(state_getter, "__func__", None) is not sdk_state_getter
        or not callable(sdk_scalar_armer)
        or getattr(scalar_armer, "__func__", None) is not sdk_scalar_armer
    ):
        return None
    return sdk_module, trader_client_type


def canonical_sdk_route_instrument(
    client: Any,
    key: Tuple[str, str],
    *,
    bundle: bool,
) -> Optional[str]:
    """Resolve one route key through the canonical SDK's own instrument identity."""

    canonical_client = canonical_sdk_direct_grant_client(client)
    if canonical_client is None:
        return None
    sdk_module, _trader_client_type = canonical_client
    canonicalize = getattr(
        sdk_module,
        "canonical_ctp_bundle_instrument" if bundle else "canonical_ctp_instrument",
        None,
    )
    if not callable(canonicalize):
        return None
    try:
        result = canonicalize(key[0], key[1])
    except Exception:
        return None
    if type(result) is not str or not result or result != result.strip():
        return None
    return result


def write_route_invest_unit_id(instrument_id: Any, exchange_id: Any) -> Optional[str]:
    """Return the ``InvestUnitID`` this CTP order route writes.

    The current direct-order adapter writes the CTP default unit (empty string)
    into every ``InputOrder``/``InputOrderAction`` field. Keeping the value in
    one source helper lets the read-side fee projection bind to the same route
    contract used to construct the native request. ``None`` means the route key
    itself is not exact text, never a default unit.
    """

    if (
        type(instrument_id) is not str
        or not instrument_id
        or instrument_id != instrument_id.strip()
        or type(exchange_id) is not str
        or exchange_id != exchange_id.strip()
    ):
        return None
    return ""


def gate_state_snapshot(
    state: Any,
    key: Tuple[str, str],
    client: Any = None,
) -> Optional[Dict[str, Any]]:
    """Return an immutable route-bound snapshot of an armed SDK grant.

    The bundle contract keeps its full authorized-instrument set. The public
    direct registered-simulation SDK contract is a distinct single-contract
    grant; accept it only from the canonical TraderClient and only when its
    exchange-qualified SDK identity resolves to this exact Store route.
    """

    if (
        not isinstance(state, Mapping)
        or not isinstance(key, tuple)
        or len(key) != 2
        or not all(type(value) is str and value and value == value.strip() for value in key)
    ):
        return None

    authorized = state.get("authorized_instruments")
    scope_version = state.get("scope_version")
    sdk_instrument = state.get("instrument")
    canonical_client = canonical_sdk_direct_grant_client(client)
    authorized_tuple: Optional[Tuple[str, ...]] = None
    grant_kind = "bundle"

    if scope_version is None and authorized is None:
        if canonical_client is None:
            return None
        expected_sdk_instrument = canonical_sdk_route_instrument(client, key, bundle=False)
        if expected_sdk_instrument is None or sdk_instrument != expected_sdk_instrument:
            return None
        grant_kind = "direct_single_instrument"
    else:
        if not isinstance(authorized, (tuple, list)):
            return None
        authorized_tuple = tuple(authorized)
        if (
            not authorized_tuple
            or any(
                not isinstance(item, str) or not item or item != item.strip()
                for item in authorized_tuple
            )
            or len(set(authorized_tuple)) != len(authorized_tuple)
        ):
            return None
        expected_sdk_instrument = canonical_sdk_route_instrument(client, key, bundle=True)
        if expected_sdk_instrument is None and canonical_client is None:
            expected_sdk_instrument = key[0]
        if expected_sdk_instrument is None:
            return None
        if (
            type(expected_sdk_instrument) is not str
            or not expected_sdk_instrument
            or sdk_instrument != expected_sdk_instrument
            or expected_sdk_instrument not in authorized_tuple
            or not isinstance(scope_version, str)
            or not scope_version
        ):
            return None

    proof_sha256 = state.get("proof_sha256")
    environment_profile = state.get("environment_profile")
    if (
        state.get("managed") is not True
        or state.get("armed") is not True
        or type(state.get("connection_generation")) is not int
        or state.get("connection_generation") <= 0
        or not isinstance(state.get("trading_day"), str)
        or re.fullmatch(r"[0-9]{8}", state.get("trading_day", "")) is None
        or not isinstance(proof_sha256, str)
        or SHA256_PATTERN.fullmatch(proof_sha256) is None
        or not isinstance(environment_profile, str)
        or not environment_profile
        or "revocation_reason" not in state
        or state.get("revocation_reason") is not None
    ):
        return None

    # Keep the complete bundle scope. The scalar form is explicitly labelled
    # as one SDK-issued contract; no bundle scope or authorized list is
    # synthesized for it.
    return {
        "managed": True,
        "armed": True,
        "instrument": key[0],
        "sdk_instrument": sdk_instrument,
        "connection_generation": state["connection_generation"],
        "trading_day": state["trading_day"],
        "grant_kind": grant_kind,
        "scope_version": scope_version,
        "authorized_instruments": authorized_tuple,
        "environment_profile": environment_profile,
        "proof_sha256": proof_sha256,
        "revocation_reason": None,
    }


def detached_gate_state(snapshot: Mapping[str, Any]) -> Dict[str, Any]:
    """Copy cached proof evidence without exposing its tuple-backed authority."""

    result = dict(snapshot)
    authorized = snapshot["authorized_instruments"]
    result["authorized_instruments"] = list(authorized) if authorized is not None else None
    return result


def public_gate_projection(state: Any) -> Optional[Dict[str, Any]]:
    """Copy non-secret gate observations for a coherent Store facts snapshot."""

    if not isinstance(state, Mapping):
        return None
    authorized = state.get("authorized_instruments")
    if isinstance(authorized, (tuple, list)) and all(isinstance(item, str) for item in authorized):
        authorized = tuple(authorized)
    else:
        authorized = None
    return {
        "managed": state.get("managed") if type(state.get("managed")) is bool else None,
        "armed": state.get("armed") if type(state.get("armed")) is bool else None,
        "instrument": state.get("instrument") if isinstance(state.get("instrument"), str) else None,
        "connection_generation": state.get("connection_generation")
        if type(state.get("connection_generation")) is int
        else None,
        "trading_day": state.get("trading_day")
        if isinstance(state.get("trading_day"), str)
        else None,
        "authorized_instruments": authorized,
    }


def native_order_ref(field: Any) -> str:
    """Return the native field's text ``OrderRef``, or ``""`` when it is not text.

    A non-string ``OrderRef`` is not a route the framework may key by, so it
    projects to the same empty value the caller already used.
    """

    raw_order_ref = getattr(field, "OrderRef", None)
    return raw_order_ref if isinstance(raw_order_ref, str) else ""


def native_field_identity_rejection(operation: str, field: Any, req_id: Any) -> Optional[str]:
    """Return one finite code when the outbound struct is not its own request.

    ``None`` means the assertions hold. The caller maps each code onto its own
    public error text and raises before any native method is looked up.
    """

    order_ref = native_order_ref(field)
    if order_ref and order_ref != order_ref.strip():
        return NATIVE_FIELD_IDENTITY_ORDER_REF_NOT_EXACT_TEXT
    if operation == "insert" and not order_ref:
        return NATIVE_FIELD_IDENTITY_INSERT_ORDER_REF_MISSING
    if type(req_id) is not int or req_id <= 0 or getattr(field, "RequestID", None) != req_id:
        return NATIVE_FIELD_IDENTITY_MISMATCH
    return None


def native_action_matches_cancel_target(field: Any, req_id: Any, target: Any) -> bool:
    """Whether one native ``InputOrderActionField`` is exactly its target.

    Every CTP identity field the provider keys a cancellation on must match the
    verified ``RegisteredSimCancelTarget`` bit for bit, including the
    request-bound ``OrderActionRef`` and the ``ActionFlag`` spelling. This is a
    field comparison only: the caller still proves the target's issuer, seal,
    freshness and route binding, and a ``False`` never permits a retry.
    """

    return bool(
        getattr(field, "InstrumentID", None) == target.instrument_id
        and getattr(field, "ExchangeID", None) == target.exchange_id
        and getattr(field, "OrderRef", None) == target.order_ref
        and getattr(field, "OrderSysID", None) == target.order_sys_id
        and getattr(field, "FrontID", None) == target.front_id
        and getattr(field, "SessionID", None) == target.session_id
        and getattr(field, "BrokerID", None) == target.broker_id
        and getattr(field, "InvestorID", None) == target.investor_id
        and getattr(field, "OrderActionRef", None) == req_id
        and getattr(field, "ActionFlag", None) == NATIVE_ACTION_FLAG_CANCEL
    )
