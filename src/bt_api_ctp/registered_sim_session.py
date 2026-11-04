"""Sealed CTP query-session identity binding used inside the SDK.

The binding is internal provider evidence. Only source_ref and opaque
generation values are suitable for neutral query receipts; account and native
callback identity stay inside bt_api_ctp.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import time
from dataclasses import dataclass, field
from typing import Any

from bt_api_ctp.ctp.client import TraderClient
from bt_api_ctp.query import _QUERY_SCOPE_SEAL, _QuerySessionScope


_BINDING_SEAL = object()
_HASH_RE = re.compile(r"^[0-9a-f]{64}$", re.ASCII)
_ENVIRONMENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$", re.ASCII)
_ENVIRONMENT_PROFILE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$", re.ASCII)
_TRADING_DAY_RE = re.compile(r"^[0-9]{8}$", re.ASCII)
_SOURCE_REF_PREFIX = "ctp-query-source-v1:"
_GENERATION_PREFIX = "ctp-query-generation-v1:"


class CtpQuerySessionBindingError(ValueError):
    """The exact CTP client's current read-only query scope cannot be bound."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class CtpQuerySessionBinding:
    """Sealed view of one current CTP query session.

    account_fingerprint, broker/investor identifiers and native source tags
    are retained for SDK-side revalidation only. Neutral query receipts should
    use only source_ref and generation.
    """

    source_ref: str
    generation: str
    environment: str | None
    environment_profile: str | None = field(repr=False, compare=False)
    account_fingerprint: str = field(repr=False, compare=False)
    connection_generation: int = field(repr=False, compare=False)
    trading_day: str = field(repr=False, compare=False)
    broker_id: str = field(repr=False, compare=False)
    investor_id: str = field(repr=False, compare=False)
    _issuer: Any = field(repr=False, compare=False)
    _source_tags: tuple[str, str, str, str, int, int] = field(repr=False, compare=False)
    _captured_monotonic: float = field(repr=False, compare=False)
    _seal: object = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        if self._seal is not _BINDING_SEAL:
            raise CtpQuerySessionBindingError("ctp_query_session_binding_unsealed")
        if type(self.source_ref) is not str or not self.source_ref.startswith(_SOURCE_REF_PREFIX):
            raise CtpQuerySessionBindingError("ctp_query_session_source_ref_invalid")
        if type(self.generation) is not str or not self.generation.startswith(_GENERATION_PREFIX):
            raise CtpQuerySessionBindingError("ctp_query_session_generation_invalid")
        if self.environment is not None and (
            type(self.environment) is not str
            or _ENVIRONMENT_RE.fullmatch(self.environment) is None
        ):
            raise CtpQuerySessionBindingError("ctp_query_session_environment_invalid")
        if self.environment_profile is not None and (
            type(self.environment_profile) is not str
            or _ENVIRONMENT_PROFILE_RE.fullmatch(self.environment_profile) is None
        ):
            raise CtpQuerySessionBindingError("ctp_query_session_environment_profile_invalid")
        if (
            type(self.account_fingerprint) is not str
            or re.fullmatch(r"[0-9a-f]{16}", self.account_fingerprint, re.ASCII) is None
        ):
            raise CtpQuerySessionBindingError("ctp_query_session_account_invalid")
        if type(self.connection_generation) is not int or self.connection_generation <= 0:
            raise CtpQuerySessionBindingError("ctp_query_session_connection_generation_invalid")
        if type(self.trading_day) is not str or _TRADING_DAY_RE.fullmatch(self.trading_day) is None:
            raise CtpQuerySessionBindingError("ctp_query_session_trading_day_invalid")
        if not _exact_text(self.broker_id) or not _exact_text(self.investor_id):
            raise CtpQuerySessionBindingError("ctp_query_session_native_identity_invalid")
        if self._issuer is None:
            raise CtpQuerySessionBindingError("ctp_query_session_issuer_invalid")
        if not _valid_source_tags(self._source_tags):
            raise CtpQuerySessionBindingError("ctp_query_session_source_tags_invalid")
        if (
            type(self._captured_monotonic) is not float
            or not math.isfinite(self._captured_monotonic)
            or self._captured_monotonic < 0
        ):
            raise CtpQuerySessionBindingError("ctp_query_session_capture_time_invalid")
        for value, prefix in (
            (self.source_ref, _SOURCE_REF_PREFIX),
            (self.generation, _GENERATION_PREFIX),
        ):
            if _HASH_RE.fullmatch(value[len(prefix) :]) is None:
                raise CtpQuerySessionBindingError("ctp_query_session_digest_invalid")


