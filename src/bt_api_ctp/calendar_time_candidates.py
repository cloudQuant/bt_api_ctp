# Keep annotations resolvable by typing.get_type_hints on Python 3.9.
# ruff: noqa: UP045
"""Conservative third-party session-text syntax candidates, never calendar facts.

Input is a caller-provided source identity plus item/value rows; the source ID
and row index locate the original field, but do not attest its provenance.
Only explicitly labelled 日盘/夜盘/上午/下午 ranges are extracted. NFKC offsets
refer to ``normalized_text``; ``raw_text`` is preserved separately. Unsupported
text, unlabelled ranges, overlap, and ambiguous clock rollovers require review.
Clock-only rollover is marked inferred, never a venue fact. No trading date,
expiry, source validity, holiday, option inheritance, break or UTC time is
inferred; in particular a continuous range is never split into an assumed break.
This experimental result is always UNVERIFIED and must not be published directly.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Optional

MAX_FIELDS = 512
MAX_TEXT = 4096
MAX_SOURCE_ID = 512
_LABEL = re.compile(r"(日盘|夜盘|上午|下午)\s*:?")
_RANGE = re.compile(
    r"(?<![0-9])(?P<start>[0-9]{1,2}:[0-9]{2})(?![0-9])\s*(?:——|—|–|-|~|至)\s*"
    r"(?P<next>次日)?\s*(?P<end>[0-9]{1,2}:[0-9]{2})(?![0-9])"
)
_SEPARATORS = frozenset(" \t\r\n,;、。")
_SAFE_COMPAT = frozenset("０１２３４５６７８９：－～")
_SENSITIVE_SYNTAX = frozenset("0123456789:-~")


@dataclass(frozen=True)
class TimeRangeCandidate:
    label: str
    scope: str
    start: str
    end: str
    day_transition: str
    normalized_span: tuple[int, int]


@dataclass(frozen=True)
class TimeTextCandidate:
    status: str
    source_id: str
    field_index: Optional[int] = None
    raw_text: Optional[str] = None
    normalized_text: Optional[str] = None
    duplicate_fields: tuple[tuple[int, str], ...] = ()
    duplicate_indexes: tuple[int, ...] = ()
    ranges: tuple[TimeRangeCandidate, ...] = ()
    # Spans are offsets into normalized_text and include all unrecognized text.
    uncertain_spans: tuple[tuple[int, int], ...] = ()
    review_reasons: tuple[str, ...] = ()
    verification: str = "UNVERIFIED"


def _minutes(clock: str) -> Optional[int]:
    hour_text, minute_text = clock.split(":")
    hour, minute = int(hour_text), int(minute_text)
    if hour > 23 or minute > 59:
        return None
    return hour * 60 + minute


def _unsafe_compatibility(text: str) -> bool:
    # NFKC is useful for full-width digits and punctuation, but accepting e.g.
    # superscripts/circled digits as ordinary clocks would create new syntax.
    for char in text:
        normalized = unicodedata.normalize("NFKC", char)
        if (
            char not in _SAFE_COMPAT
            and normalized != char
            and any(part in _SENSITIVE_SYNTAX for part in normalized)
        ):
            return True
    return False


def parse_time_candidates(*, source_id: str, fields: object) -> TimeTextCandidate:
    """Inspect synthetic/third-party item/value rows without executing IO.

    Field rows must be an explicit list/tuple of two-element list/tuple rows.
    This does not adapt or enable any existing fetcher; callers must keep the
    source ID bound to the exact original capture. Failures never echo input.
    """
    if type(source_id) is not str or not source_id.strip() or len(source_id) > MAX_SOURCE_ID:
        raise ValueError("invalid source identity")
    if type(fields) not in (list, tuple) or len(fields) > MAX_FIELDS:
        return TimeTextCandidate("INVALID_FIELDS", source_id)
    matches = []
    for index, row in enumerate(fields):
        if (
            type(row) not in (tuple, list)
            or len(row) != 2
            or type(row[0]) is not str
            or len(row[0]) > MAX_TEXT
        ):
            return TimeTextCandidate("INVALID_FIELDS", source_id)
        if unicodedata.normalize("NFKC", row[0]).strip() == "交易时间":
            matches.append((index, row[1]))
    if not matches:
        return TimeTextCandidate("MISSING_FIELD", source_id)
    if len(matches) != 1:
        # Preserve all bounded string fields for diagnosis; never choose a winner.
        duplicates = tuple(
            (index, value)
            for index, value in matches
            if type(value) is str and len(value) <= MAX_TEXT
        )
        omitted = ("DUPLICATE_VALUES_OMITTED",) if len(duplicates) != len(matches) else ()
        return TimeTextCandidate(
            "DUPLICATE_FIELD",
            source_id,
            duplicate_fields=duplicates,
            duplicate_indexes=tuple(index for index, _ in matches),
            review_reasons=omitted,
        )
    field_index, text = matches[0]
    if type(text) is not str:
        return TimeTextCandidate("INVALID_FIELD", source_id, field_index)
    if len(text) > MAX_TEXT:
        return TimeTextCandidate("INPUT_TOO_LONG", source_id, field_index)
    normalized = unicodedata.normalize("NFKC", text)
    if _unsafe_compatibility(text):
        return TimeTextCandidate(
            "NEEDS_REVIEW",
            source_id,
            field_index,
            text,
            normalized,
            uncertain_spans=((0, len(normalized)),),
            review_reasons=("UNSAFE_NORMALIZATION",),
        )

    candidates = []
    uncertain = []
    reasons = set()
    occupied = []
    label = None
    label_has_range = False
    offset = 0
    while offset < len(normalized):
        if normalized[offset] in _SEPARATORS:
            offset += 1
            continue
        label_match = _LABEL.match(normalized, offset)
        if label_match:
            if label is not None and not label_has_range:
                reasons.add("LABEL_WITHOUT_RANGE")
            label = label_match.group(1)
            label_has_range = False
            offset = label_match.end()
            continue
        range_match = _RANGE.match(normalized, offset)
        if range_match:
            start, end = range_match.group("start"), range_match.group("end")
            start_minutes, end_minutes = _minutes(start), _minutes(end)
            span = (offset, range_match.end())
            if label is None:
                uncertain.append(span)
                reasons.add("UNLABELED_RANGE")
            elif start_minutes is None or end_minutes is None or start_minutes == end_minutes:
                uncertain.append(span)
                reasons.add("INVALID_TIME_RANGE")
                label_has_range = True
            else:
                label_has_range = True
                start = f"{start_minutes // 60:02d}:{start_minutes % 60:02d}"
                end = f"{end_minutes // 60:02d}:{end_minutes % 60:02d}"
                scope = "NIGHT" if label == "夜盘" else "DAY"
                explicit_next = range_match.group("next") is not None
                if explicit_next:
                    transition = "EXPLICIT_NEXT_DAY"
                    if scope != "NIGHT" or end_minutes >= start_minutes:
                        reasons.add("AMBIGUOUS_DAY_TRANSITION")
                elif end_minutes < start_minutes:
                    transition = "INFERRED_NEXT_DAY"
                    reasons.add("INFERRED_DAY_TRANSITION")
                else:
                    transition = "SAME_CLOCK_DAY"
                if (label == "上午" and (start_minutes >= 720 or end_minutes > 720)) or (
                    label == "下午" and (start_minutes < 720 or end_minutes < 720)
                ):
                    reasons.add("AMBIGUOUS_DAY_LABEL")
                candidates.append(TimeRangeCandidate(label, scope, start, end, transition, span))
                if end_minutes < start_minutes:
                    occupied.extend(((start_minutes, 1440), (0, end_minutes)))
                else:
                    occupied.append((start_minutes, end_minutes))
            offset = range_match.end()
            continue
        # Preserve every unrecognized run for review, including punctuation and
        # ambiguities near otherwise recognized ranges. A prior label cannot
        # extend across unknown wording to an otherwise unlabeled range.
        if label is not None and not label_has_range:
            reasons.add("LABEL_WITHOUT_RANGE")
        label = None
        label_has_range = False
        start = offset
        offset += 1
        while (
            offset < len(normalized)
            and normalized[offset] not in _SEPARATORS
            and not _LABEL.match(normalized, offset)
            and not _RANGE.match(normalized, offset)
        ):
            offset += 1
        uncertain.append((start, offset))
        reasons.add("UNRECOGNIZED_TEXT")
    if label is not None and not label_has_range:
        reasons.add("LABEL_WITHOUT_RANGE")
    if not candidates:
        reasons.add("NO_LABELED_RANGES")
    occupied.sort()
    if any(left[1] > right[0] for left, right in zip(occupied, occupied[1:])):
        reasons.add("OVERLAPPING_RANGES")
    return TimeTextCandidate(
        status="NEEDS_REVIEW" if reasons else "SYNTAX_CANDIDATE",
        source_id=source_id,
        field_index=field_index,
        raw_text=text,
        normalized_text=normalized,
        ranges=tuple(candidates),
        uncertain_spans=tuple(uncertain),
        review_reasons=tuple(sorted(reasons)),
    )
