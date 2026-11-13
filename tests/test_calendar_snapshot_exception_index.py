"""Independent synthetic exception-order checks; no exchange facts implied."""

from copy import deepcopy
from datetime import date

import pytest

from bt_api_ctp.calendar_snapshot import CalendarSnapshot, CalendarSnapshotError

DAY1 = "2026-06-01"
DAY2 = "2026-06-02"


def fixture():
    sources = ["sim"]

    def rule(venue, kind, product, contracts):
        return {
            "venue": venue,
            "kind": kind,
            "product": product,
            "contracts": contracts,
            "valid_from": DAY1,
            "valid_to": DAY2,
            "day_windows": [
                {
                    "id": "a",
                    "phase": "auction",
                    "start": "08:55",
                    "end": "09:00",
                    "end_day_offset": 0,
                },
                {
                    "id": "b",
                    "phase": "continuous",
                    "start": "09:00",
                    "end": "10:00",
                    "end_day_offset": 0,
                },
            ],
            "night_windows": [],
            "night_mappings": [
                {"trading_day": day, "start_date": None, "source_ids": sources}
                for day in (DAY1, DAY2)
            ],
            "windows_complete": True,
            "exceptions_complete": True,
            "source_ids": sources,
        }

    return {
        "sources": [
            {"id": "sim", "description": "simulation"},
            {"id": "x", "description": "simulated exception"},
        ],
        "trading_days": [
            {"venue": v, "day": d, "status": "open", "source_ids": sources}
            for v in ("XSH", "XDCE")
            for d in (DAY1, DAY2)
        ],
        "trading_hours": [
            rule("XSH", "future", "cu", ["cu1", "cu2"]),
            rule("XSH", "option", "cu", ["cuopt"]),
            rule("XSH", "future", "zn", ["zn1"]),
            rule("XDCE", "future", "m", ["m1"]),
        ],
        "exceptions": [],
    }


def exception(
    *,
    venue="XSH",
    kind="future",
    product="cu",
    contract=None,
    day=DAY1,
    action="cancel_window",
    name="a",
):
    return {
        "venue": venue,
        "kind": kind,
        "product": product,
        "contract": contract,
        "trading_day": day,
        "action": action,
        "window_id": name,
        "source_ids": ["x"],
    }


def get(snapshot, *, venue="XSH", kind="future", product="cu", contract="cu1", day=DAY1):
    return snapshot.query(
        venue=venue,
        kind=kind,
        product=product,
        contract=contract,
        trading_day=date.fromisoformat(day),
    )


def test_interleaved_exceptions_preserve_scope_day_contract_and_sources():
    data = fixture()
    data["exceptions"] = [
        exception(contract=None, name="a"),
        exception(kind="option", contract="cuopt", name="b"),
        exception(contract="cu2", day=DAY2, name="b"),
        exception(venue="XDCE", product="m", contract="m1", day=DAY2, name="a"),
        exception(contract="cu2", name="b"),
        exception(product="zn", contract="zn1", day=DAY2, name="b"),
    ]
    snapshot = CalendarSnapshot(data, calendar_ref="simulation")
    cases = [
        ({}, ["b"], ("sim", "x")),
        ({"contract": "cu2"}, [], ("sim", "x")),
        ({"contract": "cu2", "day": DAY2}, ["a"], ("sim", "x")),
        ({"contract": "cu1", "day": DAY2}, ["a", "b"], ("sim",)),
        ({"kind": "option", "contract": "cuopt"}, ["a"], ("sim", "x")),
        ({"product": "zn", "contract": "zn1"}, ["a", "b"], ("sim",)),
        ({"venue": "XDCE", "product": "m", "contract": "m1", "day": DAY2}, ["b"], ("sim", "x")),
    ]
    for scope, expected, sources in cases:
        result = get(snapshot, **scope)
        assert result.status == "COMPLETE" and result.sources_verified is False
        assert [w.window_id for w in result.windows] == expected
        assert result.source_ids == sources


@pytest.mark.parametrize(
    "items,message",
    [
        ([exception(name="a"), exception(action="close_day", name=None)], "overlapping exceptions"),
        ([exception(action="close_day", name=None), exception(name="a")], "overlapping exceptions"),
        (
            [exception(name="a"), exception(contract="cu1", name="a")],
            "duplicate or unmatched exception window",
        ),
        (
            [exception(contract="cu1", name="a"), exception(name="a")],
            "duplicate or unmatched exception window",
        ),
        ([exception(name="absent")], "duplicate or unmatched exception window"),
        ([exception(contract="missing")], "exception scope or effective period is unknown"),
        ([exception(product="missing")], "exception scope or effective period is unknown"),
    ],
)
def test_conflict_order_wildcards_and_unapplied_are_not_hidden(items, message):
    data = fixture()
    data["exceptions"] = items
    with pytest.raises(CalendarSnapshotError, match=message):
        CalendarSnapshot(data, calendar_ref="simulation")


def test_unknown_out_of_scope_and_invalid_day_remain_distinct():
    data = fixture()
    data["trading_days"] = [
        row for row in data["trading_days"] if not (row["venue"] == "XSH" and row["day"] == DAY2)
    ]
    for rule in data["trading_hours"]:
        if rule["venue"] == "XSH":
            rule["night_mappings"] = [
                row for row in rule["night_mappings"] if row["trading_day"] != DAY2
            ]
    snapshot = CalendarSnapshot(data, calendar_ref="simulation")
    assert (get(snapshot, day=DAY2).status, get(snapshot, day=DAY2).reason) == (
        "UNKNOWN",
        "COVERAGE_INSUFFICIENT",
    )
    assert get(snapshot, contract="missing").status == "OUT_OF_SCOPE"
    data["trading_hours"][0]["exceptions_complete"] = False
    snapshot = CalendarSnapshot(data, calendar_ref="simulation")
    assert (get(snapshot).status, get(snapshot).reason, get(snapshot).windows) == (
        "UNKNOWN",
        "RULE_COVERAGE_INCOMPLETE",
        (),
    )
    bad = deepcopy(data)
    bad["exceptions"] = [exception(day=DAY2)]
    with pytest.raises(CalendarSnapshotError, match="exception requires known open day"):
        CalendarSnapshot(bad, calendar_ref="simulation")


def test_exception_outside_matching_rule_effective_dates_remains_unapplied():
    data = fixture()
    rule = data["trading_hours"][0]
    rule["valid_to"] = DAY1
    rule["night_mappings"] = rule["night_mappings"][:1]
    data["exceptions"] = [exception(day=DAY2)]
    with pytest.raises(
        CalendarSnapshotError, match="exception scope or effective period is unknown"
    ):
        CalendarSnapshot(data, calendar_ref="simulation")