def _exact_text(value: Any) -> bool:
    return type(value) is str and bool(value) and value == value.strip()


def _valid_source_tags(value: Any) -> bool:
    return bool(
        type(value) is tuple
        and len(value) == 6
        and all(_exact_text(item) for item in value[:4])
        and type(value[4]) is int
        and value[4] > 0
        and type(value[5]) is int
        and value[5] > 0
    )


def _trusted_environment(
    value: Any,
    client: TraderClient,
) -> tuple[str | None, str | None]:
    if value is not None and (
        type(value) is not str or _ENVIRONMENT_RE.fullmatch(value) is None
    ):
        raise CtpQuerySessionBindingError("ctp_query_session_environment_invalid")
    profile = client.ctp_env_profile
    if profile is not None and (
        type(profile) is not str or _ENVIRONMENT_PROFILE_RE.fullmatch(profile) is None
    ):
        raise CtpQuerySessionBindingError("ctp_query_session_environment_profile_invalid")
    return value, profile


def _read_current_scope(
    client: Any,
    *,
    environment: Any,
) -> tuple[str | None, str | None, _QuerySessionScope]:
    if type(client) is not TraderClient:
        raise CtpQuerySessionBindingError("ctp_query_session_client_invalid")
    trusted_environment, environment_profile = _trusted_environment(environment, client)
    try:
        scope = client.get_query_session_scope()
        state = client.get_session_state()
    except Exception:
        raise CtpQuerySessionBindingError("ctp_query_session_state_unavailable") from None
    if type(scope) is not _QuerySessionScope or scope._seal is not _QUERY_SCOPE_SEAL:
        raise CtpQuerySessionBindingError("ctp_query_session_scope_unsealed")
    issuer = getattr(client, "_query_evidence_issuer", None)
    if issuer is None or scope.issuer is not issuer:
        raise CtpQuerySessionBindingError("ctp_query_session_issuer_mismatch")
    if scope.read_only_ready is not True or client.is_read_only_ready is not True:
        raise CtpQuerySessionBindingError("ctp_query_session_read_only_not_ready")

    broker_id = getattr(client, "_bound_broker_id", None)
    investor_id = getattr(client, "_bound_user_id", None)
    if not _exact_text(broker_id) or not _exact_text(investor_id):
        raise CtpQuerySessionBindingError("ctp_query_session_native_identity_invalid")
    expected_account = hashlib.sha256(
        (broker_id + ":" + investor_id).encode("utf-8")
    ).hexdigest()[:16]
    generation = getattr(client, "connection_generation", None)
    trading_day = getattr(scope, "trading_day", None)
    source_tags = getattr(scope, "source_tags", None)
    if (
        scope.account_fingerprint != expected_account
        or getattr(client, "_account_fingerprint", None) != expected_account
        or type(generation) is not int
        or generation <= 0
        or type(scope.connection_generation) is not int
        or scope.connection_generation != generation
        or scope.broker_id != broker_id
        or scope.investor_id != investor_id
        or type(trading_day) is not str
        or _TRADING_DAY_RE.fullmatch(trading_day) is None
        or not _valid_source_tags(source_tags)
    ):
        raise CtpQuerySessionBindingError("ctp_query_session_identity_invalid")

    if not isinstance(state, dict) or (
        state.get("read_only_ready") is not True
        or state.get("connection_generation") != generation
        or state.get("account_fingerprint") != expected_account
        or state.get("trading_day") != trading_day
        or state.get("auth_state") != "authenticated"
        or state.get("login_state") != "logged_in"
    ):
        raise CtpQuerySessionBindingError("ctp_query_session_live_state_mismatch")
    return trusted_environment, environment_profile, scope


