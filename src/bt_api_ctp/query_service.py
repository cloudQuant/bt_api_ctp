"""Provider-neutral query snapshots over one sealed CTP trader session.

This service owns the CTP request-family mapping, response validation,
normalization into published ``bt_api_base`` records, and snapshot freshness
leases. Provider-native rows and session identities never cross its boundary.
"""

from __future__ import annotations

import threading
import time
import uuid
import re
from collections import OrderedDict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone
from math import isfinite
from typing import Any

from bt_api_base.account_protocol import AccountBalance, AccountPosition, AccountValuation
from bt_api_base.execution_correlation_protocol import (
    ManagedOrderObservationIssuer,
    OrderCorrelation,
)
from bt_api_base.market_protocol import MarketInstrumentKey
from bt_api_base.query_protocol import (
    MAX_QUERY_BATCH_REQUESTS,
    MAX_QUERY_RECORDS,
    ProviderQueryPort,
    QueryBatchReceipt,
    QueryCompleteness,
    QueryRequest,
    QuerySnapshot,
    QuerySnapshotLease,
)
from bt_api_ctp.account_provider import (
    _RESOURCE,
    _decimal_text,
    _field,
    _integer,
    _project_positions,
    _text,
    _utc_text,
)
from bt_api_ctp.execution_query_projection import (
    CtpExecutionQueryProjector,
    native_hedge_flag,
)
from bt_api_ctp.ctp.client import TraderClient
from bt_api_ctp.instrument import normalize_ctp_instrument
from bt_api_ctp.query import (
    _QUERY_EVIDENCE_MAX_TTL_SECONDS,
    _QUERY_MONOTONIC_CLOCK_DOMAIN,
    _QUERY_SOURCE_SEAL,
    _QuerySource,
    QueryResult,
    _query_records_digest,
)
from bt_api_ctp.registered_sim_session import (
    CtpQuerySessionBinding,
    CtpQuerySessionBindingError,
    capture_query_session,
    validate_query_session,
)

_UTC = timezone.utc
_RESOURCE_MODEL = _RESOURCE
_QUERY_REQUEST_TYPES = {
    "account": "account",
    "account_valuation": "account",
    "positions": "positions",
    "orders": "orders",
    "trades": "trades",
    "instruments": "instruments",
    "commission_rates": "commission_rate",
    "margin_rates": "margin_rate",
    "futures_catalog": "instruments",
    "futures_product_universe": "products",
    "futures_quotes": "depth_market_data",
    "futures_depth": "depth_market_data",
}
_FUTURES_FAMILIES = frozenset(
    {
        "futures_session",
        "futures_catalog",
        "futures_product_universe",
        "futures_quotes",
        "futures_depth",
    }
)
# Per native query cap; the caller's batch deadline still applies. During
# trading hours a full SimNow instrument catalog (~11k rows) can take more
# than 30s to stream, and positions depend on it.
_MAX_QUERY_SECONDS = 90.0
_MAX_RETAINED_SNAPSHOTS = 512
_MAX_RETAINED_RECEIPTS = 128
_MAX_RETAINED_LEASES = 512
_CTP_FUTURE_ROUTE = "CTP___FUTURE"
_EXECUTION_QUERY_METHODS = frozenset({"query_orders_result", "query_trades_result"})
_ORDER_OBSERVATION_ID_PATTERN = re.compile(
    r"^sdk-order-observation-v1-[0-9a-f]{64}$", re.ASCII
)


class CtpProviderQueryError(RuntimeError):
    """Finite, value-free construction error for the CTP query port."""

    _CODES = frozenset(
        {
            "query_client_not_canonical",
            "query_session_not_current",
            "query_session_binding_invalid",
        }
    )

    def __init__(self, code: str) -> None:
        self.code = code if type(code) is str and code in self._CODES else "query_session_binding_invalid"
        super().__init__(self.code)


@dataclass(frozen=True)
class _PreparedQuery:
    request: QueryRequest
    method_name: str | None
    native_filters: Mapping[str, str]
    arguments: Mapping[str, str]
    blocker: str | None = None


@dataclass(frozen=True)
class _SnapshotEvidence:
    snapshot: QuerySnapshot
    results: tuple[tuple[QueryResult[Any], Mapping[str, str]], ...]
    records_digest: str
    expires_monotonic: float
    expires_at_utc: datetime


@dataclass(frozen=True)
class _ReceiptEvidence:
    receipt: QueryBatchReceipt
    snapshot_refs: tuple[str, ...]


@dataclass(frozen=True)
class _LeaseEvidence:
    lease: QuerySnapshotLease
    receipt: QueryBatchReceipt
    snapshot_refs: tuple[str, ...]


