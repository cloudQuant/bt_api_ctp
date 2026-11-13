"""Synthetic fixtures exercise a provisional model, never official venue facts."""

from copy import deepcopy
from datetime import date, datetime, timedelta, timezone

import pytest

from bt_api_ctp.calendar_snapshot import CalendarSnapshot, CalendarSnapshotError


def window(name, start, end, offset=0, phase="continuous"):
    return {"id": name, "phase": phase, "start": start, "end": end, "end_day_offset": offset}


def mapping(day, start_date):
    return {"trading_day": day, "start_date": start_date, "source_ids": ["synthetic"]}


def synthetic():
    source = ["synthetic"]
    days = [
        {"venue": venue, "day": day, "status": status, "source_ids": source}
        for venue in ("XSH", "XDCE")
        for day, status in (
            ("2026-10-09", "open"),
            ("2026-10-10", "closed"),
            ("2026-10-11", "closed"),
            ("2026-10-12", "open"),
            ("2026-10-13", "open"),
        )
    ]

    def rules(venue, kind, product, contracts, day_windows, night_windows, friday_night):
        return {
            "venue": venue,
            "kind": kind,
            "product": product,
            "contracts": contracts,
            "valid_from": "2026-10-09",
            "valid_to": "2026-10-13",
            "day_windows": day_windows,
            "night_windows": night_windows,
            "night_mappings": [
                mapping("2026-10-09", None),
                mapping("2026-10-12", friday_night),
                mapping("2026-10-13", None),
            ],
            "windows_complete": True,
            "exceptions_complete": True,
            "source_ids": source,
        }

    return {
        "sources": [
            {"id": "synthetic", "description": "fictional data for unit tests only"},
            {"id": "synthetic-exception", "description": "fictional exception notice"},
        ],
        "trading_days": days,
        "trading_hours": [
            rules(
                "XSH",
                "future",
                "cu",
                ["cu2601", "cu2602"],
                [window("am", "09:00", "10:15"), window("pm", "13:30", "15:00")],
                [window("night", "21:00", "01:00", 1)],
                "2026-10-09",
            ),
            rules(
                "XDCE",
                "future",
                "m",
                ["m2601"],
                [window("am", "09:00", "11:30")],
                [window("night", "21:00", "23:00")],
                "2026-10-09",
            ),
            rules(
                "XSH",
                "option",
                "cu",
                ["cu2601-C-100"],
                [window("option-am", "10:00", "11:00")],
                [],
                None,
            ),
        ],
        "exceptions": [
            {
                "venue": "XSH",
                "kind": "future",
                "product": "cu",
                "contract": "cu2601",
                "trading_day": "2026-10-12",
                "action": "close_day",
                "window_id": None,
                "source_ids": ["synthetic-exception"],
            },
            {
                "venue": "XSH",
                "kind": "future",
                "product": "cu",
                "contract": "cu2602",
                "trading_day": "2026-10-13",
                "action": "cancel_window",
                "window_id": "pm",
                "source_ids": ["synthetic-exception"],
            },
        ],
    }


def query(snapshot, day="2026-10-12", venue="XSH", kind="future", product="cu", contract="cu2602"):
    return snapshot.query(
        venue=venue,
        kind=kind,
        product=product,
        contract=contract,
        trading_day=date.fromisoformat(day),
    )


def test_explicit_friday_night_belongs_to_monday_and_is_half_open_utc():
    snapshot = CalendarSnapshot(synthetic(), calendar_ref="synthetic-v1")
    result = query(snapshot)
    assert (result.status, result.calendar_ref, result.sources_verified) == (
        "COMPLETE",
        "synthetic-v1",
        False,
    )
    assert result.source_ids == ("synthetic",)
    night = result.windows[0]
    assert (night.trading_day, night.phase, night.window_id) == (
        date(2026, 10, 12),
        "continuous",
        "night",
    )
    assert night.start_local.isoformat() == "2026-10-09T21:00:00+08:00"
    assert night.end_local.isoformat() == "2026-10-10T01:00:00+08:00"
    assert night.start_utc == datetime(2026, 10, 9, 13, tzinfo=timezone.utc)
    assert night.end_utc == datetime(2026, 10, 9, 17, tzinfo=timezone.utc)
    assert night.contains(night.start_utc)
    assert night.contains(night.end_utc - timedelta(microseconds=1))
    assert not night.contains(night.end_utc)
    assert not night.contains(night.start_utc - timedelta(microseconds=1))
    with pytest.raises(CalendarSnapshotError):
        night.contains(datetime(2026, 10, 9, 13))
    with pytest.raises(CalendarSnapshotError):
        night.contains(night.start_local)


