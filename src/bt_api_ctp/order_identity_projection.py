"""Map neutral order identity to CTP query/cancel fields."""

from __future__ import annotations

from typing import Any


def project_order_identity(operation: str, request: Any, kwargs: dict[str, Any], default_order_id: Any) -> Any:
    """Preserve the existing CTP query correlation fields and cancel projection."""
    if operation != "query":
        return default_order_id
    order_id = request.order_id
    for key in ("exchange_id", "front_id", "session_id", "order_ref"):
        value = getattr(request, key, None)
        if value is not None:
            kwargs[key] = value
    if order_id is None:
        kwargs.setdefault("order_ref", request.order_ref or request.client_order_id)
    return order_id
