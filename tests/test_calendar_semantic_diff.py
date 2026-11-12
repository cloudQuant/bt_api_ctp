"""Synthetic declarations, not source or venue evidence."""

import builtins
from copy import deepcopy
from datetime import date, timedelta

import pytest

import bt_api_ctp.calendar_semantic_diff as semantic_diff
from bt_api_ctp.calendar_semantic_diff import CalendarScope, compare_calendar_semantics

START = date(2026, 1, 2)
END = date(2026, 1, 3)
SCOPE = (CalendarScope("X", "future", "cu", "cu1"),)


def sample():
    return {
        "sources": [
            {"id": "a", "description": "synthetic"},
            {"id": "b", "description": "second synthetic"},
        ],
        "trading_days": [
            {"venue": "X", "day": "2026-01-02", "status": "open", "source_ids": ["a"]},
            {"venue": "X", "day": "2026-01-03", "status": "closed", "source_ids": ["a"]},
        ],
        "trading_hours": [
            {
                "venue": "X",
                "kind": "future",
                "product": "cu",
                "contracts": ["cu1", "cu2"],
                "valid_from": "2026-01-02",
                "valid_to": "2026-01-03",
                "day_windows": [
                    {
                        "id": "day",
                        "phase": "continuous",
                        "start": "09:00",
                        "end": "11:00",
                        "end_day_offset": 0,
                    }
                ],
                "night_windows": [
                    {
                        "id": "night",
                        "phase": "continuous",
                        "start": "21:00",
                        "end": "01:00",
                        "end_day_offset": 1,
                    }
                ],
                "night_mappings": [
                    {"trading_day": "2026-01-02", "start_date": "2026-01-01", "source_ids": ["a"]}
                ],
                "source_ids": ["a"],
                "windows_complete": True,
                "exceptions_complete": True,
            }
        ],
        "exceptions": [],
    }


def compare(a, b, **kwargs):
    return compare_calendar_semantics(
        a,
        b,
        scopes=kwargs.get("scopes", SCOPE),
        start=kwargs.get("start", START),
        end=kwargs.get("end", END),
        source_text_before=kwargs.get("source_text_before", {"a": "synthetic source text"}),
        source_text_after=kwargs.get("source_text_after", {"a": "synthetic source text"}),
    )


def test_equal_and_order_independent_and_detached():
    a = sample()
    b = deepcopy(a)
    b["sources"].reverse()
    b["trading_days"].reverse()
    b["trading_hours"][0]["contracts"].reverse()
    originals = deepcopy((a, b))
    result = compare(a, b)
    assert (result.status, result.reason, result.changes) == ("SAME", "DECLARATIONS_EQUAL", ())
    assert result.range_basis == "OWNER_TRADING_DAY"
    assert result.source_authentication == "NOT_PROVEN"
    assert result.publication == "NOT_AUTHORIZED"
    assert result.digest == compare(b, a).digest
    assert (a, b) == originals


def test_sessions_and_exception_added_removed_both_directions():
    a, b = sample(), sample()
    b["trading_hours"][0]["day_windows"].append(
        {"id": "extra", "phase": "auction", "start": "13:00", "end": "14:00", "end_day_offset": 0}
    )
    b["exceptions"].append(
        {
            "venue": "X",
            "kind": "future",
            "product": "cu",
            "contract": "cu1",
            "trading_day": "2026-01-02",
            "action": "cancel_window",
            "window_id": "extra",
            "source_ids": ["a"],
        }
    )
    fwd, rev = compare(a, b), compare(b, a)
    assert fwd.status == rev.status == "CHANGED"
    assert {(c.category, c.operation) for c in fwd.changes} == {
        ("trading_session", "added"),
        ("exception", "added"),
    }
    assert {(c.category, c.operation) for c in rev.changes} == {
        ("trading_session", "removed"),
        ("exception", "removed"),
    }
    assert fwd.digest == compare(a, b).digest
    assert fwd.digest != rev.digest


def test_night_mapping_day_status_and_scope_changes():
    a, b = sample(), sample()
    b["trading_hours"][0]["night_mappings"][0]["start_date"] = None
    b["trading_hours"][0]["valid_to"] = "2026-01-04"
    result = compare(a, b)
    assert result.status == "CHANGED"
    assert {c.category for c in result.changes} >= {"night_mapping", "applicability"}
    b = sample()
    b["trading_days"][1]["status"] = "open"
    b["trading_hours"][0]["night_mappings"].append(
        {"trading_day": "2026-01-03", "start_date": None, "source_ids": ["a"]}
    )
    assert {c.category for c in compare(a, b).changes} == {"trading_day", "night_mapping"}


