"""CTP implementation of the neutral account-session and preflight ports.

This module owns the CTP session readback and query binding behind the SDK
boundary. Backtrader receives only ``bt_api_base`` DTOs, normalized query
receipts, freshness leases, and finite reason codes. No framework imports or
native CTP rows leave this module.

Settlement confirmation writes are intentionally not implemented by this
read port. They require the separate SDK-issued ``SettlementConfirmationPort``
and its one-shot authorization/claim flow.
"""

from __future__ import annotations

import hashlib
import threading
import time
import uuid
from collections.abc import Mapping
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from math import isfinite
from typing import Any

from bt_api_base.account_session_protocol import (
    AccountPreflightRequest,
    AccountPreflightSnapshot,
    AccountSessionPort,
    AccountSessionSnapshot,
    SettlementConfirmationPort,
    SettlementConfirmationReceipt,
    SettlementConfirmationRequest,
    SettlementVerificationReceipt,
)
from bt_api_base.account_session_registry_protocol import (
    AccountSessionProviderRegistry,
    SelectedAccountSessionRoute,
)
from bt_api_base.query_protocol import (
    QueryBatchReceipt,
    QueryRequest,
)
from bt_api_ctp.ctp.client import (
    CTP_REQUEST_COUNT_KEYS,
    TraderClient,
    _CTP_EXECUTION_AUTHORIZATION_SEAL,
    _CtpSettlementAuthorization,
    _is_ctp_core_execution_authority,
)
from bt_api_ctp.query import (
    _QUERY_EVIDENCE_MAX_TTL_SECONDS,
    _QUERY_MONOTONIC_CLOCK_DOMAIN,
    _QUERY_SOURCE_SEAL,
    _QuerySource,
    QueryResult,
    _query_records_digest,
)
from bt_api_ctp.query_service import CtpProviderQueryService
from bt_api_ctp.registered_sim_session import (
    CtpQuerySessionBinding,
    capture_query_session,
    validate_query_session,
)

_UTC = timezone.utc
_READBACK_WRITE_COUNTERS = ("settlement_confirm", "order_insert", "order_action")
_WRITE_COUNTERS = frozenset(_READBACK_WRITE_COUNTERS)
_MAX_PREFLIGHT_LEASE_SECONDS = 5.0
_CTP_FUTURE_ROUTE = "CTP___FUTURE"


class CtpStoreAccountSessionError(RuntimeError):
    """Finite, value-free construction or request error for the CTP account port."""

    _CODES = frozenset(
        {
            "account_session_client_not_canonical",
            "account_session_exchange_invalid",
            "account_session_environment_invalid",
            "account_session_binding_unavailable",
            "settlement_authorization_invalid",
            "settlement_claim_invalid",
            "settlement_claim_capacity_exceeded",
        }
    )

    def __init__(self, code: str) -> None:
        self.code = code if type(code) is str and code in self._CODES else "account_session_binding_unavailable"
        super().__init__(self.code)


