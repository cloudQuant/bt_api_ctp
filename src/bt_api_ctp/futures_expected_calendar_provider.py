"""CTP package owner for reviewed 2026 expected futures calendars.

The module projects only the package's explicitly reviewed rule set. It does
not call a CTP query, infer that the rules are an observed exchange calendar,
or accept caller-provided holiday/session rules. Current trading days and
selected-contract expiry rows are request scope and must be sourced from an
independently validated neutral snapshot by SDK composition.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
import hashlib
import json
from threading import RLock
from typing import Any, Mapping
from weakref import WeakValueDictionary

from bt_api_base.futures_expected_calendar_protocol import (
    FuturesExpectedCalendarEvidence,
    FuturesExpectedCalendarError,
    FuturesExpectedCalendarNotice,
    FuturesExpectedCalendarPort,
    FuturesExpectedCalendarRequest,
    FuturesExpectedCalendarSnapshot,
    FuturesExpectedCalendarStatus,
    FuturesExpectedProductDay,
    FuturesExpectedProductDayKey,
    FuturesExpectedSessionWindow,
    FuturesExpectedTradingDay,
    FuturesExpectedVenueCalendar,
    FuturesProductLastTradingDay,
    SelectedFuturesExpectedCalendarRoute,
)
from bt_api_base.futures_market_protocol import FuturesProductKey


EXPECTED_RULE_EVIDENCE_MODE = "expected_rule_derived"
RULE_REVIEW_VERSION = "public-rule-review-2026-10-26"
RULE_REVIEW_RETRIEVED_AT_UTC = datetime(2026, 10, 26, 15, 23, 19, tzinfo=timezone.utc)
SUPPORTED_START = date(2026, 1, 1)
SUPPORTED_END = date(2026, 12, 31)
CHINA_TIME = timezone(timedelta(hours=8), "China Standard Time")
MAX_PRODUCT_DAY_ROWS = 100_000
_RULE_SOURCE_REF = "ctp.expected-futures-calendar.2026"

STATE_COUNCIL_2026_HOLIDAY_RANGES = (
    (date(2026, 1, 1), date(2026, 1, 3)),
    (date(2026, 2, 15), date(2026, 2, 23)),
    (date(2026, 4, 4), date(2026, 4, 6)),
    (date(2026, 5, 1), date(2026, 5, 5)),
    (date(2026, 6, 19), date(2026, 6, 21)),
    (date(2026, 9, 25), date(2026, 9, 27)),
    (date(2026, 10, 1), date(2026, 10, 7)),
)

# Weekends designated as State Council make-up workdays remain exchange-closed.
STATE_COUNCIL_2026_MAKEUP_WEEKENDS = (
    date(2026, 1, 4),
    date(2026, 2, 14),
    date(2026, 2, 28),
    date(2026, 5, 9),
    date(2026, 9, 20),
    date(2026, 10, 10),
)

STATE_COUNCIL_HOLIDAY_URL = (
    "https://big5.www.gov.cn/gate/big5/www.gov.cn/zhengce/zhengceku/"
    "202511/content_7047091.htm"
)
STATE_COUNCIL_HOLIDAY_REPRODUCTION_URL = (
    "https://www.beijing.gov.cn/cs/gncs/zcwj/202603/t20260327_4568275.html"
)

EXCHANGE_RULE_SOURCE_URLS = {
    "SHFE": (
        "https://www.shfe.com.cn/services/calenderandholidays/tradinghours/",
        "https://www.shfe.com.cn/services/calenderandholidays/holiday/",
        "https://edu.shfe.com.cn/TradingAssistant/RulesIntroduction/518.html",
    ),
    "INE": (
        "https://www.ine.cn/services/calenderandholidays/tradinghours/",
        "https://www.ine.cn/services/calenderandholidays/holiday/",
        "https://www.shfe.com.cn/eng/services/Rules/INERules/202606/t20260603_831946.html",
    ),
    "DCE": (
        "https://www.dce.com.cn/dceg/file/2025-05-29/"
        "1748480876704ff80808197162e7128701971994e0a01313.pdf",
        "https://neris.csrc.gov.cn/falvfagui/rdqsHeader/mainbody?navbarId=2&"
        "secFutrsLawId=2805291336fd4780a93b6268d93caad0",
        "https://finance.eastmoney.com/a/202512173594230398.html",
    ),
    "CZCE": (
        "https://www.czce.com.cn/cn/content_file/flfg/zcjywgz/zcjjygz/2026/3/"
        "68c0dce01f984475be768ed93afd9b4f.pdf",
        "https://www.czce.com.cn/cn/content_file/flfg/zcjywgz/ywbf/2026/5/"
        "a5ad00e02e484498abdbac8ddbfd5c35.pdf",
        "https://www.czce.com.cn/cn/flfg/zcjywgz/pzxz/webinfo/2024/02/"
        "1708568084281851.htm",
        "https://finance.eastmoney.com/a/202512173594248238.html",
        "https://www.founderfu.com/fzzqqh_2019/details_247_85034.html",
    ),
    "CFFEX": (
        "https://www.cffex.com.cn/u/cms/www/202003/27155912c7m4.pdf",
        "https://www.cffex.com.cn/u/cms/www/202003/27165505w3j5.pdf",
        "https://www.cffex.com.cn/u/cms/www/202207/182020307zry.pdf",
        "https://www.cffex.com.cn/u/cms/www/202003/27165822h30l.pdf",
        "https://www.cffex.com.cn/u/cms/www/202003/27165958okn6.pdf",
        "https://www.e-cffex.com.cn/u/cms/www/202007/08152522nwhg.pdf",
        "https://www.e-cffex.com.cn/u/cms/www/202007/08152340rnz2.pdf",
        "https://e-cffex.com.cn/u/cms/www/202007/08152235b2ys.pdf",
        "https://oa.hicend.com.cn/ftpfile/static/file/upload/file/202304/17/"
        "kmaiwuEFxlDM8cuu.pdf",
        "https://www.cffex.com.cn/u/cms/www/202405/31111942nrka.pdf",
        "https://news.bjd.com.cn/2025/12/17/11469627.shtml",
    ),
    "GFEX": (
        "https://www.gfex.com.cn/gfex/sspzb/sspz.shtml",
        "https://www.gfex.com.cn/gfex/sytslqhhy/202307/"
        "9ad927b8ec4c458594e172fd1ada2a9b.shtml",
        "https://www.gfex.com.cn/en/Notices/202601/ecd5e39ce8a04d2eb0a88d47ba2abd26.shtml",
    ),
}

EXCHANGE_ANNUAL_NOTICE_METADATA = {
    "DCE": {
        "issuer": "大连商品交易所",
        "notice_id": "大商所发〔2025〕437号",
        "issued_on": "2025-12-17",
        "source_urls": ("https://finance.eastmoney.com/a/202512173594230398.html",),
        "provenance": "issuer-attributed notice text reproduced by a third-party portal",
        "byte_identity_limit": "not exchange-served bytes; raw notice digest not captured",
    },
    "CZCE": {
        "issuer": "郑州商品交易所",
        "notice_id": "郑商函〔2025〕939号",
        "issued_on": "2025-12-17",
        "source_urls": (
            "https://finance.eastmoney.com/a/202512173594248238.html",
            "https://www.founderfu.com/fzzqqh_2019/details_247_85034.html",
        ),
        "provenance": "issuer-attributed notice text reproduced by third-party portals",
        "byte_identity_limit": "not exchange-served bytes; raw notice digest not captured",
    },
    "CFFEX": {
        "issuer": "中国金融期货交易所",
        "notice_id": "中金所发〔2025〕55号",
        "issued_on": "2025-12-17",
        "source_urls": ("https://news.bjd.com.cn/2025/12/17/11469627.shtml",),
        "provenance": "issuer-attributed notice text reproduced by a third-party portal",
        "byte_identity_limit": "not exchange-served bytes; raw notice digest not captured",
    },
}

EXCHANGE_IDS = tuple(sorted(EXCHANGE_RULE_SOURCE_URLS))
COMMODITY_DAY_SESSIONS = (
    ("day-am-1", time(9, 0), time(10, 15)),
    ("day-am-2", time(10, 30), time(11, 30)),
    ("day-pm", time(13, 30), time(15, 0)),
)
CFFEX_INDEX_SESSIONS = (
    ("day-am", time(9, 30), time(11, 30)),
    ("day-pm", time(13, 0), time(15, 0)),
)
CFFEX_TREASURY_SESSIONS = (
    ("day-am", time(9, 30), time(11, 30)),
    ("day-pm", time(13, 0), time(15, 15)),
)
CFFEX_TREASURY_PRODUCTS = frozenset(("TS", "TF", "T", "TL"))
CFFEX_INDEX_PRODUCTS = frozenset(("IF", "IH", "IC", "IM"))

_LIMITATIONS = (
    "Reviewed expected-rule coverage is limited to 2026.",
    "The normalized output includes reviewed daytime continuous sessions only; night sessions and call auctions are outside this calendar contract.",
    "DCE, CZCE, and CFFEX 2026 annual closure notices were reviewed as issuer-attributed mirror text on 2026-10-26; exchange-served byte identity and raw notice digests were not captured.",
    "The 2026 State Council schedule was cross-checked against a Beijing municipal portal reproduction with an unrelated test breadcrumb; the Gazette PDF was not byte-retrieved in the 2026-10-26 capture.",
    "Rule URLs are public references rather than captured response bytes; indexed rule text was used where direct DCE, CZCE, or CFFEX document retrieval failed.",
    "Exchange-specific exceptional closure dates beyond the annual holiday schedule are not included unless a later provider rule package explicitly supports them.",
    "The CFFEX TL rule is exchange-authored text hosted by a broker mirror; no exchange-served byte identity or latest-revision claim is made.",
    "CFFEX Treasury final-trading-day afternoon is removed only when the exact selected contract last-trading-day overlay is present; the five-session expiry rule excludes such a contract from normal main selection.",
    "The calendar is expected under cited weekday rules and known holiday closures; later exchange notices can change it.",
)


class CtpExpectedCalendarProjectionError(ValueError):
    """Finite CTP calendar provider failure without source or account values."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _sha256(payload: Any) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def expected_trading_days(venue: str, year: int) -> tuple[date, ...]:
    """Expand the explicitly reviewed 2026 weekday/holiday rule for one venue."""

    if type(venue) is not str or venue not in EXCHANGE_IDS:
        raise CtpExpectedCalendarProjectionError("FUTURES_EXPECTED_VENUE_UNSUPPORTED")
    if type(year) is not int or year != 2026:
        raise CtpExpectedCalendarProjectionError("FUTURES_EXPECTED_CALENDAR_YEAR_UNSUPPORTED")
    holidays = tuple(STATE_COUNCIL_2026_HOLIDAY_RANGES)
    values = []
    cursor = SUPPORTED_START
    while cursor <= SUPPORTED_END:
        is_holiday = any(start <= cursor <= end for start, end in holidays)
        if cursor.weekday() < 5 and not is_holiday:
            values.append(cursor)
        cursor += timedelta(days=1)
    if not values:
        raise CtpExpectedCalendarProjectionError("FUTURES_EXPECTED_CALENDAR_EMPTY")
    return tuple(values)