def test_missing_day_or_contract_is_unknown_even_when_deletion_observable():
    a, b = sample(), sample()
    b["trading_days"].pop()
    forward, reverse = compare(a, b), compare(b, a)
    assert forward.status == reverse.status == "UNKNOWN"
    assert any(c.category == "trading_day" and c.operation == "removed" for c in forward.changes)
    assert any(c.category == "trading_day" and c.operation == "added" for c in reverse.changes)
    b = sample()
    b["trading_hours"][0]["contracts"].remove("cu1")
    assert compare(a, b).reason == "SCOPE_INCOMPLETE"


def test_source_change_not_equated_with_verified_change():
    a, b = sample(), sample()
    b["trading_days"][0]["source_ids"] = ["b"]
    result = compare(a, b, source_text_after={"a": "synthetic source text", "b": "other source"})
    assert (result.status, result.reason) == ("UNKNOWN", "SOURCE_CHANGE_UNEXPLAINED")
    assert any(c.category == "source_basis" for c in result.changes)
    b = sample()
    b["sources"][0]["description"] = "unexplained replacement"
    assert compare(a, b).reason == "SOURCE_CHANGE_UNEXPLAINED"
    b = sample()
    b["sources"][0]["page_sha256"] = "changed"
    assert compare(a, b).status == "UNKNOWN"


@pytest.mark.parametrize("field", ["windows_complete", "exceptions_complete"])
def test_partial_cannot_prove_unchanged(field):
    a, b = sample(), sample()
    b["trading_hours"][0][field] = False
    assert compare(a, b).reason == "SCOPE_INCOMPLETE"


def test_conflict_unknown_fields_missing_references_and_uncovered_scope():
    a = sample()
    for edit in (
        lambda b: b["trading_hours"].append(deepcopy(b["trading_hours"][0])),
        lambda b: b["trading_hours"][0].pop("night_windows"),
        lambda b: b["trading_hours"][0].update(extra_field="ignored?"),
        lambda b: b["trading_days"][0].update(source_ids=["missing"]),
    ):
        b = sample()
        edit(b)
        assert compare(a, b).status == "UNKNOWN"
    assert compare(a, a, scopes=()).status == "UNKNOWN"
    assert compare(a, a, end=date(2026, 1, 4)).reason == "SCOPE_INCOMPLETE"


def test_venue_and_option_identity_do_not_collide():
    a = sample()
    extra = deepcopy(a["trading_hours"][0])
    extra.update(kind="option", contracts=["opt1"])
    a["trading_hours"].append(extra)
    for row in deepcopy(a["trading_days"]):
        row["venue"] = "Y"
        a["trading_days"].append(row)
    extra = deepcopy(a["trading_hours"][0])
    extra.update(venue="Y", contracts=["cu1"])
    a["trading_hours"].append(extra)
    b = deepcopy(a)
    b["trading_hours"][1]["day_windows"][0]["start"] = "10:00"
    assert compare(a, b).status == "SAME"
    scope = (CalendarScope("X", "option", "cu", "opt1"),)
    assert compare(a, b, scopes=scope).status == "CHANGED"
    scope = (CalendarScope("Y", "future", "cu", "cu1"),)
    assert compare(a, b, scopes=scope).status == "SAME"


def test_missing_hash_only_and_changed_source_content_are_unknown():
    a = sample()
    assert compare(a, a, source_text_before={}).reason == "SOURCE_EVIDENCE_MISSING"
    assert compare(a, a, source_text_before={"a": "a" * 64}).reason == "SOURCE_EVIDENCE_MISSING"
    assert compare(a, a, source_text_before={"a": "SHA-256 = " + "a" * 64}).reason == (
        "SOURCE_EVIDENCE_MISSING"
    )
    assert compare(a, a, source_text_after={"a": "different text"}).reason == (
        "SOURCE_CHANGE_UNEXPLAINED"
    )
    changed = deepcopy(a)
    changed["trading_hours"][0]["day_windows"][0]["end"] = "10:00"
    result = compare(a, changed, source_text_after={"a": "different text"})
    assert result.status == "UNKNOWN"
    assert any(change.category == "trading_session" for change in result.changes)


