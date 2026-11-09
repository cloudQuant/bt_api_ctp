"""CTP prepared-place source identity and domain-separated attempt key.

Labels identify the Python callback boundary, not a venue's internal queue.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from types import MappingProxyType

from bt_api_ctp.gateway_cancel_source import (
    capture_cancel_source_locked,
    validate_cancel_source_binding,
)
from bt_api_ctp.native_session_id import is_ctp_session_id


def validate_place_source_binding(binding: object) -> dict:
    if type(binding) not in (dict, MappingProxyType):
        raise ValueError("INVALID_PLACE_SOURCE")
    source = dict(binding)
    if source.get("schema") != "ctp-place-source-v1" or type(source.get("schema")) is not str:
        raise ValueError("INVALID_PLACE_SOURCE")
    source["schema"] = "ctp-cancel-source-v1"
    validate_cancel_source_binding(source)
    source["schema"] = "ctp-place-source-v1"
    return source


def capture_place_source_locked(trader, *, environment):
    source = capture_cancel_source_locked(trader, environment=environment)
    source["schema"] = "ctp-place-source-v1"
    return validate_place_source_binding(source)


def place_attempt_key(
    *, broker, investor, day, front, session, request_id, order_ref, source_binding
):
    source = validate_place_source_binding(source_binding)
    if (
        any(
            type(v) is not str
            or not v
            or v != v.strip()
            or not v.isprintable()
            or len(v) > 256
            or len(v.encode("utf-8")) > 256
            for v in (broker, investor)
        )
        or type(day) is not str
        or len(day) != 8
        or not day.isascii()
        or not day.isdigit()
        or any(type(v) is not int or not 0 < v < 2**31 for v in (front, request_id))
        or not is_ctp_session_id(session)
        or type(order_ref) is not str
        or len(order_ref) != 12
        or not order_ref.isascii()
        or not order_ref.isdigit()
    ):
        raise ValueError("INVALID_PLACE_SOURCE")
    try:
        datetime.strptime(day, "%Y%m%d")
    except ValueError:
        raise ValueError("INVALID_PLACE_SOURCE") from None
    material = [
        "ctp-place-v1",
        broker,
        investor,
        day,
        front,
        session,
        request_id,
        order_ref,
        source,
    ]
    encoded = json.dumps(material, sort_keys=True, ensure_ascii=True, separators=(",", ":"))
    return "ctp-place-v1:" + hashlib.sha256(encoded.encode("ascii")).hexdigest()