def test_different_venue_hours_and_no_implied_night_before_holiday():
    snapshot = CalendarSnapshot(synthetic(), calendar_ref="synthetic-v1")
    dce = query(snapshot, venue="XDCE", product="m", contract="m2601")
    assert dce.windows[0].end_local.isoformat() == "2026-10-09T23:00:00+08:00"
    preholiday = query(snapshot, day="2026-10-13")
    assert [w.window_id for w in preholiday.windows] == ["am"]
    assert "synthetic-exception" in preholiday.source_ids


def test_night_date_comes_from_explicit_mapping_not_weekday_arithmetic():
    data = synthetic()
    data["trading_hours"][0]["night_mappings"][1]["start_date"] = "2026-10-08"
    snapshot = CalendarSnapshot(data, calendar_ref="synthetic-v1")
    assert query(snapshot).windows[0].start_local.date() == date(2026, 10, 8)
    assert query(snapshot).windows[0].trading_day == date(2026, 10, 12)


def test_product_exception_does_not_escape_kind_or_product_scope():
    data = synthetic()
    data["exceptions"].append(
        {
            "venue": "XSH",
            "kind": "future",
            "product": "cu",
            "contract": None,
            "trading_day": "2026-10-13",
            "action": "cancel_window",
            "window_id": "am",
            "source_ids": ["synthetic-exception"],
        }
    )
    snapshot = CalendarSnapshot(data, calendar_ref="synthetic-v1")
    assert [w.window_id for w in query(snapshot, day="2026-10-13", contract="cu2601").windows] == [
        "pm"
    ]
    second = query(snapshot, day="2026-10-13", contract="cu2602")
    assert second.status == "COMPLETE" and second.windows == ()
    option = query(snapshot, day="2026-10-13", kind="option", contract="cu2601-C-100")
    assert [w.window_id for w in option.windows] == ["option-am"]


def test_exact_contract_exception_does_not_affect_sibling_or_option():
    snapshot = CalendarSnapshot(synthetic(), calendar_ref="synthetic-v1")
    closed = query(snapshot, contract="cu2601")
    sibling = query(snapshot, contract="cu2602")
    option = query(snapshot, kind="option", contract="cu2601-C-100")
    assert closed.status == "COMPLETE" and closed.windows == ()
    assert "synthetic-exception" in closed.source_ids
    assert [w.window_id for w in sibling.windows] == ["night", "am", "pm"]
    assert [w.window_id for w in option.windows] == ["option-am"]
    assert option.windows[0].start_local.hour == 10
    assert "synthetic-exception" not in option.source_ids
    assert query(snapshot, kind="future", contract="cu2601-C-100").status == "OUT_OF_SCOPE"
    assert query(snapshot, kind="option", contract="cu2602").status == "OUT_OF_SCOPE"


def test_explicit_closed_day_empty_differs_from_unknown_and_wrong_identity():
    snapshot = CalendarSnapshot(synthetic(), calendar_ref="synthetic-v1")
    closed = query(snapshot, day="2026-10-10")
    assert closed.status == "COMPLETE" and closed.windows == () and closed.source_ids
    assert query(snapshot, day="2026-10-14").status == "UNKNOWN"
    assert query(snapshot, day="2026-10-14").reason == "COVERAGE_INSUFFICIENT"
    assert query(snapshot, venue="WRONG").status == "OUT_OF_SCOPE"
    assert query(snapshot, product="wrong").status == "OUT_OF_SCOPE"
    assert query(snapshot, contract="cu9999").status == "OUT_OF_SCOPE"
    with pytest.raises(CalendarSnapshotError):
        snapshot.query(
            venue="XSH", kind="future", product="cu", contract="cu2602", trading_day="2026-10-12"
        )


