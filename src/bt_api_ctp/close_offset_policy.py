"""CTP venue rule: which exchanges need an explicit today/yesterday close.

SHFE and INE reject an ordinary ``close`` for today's position: the order must
say ``close_today`` or ``close_yesterday``. On CZCE, DCE, CFFEX and GFEX an
ordinary ``close`` is accepted and the venue chooses the bucket. CFFEX and GFEX
were added by decision D63-51 after the SimNow counter accepted an ordinary
close of a position opened the same day on each of them (2026-11-11). The split
set equals the one of ``ctp_close_plan``; that planner serves the direct path
and keeps its own, narrower generic set. Any other exchange is refused instead
of guessed. This is a provider fact shared with the Gateway strategy ledger
binding; it grants no authority and proves no account state.
"""

from __future__ import annotations

SPLIT_CLOSE_EXCHANGES = frozenset(("SHFE", "INE"))
GENERIC_CLOSE_EXCHANGES = frozenset(("CZCE", "DCE", "CFFEX", "GFEX"))
KNOWN_EXCHANGES = SPLIT_CLOSE_EXCHANGES | GENERIC_CLOSE_EXCHANGES


def split_close_required(exchange_id: object) -> bool:
    """Whether a close on this exchange must name today or yesterday.

    An exchange without an established rule raises ``ValueError``.
    """
    if type(exchange_id) is not str or exchange_id not in KNOWN_EXCHANGES:
        raise ValueError("CTP_CLOSE_RULE_UNKNOWN")
    return exchange_id in SPLIT_CLOSE_EXCHANGES


__all__ = ("GENERIC_CLOSE_EXCHANGES", "KNOWN_EXCHANGES", "SPLIT_CLOSE_EXCHANGES", "split_close_required")
