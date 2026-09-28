"""Opt-in, offline verification and one-use claim boundary for CTP commands.

This module does not provide a production approval verifier and never submits
native requests.  A caller may inject a verifier that freshly reloads the
sealed configuration, current source evidence and per-action approval, then
durably consumes that approval exactly once.  Only after that callback returns
matching evidence does this module use the execution store's transactional
READY-to-CLAIMED operation.  The SQLite claim and external approval
consumption are separate stores and are deliberately not described as one
atomic transaction.  If verification succeeds but claiming fails, the
approval may be spent; the command is never dispatched here.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Protocol

from .managed_outbox_pre_dispatch import (
    CtpManagedNativeOutboxCandidateV1,
    CtpOutboxDispatchAuthorityRequired,
    CtpOutboxPreDispatchError,
    _copy_json_mapping,
    _scope_facts,
    _sha256_json,
    prepare_ctp_managed_native_outbox_candidate,
)

_DIGEST_RE = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)
_ACCOUNT_FINGERPRINT_RE = re.compile(r"[0-9a-f]{16}\Z", re.ASCII)
_TRADING_DAY_RE = re.compile(r"[0-9]{8}\Z", re.ASCII)
_CONSUMPTION_ID_RE = re.compile(r"[A-Za-z0-9._:-]{1,128}\Z", re.ASCII)


@dataclass(frozen=True, slots=True)
class CtpCurrentSessionFactsV1:
    """Non-secret CTP account and login-session facts collected under the SDK lock."""

    account_fingerprint_sha256: str
    trading_day: str
    connection_generation: int
    front_id: int
    session_id: int


@dataclass(frozen=True, slots=True)
class CtpFreshActionVerificationRequestV1:
    """Immutable facts a trusted, external verifier must freshly validate.

    ``sealed_scope`` is the current scope object supplied by the caller.  This
    package checks its durable key facts but cannot prove that the object was
    created by a sealed configuration loader.  The injected verifier must
    recollect that sealed configuration and bind it to ``candidate``.
    ``durable_command`` includes the stored approval and session/source
    bindings.  The verifier owns their schema and must compare them with fresh
    source and approval material; a digest in the outbox is only a commitment.
    """

    candidate: CtpManagedNativeOutboxCandidateV1
    sealed_scope: Any = field(repr=False, compare=False)
    durable_command: Mapping[str, Any] = field(repr=False)
    session_binding: Mapping[str, Any] = field(repr=False)
    native_fields: Mapping[str, Any] = field(repr=False)
    current_session: CtpCurrentSessionFactsV1
    action_binding_sha256: str


@dataclass(frozen=True, slots=True)
class CtpFreshActionUseReceiptV1:
    """Non-authorizing echo returned after an injected verifier consumes once."""

    action_binding_sha256: str
    source_binding_sha256: str
    approval_use_id: str
    approval_digest: str
    session_binding_sha256: str
    scope_digest_sha256: str
    account_fingerprint_sha256: str
    trading_day: str
    connection_generation: int
    front_id: int
    session_id: int
    consumption_id: str

    @property
    def authorizes_dispatch(self) -> bool:
        """A verification receipt is evidence only, not a native write capability."""

        return False


class CtpFreshActionVerifier(Protocol):
    """Trusted integration port; implementations must reload and consume fresh evidence.

    ``verify_and_consume`` must, during this call, reread the current sealed
    configuration and source set, revalidate the exact staged approval and its
    expiry/revocation policy, compare every request and target binding in
    ``request``, and durably consume that per-action approval once.  It must
    return a receipt for the exact request.  Implementations must not rely on
    cached approval results.  No implementation is supplied by this package.
    """

    def verify_and_consume(
        self, request: CtpFreshActionVerificationRequestV1
    ) -> CtpFreshActionUseReceiptV1: ...


@dataclass(frozen=True, slots=True)
class CtpVerifiedClaimedOutboxActionV1:
    """A one-use local command claim plus non-authorizing verification evidence."""

    candidate: CtpManagedNativeOutboxCandidateV1
    claimed_command: Any = field(repr=False, compare=False)
    use_receipt: CtpFreshActionUseReceiptV1 = field(repr=False)
    verification_receipt_sha256: str

    @property
    def authorizes_dispatch(self) -> bool:
        """Neither the claim nor external-verifier echo grants native dispatch."""

        return False

    def require_dispatch_authority(self) -> None:
        """Keep this offline boundary disconnected from native submit APIs."""

        raise CtpOutboxDispatchAuthorityRequired(
            "verified outbox claim is not native dispatch authority"
        )


def verify_and_claim_ctp_managed_native_outbox_action(
    store: Any,
    scope: Any,
    command_id: str,
    trader: Any,
    native_fields: Mapping[str, Any],
    *,
    writer_lease: object,
    verifier: CtpFreshActionVerifier | None,
) -> CtpVerifiedClaimedOutboxActionV1:
    """Freshly verify one staged action, then atomically consume its local claim.

    Verification is required and has no default implementation. A verifier
    failure, stale pre-claim scope/session, or mismatched receipt stops before
    the SQLite claim. A session change detected after claiming fails closed and
    leaves the durable row CLAIMED, so it cannot be replayed. The store claim is
    the existing account-fenced, conditional READY-to-CLAIMED transaction; it
    is rechecked against every immutable command field before this function
    returns. CLAIMED and UNKNOWN rows are never retried. No native call is made
    here.
    """

    if verifier is None or not callable(getattr(verifier, "verify_and_consume", None)):
        raise CtpOutboxDispatchAuthorityRequired(
            "a fresh per-action verifier is required before claiming"
        )

    scope_facts = _scope_facts(scope)
    fields = _copy_json_mapping(native_fields, "native_fields")
    candidate = prepare_ctp_managed_native_outbox_candidate(
        store,
        scope,
        command_id,
        trader,
        fields,
        writer_lease=writer_lease,
    )

    command_reader = getattr(store, "read_ctp_dispatch_command", None)
    claim_command = getattr(store, "claim_ctp_dispatch_command", None)
    lease_assertion = getattr(store, "assert_writer_lease", None)
    if not callable(command_reader) or not callable(claim_command) or not callable(lease_assertion):
        raise TypeError("store must expose CTP command read, atomic claim, and lease APIs")

    command = command_reader(scope, command_id)
    if command is None:
        raise CtpOutboxPreDispatchError("unknown CTP command")
    durable_command = _durable_command_snapshot(command)
    if durable_command["status"] != "READY":
        raise CtpOutboxPreDispatchError("only a READY staged command is eligible")
    if any(
        getattr(command, name, None) is not None
        for name in (
            "claimed_at_ns",
            "claimed_owner_id",
            "claimed_fencing_token",
            "completed_at_ns",
            "unknown_at_ns",
            "unknown_reason",
            "native_receipt_payload",
            "native_receipt_sha256",
            "completion_echo_sha256",
        )
    ):
        raise CtpOutboxPreDispatchError("READY command carries prior claim or receipt state")
    _assert_candidate_matches_command(candidate, durable_command, fields)

    current_session = _current_session_facts(trader)
    _assert_session_matches_candidate(current_session, candidate)
    request = CtpFreshActionVerificationRequestV1(
        candidate=candidate,
        sealed_scope=scope,
        durable_command=_deep_freeze(durable_command),
        session_binding=_deep_freeze(durable_command["session_binding"]),
        native_fields=_deep_freeze(fields),
        current_session=current_session,
        action_binding_sha256=_action_binding_sha256(
            candidate,
            durable_command,
            fields,
            current_session,
            scope_facts,
        ),
    )

    verification_failed = False
    try:
        use_receipt = verifier.verify_and_consume(request)
    except Exception:
        # Drop the exception completely instead of chaining it: even a
        # suppressed ``__context__`` can be inspected by telemetry hooks.
        verification_failed = True
    if verification_failed:
        # Raise after leaving the except suite so the original exception is
        # not retained as the new exception's context.
        raise CtpOutboxDispatchAuthorityRequired("fresh action verification failed")
    _validate_use_receipt(use_receipt, request)

    # Re-read all local facts after external verification.  The command's
    # immutable columns cannot change, while this catches a competing claim;
    # the transactional claim below remains the atomic one-use gate.
    if _scope_facts(scope) != scope_facts:
        raise CtpOutboxPreDispatchError("sealed scope facts changed during verification")
    try:
        verified_session = _current_session_facts(trader)
    except CtpOutboxPreDispatchError as exc:
        raise CtpOutboxPreDispatchError("SDK account/session changed during verification") from exc
    if verified_session != current_session:
        raise CtpOutboxPreDispatchError("SDK account/session changed during verification")
    _assert_session_matches_candidate(verified_session, candidate)
    latest = command_reader(scope, command_id)
    if latest is None:
        raise CtpOutboxPreDispatchError("command disappeared during verification")
    latest_snapshot = _durable_command_snapshot(latest)
    if latest_snapshot != durable_command:
        raise CtpOutboxPreDispatchError("durable command changed during verification")
    lease_assertion(scope, writer_lease)

    claimed = claim_command(scope, command_id, writer_lease=writer_lease)
    if claimed is None:
        raise CtpOutboxPreDispatchError("command was not atomically claimed")
    _assert_claim_echo(claimed, durable_command, writer_lease)
    lease_assertion(scope, writer_lease)
    # The SDK lock cannot be held across the external verifier or SQLite
    # transaction. Recheck after the claim so a session change in the final
    # check-to-claim window leaves a durable CLAIMED row and cannot be retried.
    try:
        claimed_session = _current_session_facts(trader)
    except CtpOutboxPreDispatchError as exc:
        raise CtpOutboxPreDispatchError("SDK account/session changed after atomic claim") from exc
    if claimed_session != current_session:
        raise CtpOutboxPreDispatchError("SDK account/session changed after atomic claim")

    receipt_digest = _sha256_json(
        {
            "schema": "bt_api_ctp.fresh_action_use_receipt.v1",
            "action_binding_sha256": use_receipt.action_binding_sha256,
            "source_binding_sha256": use_receipt.source_binding_sha256,
            "approval_use_id": use_receipt.approval_use_id,
            "approval_digest": use_receipt.approval_digest,
            "session_binding_sha256": use_receipt.session_binding_sha256,
            "scope_digest_sha256": use_receipt.scope_digest_sha256,
            "account_fingerprint_sha256": use_receipt.account_fingerprint_sha256,
            "trading_day": use_receipt.trading_day,
            "connection_generation": use_receipt.connection_generation,
            "front_id": use_receipt.front_id,
            "session_id": use_receipt.session_id,
            "consumption_id": use_receipt.consumption_id,
        }
    )
    return CtpVerifiedClaimedOutboxActionV1(
        candidate=candidate,
        claimed_command=claimed,
        use_receipt=use_receipt,
        verification_receipt_sha256=receipt_digest,
    )


def _durable_command_snapshot(command: Any) -> dict[str, Any]:
    """Copy the complete immutable v5 command binding and current status."""

    names = (
        "account_key",
        "scope_key",
        "trading_day",
        "operation",
        "command_id",
        "request_payload_sha256",
        "reservation_managed_intent_id",
        "order_ref",
        "cancel_target_order_ref",
        "cancel_target_exchange_id",
        "cancel_target_order_sys_id",
        "cancel_target_front_id",
        "cancel_target_session_id",
        "approval_use_id",
        "approval_digest",
        "session_binding_sha256",
        "status",
    )
    snapshot = {name: getattr(command, name, None) for name in names}
    snapshot["request_payload"] = _copy_json_mapping(
        getattr(command, "request_payload", None), "request_payload"
    )
    snapshot["session_binding"] = _copy_json_mapping(
        getattr(command, "session_binding", None), "session_binding"
    )
    if (
        _sha256_json(snapshot["request_payload"]) != snapshot["request_payload_sha256"]
        or _sha256_json(snapshot["session_binding"]) != snapshot["session_binding_sha256"]
    ):
        raise CtpOutboxPreDispatchError("durable command binding digest mismatch")
    return snapshot


def _assert_candidate_matches_command(
    candidate: CtpManagedNativeOutboxCandidateV1,
    command: Mapping[str, Any],
    native_fields: Mapping[str, Any],
) -> None:
    expected = {
        "account_key": candidate.account_key,
        "scope_key": candidate.scope_key,
        "trading_day": candidate.trading_day,
        "operation": candidate.operation,
        "command_id": candidate.command_id,
        "request_payload_sha256": candidate.request_payload_sha256,
        "reservation_managed_intent_id": candidate.reservation_managed_intent_id,
        "approval_use_id": candidate.approval_use_id,
        "approval_digest": candidate.approval_digest,
        "session_binding_sha256": candidate.session_binding_sha256,
        "request_payload": dict(native_fields),
    }
    if any(command.get(name) != value for name, value in expected.items()):
        raise CtpOutboxPreDispatchError("candidate does not exactly match its durable command")
    request = candidate.request
    if request.native_fields_sha256 != _sha256_json(native_fields):
        raise CtpOutboxPreDispatchError("candidate native payload digest mismatch")
    if candidate.operation == "SUBMIT" and (
        command["order_ref"] != request.order_ref
        or any(
            command[name] is not None
            for name in (
                "cancel_target_order_ref",
                "cancel_target_exchange_id",
                "cancel_target_order_sys_id",
                "cancel_target_front_id",
                "cancel_target_session_id",
            )
        )
    ):
        raise CtpOutboxPreDispatchError("submit command binding changed before claim")
    if candidate.operation == "CANCEL" and (
        command["order_ref"] is not None
        or command["cancel_target_order_ref"] != request.order_ref
        or command["cancel_target_exchange_id"] != request.target_exchange_id
        or command["cancel_target_order_sys_id"] != request.target_order_sys_id
        or command["cancel_target_front_id"] != request.target_front_id
        or command["cancel_target_session_id"] != request.target_session_id
    ):
        raise CtpOutboxPreDispatchError("cancel target changed before claim")


def _current_session_facts(trader: Any) -> CtpCurrentSessionFactsV1:
    lock = getattr(trader, "_query_state_lock", None)
    if lock is None or not callable(getattr(lock, "__enter__", None)):
        raise CtpOutboxPreDispatchError("trader does not expose the SDK state lock")
    with lock:
        fingerprint = getattr(trader, "_account_fingerprint", None)
        trading_day = getattr(trader, "_trading_day", None)
        generation = getattr(trader, "_connection_generation", None)
        login_generation = getattr(trader, "_login_connection_generation", None)
        front_id = getattr(trader, "_front_id", None)
        session_id = getattr(trader, "_session_id", None)
    if type(fingerprint) is not str or _ACCOUNT_FINGERPRINT_RE.fullmatch(fingerprint) is None:
        raise CtpOutboxPreDispatchError("SDK account identity is unavailable")
    if type(trading_day) is not str or _TRADING_DAY_RE.fullmatch(trading_day) is None:
        raise CtpOutboxPreDispatchError("SDK trading day is unavailable")
    if type(generation) is not int or generation <= 0 or login_generation != generation:
        raise CtpOutboxPreDispatchError("SDK login session generation is unavailable")
    if type(front_id) is not int or front_id <= 0:
        raise CtpOutboxPreDispatchError("SDK front identity is unavailable")
    if type(session_id) is not int or session_id <= 0:
        raise CtpOutboxPreDispatchError("SDK session identity is unavailable")
    import hashlib

    return CtpCurrentSessionFactsV1(
        account_fingerprint_sha256=hashlib.sha256(
            f"acct_{fingerprint}".encode("ascii")
        ).hexdigest(),
        trading_day=trading_day,
        connection_generation=generation,
        front_id=front_id,
        session_id=session_id,
    )


def _assert_session_matches_candidate(
    session: CtpCurrentSessionFactsV1,
    candidate: CtpManagedNativeOutboxCandidateV1,
) -> None:
    request = candidate.request
    if (
        session.account_fingerprint_sha256 != request.account_fingerprint_sha256
        or session.trading_day != request.trading_day
        or session.connection_generation != request.connection_generation
    ):
        raise CtpOutboxPreDispatchError("SDK account/session changed during verification")


def _action_binding_sha256(
    candidate: CtpManagedNativeOutboxCandidateV1,
    durable_command: Mapping[str, Any],
    native_fields: Mapping[str, Any],
    session: CtpCurrentSessionFactsV1,
    scope_facts: tuple[str, str, str, str],
) -> str:
    account_key, scope_key, trading_day, scope_digest = scope_facts
    return _sha256_json(
        {
            "schema": "bt_api_ctp.fresh_action_verification_request.v1",
            "account_key": account_key,
            "scope_key": scope_key,
            "scope_digest_sha256": scope_digest,
            "trading_day": trading_day,
            "operation": candidate.operation,
            "command_id": candidate.command_id,
            "request_payload_sha256": candidate.request_payload_sha256,
            "native_fields_sha256": _sha256_json(native_fields),
            "parent_handoff_digest_sha256": candidate.parent_handoff_digest_sha256,
            "native_request_digest_sha256": candidate.request.request_digest_sha256,
            "durable_command_sha256": _sha256_json(durable_command),
            "approval_use_id": candidate.approval_use_id,
            "approval_digest": candidate.approval_digest,
            "session_binding_sha256": candidate.session_binding_sha256,
            "current_session": {
                "account_fingerprint_sha256": session.account_fingerprint_sha256,
                "trading_day": session.trading_day,
                "connection_generation": session.connection_generation,
                "front_id": session.front_id,
                "session_id": session.session_id,
            },
        }
    )


def _validate_use_receipt(receipt: Any, request: CtpFreshActionVerificationRequestV1) -> None:
    if type(receipt) is not CtpFreshActionUseReceiptV1:
        raise CtpOutboxDispatchAuthorityRequired("fresh verifier returned an invalid receipt")
    session = request.current_session
    expected = {
        "action_binding_sha256": request.action_binding_sha256,
        "approval_use_id": request.candidate.approval_use_id,
        "approval_digest": request.candidate.approval_digest,
        "session_binding_sha256": request.candidate.session_binding_sha256,
        "scope_digest_sha256": request.candidate.request.scope_digest_sha256,
        "account_fingerprint_sha256": session.account_fingerprint_sha256,
        "trading_day": session.trading_day,
        "connection_generation": session.connection_generation,
        "front_id": session.front_id,
        "session_id": session.session_id,
    }
    if any(getattr(receipt, name, None) != value for name, value in expected.items()):
        raise CtpOutboxDispatchAuthorityRequired("fresh verifier receipt binding mismatch")
    if (
        type(receipt.source_binding_sha256) is not str
        or _DIGEST_RE.fullmatch(receipt.source_binding_sha256) is None
        or type(receipt.consumption_id) is not str
        or _CONSUMPTION_ID_RE.fullmatch(receipt.consumption_id) is None
    ):
        raise CtpOutboxDispatchAuthorityRequired("fresh verifier receipt is incomplete")


def _assert_claim_echo(
    claimed: Any,
    ready_snapshot: Mapping[str, Any],
    writer_lease: object,
) -> None:
    claimed_snapshot = _durable_command_snapshot(claimed)
    expected_snapshot = dict(ready_snapshot)
    expected_snapshot["status"] = "CLAIMED"
    if claimed_snapshot != expected_snapshot:
        raise CtpOutboxPreDispatchError("atomic claim did not echo the exact durable command")
    owner_id = getattr(writer_lease, "owner_id", None)
    fencing_token = getattr(writer_lease, "fencing_token", None)
    if (
        type(owner_id) is not str
        or getattr(claimed, "claimed_owner_id", None) != owner_id
        or type(fencing_token) is not int
        or getattr(claimed, "claimed_fencing_token", None) != fencing_token
        or type(getattr(claimed, "claimed_at_ns", None)) is not int
        or claimed.claimed_at_ns <= 0
        or any(
            getattr(claimed, name, None) is not None
            for name in (
                "completed_at_ns",
                "unknown_at_ns",
                "unknown_reason",
                "native_receipt_payload",
                "native_receipt_sha256",
                "completion_echo_sha256",
            )
        )
    ):
        raise CtpOutboxPreDispatchError("atomic claim result is incomplete or mismatched")


def _deep_freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({key: _deep_freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_deep_freeze(item) for item in value)
    return value


__all__ = [
    "CtpCurrentSessionFactsV1",
    "CtpFreshActionUseReceiptV1",
    "CtpFreshActionVerificationRequestV1",
    "CtpFreshActionVerifier",
    "CtpVerifiedClaimedOutboxActionV1",
    "verify_and_claim_ctp_managed_native_outbox_action",
]
