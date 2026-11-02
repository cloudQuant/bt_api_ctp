"""Single owner for the CTP registered-sim query protocol and record facts.

The names here were previously a second source of truth inside the Backtrader
``registered_sim`` Store. They are provider facts, so they now live only in this
module:

* ``QUERY_METHODS`` / ``QUERY_REQUEST_TYPES`` / ``REQUIRED_QUERY_FAMILIES`` map
  one query family to the ``TraderClient`` method, its typed ``request_type`` and
  the families a complete account observation requires. ``QUERY_REQUEST_TYPES``
  is the registered-sim lane's own family table; the separately scoped
  read-only lane in :mod:`bt_api_ctp.account_provider` keeps its private
  request-type-keyed subset and is not changed here.
* the ``query_*`` and ``commission_*``/``instrument_*`` helpers interpret the
  records and sealed ``_QuerySource`` provenance a live CTP query returns.

Filter/status/identity tables that already have an owner in this package are
imported, never copied: ``status.COMMISSION_REQUEST_FILTER_FIELDS`` stays the
fee filter owner, and ``query.QueryResult``/``_QuerySource`` stay the result
shape owners.

Every function here is pure with respect to the framework: it reads values and
returns plain values. Nothing grants admission, authorizes a write, or
reconciles an account. ``query_result_facts`` cannot project a native record
into a credential-safe public mapping by itself, so the caller injects that
``row_projector``; the framework keeps that projection responsibility.
"""

from __future__ import annotations

import copy
import inspect
import math
import re
import sys
import time
from collections.abc import Mapping
from datetime import datetime
from typing import Any, Callable, Dict, Optional, Tuple

from bt_api_ctp.status import COMMISSION_REQUEST_FILTER_FIELDS

__all__ = [
    "QUERY_METHODS",
    "QUERY_REQUEST_TYPES",
    "REQUIRED_QUERY_FAMILIES",
    "SDK_CLIENT_MODULE_NAMES",
    "SHA256_PATTERN",
    "is_aware_datetime",
    "is_positive_int",
    "read_fact",
    "query_error_is_clear",
    "freeze_query_filter",
    "query_snapshot_key",
    "public_query_snapshot",
    "request_filter_map",
    "commission_request_filters_match",
    "commission_rows_match_session",
    "instrument_is_trading",
    "instrument_eligibility_from_rows",
    "query_result_facts",
    "sdk_cancel_deadline_contract",
    "is_sdk_pre_dispatch_gate_error",
]

#: The only module names a canonical loaded CTP trader client may come from.
#: Identity checks below compare against these names and then against the
#: class object actually stored in ``sys.modules``; a duck-typed or subclassed
#: client never satisfies them.
SDK_CLIENT_MODULE_NAMES = ("bt_api_ctp.ctp.client", "bt_api_py.ctp.client")

#: ``family -> TraderClient method`` for the registered-sim query lane.
QUERY_METHODS = {
    "account_result": "query_account_result",
    "positions_result": "query_positions_result",
    "orders_result": "query_orders_result",
    "trades_result": "query_trades_result",
    "instruments_result": "query_instruments_result",
    "instrument_margin_rate_result": "query_instrument_margin_rate_result",
    "instrument_commission_rate_result": "query_instrument_commission_rate_result",
}
#: ``family -> typed QueryResult.request_type``.
QUERY_REQUEST_TYPES = {
    "account_result": "account",
    "positions_result": "positions",
    "orders_result": "orders",
    "trades_result": "trades",
    "instruments_result": "instruments",
    "instrument_margin_rate_result": "margin_rate",
    "instrument_commission_rate_result": "commission_rate",
}
#: Families a caller must have before it may claim a complete account state.
#: Reference data (instruments/margin/commission) is deliberately not here.
REQUIRED_QUERY_FAMILIES = frozenset(
    {"account_result", "positions_result", "orders_result", "trades_result"}
)

SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")

#: Distinct-from-``None`` marker so an absent native field stays unobserved
#: instead of being fabricated as ``None``. Never leaves this module: the facts
#: it produces report absence as ``None`` plus an explicit ``*_observed`` bool.
_UNOBSERVED = object()

