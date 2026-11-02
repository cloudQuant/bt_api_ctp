"""CTP-specific settings required before a managed read-only connection."""

from __future__ import annotations

from typing import Any


_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})


def _auto_settlement_confirm_enabled(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().casefold() in _TRUE_VALUES


def require_read_only_settings(
    exchange_params: dict[str, Any], *, operation: str, error_type: type[Exception]
) -> None:
    """Reject and disable automatic CTP settlement writes during preflight."""
    if _auto_settlement_confirm_enabled(exchange_params.get("auto_settlement_confirm")):
        raise error_type(
            operation,
            "ctp_auto_settlement_confirm_enabled",
            definite_reject=True,
        )
    # An explicit value prevents a provider-default change from enabling writes.
    exchange_params["auto_settlement_confirm"] = False
