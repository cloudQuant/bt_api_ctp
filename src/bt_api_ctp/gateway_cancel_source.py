"""Exact Python CTP cancel source labels; these are not vendor enqueue proof."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from types import MappingProxyType
from typing import Any

_SOURCE_RE = re.compile(r"ctp-query-source-v1:[0-9a-f]{64}\Z", re.ASCII)
_DIGEST_RE = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)
_SOURCE_FIELDS = frozenset({"schema", "source_ref", "source_tags_digest", "login_request_id"})


def source_tags_digest(tags: object) -> str:
    if (
        type(tags) is not tuple
        or len(tags) != 6
        or any(
            type(value) is not str
            or not value
            or value != value.strip()
            or len(value) > 256
            or len(value.encode("utf-8")) > 256
            for value in tags[:4]
        )
        or any(type(value) is not int or not 0 < value < 2**63 for value in tags[4:])
    ):
        raise ValueError("INVALID_CANCEL_SOURCE")
    raw = json.dumps(list(tags), sort_keys=True, ensure_ascii=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("ascii")).hexdigest()


def validate_cancel_source_binding(binding: object) -> dict[str, Any]:
    if type(binding) not in (dict, MappingProxyType) or set(binding) != _SOURCE_FIELDS:
        raise ValueError("INVALID_CANCEL_SOURCE")
    result = dict(binding)
    if (
        result["schema"] != "ctp-cancel-source-v1"
        or type(result["schema"]) is not str
        or type(result["source_ref"]) is not str
        or len(result["source_ref"]) != len("ctp-query-source-v1:") + 64
        or _SOURCE_RE.fullmatch(result["source_ref"]) is None
        or type(result["source_tags_digest"]) is not str
        or _DIGEST_RE.fullmatch(result["source_tags_digest"]) is None
        or type(result["login_request_id"]) is not int
        or not 0 < result["login_request_id"] < 2**63
    ):
        raise ValueError("INVALID_CANCEL_SOURCE")
    return result


def capture_cancel_source_locked(trader: Any, *, environment: str | None) -> dict[str, Any]:
    """Read a sealed current login and query-source identity under Trader's lock."""
    from bt_api_ctp.registered_sim_session import capture_query_session

    login_before = trader._current_login_identity_locked()
    if (
        login_before is None
        or type(login_before.request_id) is not int
        or login_before.request_id <= 0
    ):
        raise ValueError("INVALID_CANCEL_SOURCE")
    api, spi = trader._api, trader._spi
    binding = capture_query_session(trader, environment=environment)
    login_after = trader._current_login_identity_locked()
    if (
        login_after is not login_before
        or trader._api is not api
        or trader._spi is not spi
        or trader._session_native_api is not api
        or spi is None
        or spi._native_api is not api
        or not spi._is_current_locked()
        or binding.connection_generation != login_before.connection_generation
        or binding.trading_day != login_before.trading_day
        or binding.broker_id != login_before.broker_id
        or binding.investor_id != login_before.user_id
    ):
        raise ValueError("INVALID_CANCEL_SOURCE")
    return validate_cancel_source_binding(
        {
            "schema": "ctp-cancel-source-v1",
            "source_ref": binding.source_ref,
            "source_tags_digest": source_tags_digest(binding._source_tags),
            "login_request_id": login_before.request_id,
        }
    )


def cancel_attempt_key(
    *,
    broker: str,
    investor: str,
    day: str,
    front: int,
    session: int,
    request_id: int,
    action_ref: int,
    source_binding: object,
) -> str:
    """Stable key over actual dispatch scope and the exact Python source label."""
    source = validate_cancel_source_binding(source_binding)
    if (
        any(
            type(v) is not str
            or not v
            or v != v.strip()
            or not v.isprintable()
            or len(v.encode("utf-8")) > 256
            for v in (broker, investor)
        )
        or type(day) is not str
        or len(day) != 8
        or not day.isascii()
        or not day.isdigit()
        or any(
            type(v) is not int or not 0 < v < 2**31
            for v in (front, session, request_id, action_ref)
        )
    ):
        raise ValueError("INVALID_CANCEL_SOURCE")
    material = [
        "ctp-cancel-v2",
        broker,
        investor,
        day,
        front,
        session,
        request_id,
        action_ref,
        source,
    ]
    try:
        datetime.strptime(day, "%Y%m%d")
    except ValueError:
        raise ValueError("INVALID_CANCEL_SOURCE") from None
    encoded = json.dumps(material, sort_keys=True, ensure_ascii=True, separators=(",", ":"))
    return "ctp-cancel-v2:" + hashlib.sha256(encoded.encode("ascii")).hexdigest()
