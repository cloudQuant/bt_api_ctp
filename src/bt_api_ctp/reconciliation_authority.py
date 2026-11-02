"""CTP-backed, fail-closed reconciliation evidence collection.

This provider adapter stays inside bt_api_ctp. It validates CTP's sealed query
sources and normalizes native rows to bt_api_base DTOs. CTP currently cannot
prove strategy intent correlation, a coherent atomic multi-query snapshot,
fee currency for each fill, or strategy-level position consistency, so the
adapter emits explicitly unresolved observations and never a safe receipt.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import secrets
import threading
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

from bt_api_base.account_protocol import AccountKey
from bt_api_base.reconciliation_protocol import (
    ProviderReconciliationAuthority,
    ReconciliationCoverage,
    ReconciliationEvidence,
    ReconciliationRequest,
    TerminalOrderSnapshot,
    UnresolvedFillObservation,
    UnresolvedPositionObservation,
)
from bt_api_base.managed_execution_runtime_protocol import (
    OwnerThreadCallError,
    OwnerThreadCallPort,
)
from bt_api_ctp.ctp.client import TraderClient
from bt_api_ctp.query import (
    _QUERY_EVIDENCE_MAX_TTL_SECONDS,
    _QUERY_MONOTONIC_CLOCK_DOMAIN,
    _QUERY_SOURCE_SEAL,
    _query_records_digest,
    QueryResult,
    _QuerySource,
)
from bt_api_ctp.registered_sim_session import (
    CtpQuerySessionBinding,
    capture_query_session,
    validate_query_session,
)

_UTC = timezone.utc
_CHINA = timezone(timedelta(hours=8))
_MAX_ISSUED_EVIDENCE = 32
_IDENTITY_NAMESPACE_ATTR = "_bt_api_ctp_reconciliation_identity_namespace_v1"
_IDENTITY_NAMESPACE_LOCK = threading.RLock()


class _OpaqueIdentityNamespace:
    """Private, source-bound opaque ID issuer for one live CTP session."""

    __slots__ = ("source_ref", "generation", "issuer", "_key")

    def __init__(self, binding: CtpQuerySessionBinding) -> None:
        self.source_ref = binding.source_ref
        self.generation = binding.generation
        self.issuer = binding._issuer
        self._key = secrets.token_bytes(32)

    def matches(self, binding: CtpQuerySessionBinding) -> bool:
        return bool(
            self.source_ref == binding.source_ref
            and self.generation == binding.generation
            and self.issuer is binding._issuer
        )

    def issue(self, kind: str, native_identity: object) -> str:
        material = _canonical_json(
            {
                "source_ref": self.source_ref,
                "generation": self.generation,
                "kind": kind,
                "native_identity": native_identity,
            }
        )
        digest = hmac.new(self._key, material, hashlib.sha256).hexdigest()
        return f"{kind}:v1:{digest}"


def _identity_namespace(
    client: TraderClient,
    binding: CtpQuerySessionBinding,
) -> _OpaqueIdentityNamespace:
    with _IDENTITY_NAMESPACE_LOCK:
        current = getattr(client, _IDENTITY_NAMESPACE_ATTR, None)
        if type(current) is _OpaqueIdentityNamespace and current.matches(binding):
            return current
        current = _OpaqueIdentityNamespace(binding)
        setattr(client, _IDENTITY_NAMESPACE_ATTR, current)
        return current


def _same_query_binding(
    left: CtpQuerySessionBinding,
    right: CtpQuerySessionBinding,
) -> bool:
    """Compare every session fact while keeping the native issuer identity exact."""

    return bool(
        type(left) is CtpQuerySessionBinding
        and type(right) is CtpQuerySessionBinding
        and left.source_ref == right.source_ref
        and left.generation == right.generation
        and left.environment == right.environment
        and left.environment_profile == right.environment_profile
        and left.account_fingerprint == right.account_fingerprint
        and left.connection_generation == right.connection_generation
        and left.trading_day == right.trading_day
        and left.broker_id == right.broker_id
        and left.investor_id == right.investor_id
        and left._issuer is right._issuer
        and left._source_tags == right._source_tags
        and left._captured_monotonic == right._captured_monotonic
    )


class CtpReconciliationAuthorityError(RuntimeError):
    """Finite, value-free CTP reconciliation authority failure."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class CtpReconciliationAuthority(ProviderReconciliationAuthority):
    """Collect CTP positions/orders/trades under one exact client generation.

    ``account_key`` is checked against the sealed CTP login scope. ``request``
    is fixed by trusted SDK composition when the authority is constructed; an
    opaque-handle facade must pass this same object. The present CTP surfaces
    do not carry enough strategy and accounting evidence to issue a lease or
    mark the result safe.
    """

    def __init__(
        self,
        client: TraderClient,
        binding: CtpQuerySessionBinding,
        account_key: AccountKey,
        request: ReconciliationRequest,
        *,
        selected_feed: object,
        runtime_context: object,
        environment: str,
        exchange_name: str = "CTP___FUTURE",
    ) -> None:
        if type(client) is not TraderClient:
            raise CtpReconciliationAuthorityError("ctp_reconciliation_client_invalid")
        if type(binding) is not CtpQuerySessionBinding:
            raise CtpReconciliationAuthorityError("ctp_reconciliation_binding_invalid")
        if type(account_key) is not AccountKey:
            raise CtpReconciliationAuthorityError("ctp_reconciliation_account_key_invalid")
        if type(request) is not ReconciliationRequest:
            raise CtpReconciliationAuthorityError("ctp_reconciliation_request_invalid")
        if (
            type(environment) is not str
            or not environment
            or type(exchange_name) is not str
            or exchange_name != "CTP___FUTURE"
        ):
            raise CtpReconciliationAuthorityError("ctp_reconciliation_route_invalid")
        if (
            binding.environment is None
            or account_key.provider != "ctp"
            or account_key.environment != binding.environment
            or account_key.account_id != binding.investor_id
            or account_key.subaccount_id != binding.broker_id
            or request.venue_ref != account_key.venue
            or environment != binding.environment
        ):
            raise CtpReconciliationAuthorityError("ctp_reconciliation_scope_unbound")
        self._client = client
        self._binding = binding
        self._account_key = account_key
        self._request = request
        self._selected_feed = selected_feed
        self._runtime_context = runtime_context
        self._environment = environment
        self._exchange_name = exchange_name
        self._state_lock = threading.RLock()
        self._uncertain = False
        self._issued: dict[int, tuple[ReconciliationEvidence, tuple[QueryResult[Any], ...]]] = {}

        # Runtime composition is the only source of the owner mailbox and its
        # selected-feed seal. Do not inspect or query the CTP client until that
        # exact route has been revalidated on the owner thread.
        try:
            self._identities = self._call_on_owner_thread(
                self._initialize_on_owner_thread,
                timeout_seconds=self._owner_timeout_seconds(),
            )
        except CtpReconciliationAuthorityError:
            raise
        except Exception:
            raise CtpReconciliationAuthorityError(
                "ctp_reconciliation_runtime_binding_unavailable"
            ) from None

    def _owner_timeout_seconds(self) -> float:
        context = self._runtime_context
        timeout = getattr(context, "owner_call_timeout_seconds", None)
        if (
            type(timeout) not in (int, float)
            or isinstance(timeout, bool)
            or not math.isfinite(float(timeout))
            or not 0.01 <= float(timeout) <= 30.0
        ):
            raise CtpReconciliationAuthorityError(
                "ctp_reconciliation_owner_timeout_invalid"
            )
        return float(timeout)

    def _runtime_route_current_on_owner_thread(self) -> bool:
        """Revalidate the exact selected feed/context before touching CTP state."""

        try:
            from bt_api_execution.contracts import ExecutionScope
            from bt_api_ctp.feeds.live_ctp_feed import (
                CtpRequestDataFuture,
            )
            from bt_api_ctp.order_provider import CtpOrderProviderRuntimeContext

            feed = self._selected_feed
            context = self._runtime_context
            if type(context) is not CtpOrderProviderRuntimeContext:
                return False
            scope = getattr(context, "scope", None)
            runtime_binding = getattr(context, "runtime_binding", None)
            invoker = getattr(context, "owner_thread_invoker", None)
            if (
                type(feed) is not CtpRequestDataFuture
                or type(scope) is not ExecutionScope
                or not isinstance(invoker, OwnerThreadCallPort)
                or runtime_binding is None
                or getattr(feed, "_managed_runtime_binding", None) is not runtime_binding
                or feed.trader_client is not self._client
                or scope.provider != "ctp"
                or scope.trading_day != self._binding.trading_day
                or self._request.scope_ref != scope.key
                or self._request.account_ref != scope.account_ref
                or self._request.strategy_ref != scope.strategy_id
            ):
                return False

            validate_context = getattr(runtime_binding, "validate_provider_context", None)
            if not callable(validate_context):
                return False
            if (
                validate_context(
                    context,
                    exchange_name=self._exchange_name,
                    selected_feed=feed,
                    trader_client=self._client,
                )
                is not True
            ):
                return False
            return validate_query_session(
                self._client,
                self._binding,
                now_monotonic=time.monotonic(),
            ) is True
        except Exception:
            return False

    def _call_on_owner_thread(
        self,
        callback: Any,
        *,
        timeout_seconds: float,
    ) -> Any:
        """Run one at-most-once operation through the existing CTP mailbox."""

        with self._state_lock:
            if self._uncertain:
                raise CtpReconciliationAuthorityError(
                    "ctp_reconciliation_owner_call_unknown"
                )
        context = self._runtime_context
        invoker = getattr(context, "owner_thread_invoker", None)
        if not isinstance(invoker, OwnerThreadCallPort):
            raise CtpReconciliationAuthorityError(
                "ctp_reconciliation_owner_invoker_unavailable"
            )

        def run_on_owner() -> Any:
            with self._state_lock:
                if self._uncertain:
                    raise CtpReconciliationAuthorityError(
                        "ctp_reconciliation_owner_call_unknown"
                    )
            if not self._runtime_route_current_on_owner_thread():
                raise CtpReconciliationAuthorityError(
                    "ctp_reconciliation_runtime_route_stale"
                )
            return callback()

        try:
            return invoker.call_on_owner_thread(
                run_on_owner,
                timeout_seconds=timeout_seconds,
            )
        except OwnerThreadCallError as exc:
            if exc.code == "OWNER_THREAD_CALL_TIMEOUT_AFTER_START":
                self._mark_uncertain()
                raise CtpReconciliationAuthorityError(
                    "ctp_reconciliation_owner_call_unknown"
                ) from None
            raise CtpReconciliationAuthorityError(
                "ctp_reconciliation_owner_call_unavailable"
            ) from None
        except CtpReconciliationAuthorityError:
            raise
        except Exception:
            # A callback exception can occur after native query dispatch. Its
            # outcome is unknown, so this authority must never retry it.
            self._mark_uncertain()
            raise CtpReconciliationAuthorityError(
                "ctp_reconciliation_owner_call_unknown"
            ) from None

    def _mark_uncertain(self) -> None:
        with self._state_lock:
            self._uncertain = True
            self._issued.clear()

    def _initialize_on_owner_thread(self) -> _OpaqueIdentityNamespace:
        # Capture the session lease on the owner thread, then require that it
        # is the same sealed source/generation supplied by trusted composition.
        # This avoids moving a caller-thread session capture into provider use.
        captured = capture_query_session(
            self._client,
            environment=self._environment,
        )
        if not _same_query_binding(captured, self._binding):
            raise CtpReconciliationAuthorityError(
                "ctp_reconciliation_session_binding_mismatch"
            )
        self._binding = captured
        identities = _identity_namespace(self._client, self._binding)
        issuer_material = {
            "account_key_id": self._account_key.account_key_id,
            "scope_ref": self._request.scope_ref,
            "strategy_ref": self._request.strategy_ref,
        }
        self.issuer_ref = identities.issue("reconciliation_issuer", issuer_material)
        self._source_ref = identities.issue("source", self._binding.source_ref)
        self._generation = identities.issue(
            "generation",
            {
                "source_ref": self._binding.source_ref,
                "generation": self._binding.generation,
            },
        )
        return identities

    def query_reconciliation(
        self,
        request: ReconciliationRequest,
        *,
        deadline: datetime,
    ) -> ReconciliationEvidence:
        """Read on the selected runtime owner and return unresolved neutral rows."""

        if type(request) is not ReconciliationRequest or request is not self._request:
            return self._blocked_evidence("ctp_reconciliation_scope_mismatch")
        if type(deadline) is not datetime or deadline.tzinfo is None:
            return self._blocked_evidence("ctp_reconciliation_deadline_invalid")
        if deadline.utcoffset() != timedelta(0):
            return self._blocked_evidence("ctp_reconciliation_deadline_invalid")
        remaining = (deadline - datetime.now(_UTC)).total_seconds()
        if not math.isfinite(remaining) or remaining <= 0:
            return self._blocked_evidence("ctp_reconciliation_deadline_expired")
        deadline_monotonic = time.monotonic() + remaining
        timeout_seconds = min(self._owner_timeout_seconds(), remaining)
        if timeout_seconds < 0.01:
            return self._blocked_evidence("ctp_reconciliation_deadline_expired")
        try:
            return self._call_on_owner_thread(
                lambda: self._query_reconciliation_on_owner_thread(
                    request,
                    deadline=deadline,
                    deadline_monotonic=deadline_monotonic,
                ),
                timeout_seconds=timeout_seconds,
            )
        except CtpReconciliationAuthorityError as exc:
            return self._blocked_evidence(exc.code)

    def _query_reconciliation_on_owner_thread(
        self,
        request: ReconciliationRequest,
        *,
        deadline: datetime,
        deadline_monotonic: float,
    ) -> ReconciliationEvidence:
        """Perform a single serialized read sweep on the bound CTP owner."""

        if type(request) is not ReconciliationRequest or request is not self._request:
            return self._blocked_evidence("ctp_reconciliation_scope_mismatch")
        if type(deadline) is not datetime or deadline.tzinfo is None:
            return self._blocked_evidence("ctp_reconciliation_deadline_invalid")
        if deadline.utcoffset() != timedelta(0):
            return self._blocked_evidence("ctp_reconciliation_deadline_invalid")
        if (
            not math.isfinite(deadline_monotonic)
            or deadline_monotonic <= time.monotonic()
        ):
            return self._blocked_evidence("ctp_reconciliation_deadline_expired")
        if not validate_query_session(
            self._client, self._binding, now_monotonic=time.monotonic()
        ):
            return self._blocked_evidence("ctp_reconciliation_session_stale")

        results: dict[str, QueryResult[Any] | None] = {
            "orders": None,
            "fills": None,
            "positions": None,
        }
        blockers = {
            "ctp_strategy_intent_mapping_unavailable",
            "ctp_strategy_position_attribution_unavailable",
            "ctp_position_consistency_baseline_unavailable",
            "ctp_multi_query_atomic_snapshot_unavailable",
            "ctp_order_history_window_unproven",
        }
        if request.instrument_refs:
            blockers.add("ctp_instrument_reference_mapping_unavailable")
        if request.intent_refs:
            blockers.add("ctp_requested_intent_mapping_unavailable")

        calls = (
            (
                "positions",
                lambda timeout: self._client.query_positions_result(
                    timeout=timeout,
                    deadline_monotonic=deadline_monotonic,
                ),
            ),
            (
                "orders",
                lambda timeout: self._client.query_orders_result(
                    timeout=timeout,
                    deadline_monotonic=deadline_monotonic,
                ),
            ),
            (
                "fills",
                lambda timeout: self._client.query_trades_result(
                    timeout=timeout,
                    deadline_monotonic=deadline_monotonic,
                ),
            ),
        )
        for family, query in calls:
            if self._is_uncertain():
                blockers.add("ctp_reconciliation_owner_call_unknown")
                break
            left = deadline_monotonic - time.monotonic()
            if not math.isfinite(left) or left <= 0:
                blockers.add("ctp_reconciliation_query_deadline_exceeded")
                break
            try:
                result = query(left)
            except Exception:
                self._mark_uncertain()
                blockers.add("ctp_reconciliation_query_outcome_unknown")
                break
            if type(result) is QueryResult and (
                result.timed_out is True
                or (
                    type(result.late_callback_count) is int
                    and result.late_callback_count > 0
                )
            ):
                self._mark_uncertain()
                blockers.add("ctp_reconciliation_query_outcome_unknown")
                break
            if self._validate_query_result(result, family):
                results[family] = result
            else:
                blockers.add("ctp_reconciliation_query_evidence_invalid")
            if self._is_uncertain():
                blockers.add("ctp_reconciliation_owner_call_unknown")
                break

        if not validate_query_session(
            self._client, self._binding, now_monotonic=time.monotonic()
        ):
            blockers.add("ctp_reconciliation_session_changed")
        if not self._runtime_route_current_on_owner_thread():
            blockers.add("ctp_reconciliation_runtime_route_changed")

        snapshots: dict[str, tuple[Any, ...] | None] = {
            "orders": None,
            "fills": None,
            "positions": None,
        }
        normalizers = {
            "orders": self._normalize_orders,
            "fills": self._normalize_fills,
            "positions": self._normalize_positions,
        }
        for family, result in results.items():
            if result is None:
                continue
            try:
                snapshots[family] = normalizers[family](result)
            except Exception:
                blockers.add(f"ctp_{family}_row_projection_invalid")

        if snapshots["orders"] is not None and snapshots["fills"] is not None:
            blockers.update(
                _order_fill_blockers(snapshots["orders"], snapshots["fills"])
            )
            if any(fill.commission is None for fill in snapshots["fills"]):
                blockers.add("ctp_fill_commission_currency_unavailable")

        # An account-wide CTP result is useful diagnostics, but it cannot be
        # attributed to one strategy. Never relabel it as strategy evidence
        # or fold it into proof-grade order/fill/position fields.
        valid_results = tuple(result for result in results.values() if result is not None)
        snapshot_ref: str | None = None
        consistency_ref: str | None = None
        observed_at: datetime | None = None
        coverage: tuple[ReconciliationCoverage, ...] | None = None
        if valid_results:
            source_rows = [
                {
                    "request_type": result.request_type,
                    "request_id": result.request_id,
                    "records_sha256": result.query_source.records_sha256,
                    "completed_at": result.completed_at_utc.isoformat(),
                }
                for result in valid_results
            ]
            snapshot_ref = self._identities.issue("snapshot", source_rows)
            consistency_ref = self._identities.issue("non_atomic_consistency", source_rows)
            observed_at = max(result.completed_at_utc for result in valid_results)
            coverage = tuple(
                self._coverage(family, results[family], snapshots[family], snapshot_ref, consistency_ref)
                for family in ("orders", "fills", "positions")
            )

        if snapshots["orders"] is None:
            unresolved_orders = None
        else:
            unresolved_orders = snapshots["orders"]
        if snapshots["fills"] is None:
            unresolved_fills = None
        else:
            unresolved_fills = snapshots["fills"]
        if snapshots["positions"] is None:
            unresolved_positions = None
        else:
            unresolved_positions = snapshots["positions"]

        evidence = ReconciliationEvidence(
            request=request,
            issuer_ref=self.issuer_ref,
            source_ref=self._source_ref,
            generation=self._generation,
            snapshot_ref=snapshot_ref,
            consistency_ref=consistency_ref,
            observed_at=observed_at,
            coverage=coverage,
            orders=None,
            fills=None,
            positions=None,
            unresolved_orders=unresolved_orders,
            unresolved_fills=unresolved_fills,
            unresolved_positions=unresolved_positions,
            lease=None,
            position_receipt=None,
            blockers=tuple(sorted(blockers)),
        )
        with self._state_lock:
            if not self._uncertain:
                self._issued[id(evidence)] = (evidence, valid_results)
                while len(self._issued) > _MAX_ISSUED_EVIDENCE:
                    self._issued.pop(next(iter(self._issued)))
        return evidence

    def verify_reconciliation_evidence(
        self,
        evidence: ReconciliationEvidence,
        request: ReconciliationRequest,
    ) -> bool:
        """Verify evidence on the same selected runtime owner thread."""

        if (
            type(evidence) is not ReconciliationEvidence
            or type(request) is not ReconciliationRequest
            or request is not self._request
            or evidence.request is not request
        ):
            return False
        try:
            return self._call_on_owner_thread(
                lambda: self._verify_reconciliation_evidence_on_owner_thread(
                    evidence,
                    request,
                ),
                timeout_seconds=self._owner_timeout_seconds(),
            ) is True
        except CtpReconciliationAuthorityError:
            return False

    def _verify_reconciliation_evidence_on_owner_thread(
        self,
        evidence: ReconciliationEvidence,
        request: ReconciliationRequest,
    ) -> bool:
        """Check exact issued evidence/source objects while on the owner."""

        if (
            type(evidence) is not ReconciliationEvidence
            or type(request) is not ReconciliationRequest
            or request is not self._request
            or evidence.request is not request
            or evidence.issuer_ref != self.issuer_ref
            or evidence.source_ref != self._source_ref
            or evidence.generation != self._generation
        ):
            return False
        with self._state_lock:
            issued = self._issued.get(id(evidence))
        if issued is None or issued[0] is not evidence:
            return False
        if not validate_query_session(
            self._client, self._binding, now_monotonic=time.monotonic()
        ):
            return False
        valid_types = {"orders", "fills", "positions"}
        for result in issued[1]:
            family = {
                "orders": "orders",
                "trades": "fills",
                "positions": "positions",
            }.get(result.request_type)
            if family not in valid_types or not self._validate_query_result(result, family):
                return False
        return True

    def _is_uncertain(self) -> bool:
        with self._state_lock:
            return self._uncertain is True

    def validate_reconciliation_lease(
        self,
        evidence: ReconciliationEvidence,
        *,
        now: datetime,
    ) -> bool:
        """CTP has no strategy-scoped reconciliation lease issuer yet."""

        return False

    def verify_position_consistency(self, evidence: ReconciliationEvidence) -> bool:
        """CTP exposes no strategy position baseline / order-fill accounting proof."""

        return False

    def _blocked_evidence(self, blocker: str) -> ReconciliationEvidence:
        return ReconciliationEvidence(
            request=self._request,
            issuer_ref=self.issuer_ref,
            source_ref=self._source_ref,
            generation=self._generation,
            blockers=(blocker, "ctp_reconciliation_unavailable"),
        )

    def _validate_query_result(self, result: Any, family: str) -> bool:
        expected_request_type = {
            "orders": "orders",
            "fills": "trades",
            "positions": "positions",
        }.get(family)
        binding = self._binding
        if (
            expected_request_type is None
            or type(result) is not QueryResult
            or result.request_type != expected_request_type
            or result.complete is not True
            or result.is_last_seen is not True
            or result.timed_out is not False
            or result.unsupported is not False
            or result.record_limit_exceeded is not False
            or type(result.late_callback_count) is not int
            or result.late_callback_count != 0
            or type(result.error_message) is not str
            or result.error_message != ""
            or not (
                result.error_code is None
                or (type(result.error_code) is int and result.error_code == 0)
            )
            or type(result.submit_code) is not int
            or result.submit_code != 0
            or type(result.records) is not tuple
            or type(result.request_id) is not int
            or result.request_id <= 0
            or type(result.connection_generation) is not int
            or result.connection_generation != binding.connection_generation
            or type(result.account_fingerprint) is not str
            or result.account_fingerprint != binding.account_fingerprint
            or result.completion_origin != "success"
            or type(result.query_source) is not _QuerySource
        ):
            return False
        source = result.query_source
        now_monotonic = time.monotonic()
        now_utc = datetime.now(_UTC)
        if (
            source._seal is not _QUERY_SOURCE_SEAL
            or source.issuer is not binding._issuer
            or source.request_type != expected_request_type
            or source.request_id != result.request_id
            or source.account_fingerprint != binding.account_fingerprint
            or source.connection_generation != binding.connection_generation
            or source.trading_day != binding.trading_day
            or source.broker_id != binding.broker_id
            or source.investor_id != binding.investor_id
            or source.source_tags != binding._source_tags
            or source.clock_domain_id != _QUERY_MONOTONIC_CLOCK_DOMAIN
            or source.started_at_utc != result.started_at_utc
            or source.completed_at_utc != result.completed_at_utc
            or type(source.started_at_utc) is not datetime
            or type(source.completed_at_utc) is not datetime
            or source.started_at_utc.tzinfo is None
            or source.completed_at_utc.tzinfo is None
            or source.started_at_utc.utcoffset() != timedelta(0)
            or source.completed_at_utc.utcoffset() != timedelta(0)
            or source.started_at_utc > source.completed_at_utc
            or type(source.started_monotonic) not in (int, float)
            or isinstance(source.started_monotonic, bool)
            or not math.isfinite(source.started_monotonic)
            or source.started_monotonic < binding._captured_monotonic
            or type(source.completed_monotonic) not in (int, float)
            or isinstance(source.completed_monotonic, bool)
            or not math.isfinite(source.completed_monotonic)
            or source.completed_monotonic < source.started_monotonic
            or source.completed_monotonic > now_monotonic
            or type(source.trusted_expires_monotonic) not in (int, float)
            or isinstance(source.trusted_expires_monotonic, bool)
            or not math.isfinite(source.trusted_expires_monotonic)
            or now_monotonic >= source.trusted_expires_monotonic
            or type(source.trusted_expires_at_utc) is not datetime
            or source.trusted_expires_at_utc.tzinfo is None
            or source.trusted_expires_at_utc.utcoffset() != timedelta(0)
            or now_utc >= source.trusted_expires_at_utc
            or abs(
                (source.trusted_expires_at_utc - source.completed_at_utc).total_seconds()
                - _QUERY_EVIDENCE_MAX_TTL_SECONDS
            ) > 1e-6
            or type(source.records_sha256) is not str
            or source.record_limit is not None
            or source.record_limit_exceeded is not False
            or result.started_at_utc != source.started_at_utc
            or result.completed_at_utc != source.completed_at_utc
        ):
            return False
        expected_filters = {
            "positions": {
                "BrokerID": binding.broker_id,
                "InvestorID": binding.investor_id,
            },
            "orders": {
                "BrokerID": binding.broker_id,
                "ExchangeID": "",
                "InstrumentID": "",
                "InvestorID": binding.investor_id,
                "OrderSysID": "",
            },
            "trades": {
                "BrokerID": binding.broker_id,
                "ExchangeID": "",
                "InstrumentID": "",
                "InvestorID": binding.investor_id,
                "TradeID": "",
                "TradeTimeEnd": "",
                "TradeTimeStart": "",
            },
        }.get(expected_request_type)
        if expected_filters is None:
            return False
        try:
            if dict(source.request_filters) != expected_filters:
                return False
            if dict(source.request_intent_filters) != expected_filters:
                return False
            if source.request_parameters or source.request_intent_parameters:
                return False
            if _query_records_digest(result.records) != source.records_sha256:
                return False
        except Exception:
            return False
        return True

    def _coverage(
        self,
        family: str,
        result: QueryResult[Any] | None,
        rows: tuple[Any, ...] | None,
        snapshot_ref: str,
        consistency_ref: str,
    ) -> ReconciliationCoverage:
        complete = bool(result is not None and rows is not None)
        empty_proven = bool(
            result is not None
            and rows is not None
            and not result.records
            and result.complete is True
            and result.is_last_seen is True
            and result.error_code in (None, 0)
            and result.record_limit_exceeded is False
        )
        blockers = () if complete else ("ctp_query_or_projection_incomplete",)
        return ReconciliationCoverage(
            family=family,
            scope_ref=self._request.scope_ref,
            source_ref=self._source_ref,
            generation=self._generation,
            snapshot_ref=snapshot_ref,
            consistency_ref=consistency_ref,
            complete=complete,
            scope_complete=False,
            history_complete=False,
            empty_result_proven=empty_proven,
            blockers=blockers,
        )

    def _normalize_orders(
        self, result: QueryResult[Any]
    ) -> tuple[TerminalOrderSnapshot, ...]:
        observed_at = result.completed_at_utc
        if observed_at is None:
            raise ValueError
        projected = []
        seen: set[str] = set()
        for row in result.records:
            _require_row_scope(row, self._binding)
            instrument_id = _text(_field(row, "InstrumentID"))
            exchange_id = _text(_field(row, "ExchangeID"))
            instrument_ref = self._instrument_ref(exchange_id, instrument_id)
            side_code = _text(_field(row, "Direction"))
            side = {"0": "buy", "1": "sell"}.get(side_code)
            if side is None:
                raise ValueError
            quantity = _integer(_field(row, "VolumeTotalOriginal"))
            filled = _integer(_field(row, "VolumeTraded"))
            if quantity <= 0 or filled < 0 or filled > quantity:
                raise ValueError
            status_code = _text(_field(row, "OrderStatus"))
            status = _order_status(status_code, filled, quantity)
            order_id = self._order_id(row)
            if order_id in seen:
                raise ValueError
            seen.add(order_id)
            price_value = _optional_decimal(row, "LimitPrice")
            price = price_value if price_value is not None and price_value > 0 else None
            projected.append(
                TerminalOrderSnapshot(
                    scope_ref=self._request.scope_ref,
                    instrument_ref=instrument_ref,
                    order_id=order_id,
                    symbol=instrument_ref,
                    side=side,
                    status=status,
                    terminal_confirmed=status in {"completed", "canceled", "rejected", "expired"},
                    quantity=Decimal(quantity),
                    filled_quantity=Decimal(filled),
                    quantity_unit="contract",
                    price=price,
                    average_price=None,
                    observed_at=observed_at,
                )
            )
        if len(projected) > 100_000:
            raise ValueError
        return tuple(projected)

    def _normalize_fills(
        self, result: QueryResult[Any]
    ) -> tuple[UnresolvedFillObservation, ...]:
        projected = []
        seen: set[str] = set()
        for row in result.records:
            _require_row_scope(row, self._binding)
            instrument_id = _text(_field(row, "InstrumentID"))
            exchange_id = _text(_field(row, "ExchangeID"))
            instrument_ref = self._instrument_ref(exchange_id, instrument_id)
            side_code = _text(_field(row, "Direction"))
            side = {"0": "buy", "1": "sell"}.get(side_code)
            if side is None:
                raise ValueError
            quantity = _integer(_field(row, "Volume"))
            price = _decimal(_field(row, "Price"))
            if quantity <= 0 or price <= 0:
                raise ValueError
            trade_id = _text(_field(row, "TradeID"))
            trading_day = _text(_field(row, "TradingDay"))
            if trading_day != self._binding.trading_day:
                raise ValueError
            fill_id = self._identities.issue(
                "fill",
                (exchange_id, trading_day, trade_id),
            )
            if fill_id in seen:
                raise ValueError
            seen.add(fill_id)
            commission = _commission(row)
            projected.append(
                UnresolvedFillObservation(
                    scope_ref=self._request.scope_ref,
                    fill_id=fill_id,
                    order_id=self._order_id(row),
                    instrument_ref=instrument_ref,
                    symbol=instrument_ref,
                    side=side,
                    quantity=Decimal(quantity),
                    quantity_unit="contract",
                    price=price,
                    timestamp=_trade_timestamp(row),
                    commission=commission,
                )
            )
        if len(projected) > 100_000:
            raise ValueError
        return tuple(projected)

    def _normalize_positions(
        self, result: QueryResult[Any]
    ) -> tuple[UnresolvedPositionObservation, ...]:
        totals: dict[tuple[str, str, str], Decimal] = {}
        for row in result.records:
            _require_row_scope(row, self._binding)
            instrument_id = _text(_field(row, "InstrumentID"))
            exchange_id = _text(_field(row, "ExchangeID"))
            side_code = _text(_field(row, "PosiDirection"))
            side = {"1": "net", "2": "long", "3": "short"}.get(side_code)
            if side is None:
                raise ValueError
            quantity = Decimal(_integer(_field(row, "Position")))
            if quantity < 0:
                raise ValueError
            key = (exchange_id, instrument_id, side)
            totals[key] = totals.get(key, Decimal("0")) + quantity
        if len(totals) > 100_000:
            raise ValueError
        observed = tuple(
            UnresolvedPositionObservation(
                scope_ref=self._request.scope_ref,
                instrument_ref=self._instrument_ref(exchange_id, instrument_id),
                symbol=self._instrument_ref(exchange_id, instrument_id),
                reported_position_side=side,
                quantity=quantity,
                quantity_unit="contract",
                observed_at=result.completed_at_utc,
                average_price=None,
            )
            for (exchange_id, instrument_id, side), quantity in sorted(totals.items())
        )
        return observed

    def _instrument_ref(self, exchange_id: str, instrument_id: str) -> str:
        return self._identities.issue("instrument", (exchange_id, instrument_id))

    def _order_id(self, row: Any) -> str:
        return self._identities.issue(
            "order",
            _native_order_identity(row, self._binding),
        )