_COMMISSION_FAMILY = "instrument_commission_rate_result"


def read_fact(value: Any, name: str, default: Any = None) -> Any:
    """Read one field from a Mapping record or an object attribute."""

    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


def is_positive_int(value: Any) -> bool:
    """Whether the value is an exact ``int`` (not ``bool``) greater than zero."""

    return type(value) is int and value > 0


def is_aware_datetime(value: Any) -> bool:
    """Whether the value is a datetime carrying an actual UTC offset."""

    return (
        isinstance(value, datetime) and value.tzinfo is not None and value.utcoffset() is not None
    )


def query_error_is_clear(value: Any) -> bool:
    """Whether one CTP error field reports no error.

    CTP returns an error id as ``int`` and, through some vendor paths, as text;
    a missing value is treated as clear only because the caller separates
    "absent" with its own observation flag.
    """

    if value is None:
        return True
    if type(value) is int:
        return value == 0
    return type(value) is str and value in ("", "0")


def freeze_query_filter(value: Any) -> Any:
    """Build a type-sensitive, immutable key for public query kwargs."""

    value_type = type(value)
    if value is None or value_type in (str, bool, int):
        return (value_type.__name__, value)
    if value_type is float:
        if not math.isfinite(value):
            raise ValueError("query filters must not contain non-finite floats")
        return ("float", value)
    if value_type in (tuple, list):
        return (value_type.__name__, tuple(freeze_query_filter(item) for item in value))
    if isinstance(value, Mapping):
        items = []
        for key, item in value.items():
            if type(key) is not str:
                raise TypeError("query filter mapping keys must be exact strings")
            items.append((key, freeze_query_filter(item)))
        return ("mapping", tuple(sorted(items)))
    raise TypeError("query filters must contain only immutable public values")


def query_snapshot_key(family: str, filters: Mapping) -> Tuple[Any, ...]:
    """Return the cache key binding one query family to its exact filters."""

    return (
        family,
        tuple(
            (key, freeze_query_filter(value))
            for key, value in sorted(filters.items())
            if type(key) is str
        ),
    )


def public_query_snapshot(facts: Mapping) -> Dict[str, Any]:
    """Return detached query facts without the opaque source-issuer token."""

    return {
        key: copy.deepcopy(value)
        for key, value in facts.items()
        if isinstance(key, str) and not key.startswith("_")
    }


def request_filter_map(value: Any) -> Optional[Dict[str, str]]:
    """Normalize immutable SDK request-filter pairs for an internal comparison."""

    if not isinstance(value, tuple):
        return None
    result = {}
    for pair in value:
        if (
            not isinstance(pair, tuple)
            or len(pair) != 2
            or type(pair[0]) is not str
            or type(pair[1]) is not str
        ):
            return None
        name = COMMISSION_REQUEST_FILTER_FIELDS.get(pair[0])
        if name is None or name in result:
            return None
        result[name] = pair[1]
    return result


def commission_request_filters_match(source: Any, scope: Any, filters: Mapping) -> bool:
    """Bind a fee result to the exact contract plus the current CTP identity."""

    instrument = filters.get("instrument_id")
    exchange = filters.get("exchange_id")
    broker_id = read_fact(scope, "broker_id")
    investor_id = read_fact(scope, "investor_id")
    if not all(
        isinstance(value, str) and value and value == value.strip()
        for value in (instrument, exchange, broker_id, investor_id)
    ):
        return False
    expected = {
        "InstrumentID": instrument,
        "ExchangeID": exchange,
        "BrokerID": broker_id,
        "InvestorID": investor_id,
    }
    intent = request_filter_map(read_fact(source, "request_intent_filters"))
    actual = request_filter_map(read_fact(source, "request_filters"))
    explicit = read_fact(source, "explicit_request_filters")
    return bool(
        intent == expected
        and actual == expected
        and explicit == ()
    )