class CtpStoreAccountSessionService(AccountSessionPort):
    """Read-only CTP account-session capability for a selected SDK route.

    Construction does not connect, authenticate, confirm settlement, or
    submit an order. The exact ``TraderClient`` and trusted environment come
    from SDK composition. A query service is created only after the same live
    client supplies a sealed current read-only session binding.
    """

    def __init__(
        self,
        client: TraderClient,
        *,
        exchange_name: str,
        environment: str | None,
    ) -> None:
        if type(client) is not TraderClient:
            raise CtpStoreAccountSessionError("account_session_client_not_canonical")
        if (
            type(exchange_name) is not str
            or not exchange_name
            or exchange_name != exchange_name.strip()
            or exchange_name.partition("___")[0].upper() != "CTP"
        ):
            raise CtpStoreAccountSessionError("account_session_exchange_invalid")
        if environment is not None and (
            type(environment) is not str
            or not environment
            or environment != environment.strip()
        ):
            raise CtpStoreAccountSessionError("account_session_environment_invalid")
        self._client = client
        self._exchange_name = exchange_name
        self._environment = environment
        self._lock = threading.RLock()
        self._query_binding: CtpQuerySessionBinding | None = None
        self._query_service: CtpProviderQueryService | None = None

    def session_snapshot(self) -> AccountSessionSnapshot:
        """Read sanitized current state without starting a query or write."""

        state = self._read_session_state()
        if state is None:
            return AccountSessionSnapshot(
                exchange_name=self._exchange_name,
                source_ref=None,
                generation=None,
                session_ref=None,
                connected=None,
                authenticated=None,
                trading_enabled=None,
                business_day=None,
                settlement_status="unavailable",
                observed_at=None,
                available=False,
                blockers=("session_state_unavailable",),
            )

        binding = self._capture_binding()
        connected = _optional_bool(state.get("connected"))
        authenticated = _authenticated_state(state.get("auth_state"))
        trading_enabled = _optional_bool(state.get("trading_ready"))
        business_day = _optional_day(state.get("trading_day"))
        settlement_status = _settlement_state(state)
        blockers: list[str] = []
        if binding is None:
            blockers.append("current_read_only_session_unavailable")
        if connected is not True:
            blockers.append("session_not_connected")
        if authenticated is not True:
            blockers.append("session_not_authenticated")
        if not business_day:
            blockers.append("business_day_unavailable")

        session_ref = None
        source_ref = None
        generation = None
        if binding is not None:
            source_ref = binding.source_ref
            generation = binding.generation
            session_ref = _opaque_session_ref(binding.source_ref, binding.generation)
        return AccountSessionSnapshot(
            exchange_name=self._exchange_name,
            source_ref=source_ref,
            generation=generation,
            session_ref=session_ref,
            connected=connected,
            authenticated=authenticated,
            trading_enabled=trading_enabled,
            business_day=business_day,
            settlement_status=settlement_status,
            observed_at=datetime.now(_UTC),
            available=True,
            blockers=tuple(dict.fromkeys(blockers)),
        )

    def preflight(self, request: AccountPreflightRequest) -> AccountPreflightSnapshot:
        """Read a bounded neutral query batch and return its freshness lease.

        The published neutral query schema does not currently support complete
        execution/trade snapshots. The method therefore preserves available
        account, position, open-order, and instrument receipts but never
        reports the preflight ready until the shared execution-query contract
        can represent the required same-session trade reconciliation. Cancel
        requests have no neutral target-order identity and return unavailable
        before dispatching any query.
        """

        if type(request) is not AccountPreflightRequest:
            raise TypeError("request must be an AccountPreflightRequest")
        if request.exchange_name != self._exchange_name:
            return _unavailable_preflight(request, "account_route_mismatch")
        now_utc = datetime.now(_UTC)
        if request.deadline <= now_utc:
            return _unavailable_preflight(request, "preflight_deadline_elapsed")
        if request.operation == "cancel":
            return _unavailable_preflight(
                request,
                "cancel_target_identity_unavailable",
            )

        before_state = self._read_session_state()
        binding = self._capture_binding()
        if before_state is None or binding is None:
            return _unavailable_preflight(request, "query_session_unavailable")
        query_service = self._query_service_for(binding)
        if query_service is None:
            return _unavailable_preflight(request, "query_service_unavailable")

        requests = _preflight_queries(request)
        try:
            receipt = query_service.query_batch(requests, deadline=request.deadline)
        except Exception:
            # A raised provider query can have an uncertain dispatch outcome.
            # Do not retry or project an exception string into BT.
            return _unavailable_preflight(request, "query_outcome_unknown")
        if (
            type(receipt) is not QueryBatchReceipt
            or receipt.requests != requests
            or receipt.source_ref != binding.source_ref
            or receipt.generation != binding.generation
        ):
            return _unavailable_preflight(request, "query_receipt_untrusted")
        if (
            receipt.started_at > request.deadline
            or receipt.completed_at > request.deadline
            or any(
                timestamp is not None and timestamp > request.deadline
                for snapshot in receipt.snapshots
                for timestamp in (
                    snapshot.started_at,
                    snapshot.completed_at,
                    snapshot.observed_at,
                )
            )
        ):
            return _unavailable_preflight(request, "preflight_deadline_exceeded")

        after_state = self._read_session_state()
        current_binding = self._capture_binding()
        blockers = list(receipt.blockers)
        if not receipt.complete:
            blockers.extend(_receipt_blockers(receipt))
        same_session = bool(
            current_binding is not None
            and current_binding.source_ref == binding.source_ref
            and current_binding.generation == binding.generation
            and receipt.source_ref == binding.source_ref
            and receipt.generation == binding.generation
        )
        if not same_session:
            blockers.append("query_session_changed")

        write_request_free = _write_request_free(before_state, after_state)
        if write_request_free is not True:
            blockers.append("write_request_evidence_unavailable_or_changed")
        if (
            before_state.get("auto_settlement_confirm") is not False
            or after_state is None
            or after_state.get("auto_settlement_confirm") is not False
        ):
            blockers.append("automatic_settlement_confirmation_not_disabled")

        lease = None
        if receipt.complete and same_session:
            lease_now = datetime.now(_UTC)
            try:
                expiry = min(
                    lease_now + timedelta(seconds=_MAX_PREFLIGHT_LEASE_SECONDS),
                    _query_evidence_expiry(receipt),
                )
                lease = query_service.acquire_snapshot_lease(
                    receipt,
                    purpose="account_preflight",
                    expires_at=expiry,
                )
            except (TypeError, ValueError):
                blockers.append("query_lease_unavailable")
        if lease is not None and lease.issued_at > request.deadline:
            return _unavailable_preflight(request, "preflight_deadline_exceeded")

        # The account query schemas currently have no complete neutral
        # execution/trade family. A Store must not treat a batch omitting that
        # required safety evidence as a complete CTP order preflight.
        blockers.append("execution_query_schema_unavailable")
        unique_blockers = tuple(dict.fromkeys(blockers))
        observed_at = datetime.now(_UTC)
        if observed_at > request.deadline:
            return _unavailable_preflight(request, "preflight_deadline_exceeded")
        if lease is not None and not (lease.issued_at <= observed_at < lease.expires_at):
            lease = None
            unique_blockers = tuple(
                dict.fromkeys((*unique_blockers, "query_lease_unavailable"))
            )
        return AccountPreflightSnapshot(
            request=request,
            source_ref=receipt.source_ref,
            generation=receipt.generation,
            observed_at=observed_at,
            ready=None,
            available=True,
            blockers=unique_blockers,
            query_receipt=receipt,
            query_lease=lease,
        )

    def verify_settlement(self, *, deadline: datetime) -> SettlementVerificationReceipt:
        """Query server settlement readback and prove no write counter changed."""

        _require_utc(deadline, "deadline")
        before_state = self._read_session_state()
        binding = self._capture_binding()
        observed_before = datetime.now(_UTC)
        if before_state is None or binding is None:
            return _settlement_unavailable(
                self._exchange_name,
                "settlement_session_unavailable",
                observed_at=observed_before,
            )
        remaining = (deadline - observed_before).total_seconds()
        if not isfinite(remaining) or remaining <= 0:
            return _settlement_unavailable(
                self._exchange_name,
                "settlement_deadline_elapsed",
                binding=binding,
                observed_at=observed_before,
            )
        method = getattr(self._client, "verify_settlement_confirmation", None)
        canonical_method = getattr(TraderClient, "verify_settlement_confirmation", None)
        if (
            not callable(method)
            or not callable(canonical_method)
            or getattr(method, "__self__", None) is not self._client
            or getattr(method, "__func__", None) is not canonical_method
        ):
            return _settlement_unavailable(
                self._exchange_name,
                "settlement_readback_capability_unavailable",
                binding=binding,
                observed_at=datetime.now(_UTC),
            )
        try:
            result = method(timeout=remaining)
        except Exception:
            return _settlement_unavailable(
                self._exchange_name,
                "settlement_readback_outcome_unknown",
                binding=binding,
                observed_at=datetime.now(_UTC),
            )
        after_state = self._read_session_state()
        current_binding = self._capture_binding()
        observed_at = datetime.now(_UTC)
        write_request_free = _write_request_free(before_state, after_state)
        automatic_confirmation_disabled = bool(
            before_state.get("auto_settlement_confirm") is False
            and after_state is not None
            and after_state.get("auto_settlement_confirm") is False
        )
        source_valid = _valid_settlement_result(result, binding)
        session_current = bool(
            current_binding is not None
            and current_binding.source_ref == binding.source_ref
            and current_binding.generation == binding.generation
            and validate_query_session(
                self._client,
                binding,
                now_monotonic=time.monotonic(),
            )
        )
        readback_verified = bool(
            after_state is not None
            and after_state.get("settlement_state") == "confirmed"
            and after_state.get("settlement_readback_verified") is True
            and after_state.get("trading_ready") is True
            and after_state.get("connection_generation") == binding.connection_generation
            and after_state.get("trading_day") == binding.trading_day
        )
        settlement_required = bool(
            source_valid
            and session_current
            and write_request_free is True
            and automatic_confirmation_disabled
            and after_state is not None
            and _negative_settlement_readback(result, after_state, binding)
        )
        complete = bool(
            source_valid
            and session_current
            and write_request_free is True
            and automatic_confirmation_disabled
            and (readback_verified or settlement_required)
            and observed_at <= deadline
        )
        reason_code = "settlement_confirmation_required" if settlement_required else None
        if not complete:
            reason_code = _settlement_failure_code(
                source_valid=source_valid,
                session_current=session_current,
                write_request_free=write_request_free,
                automatic_confirmation_disabled=automatic_confirmation_disabled,
                readback_verified=readback_verified,
                deadline=deadline,
                observed_at=observed_at,
            )
        return SettlementVerificationReceipt(
            exchange_name=self._exchange_name,
            source_ref=binding.source_ref,
            generation=binding.generation,
            business_day=binding.trading_day,
            status=(
                "confirmed"
                if readback_verified and complete
                else "required"
                if settlement_required and complete
                else "unknown"
            ),
            observed_at=observed_at,
            complete=complete,
            read_only_safe=write_request_free is True and automatic_confirmation_disabled,
            receipt_ref=("ctp-settlement-readback:" + uuid.uuid4().hex) if complete else None,
            reason_code=reason_code,
        )

    def validate_preflight(
        self,
        snapshot: AccountPreflightSnapshot,
        *,
        now: datetime,
    ) -> bool:
        """Revalidate the exact query lease and current CTP session binding."""

        if type(snapshot) is not AccountPreflightSnapshot or snapshot.ready is not True:
            return False
        _require_utc(now, "now")
        if snapshot.request.exchange_name != self._exchange_name:
            return False
        receipt = snapshot.query_receipt
        lease = snapshot.query_lease
        if receipt is None or lease is None:
            return False
        with self._lock:
            binding = self._capture_binding()
            service = self._query_service
            cached_binding = self._query_binding
            if (
                binding is None
                or service is None
                or cached_binding is None
                or binding.source_ref != snapshot.source_ref
                or binding.generation != snapshot.generation
                or cached_binding.source_ref != binding.source_ref
                or cached_binding.generation != binding.generation
                or receipt.source_ref != binding.source_ref
                or receipt.generation != binding.generation
            ):
                return False
            try:
                return service.validate_lease(lease, receipt, now=now)
            except Exception:
                return False

    def _read_session_state(self) -> Mapping[str, Any] | None:
        method = getattr(self._client, "get_session_state", None)
        canonical_method = getattr(TraderClient, "get_session_state", None)
        if (
            not callable(method)
            or not callable(canonical_method)
            or getattr(method, "__self__", None) is not self._client
            or getattr(method, "__func__", None) is not canonical_method
        ):
            return None
        try:
            state = method()
            return dict(state) if isinstance(state, Mapping) else None
        except Exception:
            return None

    def _capture_binding(self) -> CtpQuerySessionBinding | None:
        try:
            return capture_query_session(self._client, environment=self._environment)
        except Exception:
            return None

    def _query_service_for(
        self,
        binding: CtpQuerySessionBinding,
    ) -> CtpProviderQueryService | None:
        with self._lock:
            cached = self._query_binding
            if (
                cached is not None
                and self._query_service is not None
                and cached.source_ref == binding.source_ref
                and cached.generation == binding.generation
                and cached._issuer is binding._issuer
                and cached._source_tags == binding._source_tags
            ):
                return self._query_service
            try:
                service = CtpProviderQueryService(self._client, binding)
            except Exception:
                self._query_binding = None
                self._query_service = None
                return None
            self._query_binding = binding
            self._query_service = service
            return service


