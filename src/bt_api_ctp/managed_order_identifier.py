"""CTP native order identifier encoding for explicitly managed execution.

An encoded identifier is a value only; it does not verify a provider order or
grant dispatch authority. This module requires the managed Execution extra.
"""
from __future__ import annotations

import base64
import json
from typing import Any

from bt_api_execution import ContractValidationError


_CTP_PROVIDER_ORDER_ID_PREFIX = "ctp:v1:"
_CTP_PROVIDER_ORDER_ID_MAX_LENGTH = 128


def _validate_ctp_provider_order_identifier(value: Any, field_name: str) -> str:
    if (
        type(value) is not str
        or not value
        or value != value.strip()
        or len(value) > 128
        or not value.isascii()
        or any(not (char.isalnum() or char in "._:-") for char in value)
    ):
        raise ContractValidationError("invalid CTP provider " + field_name)
    return value


def encode_ctp_provider_order_id(exchange_id: str, order_sys_id: str) -> str:
    """Encode the two native CTP order identifiers without delimiter ambiguity."""

    exchange = _validate_ctp_provider_order_identifier(exchange_id, "ExchangeID")
    order = _validate_ctp_provider_order_identifier(order_sys_id, "OrderSysID")
    payload = json.dumps(
        [exchange, order], separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    encoded = base64.urlsafe_b64encode(payload).rstrip(b"=").decode("ascii")
    result = _CTP_PROVIDER_ORDER_ID_PREFIX + encoded
    if len(result) > _CTP_PROVIDER_ORDER_ID_MAX_LENGTH:
        raise ContractValidationError("CTP provider order id exceeds its length bound")
    return result


def decode_ctp_provider_order_id(provider_order_id: str) -> tuple[str, str]:
    """Decode and canonically validate one versioned CTP provider order ID."""

    if (
        type(provider_order_id) is not str
        or len(provider_order_id) > _CTP_PROVIDER_ORDER_ID_MAX_LENGTH
        or not provider_order_id.startswith(_CTP_PROVIDER_ORDER_ID_PREFIX)
    ):
        raise ContractValidationError("invalid CTP provider order id")
    encoded = provider_order_id[len(_CTP_PROVIDER_ORDER_ID_PREFIX) :]
    if (
        not encoded
        or not encoded.isascii()
        or any(not (char.isalnum() or char in "-_") for char in encoded)
        or len(encoded) % 4 == 1
    ):
        raise ContractValidationError("invalid CTP provider order id encoding")
    try:
        raw = base64.b64decode(
            encoded + "=" * (-len(encoded) % 4), altchars=b"-_", validate=True
        )
        decoded = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ContractValidationError("invalid CTP provider order id payload") from error
    if type(decoded) is not list or len(decoded) != 2:
        raise ContractValidationError("invalid CTP provider order id pair")
    exchange = _validate_ctp_provider_order_identifier(decoded[0], "ExchangeID")
    order = _validate_ctp_provider_order_identifier(decoded[1], "OrderSysID")
    result = (exchange, order)
    if encode_ctp_provider_order_id(*result) != provider_order_id:
        raise ContractValidationError("non-canonical CTP provider order id")
    return result


__all__ = ("encode_ctp_provider_order_id", "decode_ctp_provider_order_id")
