"""Explicit scoped projection checks with synthetic venue data only."""

from copy import deepcopy
from datetime import date, timedelta
from time import perf_counter

import pytest

from bt_api_ctp.calendar_scope_projection import _bound_full, prepare_venue_scope
from bt_api_ctp.calendar_semantic_diff import CalendarScope, _bounded


def six_venues_32_open_contracts_two_years():
    venues = ("SHFE", "INE", "DCE", "CZCE", "CFFEX", "GFEX")
    start = date(2026, 1, 1)
    days = [start + timedelta(days=i) for i in range(730)]
    content = {
        "sources": [{"id": "synthetic", "description": "fictional"}],
        "trading_days": [],
        "trading_hours": [],
        "exceptions": [],
    }
    for venue in venues:
        content["trading_days"].extend(
            {"venue": venue, "day": day.isoformat(), "status": "open", "source_ids": ["synthetic"]}
            for day in days
        )
        for n in range(32):
            content["trading_hours"].append(
                {
                    "venue": venue,
                    "kind": "future" if n < 16 else "option",
                    "product": f"p{n}",
                    "contracts": [f"{venue.lower()}contract{n}"],
                    "valid_from": days[0].isoformat(),
                    "valid_to": days[-1].isoformat(),
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
                    "night_mappings": [
                        {
                            "trading_day": day.isoformat(),
                            "start_date": None,
                            "source_ids": ["synthetic"],
                        }
                        for day in days
                    ],
                    "source_ids": ["synthetic"],
                    "windows_complete": True,
                    "exceptions_complete": True,
                }
            )
    return content


def test_two_year_32_contract_per_venue_explicit_mapping_budget():
    content = six_venues_32_open_contracts_two_years()
    started = perf_counter()
    scope = CalendarScope("SHFE", "future", "p0", "shfecontract0")
    result = prepare_venue_scope(
        content, scope=scope, start=date(2026, 1, 1), end=date(2027, 12, 31)
    )
    elapsed = perf_counter() - started
    print(f"six venues x 730 open days x 32 mappings: {result.status}, {elapsed:.3f}s")
    assert result.status == "UNKNOWN"
    # This conservative budget is an intentional current limitation; do not
    # raise it solely to make a synthetic test pass.
    venue = {"sources": content["sources"]}
    for layer in ("trading_days", "trading_hours", "exceptions"):
        venue[layer] = [row for row in content[layer] if row["venue"] == "SHFE"]
    with pytest.raises(ValueError, match="size limit"):
        _bound_full(content)
    with pytest.raises(ValueError, match="budget"):
        _bounded(venue)


def test_complete_venue_rejected_before_selecting_other_contract():
    content = six_venues_32_open_contracts_two_years()
    content["trading_days"] = [row for row in content["trading_days"] if row["day"] == "2026-01-01"]
    for row in content["trading_hours"]:
        row["valid_to"] = "2026-01-01"
        row["night_mappings"] = row["night_mappings"][:1]
    content["trading_days"].append(deepcopy(content["trading_days"][0]))
    scope = CalendarScope("SHFE", "future", "p0", "shfecontract0")
    assert (
        prepare_venue_scope(
            content, scope=scope, start=date(2026, 1, 1), end=date(2026, 1, 1)
        ).status
        == "UNKNOWN"
    )


def test_two_year_32_contracts_shared_product_rules():
    venues = ("SHFE", "INE", "DCE", "CZCE", "CFFEX", "GFEX")
    days = [date(2026, 1, 1) + timedelta(days=i) for i in range(730)]
    content = {
        "sources": [{"id": "synthetic", "description": "fictional"}],
        "trading_days": [],
        "trading_hours": [],
        "exceptions": [],
    }
    for venue in venues:
        content["trading_days"].extend(
            {"venue": venue, "day": day.isoformat(), "status": "open", "source_ids": ["synthetic"]}
            for day in days
        )
        for kind in ("future", "option"):
            content["trading_hours"].append(
                {
                    "venue": venue,
                    "kind": kind,
                    "product": f"p{kind}",
                    "contracts": [f"{venue.lower()}{kind}{i}" for i in range(16)],
                    "valid_from": days[0].isoformat(),
                    "valid_to": days[-1].isoformat(),
                    "day_windows": [
                        {
                            "id": "day",
                            "phase": "continuous",
                            "start": "09:00",
                            "end": "10:00",
                            "end_day_offset": 0,
                        }
                    ],
                    "night_windows": [
                        {
                            "id": "night",
                            "phase": "continuous",
                            "start": "21:00",
                            "end": "23:00",
                            "end_day_offset": 0,
                        }
                    ],
                    "night_mappings": [
                        {
                            "trading_day": day.isoformat(),
                            "start_date": (day - timedelta(days=1)).isoformat(),
                            "source_ids": ["synthetic"],
                        }
                        for day in days
                    ],
                    "source_ids": ["synthetic"],
                    "windows_complete": True,
                    "exceptions_complete": True,
                }
            )
    venue = {"sources": content["sources"]}
    for layer in ("trading_days", "trading_hours", "exceptions"):
        venue[layer] = [row for row in content[layer] if row["venue"] == "SHFE"]
    work = _bounded(venue)
    scope = CalendarScope("SHFE", "future", "pfuture", "shfefuture0")
    started = perf_counter()
    before = prepare_venue_scope(content, scope=scope, start=days[0], end=days[-1])
    after = prepare_venue_scope(content, scope=scope, start=days[0], end=days[-1])
    elapsed = perf_counter() - started
    print(
        f"six venues x 730 days x 32 contracts grouped by two rules: {before.status}/{after.status}, {elapsed:.3f}s, work={work}"
    )
    assert before.status == after.status == "READY_UNVERIFIED"