_SETTLEMENT_CLAIM_SEAL = object()
_MAX_SETTLEMENT_CLAIMS = 128
_MAX_SETTLEMENT_RECEIPTS = 256


@dataclass(frozen=True)
class _CtpSettlementClaim:
    """Private one-shot claim bound to one exact CTP auth object and session."""

    claim_ref: str
    request: SettlementConfirmationRequest
    authorization: object = field(repr=False, compare=False)
    binding: CtpQuerySessionBinding = field(repr=False, compare=False)
    _seal: object = field(repr=False, compare=False)


class CtpSettlementConfirmationService(SettlementConfirmationPort):
    """CTP native settlement confirmation behind a distinct authorized port.

    The constructor requires the exact core execution authority already
    installed on the ``TraderClient`` and a verified trusted environment
    profile. ``claim_settlement_confirmation`` accepts only the exact sealed
    one-shot native settlement authorization issued for that client; this
    class never issues an authorization or derives one from read state.
    """

    def __init__(
        self,
        client: TraderClient,
        *,
        exchange_name: str,
        environment: str | None,
        environment_profile: str,
        environment_verified: bool,
        execution_capability: object,
    ) -> None:
        if type(client) is not TraderClient:
            raise CtpStoreAccountSessionError("account_session_client_not_canonical")
        if (
            type(exchange_name) is not str
            or not exchange_name
            or exchange_name != exchange_name.strip()
            or exchange_name.partition("___")[0].upper() != "CTP"
        ):
            raise CtpStoreAccountSessionError("account_session_exchange_invalid")
        if (
            type(environment_profile) is not str
            or not environment_profile
            or environment_profile != environment_profile.strip()
            or type(environment_verified) is not bool
            or environment_verified is not True
        ):
            raise CtpStoreAccountSessionError("settlement_authorization_invalid")
        if (
            not _is_ctp_core_execution_authority(execution_capability)
            or getattr(client, "_execution_gate_capability", None) is not execution_capability
        ):
            raise CtpStoreAccountSessionError("settlement_authorization_invalid")
        self._client = client
        self._exchange_name = exchange_name
        self._environment = environment
        self._environment_profile = environment_profile
        self._execution_capability = execution_capability
        self._session_reader = CtpStoreAccountSessionService(
            client,
            exchange_name=exchange_name,
            environment=environment,
        )
        self._lock = threading.RLock()
        self._claims: OrderedDict[str, _CtpSettlementClaim] = OrderedDict()
        self._claimed_authorizations: dict[int, object] = {}
        self._receipts: OrderedDict[str, tuple[SettlementConfirmationReceipt, CtpQuerySessionBinding]] = (
            OrderedDict()
        )

    def claim_settlement_confirmation(
        self,
        request: SettlementConfirmationRequest,
        *,
        authorization: object,
    ) -> object:
        """Bind one existing CTP one-shot grant to the exact current request."""

        if type(request) is not SettlementConfirmationRequest:
            raise TypeError("request must be a SettlementConfirmationRequest")
        if request.exchange_name != self._exchange_name:
            raise CtpStoreAccountSessionError("settlement_authorization_invalid")
        if datetime.now(_UTC) >= request.deadline:
            raise CtpStoreAccountSessionError("settlement_authorization_invalid")
        binding = self._session_reader._capture_binding()
        if (
            binding is None
            or request.expected_generation != binding.generation
            or request.business_day != binding.trading_day
        ):
            raise CtpStoreAccountSessionError("settlement_authorization_invalid")
        if (
            type(authorization) is not _CtpSettlementAuthorization
            or authorization._seal is not _CTP_EXECUTION_AUTHORIZATION_SEAL
            or authorization._client_ref() is not self._client
            or authorization._capability is not self._execution_capability
            or authorization._used is not False
            or authorization._scope != "settlement_confirmation"
            or authorization._budget != 1
            or authorization._connection_generation != binding.connection_generation
            or authorization._account_fingerprint != f"acct_{binding.account_fingerprint}"
            or authorization._trading_day != binding.trading_day
            or authorization._environment_profile != self._environment_profile
        ):
            raise CtpStoreAccountSessionError("settlement_authorization_invalid")
        with self._lock:
            existing = self._claimed_authorizations.get(id(authorization))
            if existing is authorization:
                raise CtpStoreAccountSessionError("settlement_authorization_invalid")
            self._prune_claims(datetime.now(_UTC))
            if (
                len(self._claims) >= _MAX_SETTLEMENT_CLAIMS
                or len(self._claimed_authorizations) >= _MAX_SETTLEMENT_RECEIPTS
            ):
                raise CtpStoreAccountSessionError("settlement_claim_capacity_exceeded")
            with self._client._query_state_lock:
                try:
                    self._client._require_settlement_write_locked(
                        self._execution_capability,
                        authorization,
                        self._environment_profile,
                        True,
                    )
                except Exception:
                    raise CtpStoreAccountSessionError("settlement_authorization_invalid") from None
            claim = _CtpSettlementClaim(
                claim_ref="ctp-settlement-claim:" + uuid.uuid4().hex,
                request=request,
                authorization=authorization,
                binding=binding,
                _seal=_SETTLEMENT_CLAIM_SEAL,
            )
            self._claims[claim.claim_ref] = claim
            self._claimed_authorizations[id(authorization)] = authorization
            return claim

    def confirm_settlement(
        self,
        request: SettlementConfirmationRequest,
        *,
        claim: object,
        authorization: object,
    ) -> SettlementConfirmationReceipt:
        """Consume one matching claim and make at most one native request."""

        if (
            type(request) is not SettlementConfirmationRequest
            or type(claim) is not _CtpSettlementClaim
            or claim._seal is not _SETTLEMENT_CLAIM_SEAL
            or claim.request != request
            or claim.authorization is not authorization
        ):
            raise CtpStoreAccountSessionError("settlement_claim_invalid")
        with self._lock:
            retained = self._claims.get(claim.claim_ref)
            if retained is not claim or request.deadline <= datetime.now(_UTC):
                self._claims.pop(claim.claim_ref, None)
                return _settlement_confirmation_unknown(
                    request,
                    "settlement_claim_expired_or_consumed",
                )
            self._claims.pop(claim.claim_ref, None)

        binding = self._session_reader._capture_binding()
        if (
            binding is None
            or binding.source_ref != claim.binding.source_ref
            or binding.generation != claim.binding.generation
            or request.expected_generation != binding.generation
            or request.business_day != binding.trading_day
        ):
            return _settlement_confirmation_unknown(request, "settlement_session_changed")
        method = getattr(self._client, "confirm_settlement", None)
        canonical_method = getattr(TraderClient, "confirm_settlement", None)
        if (
            not callable(method)
            or not callable(canonical_method)
            or getattr(method, "__self__", None) is not self._client
            or getattr(method, "__func__", None) is not canonical_method
        ):
            return _settlement_confirmation_unknown(
                request,
                "settlement_confirmation_capability_unavailable",
            )
        remaining = (request.deadline - datetime.now(_UTC)).total_seconds()
        if not isfinite(remaining) or remaining <= 0:
            return _settlement_confirmation_unknown(request, "settlement_deadline_elapsed")
        try:
            confirmed = method(
                timeout=remaining,
                _execution_capability=self._execution_capability,
                _settlement_authorization=authorization,
                _settlement_environment_profile=self._environment_profile,
                _settlement_environment_verified=True,
            )
        except Exception:
            return _settlement_confirmation_unknown(
                request,
                "settlement_confirmation_outcome_unknown",
            )
        observed_at = datetime.now(_UTC)
        current = self._session_reader._capture_binding()
        if (
            confirmed is not True
            or current is None
            or current.source_ref != binding.source_ref
            or current.generation != binding.generation
        ):
            return _settlement_confirmation_unknown(
                request,
                "settlement_confirmation_not_confirmed",
                observed_at=observed_at,
            )
        receipt = SettlementConfirmationReceipt(
            request=request,
            disposition="accepted",
            observed_at=observed_at,
            receipt_ref="ctp-settlement-confirmation:" + uuid.uuid4().hex,
        )
        with self._lock:
            self._receipts[receipt.receipt_ref] = (receipt, binding)
            while len(self._receipts) > _MAX_SETTLEMENT_RECEIPTS:
                self._receipts.popitem(last=False)
        return receipt

    def verify_confirmation(
        self,
        receipt: SettlementConfirmationReceipt,
        *,
        now: datetime,
    ) -> bool:
        """Require exact receipt custody and a fresh matching server readback."""

        _require_utc(now, "now")
        if type(receipt) is not SettlementConfirmationReceipt or receipt.disposition != "accepted":
            return False
        if now > datetime.now(_UTC) or receipt.receipt_ref is None:
            return False
        with self._lock:
            retained = self._receipts.get(receipt.receipt_ref)
        if retained is None or retained[0] is not receipt:
            return False
        _, binding = retained
        if (
            receipt.request.exchange_name != self._exchange_name
            or receipt.request.expected_generation != binding.generation
            or receipt.request.business_day != binding.trading_day
        ):
            return False
        verification = self._session_reader.verify_settlement(
            deadline=datetime.now(_UTC) + timedelta(seconds=5.0)
        )
        return bool(
            verification.complete is True
            and verification.read_only_safe is True
            and verification.status == "confirmed"
            and verification.source_ref == binding.source_ref
            and verification.generation == binding.generation
            and verification.business_day == binding.trading_day
        )

    def _prune_claims(self, now: datetime) -> None:
        expired = [
            claim_ref
            for claim_ref, claim in self._claims.items()
            if claim.request.deadline <= now
        ]
        for claim_ref in expired:
            self._claims.pop(claim_ref, None)