def commission_rows_match_session(rows: Any, scope: Any, filters: Mapping) -> bool:
    """Project only known CTP commission identities for the current session."""

    broker_id = read_fact(scope, "broker_id")
    investor_id = read_fact(scope, "investor_id")
    instrument = filters.get("instrument_id")
    exchange = filters.get("exchange_id")
    if not isinstance(rows, list) or not rows:
        return False
    for row in rows:
        if not isinstance(row, Mapping) or not (
            row.get("BrokerID") == broker_id
            and row.get("InstrumentID") == instrument
            and row.get("ExchangeID") == exchange
            and row.get("InvestUnitID") == ""
            and row.get("BizType") in {"1", "2"}
        ):
            return False
        investor_range = row.get("InvestorRange")
        row_investor = row.get("InvestorID")
        if investor_range == "1":
            if row_investor not in ("", "\x00", investor_id):
                return False
        elif investor_range == "3":
            if row_investor != investor_id:
                return False
        else:
            # Group membership and unknown range codes cannot be proved here.
            return False
    return True


def instrument_is_trading(row: Mapping) -> Optional[bool]:
    """Return CTP ``IsTrading`` as a tri-state, never a guessed default."""

    raw = row.get("IsTrading", row.get("is_trading"))
    if raw is True or type(raw) is int and raw == 1:
        return True
    if type(raw) is str and raw.strip().lower() in {"1", "true", "yes"}:
        return True
    if raw is False or type(raw) is int and raw == 0:
        return False
    if type(raw) is str and raw.strip().lower() in {"0", "false", "no"}:
        return False
    return None


def instrument_eligibility_from_rows(
    rows: Any, instrument_id: str, exchange_id: str
) -> Optional[bool]:
    """Project one exact route from a complete, well-shaped instrument set."""

    if not isinstance(rows, list):
        return None
    matching = []
    for row in rows:
        if not isinstance(row, Mapping):
            return None
        instrument = row.get("InstrumentID", row.get("instrument_id"))
        exchange = row.get("ExchangeID", row.get("exchange_id"))
        trading = instrument_is_trading(row)
        if (
            not isinstance(instrument, str)
            or not instrument
            or not isinstance(exchange, str)
            or not exchange
            or trading is None
        ):
            return None
        if instrument == instrument_id and exchange == exchange_id:
            matching.append(trading)
    if not matching:
        return False
    if any(value is not matching[0] for value in matching[1:]):
        return None
    return matching[0]


