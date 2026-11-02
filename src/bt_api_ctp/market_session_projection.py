"""Resolve a qualified CTP market event against a neutral session schedule.

This adapter owns CTP TradingDay/ActionDay interpretation, the China fixed
UTC+08:00 clock mapping, and the CFFEX index-futures opening-auction baseline.
It does not create a bar grid or mark clocks fresh. A matching schedule cannot
upgrade an unqualified provider event.

The CFFEX IF/IH/IC/IM opening-auction interval is 09:25 to 09:30 local time;
continuous sessions begin at 09:30. Official contract-rule references:

* IF: https://www.cffex.com.cn/u/cms/www/202003/27165505w3j5.pdf
* IH: https://www.cffex.com.cn/u/cms/www/202003/271658063l8q.pdf
* IC: https://www.cffex.com.cn/u/cms/www/202003/27165943rkyd.pdf
* IM: https://www.cffex.com.cn/u/cms/www/202207/182020307zry.pdf
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from typing import Optional

from bt_api_base.market_session_protocol import (
    MarketSessionCalendar,
    MarketSessionProviderRouteError,
    MarketSessionResolution,
    MarketSessionResolutionStatus,
    MarketSessionSegmentKind,
    SelectedMarketSessionRoute,
)
from bt_api_base.market_protocol import MarketInstrumentKey
from bt_api_base.market_tick_protocol import (
    MarketClockQuality,
    MarketFreshnessStatus,
    MarketTickEvent,
)


CTP_MARKET_SESSION_TIME_CONTRACT = "ctp-asia-shanghai-fixed-utc8-v1"
_CHINA_OFFSET = timezone(timedelta(hours=8))
_UTC = timezone.utc
_CFFEX_INDEX_PRODUCTS = frozenset(("IF", "IH", "IC", "IM"))
_CFFEX_AUCTION_START = time(9, 25)
_CFFEX_AUCTION_END = time(9, 30)


class CtpMarketSessionProjectionError(ValueError):
    """Finite, value-free failure while resolving a CTP market session."""

    _CODES = frozenset(
        {
            "CTP_MARKET_SESSION_EVENT_INVALID",
            "CTP_MARKET_SESSION_CALENDAR_INVALID",
            "CTP_MARKET_SESSION_INSTRUMENT_MISMATCH",
        }
    )

    def __init__(self, code: str) -> None:
        self.code = code if type(code) is str and code in self._CODES else "CTP_MARKET_SESSION_EVENT_INVALID"
        super().__init__(self.code)


def _parse_day(value: object) -> Optional[date]:
    if type(value) is not str:
        return None
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.isoformat() == value else None


def _parse_utc(value: object) -> Optional[datetime]:
    if type(value) is not str:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    normalized = parsed.astimezone(_UTC)
    return normalized if normalized.isoformat(timespec="microseconds").replace("+00:00", "Z") == value else None


def _unknown(
    event: MarketTickEvent,
    calendar: MarketSessionCalendar,
    *,
    status: MarketSessionResolutionStatus,
    blocker: str,
    trading_date: Optional[date],
    event_session_date: Optional[date],
    event_time_utc: Optional[datetime],
) -> MarketSessionResolution:
    data = event.data
    return MarketSessionResolution(
        status=status,
        instrument=calendar.instrument,
        product=calendar.product,
        trading_date=trading_date,
        session_date=None,
        event_session_date=event_session_date,
        event_time_utc=event_time_utc,
        session_id=None,
        segment_kind=MarketSessionSegmentKind.UNKNOWN,
        calendar_ref=calendar.calendar_ref,
        calendar_digest=calendar.calendar_digest,
        source_clock_quality=data.source_clock_quality,
        receive_clock_quality=data.receive_clock_quality,
        freshness_status=data.freshness_status,
        blockers=(blocker,),
    )


def resolve_ctp_market_session(
    event: MarketTickEvent,
    calendar: MarketSessionCalendar,
) -> MarketSessionResolution:
    """Resolve one exact typed CTP event against a neutral product schedule.

    The schedule remains framework-owned input. This provider adapter only
    maps a source-qualified event to that schedule and reports explicit CTP
    auction semantics. Event times are matched to UTC half-open windows; CTP's
    local ActionDay-equivalent must also be listed by the selected window.

    ``COMPLETE`` requires the event's existing source and receive clocks to be
    verified and fresh. The adapter never changes those quality observations.
    """

    if type(event) is not MarketTickEvent:
        raise CtpMarketSessionProjectionError("CTP_MARKET_SESSION_EVENT_INVALID")
    if type(calendar) is not MarketSessionCalendar:
        raise CtpMarketSessionProjectionError("CTP_MARKET_SESSION_CALENDAR_INVALID")
    if (
        event.key.instrument != calendar.instrument
        or event.key.instrument.provider != "ctp"
        or event.data.provider_source != "ctp.native.md"
    ):
        raise CtpMarketSessionProjectionError("CTP_MARKET_SESSION_INSTRUMENT_MISMATCH")

    data = event.data
    trading_date = _parse_day(data.trading_date)
    event_session_date = _parse_day(data.event_session_date)
    event_time_utc = _parse_utc(event.clock.provider_event_time_utc)
    received_at_utc = _parse_utc(event.clock.received_at_utc)

    if not calendar.complete:
        return _unknown(
            event,
            calendar,
            status=MarketSessionResolutionStatus.UNAVAILABLE,
            blocker="market_session_calendar_incomplete",
            trading_date=trading_date,
            event_session_date=event_session_date,
            event_time_utc=event_time_utc,
        )
    if (
        event.data.event_time_provenance != "provider_reported"
        or event_time_utc is None
        or event_time_utc != _parse_utc(data.provider_event_time_utc)
        or trading_date is None
        or event_session_date is None
    ):
        return _unknown(
            event,
            calendar,
            status=MarketSessionResolutionStatus.UNKNOWN,
            blocker="ctp_provider_event_time_unavailable",
            trading_date=trading_date,
            event_session_date=event_session_date,
            event_time_utc=event_time_utc,
        )
    if not event.data_clock_qualified:
        return _unknown(
            event,
            calendar,
            status=MarketSessionResolutionStatus.UNKNOWN,
            blocker="market_clock_unqualified",
            trading_date=trading_date,
            event_session_date=event_session_date,
            event_time_utc=event_time_utc,
        )
    if received_at_utc is None or event_time_utc > received_at_utc:
        return _unknown(
            event,
            calendar,
            status=MarketSessionResolutionStatus.UNKNOWN,
            blocker="ctp_event_time_after_receive_time",
            trading_date=trading_date,
            event_session_date=event_session_date,
            event_time_utc=event_time_utc,
        )
    if trading_date not in calendar.trading_dates:
        return _unknown(
            event,
            calendar,
            status=MarketSessionResolutionStatus.UNKNOWN,
            blocker="ctp_trading_date_outside_calendar",
            trading_date=trading_date,
            event_session_date=event_session_date,
            event_time_utc=event_time_utc,
        )

    local_event = event_time_utc.astimezone(_CHINA_OFFSET)
    if local_event.date() != event_session_date:
        return _unknown(
            event,
            calendar,
            status=MarketSessionResolutionStatus.UNKNOWN,
            blocker="ctp_event_session_date_mismatch",
            trading_date=trading_date,
            event_session_date=event_session_date,
            event_time_utc=event_time_utc,
        )

    trading_windows = tuple(
        window for window in calendar.windows if window.trading_date == trading_date
    )
    matched = next(
        (
            window
            for window in trading_windows
            if window.start_at_utc <= event_time_utc < window.end_at_utc
        ),
        None,
    )
    if matched is not None:
        if event_session_date not in matched.event_session_dates:
            return _unknown(
                event,
                calendar,
                status=MarketSessionResolutionStatus.UNKNOWN,
                blocker="ctp_event_date_not_in_session_window",
                trading_date=trading_date,
                event_session_date=event_session_date,
                event_time_utc=event_time_utc,
            )
        return _complete(
            event,
            calendar,
            trading_date=trading_date,
            session_date=matched.session_date,
            event_session_date=event_session_date,
            event_time_utc=event_time_utc,
            session_id=matched.session_id,
            segment_kind=MarketSessionSegmentKind.CONTINUOUS,
        )

    local_clock = local_event.timetz().replace(tzinfo=None)
    cffex_opening_auction = (
        calendar.product.venue == "CFFEX"
        and calendar.product.product_id in _CFFEX_INDEX_PRODUCTS
        and local_event.date() == trading_date
        and event_session_date == trading_date
        and _CFFEX_AUCTION_START <= local_clock < _CFFEX_AUCTION_END
    )
    if cffex_opening_auction:
        continuous_open = datetime.combine(
            trading_date,
            _CFFEX_AUCTION_END,
            tzinfo=_CHINA_OFFSET,
        ).astimezone(_UTC)
        if not any(
            window.start_at_utc == continuous_open
            and window.session_date == trading_date
            for window in trading_windows
        ):
            return _unknown(
                event,
                calendar,
                status=MarketSessionResolutionStatus.UNKNOWN,
                blocker="cffex_opening_auction_boundary_unproven",
                trading_date=trading_date,
                event_session_date=event_session_date,
                event_time_utc=event_time_utc,
            )
        return _complete(
            event,
            calendar,
            trading_date=trading_date,
            session_date=trading_date,
            event_session_date=event_session_date,
            event_time_utc=event_time_utc,
            session_id=None,
            segment_kind=MarketSessionSegmentKind.OPENING_AUCTION_BASELINE,
        )

    event_day_windows = tuple(
        window for window in trading_windows if event_session_date in window.event_session_dates
    )
    if event_day_windows:
        session_dates = {window.session_date for window in event_day_windows}
        if len(session_dates) == 1:
            return _complete(
                event,
                calendar,
                trading_date=trading_date,
                session_date=next(iter(session_dates)),
                event_session_date=event_session_date,
                event_time_utc=event_time_utc,
                session_id=None,
                segment_kind=MarketSessionSegmentKind.OUTSIDE,
            )
    blocker = (
        "ctp_event_session_date_ambiguous"
        if event_day_windows
        else "ctp_event_session_date_outside_calendar"
    )
    return _unknown(
        event,
        calendar,
        status=MarketSessionResolutionStatus.UNKNOWN,
        blocker=blocker,
        trading_date=trading_date,
        event_session_date=event_session_date,
        event_time_utc=event_time_utc,
    )


def _complete(
    event: MarketTickEvent,
    calendar: MarketSessionCalendar,
    *,
    trading_date: date,
    session_date: date,
    event_session_date: date,
    event_time_utc: datetime,
    session_id: Optional[str],
    segment_kind: MarketSessionSegmentKind,
) -> MarketSessionResolution:
    data = event.data
    return MarketSessionResolution(
        status=MarketSessionResolutionStatus.COMPLETE,
        instrument=calendar.instrument,
        product=calendar.product,
        trading_date=trading_date,
        session_date=session_date,
        event_session_date=event_session_date,
        event_time_utc=event_time_utc,
        session_id=session_id,
        segment_kind=segment_kind,
        calendar_ref=calendar.calendar_ref,
        calendar_digest=calendar.calendar_digest,
        source_clock_quality=data.source_clock_quality,
        receive_clock_quality=data.receive_clock_quality,
        freshness_status=data.freshness_status,
        blockers=(),
    )


class CtpMarketSessionResolver:
    """CTP resolver bound to one SDK-selected neutral instrument."""

    __slots__ = ("_instrument",)

    def __init__(self, route: SelectedMarketSessionRoute) -> None:
        if type(route) is not SelectedMarketSessionRoute:
            raise MarketSessionProviderRouteError("MARKET_SESSION_PROVIDER_ROUTE_INVALID")
        if (
            route.exchange_name != "CTP___FUTURE"
            or type(route.instrument) is not MarketInstrumentKey
            or route.instrument.provider != "ctp"
        ):
            raise MarketSessionProviderRouteError("MARKET_SESSION_PROVIDER_ROUTE_INVALID")
        self._instrument = route.instrument

    def resolve(
        self,
        event: MarketTickEvent,
        calendar: MarketSessionCalendar,
    ) -> MarketSessionResolution:
        if type(event) is not MarketTickEvent:
            raise CtpMarketSessionProjectionError("CTP_MARKET_SESSION_EVENT_INVALID")
        if type(calendar) is not MarketSessionCalendar:
            raise CtpMarketSessionProjectionError("CTP_MARKET_SESSION_CALENDAR_INVALID")
        if (
            calendar.instrument != self._instrument
            or event.key.instrument != self._instrument
            or event.key.instrument.provider != "ctp"
            or event.data.provider_source != "ctp.native.md"
        ):
            raise CtpMarketSessionProjectionError("CTP_MARKET_SESSION_INSTRUMENT_MISMATCH")
        return resolve_ctp_market_session(event, calendar)


__all__ = [
    "CTP_MARKET_SESSION_TIME_CONTRACT",
    "CtpMarketSessionResolver",
    "CtpMarketSessionProjectionError",
    "resolve_ctp_market_session",
]