def _ctp_trader_client(route: object) -> tuple[TraderClient, str | None]:
    """Unwrap only the exact CTP future feed inside the CTP SDK package."""

    from bt_api_ctp.feeds.live_ctp_feed import CtpRequestDataFuture

    if (
        type(route) is not SelectedAccountSessionRoute
        or route.exchange_name != _CTP_FUTURE_ROUTE
        or type(route.client) is not CtpRequestDataFuture
    ):
        raise CtpStoreAccountSessionError("account_session_client_not_canonical")
    try:
        client = route.client.trader_client
    except Exception:
        raise CtpStoreAccountSessionError("account_session_client_not_canonical") from None
    if type(client) is not TraderClient:
        raise CtpStoreAccountSessionError("account_session_client_not_canonical")
    return client, route.environment


def _create_ctp_account_session_port(route: object) -> AccountSessionPort:
    client, environment = _ctp_trader_client(route)
    return CtpStoreAccountSessionService(
        client,
        exchange_name=_CTP_FUTURE_ROUTE,
        environment=environment,
    )


def _create_ctp_settlement_confirmation_port(
    route: object,
) -> SettlementConfirmationPort:
    client, environment = _ctp_trader_client(route)
    if environment not in {"production", "demo", "testnet"}:
        raise CtpStoreAccountSessionError("settlement_authorization_invalid")
    capability = getattr(client, "_execution_gate_capability", None)
    if (
        not _is_ctp_core_execution_authority(capability)
        or capability is not getattr(client, "_execution_gate_capability", None)
    ):
        raise CtpStoreAccountSessionError("settlement_authorization_invalid")
    environment_profile = getattr(client, "_execution_gate_environment_profile", None)
    if type(environment_profile) is not str or not environment_profile:
        raise CtpStoreAccountSessionError("settlement_authorization_invalid")
    return CtpSettlementConfirmationService(
        client,
        exchange_name=_CTP_FUTURE_ROUTE,
        environment=environment,
        environment_profile=environment_profile,
        environment_verified=True,
        execution_capability=capability,
    )