class CtpProviderQueryService(ProviderQueryPort):
    """Read-only query port bound to one exact, current CTP trader session.

    ``session_binding`` must be issued by ``capture_query_session`` from a
    trusted SDK/facade environment configuration. It is not constructed from
    a Backtrader request or strategy parameter.
    """

    def __init__(
        self,
        client: TraderClient,
        session_binding: CtpQuerySessionBinding,
        *,
        selected_feed: object | None = None,
        execution_context: object | None = None,
        order_observation_issuer: object | None = None,
    ) -> None:
        if type(client) is not TraderClient:
            raise CtpProviderQueryError("query_client_not_canonical")
        if type(session_binding) is not CtpQuerySessionBinding:
            raise CtpProviderQueryError("query_session_binding_invalid")
        if not validate_query_session(client, session_binding, now_monotonic=time.monotonic()):
            raise CtpProviderQueryError("query_session_not_current")
        self._client = client
        self._session = session_binding
        self._selected_feed: object | None = None
        self._execution_context: object | None = None
        self._observation_issuer: ManagedOrderObservationIssuer | None = None
        self._execution_scope: object | None = None
        self._managed_identity_store: object | None = None
        self._ledger_environment: str | None = None
        self._account_ref: str | None = None
        self._scope_ref: str | None = None
        self._observation_route_ready = self._bind_order_observation_route(
            selected_feed,
            execution_context,
            order_observation_issuer,
        )
        self._execution_query_projector = CtpExecutionQueryProjector(
            session_binding,
            observation_issuer=self._observation_issuer,
            ledger_environment=self._ledger_environment,
            account_ref=self._account_ref,
            scope_ref=self._scope_ref,
            managed_order_resolver=(
                self._resolve_managed_order_reference
                if self._managed_identity_store is not None
                else None
            ),
        )
        self._lock = threading.RLock()
        self._snapshots: OrderedDict[str, _SnapshotEvidence] = OrderedDict()
        self._receipts: OrderedDict[str, _ReceiptEvidence] = OrderedDict()
        self._leases: OrderedDict[str, _LeaseEvidence] = OrderedDict()

    @classmethod
    def from_trusted_runtime(
        cls,
        client: TraderClient,
        *,
        environment: str | None,
        selected_feed: object | None = None,
        execution_context: object | None = None,
        order_observation_issuer: object | None = None,
    ) -> CtpProviderQueryService:
        """Create the service using the runtime's already trusted environment."""

        try:
            binding = capture_query_session(client, environment=environment)
        except CtpQuerySessionBindingError as exc:
            raise CtpProviderQueryError("query_session_not_current") from exc
        return cls(
            client,
            binding,
            selected_feed=selected_feed,
            execution_context=execution_context,
            order_observation_issuer=order_observation_issuer,
        )

    def query_batch(
        self,
        requests: Sequence[QueryRequest],
        *,
        deadline: datetime,
    ) -> QueryBatchReceipt:
        """Read a bounded ordered set of neutral families before ``deadline``."""

        request_tuple = _validate_requests(requests)
        _require_utc(deadline, "deadline")
        with self._lock:
            self._prune(time.monotonic())
            started_at = datetime.now(_UTC)
            started_monotonic = time.monotonic()
            deadline_monotonic = started_monotonic + max(
                0.0, (deadline - started_at).total_seconds()
            )
            if not self._session_current(started_monotonic):
                return self._blocked_receipt(
                    request_tuple,
                    started_at,
                    datetime.now(_UTC),
                    blocker="query_session_not_current",
                )

            prepared = tuple(self._prepare(request) for request in request_tuple)
            futures_items_by_family: dict[str, list[_PreparedQuery]] = {}
            for item in prepared:
                if item.request.family in _FUTURES_FAMILIES:
                    futures_items_by_family.setdefault(item.request.family, []).append(item)
            duplicate_futures_families = {
                family for family, items in futures_items_by_family.items() if len(items) > 1
            }
            needs_instrument_catalog = any(
                item.blocker is None
                and item.request.family
                in {
                    "positions",
                    "open_orders",
                    "orders",
                    "trades",
                    "commission_rates",
                    "margin_rates",
                }
                for item in prepared
            ) or any(
                family in {"futures_catalog", "futures_quotes", "futures_depth"}
                and family not in duplicate_futures_families
                and any(item.blocker is None for item in items)
                for family, items in futures_items_by_family.items()
            )
            catalog_item = next(
                (
                    item
                    for item in prepared
                    if item.blocker is None
                    and item.request.family == "instruments"
                    and not item.request.filters
                ),
                None,
            )
            catalog_result: QueryResult[Any] | None = None
            catalog_blocker: str | None = None
            catalog_types: dict[tuple[str, str], str] = {}
            result_by_ref: dict[str, QueryResult[Any] | None] = {}
            issue_by_ref: dict[str, tuple[QueryCompleteness, str]] = {}
            catalog_expected = {"InstrumentID": ""}
            depth_expected = {"InstrumentID": "", "ExchangeID": ""}
            product_universe_expected = {
                "ProductClass": "1",
                "ExchangeID": "",
                "ProductID": "",
            }

            account_items_by_family: dict[str, list[_PreparedQuery]] = {}
            for item in prepared:
                if item.request.family in {"account", "account_valuation"}:
                    account_items_by_family.setdefault(item.request.family, []).append(item)
            duplicate_account_families = {
                family for family, items in account_items_by_family.items() if len(items) > 1
            }
            for family in duplicate_account_families:
                for item in account_items_by_family[family]:
                    issue_by_ref[item.request.request_ref] = (
                        QueryCompleteness.UNAVAILABLE,
                        "duplicate_account_family_request_unsupported",
                    )

            account_items = [
                item
                for family, items in account_items_by_family.items()
                if family not in duplicate_account_families
                for item in items
                if item.blocker is None
            ]
            account_result: QueryResult[Any] | None = None
            account_blocker: str | None = None
            if account_items:
                account_item = account_items[0]
                account_result, account_blocker = self._query_native(
                    "query_account_result",
                    account_item.arguments,
                    deadline_monotonic,
                )
                if account_result is not None and account_blocker is None:
                    if self._validate_native_result(
                        account_result,
                        family="account",
                        expected_filters=account_item.native_filters,
                        deadline_monotonic=deadline_monotonic,
                    ) is None:
                        account_blocker = "query_source_untrusted"
                for item in account_items:
                    request_ref = item.request.request_ref
                    result_by_ref[request_ref] = account_result
                    if account_blocker is not None:
                        issue_by_ref[request_ref] = (
                            self._failure_completeness(account_result, account_blocker),
                            account_blocker,
                        )

            for family in duplicate_futures_families:
                for item in futures_items_by_family[family]:
                    if item.blocker is None:
                        issue_by_ref[item.request.request_ref] = (
                            QueryCompleteness.UNAVAILABLE,
                            "duplicate_family_request_unsupported",
                        )

            if needs_instrument_catalog:
                if catalog_item is None:
                    catalog_result, catalog_blocker = self._query_native(
                        "query_instruments_result",
                        {"instrument_id": "", "exchange_id": "", "product_id": ""},
                        deadline_monotonic,
                    )
                else:
                    catalog_result, catalog_blocker = self._query_native(
                        catalog_item.method_name or "",
                        catalog_item.arguments,
                        deadline_monotonic,
                    )
                    result_by_ref[catalog_item.request.request_ref] = catalog_result
                    if catalog_blocker is not None:
                        issue_by_ref[catalog_item.request.request_ref] = (
                            self._failure_completeness(catalog_result, catalog_blocker),
                            catalog_blocker,
                        )
                if catalog_result is not None and catalog_blocker is None:
                    facts = self._validate_native_result(
                        catalog_result,
                        family="instruments",
                        expected_filters=catalog_expected,
                        deadline_monotonic=deadline_monotonic,
                    )
                    if facts is None:
                        catalog_blocker = "query_source_untrusted"
                    else:
                        catalog_types = _instrument_types(catalog_result.records)

            needs_futures_depth = any(
                family in {"futures_quotes", "futures_depth"}
                and family not in duplicate_futures_families
                and any(item.blocker is None for item in items)
                for family, items in futures_items_by_family.items()
            )
            futures_depth_result: QueryResult[Any] | None = None
            futures_depth_blocker: str | None = None
            if needs_futures_depth:
                futures_depth_result, futures_depth_blocker = self._query_native(
                    "query_depth_market_data_result",
                    {"instrument_id": "", "exchange_id": ""},
                    deadline_monotonic,
                )

            needs_product_universe = (
                len(futures_items_by_family.get("futures_product_universe", ())) == 1
                and "futures_product_universe" not in duplicate_futures_families
                and futures_items_by_family["futures_product_universe"][0].blocker is None
            )
            product_universe_result: QueryResult[Any] | None = None
            product_universe_blocker: str | None = None
            if needs_product_universe:
                product_universe_result, product_universe_blocker = self._query_native(
                    "query_products_result",
                    {"product_class": "1", "exchange_id": "", "product_id": ""},
                    deadline_monotonic,
                )

            for family, items in futures_items_by_family.items():
                if len(items) != 1:
                    continue
                item = items[0]
                request_ref = item.request.request_ref
                if item.blocker is not None:
                    issue_by_ref[request_ref] = (
                        QueryCompleteness.UNAVAILABLE,
                        item.blocker,
                    )
                elif family == "futures_session":
                    continue
                elif family == "futures_catalog":
                    result_by_ref[request_ref] = catalog_result
                    if catalog_blocker is not None:
                        issue_by_ref[request_ref] = (
                            self._failure_completeness(catalog_result, catalog_blocker),
                            catalog_blocker,
                        )
                elif family in {"futures_quotes", "futures_depth"}:
                    result_by_ref[request_ref] = futures_depth_result
                    if futures_depth_blocker is not None:
                        issue_by_ref[request_ref] = (
                            self._failure_completeness(
                                futures_depth_result,
                                futures_depth_blocker,
                            ),
                            futures_depth_blocker,
                        )
                elif family == "futures_product_universe":
                    result_by_ref[request_ref] = product_universe_result
                    if product_universe_blocker is not None:
                        issue_by_ref[request_ref] = (
                            self._failure_completeness(
                                product_universe_result,
                                product_universe_blocker,
                            ),
                            product_universe_blocker,
                        )

            for item in prepared:
                request_ref = item.request.request_ref
                if item.blocker is not None:
                    issue_by_ref[request_ref] = (QueryCompleteness.UNAVAILABLE, item.blocker)
                    continue
                if request_ref in issue_by_ref:
                    continue
                if request_ref in result_by_ref:
                    continue
                if item.request.family in _FUTURES_FAMILIES:
                    # The session row is derived from the sealed binding;
                    # futures catalog/depth data was collected once above.
                    continue
                if (
                    needs_instrument_catalog
                    and catalog_item is not None
                    and item.request.family == "instruments"
                    and not item.request.filters
                ):
                    result_by_ref[request_ref] = catalog_result
                    if catalog_blocker is not None:
                        issue_by_ref[request_ref] = (
                            self._failure_completeness(catalog_result, catalog_blocker),
                            catalog_blocker,
                        )
                    continue
                if item.request.family in {
                    "positions",
                    "open_orders",
                    "orders",
                    "trades",
                    "commission_rates",
                    "margin_rates",
                }:
                    if self._session.environment is None:
                        issue_by_ref[request_ref] = (
                            QueryCompleteness.UNAVAILABLE,
                            "neutral_environment_unavailable",
                        )
                        continue
                    if catalog_blocker is not None:
                        issue_by_ref[request_ref] = (
                            QueryCompleteness.UNAVAILABLE,
                            "instrument_reference_unavailable",
                        )
                        continue
                if item.request.family == "instruments" and self._session.environment is None:
                    issue_by_ref[request_ref] = (
                        QueryCompleteness.UNAVAILABLE,
                        "neutral_environment_unavailable",
                    )
                    continue
                result, blocker = self._query_native(
                    item.method_name or "",
                    item.arguments,
                    deadline_monotonic,
                )
                result_by_ref[request_ref] = result
                if blocker is not None:
                    issue_by_ref[request_ref] = (
                        self._failure_completeness(result, blocker),
                        blocker,
                    )

            future_requests_by_family: dict[str, QueryRequest] = {}
            for family, items in futures_items_by_family.items():
                if len(items) == 1 and items[0].blocker is None:
                    if items[0].request.request_ref not in issue_by_ref:
                        future_requests_by_family[family] = items[0].request

            future_projection_by_family: Mapping[str, Any] = {}
            if future_requests_by_family:
                catalog_source_valid = (
                    catalog_result is not None
                    and catalog_blocker is None
                    and self._validate_native_result(
                        catalog_result,
                        family="instruments",
                        expected_filters=catalog_expected,
                        deadline_monotonic=deadline_monotonic,
                    )
                    is not None
                )
                futures_depth_source_valid = (
                    futures_depth_result is not None
                    and futures_depth_blocker is None
                    and self._validate_native_result(
                        futures_depth_result,
                        family="futures_quotes",
                        expected_filters=depth_expected,
                        deadline_monotonic=deadline_monotonic,
                    )
                    is not None
                )
                product_universe_source_valid = (
                    product_universe_result is not None
                    and product_universe_blocker is None
                    and self._validate_native_result(
                        product_universe_result,
                        family="futures_product_universe",
                        expected_filters=product_universe_expected,
                        deadline_monotonic=deadline_monotonic,
                    )
                    is not None
                )
                if (
                    "futures_product_universe" in future_requests_by_family
                    and product_universe_result is not None
                    and product_universe_blocker is None
                    and not product_universe_source_valid
                ):
                    request = future_requests_by_family["futures_product_universe"]
                    issue_by_ref[request.request_ref] = (
                        QueryCompleteness.UNKNOWN,
                        "query_source_untrusted",
                    )
                if catalog_result is not None and catalog_blocker is None and not catalog_source_valid:
                    for family in ("futures_catalog", "futures_quotes", "futures_depth"):
                        request = future_requests_by_family.get(family)
                        if request is not None:
                            issue_by_ref[request.request_ref] = (
                                QueryCompleteness.UNKNOWN,
                                "query_source_untrusted",
                            )
                elif catalog_blocker is not None:
                    catalog_status = self._failure_completeness(
                        catalog_result,
                        catalog_blocker,
                    )
                    for family in ("futures_catalog", "futures_quotes", "futures_depth"):
                        request = future_requests_by_family.get(family)
                        if request is not None and family != "futures_session":
                            issue_by_ref[request.request_ref] = (
                                catalog_status,
                                "instrument_reference_unavailable",
                            )
                if (
                    futures_depth_result is not None
                    and futures_depth_blocker is None
                    and not futures_depth_source_valid
                ):
                    for family in ("futures_quotes", "futures_depth"):
                        request = future_requests_by_family.get(family)
                        if request is not None:
                            issue_by_ref[request.request_ref] = (
                                QueryCompleteness.UNKNOWN,
                                "query_source_untrusted",
                            )
                future_requests_by_family = {
                    family: request
                    for family, request in future_requests_by_family.items()
                    if request.request_ref not in issue_by_ref
                }
                futures_results_by_family: dict[str, QueryResult[Any] | None] = {}
                for family in future_requests_by_family:
                    if family == "futures_session":
                        futures_results_by_family[family] = None
                    elif family == "futures_product_universe":
                        futures_results_by_family[family] = (
                            product_universe_result if product_universe_source_valid else None
                        )
                    elif family == "futures_catalog":
                        futures_results_by_family[family] = (
                            catalog_result if catalog_source_valid else None
                        )
                    elif family in {"futures_quotes", "futures_depth"}:
                        futures_results_by_family[family] = (
                            futures_depth_result if futures_depth_source_valid else None
                        )
                # Quotes/depth need a full instrument catalog even if the
                # caller requests only one of those families. This is an
                # internal evidence dependency, not an extra neutral snapshot.
                if any(
                    family in {"futures_quotes", "futures_depth"}
                    for family in future_requests_by_family
                ):
                    futures_results_by_family["futures_catalog"] = (
                        catalog_result if catalog_source_valid else None
                    )
                try:
                    from bt_api_ctp.futures_market_projection import (
                        CtpFuturesFamilyProjection,
                        project_futures_market_batch,
                    )

                    projected = project_futures_market_batch(
                        future_requests_by_family,
                        futures_results_by_family,
                        self._session,
                    )
                    if not isinstance(projected, Mapping):
                        raise TypeError("future projection result must be a mapping")
                    future_projection_by_family = projected
                    for family, request in future_requests_by_family.items():
                        projection = projected.get(family)
                        if type(projection) is not CtpFuturesFamilyProjection:
                            issue_by_ref[request.request_ref] = (
                                QueryCompleteness.UNKNOWN,
                                "futures_projection_result_untrusted",
                            )
                except Exception:
                    for request in future_requests_by_family.values():
                        issue_by_ref[request.request_ref] = (
                            QueryCompleteness.UNKNOWN,
                            "futures_projection_failed",
                        )

            now_monotonic = time.monotonic()
            session_still_current = self._session_current(now_monotonic)
            completed_at = datetime.now(_UTC)
            batch_blockers: list[str] = []
            if not session_still_current:
                batch_blockers.append("query_session_changed")
            if now_monotonic >= deadline_monotonic or completed_at > deadline:
                batch_blockers.append("query_batch_deadline_exceeded")

            snapshots: list[QuerySnapshot] = []
            snapshot_evidence: list[_SnapshotEvidence] = []
            for item in prepared:
                request = item.request
                request_ref = request.request_ref
                if not session_still_current:
                    snapshot = self._unknown_snapshot(request, "query_session_changed")
                    snapshots.append(snapshot)
                    continue
                issue = issue_by_ref.get(request_ref)
                if issue is not None:
                    status, code = issue
                    snapshot = self._empty_snapshot(request, status, code)
                    snapshots.append(snapshot)
                    continue
                if request.family in _FUTURES_FAMILIES:
                    projection = future_projection_by_family.get(request.family)
                    if projection is None:
                        snapshot = self._empty_snapshot(
                            request,
                            QueryCompleteness.UNAVAILABLE,
                            "futures_projection_unavailable",
                        )
                        snapshots.append(snapshot)
                        continue
                    if request.family == "futures_session":
                        dependencies: tuple[
                            tuple[QueryResult[Any], Mapping[str, str]], ...
                        ] = ()
                    elif request.family == "futures_catalog":
                        dependencies = (
                            ((catalog_result, catalog_expected),)
                            if catalog_result is not None
                            else ()
                        )
                    elif request.family == "futures_product_universe":
                        dependencies = (
                            ((product_universe_result, product_universe_expected),)
                            if product_universe_result is not None
                            else ()
                        )
                    else:
                        dependency_rows: list[
                            tuple[QueryResult[Any], Mapping[str, str]]
                        ] = []
                        if futures_depth_result is not None:
                            dependency_rows.append((futures_depth_result, depth_expected))
                        if catalog_result is not None:
                            dependency_rows.append((catalog_result, catalog_expected))
                        dependencies = tuple(dependency_rows)

                    native_valid = all(
                        self._validate_native_result(
                            dependency,
                            family=_family_for_native_type(dependency.request_type),
                            expected_filters=expected,
                            deadline_monotonic=deadline_monotonic,
                        )
                        is not None
                        for dependency, expected in dependencies
                    )
                    if not native_valid:
                        snapshot = self._unknown_snapshot(
                            request,
                            "query_source_untrusted",
                        )
                        snapshots.append(snapshot)
                        continue
                    snapshot, evidence = self._project_futures_snapshot(
                        request,
                        projection,
                        dependencies,
                        started_at=started_at,
                        completed_at=completed_at,
                    )
                    snapshots.append(snapshot)
                    if evidence is not None:
                        snapshot_evidence.append(evidence)
                    continue
                result = result_by_ref.get(request_ref)
                if result is None:
                    snapshot = self._empty_snapshot(
                        request, QueryCompleteness.UNAVAILABLE, "query_not_dispatched"
                    )
                    snapshots.append(snapshot)
                    continue

                dependencies: tuple[tuple[QueryResult[Any], Mapping[str, str]], ...]
                if request.family in {
                    "positions",
                    "open_orders",
                    "orders",
                    "trades",
                    "commission_rates",
                    "margin_rates",
                }:
                    if catalog_result is None:
                        snapshot = self._empty_snapshot(
                            request,
                            QueryCompleteness.UNAVAILABLE,
                            "instrument_reference_unavailable",
                        )
                        snapshots.append(snapshot)
                        continue
                    dependencies = (
                        (result, item.native_filters),
                        (catalog_result, catalog_expected),
                    )
                else:
                    dependencies = ((result, item.native_filters),)

                native_valid = all(
                    self._validate_native_result(
                        dependency,
                        family=(
                            ("orders" if request.family == "open_orders" else request.family)
                            if dependency is result
                            else _family_for_native_type(dependency.request_type)
                        ),
                        expected_filters=expected,
                        deadline_monotonic=deadline_monotonic,
                        expected_explicit_filters=(
                            ("HedgeFlag",)
                            if request.family == "margin_rates" and dependency is result
                            else ()
                        ),
                    )
                    is not None
                    for dependency, expected in dependencies
                )
                if not native_valid:
                    snapshot = self._unknown_snapshot(request, "query_source_untrusted")
                    snapshots.append(snapshot)
                    continue

                snapshot, evidence = self._project_snapshot(
                    request,
                    result,
                    dependencies,
                    catalog_types,
                )
                snapshots.append(snapshot)
                if evidence is not None:
                    snapshot_evidence.append(evidence)

            if not session_still_current:
                snapshot_evidence.clear()
            receipt = QueryBatchReceipt(
                batch_ref="ctp-query-batch:" + uuid.uuid4().hex,
                source_ref=self._session.source_ref,
                generation=self._session.generation,
                started_at=started_at,
                completed_at=max(started_at, completed_at),
                requests=request_tuple,
                snapshots=tuple(snapshots),
                blockers=tuple(batch_blockers),
            )
            for evidence in snapshot_evidence:
                if evidence.snapshot.completeness is QueryCompleteness.COMPLETE:
                    self._remember_snapshot(evidence)
            self._remember_receipt(receipt)
            return receipt

    def acquire_snapshot_lease(
        self,
        receipt: QueryBatchReceipt,
        *,
        purpose: str,
        expires_at: datetime,
    ) -> QuerySnapshotLease:
        """Lease only complete snapshots issued by this current CTP port."""

        if type(receipt) is not QueryBatchReceipt:
            raise TypeError("receipt must be a QueryBatchReceipt")
        _require_utc(expires_at, "expires_at")
        if type(purpose) is not str or not purpose or purpose != purpose.strip():
            raise ValueError("purpose must be non-empty trimmed text")
        with self._lock:
            now_utc = datetime.now(_UTC)
            now_monotonic = time.monotonic()
            self._prune(now_monotonic)
            evidence = self._receipts.get(receipt.receipt_digest)
            if (
                evidence is None
                or receipt != evidence.receipt
                or not receipt.complete
                or receipt.source_ref != self._session.source_ref
                or receipt.generation != self._session.generation
                or not self._session_current(now_monotonic)
            ):
                raise ValueError("query_receipt_not_current_or_complete")
            refs = receipt.complete_snapshot_refs
            if not refs or refs != evidence.snapshot_refs:
                raise ValueError("query_receipt_snapshot_set_invalid")
            snapshots = [self._snapshots.get(ref) for ref in refs]
            if any(snapshot is None for snapshot in snapshots):
                raise ValueError("query_snapshot_evidence_expired")
            typed_snapshots = [snapshot for snapshot in snapshots if snapshot is not None]
            if any(not self._evidence_current(snapshot, now_monotonic) for snapshot in typed_snapshots):
                raise ValueError("query_snapshot_evidence_expired")
            receipt_snapshots = {
                snapshot.snapshot_ref: snapshot
                for snapshot in receipt.snapshots
                if snapshot.snapshot_ref is not None
            }
            if any(
                receipt_snapshots.get(snapshot.snapshot.snapshot_ref) != snapshot.snapshot
                for snapshot in typed_snapshots
            ):
                raise ValueError("query_receipt_snapshot_evidence_mismatch")
            min_expiry = min(snapshot.expires_at_utc for snapshot in typed_snapshots)
            if expires_at <= now_utc or expires_at > min_expiry:
                raise ValueError("query_lease_expiry_outside_evidence_window")
            lease = QuerySnapshotLease(
                lease_ref="ctp-query-lease:" + uuid.uuid4().hex,
                receipt_digest=receipt.receipt_digest,
                source_ref=self._session.source_ref,
                generation=self._session.generation,
                snapshot_refs=refs,
                issued_at=now_utc,
                expires_at=expires_at,
                purpose=purpose,
            )
            self._leases[lease.lease_ref] = _LeaseEvidence(lease, receipt, refs)
            while len(self._leases) > _MAX_RETAINED_LEASES:
                self._leases.popitem(last=False)
            return lease

    def validate_lease(
        self,
        lease: QuerySnapshotLease,
        receipt: QueryBatchReceipt,
        *,
        now: datetime,
    ) -> bool:
        """Revalidate provider receipt identity, query seals, session and expiry."""

        if type(lease) is not QuerySnapshotLease or type(receipt) is not QueryBatchReceipt:
            raise TypeError("lease and receipt must use the published query protocol types")
        _require_utc(now, "now")
        with self._lock:
            now_monotonic = time.monotonic()
            self._prune(now_monotonic)
            evidence = self._leases.get(lease.lease_ref)
            receipt_evidence = self._receipts.get(receipt.receipt_digest)
            if (
                evidence is None
                or evidence.lease != lease
                or evidence.receipt != receipt
                or receipt_evidence is None
                or receipt_evidence.receipt != receipt
                or not receipt.complete
                or now < lease.issued_at
                or now >= lease.expires_at
                or lease.receipt_digest != receipt.receipt_digest
                or lease.source_ref != self._session.source_ref
                or lease.generation != self._session.generation
                or lease.snapshot_refs != receipt.complete_snapshot_refs
                or not self._session_current(now_monotonic)
            ):
                return False
            for ref in lease.snapshot_refs:
                snapshot = self._snapshots.get(ref)
                if snapshot is None or not self._evidence_current(snapshot, now_monotonic):
                    return False
                if not any(
                    item.snapshot_ref == ref and item == snapshot.snapshot
                    for item in receipt.snapshots
                ):
                    return False
                if snapshot.expires_at_utc < lease.expires_at:
                    return False
            return True

    def _bind_order_observation_route(
        self,
        selected_feed: object | None,
        execution_context: object | None,
        observation_issuer: object | None,
    ) -> bool:
        """Retain only an exact SDK runtime scope and neutral observation issuer."""

        try:
            from bt_api_ctp.feeds.live_ctp_feed import (
                CtpRequestDataFuture,
                _CtpRuntimeOwnedRequestFeedBinding,
            )
            from bt_api_ctp.order_provider import CtpOrderProviderRuntimeContext
            from bt_api_execution.contracts import ExecutionScope
            from bt_api_execution.store import SqliteExecutionStore
        except Exception:
            return False
        if (
            type(selected_feed) is not CtpRequestDataFuture
            or selected_feed.trader_client is not self._client
            or type(execution_context) is not CtpOrderProviderRuntimeContext
            or not isinstance(observation_issuer, ManagedOrderObservationIssuer)
            or execution_context.owner_thread_invoker is None
            or type(execution_context.runtime_binding) is not _CtpRuntimeOwnedRequestFeedBinding
        ):
            return False
        scope = execution_context.scope
        if (
            type(scope) is not ExecutionScope
            or scope.provider != "ctp"
            or type(scope.environment) is not str
            or not scope.environment
            or type(scope.account_ref) is not str
            or not scope.account_ref
            or type(scope.key) is not str
            or not scope.key
            or type(scope.trading_day) is not str
            or scope.trading_day != self._session.trading_day
        ):
            return False
        store = execution_context.execution_store
        if store is not None and type(store) is not SqliteExecutionStore:
            store = None
        self._selected_feed = selected_feed
        self._execution_context = execution_context
        self._observation_issuer = observation_issuer
        self._execution_scope = scope
        self._managed_identity_store = store
        self._ledger_environment = scope.environment
        self._account_ref = scope.account_ref
        self._scope_ref = scope.key
        return True

    def _assert_execution_route_current(self) -> None:
        context = self._execution_context
        feed = self._selected_feed
        scope = self._execution_scope
        if (
            not self._observation_route_ready
            or context is None
            or feed is None
            or scope is None
            or context.scope is not scope
            or context.owner_thread_invoker is None
        ):
            raise RuntimeError("query_execution_route_unavailable")
        try:
            from bt_api_ctp.feeds.live_ctp_feed import _CtpRuntimeOwnedRequestFeedBinding

            runtime_binding = context.runtime_binding
            valid = (
                type(runtime_binding) is _CtpRuntimeOwnedRequestFeedBinding
                and feed._connected is True
                and feed.trader_client is self._client
                and runtime_binding.validate_provider_context(
                    context,
                    exchange_name=_CTP_FUTURE_ROUTE,
                    selected_feed=feed,
                    trader_client=self._client,
                )
            )
        except Exception:
            valid = False
        if type(valid) is not bool or valid is not True:
            raise RuntimeError("query_execution_route_changed")
        if not self._session_current(time.monotonic()):
            raise RuntimeError("query_session_changed")

    def _call_execution_owner(self, callback: Any) -> Any:
        context = self._execution_context
        if context is None or context.owner_thread_invoker is None:
            raise RuntimeError("query_execution_owner_unavailable")

        def checked() -> Any:
            self._assert_execution_route_current()
            return callback()

        return context.owner_thread_invoker.call_on_owner_thread(
            checked,
            timeout_seconds=float(context.owner_call_timeout_seconds),
        )

    def _resolve_managed_order_reference(
        self,
        order_ref: str,
        instrument: MarketInstrumentKey,
    ) -> tuple[OrderCorrelation, object] | None:
        store = self._managed_identity_store
        scope = self._execution_scope
        issuer = self._observation_issuer
        environment = self._ledger_environment
        account_ref = self._account_ref
        scope_ref = self._scope_ref
        if (
            store is None
            or scope is None
            or issuer is None
            or environment is None
            or account_ref is None
            or scope_ref is None
        ):
            return None
        if (
            type(order_ref) is not str
            or len(order_ref) != 12
            or not order_ref.isascii()
            or not order_ref.isdigit()
        ):
            return None

        def resolve() -> tuple[OrderCorrelation, object] | None:
            self._assert_execution_route_current()
            identity = store.read_ctp_order_identity_by_order_ref(scope, order_ref)
            if identity is None:
                return None
            from bt_api_execution.store import CtpOrderIdentityReservation

            if (
                type(identity) is not CtpOrderIdentityReservation
                or identity.account_key != scope.account_key
                or identity.trading_day != scope.trading_day
                or identity.trading_day != self._session.trading_day
                or identity.scope_key != scope_ref
                or identity.order_ref != order_ref
                or type(identity.managed_intent_id) is not str
                or not identity.managed_intent_id
                or type(identity.runtime_order_id) is not str
                or not identity.runtime_order_id
            ):
                raise RuntimeError("query_managed_order_identity_invalid")
            resolved = issuer.resolve_provider_order_reference(
                identity.managed_intent_id,
                provider_name=_CTP_FUTURE_ROUTE,
                environment=environment,
                account_ref=account_ref,
                scope_ref=scope_ref,
            )
            if resolved is None:
                raise RuntimeError("query_managed_order_reference_unresolved")
            if (
                type(resolved) is not tuple
                or len(resolved) != 2
                or type(resolved[0]) is not OrderCorrelation
                or resolved[0].exchange_name != _CTP_FUTURE_ROUTE
                or resolved[0].symbol != instrument.instrument_id
            ):
                raise RuntimeError("query_managed_order_reference_invalid")
            return resolved

        return self._call_execution_owner(resolve)

    def _prepare(self, request: QueryRequest) -> _PreparedQuery:
        family = request.family
        filters = request.filters
        if family == "open_orders":
            if not self._observation_route_ready:
                return _PreparedQuery(
                    request,
                    None,
                    {},
                    {},
                    "open_order_identity_route_unavailable",
                )
            allowed = {
                "symbol",
                "instrument_id",
                "exchange",
                "trading_day",
                "provider_order_id",
            }
            if set(filters) - allowed:
                return _PreparedQuery(request, None, {}, {}, "unsupported_query_filter")
            symbol, issue = _text_alias(filters, "symbol", "instrument_id")
            if issue is not None:
                return _PreparedQuery(request, None, {}, {}, issue)
            exchange, issue = _optional_filter_text(filters, "exchange")
            if issue is not None:
                return _PreparedQuery(request, None, {}, {}, issue)
            trading_day, issue = _optional_filter_text(filters, "trading_day")
            if issue is not None:
                return _PreparedQuery(request, None, {}, {}, issue)
            if trading_day is not None and trading_day != self._session.trading_day:
                return _PreparedQuery(
                    request,
                    None,
                    {},
                    {},
                    "requested_trading_day_mismatch_or_unavailable",
                )
            provider_order_id = filters.get("provider_order_id")
            if provider_order_id is not None and (
                type(provider_order_id) is not str
                or not _ORDER_OBSERVATION_ID_PATTERN.fullmatch(provider_order_id)
            ):
                return _PreparedQuery(
                    request,
                    None,
                    {},
                    {},
                    "provider_order_id_invalid",
                )
            arguments = {
                "instrument_id": symbol or "",
                "exchange_id": exchange or "",
                "order_sys_id": "",
            }
            native = {
                "BrokerID": self._session.broker_id,
                "InvestorID": self._session.investor_id,
                "InstrumentID": arguments["instrument_id"],
                "ExchangeID": arguments["exchange_id"],
                "OrderSysID": "",
            }
            return _PreparedQuery(
                request,
                "query_orders_result",
                native,
                arguments,
            )
        if family in _FUTURES_FAMILIES:
            blocker = _validate_futures_filter_shape(family, filters)
            if blocker is not None:
                return _PreparedQuery(request, None, {}, {}, blocker)
            if self._session.environment is None:
                return _PreparedQuery(
                    request, None, {}, {}, "neutral_environment_unavailable"
                )
            if filters.get("environment") != self._session.environment:
                return _PreparedQuery(
                    request, None, {}, {}, "requested_environment_mismatch"
                )
            if family == "futures_session":
                return _PreparedQuery(request, None, {}, {}, None)
            if family == "futures_product_universe":
                if request.required is not True or request.require_complete_scope is not True:
                    return _PreparedQuery(
                        request, None, {}, {}, "futures_complete_scope_required"
                    )
                return _PreparedQuery(
                    request,
                    "query_products_result",
                    {"ProductClass": "1", "ExchangeID": "", "ProductID": ""},
                    {"product_class": "1", "exchange_id": "", "product_id": ""},
                )
            if family == "futures_catalog":
                return _PreparedQuery(
                    request,
                    "query_instruments_result",
                    {"InstrumentID": ""},
                    {"instrument_id": "", "exchange_id": "", "product_id": ""},
                )
            if family == "futures_depth" and filters["depth_levels"] > 5:
                return _PreparedQuery(
                    request,
                    None,
                    {},
                    {},
                    "ctp_depth_levels_exceed_provider_limit",
                )
            if family in {"futures_quotes", "futures_depth"}:
                return _PreparedQuery(
                    request,
                    "query_depth_market_data_result",
                    {"InstrumentID": "", "ExchangeID": ""},
                    {"instrument_id": "", "exchange_id": ""},
                )

        if family not in _QUERY_REQUEST_TYPES:
            return _PreparedQuery(request, None, {}, {}, "unsupported_family_schema")

        if family in {"account", "account_valuation"}:
            if filters:
                return _PreparedQuery(request, None, {}, {}, "unsupported_query_filter")
            if family == "account_valuation" and request.require_complete_scope is not True:
                return _PreparedQuery(
                    request,
                    None,
                    {},
                    {},
                    "account_valuation_complete_scope_required",
                )
            return _PreparedQuery(
                request,
                "query_account_result",
                {"BrokerID": self._session.broker_id, "InvestorID": self._session.investor_id},
                {},
            )

        if family == "positions":
            if self._session.environment is None:
                return _PreparedQuery(request, None, {}, {}, "neutral_environment_unavailable")
            if filters:
                return _PreparedQuery(request, None, {}, {}, "unsupported_query_filter")
            return _PreparedQuery(
                request,
                "query_positions_result",
                {"BrokerID": self._session.broker_id, "InvestorID": self._session.investor_id},
                {},
            )

        if family in {"orders", "trades", "commission_rates", "margin_rates"}:
            if self._session.environment is None:
                return _PreparedQuery(request, None, {}, {}, "neutral_environment_unavailable")
            if family in {"orders", "trades"} and not self._observation_route_ready:
                return _PreparedQuery(
                    request,
                    None,
                    {},
                    {},
                    "execution_observation_issuer_unavailable",
                )
            allowed = {"symbol", "instrument_id", "exchange", "trading_day"}
            if family == "margin_rates":
                allowed.add("hedge_class")
            if set(filters) - allowed:
                return _PreparedQuery(request, None, {}, {}, "unsupported_query_filter")
            symbol, issue = _text_alias(filters, "symbol", "instrument_id")
            if issue is not None:
                return _PreparedQuery(request, None, {}, {}, issue)
            exchange, issue = _optional_filter_text(filters, "exchange")
            if issue is not None:
                return _PreparedQuery(request, None, {}, {}, issue)
            trading_day, issue = _optional_filter_text(filters, "trading_day")
            if issue is not None:
                return _PreparedQuery(request, None, {}, {}, issue)
            if trading_day is not None and trading_day != self._session.trading_day:
                return _PreparedQuery(
                    request,
                    None,
                    {},
                    {},
                    "requested_trading_day_mismatch_or_unavailable",
                )
            if family in {"commission_rates", "margin_rates"} and (not symbol or not exchange):
                return _PreparedQuery(
                    request, None, {}, {}, "rate_schedule_instrument_scope_required"
                )
            arguments = {
                "instrument_id": symbol or "",
                "exchange_id": exchange or "",
            }
            native = {
                "BrokerID": self._session.broker_id,
                "InvestorID": self._session.investor_id,
                "InstrumentID": arguments["instrument_id"],
                "ExchangeID": arguments["exchange_id"],
            }
            if family == "orders":
                arguments["order_sys_id"] = ""
                native["OrderSysID"] = ""
                return _PreparedQuery(request, "query_orders_result", native, arguments)
            if family == "trades":
                arguments.update({"trade_id": "", "start_time": "", "end_time": ""})
                native.update(
                    {"TradeID": "", "TradeTimeStart": "", "TradeTimeEnd": ""}
                )
                return _PreparedQuery(request, "query_trades_result", native, arguments)
            if family == "commission_rates":
                return _PreparedQuery(
                    request,
                    "query_instrument_commission_rate_result",
                    native,
                    arguments,
                )

            hedge_class = filters.get("hedge_class")
            if type(hedge_class) is not str or hedge_class not in {
                "speculation",
                "arbitrage",
                "hedge",
            }:
                return _PreparedQuery(request, None, {}, {}, "margin_hedge_class_unavailable")
            hedge_flag = native_hedge_flag(hedge_class)
            if hedge_flag is None:
                return _PreparedQuery(request, None, {}, {}, "margin_hedge_class_unsupported")
            native["HedgeFlag"] = hedge_flag
            arguments["hedge_flag"] = hedge_flag
            return _PreparedQuery(
                request,
                "query_instrument_margin_rate_result",
                native,
                arguments,
            )

        if family == "instruments":
            if self._session.environment is None:
                return _PreparedQuery(request, None, {}, {}, "neutral_environment_unavailable")
            allowed = {"symbol", "instrument_id", "exchange", "product_id"}
            if set(filters) - allowed:
                return _PreparedQuery(request, None, {}, {}, "unsupported_query_filter")
            symbol, issue = _text_alias(filters, "symbol", "instrument_id")
            if issue is not None:
                return _PreparedQuery(request, None, {}, {}, issue)
            exchange, issue = _optional_filter_text(filters, "exchange")
            if issue is not None:
                return _PreparedQuery(request, None, {}, {}, issue)
            product_id, issue = _optional_filter_text(filters, "product_id")
            if issue is not None:
                return _PreparedQuery(request, None, {}, {}, issue)
            arguments = {
                "instrument_id": symbol or "",
                "exchange_id": exchange or "",
                "product_id": product_id or "",
            }
            native = {"InstrumentID": arguments["instrument_id"]}
            if arguments["exchange_id"]:
                native["ExchangeID"] = arguments["exchange_id"]
            if arguments["product_id"]:
                native["ProductID"] = arguments["product_id"]
            return _PreparedQuery(request, "query_instruments_result", native, arguments)

        return _PreparedQuery(request, None, {}, {}, "unsupported_family_schema")

    def _query_native(
        self,
        method_name: str,
        arguments: Mapping[str, str],
        deadline_monotonic: float,
    ) -> tuple[QueryResult[Any] | None, str | None]:
        now = time.monotonic()
        if now >= deadline_monotonic:
            return None, "query_deadline_elapsed_before_dispatch"
        method = getattr(self._client, method_name, None)
        canonical_method = getattr(TraderClient, method_name, None)
        if (
            not callable(method)
            or not callable(canonical_method)
            or getattr(method, "__self__", None) is not self._client
            or getattr(method, "__func__", None) is not canonical_method
        ):
            return None, "query_method_unavailable"
        query_deadline = min(deadline_monotonic, now + _MAX_QUERY_SECONDS)
        remaining = query_deadline - now
        if remaining <= 0:
            return None, "query_deadline_elapsed_before_dispatch"
        kwargs: dict[str, Any] = dict(arguments)
        kwargs["timeout"] = remaining
        kwargs["deadline_monotonic"] = query_deadline
        if method_name in {
            "query_instruments_result",
            "query_depth_market_data_result",
            "query_products_result",
        }:
            kwargs["max_duration"] = remaining
        if not self._session_current(time.monotonic()):
            return None, "query_session_not_current"
        try:
            if method_name in _EXECUTION_QUERY_METHODS:
                result = self._call_execution_owner(lambda: method(**kwargs))
            else:
                result = method(**kwargs)
        except Exception:
            # A raised exception may have occurred after the provider request
            # was entered. Never reinterpret it as a safe retry instruction.
            return None, "query_call_outcome_unknown"
        if type(result) is not QueryResult:
            return None, "query_result_type_untrusted"
        if result.unsupported is True:
            return result, "query_unsupported"
        if result.complete is not True:
            if result.completion_origin in {
                "local_preflight",
                "response_error",
                "submit_rejected",
            }:
                return result, "query_definitely_unavailable"
            return result, "query_result_incomplete"
        return result, None

    def _validate_native_result(
        self,
        result: QueryResult[Any],
        *,
        family: str,
        expected_filters: Mapping[str, str],
        deadline_monotonic: float | None,
        expected_explicit_filters: tuple[str, ...] = (),
    ) -> _QuerySource | None:
        if (
            type(result) is not QueryResult
            or family not in _QUERY_REQUEST_TYPES
            or result.request_type != _QUERY_REQUEST_TYPES[family]
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
            or result.record_limit is not None
            or result.record_limit_exceeded is not False
            or type(result.request_id) is not int
            or result.request_id <= 0
            or type(result.connection_generation) is not int
            or result.connection_generation != self._session.connection_generation
            or result.account_fingerprint != self._session.account_fingerprint
            or not _utc_datetime(result.started_at_utc)
            or not _utc_datetime(result.completed_at_utc)
            or result.started_at_utc > result.completed_at_utc
            or result.completed_at_utc > datetime.now(_UTC)
            or type(result.records) is not tuple
            or len(result.records) > MAX_QUERY_RECORDS
        ):
            return None
        source = result.query_source
        if type(source) is not _QuerySource or source._seal is not _QUERY_SOURCE_SEAL:
            return None
        now_monotonic = time.monotonic()
        if (
            source.issuer is not self._session._issuer
            or source.source_tags != self._session._source_tags
            or source.request_type != result.request_type
            or source.request_id != result.request_id
            or source.account_fingerprint != self._session.account_fingerprint
            or source.connection_generation != self._session.connection_generation
            or source.trading_day != self._session.trading_day
            or source.broker_id != self._session.broker_id
            or source.investor_id != self._session.investor_id
            or source.started_at_utc != result.started_at_utc
            or source.completed_at_utc != result.completed_at_utc
            or source.clock_domain_id != _QUERY_MONOTONIC_CLOCK_DOMAIN
            or not _finite_number(source.started_monotonic)
            or not _finite_number(source.completed_monotonic)
            or source.started_monotonic > source.completed_monotonic
            or source.completed_monotonic > now_monotonic
            or (
                deadline_monotonic is not None
                and source.completed_monotonic > deadline_monotonic
            )
            or not _finite_number(source.trusted_expires_monotonic)
            or source.trusted_expires_monotonic
            != source.completed_monotonic + _QUERY_EVIDENCE_MAX_TTL_SECONDS
            or now_monotonic >= source.trusted_expires_monotonic
            or not _utc_datetime(source.trusted_expires_at_utc)
            or source.trusted_expires_at_utc
            != source.completed_at_utc + timedelta(seconds=_QUERY_EVIDENCE_MAX_TTL_SECONDS)
            or not _native_filter_map_matches(source.request_intent_filters, expected_filters)
            or not _native_filter_map_matches(source.request_filters, expected_filters)
            or source.explicit_request_filters != expected_explicit_filters
            or source.request_parameters != ()
            or source.request_intent_parameters != ()
            or type(source.records_sha256) is not str
        ):
            return None
        try:
            if _query_records_digest(result.records) != source.records_sha256:
                return None
        except Exception:
            return None
        return source

    def _project_snapshot(
        self,
        request: QueryRequest,
        result: QueryResult[Any],
        dependencies: tuple[tuple[QueryResult[Any], Mapping[str, str]], ...],
        catalog_types: Mapping[tuple[str, str], str],
    ) -> tuple[QuerySnapshot, _SnapshotEvidence | None]:
        source = result.query_source
        assert type(source) is _QuerySource
        observed_at = datetime.now(_UTC)
        if result.completed_at_utc is None or observed_at < result.completed_at_utc:
            return self._unknown_snapshot(request, "query_clock_order_invalid"), None
        observed_text = _utc_text(result.completed_at_utc)
        blockers: list[str] = []
        records: tuple[Mapping[str, Any], ...]
        try:
            if request.family == "account":
                records, projection_blockers = self._project_account(result.records, observed_text)
            elif request.family == "account_valuation":
                records, projection_blockers = self._project_account_valuation(
                    result.records,
                    observed_text,
                )
            elif request.family == "positions":
                records, projection_blockers = self._project_account_positions(
                    result.records, observed_text, catalog_types
                )
            elif request.family == "open_orders":
                records, projection_blockers = (
                    self._execution_query_projector.project_open_orders(
                        result.records,
                        observed_at=observed_text,
                        catalog_types=catalog_types,
                        filters=request.filters,
                    )
                )
            elif request.family == "orders":
                records, projection_blockers = self._execution_query_projector.project_orders(
                    result.records,
                    observed_at=observed_text,
                    catalog_types=catalog_types,
                    filters=request.filters,
                )
            elif request.family == "trades":
                records, projection_blockers = self._execution_query_projector.project_trades(
                    result.records,
                    observed_at=observed_text,
                    catalog_types=catalog_types,
                    filters=request.filters,
                )
            elif request.family == "commission_rates":
                records, projection_blockers = (
                    self._execution_query_projector.project_commission_rates(
                        result.records,
                        catalog_types=catalog_types,
                        filters=request.filters,
                    )
                )
            elif request.family == "margin_rates":
                records, projection_blockers = (
                    self._execution_query_projector.project_margin_rates(
                        result.records,
                        catalog_types=catalog_types,
                        filters=request.filters,
                    )
                )
            elif request.family == "instruments":
                records, projection_blockers = self._project_instruments(result.records)
            else:
                return self._empty_snapshot(
                    request, QueryCompleteness.UNAVAILABLE, "unsupported_family_schema"
                ), None
            blockers.extend(projection_blockers)
        except Exception:
            records = ()
            blockers.append("record_projection_failed")

        if request.family == "account_valuation" and not records:
            completeness = QueryCompleteness.UNAVAILABLE
        elif not records and request.family in {"commission_rates", "margin_rates"} and (
            "rate_schedule_rows_empty" in blockers
        ):
            completeness = QueryCompleteness.UNAVAILABLE
        else:
            completeness = QueryCompleteness.PARTIAL if blockers else QueryCompleteness.COMPLETE
        scope_complete = not blockers
        snapshot_ref = "ctp-query-snapshot:" + uuid.uuid4().hex
        combined_started = min(dependency.started_at_utc for dependency, _ in dependencies)
        combined_completed = max(dependency.completed_at_utc for dependency, _ in dependencies)
        if not _utc_datetime(combined_started) or not _utc_datetime(combined_completed):
            return self._unknown_snapshot(request, "query_time_unavailable"), None
        try:
            snapshot = QuerySnapshot(
                request=request,
                source_ref=self._session.source_ref,
                generation=self._session.generation,
                snapshot_ref=snapshot_ref,
                completeness=completeness,
                started_at=combined_started,
                completed_at=combined_completed,
                observed_at=max(observed_at, combined_completed),
                scope_complete=scope_complete,
                records=records,
                blockers=tuple(sorted(set(blockers))),
            )
        except Exception:
            return self._unknown_snapshot(request, "neutral_record_invalid"), None

        evidence_rows = tuple(dependencies)
        expires_monotonic = min(
            dependency.query_source.trusted_expires_monotonic
            for dependency, _ in evidence_rows
        )
        expires_at_utc = min(
            dependency.query_source.trusted_expires_at_utc
            for dependency, _ in evidence_rows
        )
        evidence = _SnapshotEvidence(
            snapshot=snapshot,
            results=evidence_rows,
            records_digest=snapshot.records_digest,
            expires_monotonic=expires_monotonic,
            expires_at_utc=expires_at_utc,
        )
        return snapshot, evidence

    def _project_futures_snapshot(
        self,
        request: QueryRequest,
        projection: Any,
        dependencies: tuple[tuple[QueryResult[Any], Mapping[str, str]], ...],
        *,
        started_at: datetime,
        completed_at: datetime,
    ) -> tuple[QuerySnapshot, _SnapshotEvidence | None]:
        from bt_api_ctp.futures_market_projection import CtpFuturesFamilyProjection

        if (
            type(projection) is not CtpFuturesFamilyProjection
            or projection.family != request.family
            or type(projection.records) is not tuple
            or type(projection.completeness) is not QueryCompleteness
            or projection.scope_complete not in (True, False, None)
            or type(projection.blockers) is not tuple
            or any(not isinstance(record, Mapping) for record in projection.records)
            or any(type(blocker) is not str for blocker in projection.blockers)
        ):
            return self._unknown_snapshot(request, "futures_projection_result_untrusted"), None

        required_dependencies = {
            "futures_session": 0,
            "futures_product_universe": 1,
            "futures_catalog": 1,
            "futures_quotes": 2,
            "futures_depth": 2,
        }.get(request.family)
        if required_dependencies is None or (
            projection.completeness is QueryCompleteness.COMPLETE
            and len(dependencies) != required_dependencies
        ):
            return self._unknown_snapshot(request, "futures_projection_evidence_missing"), None

        now_utc = datetime.now(_UTC)
        combined_started = min(
            (result.started_at_utc for result, _ in dependencies),
            default=started_at,
        )
        combined_completed = max(
            (result.completed_at_utc for result, _ in dependencies),
            default=completed_at,
        )
        if (
            not _utc_datetime(combined_started)
            or not _utc_datetime(combined_completed)
            or combined_started > combined_completed
        ):
            return self._unknown_snapshot(request, "query_time_unavailable"), None
        observed_at = max(now_utc, combined_completed)
        snapshot_ref = "ctp-query-snapshot:" + uuid.uuid4().hex
        blockers = tuple(sorted(set(projection.blockers)))
        try:
            snapshot = QuerySnapshot(
                request=request,
                source_ref=self._session.source_ref,
                generation=self._session.generation,
                snapshot_ref=snapshot_ref,
                completeness=projection.completeness,
                started_at=combined_started,
                completed_at=combined_completed,
                observed_at=observed_at,
                scope_complete=projection.scope_complete,
                records=projection.records,
                blockers=blockers,
            )
        except Exception:
            return self._unknown_snapshot(request, "neutral_record_invalid"), None

        if dependencies:
            evidence_rows = tuple(dependencies)
            sources = tuple(result.query_source for result, _ in evidence_rows)
            if any(type(source) is not _QuerySource for source in sources):
                return self._unknown_snapshot(request, "query_source_untrusted"), None
            expires_monotonic = min(source.trusted_expires_monotonic for source in sources)
            expires_at_utc = min(source.trusted_expires_at_utc for source in sources)
        else:
            evidence_rows = ()
            expires_monotonic = time.monotonic() + _QUERY_EVIDENCE_MAX_TTL_SECONDS
            expires_at_utc = now_utc + timedelta(seconds=_QUERY_EVIDENCE_MAX_TTL_SECONDS)
        evidence = _SnapshotEvidence(
            snapshot=snapshot,
            results=evidence_rows,
            records_digest=snapshot.records_digest,
            expires_monotonic=expires_monotonic,
            expires_at_utc=expires_at_utc,
        )
        return snapshot, evidence

    def _project_account(
        self,
        rows: tuple[Any, ...],
        observed_at: str,
    ) -> tuple[tuple[Mapping[str, Any], ...], tuple[str, ...]]:
        balances: list[Mapping[str, Any]] = []
        blockers: list[str] = []
        seen_currencies: set[str] = set()
        if not rows:
            return (), ("account_balance_rows_empty",)
        for row in rows:
            try:
                if _text(_field(row, "BrokerID")) != self._session.broker_id:
                    blockers.append("record_identity_mismatch")
                    continue
                currency = _text(_field(row, "CurrencyID"))
                if currency in seen_currencies:
                    blockers.append("duplicate_account_currency")
                    continue
                balance = AccountBalance(
                    resource=_RESOURCE_MODEL,
                    currency=currency,
                    total=_decimal_text(_field(row, "Balance")),
                    available=_decimal_text(_field(row, "Available")),
                    locked=None,
                    observed_at_utc=observed_at,
                )
            except Exception:
                blockers.append("account_balance_record_invalid")
                continue
            seen_currencies.add(currency)
            balances.append(balance.to_dict())
        return tuple(balances), tuple(sorted(set(blockers)))

    def _project_account_valuation(
        self,
        rows: tuple[Any, ...],
        observed_at: str,
    ) -> tuple[tuple[Mapping[str, Any], ...], tuple[str, ...]]:
        """Project legacy Available/Balance only for one sealed account row.

        The shared sealed query source binds the exact authenticated CTP
        account session and its BrokerID/InvestorID request scope. This
        optional family is unavailable for empty, multi-row, duplicate, or
        otherwise invalid account results. ``Available`` is preserved as the
        provider's available-cash value; it does not prove settled cash.
        """

        if len(rows) != 1:
            return (), ("account_valuation_single_account_currency_required",)
        balance_rows, balance_blockers = self._project_account(rows, observed_at)
        if balance_blockers or len(balance_rows) != 1:
            return (), tuple(
                sorted(
                    set(balance_blockers)
                    | {"account_valuation_account_row_unverified"}
                )
            )
        row = rows[0]
        try:
            valuation = AccountValuation(
                currency=_text(_field(row, "CurrencyID")),
                available_cash=_decimal_text(_field(row, "Available")),
                equity=_decimal_text(_field(row, "Balance")),
                observed_at_utc=observed_at,
            )
        except Exception:
            return (), ("account_valuation_record_invalid",)
        return (valuation.to_dict(),), ()

    def _project_account_positions(
        self,
        rows: tuple[Any, ...],
        observed_at: str,
        catalog_types: Mapping[tuple[str, str], str],
    ) -> tuple[tuple[Mapping[str, Any], ...], tuple[str, ...]]:
        blockers: list[str] = []
        totals: dict[tuple[str, str, str], tuple[AccountPosition, int]] = {}
        today_totals: dict[tuple[str, str, str], int] = {}
        unknown_split: set[tuple[str, str, str]] = set()
        for row in rows:
            try:
                if not _row_matches_session(row, self._session):
                    blockers.append("record_identity_mismatch")
                    continue
                instrument_id = _text(_field(row, "InstrumentID"))
                exchange_id = _text(_field(row, "ExchangeID"))
                if catalog_types.get((exchange_id, instrument_id)) != "future":
                    blockers.append("position_product_type_unavailable")
                    continue
                positions = _project_positions((row,), observed_at)
                for position in positions:
                    quantity = _integer(position.quantity)
                    key = (exchange_id, instrument_id, position.side)
                    prior = totals.get(key)
                    totals[key] = (position, quantity + (prior[1] if prior else 0))
                    if position.today_quantity is None or key in unknown_split:
                        unknown_split.add(key)
                    else:
                        today_totals[key] = _integer(position.today_quantity) + today_totals.get(
                            key, 0
                        )
            except Exception:
                blockers.append("position_record_invalid")
        records: list[Mapping[str, Any]] = []
        for key, (position, quantity) in sorted(totals.items()):
            instrument = replace(position.instrument, environment=self._session.environment)
            known = key not in unknown_split
            today_quantity = today_totals.get(key, 0)
            normalized = replace(
                position,
                instrument=instrument,
                quantity=str(quantity),
                today_quantity=str(today_quantity) if known else None,
                yesterday_quantity=str(quantity - today_quantity) if known else None,
            )
            records.append(normalized.to_dict())
        return tuple(records), tuple(sorted(set(blockers)))

    def _project_instruments(
        self,
        rows: tuple[Any, ...],
    ) -> tuple[tuple[Mapping[str, Any], ...], tuple[str, ...]]:
        environment = self._session.environment
        if environment is None:
            return (), ("neutral_environment_unavailable",)
        records: list[Mapping[str, Any]] = []
        blockers: list[str] = []
        seen: set[tuple[str, str]] = set()
        for row in rows:
            try:
                normalized = normalize_ctp_instrument(row)
                instrument_id = normalized.get("instrument_id")
                exchange_id = normalized.get("exchange_id")
                product_type = normalized.get("asset_type")
                if not all(type(value) is str and value for value in (instrument_id, exchange_id)):
                    raise ValueError
                if type(product_type) is not str or product_type == "unknown":
                    raise ValueError
                key = (exchange_id, instrument_id)
                if key in seen:
                    blockers.append("duplicate_instrument_identity")
                    continue
                record = MarketInstrumentKey(
                    provider="ctp",
                    venue=exchange_id,
                    environment=environment,
                    product_type=product_type,
                    instrument_id=instrument_id,
                    native_symbol=instrument_id,
                )
            except Exception:
                blockers.append("instrument_record_invalid")
                continue
            seen.add(key)
            records.append(record.to_dict())
        return tuple(records), tuple(sorted(set(blockers)))

    def _failure_completeness(
        self,
        result: QueryResult[Any] | None,
        blocker: str | None,
    ) -> QueryCompleteness:
        if result is not None and result.unsupported is True:
            return QueryCompleteness.UNAVAILABLE
        if blocker in {
            "query_deadline_elapsed_before_dispatch",
            "query_method_unavailable",
            "query_unsupported",
            "query_definitely_unavailable",
        }:
            return QueryCompleteness.UNAVAILABLE
        return QueryCompleteness.UNKNOWN

    def _empty_snapshot(
        self,
        request: QueryRequest,
        completeness: QueryCompleteness,
        blocker: str,
    ) -> QuerySnapshot:
        return QuerySnapshot(
            request=request,
            source_ref=self._session.source_ref,
            generation=self._session.generation,
            snapshot_ref=None,
            completeness=completeness,
            started_at=None,
            completed_at=None,
            observed_at=None,
            scope_complete=None,
            records=(),
            blockers=(blocker,),
        )

    def _unknown_snapshot(self, request: QueryRequest, blocker: str) -> QuerySnapshot:
        return self._empty_snapshot(request, QueryCompleteness.UNKNOWN, blocker)

    def _blocked_receipt(
        self,
        requests: tuple[QueryRequest, ...],
        started_at: datetime,
        completed_at: datetime,
        *,
        blocker: str,
    ) -> QueryBatchReceipt:
        completed = max(started_at, completed_at)
        snapshots = tuple(
            QuerySnapshot(
                request=request,
                source_ref=None,
                generation=None,
                snapshot_ref=None,
                completeness=QueryCompleteness.UNAVAILABLE,
                started_at=None,
                completed_at=None,
                observed_at=None,
                scope_complete=None,
                records=(),
                blockers=(blocker,),
            )
            for request in requests
        )
        return QueryBatchReceipt(
            batch_ref="ctp-query-batch:" + uuid.uuid4().hex,
            source_ref=None,
            generation=None,
            started_at=started_at,
            completed_at=completed,
            requests=requests,
            snapshots=snapshots,
            blockers=(blocker,),
        )

    def _session_current(self, now_monotonic: float) -> bool:
        return validate_query_session(
            self._client,
            self._session,
            now_monotonic=now_monotonic,
        )

    def _evidence_current(self, evidence: _SnapshotEvidence, now_monotonic: float) -> bool:
        if (
            now_monotonic >= evidence.expires_monotonic
            or datetime.now(_UTC) >= evidence.expires_at_utc
            or evidence.snapshot.records_digest != evidence.records_digest
            or not self._session_current(now_monotonic)
        ):
            return False
        for result, expected in evidence.results:
            family = _family_for_native_type(result.request_type)
            if family is None or self._validate_native_result(
                result,
                family=family,
                expected_filters=expected,
                deadline_monotonic=None,
            ) is None:
                return False
        return True

    def _remember_snapshot(self, evidence: _SnapshotEvidence) -> None:
        snapshot_ref = evidence.snapshot.snapshot_ref
        if snapshot_ref is None:
            return
        self._snapshots[snapshot_ref] = evidence
        self._snapshots.move_to_end(snapshot_ref)
        while len(self._snapshots) > _MAX_RETAINED_SNAPSHOTS:
            self._snapshots.popitem(last=False)

    def _remember_receipt(self, receipt: QueryBatchReceipt) -> None:
        refs = receipt.complete_snapshot_refs
        if not refs:
            return
        self._receipts[receipt.receipt_digest] = _ReceiptEvidence(receipt, refs)
        self._receipts.move_to_end(receipt.receipt_digest)
        while len(self._receipts) > _MAX_RETAINED_RECEIPTS:
            self._receipts.popitem(last=False)

    def _prune(self, now_monotonic: float) -> None:
        expired_refs = {
            ref
            for ref, evidence in self._snapshots.items()
            if now_monotonic >= evidence.expires_monotonic
        }
        for ref in expired_refs:
            self._snapshots.pop(ref, None)
        for digest, evidence in tuple(self._receipts.items()):
            if any(ref not in self._snapshots for ref in evidence.snapshot_refs):
                self._receipts.pop(digest, None)
        for lease_ref, evidence in tuple(self._leases.items()):
            if datetime.now(_UTC) >= evidence.lease.expires_at:
                self._leases.pop(lease_ref, None)