def query_result_facts(
    result: Any,
    family: str,
    attempt: int,
    expected_scope: Any,
    requested_filters: Optional[Mapping] = None,
    *,
    row_projector: Callable[..., Dict[str, Any]],
) -> Dict[str, Any]:
    """Copy completion facts only from the current SDK's sealed query source.

    ``row_projector`` is supplied by the framework caller: turning a native or
    typed record into a stable, credential-safe public mapping is the Store's
    projection job, so this module never invents one. It is called with
    ``include_commission_fields`` set only for the fee family.
    """

    records = read_fact(result, "records")
    if records is None:
        records = read_fact(result, "rows")
    public_rows = None
    if isinstance(records, (list, tuple)):
        public_rows = [
            row_projector(
                row,
                include_commission_fields=family == _COMMISSION_FAMILY,
            )
            for row in records
        ]
    source = read_fact(result, "query_source")
    expected_issuer = read_fact(expected_scope, "issuer")
    error_code = read_fact(result, "error_code", _UNOBSERVED)
    error_message = read_fact(result, "error_message", _UNOBSERVED)
    unsupported = read_fact(result, "unsupported", _UNOBSERVED)
    late_callback_count = read_fact(result, "late_callback_count", _UNOBSERVED)
    submit_code = read_fact(result, "submit_code", _UNOBSERVED)
    started_at = read_fact(result, "started_at_utc", _UNOBSERVED)
    request_type = read_fact(result, "request_type")
    facts = {
        "family": family,
        "request_type": request_type,
        "expected_request_type": QUERY_REQUEST_TYPES.get(family),
        "rows": public_rows,
        "complete": read_fact(result, "complete"),
        "is_last_seen": read_fact(result, "is_last_seen"),
        "timed_out": read_fact(result, "timed_out"),
        "error_code": None if error_code is _UNOBSERVED else error_code,
        "error_code_observed": error_code is not _UNOBSERVED,
        "error_message": None if error_message is _UNOBSERVED else error_message,
        "unsupported": None if unsupported is _UNOBSERVED else unsupported,
        "late_callback_count": (
            None if late_callback_count is _UNOBSERVED else late_callback_count
        ),
        "submit_code": None if submit_code is _UNOBSERVED else submit_code,
        "submit_code_observed": submit_code is not _UNOBSERVED,
        "started_at_utc": None if started_at is _UNOBSERVED else started_at,
        "request_id": read_fact(result, "request_id"),
        "connection_generation": read_fact(result, "connection_generation"),
        "account_fingerprint": read_fact(result, "account_fingerprint"),
        "completed_at_utc": read_fact(result, "completed_at_utc"),
        "source_owned": False,
        "source_session_identity_valid": False,
        "source_account_rows_identity_valid": False,
        "source_account_investor_mapping_valid": False,
        "source_request_filters_valid": None,
        "source_commission_rows_identity_valid": None,
        "trading_day": None,
        "source_explicit_request_filters": None,
        "source_expires_monotonic": None,
        "source_started_monotonic": None,
        "source_completed_monotonic": None,
        "attempt": attempt,
    }
    completed_at = facts["completed_at_utc"]
    source_expiry = read_fact(source, "trusted_expires_monotonic", _UNOBSERVED)
    source_filters = read_fact(source, "explicit_request_filters", _UNOBSERVED)
    source_digest = read_fact(source, "records_sha256", _UNOBSERVED)
    source_started = read_fact(source, "started_at_utc", _UNOBSERVED)
    source_completed = read_fact(source, "completed_at_utc", _UNOBSERVED)
    source_started_monotonic = read_fact(source, "started_monotonic", _UNOBSERVED)
    source_completed_monotonic = read_fact(source, "completed_monotonic", _UNOBSERVED)
    source_generation = read_fact(source, "connection_generation", _UNOBSERVED)
    source_request_id = read_fact(source, "request_id", _UNOBSERVED)
    source_account = read_fact(source, "account_fingerprint", _UNOBSERVED)
    source_request_type = read_fact(source, "request_type", _UNOBSERVED)
    source_trading_day = read_fact(source, "trading_day", _UNOBSERVED)
    source_broker_id = read_fact(source, "broker_id", _UNOBSERVED)
    source_investor_id = read_fact(source, "investor_id", _UNOBSERVED)
    scope_generation = read_fact(expected_scope, "connection_generation", _UNOBSERVED)
    scope_account = read_fact(expected_scope, "account_fingerprint", _UNOBSERVED)
    scope_trading_day = read_fact(expected_scope, "trading_day", _UNOBSERVED)
    scope_broker_id = read_fact(expected_scope, "broker_id", _UNOBSERVED)
    scope_investor_id = read_fact(expected_scope, "investor_id", _UNOBSERVED)
    source_session_identity_valid = bool(
        type(source_broker_id) is str
        and source_broker_id
        and source_broker_id == source_broker_id.strip()
        and source_broker_id == scope_broker_id
        and type(source_investor_id) is str
        and source_investor_id
        and source_investor_id == source_investor_id.strip()
        and source_investor_id == scope_investor_id
    )
    account_rows_identity_valid = bool(
        family == "account_result"
        and source_session_identity_valid
        and isinstance(public_rows, list)
        and public_rows
        and all(
            isinstance(row, Mapping)
            and row.get("BrokerID") == scope_broker_id
            and type(row.get("AccountID")) is str
            and bool(row.get("AccountID"))
            and row.get("AccountID") == row.get("AccountID").strip()
            for row in public_rows
        )
    )
    account_investor_mapping_valid = bool(
        account_rows_identity_valid
        and all(
            row.get("InvestorID") in (None, "", "\x00", scope_investor_id)
            for row in public_rows
        )
    )
    monotonic_now = time.monotonic()
    source_owned = bool(
        source is not None
        and expected_scope is not None
        and read_fact(expected_scope, "read_only_ready") is True
        and expected_issuer is not None
        and read_fact(source, "issuer") is expected_issuer
        and source_request_type == facts["expected_request_type"]
        and source_request_id == facts["request_id"]
        and source_generation == facts["connection_generation"]
        and source_account == facts["account_fingerprint"]
        and source_started == facts["started_at_utc"]
        and source_completed == facts["completed_at_utc"]
        and isinstance(source_trading_day, str)
        and re.fullmatch(r"[0-9]{8}", source_trading_day) is not None
        and source_generation == scope_generation
        and source_account == scope_account
        and source_trading_day == scope_trading_day
        and is_positive_int(scope_generation)
        and isinstance(scope_account, str)
        and bool(scope_account)
        and isinstance(scope_trading_day, str)
        and re.fullmatch(r"[0-9]{8}", scope_trading_day) is not None
        and is_aware_datetime(source_started)
        and is_aware_datetime(source_completed)
        and type(source_started_monotonic) in (int, float)
        and type(source_completed_monotonic) in (int, float)
        and math.isfinite(source_started_monotonic)
        and math.isfinite(source_completed_monotonic)
        and source_started_monotonic <= source_completed_monotonic <= monotonic_now
        and type(source_expiry) in (int, float)
        and math.isfinite(source_expiry)
        and monotonic_now < source_expiry
        and isinstance(source_filters, tuple)
        and all(type(name) is str for name in source_filters)
        and isinstance(source_digest, str)
        and SHA256_PATTERN.fullmatch(source_digest) is not None
    )
    facts["source_owned"] = source_owned
    facts["trading_day"] = source_trading_day if source_owned else None
    facts["source_explicit_request_filters"] = source_filters if source_owned else None
    facts["source_records_sha256"] = source_digest if source_owned else None
    facts["source_expires_monotonic"] = source_expiry if source_owned else None
    facts["source_started_monotonic"] = source_started_monotonic if source_owned else None
    facts["source_completed_monotonic"] = source_completed_monotonic if source_owned else None
    facts["_source_issuer"] = expected_issuer if source_owned else None
    facts["source_session_identity_valid"] = source_session_identity_valid
    facts["source_account_rows_identity_valid"] = account_rows_identity_valid
    facts["source_account_investor_mapping_valid"] = account_investor_mapping_valid
    facts["source_request_filters_valid"] = (
        commission_request_filters_match(source, expected_scope, requested_filters or {})
        if family == _COMMISSION_FAMILY
        else None
    )
    facts["source_commission_rows_identity_valid"] = (
        commission_rows_match_session(public_rows, expected_scope, requested_filters or {})
        if family == _COMMISSION_FAMILY
        else None
    )
    facts["_source_broker_id"] = scope_broker_id if source_session_identity_valid else None
    facts["_source_investor_id"] = scope_investor_id if source_session_identity_valid else None
    submit_code_clear = submit_code is None or (type(submit_code) is int and submit_code == 0)
    records_are_public = isinstance(public_rows, list) and all(
        isinstance(row, Mapping) and bool(row) for row in public_rows
    )
    facts["query_complete"] = bool(
        facts["complete"] is True
        and facts["is_last_seen"] is True
        and facts["timed_out"] is False
        and facts["error_code_observed"] is True
        and query_error_is_clear(error_code)
        and type(facts["error_message"]) is str
        and facts["error_message"] == ""
        and facts["unsupported"] is False
        and type(facts["late_callback_count"]) is int
        and facts["late_callback_count"] == 0
        and facts["submit_code_observed"] is True
        and submit_code_clear
        and is_positive_int(facts["request_id"])
        and is_positive_int(facts["connection_generation"])
        and facts["request_type"] == facts["expected_request_type"]
        and isinstance(facts["account_fingerprint"], str)
        and bool(facts["account_fingerprint"])
        and is_aware_datetime(facts["started_at_utc"])
        and is_aware_datetime(completed_at)
        and facts["started_at_utc"] <= completed_at
        and source_owned
        and records_are_public
    )
    return facts


