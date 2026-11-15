"""Local synthetic data only; no external venue or trading assertions."""

import json
from copy import deepcopy
from datetime import date, datetime

import pytest
import yaml

from bt_api_ctp.calendar_auto_candidate import MAX_JSON_BYTES, build_auto_candidate
from bt_api_ctp.calendar_candidate_coverage import CoverageRequirement, check_candidate_coverage
from bt_api_ctp.calendar_supplement import (
    MAX_INPUT_BYTES,
    MAX_YAML_EVENTS,
    SupplementError,
    parse_supplement,
)

START, END = date(2026, 1, 1), date(2027, 12, 31)
SOURCE = "shfe.holiday.2026." + "a" * 64


def layers(source=SOURCE):
    days = []
    mappings = []
    for offset in range(730):
        day = date.fromordinal(START.toordinal() + offset).isoformat()
        opened = offset % 7 < 5
        days.append(
            {
                "venue": "SHFE",
                "day": day,
                "status": "open" if opened else "closed",
                "source_ids": [source],
            }
        )
        if opened:
            mappings.append({"trading_day": day, "start_date": None, "source_ids": [source]})
    templates = []
    for kind, contract in (
        ("future", "cu_future"),
        ("option", "cu_option1"),
        ("option", "cu_option2"),
    ):
        templates.append(
            {
                "venue": "SHFE",
                "kind": kind,
                "product": "cu",
                "contracts": [contract],
                "valid_from": START.isoformat(),
                "valid_to": END.isoformat(),
                "day_windows": [
                    {
                        "id": "day",
                        "phase": "continuous",
                        "start": "09:00",
                        "end": "10:00",
                        "end_day_offset": 0,
                    }
                ],
                "night_windows": [],
                "night_mappings": deepcopy(mappings),
                "source_ids": [source],
                "windows_complete": True,
                "exceptions_complete": True,
            }
        )
    return {
        "sources": [{"id": source, "description": "synthetic only"}],
        "trading_days": days,
        "trading_hours": templates,
        "exceptions": [],
    }


def build(value, **kwargs):
    args = {"venue": "SHFE", "coverage_start": START, "coverage_end": END, "layers": value}
    args.update(kwargs)
    return build_auto_candidate(**args)


def test_large_three_scope_candidate_exceeds_yaml_budgets_but_passes_structure_check():
    value = layers()
    original = deepcopy(value)
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    events = sum(1 for _ in yaml.parse(encoded.decode(), Loader=yaml.SafeLoader))
    assert len(SOURCE) == 82 and len(value["trading_days"]) == 730
    assert len(value["trading_hours"][0]["night_mappings"]) == 522
    assert len(encoded) > MAX_INPUT_BYTES and events > MAX_YAML_EVENTS
    with pytest.raises(SupplementError):
        parse_supplement(encoded)
    result = build(value)
    assert result.status == "CANDIDATE" and result.candidate.verification == "UNVERIFIED"
    assert (result.verification, result.coverage, result.publication) == (
        "UNVERIFIED",
        "NOT_PROVEN",
        "NOT_AUTHORIZED",
    )
    assert (result.venue, result.coverage_start, result.coverage_end) == ("SHFE", START, END)
    assert result.candidate.content == original and value == original
    requests = tuple(
        CoverageRequirement("SHFE", kind, "cu", contract, START, END)
        for kind, contract in (
            ("future", "cu_future"),
            ("option", "cu_option1"),
            ("option", "cu_option2"),
        )
    )
    checked = check_candidate_coverage(result.candidate, requirements=requests)
    assert (checked.status, checked.requested_days, checked.checked_days, checked.gaps) == (
        "STRUCTURAL_COMPLETE_UNVERIFIED",
        2190,
        2190,
        (),
    )
    assert checked.publication == "NOT_AUTHORIZED"
    value["sources"][0]["description"] = "mutated"
    value["trading_hours"][0]["night_mappings"][0]["start_date"] = "1990-01-01"
    assert result.candidate.content == original
    result.candidate.content["trading_days"][0]["status"] = "closed"
    assert value["trading_days"][0]["status"] == "open"