def test_missing_daily_record_and_partial_layers_cannot_prove_empty():
    data = synthetic()
    data["trading_days"] = [
        row
        for row in data["trading_days"]
        if not (row["venue"] == "XSH" and row["day"] == "2026-10-10")
    ]
    snapshot = CalendarSnapshot(data, calendar_ref="synthetic-v1")
    assert query(snapshot, day="2026-10-10").status == "UNKNOWN"
    for flag in ("windows_complete", "exceptions_complete"):
        data = synthetic()
        data["trading_hours"][0][flag] = False
        snapshot = CalendarSnapshot(data, calendar_ref="synthetic-v1")
        for day in ("2026-10-10", "2026-10-12"):
            result = query(snapshot, day=day)
            assert (result.status, result.reason, result.windows) == (
                "UNKNOWN",
                "RULE_COVERAGE_INCOMPLETE",
                (),
            )


def test_snapshot_is_detached_and_read_only():
    data = synthetic()
    snapshot = CalendarSnapshot(data, calendar_ref="synthetic-v1")
    data["trading_hours"][0]["day_windows"][0]["start"] = "06:00"
    assert query(snapshot).windows[1].start_local.hour == 9
    with pytest.raises(AttributeError):
        snapshot._ref = "other"
    with pytest.raises(TypeError):
        snapshot._values[("x",)] = ()


@pytest.mark.parametrize("slot", CalendarSnapshot.__slots__)
def test_sealed_snapshot_rejects_slot_deletion(slot):
    snapshot = CalendarSnapshot(synthetic(), calendar_ref="synthetic-v1")
    with pytest.raises(AttributeError, match="read-only"):
        delattr(snapshot, slot)
    assert query(snapshot).status == "COMPLETE"
    assert (
        snapshot.query_venue_day(venue="XSH", trading_day=date(2026, 10, 12)).day_status == "open"
    )


def test_unsealed_snapshot_allows_slot_deletion():
    snapshot = CalendarSnapshot.__new__(CalendarSnapshot)
    snapshot._ref = "temporary"
    del snapshot._ref
    assert not hasattr(snapshot, "_ref")


@pytest.mark.parametrize("venue", ["SHFE", "DCE", "CZCE", "CFFEX", "INE", "GFEX"])
def test_venue_day_returns_explicit_open_and_closed_with_daily_sources(venue):
    data = synthetic()
    data["trading_days"].extend(
        {"venue": venue, "day": day, "status": status, "source_ids": ["synthetic-exception"]}
        for day, status in (("2026-10-12", "open"), ("2026-10-13", "closed"))
    )
    snapshot = CalendarSnapshot(data, calendar_ref="synthetic-v1")
    for day, declared in ((date(2026, 10, 12), "open"), (date(2026, 10, 13), "closed")):
        result = snapshot.query_venue_day(venue=venue, trading_day=day)
        assert (result.status, result.reason, result.day_status) == (
            "COMPLETE",
            "EXPLICIT_DATA",
            declared,
        )
        assert (result.venue, result.trading_day, result.calendar_ref) == (
            venue,
            day,
            query(snapshot).calendar_ref,
        )
        assert result.source_ids == ("synthetic-exception",)
        assert result.sources_verified is False


def test_venue_day_is_not_inferred_from_contract_windows_or_completeness():
    data = synthetic()
    data["trading_hours"][0]["windows_complete"] = False
    snapshot = CalendarSnapshot(data, calendar_ref="synthetic-v1")
    day = date(2026, 10, 12)
    assert query(snapshot, contract="cu2601").status == "UNKNOWN"
    venue = snapshot.query_venue_day(venue="XSH", trading_day=day)
    assert (venue.status, venue.day_status, venue.source_ids) == (
        "COMPLETE",
        "open",
        ("synthetic",),
    )
    # An explicit contract closure also cannot imply a venue closure.
    complete = CalendarSnapshot(synthetic(), calendar_ref="synthetic-v1")
    assert query(complete, contract="cu2601").windows == ()
    assert complete.query_venue_day(venue="XSH", trading_day=day).day_status == "open"