def _order_fill_blockers(
    orders: tuple[TerminalOrderSnapshot, ...],
    fills: tuple[UnresolvedFillObservation, ...],
) -> set[str]:
    """Check exact native order-key joins and traded-quantity totals."""

    by_id = {order.order_id: order for order in orders}
    totals: dict[str, Decimal] = {}
    blockers: set[str] = set()
    for fill in fills:
        order = by_id.get(fill.order_id)
        if (
            order is None
            or order.instrument_ref != fill.instrument_ref
            or order.symbol != fill.symbol
            or order.side != fill.side
            or order.quantity_unit != fill.quantity_unit
        ):
            blockers.add("ctp_trade_order_join_unverified")
            continue
        totals[fill.order_id] = totals.get(fill.order_id, Decimal("0")) + fill.quantity
    for order in orders:
        if totals.get(order.order_id, Decimal("0")) != order.filled_quantity:
            blockers.add("ctp_order_trade_quantity_mismatch")
    return blockers


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("ascii")


def _field(row: Any, name: str) -> Any:
    if type(row) is not dict or name not in row:
        raise ValueError
    return row[name]


def _require_row_scope(row: Any, binding: CtpQuerySessionBinding) -> None:
    if type(row) is not dict:
        raise ValueError
    for name, expected in (
        ("BrokerID", binding.broker_id),
        ("InvestorID", binding.investor_id),
        ("TradingDay", binding.trading_day),
    ):
        if name in row and _text(row[name]) != expected:
            raise ValueError


