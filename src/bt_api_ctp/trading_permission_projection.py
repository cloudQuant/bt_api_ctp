"""CTP native rows -> neutral trading-permission facts (iteration 62, D62-25).

Each function receives one *complete* query result of the current session (or
``None`` when that query was incomplete, failed, stale or skipped) and returns
one :class:`~bt_api_ctp.trading_permission_facts.PermissionFact`.  Field
meanings follow the bundled ``ThostFtdcUserApiDataType.h``:

* ``CThostFtdcInvestorField.IsActive``           1 active / 0 inactive;
* ``CThostFtdcTradingCodeField.IsActive``        1 active / 0 inactive;
  ``ClientIDType`` '1' speculation, '2' arbitrage, '3' hedge, '5' market maker;
* ``CThostFtdcInstrumentField.IsTrading``        1 / 0 (reference flag);
  ``InstLifePhase`` '0' not started, '1' started, '2' paused, '3' expired.

Definitive negatives are deliberately narrow: an inactive investor, no active
trading code at all on the exchange (absent or every row inactive), an
instrument row that is not trading, expired or not yet started.  Everything
else that is not clearly positive stays ``unknown`` (an instrument missing
from the query, a paused instrument, active codes without a speculation code,
unreadable or unattributable rows, missing rate rows).  Direct orders use the
speculation hedge flag, so only an active speculation code counts as positive.
"""

from __future__ import annotations

from typing import Any, Optional

from bt_api_ctp.trading_permission_facts import (
    KNOWN_NEGATIVE,
    KNOWN_POSITIVE,
    UNKNOWN,
    PermissionFact,
)

__all__ = (
    "instrument_fact",
    "investor_fact",
    "rate_fact",
    "request_filter",
    "trading_code_fact",
)

_CODE_KINDS = {"1": "speculation", "2": "arbitrage", "3": "hedge", "5": "market_maker"}


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    return str(value).strip()


def _flag(value: Any) -> Optional[int]:
    """Native bool/int flag as 1/0, ``None`` when unreadable."""
    if value in (1, "1", b"1", True):
        return 1
    if value in (0, "0", b"0", False):
        return 0
    return None


def _row_value(row: Any, name: str) -> Any:
    if isinstance(row, dict):
        return row.get(name)
    return getattr(row, name, None)


def request_filter(result: Any, name: str) -> Optional[str]:
    """Value of ``name`` read back from the populated native request, if any."""
    source = getattr(result, "query_source", None)
    if source is None:
        return None
    try:
        filters = dict(getattr(source, "request_filters", ()) or ())
    except (TypeError, ValueError):
        return None
    value = filters.get(name)
    return value if type(value) is str else None


def _unknown(name: str, reason: str, *, complete: bool) -> PermissionFact:
    return PermissionFact(name=name, status=UNKNOWN, reason=reason, query_complete=complete)


def investor_fact(result: Any) -> PermissionFact:
    name = "investor_active"
    if result is None:
        return _unknown(name, "query_incomplete", complete=False)
    rows = list(result.records)
    expected = request_filter(result, "InvestorID")
    if expected:
        rows = [row for row in rows if _text(_row_value(row, "InvestorID")) == expected]
    if not rows:
        return _unknown(name, "investor_row_absent", complete=True)
    if len(rows) > 1:
        return _unknown(name, "investor_rows_ambiguous", complete=True)
    active = _flag(_row_value(rows[0], "IsActive"))
    if active == 1:
        return PermissionFact(name, KNOWN_POSITIVE, "investor_active", True)
    if active == 0:
        return PermissionFact(name, KNOWN_NEGATIVE, "investor_inactive", True)
    return _unknown(name, "investor_state_unreadable", complete=True)


