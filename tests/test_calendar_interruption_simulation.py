"""Versioned mock-only local interruptions, never exchange evidence."""

from copy import deepcopy
from datetime import date, datetime, timedelta, timezone

import pytest
import yaml

from bt_api_ctp import calendar_snapshot as module
from bt_api_ctp.calendar_snapshot import CalendarSnapshot, CalendarSnapshotError
from bt_api_ctp.calendar_supplement import SupplementError, parse_supplement

MOCK = "MOCK.simulation_only"
DAY = "2026-06-02"


def fixture():
    def rule(kind, contracts):
        return {
            "venue": "SHFE",
            "kind": kind,
            "product": "cu",
            "contracts": contracts,
            "valid_from": DAY,
            "valid_to": DAY,
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
                {"trading_day": DAY, "start_date": "2026-06-01", "source_ids": [MOCK]}
            ],
            "source_ids": [MOCK],
            "windows_complete": True,
            "exceptions_complete": True,
        }

    return {
        "sources": [{"id": MOCK, "description": "explicit mock data"}],
        "trading_days": [{"venue": "SHFE", "day": DAY, "status": "open", "source_ids": [MOCK]}],
        "trading_hours": [rule("future", ["cu1", "cu2"]), rule("option", ["cuopt"])],
        "exceptions": [],
    }


def interruption(**changes):
    value = {
        "venue": "SHFE",
        "kind": "future",
        "product": "cu",
        "contract": "cu1",
        "trading_day": DAY,
        "action": "interrupt_window",
        "window_id": "day",
        "exception_schema": "bt_api.calendar-window-interruption/1",
        "start_utc": "2026-06-02T02:00:00Z",
        "end_utc": "2026-06-02T02:30:00Z",
        "source_ids": [MOCK],
    }
    value.update(changes)
    return value


def get(snapshot, *, kind="future", contract="cu1"):
    return snapshot.query(
        venue="SHFE",
        kind=kind,
        product="cu",
        contract=contract,
        trading_day=date.fromisoformat(DAY),
    )


def test_day_interruption_half_open_and_contract_option_isolation():
    data = fixture()
    data["exceptions"] = [interruption()]
    result = get(CalendarSnapshot(data, calendar_ref="mock-v1"))
    assert result.status == "COMPLETE" and result.sources_verified is False
    assert result.source_ids == (MOCK,)
    assert len(result.windows) == 3
    night, before, after = result.windows
    assert night.window_id == "night"
    assert before.window_id.startswith("interrupt.") and before.window_id.endswith(".pre")
    assert after.window_id.startswith("interrupt.") and after.window_id.endswith(".post")
    assert before.window_id != after.window_id
    start, end = (
        datetime(2026, 6, 2, 2, tzinfo=timezone.utc),
        datetime(2026, 6, 2, 2, 30, tzinfo=timezone.utc),
    )
    assert before.start_local.hour == 9 and before.end_local.hour == 10
    assert (
        after.start_local.hour == 10
        and after.start_local.minute == 30
        and after.end_local.hour == 11
    )
    assert before.contains(start - timedelta(microseconds=1))
    assert not before.contains(start) and not after.contains(start)
    assert not before.contains(end - timedelta(microseconds=1))
    assert after.contains(end) and not after.contains(after.end_utc)
    assert before.phase == after.phase == "continuous"
    assert before.trading_day == after.trading_day == date.fromisoformat(DAY)
    again = get(CalendarSnapshot(deepcopy(data), calendar_ref="mock-v2"))
    assert [w.window_id for w in again.windows] == [w.window_id for w in result.windows]
    sibling = get(CalendarSnapshot(data, calendar_ref="mock-v1"), contract="cu2")
    option = get(CalendarSnapshot(data, calendar_ref="mock-v1"), kind="option", contract="cuopt")
    assert [w.window_id for w in sibling.windows] == ["night", "day"]
    assert [w.window_id for w in option.windows] == ["night", "day"]


def test_night_interruption_crosses_midnight_preserving_owner():
    data = fixture()
    data["exceptions"] = [
        interruption(
            window_id="night", start_utc="2026-06-01T15:30:00Z", end_utc="2026-06-01T16:15:00Z"
        )
    ]
    before, after, day = get(CalendarSnapshot(data, calendar_ref="mock")).windows
    assert before.start_local.isoformat() == "2026-06-01T21:00:00+08:00"
    assert before.end_local.isoformat() == "2026-06-01T23:30:00+08:00"
    assert after.start_local.isoformat() == "2026-06-02T00:15:00+08:00"
    assert after.end_local.isoformat() == "2026-06-02T01:00:00+08:00"
    assert before.trading_day == after.trading_day == date.fromisoformat(DAY)
    assert day.window_id == "day"