def register_ctp_account_session_provider(registry: object) -> None:
    """Register CTP account and settlement ports for the exact futures route.

    The SDK entry-point loader passes its neutral route registry here. This
    registrar does not select or connect a feed. The selected feed is unwrapped
    only by the factories above after the SDK has already fixed the route.
    """

    if not isinstance(registry, AccountSessionProviderRegistry):
        raise TypeError("registry must satisfy AccountSessionProviderRegistry")
    registry.register(
        _CTP_FUTURE_ROUTE,
        _create_ctp_account_session_port,
        _create_ctp_settlement_confirmation_port,
    )


def _optional_bool(value: Any) -> bool | None:
    return value if type(value) is bool else None


def _authenticated_state(value: Any) -> bool | None:
    if type(value) is not str:
        return None
    if value in {"authenticated", "success", "ready", "logged_in"}:
        return True
    if value in {"failed", "unauthenticated", "disconnected", "blocked"}:
        return False
    return None


def _optional_day(value: Any) -> str | None:
    if type(value) is str and len(value) == 8 and value.isascii() and value.isdigit():
        return value
    return None


def _settlement_state(state: Mapping[str, Any]) -> str:
    value = state.get("settlement_state")
    if (
        value == "confirmed"
        and state.get("settlement_readback_verified") is True
    ):
        return "confirmed"
    if type(value) is not str:
        return "unknown"
    if value in {"not_requested", "required", "failed"}:
        return "required"
    if value == "not_required":
        return "not_required"
    return "unknown"


