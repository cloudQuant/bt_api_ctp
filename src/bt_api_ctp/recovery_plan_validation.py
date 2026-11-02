"""CTP recovery-plan field matching; callers retain authorization and state.

These checks compare supplied records only and do not issue an approval.
"""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal, InvalidOperation
from typing import Any


def ctp_recovery_action_plan_matches(
    approval: Any, plan: Mapping[str, Any]
) -> bool:
    """Match every signed recovery action to the current plan allowance."""

    payload = approval.payload
    actions = tuple(approval.recovery_actions)
    allowed_closes = tuple(plan.get("allowed_closes") or ())
    allowed_cancels = tuple(plan.get("allowed_cancels") or ())
    if (
        not all(isinstance(action, Mapping) for action in actions)
        or not all(isinstance(row, Mapping) for row in allowed_closes)
        or not all(isinstance(row, Mapping) for row in allowed_cancels)
    ):
        return False
    if len(actions) != len(allowed_closes) + len(allowed_cancels):
        return False
    common = {
        "account_fingerprint": payload.get("account_fingerprint"),
        "trading_day": payload.get("trading_day"),
        "connection_generation": payload.get("connection_generation"),
        "environment_profile": payload.get("environment_profile"),
        "candidate_id": payload.get("candidate_id"),
        "execution_cycle_id": payload.get("execution_cycle_id"),
    }
    action_index = 0
    for index, allowed in enumerate(allowed_closes):
        action = actions[action_index]
        action_index += 1
        expected = {
            "action_id": f"close:{index}",
            "action_kind": "close",
            "instrument_id": allowed.get("symbol"),
            "exchange_id": allowed.get("exchange_id"),
            "side": allowed.get("side"),
            "position_side": allowed.get("position_side"),
            "offset": allowed.get("offset"),
            "quantity": allowed.get("quantity"),
            "quantity_unit": allowed.get("quantity_unit"),
            **common,
        }
        if any(action.get(field) != value for field, value in expected.items()):
            return False
        if action.get("expires_at") != payload.get("expires_at"):
            return False
    for index, allowed in enumerate(allowed_cancels):
        action = actions[action_index]
        action_index += 1
        expected = {
            "action_id": f"cancel:{index}",
            "action_kind": "cancel",
            "instrument_id": allowed.get("symbol"),
            "exchange_id": allowed.get("exchange_id"),
            **common,
        }
        if any(action.get(field) != value for field, value in expected.items()):
            return False
        if action.get("expires_at") != payload.get("expires_at"):
            return False
    return True

def ctp_recovery_remaining_plan_matches(
    current: Mapping[str, Any], expected: Mapping[str, Any]
) -> bool:
    """Ensure consumed recovery allowances cannot be restored or borrowed."""

    if not isinstance(current, Mapping) or not isinstance(expected, Mapping):
        return False
    mutable = {"allowed_closes", "allowed_cancels"}
    if set(current) != set(expected):
        return False
    if any(current[field] != expected[field] for field in current if field not in mutable):
        return False

    def close_rows_fit(current_rows: Any, expected_rows: Any) -> bool:
        if not isinstance(current_rows, (list, tuple)) or not isinstance(
            expected_rows, (list, tuple)
        ):
            return False
        used = [False] * len(expected_rows)
        for current_row in current_rows:
            if not isinstance(current_row, Mapping):
                return False
            matched = False
            for index, expected_row in enumerate(expected_rows):
                if used[index] or not isinstance(expected_row, Mapping):
                    continue
                if set(current_row) != set(expected_row):
                    continue
                if any(
                    current_row[field] != expected_row[field]
                    for field in current_row
                    if field != "quantity"
                ):
                    continue
                try:
                    current_quantity = Decimal(str(current_row["quantity"]))
                    expected_quantity = Decimal(str(expected_row["quantity"]))
                except (InvalidOperation, TypeError, ValueError, KeyError):
                    return False
                if (
                    not current_quantity.is_finite()
                    or not expected_quantity.is_finite()
                    or current_quantity <= 0
                    or current_quantity > expected_quantity
                ):
                    return False
                used[index] = True
                matched = True
                break
            if not matched:
                return False
        return True

    current_closes = current.get("allowed_closes")
    expected_closes = expected.get("allowed_closes")
    current_cancels = current.get("allowed_cancels")
    expected_cancels = expected.get("allowed_cancels")
    if not close_rows_fit(current_closes, expected_closes):
        return False
    if not isinstance(current_cancels, (list, tuple)) or not isinstance(
        expected_cancels, (list, tuple)
    ):
        return False
    remaining_cancels = list(expected_cancels)
    for current_row in current_cancels:
        if not isinstance(current_row, Mapping):
            return False
        try:
            index = remaining_cancels.index(current_row)
        except ValueError:
            return False
        remaining_cancels.pop(index)
    return True


__all__ = ("ctp_recovery_action_plan_matches", "ctp_recovery_remaining_plan_matches")