def test_wildcard_applies_all_contracts_and_option_only_when_declared():
    data = fixture()
    data["exceptions"] = [interruption(contract=None)]
    snapshot = CalendarSnapshot(data, calendar_ref="mock")
    assert (
        len(get(snapshot, contract="cu1").windows)
        == len(get(snapshot, contract="cu2").windows)
        == 3
    )
    assert [w.window_id for w in get(snapshot, kind="option", contract="cuopt").windows] == [
        "night",
        "day",
    ]
    data["exceptions"].append(interruption(kind="option", contract="cuopt"))
    assert (
        len(
            get(
                CalendarSnapshot(data, calendar_ref="mock"), kind="option", contract="cuopt"
            ).windows
        )
        == 3
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"start_utc": "2026-06-02T01:00:00Z"},
        {"end_utc": "2026-06-02T03:00:00Z"},
        {"start_utc": "2026-06-02T00:59:59Z"},
        {"end_utc": "2026-06-02T03:00:01Z"},
        {"start_utc": "2026-06-02T02:30:00Z"},
        {"start_utc": "2026-06-02T02:31:00Z"},
        {"start_utc": "2026-06-02T02:00Z"},
        {"start_utc": "2026-06-02T02:00:00+00:00"},
        {"start_utc": "2026-06-02T02:00:00.0Z"},
        {"exception_schema": "bt_api.calendar-window-interruption/2"},
        {"source_ids": []},
        {"source_ids": ["unknown"]},
        {"contract": "unknown"},
        {"trading_day": "2026-06-03"},
        {"window_id": "unknown"},
    ],
)
def test_invalid_interruption_fails_closed(changes):
    data = fixture()
    data["exceptions"] = [interruption(**changes)]
    with pytest.raises(CalendarSnapshotError):
        CalendarSnapshot(data, calendar_ref="mock")


def test_fractional_canonical_utc_is_supported():
    data = fixture()
    data["exceptions"] = [interruption(start_utc="2026-06-02T02:00:00.000001Z")]
    result = get(CalendarSnapshot(data, calendar_ref="mock"))
    assert result.windows[1].end_utc.microsecond == 1


@pytest.mark.parametrize(
    "other",
    [
        interruption(),
        interruption(contract=None),
        {
            "venue": "SHFE",
            "kind": "future",
            "product": "cu",
            "contract": "cu1",
            "trading_day": DAY,
            "action": "cancel_window",
            "window_id": "day",
            "source_ids": [MOCK],
        },
        {
            "venue": "SHFE",
            "kind": "future",
            "product": "cu",
            "contract": "cu1",
            "trading_day": DAY,
            "action": "close_day",
            "window_id": None,
            "source_ids": [MOCK],
        },
    ],
)
@pytest.mark.parametrize("reverse", [False, True])
def test_same_window_and_day_conflicts_rejected_in_either_order(other, reverse):
    data = fixture()
    data["exceptions"] = [interruption(), other]
    if reverse:
        data["exceptions"].reverse()
    with pytest.raises(CalendarSnapshotError):
        CalendarSnapshot(data, calendar_ref="mock")


def test_generated_ids_cannot_collide_with_templates_or_other_segments(monkeypatch):
    data = fixture()
    data["exceptions"] = [interruption()]
    monkeypatch.setattr(module, "_interruption_ids", lambda *args: ("day", "new"))
    with pytest.raises(CalendarSnapshotError, match="collision"):
        CalendarSnapshot(data, calendar_ref="mock")
    monkeypatch.setattr(module, "_interruption_ids", lambda *args: ("same", "same"))
    with pytest.raises(CalendarSnapshotError, match="collision"):
        CalendarSnapshot(data, calendar_ref="mock")
    data["exceptions"].append(
        interruption(
            window_id="night", start_utc="2026-06-01T15:30:00Z", end_utc="2026-06-01T16:15:00Z"
        )
    )
    monkeypatch.setattr(module, "_interruption_ids", lambda *args: ("new1", "new2"))
    with pytest.raises(CalendarSnapshotError, match="collision"):
        CalendarSnapshot(data, calendar_ref="mock")


