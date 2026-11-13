"""Offline composition against SDK views; no account, network or mail access."""

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace

import pytest
from bt_api_base.trading_session_protocol import TradingPhase

from bt_api_ctp.simulation_calendar_ports import (
    SimulationCalendarUnavailable,
    SimulationMapping,
    SimulationOwnerClosure,
    create_simulation_calendar_ports,
)

START = datetime(2027, 1, 1, 12, tzinfo=UTC)
END = datetime(2027, 1, 1, 15, 30, tzinfo=UTC)
DAY = date(2027, 1, 4)
MAPPING = SimulationMapping("declared.rb", "SHFE", "future", "pfuture", "mock_shfe_future_contract")
OPTION = SimulationMapping(
    "declared.option", "SHFE", "option", "poption", "mock_shfe_option_contract"
)


def compose(view, mappings=(MAPPING, OPTION), **changes):
    values = dict(
        version_sha256=view.status().version_sha256,
        start_utc=START,
        end_utc=END,
        owner_start=date(2027, 1, 1),
        owner_end=DAY,
        max_advance_days=3,
        max_spill_days=0,
    )
    values.update(changes)
    return create_simulation_calendar_ports(
        mode="SIMULATION_ONLY",
        view=view,
        mappings=mappings,
        closure=SimulationOwnerClosure(**values),
    )


def setup(tmp_path):
    # Optional integration into the separately checked-out SDK, never a CTP
    # runtime dependency. Pure protocol tests below run without that checkout.
    pytest.importorskip("bt_api_py.trading_calendar_binding")
    pytest.importorskip("tests.unit.test_trading_calendar_bound_session_simulation")
    pytest.importorskip("tests.unit.test_trading_calendar_runtime_mixed_query")
    from bt_api_py.trading_calendar_store import TradingCalendarStore
    from tests.unit.test_trading_calendar_bound_session_simulation import bind
    from tests.unit.test_trading_calendar_runtime_mixed_query import publish

    publish(tmp_path, mixed_version=True)
    store = TradingCalendarStore(str(tmp_path))
    bound = bind(store)
    return store, bound, bound.capture_read_view(mode="SIMULATION_ONLY")


def test_real_sdk_view_both_ports_remain_pinned_across_activation(tmp_path):
    pytest.importorskip("bt_api_py.trading_calendar_binding")
    pytest.importorskip("tests.unit.test_trading_calendar_bound_session_simulation")
    from bt_api_py.trading_calendar_binding import GateDecision
    from tests.unit.test_trading_calendar_bound_session_simulation import publish_changed

    store, bound, view = setup(tmp_path)
    old = compose(view)
    at = datetime(2027, 1, 1, 13, tzinfo=UTC)
    assert old.schedule.session_state("declared.rb", at).phase is TradingPhase.CONTINUOUS
    assert old.schedule.session_state("declared.option", at).phase is TradingPhase.CONTINUOUS
    assert (
        old.schedule.session_state("declared.rb", at - timedelta(minutes=5)).phase
        is TradingPhase.CLOSED
    )
    assert (
        old.schedule.session_state("declared.rb", datetime(2027, 1, 1, 15, tzinfo=UTC)).phase
        is TradingPhase.UNKNOWN
    )
    (window,) = old.schedule.windows("declared.rb", START, END)
    assert (window.start_utc, window.end_utc, window.trading_day) == (
        at,
        datetime(2027, 1, 1, 15, tzinfo=UTC),
        DAY,
    )
    assert old.schedule.windows("declared.rb", window.end_utc, END) == ()
    assert old.identity.source_authentication == "NOT_PROVEN"
    assert (
        old.identity.identity_binding == old.identity.owner_closure == "CALLER_DECLARED_NOT_PROVEN"
    )
    assert old.identity.trade_authorization == "NONE"
    with pytest.raises(SimulationCalendarUnavailable):
        old.expected_days.expected_trading_days("SHFE", 2027)
    with pytest.raises(SimulationCalendarUnavailable):
        old.expected_days.build(None)
    assert old.expected_days.validate(None) is False
    publish_changed(tmp_path, store)
    assert (
        bound.activate(GateDecision(True, "route-a", "gen-1", DAY, 2, "switch")).outcome
        == "ACTIVATED"
    )
    new = compose(bound.capture_read_view(mode="SIMULATION_ONLY"))
    assert (
        old.identity.calendar_ref
        == old.schedule.session_state("declared.rb", at).calendar_ref
        == "MOCK_ver"
    )
    assert (
        new.identity.calendar_ref
        == new.schedule.session_state("declared.rb", at).calendar_ref
        == "MOCK_ver2"
    )
    assert old.identity.version_sha256 != new.identity.version_sha256