def _opaque_session_ref(source_ref: str, generation: str) -> str:
    material = (source_ref + "\0" + generation).encode("utf-8", errors="strict")
    return "account-session:" + hashlib.sha256(material).hexdigest()


def _preflight_queries(request: AccountPreflightRequest) -> tuple[QueryRequest, ...]:
    symbol = request.symbol
    specs = (
        ("account", {}),
        ("positions", {}),
        ("open_orders", {"symbol": symbol}),
        ("instruments", {"symbol": symbol}),
    )
    return tuple(
        QueryRequest(
            request_ref="account-preflight:" + uuid.uuid4().hex,
            family=family,
            filters=filters,
            required=True,
            require_complete_scope=True,
        )
        for family, filters in specs
    )


def _unavailable_preflight(
    request: AccountPreflightRequest,
    blocker: str,
) -> AccountPreflightSnapshot:
    return AccountPreflightSnapshot(
        request=request,
        source_ref=None,
        generation=None,
        observed_at=None,
        ready=None,
        available=False,
        blockers=(blocker,),
    )


def _receipt_blockers(receipt: QueryBatchReceipt) -> tuple[str, ...]:
    blockers = list(receipt.blockers)
    for snapshot in receipt.snapshots:
        blockers.extend(snapshot.blockers)
        if not snapshot.is_complete:
            blockers.append(snapshot.blockers[0] if snapshot.blockers else "query_snapshot_incomplete")
    return tuple(dict.fromkeys(blockers)) or ("query_batch_incomplete",)


