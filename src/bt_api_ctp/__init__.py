"""Public package surface for the CTP provider."""

from __future__ import annotations

from bt_api_ctp.instrument import normalize_ctp_instrument
from bt_api_ctp.query import QueryResult


def get_ctp_native_diagnostics():
    """Return diagnostics for the runtime selected by the CTP client."""
    from bt_api_ctp.ctp.client import get_ctp_native_diagnostics as _diagnostics

    return _diagnostics()


def is_ctp_native_loaded() -> bool:
    """Return whether the selected CTP runtime loaded its native binary."""
    return bool(get_ctp_native_diagnostics()["native_loaded"])


__version__ = "2.0.2"
__all__ = [
    "QueryResult",
    "get_ctp_native_diagnostics",
    "is_ctp_native_loaded",
    "normalize_ctp_instrument",
]