def test_scope_and_closure_are_explicit_and_bounded(tmp_path):
    _, _, view = setup(tmp_path)
    with pytest.raises(SimulationCalendarUnavailable):
        compose(view, owner_end=date(2027, 1, 3))
    with pytest.raises(SimulationCalendarUnavailable):
        compose(view, version_sha256="0" * 64)
    with pytest.raises(SimulationCalendarUnavailable):
        compose(view, max_advance_days=8)
    with pytest.raises(SimulationCalendarUnavailable):
        compose(view, mappings=(replace(MAPPING, mock_contract="rb2701"),))
    ports = compose(view)
    with pytest.raises(SimulationCalendarUnavailable):
        ports.schedule.windows("real-rb2701", START, END)
    with pytest.raises(SimulationCalendarUnavailable):
        ports.schedule.windows("declared.rb", START - timedelta(seconds=1), END)
    assert ports.schedule.session_state("real-rb2701", START).phase is TradingPhase.UNKNOWN


class ViewProxy:
    def __init__(self, view, day_change=None, query_change=None):
        self.view, self.day_change, self.query_change = view, day_change, query_change
        self.calls = 0

    def status(self):
        return self.view.status()

    def query_venue_day(self, **kwargs):
        row = self.view.query_venue_day(**kwargs)
        return self.day_change(row, kwargs) if self.day_change else row

    def query(self, **kwargs):
        self.calls += 1
        row = self.view.query(**kwargs)
        return self.query_change(row, kwargs) if self.query_change else row


@pytest.mark.parametrize(
    "field,wrong",
    [
        ("venue", "DCE"),
        ("kind", "option"),
        ("product", "wrong"),
        ("contract", "mock_wrong"),
        ("trading_day", date(2027, 1, 3)),
    ],
)
def test_fake_provider_scope_mismatch_is_unknown(tmp_path, field, wrong):
    _, _, view = setup(tmp_path)

    def altered(row, scope):
        if row.result.status == "COMPLETE":
            return replace(row, result=replace(row.result, **{field: wrong}))
        return row

    ports = compose(ViewProxy(view, query_change=altered))
    with pytest.raises(SimulationCalendarUnavailable):
        ports.schedule.windows("declared.rb", START, END)
    assert ports.schedule.session_state("declared.rb", START).phase is TradingPhase.UNKNOWN


@pytest.mark.parametrize("case", ["missing", "phase", "target", "locator", "time", "source"])
def test_missing_or_forged_window_evidence_fails_closed(tmp_path, case):
    _, _, view = setup(tmp_path)

    def altered(row, scope):
        p = row.simulation_provenance
        if not p or not p["windows"]:
            return row
        if case == "missing":
            p["windows"].pop()
        elif case == "phase":
            p["windows"][0]["simulation"]["phase"]["basis"] = "AKSHARE_SYNTAX_CANDIDATE"
        elif case == "target":
            p["target"]["mock_contract"] = "mock_other"
        elif case == "locator":
            del p["evidence_locator"]["response_digest"]
        elif case == "time":
            p["windows"][0]["fields"]["start"]["value"] = "12:34"
        else:
            p["windows"][0]["source_type"] = "wrong"
        return row

    ports = compose(ViewProxy(view, query_change=altered))
    with pytest.raises(SimulationCalendarUnavailable):
        ports.schedule.windows("declared.rb", START, END)


