"""CTP native order and position-side field selection."""
from __future__ import annotations

from bt_api_base.order_side_projection import selected_order_side_candidates


def order_side_candidates(row, *, position=False):
    return selected_order_side_candidates(
        row, position=position,
        side_fields=("side", "order_side", "trade_side"),
        position_side_fields=('position_side',),
    )


__all__ = ("order_side_candidates",)