def _manifest_payload(
    overlays: tuple[FuturesProductLastTradingDay, ...],
) -> dict[str, Any]:
    product_days: dict[FuturesProductKey, set[date]] = {}
    for item in overlays:
        if item.last_trading_day is not None:
            product_days.setdefault(item.product, set()).add(item.last_trading_day)
    return {
        "evidence_mode": EXPECTED_RULE_EVIDENCE_MODE,
        "version": RULE_REVIEW_VERSION,
        "supported_range": [SUPPORTED_START.isoformat(), SUPPORTED_END.isoformat()],
        "state_council_holiday_url": STATE_COUNCIL_HOLIDAY_URL,
        "state_council_holiday_reproduction_url": STATE_COUNCIL_HOLIDAY_REPRODUCTION_URL,
        "state_council_holiday_ranges": [
            [start.isoformat(), end.isoformat()] for start, end in STATE_COUNCIL_2026_HOLIDAY_RANGES
        ],
        "state_council_makeup_weekends_closed": [
            day.isoformat() for day in STATE_COUNCIL_2026_MAKEUP_WEEKENDS
        ],
        "exchange_sources": {
            venue: list(EXCHANGE_RULE_SOURCE_URLS[venue]) for venue in EXCHANGE_IDS
        },
        "exchange_annual_notice_metadata": {
            venue: {
                key: list(value) if key == "source_urls" else value
                for key, value in EXCHANGE_ANNUAL_NOTICE_METADATA[venue].items()
            }
            for venue in sorted(EXCHANGE_ANNUAL_NOTICE_METADATA)
        },
        "exchange_closures": {venue: [] for venue in EXCHANGE_IDS},
        "commodity_day_sessions": [
            [session_id, start.isoformat(), end.isoformat()]
            for session_id, start, end in COMMODITY_DAY_SESSIONS
        ],
        "cffex_index_products": sorted(CFFEX_INDEX_PRODUCTS),
        "cffex_treasury_products": sorted(CFFEX_TREASURY_PRODUCTS),
        "cffex_index_sessions": [
            [session_id, start.isoformat(), end.isoformat()]
            for session_id, start, end in CFFEX_INDEX_SESSIONS
        ],
        "cffex_treasury_sessions": [
            [session_id, start.isoformat(), end.isoformat()]
            for session_id, start, end in CFFEX_TREASURY_SESSIONS
        ],
        "selected_contract_last_trading_days": {
            "{}.{}".format(product.venue, product.product_id): [
                day.isoformat() for day in sorted(days)
            ]
            for product, days in sorted(
                product_days.items(), key=lambda pair: (pair[0].venue, pair[0].product_id)
            )
        },
        "limitations": list(_LIMITATIONS),
    }