@pytest.mark.parametrize(
    "kwargs",
    [
        {"venue": "DCE"},
        {"venue": "UNKNOWN"},
        {"venue": 1},
        {"coverage_start": datetime(2026, 1, 1)},
        {"coverage_end": date(2025, 1, 1)},
        {"coverage_end": date(2028, 1, 2)},
    ],
)
def test_invalid_scope_or_declaration_rejects_without_candidate(kwargs):
    result = build(layers(), **kwargs)
    assert (result.status, result.candidate, result.venue) == ("INVALID_INPUT", None, None)
    assert result.coverage == "NOT_PROVEN" and result.publication == "NOT_AUTHORIZED"


@pytest.mark.parametrize(
    "edit",
    [
        lambda value: value["trading_days"][0].update(venue="DCE"),
        lambda value: value["trading_hours"][0].update(venue="DCE"),
        lambda value: value["trading_days"][0].update(source_ids=["missing"]),
        lambda value: value["trading_days"][0].update(unknown="secret"),
        lambda value: value["trading_days"].append(deepcopy(value["trading_days"][0])),
        lambda value: value["trading_hours"].append(deepcopy(value["trading_hours"][0])),
        lambda value: value["trading_hours"][1].update(contracts=["cu_future"]),
        lambda value: value["trading_hours"][0]["day_windows"][0].update(end_day_offset=True),
        lambda value: value.update(extra=[]),
    ],
)
def test_cross_scope_missing_sources_duplicate_and_unknown_fields_rejected(edit):
    value = layers()
    edit(value)
    before = deepcopy(value)
    result = build(value)
    assert (result.status, result.candidate, result.reason) == (
        "INVALID_INPUT",
        None,
        "INVALID_OR_BOUNDED_INPUT",
    )
    assert value == before and "secret" not in repr(result)


def test_exception_venue_and_overlapping_exception_scope_rejected():
    value = layers()
    item = {
        "venue": "DCE",
        "kind": "future",
        "product": "cu",
        "contract": "cu_future",
        "trading_day": "2026-01-01",
        "action": "close_day",
        "window_id": None,
        "source_ids": [SOURCE],
    }
    value["exceptions"].append(item)
    assert build(value).status == "INVALID_INPUT"
    item["venue"] = "SHFE"
    value["exceptions"].append({**item, "contract": None})
    assert build(value).status == "INVALID_INPUT"


def test_list_nodes_bytes_depth_and_cycles_are_bounded():
    value = layers()
    value["trading_days"] *= 2
    assert build(value).status == "INVALID_INPUT"
    # All strings fit the ordinary per-string bound; their aggregate exceeds it.
    value = layers()
    value["sources"] = [
        {"id": f"source{i}", "description": "x" * 4096} for i in range(MAX_JSON_BYTES // 4096 + 1)
    ]
    assert build(value).status == "INVALID_INPUT"
    value = layers()
    value["extra"] = [[0] * 600 for _ in range(200)]
    assert build(value).status == "INVALID_INPUT"
    value = layers()
    nested = {}
    value["extra"] = nested
    for _ in range(30):
        nested["a"] = {}
        nested = nested["a"]
    assert build(value).status == "INVALID_INPUT"
    value = layers()
    cycle = []
    cycle.append(cycle)
    value["extra"] = cycle
    assert build(value).status == "INVALID_INPUT"


@pytest.mark.parametrize(
    "bad", [float("nan"), float("inf"), b"binary", {1: "bad"}, (1, 2), object()]
)
def test_non_json_or_unsupported_values_are_rejected(bad):
    value = layers()
    value["extra"] = bad
    assert build(value).status == "INVALID_INPUT"


def test_exact_two_year_leap_bound_does_not_assert_coverage():
    result = build(layers(), coverage_start=date(2027, 1, 1), coverage_end=date(2028, 12, 31))
    assert result.status == "CANDIDATE" and result.coverage == "NOT_PROVEN"
