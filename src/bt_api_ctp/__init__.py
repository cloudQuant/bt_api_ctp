"""Public package surface for the CTP provider."""

from __future__ import annotations

from bt_api_ctp.containers.ctp.ctp_native_query_certificate import (
    CtpNativeQueryCertificate,
    CtpNativeQueryCertificateBuilder,
    CtpNativeQueryCertificateError,
)
from bt_api_ctp.instrument import normalize_ctp_instrument
from bt_api_ctp.order_action import CtpOrderActionEvidence
from bt_api_ctp.query import QueryResult
from bt_api_ctp.query_facts import (
    QUERY_METHODS,
    QUERY_REQUEST_TYPES,
    REQUIRED_QUERY_FAMILIES,
)
from bt_api_ctp.receipts import (
    RegisteredSimCancelTarget,
    RegisteredSimDispatchEvidence,
    RegisteredSimOrderIdentityQueryReceipt,
    RegisteredSimRouteEligibility,
    RegisteredSimUnknownRetirement,
)
from bt_api_ctp.simnow import (
    RegisteredSimSessionIdentityError,
    expected_session,
    gate_state_snapshot,
    session_identity,
    session_matches,
)
from bt_api_ctp.status import COMMISSION_REQUEST_FILTER_FIELDS, TERMINAL_ORDER_STATUSES


def get_ctp_native_diagnostics():
    """Return diagnostics for the runtime selected by the CTP client."""
    from bt_api_ctp.ctp.client import get_ctp_native_diagnostics as _diagnostics

    return _diagnostics()


def is_ctp_native_loaded() -> bool:
    """Return whether the selected CTP runtime loaded its native binary."""
    return bool(get_ctp_native_diagnostics()["native_loaded"])


__version__ = "2.0.4+iteration41.i9"
__all__ = [
    "QueryResult",
    "CtpOrderActionEvidence",
    "CtpNativeQueryCertificate",
    "CtpNativeQueryCertificateBuilder",
    "CtpNativeQueryCertificateError",
    "QUERY_METHODS",
    "QUERY_REQUEST_TYPES",
    "REQUIRED_QUERY_FAMILIES",
    "RegisteredSimCancelTarget",
    "RegisteredSimDispatchEvidence",
    "RegisteredSimOrderIdentityQueryReceipt",
    "RegisteredSimRouteEligibility",
    "RegisteredSimSessionIdentityError",
    "RegisteredSimUnknownRetirement",
    "COMMISSION_REQUEST_FILTER_FIELDS",
    "TERMINAL_ORDER_STATUSES",
    "expected_session",
    "gate_state_snapshot",
    "get_ctp_native_diagnostics",
    "is_ctp_native_loaded",
    "normalize_ctp_instrument",
    "session_identity",
    "session_matches",
]
