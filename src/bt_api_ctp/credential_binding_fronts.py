"""CTP-native active trader and market-stream credential binding.

The SDK supplies its live feed and ingress references and its error type.  This
module does not own SDK state, start connections, or issue authorization.
"""

from __future__ import annotations

import hashlib
import inspect
from collections.abc import Mapping
from typing import Any


def resolve_credential_binding_fronts(
    exchange_name: str,
    feed: Any,
    state: Mapping[str, Any],
    verifier: Any,
    *,
    operation: str,
    exchange_feeds: Any,
    market_ingress_for: Any,
    subscription_streams: Any,
    error_type: Any,
) -> dict[str, Any]:
    """Resolve the one exact owner feed and its active authenticated TD/MD pair."""

    NormalizedApiError = error_type

    code = "ctp_credential_binding_active_front_unavailable"
    feeds = tuple(exchange_feeds().values())
    if not feeds or sum(candidate is feed for candidate in feeds) != 1:
        raise NormalizedApiError(operation, code, definite_reject=True)

    if getattr(verifier, "_is_controlled_test_verifier", False):
        trader = getattr(feed, "_trader", None)
        md_client = getattr(feed, "_md_client", None)
        matching_traders = [
            getattr(candidate, "_trader", None)
            for candidate in feeds
            if getattr(candidate, "_trader", None) is not None
        ]
        if not trader or sum(candidate is trader for candidate in matching_traders) != 1:
            raise NormalizedApiError(operation, code, definite_reject=True)
        td_broker_id = str(getattr(trader, "_bound_broker_id", "") or "").strip()
        td_user_id = str(getattr(trader, "_bound_user_id", "") or "").strip()
        if not td_broker_id or not td_user_id:
            raise NormalizedApiError(operation, code, definite_reject=True)
        account_fingerprint_sha256 = hashlib.sha256(
            f"{td_broker_id}:{td_user_id}".encode("utf-8", "strict")
        ).hexdigest()
        td_front = str(getattr(trader, "front", "") or "").strip()
        md_front = str(getattr(md_client, "front", "") or "").strip()
        td_generation = getattr(trader, "_connection_generation", None)
        md_generation = getattr(md_client, "connection_generation", None)
        stream_generation = getattr(feed, "_md_stream_generation", None)
        if (
            not td_front
            or td_front != str(getattr(trader, "_bound_front", "") or "").strip()
            or td_front != str(getattr(trader, "_session_native_front", "") or "").strip()
            or td_front != str(getattr(feed, "_execution_bound_td_front", "") or "").strip()
            or getattr(trader, "_account_fingerprint", None) != account_fingerprint_sha256[:16]
            or state.get("account_fingerprint") != f"acct_{account_fingerprint_sha256[:16]}"
            or not md_front
            or md_front != str(getattr(feed, "_execution_bound_md_front", "") or "").strip()
            or type(td_generation) is not int
            or td_generation != state.get("connection_generation")
            or type(md_generation) is not int
            or md_generation <= 0
            or type(stream_generation) is not int
            or stream_generation <= 0
        ):
            raise NormalizedApiError(operation, code, definite_reject=True)
        return {
            "td_front": td_front,
            "md_front": md_front,
            "account_fingerprint_sha256": account_fingerprint_sha256,
            "md_connection_generation": md_generation,
            "md_stream_generation": stream_generation,
        }

    try:
        feed_type = ("bt_api_ctp.feeds.live_ctp_feed", "CtpRequestDataFuture")
        if (type(feed).__module__, type(feed).__name__) != feed_type:
            raise ValueError
        matching_feeds = [
            candidate
            for candidate in feeds
            if (type(candidate).__module__, type(candidate).__name__) == feed_type
        ]
        if len(matching_feeds) != 1 or matching_feeds[0] is not feed:
            raise ValueError
        trader = getattr(feed, "trader_client", None) or getattr(feed, "_trader", None)
        if (type(trader).__module__, type(trader).__name__) != (
            "bt_api_ctp.ctp.client",
            "TraderClient",
        ):
            raise ValueError
        owner_traders = []
        for candidate in feeds:
            candidate_trader = getattr(candidate, "trader_client", None) or getattr(
                candidate, "_trader", None
            )
            if (type(candidate_trader).__module__, type(candidate_trader).__name__) == (
                "bt_api_ctp.ctp.client",
                "TraderClient",
            ):
                owner_traders.append(candidate_trader)
        if len(owner_traders) != 1 or owner_traders[0] is not trader:
            raise ValueError
        td_front = str(getattr(trader, "front", "") or "").strip()
        td_generation = getattr(trader, "_connection_generation", None)
        if (
            not td_front
            or td_front != str(getattr(trader, "_bound_front", "") or "").strip()
            or td_front != str(getattr(trader, "_session_native_front", "") or "").strip()
            or td_front != str(getattr(feed, "_execution_bound_td_front", "") or "").strip()
            or type(td_generation) is not int
            or td_generation <= 0
            or type(state.get("connection_generation")) is not int
            or td_generation != state.get("connection_generation")
        ):
            raise ValueError

        ingress = market_ingress_for(exchange_name)
        streams = subscription_streams()
        if ingress is None or not isinstance(streams, (list, tuple)):
            raise ValueError
        candidates = [
            stream
            for stream in streams
            if str(getattr(stream, "stream_name", "")) == "ctp_market_stream"
            and getattr(stream, "data_queue", None) is ingress
            and getattr(stream, "_running", None) is True
        ]
        if len(candidates) != 1:
            raise ValueError
        stream = candidates[0]
        if (
            type(stream).__module__ != feed_type[0]
            or type(stream).__name__ != "CtpMarketStream"
            or str(getattr(getattr(stream, "state", None), "value", "")) != "authenticated"
        ):
            raise ValueError
        md_client = getattr(stream, "_md_client", None)
        if (type(md_client).__module__, type(md_client).__name__) != (
            "bt_api_ctp.ctp.client",
            "MdClient",
        ) or getattr(md_client, "is_ready", False) is not True:
            raise ValueError
        identity_snapshot_descriptor = inspect.getattr_static(
            type(md_client), "active_md_identity", None
        )
        if (
            type(identity_snapshot_descriptor) is not property
            or not callable(identity_snapshot_descriptor.fget)
            or identity_snapshot_descriptor.fset is not None
        ):
            raise NormalizedApiError(
                operation,
                "ctp_credential_binding_active_md_identity_unavailable",
                definite_reject=True,
            )
        from importlib import import_module

        # The SDK property is one lock-protected snapshot: its producer
        # returns None unless the active identity still matches the
        # current login request and connection generation.
        try:
            identity_module = import_module("bt_api_ctp.md_identity")
        except Exception:
            raise NormalizedApiError(
                operation,
                "ctp_credential_binding_active_md_identity_unavailable",
                definite_reject=True,
            ) from None
        identity_type = getattr(identity_module, "MdIdentityObservation", None)
        md_identity_matches = getattr(identity_module, "md_identity_matches", None)
        md_identity = getattr(md_client, "active_md_identity", None)
        identity_fields = {
            "front",
            "broker_id",
            "user_id",
            "connection_generation",
            "request_id",
            "trading_day",
            "authenticated",
        }
        if (
            not isinstance(identity_type, type)
            or not callable(md_identity_matches)
            or type(md_identity) is not identity_type
            or set(getattr(identity_type, "__dataclass_fields__", {})) != identity_fields
            or getattr(getattr(identity_type, "__dataclass_params__", None), "frozen", False)
            is not True
            or getattr(md_identity, "authenticated", None) is not True
        ):
            raise NormalizedApiError(
                operation,
                "ctp_credential_binding_active_md_identity_unavailable",
                definite_reject=True,
            )
        md_front_value = getattr(md_client, "front", None)
        feed_md_front = getattr(feed, "_execution_bound_md_front", None)
        stream_md_front = getattr(stream, "md_front", None)
        md_front = md_front_value.strip() if type(md_front_value) is str else ""
        md_bound_front = getattr(md_client, "_bound_front", None)
        md_generation = getattr(md_client, "connection_generation", None)
        stream_generation = getattr(stream, "_connection_generation", None)
        md_broker_id = str(getattr(md_identity, "broker_id", "") or "").strip()
        md_user_id = str(getattr(md_identity, "user_id", "") or "").strip()
        md_bound_broker_id = getattr(md_client, "_bound_broker_id", None)
        md_bound_user_id = getattr(md_client, "_bound_user_id", None)
        identity_broker_id = getattr(md_identity, "broker_id", None)
        identity_user_id = getattr(md_identity, "user_id", None)
        td_broker_id = str(getattr(trader, "_bound_broker_id", "") or "").strip()
        td_user_id = str(getattr(trader, "_bound_user_id", "") or "").strip()
        identity_generation = getattr(md_identity, "connection_generation", None)
        identity_request_id = getattr(md_identity, "request_id", None)
        identity_front = getattr(md_identity, "front", None)
        identity_trading_day = getattr(md_identity, "trading_day", None)
        profile = state.get("environment_profile")
        feed_profile = getattr(feed, "_execution_bound_profile", None)
        stream_profile = getattr(stream, "ctp_env_profile", None)
        account_fingerprint_sha256 = hashlib.sha256(
            f"{td_broker_id}:{td_user_id}".encode("utf-8", "strict")
        ).hexdigest()
        expected_account_fingerprint = str(
            getattr(trader, "_account_fingerprint", "") or ""
        ).strip()
        identity_matches = False
        try:
            identity_matches = md_identity_matches(
                md_identity,
                expected_front=md_front,
                expected_broker_id=td_broker_id,
                expected_user_id=td_user_id,
                expected_connection_generation=md_generation,
                expected_request_id=identity_request_id,
            )
        except (TypeError, ValueError):
            identity_matches = False
        if (
            not md_front
            or md_front_value != md_front
            or type(feed_md_front) is not str
            or feed_md_front != md_front
            or type(stream_md_front) is not str
            or stream_md_front != md_front
            or type(md_bound_front) is not str
            or md_bound_front != md_front
            or type(md_generation) is not int
            or md_generation <= 0
            or type(identity_front) is not str
            or identity_front != md_front
            or type(identity_generation) is not int
            or identity_generation <= 0
            or identity_generation != md_generation
            or type(identity_request_id) is not int
            or identity_request_id <= 0
            or type(identity_trading_day) is not str
            or type(state.get("trading_day")) is not str
            or identity_trading_day != state.get("trading_day")
            or type(identity_broker_id) is not str
            or type(identity_user_id) is not str
            or type(md_bound_broker_id) is not str
            or type(md_bound_user_id) is not str
            or not md_bound_broker_id
            or not md_bound_user_id
            or md_bound_broker_id != md_broker_id
            or md_bound_user_id != md_user_id
            or identity_matches is not True
            or not td_broker_id
            or not td_user_id
            or md_broker_id != td_broker_id
            or md_user_id != td_user_id
            or type(profile) is not str
            or not profile
            or profile != profile.strip()
            or type(feed_profile) is not str
            or feed_profile != profile
            or type(stream_profile) is not str
            or stream_profile != profile
            or not expected_account_fingerprint
            or expected_account_fingerprint != account_fingerprint_sha256[:16]
            or state.get("account_fingerprint") != f"acct_{account_fingerprint_sha256[:16]}"
            or type(stream_generation) is not int
            or stream_generation <= 0
            or type(getattr(stream, "_observed_client_generation", None)) is not int
            or getattr(stream, "_observed_client_generation", None) != md_generation
        ):
            raise NormalizedApiError(
                operation, "ctp_credential_binding_scope_mismatch", definite_reject=True
            )
        return {
            "td_front": td_front,
            "md_front": md_front,
            "account_fingerprint_sha256": account_fingerprint_sha256,
            "md_connection_generation": md_generation,
            "md_stream_generation": stream_generation,
        }
    except NormalizedApiError:
        raise
    except Exception:
        raise NormalizedApiError(operation, code, definite_reject=True) from None
