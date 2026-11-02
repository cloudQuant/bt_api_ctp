"""CTP native submit/cancel field creation for an already claimed payload.

This module creates protocol fields only; the caller retains dispatch authority,
session ownership, request claiming and the exception boundary.
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def make_native_order_field(payload: Mapping[str, Any], *, validation_error: type[Exception]) -> Any:
    """Build a CTP leaf from the Store-claimed, canonical native payload."""

    if not isinstance(payload, Mapping) or not payload:
        raise validation_error("Store-claimed CTP native payload is required")
    # Keep the generated wrapper and its native extension out of module import.
    from bt_api_ctp.ctp.ctp_structs_order import CThostFtdcInputOrderField

    field = CThostFtdcInputOrderField()
    for name, value in payload.items():
        if type(name) is not str or not name or name == "OrderActionRef":
            raise validation_error("invalid field in CTP submit payload")
        setattr(field, name, value)
    return field


def make_native_order_action_field(payload: Mapping[str, Any], *, validation_error: type[Exception]) -> Any:
    """Build a CTP delete-action leaf from a Store-claimed native payload."""

    if not isinstance(payload, Mapping) or not payload:
        raise validation_error("Store-claimed CTP native cancel payload is required")
    # Keep the generated wrapper and its native extension out of module import.
    from bt_api_ctp.ctp.ctp_structs_order import CThostFtdcInputOrderActionField

    field = CThostFtdcInputOrderActionField()
    for name, value in payload.items():
        if type(name) is not str or not name or not hasattr(field, name):
            raise validation_error("invalid field in CTP cancel payload")
        if name == "OrderActionRef" and (type(value) is not int or value <= 0):
            raise validation_error("invalid Store-issued CTP ActionRef")
        setattr(field, name, value)
    return field