def instrument_fact(
    result: Any, symbol: str, given_venue: Optional[str]
) -> tuple[PermissionFact, Optional[str], Optional[str]]:
    """``(fact, venue, product_id)`` of ``symbol`` from a filtered instrument query.

    ``venue`` is the instrument's exchange when the venue reported exactly one;
    ``None`` otherwise.  A caller-given venue that differs from it leaves the
    fact unknown (the order would go to the caller's venue).
    """
    name = "instrument_trading"
    if result is None:
        return _unknown(name, "query_incomplete", complete=False), None, None
    rows = [row for row in result.records if _text(_row_value(row, "InstrumentID")) == symbol]
    if not rows:
        return _unknown(name, "instrument_not_found", complete=True), None, None
    venues = {_text(_row_value(row, "ExchangeID")) for row in rows}
    if len(rows) != 1 or len(venues) != 1 or "" in venues:
        return _unknown(name, "instrument_rows_ambiguous", complete=True), None, None
    row = rows[0]
    venue = venues.pop()
    product = _text(_row_value(row, "ProductID")) or None
    if given_venue is not None and given_venue != venue:
        return _unknown(name, "instrument_venue_mismatch", complete=True), venue, product
    trading = _flag(_row_value(row, "IsTrading"))
    phase = _text(_row_value(row, "InstLifePhase"))
    if trading == 0:
        return PermissionFact(name, KNOWN_NEGATIVE, "instrument_not_trading", True), venue, product
    if phase == "3":
        return PermissionFact(name, KNOWN_NEGATIVE, "instrument_expired", True), venue, product
    if phase == "0":
        return PermissionFact(name, KNOWN_NEGATIVE, "instrument_not_started", True), venue, product
    if trading == 1 and phase == "1":
        return PermissionFact(name, KNOWN_POSITIVE, "instrument_trading", True), venue, product
    if phase == "2":
        return _unknown(name, "instrument_paused", complete=True), venue, product
    return _unknown(name, "instrument_state_unreadable", complete=True), venue, product


def trading_code_fact(
    result: Any, venue: Optional[str], *, venue_query_complete: bool
) -> tuple[PermissionFact, tuple[str, ...], Optional[str]]:
    """``(fact, active kinds, blocker)`` of the trading codes on ``venue``.

    ``venue_query_complete`` tells whether an unknown venue is itself a
    complete fact (the instrument query completed) or the consequence of an
    incomplete instrument query.  ``blocker`` names request evidence for
    another exchange.
    """
    name = "trading_code"
    if venue is None:
        return _unknown(name, "venue_unknown", complete=venue_query_complete), (), None
    if result is None:
        return _unknown(name, "query_incomplete", complete=False), (), None
    requested = request_filter(result, "ExchangeID")
    if requested is not None and requested != venue:
        return (
            _unknown(name, "trading_code_filter_mismatch", complete=False),
            (),
            "trading_code_filter_mismatch",
        )
    unattributed = False
    unreadable = False
    venue_rows = []
    for row in result.records:
        row_venue = _text(_row_value(row, "ExchangeID"))
        if not row_venue:
            unattributed = True
        elif row_venue == venue:
            venue_rows.append(row)
    active = []
    for row in venue_rows:
        flag = _flag(_row_value(row, "IsActive"))
        if flag is None or not _text(_row_value(row, "ClientID")):
            unreadable = True
        elif flag == 1:
            active.append(row)
    kinds = tuple(
        sorted(
            {_CODE_KINDS.get(_text(_row_value(row, "ClientIDType")), "other") for row in active}
        )
    )
    if "speculation" in kinds:
        return PermissionFact(name, KNOWN_POSITIVE, "speculation_code_active", True), kinds, None
    if active:
        return _unknown(name, "speculation_code_not_found", complete=True), kinds, None
    if unattributed:
        return _unknown(name, "trading_code_rows_unattributed", complete=True), kinds, None
    if unreadable:
        return _unknown(name, "trading_code_state_unreadable", complete=True), kinds, None
    if venue_rows:
        return PermissionFact(name, KNOWN_NEGATIVE, "trading_code_inactive", True), kinds, None
    return PermissionFact(name, KNOWN_NEGATIVE, "trading_code_absent", True), kinds, None


def rate_fact(
    name: str,
    result: Any,
    symbol: str,
    venue: Optional[str],
    product: Optional[str],
) -> PermissionFact:
    """A commission or margin rate row that applies to ``symbol``.

    CTP may key rate rows by the instrument or by its product; a row of
    another venue does not apply.  A missing row is ``unknown``, never a
    denial (the broker may apply a default the query does not show).
    """
    if result is None:
        return _unknown(name, "query_incomplete", complete=False)
    keys = {symbol}
    if product:
        keys.add(product)
    for row in result.records:
        row_symbol = _text(_row_value(row, "InstrumentID"))
        row_venue = _text(_row_value(row, "ExchangeID"))
        if venue is not None and row_venue and row_venue != venue:
            continue
        if row_symbol in keys or (
            product is None and row_symbol.isalpha() and symbol.startswith(row_symbol)
        ):
            return PermissionFact(name, KNOWN_POSITIVE, "rate_row_present", True)
    return _unknown(name, "rate_row_absent", complete=True)