def _source_urls(venue: str) -> tuple[str, ...]:
    return tuple(
        sorted((STATE_COUNCIL_HOLIDAY_URL, STATE_COUNCIL_HOLIDAY_REPRODUCTION_URL)
               + EXCHANGE_RULE_SOURCE_URLS[venue])
    )


def _window(
    session_id: str,
    trading_day: date,
    start: time,
    end: time,
) -> FuturesExpectedSessionWindow:
    local_start = datetime.combine(trading_day, start, tzinfo=CHINA_TIME)
    local_end = datetime.combine(trading_day, end, tzinfo=CHINA_TIME)
    return FuturesExpectedSessionWindow(
        trading_day=trading_day,
        session_date=trading_day,
        start_at_utc=local_start.astimezone(timezone.utc),
        end_at_utc=local_end.astimezone(timezone.utc),
        session_id=session_id,
    )


def _product_windows(
    product: FuturesProductKey,
    trading_day: date,
    last_days: Mapping[FuturesProductKey, tuple[date, ...]],
) -> tuple[FuturesExpectedSessionWindow, ...] | None:
    if product.venue != "CFFEX":
        intervals = COMMODITY_DAY_SESSIONS
    elif product.product_id in CFFEX_TREASURY_PRODUCTS:
        intervals = CFFEX_TREASURY_SESSIONS
        if trading_day in last_days.get(product, ()):
            intervals = CFFEX_TREASURY_SESSIONS[:1]
    elif product.product_id in CFFEX_INDEX_PRODUCTS:
        intervals = CFFEX_INDEX_SESSIONS
    else:
        return None
    return tuple(_window(session_id, trading_day, start, end) for session_id, start, end in intervals)


