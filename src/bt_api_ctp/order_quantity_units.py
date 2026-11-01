"""CTP-owned direct order quantity-unit projection."""

from __future__ import annotations

from typing import Any


def validate_order_quantity_units(exchange_name: str, request: Any) -> None:
    """Preserve the former CTP contracts/lots path without creating authority."""
    del exchange_name, request


__all__ = ("validate_order_quantity_units",)
