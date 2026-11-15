"""Synthetic supplement candidates, never exchange evidence."""

import builtins
from copy import deepcopy

import pytest
import yaml

from bt_api_ctp.calendar_snapshot import CalendarSnapshot
from bt_api_ctp.calendar_supplement import (
    MAX_INPUT_BYTES,
    MAX_LIST_ITEMS,
    MAX_TEXT_LENGTH,
    MAX_YAML_EVENTS,
    SupplementError,
    parse_supplement,
)


def example():
    return {
        "sources": [
            {"id": "notice-a", "description": "合成公告甲：仅用于测试"},
            {"id": "notice-b", "description": "合成公告乙：范围待核实"},
            {"id": "unused", "description": "保留但没有引用"},
        ],
        "trading_days": [
            {
                "venue": "SHFE",
                "day": "2026-11-12",
                "status": "open",
                "source_ids": ["notice-a", "notice-b"],
            },
        ],
        "trading_hours": [
            {
                "venue": "SHFE",
                "kind": "future",
                "product": "rb",
                "contracts": ["rb2701"],
                "valid_from": "2026-11-12",
                "valid_to": "2026-11-12",
                "day_windows": [
                    {
                        "id": "morning",
                        "phase": "continuous",
                        "start": "09:00",
                        "end": "10:15",
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
                    {
                        "trading_day": "2026-11-12",
                        "start_date": "2026-11-11",
                        "source_ids": ["notice-b"],
                    }
                ],
                "source_ids": ["notice-a"],
                "windows_complete": False,
                "exceptions_complete": False,
            },
        ],
        "exceptions": [
            {
                "venue": "SHFE",
                "kind": "future",
                "product": "rb",
                "contract": "rb2701",
                "trading_day": "2026-11-12",
                "action": "cancel_window",
                "window_id": "night",
                "source_ids": ["notice-a", "notice-b"],
            },
        ],
    }


def encode(content):
    return yaml.safe_dump(content, allow_unicode=True, sort_keys=False).encode("utf-8")


def test_chinese_all_layers_sources_references_and_unused_source_are_preserved():
    content = example()
    result = parse_supplement(b"\xef\xbb\xbf" + encode(content))
    assert result.content == content
    assert result.unused_source_ids == ("unused",)
    assert result.verification == "UNVERIFIED"
    assert result.content["exceptions"][0]["source_ids"] == ["notice-a", "notice-b"]


def test_overlapping_records_are_preserved_and_never_marked_verified():
    content = example()
    conflict = deepcopy(content["trading_days"][0])
    conflict["status"] = "closed"
    content["trading_days"].append(conflict)
    content["exceptions"].append(deepcopy(content["exceptions"][0]))
    result = parse_supplement(encode(content))
    assert result.content == content
    assert result.verification == "UNVERIFIED"


def test_synthetic_content_shape_can_be_consumed_without_asserting_source_truth():
    from datetime import date

    candidate = parse_supplement(encode(example()))
    snapshot = CalendarSnapshot(candidate.content, calendar_ref="synthetic")
    result = snapshot.query(
        venue="SHFE",
        kind="future",
        product="rb",
        contract="rb2701",
        trading_day=date(2026, 11, 12),
    )
    assert result.status == "UNKNOWN"
    assert result.sources_verified is False


def test_user_asserted_completeness_is_not_source_verification_or_release_approval():
    from datetime import date

    content = example()
    content["trading_hours"][0]["windows_complete"] = True
    content["trading_hours"][0]["exceptions_complete"] = True
    candidate = parse_supplement(encode(content))
    snapshot = CalendarSnapshot(candidate.content, calendar_ref="synthetic")
    result = snapshot.query(
        venue="SHFE",
        kind="future",
        product="rb",
        contract="rb2701",
        trading_day=date(2026, 11, 12),
    )
    assert candidate.verification == "UNVERIFIED"
    assert result.status == "COMPLETE"  # reflects input flags, not evidence
    assert result.sources_verified is False


@pytest.mark.parametrize(
    "raw",
    [
        b"\xff",
        b"",
        b"null",
        b"[]",
        b"!!python/object/apply:os.system ['echo blocked']",
        b"sources: []\nsources: []\ntrading_days: []\ntrading_hours: []\nexceptions: []",
        b"sources: &a []\ntrading_days: *a\ntrading_hours: []\nexceptions: []",
        b"sources: &a [*a]\ntrading_days: []\ntrading_hours: []\nexceptions: []",
        b"[" * 26 + b"]" * 26,
        b"a" * (MAX_INPUT_BYTES + 1),
        b"sources: []\ntrading_days: []\ntrading_hours: []\nexceptions: []\nextra: true",
    ],
)
def test_invalid_unsafe_and_bounded_yaml_is_rejected(raw):
    with pytest.raises(SupplementError):
        parse_supplement(raw)


def test_event_count_is_bounded_before_yaml_construction():
    raw = b"[" + b"0," * MAX_YAML_EVENTS + b"]"
    assert len(raw) < MAX_INPUT_BYTES
    with pytest.raises(SupplementError, match="event limit"):
        parse_supplement(raw)


@pytest.mark.parametrize(
    "change",
    [
        lambda c: c.update(sources=[]),
        lambda c: c.update(trading_days=[], trading_hours=[], exceptions=[]),
        lambda c: c["sources"].append(deepcopy(c["sources"][0])),
        lambda c: c["sources"][0].update(description=" "),
        lambda c: c["sources"][0].update(description="a" * (MAX_TEXT_LENGTH + 1)),
        lambda c: c["trading_days"][0].update(source_ids=[]),
        lambda c: c["trading_days"][0].update(source_ids=["missing"]),
        lambda c: c["trading_days"][0].update(source_ids=["notice-a", "notice-a"]),
        lambda c: c["trading_days"][0].update(day="2026-02-30"),
        lambda c: c["trading_days"][0].update(day="2026-1-2"),
        lambda c: c["trading_days"][0].update(venue="UNKNOWN"),
        lambda c: c["trading_days"][0].update(status=float("nan")),
        lambda c: c["trading_hours"][0].update(valid_from="2026-11-13"),
        lambda c: c["trading_hours"][0].update(kind="unknown"),
        lambda c: c["trading_hours"][0].update(contracts=[]),
        lambda c: c["trading_hours"][0].update(windows_complete=1),
        lambda c: c["trading_hours"][0]["day_windows"][0].update(end="08:59"),
        lambda c: c["trading_hours"][0]["night_windows"][0].update(end_day_offset=True),
        lambda c: c["trading_hours"][0]["night_mappings"][0].update(start_date="2026-11-12"),
        lambda c: c["exceptions"][0].update(action="unknown"),
        lambda c: c["exceptions"][0].update(window_id=None),
        lambda c: c["exceptions"][0].update(extra="unknown"),
        lambda c: c.update(trading_days=[None] * (MAX_LIST_ITEMS + 1)),
    ],
)
def test_invalid_schema_scope_references_and_types(change):
    content = example()
    change(content)
    with pytest.raises(SupplementError):
        parse_supplement(encode(content))


def test_unquoted_yaml_date_and_duplicate_nested_mapping_are_rejected():
    raw = encode(example()).replace(b"'2026-11-12'", b"2026-11-12", 1)
    with pytest.raises(SupplementError):
        parse_supplement(raw)
    raw = encode(example()).replace(b"status: open", b"status: open\n  status: closed")
    with pytest.raises(SupplementError):
        parse_supplement(raw)


def test_optional_dependency_missing_is_reported_without_installing(monkeypatch):
    original = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "yaml":
            raise ModuleNotFoundError("yaml")
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    with pytest.raises(SupplementError, match="PyYAML dependency unavailable"):
        parse_supplement(b"{}")
