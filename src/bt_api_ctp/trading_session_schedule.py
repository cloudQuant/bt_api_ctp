"""CTP-owned trading-session schedule with night sessions and call auctions.

Iteration 62 (I62-04).  Venue facts only: which instants are continuous
trading, call auction or closed for one CTP futures product.  Trading days come
from the reviewed 2026 expected calendar
(:func:`bt_api_ctp.futures_expected_calendar_provider.expected_trading_days`);
night-session products and end times follow the exchanges' published trading
hours.  Anything outside the reviewed coverage, or a product missing from the
tables below, answers ``UNKNOWN`` instead of guessing.

Rules encoded (exchange-published, China time UTC+08:00):

* Commodity day sessions: 09:00-10:15, 10:30-11:30, 13:30-15:00.
* A night session on the evening of trading day ``D`` belongs to the next
  trading day ``T``.  It is held only when every calendar day strictly between
  ``D`` and ``T`` is a weekend day (no exchange holiday in between).
* Products with a held night session open with a call auction 20:55-21:00 on
  ``D`` and the next day session has no opening auction.  When no night session
  is held for ``T`` (or the product has none), the day session opens with a call
  auction 08:55-09:00 on ``T``.
"""

from __future__ import annotations

import re
from datetime import date, datetime, time, timedelta, timezone
from functools import lru_cache
from typing import Optional

from bt_api_base.trading_session_protocol import (
    TradingPhase,
    TradingSessionSchedulePort,
    TradingSessionState,
    TradingWindow,
    TradingWindowKind,
)

__all__ = (
    "CALENDAR_REF",
    "CtpTradingSessionSchedule",
    "create_ctp_trading_session_schedule",
    "SESSION_SOURCE_URLS",
    "product_session_profile",
)

CALENDAR_REF = "ctp.trading-sessions.2026.v1"
CHINA_TIME = timezone(timedelta(hours=8), "China Standard Time")
COVERAGE_START = date(2026, 1, 1)
COVERAGE_END = date(2026, 12, 31)

SESSION_SOURCE_URLS = {
    "SHFE": "https://www.shfe.com.cn/services/calenderandholidays/tradinghours/",
    "CZCE": "http://www.czce.com.cn/cn/jysj/jysj/H770303index_1.htm",
    "DCE": "http://www.dce.com.cn/dalianshangpin/ywfw/jystz/ywtz/index.html",
    "INE": "https://www.ine.cn/services/calenderandholidays/tradinghours/",
}

_DAY_SESSIONS = (
    ("day-am-1", time(9, 0), time(10, 15)),
    ("day-am-2", time(10, 30), time(11, 30)),
    ("day-pm", time(13, 30), time(15, 0)),
)
_DAY_AUCTION = ("day-auction", time(8, 55), time(9, 0))
_NIGHT_AUCTION = ("night-auction", time(20, 55), time(21, 0))
_NIGHT_START = time(21, 0)

_T2300 = time(23, 0)
_T0100 = time(1, 0)
_T0230 = time(2, 30)

# (exchange, product) -> night session end (local); None = day-only product.
# Only products whose hours were checked against the exchanges' published
# trading-hour tables are listed; any other product answers UNKNOWN.
_PRODUCT_NIGHT_END: dict[tuple[str, str], Optional[time]] = {
    ("SHFE", "rb"): _T2300,
    ("SHFE", "hc"): _T2300,
    ("SHFE", "bu"): _T2300,
    ("SHFE", "ru"): _T2300,
    ("SHFE", "fu"): _T2300,
    ("SHFE", "sp"): _T2300,
    ("SHFE", "cu"): _T0100,
    ("SHFE", "al"): _T0100,
    ("SHFE", "zn"): _T0100,
    ("SHFE", "pb"): _T0100,
    ("SHFE", "ni"): _T0100,
    ("SHFE", "sn"): _T0100,
    ("SHFE", "ss"): _T0100,
    ("SHFE", "au"): _T0230,
    ("SHFE", "ag"): _T0230,
    ("SHFE", "wr"): None,
    ("INE", "sc"): _T0230,
    ("CZCE", "SA"): _T2300,
    ("CZCE", "FG"): _T2300,
    ("CZCE", "MA"): _T2300,
    ("CZCE", "TA"): _T2300,
    ("CZCE", "SR"): _T2300,
    ("CZCE", "CF"): _T2300,
    ("CZCE", "RM"): _T2300,
    ("CZCE", "OI"): _T2300,
    ("CZCE", "AP"): None,
    ("CZCE", "CJ"): None,
    ("DCE", "i"): _T2300,
    ("DCE", "j"): _T2300,
    ("DCE", "jm"): _T2300,
    ("DCE", "m"): _T2300,
    ("DCE", "y"): _T2300,
    ("DCE", "p"): _T2300,
    ("DCE", "a"): _T2300,
    ("DCE", "c"): _T2300,
    ("DCE", "l"): _T2300,
    ("DCE", "v"): _T2300,
    ("DCE", "pp"): _T2300,
    ("DCE", "eg"): _T2300,
    ("DCE", "jd"): None,
    ("DCE", "lh"): None,
}

