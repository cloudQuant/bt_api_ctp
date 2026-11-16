from copy import deepcopy
from dataclasses import FrozenInstanceError
from datetime import date

import pytest
from test_calendar_interruption_simulation import fixture, interruption
from test_calendar_snapshot import synthetic

from bt_api_ctp.calendar_snapshot import CalendarSnapshot


def bounds(snapshot, venue="XSH", kind="future", product="cu", contract="cu2602"):
    return snapshot.owner_day_bounds(venue=venue, kind=kind, product=product, contract=contract)


def test_whole_scope_far_owner_and_exceptions_are_compiled():
    data = synthetic()
    data["trading_hours"][0]["night_mappings"][1]["start_date"] = "2026-09-01"
    snapshot = CalendarSnapshot(data, calendar_ref="v1")
    far = bounds(snapshot)
    assert (far.status, far.max_advance_days, far.max_spill_days) == ("COMPLETE", 41, 0)
    assert far.calendar_ref == "v1" and far.sources_verified is False
    assert bounds(snapshot, contract="cu2601").max_advance_days == 0  # close_day
    assert bounds(snapshot, kind="option", contract="cu2601-C-100").max_advance_days == 0
    assert bounds(snapshot, venue="XDCE", product="m", contract="m2601").max_advance_days == 3
    assert bounds(snapshot, contract="absent").status == "OUT_OF_SCOPE"
    with pytest.raises(FrozenInstanceError):
        far.max_advance_days = 0
    assert (
        snapshot.query(
            venue="XSH",
            kind="future",
            product="cu",
            contract="cu2602",
            trading_day=date(2026, 10, 14),
        ).status
        == "UNKNOWN"
    )


def test_interruption_and_cancel_window_preserve_exact_scope():
    data = fixture()
    data["exceptions"] = [
        interruption(
            window_id="night", start_utc="2026-06-01T15:30:00Z", end_utc="2026-06-01T16:15:00Z"
        )
    ]
    snapshot = CalendarSnapshot(data, calendar_ref="v1")
    scope = dict(venue="SHFE", product="cu")
    assert bounds(snapshot, **scope, contract="cu1").max_advance_days == 1
    assert bounds(snapshot, **scope, kind="option", contract="cuopt").max_advance_days == 1
    data["exceptions"] = [
        dict(
            venue="SHFE",
            kind="future",
            product="cu",
            contract="cu1",
            trading_day="2026-06-02",
            action="cancel_window",
            window_id="night",
            source_ids=["MOCK.simulation_only"],
        )
    ]
    snapshot = CalendarSnapshot(data, calendar_ref="v2")
    assert bounds(snapshot, **scope, contract="cu1").max_advance_days == 0
    assert bounds(snapshot, **scope, contract="cu2").max_advance_days == 1


def test_incomplete_any_compiled_row_empty_closed_and_no_rows():
    data = synthetic()
    data["trading_hours"][0]["windows_complete"] = False
    result = bounds(CalendarSnapshot(data, calendar_ref="v1"))
    assert (result.status, result.reason, result.max_advance_days) == (
        "UNKNOWN",
        "RULE_COVERAGE_INCOMPLETE",
        None,
    )
    data = synthetic()
    data["trading_days"] = [row for row in data["trading_days"] if row["status"] == "closed"]
    for rule in data["trading_hours"]:
        rule["night_mappings"] = []
    data["exceptions"] = []
    snapshot = CalendarSnapshot(data, calendar_ref="v2")
    result = bounds(snapshot)
    assert (result.status, result.max_advance_days, result.max_spill_days) == ("COMPLETE", 0, 0)
    data["trading_days"] = []
    result = bounds(CalendarSnapshot(data, calendar_ref="v3"))
    assert (result.status, result.reason) == ("UNKNOWN", "NO_COMPILED_ROWS")


def test_one_incomplete_period_poisoning_scope_ignores_row_order():
    data = synthetic()
    first = data["trading_hours"][0]
    second = deepcopy(first)
    first["valid_to"] = "2026-10-11"
    first["night_mappings"] = first["night_mappings"][:1]
    second["valid_from"] = "2026-10-12"
    second["night_mappings"] = second["night_mappings"][1:]
    second["exceptions_complete"] = False
    data["trading_hours"] = [first, second, *data["trading_hours"][1:]]
    for rows in (data["trading_hours"], list(reversed(data["trading_hours"]))):
        data["trading_hours"] = rows
        result = bounds(CalendarSnapshot(data, calendar_ref="v1"))
        assert (result.status, result.reason) == ("UNKNOWN", "RULE_COVERAGE_INCOMPLETE")