class CtpFuturesExpectedCalendarProvider(FuturesExpectedCalendarPort):
    """CTP package's route-bound rule-data provider; it does not query CTP."""

    def __init__(self, route: SelectedFuturesExpectedCalendarRoute) -> None:
        if type(route) is not SelectedFuturesExpectedCalendarRoute:
            raise FuturesExpectedCalendarError(
                "FUTURES_EXPECTED_CALENDAR_PROVIDER_ROUTE_INVALID"
            )
        if route.exchange_name != "CTP___FUTURE":
            raise FuturesExpectedCalendarError(
                "FUTURES_EXPECTED_CALENDAR_PROVIDER_ROUTE_INVALID"
            )
        self._route = route
        self._issued: WeakValueDictionary[int, FuturesExpectedCalendarSnapshot] = (
            WeakValueDictionary()
        )
        self._issued_lock = RLock()

    def expected_trading_days(self, venue: str, year: int) -> tuple[date, ...]:
        return expected_trading_days(venue, year)

    def validate(self, snapshot: FuturesExpectedCalendarSnapshot) -> bool:
        if type(snapshot) is not FuturesExpectedCalendarSnapshot:
            return False
        with self._issued_lock:
            return self._issued.get(id(snapshot)) is snapshot

    def build(
        self,
        request: FuturesExpectedCalendarRequest,
    ) -> FuturesExpectedCalendarSnapshot:
        if type(request) is not FuturesExpectedCalendarRequest:
            raise FuturesExpectedCalendarError("FUTURES_EXPECTED_CALENDAR_REQUEST_INVALID")
        manifest = _sha256(_manifest_payload(request.selected_contract_last_trading_days))
        if request.as_of_utc.year != 2026 or any(
            item.trading_day.year != 2026 for item in request.current_trading_days
        ):
            return self._unavailable(request, manifest, "FUTURES_EXPECTED_CALENDAR_YEAR_UNSUPPORTED")
        if request.as_of_utc < RULE_REVIEW_RETRIEVED_AT_UTC:
            return self._unavailable(
                request,
                manifest,
                "FUTURES_EXPECTED_RULES_NOT_AVAILABLE_AS_OF_REQUEST_TIME",
            )

        current = {item.venue: item.trading_day for item in request.current_trading_days}
        overlays: dict[FuturesProductKey, tuple[date, ...]] = {}
        blockers: list[str] = []
        for item in request.selected_contract_last_trading_days:
            if item.product.venue == "CFFEX" and item.product.product_id in CFFEX_TREASURY_PRODUCTS:
                if item.last_trading_day is None:
                    blockers.append("FUTURES_EXPECTED_LAST_TRADING_DAY_UNAVAILABLE")
                else:
                    overlays[item.product] = tuple(
                        sorted(set(overlays.get(item.product, ()) + (item.last_trading_day,)))
                    )

        calendars = []
        product_days = []
        sources = []
        fifth = []
        missing = []
        venues_by_product: dict[str, tuple[FuturesProductKey, ...]] = {
            venue: tuple(item for item in request.products if item.venue == venue)
            for venue in request.venues
        }
        for venue in request.venues:
            if venue not in EXCHANGE_IDS:
                blockers.append("FUTURES_EXPECTED_VENUE_UNSUPPORTED")
                continue
            days = expected_trading_days(venue, 2026)
            venue_overlays = tuple(
                item
                for item in request.selected_contract_last_trading_days
                if item.product.venue == venue
            )
            for item in venue_overlays:
                if (
                    item.product.venue == "CFFEX"
                    and item.product.product_id in CFFEX_TREASURY_PRODUCTS
                    and item.last_trading_day is None
                ):
                    blockers.append("FUTURES_EXPECTED_LAST_TRADING_DAY_UNAVAILABLE")
                elif (
                    item.product.venue == "CFFEX"
                    and item.product.product_id in CFFEX_TREASURY_PRODUCTS
                    and item.last_trading_day is not None
                    and item.last_trading_day not in days
                ):
                    blockers.append("FUTURES_EXPECTED_LAST_TRADING_DAY_UNSUPPORTED")
            current_day = current[venue]
            try:
                current_index = days.index(current_day)
            except ValueError:
                blockers.append("FUTURES_EXPECTED_CURRENT_DAY_UNSUPPORTED")
                continue
            horizon_index = current_index + 5
            if horizon_index >= len(days):
                blockers.append("FUTURES_EXPECTED_FIVE_DAY_HORIZON_UNAVAILABLE")
                continue
            coverage_days = days[max(0, current_index - 1) : horizon_index + 1]
            venue_products = venues_by_product[venue]
            if len(venue_products) * len(coverage_days) > MAX_PRODUCT_DAY_ROWS:
                blockers.append("FUTURES_EXPECTED_SCOPE_LIMIT_EXCEEDED")
                continue
            source_id = "{}.expected-daytime-2026".format(venue)
            annual_notice = EXCHANGE_ANNUAL_NOTICE_METADATA.get(venue)
            notices = ()
            if annual_notice is not None:
                notices = (
                    FuturesExpectedCalendarNotice(
                        issuer_name=annual_notice["issuer"],
                        notice_ref=annual_notice["notice_id"],
                        issued_on=date.fromisoformat(annual_notice["issued_on"]),
                        source_urls=tuple(annual_notice["source_urls"]),
                        provenance=annual_notice["provenance"],
                        byte_identity_limit=annual_notice["byte_identity_limit"],
                    ),
                )
            source = FuturesExpectedCalendarEvidence(
                venue=venue,
                source_id=source_id,
                source_urls=_source_urls(venue),
                version=RULE_REVIEW_VERSION,
                available_at_utc=RULE_REVIEW_RETRIEVED_AT_UTC,
                retrieved_at_utc=RULE_REVIEW_RETRIEVED_AT_UTC,
                rules_manifest_sha256=manifest,
                limitations=_LIMITATIONS,
                notices=notices,
            )
            sources.append(source)
            calendars.append(
                FuturesExpectedVenueCalendar(
                    venue=venue,
                    coverage_start=coverage_days[0],
                    coverage_through=coverage_days[-1],
                    trading_days=coverage_days,
                    source_id=source_id,
                    complete=True,
                )
            )
            fifth.append(FuturesExpectedTradingDay(venue, days[horizon_index]))
            for product in venue_products:
                for trading_day in coverage_days:
                    windows = _product_windows(product, trading_day, overlays)
                    if windows is None:
                        blockers.append("FUTURES_EXPECTED_PRODUCT_SESSION_CLASSIFICATION_UNKNOWN")
                        missing.append(FuturesExpectedProductDayKey(product, trading_day))
                        continue
                    product_days.append(
                        FuturesExpectedProductDay(
                            product=product,
                            trading_day=trading_day,
                            source_id=source_id,
                            windows=windows,
                        )
                    )

        blockers = sorted(set(blockers))
        status = FuturesExpectedCalendarStatus.COMPLETE if not blockers else FuturesExpectedCalendarStatus.PARTIAL
        result = FuturesExpectedCalendarSnapshot(
            request=request,
            status=status,
            venue_calendars=tuple(calendars),
            product_days=tuple(product_days),
            sources=tuple(sources),
            fifth_future_trading_days=tuple(fifth),
            rules_manifest_sha256=manifest,
            blockers=tuple(blockers),
            missing_product_days=tuple(missing),
        )
        self._remember(result)
        return result

    def _unavailable(
        self,
        request: FuturesExpectedCalendarRequest,
        manifest: str,
        blocker: str,
    ) -> FuturesExpectedCalendarSnapshot:
        result = FuturesExpectedCalendarSnapshot(
            request=request,
            status=FuturesExpectedCalendarStatus.UNAVAILABLE,
            venue_calendars=(),
            product_days=(),
            sources=(),
            fifth_future_trading_days=(),
            rules_manifest_sha256=manifest,
            blockers=(blocker,),
        )
        self._remember(result)
        return result

    def _remember(self, snapshot: FuturesExpectedCalendarSnapshot) -> None:
        with self._issued_lock:
            self._issued[id(snapshot)] = snapshot