_PRODUCT_RE = re.compile(r"^([A-Za-z]+)\d{3,4}$")


def product_session_profile(instrument_id: str) -> Optional[tuple[str, str, Optional[time]]]:
    """Return ``(exchange, product, night_end)`` for a known product, else ``None``.

    CZCE products are upper-case, the other exchanges lower-case; the case of
    the instrument id therefore selects the table row exactly.
    """

    if type(instrument_id) is not str:
        return None
    match = _PRODUCT_RE.match(instrument_id)
    if match is None:
        return None
    product = match.group(1)
    for (exchange, known), night_end in _PRODUCT_NIGHT_END.items():
        if known == product:
            return exchange, product, night_end
    return None


@lru_cache(maxsize=8)
def _trading_days(exchange: str) -> tuple[date, ...]:
    from bt_api_ctp.futures_expected_calendar_provider import expected_trading_days

    return expected_trading_days(exchange, 2026)


def _local(day: date, at: time) -> datetime:
    return datetime.combine(day, at, tzinfo=CHINA_TIME).astimezone(timezone.utc)


class _Coverage(Exception):
    pass


class CtpTradingSessionSchedule(TradingSessionSchedulePort):
    """Neutral schedule for CTP futures products listed in this module."""

    def _windows_for_trading_day(
        self, exchange: str, night_end: Optional[time], trading_day: date
    ) -> list[TradingWindow]:
        days = _trading_days(exchange)
        if not COVERAGE_START <= trading_day <= COVERAGE_END:
            raise _Coverage("calendar_coverage_unavailable")
        windows: list[TradingWindow] = []
        night_held = False
        if night_end is not None:
            index = days.index(trading_day)
            if index == 0:
                gap_start = COVERAGE_START
                previous: Optional[date] = None
            else:
                previous = days[index - 1]
                gap_start = previous + timedelta(days=1)
            gap = [gap_start + timedelta(days=i) for i in range((trading_day - gap_start).days)]
            if previous is None:
                # The previous trading day is outside coverage; a holiday weekday
                # inside the gap proves no night session, anything else is unknown.
                if not any(day.weekday() < 5 for day in gap):
                    raise _Coverage("calendar_coverage_unavailable")
            else:
                night_held = all(day.weekday() >= 5 for day in gap)
            if night_held and previous is not None:
                end_day = previous if night_end > _NIGHT_START else previous + timedelta(days=1)
                windows.append(
                    TradingWindow(
                        trading_day=trading_day,
                        kind=TradingWindowKind.AUCTION,
                        start_utc=_local(previous, _NIGHT_AUCTION[1]),
                        end_utc=_local(previous, _NIGHT_AUCTION[2]),
                        session_id=_NIGHT_AUCTION[0],
                    )
                )
                windows.append(
                    TradingWindow(
                        trading_day=trading_day,
                        kind=TradingWindowKind.CONTINUOUS,
                        start_utc=_local(previous, _NIGHT_START),
                        end_utc=_local(end_day, night_end),
                        session_id="night",
                    )
                )
        if not night_held:
            windows.append(
                TradingWindow(
                    trading_day=trading_day,
                    kind=TradingWindowKind.AUCTION,
                    start_utc=_local(trading_day, _DAY_AUCTION[1]),
                    end_utc=_local(trading_day, _DAY_AUCTION[2]),
                    session_id=_DAY_AUCTION[0],
                )
            )
        for session_id, start, end in _DAY_SESSIONS:
            windows.append(
                TradingWindow(
                    trading_day=trading_day,
                    kind=TradingWindowKind.CONTINUOUS,
                    start_utc=_local(trading_day, start),
                    end_utc=_local(trading_day, end),
                    session_id=session_id,
                )
            )
        return windows

    def windows(
        self, instrument_id: str, start_utc: datetime, end_utc: datetime
    ) -> tuple[TradingWindow, ...]:
        profile = product_session_profile(instrument_id)
        if profile is None:
            return ()
        exchange, _product, night_end = profile
        start_local = start_utc.astimezone(CHINA_TIME).date() - timedelta(days=1)
        end_local = end_utc.astimezone(CHINA_TIME).date() + timedelta(days=1)
        result: list[TradingWindow] = []
        try:
            for day in _trading_days(exchange):
                if start_local <= day <= end_local:
                    result.extend(self._windows_for_trading_day(exchange, night_end, day))
        except _Coverage:
            return ()
        return tuple(
            sorted(
                (w for w in result if w.end_utc > start_utc and w.start_utc < end_utc),
                key=lambda w: w.start_utc,
            )
        )

    def session_state(self, instrument_id: str, at_utc: datetime) -> TradingSessionState:
        at = at_utc.astimezone(timezone.utc)
        profile = product_session_profile(instrument_id)
        if profile is None:
            return TradingSessionState(
                instrument_id=instrument_id,
                at_utc=at,
                phase=TradingPhase.UNKNOWN,
                trading_day=None,
                current_window=None,
                next_continuous=None,
                calendar_ref=CALENDAR_REF,
                reason="product_session_unknown",
            )
        exchange, _product, night_end = profile
        local_day = at.astimezone(CHINA_TIME).date()
        if not (COVERAGE_START - timedelta(days=1) <= local_day <= COVERAGE_END):
            return self._unknown(instrument_id, at, "calendar_coverage_unavailable")
        try:
            days = [
                day
                for day in _trading_days(exchange)
                if local_day - timedelta(days=4) <= day <= local_day + timedelta(days=20)
            ]
            windows: list[TradingWindow] = []
            for day in days:
                windows.extend(self._windows_for_trading_day(exchange, night_end, day))
        except _Coverage as exc:
            return self._unknown(instrument_id, at, str(exc))
        windows.sort(key=lambda w: w.start_utc)
        current = next((w for w in windows if w.contains(at)), None)
        upcoming = next(
            (w for w in windows if w.kind is TradingWindowKind.CONTINUOUS and w.start_utc > at),
            None,
        )
        if current is not None:
            phase = (
                TradingPhase.CONTINUOUS
                if current.kind is TradingWindowKind.CONTINUOUS
                else TradingPhase.AUCTION
            )
            return TradingSessionState(
                instrument_id=instrument_id,
                at_utc=at,
                phase=phase,
                trading_day=current.trading_day,
                current_window=current,
                next_continuous=upcoming,
                calendar_ref=CALENDAR_REF,
            )
        if upcoming is None:
            return self._unknown(instrument_id, at, "calendar_coverage_unavailable")
        return TradingSessionState(
            instrument_id=instrument_id,
            at_utc=at,
            phase=TradingPhase.CLOSED,
            trading_day=upcoming.trading_day,
            current_window=None,
            next_continuous=upcoming,
            calendar_ref=CALENDAR_REF,
        )

    @staticmethod
    def _unknown(instrument_id: str, at: datetime, reason: str) -> TradingSessionState:
        return TradingSessionState(
            instrument_id=instrument_id,
            at_utc=at,
            phase=TradingPhase.UNKNOWN,
            trading_day=None,
            current_window=None,
            next_continuous=None,
            calendar_ref=CALENDAR_REF,
            reason=reason,
        )


def create_ctp_trading_session_schedule(_feed: object = None) -> CtpTradingSessionSchedule:
    """Factory for ``bt_api.trading_session_providers`` (CTP futures)."""

    return CtpTradingSessionSchedule()
