# Keep annotations resolvable by typing.get_type_hints on Python 3.9.
# ruff: noqa: UP045
"""Unverified single-contract AKShare detail candidates, with no network client.

The caller supplies a fetch port that behaves like
``futures_contract_detail(symbol=...)``. This module does not import AKShare,
make network calls, prove TLS or enforce a timeout. The port may block forever;
the future updater must supply independently bounded and verified transport.
The port must normalize its timeout exceptions to the built-in ``TimeoutError``
for ``FETCH_TIMEOUT`` classification; other exceptions, including unnormalized
third-party timeout exceptions, are classified as ``FETCH_ERROR``.
The request date is metadata only: this interface does not send a date and its
response does not establish an effective or trading date. No sessions are parsed.
``collected_at`` is the local time immediately after the fetch attempt ends,
not the source's update time.
"""

from __future__ import annotations

import math
import re
import unicodedata
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Callable, Optional

_VENUES = frozenset({"SHFE", "INE", "CZCE", "DCE", "CFFEX", "GFEX"})
_CONTRACT = re.compile(r"[A-Za-z]{1,8}[0-9]{3,4}\Z", re.ASCII)


@dataclass(frozen=True)
class CandidateField:
    """One source row, retaining its original text and normalized text."""

    raw_name: Optional[str]
    raw_value: Optional[str]
    name: Optional[str]
    value: Optional[str]


@dataclass(frozen=True)
class CandidateResult:
    status: str
    venue: str
    contract: str
    requested_date: date
    collected_at: datetime
    source: str
    source_version: Optional[str]
    fields: tuple[CandidateField, ...] = ()
    verification: str = "UNVERIFIED"
    timeout_enforced: bool = False


def _text(value: object) -> Optional[str]:
    # Pandas can represent absent cells as None, float NaN, pd.NA or NaT.
    if value is None:
        return None
    if isinstance(value, float) and math.isnan(value):
        return None
    if type(value).__name__ in {"NAType", "NaTType"}:
        return None
    if isinstance(value, str):
        return value
    # Detail fields are text, but numeric table cells are common. Avoid
    # stringifying containers or arbitrary objects into misleading evidence.
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    raise ValueError("unsupported cell")


def _normalize(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    normalized = " ".join(unicodedata.normalize("NFKC", value).split())
    return normalized or None


def collect_contract_candidate(
    *,
    venue: str,
    contract: str,
    requested_date: date,
    source_version: Optional[str],
    fetch: Callable[..., object],
) -> CandidateResult:
    """Collect a single future's raw detail; results are never verified facts.

    The supplied version is an unverified caller declaration, not an attestation.
    The port receives only ``symbol``. A response cannot prove it corresponds to
    the requested venue, contract, requested date or an official exchange source.
    Exceptions are classified without returning potentially sensitive messages.
    """
    if type(venue) is not str or venue not in _VENUES:
        raise ValueError("unsupported venue")
    if type(contract) is not str or _CONTRACT.fullmatch(contract) is None:
        raise ValueError("invalid futures contract")
    if type(requested_date) is not date:
        raise ValueError("requested_date must be a date")
    if source_version is not None and (
        type(source_version) is not str or not source_version.strip()
    ):
        raise ValueError("source_version must be a nonempty string or None")
    if not callable(fetch):
        raise ValueError("fetch port must be callable")

    def result(status: str, fields: tuple[CandidateField, ...] = ()) -> CandidateResult:
        return CandidateResult(
            status=status,
            venue=venue,
            contract=contract,
            requested_date=requested_date,
            collected_at=collected_at,
            source="akshare.futures_contract_detail",
            source_version=source_version,
            fields=fields,
        )

    try:
        frame = fetch(symbol=contract)
    except ImportError:
        fetch_status = "DEPENDENCY_UNAVAILABLE"
    except TimeoutError:
        fetch_status = "FETCH_TIMEOUT"
    except Exception:
        fetch_status = "FETCH_ERROR"
    else:
        fetch_status = None
    finally:
        collected_at = datetime.now(timezone.utc)

    if fetch_status is not None:
        return result(fetch_status)

    try:
        columns = list(frame.columns)
        if len(columns) != 2 or set(columns) != {"item", "value"}:
            return result("STRUCTURE_CHANGED")
        item_index, value_index = columns.index("item"), columns.index("value")
        rows = []
        original_value_is_text = []
        for row in frame.itertuples(index=False, name=None):
            if len(row) != 2:
                return result("STRUCTURE_CHANGED", tuple(rows))
            raw_name, raw_value = _text(row[item_index]), _text(row[value_index])
            rows.append(
                CandidateField(raw_name, raw_value, _normalize(raw_name), _normalize(raw_value))
            )
            original_value_is_text.append(isinstance(row[value_index], str))
    except Exception:
        return result("STRUCTURE_CHANGED")

    fields = tuple(rows)
    if not fields:
        return result("EMPTY", fields)
    names = [field.name for field in fields if field.name is not None]
    if len(names) != len(set(names)) or len(names) != len(fields):
        return result("STRUCTURE_CHANGED", fields)
    trading_time = next(
        (
            (field.value, original_value_is_text[index])
            for index, field in enumerate(fields)
            if field.name == "交易时间"
        ),
        (None, False),
    )
    if trading_time[0] is None or not trading_time[1]:
        return result("MISSING_TRADING_TIME", fields)
    return result("SUCCESS", fields)