def test_no_unknown_result_is_cached_and_success_is_cached(tmp_path):
    _, _, view = setup(tmp_path)
    state = {"bad": True}

    def altered(row, scope):
        return replace(row, status="UNKNOWN") if state["bad"] else row

    proxy = ViewProxy(view, day_change=altered)
    ports = compose(proxy)
    with pytest.raises(SimulationCalendarUnavailable):
        ports.schedule.windows("declared.rb", START, END)
    state["bad"] = False
    ports.schedule.windows("declared.rb", START, END)
    count = proxy.calls
    for _ in range(50):
        ports.schedule.session_state("declared.rb", datetime(2027, 1, 1, 13, tzinfo=UTC))
    assert proxy.calls == count


def simulated_store(tmp_path, data, start, end):
    import json

    pytest.importorskip("bt_api_py.trading_calendar_binding")
    pytest.importorskip("tests.calendar_test_store")
    pytest.importorskip("tests.unit.test_trading_calendar_binding")
    from bt_api_py.trading_calendar_binding import SimulationCalendarBinding
    from bt_api_py.trading_calendar_composite import compose_simulated
    from bt_api_py.trading_calendar_update_simulation import VenueContent
    from tests.calendar_test_store import FixtureCalendarStore
    from tests.unit.test_trading_calendar_binding import FakeGate

    from bt_api_ctp.calendar_snapshot import CalendarSnapshot

    group = VenueContent.from_layers("SHFE", start, end, data)
    bundle = compose_simulated(
        mode="SIMULATION_ONLY", venues=(group,), snapshot_factory=CalendarSnapshot
    )
    assert bundle.status == "READY_SIMULATED", bundle
    store = FixtureCalendarStore(tmp_path)
    result = store.publish(
        json.loads(bundle.snapshot_bytes),
        publication_id="pub-year",
        version_id="v-year",
        coverage_manifest_bytes=bundle.manifest_bytes,
        coverage_manifest_sha256=bundle.manifest_sha256,
        binding_bytes=bundle.binding_bytes,
        binding_sha256=bundle.binding_sha256,
    )
    assert result.visibility == "COMMITTED"
    bound = SimulationCalendarBinding(
        mode="SIMULATION_ONLY",
        store=store,
        snapshot_factory=CalendarSnapshot,
        gate=FakeGate(),
        route="route",
        source_generation="gen",
        trading_day=start,
        last_sequence=1,
    )
    return bound.capture_read_view(mode="SIMULATION_ONLY")


def simulated_content(start, end, open_days):
    dates = [start + timedelta(days=i) for i in range((end - start).days + 1)]
    return {
        "sources": [{"id": "MOCK.simulation_only", "description": "fictional"}],
        "trading_days": [
            {
                "venue": "SHFE",
                "day": d.isoformat(),
                "status": "open" if d in open_days else "closed",
                "source_ids": ["MOCK.simulation_only"],
            }
            for d in dates
        ],
        "trading_hours": [
            {
                "venue": "SHFE",
                "kind": "future",
                "product": "pfuture",
                "contracts": ["mock_shfe_future_contract"],
                "valid_from": start.isoformat(),
                "valid_to": end.isoformat(),
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
                        "trading_day": d.isoformat(),
                        "start_date": None,
                        "source_ids": ["MOCK.simulation_only"],
                    }
                    for d in dates
                    if d in open_days
                ],
                "windows_complete": True,
                "exceptions_complete": True,
                "source_ids": ["MOCK.simulation_only"],
            }
        ],
        "exceptions": [],
    }