def _scope_digests(
    environment: str | None,
    environment_profile: str | None,
    scope: _QuerySessionScope,
) -> tuple[str, str]:
    source_payload = {
        "version": 1,
        "environment": environment,
        "environment_profile": environment_profile,
        "issuer_identity": id(scope.issuer),
        "source_tags": list(scope.source_tags or ()),
    }
    source_material = json.dumps(
        source_payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("ascii")
    source_digest = hashlib.sha256(source_material).hexdigest()

    generation_payload = {
        "source_ref": source_digest,
        "connection_generation": scope.connection_generation,
        "trading_day": scope.trading_day,
    }
    generation_material = json.dumps(
        generation_payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("ascii")
    generation_digest = hashlib.sha256(generation_material).hexdigest()
    return (
        _SOURCE_REF_PREFIX + source_digest,
        _GENERATION_PREFIX + generation_digest,
    )


def capture_query_session(
    client: Any,
    *,
    environment: str | None,
) -> CtpQuerySessionBinding:
    """Bind the exact current, authenticated, read-only-ready CTP query scope.

    environment is the trusted account-realm label from SDK provider
    configuration, not the client's CTP environment-profile name. If it is
    unavailable, it remains None; consumers must keep dependent query families
    unavailable rather than derive a realm from a profile. This function
    performs no native write and exposes no credential or provider rows.
    """

    trusted_environment, environment_profile, scope = _read_current_scope(
        client,
        environment=environment,
    )
    source_ref, generation = _scope_digests(
        trusted_environment,
        environment_profile,
        scope,
    )
    return CtpQuerySessionBinding(
        source_ref=source_ref,
        generation=generation,
        environment=trusted_environment,
        environment_profile=environment_profile,
        account_fingerprint=scope.account_fingerprint,
        connection_generation=scope.connection_generation,
        trading_day=scope.trading_day,
        broker_id=scope.broker_id,
        investor_id=scope.investor_id,
        _issuer=scope.issuer,
        _source_tags=scope.source_tags,
        _captured_monotonic=scope.captured_monotonic,
        _seal=_BINDING_SEAL,
    )


def _bind_validated_query_scope(
    scope: Any,
    *,
    environment: str,
) -> CtpQuerySessionBinding:
    """Seal one query scope that its provider owner has already validated.

    Provider-internal (iteration 60): ``bt_api_ctp.account_provider`` calls
    it only after its own exact issuer, account and read-only checks of
    ``scope``, to give ``CtpExecutionQueryProjector`` the trading day and
    account realm of that scope. It reads no client and does not prove that
    the session is still current; the caller keeps re-validating the scope
    around every query.
    """

    if (
        type(scope) is not _QuerySessionScope
        or scope._seal is not _QUERY_SCOPE_SEAL
        or scope.read_only_ready is not True
        or type(environment) is not str
        or _ENVIRONMENT_RE.fullmatch(environment) is None
        or type(scope.captured_monotonic) is not float
    ):
        raise CtpQuerySessionBindingError("ctp_query_session_scope_unsealed")
    source_ref, generation = _scope_digests(environment, None, scope)
    return CtpQuerySessionBinding(
        source_ref=source_ref,
        generation=generation,
        environment=environment,
        environment_profile=None,
        account_fingerprint=scope.account_fingerprint,
        connection_generation=scope.connection_generation,
        trading_day=scope.trading_day,
        broker_id=scope.broker_id,
        investor_id=scope.investor_id,
        _issuer=scope.issuer,
        _source_tags=scope.source_tags,
        _captured_monotonic=scope.captured_monotonic,
        _seal=_BINDING_SEAL,
    )


def validate_query_session(
    client: Any,
    binding: CtpQuerySessionBinding,
    *,
    now_monotonic: float,
) -> bool:
    """Return whether the same current CTP read-only session still owns binding."""

    if (
        type(binding) is not CtpQuerySessionBinding
        or binding._seal is not _BINDING_SEAL
        or type(now_monotonic) is not float
        or not math.isfinite(now_monotonic)
        or now_monotonic < 0
        or now_monotonic < binding._captured_monotonic
        or now_monotonic > time.monotonic()
    ):
        return False
    try:
        current = capture_query_session(client, environment=binding.environment)
    except CtpQuerySessionBindingError:
        return False
    return bool(
        current._issuer is binding._issuer
        and current._source_tags == binding._source_tags
        and current.source_ref == binding.source_ref
        and current.generation == binding.generation
        and current.environment == binding.environment
        and current.environment_profile == binding.environment_profile
        and current.account_fingerprint == binding.account_fingerprint
        and current.connection_generation == binding.connection_generation
        and current.trading_day == binding.trading_day
        and current.broker_id == binding.broker_id
        and current.investor_id == binding.investor_id
    )


__all__ = [
    "CtpQuerySessionBinding",
    "CtpQuerySessionBindingError",
    "capture_query_session",
    "validate_query_session",
]
