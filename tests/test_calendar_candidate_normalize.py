"""Synthetic structural comparisons only; no venue, source or release proof."""

import hashlib
import json
from copy import deepcopy
from dataclasses import replace

import pytest

from bt_api_ctp.calendar_candidate_normalize import normalize_calendar_candidate
from bt_api_ctp.calendar_supplement import SupplementCandidate, parse_supplement


def content():
    first = {
        "venue": "SHFE",
        "kind": "future",
        "product": "rb",
        "contracts": ["rb2701", "rb2705"],
        "valid_from": "2026-01-01",
        "valid_to": "2026-01-31",
        "day_windows": [
            {
                "id": "morning",
                "phase": "continuous",
                "start": "09:00",
                "end": "10:00",
                "end_day_offset": 0,
            },
            {
                "id": "afternoon",
                "phase": "continuous",
                "start": "13:00",
                "end": "14:00",
                "end_day_offset": 0,
            },
        ],
        "night_windows": [
            {
                "id": "night_a",
                "phase": "continuous",
                "start": "21:00",
                "end": "22:00",
                "end_day_offset": 0,
            },
            {
                "id": "night_b",
                "phase": "continuous",
                "start": "23:00",
                "end": "01:00",
                "end_day_offset": 1,
            },
        ],
        "night_mappings": [
            {"trading_day": "2026-01-06", "start_date": "2026-01-05", "source_ids": ["b", "a"]},
            {"trading_day": "2026-01-07", "start_date": "2026-01-06", "source_ids": ["a", "b"]},
        ],
        "source_ids": ["b", "a"],
        "windows_complete": False,
        "exceptions_complete": False,
    }
    second = deepcopy(first)
    second.update(product="cu", contracts=["cu2701"])
    return {
        "sources": [
            {"id": "b", "description": "合成来源乙"},
            {"id": "a", "description": "合成来源甲"},
        ],
        "trading_days": [
            {"venue": "SHFE", "day": "2026-01-07", "status": "open", "source_ids": ["b", "a"]},
            {"venue": "SHFE", "day": "2026-01-06", "status": "open", "source_ids": ["a", "b"]},
        ],
        "trading_hours": [first, second],
        "exceptions": [
            {
                "venue": "SHFE",
                "kind": "future",
                "product": "rb",
                "contract": "rb2701",
                "trading_day": "2026-01-06",
                "action": "cancel_window",
                "window_id": "morning",
                "source_ids": ["b", "a"],
            },
            {
                "venue": "SHFE",
                "kind": "future",
                "product": "cu",
                "contract": None,
                "trading_day": "2026-01-07",
                "action": "close_day",
                "window_id": None,
                "source_ids": ["a", "b"],
            },
        ],
    }


def candidate(value):
    return parse_supplement(json.dumps(value, ensure_ascii=False).encode("utf-8"))


def reorder(value):
    for key in ("sources", "trading_days", "trading_hours", "exceptions"):
        value[key].reverse()
    for key in ("trading_days", "trading_hours", "exceptions"):
        for row in value[key]:
            row["source_ids"].reverse()
    for row in value["trading_hours"]:
        for key in ("contracts", "day_windows", "night_windows", "night_mappings"):
            row[key].reverse()
        for mapping in row["night_mappings"]:
            mapping["source_ids"].reverse()
    return value


def test_stable_sort_preserves_all_fields_and_does_not_mutate_input():
    original, permuted = candidate(content()), candidate(reorder(content()))
    before_original, before_permuted = deepcopy(original), deepcopy(permuted)
    first, second = normalize_calendar_candidate(original), normalize_calendar_candidate(permuted)
    assert first.status == second.status == "NORMALIZED"
    assert first.json_bytes == second.json_bytes and first.sha256 == second.sha256
    assert first.sha256 == hashlib.sha256(first.json_bytes).hexdigest()
    assert (first.verification, first.coverage, first.publication) == (
        "UNVERIFIED",
        "NOT_PROVEN",
        "NOT_AUTHORIZED",
    )
    assert (first.comparison, first.unresolved_nights) == (
        "FOUR_LAYER_STRUCTURE_ONLY",
        "NOT_INCLUDED",
    )
    assert original == before_original and permuted == before_permuted
    normalized = json.loads(first.json_bytes)
    assert candidate(normalized).verification == "UNVERIFIED"
    # Directly check the SDK store's exact canonical content digest input.
    from bt_api_py.trading_calendar_store import _content, _sha

    assert _content(normalized) == first.json_bytes
    assert _sha(_content(normalized)) == first.sha256