@pytest.mark.parametrize("year", [2026, 2028])
def test_full_year_explicit_days_only_and_missing_day_not_closed(tmp_path, year):
    first, last = date(year, 1, 1), date(year, 12, 31)
    open_day = date(year, 6, 1)
    view = simulated_store(tmp_path, simulated_content(first, last, {open_day}), first, last)
    start = datetime(year, 6, 1, 1, tzinfo=UTC)
    proxy = MutableStatusView(view)
    ports = compose(
        proxy,
        mappings=(MAPPING,),
        start_utc=start,
        end_utc=start + timedelta(hours=1),
        owner_start=open_day,
        owner_end=open_day,
        max_advance_days=0,
    )
    assert ports.expected_days.expected_trading_days("SHFE", year) == (open_day,)
    proxy.current = replace(proxy.current, data_origin="AKSHARE_MOCK")
    with pytest.raises(SimulationCalendarUnavailable):
        ports.expected_days.expected_trading_days("SHFE", year)
    calls = []

    def bad_day(row, scope):
        calls.append(scope["trading_day"])
        if scope["trading_day"] == date(year, 3, 1):
            return replace(row, status="UNKNOWN")
        return row

    bad = compose(
        ViewProxy(view, day_change=bad_day),
        mappings=(MAPPING,),
        start_utc=start,
        end_utc=start + timedelta(hours=1),
        owner_start=open_day,
        owner_end=open_day,
        max_advance_days=0,
    )
    with pytest.raises(SimulationCalendarUnavailable):
        bad.expected_days.expected_trading_days("SHFE", year)
    with pytest.raises(SimulationCalendarUnavailable):
        bad.expected_days.expected_trading_days("SHFE", year)
    assert calls.count(date(year, 3, 1)) == 2


def test_actual_snapshot_cross_midnight_and_closure_exception(tmp_path):
    first, last = date(2026, 6, 5), date(2026, 6, 7)
    data = simulated_content(first, last, {first, first + timedelta(days=1), last})
    rule = data["trading_hours"][0]
    rule["night_windows"] = [
        {
            "id": "night",
            "phase": "continuous",
            "start": "21:00",
            "end": "01:00",
            "end_day_offset": 1,
        }
    ]
    rule["night_mappings"][1]["start_date"] = first.isoformat()
    rule["night_mappings"][2]["start_date"] = (first + timedelta(days=1)).isoformat()
    data["exceptions"] = [
        {
            "venue": "SHFE",
            "kind": "future",
            "product": "pfuture",
            "contract": "mock_shfe_future_contract",
            "trading_day": first.isoformat(),
            "action": "close_day",
            "window_id": None,
            "source_ids": ["MOCK.simulation_only"],
        }
    ]
    view = simulated_store(tmp_path, data, first, last)
    start, end = datetime(2026, 6, 5, 12, tzinfo=UTC), datetime(2026, 6, 5, 18, tzinfo=UTC)
    ports = compose(
        view,
        mappings=(MAPPING,),
        start_utc=start,
        end_utc=end,
        owner_start=first,
        owner_end=last,
        max_advance_days=1,
    )
    windows = ports.schedule.windows("declared.rb", start, end)
    assert len(windows) == 1
    night = windows[0]
    assert (night.start_utc, night.end_utc, night.trading_day) == (
        datetime(2026, 6, 5, 13, tzinfo=UTC),
        datetime(2026, 6, 5, 17, tzinfo=UTC),
        date(2026, 6, 6),
    )
    assert (
        ports.schedule.session_state("declared.rb", night.start_utc).phase
        is TradingPhase.CONTINUOUS
    )
    assert ports.schedule.session_state("declared.rb", night.end_utc).phase is TradingPhase.UNKNOWN
    # The day-level exception cancels Friday day without changing venue-day open.
    assert (
        view.query_venue_day(mode="SIMULATION_ONLY", venue="SHFE", trading_day=first).day_status
        == "open"
    )
    assert (
        view.query(
            venue="SHFE",
            kind="future",
            product="pfuture",
            contract="mock_shfe_future_contract",
            trading_day=first,
        ).result.windows
        == ()
    )