def _text(value: Any) -> str:
    if type(value) is bytes:
        value = value.decode("gbk", errors="strict")
    if type(value) is not str or not value:
        raise ValueError
    result = value.strip().strip("\x00").strip()
    if not result or not result.isprintable() or len(result.encode("utf-8", errors="strict")) > 256:
        raise ValueError
    return result


def _decimal(value: Any) -> Decimal:
    if type(value) is bool or value is None or type(value) not in (int, float, str, Decimal):
        raise ValueError
    try:
        number = value if type(value) is Decimal else Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        raise ValueError from None
    if not number.is_finite():
        raise ValueError
    return number


def _optional_decimal(row: Any, name: str) -> Decimal | None:
    if name not in row or row[name] is None:
        return None
    return _decimal(row[name])


def _integer(value: Any) -> int:
    number = _decimal(value)
    if number != number.to_integral_value():
        raise ValueError
    return int(number)


def _order_status(code: str, filled: int, quantity: int) -> str:
    if code == "0":
        if filled != quantity:
            raise ValueError
        return "completed"
    if code == "1":
        if filled <= 0 or filled >= quantity:
            raise ValueError
        return "partially_filled"
    if code == "2":
        return "canceled"
    if code == "3":
        if filled == quantity:
            raise ValueError
        return "partially_filled" if 0 < filled < quantity else "open"
    if code in {"4", "5"}:
        return "canceled"
    if code in {"a", "b", "c"}:
        return "unknown"
    raise ValueError