def test_venue_day_missing_scope_coverage_and_source():
    data = synthetic()
    data["trading_days"] = [
        row
        for row in data["trading_days"]
        if row["venue"] != "XDCE" and not (row["venue"] == "XSH" and row["day"] == "2026-10-11")
    ]
    data["trading_hours"][1]["night_mappings"] = []
    snapshot = CalendarSnapshot(data, calendar_ref="synthetic-v1")
    missing = snapshot.query_venue_day(venue="XDCE", trading_day=date(2026, 10, 12))
    assert (missing.status, missing.reason, missing.day_status, missing.source_ids) == (
        "UNKNOWN",
        "COVERAGE_INSUFFICIENT",
        None,
        (),
    )
    for day in (date(2026, 10, 8), date(2026, 10, 11), date(2026, 10, 14)):
        assert snapshot.query_venue_day(venue="XSH", trading_day=day).status == "UNKNOWN"
    unknown = snapshot.query_venue_day(venue="ABSENT", trading_day=date(2026, 10, 12))
    assert (unknown.status, unknown.reason, unknown.day_status, unknown.source_ids) == (
        "OUT_OF_SCOPE",
        "VENUE_NOT_IN_SNAPSHOT",
        None,
        (),
    )
    for source_ids in ([], ["missing"]):
        data = synthetic()
        data["trading_days"][0]["source_ids"] = source_ids
        with pytest.raises(CalendarSnapshotError):
            CalendarSnapshot(data, calendar_ref="synthetic-v1")


def test_venue_day_date_boundaries_validation_and_mutation_isolation():
    data = synthetic()
    data["trading_days"].extend(
        {"venue": "XSH", "day": day, "status": status, "source_ids": ["synthetic"]}
        for day, status in (("0001-01-01", "closed"), ("9999-12-31", "open"))
    )
    snapshot = CalendarSnapshot(data, calendar_ref="synthetic-v1")
    for day, declared in ((date.min, "closed"), (date.max, "open")):
        assert snapshot.query_venue_day(venue="XSH", trading_day=day).day_status == declared
    for invalid in ("2026-10-12", datetime(2026, 10, 12), None):
        with pytest.raises(CalendarSnapshotError):
            snapshot.query_venue_day(venue="XSH", trading_day=invalid)
    with pytest.raises(CalendarSnapshotError):
        snapshot.query_venue_day(venue="", trading_day=date.min)
    data["trading_days"][-1]["status"] = "closed"
    data["trading_days"][-1]["source_ids"].clear()
    result = snapshot.query_venue_day(venue="XSH", trading_day=date.max)
    assert (result.day_status, result.source_ids) == ("open", ("synthetic",))
    with pytest.raises(AttributeError):
        result.day_status = "closed"
    with pytest.raises(TypeError):
        snapshot._days[("XSH", date.max)] = ("closed", ())


@pytest.mark.parametrize(
    "change",
    [
        lambda x: x["trading_days"][0].update(source_ids=["missing"]),
        lambda x: x["sources"].clear(),
        lambda x: x["trading_hours"][0].update(valid_to="2026-10-08"),
        lambda x: x["trading_hours"][0]["night_mappings"].pop(),
        lambda x: x["trading_hours"][0]["night_mappings"][1].update(start_date="2026-10-12"),
        lambda x: x["trading_hours"][0]["day_windows"].append(window("overlap", "10:00", "11:00")),
        lambda x: x["exceptions"][0].update(action="partial_close"),
        lambda x: x["exceptions"][1].update(window_id="absent"),
        lambda x: x["exceptions"][0].update(contract="unknown"),
        lambda x: x["trading_hours"][2].update(contracts=["cu2602"]),
        lambda x: x["trading_hours"].append(deepcopy(x["trading_hours"][0])),
        lambda x: x["trading_days"].append(deepcopy(x["trading_days"][0])),
        lambda x: x["trading_hours"][0]["night_windows"][0].update(end_day_offset=0),
        lambda x: x["trading_hours"][0].update(windows_complete=None),
        lambda x: x["trading_hours"][0].update(unknown_rule=True),
        lambda x: x["exceptions"].append(deepcopy(x["exceptions"][0])),
    ],
)
def test_invalid_unknown_conflicting_or_unsupported_input_is_rejected(change):
    data = synthetic()
    change(data)
    with pytest.raises(CalendarSnapshotError):
        CalendarSnapshot(data, calendar_ref="synthetic-v1")