def test_supplement_accepts_only_versioned_mock_interruption_and_old_shape_still_works():
    data = fixture()
    data["exceptions"] = [interruption()]
    encoded = yaml.safe_dump(data, sort_keys=False).encode()
    result = parse_supplement(encoded)
    assert result.verification == "UNVERIFIED" and result.content == data
    assert get(CalendarSnapshot(result.content, calendar_ref="mock")).sources_verified is False
    for changes in (
        {"source_ids": ["bad"]},
        {"exception_schema": "other"},
        {"start_utc": "2026-06-02T02:00Z"},
        {"end_utc": "2026-06-02T01:00:00Z"},
    ):
        invalid = deepcopy(data)
        invalid["exceptions"][0].update(changes)
        with pytest.raises(SupplementError):
            parse_supplement(yaml.safe_dump(invalid).encode())
    old = fixture()
    assert parse_supplement(yaml.safe_dump(old).encode()).content == old


def test_existing_exact_field_readers_reject_new_exception_shape():
    from bt_api_ctp import calendar_supplement

    legacy_fields = {
        "venue",
        "kind",
        "product",
        "contract",
        "trading_day",
        "action",
        "window_id",
        "source_ids",
    }
    new_row = interruption()
    with pytest.raises(CalendarSnapshotError):
        module._object(new_row, legacy_fields)
    with pytest.raises(SupplementError):
        calendar_supplement._object(new_row, legacy_fields)


def test_non_mock_but_known_source_is_not_misrepresented_as_simulation():
    data = fixture()
    data["sources"].append({"id": "other", "description": "not a mock assertion"})
    data["exceptions"] = [interruption(source_ids=["other"])]
    with pytest.raises(CalendarSnapshotError):
        CalendarSnapshot(data, calendar_ref="mock")
    with pytest.raises(SupplementError):
        parse_supplement(yaml.safe_dump(data).encode())


def test_interrupted_auction_preserves_phase():
    data = fixture()
    data["trading_hours"][0]["day_windows"][0]["phase"] = "auction"
    data["exceptions"] = [interruption()]
    result = get(CalendarSnapshot(data, calendar_ref="mock"))
    assert [window.phase for window in result.windows[1:]] == ["auction", "auction"]


def test_exceptions_cannot_target_a_generated_segment():
    data = fixture()
    data["exceptions"] = [interruption()]
    generated = get(CalendarSnapshot(data, calendar_ref="mock")).windows[1].window_id
    nested = interruption(
        window_id=generated, start_utc="2026-06-02T01:30:00Z", end_utc="2026-06-02T01:45:00Z"
    )
    data["exceptions"].append(nested)
    with pytest.raises(CalendarSnapshotError):
        CalendarSnapshot(data, calendar_ref="mock")
    data["exceptions"][1] = {
        "venue": "SHFE",
        "kind": "future",
        "product": "cu",
        "contract": "cu1",
        "trading_day": DAY,
        "action": "cancel_window",
        "window_id": generated,
        "source_ids": [MOCK],
    }
    with pytest.raises(CalendarSnapshotError):
        CalendarSnapshot(data, calendar_ref="mock")


def test_generated_ids_cannot_collide_across_other_scopes(monkeypatch):
    data = fixture()
    data["trading_hours"][1]["day_windows"][0]["id"] = "option-day"
    data["exceptions"] = [interruption()]
    monkeypatch.setattr(module, "_interruption_ids", lambda *args: ("option-day", "new"))
    with pytest.raises(CalendarSnapshotError, match="collision"):
        CalendarSnapshot(data, calendar_ref="mock")
    data["exceptions"].append(interruption(kind="option", contract="cuopt", window_id="option-day"))
    monkeypatch.setattr(module, "_interruption_ids", lambda *args: ("generated1", "generated2"))
    with pytest.raises(CalendarSnapshotError, match="collision"):
        CalendarSnapshot(data, calendar_ref="mock")


def test_old_exception_error_priority_is_unchanged():
    data = fixture()
    data["exceptions"] = [
        {
            "venue": "SHFE",
            "kind": "future",
            "product": "cu",
            "contract": "cu1",
            "trading_day": "2026-06-03",
            "action": "cancel_window",
            "window_id": "day",
            "source_ids": ["unknown"],
        }
    ]
    with pytest.raises(CalendarSnapshotError, match="exception requires known open day"):
        CalendarSnapshot(data, calendar_ref="mock")