def create_ctp_provider_query_service(
    client: TraderClient,
    *,
    environment: str | None,
    selected_feed: object | None = None,
    execution_context: object | None = None,
    order_observation_issuer: object | None = None,
) -> CtpProviderQueryService:
    """Trusted runtime factory for the CTP implementation of ProviderQueryPort."""

    return CtpProviderQueryService.from_trusted_runtime(
        client,
        environment=environment,
        selected_feed=selected_feed,
        execution_context=execution_context,
        order_observation_issuer=order_observation_issuer,
    )


def _validate_requests(requests: Sequence[QueryRequest]) -> tuple[QueryRequest, ...]:
    if isinstance(requests, (str, bytes)) or not isinstance(requests, Sequence):
        raise TypeError("requests must be a sequence of QueryRequest values")
    items = tuple(requests)
    if not 1 <= len(items) <= MAX_QUERY_BATCH_REQUESTS:
        raise ValueError("requests exceed the published query batch bounds")
    if any(type(item) is not QueryRequest for item in items):
        raise TypeError("requests must contain exact QueryRequest values")
    refs = tuple(item.request_ref for item in items)
    if len(set(refs)) != len(refs):
        raise ValueError("request_ref values must be unique within a query batch")
    return items


def _validate_futures_filter_shape(
    family: str,
    filters: Mapping[str, Any],
) -> str | None:
    """Validate the complete neutral futures filter shape before dispatch."""

    shared = {"environment", "trading_day", "venues"}
    expected = {
        "futures_session": shared | {"require_active_session"},
        "futures_catalog": shared | {"products", "instruments", "contract_kinds"},
        "futures_product_universe": shared | {"products"},
        "futures_quotes": shared
        | {
            "products",
            "instruments",
            "contract_kinds",
            "required_quote_fields",
            "required_open_interest_unit",
            "required_volume_unit",
            "required_volume_semantics",
        },
        "futures_depth": shared
        | {
            "products",
            "instruments",
            "contract_kinds",
            "depth_levels",
            "required_depth_volume_unit",
        },
    }.get(family)
    if expected is None:
        return "unsupported_family_schema"
    if not isinstance(filters, Mapping) or set(filters) != expected:
        return "unsupported_query_filter"
    if not _valid_filter_text(filters.get("environment")):
        return "invalid_query_filter_value"

    trading_day = filters.get("trading_day")
    if trading_day is not None:
        if type(trading_day) is not str:
            return "invalid_query_filter_value"
        try:
            parsed_day = date.fromisoformat(trading_day)
        except ValueError:
            return "invalid_query_filter_value"
        if parsed_day.isoformat() != trading_day:
            return "invalid_query_filter_value"

    venues = filters.get("venues")
    if venues is not None and not _valid_text_sequence(venues, nonempty=True):
        return "invalid_query_filter_value"

    if family == "futures_session":
        if type(filters.get("require_active_session")) is not bool:
            return "invalid_query_filter_value"
        return None

    from bt_api_base.futures_market_protocol import (
        FuturesProductKey,
        FuturesQuantityUnit,
        FuturesVolumeSemantics,
    )

    products = filters.get("products")
    if products is not None:
        if type(products) not in (tuple, list) or not products:
            return "invalid_query_filter_value"
        try:
            product_rows = tuple(FuturesProductKey.from_dict(item) for item in products)
        except Exception:
            return "invalid_query_filter_value"
        product_ids = tuple((item.venue, item.product_id) for item in product_rows)
        if len(set(product_ids)) != len(product_ids):
            return "invalid_query_filter_value"
        if venues is not None and any(item.venue not in venues for item in product_rows):
            return "invalid_query_filter_value"

    if family == "futures_product_universe":
        return None

    instruments = filters.get("instruments")
    if instruments is not None:
        if type(instruments) not in (tuple, list) or not instruments:
            return "invalid_query_filter_value"
        try:
            instrument_rows = tuple(MarketInstrumentKey.from_dict(item) for item in instruments)
        except Exception:
            return "invalid_query_filter_value"
        identities = tuple(
            (
                item.provider,
                item.venue,
                item.environment,
                item.product_type,
                item.instrument_id,
                item.native_symbol,
            )
            for item in instrument_rows
        )
        if len(set(identities)) != len(identities):
            return "invalid_query_filter_value"
        if any(item.provider != "ctp" or item.environment != filters["environment"] for item in instrument_rows):
            return "invalid_query_filter_value"
        if venues is not None and any(item.venue not in venues for item in instrument_rows):
            return "invalid_query_filter_value"

    kinds = filters.get("contract_kinds")
    if (
        not _valid_text_sequence(kinds, nonempty=True)
        or any(item not in {"future", "option"} for item in kinds)
    ):
        return "invalid_query_filter_value"

    if family == "futures_quotes":
        quote_fields = filters.get("required_quote_fields")
        allowed_fields = {
            "last_price",
            "open_interest",
            "volume",
            "bid_price",
            "bid_volume",
            "ask_price",
            "ask_volume",
        }
        if (
            not _valid_text_sequence(quote_fields, nonempty=True)
            or any(item not in allowed_fields for item in quote_fields)
        ):
            return "invalid_query_filter_value"
        for name in ("required_open_interest_unit", "required_volume_unit"):
            value = filters.get(name)
            if value is not None and (
                type(value) is not str
                or value not in {item.value for item in FuturesQuantityUnit if item.value != "unknown"}
            ):
                return "invalid_query_filter_value"
        value = filters.get("required_volume_semantics")
        if value is not None and (
            type(value) is not str
            or value
            not in {
                item.value
                for item in FuturesVolumeSemantics
                if item.value != "unknown"
            }
        ):
            return "invalid_query_filter_value"
    elif family == "futures_depth":
        levels = filters.get("depth_levels")
        if type(levels) is not int or not 1 <= levels <= 1000:
            return "invalid_query_filter_value"
        value = filters.get("required_depth_volume_unit")
        if value is not None and (
            type(value) is not str
            or value not in {item.value for item in FuturesQuantityUnit if item.value != "unknown"}
        ):
            return "invalid_query_filter_value"
    return None