def _request_counts(state: Mapping[str, Any] | None) -> Mapping[str, int] | None:
    if state is None:
        return None
    counts = state.get("request_counts")
    if not isinstance(counts, Mapping) or set(counts) != set(CTP_REQUEST_COUNT_KEYS):
        return None
    result: dict[str, int] = {}
    for name in CTP_REQUEST_COUNT_KEYS:
        value = counts.get(name)
        if type(value) is not int or value < 0:
            return None
        result[name] = value
    return result


def _write_request_free(
    before: Mapping[str, Any] | None,
    after: Mapping[str, Any] | None,
) -> bool | None:
    before_counts = _request_counts(before)
    after_counts = _request_counts(after)
    if before_counts is None or after_counts is None:
        return None
    return all(before_counts[name] == after_counts[name] for name in _WRITE_COUNTERS)


def _valid_settlement_result(
    result: Any,
    binding: CtpQuerySessionBinding,
) -> bool:
    if (
        type(result) is not QueryResult
        or result.request_type != "settlement_confirmation"
        or result.complete is not True
        or result.is_last_seen is not True
        or result.timed_out is not False
        or result.unsupported is not False
        or result.completion_origin != "success"
        or result.error_message != ""
        or result.error_code not in (None, 0)
        or type(result.error_code) not in (int, type(None))
        or type(result.late_callback_count) is not int
        or result.late_callback_count != 0
        or type(result.submit_code) not in (int, type(None))
        or result.submit_code not in (None, 0)
        or result.first_completion_origin != "success"
        or result.record_limit is not None
        or result.record_limit_exceeded is not False
        or type(result.request_id) is not int
        or result.request_id <= 0
        or result.connection_generation != binding.connection_generation
        or result.account_fingerprint != binding.account_fingerprint
        or type(result.records) is not tuple
        or len(result.records) > 100_000
    ):
        return False
    source = result.query_source
    if type(source) is not _QuerySource or source._seal is not _QUERY_SOURCE_SEAL:
        return False
    expected_filters = tuple(
        sorted(
            (
                ("BrokerID", binding.broker_id),
                ("InvestorID", binding.investor_id),
            )
        )
    )
    try:
        records_digest = _query_records_digest(result.records)
    except Exception:
        return False
    if (
        source.issuer is not binding._issuer
        or source.source_tags != binding._source_tags
        or source.request_type != result.request_type
        or source.request_id != result.request_id
        or source.account_fingerprint != binding.account_fingerprint
        or source.connection_generation != binding.connection_generation
        or source.trading_day != binding.trading_day
        or source.broker_id != binding.broker_id
        or source.investor_id != binding.investor_id
        or source.started_at_utc != result.started_at_utc
        or source.completed_at_utc != result.completed_at_utc
        or source.records_sha256 != records_digest
        or source.clock_domain_id != _QUERY_MONOTONIC_CLOCK_DOMAIN
        or source.request_intent_filters != expected_filters
        or source.request_filters != expected_filters
        or source.request_intent_parameters != ()
        or source.request_parameters != ()
        or source.explicit_request_filters != ()
        or source.record_limit is not None
        or source.record_limit_exceeded is not False
        or not _aware_utc(result.started_at_utc)
        or not _aware_utc(result.completed_at_utc)
        or result.started_at_utc > result.completed_at_utc
        or result.completed_at_utc > datetime.now(_UTC)
        or not _finite_nonnegative(source.started_monotonic)
        or not _finite_nonnegative(source.completed_monotonic)
        or source.started_monotonic > source.completed_monotonic
        or source.completed_monotonic > time.monotonic()
        or not _finite_nonnegative(source.trusted_expires_monotonic)
        or source.trusted_expires_monotonic
        != source.completed_monotonic + _QUERY_EVIDENCE_MAX_TTL_SECONDS
        or time.monotonic() >= source.trusted_expires_monotonic
        or not _aware_utc(source.trusted_expires_at_utc)
        or source.trusted_expires_at_utc
        != result.completed_at_utc + timedelta(seconds=_QUERY_EVIDENCE_MAX_TTL_SECONDS)
    ):
        return False
    return True


