"""Project sealed CTP reference queries into neutral futures market rows.

The query service owns native acquisition, live session revalidation, and
QuerySnapshot provenance. This module only projects results already returned
by that service after its active ``_validate_native_result`` check. It repeats
seal, scope, and row-digest checks, but never issues QuerySnapshot evidence. A
completed native query is not a freshness attestation, and exchange-open status
is never guessed from wall time.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from types import MappingProxyType
from typing import Any

from bt_api_base.futures_market_protocol import (
    FUTURES_CATALOG_FAMILY,
    FUTURES_DEPTH_FAMILY,
    FUTURES_MARKET_FAMILIES,
    FUTURES_PRODUCT_UNIVERSE_FAMILY,
    FUTURES_QUOTES_FAMILY,
    FUTURES_SESSION_FAMILY,
    FuturesAssetType,
    FuturesCatalogDisposition,
    FuturesCatalogRecord,
    FuturesContractKind,
    FuturesDepthLevel,
    FuturesDepthRecord,
    FuturesProductKey,
    FuturesQuantityUnit,
    FuturesQuoteRecord,
    FuturesSessionRecord,
    FuturesSessionStatus,
    FuturesVolumeSemantics,
)
from bt_api_base.market_protocol import MarketInstrumentKey
from bt_api_base.query_protocol import QueryCompleteness, QueryRequest
from bt_api_ctp.instrument import normalize_ctp_instrument
from bt_api_ctp.query import (
    _QUERY_MONOTONIC_CLOCK_DOMAIN,
    _QUERY_SOURCE_SEAL,
    _QuerySource,
    QueryResult,
    _query_records_digest,
)
from bt_api_ctp.registered_sim_session import CtpQuerySessionBinding, _BINDING_SEAL


_CTP_DATE_RE = re.compile(r"[0-9]{8}\Z", re.ASCII)
_EXPECTED_NATIVE_FILTERS = {
    FUTURES_CATALOG_FAMILY: {"InstrumentID": ""},
    FUTURES_PRODUCT_UNIVERSE_FAMILY: {
        "ProductClass": "1",
        "ExchangeID": "",
        "ProductID": "",
    },
    FUTURES_QUOTES_FAMILY: {"InstrumentID": "", "ExchangeID": ""},
    FUTURES_DEPTH_FAMILY: {"InstrumentID": "", "ExchangeID": ""},
}
_EXPECTED_NATIVE_TYPES = {
    FUTURES_CATALOG_FAMILY: "instruments",
    FUTURES_PRODUCT_UNIVERSE_FAMILY: "products",
    FUTURES_QUOTES_FAMILY: "depth_market_data",
    FUTURES_DEPTH_FAMILY: "depth_market_data",
}
_NORMALIZED_CTP_CONTRACT_KINDS = {
    "future": FuturesContractKind.FUTURE,
    "option": FuturesContractKind.OPTION,
    "combination": FuturesContractKind.NON_FUTURES,
    "spot": FuturesContractKind.NON_FUTURES,
    "efp": FuturesContractKind.NON_FUTURES,
    "spot_option": FuturesContractKind.NON_FUTURES,
}
# No reviewed ProductID-to-neutral-asset mapping is currently bundled. Keep
# future rows unclassified until a versioned SDK-owned source table is added.
_CTP_PRODUCT_ASSET_TYPES: Mapping[tuple[str, str], FuturesAssetType] = MappingProxyType({})

_MISSING = object()
_QUOTE_FIELDS = frozenset(
    {
        "last_price",
        "open_interest",
        "volume",
        "bid_price",
        "bid_volume",
        "ask_price",
        "ask_volume",
    }
)


@dataclass(frozen=True)
class CtpFuturesFamilyProjection:
    """Neutral row projection consumed by CtpProviderQueryService.

    This object deliberately omits provider source, generation, request, and
    snapshot identifiers. The query service must bind those from its sealed
    QueryResult and exact current CtpQuerySessionBinding when creating each
    QuerySnapshot.
    """

    family: str
    records: tuple[Mapping[str, Any], ...]
    completeness: QueryCompleteness
    scope_complete: bool | None
    blockers: tuple[str, ...]

    def __post_init__(self) -> None:
        if type(self.family) is not str or self.family not in FUTURES_MARKET_FAMILIES:
            raise ValueError("family is not a published neutral futures family")
        if type(self.records) not in (tuple, list):
            raise TypeError("records must be a tuple or list")
        records = tuple(self.records)
        if any(not isinstance(row, Mapping) for row in records):
            raise TypeError("records must contain neutral mappings")
        if type(self.completeness) is not QueryCompleteness:
            raise TypeError("completeness must be a QueryCompleteness")
        if self.scope_complete is not None and type(self.scope_complete) is not bool:
            raise TypeError("scope_complete must be a bool or None")
        if type(self.blockers) not in (tuple, list):
            raise TypeError("blockers must be a tuple or list")
        blockers = tuple(self.blockers)
        if any(type(code) is not str or not code for code in blockers):
            raise ValueError("blockers must contain finite non-empty codes")
        if self.completeness is QueryCompleteness.COMPLETE:
            if blockers or self.scope_complete is not True:
                raise ValueError("complete projection requires a complete blocker-free scope")
        elif self.completeness in (QueryCompleteness.UNAVAILABLE, QueryCompleteness.UNKNOWN):
            if records or self.scope_complete is not None or not blockers:
                raise ValueError("unavailable projection requires no rows and a blocker")
        elif self.completeness is QueryCompleteness.PARTIAL:
            if self.scope_complete is not False or not blockers:
                raise ValueError("partial projection requires blockers and an incomplete scope")
        object.__setattr__(self, "records", records)
        object.__setattr__(self, "blockers", tuple(sorted(set(blockers))))


@dataclass(frozen=True)
class _RequestScope:
    environment: str
    trading_day: date | None
    venues: tuple[str, ...] | None
    products: tuple[FuturesProductKey, ...] | None = None
    instruments: tuple[MarketInstrumentKey, ...] | None = None
    contract_kinds: tuple[FuturesContractKind, ...] = (FuturesContractKind.FUTURE,)
    require_active_session: bool = False
    required_quote_fields: tuple[str, ...] = ("open_interest", "volume")
    depth_levels: int = 5


@dataclass(frozen=True)
class _CatalogRows:
    projection: CtpFuturesFamilyProjection
    all_rows: Mapping[tuple[str, str], FuturesCatalogRecord]


def project_futures_market_batch(
    requests_by_family: Mapping[str, QueryRequest],
    results_by_family: Mapping[str, QueryResult[Any] | None],
    binding: CtpQuerySessionBinding,
) -> Mapping[str, CtpFuturesFamilyProjection]:
    """Project one CTP futures query batch after service-side validation.

    Catalog and quote/depth native reads must cover the selected CTP route:
    their sealed native request filters are required to be empty. QueryResult
    source seals, raw-row digests, request families, and connection-session
    identity are checked again here. The caller still owns live-session and
    source-expiry revalidation immediately before and after this function.
    """

    request_map = _validate_requests(requests_by_family)
    result_map = _validate_results(results_by_family, request_map)
    if type(binding) is not CtpQuerySessionBinding or binding._seal is not _BINDING_SEAL:
        raise TypeError("binding must be an SDK-issued CtpQuerySessionBinding")

    parsed: dict[str, _RequestScope] = {}
    request_errors: dict[str, str] = {}
    for family, request in request_map.items():
        try:
            parsed[family] = _parse_request_scope(request)
        except (TypeError, ValueError):
            request_errors[family] = "futures_request_filter_invalid"

    shared_scope_error = _shared_scope_error(parsed, request_errors)
    if shared_scope_error is not None:
        return MappingProxyType(
            {
                family: _unavailable(family, shared_scope_error)
                for family in request_map
            }
        )

    environment = binding.environment
    if environment is None:
        return MappingProxyType(
            {
                family: _unavailable(family, "neutral_environment_unavailable")
                for family in request_map
            }
        )
    try:
        session_day = _ctp_date(binding.trading_day)
    except (TypeError, ValueError):
        return MappingProxyType(
            {
                family: _unknown(family, "sealed_session_trading_day_invalid")
                for family in request_map
            }
        )

    request_day = next(iter(parsed.values())).trading_day
    if request_day is not None and request_day != session_day:
        return MappingProxyType(
            {
                family: _unavailable(family, "requested_trading_day_unavailable")
                for family in request_map
            }
        )
    if any(scope.environment != environment for scope in parsed.values()):
        return MappingProxyType(
            {
                family: _unavailable(family, "query_environment_not_current")
                for family in request_map
            }
        )

    projections: dict[str, CtpFuturesFamilyProjection] = {}
    if FUTURES_PRODUCT_UNIVERSE_FAMILY in request_map:
        product_request = request_map[FUTURES_PRODUCT_UNIVERSE_FAMILY]
        if not _valid_required_request(product_request):
            projections[FUTURES_PRODUCT_UNIVERSE_FAMILY] = _unavailable(
                FUTURES_PRODUCT_UNIVERSE_FAMILY, "futures_complete_scope_required"
            )
        else:
            projections[FUTURES_PRODUCT_UNIVERSE_FAMILY] = _project_product_universe(
                result_map.get(FUTURES_PRODUCT_UNIVERSE_FAMILY),
                parsed[FUTURES_PRODUCT_UNIVERSE_FAMILY],
                binding,
            )
    if FUTURES_SESSION_FAMILY in request_map:
        session_request = request_map[FUTURES_SESSION_FAMILY]
        session_scope = parsed[FUTURES_SESSION_FAMILY]
        if not _valid_required_request(session_request):
            projections[FUTURES_SESSION_FAMILY] = _unavailable(
                FUTURES_SESSION_FAMILY, "futures_complete_scope_required"
            )
        else:
            route_session = FuturesSessionRecord(
                venue=None,
                environment=environment,
                trading_day=session_day,
                session_status=FuturesSessionStatus.UNKNOWN,
            )
            session_blockers = (
                ("exchange_session_status_unavailable",)
                if session_scope.require_active_session
                else ()
            )
            projections[FUTURES_SESSION_FAMILY] = _projection(
                FUTURES_SESSION_FAMILY,
                (route_session.to_dict(),),
                session_blockers,
            )

    market_families = {FUTURES_QUOTES_FAMILY, FUTURES_DEPTH_FAMILY}.intersection(request_map)
    catalog_rows: _CatalogRows | None = None
    if FUTURES_CATALOG_FAMILY in request_map or market_families:
        catalog_scope = parsed.get(FUTURES_CATALOG_FAMILY)
        if catalog_scope is None:
            catalog_scope = parsed.get(FUTURES_QUOTES_FAMILY) or parsed.get(FUTURES_DEPTH_FAMILY)
        assert catalog_scope is not None
        catalog_rows = _project_catalog(
            result_map.get(FUTURES_CATALOG_FAMILY), catalog_scope, binding
        )
        if FUTURES_CATALOG_FAMILY in request_map:
            projections[FUTURES_CATALOG_FAMILY] = catalog_rows.projection

    if market_families:
        if (
            market_families == {FUTURES_DEPTH_FAMILY}
            and parsed[FUTURES_DEPTH_FAMILY].depth_levels > 5
        ):
            projections[FUTURES_DEPTH_FAMILY] = _unavailable(
                FUTURES_DEPTH_FAMILY, "ctp_depth_levels_exceed_provider_limit"
            )
            return MappingProxyType(projections)
        quote_result = result_map.get(FUTURES_QUOTES_FAMILY)
        depth_result = result_map.get(FUTURES_DEPTH_FAMILY)
        market_result = quote_result if quote_result is not None else depth_result
        if (
            FUTURES_QUOTES_FAMILY in request_map
            and FUTURES_DEPTH_FAMILY in request_map
            and quote_result is not None
            and depth_result is not None
            and depth_result is not quote_result
        ):
            for family in market_families:
                projections[family] = _unknown(
                    family, "quote_depth_native_result_not_shared"
                )
        else:
            assert catalog_rows is not None
            projections.update(
                _project_market_rows(
                    market_result,
                    request_map,
                    parsed,
                    catalog_rows,
                    binding,
                    session_day,
                )
            )

    return MappingProxyType(projections)


def _validate_requests(value: Any) -> dict[str, QueryRequest]:
    if not isinstance(value, Mapping) or not value:
        raise TypeError("requests_by_family must be a non-empty mapping")
    requests: dict[str, QueryRequest] = {}
    for family, request in value.items():
        if type(family) is not str or family not in FUTURES_MARKET_FAMILIES:
            raise ValueError("requests contain an unsupported futures family")
        if type(request) is not QueryRequest or request.family != family:
            raise TypeError("request family must match its exact QueryRequest")
        requests[family] = request
    return requests


def _validate_results(
    value: Any,
    requests: Mapping[str, QueryRequest],
) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise TypeError("results_by_family must be a mapping")
    extra = set(value) - set(requests)
    market_requested = bool(
        {FUTURES_QUOTES_FAMILY, FUTURES_DEPTH_FAMILY}.intersection(requests)
    )
    allowed_internal = {FUTURES_CATALOG_FAMILY} if market_requested else set()
    if extra - allowed_internal:
        raise ValueError("results contain an unsupported internal dependency")
    results: dict[str, Any] = {}
    for family, result in value.items():
        if family == FUTURES_SESSION_FAMILY and result is not None:
            raise ValueError("session records must come from the sealed query binding")
        results[family] = result
    return results


def _parse_request_scope(request: QueryRequest) -> _RequestScope:
    filters = request.filters
    base_fields = {"environment", "trading_day", "venues"}
    family_fields = {
        FUTURES_SESSION_FAMILY: base_fields | {"require_active_session"},
        FUTURES_CATALOG_FAMILY: base_fields
        | {"products", "instruments", "contract_kinds"},
        FUTURES_PRODUCT_UNIVERSE_FAMILY: base_fields | {"products"},
        FUTURES_QUOTES_FAMILY: base_fields
        | {
            "products",
            "instruments",
            "contract_kinds",
            "required_quote_fields",
            "required_open_interest_unit",
            "required_volume_unit",
            "required_volume_semantics",
        },
        FUTURES_DEPTH_FAMILY: base_fields
        | {
            "products",
            "instruments",
            "contract_kinds",
            "depth_levels",
            "required_depth_volume_unit",
        },
    }
    if set(filters) != family_fields[request.family]:
        raise ValueError("family filters do not match the published neutral request")
    environment = filters.get("environment")
    if type(environment) is not str or not environment or environment != environment.strip():
        raise ValueError("environment is invalid")
    day_value = filters.get("trading_day")
    trading_day = None if day_value is None else _iso_date(day_value)
    venues = _optional_text_tuple(filters.get("venues"), "venues")

    products: tuple[FuturesProductKey, ...] | None = None
    instruments: tuple[MarketInstrumentKey, ...] | None = None
    contract_kinds = (FuturesContractKind.FUTURE,)
    require_active = False
    quote_fields = ("open_interest", "volume")
    depth_levels = 5
    if request.family == FUTURES_PRODUCT_UNIVERSE_FAMILY:
        products = _product_tuple(filters.get("products"))
    elif request.family != FUTURES_SESSION_FAMILY:
        products = _product_tuple(filters.get("products"))
        instruments = _instrument_tuple(filters.get("instruments"))
        kinds_value = filters.get("contract_kinds")
        if type(kinds_value) not in (tuple, list) or not kinds_value:
            raise ValueError("contract_kinds is invalid")
        contract_kinds = tuple(FuturesContractKind(item) for item in kinds_value)
        if (
            len(set(contract_kinds)) != len(contract_kinds)
            or FuturesContractKind.UNKNOWN in contract_kinds
            or FuturesContractKind.NON_FUTURES in contract_kinds
        ):
            raise ValueError("contract_kinds is invalid")
    if request.family == FUTURES_SESSION_FAMILY:
        require_active_value = filters.get("require_active_session")
        if type(require_active_value) is not bool:
            raise ValueError("require_active_session is invalid")
        require_active = require_active_value
    elif request.family == FUTURES_QUOTES_FAMILY:
        quote_fields_value = filters.get("required_quote_fields")
        if type(quote_fields_value) not in (tuple, list) or not quote_fields_value:
            raise ValueError("required_quote_fields is invalid")
        quote_fields = tuple(quote_fields_value)
        if (
            any(type(item) is not str or item not in _QUOTE_FIELDS for item in quote_fields)
            or len(set(quote_fields)) != len(quote_fields)
        ):
            raise ValueError("required_quote_fields is invalid")
        _validate_optional_enum(
            filters.get("required_open_interest_unit"), FuturesQuantityUnit, "quantity unit"
        )
        _validate_optional_enum(
            filters.get("required_volume_unit"), FuturesQuantityUnit, "quantity unit"
        )
        _validate_optional_enum(
            filters.get("required_volume_semantics"),
            FuturesVolumeSemantics,
            "volume semantics",
        )
    elif request.family == FUTURES_DEPTH_FAMILY:
        depth_levels_value = filters.get("depth_levels")
        if type(depth_levels_value) is not int or not 1 <= depth_levels_value <= 1000:
            raise ValueError("depth_levels is invalid")
        depth_levels = depth_levels_value
        _validate_optional_enum(
            filters.get("required_depth_volume_unit"), FuturesQuantityUnit, "quantity unit"
        )

    if instruments is not None:
        if any(item.environment != environment or item.provider != "ctp" for item in instruments):
            raise ValueError("instrument filter is outside CTP environment")
        if venues is not None and any(item.venue not in venues for item in instruments):
            raise ValueError("instrument filter is outside requested venues")
    if products is not None and venues is not None:
        if any(item.venue not in venues for item in products):
            raise ValueError("product filter is outside requested venues")
    return _RequestScope(
        environment=environment,
        trading_day=trading_day,
        venues=venues,
        products=products,
        instruments=instruments,
        contract_kinds=contract_kinds,
        require_active_session=require_active,
        required_quote_fields=quote_fields,
        depth_levels=depth_levels,
    )


def _shared_scope_error(
    parsed: Mapping[str, _RequestScope],
    request_errors: Mapping[str, str],
) -> str | None:
    if request_errors:
        return "futures_request_filter_invalid"
    if not parsed:
        return "futures_request_filter_invalid"
    first = next(iter(parsed.values()))
    if any(
        (scope.environment, scope.trading_day, scope.venues)
        != (first.environment, first.trading_day, first.venues)
        for scope in parsed.values()
    ):
        return "futures_family_scope_mismatch"
    resource_scopes = [
        scope
        for family, scope in parsed.items()
        if family
        in {FUTURES_CATALOG_FAMILY, FUTURES_QUOTES_FAMILY, FUTURES_DEPTH_FAMILY}
    ]
    if resource_scopes:
        resource = resource_scopes[0]
        if any(
            (scope.products, scope.instruments, scope.contract_kinds)
            != (resource.products, resource.instruments, resource.contract_kinds)
            for scope in resource_scopes
        ):
            return "futures_family_resource_scope_mismatch"
    product_scope = parsed.get(FUTURES_PRODUCT_UNIVERSE_FAMILY)
    if product_scope is not None and any(
        scope.products != product_scope.products for scope in resource_scopes
    ):
        return "futures_product_universe_filter_mismatch"
    return None


def _valid_required_request(request: QueryRequest) -> bool:
    return request.required is True and request.require_complete_scope is True


def _validate_optional_enum(value: Any, enum_type: type, name: str) -> None:
    if value is None:
        return
    if type(value) is not str:
        raise ValueError(f"{name} is invalid")
    parsed = enum_type(value)
    if parsed.value == "unknown":
        raise ValueError(f"{name} cannot require an unknown value")


def _product_tuple(value: Any) -> tuple[FuturesProductKey, ...] | None:
    if value is None:
        return None
    if type(value) not in (tuple, list) or not value:
        raise ValueError("products is invalid")
    products = tuple(FuturesProductKey.from_dict(item) for item in value)
    if len({(item.venue, item.product_id) for item in products}) != len(products):
        raise ValueError("products contain duplicates")
    return products


def _instrument_tuple(value: Any) -> tuple[MarketInstrumentKey, ...] | None:
    if value is None:
        return None
    if type(value) not in (tuple, list) or not value:
        raise ValueError("instruments is invalid")
    instruments = tuple(MarketInstrumentKey.from_dict(item) for item in value)
    identities = tuple(_instrument_identity(item) for item in instruments)
    if len(set(identities)) != len(identities):
        raise ValueError("instruments contain duplicates")
    return instruments


def _optional_text_tuple(value: Any, name: str) -> tuple[str, ...] | None:
    if value is None:
        return None
    if type(value) not in (tuple, list) or not value:
        raise ValueError(f"{name} is invalid")
    items = tuple(value)
    if any(type(item) is not str or not item or item != item.strip() for item in items):
        raise ValueError(f"{name} is invalid")
    if len(set(items)) != len(items):
        raise ValueError(f"{name} contains duplicates")
    return items


def _project_catalog(
    result: QueryResult[Any] | None,
    scope: _RequestScope,
    binding: CtpQuerySessionBinding,
) -> _CatalogRows:
    issue = _validate_native_result(result, FUTURES_CATALOG_FAMILY, binding)
    if issue is not None:
        return _CatalogRows(_failed_projection(FUTURES_CATALOG_FAMILY, issue), MappingProxyType({}))
    assert result is not None
    blockers: list[str] = []
    records: list[Mapping[str, Any]] = []
    all_rows: dict[tuple[str, str], FuturesCatalogRecord] = {}
    observed_venues: set[str] = set()
    seen: set[tuple[str, str]] = set()
    for row in result.records:
        observed_venue = _native_text(_native_value(row, "ExchangeID"))
        if observed_venue is not None:
            observed_venues.add(observed_venue)
        native = _native_catalog_row(row, scope, blockers)
        if native is None:
            continue
        record, identity = native
        if identity in seen:
            blockers.append("catalog_duplicate_instrument_identity")
            continue
        seen.add(identity)
        all_rows[identity] = record
        if _in_scope(record.instrument, record.product, record.contract_kind, scope):
            records.append(record.to_dict())

    if scope.venues is not None:
        missing = set(scope.venues) - observed_venues
        if missing:
            blockers.append("requested_venue_scope_unobserved")
    issue = _validate_native_result(result, FUTURES_CATALOG_FAMILY, binding)
    if issue is not None:
        return _CatalogRows(_unknown(FUTURES_CATALOG_FAMILY, issue), MappingProxyType({}))
    return _CatalogRows(
        _projection(FUTURES_CATALOG_FAMILY, records, blockers),
        MappingProxyType(all_rows),
    )


def _project_product_universe(
    result: QueryResult[Any] | None,
    scope: _RequestScope,
    binding: CtpQuerySessionBinding,
) -> CtpFuturesFamilyProjection:
    """Project the sealed, all-visible CTP futures-product result into keys.

    The native query is required to select ProductClass ``1`` (futures) with
    empty exchange and product filters.  The terminal result therefore proves
    the complete route-visible futures product universe, including products
    with no instrument rows.  Request filters are applied only after that
    complete set has been sealed; requested missing keys make the projection
    partial rather than silently shrinking the universe.
    """
    issue = _validate_native_result(result, FUTURES_PRODUCT_UNIVERSE_FAMILY, binding)
    if issue is not None:
        return _failed_projection(FUTURES_PRODUCT_UNIVERSE_FAMILY, issue)
    assert result is not None

    blockers: list[str] = []
    observed: dict[tuple[str, str], FuturesProductKey] = {}
    for row in result.records:
        venue = _native_text(_native_value(row, "ExchangeID"))
        if venue is None:
            blockers.append("product_universe_venue_unavailable")
            continue
        try:
            normalized_kind = normalize_ctp_instrument(row).get("contract_type")
        except Exception:
            normalized_kind = None
        if normalized_kind != "future":
            blockers.append("product_universe_contract_kind_mismatch")
            continue
        if scope.venues is not None and venue not in scope.venues:
            continue
        product_id = _native_text(_native_value(row, "ProductID"))
        if product_id is None:
            blockers.append("product_universe_product_id_unavailable")
            continue
        try:
            product = FuturesProductKey(venue=venue, product_id=product_id)
        except (TypeError, ValueError):
            blockers.append("product_universe_identity_invalid")
            continue
        identity = (venue, product_id)
        if identity in observed:
            blockers.append("product_universe_duplicate_product_identity")
            continue
        observed[identity] = product

    if scope.products is not None:
        requested = {(item.venue, item.product_id) for item in scope.products}
        missing = requested - set(observed)
        if missing:
            blockers.append("product_universe_requested_product_unobserved")
        observed = {key: value for key, value in observed.items() if key in requested}

    issue = _validate_native_result(result, FUTURES_PRODUCT_UNIVERSE_FAMILY, binding)
    if issue is not None:
        return _unknown(FUTURES_PRODUCT_UNIVERSE_FAMILY, issue)
    products = tuple(
        product.to_dict()
        for _key, product in sorted(observed.items(), key=lambda item: item[0])
    )
    return _projection(FUTURES_PRODUCT_UNIVERSE_FAMILY, products, blockers)


def _project_market_rows(
    result: QueryResult[Any] | None,
    requests: Mapping[str, QueryRequest],
    scopes: Mapping[str, _RequestScope],
    catalog: _CatalogRows,
    binding: CtpQuerySessionBinding,
    session_day: date,
) -> dict[str, CtpFuturesFamilyProjection]:
    families = [
        family
        for family in (FUTURES_QUOTES_FAMILY, FUTURES_DEPTH_FAMILY)
        if family in requests
    ]
    scope = scopes.get(FUTURES_QUOTES_FAMILY) or scopes.get(FUTURES_DEPTH_FAMILY)
    assert scope is not None
    want_quotes = FUTURES_QUOTES_FAMILY in requests
    depth_scope = scopes.get(FUTURES_DEPTH_FAMILY)
    if not catalog.all_rows:
        return {
            family: _unavailable(family, "futures_catalog_scope_unavailable")
            for family in families
        }
    for family in families:
        if not _valid_required_request(requests[family]):
            return {
                item: _unavailable(item, "futures_complete_scope_required")
                for item in families
            }
    issue = _validate_native_result(result, FUTURES_QUOTES_FAMILY, binding)
    if issue is not None:
        return {family: _failed_projection(family, issue) for family in families}

    assert result is not None
    if depth_scope is not None and depth_scope.depth_levels > 5:
        depth_unavailable = _unavailable(
            FUTURES_DEPTH_FAMILY, "ctp_depth_levels_exceed_provider_limit"
        )
    else:
        depth_unavailable = None

    quote_records: list[Mapping[str, Any]] = []
    depth_records: list[Mapping[str, Any]] = []
    quote_blockers: list[str] = []
    depth_blockers: list[str] = []
    if catalog.projection.completeness is not QueryCompleteness.COMPLETE:
        if want_quotes:
            quote_blockers.append("futures_catalog_scope_incomplete")
        if depth_scope is not None:
            depth_blockers.append("futures_catalog_scope_incomplete")
    seen_quotes: set[tuple[str, str]] = set()
    seen_depth: set[tuple[str, str]] = set()
    quote_depth = depth_scope.depth_levels if depth_scope is not None else 5

    for row in result.records:
        venue = _native_text(_native_value(row, "ExchangeID"))
        instrument_id = _native_text(_native_value(row, "InstrumentID"))
        if venue is not None and scope.venues is not None and venue not in scope.venues:
            continue
        if venue is None or instrument_id is None:
            if want_quotes:
                quote_blockers.append("market_data_identity_unavailable")
            if depth_scope is not None:
                depth_blockers.append("market_data_identity_unavailable")
            continue

        key = (venue, instrument_id)
        catalog_record = catalog.all_rows.get(key)
        if catalog_record is None:
            if not _native_row_could_match_scope(row, key, scope):
                continue
            if scope.products is not None and _native_text(_native_value(row, "ProductID")) is None:
                if want_quotes:
                    quote_blockers.append("market_data_product_identity_unavailable")
                if depth_scope is not None:
                    depth_blockers.append("market_data_product_identity_unavailable")
                continue
            market_instrument = _market_instrument_identity(
                key, scope.environment, FuturesContractKind.UNKNOWN
            )
            if want_quotes:
                quote_blockers.append("market_data_instrument_not_in_catalog")
            if depth_scope is not None:
                depth_blockers.append("market_data_instrument_not_in_catalog")
        else:
            if not _in_scope(
                catalog_record.instrument,
                catalog_record.product,
                catalog_record.contract_kind,
                scope,
            ):
                continue
            market_instrument = catalog_record.instrument
            if catalog_record.contract_kind is FuturesContractKind.UNKNOWN:
                if want_quotes:
                    quote_blockers.append("market_data_contract_kind_unavailable")
                if depth_scope is not None:
                    depth_blockers.append("market_data_contract_kind_unavailable")
            elif catalog_record.contract_kind not in scope.contract_kinds:
                continue
            if catalog_record.disposition is FuturesCatalogDisposition.UNKNOWN:
                if want_quotes:
                    quote_blockers.append("market_data_catalog_classification_unavailable")
                if depth_scope is not None:
                    depth_blockers.append("market_data_catalog_classification_unavailable")
            elif catalog_record.disposition is FuturesCatalogDisposition.EXCLUDED:
                continue
            if catalog_record.is_trading is False:
                continue
            if catalog_record.is_trading is None:
                if want_quotes:
                    quote_blockers.append("market_data_catalog_tradability_unavailable")
                if depth_scope is not None:
                    depth_blockers.append("market_data_catalog_tradability_unavailable")

        row_day_blockers: list[str] = []
        trading_day = _optional_ctp_date(
            _native_value(row, "TradingDay"), row_day_blockers, "market_data_trading_day_invalid"
        )
        if trading_day is None or trading_day != session_day:
            row_day_blockers.append("market_data_trading_day_mismatch")
        if want_quotes:
            quote_blockers.extend(row_day_blockers)
        if depth_scope is not None:
            depth_blockers.extend(row_day_blockers)
        if trading_day is None or trading_day != session_day:
            continue
        event_blockers: list[str] = []
        event_date = _optional_event_date(_native_value(row, "ActionDay"), event_blockers)
        event_time = _optional_event_time(_native_value(row, "UpdateTime"), event_blockers)
        event_millisecond = _optional_event_millisecond(
            _native_value(row, "UpdateMillisec"), event_blockers
        )
        if want_quotes:
            quote_blockers.extend(event_blockers)
        if depth_scope is not None:
            depth_blockers.extend(event_blockers)

        if want_quotes:
            quote = _quote_record(
                row,
                market_instrument,
                trading_day,
                event_date,
                event_time,
                event_millisecond,
                scope.required_quote_fields,
                quote_blockers,
            )
            if key in seen_quotes:
                quote_blockers.append("market_data_duplicate_quote_identity")
            else:
                seen_quotes.add(key)
                quote_records.append(quote.to_dict())

        if depth_scope is not None and depth_unavailable is None:
            depth_record = _depth_record(
                row,
                market_instrument,
                trading_day,
                event_date,
                event_time,
                event_millisecond,
                quote_depth,
                depth_blockers,
            )
            if key in seen_depth:
                depth_blockers.append("market_data_duplicate_depth_identity")
            else:
                seen_depth.add(key)
                depth_records.append(depth_record.to_dict())

    issue = _validate_native_result(result, FUTURES_QUOTES_FAMILY, binding)
    if issue is not None:
        return {family: _unknown(family, issue) for family in families}

    output: dict[str, CtpFuturesFamilyProjection] = {}
    if want_quotes:
        output[FUTURES_QUOTES_FAMILY] = _projection(
            FUTURES_QUOTES_FAMILY, quote_records, quote_blockers
        )
    if depth_scope is not None:
        output[FUTURES_DEPTH_FAMILY] = (
            depth_unavailable
            if depth_unavailable is not None
            else _projection(FUTURES_DEPTH_FAMILY, depth_records, depth_blockers)
        )
    return output


def _quote_record(
    row: Any,
    instrument: MarketInstrumentKey,
    trading_day: date,
    event_date: date | None,
    event_time: str | None,
    event_millisecond: int | None,
    required_fields: tuple[str, ...],
    blockers: list[str],
) -> FuturesQuoteRecord:
    values: dict[str, Decimal | None] = {}
    field_names = {
        "last_price": "LastPrice",
        "open_interest": "OpenInterest",
        "volume": "Volume",
        "bid_price": "BidPrice1",
        "bid_volume": "BidVolume1",
        "ask_price": "AskPrice1",
        "ask_volume": "AskVolume1",
    }
    for neutral_name, native_name in field_names.items():
        value = _decimal_value(
            _native_value(row, native_name),
            positive=neutral_name in {"last_price", "bid_price", "ask_price"},
            nonnegative=neutral_name in {"open_interest", "volume", "bid_volume", "ask_volume"},
        )
        values[neutral_name] = value
        if _native_value(row, native_name) is not _MISSING and value is None:
            blockers.append("market_data_numeric_field_invalid")
    for field_name in required_fields:
        if values.get(field_name) is None:
            blockers.append("market_data_required_quote_field_unavailable")
    return FuturesQuoteRecord(
        instrument=instrument,
        trading_day=trading_day,
        last_price=values["last_price"],
        open_interest=values["open_interest"],
        volume=values["volume"],
        open_interest_unit=FuturesQuantityUnit.CONTRACTS,
        volume_unit=FuturesQuantityUnit.CONTRACTS,
        volume_semantics=FuturesVolumeSemantics.CUMULATIVE_TRADING_DAY,
        bid_price=values["bid_price"],
        bid_volume=values["bid_volume"],
        ask_price=values["ask_price"],
        ask_volume=values["ask_volume"],
        provider_event_date=event_date,
        provider_event_time=event_time,
        provider_event_millisecond=event_millisecond,
    )


def _depth_record(
    row: Any,
    instrument: MarketInstrumentKey,
    trading_day: date,
    event_date: date | None,
    event_time: str | None,
    event_millisecond: int | None,
    levels: int,
    blockers: list[str],
) -> FuturesDepthRecord:
    bids: list[FuturesDepthLevel] = []
    asks: list[FuturesDepthLevel] = []
    invalid = False
    for index in range(1, levels + 1):
        bid_price = _decimal_value(_native_value(row, f"BidPrice{index}"), positive=True)
        bid_volume = _decimal_value(_native_value(row, f"BidVolume{index}"), nonnegative=True)
        ask_price = _decimal_value(_native_value(row, f"AskPrice{index}"), positive=True)
        ask_volume = _decimal_value(_native_value(row, f"AskVolume{index}"), nonnegative=True)
        if bid_price is None or bid_volume is None or ask_price is None or ask_volume is None:
            invalid = True
            continue
        bids.append(FuturesDepthLevel(price=bid_price, volume=bid_volume))
        asks.append(FuturesDepthLevel(price=ask_price, volume=ask_volume))
    complete_state = not invalid and len(bids) == levels and len(asks) == levels
    if not complete_state:
        blockers.append("market_data_depth_levels_incomplete")
    try:
        return FuturesDepthRecord(
            instrument=instrument,
            trading_day=trading_day,
            bids=tuple(bids),
            asks=tuple(asks),
            complete_state=complete_state,
            volume_unit=FuturesQuantityUnit.CONTRACTS,
            provider_event_date=event_date,
            provider_event_time=event_time,
            provider_event_millisecond=event_millisecond,
        )
    except (TypeError, ValueError):
        blockers.append("market_data_depth_record_invalid")
        # An invalid price order cannot be represented by the neutral DTO.
        return FuturesDepthRecord(
            instrument=instrument,
            trading_day=trading_day,
            bids=(),
            asks=(),
            complete_state=False,
            volume_unit=FuturesQuantityUnit.CONTRACTS,
            provider_event_date=event_date,
            provider_event_time=event_time,
            provider_event_millisecond=event_millisecond,
        )


def _validate_native_result(
    result: QueryResult[Any] | None,
    family: str,
    binding: CtpQuerySessionBinding,
) -> str | None:
    if result is None:
        return "futures_native_query_unavailable"
    expected_type = _EXPECTED_NATIVE_TYPES[family]
    if type(result) is not QueryResult:
        return "futures_native_result_type_untrusted"
    if (
        result.request_type != expected_type
        or type(result.request_id) is not int
        or result.request_id <= 0
        or type(result.connection_generation) is not int
        or result.complete is not True
        or result.is_last_seen is not True
        or result.timed_out is not False
        or result.unsupported is not False
        or result.completion_origin != "success"
        or result.error_message != ""
        or type(result.error_code) not in (int, type(None))
        or result.error_code not in (None, 0)
        or type(result.submit_code) not in (int, type(None))
        or result.submit_code not in (None, 0)
        or type(result.late_callback_count) is not int
        or result.late_callback_count != 0
        or result.record_limit is not None
        or type(result.record_limit_exceeded) is not bool
        or result.record_limit_exceeded is not False
        or type(result.records) is not tuple
        or not _valid_utc_datetime(result.started_at_utc)
        or not _valid_utc_datetime(result.completed_at_utc)
        or result.started_at_utc > result.completed_at_utc
        or result.connection_generation != binding.connection_generation
        or result.account_fingerprint != binding.account_fingerprint
    ):
        return "futures_native_result_incomplete_or_unbound"
    source = result.query_source
    if type(source) is not _QuerySource or source._seal is not _QUERY_SOURCE_SEAL:
        return "futures_native_source_unsealed"
    expected_filters = _EXPECTED_NATIVE_FILTERS[family]
    if (
        source.issuer is not binding._issuer
        or source.source_tags != binding._source_tags
        or source.request_type != expected_type
        or type(source.request_id) is not int
        or source.request_id != result.request_id
        or source.connection_generation != binding.connection_generation
        or source.account_fingerprint != binding.account_fingerprint
        or source.trading_day != binding.trading_day
        or source.started_at_utc != result.started_at_utc
        or source.completed_at_utc != result.completed_at_utc
        or source.clock_domain_id != _QUERY_MONOTONIC_CLOCK_DOMAIN
        or not _finite_monotonic(source.started_monotonic)
        or not _finite_monotonic(source.completed_monotonic)
        or source.started_monotonic > source.completed_monotonic
        or not _native_filter_map_matches(source.request_intent_filters, expected_filters)
        or not _native_filter_map_matches(source.request_filters, expected_filters)
        or source.explicit_request_filters != ()
        or source.request_parameters != ()
        or source.request_intent_parameters != ()
        or source.records_sha256 is None
    ):
        return "futures_native_source_scope_untrusted"
    try:
        if _query_records_digest(result.records) != source.records_sha256:
            return "futures_native_records_digest_mismatch"
    except Exception:
        return "futures_native_records_digest_unavailable"
    return None


def _valid_utc_datetime(value: Any) -> bool:
    return bool(
        type(value) is datetime
        and value.tzinfo is not None
        and value.utcoffset() is not None
        and value.utcoffset().total_seconds() == 0
    )


def _finite_monotonic(value: Any) -> bool:
    return bool(type(value) is float and value >= 0 and value < float("inf"))


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


def _native_catalog_row(
    row: Any,
    scope: _RequestScope,
    blockers: list[str],
) -> tuple[FuturesCatalogRecord, tuple[str, str]] | None:
    instrument_id = _native_text(_native_value(row, "InstrumentID"))
    venue = _native_text(_native_value(row, "ExchangeID"))
    product_id = _native_text(_native_value(row, "ProductID"))
    if venue is not None and scope.venues is not None and venue not in scope.venues:
        return None
    if scope.instruments is not None and instrument_id is not None and not any(
        item.venue == venue and item.instrument_id == instrument_id
        for item in scope.instruments
    ):
        return None
    if scope.products is not None and product_id is not None and not any(
        item.venue == venue and item.product_id == product_id for item in scope.products
    ):
        return None
    if instrument_id is None or venue is None or product_id is None:
        blockers.append("catalog_identity_or_product_unavailable")
        return None

    try:
        normalized_kind = normalize_ctp_instrument(row).get("contract_type")
    except Exception:
        normalized_kind = None
    contract_kind = _NORMALIZED_CTP_CONTRACT_KINDS.get(
        normalized_kind, FuturesContractKind.UNKNOWN
    )
    try:
        instrument = MarketInstrumentKey(
            provider="ctp",
            venue=venue,
            environment=scope.environment,
            product_type=contract_kind.value,
            instrument_id=instrument_id,
            native_symbol=instrument_id,
        )
        product = FuturesProductKey(venue=venue, product_id=product_id)
    except (TypeError, ValueError):
        blockers.append("catalog_identity_or_product_invalid")
        return None
    is_trading = _trading_flag(_native_value(row, "IsTrading"))
    expiry = _optional_ctp_date(
        _native_value(row, "ExpireDate"), blockers, "catalog_expiry_date_invalid"
    )
    underlying = _native_text(_native_value(row, "UnderlyingInstrID"))
    option_type = _native_text(_native_value(row, "OptionsType"))
    option_right = {"1": "call", "2": "put"}.get(option_type)
    strike = _decimal_value(_native_value(row, "StrikePrice"), positive=True)

    if contract_kind is FuturesContractKind.NON_FUTURES:
        asset_type = FuturesAssetType.UNKNOWN
        disposition = FuturesCatalogDisposition.EXCLUDED
        reason = "known_non_futures_contract_kind"
    elif contract_kind is FuturesContractKind.UNKNOWN:
        asset_type = FuturesAssetType.UNKNOWN
        disposition = FuturesCatalogDisposition.UNKNOWN
        reason = "contract_kind_classification_unavailable"
        blockers.append("catalog_contract_kind_unavailable")
    elif contract_kind not in scope.contract_kinds:
        asset_type = FuturesAssetType.UNKNOWN
        disposition = FuturesCatalogDisposition.EXCLUDED
        reason = "contract_kind_not_requested"
    else:
        asset_type = _CTP_PRODUCT_ASSET_TYPES.get((venue, product_id), FuturesAssetType.UNKNOWN)
        if asset_type is FuturesAssetType.UNKNOWN:
            disposition = FuturesCatalogDisposition.UNKNOWN
            reason = "neutral_asset_classification_unavailable"
            blockers.append("catalog_asset_classification_unavailable")
        else:
            disposition = FuturesCatalogDisposition.INCLUDED
            reason = "sdk_product_classification"

    if contract_kind in scope.contract_kinds and disposition is not FuturesCatalogDisposition.EXCLUDED:
        if is_trading is None:
            blockers.append("catalog_tradability_unavailable")
        if expiry is None:
            blockers.append("catalog_expiry_unavailable")
    if contract_kind is FuturesContractKind.OPTION and contract_kind in scope.contract_kinds:
        if option_type is not None and option_right is None:
            option_right = "unknown"
            blockers.append("catalog_option_right_unavailable")
        if underlying is None:
            blockers.append("catalog_option_underlying_unavailable")
        if strike is None:
            blockers.append("catalog_option_strike_unavailable")
    try:
        record = FuturesCatalogRecord(
            instrument=instrument,
            product=product,
            asset_type=asset_type,
            contract_kind=contract_kind,
            is_trading=is_trading,
            expiry_date=expiry,
            disposition=disposition,
            disposition_reason=reason,
            underlying_instrument_id=underlying if contract_kind is FuturesContractKind.OPTION else None,
            option_right=option_right if contract_kind is FuturesContractKind.OPTION else None,
            strike_price=strike if contract_kind is FuturesContractKind.OPTION else None,
        )
    except (TypeError, ValueError):
        blockers.append("catalog_neutral_record_invalid")
        return None
    return record, (venue, instrument_id)


def _in_scope(
    instrument: MarketInstrumentKey,
    product: FuturesProductKey,
    contract_kind: FuturesContractKind,
    scope: _RequestScope,
) -> bool:
    if scope.venues is not None and instrument.venue not in scope.venues:
        return False
    if scope.products is not None and product not in scope.products:
        return False
    if scope.instruments is not None and instrument not in scope.instruments:
        return False
    return contract_kind is FuturesContractKind.UNKNOWN or contract_kind in scope.contract_kinds


def _native_row_could_match_scope(
    row: Any,
    identity: tuple[str, str],
    scope: _RequestScope,
) -> bool:
    venue, instrument_id = identity
    if scope.venues is not None and venue not in scope.venues:
        return False
    if scope.instruments is not None and not any(
        item.venue == venue
        and item.instrument_id == instrument_id
        and item.native_symbol == instrument_id
        for item in scope.instruments
    ):
        return False
    if scope.products is not None:
        product_id = _native_text(_native_value(row, "ProductID"))
        if product_id is not None and not any(
            item.venue == venue and item.product_id == product_id for item in scope.products
        ):
            return False
    return True


def _market_instrument_identity(
    identity: tuple[str, str],
    environment: str,
    contract_kind: FuturesContractKind,
) -> MarketInstrumentKey:
    venue, instrument_id = identity
    return MarketInstrumentKey(
        provider="ctp",
        venue=venue,
        environment=environment,
        product_type=contract_kind.value,
        instrument_id=instrument_id,
        native_symbol=instrument_id,
    )


def _ctp_date(value: Any) -> date:
    text = _native_text(value)
    if text is None or _CTP_DATE_RE.fullmatch(text) is None:
        raise ValueError("CTP date is unavailable")
    return date(int(text[:4]), int(text[4:6]), int(text[6:8]))


def _iso_date(value: Any) -> date:
    if type(value) is not str:
        raise TypeError("date filter must be a string")
    parsed = date.fromisoformat(value)
    if parsed.isoformat() != value:
        raise ValueError("date filter is not canonical")
    return parsed


def _optional_ctp_date(value: Any, blockers: list[str], code: str) -> date | None:
    if value is _MISSING or value is None or value in ("", b""):
        return None
    try:
        return _ctp_date(value)
    except (TypeError, ValueError):
        blockers.append(code)
        return None


def _optional_event_date(value: Any, blockers: list[str]) -> date | None:
    return _optional_ctp_date(value, blockers, "market_data_event_date_invalid")


def _optional_event_time(value: Any, blockers: list[str]) -> str | None:
    if value is _MISSING or value is None or value in ("", b""):
        return None
    text = _native_text(value)
    if text is None or len(text) != 8 or text[2] != ":" or text[5] != ":":
        blockers.append("market_data_event_time_invalid")
        return None
    digits = text[:2] + text[3:5] + text[6:]
    if not digits.isdigit():
        blockers.append("market_data_event_time_invalid")
        return None
    hour, minute, second = int(digits[:2]), int(digits[2:4]), int(digits[4:6])
    if hour > 23 or minute > 59 or second > 59:
        blockers.append("market_data_event_time_invalid")
        return None
    return text


def _optional_event_millisecond(value: Any, blockers: list[str]) -> int | None:
    if value is _MISSING or value is None:
        return None
    if type(value) is int and 0 <= value <= 999:
        return value
    blockers.append("market_data_event_millisecond_invalid")
    return None


def _trading_flag(value: Any) -> bool | None:
    if type(value) is bool:
        return value
    if type(value) is int and value in (0, 1):
        return bool(value)
    if type(value) is str and value in ("0", "1"):
        return value == "1"
    if type(value) is bytes and value in (b"0", b"1"):
        return value == b"1"
    return None


def _decimal_value(
    value: Any,
    *,
    positive: bool = False,
    nonnegative: bool = False,
) -> Decimal | None:
    if value is _MISSING or value is None or type(value) is bool:
        return None
    if type(value) not in (int, float, str, Decimal):
        return None
    try:
        parsed = value if type(value) is Decimal else Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        return None
    if not parsed.is_finite() or len(format(parsed, "f")) > 256:
        return None
    if parsed.copy_abs() >= Decimal("1e308"):
        return None
    if positive and parsed <= 0:
        return None
    if nonnegative and parsed < 0:
        return None
    return parsed


def _native_value(row: Any, name: str) -> Any:
    if isinstance(row, Mapping):
        return row.get(name, _MISSING)
    try:
        return getattr(row, name)
    except Exception:
        return _MISSING


def _native_text(value: Any) -> str | None:
    if value is _MISSING or value is None:
        return None
    if type(value) is bytes:
        try:
            value = value.decode("ascii", errors="strict")
        except UnicodeDecodeError:
            return None
    if type(value) is not str:
        return None
    text = value.strip().strip("\x00").strip()
    if not text or not text.isprintable():
        return None
    return text


def _projection(
    family: str,
    records: Sequence[Mapping[str, Any]],
    blockers: Sequence[str],
) -> CtpFuturesFamilyProjection:
    codes = tuple(sorted(set(blockers)))
    if codes:
        return CtpFuturesFamilyProjection(
            family=family,
            records=tuple(records),
            completeness=QueryCompleteness.PARTIAL,
            scope_complete=False,
            blockers=codes,
        )
    return CtpFuturesFamilyProjection(
        family=family,
        records=tuple(records),
        completeness=QueryCompleteness.COMPLETE,
        scope_complete=True,
        blockers=(),
    )


def _unavailable(family: str, blocker: str) -> CtpFuturesFamilyProjection:
    return CtpFuturesFamilyProjection(
        family=family,
        records=(),
        completeness=QueryCompleteness.UNAVAILABLE,
        scope_complete=None,
        blockers=(blocker,),
    )


def _unknown(family: str, blocker: str) -> CtpFuturesFamilyProjection:
    return CtpFuturesFamilyProjection(
        family=family,
        records=(),
        completeness=QueryCompleteness.UNKNOWN,
        scope_complete=None,
        blockers=(blocker,),
    )


def _failed_projection(family: str, issue: str) -> CtpFuturesFamilyProjection:
    # A missing query is definitely unavailable. A present but untrusted or
    # incomplete result has an uncertain provider outcome and stays UNKNOWN.
    if issue == "futures_native_query_unavailable":
        return _unavailable(family, issue)
    return _unknown(family, issue)


def _instrument_identity(instrument: MarketInstrumentKey) -> tuple[str, ...]:
    return (
        instrument.provider,
        instrument.venue,
        instrument.environment,
        instrument.product_type,
        instrument.instrument_id,
        instrument.native_symbol,
    )


__all__ = [
    "CtpFuturesFamilyProjection",
    "project_futures_market_batch",
]