def _valid_text_sequence(value: Any, *, nonempty: bool) -> bool:
    if type(value) not in (tuple, list) or (nonempty and not value):
        return False
    return bool(
        all(_valid_filter_text(item) for item in value)
        and len(set(value)) == len(value)
    )


def _require_utc(value: datetime, name: str) -> None:
    if type(value) is not datetime:
        raise TypeError(f"{name} must be a datetime")
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError(f"{name} must be timezone-aware UTC")


def _utc_datetime(value: Any) -> bool:
    return bool(
        type(value) is datetime
        and value.tzinfo is not None
        and value.utcoffset() == timedelta(0)
    )


def _finite_number(value: Any) -> bool:
    return bool(
        type(value) in (int, float)
        and not isinstance(value, bool)
        and isfinite(float(value))
    )


def _native_filter_map_matches(value: Any, expected: Mapping[str, str]) -> bool:
    if type(value) is not tuple:
        return False
    result: dict[str, str] = {}
    for pair in value:
        if (
            type(pair) is not tuple
            or len(pair) != 2
            or type(pair[0]) is not str
            or type(pair[1]) is not str
            or pair[0] in result
        ):
            return False
        result[pair[0]] = pair[1]
    return result == dict(expected)


def _text_alias(filters: Mapping[str, Any], first: str, second: str) -> tuple[str | None, str | None]:
    has_left = first in filters
    has_right = second in filters
    left = filters[first] if has_left else None
    right = filters[second] if has_right else None
    if has_left and not _valid_filter_text(left):
        return None, "invalid_query_filter_value"
    if has_right and not _valid_filter_text(right):
        return None, "invalid_query_filter_value"
    if has_left and has_right and left != right:
        return None, "conflicting_instrument_filter_aliases"
    if not has_left and not has_right:
        return None, None
    value = left if has_left else right
    return value, None