def _native_order_identity(row: Any, binding: CtpQuerySessionBinding) -> tuple[Any, ...]:
    exchange_id = _text(_field(row, "ExchangeID"))
    trading_day = _text(_field(row, "TradingDay"))
    if trading_day != binding.trading_day:
        raise ValueError
    system_id = row.get("OrderSysID")
    if system_id not in (None, "", b""):
        return ("exchange_system", exchange_id, trading_day, _text(system_id))
    order_ref = _text(_field(row, "OrderRef"))
    front_id = _integer(_field(row, "FrontID"))
    session_id = _integer(_field(row, "SessionID"))
    if (
        front_id <= 0
        or session_id <= 0
        or len(trading_day) != 8
        or not trading_day.isdigit()
    ):
        raise ValueError
    return (
        "exchange_front_session_ref",
        exchange_id,
        trading_day,
        front_id,
        session_id,
        order_ref,
    )


def _trade_timestamp(row: Any) -> datetime:
    date_text = _text(_field(row, "TradeDate"))
    time_text = _text(_field(row, "TradeTime"))
    return datetime.strptime(date_text + time_text, "%Y%m%d%H:%M:%S").replace(
        tzinfo=_CHINA
    ).astimezone(_UTC)


def _commission(row: Any):
    # CTP trade rows do not consistently carry per-trade commission and its
    # currency. Never infer it from account totals or a rate without currency.
    if "Commission" not in row or "CurrencyID" not in row:
        return None
    from bt_api_base.reconciliation_protocol import CommissionAmount

    amount = _decimal(row["Commission"])
    currency = _text(row["CurrencyID"])
    return CommissionAmount(amount=amount, currency=currency)


__all__ = ["CtpReconciliationAuthority", "CtpReconciliationAuthorityError"]
