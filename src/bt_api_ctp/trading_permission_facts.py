"""Instrument-level trading-permission facts (iteration 62, D62-25, N-1/2, 62.03).

A direct route reads, per requested instrument, the venue facts that bear on
whether the account can trade it:

* ``investor_active``   - the bound investor is active (account level);
* ``trading_code``      - an active trading code exists on the instrument's
  exchange (and whether one of them is a speculation code);
* ``instrument_trading`` - the instrument is listed, trading and in its
  started life phase;
* ``commission_rate`` / ``margin_rate`` - a rate row applies to the
  instrument (optional, informational).

Every fact is ``known_positive``, ``known_negative`` or ``unknown`` with a
finite reason code; a missing, failed, timed-out or stale query leaves it
``unknown``.  Only ``investor_active``, ``trading_code`` and
``instrument_trading`` can be ``known_negative``; a missing rate row is never
a denial.

The result never proves that the account may trade: ``proves_permission`` is
always ``False`` and the best outcome is ``no_negative_found``.  An active
investor or an active trading code is a single input, not a permission
(D62-25); broker-side risk rules and the venue remain authoritative.  Nothing
here relates to the BT ``net``/``dual_side`` projection: a venue's two-sided
position bookkeeping is neither an account authorization nor a reason to
refuse BT ``net`` orders.

The facts are bound to one session generation and trading day; a session
change while reading makes every fact ``unknown`` (``session_consistent`` is
False) and consumers must treat facts of another session or trading day as
unknown.  The types carry no account identifiers or native field names; they
live in the provider package only because ``bt_api_base`` is not changed in
this step and can move to the Base direct-write protocol additively.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

__all__ = (
    "DENIAL_FACTS",
    "FACT_NAMES",
    "KNOWN_NEGATIVE",
    "KNOWN_POSITIVE",
    "OUTCOME_KNOWN_NEGATIVE",
    "OUTCOME_NO_NEGATIVE_FOUND",
    "OUTCOME_UNKNOWN",
    "PERMISSION_SCOPE",
    "REQUIRED_FACTS",
    "TRADING_CODE_KINDS",
    "UNKNOWN",
    "InstrumentPermissionFacts",
    "PermissionFact",
    "TradingPermissionFacts",
)

PERMISSION_SCOPE = "instrument_permission"

KNOWN_POSITIVE = "known_positive"
KNOWN_NEGATIVE = "known_negative"
UNKNOWN = "unknown"
_STATUSES = frozenset((KNOWN_POSITIVE, KNOWN_NEGATIVE, UNKNOWN))

OUTCOME_KNOWN_NEGATIVE = "known_negative"
OUTCOME_NO_NEGATIVE_FOUND = "no_negative_found"
OUTCOME_UNKNOWN = "unknown"

FACT_NAMES = (
    "investor_active",
    "trading_code",
    "instrument_trading",
    "commission_rate",
    "margin_rate",
)
# Facts whose definitive negative means the venue cannot accept the order.
DENIAL_FACTS = frozenset(("investor_active", "trading_code", "instrument_trading"))
# Facts every instrument entry must carry (rates are optional).
REQUIRED_FACTS = ("investor_active", "trading_code", "instrument_trading")

TRADING_CODE_KINDS = frozenset(("speculation", "arbitrage", "hedge", "market_maker", "other"))
_MAX_REASON = 64


def _valid_reason(value: object) -> bool:
    return (
        type(value) is str
        and 0 < len(value) <= _MAX_REASON
        and value.isascii()
        and all(char.islower() or char.isdigit() or char == "_" for char in value)
    )


@dataclass(frozen=True)
class PermissionFact:
    """One permission input: status, finite reason, query completeness."""

    name: str
    status: str
    reason: str
    query_complete: bool

    def __post_init__(self) -> None:
        if self.name not in FACT_NAMES:
            raise ValueError("unknown permission fact")
        if self.status not in _STATUSES:
            raise ValueError("invalid permission fact status")
        if not _valid_reason(self.reason):
            raise ValueError("permission fact reason must be a finite code")
        if type(self.query_complete) is not bool:
            raise ValueError("query_complete must be a bool")
        if self.status != UNKNOWN and not self.query_complete:
            raise ValueError("a known fact needs a complete query")
        if self.status == KNOWN_NEGATIVE and self.name not in DENIAL_FACTS:
            raise ValueError("this fact can never be a definitive negative")


@dataclass(frozen=True)
class InstrumentPermissionFacts:
    """Permission inputs of exactly one instrument (never a permission proof)."""

    symbol: str
    venue: Optional[str]
    facts: tuple[PermissionFact, ...]
    trading_code_kinds: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if type(self.symbol) is not str or not self.symbol:
            raise ValueError("symbol is required")
        if self.venue is not None and (type(self.venue) is not str or not self.venue):
            raise ValueError("venue must be a non-empty string or None")
        if type(self.facts) is not tuple or any(
            type(fact) is not PermissionFact for fact in self.facts
        ):
            raise ValueError("facts must be a tuple of PermissionFact")
        names = [fact.name for fact in self.facts]
        if len(set(names)) != len(names):
            raise ValueError("duplicate permission fact")
        if any(name not in names for name in REQUIRED_FACTS):
            raise ValueError("required permission fact missing")
        if type(self.trading_code_kinds) is not tuple or any(
            kind not in TRADING_CODE_KINDS for kind in self.trading_code_kinds
        ):
            raise ValueError("invalid trading code kind")

    def fact(self, name: str) -> Optional[PermissionFact]:
        return next((fact for fact in self.facts if fact.name == name), None)

    @property
    def denial_reasons(self) -> tuple[str, ...]:
        return tuple(fact.reason for fact in self.facts if fact.status == KNOWN_NEGATIVE)

    @property
    def outcome(self) -> str:
        if self.denial_reasons:
            return OUTCOME_KNOWN_NEGATIVE
        if all(fact.status == KNOWN_POSITIVE for fact in self.facts):
            return OUTCOME_NO_NEGATIVE_FOUND
        return OUTCOME_UNKNOWN

    @property
    def complete(self) -> bool:
        return all(fact.query_complete for fact in self.facts)

    @property
    def proves_permission(self) -> bool:
        return False


@dataclass(frozen=True)
class TradingPermissionFacts:
    """Permission inputs of the requested instruments in one session."""

    exchange_name: str
    trading_day: Optional[str]
    session_generation: Optional[int]
    observed_at_utc: str
    instruments: tuple[InstrumentPermissionFacts, ...]
    session_consistent: bool
    blockers: tuple[str, ...] = ()
    native_queries: int = 0
    scope: str = PERMISSION_SCOPE
    proves_permission: bool = False

    def __post_init__(self) -> None:
        if self.scope != PERMISSION_SCOPE:
            raise ValueError("trading permission facts must have instrument_permission scope")
        if self.proves_permission is not False:
            raise ValueError("trading permission facts never prove a permission")
        if type(self.session_consistent) is not bool:
            raise ValueError("session_consistent must be a bool")
        if type(self.instruments) is not tuple or not self.instruments or any(
            type(entry) is not InstrumentPermissionFacts for entry in self.instruments
        ):
            raise ValueError("instruments must be a non-empty tuple of InstrumentPermissionFacts")
        symbols = [entry.symbol for entry in self.instruments]
        if len(set(symbols)) != len(symbols):
            raise ValueError("duplicate instrument")
        if type(self.blockers) is not tuple or any(type(item) is not str for item in self.blockers):
            raise ValueError("blockers must be a tuple of strings")
        if type(self.native_queries) is not int or self.native_queries < 0:
            raise ValueError("native_queries must be a non-negative int")
        if not self.session_consistent:
            if not self.blockers:
                raise ValueError("an inconsistent session needs a blocker")
            if any(fact.status != UNKNOWN for entry in self.instruments for fact in entry.facts):
                raise ValueError("facts of a changed session must all be unknown")

    @property
    def symbols(self) -> tuple[str, ...]:
        return tuple(entry.symbol for entry in self.instruments)

    @property
    def complete(self) -> bool:
        """Every requested query completed in one unchanged session."""
        return (
            self.session_consistent
            and not self.blockers
            and all(entry.complete for entry in self.instruments)
        )

    def for_symbol(self, symbol: str) -> Optional[InstrumentPermissionFacts]:
        return next((entry for entry in self.instruments if entry.symbol == symbol), None)