def _negative_settlement_readback(
    result: QueryResult[Any],
    state: Mapping[str, Any],
    binding: CtpQuerySessionBinding,
) -> bool:
    """Prove the complete current-account response omitted today's receipt."""

    last_error = state.get("last_error")
    if (
        not isinstance(last_error, Mapping)
        or last_error.get("error")
        != "ctp_execution_gate_settlement_readback_identity_mismatch"
    ):
        return False
    for record in result.records:
        if isinstance(record, Mapping):
            getter = record.get
        else:
            getter = lambda name, default="", _record=record: getattr(
                _record, name, default
            )
        try:
            broker_id = str(getter("BrokerID", "") or "")
            investor_id = str(getter("InvestorID", "") or "")
            trading_day = str(
                getter("TradingDay", "") or getter("ConfirmDate", "") or ""
            )
        except Exception:
            return False
        if (
            broker_id != binding.broker_id
            or investor_id != binding.investor_id
            or trading_day == binding.trading_day
        ):
            return False
    return True


def _aware_utc(value: Any) -> bool:
    return bool(
        type(value) is datetime
        and value.tzinfo is not None
        and value.utcoffset() is not None
        and value.utcoffset() == timedelta(0)
    )


def _finite_nonnegative(value: Any) -> bool:
    return bool(
        type(value) in (int, float)
        and not isinstance(value, bool)
        and isfinite(float(value))
        and value >= 0
    )


def _query_evidence_expiry(receipt: QueryBatchReceipt) -> datetime:
    completed = [
        snapshot.completed_at
        for snapshot in receipt.snapshots
        if snapshot.is_complete and snapshot.completed_at is not None
    ]
    if not completed:
        raise ValueError("complete query receipt has no complete snapshot timestamps")
    # CTP evidence uses a 35 second SDK lifetime. Keep this provider policy
    # private and issue a short bounded lease well inside that limit.
    return min(completed) + timedelta(seconds=30.0)


def _settlement_unavailable(
    exchange_name: str,
    reason_code: str,
    *,
    binding: CtpQuerySessionBinding | None = None,
    observed_at: datetime | None = None,
) -> SettlementVerificationReceipt:
    return SettlementVerificationReceipt(
        exchange_name=exchange_name,
        source_ref=None if binding is None else binding.source_ref,
        generation=None if binding is None else binding.generation,
        business_day=None if binding is None else binding.trading_day,
        status="unknown",
        observed_at=observed_at,
        complete=False,
        read_only_safe=False,
        receipt_ref=None,
        reason_code=reason_code,
    )


def _settlement_confirmation_unknown(
    request: SettlementConfirmationRequest,
    reason_code: str,
    *,
    observed_at: datetime | None = None,
) -> SettlementConfirmationReceipt:
    return SettlementConfirmationReceipt(
        request=request,
        disposition="unknown",
        observed_at=datetime.now(_UTC) if observed_at is None else observed_at,
        receipt_ref=None,
        reason_code=reason_code,
    )


def _settlement_failure_code(
    *,
    source_valid: bool,
    session_current: bool,
    write_request_free: bool | None,
    automatic_confirmation_disabled: bool,
    readback_verified: bool,
    deadline: datetime,
    observed_at: datetime,
) -> str:
    if observed_at > deadline:
        return "settlement_deadline_exceeded"
    if not source_valid:
        return "settlement_query_evidence_incomplete"
    if not session_current:
        return "settlement_session_changed"
    if write_request_free is not True:
        return "settlement_read_only_evidence_unavailable_or_changed"
    if not automatic_confirmation_disabled:
        return "automatic_settlement_confirmation_not_disabled"
    if not readback_verified:
        return "settlement_server_readback_not_confirmed"
    return "settlement_verification_incomplete"


__all__ = [
    "CtpSettlementConfirmationService",
    "CtpStoreAccountSessionError",
    "CtpStoreAccountSessionService",
    "register_ctp_account_session_provider",
]