def create_ctp_expected_calendar_provider(
    route: SelectedFuturesExpectedCalendarRoute,
) -> CtpFuturesExpectedCalendarProvider:
    return CtpFuturesExpectedCalendarProvider(route)


def register_ctp_futures_expected_calendar_provider(registry: object) -> None:
    if registry is None or not callable(getattr(registry, "register", None)):
        raise FuturesExpectedCalendarError(
            "FUTURES_EXPECTED_CALENDAR_PROVIDER_ROUTE_INVALID"
        )
    registry.register("CTP___FUTURE", create_ctp_expected_calendar_provider)


__all__ = [
    "CFFEX_INDEX_PRODUCTS",
    "CFFEX_TREASURY_PRODUCTS",
    "COMMODITY_DAY_SESSIONS",
    "CtpExpectedCalendarProjectionError",
    "CtpFuturesExpectedCalendarProvider",
    "EXCHANGE_ANNUAL_NOTICE_METADATA",
    "EXCHANGE_IDS",
    "EXCHANGE_RULE_SOURCE_URLS",
    "EXPECTED_RULE_EVIDENCE_MODE",
    "RULE_REVIEW_RETRIEVED_AT_UTC",
    "RULE_REVIEW_VERSION",
    "STATE_COUNCIL_2026_HOLIDAY_RANGES",
    "STATE_COUNCIL_2026_MAKEUP_WEEKENDS",
    "expected_trading_days",
    "register_ctp_futures_expected_calendar_provider",
]
