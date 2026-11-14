"""Protocol-only fake /2 provenance tests; no SDK dependency or authentication."""

import hashlib
from copy import deepcopy
from datetime import UTC, date, datetime
from types import SimpleNamespace

import pytest
from bt_api_base.trading_session_protocol import TradingPhase

from bt_api_ctp.simulation_calendar_ports import (
    SimulationCalendarUnavailable,
    SimulationMapping,
    SimulationOwnerClosure,
    create_prepared_simulation_calendar_ports,
)

DAY = date(2026, 6, 2)
MAP = SimulationMapping("declared.p", "SHFE", "future", "p", "mock_p")
MOCK = "MOCK.simulation_only"


def z(value):
    return value.isoformat().replace("+00:00", "Z")


class FakeView:
    def __init__(self, case=None):
        self.case = case
        self._status = SimpleNamespace(
            mode="SIMULATION_ONLY",
            route="route",
            source_generation="gen",
            calendar_ref="fake-v1",
            publication_id="pub",
            content_sha256="a" * 64,
            coverage_manifest_sha256="b" * 64,
            binding_sha256="c" * 64,
            version_sha256="d" * 64,
            bound_trading_day=DAY,
            last_sequence=1,
            coverage_status="DECLARED_UNVERIFIED",
            load_error=None,
            data_origin="UNSPECIFIED",
            declared_coverage=(
                SimpleNamespace(
                    venue="SHFE", start_day=date(2026, 1, 1), end_day=date(2026, 12, 31)
                ),
            ),
        )

    def status(self):
        return self._status

    def query_venue_day(self, *, mode, venue, trading_day):
        return SimpleNamespace(
            binding=self._status,
            status="COMPLETE",
            mode=mode,
            venue=venue,
            trading_day=trading_day,
            day_status="open" if trading_day == DAY else "closed",
            source_ids=(MOCK,),
            source_authentication="NOT_PROVEN",
            trade_authorization="NONE",
        )

    def query(self, *, venue, kind, product, contract, trading_day):
        start = datetime(2026, 6, 2, 1, tzinfo=UTC)
        first = datetime(2026, 6, 2, 2, 0, 0, 1, tzinfo=UTC)
        last = datetime(2026, 6, 2, 2, 30, 0, 2, tzinfo=UTC)
        end = datetime(2026, 6, 2, 3, tzinfo=UTC)
        values = (
            venue,
            kind,
            product,
            contract,
            trading_day.isoformat(),
            "day",
            first.isoformat(),
            last.isoformat(),
        )
        digest = hashlib.sha256("\0".join(values).encode()).hexdigest()
        ids = (f"interrupt.{digest}.pre", f"interrupt.{digest}.post")
        windows = tuple(
            SimpleNamespace(
                window_id=ident, phase="continuous", trading_day=trading_day, start_utc=a, end_utc=b
            )
            for ident, a, b in ((ids[0], start, first), (ids[1], last, end))
        )
        interruption = {
            "start_utc": z(first),
            "end_utc": z(last),
            "source_id": MOCK,
            "source_authentication": "NOT_PROVEN",
        }
        rows = [
            {
                "scope": "DAY",
                "window_id": ident,
                "source_type": "MOCK",
                "source_id": MOCK,
                "source_authentication": "NOT_PROVEN",
                "declaration": "PINNED_RULE_AND_OWNER_DAY",
                "original_window_id": "day",
                "segment": side,
                "parent_start_utc": z(start),
                "parent_end_utc": z(end),
                "interruption": deepcopy(interruption),
                "trading_day": trading_day.isoformat(),
                "phase": "continuous",
                "start_utc": z(a),
                "end_utc": z(b),
            }
            for ident, side, a, b in ((ids[0], "PRE", start, first), (ids[1], "POST", last, end))
        ]
        proof = {
            "schema": "bt_api.calendar-declared-mock-windows/2",
            "environment": "SIMULATION_ONLY",
            "binding": {
                "calendar_ref": "fake-v1",
                "version_sha256": "d" * 64,
                "content_sha256": "a" * 64,
            },
            "scope": {
                "venue": venue,
                "kind": kind,
                "product": product,
                "contract": contract,
                "trading_day": trading_day.isoformat(),
            },
            "windows": rows,
        }
        if self.case == "version":
            proof["binding"]["version_sha256"] = "0" * 64
        if self.case == "content":
            proof["binding"]["content_sha256"] = "0" * 64
        if self.case == "ref":
            proof["binding"]["calendar_ref"] = "other"
        if self.case == "scope":
            proof["scope"]["venue"] = "DCE"
        if self.case == "owner":
            rows[0]["trading_day"] = "2026-06-03"
        if self.case == "phase":
            rows[0]["phase"] = "auction"
        if self.case == "source":
            rows[0]["interruption"]["source_id"] = "other"
        if self.case == "source_status":
            rows[0]["source_authentication"] = "PROVEN"
        if self.case == "first_edge":
            rows[0]["end_utc"] = z(first.replace(microsecond=0))
        if self.case == "gap":
            rows[1]["interruption"]["end_utc"] = z(last.replace(microsecond=0))
        if self.case == "segment":
            rows[1]["segment"] = "PRE"
        if self.case == "missing":
            rows.pop()
        if self.case == "extra":
            rows[0]["extra"] = True
        if self.case == "id":
            rows[0]["window_id"] = "other"
            windows[0].window_id = "other"
        result = SimpleNamespace(
            status="COMPLETE",
            calendar_ref="fake-v1",
            sources_verified=False,
            venue=venue,
            kind=kind,
            product=product,
            contract=contract,
            trading_day=trading_day,
            source_ids=(MOCK,),
            windows=windows,
        )
        return SimpleNamespace(
            binding=self._status,
            result=result,
            simulation_provenance=None,
            declared_mock_provenance=proof,
        )


def prepared(view):
    closure = SimulationOwnerClosure(
        "d" * 64,
        datetime(2026, 6, 2, 0, tzinfo=UTC),
        datetime(2026, 6, 2, 4, tzinfo=UTC),
        DAY,
        DAY,
        0,
        0,
    )
    return create_prepared_simulation_calendar_ports(
        mode="SIMULATION_ONLY",
        view=view,
        mappings=(MAP,),
        closure=closure,
        required_instruments=(MAP.instrument_id,),
        required_years=(("SHFE", 2026),),
    )


def test_fake_protocol_prepared_checks_pair_and_remains_unverified():
    pair = prepared(FakeView())
    assert pair.identity.source_authentication == "NOT_PROVEN"
    assert pair.expected_days.expected_trading_days("SHFE", 2026) == (DAY,)
    stamp = datetime(2026, 6, 2, 2, 0, 0, 1, tzinfo=UTC)
    assert pair.schedule.session_state(MAP.instrument_id, stamp).phase is TradingPhase.CLOSED
    assert (
        pair.schedule.session_state(
            MAP.instrument_id, datetime(2026, 6, 2, 2, 30, 0, 2, tzinfo=UTC)
        ).phase
        is TradingPhase.CONTINUOUS
    )


@pytest.mark.parametrize(
    "case",
    [
        "version",
        "content",
        "ref",
        "scope",
        "owner",
        "phase",
        "source",
        "source_status",
        "first_edge",
        "gap",
        "segment",
        "missing",
        "extra",
        "id",
    ],
)
def test_fake_protocol_inconsistent_proof_rejects_prepared(case):
    with pytest.raises(SimulationCalendarUnavailable):
        prepared(FakeView(case))
