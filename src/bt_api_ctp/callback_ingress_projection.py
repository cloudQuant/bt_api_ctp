"""CTP callback ingress snapshot and native field projection.

The Execution store supplies its error type and digest-shape validator and
retains the transaction, source owner, writer fence and persistent state.
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any


def callback_payload_from_record(
    record: Any, *, expected_record_type: type, _is_sha256, ContractValidationError
) -> tuple[dict[str, Any], str, str]:
    """Validate the immutable callback snapshot without importing the SDK."""

    if type(expected_record_type) is not type or type(record) is not expected_record_type:
        raise ContractValidationError("typed CTP callback ingress record is required")
    to_payload = getattr(record, "to_payload", None)
    if not callable(to_payload):
        raise ContractValidationError("typed CTP callback ingress record is malformed")
    try:
        payload = to_payload()
        if type(payload) is not dict:
            raise ValueError
        expected = {
            "schema",
            "owner_intent_id",
            "callback_name",
            "callback_class",
            "phase",
            "source_instance_id",
            "native_client_epoch",
            "native_api_source_id",
            "native_spi_source_id",
            "native_api_generation",
            "source_connection_generation",
            "connection_generation",
            "sequence",
            "monotonic_ns",
            "named_args",
            "flattened_fields",
            "capture_complete",
            "missing_getters",
            "digest",
        }
        if set(payload) != expected or payload["schema"] != "ctp_trader_callback_ingress.v2":
            raise ValueError
        digest = payload["digest"]
        if not _is_sha256(digest):
            raise ValueError
        unsigned = dict(payload)
        del unsigned["digest"]
        digest_bytes = json.dumps(
            unsigned,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
        if hashlib.sha256(digest_bytes).hexdigest() != digest:
            raise ValueError
        canonical_bytes = json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
        if len(canonical_bytes) > 32 * 1024:
            raise ValueError
        if (
            payload["capture_complete"] is not True
            or payload["missing_getters"] != []
            or type(payload["sequence"]) is not int
            or payload["sequence"] <= 0
            or type(payload["monotonic_ns"]) is not int
            or payload["monotonic_ns"] <= 0
            or type(payload["native_api_generation"]) is not int
            or payload["native_api_generation"] <= 0
            or type(payload["source_connection_generation"]) is not int
            or payload["source_connection_generation"] < 0
            or type(payload["connection_generation"]) is not int
            or payload["connection_generation"] < 0
            or type(payload["named_args"]) is not list
            or type(payload["flattened_fields"]) is not list
        ):
            raise ValueError
        for key in (
            "owner_intent_id",
            "callback_name",
            "callback_class",
            "phase",
            "source_instance_id",
            "native_client_epoch",
            "native_api_source_id",
            "native_spi_source_id",
        ):
            if type(payload[key]) is not str or not payload[key] or len(payload[key]) > 128:
                raise ValueError
        if payload["callback_class"] not in {
            "PRE_LOGIN",
            "ROUTEABLE",
            "AUDIT_QUERY",
            "AUDIT_INFORMATIONAL",
            "LIFECYCLE_POISON",
            "UNSUPPORTED_FINANCIAL",
        }:
            raise ValueError
        if payload["phase"] not in {"PRE_LOGIN", "ACTIVE", "POISONED"}:
            raise ValueError
        # The SDK snapshot format is scalar-only. Recanonicalization also
        # rejects arbitrary objects, bytes, non-finite floats and NaN.
        encoded = json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
        self_digest = str(digest)
        return payload, encoded, self_digest
    except (AttributeError, TypeError, ValueError, KeyError, UnicodeError):
        raise ContractValidationError("CTP callback ingress record is invalid") from None

def query_named_scalar(
    payload: Mapping[str, Any], slot: int, expected_name: str, *, error_type
) -> Any:
    """Read one exact captured native-query argument without asserting its origin."""
    named = payload.get("named_args")
    if type(named) is not list:
        raise error_type("CTP query callback arguments are not captured")
    matches = [
        item
        for item in named
        if isinstance(item, dict) and item.get("argument_slot") == slot
    ]
    if len(matches) != 1:
        raise error_type("CTP query callback argument slot is ambiguous")
    item = matches[0]
    if (
        item.get("name") != expected_name
        or item.get("present") is not True
        or item.get("scalar_captured") is not True
    ):
        raise error_type("CTP query callback argument is incomplete")
    return item.get("value")


def callback_field(payload: Mapping[str, Any], argument_slot: int, field_name: str) -> Any:
    for item in payload["flattened_fields"]:
        if (
            type(item) is dict
            and item.get("argument_slot") == argument_slot
            and item.get("field_name") == field_name
        ):
            if item.get("present") is not True or item.get("scalar_captured") is not True:
                return None
            return item.get("value")
    return None

def callback_named_arg(payload: Mapping[str, Any], slot: int) -> Any:
    if slot < 0 or slot >= len(payload["named_args"]):
        return None
    item = payload["named_args"][slot]
    if type(item) is not dict or item.get("present") is not True:
        return None
    if item.get("scalar_captured") is not True:
        return None
    return item.get("value")