def test_standalone_fake_view_contract_and_cache_without_sdk():
    from threading import Thread

    d = date(2026, 1, 2)
    status = SimpleNamespace(
        mode="SIMULATION_ONLY",
        route="route",
        source_generation="generation",
        calendar_ref="v1",
        publication_id="pub1",
        content_sha256="a" * 64,
        coverage_manifest_sha256="b" * 64,
        binding_sha256="c" * 64,
        version_sha256="d" * 64,
        bound_trading_day=d,
        last_sequence=1,
        coverage_status="DECLARED_UNVERIFIED",
        load_error=None,
        data_origin="UNSPECIFIED",
        declared_coverage=(
            SimpleNamespace(venue="SHFE", start_day=date(2026, 1, 1), end_day=date(2026, 12, 31)),
        ),
    )
    window = SimpleNamespace(
        window_id="one",
        phase="continuous",
        trading_day=d,
        start_utc=datetime(2026, 1, 2, 1, tzinfo=UTC),
        end_utc=datetime(2026, 1, 2, 2, tzinfo=UTC),
    )

    class Fake:
        calls = 0

        def status(self):
            return status

        def query_venue_day(self, *, mode, venue, trading_day):
            return SimpleNamespace(
                binding=status,
                status="COMPLETE",
                mode=mode,
                venue=venue,
                trading_day=trading_day,
                day_status="open" if trading_day == d else "closed",
                source_ids=("MOCK.simulation_only",),
                source_authentication="NOT_PROVEN",
                trade_authorization="NONE",
            )

        def query(self, *, venue, kind, product, contract, trading_day):
            self.calls += 1
            result = SimpleNamespace(
                status="COMPLETE",
                calendar_ref="v1",
                sources_verified=False,
                venue=venue,
                kind=kind,
                product=product,
                contract=contract,
                trading_day=trading_day,
                source_ids=("MOCK.simulation_only",),
                windows=(window,),
            )
            provenance = {
                "schema": "bt_api.calendar-declared-mock-windows/1",
                "environment": "SIMULATION_ONLY",
                "windows": [
                    {
                        "window_id": "one",
                        "scope": "DAY",
                        "source_type": "MOCK",
                        "source_id": "MOCK.simulation_only",
                        "source_authentication": "NOT_PROVEN",
                    }
                ],
            }
            return SimpleNamespace(
                binding=status,
                result=result,
                simulation_provenance=None,
                declared_mock_provenance=provenance,
            )

    view = Fake()
    start, end = datetime(2026, 1, 2, 0, tzinfo=UTC), datetime(2026, 1, 2, 3, tzinfo=UTC)
    ports = compose(
        view,
        mappings=(MAPPING,),
        start_utc=start,
        end_utc=end,
        owner_start=d,
        owner_end=d,
        max_advance_days=0,
    )
    assert ports.expected_days.expected_trading_days("SHFE", 2026) == (d,)
    results = []
    workers = [
        Thread(target=lambda: results.append(ports.schedule.windows("declared.rb", start, end)))
        for _ in range(8)
    ]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(5)
    assert len(results) == 8 and view.calls == 1
    assert (
        ports.schedule.session_state("declared.rb", window.start_utc).phase
        is TradingPhase.CONTINUOUS
    )
    assert ports.schedule.session_state("declared.rb", start).phase is TradingPhase.CLOSED
    assert ports.schedule.session_state("declared.rb", window.end_utc).phase is TradingPhase.UNKNOWN


@pytest.mark.parametrize(
    "field,delta",
    [
        ("start_utc", timedelta(seconds=1)),
        ("start_utc", timedelta(microseconds=1)),
        ("end_utc", timedelta(seconds=1)),
        ("end_utc", timedelta(microseconds=1)),
    ],
)
def test_mixed_window_rejects_subminute_provider_time(tmp_path, field, delta):
    _, _, view = setup(tmp_path)

    def altered(row, scope):
        if row.simulation_provenance and row.result.windows:
            window = row.result.windows[0]
            changed = replace(window, **{field: getattr(window, field) + delta})
            return replace(
                row, result=replace(row.result, windows=(changed,) + row.result.windows[1:])
            )
        return row

    ports = compose(ViewProxy(view, query_change=altered))
    with pytest.raises(SimulationCalendarUnavailable) as info:
        ports.schedule.windows("declared.rb", START, END)
    assert info.value.reason == "SIMULATION_WINDOW_SOURCE_UNAVAILABLE"
    assert ports.schedule.session_state("declared.rb", START).phase is TradingPhase.UNKNOWN


