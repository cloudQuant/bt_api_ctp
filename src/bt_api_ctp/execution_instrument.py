"""CTP native execution instrument spelling and normalization; no authorization."""

import re

_CTP_EXCHANGES = {"CFFEX", "CZCE", "DCE", "GFEX", "INE", "SHFE"}
_CTP_EXCHANGE_ALIASES = {"ZCE": "CZCE"}
_CTP_INSTRUMENT_RE = re.compile(r"^[A-Z]{1,3}[0-9]{3,4}$")
_CTP_BUNDLE_INSTRUMENT_RE = re.compile(r"^[A-Za-z][A-Za-z0-9]*(?:-[A-Za-z0-9]+)*$")


def _canonical_ctp_exchange(value):
    exchange = str(value or "").strip().upper()
    exchange = _CTP_EXCHANGE_ALIASES.get(exchange, exchange)
    return exchange if exchange in _CTP_EXCHANGES else ""


def canonical_ctp_instrument(value, exchange_id=None):
    """Return ``EXCHANGE.INSTRUMENT`` for one strictly scoped CTP contract.

    Public write requests may use either prefix/suffix notation or a bare
    instrument paired with ``exchange_id``.  The arm proof itself must already
    equal the returned canonical representation.  CZCE's four-digit alias is
    collapsed to the native three-digit year/month representation.
    """
    text = str(value or "").strip().upper()
    supplied_exchange = _canonical_ctp_exchange(exchange_id)
    if not text or (exchange_id not in (None, "") and not supplied_exchange):
        return ""
    parts = text.split(".")
    if len(parts) == 1:
        exchange = supplied_exchange
        instrument = parts[0]
    elif len(parts) == 2:
        first_exchange = _canonical_ctp_exchange(parts[0])
        last_exchange = _canonical_ctp_exchange(parts[1])
        if bool(first_exchange) == bool(last_exchange):
            return ""
        exchange = first_exchange or last_exchange
        instrument = parts[1] if first_exchange else parts[0]
        if supplied_exchange and supplied_exchange != exchange:
            return ""
    else:
        return ""
    if not exchange or not _CTP_INSTRUMENT_RE.fullmatch(instrument):
        return ""
    letters = instrument.rstrip("0123456789")
    digits = instrument[len(letters) :]
    if exchange == "CZCE" and len(digits) == 4:
        digits = digits[-3:]
    return f"{exchange}.{letters}{digits}"


def canonical_ctp_bundle_instrument(value, exchange_id=None):
    """Return one exact raw CTP instrument identity for a V2 bundle proof.

    Option ``InstrumentID`` values are exchange-native identifiers.  In
    particular, DCE option IDs contain hyphens and are case-sensitive at this
    authorization boundary (for example ``DCE.m2701-C-3400``).  This helper
    deliberately does not apply the legacy CZCE alias conversion or uppercase
    an instrument.  A bundle proof therefore authorizes exactly the raw IDs
    that will reach the native CTP submit/cancel fields.
    """
    text = str(value or "").strip()
    supplied_exchange = _canonical_ctp_exchange(exchange_id)
    if not text or (exchange_id not in (None, "") and not supplied_exchange):
        return ""
    parts = text.split(".")
    if len(parts) == 1:
        exchange = supplied_exchange
        instrument = parts[0]
    elif len(parts) == 2:
        first_exchange = _canonical_ctp_exchange(parts[0])
        last_exchange = _canonical_ctp_exchange(parts[1])
        if bool(first_exchange) == bool(last_exchange):
            return ""
        exchange = first_exchange or last_exchange
        instrument = parts[1] if first_exchange else parts[0]
        if supplied_exchange and supplied_exchange != exchange:
            return ""
    else:
        return ""
    if (
        not exchange
        or len(instrument) > 80
        or not _CTP_BUNDLE_INSTRUMENT_RE.fullmatch(instrument)
        or not any(character.isdigit() for character in instrument)
    ):
        return ""
    return f"{exchange}.{instrument}"


def canonical_ctp_bundle_wire_instrument(value, exchange_id=None):
    """Return a V2 identity only when native CTP fields need no rewriting.

    Bundle proof parsing intentionally accepts familiar fully-qualified forms
    while validating a signed proof or reading historical recovery rows.  A
    submit/cancel request is different: ``CtpRequestDataFuture`` forwards its
    ``symbol`` and ``exchange_id`` verbatim to ``InstrumentID`` and
    ``ExchangeID``.  V2 therefore accepts only the exact bare native ID and
    canonical exchange spelling that the proof authorizes.
    """
    if (
        not isinstance(value, str)
        or value != value.strip()
        or "." in value
        or not isinstance(exchange_id, str)
        or exchange_id != exchange_id.strip()
    ):
        return ""
    canonical = canonical_ctp_bundle_instrument(value, exchange_id)
    if not canonical:
        return ""
    exchange, instrument = canonical.split(".", 1)
    return canonical if instrument == value and exchange == exchange_id else ""


