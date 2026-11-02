"""CTP provider implementation for neutral futures EOD ranking contracts.

This provider owns both official-source parsers' CTP catalog joins. Its public
result contains only ``bt_api_base`` identities, normalized numeric strings,
bounded source provenance, and explicit coverage. Construction and import are
offline; collection performs only the explicitly requested public HTTP GETs.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Callable, Mapping
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Optional

from bt_api_base.futures_eod_ranking_protocol import (
    FuturesEodCatalogScope,
    FuturesEodProductCoverage,
    FuturesEodRankingCompleteness,
    FuturesEodRankingPort,
    FuturesEodRankingRecord,
    FuturesEodRankingRequest,
    FuturesEodRankingSnapshot,
    FuturesEodSourceCoverage,
    futures_eod_records_digest,
)
from bt_api_base.futures_market_protocol import (
    FuturesAssetType,
    FuturesCatalogDisposition,
    FuturesCatalogRecord,
    FuturesContractKind,
    FuturesProductKey,
    FuturesQuantityUnit,
    FuturesVolumeSemantics,
)
from bt_api_base.market_protocol import MarketInstrumentKey

from .cffex import (
    CFFEX_EOD_MAX_BYTES,
    CFFEX_EOD_SOURCE_ID,
    CffexDailyReport,
    CffexEodError,
    fetch_cffex_eod,
)
from .shfe import (
    SHFE_EOD_MAX_BYTES,
    SHFE_EOD_SOURCE_ID,
    SHFE_INE_EXCHANGE_IDS,
    ShfeDailyReport,
    ShfeEodError,
    fetch_shfe_eod,
)

_UTC = timezone.utc
_SUPPORTED_VENUES = frozenset(("CFFEX", "SHFE", "INE"))
_DEFAULT_TIMEOUT_SECONDS = 10.0
_MAX_TIMEOUT_SECONDS = 60.0
_MAX_RESPONSE_BYTES = 2 * 1024 * 1024


class CtpEodRankingError(RuntimeError):
    """Finite, value-free error at the CTP EOD provider boundary."""

    _CODES = frozenset(
        {
            "request_invalid",
            "catalog_invalid",
            "deadline_invalid",
            "transport_invalid",
        }
    )

    def __init__(self, code: str) -> None:
        self.code = code if type(code) is str and code in self._CODES else "request_invalid"
        super().__init__(self.code)


def _aware_utc(value: Any, field: str) -> datetime:
    if type(value) is not datetime or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("{} must be timezone-aware".format(field))
    if value.utcoffset() != timedelta(0):
        raise ValueError("{} must use UTC".format(field))
    return value


def _decimal_text(value: Any) -> str:
    if type(value) is bool or not isinstance(value, (int, Decimal)):
        raise ValueError("ranking values must be exact integer or Decimal")
    decimal_value = value if isinstance(value, Decimal) else Decimal(value)
    if not decimal_value.is_finite() or decimal_value < 0:
        raise ValueError("ranking values must be finite and non-negative")
    text = format(decimal_value, "f")
    return text if "." not in text else text.rstrip("0").rstrip(".")


def _instrument_key(item: MarketInstrumentKey) -> tuple[str, ...]:
    return (
        item.provider,
        item.venue,
        item.environment,
        item.product_type,
        item.instrument_id,
        item.native_symbol,
    )


def _known_candidate(record: FuturesCatalogRecord) -> bool:
    return (
        record.disposition is not FuturesCatalogDisposition.EXCLUDED
        and record.contract_kind
        in (FuturesContractKind.FUTURE, FuturesContractKind.UNKNOWN)
    )


def _classification_blockers(records: tuple[FuturesCatalogRecord, ...]) -> tuple[str, ...]:
    blockers = set()
    for record in records:
        if record.disposition is FuturesCatalogDisposition.EXCLUDED:
            continue
        if record.contract_kind is FuturesContractKind.UNKNOWN:
            blockers.add("catalog_contract_classification_unknown")
        if record.asset_type is FuturesAssetType.UNKNOWN:
            blockers.add("catalog_asset_classification_unknown")
        if record.disposition is FuturesCatalogDisposition.UNKNOWN:
            blockers.add("catalog_disposition_unknown")
    return tuple(sorted(blockers))


def _record_for(
    record: FuturesCatalogRecord,
    product: FuturesProductKey,
    trading_day: date,
    open_interest: Any,
    volume: Any,
) -> FuturesEodRankingRecord:
    if record.product != product:
        raise ValueError("catalog product does not match EOD row")
    return FuturesEodRankingRecord(
        instrument=record.instrument,
        product=product,
        trading_day=trading_day,
        open_interest=_decimal_text(open_interest),
        volume=_decimal_text(volume),
        open_interest_unit=FuturesQuantityUnit.CONTRACTS,
        volume_unit=FuturesQuantityUnit.CONTRACTS,
        volume_semantics=FuturesVolumeSemantics.CUMULATIVE_TRADING_DAY,
    )


class CtpEodRankingProvider(FuturesEodRankingPort):
    """Fetch and map public CFFEX/SHFE/INE EOD reports on explicit request."""

    def __init__(
        self,
        *,
        openers: Optional[Mapping[str, Callable[..., Any]]] = None,
        clock: Optional[Callable[[], datetime]] = None,
        timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
        max_bytes: int = _MAX_RESPONSE_BYTES,
    ) -> None:
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or timeout_seconds <= 0
            or timeout_seconds > _MAX_TIMEOUT_SECONDS
        ):
            raise CtpEodRankingError("transport_invalid")
        if type(max_bytes) is not int or not 1 <= max_bytes <= _MAX_RESPONSE_BYTES:
            raise CtpEodRankingError("transport_invalid")
        if openers is not None:
            if not isinstance(openers, Mapping) or any(
                key not in ("CFFEX", "SHFE_INE") or not callable(value)
                for key, value in openers.items()
            ):
                raise CtpEodRankingError("transport_invalid")
            self._openers = dict(openers)
        else:
            self._openers = {}
        if clock is not None and not callable(clock):
            raise CtpEodRankingError("transport_invalid")
        self._clock = clock or (lambda: datetime.now(_UTC))
        self._timeout_seconds = float(timeout_seconds)
        self._max_bytes = max_bytes

    def collect(
        self,
        request: FuturesEodRankingRequest,
        catalog: FuturesEodCatalogScope,
        *,
        deadline: datetime,
    ) -> FuturesEodRankingSnapshot:
        if type(request) is not FuturesEodRankingRequest:
            raise CtpEodRankingError("request_invalid")
        if type(catalog) is not FuturesEodCatalogScope:
            raise CtpEodRankingError("catalog_invalid")
        requested_products = {(item.venue, item.product_id) for item in request.products}
        if (
            catalog.venues is not None
            and any(venue not in catalog.venues for venue, _product_id in requested_products)
        ) or (
            catalog.catalog_products is not None
            and not requested_products.issubset(
                {(item.venue, item.product_id) for item in catalog.catalog_products}
            )
        ):
            raise CtpEodRankingError("catalog_invalid")
        if any(
            item.instrument.environment != catalog.environment
            for item in catalog.catalog_records
        ):
            raise CtpEodRankingError("catalog_invalid")
        try:
            deadline_utc = _aware_utc(deadline, "deadline")
            requested_at = _aware_utc(self._clock(), "requested_at")
        except (TypeError, ValueError):
            raise CtpEodRankingError("deadline_invalid") from None
        if requested_at >= deadline_utc:
            deadline_blocked = True
        else:
            deadline_blocked = False

        products_by_venue: dict[str, list[FuturesProductKey]] = defaultdict(list)
        for product in request.products:
            products_by_venue[product.venue.upper()].append(product)
        requested_unsupported = tuple(
            sorted(venue for venue in products_by_venue if venue not in _SUPPORTED_VENUES)
        )
        all_records: list[FuturesEodRankingRecord] = []
        sources: list[FuturesEodSourceCoverage] = []
        source_for_product: dict[tuple[str, str], str] = {}
        product_unmatched: Counter[tuple[str, str]] = Counter()
        product_duplicates: Counter[tuple[str, str]] = Counter()
        product_ambiguous: Counter[tuple[str, str]] = Counter()

        if "CFFEX" in products_by_venue:
            source, mapped, unmatched, duplicates, ambiguous = self._collect_cffex(
                request,
                catalog,
                products_by_venue["CFFEX"],
                deadline_utc,
                deadline_blocked,
            )
            sources.append(source)
            all_records.extend(mapped)
            _merge_product_counts(product_unmatched, unmatched)
            _merge_product_counts(product_duplicates, duplicates)
            _merge_product_counts(product_ambiguous, ambiguous)
            for item in products_by_venue["CFFEX"]:
                source_for_product[(item.venue, item.product_id)] = source.source_ref

        shfe_products = products_by_venue.get("SHFE", []) + products_by_venue.get("INE", [])
        if shfe_products:
            source, mapped, unmatched, duplicates, ambiguous = self._collect_shfe_ine(
                request,
                catalog,
                shfe_products,
                deadline_utc,
                deadline_blocked,
            )
            sources.append(source)
            all_records.extend(mapped)
            _merge_product_counts(product_unmatched, unmatched)
            _merge_product_counts(product_duplicates, duplicates)
            _merge_product_counts(product_ambiguous, ambiguous)
            for item in shfe_products:
                source_for_product[(item.venue, item.product_id)] = source.source_ref

        product_coverage = []
        blockers = []
        for product in request.products:
            product_key = (product.venue, product.product_id)
            candidates = tuple(
                item
                for item in catalog.catalog_records
                if item.product == product and _known_candidate(item)
            )
            expected = tuple(item.instrument for item in candidates)
            product_catalog_rows = tuple(
                item for item in catalog.catalog_records if item.product == product
            )
            product_rows = tuple(item for item in all_records if item.product == product)
            ranked = tuple(item.instrument for item in product_rows)
            missing = tuple(item for item in expected if item not in set(ranked))
            classification = _classification_blockers(candidates)
            catalog_family_complete = (
                catalog.catalog_completeness.value == "complete"
                and catalog.catalog_scope_complete is True
            )
            if product_catalog_rows:
                candidate_set_complete = catalog_family_complete
            else:
                candidate_set_complete = (
                    catalog.product_universe_complete is True
                    and product in (catalog.product_universe or ())
                )
            product_blockers = list(classification)
            if not candidate_set_complete:
                product_blockers.append(
                    "product_catalog_scope_incomplete"
                    if candidates
                    else "product_universe_unavailable"
                )
            if product_unmatched[product_key]:
                product_blockers.append("source_rows_unmatched")
            if product_duplicates[product_key]:
                product_blockers.append("source_rows_duplicated")
            if product_ambiguous[product_key]:
                product_blockers.append("source_rows_ambiguous")
            if missing:
                product_blockers.append("ranking_rows_missing")
            selected_source = source_for_product.get(product_key)
            source_row = next(
                (item for item in sources if item.source_ref == selected_source),
                None,
            )
            source_ready = source_row is not None and source_row.complete
            if not source_ready:
                product_blockers.append("ranking_source_unavailable")
            ranking_values_complete = (
                source_ready
                and not missing
                and not product_unmatched[product_key]
                and not product_duplicates[product_key]
                and not product_ambiguous[product_key]
            )
            if not ranking_values_complete and not any(
                item in product_blockers
                for item in (
                    "source_rows_unmatched",
                    "source_rows_duplicated",
                    "source_rows_ambiguous",
                    "ranking_rows_missing",
                    "ranking_source_unavailable",
                )
            ):
                product_blockers.append("ranking_values_incomplete")
            item = FuturesEodProductCoverage(
                product=product,
                expected_instruments=expected,
                ranking_instruments=ranked,
                missing_instruments=missing,
                candidate_set_complete=candidate_set_complete,
                ranking_values_complete=ranking_values_complete,
                unmatched_source_row_count=product_unmatched[product_key],
                duplicate_source_row_count=(
                    product_duplicates[product_key] + product_ambiguous[product_key]
                ),
                source_refs=() if selected_source is None else (selected_source,),
                blockers=tuple(dict.fromkeys(product_blockers)),
            )
            product_coverage.append(item)
            blockers.extend(item.blockers)

        if requested_unsupported:
            blockers.append("requested_venue_unsupported")
        for source in sources:
            blockers.extend(source.blockers)
        if not sources:
            blockers.append("ranking_source_unavailable")

        all_records.sort(
            key=lambda item: (
                item.product.venue,
                item.product.product_id,
                _instrument_key(item.instrument),
            )
        )
        records = tuple(all_records)
        observed_at = _aware_utc(self._clock(), "observed_at")
        if observed_at > deadline_utc:
            blockers.append("ranking_collection_deadline_exceeded")
        completed_at = observed_at
        if not sources:
            completeness = FuturesEodRankingCompleteness.UNAVAILABLE
        elif requested_unsupported:
            completeness = FuturesEodRankingCompleteness.PARTIAL
        elif (
            not blockers
            and all(item.complete for item in sources)
            and all(item.complete for item in product_coverage)
            and catalog.catalog_completeness.value == "complete"
            and catalog.catalog_scope_complete is True
        ):
            completeness = FuturesEodRankingCompleteness.COMPLETE
        else:
            completeness = FuturesEodRankingCompleteness.PARTIAL
        normalized_blockers = tuple(sorted(set(blockers)))
        if completeness is not FuturesEodRankingCompleteness.COMPLETE and not normalized_blockers:
            normalized_blockers = ("ranking_coverage_incomplete",)
        return FuturesEodRankingSnapshot(
            request=request,
            catalog=catalog,
            requested_at=requested_at,
            completed_at=completed_at,
            observed_at=observed_at,
            sources=tuple(sorted(sources, key=lambda item: item.source_ref)),
            product_coverage=tuple(product_coverage),
            records=records,
            records_digest=futures_eod_records_digest(records),
            completeness=completeness,
            blockers=normalized_blockers,
        )

    def _collect_cffex(
        self,
        request: FuturesEodRankingRequest,
        catalog: FuturesEodCatalogScope,
        products: list[FuturesProductKey],
        deadline: datetime,
        deadline_blocked: bool,
    ) -> tuple[
        FuturesEodSourceCoverage,
        tuple[FuturesEodRankingRecord, ...],
        Counter[tuple[str, str]],
        Counter[tuple[str, str]],
        Counter[tuple[str, str]],
    ]:
        now = _aware_utc(self._clock(), "observed_at")
        if deadline_blocked or now >= deadline:
            return self._unavailable_source(
                CFFEX_EOD_SOURCE_ID,
                ("CFFEX",),
                request.trading_day,
                now,
                "source_deadline_exceeded",
            ), (), Counter(), Counter(), Counter()
        report = None
        try:
            report = fetch_cffex_eod(
                request.trading_day,
                timeout_seconds=self._remaining_timeout(deadline),
                max_bytes=min(self._max_bytes, CFFEX_EOD_MAX_BYTES),
                opener=self._openers.get("CFFEX"),
                clock=self._clock,
            )
        except (CffexEodError, OSError, TimeoutError, ValueError):
            now = _aware_utc(self._clock(), "observed_at")
            return self._unavailable_source(
                CFFEX_EOD_SOURCE_ID,
                ("CFFEX",),
                request.trading_day,
                now,
                "source_fetch_or_parse_failed",
            ), (), Counter(), Counter(), Counter()

        target_ids = {item.product_id.upper() for item in products}
        scoped_rows = tuple(row for row in report.rows if row.product_id.upper() in target_ids)
        source_counts = Counter(row.instrument_id for row in scoped_rows)
        duplicate_ids = {key for key, count in source_counts.items() if count > 1}
        candidates = tuple(
            item
            for item in catalog.catalog_records
            if item.product.venue == "CFFEX"
            and item.product.product_id.upper() in target_ids
            and _known_candidate(item)
        )
        by_key: dict[tuple[str, str], list[FuturesCatalogRecord]] = defaultdict(list)
        for item in candidates:
            by_key[(item.product.product_id.upper(), item.instrument.instrument_id)].append(item)
        mapped: list[FuturesEodRankingRecord] = []
        unmatched: Counter[tuple[str, str]] = Counter()
        duplicates: Counter[tuple[str, str]] = Counter()
        ambiguous: Counter[tuple[str, str]] = Counter()
        mapping_blockers = []
        for row in scoped_rows:
            product_matches = tuple(
                item for item in products if item.product_id.upper() == row.product_id.upper()
            )
            if len(product_matches) != 1:
                for product in product_matches:
                    ambiguous[(product.venue, product.product_id)] += 1
                continue
            product = product_matches[0]
            product_key = (product.venue, product.product_id)
            if row.instrument_id in duplicate_ids:
                duplicates[product_key] += 1
                continue
            matches = by_key.get((row.product_id.upper(), row.instrument_id), [])
            if not matches:
                unmatched[product_key] += 1
                continue
            if len(matches) != 1:
                ambiguous[product_key] += 1
                continue
            try:
                mapped.append(
                    _record_for(
                        matches[0],
                        product,
                        request.trading_day,
                        row.open_interest,
                        row.volume,
                    )
                )
            except (TypeError, ValueError):
                mapping_blockers.append("ranking_value_invalid")
        for product in products:
            if not any(item.product == product for item in catalog.catalog_records):
                if not catalog.product_universe_complete or product not in (catalog.product_universe or ()):
                    unmatched[(product.venue, product.product_id)] += 0
        if unmatched:
            mapping_blockers.append("source_rows_unmatched")
        if duplicates:
            mapping_blockers.append("source_rows_duplicated")
        if ambiguous:
            mapping_blockers.append("source_rows_ambiguous")
        if mapping_blockers:
            completeness = FuturesEodRankingCompleteness.PARTIAL
        else:
            completeness = FuturesEodRankingCompleteness.COMPLETE
        now = _aware_utc(self._clock(), "observed_at")
        coverage = FuturesEodSourceCoverage(
            source_ref=CFFEX_EOD_SOURCE_ID,
            venues=("CFFEX",),
            snapshot_ref=report.snapshot_id,
            source_digest=report.source_sha256,
            trading_day=report.trading_day,
            reported_update_at=(
                None
                if report.http_last_modified_at is None
                else report.http_last_modified_at.astimezone(_UTC)
            ),
            retrieved_at=report.retrieved_at.astimezone(_UTC),
            observed_at=now,
            source_row_count=report.source_row_count,
            ranking_row_count=len(mapped),
            excluded_row_count=report.option_row_count + len(report.summary_rows),
            unmatched_row_count=sum(unmatched.values()),
            ambiguous_row_count=sum(ambiguous.values()),
            duplicate_row_count=sum(duplicates.values()),
            completeness=completeness,
            blockers=tuple(mapping_blockers),
        )
        return coverage, tuple(mapped), unmatched, duplicates, ambiguous

    def _collect_shfe_ine(
        self,
        request: FuturesEodRankingRequest,
        catalog: FuturesEodCatalogScope,
        products: list[FuturesProductKey],
        deadline: datetime,
        deadline_blocked: bool,
    ) -> tuple[
        FuturesEodSourceCoverage,
        tuple[FuturesEodRankingRecord, ...],
        Counter[tuple[str, str]],
        Counter[tuple[str, str]],
        Counter[tuple[str, str]],
    ]:
        requested_venues = tuple(sorted({item.venue for item in products}))
        now = _aware_utc(self._clock(), "observed_at")
        if deadline_blocked or now >= deadline:
            return self._unavailable_source(
                SHFE_EOD_SOURCE_ID,
                requested_venues,
                request.trading_day,
                now,
                "source_deadline_exceeded",
            ), (), Counter(), Counter(), Counter()
        report = None
        try:
            report = fetch_shfe_eod(
                request.trading_day,
                timeout_seconds=self._remaining_timeout(deadline),
                max_bytes=min(self._max_bytes, SHFE_EOD_MAX_BYTES),
                opener=self._openers.get("SHFE_INE"),
                clock=self._clock,
            )
        except (ShfeEodError, OSError, TimeoutError, ValueError):
            now = _aware_utc(self._clock(), "observed_at")
            return self._unavailable_source(
                SHFE_EOD_SOURCE_ID,
                requested_venues,
                request.trading_day,
                now,
                "source_fetch_or_parse_failed",
            ), (), Counter(), Counter(), Counter()

        target_groups = {item.product_id.casefold() for item in products}
        scoped_rows = tuple(
            row for row in report.rows if row.product_group_id.casefold() in target_groups
        )
        source_key_counts = Counter(
            (row.product_group_id.casefold(), row.delivery_month) for row in scoped_rows
        )
        duplicate_keys = {key for key, count in source_key_counts.items() if count > 1}
        candidates = tuple(
            item
            for item in catalog.catalog_records
            if item.product.venue in SHFE_INE_EXCHANGE_IDS
            and item.product.venue in {value.venue for value in products}
            and item.product.product_id.casefold() in target_groups
            and _known_candidate(item)
        )
        mapped: list[FuturesEodRankingRecord] = []
        unmatched: Counter[tuple[str, str]] = Counter()
        duplicates: Counter[tuple[str, str]] = Counter()
        ambiguous: Counter[tuple[str, str]] = Counter()
        mapping_blockers = []
        for row in scoped_rows:
            source_key = (row.product_group_id.casefold(), row.delivery_month)
            target_products = tuple(
                item for item in products if item.product_id.casefold() == row.product_group_id.casefold()
            )
            target_keys = tuple((item.venue, item.product_id) for item in target_products)
            if source_key in duplicate_keys:
                for product_key in target_keys:
                    duplicates[product_key] += 1
                continue
            matches = tuple(
                item
                for item in candidates
                if item.product.product_id.casefold() == row.product_group_id.casefold()
                and item.instrument.instrument_id.casefold()
                == (item.product.product_id + row.delivery_month).casefold()
            )
            if not matches:
                for product_key in target_keys:
                    unmatched[product_key] += 1
                continue
            if len(matches) != 1:
                for product_key in target_keys:
                    ambiguous[product_key] += 1
                continue
            candidate = matches[0]
            product = candidate.product
            if product not in target_products:
                for product_key in target_keys:
                    ambiguous[product_key] += 1
                continue
            try:
                mapped.append(
                    _record_for(
                        candidate,
                        product,
                        request.trading_day,
                        row.open_interest,
                        row.volume,
                    )
                )
            except (TypeError, ValueError):
                mapping_blockers.append("ranking_value_invalid")
        if unmatched:
            mapping_blockers.append("source_rows_unmatched")
        if duplicates:
            mapping_blockers.append("source_rows_duplicated")
        if ambiguous:
            mapping_blockers.append("source_rows_ambiguous")
        completeness = (
            FuturesEodRankingCompleteness.PARTIAL
            if mapping_blockers
            else FuturesEodRankingCompleteness.COMPLETE
        )
        now = _aware_utc(self._clock(), "observed_at")
        coverage = FuturesEodSourceCoverage(
            source_ref=SHFE_EOD_SOURCE_ID,
            venues=requested_venues,
            snapshot_ref=report.snapshot_id,
            source_digest=report.source_sha256,
            trading_day=report.report_date,
            reported_update_at=report.reported_update_at.astimezone(_UTC),
            retrieved_at=report.retrieved_at.astimezone(_UTC),
            observed_at=now,
            source_row_count=report.source_row_count,
            ranking_row_count=len(mapped),
            excluded_row_count=(
                report.product_summary_row_count
                + report.overall_summary_row_count
                + report.nonordinary_row_count
            ),
            unmatched_row_count=sum(unmatched.values()),
            ambiguous_row_count=sum(ambiguous.values()),
            duplicate_row_count=sum(duplicates.values()),
            completeness=completeness,
            blockers=tuple(mapping_blockers),
        )
        return coverage, tuple(mapped), unmatched, duplicates, ambiguous

    def _unavailable_source(
        self,
        source_ref: str,
        venues: tuple[str, ...],
        trading_day: date,
        observed_at: datetime,
        blocker: str,
    ) -> FuturesEodSourceCoverage:
        return FuturesEodSourceCoverage(
            source_ref=source_ref,
            venues=venues,
            snapshot_ref=None,
            source_digest=None,
            trading_day=trading_day,
            reported_update_at=None,
            retrieved_at=None,
            observed_at=observed_at,
            source_row_count=0,
            ranking_row_count=0,
            excluded_row_count=0,
            unmatched_row_count=0,
            ambiguous_row_count=0,
            duplicate_row_count=0,
            completeness=FuturesEodRankingCompleteness.UNAVAILABLE,
            blockers=(blocker,),
        )

    def _remaining_timeout(self, deadline: datetime) -> float:
        try:
            remaining = (deadline - _aware_utc(self._clock(), "now")).total_seconds()
        except (TypeError, ValueError):
            raise CtpEodRankingError("deadline_invalid") from None
        return min(self._timeout_seconds, max(0.001, remaining))


def _merge_product_counts(
    destination: Counter[tuple[str, str]],
    source: Counter[tuple[str, str]],
) -> None:
    for key, count in source.items():
        destination[key] += count


__all__ = ["CtpEodRankingError", "CtpEodRankingProvider"]
