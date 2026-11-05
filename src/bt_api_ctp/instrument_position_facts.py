"""Instrument-scoped position result (iteration 62, D62-11a).

Two position interfaces exist for a direct route:

* ``account_facts(positions=True)`` returns
  :class:`~bt_api_base.direct_write_protocol.DirectAccountFacts`: the
  unfiltered whole-account read.  Only that result may prove the whole account
  flat (startup reconciliation, account audit, discovery of unconfigured
  instruments).
* ``instrument_positions(symbol, exchange_id=None)`` returns
  :class:`InstrumentPositionFacts`: a native query filtered to one instrument.
  Its ``scope`` is always ``"instrument"`` and ``proves_account_flat`` is always
  ``False``; an empty complete result proves only that the venue reported no
  position for that instrument in the current session.  It is deliberately a
  different type without ``active_orders``/``balance`` sections so it cannot be
  passed where whole-account facts are consumed.

Rows of other instruments (or another venue) that a venue returns despite the
filter are dropped and reported (``filter_ignored``/``dropped_rows``); they are
never used.  The type is neutral (no CTP field names) and lives in the provider
package only because ``bt_api_base`` is not changed in this step; it can move to
the Base direct-write protocol additively.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from bt_api_base.direct_write_protocol import DirectPositionFact

__all__ = ("INSTRUMENT_POSITION_SCOPE", "InstrumentPositionFacts")

INSTRUMENT_POSITION_SCOPE = "instrument"


@dataclass(frozen=True)
class InstrumentPositionFacts:
    """Venue-reported positions of exactly one instrument (never the account).

    ``complete`` is True only when the filtered native query completed without
    error for the current session and every row of the requested instrument
    could be projected; ``positions`` is then the (possibly empty) tuple of
    that instrument's long/short/net positions.  ``positions`` is ``None`` when
    incomplete.  ``native_filter_verified`` is True when the query's request
    evidence shows the instrument filter was written to the native request,
    False when the evidence shows it was not, ``None`` when no evidence exists.
    """

    exchange_name: str
    symbol: str
    venue: Optional[str]
    trading_day: Optional[str]
    session_generation: Optional[int]
    observed_at_utc: str
    positions: Optional[tuple[DirectPositionFact, ...]]
    complete: bool
    blockers: tuple[str, ...] = ()
    filter_ignored: bool = False
    dropped_rows: int = 0
    dropped_instruments: tuple[str, ...] = ()
    native_filter_verified: Optional[bool] = None
    scope: str = INSTRUMENT_POSITION_SCOPE
    proves_account_flat: bool = False

    def __post_init__(self) -> None:
        if self.scope != INSTRUMENT_POSITION_SCOPE:
            raise ValueError("instrument position facts must have instrument scope")
        if self.proves_account_flat is not False:
            raise ValueError("instrument position facts never prove the account flat")
        if type(self.symbol) is not str or not self.symbol:
            raise ValueError("symbol is required")
        if type(self.complete) is not bool:
            raise ValueError("complete must be a bool")
        if self.complete and (self.positions is None or self.blockers):
            raise ValueError("complete facts need positions and no blockers")
        if not self.complete and self.positions is not None:
            raise ValueError("incomplete facts carry no positions")
        for fact in self.positions or ():
            if type(fact) is not DirectPositionFact or fact.symbol != self.symbol:
                raise ValueError("position outside the requested instrument")
            if self.venue is not None and fact.venue != self.venue:
                raise ValueError("position outside the requested venue")

    @property
    def instrument_flat(self) -> Optional[bool]:
        """True/False for the requested instrument only; ``None`` when unknown."""

        if not self.complete or self.positions is None:
            return None
        return not any(fact.total > 0 for fact in self.positions)