def _optional_filter_text(filters: Mapping[str, Any], name: str) -> tuple[str | None, str | None]:
    if name not in filters:
        return None, None
    value = filters[name]
    if not _valid_filter_text(value):
        return None, "invalid_query_filter_value"
    return value, None


def _valid_filter_text(value: Any) -> bool:
    if type(value) is not str or not value or value != value.strip() or not value.isprintable():
        return False
    try:
        return len(value.encode("utf-8", errors="strict")) <= 256
    except UnicodeEncodeError:
        return False


def _row_matches_session(row: Any, session: CtpQuerySessionBinding) -> bool:
    return bool(
        _text(_field(row, "BrokerID")) == session.broker_id
        and _text(_field(row, "InvestorID")) == session.investor_id
    )


def _instrument_types(rows: tuple[Any, ...]) -> dict[tuple[str, str], str]:
    result: dict[tuple[str, str], str] = {}
    conflicting: set[tuple[str, str]] = set()
    for row in rows:
        try:
            normalized = normalize_ctp_instrument(row)
            instrument_id = normalized.get("instrument_id")
            exchange_id = normalized.get("exchange_id")
            product_type = normalized.get("asset_type")
            if (
                type(instrument_id) is str
                and instrument_id
                and type(exchange_id) is str
                and exchange_id
                and type(product_type) is str
                and product_type != "unknown"
            ):
                key = (exchange_id, instrument_id)
                if key in conflicting:
                    continue
                prior = result.get(key)
                if prior is None or prior == product_type:
                    result[key] = product_type
                else:
                    result.pop(key, None)
                    conflicting.add(key)
        except Exception:
            continue
    return result


def _family_for_native_type(request_type: str) -> str | None:
    for family, native_type in _QUERY_REQUEST_TYPES.items():
        if native_type == request_type:
            return family
    return None


__all__ = [
    "CtpProviderQueryError",
    "CtpProviderQueryService",
    "create_ctp_provider_query_service",
]