class MutableStatusView(ViewProxy):
    def __init__(self, view, **kwargs):
        super().__init__(view, **kwargs)
        self.current = view.status()

    def status(self):
        return self.current


@pytest.mark.parametrize(
    "change", ["coverage", "coverage_type", "duplicate", "origin", "origin_type"]
)
def test_cached_and_cold_view_reject_changed_scope_identity(tmp_path, change):
    _, _, view = setup(tmp_path)
    warm_proxy = MutableStatusView(view)
    warm = compose(warm_proxy)
    warm.schedule.windows("declared.rb", START, END)
    cold_proxy = MutableStatusView(view)
    cold = compose(cold_proxy)

    def changed(status):
        if change == "coverage":
            first = status.declared_coverage[0]
            return replace(
                status,
                declared_coverage=(replace(first, end_day=first.start_day),)
                + status.declared_coverage[1:],
            )
        if change == "coverage_type":
            return replace(status, declared_coverage=list(status.declared_coverage))
        if change == "duplicate":
            return replace(
                status, declared_coverage=status.declared_coverage + (status.declared_coverage[0],)
            )
        if change == "origin":
            return replace(status, data_origin="UNSPECIFIED")
        return replace(status, data_origin=123)

    warm_proxy.current = changed(warm_proxy.current)
    cold_proxy.current = changed(cold_proxy.current)
    for ports in (warm, cold):
        with pytest.raises(SimulationCalendarUnavailable):
            ports.schedule.windows("declared.rb", START, END)
        assert ports.schedule.session_state("declared.rb", START).phase is TradingPhase.UNKNOWN


def test_changed_scope_during_query_cannot_populate_cache(tmp_path):
    from threading import Event, Thread

    _, _, view = setup(tmp_path)
    entered, release = Event(), Event()

    def paused(row, scope):
        entered.set()
        assert release.wait(5)
        return row

    proxy = MutableStatusView(view, query_change=paused)
    ports = compose(proxy)
    outcomes = []

    def read():
        try:
            ports.schedule.windows("declared.rb", START, END)
        except SimulationCalendarUnavailable as exc:
            outcomes.append(exc.reason)

    worker = Thread(target=read)
    worker.start()
    try:
        assert entered.wait(5)
        proxy.current = replace(proxy.current, data_origin="UNSPECIFIED")
    finally:
        release.set()
        worker.join(5)
    assert not worker.is_alive() and outcomes == ["SIMULATION_BINDING_CHANGED"]
    assert ports.schedule.session_state("declared.rb", START).phase is TradingPhase.UNKNOWN


def test_base_unavailable_code_and_finite_sanitized_reason(tmp_path):
    import traceback

    _, _, view = setup(tmp_path)
    ports = compose(view)
    with pytest.raises(SimulationCalendarUnavailable) as info:
        ports.expected_days.build(None)
    assert info.value.code == str(info.value) == "FUTURES_EXPECTED_CALENDAR_PROVIDER_UNAVAILABLE"
    assert info.value.reason == "SIMULATION_RESEARCH_BUILD_UNAVAILABLE"
    unsafe = SimulationCalendarUnavailable("/private/secret")
    assert unsafe.code == "FUTURES_EXPECTED_CALENDAR_PROVIDER_UNAVAILABLE"
    assert unsafe.reason == "SIMULATION_UNAVAILABLE"
    assert "secret" not in str(unsafe)

    def broken(row, scope):
        raise RuntimeError("/private/secret")

    bad = compose(ViewProxy(view, day_change=broken))
    with pytest.raises(SimulationCalendarUnavailable) as info:
        bad.schedule.windows("declared.rb", START, END)
    assert info.value.reason == "SIMULATION_DAY_UNAVAILABLE"
    assert "secret" not in "".join(traceback.format_exception(info.value))