def test_comparison_work_and_source_text_are_bounded():
    a = sample()
    assert compare(a, a, source_text_before={"a": "x" * 2_000_001}).reason == (
        "SOURCE_EVIDENCE_MISSING"
    )
    b = sample()
    b["sources"][0]["description"] = "x" * 8_000_001
    assert compare(a, b).status == "UNKNOWN"


def closed_days(count, contract_count=1):
    content = sample()
    content["trading_days"] = [
        {
            "venue": "X",
            "day": (START + timedelta(days=i)).isoformat(),
            "status": "closed",
            "source_ids": ["a"],
        }
        for i in range(count)
    ]
    row = content["trading_hours"][0]
    row["contracts"] = [f"c{i}" for i in range(contract_count)]
    row["valid_to"] = (START + timedelta(days=count - 1)).isoformat()
    row["night_mappings"] = []
    return content


def test_two_year_ten_contract_declaration_is_comparable():
    content = closed_days(731, 10)
    row = content["trading_hours"][0]
    for index, day in enumerate(content["trading_days"]):
        if index % 7 < 5:
            day["status"] = "open"
            row["night_mappings"].append(
                {"trading_day": day["day"], "start_date": None, "source_ids": ["a"]}
            )
    result = compare(
        content,
        deepcopy(content),
        scopes=(CalendarScope("X", "future", "cu", "c0"),),
        end=START + timedelta(days=730),
    )
    assert result.status == "SAME"


def test_scope_scan_and_window_expansion_are_rejected_before_snapshot(monkeypatch):
    def fail_snapshot(*args, **kwargs):
        raise AssertionError("snapshot should not be constructed")

    monkeypatch.setattr(semantic_diff, "CalendarSnapshot", fail_snapshot)
    large_scope = closed_days(400, 400)
    assert compare(large_scope, large_scope).status == "UNKNOWN"
    large_windows = closed_days(365)
    large_windows["trading_hours"][0]["day_windows"] = [
        {
            "id": f"w{i}",
            "phase": "continuous",
            "start": "09:00",
            "end": "10:00",
            "end_day_offset": 0,
        }
        for i in range(1000)
    ]
    assert compare(large_windows, large_windows).status == "UNKNOWN"


def test_query_and_structure_budget_rejected_before_snapshot(monkeypatch):
    def fail_snapshot(*args, **kwargs):
        raise AssertionError("snapshot should not be constructed")

    monkeypatch.setattr(semantic_diff, "CalendarSnapshot", fail_snapshot)
    content = closed_days(731)
    scopes = tuple(CalendarScope("X", "future", "cu", f"c{i}") for i in range(150))
    assert compare(content, content, scopes=scopes, end=START + timedelta(days=730)).status == (
        "UNKNOWN"
    )
    content["sources"][0]["description"] = "x" * 8_000_001
    assert compare(content, content).status == "UNKNOWN"


def test_source_map_size_and_key_type_are_checked_before_set(monkeypatch):
    def fail_set(value):
        raise AssertionError("invalid source map must not be converted to a set")

    records = {("source_basis", '"a"'): "{}"}
    monkeypatch.setattr(semantic_diff, "set", fail_set, raising=False)
    assert not semantic_diff._valid_source_texts({"a": "x", "extra": "y"}, records)
    assert not semantic_diff._valid_source_texts({"x" * 129: "x"}, records)
    assert not semantic_diff._valid_source_texts({123: "x"}, records)


def test_contract_membership_uses_set_and_keeps_sorted_output(monkeypatch):
    content = sample()
    content["trading_hours"][0]["contracts"].reverse()
    expected = semantic_diff._records(content, SCOPE, START.isoformat(), END.isoformat())

    class NoListMembership(list):
        def __contains__(self, value):
            raise AssertionError("contract membership must not scan a list")

    def checked_sorted(values, *args, **kwargs):
        result = builtins.sorted(values, *args, **kwargs)
        return NoListMembership(result) if result == ["cu1", "cu2"] else result

    monkeypatch.setattr(semantic_diff, "sorted", checked_sorted, raising=False)
    assert semantic_diff._records(content, SCOPE, START.isoformat(), END.isoformat()) == expected
