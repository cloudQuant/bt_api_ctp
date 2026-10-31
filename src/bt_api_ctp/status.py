"""Single owner for the CTP registered-sim receipt status and filter tables.

``fields.normalize_order_status`` stays the owner of CTP order-status *text*.
The set here is the distinct native-code terminal projection a durable
consumer compares against raw ``OrderStatus`` characters, so its membership is
deliberately not derived from that text map: ``2`` is excluded and ``a`` is
included. ``account_provider`` keeps its own separately scoped private
terminal-status constant for the read-only account lane; this module does not
change or replace it.

These facts classify observed provider records. They grant no admission, no
retry permission and no account authority.
"""

from __future__ import annotations

TERMINAL_ORDER_STATUSES = frozenset({"0", "4", "5", "a"})

COMMISSION_REQUEST_FILTER_FIELDS = {
    "instrument_id": "InstrumentID",
    "exchange_id": "ExchangeID",
    "InstrumentID": "InstrumentID",
    "ExchangeID": "ExchangeID",
    "BrokerID": "BrokerID",
    "InvestorID": "InvestorID",
}

__all__ = [
    "COMMISSION_REQUEST_FILTER_FIELDS",
    "TERMINAL_ORDER_STATUSES",
]