def _supports_explicit_keyword(method: Any, name: str) -> bool:
    if not callable(method):
        return False
    try:
        parameter = inspect.signature(method).parameters.get(name)
    except (TypeError, ValueError):
        return False
    return parameter is not None and parameter.kind in (
        inspect.Parameter.POSITIONAL_OR_KEYWORD,
        inspect.Parameter.KEYWORD_ONLY,
    )


def sdk_cancel_deadline_contract(client: Any, submit_method: Any = None) -> bool:
    """Require the exact SDK client and its native-boundary deadline contract.

    The contract is the canonical TraderClient implementation plus its typed
    deadline outcome/error API and explicit deadline arguments at both method
    boundaries. Duck-typed or instance-overridden methods are not a supported
    substitute, even when they accept a keyword with the same name.
    """

    module_name = type(client).__module__
    if module_name not in SDK_CLIENT_MODULE_NAMES:
        return False
    sdk_module = sys.modules.get(module_name)
    trader_client_type = getattr(sdk_module, "TraderClient", None) if sdk_module else None
    gate_error = getattr(sdk_module, "CtpExecutionGateError", None) if sdk_module else None
    deadline_error = (
        getattr(sdk_module, "_CtpNativeRequestDeadlineExpired", None) if sdk_module else None
    )
    dispatch_outcome_type = (
        getattr(sdk_module, "_CtpNativeRequestDispatchOutcome", None) if sdk_module else None
    )
    if (
        not isinstance(trader_client_type, type)
        or type(client) is not trader_client_type
        or not isinstance(gate_error, type)
        or gate_error.__module__ != module_name
        or not isinstance(deadline_error, type)
        or deadline_error.__module__ != module_name
        or not issubclass(deadline_error, gate_error)
        or not isinstance(dispatch_outcome_type, type)
        or dispatch_outcome_type.__module__ != module_name
        or "native_call_started"
        not in getattr(dispatch_outcome_type, "__dataclass_fields__", {})
    ):
        return False

    submit_method = (
        getattr(client, "submit_order_action", None)
        if submit_method is None
        else submit_method
    )
    sdk_submit = getattr(trader_client_type, "submit_order_action", None)
    getter = getattr(client, "get_order_action_evidence", None)
    sdk_getter = getattr(trader_client_type, "get_order_action_evidence", None)
    invoke = getattr(client, "_invoke_session_native_request", None)
    sdk_invoke = getattr(trader_client_type, "_invoke_session_native_request", None)
    if (
        not callable(sdk_submit)
        or getattr(submit_method, "__func__", None) is not sdk_submit
        or not _supports_explicit_keyword(submit_method, "deadline_monotonic")
        or not callable(sdk_getter)
        or getattr(getter, "__func__", None) is not sdk_getter
        or not callable(sdk_invoke)
        or getattr(invoke, "__func__", None) is not sdk_invoke
        or not _supports_explicit_keyword(invoke, "deadline_monotonic")
        or not _supports_explicit_keyword(invoke, "dispatch_outcome")
    ):
        return False

    return True


def is_sdk_pre_dispatch_gate_error(
    error: BaseException,
    *,
    client: Any,
    request_id: Any,
    order_action_ref: Any,
) -> bool:
    """Accept canonical SDK gate failures only when its action row is absent.

    Class identity binds the error to the loaded SDK module; the evidence
    lookup distinguishes a gate rejection from the same exception escaping
    after ReqOrderAction was entered. Missing or unreadable evidence fails
    closed as ambiguous.
    """

    module_name = type(error).__module__
    if module_name not in SDK_CLIENT_MODULE_NAMES:
        return False
    sdk_module = sys.modules.get(module_name)
    gate_error = getattr(sdk_module, "CtpExecutionGateError", None) if sdk_module else None
    if (
        not isinstance(gate_error, type)
        or gate_error.__module__ != module_name
        or gate_error not in type(error).__mro__
        or not sdk_cancel_deadline_contract(client)
        or type(request_id) is not int
        or request_id <= 0
        or type(order_action_ref) not in (int, str)
    ):
        return False
    evidence = client.get_order_action_evidence(
        request_id, order_action_ref=order_action_ref
    )
    return evidence is None