def test_standalone_identity_snapshot_and_error_code_without_sdk():
    from bt_api_ctp.simulation_calendar_ports import _identity

    day = date(2026, 1, 1)
    coverage = SimpleNamespace(venue="SHFE", start_day=day, end_day=day)
    status = SimpleNamespace(
        mode="SIMULATION_ONLY",
        route="route",
        source_generation="generation",
        calendar_ref="v1",
        publication_id="p1",
        content_sha256="a" * 64,
        coverage_manifest_sha256="b" * 64,
        binding_sha256="c" * 64,
        version_sha256="d" * 64,
        bound_trading_day=day,
        last_sequence=1,
        coverage_status="DECLARED_UNVERIFIED",
        load_error=None,
        data_origin="UNSPECIFIED",
        declared_coverage=(coverage,),
    )
    old = _identity(status)
    coverage.end_day = date(2026, 1, 2)
    assert _identity(status) != old
    status.data_origin = "AKSHARE_MOCK"
    assert _identity(status) != old
    status.declared_coverage = (coverage, coverage)
    with pytest.raises(SimulationCalendarUnavailable):
        _identity(status)
    exc = SimulationCalendarUnavailable("private-path-value")
    assert exc.code == "FUTURES_EXPECTED_CALENDAR_PROVIDER_UNAVAILABLE"
    assert exc.reason == "SIMULATION_UNAVAILABLE" and "private" not in str(exc)


@pytest.mark.parametrize("field", ["start_utc", "end_utc"])
@pytest.mark.parametrize("delta", [timedelta(seconds=1), timedelta(microseconds=1)])
def test_standalone_mixed_time_precision_without_sdk(field, delta):
    from bt_api_ctp.simulation_calendar_ports import _window_sources

    day = date(2026, 1, 1)
    values = {
        "window_id": "day",
        "phase": "continuous",
        "trading_day": day,
        "start_utc": datetime(2026, 1, 1, 1, tzinfo=UTC),
        "end_utc": datetime(2026, 1, 1, 2, tzinfo=UTC),
    }
    window = SimpleNamespace(**values)
    fields = {
        "scope": {"value": "DAY", "basis": "AKSHARE_SYNTAX_CANDIDATE"},
        "start": {"value": "09:00", "basis": "AKSHARE_SYNTAX_CANDIDATE"},
        "end": {"value": "10:00", "basis": "AKSHARE_SYNTAX_CANDIDATE"},
        "day_transition": {"value": "SAME_CLOCK_DAY", "basis": "AKSHARE_SYNTAX_CANDIDATE"},
    }
    provenance = {
        "schema": "bt_api.calendar-mixed-window-provenance/2",
        "environment": "SIMULATION_ONLY",
        "target": {
            "venue": MAPPING.venue,
            "kind": MAPPING.kind,
            "product": MAPPING.product,
            "mock_contract": MAPPING.mock_contract,
            "valid_from": "2026-01-01",
            "valid_to": "2026-01-01",
        },
        "evidence_locator": {
            "candidate_id": "a",
            "parent_candidate_id": "b",
            "observation_id": "c",
            "observation_run_id": "d",
            "capture_id": "e",
            "response_digest": "f" * 64,
        },
        "windows": [
            {
                "window_id": "day",
                "source_type": "AKSHARE",
                "scope": "DAY",
                "fields": fields,
                "simulation": {"phase": {"value": "continuous", "basis": "MOCK_ASSUMPTION"}},
            }
        ],
    }
    query = SimpleNamespace(simulation_provenance=provenance, declared_mock_provenance=None)
    result = SimpleNamespace(
        windows=(window,), trading_day=day, source_ids=("AKSHARE.simulation_unverified",)
    )
    _window_sources(query, result, MAPPING)
    setattr(window, field, getattr(window, field) + delta)
    with pytest.raises(SimulationCalendarUnavailable):
        _window_sources(query, result, MAPPING)