@pytest.mark.parametrize(
    "change",
    [
        lambda c: c["sources"][0].update(description="changed source"),
        lambda c: c["trading_days"][0].update(status="closed"),
        lambda c: c["trading_days"][0].update(source_ids=["b"]),
        lambda c: c["trading_hours"][0].update(valid_to="2026-02-01"),
        lambda c: c["trading_hours"][0].update(contracts=["rb2701", "rb2710"]),
        lambda c: c["trading_hours"][0]["day_windows"][0].update(start="09:01"),
        lambda c: c["trading_hours"][0]["night_mappings"][0].update(start_date="2026-01-04"),
        lambda c: c["exceptions"][0].update(window_id="afternoon"),
    ],
)
def test_substantive_change_changes_digest(change):
    original = content()
    baseline = normalize_calendar_candidate(candidate(original))
    updated = deepcopy(original)
    change(updated)
    result = normalize_calendar_candidate(candidate(updated))
    assert baseline.status == result.status == "NORMALIZED"
    assert baseline.sha256 != result.sha256


@pytest.mark.parametrize(
    "change",
    [
        lambda c: c["sources"].append(deepcopy(c["sources"][0])),
        lambda c: c["trading_days"].append(deepcopy(c["trading_days"][0])),
        lambda c: c["trading_days"][0]["source_ids"].append("a"),
        lambda c: c["trading_hours"][0]["contracts"].append("rb2701"),
        lambda c: c["trading_hours"][0]["night_mappings"].append(
            deepcopy(c["trading_hours"][0]["night_mappings"][0])
        ),
        lambda c: c["trading_hours"][0]["day_windows"].append(
            deepcopy(c["trading_hours"][0]["day_windows"][0])
        ),
        lambda c: c["trading_hours"].append(deepcopy(c["trading_hours"][0])),
        lambda c: c["exceptions"].append(deepcopy(c["exceptions"][0])),
        lambda c: c["trading_hours"][0]["day_windows"][1].update(start="09:30"),
        lambda c: c["trading_hours"][1].update(contracts=["rb2701"]),
        lambda c: c["trading_days"][0].update(extra="unknown"),
        lambda c: c.update(extra=[]),
    ],
)
def test_duplicate_conflicting_or_unsupported_input_never_returns_bytes(change):
    malformed = content()
    change(malformed)
    # Construct directly to exercise both schema rejection and extra ambiguity checks.
    result = normalize_calendar_candidate(SupplementCandidate(malformed, ()))
    assert (result.status, result.json_bytes, result.sha256) == ("INVALID_INPUT", None, None)


def test_wildcard_exception_overlap_cannot_be_sorted_away():
    value = content()
    duplicate = deepcopy(value["exceptions"][0])
    duplicate["contract"] = None
    value["exceptions"].append(duplicate)
    result = normalize_calendar_candidate(candidate(value))
    assert result.status == "INVALID_INPUT" and result.json_bytes is None


def test_false_verification_and_source_bookkeeping_rejected():
    value = candidate(content())
    assert (
        normalize_calendar_candidate(replace(value, verification="VERIFIED")).status
        == "INVALID_INPUT"
    )
    assert (
        normalize_calendar_candidate(replace(value, unused_source_ids=("fabricated",))).status
        == "INVALID_INPUT"
    )
    assert normalize_calendar_candidate(None).status == "INVALID_INPUT"


def test_total_nested_item_bound_rejects_large_forged_input():
    value = content()
    template = value["trading_hours"][0]
    value["trading_hours"] = []
    for index in range(101):
        row = deepcopy(template)
        row["product"] = f"p{index}"
        row["contracts"] = [f"c{index}_{number}" for number in range(200)]
        value["trading_hours"].append(row)
    result = normalize_calendar_candidate(SupplementCandidate(value, ()))
    assert (result.status, result.json_bytes, result.sha256) == ("INVALID_INPUT", None, None)


def test_source_digest_changes_are_preserved_not_classified_semantic_noop():
    value = content()
    first = normalize_calendar_candidate(candidate(value))
    value["sources"][0]["description"] += " page sha changed"
    second = normalize_calendar_candidate(candidate(value))
    assert first.sha256 != second.sha256
    assert (
        second.comparison == "FOUR_LAYER_STRUCTURE_ONLY"
        and second.unresolved_nights == "NOT_INCLUDED"
    )
